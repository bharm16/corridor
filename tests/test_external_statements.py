"""The External Party statement command is the sole behavioral seam for #217.

These tests exercise the record a coordinator and later readers receive, rather
than the command's helper calls or query shape.  Each fixture owns a real
Postgres transaction, matching the rest of the database test suite.
"""

from dataclasses import replace
from datetime import date
import hashlib

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScopeDecision,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def statement_record(session):
    project = Project(
        slug="external-statements-test",
        name="External statements test",
        is_synthetic=True,
    )
    # External organizations are global identities.  Other live-database tests
    # can legitimately leave this fixture actor present, so reuse it rather
    # than making the transaction fixture's isolation depend on test order.
    party = session.scalar(select(ExternalOrg).where(ExternalOrg.name == "Equistar"))
    if party is None:
        party = ExternalOrg(name="Equistar")
        session.add(party)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"statement.pdf").hexdigest(),
        filename="statement.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-1",
        dep_type="utility_relocation",
        title="Equistar relocation",
        external_org_id=party.id,
    )
    session.add_all([document, dependency])
    session.flush()
    # Each cited statement below is intentionally quoted from this one
    # fixture page.  The writer now proves that page membership itself, so
    # these are real test citations rather than free-floating strings.
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text="\n".join(
                (
                    "Equistar to provide a chain of title (Due date of 01/2025).",
                    "The March 2026 completion timeline seems unattainable. Propose extending to May 16th.",
                    "Equistar will complete relocation.",
                    "January 2025",
                    "Equistar will complete relocation by 2025-06-01.",
                    "Older cited commitment.",
                    "Equistar will complete relocation in January 2026.",
                    "The March 2026 completion timeline is unattainable.",
                    "refuse this statement evidence",
                )
            ),
        )
    )
    session.flush()
    return project, party, document, dependency


def test_human_recorded_candidate_7129_preserves_month_timing_and_unknown_scope(
    session, statement_record
):
    """Candidate 7129's January 2025 is not an invented January 1 date."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description="Equistar to provide a chain of title (Due date of 01/2025).",
        new_timing=StatementTiming.month("01/2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="local:statement-coordinator",
        evidence=CitedStatementEvidence(
            document_id=document.id,
            page_no=1,
            quote="Equistar to provide a chain of title (Due date of 01/2025).",
        ),
    )

    assert event.event_type == "commitment"
    assert event.project_id == project.id
    assert event.affected_external_org_id == party.id
    assert event.stated_external_org_id == party.id
    assert event.attribution_state == "resolved"
    assert event.scope_mode == "unknown"
    assert event.new_timing.text == "01/2025"
    assert event.new_timing.precision == "month"
    assert event.new_timing.start_date == date(2025, 1, 1)
    assert event.new_timing.end_date == date(2025, 1, 31)
    assert session.scalars(
        select(DependencyEventScope).where(DependencyEventScope.event_id == event.id)
    ).all() == []
    session.refresh(dependency)
    assert dependency.committed_date is None


def test_shared_writer_refuses_an_unregistered_page_or_quote(
    session, statement_record
):
    """A cited event can only own Evidence that the stored page proves."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    for evidence, message in (
        (CitedStatementEvidence(document.id, 2, "Equistar will complete relocation."), "not registered"),
        (CitedStatementEvidence(document.id, 1, "Invented sentence."), "not found"),
    ):
        with pytest.raises(StatementRefusal, match=message):
            record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_party=party.name,
                stated_external_org_id=party.id,
                source_kind="cited",
                event_date=None,
                description="Equistar will complete relocation.",
                new_timing=StatementTiming.month("January 2025", 2025, 1),
                scope=StatementScope.selected((dependency.id,)),
                created_by="local:statement-coordinator",
                evidence=evidence,
            )

    assert session.scalars(select(DependencyEvent)).all() == []


def test_shared_writer_refuses_non_alias_party_words_without_guided_resolution(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, _, document, _ = statement_record
    selected_party = ExternalOrg(name="Equistar Pipeline")
    session.add(selected_party)
    session.flush()

    with pytest.raises(
        StatementRefusal,
        match="stated-party wording does not resolve",
    ):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=selected_party.id,
            stated_party="Equistar",
            stated_external_org_id=selected_party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete relocation.",
            new_timing=StatementTiming.month("January 2025", 2025, 1),
            scope=StatementScope.unknown(),
            created_by="local:statement-coordinator",
            evidence=CitedStatementEvidence(
                document.id, 1, "Equistar will complete relocation."
            ),
        )

    assert session.scalars(select(DependencyEvent)).all() == []


def test_shared_writer_refuses_a_guided_resolution_bound_to_different_facts(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        EvidenceBoundPartyResolution,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, _ = statement_record
    selected_party = ExternalOrg(name="Equistar Pipeline Resolution Test")
    session.add(selected_party)
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"fields": {"external_org": "Equistar"}},
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="guided-resolution-test",
        model="test-model",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    evidence = CitedStatementEvidence(
        document.id, 1, "Equistar will complete relocation."
    )
    resolution = EvidenceBoundPartyResolution(
        mode="guided_evidence_bound",
        project_id=project.id,
        candidate_id=candidate.id,
        stated_party="Equistar",
        stated_external_org_id=selected_party.id,
        principal="local:statement-coordinator",
        evidence=(evidence,),
    )
    mismatches = (
        replace(resolution, project_id=project.id + 1000),
        replace(resolution, stated_party="Different Utility"),
        replace(resolution, stated_external_org_id=party.id),
        replace(resolution, principal="local:different-coordinator"),
        replace(
            resolution,
            evidence=(CitedStatementEvidence(document.id, 1, "January 2025"),),
        ),
    )

    for mismatch in mismatches:
        with pytest.raises(
            StatementRefusal,
            match="guided party resolution does not match",
        ):
            record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=selected_party.id,
                stated_party="Equistar",
                stated_external_org_id=selected_party.id,
                source_kind="cited",
                event_date=None,
                description="Equistar will complete relocation.",
                new_timing=StatementTiming.month("January 2025", 2025, 1),
                scope=StatementScope.unknown(),
                created_by="local:statement-coordinator",
                evidence=evidence,
                party_resolution=mismatch,
            )

    assert session.scalars(select(DependencyEvent)).all() == []


def test_shared_writer_refuses_cross_project_evidence_in_a_guided_resolution(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        EvidenceBoundPartyResolution,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, _, source_document, _ = statement_record
    selected_party = ExternalOrg(name="Equistar Pipeline Cross Project Test")
    other_project = Project(
        slug=f"other-statement-project-{project.id}",
        name="Other statement project",
        is_synthetic=True,
    )
    session.add_all([selected_party, other_project])
    session.flush()
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"fields": {"external_org": "Equistar"}},
        source_document_id=source_document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="guided-resolution-test",
        model="test-model",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    other_document = Document(
        project_id=other_project.id,
        sha256=hashlib.sha256(f"other:{project.id}".encode()).hexdigest(),
        filename="other-project-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(other_document)
    session.flush()
    session.add(
        DocPage(
            document_id=other_document.id,
            page_no=1,
            text="Equistar will complete relocation.",
        )
    )
    session.flush()
    evidence = CitedStatementEvidence(
        other_document.id, 1, "Equistar will complete relocation."
    )
    resolution = EvidenceBoundPartyResolution(
        mode="guided_evidence_bound",
        project_id=project.id,
        candidate_id=candidate.id,
        stated_party="Equistar",
        stated_external_org_id=selected_party.id,
        principal="local:statement-coordinator",
        evidence=(evidence,),
    )

    with pytest.raises(StatementRefusal, match="belongs to another project"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=selected_party.id,
            stated_party="Equistar",
            stated_external_org_id=selected_party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete relocation.",
            new_timing=StatementTiming.month("January 2025", 2025, 1),
            scope=StatementScope.unknown(),
            created_by="local:statement-coordinator",
            evidence=evidence,
            party_resolution=resolution,
        )

    assert session.scalars(select(DependencyEvent)).all() == []


def test_shared_writer_requires_an_exact_quote_on_cells_text(
    session, statement_record
):
    """Cell-derived text has no extraction tolerance to spend on a near match."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    session.add(
        DocPage(
            document_id=document.id,
            page_no=2,
            text="Equistar will complete relocation!",
            text_source="cells",
        )
    )
    session.flush()

    with pytest.raises(StatementRefusal, match="quote was not found"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party=party.name,
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete relocation.",
            new_timing=StatementTiming.month("January 2025", 2025, 1),
            scope=StatementScope.selected((dependency.id,)),
            created_by="local:statement-coordinator",
            evidence=CitedStatementEvidence(
                document.id, 2, "Equistar will complete relocation."
            ),
        )

    assert session.scalars(select(DependencyEvent)).all() == []


def test_corrections_cannot_move_a_commitment_lineage_to_other_external_parties(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    root = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description="Equistar will complete relocation.",
        new_timing=StatementTiming.day("August 20, 2026", date(2026, 8, 20)),
        scope=StatementScope.unknown(),
        created_by="local:statement-coordinator",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )
    other_party = ExternalOrg(name="Kinder Morgan")
    session.add(other_party)
    session.flush()

    with pytest.raises(StatementRefusal, match="same External Parties"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=other_party.id,
            stated_party=other_party.name,
            stated_external_org_id=other_party.id,
            source_kind="cited",
            event_date=None,
            description="Kinder Morgan will complete relocation.",
            new_timing=StatementTiming.day("August 27, 2026", date(2026, 8, 27)),
            scope=StatementScope.unknown(),
            created_by="local:statement-coordinator",
            commitment_lineage_id=root.commitment_lineage_id,
            evidence=CitedStatementEvidence(
                document.id, 1, "Equistar will complete relocation."
            ),
        )

    with pytest.raises(IntegrityError, match="same External Parties"):
        with session.begin_nested():
            raw = DependencyEvent(
                project_id=project.id,
                commitment_lineage_id=root.commitment_lineage_id,
                supersedes_event_id=root.id,
                affected_external_org_id=other_party.id,
                stated_external_org_id=other_party.id,
                attribution_state="resolved",
                stated_party=other_party.name,
                event_type="commitment",
                source_kind="cited",
                scope_mode="unknown",
                timing_direction=None,
                event_date=None,
                description="Kinder Morgan will complete relocation.",
                created_by="local:statement-coordinator",
            )
            session.add(raw)
            session.flush([raw])
            session.add(
                DependencyEventTiming(
                    event_id=raw.id,
                    kind="new",
                    text="August 27, 2026",
                    precision="day",
                    start_date=date(2026, 8, 27),
                    end_date=date(2026, 8, 27),
                )
            )
            evidence = EvidenceLink(
                dependency_id=None,
                document_id=document.id,
                page_no=1,
                quote="Equistar will complete relocation.",
                verified=True,
            )
            session.add(evidence)
            session.flush([evidence])
            session.add(
                DependencyEventEvidence(
                    evidence_link_id=evidence.id,
                    event_id=raw.id,
                    recorded_by="local:statement-coordinator",
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_human_recorded_candidate_7296_preserves_both_timings_direction_and_unknown_scope(
    session, statement_record
):
    """The coordinator preserves the party-level change without choosing a row."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, _, document, dependency = statement_record
    kinder_morgan = ExternalOrg(name="Kinder Morgan")
    session.add(kinder_morgan)
    session.flush()

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=kinder_morgan.id,
        stated_party="Kinder Morgan",
        stated_external_org_id=kinder_morgan.id,
        source_kind="cited",
        event_date=None,
        description=(
            "The March 2026 completion timeline seems unattainable. "
            "Propose extending to May 16th."
        ),
        previous_timing=StatementTiming.month("March 2026", 2026, 3),
        new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
        scope=StatementScope.unknown(),
        created_by="local:statement-coordinator",
        evidence=CitedStatementEvidence(
            document_id=document.id,
            page_no=1,
            quote=(
                "The March 2026 completion timeline seems unattainable. "
                "Propose extending to May 16th."
            ),
        ),
    )

    assert event.event_type == "committed_date_change"
    assert event.timing_direction == "later"
    assert (
        event.previous_timing.text,
        event.previous_timing.precision,
        event.previous_timing.start_date,
        event.previous_timing.end_date,
    ) == ("March 2026", "month", date(2026, 3, 1), date(2026, 3, 31))
    assert (
        event.new_timing.text,
        event.new_timing.precision,
        event.new_timing.start_date,
        event.new_timing.end_date,
    ) == ("May 16th", "day", date(2026, 5, 16), date(2026, 5, 16))
    assert event.scope_mode == "unknown"
    assert session.scalars(
        select(DependencyEventScope).where(DependencyEventScope.event_id == event.id)
    ).all() == []
    session.refresh(dependency)
    assert dependency.committed_date is None


def test_scope_correction_supersedes_an_unknown_decision_without_rewriting_history(
    session, statement_record
):
    """A coordinator can place a preserved party statement in a later act."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
        record_statement_scope_decision,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.unknown(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )
    [original] = session.scalars(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == event.id
        )
    ).all()

    correction = record_statement_scope_decision(
        session,
        event_id=event.id,
        scope=StatementScope.selected((dependency.id,)),
        actor=HumanPrincipal("local:scope-coordinator"),
    )

    assert correction.supersedes_scope_decision_id == original.id
    assert correction.decided_by == "local:scope-coordinator"
    assert session.scalars(
        select(DependencyEventScope).where(
            DependencyEventScope.scope_decision_id == original.id
        )
    ).all() == []
    assert session.scalars(
        select(DependencyEventScope).where(
            DependencyEventScope.scope_decision_id == correction.id
        )
    ).one().dependency_id == dependency.id
    session.refresh(dependency)
    assert dependency.committed_date == date(2025, 6, 1)


def test_scope_decision_database_has_one_root_and_attributable_actors(
    session, statement_record
):
    """Scope history cannot fork or turn a role label into its actor."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, _ = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description="Equistar will provide a chain of title in January 2025.",
        new_timing=StatementTiming.month("January 2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "January 2025"),
    )
    with pytest.raises(IntegrityError, match="uq_dependency_event_scope_decision_root"):
        with session.begin_nested():
            session.add(
                DependencyEventScopeDecision(
                    event_id=event.id,
                    scope_mode="unknown",
                    decided_by="corridor:event-admission",
                )
            )
            session.flush()

    root = session.scalar(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == event.id
        )
    )
    assert root is not None
    with pytest.raises(IntegrityError, match="named human or deployed policy"):
        with session.begin_nested():
            session.add(
                DependencyEventScopeDecision(
                    event_id=event.id,
                    scope_mode="unknown",
                    supersedes_scope_decision_id=root.id,
                    decided_by="agent",
                )
            )
            session.flush()

    with pytest.raises(IntegrityError, match="named human or deployed policy"):
        with session.begin_nested():
            session.add(
                DependencyEvent(
                    project_id=project.id,
                    affected_external_org_id=party.id,
                    stated_external_org_id=party.id,
                    scope_mode="unknown",
                    event_type="commitment",
                    source_kind="cited",
                    stated_party="Equistar",
                    description="A role label is not a statement-scope actor.",
                    created_by="agent",
                )
            )
            session.flush()


def test_database_still_requires_a_verbal_conversation_date(session, statement_record):
    """The extended guard keeps the one verbal-specific requirement (#335).

    A Verbal now obeys the same precision and scope rules as any statement, so
    the trigger no longer forces exact-day, single-Constraint shapes.  It still
    refuses a Verbal that omits when the conversation happened, even by a raw
    writer that forces the deferred constraint triggers to check.
    """
    project, party, _, _ = statement_record
    with pytest.raises(IntegrityError, match="conversation date"):
        with session.begin_nested():
            event = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=party.id,
                scope_mode="unknown",
                event_type="commitment",
                source_kind="verbal",
                stated_party="Equistar",
                event_date=None,
                description="A Verbal that never says when it was heard.",
                created_by="local:statement-recorder",
            )
            session.add(event)
            session.flush()
            session.add(
                DependencyEventTiming(
                    event_id=event.id,
                    kind="new",
                    text="2025-08-15",
                    precision="day",
                    start_date=date(2025, 8, 15),
                    end_date=date(2025, 8, 15),
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_one_exact_day_statement_links_each_selected_dependency_once_and_projects_it(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, first = statement_record
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-2",
        dep_type="utility_relocation",
        title="Second Equistar relocation",
        external_org_id=party.id,
    )
    session.add(second)
    session.flush()

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((first.id, second.id)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
    )

    links = session.scalars(
        select(DependencyEventScope)
        .where(DependencyEventScope.event_id == event.id)
        .order_by(DependencyEventScope.dependency_id)
    ).all()
    assert [link.dependency_id for link in links] == [first.id, second.id]
    assert len(session.scalars(select(DependencyEvent)).all()) == 1
    [evidence] = session.scalars(
        select(EvidenceLink)
        .join(
            DependencyEventEvidence,
            DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
        )
        .where(DependencyEventEvidence.event_id == event.id)
    ).all()
    assert evidence.dependency_id is None
    session.refresh(first)
    session.refresh(second)
    assert (first.committed_date, second.committed_date) == (
        date(2025, 6, 1),
        date(2025, 6, 1),
    )


def test_current_statement_projection_keeps_identity_date_provenance_and_closure(
    session, statement_record
):
    """Compatibility readers receive one statement fact, not four queries."""
    from corridor.dependency_events import (
        current_dependency_statements,
        project_committed_date,
    )
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )
    closure = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_external_org_id=party.id,
        scope_mode="selected",
        event_type="closure",
        event_date=date(2025, 2, 1),
        description="Relocation complete.",
        created_by="local:closure-reviewer",
    )
    session.add(closure)
    session.flush()
    session.add(DependencyEventScope(event_id=closure.id, dependency_id=dependency.id))
    session.flush()

    statement = current_dependency_statements(session, (dependency.id,))[dependency.id]

    assert statement.event is event
    assert statement.effective_date == date(2025, 6, 1)
    assert statement.provenance_class == "cited"
    assert statement.is_closed is True
    dependency.committed_date = None
    project_committed_date(session, dependency.id)
    assert dependency.committed_date == statement.effective_date


def test_full_statement_publication_uses_the_current_verbal_with_its_provenance(
    session, statement_record
):
    """Every full publisher receives one supported reading of the statement."""
    from corridor.dependency_events import published_dependency_statements
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation by 2025-06-01."
        ),
    )
    verbal = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2025, 2, 1),
        description="Equistar said relocation will finish on July 15.",
        new_timing=StatementTiming.day("July 15", date(2025, 7, 15)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )

    publication = published_dependency_statements(
        session, (dependency.id,), project_id=project.id
    )
    statement = publication.by_dependency[dependency.id]

    assert statement.current_event is verbal
    assert statement.event is verbal
    assert statement.committed_date == date(2025, 7, 15)
    assert statement.source_attribution == (
        "Verbal — Equistar told local:statement-coordinator on 2025-02-01"
    )
    assert publication.committed_dates == {dependency.id: date(2025, 7, 15)}
    assert publication.committed_events == {dependency.id: verbal}
    assert publication.unsupported_dependency_ids == frozenset()


def test_document_only_statement_publication_falls_back_to_verified_cited_history(
    session, statement_record
):
    """Document-only publishers select cited history before choosing the newest."""
    from corridor.dependency_events import published_dependency_statements
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    cited = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation by 2025-06-01."
        ),
    )
    verbal = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2025, 2, 1),
        description="Equistar said relocation will finish on July 15.",
        new_timing=StatementTiming.day("July 15", date(2025, 7, 15)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="local:statement-coordinator",
    )

    publication = published_dependency_statements(
        session,
        (dependency.id,),
        project_id=project.id,
        document_only=True,
    )
    statement = publication.by_dependency[dependency.id]

    assert statement.current_event is verbal
    assert statement.event is cited
    assert statement.committed_date == date(2025, 6, 1)
    assert statement.source_attribution == "Cited statement"
    assert statement.cited_provenance is not None
    assert statement.cited_provenance.document_id == document.id
    assert statement.cited_provenance.page_no == 1
    assert publication.committed_dates == {dependency.id: date(2025, 6, 1)}
    assert publication.committed_events == {dependency.id: cited}
    assert publication.unsupported_dependency_ids == frozenset()


def test_document_only_statement_publication_withholds_an_unsupported_current_citation(
    session, statement_record
):
    """An unsupported current citation must not expose stale cited history."""
    from corridor.dependency_events import published_dependency_statements
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "Older cited commitment."),
    )
    current = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        attribution_state="resolved",
        stated_party="Equistar",
        stated_external_org_id=party.id,
        scope_mode="selected",
        event_type="commitment",
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description="Equistar later stated relocation would finish July 15.",
        created_by="corridor:event-admission",
    )
    session.add(current)
    session.flush()
    current_decision = session.scalar(
        select(DependencyEventScopeDecision).where(
            DependencyEventScopeDecision.event_id == current.id
        )
    )
    session.add_all(
        (
            DependencyEventTiming(
                event_id=current.id,
                kind="new",
                text="July 15",
                precision="day",
                start_date=date(2025, 7, 15),
                end_date=date(2025, 7, 15),
            ),
            DependencyEventScope(
                event_id=current.id,
                scope_decision_id=current_decision.id,
                dependency_id=dependency.id,
                recorded_by="corridor:event-admission",
            ),
        )
    )
    session.flush()

    publication = published_dependency_statements(
        session,
        (dependency.id,),
        project_id=project.id,
        document_only=True,
    )
    statement = publication.by_dependency[dependency.id]

    assert statement.current_event is current
    assert statement.event is None
    assert statement.committed_date is None
    assert statement.cited_provenance is None
    assert publication.unsupported_dependency_ids == frozenset((dependency.id,))


def test_current_statement_projection_clears_an_older_scalar_for_month_precision(
    session, statement_record
):
    """A month statement is current, even though legacy readers cannot print it."""
    from corridor.dependency_events import current_dependency_statements
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )
    month_event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description="Equistar will complete relocation in January 2026.",
        new_timing=StatementTiming.month("January 2026", 2026, 1),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation in January 2026."
        ),
    )

    statement = current_dependency_statements(session, (dependency.id,))[dependency.id]

    assert statement.event is month_event
    assert statement.provenance_class == "cited"
    assert statement.effective_date is None
    assert dependency.committed_date is None


def test_all_active_scope_is_a_snapshot_and_change_keeps_both_timings(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, first = statement_record
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-2",
        dep_type="utility_relocation",
        title="Second Equistar relocation",
        external_org_id=party.id,
    )
    session.add(second)
    session.flush()

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 2, 1),
        description="The March 2026 completion timeline is unattainable. Propose May 16th.",
        previous_timing=StatementTiming.month("March 2026", 2026, 3),
        new_timing=StatementTiming.day("May 16th", date(2026, 5, 16)),
        scope=StatementScope.all_active(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "The March 2026 completion timeline is unattainable."),
    )
    later_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-EQUISTAR-3",
        dep_type="utility_relocation",
        title="Later Equistar relocation",
        external_org_id=party.id,
    )
    session.add(later_dependency)
    session.flush()

    assert event.event_type == "committed_date_change"
    assert event.timing_direction == "later"
    assert event.previous_timing.text == "March 2026"
    assert event.new_timing.text == "May 16th"
    assert set(
        session.scalars(
            select(DependencyEventScope.dependency_id).where(
                DependencyEventScope.event_id == event.id
            )
        )
    ) == {first.id, second.id}
    session.refresh(first)
    session.refresh(second)
    assert (first.committed_date, second.committed_date) == (
        date(2026, 5, 16),
        date(2026, 5, 16),
    )


def test_invalid_known_scope_refuses_before_writing_any_part_of_the_statement(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, _ = statement_record
    other_party = ExternalOrg(name="Other Utility")
    session.add(other_party)
    session.flush()
    wrong_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-OTHER-1",
        dep_type="utility_relocation",
        title="Other Utility relocation",
        external_org_id=other_party.id,
    )
    session.add(wrong_dependency)
    session.flush()

    with pytest.raises(StatementRefusal, match="another External Party"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="Equistar",
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete relocation by 2025-06-01.",
            new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
            scope=StatementScope.selected((wrong_dependency.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
        )
    assert session.scalars(select(DependencyEvent)).all() == []


def test_database_refusal_leaves_no_partial_event_or_projection_and_keeps_the_write_transaction_usable(
    session, statement_record
):
    """The shared writer is one atomic act, even after it begins persisting."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, dependency = statement_record
    session.execute(
        text(
            """
            create function refuse_test_statement_evidence_write()
            returns trigger
            language plpgsql
            as $$
            begin
                if new.quote = 'refuse this statement evidence' then
                    raise exception 'statement write refused' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_test_statement_evidence_write
            before insert on evidence_links
            for each row execute function refuse_test_statement_evidence_write();
            """
        )
    )

    with pytest.raises(IntegrityError, match="statement write refused"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="Equistar",
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=date(2025, 1, 16),
            description="Equistar will complete relocation by 2025-06-01.",
            new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
            scope=StatementScope.selected((dependency.id,)),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(
                document.id, 1, "refuse this statement evidence"
            ),
        )

    assert session.scalars(
        select(DependencyEvent).where(DependencyEvent.project_id == project.id)
    ).all() == []
    assert session.scalars(
        select(EvidenceLink).where(EvidenceLink.document_id == document.id)
    ).all() == []
    session.refresh(dependency)
    assert dependency.committed_date is None

    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )

    assert event.id is not None
    session.refresh(dependency)
    assert dependency.committed_date == date(2025, 6, 1)


def test_month_timing_must_cover_that_calendar_month_exactly(
    session, statement_record
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, document, _ = statement_record
    with pytest.raises(StatementRefusal, match="invalid calendar bounds"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="Equistar",
            stated_external_org_id=party.id,
            source_kind="cited",
            event_date=None,
            description="Equistar will complete in January 2025.",
            new_timing=StatementTiming(
                "January 2025", "month", date(2025, 1, 1), date(2025, 1, 30)
            ),
            scope=StatementScope.unknown(),
            created_by="corridor:event-admission",
            evidence=CitedStatementEvidence(document.id, 1, "January 2025"),
        )


def test_shared_seam_requires_a_verbal_date_but_keeps_its_precision(
    session, statement_record
):
    """A Verbal still needs its conversation date, but keeps month precision (#335)."""
    from corridor.external_statements import (
        StatementRefusal,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )

    project, party, _, dependency = statement_record
    with pytest.raises(StatementRefusal, match="conversation date"):
        record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=party.id,
            stated_party="Equistar",
            stated_external_org_id=party.id,
            source_kind="verbal",
            event_date=None,
            description="Equistar said it will complete in January.",
            new_timing=StatementTiming.month("January 2025", 2025, 1),
            scope=StatementScope.selected((dependency.id,)),
            created_by="local:recorder",
        )
    # A month-precision, party-level Verbal is now accepted at the shared seam.
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="verbal",
        event_date=date(2025, 1, 16),
        description="Equistar said it will complete in January.",
        new_timing=StatementTiming.month("January 2025", 2025, 1),
        scope=StatementScope.unknown(),
        created_by="local:recorder",
    )
    assert event.new_timing.precision == "month"
    assert event.new_timing.start_date == date(2025, 1, 1)
    assert event.scope_mode == "unknown"


def test_event_evidence_requires_an_explicit_dependency_sufficiency_judgment(
    session, statement_record
):
    from corridor.dependency_events import current_statement_evidence_memberships
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )
    from corridor.ledger import is_ready, mark_satisfies
    from corridor.ledger import load_dependency
    from corridor.operative_support import (
        designate_publication_support,
        resolve_operative_support,
    )

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.selected((dependency.id,)),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(document.id, 1, "Equistar will complete relocation."),
    )
    evidence = session.scalar(
        select(EvidenceLink)
        .join(
            DependencyEventEvidence,
            DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
        )
        .where(DependencyEventEvidence.event_id == event.id)
    )

    assert evidence.dependency_id is None
    membership = current_statement_evidence_memberships(
        session, (dependency.id,)
    )
    [member] = membership.for_dependency(dependency.id)
    assert member.event_id == event.id
    assert member.evidence_link.id == evidence.id
    assert member.document.id == document.id
    assert membership.contains(dependency.id, evidence.id) is True
    assert is_ready(session, dependency.id) is False
    designate_publication_support(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:statement-reviewer"),
    )
    assert (
        resolve_operative_support(session, [dependency.id])[dependency.id]
        .publication
        .evidence_link_id
        == evidence.id
    )
    assert [link.id for link, _ in load_dependency(session, dependency.id).evidence] == [
        evidence.id
    ]
    assert mark_satisfies(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:statement-reviewer"),
    ) is True
    assert is_ready(session, dependency.id) is True
    assert session.scalar(
        select(DependencyEvidenceSufficiency).where(
            DependencyEvidenceSufficiency.dependency_id == dependency.id,
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
        )
    ) is not None


def test_event_evidence_has_one_source_identity_and_roles_bind_the_scope_link(
    session, statement_record
):
    """A party-level citation gains no Dependency judgment until placement."""
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
        record_statement_scope_decision,
    )
    from corridor.dependency_events import current_statement_evidence_memberships
    from corridor.ledger import mark_satisfies
    from corridor.operative_support import designate_publication_support

    project, party, document, dependency = statement_record
    event = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="Equistar",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2025, 1, 16),
        description="Equistar will complete relocation by 2025-06-01.",
        new_timing=StatementTiming.day("2025-06-01", date(2025, 6, 1)),
        scope=StatementScope.unknown(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document.id, 1, "Equistar will complete relocation."
        ),
    )
    evidence = session.scalar(
        select(EvidenceLink)
        .join(
            DependencyEventEvidence,
            DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
        )
        .where(DependencyEventEvidence.event_id == event.id)
    )
    [event_evidence] = session.scalars(
        select(DependencyEventEvidence).where(
            DependencyEventEvidence.event_id == event.id
        )
    ).all()
    assert event_evidence.evidence_link_id == evidence.id
    assert evidence.dependency_id is None

    decision = record_statement_scope_decision(
        session,
        event_id=event.id,
        scope=StatementScope.selected((dependency.id,)),
        actor=HumanPrincipal("local:scope-coordinator"),
    )
    [scope_link] = session.scalars(
        select(DependencyEventScope).where(
            DependencyEventScope.scope_decision_id == decision.id
        )
    ).all()
    designate_publication_support(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:ready-judge"),
    )
    assert mark_satisfies(
        session,
        dependency.id,
        evidence.id,
        principal=HumanPrincipal("local:ready-judge"),
    ) is True
    sufficiency = session.scalar(
        select(DependencyEvidenceSufficiency).where(
            DependencyEvidenceSufficiency.dependency_id == dependency.id,
            DependencyEvidenceSufficiency.evidence_link_id == evidence.id,
        )
    )
    assert sufficiency.scope_link_id == scope_link.id

    record_statement_scope_decision(
        session,
        event_id=event.id,
        scope=StatementScope.unknown(),
        actor=HumanPrincipal("local:scope-coordinator"),
    )
    membership = current_statement_evidence_memberships(
        session, (dependency.id,)
    )
    assert membership.for_dependency(dependency.id) == ()
    assert membership.contains(dependency.id, evidence.id) is False


def test_database_rejects_scope_links_for_unknown_scope_and_empty_known_scope(
    session, statement_record
):
    project, party, _, dependency = statement_record
    unknown = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_external_org_id=party.id,
        scope_mode="unknown",
        event_type="commitment",
        source_kind="cited",
        stated_party="Equistar",
        description="A party-level statement.",
        created_by="corridor:event-admission",
    )
    session.add(unknown)
    session.flush()
    session.add(
        DependencyEventTiming(
            event_id=unknown.id,
            kind="new",
            text="June 2025",
            precision="month",
            start_date=date(2025, 6, 1),
            end_date=date(2025, 6, 30),
        )
    )
    session.flush()
    with pytest.raises(IntegrityError, match="unknown statement scope"):
        with session.begin_nested():
            session.add(DependencyEventScope(event_id=unknown.id, dependency_id=dependency.id))
            session.flush()

    with pytest.raises(IntegrityError, match="known statement scope has no"):
        with session.begin_nested():
            known = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=party.id,
                scope_mode="selected",
                event_type="commitment",
                source_kind="cited",
                stated_party="Equistar",
                description="A known-scope statement.",
                created_by="corridor:event-admission",
            )
            session.add(known)
            session.flush()
            session.add(
                DependencyEventTiming(
                    event_id=known.id,
                    kind="new",
                    text="June 2025",
                    precision="month",
                    start_date=date(2025, 6, 1),
                    end_date=date(2025, 6, 30),
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_database_requires_attribution_state_to_match_resolved_party(
    session, statement_record
):
    project, party, _, _ = statement_record

    with pytest.raises(IntegrityError, match="ck_dependency_events_attribution"):
        with session.begin_nested():
            session.execute(
                text(
                    """
                    insert into dependency_events
                        (project_id, affected_external_org_id,
                         stated_external_org_id, attribution_state, scope_mode,
                         event_type, source_kind, stated_party, description,
                         created_by)
                    values
                        (:project_id, :party_id, :party_id, 'unresolved',
                         'unknown', 'commitment', 'cited', 'Equistar',
                         'Attribution contradicts its resolved party.',
                         'corridor:event-admission')
                    """
                ),
                {"project_id": project.id, "party_id": party.id},
            )


def test_statement_storage_preserves_explicit_unresolved_attribution(
    session, statement_record
):
    project, party, _, _ = statement_record
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_external_org_id=None,
        attribution_state="unresolved",
        scope_mode="unknown",
        event_type="commitment",
        source_kind="cited",
        stated_party="the pipeline operator",
        description="The source wording does not resolve the speaker.",
        created_by="corridor:event-admission",
    )
    session.add(event)
    session.flush()
    session.add(
        DependencyEventTiming(
            event_id=event.id,
            kind="new",
            text="late summer",
            precision="approximate",
            start_date=None,
            end_date=None,
        )
    )
    session.flush()

    assert event.affected_external_org_id == party.id
    assert event.stated_party == "the pipeline operator"
    assert event.stated_external_org_id is None
    assert event.attribution_state == "unresolved"


@pytest.mark.parametrize(
    ("event_type", "timings", "message"),
    [
        ("commitment", (), "Commitment requires exactly one new timing"),
        (
            "committed_date_change",
            (("new", "June 2025"),),
            "Committed Date Change requires previous and new timings",
        ),
        (
            "closure",
            (("new", "June 2025"),),
            "closure cannot carry a commitment timing",
        ),
    ],
)
def test_database_enforces_statement_timing_cardinality(
    session, statement_record, event_type, timings, message
):
    project, party, _, _ = statement_record

    with pytest.raises(IntegrityError, match=message):
        with session.begin_nested():
            event = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=party.id,
                attribution_state="resolved",
                scope_mode="unknown",
                event_type=event_type,
                source_kind="cited",
                stated_party="Equistar",
                description="A deliberately invalid timing shape.",
                created_by="corridor:event-admission",
            )
            session.add(event)
            session.flush()
            for kind, wording in timings:
                session.add(
                    DependencyEventTiming(
                        event_id=event.id,
                        kind=kind,
                        text=wording,
                        precision="month",
                        start_date=date(2025, 6, 1),
                        end_date=date(2025, 6, 30),
                    )
                )
            session.flush()
            session.execute(text("set constraints all immediate"))


def test_database_requires_explicit_scope_mode_and_event_evidence_ownership(
    session, statement_record
):
    project, party, document, dependency = statement_record
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text(
                    """
                    insert into dependency_events
                        (project_id, affected_external_org_id, stated_external_org_id,
                         scope_mode, event_type, source_kind, stated_party,
                         description, created_by)
                    values
                        (:project_id, :party_id, :party_id, null, 'commitment',
                         'cited', 'Equistar', 'No declared scope.',
                         'corridor:event-admission')
                    """
                ),
                {"project_id": project.id, "party_id": party.id},
            )

    # An Evidence row without direct ownership must have one explicit event
    # owner. The event id is no longer an inline shadow on EvidenceLink.
    with pytest.raises(IntegrityError, match="statement Evidence needs exactly one event owner"):
        with session.begin_nested():
            session.add(
                EvidenceLink(
                    dependency_id=None,
                    document_id=document.id,
                    page_no=1,
                    quote="Party-level statement.",
                    verified=True,
                )
            )
            session.flush()
            session.execute(text("set constraints all immediate"))
