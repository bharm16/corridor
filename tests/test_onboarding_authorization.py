"""The limited onboarding authorization, and the product path it permits (#827).

ADR-0099 decides that everything before authoritative activation runs under a
bounded permission the control plane issues, that the coordinator's adoption is
their own attributable act inside it, and that the permission's consumption and
the retained proof of its validity are one atomic customer-side commit.

Every scenario below is written against a real PostgreSQL database, because
every rule the decision states is enforced by a command or a constraint rather
than by the Python that calls one. Where a case is about what a *different*
credential may do, it reads as that credential.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from corridor import processing_holds
from corridor import access, audit, intake_hardening
from corridor.baseline_adoption import (
    ANSWER_EFFECTS,
    BLOCKING_QUESTION_KINDS,
    PERMITTED_ANSWERS,
    BaselineAnswerRefused,
    QuestionAnswer,
    RetainedPreview,
    adopt_retained_baseline,
    adoption_material_payload,
    latest_baseline_reading,
    prepare_baseline_reading,
    prove_answers,
    retained_baseline_reading,
)
from corridor.config import settings
from corridor.field_mapping_manifest import DEMO_EXTERNAL_REFERENCES, MappingDeclaration
from corridor.migrations.source_append_commands import onboarding_authorization as frozen
from corridor.models import (
    AuditLog,
    OnboardingAct,
    OnboardingGrant,
    OnboardingPreview,
    Project,
)
from corridor.onboarding_authorization import (
    ADOPT_BASELINE,
    APPROVE_ISSUE_PROFILE,
    INSPECT_COMPATIBILITY,
    ONBOARDING_ACT_OPERATIONS,
    ONBOARDING_EVENT_KINDS,
    ONBOARDING_OPERATIONS,
    REVALIDATION_WINDOW,
    WITHDRAWAL_REQUESTED_NOTICE,
    OnboardingRefused,
    canonical_material_digest,
    check_onboarding_act,
    commit_onboarding_act,
    completed_act,
    held_grant,
    onboarding_standing,
    permitted_operations_now,
    record_onboarding_event,
    record_onboarding_grant,
    require_onboarding_permission,
    withdrawal_record,
)
from corridor.operating_mode import ADOPTED_BASELINE, project_operating_mode
from corridor.principals import HumanPrincipal
from corridor.source_intake import validate_and_stage

from access_support import seed_membership
from harness_support import as_role


COORDINATOR = HumanPrincipal("local:onboarding-coordinator")
OPERATOR = HumanPrincipal("local:onboarding-operator")
OPERATIONS_ACTOR = "operations:deployment-desk"
DEMO = MappingDeclaration(external_references=DEMO_EXTERNAL_REFERENCES)

AT = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)

HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Size",
    "Material",
    "Station Origin",
    "Start Station",
    "End Station",
    "Resolution Strategy Selected (from Resolution Alternatives)",
    "Promised For",
    "Action Due Date",
    "Comment",
    "UCM Record ID",
    "Record URL",
    "Early TxDOT Utility Activity",
]

# Two ordinary rows on one station origin, so the only coordinator question a
# clean workbook raises is the bulk scope confirmation ADR-0076 makes the
# adoption act itself. Synthetic throughout (ADR-0046).
ROWS = [
    ["UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel", "SR-BL",
     "1149+00", "1150+00", "Relocate", "2026-03-01", "2026-02-01",
     "pole at station", "UCM-1001", "https://ucm.example/records/1001", "Yes"],
    ["UC-2", "City of Austin", "Water", "8 in", "PVC", "SR-BL",
     "1160+00", "1161+00", "Adjust", "2026-04-01", "2026-03-01",
     "", "UCM-1002", "", ""],
]


# --- the fixtures, and nothing that pre-performs an act ---------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def workbook(tmp_path, name="ucm.xlsx", rows=None) -> bytes:
    path = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(list(HEADINGS))
    for row in ROWS if rows is None else rows:
        sheet.append(list(row))
    book.save(path)
    return path.read_bytes()


@pytest.fixture
def onboarding(session, tmp_path, store):
    """A provisioned project, a coordinator, and a recorded onboarding grant.

    The grant is recorded by the operations capability's own command, which is
    how it arrives in a real environment; nothing here previews, prepares or
    adopts anything, because those are the acts under test.
    """

    project = Project(slug=f"onboard-{uuid4().hex[:8]}", name="Onboarding", is_synthetic=True)
    session.add(project)
    session.flush()
    seed_membership(session, project, COORDINATOR, designations=[access.COORDINATION])
    grant_id = record_onboarding_grant(
        session,
        project_id=int(project.id),
        authorization_id="loa-2026-05",
        grant_version=1,
        customer="lone-star-transit",
        environment="pilot-1",
        permitted_operations=ONBOARDING_OPERATIONS,
        source_scope="ucm workbook revisions for project onboard",
        governing_authorization_identity="customer-authorization-7",
        governing_authorization_version="2026-04-01",
        evidence_identity="s3://authorizations/customer-authorization-7.pdf",
        evidence_sha256="a" * 64,
        issued_at=AT - timedelta(minutes=5),
        expires_at=AT + timedelta(days=14),
        issued_by_actor=OPERATIONS_ACTOR,
        recorded_by_actor=OPERATIONS_ACTOR,
    )
    session.flush()
    return project, grant_id


def prepared(session, project, tmp_path, *, at=AT, name="ucm.xlsx", rows=None):
    staged = validate_and_stage(workbook(tmp_path, name=name, rows=rows), name)
    return prepare_baseline_reading(
        session,
        project=project,
        staged=staged,
        customer="Lone Star Transit Authority",
        source_identity="UCM workbook revision C",
        principal=COORDINATOR,
        at=at,
        field_mapping=DEMO,
        images_dir=tmp_path / "images",
    )


def confirmations(retained: RetainedPreview) -> tuple[QuestionAnswer, ...]:
    """The one permitted answer to each blocking question this reading asks."""

    answers = []
    for question in retained.questions:
        if question.kind not in BLOCKING_QUESTION_KINDS:
            continue
        answer = PERMITTED_ANSWERS[question.kind][0]
        answers.append(
            QuestionAnswer(
                kind=question.kind,
                subject=question.subject,
                answer=answer,
                choice=question.subject if answer == "confirm_basis" else "",
            )
        )
    return tuple(answers)


@contextmanager
def refusal(session, kind=OnboardingRefused):
    """A refusal walk, held in a savepoint so the transaction survives it.

    PostgreSQL runs nothing at all in a failed transaction, and every refusal
    below is raised by a command rather than by Python, so the assertions that
    follow one need the savepoint's rollback to have happened first.
    """

    marker = session.begin_nested()
    try:
        with pytest.raises(kind) as caught:
            yield caught
    finally:
        marker.rollback()


def adopt(session, retained, *, key="adopt-1", at=AT, answers=None):
    return adopt_retained_baseline(
        session,
        retained=retained,
        principal=COORDINATOR,
        answers=confirmations(retained) if answers is None else answers,
        request_key=key,
        at=at,
    )


# --- the vocabulary the migration froze -------------------------------------


def test_the_migration_and_the_application_name_the_same_operations():
    """A frozen copy that drifts is a permission nobody can grant or refuse.

    The revision is source bytes and may not import the application, so it
    carries its own copy of the three closed sets. This is the test that keeps
    the copy honest, the same way #680's boundary copies are kept honest.
    """

    assert frozen.ONBOARDING_OPERATIONS == ONBOARDING_OPERATIONS
    assert frozen.ONBOARDING_ACT_OPERATIONS == ONBOARDING_ACT_OPERATIONS
    assert frozen.ONBOARDING_EVENT_KINDS == ONBOARDING_EVENT_KINDS
    assert frozen.BLOCKING_QUESTION_KINDS == BLOCKING_QUESTION_KINDS


def test_every_question_kind_has_permitted_answers_with_stated_effects():
    """A question a coordinator cannot answer is a screen they cannot finish."""

    from corridor.baseline_adoption import COORDINATOR_QUESTION_KINDS

    assert set(PERMITTED_ANSWERS) == set(COORDINATOR_QUESTION_KINDS)
    for kind, answers in PERMITTED_ANSWERS.items():
        assert answers, kind
        for answer in answers:
            assert ANSWER_EFFECTS[answer], answer
    assert set(BLOCKING_QUESTION_KINDS) <= set(COORDINATOR_QUESTION_KINDS)


# --- custody: who may permit, and who may not -------------------------------


def test_the_web_capability_cannot_record_or_annotate_an_onboarding_grant(
    session, onboarding
):
    """A coordinator cannot self-authorize, whatever their designation says.

    ADR-0099 separates the grant that says a person may act inside a customer
    database from the permission that says the database may be processed at
    all. The second is the operations actor's, and this is where that is
    enforced rather than described: the web login holds no execute on either
    command and no write on the relation.
    """

    project, grant_id = onboarding
    with as_role(session, "corridor_web"):
        with refusal(session, DBAPIError) as refused:
            session.execute(
                select(
                    func.record_onboarding_grant(
                        int(project.id), "self-issued", 2, "c", "e",
                        ["adopt_baseline"], "scope", "gov", "v1", "ev", "b" * 64,
                        AT, AT + timedelta(days=1), "coordinator", "coordinator",
                    )
                )
            )
        assert "permission denied" in str(refused.value).lower()


def test_the_web_capability_cannot_write_the_relations_behind_the_grant(
    session, onboarding
):
    """Not even through a raw insert: the guard names the command's own role."""

    project, grant_id = onboarding
    with as_role(session, "corridor_web"):
        with refusal(session, DBAPIError):
            session.execute(
                text(
                    "insert into project_onboarding_acts "
                    "(project_id, grant_id, authorization_id, grant_version, "
                    "operation, request_key, material_sha256, principal, "
                    "committed_at, validity, result) values "
                    "(:p, :g, 'x', 1, 'adopt_baseline', 'k', :d, 'p', now(), "
                    "'{}'::jsonb, '{}'::jsonb)"
                ),
                {"p": int(project.id), "g": grant_id, "d": "c" * 64},
            )


# --- what the standing says, and when it fails closed -----------------------


def test_a_project_with_no_grant_refuses_every_onboarding_operation(session, project):
    """Fail closed: no authorization is not a quieter kind of permission."""

    for operation in ONBOARDING_OPERATIONS:
        standing = onboarding_standing(
            session, project_id=int(project.id), operation=operation, at=AT
        )
        assert standing.permitted is False
        assert standing.reason == "no_onboarding_authorization"


def test_an_expired_grant_refuses_a_new_act_and_keeps_its_recorded_terms(
    session, onboarding
):
    project, _ = onboarding
    late = AT + timedelta(days=15)
    with refusal(session) as refused:
        require_onboarding_permission(
            session, project_id=int(project.id), operation=ADOPT_BASELINE, at=late
        )
    assert refused.value.code == "onboarding_authorization_expired"
    assert held_grant(session, int(project.id)).authorization_id == "loa-2026-05"


def test_a_positive_result_is_good_for_the_stated_window_and_no_longer(
    session, onboarding
):
    """No offline grace. A cached yes is not a permission (ADR-0099)."""

    project, grant_id = onboarding
    issued = held_grant(session, int(project.id)).issued_at
    inside = issued + REVALIDATION_WINDOW - timedelta(minutes=1)
    outside = issued + REVALIDATION_WINDOW + timedelta(minutes=1)

    assert onboarding_standing(
        session, project_id=int(project.id), operation=ADOPT_BASELINE, at=inside
    ).permitted
    stale = onboarding_standing(
        session, project_id=int(project.id), operation=ADOPT_BASELINE, at=outside
    )
    assert stale.permitted is False
    assert stale.reason == "onboarding_authorization_revalidation_required"

    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="revalidated",
        executed_by_actor=OPERATIONS_ACTOR,
        executed_at=outside,
    )
    session.flush()
    assert onboarding_standing(
        session, project_id=int(project.id), operation=ADOPT_BASELINE, at=outside
    ).permitted


# --- clarification 3: a superseded governing authorization ------------------


def test_a_superseded_governing_authorization_makes_the_grant_stale_for_writes(
    session, onboarding, tmp_path
):
    """History stays valid; new protected writes need explicit revalidation.

    The replacement document is not read, compared, or inferred from. Nothing
    here decides that a difference is "probably just a typo": the grant is
    stale for new writes until operations records a current one.
    """

    project, grant_id = onboarding
    retained = prepared(session, project, tmp_path)
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="governing_authorization_superseded",
        executed_by_actor=OPERATIONS_ACTOR,
        executed_at=AT,
        reason="customer-authorization-7 replaced by customer-authorization-8",
    )
    session.flush()

    with refusal(session) as refused:
        adopt(session, retained)
    assert refused.value.code == "governing_authorization_superseded"

    # Reissuing the permission is one act. It does not require re-adopting a
    # valid baseline, and it inherits neither more nor less than it names.
    record_onboarding_grant(
        session,
        project_id=int(project.id),
        authorization_id="loa-2026-05",
        grant_version=2,
        customer="lone-star-transit",
        environment="pilot-1",
        permitted_operations=(INSPECT_COMPATIBILITY, ADOPT_BASELINE),
        source_scope="ucm workbook revisions for project onboard",
        governing_authorization_identity="customer-authorization-8",
        governing_authorization_version="2026-05-01",
        evidence_identity="s3://authorizations/customer-authorization-8.pdf",
        evidence_sha256="b" * 64,
        issued_at=AT,
        expires_at=AT + timedelta(days=14),
        issued_by_actor=OPERATIONS_ACTOR,
        recorded_by_actor=OPERATIONS_ACTOR,
    )
    session.flush()

    result = adopt(session, retained)
    assert result.created
    assert (
        onboarding_standing(
            session,
            project_id=int(project.id),
            operation=APPROVE_ISSUE_PROFILE,
            at=AT,
        ).permitted
        is False
    ), "the replacement grant permits only what it names"


# --- clarification 4: a withdrawal, told three ways -------------------------


def test_a_requested_withdrawal_is_not_described_as_enforced(session, onboarding):
    """Effective and enforcement state are two facts, and the words say so."""

    project, grant_id = onboarding
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes, records custodian",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT + timedelta(minutes=2),
        reason="customer paused processing pending counsel review",
    )
    session.flush()

    record = withdrawal_record(session, int(project.id))
    assert record.requested and not record.enforced
    assert record.coordinator_notice == WITHDRAWAL_REQUESTED_NOTICE
    assert "withdrawn" not in record.coordinator_notice.lower()
    assert "pending" in record.customer_acknowledgement
    operations = record.operations_record()
    assert operations["requested_by"] == "Dana Reyes, records custodian"
    assert operations["executed_by"] == "security:duty-officer"
    assert operations["requested_at"] == AT
    assert operations["enforcement_state"] == "pending"
    assert operations["governing_authorization_identity"] == "customer-authorization-7"

    # And the customer environment stops starting new protected work at once.
    with refusal(session) as refused:
        require_onboarding_permission(
            session, project_id=int(project.id), operation=ADOPT_BASELINE, at=AT
        )
    assert refused.value.code == "onboarding_authorization_withdrawn"


def test_an_enforced_withdrawal_names_the_time_and_any_outstanding_failure(
    session, onboarding
):
    project, grant_id = onboarding
    enforced_at = AT + timedelta(minutes=6)
    for kind, extra in (
        (
            "withdrawal_requested",
            {
                "requested_by": "Dana Reyes",
                "requested_at": AT,
                "reason": "customer paused processing",
            },
        ),
        ("withdrawal_enforcement_failed", {"detail": "shadow replica unreachable"}),
        ("withdrawal_enforced", {}),
    ):
        record_onboarding_event(
            session,
            project_id=int(project.id),
            grant_id=grant_id,
            kind=kind,
            executed_by_actor="security:duty-officer",
            executed_at=enforced_at,
            **extra,
        )
    session.flush()

    record = withdrawal_record(session, int(project.id))
    assert record.enforced
    assert enforced_at.isoformat() in record.coordinator_notice
    assert "disabled" in record.coordinator_notice
    assert record.operations_record()["enforcement_state"] == "enforced"
    # The failure was recorded before the enforcement that answered it, so it
    # is no longer outstanding.
    assert record.operations_record()["outstanding_failure"] == ""


def test_the_coordinator_notice_names_this_project_and_nothing_wider(
    session, onboarding
):
    """The third audience gets why it is paused and what is still available."""

    project, grant_id = onboarding
    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT,
        reason="counsel review",
    )
    session.flush()

    record = withdrawal_record(session, int(project.id))
    notice = record.coordinator_notice
    grant = held_grant(session, int(project.id))
    assert grant.evidence_identity not in notice
    assert grant.evidence_sha256 not in notice
    assert grant.customer not in notice
    assert grant.environment not in notice
    assert permitted_operations_now(session, project_id=int(project.id), at=AT) == ()


# --- where the parsing happens ----------------------------------------------


def test_the_reading_is_prepared_once_and_the_approval_opens_no_workbook(
    session, onboarding, tmp_path, monkeypatch
):
    """#893's mistake, not repeated: the committing request reads no file.

    The preparation pass is where the workbook is opened, the Document is
    registered and its Source Segments are written. The approval request
    verifies retained identities and writes. This asserts it by making the
    workbook reader raise if anything calls it after preparation.
    """

    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    assert retained.document_id

    import corridor.baseline_adoption as module

    def refuse(*args, **kwargs):
        raise AssertionError("the approval request opened the workbook")

    monkeypatch.setattr(module, "read_baseline_workbook", refuse)
    monkeypatch.setattr(module, "preview_baseline_adoption", refuse)

    result = adopt(session, retained)
    assert result.created
    assert project_operating_mode(session, int(project.id)) == ADOPTED_BASELINE


def test_the_approval_request_stays_short_as_the_workbook_grows(
    session, onboarding, tmp_path
):
    """The measured property, on the input that used to break it.

    A 120-row workbook is about 1,300 Source Facts and as many Support
    Assessments. While the capture ran inside the approval request, adopting it
    took 14.7 seconds and scaled with the customer's workbook; with the capture
    where ADR-0076 already puts it -- with the reading, not with the decision --
    it is a fixed handful of round trips and measured 0.14 seconds.

    The ceiling below is deliberately loose: this is a round-trip budget on a
    developer machine, not a benchmark. What it catches is the regression that
    matters -- an adoption whose cost grows with the workbook again.
    """

    rows = [
        [f"UC-{index}", "CenterPoint Energy", "Electric", "12 in", "Steel",
         "SR-BL", "1149+00", "1150+00", "Relocate", "2026-03-01", "2026-02-01",
         "pole", f"UCM-{index}", "", ""]
        for index in range(1, 121)
    ]
    project, _ = onboarding
    retained = prepared(session, project, tmp_path, rows=rows)
    assert len(retained.fact_ids) > 1000, "the workbook under test is not large"

    started = time.perf_counter()
    adopt(session, retained)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"the approval request took {elapsed:.3f}s"


# --- the answers, and what each one does ------------------------------------


def test_an_unresolved_blocking_question_cannot_be_bypassed_by_adopting(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    with refusal(session, BaselineAnswerRefused) as refused:
        adopt(session, retained, answers=())
    assert "decided before the baseline is adopted" in str(refused.value)
    assert completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE) is None


def test_an_answer_a_question_does_not_have_is_refused(session, onboarding, tmp_path):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    scope = next(item for item in retained.questions if item.kind == "adopted_scope")
    with pytest.raises(BaselineAnswerRefused):
        prove_answers(
            retained.questions,
            [QuestionAnswer(kind="adopted_scope", subject=scope.subject, answer="distinct")],
        )


def test_an_answer_that_changes_what_would_be_adopted_prepares_the_reading_again(
    session, onboarding, tmp_path
):
    """Some answers regenerate; a regenerating answer never adopts."""

    project, _ = onboarding
    retained = prepared(
        session,
        project,
        tmp_path,
        rows=[*ROWS, ["UC-4", "Not Used", "", "", "", "", "", "", "", "", "", "", "", "", ""]],
    )
    exclusion = next(
        item for item in retained.questions if item.kind == "proposed_exclusion"
    )
    with pytest.raises(BaselineAnswerRefused) as refused:
        adopt(
            session,
            retained,
            answers=(
                *confirmations(retained),
                QuestionAnswer(
                    kind="proposed_exclusion",
                    subject=exclusion.subject,
                    answer="include_row",
                ),
            ),
        )
    assert "prepares the reading again" in str(refused.value)


def test_the_receipt_binds_the_answers_and_their_effects(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    answers = confirmations(retained)
    adopt(session, retained, answers=answers)
    session.flush()

    recorded = session.scalars(
        select(AuditLog).where(AuditLog.action == audit.ADOPT_BASELINE)
    ).all()[-1]
    bound = recorded.after_json["coordinator_answers"]
    assert [item["answer"] for item in bound] == [item.answer for item in answers]
    assert all(item["effect"] for item in bound)

    act = completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE)
    assert act.material_sha256 == canonical_material_digest(
        adoption_material_payload(retained, answers)
    )


def test_the_recorded_answers_are_immutable_history(session, onboarding, tmp_path):
    """Changing the answers in a completed adoption is not a supported act."""

    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    adopt(session, retained)
    session.flush()
    act = completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE)

    with refusal(session, DBAPIError) as refused:
        session.execute(
            text(
                "update project_onboarding_acts set result = cast(:empty as jsonb) "
                "where id = :i"
            ),
            {"empty": "{}", "i": int(act.id)},
        )
    assert "immutable" in str(refused.value)


# --- clarification 2: the five submission cases -----------------------------


def test_same_key_same_payload_returns_the_original_receipt(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    first = adopt(session, retained, key="submit-1")
    session.flush()
    again = adopt(session, retained, key="submit-1")

    assert again.revision_id == first.revision_id
    assert again.adoption_id == first.adoption_id
    assert again.created is False
    assert (
        session.scalar(
            select(func.count()).select_from(OnboardingAct).where(
                OnboardingAct.project_id == int(project.id)
            )
        )
        == 1
    )


def test_same_key_different_payload_is_refused_as_conflicting_reuse(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    adopt(session, retained, key="submit-1")
    session.flush()

    scope = next(item for item in retained.questions if item.kind == "adopted_scope")
    with refusal(session) as refused:
        check_onboarding_act(
            session,
            project_id=int(project.id),
            operation=ADOPT_BASELINE,
            request_key="submit-1",
            material_sha256=canonical_material_digest({"different": scope.subject}),
            at=AT,
            preview_fingerprint=retained.binding_fingerprint,
        )
    assert refused.value.code == "conflicting_reuse"


def test_a_new_key_on_an_adopted_project_refuses_a_second_adoption(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    adopt(session, retained, key="submit-1")
    session.flush()

    with refusal(session) as refused:
        adopt(session, retained, key="submit-2")
    assert refused.value.code == "already_performed"


def test_a_rolled_back_first_request_leaves_the_permission_unconsumed(
    session, onboarding, tmp_path
):
    """A failed or rolled-back adoption consumes nothing (ADR-0099)."""

    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    session.flush()
    marker = session.begin_nested()
    adopt(session, retained, key="submit-1")
    marker.rollback()

    assert completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE) is None
    result = adopt(session, retained, key="submit-1")
    assert result.created is True


def test_an_exact_retry_does_not_require_a_live_permission_or_todays_preview(
    session, onboarding, tmp_path
):
    """Retrieval of a prior result, not the exercise of expired authority.

    Fresh cookies and a fresh request-forgery token are not adoption content,
    so a retry after the grant lapsed, and after a newer reading was prepared,
    still returns the receipt the key names.
    """

    project, grant_id = onboarding
    retained = prepared(session, project, tmp_path)
    first = adopt(session, retained, key="submit-1")
    session.flush()

    record_onboarding_event(
        session,
        project_id=int(project.id),
        grant_id=grant_id,
        kind="withdrawal_requested",
        requested_by="Dana Reyes",
        requested_at=AT,
        executed_by_actor="security:duty-officer",
        executed_at=AT,
        reason="counsel review",
    )
    session.flush()

    late = AT + timedelta(days=30)
    again = adopt(session, retained, key="submit-1", at=late)
    assert again.revision_id == first.revision_id
    assert again.created is False


def test_concurrent_identical_submissions_adopt_once(runtime_database, tmp_path, monkeypatch):
    """Two committed transactions, one adoption, and both see its result."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory

    with factory.begin() as setup:
        project = Project(
            slug=f"onboard-{uuid4().hex[:8]}", name="Onboarding", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        project_id = int(project.id)
        seed_membership(setup, project, COORDINATOR, designations=[access.COORDINATION])
        record_onboarding_grant(
            setup,
            project_id=project_id,
            authorization_id="loa-concurrent",
            grant_version=1,
            customer="lone-star-transit",
            environment="pilot-1",
            permitted_operations=ONBOARDING_OPERATIONS,
            source_scope="ucm workbook revisions",
            governing_authorization_identity="customer-authorization-7",
            governing_authorization_version="2026-04-01",
            evidence_identity="s3://authorizations/7.pdf",
            evidence_sha256="a" * 64,
            issued_at=AT - timedelta(minutes=5),
            expires_at=AT + timedelta(days=14),
            issued_by_actor=OPERATIONS_ACTOR,
            recorded_by_actor=OPERATIONS_ACTOR,
        )

    with factory.begin() as preparing:
        fingerprint = prepared(
            preparing, preparing.get_one(Project, project_id), tmp_path
        ).binding_fingerprint

    outcomes = []
    for key in ("race-a", "race-b"):
        try:
            with factory.begin() as racing:
                retained = retained_baseline_reading(
                    racing, project_id=project_id, binding_fingerprint=fingerprint
                )
                outcomes.append(adopt(racing, retained, key=key).revision_id)
        except OnboardingRefused as refused:
            outcomes.append(refused.code)

    with factory() as reading:
        assert (
            reading.scalar(
                select(func.count()).select_from(OnboardingAct).where(
                    OnboardingAct.project_id == project_id
                )
            )
            == 1
        )
        act = completed_act(reading, project_id=project_id, operation=ADOPT_BASELINE)
    assert outcomes[0] == act.result["revision_id"]
    assert outcomes[1] == "already_performed"


# --- clarification 1: what is permitted after adoption ----------------------


def test_reading_the_completed_onboarding_survives_the_consumed_permission(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    adopt(session, retained)
    session.flush()

    act = completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE)
    assert act is not None and act.result["revision_id"]
    again = retained_baseline_reading(
        session, project_id=int(project.id), binding_fingerprint=retained.binding_fingerprint
    )
    assert again is not None and again.questions


def test_a_remaining_preview_permission_cannot_produce_a_second_adoptable_baseline(
    session, onboarding, tmp_path
):
    """The one-way adopted state is not overridden by a live preview permission.

    The database decides adoptability when the reading is retained, from
    `project_operating_mode`, so a reading regenerated after adoption is
    retained for verification and refuses to be adopted -- and the permission
    that produced it is still perfectly valid, which is the point.
    """

    project, _ = onboarding
    first = prepared(session, project, tmp_path)
    adopt(session, first)
    session.flush()

    assert onboarding_standing(
        session, project_id=int(project.id), operation=INSPECT_COMPATIBILITY, at=AT
    ).permitted, "the compatibility permission is still in force"

    regenerated = prepared(session, project, tmp_path, name="ucm-again.xlsx")
    assert regenerated.adoptable is False
    with refusal(session) as refused:
        adopt(session, regenerated, key="second-adoption")
    assert refused.value.code in {"already_performed", "preview_not_adoptable"}


def test_the_bounded_setup_permission_is_its_own_named_operation(
    session, onboarding, tmp_path
):
    """Consuming the adoption permission must not strand the coordinator.

    ADR-0099's consequences require the step after adoption (#828) to have its
    own explicit bounded permission. It is a separate operation, so consuming
    `adopt_baseline` leaves it untouched, and it is consumed on its own terms.
    """

    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    adopt(session, retained)
    session.flush()

    assert onboarding_standing(
        session, project_id=int(project.id), operation=ADOPT_BASELINE, at=AT
    ).permitted, "the grant itself is untouched; the act is what was consumed"
    assert completed_act(session, project_id=int(project.id), operation=ADOPT_BASELINE)

    setup = onboarding_standing(
        session, project_id=int(project.id), operation=APPROVE_ISSUE_PROFILE, at=AT
    )
    assert setup.permitted
    assert completed_act(
        session, project_id=int(project.id), operation=APPROVE_ISSUE_PROFILE
    ) is None
    outcome = commit_onboarding_act(
        session,
        project_id=int(project.id),
        operation=APPROVE_ISSUE_PROFILE,
        request_key="profile-1",
        material_sha256=canonical_material_digest({"artifacts": ["updated_ucm"]}),
        principal=COORDINATOR.subject,
        at=AT,
        result={"profile_version": 1},
    )
    assert outcome.created


# --- #919: the conservative refusal, kept ------------------------------------


def test_a_held_source_is_refused_a_rich_onboarding_read(
    session, onboarding, tmp_path
):
    """An operation needs a valid permission *and* no prohibiting hold.

    #919 owns the typed hold and the stage-aware decision; this lane consults
    the same gate rather than inventing a second one, and keeps the
    conservative refusal in the meantime.
    """

    project, _ = onboarding
    # One set of bytes, staged twice. `openpyxl` stamps the save time into the
    # workbook, so building it a second time yields a different digest -- and
    # the gate this test is about asks whether *these exact bytes* are already
    # registered and held. Regenerating the file passed whenever both saves
    # landed in the same second and failed under load, which is what it did
    # once on CI before this.
    bytes_ = workbook(tmp_path)
    first = prepare_baseline_reading(
        session,
        project=project,
        staged=validate_and_stage(bytes_, "ucm.xlsx"),
        customer="Lone Star Transit Authority",
        source_identity="UCM workbook revision C",
        principal=COORDINATOR,
        at=AT,
        field_mapping=DEMO,
        images_dir=tmp_path / "images",
    )
    _hold_reading(session, first.document_id)
    session.flush()

    staged = validate_and_stage(bytes_, "ucm.xlsx")
    assert staged.sha256 == first.content_sha256, (
        "the gate asks about these exact bytes, so the two stagings must agree"
    )
    with refusal(session, processing_holds.ProcessingHoldInForce) as refused:
        prepare_baseline_reading(
            session,
            project=project,
            staged=staged,
            customer="Lone Star Transit Authority",
            source_identity="UCM workbook revision C",
            principal=COORDINATOR,
            at=AT,
            field_mapping=DEMO,
            images_dir=tmp_path / "images",
        )
    assert "document reading is not permitted" in str(refused.value)


def test_a_valid_permission_does_not_lift_a_hold_and_a_hold_is_not_a_permission(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    assert onboarding_standing(
        session, project_id=int(project.id), operation=INSPECT_COMPATIBILITY, at=AT
    ).permitted
    first = prepared(session, project, tmp_path)
    _hold_reading(session, first.document_id)
    session.flush()
    with refusal(session, processing_holds.ProcessingHoldInForce):
        intake_hardening.assert_staged_bytes_may_be_read_richly(
            session, project_id=int(project.id), sha256=first.content_sha256
        )


def _hold_reading(session, document_id):
    """One recorded restriction that prohibits reading this source (#919)."""

    return processing_holds.impose_hold(
        session,
        document_id=document_id,
        prohibited_stage=processing_holds.DOCUMENT_READING,
        reason_code=processing_holds.INTAKE_SECURITY_FINDING,
        reason="an intake check refused these bytes for rich reading",
        authority=processing_holds.INTAKE_SECURITY,
        imposed_by="tests.test_onboarding_authorization",
        evidence=f"documents.id={document_id}",
    )


# --- the adoption itself ----------------------------------------------------


def test_the_adoption_runs_without_the_owner_bootstrap(
    session, onboarding, tmp_path, monkeypatch
):
    """The web capability never reaches the schema-owner context (ADR-0099)."""

    import corridor.activation_runtime as runtime

    def refuse(*args, **kwargs):
        raise AssertionError("the product adoption path entered the owner bootstrap")

    monkeypatch.setattr(runtime, "owner_source_bootstrap", refuse)
    monkeypatch.setattr("corridor.baseline_adoption.limited_onboarding_authorization",
                        runtime.limited_onboarding_authorization)

    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    result = adopt(session, retained)
    assert result.created
    assert project_operating_mode(session, int(project.id)) == ADOPTED_BASELINE


def test_adopting_a_reading_this_project_never_retained_is_refused(
    session, onboarding, tmp_path
):
    project, _ = onboarding
    retained = prepared(session, project, tmp_path)
    forged = RetainedPreview(
        **{
            **{
                name: getattr(retained, name)
                for name in RetainedPreview.__dataclass_fields__
            },
            "binding_fingerprint": "f" * 64,
        }
    )
    with refusal(session) as refused:
        adopt(session, forged, key="forged")
    assert refused.value.code == "preview_not_retained"


def test_the_latest_reading_is_what_the_screen_offers(session, onboarding, tmp_path):
    project, _ = onboarding
    prepared(session, project, tmp_path)
    second = prepared(session, project, tmp_path, name="ucm-2.xlsx")
    session.flush()
    assert latest_baseline_reading(session, project_id=int(project.id)).preview_id == (
        second.preview_id
    )
