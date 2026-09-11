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

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from corridor import refusals
from corridor.models import Document, Fact, SourceSegment
from corridor.packet_review import ChildReading, ItemReading
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_segments import source_segment_locator_words


__all__ = [
    "CONTROL_INTERPRETATION",
    "CONTROL_PASSAGE",
    "CORRECTION_CONTROL",
    "CORRECTION_SUPPORTING_TEXT",
    "CaptureCorrectionRefused",
    "ChallengedCapture",
    "CorrectionRequest",
    "NO_CHANGE_EXIT_UNAVAILABLE",
    "build_correction_request",
    "challenged_capture",
    "offers_correction",
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
