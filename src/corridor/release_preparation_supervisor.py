"""The deployed process that runs a confirmed preparation request (#690).

#675 recorded a coordinator's **Confirm coverage and prepare issue** as two
append-only rows and #529 built the three phases that prepare a candidate from
them. Nothing between the two ever ran. ``run_preparation_request`` had no
caller outside a test — no Due Work kind, no ``InputResolver`` — so a
coordinator who confirmed the derived reading left a request that no deployed
process would execute, and the Issue section read "Preparing this issue" for
as long as the project existed. This module is that process.

**A resolver resolves retained authorities; it does not compose issue inputs.**
That is the whole discipline here and every rule below is a consequence of it.
Nothing in this module selects "the latest" of anything, reads a customer
workbook off a filesystem, refetches from a customer system, or invents a
value #529 would otherwise take from its caller. Where an authority cannot be
proved, preparation fails with a bounded reason and records a finished
attempt; it never falls back to a similar one.

**The template bytes come from the registration, by identity and digest.**
The chain is exact:

    ReleasePreparationRequest
    → the effective output-template BaselineFormat registration
    → the retained object bound to that registration
    → ObjectStore.get(..., sha256=the registered digest)

``project_baseline_format_objects`` is that binding (#690). Where a
registration predates it, the object is proved by **exact digest** instead —
one Document of this project holding that exact SHA-256, which is how the
as-adopted template got into the store in the first place. Identical bytes
under the same digest are the same content, so a digest match is proof; a
filename, a timestamp or a similar workbook is not.

**One request binds exactly one report-preparation reading.** Not the latest
completed receipt for the project: the weekly pass keeps completing receipts,
so resolving "latest" at execution time would prepare a different window from
the one that was confirmed, and would let an unapproved or failed preparation
advance the next reading's floor. ``release_preparation_readings`` records the
receipt identity and its result digest, freezes the window's ceilings, and
takes its floors from the reading bound to the **previous authorized package**
— ADR-0086 is explicit that only an authorized package advances the external
comparison baseline. Every retry of the same request reuses that row.
The resolver counts this external window before rendering; passing through
the weekly receipt's counts would silently shorten a skipped issue (#709).
The retained weekly receipt and its digest are never rewritten.

**One occurrence, one request.** Due Work coalesces cadence occurrences onto a
slot, and a slot cannot name a request. Publication creates one occurrence per
durable request, so the lease, the retry budget and the receipt belong to one
coordinator's issue, and a reclaimed occurrence resumes that request and no
other.

**No clock.** Every instant is declared by the runtime's clock seam and passed
in. Nothing here calls ``datetime.now``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.analytics import default_binding
from corridor.baseline_adoption import effective_baseline_formats
from corridor.issue_profile import effective_issue_inventory
from corridor.issue_rendering import (
    RELEASED_FIRST_ISSUE_BEHAVIOR,
    RELEASED_TEMPLATE_SECTIONS,
    TemplateBinding,
)
from corridor.models import (
    BaselineFormat,
    BaselineFormatObject,
    Document,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    ReleasePreparationAttempt,
    ReleasePreparationPublication,
    ReleasePreparationReading,
    ReleasePreparationRequest,
)
from corridor.object_storage import (
    ObjectStore,
    StorageError,
    content_store,
)
from corridor.release_candidate import latest_authorized_package
from corridor.release_preparation import (
    PREPARED_OUTCOME,
    pending_request_ids,
)
from corridor.release_preparation_worker import (
    PreparationInputs,
    record_failed_attempt,
    run_preparation_request,
)
from corridor.report_preparation import (
    AUTHORIZED_PACKAGE_COMPARISON,
    count_delta_window,
)


# The one server-owned handler key this module's work runs under. It matches
# ``due_work.HANDLER_RELEASE_PREPARATION``; the constant lives here because
# this is the lower module and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "release_preparation"

# The reading this supervisor binds is #488's, and only #488's.
REPORT_PREPARATION_HANDLER_KEY = "report_preparation"
REPORT_PREPARATION_SCHEMA_VERSION = "report-preparation-result-v1"

RESULT_SCHEMA_VERSION = "release-preparation-result-v1"


class PreparationSupervisionRefused(ValueError):
    """A retained authority this request needs cannot be proved.

    Carried rather than raised as a bare error because it is *terminal*: the
    template bytes are missing, the profile has moved, the revision the
    request named is not the one the reading counts against. Retrying would
    ask the same unanswerable question, so the supervisor records a finished
    failed attempt immediately rather than burning the occurrence's retries
    and leaving the Issue section at "Preparing this issue".
    """


# --- publication -------------------------------------------------------------


def occurrence_key(request_id: int) -> str:
    """The stable occurrence identity for one request, and only that request."""

    return hashlib.sha256(
        json.dumps(
            {"handler": HANDLER_KEY, "request_id": int(request_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def publish_pending_preparation_requests(
    session: Session, schedule: DueWorkSchedule, *, now: datetime
) -> tuple[str, ...]:
    """Give every unworked request of this project an occurrence of its own.

    Called by ``enqueue_due_work`` in place of computing a cadence slot. Three
    things happen here and nothing else:

    - a request nothing has published gets one occurrence, keyed by the
      request so a second tick converges rather than queueing a duplicate;
    - a request whose occurrence the runtime has already given up on gets the
      terminal failed attempt that ends the wait, which is the abandoned-request
      hole #675 recorded and could not close from inside itself;
    - a request that already has an occurrence in flight is left alone.

    ``pending_request_ids`` is the reader, so a request with a finished attempt
    is never republished and a completed preparation is never run twice.
    """

    project_id = int(schedule.project_id)
    published: list[str] = []
    pending = pending_request_ids(session, project_id=project_id)
    if not pending:
        return ()
    existing = {
        int(row.request_id): row
        for row in session.scalars(
            select(ReleasePreparationPublication).where(
                ReleasePreparationPublication.request_id.in_(pending)
            )
        ).all()
    }
    for request_id in pending:
        publication = existing.get(int(request_id))
        if publication is not None:
            occurrence = session.get(
                DueWorkOccurrence, int(publication.occurrence_id)
            )
            if occurrence is not None and occurrence.state == "failed":
                _record_abandoned_request(
                    session,
                    request_id=int(request_id),
                    project_id=project_id,
                    occurrence=occurrence,
                    now=now,
                )
            continue
        request = session.get(ReleasePreparationRequest, int(request_id))
        if request is None or int(request.project_id) != project_id:
            continue
        key = occurrence_key(int(request_id))
        session.execute(
            insert(DueWorkOccurrence)
            .values(
                public_id=f"due-occurrence:{key[:24]}",
                scheduled_job_id=int(schedule.id),
                occurrence_key=key,
                # The instant the coordinator asked, so a request that waited
                # is claimed before one asked for later. Ordering by the
                # declared instant is safe here because the occurrence key is
                # the request, not a slot: two requests can never collide.
                due_at=_aware_utc(request.requested_at),
                state="pending",
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["occurrence_key"])
        )
        occurrence = session.scalar(
            select(DueWorkOccurrence).where(
                DueWorkOccurrence.occurrence_key == key
            )
        )
        if occurrence is None:
            continue
        session.add(
            ReleasePreparationPublication(
                project_id=project_id,
                request_id=int(request_id),
                occurrence_id=int(occurrence.id),
                published_at=_aware_utc(now),
            )
        )
        session.flush()
        published.append(key)
    return tuple(published)


def _record_abandoned_request(
    session: Session,
    *,
    request_id: int,
    project_id: int,
    occurrence: DueWorkOccurrence,
    now: datetime,
) -> None:
    """End the wait for a request the runtime has given up on.

    #675 said out loud that a request whose worker died is indistinguishable
    from one still running and that nothing else appends the attempt that would
    end the wait. This is that appender: once Due Work has exhausted the
    occurrence's retries and marked it failed, the request has a terminal
    finished attempt, the Issue section stops saying "Preparing this issue",
    and asking again is a second request rather than a re-run of this one.
    """

    moment = _aware_utc(now)
    record_failed_attempt(
        session,
        request_id=request_id,
        project_id=project_id,
        reason=(
            "the supervisor gave up on this preparation after "
            f"{occurrence.attempt_count} attempt(s): "
            f"{occurrence.last_error_code or 'the claim was abandoned'}. "
            "Confirm the coverage again to ask for a fresh preparation."
        ),
        started_at=moment,
        finished_at=moment,
    )


# --- the retained authorities ------------------------------------------------


def previous_authorized_reading(
    session: Session, *, project_id: int
) -> tuple[int | None, ReleasePreparationReading | None]:
    """The package the next window is measured from, and its bound reading.

    The chain ADR-0086 requires, followed one link at a time::

        previous authorized package
        → its candidate
        → the attempt that prepared that candidate
        → that attempt's request
        → the reading bound to it

    A candidate that was merely prepared, or blocked, or refused, is nowhere on
    it, which is exactly why a failed preparation cannot advance the floor.
    """

    package = latest_authorized_package(session, project_id)
    if package is None:
        return (None, None)
    reading = session.scalars(
        select(ReleasePreparationReading)
        .join(
            ReleasePreparationAttempt,
            ReleasePreparationAttempt.request_id
            == ReleasePreparationReading.request_id,
        )
        .where(
            ReleasePreparationAttempt.project_id == project_id,
            ReleasePreparationAttempt.outcome == PREPARED_OUTCOME,
            ReleasePreparationAttempt.candidate_id == int(package.candidate_id),
        )
        .limit(1)
    ).first()
    return (int(package.id), reading)


def bind_report_preparation_reading(
    session: Session,
    *,
    request: ReleasePreparationRequest,
    bound_at: datetime,
) -> ReleasePreparationReading:
    """Bind this request to exactly one completed report-preparation receipt.

    Written once and reused by every retry of the same request. A worker crash
    would otherwise change the window between attempts: the next attempt could
    count a different population, and ``previous_reading`` could see the first
    internal receipt and produce a zero or shortened second window. A *new*
    request — one created after a completed failure, or after the inputs moved
    — may receive a new reading and new watermarks, because it is a different
    request.
    """

    project_id = int(request.project_id)
    existing = session.scalars(
        select(ReleasePreparationReading).where(
            ReleasePreparationReading.request_id == int(request.id)
        )
    ).first()
    if existing is not None:
        return existing

    previous_package_id, prior = previous_authorized_reading(
        session, project_id=project_id
    )
    if previous_package_id is not None and prior is None:
        raise PreparationSupervisionRefused(
            f"authorized package {previous_package_id} carries no bound "
            "report-preparation reading, so the window this issue would "
            "measure has no provable floor. Nothing here guesses one"
        )
    delta_floor = 0 if prior is None else int(prior.through_delta_id)
    disposition_floor = (
        0 if prior is None else int(prior.through_disposition_id)
    )

    receipt = _completed_report_preparation_receipt(
        session, project_id=project_id
    )
    if receipt is None:
        raise PreparationSupervisionRefused(
            "this project has no completed report-preparation reading to "
            "prepare an issue from; the weekly reading runs first"
        )
    result = dict(receipt.handler_result_json or {})
    if result.get("schema_version") != REPORT_PREPARATION_SCHEMA_VERSION:
        raise PreparationSupervisionRefused(
            "the retained reading is not the report-preparation result this "
            "renderer reads"
        )
    if int(result.get("accepted_revision_id") or 0) != int(
        request.accepted_revision_id
    ):
        raise PreparationSupervisionRefused(
            "the retained reading counts against accepted revision "
            f"{result.get('accepted_revision_id')} and this issue was "
            f"reviewed at revision {request.accepted_revision_id}; one issue "
            "is one accepted record, never two"
        )
    ceiling_delta = int(result.get("through_delta_id") or 0)
    ceiling_disposition = int(result.get("through_disposition_id") or 0)
    if ceiling_delta < delta_floor or ceiling_disposition < disposition_floor:
        raise PreparationSupervisionRefused(
            "the retained reading stops before the previous authorized "
            "package's watermark, so it describes no window at all"
        )

    row = ReleasePreparationReading(
        project_id=project_id,
        request_id=int(request.id),
        receipt_id=int(receipt.id),
        handler_key=REPORT_PREPARATION_HANDLER_KEY,
        result_schema_version=str(result["schema_version"]),
        result_sha256=result_digest(result),
        accepted_revision_id=int(request.accepted_revision_id),
        source_cutoff=_aware_utc(request.source_cutoff),
        previous_package_id=previous_package_id,
        prior_delta_floor=delta_floor,
        prior_disposition_floor=disposition_floor,
        through_delta_id=ceiling_delta,
        through_disposition_id=ceiling_disposition,
        bound_at=_aware_utc(bound_at),
    )
    session.add(row)
    session.flush()
    return row


def _completed_report_preparation_receipt(
    session: Session, *, project_id: int
) -> DueWorkReceipt | None:
    """The newest completed weekly reading of this project, at binding time.

    "Newest at binding time" is a resolution, not a selection policy the
    request carries: the moment this returns, the receipt is written onto the
    request and never consulted again. Ordered by receipt identity rather than
    ``finished_at`` for #488's own reason — the identifier is monotonic in
    insertion order whatever any clock said.
    """

    return session.scalars(
        select(DueWorkReceipt)
        .join(
            DueWorkOccurrence,
            DueWorkOccurrence.id == DueWorkReceipt.occurrence_id,
        )
        .join(
            DueWorkSchedule,
            DueWorkSchedule.id == DueWorkOccurrence.scheduled_job_id,
        )
        .where(
            DueWorkSchedule.project_id == project_id,
            DueWorkReceipt.handler_key == REPORT_PREPARATION_HANDLER_KEY,
            DueWorkReceipt.execution_outcome == "completed",
        )
        .order_by(DueWorkReceipt.id.desc())
        .limit(1)
    ).first()


def result_digest(result: dict[str, Any]) -> str:
    """The digest of one retained reading, over canonical bytes."""

    return hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RetainedTemplate:
    """The exact registered output template, and the bytes it stands on."""

    format_id: int
    identity: str
    version: str
    content_sha256: str
    storage_key: str
    content: bytes


def retained_output_template(
    session: Session, *, project_id: int, store: ObjectStore
) -> RetainedTemplate:
    """The customer's own template, by registration identity and digest.

    Never "the newest workbook", never a filename, never a path, and never
    another project's or another registration's bytes. The digest the
    registration recorded is the digest the store is asked for, and the store
    verifies what it hands back against it, so bytes that are not the
    registered content cannot reach a renderer even if something in the store
    were wrong.
    """

    registration = effective_baseline_formats(session, project_id).get(
        "output_template"
    )
    if registration is None:
        raise PreparationSupervisionRefused(
            "this project has no effective output-template registration, so "
            "the one mandatory artifact has no template to render through"
        )
    key = _template_storage_key(session, registration, store=store)
    try:
        content = store.get(key, sha256=registration.content_sha256)
    except StorageError as exc:
        raise PreparationSupervisionRefused(
            "the bytes registered output template "
            f"{registration.format_identity} {registration.format_version} "
            f"was registered over could not be retrieved: {exc}"
        ) from exc
    return RetainedTemplate(
        format_id=int(registration.id),
        identity=registration.format_identity,
        version=registration.format_version,
        content_sha256=registration.content_sha256,
        storage_key=key,
        content=content,
    )


def _template_storage_key(
    session: Session, registration: BaselineFormat, *, store: ObjectStore
) -> str:
    """Where this registration's exact bytes are, or a bounded refusal.

    Two proofs, in order, and no third. The binding row is the registration's
    own storage binding and is what a replacement template writes. Where there
    is none — the as-adopted template, registered before #690 — the object is
    proved by exact digest: one Document of this project holds that SHA-256,
    which is the case the maintainer's decision calls valid proof because
    identical bytes under one digest are the same content.
    """

    binding = session.get(BaselineFormatObject, int(registration.id))
    if binding is not None:
        return binding.storage_key
    proved = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.project_id == int(registration.project_id),
            Document.sha256 == registration.content_sha256,
        )
    )
    if not proved:
        raise PreparationSupervisionRefused(
            "registered output template "
            f"{registration.format_identity} {registration.format_version} "
            "names a digest no retained object of this project holds, so the "
            "template it was registered over cannot be proved. Register the "
            "replacement template again with its exact bytes"
        )
    key = store.resolve(registration.content_sha256)
    if key is None:
        raise PreparationSupervisionRefused(
            "registered output template "
            f"{registration.format_identity} {registration.format_version} "
            "is recorded against a digest the content store does not hold"
        )
    return key


def resolve_preparation_inputs(
    session: Session,
    request: ReleasePreparationRequest,
    *,
    store: ObjectStore,
    reading: ReleasePreparationReading,
) -> PreparationInputs:
    """Every input #529 takes from its caller, read from its retained owner.

    | input                     | owner                                       |
    | ------------------------- | ------------------------------------------- |
    | ``preparation``           | bound weekly standing and external window   |
    | ``template_bytes``        | the object bound to the registration        |
    | template/mapping identity | the request's exact Issue Profile           |
    | template sections         | the released renderer contract              |
    | ``first_issue_behavior``  | the released renderer contract              |
    | ``follow_up_plans``       | see below                                   |
    | analytics binding         | the deployed code and product identity      |

    ``follow_up_plans`` resolves empty, deliberately and not by oversight.
    #425 retains an open question, a responsible party and a return date;
    ``AcceptedFollowUpPlan`` additionally requires the **next action** sentence
    the report prints, and no retained record holds one. A supervisor that
    composed that sentence would be writing customer-facing prose out of a
    background job, which is the exact thing this module refuses to do. The
    chase list still reaches the candidate: ``bind_preparation`` reads
    ``read_follow_up_bundles`` itself, from the same #425 records.
    """

    receipt = session.get(DueWorkReceipt, int(reading.receipt_id))
    if receipt is None or receipt.handler_result_json is None:
        raise PreparationSupervisionRefused(
            "the report-preparation receipt this request was bound to no "
            "longer holds its reading"
        )
    preparation = dict(receipt.handler_result_json)
    if result_digest(preparation) != reading.result_sha256:
        raise PreparationSupervisionRefused(
            "the report-preparation reading this request was bound to no "
            "longer digests to what was bound"
        )

    # Recount only the immutable lifecycle window. The receipt owns the frozen
    # revision and current standing; this request owns the external comparison
    # floors and ceilings. A later weekly close must change neither on replay.
    preparation.update(
        count_delta_window(
            session,
            project_id=int(reading.project_id),
            delta_floor=int(reading.prior_delta_floor),
            delta_ceiling=int(reading.through_delta_id),
            disposition_floor=int(reading.prior_disposition_floor),
            disposition_ceiling=int(reading.through_disposition_id),
        ),
        comparison_baseline=AUTHORIZED_PACKAGE_COMPARISON,
        previous_authorized_package_id=(
            None if reading.previous_package_id is None else int(reading.previous_package_id)
        ),
        prior_delta_floor=int(reading.prior_delta_floor),
        prior_disposition_floor=int(reading.prior_disposition_floor),
    )
    # This weekly date label does not describe the external issue's window.
    # Its predecessor and watermarks carry the comparison without a clock.
    preparation.pop("window_start", None)

    inventory = effective_issue_inventory(
        session, int(request.project_id), _aware_utc(request.source_cutoff)
    )
    if inventory is None:
        raise PreparationSupervisionRefused(
            "this project is not configured to issue anything at that cutoff"
        )
    if int(inventory.profile_id) != int(request.issue_profile_id) or int(
        inventory.profile_version
    ) != int(request.issue_profile_version):
        raise PreparationSupervisionRefused(
            "what this project is configured to issue changed after this "
            "issue was requested, so the profile the coordinator confirmed is "
            "not the one in force. Read the week again"
        )

    template = retained_output_template(
        session, project_id=int(request.project_id), store=store
    )
    return PreparationInputs(
        preparation=preparation,
        templates=TemplateBinding(
            template_identity=inventory.output_template.identity,
            template_version=inventory.output_template.version,
            mapping_identity=inventory.field_mapping.identity,
            mapping_version=inventory.field_mapping.version,
            sections=RELEASED_TEMPLATE_SECTIONS,
        ),
        first_issue_behavior=RELEASED_FIRST_ISSUE_BEHAVIOR,
        template_bytes=template.content,
        follow_up_plans=(),
        binding=default_binding(),
        report_receipt_id=int(reading.receipt_id),
        report_result_sha256=reading.result_sha256,
    )


# --- what one claimed occurrence does ----------------------------------------


def execute_release_preparation(
    session_factory: Callable[[], Session],
    *,
    occurrence_id: int,
    schedule_id: int,
    clock: Any,
    runtime_owner: str = "runtime:release_preparation",
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Prepare the one issue this claimed occurrence names.

    The eight steps the maintainer's decision names, in order: claim (the
    runtime's, already held), load the confirmed declaration (#529 does, and
    refuses a request whose declaration moved), resolve and bind the exact
    report-preparation receipt, resolve the exact template object and renderer
    inputs, invoke ``run_preparation_request`` with the session factory, and
    record the finished attempt. Finalizing the occurrence is the runtime's.

    A *permanent* resolver failure records its finished failed attempt here and
    returns a completed receipt, because retrying would ask the same
    unanswerable question and would leave the section saying "Preparing this
    issue" until the retries ran out. A transient one — a lost database, a
    store that did not answer — propagates, and the claim and lease own the
    recovery.
    """

    with session_factory() as reading:
        publication = session_publication(reading, occurrence_id=occurrence_id)
        request_id = int(publication.request_id)
        project_id = int(publication.project_id)
        configuration_version = _configuration_version(reading, schedule_id)
        finished = reading.scalars(
            select(ReleasePreparationAttempt)
            .where(ReleasePreparationAttempt.request_id == request_id)
            .order_by(ReleasePreparationAttempt.id.desc())
            .limit(1)
        ).first()
        if finished is not None:
            # The attempt committed and the claim was then lost or reclaimed.
            # Re-running would prepare a second candidate from inputs that
            # already produced one, so this reports what is durable instead.
            return _receipt(
                project_id=project_id,
                configuration_version=configuration_version,
                observed_at=_aware_utc(clock.now()),
                request_id=request_id,
                attempt=finished,
                report_receipt_id=_bound_receipt_id(reading, request_id),
            )
        reading.rollback()

    started_at = _aware_utc(clock.now())
    resolved_store = store if store is not None else content_store()
    try:
        with session_factory() as binding_session:
            request = binding_session.get(
                ReleasePreparationRequest, request_id
            )
            if request is None:
                raise PreparationSupervisionRefused(
                    f"there is no preparation request {request_id}"
                )
            bound = bind_report_preparation_reading(
                binding_session, request=request, bound_at=started_at
            )
            inputs = resolve_preparation_inputs(
                binding_session, request, store=resolved_store, reading=bound
            )
            report_receipt_id = int(bound.receipt_id)
            # The binding is committed before a byte is rendered, so a worker
            # that dies mid-render comes back to the same window.
            binding_session.commit()
    except PreparationSupervisionRefused as refusal:
        finished_at = _aware_utc(clock.now())
        with session_factory() as recording:
            attempt = record_failed_attempt(
                recording,
                request_id=request_id,
                project_id=project_id,
                reason=str(refusal),
                started_at=started_at,
                finished_at=finished_at,
            )
            recording.commit()
            recording.refresh(attempt)
            return _receipt(
                project_id=project_id,
                configuration_version=configuration_version,
                observed_at=finished_at,
                request_id=request_id,
                attempt=attempt,
                report_receipt_id=None,
            )

    attempt = run_preparation_request(
        session_factory,
        request_id=request_id,
        inputs=inputs,
        started_at=started_at,
        finished_at=_aware_utc(clock.now()),
        store=resolved_store,
        surface=HANDLER_KEY,
    )
    return _receipt(
        project_id=project_id,
        configuration_version=configuration_version,
        observed_at=_aware_utc(clock.now()),
        request_id=request_id,
        attempt=attempt,
        report_receipt_id=report_receipt_id,
    )


def session_publication(
    session: Session, *, occurrence_id: int
) -> ReleasePreparationPublication:
    """The one request this occurrence was published for."""

    publication = session.scalars(
        select(ReleasePreparationPublication).where(
            ReleasePreparationPublication.occurrence_id == int(occurrence_id)
        )
    ).first()
    if publication is None:
        raise PreparationSupervisionRefused(
            f"Due Work occurrence {occurrence_id} names no preparation request"
        )
    return publication


def _bound_receipt_id(session: Session, request_id: int) -> int | None:
    bound = session.scalars(
        select(ReleasePreparationReading).where(
            ReleasePreparationReading.request_id == int(request_id)
        )
    ).first()
    return None if bound is None else int(bound.receipt_id)


def _configuration_version(session: Session, schedule_id: int) -> str:
    schedule = session.get(DueWorkSchedule, int(schedule_id))
    if schedule is None:
        raise PreparationSupervisionRefused("Due Work schedule disappeared")
    return schedule.configuration_version


def _receipt(
    *,
    project_id: int,
    configuration_version: str,
    observed_at: datetime,
    request_id: int,
    attempt: ReleasePreparationAttempt,
    report_receipt_id: int | None,
) -> dict[str, Any]:
    """The bounded receipt one preparation attempt leaves on the runtime.

    It names the durable rows and nothing else: an operator reading it can
    reach the attempt, the candidate and the reading, and no artifact content
    or customer text passes through the runtime's receipt.
    """

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "project_id": int(project_id),
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": (
            "healthy"
            if attempt.outcome == PREPARED_OUTCOME
            else "preparation_attention_required"
        ),
        "request_id": int(request_id),
        "attempt_id": int(attempt.id),
        "outcome": attempt.outcome,
        "candidate_id": (
            None if attempt.candidate_id is None else int(attempt.candidate_id)
        ),
        "refusal_code": attempt.refusal_code,
        "report_receipt_id": report_receipt_id,
    }


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
