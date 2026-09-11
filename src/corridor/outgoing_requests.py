"""Retain what was sent and what came back, so silence can become a fact (#652).

The chase list (#425) has a fully built ``unanswered_request`` band, but it
fires only for a *retained outgoing request* whose declared response boundary
has passed. ADR-0090 retired the legacy ``STALE`` alert for the opposite of
this: it fired on quiet that nobody was owed, treating an empty inbox as
evidence. A no-response finding therefore needs the request itself on record —
what was asked, of whom, covering which Utility Conflicts, when it went, and
the boundary it declared — and until this module existed there was no table to
write and ``follow_up_bundles.read_retained_outgoing_requests`` returned
nothing.

**Corridor sends nothing.** A person sends from their own mail client and
records the message here afterwards. That is not a gap waiting on a sending
system; it is the shape #425 and #652 both chose, and it is why the sender and
the person recording the send are two separate fields rather than one.

**One request, the plans it advances** (#837, the accepted #652 contract of
2026-09-04). A follow-up bundle groups several questions into one
communication, so one email advances one *or more* Follow-up Plans and one
plan may take several requests before anybody answers. ``retain_outgoing_request``
therefore takes a set of plan ids and writes ``outgoing_request_plans`` in the
same statement as the request; recording that email against a single plan would
misdescribe what was asked, and recording it once per plan would invent emails
nobody sent.

**The exact sent content is retained, not only its digest.** The bytes go
through the one storage interface (``object_storage``, ADR-0079) before the row
that references them, so a crash between the two leaves an unreferenced object
rather than a row without its content; the row keeps the digest and the key,
and PostgreSQL proves the two agree because the key contains the digest. A
coordinator asking "what did we actually send them?" is answered with the text,
which is the whole reason a digest alone was not enough.

**A response stops the clock and settles nothing.** The accepted contract is
explicit: recording a response does not resolve the Follow-up Plan and does not
change an accepted project value. The question can stay open even though the
recipient replied, and the only act that retires the ask is settling the record
question itself, on the surface that owns it. So nothing in this module writes a
disposition, a plan lifecycle act, or a Project Record revision — and each
observation carries the evidence behind it, one of the incoming Document, the
Source Delivery that brought it, the exact Source Segment, or an attributable
manual observation.

**Correction is append-only.** A mistaken request or response is corrected by
appending a corrected one that names the original and says why; the original is
never rewritten, and "superseded" is derived from the successor's existence
rather than stored as a status. That is the same discipline the rest of the
spine keeps, and it is what lets the page show a coordinator both what was
recorded and what it was corrected to.

A retained outgoing request is Corridor-originated correspondence — not
source-derived evidence, not an accepted-record decision — so it reuses the
record-decision role and the immutable-receipt idiom the baseline-adoption
receipt established. ``append_outgoing_request`` and
``append_outgoing_request_response`` are ``SECURITY DEFINER`` commands owned by
that role; a guard trigger refuses every write that does not arrive through
them, so an ORM insert from any module is refused by PostgreSQL rather than by
a convention this module asks callers to keep. Both commands converge a replay
on the row they already wrote.

What is out of scope (#652, #837): drafting or sending the request, delivery
confirmation, automatic escalation, and full receipt tracking. Plan update,
cancellation, return-date change and early resume are the plan lifecycle and
belong to #835, so nothing here offers a competing control over a plan.

Terminology: ``acknowledgement``, ``partial`` and ``substantive`` are the
maintainer's own words for the three observations (#652 decision of 2026-09-04,
#837); nothing here coins a customer term.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
import json

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from corridor.digests import sha256_bytes
from corridor.models import (
    OutgoingRequest,
    OutgoingRequestPlan,
    OutgoingRequestResponse,
)
from corridor.object_storage import content_key, content_store, store_bytes


#: What one observation says about how much of the ask the reply answered. The
#: maintainer's three (#652, 2026-09-04): "whether the response was complete,
#: partial, or merely acknowledged".
RESPONSE_COMPLETENESS = ("acknowledgement", "partial", "substantive")

#: What one observation is linked to. The first three are records Corridor
#: already holds; the fourth is a person saying, under their own name, what
#: they were told.
RESPONSE_EVIDENCE_KINDS = (
    "document",
    "source_delivery",
    "source_segment",
    "manual_observation",
)

#: The suffix retained sent content is stored under. Correspondence is recorded
#: as the text a person pasted in, so the object is that text's UTF-8 bytes.
SENT_CONTENT_SUFFIX = ".txt"


class OutgoingRequestRefused(ValueError):
    """A caller cannot retain an outgoing request or record its response."""


# --- retaining what was sent ------------------------------------------------


def retain_outgoing_request(
    session: Session,
    *,
    project_id: int,
    follow_up_plan_ids: Sequence[int],
    external_organization: str,
    question: str,
    covered_subject_keys: Sequence[str],
    sent_content: bytes,
    sent_on: date,
    sent_by_principal: str,
    recorded_by_principal: str,
    expected_response_by: date,
    idempotency_key: str,
    responsible_role: str | None = None,
    boundary_rule_version: str | None = None,
    boundary_interval_days: int | None = None,
    supersedes_request_id: int | None = None,
    correction_reason: str | None = None,
) -> OutgoingRequest:
    """Retain one outgoing request, or return the one a replay already wrote.

    ``follow_up_plan_ids`` is the set of Follow-up Plans this one communication
    advanced — at least one, and as many as the coordinator addressed in it.
    ``sent_content`` is the exact message, persisted through the storage
    interface *before* the row that names it, so a crash between the two leaves
    an object nobody references rather than a request whose content is gone.

    ``expected_response_by`` is always explicit. A rule may have prefilled it
    from a plan's return date, in which case ``boundary_rule_version`` and
    ``boundary_interval_days`` record how, but the person recording the request
    confirmed the date: silence before a boundary nobody set is not a finding,
    and deriving one silently is how somebody gets accused of not answering.

    ``supersedes_request_id`` makes this a correction of a request already
    retained. The original stays exactly as it was; being superseded is derived
    from this row's existence.
    """

    if not sent_by_principal.strip():
        raise OutgoingRequestRefused("a retained outgoing request names its sender")
    if not recorded_by_principal.strip():
        raise OutgoingRequestRefused(
            "a retained outgoing request names who recorded it; Corridor sends "
            "nothing, so somebody is stating that this went out"
        )
    if not idempotency_key.strip():
        raise OutgoingRequestRefused(
            "a retained outgoing request needs an idempotency key"
        )
    plans = tuple(dict.fromkeys(int(value) for value in follow_up_plan_ids))
    if not plans:
        raise OutgoingRequestRefused(
            "a retained outgoing request advances at least one Follow-up Plan"
        )
    subjects = tuple(covered_subject_keys)
    if not subjects:
        raise OutgoingRequestRefused(
            "a retained outgoing request covers at least one Utility Conflict"
        )
    if not sent_content:
        raise OutgoingRequestRefused(
            "a retained outgoing request keeps the exact content that was "
            "sent; a digest cannot show a coordinator what was asked"
        )
    if (supersedes_request_id is None) != (
        correction_reason is None or not correction_reason.strip()
    ):
        raise OutgoingRequestRefused(
            "a correction names the request it corrects and why; anything else "
            "is a new request"
        )

    content_sha256 = sha256_bytes(sent_content)
    # Through the storage interface, before the row that references it. The
    # put is conditional and idempotent, so a replay of the same content is a
    # no-op rather than a second object (`object_storage`).
    store_bytes(sent_content, sha256=content_sha256, suffix=SENT_CONTENT_SUFFIX)
    key = content_key(content_sha256, SENT_CONTENT_SUFFIX)

    request_id = _appended(
        session,
        select(
            func.append_outgoing_request(
                project_id,
                cast(bindparam(None, list(plans)), ARRAY(BigInteger)),
                external_organization,
                responsible_role,
                question,
                cast(bindparam(None, json.dumps(list(subjects))), JSONB),
                content_sha256,
                key,
                sent_on,
                sent_by_principal,
                recorded_by_principal,
                expected_response_by,
                boundary_rule_version,
                boundary_interval_days,
                supersedes_request_id,
                correction_reason,
                idempotency_key,
            )
        ),
    )
    return session.get_one(OutgoingRequest, int(request_id))


def read_sent_content(request: OutgoingRequest | RecordedRequest) -> bytes:
    """The exact bytes that were sent, read back through the storage interface.

    The point of retaining the content rather than only its digest: a
    coordinator looking at a no-response finding can be shown what was actually
    asked. The read verifies the digest, so content that no longer hashes to
    what the row records raises rather than being displayed as the message.
    """

    return content_store().get(
        request.sent_content_key, sha256=request.content_sha256
    )


# --- recording what came back ------------------------------------------------


def record_outgoing_request_response(
    session: Session,
    *,
    project_id: int,
    request_id: int,
    received_on: date,
    recorded_by_principal: str,
    completeness: str,
    source_reference: str,
    idempotency_key: str,
    document_id: int | None = None,
    source_delivery_id: int | None = None,
    source_segment_id: int | None = None,
    observation: str | None = None,
    observed_by_principal: str | None = None,
    supersedes_response_id: int | None = None,
    correction_reason: str | None = None,
) -> OutgoingRequestResponse:
    """Record one observation that a reply arrived, with its evidence.

    This stops the literal no-response condition for ``request_id`` and does
    nothing else: no Follow-up Plan is resolved, no Proposed Delta is disposed
    of, and no accepted value moves. The record question the plan carries is
    settled on the surface that owns it, and only that settles it.

    Exactly one of ``document_id``, ``source_delivery_id``, ``source_segment_id``
    or the ``observation``/``observed_by_principal`` pair is given; the kind is
    derived from which, so a caller cannot claim a kind its evidence does not
    match. ``source_reference`` is the exact reference in every case.

    ``completeness`` says which of the three observations this is. An
    acknowledgement and the substance that follows it are two rows, because
    they are two things that happened.
    """

    if not recorded_by_principal.strip():
        raise OutgoingRequestRefused("a recorded response names who recorded it")
    if not idempotency_key.strip():
        raise OutgoingRequestRefused("a recorded response needs an idempotency key")
    if completeness not in RESPONSE_COMPLETENESS:
        raise OutgoingRequestRefused(
            f"{completeness!r} is not one of the observations a response can "
            f"be; one of {', '.join(RESPONSE_COMPLETENESS)}"
        )
    if not source_reference.strip():
        raise OutgoingRequestRefused(
            "a recorded response carries the exact reference it was read from; "
            "'they replied' with nothing behind it is the assumption ADR-0090 "
            "retired STALE for"
        )
    if (supersedes_response_id is None) != (
        correction_reason is None or not correction_reason.strip()
    ):
        raise OutgoingRequestRefused(
            "a correction names the response it corrects and why; anything "
            "else is a further observation"
        )
    evidence_kind = _evidence_kind(
        document_id=document_id,
        source_delivery_id=source_delivery_id,
        source_segment_id=source_segment_id,
        observation=observation,
        observed_by_principal=observed_by_principal,
    )

    response_id = _appended(
        session,
        select(
            func.append_outgoing_request_response(
                project_id,
                request_id,
                received_on,
                completeness,
                evidence_kind,
                document_id,
                source_delivery_id,
                source_segment_id,
                observation,
                observed_by_principal,
                source_reference,
                recorded_by_principal,
                supersedes_response_id,
                correction_reason,
                idempotency_key,
            )
        ),
    )
    return session.get_one(OutgoingRequestResponse, int(response_id))


def _appended(session: Session, statement) -> int:
    """Run one append command, keeping its refusal structured for the caller.

    The command's invariants are PostgreSQL's, so a refusal arrives as an
    exception that aborts the transaction it was raised in. The savepoint keeps
    a refused recording from costing the caller the week it was about to
    re-render, and the database's own sentence is what the coordinator is
    shown: it names the row and the rule, which no sentence composed here
    could.
    """

    try:
        with session.begin_nested():
            return int(session.scalar(statement))
    except DBAPIError as refusal:
        message = str(getattr(refusal, "orig", refusal)).strip()
        raise OutgoingRequestRefused(
            message.splitlines()[0] if message else "the append was refused"
        ) from refusal


def _evidence_kind(
    *,
    document_id: int | None,
    source_delivery_id: int | None,
    source_segment_id: int | None,
    observation: str | None,
    observed_by_principal: str | None,
) -> str:
    """Which of the four kinds of evidence this observation carries.

    Derived from what the caller passed rather than declared beside it, so the
    two cannot disagree. PostgreSQL holds the same rule on the row, which is
    what makes it true for every writer and not only this one.
    """

    manual = bool(observation and observation.strip())
    given = [
        kind
        for kind, present in (
            ("document", document_id is not None),
            ("source_delivery", source_delivery_id is not None),
            ("source_segment", source_segment_id is not None),
            ("manual_observation", manual),
        )
        if present
    ]
    if len(given) != 1:
        raise OutgoingRequestRefused(
            "a recorded response links to exactly one of the incoming "
            "Document, the Source Delivery, the Source Segment, or an "
            "attributable manual observation; this names "
            + (", ".join(given) if given else "none of them")
        )
    if given[0] == "manual_observation" and not (
        observed_by_principal and observed_by_principal.strip()
    ):
        raise OutgoingRequestRefused(
            "a manual observation names the person who made it; that "
            "attribution is what makes it evidence rather than hearsay"
        )
    return given[0]


# --- reading the correspondence back ----------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """One observation that a reply arrived, as a reader receives it."""

    response_id: int
    request_id: int
    received_on: date
    completeness: str
    evidence_kind: str
    source_reference: str
    recorded_by_principal: str
    document_id: int | None
    source_delivery_id: int | None
    source_segment_id: int | None
    observation: str | None
    observed_by_principal: str | None
    corrects_response_id: int | None
    correction_reason: str | None
    superseded_by_response_id: int | None

    @property
    def stands(self) -> bool:
        """Whether this observation is the one still in force.

        Derived from whether anything corrected it, never stored: the accepted
        contract has no mutable status and the original of a correction stays
        readable exactly as it was recorded.
        """

        return self.superseded_by_response_id is None


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One retained outgoing request, the plans it advanced, and its replies."""

    request_id: int
    external_organization: str
    responsible_role: str | None
    question: str
    covered_plan_ids: tuple[int, ...]
    covered_subject_keys: tuple[str, ...]
    content_sha256: str
    sent_content_key: str
    sent_on: date
    sent_by_principal: str
    recorded_by_principal: str
    expected_response_by: date
    corrects_request_id: int | None
    correction_reason: str | None
    superseded_by_request_id: int | None
    responses: tuple[RecordedResponse, ...]

    @property
    def stands(self) -> bool:
        """Whether this record is the one still in force (see RecordedResponse)."""

        return self.superseded_by_request_id is None

    @property
    def recorded_by_the_sender(self) -> bool:
        """Whether the person who sent it is the person who recorded it."""

        return self.sent_by_principal == self.recorded_by_principal

    @property
    def standing_responses(self) -> tuple[RecordedResponse, ...]:
        """The observations not corrected by a later one."""

        return tuple(one for one in self.responses if one.stands)

    @property
    def answered(self) -> bool:
        """Whether anything at all has come back.

        Any standing observation stops the literal no-response condition,
        including an acknowledgement: "they have not replied" stops being true
        the moment they reply, whatever the reply contained. What the reply
        contained is ``completeness``, and it is reported rather than used to
        keep the clock running.
        """

        return bool(self.standing_responses)


def read_correspondence(
    session: Session, *, project_id: int, as_of: datetime | None
) -> tuple[RecordedRequest, ...]:
    """Every retained request for this project as of a declared cutoff.

    Ordered by the day it went out and then by identity, so two readings of the
    same records produce the same sequence. Corrections are *included*, both
    the original and the one that corrects it, because a page that hid the
    original would be rewriting history rather than appending to it; ``stands``
    is how a reader tells them apart.

    ``as_of`` is the caller's declared cutoff and the only time this reading
    knows. A request sent after it is not yet retained as of this reading, and
    an observation received after it has not arrived yet.

    ``as_of=None`` is a caller that is reading *history* rather than reading as
    of a moment, and is answered with everything retained. A history surface
    has no cutoff to declare and must not invent one: the record page exists so
    that correspondence stays reachable after the follow-up it advanced is over
    (#837), and a page that quietly clipped the last request at a cutoff it
    read off a clock would be the same unreachability in a subtler form. It is
    required rather than defaulted, so declaring no cutoff stays a decision the
    caller makes in as many words.
    """

    cutoff = as_of.date() if as_of is not None else None
    requests = session.scalars(
        select(OutgoingRequest)
        .where(
            OutgoingRequest.project_id == project_id,
            *(
                (OutgoingRequest.sent_on <= cutoff,)
                if cutoff is not None
                else ()
            ),
        )
        .order_by(OutgoingRequest.sent_on, OutgoingRequest.id)
    ).all()
    if not requests:
        return ()
    ids = tuple(row.id for row in requests)

    plans: dict[int, list[int]] = {}
    for request_id, plan_id in session.execute(
        select(OutgoingRequestPlan.request_id, OutgoingRequestPlan.follow_up_plan_id)
        .where(
            OutgoingRequestPlan.project_id == project_id,
            OutgoingRequestPlan.request_id.in_(ids),
        )
        .order_by(OutgoingRequestPlan.follow_up_plan_id)
    ):
        plans.setdefault(request_id, []).append(plan_id)

    observations = session.scalars(
        select(OutgoingRequestResponse)
        .where(
            OutgoingRequestResponse.project_id == project_id,
            OutgoingRequestResponse.request_id.in_(ids),
        )
        .order_by(
            OutgoingRequestResponse.received_on, OutgoingRequestResponse.id
        )
    ).all()
    # Supersession is read from every observation, including one recorded
    # after this cutoff: an observation somebody has since corrected is known
    # to be wrong, and a reading that showed it as standing because the
    # correction is dated later would state something nobody believes.
    corrected_response = {
        row.supersedes_response_id: row.id
        for row in observations
        if row.supersedes_response_id is not None
    }
    by_request: dict[int, list[RecordedResponse]] = {}
    for row in observations:
        if cutoff is not None and row.received_on > cutoff:
            continue
        by_request.setdefault(row.request_id, []).append(
            RecordedResponse(
                response_id=row.id,
                request_id=row.request_id,
                received_on=row.received_on,
                completeness=row.completeness,
                evidence_kind=row.evidence_kind,
                source_reference=row.source_reference,
                recorded_by_principal=row.recorded_by_principal,
                document_id=row.document_id,
                source_delivery_id=row.source_delivery_id,
                source_segment_id=row.source_segment_id,
                observation=row.observation,
                observed_by_principal=row.observed_by_principal,
                corrects_response_id=row.supersedes_response_id,
                correction_reason=row.correction_reason,
                superseded_by_response_id=corrected_response.get(row.id),
            )
        )

    corrected_request = {
        row.supersedes_request_id: row.id
        for row in requests
        if row.supersedes_request_id is not None
    }
    return tuple(
        RecordedRequest(
            request_id=row.id,
            external_organization=row.external_organization,
            responsible_role=row.responsible_role,
            question=row.question,
            covered_plan_ids=tuple(plans.get(row.id, ())),
            covered_subject_keys=tuple(row.covered_subject_keys),
            content_sha256=row.content_sha256,
            sent_content_key=row.sent_content_key,
            sent_on=row.sent_on,
            sent_by_principal=row.sent_by_principal,
            recorded_by_principal=row.recorded_by_principal,
            expected_response_by=row.expected_response_by,
            corrects_request_id=row.supersedes_request_id,
            correction_reason=row.correction_reason,
            superseded_by_request_id=corrected_request.get(row.id),
            responses=tuple(by_request.get(row.id, ())),
        )
        for row in requests
    )
