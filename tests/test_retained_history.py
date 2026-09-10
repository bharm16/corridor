"""A retained citation must outlive the reader that wrote it, or retirement stops.

#741 removes the engine that wrote every historical ``prose_span`` locator.
The question that decides whether that is allowed is not "does the new reader
work" but "can a citation an accepted decision already made still be verified
afterwards".  These tests seed that history rather than reconstructing a
corpus: a registered Document, a retained segment written under the old
reading, and an Evidence Link that cites it.

They hold four promises.  A retained reading replays from its own digests with
no reader of any kind involved.  That replay is *not* a fresh reading of the
original physical location and never reports itself as one -- the seeded
impossible locator replays and refuses to reconstruct in the same test.  The
fresh path fails closed, naming what it needed, instead of silently answering
from the record.  And no amount of text-shaped agreement rebinds a retained
locator onto the replacement reader: the decision refuses it and the database
refuses the edit.
"""

from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import sys

import pytest
from sqlalchemy import text

from corridor.evidence_citations import cite_source_segments, evidence_quotation
from corridor.models import Document, EvidenceLink, SourceSegment
from corridor.retained_history import (
    BASIS_MEANING,
    FRESH_ORIGINAL_LOCATION,
    INSUFFICIENT_BINDING_EVIDENCE,
    RETAINED_READING_REPLAY,
    SUFFICIENT_BINDING_EVIDENCE,
    CrossReaderBindingProposal,
    FreshReadingUnavailable,
    cross_reader_binding_decision,
    fresh_original_location_reading,
    replay_retained_reading,
)
from corridor.source_append import SegmentValues, append_source_segments
from corridor.source_segment_errors import (
    SourceDocumentDigestMismatch,
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
)

from pdf_fixture_support import PdfFixture

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "src" / "corridor" / "retained_history.py"

# The words the retained citation stands on, placed on the fixture page and
# stored on the segment. The fixture declares the page text a conforming
# reader recovers, so the historical offsets are authored from the placement
# rather than read back out of the reader under retirement.
RETAINED_WORDS = "The utility relocation is complete."


@pytest.fixture
def registered(session, project, tmp_path):
    """A registered PDF Document and the bytes it was registered as."""

    path = tmp_path / "coordination-minutes.pdf"
    fixture = PdfFixture()
    page = fixture.add_page()
    page.text((72, 72), f"Retained coordination minutes.\n{RETAINED_WORDS}")
    fixture.save(path)
    document = Document(
        project_id=project.id,
        sha256=sha256(path.read_bytes()).hexdigest(),
        filename=path.name,
        doc_type="minutes",
    )
    session.add(document)
    session.flush()
    return document, path, page.expected_text


def _retained_segment(
    session, document, *, page_no: int, start: int, end: int, ordinal: int = 1
) -> SourceSegment:
    """One retained ``prose_span`` citation, as the old reading recorded it."""

    (segment,) = append_source_segments(
        session,
        project_id=document.project_id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=[
            SegmentValues(
                kind="prose_span",
                exact_text=RETAINED_WORDS,
                content_sha256=sha256(RETAINED_WORDS.encode("utf-8")).hexdigest(),
                ordinal=ordinal,
                page_no=page_no,
                start_offset=start,
                end_offset=end,
            )
        ],
    )
    return segment


# --- the retained reading replays without a reader -------------------------


def test_the_contract_module_imports_no_reader_of_its_own(session):
    """The replay path must stay importable when both engines are gone."""

    tree = ast.parse(CONTRACT.read_text())
    imported: set[str] = set()
    for node in tree.body:  # module scope only: a lazy import is the point
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "corridor.source_segments" not in imported
    assert {name for name in imported if name.startswith("corridor")} == {
        "corridor.models",
        "corridor.source_segment_errors",
    }


def test_a_retained_citation_replays_from_its_own_digests(session, registered):
    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )

    replay = replay_retained_reading(segment, document=document, path=path)

    assert replay.basis == RETAINED_READING_REPLAY
    assert replay.exact_text == RETAINED_WORDS
    assert replay.locator_scheme == "prose_span"
    assert replay.source_segment_id == segment.id
    assert replay.original_bytes_verified is True


def test_replay_refuses_tampered_words_and_the_wrong_bytes(
    session, registered, tmp_path
):
    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )

    other = tmp_path / "other.pdf"
    replacement = PdfFixture(identity="other")
    replacement.add_page().text((72, 72), "A different document.")
    replacement.save(other)
    with pytest.raises(SourceDocumentDigestMismatch):
        replay_retained_reading(segment, document=document, path=other)

    tampered = SourceSegment(
        project_id=segment.project_id,
        document_id=segment.document_id,
        kind="prose_span",
        exact_text=RETAINED_WORDS.replace("complete", "incomplete"),
        content_sha256=segment.content_sha256,
        ordinal=1,
        page_no=1,
        start_offset=segment.start_offset,
        end_offset=segment.end_offset,
    )
    with pytest.raises(SourceSegmentDigestMismatch):
        replay_retained_reading(tampered)


def test_a_cited_retained_segment_dereferences_through_its_citation(
    session, registered
):
    """The path an investigation actually walks: link -> citation -> words."""

    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )
    link = EvidenceLink(document_id=document.id, page_no=1, quote=RETAINED_WORDS)
    session.add(link)
    session.flush()
    cite_source_segments(session, link, (segment,))

    quotation = evidence_quotation(session, link)

    assert quotation.owner == "source_segments"
    assert quotation.passages == (RETAINED_WORDS,)
    cited = session.get(SourceSegment, quotation.source_segment_ids[0])
    assert replay_retained_reading(cited, document=document, path=path).exact_text == (
        RETAINED_WORDS
    )


# --- replay is not a fresh reading of the original location -----------------


def test_replay_is_not_a_fresh_reading_of_the_original_location(
    session, registered
):
    """A locator that cannot possibly be reconstructed still replays.

    That is the whole distinction: the replay answers a question about the
    record, and the fresh path answers a question about the source. If one
    could stand in for the other this segment would either fail both or pass
    both.
    """

    document, path, _ = registered
    impossible = _retained_segment(session, document, page_no=999, start=0, end=5)

    replay = replay_retained_reading(impossible, document=document, path=path)
    assert replay.basis == RETAINED_READING_REPLAY
    assert replay.exact_text == RETAINED_WORDS

    with pytest.raises((SourceSegmentLocatorMismatch, FreshReadingUnavailable)):
        fresh_original_location_reading(document, impossible, path)

    assert BASIS_MEANING[RETAINED_READING_REPLAY]["reader_required"] is False
    assert BASIS_MEANING[FRESH_ORIGINAL_LOCATION]["reader_required"] is True
    assert BASIS_MEANING[RETAINED_READING_REPLAY]["does_not_establish"]


def test_the_fresh_path_reads_the_recorded_location_when_its_reader_is_here(
    session, registered
):
    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )

    try:
        recovered = fresh_original_location_reading(document, segment, path)
    except FreshReadingUnavailable as unavailable:
        pytest.skip(f"the reader that wrote this locator is absent: {unavailable}")
    assert recovered == RETAINED_WORDS


def test_the_fresh_path_refuses_by_name_when_its_reader_is_gone(
    session, registered, monkeypatch
):
    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )

    # Absence, not a stub that answers: an unimportable owner is what the
    # retired environment actually presents to this call.
    monkeypatch.setitem(sys.modules, "corridor.source_segments", None)

    with pytest.raises(FreshReadingUnavailable) as refused:
        fresh_original_location_reading(document, segment, path)
    assert refused.value.locator_scheme == "prose_span"
    assert "corridor.source_segments" in str(refused.value)


# --- no text-shaped agreement rebinds a retained locator --------------------


@pytest.mark.parametrize("evidence", sorted(INSUFFICIENT_BINDING_EVIDENCE))
def test_no_text_shaped_evidence_authorizes_a_cross_reader_binding(evidence):
    decision = cross_reader_binding_decision(
        CrossReaderBindingProposal(
            source_segment_id=1,
            from_locator_scheme="prose_span",
            to_locator_scheme="pdf_span",
            evidence=(evidence,),
        )
    )

    assert decision.authorized is False
    assert decision.refused_evidence == (evidence,)
    assert INSUFFICIENT_BINDING_EVIDENCE[evidence] in decision.reason


def test_every_insufficient_evidence_at_once_is_still_insufficient():
    offered = tuple(sorted(INSUFFICIENT_BINDING_EVIDENCE))
    decision = cross_reader_binding_decision(
        CrossReaderBindingProposal(
            source_segment_id=1,
            from_locator_scheme="prose_span",
            to_locator_scheme="pdf_span",
            evidence=offered,
        )
    )

    assert decision.authorized is False
    assert decision.refused_evidence == offered


def test_unrecognized_binding_evidence_fails_closed():
    decision = cross_reader_binding_decision(
        CrossReaderBindingProposal(
            source_segment_id=1,
            from_locator_scheme="prose_span",
            to_locator_scheme="pdf_span",
            evidence=("looked_right_to_me",),
        )
    )

    assert decision.authorized is False
    assert decision.unrecognized_evidence == ("looked_right_to_me",)


def test_only_a_discriminating_physical_location_proof_authorizes_a_binding():
    (proof,) = tuple(SUFFICIENT_BINDING_EVIDENCE)
    decision = cross_reader_binding_decision(
        CrossReaderBindingProposal(
            source_segment_id=1,
            from_locator_scheme="prose_span",
            to_locator_scheme="pdf_span",
            evidence=("equal_exact_text", proof),
        )
    )

    assert decision.authorized is True
    assert decision.refused_evidence == ()
    assert "append" in decision.record_rule


def test_a_retained_locator_cannot_be_rewritten_onto_the_replacement_reader(
    session, registered
):
    """Append-only is the database's answer, not this module's good manners."""

    document, path, page_text = registered
    start = page_text.index(RETAINED_WORDS)
    segment = _retained_segment(
        session, document, page_no=1, start=start, end=start + len(RETAINED_WORDS)
    )

    for statement in (
        "update source_segments set start_offset = start_offset + 1 where id = :id",
        "update source_segments set kind = 'pdf_span' where id = :id",
        "delete from source_segments where id = :id",
    ):
        savepoint = session.begin_nested()
        with pytest.raises(Exception, match="source segments are append-only"):
            session.execute(text(statement), {"id": segment.id})
        savepoint.rollback()

    session.expire(segment)
    assert segment.kind == "prose_span"
    assert segment.start_offset == start
