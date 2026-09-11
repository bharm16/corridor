"""Reporting that Corridor read one source wrong, bound to that exact capture (#836).

ADR-0100 asks for the act a coordinator has nowhere to put today.  The source
is clear, the extraction is wrong, and no alternative captured value exists:
Apply writes the misreading, Keep current rejects a change the source really
made, Edit and apply needs another captured Source Fact that does not exist,
Needs coordination sends Corridor's own defect to the customer as their work,
and Defer moves the same dead end to a later date.  What the coordinator
actually knows is *this capture is wrong about that passage*, and that is a
request about a capture rather than a decision about the record.

**It is an ancillary action, and nothing here may make it a fifth primary
decision.**  ADR-0085's four primary decisions and secondary Defer are
preserved exactly as ADR-0085 states them, so this module does not appear in
``packet_review.FOCUSED_OUTCOMES``, builds no ``PacketChildRequest``, and
resolves no Proposed Delta.  The primary decision about what the record should
show is still waiting to be made when a correction request is over.  That is
also why the control is a separate act on the focused form rather than a sixth
option in the outcome control: an option inside that select *is* an answer to
"what should happen to this source's value?", and this is not an answer to
that question.

**The request names the exact capture, because the query does not.**  A
``ProposedDelta`` carries subject, field, source family and source revision and
no foreign key to a Source Fact, so ``packet_review._incoming_facts``
reconstructs the capture behind a delta as "the newest Fact for this subject
and field inside this delta's own source lineage".  That reconstruction is
correct for a screen and wrong for a request: the answer moves the moment a
second capture of the same document and field lands, and operations would open
the request weeks later and find a different capture than the person
challenged.  So the request records ``facts.id`` and the ``source_segments.id``
the capture cited -- both append-only, both immutable, ``facts.content_sha256``
uniquely constrained -- and ``resolve_challenged_capture`` reads those rows by
identity and never re-runs the query.  ADR-0100 states the test:
``tests/test_capture_correction.py`` appends a second capture of the same
document and field after the request and proves the request still shows the
original capture and its original evidence while the reading has moved on.

**The passage the coordinator selects is a passage of the same retained
source.**  Operations corrects a capture against bytes that are already
retained; it does not settle what a different document says.  A coordinator
who believes another document already carries the right value has Edit and
apply, and the case this act exists for is exactly the one where that is
unavailable.  A selected segment outside the challenged capture's own document
is refused here rather than discovered by operations.

**Where a report lives.**  ``capture_correction_requests`` holds one row per
report, written only through ``report_capture_correction``, the record-decision
role's ``SECURITY DEFINER`` command; the runtime capabilities hold ``select``
and a guard trigger refuses every other write.  Its composite foreign keys are
what make the binding structural rather than conventional: the Fact is named
through ``(project_id, document_id, fact_id)``, and both passage columns
through that same ``document_id``, so a selected passage from another file --
or another customer's -- is unrepresentable rather than merely refused here.
The table carries no outcome, status or closure column, because what became of
a report is answered by the corrected capture and by a lifecycle half ADR-0100
leaves explicitly undecided.

**What this module may not do, said out loud.**  It changes no accepted value,
overwrites no Source Fact and no Source Segment, and writes no Project Record
revision.  The expected interpretation a coordinator types is the reason for a
request; it is never an accepted value and nothing here lets it become one
(ADR-0084).  A human reporting an observed defect is also a different act from
a model speculating about one: ``revision_change_explanation`` still refuses a
narrated "extraction error" as a fabricated cause, and nothing here relaxes
that -- the person reporting this read the cell themselves.

**The no-change outcome has no recorded exit, and that is stated rather than
invented.**  A correction may establish that nothing changed, which ADR-0100
calls a truthful outcome rather than a failure.  The obsolete technical
finding must then leave the actionable reading, and the two exits the schema
offers are both false entries in the decision lineage ADR-0082 requires:
``DeltaDisposition('reject')`` files a coordinator decision nobody made, and
``DeltaSupersession`` is ADR-0083's *newer source version* coalescing, whose
``ck_delta_supersessions_successor`` admits only a superseding delta, an
inbound thread reading or a minutes capture -- none of which a re-read of the
same version produces.  ``withdraw_for_no_change`` is the seam #842 calls and
refuses with ``NO_CHANGE_EXIT_UNAVAILABLE``, so the missing relationship is
named by a refusal rather than closed by an unannounced new disposition.

**No clock.**  Every instant is the caller's, as everywhere else on this
screen's seam.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re
from typing import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor import refusals
from corridor.models import (
    CaptureCorrectionRequest,
    Document,
    Fact,
    SourceSegment,
)
from corridor.packet_review import ChildReading, ItemReading
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_segments import source_segment_locator_words


__all__ = [
    "CONTROL_INTERPRETATION",
    "CONTROL_PASSAGE",
    "CORRECTION_CONTROL",
    "CORRECTION_SUPPORTING_TEXT",
    "COMMAND_REFUSAL_TOKEN",
    "CaptureCorrectionRefused",
    "ChallengedCapture",
    "CorrectionRequest",
    "NO_CHANGE_EXIT_UNAVAILABLE",
    "PassageChoice",
    "ReportedCorrection",
    "build_correction_request",
    "challenged_capture",
    "correction_idempotency_key",
    "offers_correction",
    "passage_choices",
    "record_correction_request",
    "reported_corrections",
    "resolve_challenged_capture",
    "withdraw_for_no_change",
]


# The control's own words, settled by ADR-0100 so #836 coins nothing.  It
# describes an act in plain words rather than naming a customer-facing type,
# which is why the terminology-research procedure is not triggered; a defined
# type for the act would need that research first.
CORRECTION_CONTROL = "Report an extraction error"
CORRECTION_SUPPORTING_TEXT = (
    "This reports that Corridor read this source wrong. It does not change "
    "the record and it does not answer this change: operations corrects the "
    "capture against the retained source, and the corrected reading comes "
    "back here for your decision."
)

# Why one request could not be built, in the same voice as the rest of the
# screen's refusals.  Each names a shape the coordinator can correct.
NOT_OFFERED_HERE = (
    "Report an extraction error is offered only where this change has no "
    "other captured source value to apply instead."
)
NEEDS_PASSAGE = (
    "Reporting an extraction error records which passage of this source the "
    "capture should have been read from."
)
PASSAGE_NOT_IN_THIS_SOURCE = (
    "The passage has to be one this source retained: operations corrects a "
    "capture against bytes that are already held, and another document's "
    "value is an Edit and apply rather than a correction."
)
NEEDS_INTERPRETATION = (
    "Reporting an extraction error records what the passage says, so "
    "operations can check the correction against the retained source."
)
CAPTURE_NOT_RETAINED = (
    "The capture this request names is no longer readable, so the request "
    "cannot be opened against it."
)

# The relationship ADR-0083's lifecycle does not have, named rather than
# implemented.  ADR-0100 instructs #836 to state it and bring it back as a
# decision; the sentence is what ``withdraw_for_no_change`` refuses with.
NO_CHANGE_EXIT_UNAVAILABLE = (
    "a corrected capture that establishes no change has no recorded exit from "
    "the Proposed Delta lifecycle: ADR-0083's supersession is a newer source "
    "version coalescing and this is the same version read again, and closing "
    "the delta as 'reject' would file a coordinator decision nobody made. The "
    "relationship a withdrawn-on-correction finding needs is a decision "
    "(#836), not a disposition this seam may invent."
)


class CaptureCorrectionRefused(refusals.Refusal, ValueError):
    """This correction request is not one the screen offered; nothing was written.

    ``reason`` is a stable machine code and ``str(exc)`` is the sentence a
    person reads.  The codes are ``not_offered``, ``passage_required``,
    ``passage_not_in_this_source``, ``interpretation_required``,
    ``capture_not_retained`` and ``relationship_not_decided``.  A refusal that
    belongs to one control on one row carries both, exactly as
    ``packet_review.ReviewScreenRefused`` does, so the screen binds the
    sentence to the input without deciding anything a second time.
    """

    refusal_kind = refusals.NOT_OFFERED

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        kind: str = refusals.NOT_OFFERED,
        delta_id: int | None = None,
        control: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.refusal_kind = kind
        self.delta_id = delta_id
        self.control = control


# The per-request controls a refusal can be bound to, in the same shape the
# focused answers use so one screen renders both.
CONTROL_PASSAGE = "correction-passage"
CONTROL_INTERPRETATION = "correction-interpretation"


@dataclass(frozen=True, slots=True)
class ChallengedCapture:
    """The one capture a request challenges, read back by immutable identity.

    Everything here is retained state rather than a screen string: the Fact's
    own row and content digest, the Source Segment it cited with that
    segment's exact text and typed locator, and the Document those belong to.
    Two readings of the same request build the same value however many later
    captures of the same document and field have arrived.
    """

    delta_id: int
    project_id: int
    fact_id: int
    fact_content_sha256: str
    subject_identity: str
    field: str
    source_segment_id: int | None
    document_id: int | None
    document_filename: str | None
    exact_text: str | None
    locator: str | None


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """One attributable request that a named capture is wrong about its source.

    It resolves no Proposed Delta and changes nothing in the accepted record.
    ``selected_source_segment_id`` is the passage the coordinator pointed at,
    which is very often *not* the passage the capture cited -- reading the
    wrong cell is the defect being reported.
    """

    capture: ChallengedCapture
    reported_by_principal: str
    reported_at: datetime
    selected_source_segment_id: int
    expected_interpretation: str


def _capture_of(
    delta_id: int,
    fact: Fact,
    segment: SourceSegment | None,
    document: Document | None,
) -> ChallengedCapture:
    """One capture, read off the retained rows the same way at both doors.

    The request is built once and opened many times, and the two readings have
    to agree field for field or the second-capture property is only true of
    whichever door a test happened to use.
    """

    return ChallengedCapture(
        delta_id=delta_id,
        project_id=int(fact.project_id),
        fact_id=int(fact.id),
        fact_content_sha256=fact.content_sha256,
        subject_identity=fact.subject_key,
        field=fact.fact_type,
        source_segment_id=None if segment is None else int(segment.id),
        document_id=None if fact.document_id is None else int(fact.document_id),
        document_filename=None if document is None else document.filename,
        exact_text=None if segment is None else segment.exact_text,
        locator=None if segment is None else source_segment_locator_words(segment),
    )


def offers_correction(item: ItemReading, child: ChildReading) -> bool:
    """Whether this focused item's child carries the correction control.

    Offered only where the coordinator has a capture to challenge and no other
    captured source value to apply instead -- which is the case ADR-0084's
    constrained edit leaves them with nothing to choose in.  A child another
    item owns carries no control here at all, because ADR-0085's exactly-once
    rule is kept by the item that decides it.
    """

    return (
        item.focused
        and child.held_out_reason is None
        and child.incoming_fact_id is not None
        and not item.alternatives_for(child.delta_id)
    )


def challenged_capture(
    session: Session, item: ItemReading, child: ChildReading
) -> ChallengedCapture:
    """The exact capture this child is showing, as the request will name it.

    Built from the retained Fact and Source Segment rather than from the
    child's rendered strings, so what the request carries is the identity a
    later reader resolves and not a copy of the screen.
    """

    if not offers_correction(item, child):
        raise CaptureCorrectionRefused(
            "not_offered",
            NOT_OFFERED_HERE,
            delta_id=child.delta_id,
            control=CONTROL_PASSAGE,
        )
    fact = session.get(Fact, child.incoming_fact_id)
    if fact is None:
        raise CaptureCorrectionRefused(
            "capture_not_retained",
            CAPTURE_NOT_RETAINED,
            kind=refusals.STALE,
            delta_id=child.delta_id,
        )
    segment_id = child.source.source_segment_id if child.source is not None else None
    segment = (
        session.get(SourceSegment, segment_id) if segment_id is not None else None
    )
    document = (
        session.get(Document, fact.document_id)
        if fact.document_id is not None
        else None
    )
    return _capture_of(child.delta_id, fact, segment, document)


def build_correction_request(
    session: Session,
    item: ItemReading,
    child: ChildReading,
    *,
    principal: HumanPrincipal,
    reported_at: datetime,
    selected_source_segment_id: int | None,
    expected_interpretation: str,
) -> CorrectionRequest:
    """Build the one request this control records, or refuse it whole.

    Every refusal is raised before anything is read for writing and names the
    control that holds it, so a coordinator keeps what they had already typed.
    """

    actor = require_human_principal(principal)
    capture = challenged_capture(session, item, child)
    if selected_source_segment_id is None:
        raise CaptureCorrectionRefused(
            "passage_required",
            NEEDS_PASSAGE,
            kind=refusals.MALFORMED_INPUT,
            delta_id=child.delta_id,
            control=CONTROL_PASSAGE,
        )
    selected = session.get(SourceSegment, int(selected_source_segment_id))
    if (
        selected is None
        or capture.document_id is None
        or selected.document_id != capture.document_id
        or int(selected.project_id) != capture.project_id
    ):
        raise CaptureCorrectionRefused(
            "passage_not_in_this_source",
            PASSAGE_NOT_IN_THIS_SOURCE,
            kind=refusals.MALFORMED_INPUT,
            delta_id=child.delta_id,
            control=CONTROL_PASSAGE,
        )
    interpretation = expected_interpretation.strip()
    if not interpretation:
        raise CaptureCorrectionRefused(
            "interpretation_required",
            NEEDS_INTERPRETATION,
            kind=refusals.MALFORMED_INPUT,
            delta_id=child.delta_id,
            control=CONTROL_INTERPRETATION,
        )
    return CorrectionRequest(
        capture=capture,
        reported_by_principal=actor.subject,
        reported_at=reported_at,
        selected_source_segment_id=int(selected.id),
        expected_interpretation=interpretation,
    )


def resolve_challenged_capture(
    session: Session, request: CorrectionRequest
) -> ChallengedCapture:
    """Open a request against the capture it named, however old the request is.

    The recorded ``fact_id`` and ``source_segment_id`` are read directly; the
    subject-and-field query the reading uses is never re-run, because its
    answer moves when the next delivery lands.  The Fact's own content digest
    is checked against the one the request recorded, so an identity that ever
    stopped naming the same capture is a refusal rather than a silent swap.
    """

    named = request.capture
    fact = session.get(Fact, named.fact_id)
    if (
        fact is None
        or fact.content_sha256 != named.fact_content_sha256
        or int(fact.project_id) != named.project_id
    ):
        raise CaptureCorrectionRefused(
            "capture_not_retained",
            CAPTURE_NOT_RETAINED,
            kind=refusals.STALE,
            delta_id=named.delta_id,
        )
    segment = (
        session.get(SourceSegment, named.source_segment_id)
        if named.source_segment_id is not None
        else None
    )
    document = (
        session.get(Document, fact.document_id)
        if fact.document_id is not None
        else None
    )
    return _capture_of(named.delta_id, fact, segment, document)


def withdraw_for_no_change(request: CorrectionRequest) -> None:
    """The exit a no-change correction needs, which the lifecycle does not have.

    ADR-0100 requires the obsolete technical finding to be removed or
    superseded *through the declared lifecycle*, and instructs #836 to state
    the missing relationship rather than quietly implement a new disposition
    where the lifecycle cannot represent the transition.  It cannot: the
    correction is a re-read of the same source version, so ADR-0083's
    supersession does not describe it and ``ck_delta_supersessions_successor``
    has no successor to name; and a ``reject`` would record a customer
    rejection nobody performed.

    So this seam refuses, in one place, rather than leaving #842 to pick one
    of those two false entries.  Deciding the relationship -- what it is
    called, what it records, and what a reader sees where the finding used to
    be -- is the maintainer's, and this refusal is what asks for it.
    """

    raise CaptureCorrectionRefused(
        "relationship_not_decided",
        NO_CHANGE_EXIT_UNAVAILABLE,
        delta_id=request.capture.delta_id,
    )


# --- Recording one, and reading back what stands ---------------------------


#: How many retained passages either side of the cited one the form offers as
#: choices. It bounds the *picker*, never the rule: a misread almost always
#: lands next to the cell it should have read, and a workbook rendition can
#: retain thousands of cells, so a select listing every one of them is a list
#: nobody can use. ``report_capture_correction`` still accepts any passage of
#: the capture's own document, and the exact-source view (#831) is where a
#: coordinator reads one outside this window.
PASSAGE_CHOICE_WINDOW = 12


@dataclass(frozen=True, slots=True)
class PassageChoice:
    """One retained passage of the capture's own source, as the form offers it."""

    source_segment_id: int
    locator: str
    exact_text: str
    cited: bool


@dataclass(frozen=True, slots=True)
class ReportedCorrection:
    """One report that already stands against a capture, as the screen shows it.

    Shown read-only beside the control so a coordinator returning to an item
    can see that the defect was already reported, by whom, and against which
    passage. It carries no control of its own: a report is not withdrawn, and
    a further observation is a further report.
    """

    request_id: int
    delta_id: int
    fact_id: int
    reported_by_principal: str
    reported_at: datetime
    expected_interpretation: str
    selected_locator: str | None


def passage_choices(
    session: Session, capture: ChallengedCapture
) -> tuple[PassageChoice, ...]:
    """The retained passages of this capture's own source, around the cited one.

    Ordered as the source presents them, so a coordinator reads down the rows
    of the sheet or the spans of the page rather than down a list of ids.
    """

    if capture.document_id is None:
        return ()
    found = list(
        session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.project_id == capture.project_id,
                SourceSegment.document_id == capture.document_id,
            )
            .order_by(SourceSegment.ordinal, SourceSegment.id)
        )
    )
    positions = [
        index
        for index, row in enumerate(found)
        if row.id == capture.source_segment_id
    ]
    if positions:
        cited = positions[0]
        found = found[
            max(0, cited - PASSAGE_CHOICE_WINDOW) : cited + PASSAGE_CHOICE_WINDOW + 1
        ]
    else:
        found = found[: PASSAGE_CHOICE_WINDOW * 2 + 1]
    return tuple(
        PassageChoice(
            source_segment_id=int(row.id),
            locator=source_segment_locator_words(row),
            exact_text=row.exact_text,
            cited=row.id == capture.source_segment_id,
        )
        for row in found
    )


def correction_idempotency_key(request: CorrectionRequest) -> str:
    """One key per distinct report, so a resubmitted form replays (#457).

    The expected interpretation is in the key because two reports against the
    same capture that say different things about the passage are two reports;
    the same words submitted twice are one.
    """

    material = "\x00".join(
        [
            str(request.capture.delta_id),
            str(request.capture.fact_id),
            request.capture.fact_content_sha256,
            str(request.selected_source_segment_id),
            request.reported_by_principal,
            request.expected_interpretation,
        ]
    )
    return f"capture-correction:{sha256(material.encode('utf-8')).hexdigest()[:32]}"


def record_correction_request(
    session: Session, request: CorrectionRequest
) -> CaptureCorrectionRequest:
    """Append the report through the record-decision role's own command.

    The runtime capabilities hold no write on ``capture_correction_requests``
    and a guard trigger refuses one that does not arrive through
    ``report_capture_correction``, so this is the only way in. It runs in the
    caller's transaction: a rolled-back caller leaves no report and no claim
    that one was made.
    """

    capture = request.capture
    try:
        answer = session.execute(
            select(
                func.report_capture_correction(
                    capture.project_id,
                    capture.delta_id,
                    capture.fact_id,
                    capture.fact_content_sha256,
                    capture.source_segment_id,
                    request.selected_source_segment_id,
                    request.expected_interpretation,
                    request.reported_by_principal,
                    request.reported_at,
                    correction_idempotency_key(request),
                )
            )
        ).scalar_one()
    except DBAPIError as exc:
        raise _command_refusal(request, exc) from exc
    session.expire_all()
    return session.get_one(CaptureCorrectionRequest, int(answer["request_id"]))


#: How the command names the rule it refused, so the surface prints the
#: command's own sentence and takes only the machine token off it. Rewriting
#: those sentences here would put a second, quietly divergent vocabulary in
#: front of one rule, which is what `delta_refusals` exists to prevent.
COMMAND_REFUSAL_TOKEN = re.compile(r"capture_correction:([a-z_]+)\s*")


def _command_refusal(
    request: CorrectionRequest, exc: DBAPIError
) -> CaptureCorrectionRefused:
    """The command's refusal, as this screen's refusal, with its own words."""

    message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
    found = COMMAND_REFUSAL_TOKEN.search(message)
    return CaptureCorrectionRefused(
        "refused" if found is None else found.group(1),
        COMMAND_REFUSAL_TOKEN.sub("", message, count=1).strip() or message,
        kind=refusals.CONFLICT,
        delta_id=request.capture.delta_id,
    )


def reported_corrections(
    session: Session, *, project_id: int, delta_ids: Sequence[int]
) -> Mapping[int, tuple[ReportedCorrection, ...]]:
    """Every report that already stands against these findings, by finding.

    One grouped statement rather than one per child: the screen asks this for
    every change on the item it is about to render.
    """

    wanted = [int(value) for value in dict.fromkeys(delta_ids)]
    if not wanted:
        return {}
    rows = list(
        session.execute(
            select(CaptureCorrectionRequest, SourceSegment)
            .outerjoin(
                SourceSegment,
                SourceSegment.id
                == CaptureCorrectionRequest.selected_source_segment_id,
            )
            .where(
                CaptureCorrectionRequest.project_id == project_id,
                CaptureCorrectionRequest.delta_id.in_(wanted),
            )
            .order_by(CaptureCorrectionRequest.id)
        ).all()
    )
    found: dict[int, list[ReportedCorrection]] = defaultdict(list)
    for row, segment in rows:
        found[int(row.delta_id)].append(
            ReportedCorrection(
                request_id=int(row.id),
                delta_id=int(row.delta_id),
                fact_id=int(row.fact_id),
                reported_by_principal=row.reported_by_principal,
                reported_at=row.reported_at,
                expected_interpretation=row.expected_interpretation,
                selected_locator=(
                    None if segment is None else source_segment_locator_words(segment)
                ),
            )
        )
    return {key: tuple(value) for key, value in found.items()}
