"""The Support Assessment relation (#530, ADR-0082).

One typed proposition, one or more Source Segments in its project and
rendition, an evidence role, an assessment, and exactly one authority.  The
rows are append-only with supersession history, replay is idempotent, and
competing writers converge or are refused rather than duplicating an
effective assessment.  Locator validity is never read here.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import threading

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    SourceSegment,
    SupportAssessment,
    SupportAssessmentSource,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.support_assessments import (
    ExtractedProposalProposition,
    FactProposition,
    ReleasedPolicy,
    StaleSupportAssessment,
    SupportAssessmentRefused,
    assessed_source_segments,
    current_support_assessments,
    record_support_assessment,
    support_assessment_history,
    support_assessments_as_of,
)


ALICE = HumanPrincipal("local:alice")
BOB = HumanPrincipal("local:bob")
POLICY = ReleasedPolicy("structured-cell-support-v1", "ruleset-2026-09")


def _rendition(session, project, name, values, prompt_version="support_fixture_v1"):
    """One document, one run, one segment and one Fact per value, one proposal."""

    document = Document(
        project_id=project.id,
        sha256=sha256(name.encode()).hexdigest(),
        filename=name,
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    run = ExtractionRun(
        document_id=document.id,
        prompt_version=prompt_version,
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush()
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    segments, facts = [], []
    for ordinal, (cell, value) in enumerate(values, start=1):
        segment = SourceSegment(
            project_id=project.id,
            document_id=document.id,
            kind="spreadsheet_cell",
            exact_text=value,
            content_sha256=sha256(value.encode()).hexdigest(),
            ordinal=ordinal,
            sheet_name="Utility Conflicts",
            cell_range=cell,
        )
        session.add(segment)
        session.flush()
        fact = Fact(
            project_id=project.id,
            document_id=document.id,
            extraction_run_id=run.id,
            fact_type="utility_id",
            subject_kind="source_row",
            subject_key=f"Utility Conflicts!{ordinal + 1}",
            text_value=value,
            transformation="trim_cell_text_v1",
            recorded_by=f"extractor:{prompt_version}",
            content_sha256=sha256(f"{name}:{value}".encode()).hexdigest(),
        )
        session.add(fact)
        session.flush()
        session.add(
            FactSource(
                project_id=project.id,
                document_id=document.id,
                fact_id=fact.id,
                source_segment_id=segment.id,
                role="value_source",
                ordinal=1,
            )
        )
        segments.append(segment)
        facts.append(fact)
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={"fields": {}},
        source_document_id=document.id,
        source_pages=[1],
        prompt_version=run.prompt_version,
        citations_verified=False,
        state="pending",
    )
    session.add(candidate)
    session.flush()
    proposal = ExtractedProposal(
        project_id=project.id,
        document_id=document.id,
        extraction_run_id=run.id,
        candidate_id=candidate.id,
        kind="dependency",
        subject_key=facts[0].subject_key,
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush()
    session.add(
        ExtractedProposalFact(
            project_id=project.id,
            document_id=document.id,
            extraction_run_id=run.id,
            proposal_id=proposal.id,
            fact_id=facts[0].id,
            ordinal=1,
        )
    )
    session.flush()
    return document, tuple(segments), tuple(facts), proposal


@pytest.fixture
def case(session):
    project = Project(slug="support-assessment", name="Support", is_synthetic=True)
    session.add(project)
    session.flush()
    document, segments, facts, proposal = _rendition(
        session, project, "matrix.xlsx", (("A3", "UC-1"), ("A4", "UC-2"))
    )
    return project, document, segments, facts, proposal


def _count(session, model, project_id):
    return session.scalar(
        select(func.count()).select_from(model).where(model.project_id == project_id)
    )


# --- Recording ---------------------------------------------------------------


def test_a_human_assessment_binds_one_fact_to_its_segments(session, case):
    project, document, segments, facts, _proposal = case
    when = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)

    recorded = record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[1].id, segments[0].id),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
        assessed_at=when,
    )

    assert (
        recorded.project_id,
        recorded.document_id,
        recorded.proposition_kind,
        recorded.fact_id,
        recorded.extracted_proposal_id,
        recorded.evidence_role,
        recorded.assessment,
        recorded.human_principal,
        recorded.released_policy,
        recorded.ruleset_version,
        recorded.superseded_by,
        recorded.assessed_at,
    ) == (
        project.id,
        document.id,
        "source_fact",
        facts[0].id,
        None,
        "value_support",
        "supported",
        "local:alice",
        None,
        None,
        None,
        when,
    )
    assert [s.id for s in assessed_source_segments(session, recorded)] == [
        segments[1].id,
        segments[0].id,
    ]
    assert current_support_assessments(
        session, project.id, FactProposition(facts[0].id)
    ) == (recorded,)


def test_a_released_policy_assessment_names_its_ruleset(session, case):
    project, document, segments, _facts, proposal = case

    recorded = record_support_assessment(
        session,
        project_id=project.id,
        proposition=ExtractedProposalProposition(proposal.id),
        source_segment_ids=(segments[0].id,),
        evidence_role="attribution",
        assessment="partially_supported",
        authority=POLICY,
    )

    assert (
        recorded.proposition_kind,
        recorded.extracted_proposal_id,
        recorded.fact_id,
        recorded.document_id,
        recorded.human_principal,
        recorded.released_policy,
        recorded.ruleset_version,
    ) == (
        "extracted_proposal",
        proposal.id,
        None,
        document.id,
        None,
        "structured-cell-support-v1",
        "ruleset-2026-09",
    )
    assert recorded.assessed_at is not None


@pytest.mark.parametrize(
    "role, assessment",
    [
        ("value_support", "supported"),
        ("attribution", "partially_supported"),
        ("timing", "contradicted"),
        ("scope", "unclear"),
        ("context", "not_assessed"),
    ],
)
def test_every_role_and_assessment_the_decision_names_is_accepted(
    session, case, role, assessment
):
    project, _document, segments, facts, _proposal = case

    recorded = record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[0].id,),
        evidence_role=role,
        assessment=assessment,
        authority=ALICE,
    )

    assert (recorded.evidence_role, recorded.assessment) == (role, assessment)


def test_one_proposition_carries_one_effective_assessment_per_role(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    for role in ("value_support", "attribution"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=proposition,
            source_segment_ids=(segments[0].id,),
            evidence_role=role,
            assessment="supported",
            authority=ALICE,
        )

    assert [
        a.evidence_role for a in current_support_assessments(session, project.id, proposition)
    ] == ["attribution", "value_support"]


# --- Replay and supersession ------------------------------------------------


def test_replaying_the_same_assessment_returns_the_same_row(session, case):
    project, _document, segments, facts, _proposal = case
    values = dict(
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
    )

    first = record_support_assessment(session, **values)
    replayed = record_support_assessment(session, **values)

    assert replayed.id == first.id
    assert _count(session, SupportAssessment, project.id) == 1
    assert _count(session, SupportAssessmentSource, project.id) == 1


def test_a_different_assessment_of_an_assessed_role_must_supersede(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
    )

    with pytest.raises(DBAPIError, match="already exists .* supersede"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=proposition,
            source_segment_ids=(segments[0].id,),
            evidence_role="value_support",
            assessment="contradicted",
            authority=BOB,
        )


def test_supersession_appends_and_keeps_current_and_as_of_readings(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    first_time = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    second_time = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    first = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=POLICY,
        assessed_at=first_time,
    )

    second = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id, segments[1].id),
        evidence_role="value_support",
        assessment="contradicted",
        authority=BOB,
        assessed_at=second_time,
        supersedes_id=first.id,
    )

    assert second.id != first.id
    assert first.superseded_by == second.id
    assert second.superseded_by is None
    assert (first.assessment, first.released_policy) == ("supported", POLICY.policy)
    assert current_support_assessments(session, project.id, proposition) == (second,)
    assert support_assessment_history(session, project.id, proposition) == (first, second)
    assert support_assessments_as_of(
        session, project.id, proposition, first_time + timedelta(hours=1)
    ) == (first,)
    assert support_assessments_as_of(session, project.id, proposition, second_time) == (
        second,
    )
    assert support_assessments_as_of(
        session, project.id, proposition, first_time - timedelta(hours=1)
    ) == ()
    assert _count(session, SupportAssessment, project.id) == 2


def test_replaying_a_supersession_returns_the_successor(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    first = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="scope",
        assessment="unclear",
        authority=ALICE,
    )
    correction = dict(
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="scope",
        assessment="supported",
        authority=ALICE,
        supersedes_id=first.id,
    )

    second = record_support_assessment(session, **correction)
    replayed = record_support_assessment(session, **correction)

    assert replayed.id == second.id
    assert _count(session, SupportAssessment, project.id) == 2


def test_a_stale_predecessor_is_refused_not_overwritten(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    first = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="timing",
        assessment="supported",
        authority=ALICE,
    )
    record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="timing",
        assessment="contradicted",
        authority=BOB,
        supersedes_id=first.id,
    )

    with pytest.raises(StaleSupportAssessment):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=proposition,
            source_segment_ids=(segments[1].id,),
            evidence_role="timing",
            assessment="unclear",
            authority=ALICE,
            supersedes_id=first.id,
        )


def test_a_predecessor_must_assess_the_same_proposition_and_role(session, case):
    project, _document, segments, facts, _proposal = case
    other_role = record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[0].id,),
        evidence_role="context",
        assessment="supported",
        authority=ALICE,
    )

    with pytest.raises(DBAPIError, match="same proposition and role"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=FactProposition(facts[0].id),
            source_segment_ids=(segments[0].id,),
            evidence_role="value_support",
            assessment="supported",
            authority=ALICE,
            supersedes_id=other_role.id,
        )


def test_a_correction_cannot_precede_what_it_supersedes(session, case):
    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[1].id)
    first = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[1].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
        assessed_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    with pytest.raises(DBAPIError, match="cannot precede"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=proposition,
            source_segment_ids=(segments[1].id,),
            evidence_role="value_support",
            assessment="contradicted",
            authority=ALICE,
            assessed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            supersedes_id=first.id,
        )


# --- Refusals the database makes ----------------------------------------------


def test_a_proposition_outside_the_project_is_refused(session, case):
    project, _document, segments, _facts, _proposal = case
    other = Project(slug="support-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    _doc, _segments, foreign_facts, foreign_proposal = _rendition(
        session, other, "other.xlsx", (("A3", "UC-9"),), prompt_version="other_v1"
    )

    for proposition in (
        FactProposition(foreign_facts[0].id),
        ExtractedProposalProposition(foreign_proposal.id),
    ):
        with pytest.raises(DBAPIError, match="proposition is outside its project"):
            with session.begin_nested():
                record_support_assessment(
                    session,
                    project_id=project.id,
                    proposition=proposition,
                    source_segment_ids=(segments[0].id,),
                    evidence_role="value_support",
                    assessment="supported",
                    authority=ALICE,
                )


def test_a_segment_outside_the_project_is_refused(session, case):
    project, _document, _segments, facts, _proposal = case
    other = Project(slug="support-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    _doc, foreign_segments, _facts, _proposal = _rendition(
        session, other, "other.xlsx", (("A3", "UC-9"),), prompt_version="other_v1"
    )

    with pytest.raises(DBAPIError, match="source segment is outside its project"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=FactProposition(facts[0].id),
            source_segment_ids=(foreign_segments[0].id,),
            evidence_role="value_support",
            assessment="supported",
            authority=ALICE,
        )


def test_a_segment_from_another_rendition_of_the_same_project_is_refused(
    session, case
):
    project, _document, segments, facts, _proposal = case
    _doc, other_segments, _facts, _proposal = _rendition(
        session, project, "revision-b.xlsx", (("A3", "UC-1"),), prompt_version="rev_b_v1"
    )

    with pytest.raises(DBAPIError, match="belongs to another rendition"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=FactProposition(facts[0].id),
            source_segment_ids=(segments[0].id, other_segments[0].id),
            evidence_role="value_support",
            assessment="supported",
            authority=ALICE,
        )


def test_a_segment_named_twice_is_refused(session, case):
    project, _document, segments, facts, _proposal = case

    with pytest.raises(DBAPIError, match="names a Source Segment twice"):
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=FactProposition(facts[0].id),
            source_segment_ids=(segments[0].id, segments[0].id),
            evidence_role="value_support",
            assessment="supported",
            authority=ALICE,
        )


def test_the_command_refuses_a_policy_without_its_ruleset(session, case):
    from corridor.source_append import append_support_assessment

    project, _document, segments, facts, _proposal = case
    base = dict(
        project_id=project.id,
        proposition_kind="source_fact",
        fact_id=facts[0].id,
        extracted_proposal_id=None,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
    )

    for authority, message in (
        (
            dict(human_principal=None, released_policy="p-v1", ruleset_version=None),
            "needs its ruleset identity",
        ),
        (
            dict(human_principal="local:alice", released_policy=None, ruleset_version="r1"),
            "belongs to a released policy",
        ),
        (
            dict(human_principal="local:alice", released_policy="p-v1", ruleset_version="r1"),
            "one human principal or one released policy",
        ),
        (
            dict(human_principal=None, released_policy=None, ruleset_version=None),
            "one human principal or one released policy",
        ),
    ):
        with pytest.raises(DBAPIError, match=message):
            with session.begin_nested():
                append_support_assessment(session, **base, **authority)


def test_the_command_refuses_an_unknown_role_or_assessment_or_kind(session, case):
    from corridor.source_append import append_support_assessment

    project, _document, segments, facts, _proposal = case
    base = dict(
        project_id=project.id,
        proposition_kind="source_fact",
        fact_id=facts[0].id,
        extracted_proposal_id=None,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        human_principal="local:alice",
        released_policy=None,
        ruleset_version=None,
    )

    for override, message in (
        ({"evidence_role": "verified"}, "unrecognized Support Assessment evidence role"),
        ({"assessment": "verified"}, "unrecognized Support Assessment assessment"),
        (
            {"proposition_kind": "proposed_delta", "fact_id": None},
            "unrecognized Support Assessment proposition kind",
        ),
        (
            {"proposition_kind": "source_fact", "fact_id": None},
            "names exactly one Fact",
        ),
        ({"source_segment_ids": ()}, "at least one Source Segment"),
    ):
        with pytest.raises(DBAPIError, match=message):
            with session.begin_nested():
                append_support_assessment(session, **{**base, **override})


# --- Refusals the domain module makes -----------------------------------------


def test_the_module_refuses_bad_values_before_reaching_the_database(session, case):
    project, _document, segments, facts, _proposal = case
    base = dict(
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
    )

    with pytest.raises(SupportAssessmentRefused, match="evidence role"):
        record_support_assessment(session, **{**base, "evidence_role": "verified"})
    with pytest.raises(SupportAssessmentRefused, match="assessment must be"):
        record_support_assessment(session, **{**base, "assessment": "yes"})
    with pytest.raises(SupportAssessmentRefused, match="at least one"):
        record_support_assessment(session, **{**base, "source_segment_ids": ()})
    with pytest.raises(InvalidHumanPrincipal):
        record_support_assessment(session, **{**base, "authority": "reviewer"})
    with pytest.raises(SupportAssessmentRefused):
        ReleasedPolicy("p-v1", " ")
    assert _count(session, SupportAssessment, project.id) == 0


# --- Append-only ----------------------------------------------------------------


def test_not_even_the_schema_owner_writes_the_relation_raw(session, case):
    project, document, segments, facts, _proposal = case
    recorded = record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(facts[0].id),
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
    )

    with pytest.raises(DBAPIError, match="requires the append command"):
        with session.begin_nested():
            session.add(
                SupportAssessment(
                    project_id=project.id,
                    document_id=document.id,
                    proposition_kind="source_fact",
                    fact_id=facts[1].id,
                    evidence_role="value_support",
                    assessment="supported",
                    human_principal="local:alice",
                    content_sha256="0" * 64,
                    assessed_at=datetime.now(timezone.utc),
                )
            )
            session.flush()
    with pytest.raises(DBAPIError, match="requires the append command"):
        with session.begin_nested():
            session.execute(
                text(
                    "update support_assessments set assessment = 'contradicted' "
                    "where id = :id"
                ),
                {"id": recorded.id},
            )
    with pytest.raises(DBAPIError, match="requires the append command"):
        with session.begin_nested():
            session.execute(
                text("delete from support_assessments where id = :id"),
                {"id": recorded.id},
            )
    with pytest.raises(DBAPIError, match="requires the append command"):
        with session.begin_nested():
            session.execute(
                text(
                    "delete from support_assessment_sources "
                    "where support_assessment_id = :id"
                ),
                {"id": recorded.id},
            )
    session.expire_all()
    assert session.get_one(SupportAssessment, recorded.id).assessment == "supported"


def test_the_command_cannot_be_used_to_reopen_a_superseded_row(session, case):
    """The guard holds even the owner role to one supersession, never a reversal."""

    project, _document, segments, facts, _proposal = case
    proposition = FactProposition(facts[0].id)
    first = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="supported",
        authority=ALICE,
    )
    second = record_support_assessment(
        session,
        project_id=project.id,
        proposition=proposition,
        source_segment_ids=(segments[0].id,),
        evidence_role="value_support",
        assessment="contradicted",
        authority=BOB,
        supersedes_id=first.id,
    )

    with pytest.raises(DBAPIError, match="requires the append command|superseded once"):
        with session.begin_nested():
            session.execute(
                text("update support_assessments set superseded_by = null where id = :id"),
                {"id": first.id},
            )
    session.expire_all()
    assert session.get_one(SupportAssessment, first.id).superseded_by == second.id


# --- Locator validity stays a separate fact -------------------------------------


def test_the_relation_never_reads_locator_validity():
    """ADR-0082: no code infers support from a Source Passage Check.

    The domain module and the command are the only paths to the relation;
    neither mentions the legacy ``verified`` flag, the evidence-link table,
    or a validation status.
    """

    root = Path(__file__).resolve().parents[1] / "src" / "corridor"
    # The module docstring names the history it replaced; the code may not.
    module_source = (root / "support_assessments.py").read_text()
    module_tree = ast.parse(module_source)
    docstring_end = module_tree.body[0].end_lineno if ast.get_docstring(module_tree) else 0
    module = "\n".join(module_source.splitlines()[docstring_end:])
    migration = (
        root / "migrations" / "baseline_versions" / "b2d5f8a1c4e7_source_append_commands.py"
    ).read_text()
    # The command's own SQL, taken from the assignment rather than by slicing
    # to the next `def`: the revision carries other folded-in blocks after it,
    # and a text slice would silently start reading them (#605).
    assignment = next(
        node
        for node in ast.parse(migration).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "APPEND_SUPPORT_ASSESSMENT"
            for target in node.targets
        )
    )
    command = ast.get_source_segment(migration, assignment.value)

    for name, source in (("module", module), ("command", command)):
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("#", "--"))
        )
        for forbidden in ("verified", "evidence_links", "EvidenceLink", "validation_status"):
            assert forbidden not in body, f"{name} reads {forbidden}"


# --- Competing writers on committed transactions --------------------------------


def _committed_case(runtime_database):
    with runtime_database.session_factory.begin() as owner:
        project = Project(slug="support-race", name="Support Race", is_synthetic=True)
        owner.add(project)
        owner.flush()
        _document, segments, facts, _proposal = _rendition(
            owner, project, "race.xlsx", (("A3", "UC-1"),)
        )
        return project.id, facts[0].id, segments[0].id


def _owner_engine(runtime_database):
    url = (
        make_url(settings.database_url)
        .set(database=runtime_database.name)
        .render_as_string(hide_password=False)
    )
    return create_engine(url, poolclass=NullPool, future=True)


def _race(runtime_database, first_values, second_values):
    """Run two committed writers so the second blocks on the first's row.

    The first writer appends and holds its transaction open; the second
    appends the same slot and blocks on the unique index; the first commits;
    the second either converges on the same row or is refused.
    """

    project_id, fact_id, segment_id = _committed_case(runtime_database)
    engine_ = _owner_engine(runtime_database)
    base = dict(
        project_id=project_id,
        proposition=FactProposition(fact_id),
        source_segment_ids=(segment_id,),
        evidence_role="value_support",
    )
    outcome: dict[str, object] = {}
    first_appended = threading.Event()
    second_started = threading.Event()

    def second_writer():
        with OrmSession(bind=engine_) as session:
            second_started.set()
            try:
                row = record_support_assessment(session, **base, **second_values)
                session.commit()
                outcome["second"] = row.id
            except DBAPIError as exc:
                session.rollback()
                outcome["second_error"] = str(exc.orig)

    try:
        with OrmSession(bind=engine_) as first:
            first_row = record_support_assessment(first, **base, **first_values)
            outcome["first"] = first_row.id
            first_appended.set()
            worker = threading.Thread(target=second_writer)
            worker.start()
            second_started.wait(5)
            # The second writer is now blocked inside the command on the
            # effective-slot index; let it wait long enough to prove it
            # blocked rather than raced ahead.
            worker.join(0.5)
            assert worker.is_alive(), "the competing writer did not block on the slot"
            first.commit()
        worker.join(10)
        assert not worker.is_alive()
        with OrmSession(bind=engine_) as reader:
            effective = reader.scalars(
                select(SupportAssessment).where(
                    SupportAssessment.fact_id == fact_id,
                    SupportAssessment.superseded_by.is_(None),
                )
            ).all()
            total = reader.scalar(
                select(func.count()).select_from(SupportAssessment).where(
                    SupportAssessment.fact_id == fact_id
                )
            )
        return outcome, [row.id for row in effective], total
    finally:
        engine_.dispose()


def test_competing_writers_of_the_same_assessment_converge_on_one_row(
    runtime_database,
):
    same = dict(assessment="supported", authority=ALICE)

    outcome, effective, total = _race(runtime_database, same, same)

    assert outcome["second"] == outcome["first"]
    assert effective == [outcome["first"]]
    assert total == 1


def test_competing_writers_of_different_assessments_leave_one_effective_row(
    runtime_database,
):
    outcome, effective, total = _race(
        runtime_database,
        dict(assessment="supported", authority=ALICE),
        dict(assessment="contradicted", authority=BOB),
    )

    assert "second" not in outcome
    assert "already exists" in outcome["second_error"]
    assert effective == [outcome["first"]]
    assert total == 1
