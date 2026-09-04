"""A confirmed request reaches a candidate through a claimed occurrence (#690).

Every other test of this path hands the worker its inputs. This one does not
call ``run_preparation_request`` anywhere: it presses the coordinator's own
HTTP action, lets the production Due Work registry publish and claim the
occurrence, and lets the real ``InputResolver`` retrieve the authorities the
maintainer's decision names. What the test supplies is a project, a clock and a
declared instant; what it never supplies is a template, a reading, a template
binding or a first-issue behaviour.

Nothing here reads a clock. Every instant is declared, the routes take theirs
from ``get_review_clock``, and the runtime takes its from the ``Ticker`` below,
whose ``now`` returns a value this file set.
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
    ReleaseCandidate,
    ReleasePreparationAttempt,
    ReleasePreparationPublication,
    ReleasePreparationReading,
    ReleasePreparationRequest,
)
from corridor.principals import HumanPrincipal
from corridor.release_preparation import (
    FAILED,
    PREPARED,
    PREPARING,
    preparation_standing,
)
from corridor.release_preparation_supervisor import (
    PreparationSupervisionRefused,
    bind_report_preparation_reading,
    retained_output_template,
)

import test_issue_path_end_to_end as issue_path

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
_enable = issue_path.enable_runtime
_tick = issue_path.tick
_take_weekly_reading = issue_path.take_weekly_reading
_confirm_coverage = issue_path.confirm_coverage
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
