"""Verify a retained citation without re-reading the page it came from.

Why this exists.  ADR-0094 retires the reader that established every
historical ``prose_span`` locator, and a retained accepted decision goes on
citing the segment that reader produced.  One function was answering two
different questions.  ``source_segments.dereference_source_segment`` opens the
registered bytes and asks a reader to produce the text again at the recorded
physical location: that is a claim about the *source*, and only the reader
that established the location can make it.  A retained citation usually needs
the other question answered -- are these still exactly the words that were
read, out of exactly the bytes that were registered?  That is a claim about
the *record*, digests answer it, and no PDF reader takes part.

Collapsing the two is how a retirement loses history.  If the only available
check needs the retired reader, every retained citation becomes unverifiable
on the day the reader leaves.  If the surviving check quietly inherits the old
name, a receipt starts asserting that the physical location was confirmed when
nothing opened the page.  So the two bases are named separately, they travel
with what each one does and does not establish, and the fresh path fails
closed naming what it needed rather than answering from the record.

This module deliberately imports no reader.  ``fresh_original_location_reading``
imports its owner inside the call, so an environment without that reader
raises ``FreshReadingUnavailable`` here instead of making the retained-history
contract itself unimportable.  ``tests/test_retained_history.py`` holds that
module scope stays reader-free.

What was tried, and rejected.  Rebinding the historical locators onto the
replacement reader's coordinates would have let one contract serve both.  The
#733 measurement found that 5.1% of registered prose locators cannot be
rebound at all and -- the part that decides this -- that equal text, equal
occurrence counts, an occurrence ordinal, a normalized match and the
occurrence nearest the old offset are each satisfied by two different physical
labels.  None of them is a proof of location, so none of them may authorize a
binding; ``cross_reader_binding_decision`` refuses on that evidence and says
which piece failed and why.  A binding that is ever proved is appended beside
the retained locator and never written over it: ``source_segments`` refuses
``UPDATE`` and ``DELETE`` in the database, and a correction is a further
appended record rather than an edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from corridor.models import Document, SourceSegment
from corridor.source_segment_errors import (
    SourceDocumentDigestMismatch,
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
)

# The two bases a retained citation can be verified on. They are separate
# words on purpose: a receipt, a screen or a report that says which one it
# used cannot be misread as having done the other.
RETAINED_READING_REPLAY = "retained_reading_replay"
FRESH_ORIGINAL_LOCATION = "fresh_original_location"

BASIS_MEANING: dict[str, dict[str, object]] = {
    RETAINED_READING_REPLAY: {
        "question": "are these still the exact words that were read, from the "
        "bytes that were registered?",
        "establishes": (
            "the retained words are exactly the words whose digest was recorded",
            "the retained locator still names the Document it was written against",
            "the registered Document's bytes are unchanged, when those bytes "
            "were supplied to the check",
        ),
        "does_not_establish": (
            "that the recorded physical location still yields these words",
            "that any reader available now can reach that location",
            "that the passage supports the value citing it, which is a "
            "support assessment with its own actor and reversal path",
        ),
        "reader_required": False,
    },
    FRESH_ORIGINAL_LOCATION: {
        "question": "does the recorded physical location in the original bytes "
        "still yield these words?",
        "establishes": (
            "the recorded location, read by the reader that established it, "
            "still yields the retained words",
        ),
        "does_not_establish": (
            "anything under a reader other than the one that wrote the locator",
            "that the passage supports the value citing it",
        ),
        "reader_required": True,
    },
}

# Evidence that looks like agreement and is not a location. Each entry is the
# reason it cannot authorize a binding, quoted into the refusal so a caller
# reads the argument rather than a status code.
INSUFFICIENT_BINDING_EVIDENCE: dict[str, str] = {
    "equal_exact_text": "two readings can print the same characters for two "
    "different physical labels",
    "equal_occurrence_count": "equal counts say nothing about which occurrence "
    "the retained locator addressed",
    "single_occurrence": "one occurrence under the new reading does not "
    "establish that the retained reading had one",
    "occurrence_ordinal": "the two readings do not share an ordering, so the "
    "nth occurrence of one is not the nth occurrence of the other",
    "normalized_text_match": "folding whitespace or Unicode conceals exactly "
    "the difference that would have refused the match",
    "nearest_offset": "the readings do not share a coordinate system, so "
    "'nearest' is a guess wearing a number",
    "same_page": "a page is not a location",
}

SUFFICIENT_BINDING_EVIDENCE: dict[str, str] = {
    "discriminating_physical_location_proof": "independently discriminating "
    "coordinates for the retained span and the replacement span in one common "
    "source frame, recorded and reachable",
}

APPEND_ONLY_RULE = (
    "A cross-reader binding is appended beside the retained locator and never "
    "written onto it: the retained Source Segment is immutable in the database, "
    "and a later correction is a further appended record, not an edit."
)


class FreshReadingUnavailable(RuntimeError):
    """The reader that established this locator is not installed here."""

    def __init__(self, locator_scheme: str, missing: str | None) -> None:
        super().__init__(
            f"a {locator_scheme} locator can only be re-read at its original "
            f"location by the reader that established it, and "
            f"{missing or 'that reader'} is not available here"
        )
        self.locator_scheme = locator_scheme
        self.missing = missing


@dataclass(frozen=True)
class RetainedReadingReplay:
    """What a retained citation's replay establishes, and on which basis."""

    basis: str
    source_segment_id: int | None
    locator_scheme: str
    exact_text: str
    reader_identity: dict | None
    original_bytes_verified: bool


def replay_retained_reading(
    segment: SourceSegment,
    *,
    document: Document | None = None,
    path: Path | str | None = None,
) -> RetainedReadingReplay:
    """Prove a retained reading from its own digests, opening no page.

    Supplying the registered ``document`` and its ``path`` adds the original
    bytes to what is checked; that is still an integrity check on the
    registered rendition, not a reading of the locator, and the result says so
    through ``original_bytes_verified``.
    """

    if path is not None and document is None:
        raise SourceSegmentLocatorMismatch(
            "original bytes cannot be checked without the Document they were "
            "registered as"
        )
    if document is not None and (
        segment.document_id != document.id
        or segment.project_id != document.project_id
    ):
        raise SourceSegmentLocatorMismatch(
            "source segment does not belong to the supplied Document"
        )
    if sha256(segment.exact_text.encode("utf-8")).hexdigest() != segment.content_sha256:
        raise SourceSegmentDigestMismatch(
            "stored segment digest does not match its retained words"
        )
    verified = False
    if path is not None and document is not None:
        if sha256(Path(path).read_bytes()).hexdigest() != document.sha256:
            raise SourceDocumentDigestMismatch(
                "source bytes do not match the registered Document digest"
            )
        verified = True
    return RetainedReadingReplay(
        basis=RETAINED_READING_REPLAY,
        source_segment_id=segment.id,
        locator_scheme=segment.kind,
        exact_text=segment.exact_text,
        reader_identity=segment.reader_identity,
        original_bytes_verified=verified,
    )


def fresh_original_location_reading(
    document: Document, segment: SourceSegment, path: Path | str
) -> str:
    """Re-read the recorded physical location, or refuse naming what is missing.

    The import is inside the call because its absence is the answer: an
    environment without the reader that wrote this locator cannot make this
    claim, and must say so rather than substitute another reader or fall back
    to the retained words.
    """

    try:
        from corridor.source_segments import dereference_source_segment
    except ImportError as absent:
        raise FreshReadingUnavailable(
            segment.kind, getattr(absent, "name", None) or "corridor.source_segments"
        ) from absent
    return dereference_source_segment(document, segment, path)


@dataclass(frozen=True)
class CrossReaderBindingProposal:
    """A claim that a retained locator and a new locator name one location."""

    source_segment_id: int
    from_locator_scheme: str
    to_locator_scheme: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CrossReaderBindingDecision:
    authorized: bool
    reason: str
    refused_evidence: tuple[str, ...] = ()
    unrecognized_evidence: tuple[str, ...] = ()
    record_rule: str = APPEND_ONLY_RULE


def cross_reader_binding_decision(
    proposal: CrossReaderBindingProposal,
) -> CrossReaderBindingDecision:
    """Authorize a cross-reader binding only on a proof of physical location."""

    offered = tuple(dict.fromkeys(proposal.evidence))
    unrecognized = tuple(
        name
        for name in offered
        if name not in INSUFFICIENT_BINDING_EVIDENCE
        and name not in SUFFICIENT_BINDING_EVIDENCE
    )
    if unrecognized:
        return CrossReaderBindingDecision(
            authorized=False,
            reason=(
                "unrecognized binding evidence is refused rather than weighed: "
                + ", ".join(unrecognized)
            ),
            unrecognized_evidence=unrecognized,
        )
    proved = tuple(name for name in offered if name in SUFFICIENT_BINDING_EVIDENCE)
    if not proved:
        insufficient = tuple(
            name for name in offered if name in INSUFFICIENT_BINDING_EVIDENCE
        )
        return CrossReaderBindingDecision(
            authorized=False,
            reason=(
                "nothing offered identifies the physical location the retained "
                "locator addressed: "
                + "; ".join(
                    f"{name} -- {INSUFFICIENT_BINDING_EVIDENCE[name]}"
                    for name in insufficient
                )
                if insufficient
                else "a binding needs a proof of physical location and none was offered"
            ),
            refused_evidence=insufficient,
        )
    return CrossReaderBindingDecision(
        authorized=True,
        reason=(
            "the binding is proved by "
            + ", ".join(proved)
            + ": "
            + "; ".join(SUFFICIENT_BINDING_EVIDENCE[name] for name in proved)
        ),
    )
