"""The audit trail, as a property rather than a docstring.

`models.AuditLog` says "every ledger mutation writes here". Six sites
built the row by hand and one mutating module built none at all, so the
claim was unfalsifiable — there was no interface to walk.
"""

import pytest
from sqlalchemy import func, select

from corridor import audit
from corridor.adjudicate import (
    accept_candidate,
    dismiss_dependency,
    merge_candidate,
    set_resolution_strategy,
)
from corridor.db import Session, engine
from corridor.extraction_runs import (
    declare_active_run,
    record_extraction_run,
)
from corridor.ledger import load_dependency, mark_satisfies
from corridor.milestones import link_dependency
from corridor.models import (
    AuditLog,
    Candidate,
    CandidateDisposition,
    Dependency,
    DependencyDismissal,
    DisputeSettlement,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Milestone,
    Project,
)
from corridor.principals import HumanPrincipal

TEST_PRINCIPAL = HumanPrincipal("local:tester")
REVIEWER_PRINCIPAL = HumanPrincipal("local:test-reviewer")

FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "AT&T Texas",
    "utility_type": "Telecom",
    "station_from": "1149+00",
}


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def document(session):
    project = Project(slug="audit-test", name="Audit Test", is_synthetic=True)
    session.add(project)
    session.flush()
    session.add(ExternalOrg(name="AT&T Texas", aliases=[]))
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="a9" * 32,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text="FOC1-1 AT&T Texas Telecom FOC1-2 AT&T Texas Telecom",
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return doc


def make_candidate(session, document, *, utility_id="FOC1-1", activate=True):
    c = Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {**FIELDS, "utility_id": utility_id},
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": f"{utility_id} AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(c)
    session.flush()
    if activate:
        run = record_extraction_run(
            session,
            document,
            prompt_version=c.prompt_version,
            candidate_count=1,
            page_errors=0,
            candidates=(c,),
            model=c.model,
            allow_unsealed_legacy=True,
        )
        declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    session.flush()
    return c


def entries(session, dependency_id):
    return [e.action for e in audit.trail_for_dependency(session, dependency_id)]


def test_an_unknown_entity_is_refused(session):
    """Free-text `entity_type` is how the write and the read drifted."""
    with pytest.raises(ValueError, match="evidence_link"):
        audit.record(
            session,
            actor="tester",
            action="x",
            entity_type="evidence_link",
            entity_id=1,
        )


def test_every_ledger_mutation_writes_an_entry(session, document):
    """One test walks the mutating entry points.

    Adding a mutation without an audit entry now fails here rather than
    being noticed years later by someone reading a record with a hole in
    its history.
    """
    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    assert entries(session, dependency.id) == ["accept_candidate"]

    link = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    mark_satisfies(
        session, dependency.id, link.id, principal=TEST_PRINCIPAL
    )

    set_resolution_strategy(session, dependency, "relocate", actor="tester")

    milestone = Milestone(
        project_id=document.project_id, code="UTIL-CLEAR", name="Utility clearance"
    )
    session.add(milestone)
    session.flush()
    link_dependency(session, dependency, milestone, actor="tester")

    merge_candidate(
        session,
        make_candidate(session, document, utility_id="FOC1-2"),
        dependency,
        principal=TEST_PRINCIPAL,
    )

    assert entries(session, dependency.id) == [
        "accept_candidate",
        "mark_satisfies_requirement",
        "set_resolution_strategy",
        "link_milestone",
        "merge_candidate",
    ]
    assert {e.actor for e in audit.trail_for_dependency(session, dependency.id)} == {
        "tester",
        "local:tester",
    }


def test_relinking_a_milestone_records_the_date_it_moved(session, document):
    """`need_date` drives DUE_SOON and ORPHAN and lands in every recorded
    run, so a relink moves published numbers. It used to write nothing."""
    from datetime import date

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    first = Milestone(
        project_id=document.project_id,
        code="M1",
        name="First",
        need_date=date(2026, 6, 1),
    )
    second = Milestone(
        project_id=document.project_id,
        code="M2",
        name="Second",
        need_date=date(2026, 9, 1),
    )
    session.add_all([first, second])
    session.flush()

    link_dependency(session, dependency, first, actor="tester")
    link_dependency(session, dependency, second, actor="scheduler")

    relinks = [
        e
        for e in audit.trail_for_dependency(session, dependency.id)
        if e.action == "link_milestone"
    ]
    assert [e.actor for e in relinks] == ["tester", "scheduler"]
    assert relinks[1].before_json["need_date"] == "2026-06-01"
    assert relinks[1].after_json["need_date"] == "2026-09-01"
    assert relinks[1].after_json["milestone_code"] == "M2"


def test_the_edit_trail_appears_on_the_record_it_produced(session, document):
    """The defect this seam closes.

    A reviewer edits before the Dependency exists, so the entry is written
    against the Candidate — and the detail view queried only `dependency`
    entries. What the extractor originally said, which the edit-accept
    route's docstring promises survives, was unreachable from the record.
    """
    candidate = make_candidate(session, document)
    audit.record(
        session,
        actor="reviewer",
        action="edit_candidate",
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        before={"fields": {"utility_id": "FOC1-l"}},
        after={"fields": {"utility_id": "FOC1-1"}},
    )
    dependency = accept_candidate(
        session, candidate, principal=REVIEWER_PRINCIPAL
    )

    assert entries(session, dependency.id) == ["edit_candidate", "accept_candidate"]
    assert load_dependency(session, dependency.id).audit[0].before_json == {
        "fields": {"utility_id": "FOC1-l"}
    }


def test_another_record_s_candidate_history_does_not_leak_in(session, document):
    """The join is the candidate id this Dependency actually resolved."""
    mine = make_candidate(session, document, activate=False)
    theirs = make_candidate(
        session, document, utility_id="FOC9-9", activate=False
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version=mine.prompt_version,
        candidate_count=2,
        page_errors=0,
        candidates=(mine, theirs),
        model=mine.model,
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    audit.record(
        session,
        actor="reviewer",
        action="edit_candidate",
        entity_type=audit.CANDIDATE,
        entity_id=theirs.id,
        after={"fields": {}},
    )
    dependency = accept_candidate(session, mine, principal=REVIEWER_PRINCIPAL)

    assert entries(session, dependency.id) == ["accept_candidate"]


def test_an_unknown_action_is_refused(session):
    """`entity_type` was checked against its constants; `action` was not.

    It is the column a reader filters and groups the history by, so a typo
    produced an entry that existed and could not be found.
    """
    with pytest.raises(ValueError, match="marked_satisfies"):
        audit.record(
            session,
            actor="tester",
            action="marked_satisfies",
            entity_type=audit.DEPENDENCY,
            entity_id=1,
        )


def test_dependency_candidate_replay_is_not_an_admission_action():
    assert audit.REPLAY_DEPENDENCY_CANDIDATE in audit.ACTIONS
    assert audit.REPLAY_DEPENDENCY_CANDIDATE != audit.ADMIT_DEPENDENCY


def test_reconfirmation_is_a_distinct_attributable_audit_action(
    session, document
):
    """Moving support is a Ledger mutation, but it is not Admission."""

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    entry = audit.record(
        session,
        principal=REVIEWER_PRINCIPAL,
        action=audit.RECONFIRM_OPERATIVE_SUPPORT,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={"comparison_id": 41, "finding_id": 7},
    )

    assert entry.action == "reconfirm_operative_support"
    assert entry.human_principal == REVIEWER_PRINCIPAL.subject
    assert entries(session, dependency.id) == [
        "accept_candidate",
        "reconfirm_operative_support",
    ]


def test_recording_makes_the_entry_readable_without_the_caller_flushing(
    session, document
):
    """Mutate, record, flush was three statements in five modules.

    An entry only reaches a reader once it is in the database, and every
    caller remembering the third statement is not a property.
    """
    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    audit.record(
        session,
        actor="tester",
        action=audit.SET_RESOLUTION_STRATEGY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={"resolution_strategy": "relocate"},
    )

    fetched = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == dependency.id,
            AuditLog.action == audit.SET_RESOLUTION_STRATEGY,
        )
    ).all()
    assert len(fetched) == 1


def test_creating_a_milestone_writes_an_entry(session, tmp_path):
    """Only revisions were recorded.

    The Need Date every linked Dependency inherits is set on creation, so
    the value that decides whether a record is overdue entered the ledger
    with nobody's name on it.
    """
    from corridor.milestones import import_csv

    project = Project(slug="audit-ms", name="Audit MS", is_synthetic=True)
    session.add(project)
    session.flush()
    path = tmp_path / "milestones.csv"
    path.write_text("code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-09-01\n")

    result = import_csv(
        session, project_id=project.id, path=path, actor="scheduler"
    )

    [milestone] = result.created
    [entry] = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.MILESTONE,
            AuditLog.entity_id == milestone.id,
        )
    ).all()
    assert entry.action == audit.CREATE_MILESTONE
    assert entry.actor == "scheduler"
    assert entry.after_json["need_date"] == "2026-09-01"


def test_a_demo_reset_does_not_delete_another_entity_s_history(session, document):
    """`entity_id` is not a key.

    The column holds Dependency, Candidate and Milestone ids in one
    namespace, and the reset filtered on it alone — so it deleted whatever
    Milestone history happened to share a number with a demo Dependency,
    out of a table whose own docstring says append-only.
    """
    from corridor.demo import DEMO_SLUG, DemoIsolationError, _reset

    project = session.get(Project, document.project_id)
    project.slug = DEMO_SLUG
    project.is_synthetic = True
    session.flush()

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    # A milestone entry numbered like the dependency: the collision.
    audit.record(
        session,
        actor="scheduler",
        action=audit.CREATE_MILESTONE,
        entity_type=audit.MILESTONE,
        entity_id=dependency.id,
        after={"code": "UTIL-CLEAR"},
    )

    with pytest.raises(DemoIsolationError, match="organization identity history"):
        _reset(session, project)

    survived = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.MILESTONE,
            AuditLog.entity_id == dependency.id,
        )
    ).all()
    assert len(survived) == 1
    assert len(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id == dependency.id,
            )
        ).all()
    ) == 1


# --- Referenced decision identities (#604) ---------------------------------
#
# An entry that spelled out the fields a decision changed was a second copy of
# that decision's own state.  These walk the write, the derivation, and the one
# shape a reader gets whichever way an entry is stored.

COMPARED_CHANGE_FIELDS = (
    "action",
    "entity_type",
    "entity_id",
    "actor",
    "human_principal",
    "before",
    "after",
    "readable",
)


def _dismissed(session, document, *, utility_id, reason="duplicate"):
    """One real dismissal, with the entry `dismiss_dependency` wrote for it."""

    dependency = accept_candidate(
        session,
        make_candidate(session, document, utility_id=utility_id),
        principal=TEST_PRINCIPAL,
    )
    dismiss_dependency(session, dependency, reason, principal=TEST_PRINCIPAL)
    dismissal = session.scalars(
        select(DependencyDismissal).where(
            DependencyDismissal.dependency_id == dependency.id
        )
    ).one()
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == dependency.id,
            AuditLog.action == audit.DISMISS_DEPENDENCY,
        )
    ).one()
    return dependency, dismissal, entry


def test_a_dismissal_entry_names_its_decision_and_copies_nothing(session, document):
    """The stored row holds one identity, not the fields the decision holds."""

    _, dismissal, entry = _dismissed(session, document, utility_id="FOC1-1")

    assert entry.before_json is None
    assert entry.after_json == {
        "decision_reference": {
            "kind": audit.DEPENDENCY_DISMISSAL,
            "id": dismissal.id,
        }
    }
    assert audit.decision_reference(entry) == audit.DecisionIdentity(
        kind=audit.DEPENDENCY_DISMISSAL, identity=dismissal.id
    )


def test_an_old_field_map_and_a_new_reference_read_as_the_same_change(
    session, document
):
    """The criterion the whole change rests on.

    Both rows below describe the *same* dismissal: one as the bytes
    `dismiss_dependency` wrote before #604, one as the reference it writes
    now.  A reader must not be able to tell them apart from the change they
    report — only from `derived`, which is about storage and not about what
    happened.
    """

    dependency, dismissal, referencing = _dismissed(
        session, document, utility_id="FOC1-1"
    )
    legacy = AuditLog(
        actor=TEST_PRINCIPAL.subject,
        human_principal=TEST_PRINCIPAL.subject,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        before_json=None,
        # The exact payload the pre-#604 writer produced for this dismissal.
        after_json={"reason": "duplicate", "dependency_dismissal_id": dismissal.id},
    )
    session.add(legacy)
    session.flush()

    old, new = audit.recorded_changes(session, (legacy, referencing))

    assert [getattr(old, name) for name in COMPARED_CHANGE_FIELDS] == [
        getattr(new, name) for name in COMPARED_CHANGE_FIELDS
    ]
    assert old.after == {
        "reason": "duplicate",
        "dependency_dismissal_id": dismissal.id,
    }
    assert old.before == new.before == {}
    assert (old.derived, new.derived) == (False, True)
    assert old.decision_identity is None
    assert new.decision_identity == audit.DecisionIdentity(
        kind=audit.DEPENDENCY_DISMISSAL, identity=dismissal.id
    )


def test_deriving_a_change_never_rewrites_the_entry_that_carried_it(
    session, document
):
    """Historical rows keep what they recorded; reading is not a migration."""

    dependency, dismissal, _ = _dismissed(session, document, utility_id="FOC1-1")
    stored = {"reason": "duplicate", "dependency_dismissal_id": dismissal.id}
    legacy = AuditLog(
        actor=TEST_PRINCIPAL.subject,
        human_principal=TEST_PRINCIPAL.subject,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after_json=dict(stored),
    )
    session.add(legacy)
    session.flush()

    change = audit.recorded_change(session, legacy)
    change.after["reason"] = "wrong"
    change.after.pop("dependency_dismissal_id")

    # The reader hands back its own maps, so the entry a caller is holding is
    # still what was recorded...
    assert legacy.after_json == stored
    session.flush()
    session.expire_all()
    # ...and so is the row itself.
    assert session.get(AuditLog, legacy.id).after_json == stored


def test_a_reference_to_a_decision_that_is_not_there_is_unreadable(
    session, document
):
    """Fail closed. An absent decision is corruption, not an empty change."""

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    missing = session.scalar(select(func.max(DependencyDismissal.id))) or 0
    entry = audit.record(
        session,
        principal=TEST_PRINCIPAL,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.DEPENDENCY_DISMISSAL, identity=missing + 1
        ),
    )

    change = audit.recorded_change(session, entry)

    assert change.readable is False
    assert change.derived is True
    assert (change.before, change.after) == ({}, {})


def test_a_reference_to_another_record_s_decision_is_unreadable(session, document):
    """The named decision must be about the entity the entry names."""

    _, dismissal, _ = _dismissed(session, document, utility_id="FOC1-1")
    other = accept_candidate(
        session,
        make_candidate(session, document, utility_id="FOC1-2"),
        principal=TEST_PRINCIPAL,
    )
    entry = audit.record(
        session,
        principal=TEST_PRINCIPAL,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=other.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.DEPENDENCY_DISMISSAL, identity=dismissal.id
        ),
    )

    assert audit.recorded_change(session, entry).readable is False


def test_a_malformed_reference_is_never_read_as_a_stated_field_map(
    session, document
):
    """The envelope must not come back to a reader as though it were the change."""

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    entry = AuditLog(
        actor=TEST_PRINCIPAL.subject,
        human_principal=TEST_PRINCIPAL.subject,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after_json={"decision_reference": {"kind": "dependency_dismissal"}},
    )
    session.add(entry)
    session.flush()

    change = audit.recorded_change(session, entry)

    assert audit.names_a_decision(entry) is True
    assert audit.decision_reference(entry) is None
    assert change.readable is False
    assert change.after == {}


def test_a_reference_whose_kind_contradicts_its_action_is_refused(session, document):
    """One action references one kind, at the write and at the read."""

    dependency, _, _ = _dismissed(session, document, utility_id="FOC1-1")
    settlement = DisputeSettlement(
        dependency_id=dependency.id,
        field_name="station_to",
        settled_value="1105+00",
        settled_by=TEST_PRINCIPAL.subject,
        covers_assertion_id=1,
    )
    session.add(settlement)
    session.flush()

    with pytest.raises(ValueError, match="does not reference"):
        audit.record(
            session,
            principal=TEST_PRINCIPAL,
            action=audit.DISMISS_DEPENDENCY,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency.id,
            decided_by=audit.DecisionIdentity(
                kind=audit.DISPUTE_SETTLEMENT, identity=settlement.id
            ),
        )

    mismatched = AuditLog(
        actor=TEST_PRINCIPAL.subject,
        human_principal=TEST_PRINCIPAL.subject,
        action=audit.DISMISS_DEPENDENCY,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after_json={
            "decision_reference": {
                "kind": audit.DISPUTE_SETTLEMENT,
                "id": settlement.id,
            }
        },
    )
    session.add(mismatched)
    session.flush()

    assert audit.recorded_change(session, mismatched).readable is False


def test_a_named_decision_is_written_instead_of_a_field_map_never_beside_one(
    session, document
):
    """Two statements of one change is the defect; the write refuses it."""

    dependency, dismissal, _ = _dismissed(session, document, utility_id="FOC1-1")

    with pytest.raises(ValueError, match="replaces the field map"):
        audit.record(
            session,
            principal=TEST_PRINCIPAL,
            action=audit.DISMISS_DEPENDENCY,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency.id,
            after={"reason": "duplicate"},
            decided_by=audit.DecisionIdentity(
                kind=audit.DEPENDENCY_DISMISSAL, identity=dismissal.id
            ),
        )


def test_a_settlement_entry_derives_the_conclusion_the_settlement_holds(
    session, document
):
    """Every field the old entry copied comes back out of the settlement row."""

    dependency = accept_candidate(
        session, make_candidate(session, document), principal=TEST_PRINCIPAL
    )
    settlement = DisputeSettlement(
        dependency_id=dependency.id,
        field_name="station_to",
        settled_value=None,
        settled_by=TEST_PRINCIPAL.subject,
        covers_assertion_id=4321,
    )
    session.add(settlement)
    session.flush()
    entry = audit.record(
        session,
        principal=TEST_PRINCIPAL,
        action=audit.SETTLE_DISPUTE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.DISPUTE_SETTLEMENT, identity=settlement.id
        ),
    )

    change = audit.recorded_change(session, entry)

    assert change.readable is True
    assert change.before == {}
    # The exact map `settle_dispute` stored before #604, settled-as-nothing
    # included: a null conclusion is a conclusion, not a missing key.
    assert change.after == {
        "field_name": "station_to",
        "settled_value": None,
        "covers_assertion_id": 4321,
        "dispute_settlement_id": settlement.id,
    }


def test_a_not_relevant_entry_derives_the_disposition_it_names(session, document):
    """The transition and the confirmation belong to the act, not to storage."""

    candidate = make_candidate(session, document)
    disposition = CandidateDisposition(
        candidate_id=candidate.id,
        disposition="not_relevant",
        reason="duplicate-statement",
        recorded_by=REVIEWER_PRINCIPAL.subject,
    )
    session.add(disposition)
    session.flush()
    entry = audit.record(
        session,
        principal=REVIEWER_PRINCIPAL,
        action=audit.MARK_STATEMENT_NOT_RELEVANT,
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.CANDIDATE_DISPOSITION, identity=disposition.id
        ),
    )

    change = audit.recorded_change(session, entry)

    assert change.before == {"candidate_state": "pending"}
    assert change.after == {
        "candidate_state": "rejected",
        "candidate_disposition_id": disposition.id,
        "reason": "duplicate-statement",
        "confirmed": True,
    }


def test_a_disposition_that_is_not_not_relevant_is_unreadable(session, document):
    """An accepted disposition under this action is corrupt, not a variant."""

    candidate = make_candidate(session, document)
    disposition = CandidateDisposition(
        candidate_id=candidate.id,
        disposition="accepted",
        reason=None,
        recorded_by=REVIEWER_PRINCIPAL.subject,
    )
    session.add(disposition)
    session.flush()
    entry = audit.record(
        session,
        principal=REVIEWER_PRINCIPAL,
        action=audit.MARK_STATEMENT_NOT_RELEVANT,
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        decided_by=audit.DecisionIdentity(
            kind=audit.CANDIDATE_DISPOSITION, identity=disposition.id
        ),
    )

    assert audit.recorded_change(session, entry).readable is False


def test_a_named_decision_still_blocks_a_compensating_undo(session, document):
    """The Undo guard reads a reference through the key it used to be spelled as.

    Without the translation the envelope's bare `id` would match any typed key
    a caller happened to pass, and the typed key it actually stands for would
    match nothing — the guard would refuse the wrong reversals and permit the
    dangerous ones.
    """

    _, dismissal, entry = _dismissed(session, document, utility_id="FOC1-1")

    assert audit.references_typed_ids(
        entry.after_json, {"dependency_dismissal_id": {dismissal.id}}
    )
    assert not audit.references_typed_ids(
        entry.after_json, {"dependency_dismissal_id": {dismissal.id + 1}}
    )
    assert not audit.references_typed_ids(entry.after_json, {"id": {dismissal.id}})
    assert not audit.references_typed_ids(
        entry.after_json, {"dispute_settlement_id": {dismissal.id}}
    )
