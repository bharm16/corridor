"""Run one requested preparation through #529's three phases (#675).

Held apart from ``release_preparation`` on purpose, and not for tidiness: the
records and the derived status are read by ``project_workflow``, which #529
already reaches through ``follow_up_bundles``. A single module would make that
a cycle. The split follows the one this repository already keeps between
``review_packet_reading`` and ``review_packets`` — the reading is what many
surfaces consume, and the act is what one caller performs.

**Nothing is decided here.** #529 owns binding, rendering, revalidation and
attachment, and owns the three outcomes; this module reads one request, hands
#529 exactly what it needs, and appends one attempt row saying which of the
three happened. A second opinion about readiness, staleness or coverage would
be the drift #641 exists to prevent.

**The preparation is attributed to the person who asked for it.** A background
run carries no person's authority, so the requester's principal travels on the
request and is what #529 records as the preparer.

**What the request deliberately does not carry.** The customer's own template
bytes, the approved template and mapping binding, the first-issue behaviour and
the report-preparation reading are inputs #529 already takes from its caller,
and #675 does not invent a second authority over where they come from. A
resolver supplies them. Copying them onto the request would copy state another
row already owns, which is the defect #598's ratchet refuses.

**No clock.** The attempt's start and finish are declared by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.orm import Session

from corridor.analytics import AnalyticsBinding
from corridor.issue_coverage import CoverageRefused, load_declaration
from corridor.issue_rendering import TemplateBinding
from corridor.models import ReleasePreparationAttempt, ReleasePreparationRequest
from corridor.object_storage import ObjectStore
from corridor.principals import HumanPrincipal
from corridor.release_candidate import (
    PreparationOutcome,
    prepare_release_candidate,
)
from corridor.release_preparation import (
    FAILED_OUTCOME,
    PREPARED_OUTCOME,
    REFUSED_OUTCOME,
    PreparationRequestRefused,
)


@dataclass(frozen=True, slots=True)
class PreparationInputs:
    """What #529 needs that the request itself does not record.

    The customer's own template bytes, the approved template and mapping
    binding, the first-issue behaviour and the report-preparation reading are
    all inputs #529 already takes from its caller, and #675 does not invent a
    second authority over where they come from. A worker resolves them and
    hands them here; a test hands them here directly. Storing them on the
    request would copy state another row already owns, which is the defect
    #598's ratchet exists to refuse.
    """

    preparation: Mapping[str, Any]
    templates: TemplateBinding
    first_issue_behavior: str
    template_bytes: bytes
    follow_up_plans: Sequence[Any] = ()
    binding: AnalyticsBinding | None = None
    # Which retained weekly reading supplies the frozen revision and standing
    # (#690). External lifecycle counts use the request's comparison window
    # (#709), whose explicit predecessor and floors travel in ``preparation``
    # and are checked under the candidate builder's project lock. These two
    # still name the unmodified weekly receipt in the input declaration.
    report_receipt_id: int | None = None
    report_result_sha256: str | None = None


InputResolver = Callable[[ReleasePreparationRequest], PreparationInputs]



def run_preparation_request(
    sessions: Callable[[], Session],
    *,
    request_id: int,
    inputs: PreparationInputs | InputResolver,
    started_at: datetime,
    finished_at: datetime,
    store: ObjectStore | None = None,
    surface: str = "release_preparation_worker",
) -> ReleasePreparationAttempt:
    """Run #529's three phases for one request and record what they produced.

    ``sessions`` is a factory and not a session for #529's own reason: the bind
    phase commits and releases the project lock before a byte is rendered, the
    render phase's session is closed before the attach phase opens, and no
    PostgreSQL transaction spans the rendering. This function opens two of its
    own around that — one to read the request, one to append the attempt — and
    holds neither across the work.

    The preparation is attributed to the person who requested it, not to the
    worker: a background run carries no person's authority, and the record has
    to say whose issue this is.
    """

    if started_at.tzinfo is None or finished_at.tzinfo is None:
        raise PreparationRequestRefused(
            "an attempt records declared, time-zone-aware instants; nothing "
            "here reads a clock"
        )
    with sessions() as reading:
        request = reading.get(ReleasePreparationRequest, int(request_id))
        if request is None:
            raise PreparationRequestRefused(
                f"there is no preparation request {request_id}"
            )
        project_id = int(request.project_id)
        cutoff = request.source_cutoff
        declaration_id = int(request.coverage_declaration_id)
        requester = HumanPrincipal(request.requested_by_principal)
        try:
            load_declaration(
                reading, project_id=project_id, declaration_id=declaration_id
            )
        except CoverageRefused as exc:
            raise PreparationRequestRefused(str(exc)) from exc
        resolved = inputs(request) if callable(inputs) else inputs
        reading.rollback()

    outcome = prepare_release_candidate(
        sessions,
        project_id=project_id,
        prepared_by=requester,
        preparation=resolved.preparation,
        source_cutoff=cutoff,
        prepared_at=started_at,
        coverage_declaration_id=declaration_id,
        templates=resolved.templates,
        first_issue_behavior=resolved.first_issue_behavior,
        template_bytes=resolved.template_bytes,
        binding=resolved.binding,
        follow_up_plans=resolved.follow_up_plans,
        report_receipt_id=resolved.report_receipt_id,
        report_result_sha256=resolved.report_result_sha256,
        surface=surface,
        store=store,
    )
    with sessions() as recording:
        attempt = record_attempt(
            recording,
            request_id=int(request_id),
            project_id=project_id,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
        )
        recording.commit()
        recording.refresh(attempt)
        return attempt


def record_attempt(
    session: Session,
    *,
    request_id: int,
    project_id: int,
    outcome: PreparationOutcome,
    started_at: datetime,
    finished_at: datetime,
) -> ReleasePreparationAttempt:
    """Append what one finished attempt produced, in #529's own vocabulary.

    A refusal carries #529's bounded reason code, which is why this relation
    reads that vocabulary from the model rather than spelling a second one: a
    reason a person has to classify by reading is a reason nobody counts.
    """

    if outcome.prepared:
        row = ReleasePreparationAttempt(
            request_id=int(request_id),
            project_id=int(project_id),
            outcome=PREPARED_OUTCOME,
            candidate_id=int(outcome.candidate_id or 0) or None,
            started_at=started_at,
            finished_at=finished_at,
        )
    else:
        row = ReleasePreparationAttempt(
            request_id=int(request_id),
            project_id=int(project_id),
            outcome=REFUSED_OUTCOME if outcome.refusal_code else FAILED_OUTCOME,
            refusal_code=outcome.refusal_code,
            reason=(outcome.refusal_reason or "preparation produced no candidate")[
                :2000
            ],
            started_at=started_at,
            finished_at=finished_at,
        )
    session.add(row)
    session.flush()
    return row


def record_failed_attempt(
    session: Session,
    *,
    request_id: int,
    project_id: int,
    reason: str,
    started_at: datetime,
    finished_at: datetime,
) -> ReleasePreparationAttempt:
    """The receipt an attempt that never reached #529 leaves behind.

    #529 turns every refusal it can name into a bounded code, and this is for
    the ones it never sees: a resolver that could not find the customer's
    template bytes, a worker that lost its database. Without it a crashed
    attempt would leave a request looking like one still running for ever.
    """

    row = ReleasePreparationAttempt(
        request_id=int(request_id),
        project_id=int(project_id),
        outcome=FAILED_OUTCOME,
        reason=(reason or "preparation did not complete")[:2000],
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(row)
    session.flush()
    return row
