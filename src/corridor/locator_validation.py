"""The Source Passage Check: does a typed locator still reach its stored text?

Why this exists.  One mechanical flag, ``evidence_links.verified``, answered
exactly one question — is the cited quote on the cited page — and was shown to
customers as "verified".  Readers took the word for a claim about the value,
which is the one thing the check never made.  ADR-0082 names the mechanical
fact ``locator_validation_status`` (``valid``, ``invalid``, ``not_checked``):
whether a typed locator (ADR-0068) dereferences to the stored text or value,
and nothing about what the passage supports.  Semantic support is a separate
relation with its own actor, role, and reversal path
(``support_assessments.py``, #530); nothing here may stand in for it, and no
display may read support out of a passed check.

What was tried before.  The status was first sketched as a stored column on
``source_segments``.  That table is append-only in the database — a trigger
raises on every ``UPDATE`` and ``DELETE`` — and a status that goes stale the
moment the registered bytes change cannot sit on an immutable row without
lying about the present.  So the status is computed rather than stored: it
*is* the replay.  Each call dereferences the locator through the same native
reader that wrote the segment, so two readers of one revision always agree and
any reader can prove the answer from the original bytes.

``EvidenceLink.verified`` survives only as the compatibility projection
``locator_validation_status == valid``.  This module is the one place that
reads that column; every presentation asks here for a status instead, and the
column itself is removed under #458 with the legacy writer cut-over.

The ladder from a replay's outcome to a status lives here once.  The accepted
statement reader used to re-derive it beside its own byte staging, which is
how a reader could disagree with this module about the same segment.  A
reader that has the registered bytes passes their path; a reader that does
not passes ``None`` and still gets every answer that needs no bytes -- a
stored text that disagrees with its own digest is ``invalid`` and a retired
``prose_span`` scheme is ``not_re_readable`` whether or not the file is here
-- and ``not_checked`` otherwise.  Each answer carries the checker's own
sentence for a status other than ``valid``, so a reader reports the reason
without inventing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from corridor.models import Document, SourceSegment
from corridor.source_segments import (
    FreshReadingUnavailable,
    SourceSegmentIntegrityError,
    dereference_source_segment,
    replay_recorded_verbal_statement,
)
from corridor.verify import quote_appears_on

# The stored identifiers ADR-0082 fixed. They stay machine words: the customer
# labels live in ``presentation.py``.
VALID = "valid"
INVALID = "invalid"
NOT_CHECKED = "not_checked"
# The fourth state, added by #741. A retained ``prose_span`` locator is a pair
# of offsets into the page string the retired incumbent reader produced, so no
# reader now in the product can return to that location, and
# ``source_segments`` refuses with ``FreshReadingUnavailable`` rather than with
# an integrity error. Without this state the refusal would fall through to
# ``invalid``, and every retained prose citation would read as *Not found at
# cited location* -- an assertion about the source that nothing here made. The
# truthful answer is that the check could not be run, which is also not
# ``not_checked``: there is a cited location, it is recorded, and the reason it
# was not replayed is that its reader is gone (ADR-0094, ADR-0095).
#
# A retired reader is not the only way a recorded location becomes unreachable,
# and the state covers the others by covering the whole ``FreshReadingUnavailable``
# family. A live ``pdf_span``/``pdf_cell`` locator names a position inside one
# exact reading, so it is equally unreachable when the recorded reader
# configuration is not installed here (``NativeReaderUnavailable``) and when
# that reader runs and returns a different reading (``RecordedReadingNotReproduced``).
# Both used to be raised as ``SourceSegmentLocatorMismatch``, which is how a
# reader upgrade could relabel retained native citations *Not found at cited
# location* without any reader having opened a page.
NOT_RE_READABLE = "not_re_readable"
LOCATOR_VALIDATION_STATUSES = (VALID, INVALID, NOT_CHECKED, NOT_RE_READABLE)


class StoredLocatorCheck(Protocol):
    """Any row carrying the legacy stored result of the check."""

    verified: bool


@dataclass(frozen=True)
class LocatorValidation:
    """One Source Passage Check answer: the ADR-0082 status and its reason.

    ``reason`` is the replay's own sentence for a status other than ``valid``,
    and ``None`` for ``valid`` or for a check that did not run because the
    caller had no registered bytes to offer -- the caller knows why it had
    none, so it says so.
    """

    status: str
    reason: str | None = None


def source_segment_locator_validation(
    document: Document, segment: SourceSegment, path: Path | str | None
) -> LocatorValidation:
    """The status of ``source_segment_locator_validation_status`` with its reason.

    ``path`` is ``None`` when the registered bytes are not available to the
    caller.  The checks that need no bytes still run and still decide: stored
    text that disagrees with its own digest is ``invalid``, and a
    ``prose_span`` is ``not_re_readable`` because the reader that wrote it is
    gone regardless of the file.  Anything else is ``not_checked``: no
    locator was followed, and this module makes no claim about the source.
    """

    if path is None:
        if sha256(segment.exact_text.encode("utf-8")).hexdigest() != segment.content_sha256:
            return LocatorValidation(INVALID, "stored segment digest does not match its text")
        if segment.kind == "prose_span":
            return LocatorValidation(NOT_RE_READABLE, str(FreshReadingUnavailable("prose_span", None)))
        return LocatorValidation(NOT_CHECKED)
    try:
        dereference_source_segment(document, segment, path)
    except FreshReadingUnavailable as error:
        return LocatorValidation(NOT_RE_READABLE, str(error))
    except SourceSegmentIntegrityError as error:
        return LocatorValidation(INVALID, str(error))
    return LocatorValidation(VALID)


def source_segment_locator_validation_status(
    document: Document, segment: SourceSegment, path: Path | str
) -> str:
    """Replay one typed locator against its registered source bytes.

    ``valid`` only when the locator recovers the segment's exact stored text
    from the registered Document; a wrong Document, a locator that no longer
    exists, a moved span, or a digest that disagrees is ``invalid``.  The
    answer is mechanical: the same bytes and the same locator always give it.

    ``not_re_readable`` whenever the recorded location could not be reached at
    all: the reader that established the locator is no longer in the product
    (#741), the recorded native reader configuration is not installed here, or
    that reader ran on the registered bytes and returned a different reading
    from the one the locator indexes.  None of those is ``invalid``: nothing
    opened the page, so nothing can say the passage is not at its cited
    location.  Every check that needs no reader still runs first, so a changed
    Document digest or a stored text that disagrees with its own digest is
    still ``invalid``.  What a retained citation is verified by instead is
    ``retained_history.replay_retained_reading``, which proves its words and
    its registered bytes from their own digests.
    """

    return source_segment_locator_validation(document, segment, path).status


def recorded_verbal_statement_locator_validation(
    segment: SourceSegment,
) -> LocatorValidation:
    """Replay a Recorded Verbal Statement's words against their own digest.

    A verbal has no Document to dereference (ADR-0033), so its locator
    validation is the digest's self-consistency with the words it certifies
    and nothing more (ADR-0082's verbal provenance class).
    """

    try:
        replay_recorded_verbal_statement(segment)
    except SourceSegmentIntegrityError as error:
        return LocatorValidation(INVALID, str(error))
    return LocatorValidation(VALID)


def recorded_verbal_statement_locator_validation_status(
    segment: SourceSegment,
) -> str:
    """The status of ``recorded_verbal_statement_locator_validation`` alone."""

    return recorded_verbal_statement_locator_validation(segment).status


def cited_passage_locator_validation_status(
    quote: str | None, page_text: str | None
) -> str:
    """Run the legacy page locator's check now, from the page's own text.

    This is the replay behind a stored ``verified``: the same predicate
    ``verify.quote_appears_on`` that wrote it.  Without a quote or without the
    cited page's text there is nothing to dereference, so the check did not
    run rather than failing.
    """

    if not quote or page_text is None:
        return NOT_CHECKED
    return VALID if quote_appears_on(quote, page_text) else INVALID


def evidence_link_locator_validation_status(
    link: StoredLocatorCheck | None,
) -> str:
    """Read the legacy stored check as a status, for readers that display it.

    A cell with no supporting document has no locator to dereference, so its
    check has not run; that is a different fact from a cited passage that was
    looked for and not found, and the old boolean collapsed the two.  A stored
    ``False`` cannot distinguish "checked and absent" from "never checked", so
    a link that exists reports ``invalid``: the pessimistic reading is the one
    that never claims a check happened.
    """

    if link is None:
        return NOT_CHECKED
    return VALID if link.verified else INVALID


def evidence_link_verified(status: str) -> bool:
    """``EvidenceLink.verified`` as the compatibility projection it now is.

    ADR-0082 keeps the column only as ``locator_validation_status == valid``.
    The name is unchanged for ADR-0048 compatibility and its removal is
    scheduled under #458; writers that still set it derive it from here.
    """

    if status not in LOCATOR_VALIDATION_STATUSES:
        raise ValueError(f"unknown locator validation status {status!r}")
    return status == VALID
