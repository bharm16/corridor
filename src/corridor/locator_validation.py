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
"""

from __future__ import annotations

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
NOT_RE_READABLE = "not_re_readable"
LOCATOR_VALIDATION_STATUSES = (VALID, INVALID, NOT_CHECKED, NOT_RE_READABLE)


class StoredLocatorCheck(Protocol):
    """Any row carrying the legacy stored result of the check."""

    verified: bool


def source_segment_locator_validation_status(
    document: Document, segment: SourceSegment, path: Path | str
) -> str:
    """Replay one typed locator against its registered source bytes.

    ``valid`` only when the locator recovers the segment's exact stored text
    from the registered Document; a wrong Document, a locator that no longer
    exists, a moved span, or a digest that disagrees is ``invalid``.  The
    answer is mechanical: the same bytes and the same locator always give it.

    ``not_re_readable`` when the reader that established the locator is no
    longer in the product (#741).  That is not ``invalid``: nothing opened the
    page, so nothing can say the passage is not at its cited location.  What a
    retained citation is verified by instead is
    ``retained_history.replay_retained_reading``, which proves its words and
    its registered bytes from their own digests.
    """

    try:
        dereference_source_segment(document, segment, path)
    except FreshReadingUnavailable:
        return NOT_RE_READABLE
    except SourceSegmentIntegrityError:
        return INVALID
    return VALID


def recorded_verbal_statement_locator_validation_status(
    segment: SourceSegment,
) -> str:
    """Replay a Recorded Verbal Statement's words against their own digest.

    A verbal has no Document to dereference (ADR-0033), so its locator
    validation is the digest's self-consistency with the words it certifies
    and nothing more (ADR-0082's verbal provenance class).
    """

    try:
        replay_recorded_verbal_statement(segment)
    except SourceSegmentIntegrityError:
        return INVALID
    return VALID


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
