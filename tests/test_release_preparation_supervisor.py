"""A confirmed request reaches a candidate through a claimed occurrence (#690).

Every other test of this path hands the worker its inputs. The closing proof
below does not call ``run_preparation_request`` anywhere: it presses the
coordinator's own HTTP action, lets the production Due Work registry publish
and claim the occurrence, and lets the real ``InputResolver`` retrieve the
authorities the maintainer's decision names. What the test supplies is a
project, a clock and a declared instant; what it never supplies is a template,
a reading, a template binding or a first-issue behaviour. One fixture at the
very end calls the worker on purpose and says why: it is reconstructing a
candidate prepared before #690, which is the one state in which an authorized
package carries no bound reading.

**And the comparison baseline, on a project's second package (#703).** #690
built the floor chain -- previous authorized package, its candidate, that
candidate's report-preparation receipt, the exact prior watermarks -- but every
proof above authorizes at most one package, so the floor was always zero and
ADR-0086's rule could not be wrong under any of them. The last section carries
a second authorized package, a failed preparation and an unapproved candidate
between the two, so the branch that reads watermarks off the previous
authorized package actually executes against numbers a wrong rule would get
wrong.

Nothing here reads a clock. Every instant is declared, the routes take theirs
from ``get_review_clock``, and the runtime takes its from the ``Ticker`` below,
whose ``now`` returns a value this file set. The floors below are watermarks
and never timestamps: both packages are authorized at the same declared
instant, so a rule that ordered them by time could not tell which came first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from corridor.baseline_adoption import (
    BaselineAdoptionRefused,
    FormatIdentity,
    effective_baseline_formats,
    register_baseline_format,
)
from corridor.delta_resolution import ChildDecisionRequest, RecordEffect, resolve_delta
from corridor.due_work import (
    HANDLER_RELEASE_PREPARATION,
    HANDLER_REPORT_PREPARATION,
    claim_due_work,
    enqueue_due_work,
)
from corridor.models import (
    BaselineFormatObject,
    DueWorkOccurrence,
    DueWorkReceipt,
    IssueCoverageDeclaration,
    Project,
    ProjectRecordRevision,
    ReleaseCandidate,
    ReleasePackage,
    ReleasePreparationAttempt,
    ReleasePreparationPublication,
    ReleasePreparationReading,
    ReleasePreparationRequest,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
)
from corridor.release_preparation import (
    FAILED,
    PREPARED,
    PREPARING,
    preparation_standing,
    preparation_idempotency_key,
    request_preparation,
)
from corridor.release_preparation_supervisor import (
    PreparationSupervisionRefused,
    bind_report_preparation_reading,
    previous_authorized_reading,
    resolve_preparation_inputs,
    retained_output_template,
)
from corridor.release_preparation_worker import run_preparation_request
from corridor.release_authorization import (
    authorize_release_package,
    retrieve_released_artifact,
)
from corridor.native_follow_up_reading import AcceptedFollowUpPlan
from corridor.release_candidate import MIXED_READING, prepare_release_candidate
from corridor.review_packets import (
    NEEDS_COORDINATION,
    SAVED,
    CoordinationRequest,
    PacketChildRequest,
    ReviewPacketRequest,
    resolve_review_packet,
)

import test_issue_path_end_to_end as issue_path
from packet_review_support import Rendition, configure_issue, subject, support
from test_release_candidate import CHASE, SUMMARY, UCM_RENDERER, WEEKLY

# #536's end-to-end module already owns an adopted project, a coordinator, a
# designated releaser, the app on this test's own migrated database, and the
# reader that takes a form's payload out of the rendered page. Reusing them is
# the point: this file proves the *supervisor*, and it should submit exactly
# what that file's coordinator submits.
Adopted = issue_path.Adopted
adopted = issue_path.adopted
client = issue_path.client
factory = issue_path.factory
store = issue_path.store
week = issue_path.week
prose = issue_path.prose
_form = issue_path._form
# The deployed runtime, driven the way #536's end-to-end proofs drive it: the
# two released schedules, one declared tick, the retained weekly reading and
# the coordinator's own confirmation. This file proves the supervisor those
# proofs now run on, so it has to be turning the same handle.
Ticker = issue_path.Ticker
COORDINATOR = issue_path.COORDINATOR
RELEASER = issue_path.RELEASER
as_principal = issue_path.as_principal
_enable = issue_path.enable_runtime
_tick = issue_path.tick
_take_weekly_reading = issue_path.take_weekly_reading
_confirm_coverage = issue_path.confirm_coverage
_run_supervisor = issue_path.run_supervisor
WORKER_AT = issue_path.WORKER_AT


OPERATOR = HumanPrincipal("local:operator")

# The instants this file declares on top of that shared timeline.
LATER_READING_AT = datetime(2026, 3, 9, 8, 0, tzinfo=timezone.utc)
AFTER_LEASE_AT = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def _one(factory, model, adopted: Adopted):
    with factory() as reading:
        return reading.scalars(
            select(model).where(model.project_id == adopted.project_id)
        ).all()


# --- the closing proof -------------------------------------------------------


def test_a_confirmed_request_reaches_a_candidate_through_a_claimed_occurrence(
    factory, adopted, client, store
):
    """#690's closing proof, and the one thing it may not do is call the worker.

    The steps, in the order a deployment performs them: the weekly reading is
    taken through the production runtime, the coordinator confirms coverage
    through the HTTP action, the supervisor publishes one occurrence naming
    that one request, a registry handler claims it, the resolver retrieves the
    registered template object by identity and verified digest and binds one
    exact report-preparation receipt, and the candidate appears on the screen
    the coordinator was already looking at.
    """

    _enable(factory, adopted)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    requests = _one(factory, ReleasePreparationRequest, adopted)
    assert len(requests) == 1
    request_id = int(requests[0].id)

    with factory() as reading:
        assert (
            preparation_standing(reading, project_id=adopted.project_id).state
            == PREPARING
        )

    clock = Ticker(WORKER_AT)
    result = _tick(factory, clock)
    assert result is not None
    assert result.handler_key == HANDLER_RELEASE_PREPARATION, (
        "the production registry handler is what claims a published "
        f"preparation occurrence, not {result.handler_key}"
    )
    assert result.execution_outcome == "completed", result.error_code
    assert result.handler_result["outcome"] == "prepared", result.handler_result
    assert result.handler_result["request_id"] == request_id

    # One occurrence, one request, both ways.
    publications = _one(factory, ReleasePreparationPublication, adopted)
    assert len(publications) == 1
    assert int(publications[0].request_id) == request_id

    # The reading is bound to the request by identity and result digest, and
    # the receipt it names is the one the weekly pass actually retained.
    readings = _one(factory, ReleasePreparationReading, adopted)
    assert len(readings) == 1
    bound = readings[0]
    assert int(bound.request_id) == request_id
    assert bound.handler_key == HANDLER_REPORT_PREPARATION
    with factory() as reading:
        receipt = reading.get(DueWorkReceipt, int(bound.receipt_id))
        assert receipt is not None
        assert receipt.handler_key == HANDLER_REPORT_PREPARATION
        assert receipt.execution_outcome == "completed"
    # A first issue measures from zero, and from an explicit none.
    assert bound.previous_package_id is None
    assert bound.prior_delta_floor == 0
    assert bound.prior_disposition_floor == 0

    # The candidate exists, names the request's attempt, and binds the exact
    # reading in its own input declaration.
    attempts = _one(factory, ReleasePreparationAttempt, adopted)
    assert len(attempts) == 1
    assert attempts[0].outcome == "prepared"
    with factory() as reading:
        candidate = reading.get(ReleaseCandidate, int(attempts[0].candidate_id))
        assert candidate is not None
        assert f'"receipt_id":{int(bound.receipt_id)}' in (
            candidate.input_declaration
        )
        assert bound.result_sha256 in candidate.input_declaration
        assert (
            preparation_standing(reading, project_id=adopted.project_id).state
            == PREPARED
        )

    # And the coordinator sees it on the same screen, with an approval offered.
    body = week(client, adopted)
    assert "Preparing this issue" not in prose(body)
    assert _form(body, "/issue/authorize") is not None


def test_a_later_internal_reading_cannot_replace_the_one_the_request_bound(
    factory, adopted, client, store
):
    """The binding is the request's, not the project's newest.

    A second completed weekly reading arrives -- exactly what happens a week
    later, or when a schedule is re-run -- and rebinding the same request
    returns the receipt it was already bound to. Were the resolver to take
    "the latest completed receipt for this project", this request would
    prepare a different window from the one its coverage was confirmed for.
    """

    # Only the weekly schedule: this proves the *binding*, so nothing else
    # may claim the request while the second reading is taken.
    _enable(factory, adopted, supervisor=False)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    with factory() as binding:
        request = binding.scalars(
            select(ReleasePreparationRequest).where(
                ReleasePreparationRequest.project_id == adopted.project_id
            )
        ).one()
        first = bind_report_preparation_reading(
            binding, request=request, bound_at=WORKER_AT
        )
        first_receipt = int(first.receipt_id)
        binding.commit()

    # A second retained reading, taken the way the first one was.
    later = Ticker(LATER_READING_AT)
    result = _tick(factory, later)
    assert result is not None
    assert result.handler_key == HANDLER_REPORT_PREPARATION
    assert result.execution_outcome == "completed", result.error_code
    second_receipt = int(result.receipt_id)
    assert second_receipt != first_receipt

    with factory() as binding:
        request = binding.get(ReleasePreparationRequest, int(request.id))
        again = bind_report_preparation_reading(
            binding, request=request, bound_at=LATER_READING_AT
        )
        assert int(again.receipt_id) == first_receipt, (
            "the request must reuse the reading it was bound to; taking the "
            "latest receipt would move the window under a confirmed issue"
        )
        binding.rollback()

    with factory() as reading:
        assert (
            int(
                reading.scalar(
                    select(func.count())
                    .select_from(ReleasePreparationReading)
                    .where(
                        ReleasePreparationReading.project_id
                        == adopted.project_id
                    )
                )
            )
            == 1
        )


def test_a_lost_worker_is_reclaimed_and_reuses_the_frozen_reading(
    factory, adopted, client, store
):
    """A claim that vanishes leaves the occurrence reclaimable, not the section stuck.

    The first worker claims and dies without finalizing. Nothing else here
    intervenes: the lease expires, the runtime reclaims the same occurrence,
    and the retry prepares the issue from the reading the request was already
    bound to rather than from whatever has completed since.
    """

    _enable(factory, adopted)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=WORKER_AT)
    with factory() as claiming:
        with claiming.begin():
            lost = claim_due_work(
                claiming, now=WORKER_AT, owner="runtime:worker-that-died"
            )
    assert lost is not None
    with factory() as reading:
        occurrence = reading.get(DueWorkOccurrence, int(lost.occurrence_id))
        assert occurrence.state == "claimed"
        publication = reading.scalars(
            select(ReleasePreparationPublication).where(
                ReleasePreparationPublication.occurrence_id
                == int(lost.occurrence_id)
            )
        ).one()
        assert int(publication.project_id) == adopted.project_id

    # Nothing finished it, so the section is honestly still preparing.
    with factory() as reading:
        assert (
            preparation_standing(reading, project_id=adopted.project_id).state
            == PREPARING
        )

    # Past the lease, a second worker reclaims the same occurrence.
    recovered = _tick(
        factory, Ticker(AFTER_LEASE_AT), owner="runtime:worker-that-lived"
    )
    assert recovered is not None
    assert recovered.handler_key == HANDLER_RELEASE_PREPARATION
    assert recovered.occurrence_id == int(lost.occurrence_id)
    assert recovered.execution_outcome == "completed", recovered.error_code
    assert recovered.handler_result["outcome"] == "prepared"

    with factory() as reading:
        assert (
            preparation_standing(reading, project_id=adopted.project_id).state
            == PREPARED
        )
        # One reading, one attempt, one candidate: a reclaimed occurrence is
        # the same request resumed and never a second issue.
        for model in (
            ReleasePreparationReading,
            ReleasePreparationAttempt,
            ReleaseCandidate,
        ):
            assert (
                int(
                    reading.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.project_id == adopted.project_id)
                    )
                )
                == 1
            ), model.__name__


def test_a_terminal_resolver_failure_records_a_finished_attempt(
    factory, adopted, client, store
):
    """"Preparing this issue" ends, even when nothing can prepare it.

    The project has no retained weekly reading at all, so the resolver can
    prove no window. That is permanent -- retrying asks the same unanswerable
    question -- so the supervisor records a finished failed attempt straight
    away rather than leaving the Issue section waiting for a retry budget to
    run out.
    """

    # No weekly schedule at all, so there is no retained reading to bind and
    # never will be one for this request.
    _enable(factory, adopted, weekly=False)
    _confirm_coverage(client, adopted)

    result = _tick(factory, Ticker(WORKER_AT))
    assert result is not None
    assert result.handler_key == HANDLER_RELEASE_PREPARATION
    assert result.execution_outcome == "completed", result.error_code
    assert result.handler_result["outcome"] == "failed"
    assert result.handler_result["candidate_id"] is None
    assert result.safe_next_step == "inspect_preparation_attempt"

    with factory() as reading:
        standing = preparation_standing(reading, project_id=adopted.project_id)
        assert standing.state == FAILED
        assert "report-preparation reading" in (standing.reason or "")
        assert (
            int(
                reading.scalar(
                    select(func.count())
                    .select_from(ReleaseCandidate)
                    .where(ReleaseCandidate.project_id == adopted.project_id)
                )
            )
            == 0
        )



def test_a_request_the_runtime_gave_up_on_stops_saying_preparing(
    factory, adopted, client, store
):
    """#675's abandoned-request hole, closed rather than described.

    #675 said out loud that a request whose worker died reads as preparing,
    that a coordinator cannot clear it, and that nothing else appends the
    attempt that would end the wait. Here every attempt is abandoned until the
    runtime's retry budget is gone, and the next supervisor tick appends the
    terminal failed attempt: the section stops waiting, and asking again is a
    second request rather than a re-run of this one.
    """

    _enable(factory, adopted)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=WORKER_AT)

    # Three claims, each abandoned past its lease, is the whole retry budget.
    moment = WORKER_AT
    for _ in range(4):
        with factory() as claiming:
            with claiming.begin():
                claim_due_work(claiming, now=moment, owner="runtime:dies-again")
        moment = moment + timedelta(seconds=2000)

    with factory() as reading:
        publication = reading.scalars(
            select(ReleasePreparationPublication).where(
                ReleasePreparationPublication.project_id == adopted.project_id
            )
        ).one()
        occurrence = reading.get(
            DueWorkOccurrence, int(publication.occurrence_id)
        )
        assert occurrence.state == "failed", (
            "the runtime has to have given up before this test means anything"
        )
        assert (
            preparation_standing(reading, project_id=adopted.project_id).state
            == PREPARING
        )

    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=moment)

    with factory() as reading:
        standing = preparation_standing(reading, project_id=adopted.project_id)
        assert standing.state == FAILED
        assert "gave up on this preparation" in (standing.reason or "")

    # And the section offers the act again, so the coordinator is not stuck.
    body = week(client, adopted)
    assert "Preparing this issue" not in prose(body)


# --- the retained authorities, proved on their own ---------------------------


def test_the_registered_template_is_resolved_by_identity_not_by_newest_bytes(
    factory, adopted, store, tmp_path
):
    """A registered replacement wins over anything newer in the store.

    The fixture is deliberately discriminating: a *newer* workbook is retained
    for this project after the replacement template is registered, so a
    resolver that reached for "the newest workbook" would have something else
    to find and would find it.
    """

    replacement = b"a replacement output template, exact bytes" * 40
    digest = sha256(replacement).hexdigest()
    with factory() as registering:
        register_baseline_format(
            registering,
            project_id=adopted.project_id,
            identity=FormatIdentity(
                kind="output_template",
                identity="Customer standard UCM export",
                version="2026.2",
                content_sha256=digest,
            ),
            principal=OPERATOR,
            idempotency_key=f"register-{uuid4().hex[:10]}",
            template_bytes=replacement,
        )
        registering.commit()

    # Something newer, retained afterwards, with a different digest entirely.
    newer = b"a workbook that arrived later and is not the template" * 40
    store.put(
        f"{sha256(newer).hexdigest()[:2]}/{sha256(newer).hexdigest()}.xlsx",
        newer,
        sha256=sha256(newer).hexdigest(),
    )

    with factory() as reading:
        resolved = retained_output_template(
            reading, project_id=adopted.project_id, store=store
        )
    assert resolved.content == replacement
    assert resolved.content_sha256 == digest
    assert resolved.version == "2026.2"

    with factory() as reading:
        binding = reading.get(BaselineFormatObject, resolved.format_id)
        assert binding is not None
        assert binding.storage_key == resolved.storage_key
        assert binding.byte_count == len(replacement)


def test_an_output_template_is_not_registered_until_its_bytes_are_retained(
    factory, adopted, store
):
    """The registration owns the storage binding, or it does not happen.

    A registration that named a digest and supplied nothing left preparation
    with a template it could not retrieve and storage reconciliation with an
    object it did not expect. Both halves are refused here.
    """

    replacement = b"bytes that were never offered"
    digest = sha256(replacement).hexdigest()
    with factory() as registering:
        with pytest.raises(BaselineAdoptionRefused, match="exact bytes"):
            register_baseline_format(
                registering,
                project_id=adopted.project_id,
                identity=FormatIdentity(
                    kind="output_template",
                    identity="Customer standard UCM export",
                    version="2026.3",
                    content_sha256=digest,
                ),
                principal=OPERATOR,
                idempotency_key=f"register-{uuid4().hex[:10]}",
            )
        registering.rollback()

    with factory() as registering:
        with pytest.raises(BaselineAdoptionRefused, match="not the same content"):
            register_baseline_format(
                registering,
                project_id=adopted.project_id,
                identity=FormatIdentity(
                    kind="output_template",
                    identity="Customer standard UCM export",
                    version="2026.3",
                    content_sha256=digest,
                ),
                principal=OPERATOR,
                idempotency_key=f"register-{uuid4().hex[:10]}",
                template_bytes=b"different bytes entirely",
            )
        registering.rollback()

    with factory() as reading:
        effective = effective_baseline_formats(
            reading, adopted.project_id
        )["output_template"]
        assert effective.format_version != "2026.3"


def test_an_unprovable_template_object_refuses_rather_than_substituting(
    factory, adopted, store
):
    """No template is better than another customer's template.

    The registration's storage binding is taken away, and its bytes were never
    a Document, so neither proof is available. The resolver says so and stops;
    it does not reach for the adopted workbook, the newest document, or
    anything else that happens to be a workbook.
    """

    replacement = b"a replacement whose binding is about to disappear" * 20
    digest = sha256(replacement).hexdigest()
    with factory() as registering:
        registered = register_baseline_format(
            registering,
            project_id=adopted.project_id,
            identity=FormatIdentity(
                kind="output_template",
                identity="Customer standard UCM export",
                version="2026.4",
                content_sha256=digest,
            ),
            principal=OPERATOR,
            idempotency_key=f"register-{uuid4().hex[:10]}",
            template_bytes=replacement,
        )
        format_id = int(registered.id)
        registering.commit()

    with factory() as losing:
        losing.execute(
            text(
                "delete from project_baseline_format_objects "
                " where format_id = :format_id"
            ),
            {"format_id": format_id},
        )
        losing.commit()

    with factory() as reading:
        with pytest.raises(
            PreparationSupervisionRefused, match="cannot be proved"
        ):
            retained_output_template(
                reading, project_id=adopted.project_id, store=store
            )


# --- the comparison baseline, where the rule can actually be wrong -----------

# ADR-0086's rule -- only the last *authorized* package advances the external
# comparison baseline -- lives entirely in ``previous_authorized_reading``, and
# a project's first issue cannot exercise it: for the first package every floor
# is zero whether the rule is right or wrong. Everything below exists to make
# the branch that reads watermarks off the previous authorized package actually
# run, on a fixture where a wrong rule would produce a visibly different floor.
#
# Nothing here orders anything by time. The instants are declared because the
# runtime needs one, the two weekly closes are a week apart because the weekly
# cadence is, and both packages are authorized at the *same* declared instant
# on purpose: the chain is what says which is the predecessor.
FIRST_DECISIONS_AT = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
SECOND_DECISIONS_AT = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
THIRD_DECISIONS_AT = datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc)
SECOND_WORKER_AT = datetime(2026, 3, 9, 8, 5, tzinfo=timezone.utc)
THIRD_READING_AT = datetime(2026, 3, 16, 8, 0, tzinfo=timezone.utc)
THIRD_WORKER_AT = datetime(2026, 3, 16, 8, 5, tzinfo=timezone.utc)


def _resolve_one_delta(session, *, project_id: int, subject: str, at: datetime):
    """One Proposed Delta of this project, and the disposition that closes it.

    The two watermarks a reading records are maxima over exactly these two
    relations, so a project that has neither reads zero on both -- and a zero
    floor is the one value that cannot tell a right rule from a wrong one.
    Rejecting is the cheapest resolution that leaves nothing open: it writes
    the disposition the second watermark counts, and no Source Fact has to be
    invented to support a value nobody is accepting.
    """

    (delta,) = create_proposed_delta_group(
        session,
        project_id=project_id,
        source_family="REV-B",
        source_revision=f"rev-{subject}",
        deltas=[
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity=subject, field="station_from"
                ),
                accepted_value="1149+00",
                proposed_value=f"{subject}+50",
                accepted_baseline_revision=f"revision:{subject}",
            )
        ],
    )
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project_id,
            delta_id=int(delta.id),
            action="reject",
            principal=COORDINATOR,
            idempotency_key=f"reject:{subject}",
            decided_at=at,
        ),
    )
    assert outcome.status == "resolved", outcome.refusal
    return int(delta.id)


PLAN_QUESTION = "Which station does the utility hold to for UC-1?"
PLAN_RETURNS_AT = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


def _record_a_follow_up_plan(factory, adopted: Adopted, *, subject: str, at: datetime):
    """One Needs coordination outcome, the only thing that retains a plan.

    #526 writes a Follow-up Plan from a packet act and nowhere else, so this is
    what a project that has one looks like. The supervisor used to resolve
    ``follow_up_plans`` empty whatever this wrote, because the renderer's own
    twin of the reading type demanded a next-action sentence no record holds.
    """

    revision = f"plan-{subject}"
    with factory() as writing:
        (delta,) = create_proposed_delta_group(
            writing,
            project_id=adopted.project_id,
            source_family="REV-B",
            source_revision=revision,
            deltas=[
                ProposedDeltaValues(
                    change_type="modify",
                    target=ExistingSubjectTarget(
                        subject_identity=subject, field="station_from"
                    ),
                    accepted_value="1149+00",
                    proposed_value=f"{subject}+90",
                    accepted_baseline_revision=f"revision:{subject}",
                )
            ],
        )
        result = resolve_review_packet(
            writing,
            ReviewPacketRequest(
                project_id=adopted.project_id,
                grouping_rule_version="packetizer-v1",
                grouping_key_kind="source_revision",
                grouping_key=revision,
                principal=COORDINATOR,
                idempotency_key=f"plan:{subject}",
                decided_at=at,
                observed_accepted_revision_id=adopted.revision_id,
                children=(
                    PacketChildRequest(
                        delta_id=int(delta.id),
                        outcome=NEEDS_COORDINATION,
                        observed_source_revision=revision,
                        coordination=CoordinationRequest(
                            question=PLAN_QUESTION,
                            responsible_organization="City Water",
                            return_date=PLAN_RETURNS_AT,
                        ),
                    ),
                ),
            ),
        )
        assert result.status == SAVED, result
        writing.commit()
    return result


def test_the_resolved_inputs_state_the_follow_up_plans_this_project_retains(
    factory, adopted, client, store
):
    """``follow_up_plans`` is resolved from the records, not resolved empty.

    The docstring on ``resolve_preparation_inputs`` was right that a supervisor
    may not compose a next-action sentence, and wrong that the consequence is
    an empty section: the retained question, the responsible party and the
    return date are all readable, and the report states those (ADR-0084 §1).
    Nothing here composes prose.
    """

    _record_a_follow_up_plan(factory, adopted, subject="UC-1", at=FIRST_DECISIONS_AT)
    _enable(factory, adopted, supervisor=False)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    with factory() as resolving:
        request = resolving.scalars(
            select(ReleasePreparationRequest).where(
                ReleasePreparationRequest.project_id == adopted.project_id
            )
        ).one()
        bound = bind_report_preparation_reading(
            resolving, request=request, bound_at=WORKER_AT
        )
        inputs = resolve_preparation_inputs(
            resolving, request, store=store, reading=bound
        )

    (plan,) = inputs.follow_up_plans
    assert isinstance(plan, AcceptedFollowUpPlan)
    assert plan.open_question == PLAN_QUESTION
    assert plan.responsible == "City Water"
    assert plan.return_date == PLAN_RETURNS_AT
    assert plan.target_field == "station_from"
    assert plan.recorded_by == COORDINATOR.subject


def _move_the_watermarks(factory, adopted: Adopted, subjects, *, at: datetime):
    """Raise both of this project's watermarks, by resolving real deltas."""

    with factory() as writing:
        for subject in subjects:
            _resolve_one_delta(
                writing, project_id=adopted.project_id, subject=subject, at=at
            )
        writing.commit()


def _weekly_reading(factory, at: datetime) -> dict:
    """One more retained #488 reading, taken through the deployed runtime."""

    result = _tick(factory, Ticker(at))
    assert result is not None, "no weekly occurrence was due"
    assert result.handler_key == HANDLER_REPORT_PREPARATION, result.handler_key
    assert result.execution_outcome == "completed", result.error_code
    return dict(result.handler_result)


def _reading_for(factory, request_id: int) -> ReleasePreparationReading:
    with factory() as reading:
        return reading.scalars(
            select(ReleasePreparationReading).where(
                ReleasePreparationReading.request_id == int(request_id)
            )
        ).one()


def _floor(row: ReleasePreparationReading) -> tuple[int, int]:
    return (int(row.prior_delta_floor), int(row.prior_disposition_floor))


def _ceiling(row: ReleasePreparationReading) -> tuple[int, int]:
    return (int(row.through_delta_id), int(row.through_disposition_id))


def _prepare(factory, client, adopted: Adopted, *, at: datetime):
    """One coordinator confirmation, worked by the deployed supervisor."""

    _confirm_coverage(client, adopted)
    return _run_supervisor(factory, at=at)


def _authorize(client, adopted: Adopted) -> None:
    """The designated releaser approves what the section is offering."""

    approval = _form(week(client, adopted), "/issue/authorize")
    assert approval is not None, (
        "the section offered no approval, so nothing here authorized a package"
    )
    as_principal(RELEASER)
    try:
        sealed = client.post(
            f"/work/{adopted.slug}/issue/authorize", data=approval
        )
    finally:
        as_principal(COORDINATOR)
    assert sealed.status_code == 201, sealed.text


def _packages(factory, adopted: Adopted) -> list[ReleasePackage]:
    with factory() as reading:
        return list(
            reading.scalars(
                select(ReleasePackage)
                .where(ReleasePackage.project_id == adopted.project_id)
                .order_by(ReleasePackage.id)
            ).all()
        )


def _accept_station(factory, adopted, row, before, after, *, at):
    """Accept a supported value on one actual adopted workbook row."""

    with factory() as writing:
        project = writing.get_one(Project, adopted.project_id)
        revision = writing.scalar(
            select(func.max(ProjectRecordRevision.id)).where(
                ProjectRecordRevision.project_id == adopted.project_id
            )
        )
        source = Rendition(writing, project, f"station-{row}-{after}.xlsx")
        fact, segment = source.capture(
            fact_type="station_from", value=after, subject_key=subject(row)
        )
        assessment = support(writing, project, fact, segment)
        (delta,) = create_proposed_delta_group(
            writing,
            project_id=adopted.project_id,
            source_family="ucm-workbook",
            source_revision=source.document.sha256,
            deltas=[
                ProposedDeltaValues(
                    change_type="modify",
                    target=ExistingSubjectTarget(
                        subject_identity=subject(row), field="station_from"
                    ),
                    accepted_value=before,
                    proposed_value=after,
                    accepted_baseline_revision=f"revision:{revision}",
                )
            ],
        )
        outcome = resolve_delta(
            writing,
            ChildDecisionRequest(
                project_id=adopted.project_id,
                delta_id=int(delta.id),
                action="accept",
                principal=COORDINATOR,
                idempotency_key=f"accept:{delta.id}",
                decided_at=at,
                observed_accepted_revision_id=revision,
                record_effects=(RecordEffect(fact_id=fact.id),),
                support_assessment_ids=(assessment.id,),
            ),
        )
        assert outcome.status == "resolved", outcome.refusal
        writing.commit()


def _released_text(factory, package, artifact_type, store):
    with factory() as reading:
        return retrieve_released_artifact(
            reading, package, artifact_type, store=store
        ).decode("utf-8")


def _request_same_coverage(factory, prior_request_id, *, at):
    """Submit a fresh request against the same confirmed accepted revision."""

    with factory() as binding:
        prior = binding.get_one(ReleasePreparationRequest, prior_request_id)
        declaration = binding.get_one(
            IssueCoverageDeclaration, prior.coverage_declaration_id
        )
        request = request_preparation(
            binding,
            project_id=int(prior.project_id),
            declaration=declaration,
            accepted_revision_id=int(prior.accepted_revision_id),
            requested_by=COORDINATOR,
            requested_at=at,
            idempotency_key=preparation_idempotency_key(
                binding, project_id=int(prior.project_id), declaration=declaration
            ),
        )
        reading = bind_report_preparation_reading(
            binding, request=request, bound_at=at
        )
        binding.commit()
        return reading


def test_an_intervening_authorization_refuses_the_frozen_request(
    factory, adopted, client, store
):
    """Authorization can advance the predecessor without an accepted change.

    C binds to A, then the already prepared B is authorized at the same
    accepted revision C holds. Re-selecting B during candidate binding would
    give C a B-to-C detailed summary and A-to-C lifecycle counts. Only a new
    request may bind to B; replaying C must never move its frozen predecessor.
    """

    with factory() as setup:
        configure_issue(
            setup, setup.get_one(Project, adopted.project_id),
            principal=COORDINATOR, effective_from=issue_path.FEBRUARY,
            ucm=UCM_RENDERER, artifacts=(SUMMARY, WEEKLY, CHASE),
        )
        setup.commit()
    _enable(factory, adopted)
    _take_weekly_reading(factory)
    first = _prepare(factory, client, adopted, at=WORKER_AT)
    assert first.handler_result["outcome"] == "prepared", first.handler_result
    _authorize(client, adopted)
    (package_a,) = _packages(factory, adopted)

    _accept_station(
        factory, adopted, 3, "1149+00", "1149+20", at=SECOND_DECISIONS_AT
    )
    _weekly_reading(factory, LATER_READING_AT)
    second = _prepare(factory, client, adopted, at=SECOND_WORKER_AT)
    assert second.handler_result["outcome"] == "prepared", second.handler_result
    frozen = _request_same_coverage(
        factory, int(second.handler_result["request_id"]),
        at=SECOND_WORKER_AT + timedelta(seconds=10),
    )
    assert frozen.previous_package_id == package_a.id
    with factory() as approving:
        authorize_release_package(
            approving,
            project_id=adopted.project_id,
            candidate_id=int(second.handler_result["candidate_id"]),
            releaser=RELEASER,
            authorized_at=SECOND_WORKER_AT + timedelta(seconds=20),
            store=store,
        )
        approving.commit()
    _, package_b = _packages(factory, adopted)
    assert package_b.accepted_revision_id == frozen.accepted_revision_id

    refused = _run_supervisor(
        factory, at=SECOND_WORKER_AT + timedelta(minutes=1)
    )
    assert refused.handler_result["request_id"] == frozen.request_id
    assert refused.handler_result["outcome"] == "refused", refused.handler_result
    assert refused.handler_result["candidate_id"] is None
    with factory() as reading:
        attempt = reading.scalars(
            select(ReleasePreparationAttempt).where(
                ReleasePreparationAttempt.request_id == frozen.request_id
            )
        ).one()
        assert attempt.refusal_code == MIXED_READING
        assert "previous authorized package changed" in attempt.reason
        assert reading.scalar(
            select(func.count()).select_from(ReleaseCandidate).where(
                ReleaseCandidate.project_id == adopted.project_id
            )
        ) == 2
        old_request = reading.get_one(
            ReleasePreparationRequest, int(frozen.request_id)
        )
        replay = bind_report_preparation_reading(
            reading, request=old_request,
            bound_at=SECOND_WORKER_AT + timedelta(minutes=2),
        )
        assert replay.id == frozen.id
        assert replay.previous_package_id == package_a.id
        assert _floor(replay) == _floor(frozen)

    fresh = _request_same_coverage(
        factory, int(frozen.request_id),
        at=SECOND_WORKER_AT + timedelta(minutes=3),
    )
    assert fresh.request_id != frozen.request_id
    assert fresh.previous_package_id == package_b.id
    retry = _run_supervisor(
        factory, at=SECOND_WORKER_AT + timedelta(minutes=4)
    )
    assert retry.handler_result["outcome"] == "prepared", retry.handler_result
    _authorize(client, adopted)
    _, _, package_c = _packages(factory, adopted)
    assert package_c.previous_package_id == package_b.id
    summary = _released_text(factory, package_c, "accepted_change_summary", store)
    report = _released_text(factory, package_c, "weekly_coordination_report", store)
    assert "Nothing was accepted into the project record in that window" in summary
    assert "accepted as the source stated" not in report


def test_authorizing_the_first_package_during_rendering_refuses_attachment(
    factory, adopted, client, store
):
    """Explicitly no predecessor must remain true at the attach transaction."""

    _enable(factory, adopted)
    _take_weekly_reading(factory)
    first = _prepare(factory, client, adopted, at=WORKER_AT)
    assert first.handler_result["outcome"] == "prepared", first.handler_result
    frozen = _request_same_coverage(
        factory, int(first.handler_result["request_id"]),
        at=WORKER_AT + timedelta(seconds=10),
    )
    assert frozen.previous_package_id is None
    with factory() as reading:
        request = reading.get_one(ReleasePreparationRequest, int(frozen.request_id))
        inputs = resolve_preparation_inputs(
            reading, request, reading=frozen, store=store
        )

    def authorize_while_unlocked(_bound):
        with factory() as approving:
            authorize_release_package(
                approving,
                project_id=adopted.project_id,
                candidate_id=int(first.handler_result["candidate_id"]),
                releaser=RELEASER,
                authorized_at=WORKER_AT + timedelta(minutes=1),
                store=store,
            )
            approving.commit()

    outcome = prepare_release_candidate(
        factory,
        project_id=adopted.project_id,
        prepared_by=COORDINATOR,
        preparation=inputs.preparation,
        source_cutoff=request.source_cutoff,
        prepared_at=WORKER_AT + timedelta(seconds=20),
        coverage_declaration_id=int(request.coverage_declaration_id),
        templates=inputs.templates,
        first_issue_behavior=inputs.first_issue_behavior,
        template_bytes=inputs.template_bytes,
        binding=inputs.binding,
        report_receipt_id=inputs.report_receipt_id,
        report_result_sha256=inputs.report_result_sha256,
        store=store,
        on_rendered=authorize_while_unlocked,
    )
    assert not outcome.prepared
    assert outcome.refusal_code == MIXED_READING
    assert "previous authorized package changed" in outcome.refusal_reason
    assert len(_one(factory, ReleaseCandidate, adopted)) == 1
    assert len(_packages(factory, adopted)) == 1
    assert _reading_for(factory, int(frozen.request_id)).previous_package_id is None


def test_released_content_keeps_changes_since_the_authorized_issue(
    factory, adopted, client, store
):
    """Skipped issues must not consume changes in either released artifact.

    A is authorized, B is prepared but never authorized, an internal weekly
    reading closes, another value is accepted, and C is prepared and authorized.
    C's final weekly receipt counts one acceptance; its external issue owes the
    reader all three since A. The retained bytes, not the stored floors, are
    the verdict.
    """

    with factory() as setup:
        configure_issue(
            setup,
            setup.get_one(Project, adopted.project_id),
            principal=COORDINATOR,
            effective_from=issue_path.FEBRUARY,
            ucm=UCM_RENDERER,
            artifacts=(SUMMARY, WEEKLY, CHASE),
        )
        setup.commit()

    _accept_station(
        factory, adopted, 3, "1149+00", "1149+10", at=FIRST_DECISIONS_AT
    )
    _enable(factory, adopted)
    _take_weekly_reading(factory)
    first = _prepare(factory, client, adopted, at=WORKER_AT)
    assert first.handler_result["outcome"] == "prepared", first.handler_result
    _authorize(client, adopted)
    (package_a,) = _packages(factory, adopted)
    first_bytes = {
        kind: _released_text(factory, package_a, kind, store)
        for kind in ("accepted_change_summary", "weekly_coordination_report")
    }
    assert "no earlier approved issue to compare" in first_bytes["accepted_change_summary"]
    assert "no earlier approved issue to compare" in first_bytes["weekly_coordination_report"]
    assert "accepted as the source stated" not in first_bytes["weekly_coordination_report"]

    _accept_station(
        factory, adopted, 3, "1149+10", "1149+20", at=SECOND_DECISIONS_AT
    )
    _accept_station(
        factory, adopted, 4, "1160+00", "1160+30", at=SECOND_DECISIONS_AT
    )
    _weekly_reading(factory, LATER_READING_AT)
    unapproved = _prepare(factory, client, adopted, at=SECOND_WORKER_AT)
    assert unapproved.handler_result["outcome"] == "prepared", (
        unapproved.handler_result
    )
    assert len(_packages(factory, adopted)) == 1

    internal = _weekly_reading(factory, THIRD_READING_AT)
    assert internal["resolved_accepted"] == 0
    _accept_station(
        factory, adopted, 3, "1149+20", "1149+50",
        at=THIRD_READING_AT + timedelta(hours=1),
    )
    # A current accepted revision needs its own retained preparation reading.
    # This one counts only the last acceptance in the internal weekly window.
    final_at = THIRD_READING_AT + timedelta(weeks=1)
    newest = _weekly_reading(factory, final_at)
    assert newest["resolved_accepted"] == 1
    third = _prepare(factory, client, adopted, at=final_at + timedelta(minutes=5))
    assert third.handler_result["outcome"] == "prepared", third.handler_result
    _authorize(client, adopted)
    package_a_again, package_c = _packages(factory, adopted)
    assert package_c.previous_package_id == package_a.id
    assert package_c.candidate_id != unapproved.handler_result["candidate_id"]

    summary = _released_text(factory, package_c, "accepted_change_summary", store)
    assert "3 changes were accepted" in summary
    for before, after in (
        ("1149+10", "1149+20"),
        ("1160+00", "1160+30"),
        ("1149+20", "1149+50"),
    ):
        assert f"showed {before}; it now shows {after}" in summary
    assert "showed 1149+00; it now shows 1149+10" not in summary
    report = _released_text(factory, package_c, "weekly_coordination_report", store)
    assert "3 proposed changes accepted as the source stated them" in report
    assert "Since the last approved issue" in report
    assert f"previous approved issue {package_a.id}," in report
    assert "Since the last weekly reading" not in report

    # Neither receipt nor released bytes can be rewritten to make the windows
    # agree. The weekly result remains one acceptance; A remains its old issue.
    bound = _reading_for(factory, int(third.handler_result["request_id"]))
    with factory() as reading:
        receipt = reading.get_one(DueWorkReceipt, int(bound.receipt_id))
        assert receipt.handler_result_json == newest
        request = reading.get_one(ReleasePreparationRequest, int(bound.request_id))
        frozen = resolve_preparation_inputs(
            reading, request, reading=bound, store=store
        )
        assert frozen.preparation["resolved_accepted"] == 3
        assert frozen.preparation["proposed_new"] == 3

    # A later accepted value and weekly close arm the replay check: both new
    # watermarks exceed C's ceilings, while C must still render its three.
    _accept_station(
        factory, adopted, 4, "1160+30", "1160+90",
        at=final_at + timedelta(days=1),
    )
    later_at = final_at + timedelta(weeks=1)
    later = _weekly_reading(factory, later_at)
    assert later["through_delta_id"] > bound.through_delta_id
    assert later["through_disposition_id"] > bound.through_disposition_id
    with factory() as reading:
        request = reading.get_one(ReleasePreparationRequest, int(bound.request_id))
        replay_bound = bind_report_preparation_reading(
            reading, request=request, bound_at=later_at
        )
        replay = resolve_preparation_inputs(
            reading, request, reading=replay_bound, store=store
        )
        assert replay_bound.id == bound.id
        assert replay.preparation == frozen.preparation
        assert reading.get_one(
            DueWorkReceipt, int(bound.receipt_id)
        ).handler_result_json == newest
    assert _released_text(factory, package_c, "accepted_change_summary", store) == summary
    assert _released_text(factory, package_c, "weekly_coordination_report", store) == report
    for kind, original in first_bytes.items():
        assert _released_text(factory, package_a_again, kind, store) == original


def test_the_second_issue_measures_from_the_first_authorized_package(
    factory, adopted, client, store
):
    """The floor of a project's second issue is the first package's watermark.

    Two weekly closes on one project, both driven the way a deployment drives
    them. Between them a delta is resolved, so the newest internal reading sits
    strictly above the first package's watermark: a floor taken from "the
    newest completed report-preparation receipt", from the newest candidate, or
    from zero is a different number from the right one, and this test can tell
    which it got.

    Both packages are authorized at the same declared instant, because the
    predecessor is the chain head and never the later timestamp.
    """

    # A project with no deltas reads zero on both watermarks whatever the rule
    # is, so the first issue is given a floor worth measuring from.
    _move_the_watermarks(factory, adopted, ("UC-1", "UC-2"), at=FIRST_DECISIONS_AT)
    _enable(factory, adopted)
    _take_weekly_reading(factory)

    first = _prepare(factory, client, adopted, at=WORKER_AT)
    assert first.handler_result["outcome"] == "prepared", first.handler_result
    first_reading = _reading_for(factory, int(first.handler_result["request_id"]))
    assert _floor(first_reading) == (0, 0), (
        "a project's first issue measures from zero and from an explicit none"
    )
    assert first_reading.previous_package_id is None
    first_watermark = _ceiling(first_reading)
    assert first_watermark[0] > 0 and first_watermark[1] > 0, (
        "the fixture is undiscriminating: a first package whose watermarks are "
        "zero cannot distinguish the authorized-package floor from zero"
    )

    _authorize(client, adopted)
    (package_one,) = _packages(factory, adopted)

    # A week passes the way a week passes: one more decision, one more retained
    # reading. This is what arms everything below -- the newest internal
    # reading is now strictly above the floor the rule must produce.
    _move_the_watermarks(factory, adopted, ("UC-3",), at=SECOND_DECISIONS_AT)
    newest_internal = _weekly_reading(factory, LATER_READING_AT)
    assert int(newest_internal["through_delta_id"]) > first_watermark[0]
    assert int(newest_internal["through_disposition_id"]) > first_watermark[1]

    second = _prepare(factory, client, adopted, at=SECOND_WORKER_AT)
    assert second.handler_result["outcome"] == "prepared", second.handler_result
    second_reading = _reading_for(
        factory, int(second.handler_result["request_id"])
    )

    # The whole of ADR-0086's rule, on the one package that can disprove it.
    assert second_reading.previous_package_id == int(package_one.id)
    assert _floor(second_reading) == first_watermark, (
        "the second issue's floor is the watermark bound to the first "
        "authorized package, not zero and not the newest internal reading"
    )
    assert _floor(second_reading) != (0, 0)
    assert _floor(second_reading) != (
        int(newest_internal["through_delta_id"]),
        int(newest_internal["through_disposition_id"]),
    )
    assert int(second_reading.receipt_id) != int(first_reading.receipt_id)
    assert _ceiling(second_reading) == (
        int(newest_internal["through_delta_id"]),
        int(newest_internal["through_disposition_id"]),
    )

    # The candidate binds the same predecessor, so the chain the next issue
    # follows is written on the candidate as well as on the reading.
    with factory() as reading:
        candidate = reading.get_one(
            ReleaseCandidate, int(second.handler_result["candidate_id"])
        )
        assert int(candidate.previous_package_id) == int(package_one.id)

    _authorize(client, adopted)
    packages = _packages(factory, adopted)
    assert len(packages) == 2
    package_two = packages[1]
    assert int(package_two.previous_package_id) == int(package_one.id)
    assert package_two.authorized_at == package_one.authorized_at, (
        "both issues were authorized at the same declared instant on purpose: "
        "if anything here ordered packages by time this fixture could not say "
        "which is the predecessor, and the chain says it"
    )
    with factory() as reading:
        assert previous_authorized_reading(
            reading, project_id=adopted.project_id
        )[0] == int(package_two.id)


def test_neither_a_failed_nor_an_unapproved_preparation_moves_the_floor(
    factory, adopted, client, store, tmp_path
):
    """A prepared-but-unapproved set, and a failed one, leave the marker where it was.

    Both negatives are armed the same way and the arming is the point: between
    the first authorized package and every later reading a delta is resolved,
    so the project's newest internal reading, its newest candidate and its most
    recent failed preparation all carry watermarks strictly above the floor.
    A fixture where they did not could not fail, whatever the rule said.
    """

    _move_the_watermarks(factory, adopted, ("UC-1", "UC-2"), at=FIRST_DECISIONS_AT)
    _enable(factory, adopted)
    _take_weekly_reading(factory)
    first = _prepare(factory, client, adopted, at=WORKER_AT)
    assert first.handler_result["outcome"] == "prepared", first.handler_result
    first_reading = _reading_for(factory, int(first.handler_result["request_id"]))
    _authorize(client, adopted)
    (package_one,) = _packages(factory, adopted)
    floor = _ceiling(first_reading)
    assert floor > (0, 0)

    # The floor before anything else happens, read from the chain itself.
    with factory() as reading:
        package_id, bound = previous_authorized_reading(
            reading, project_id=adopted.project_id
        )
        assert package_id == int(package_one.id)
        assert bound is not None and bound.id == first_reading.id

    # A second week, and a reading that stands above the floor.
    _move_the_watermarks(factory, adopted, ("UC-3",), at=SECOND_DECISIONS_AT)
    newest_internal = _weekly_reading(factory, LATER_READING_AT)
    assert int(newest_internal["through_delta_id"]) > floor[0]
    assert int(newest_internal["through_disposition_id"]) > floor[1]

    # 1. A preparation that fails. The registered template's bytes go missing
    #    exactly as #536's third proof takes them, so the failure is reached
    #    the way a deployment reaches it.
    with factory() as reading:
        registered = retained_output_template(
            reading, project_id=adopted.project_id, store=store
        )
    (tmp_path / "files" / registered.storage_key).unlink()
    failed = _prepare(factory, client, adopted, at=SECOND_WORKER_AT)
    assert failed.handler_result["outcome"] == "failed", failed.handler_result
    assert failed.handler_result["candidate_id"] is None
    with factory() as reading:
        standing = preparation_standing(reading, project_id=adopted.project_id)
        assert standing.state == FAILED
        package_id, bound = previous_authorized_reading(
            reading, project_id=adopted.project_id
        )
        assert package_id == int(package_one.id), (
            "a failed preparation is not an authorized package"
        )
        assert _ceiling(bound) == floor

    # 2. A preparation that succeeds and is never approved. The store is put
    #    right and the retry the section offers prepares a real candidate.
    store.put(
        registered.storage_key,
        adopted.template_bytes,
        sha256=registered.content_sha256,
    )
    unapproved = _prepare(factory, client, adopted, at=THIRD_WORKER_AT)
    assert unapproved.handler_result["outcome"] == "prepared", (
        unapproved.handler_result
    )
    unapproved_reading = _reading_for(
        factory, int(unapproved.handler_result["request_id"])
    )
    # Armed: this candidate's own reading reaches above the floor, so a rule
    # that advanced the baseline on preparation would be visible here.
    assert _ceiling(unapproved_reading) > floor
    assert _floor(unapproved_reading) == floor, (
        "the failed preparation between the package and this one must not "
        "have moved the floor"
    )
    assert len(_packages(factory, adopted)) == 1

    with factory() as reading:
        package_id, bound = previous_authorized_reading(
            reading, project_id=adopted.project_id
        )
        assert package_id == int(package_one.id), (
            "a prepared but unapproved candidate is not an authorized package"
        )
        assert _ceiling(bound) == floor

    # 3. And the floor a *third* request is given is still the first package's.
    #    The unapproved candidate is made stale the ordinary way -- the accepted
    #    record moved -- so the section offers a fresh preparation and the floor
    #    is written again rather than only inspected.
    _move_the_watermarks(factory, adopted, ("UC-4",), at=THIRD_DECISIONS_AT)
    third_internal = _weekly_reading(factory, THIRD_READING_AT)
    assert int(third_internal["through_delta_id"]) > _ceiling(
        unapproved_reading
    )[0]
    third = _prepare(factory, client, adopted, at=THIRD_WORKER_AT + timedelta(hours=1))
    assert third.handler_result["outcome"] == "prepared", third.handler_result
    third_reading = _reading_for(factory, int(third.handler_result["request_id"]))
    assert _floor(third_reading) == floor, (
        "two candidates and one failure later, the only thing that moves the "
        "external comparison baseline is an authorized package"
    )
    assert third_reading.previous_package_id == int(package_one.id)


def test_an_authorized_package_with_no_bound_reading_is_refused_by_name(
    factory, adopted, client, store
):
    """An unprovable floor refuses by name; it is never quietly treated as zero.

    The state is the one that actually produces it, and it is not exotic: a
    candidate prepared by a path that binds no reading. Every candidate made
    before #690 is one -- the worker took its inputs from a caller and recorded
    no ``ReleasePreparationReading`` -- and one of them can be sitting under a
    project's authorized package today. Here the production resolver supplies
    the very same inputs and the binding is *not* committed, so the attempt,
    the candidate and the package are all real and the reading is absent.

    Falling back to zero there would restate a window the customer has already
    seen. The supervisor names the package instead, and records a finished
    failed attempt so the Issue section stops saying "Preparing this issue".
    """

    _move_the_watermarks(factory, adopted, ("UC-1", "UC-2"), at=FIRST_DECISIONS_AT)
    # Nothing may claim the request while this file prepares it by hand.
    _enable(factory, adopted, supervisor=False)
    _take_weekly_reading(factory)
    _confirm_coverage(client, adopted)

    with factory() as resolving:
        request = resolving.scalars(
            select(ReleasePreparationRequest).where(
                ReleasePreparationRequest.project_id == adopted.project_id
            )
        ).one()
        request_id = int(request.id)
        unbound = bind_report_preparation_reading(
            resolving, request=request, bound_at=WORKER_AT
        )
        inputs = resolve_preparation_inputs(
            resolving, request, store=store, reading=unbound
        )
        # The binding is thrown away. What is left is exactly a pre-#690
        # candidate: real inputs, a real attempt, and no retained reading.
        resolving.rollback()

    attempt = run_preparation_request(
        factory,
        request_id=request_id,
        inputs=inputs,
        started_at=WORKER_AT,
        finished_at=WORKER_AT,
        store=store,
        surface="tests:pre-690-candidate",
    )
    assert attempt.outcome == "prepared", attempt.refusal_code
    with factory() as reading:
        assert (
            int(
                reading.scalar(
                    select(func.count())
                    .select_from(ReleasePreparationReading)
                    .where(
                        ReleasePreparationReading.project_id
                        == adopted.project_id
                    )
                )
            )
            == 0
        ), "the fixture is not the state it means to be in"

    _authorize(client, adopted)
    (package_one,) = _packages(factory, adopted)
    with factory() as reading:
        package_id, bound = previous_authorized_reading(
            reading, project_id=adopted.project_id
        )
        assert package_id == int(package_one.id)
        assert bound is None

    # A second week, and a second issue that cannot be measured.
    _move_the_watermarks(factory, adopted, ("UC-3",), at=SECOND_DECISIONS_AT)
    _weekly_reading(factory, LATER_READING_AT)
    _enable(factory, adopted)
    _confirm_coverage(client, adopted)

    with factory() as binding:
        second = binding.scalars(
            select(ReleasePreparationRequest)
            .where(ReleasePreparationRequest.project_id == adopted.project_id)
            .order_by(ReleasePreparationRequest.id.desc())
            .limit(1)
        ).one()
        assert int(second.id) != request_id
        with pytest.raises(PreparationSupervisionRefused) as refusal:
            bind_report_preparation_reading(
                binding, request=second, bound_at=SECOND_WORKER_AT
            )
        binding.rollback()
    assert f"authorized package {int(package_one.id)}" in str(refusal.value)
    assert "no bound report-preparation reading" in str(refusal.value)
    assert "provable floor" in str(refusal.value)

    # And through the deployed path the refusal ends the wait, rather than
    # preparing an issue against a floor nobody could prove.
    result = _run_supervisor(factory, at=SECOND_WORKER_AT)
    assert result.handler_result["outcome"] == "failed", result.handler_result
    assert result.handler_result["candidate_id"] is None
    with factory() as reading:
        standing = preparation_standing(reading, project_id=adopted.project_id)
        assert standing.state == FAILED
        assert "no bound report-preparation reading" in (standing.reason or "")
    assert len(_packages(factory, adopted)) == 1
