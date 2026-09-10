"""Reader identity, source append authority and physical PDF locator integration."""

from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path

from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from corridor.config import Settings, settings
from corridor.ingest import ingest_document, ingest_native_reader
from corridor.models import (
    DocPage,
    Document,
    Project,
    SourceSegment,
    TokenLayerManifest,
)
from corridor.reader_segments import (
    NativeCellIndex,
    append_native_segments,
    native_segment_values,
    native_segment_pdf_boxes,
    native_segment_render_boxes,
    pdf_cell_id,
    read_native_pdf,
    select_pdf_cell_segment,
)
from corridor.render_profiles import render_page_derivative
from corridor.source_append import SegmentValues, append_source_segments
from corridor.locator_validation import (
    INVALID,
    NOT_RE_READABLE,
    source_segment_locator_validation_status,
)
from corridor.retained_history import FreshReadingUnavailable
from corridor.source_segment_errors import (
    NativeReaderUnavailable,
    RecordedReadingNotReproduced,
    SourceSegmentIntegrityError,
)
from corridor.source_segments import (
    SourceDocumentDigestMismatch,
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
    dereference_source_segment,
)
from corridor.token_layers import page_text_projection
from pdf_fixture_support import PdfFixture


def native_pdf(tmp_path, *, rotation=0, name="native.pdf", clipped=False):
    fixture = PdfFixture()
    page = fixture.add_page(
        width=460, height=370, rotation=rotation, cropbox=(30, 20, 430, 310)
    )
    for x in (20, 170, 320):
        page.line((x, 50), (x, 150))
    for y in (50, 100, 150):
        page.line((20, y), (320, y))
    page.text((30, 75), "Owner")
    page.text((180, 75), "Station")
    page.text((30, 125), "Exact Utilities")
    page.text((180, 125), "1149+00")
    page.text((340, 235), "OUT")
    if clipped:
        page._content.append("q 0 0 8 8 re W n\n")
        page.text((50, 260), "HIDDEN")
        page._content.append("Q\n")
    return fixture.save(tmp_path / name)


def registered(session, project, path):
    document = Document(
        project_id=project.id,
        sha256=sha256(path.read_bytes()).hexdigest(),
        filename=path.name,
        doc_type="minutes",
        pages=1,
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    return document


def reading(path, **kwargs):
    return read_native_pdf(
        path, source_sha256=sha256(path.read_bytes()).hexdigest(), **kwargs
    )


def detached(document, value):
    return SourceSegment(
        project_id=document.project_id, document_id=document.id, **asdict(value)
    )


def test_one_reading_owns_page_tokens_prose_cells_and_clipped_stream(tmp_path):
    path = native_pdf(tmp_path, clipped=True)
    first = reading(path)
    again = reading(path)
    values = native_segment_values(first)
    assert values == native_segment_values(again)
    assert first.reading_sha256 == again.reading_sha256
    layer = first.token_layers[0]
    assert layer.identity.configuration["reading_sha256"] == first.reading_sha256
    assert first.page_text(1) == page_text_projection(layer)
    assert "HIDDEN" not in first.page_text(1)
    clipped_text = first.page_text(1, stream="clipped")
    clipped_glyphs = first.pages[0]["clipped"]["value"]
    # Clipped glyphs have no advance/break metadata in this reader. Platform
    # substitute-font metrics can change their projected spacing. The source
    # sequence and location are authored; the exact projection belongs to its
    # recorded reading and is never normalized into another platform's text.
    assert [glyph["text"] for glyph in clipped_glyphs] == list("HIDDEN")
    assert clipped_text and clipped_text == again.page_text(1, stream="clipped")
    assert all(
        50 <= glyph["box"][0] < glyph["box"][2] <= 100 for glyph in clipped_glyphs
    )
    assert all(
        245 <= glyph["box"][1] < glyph["box"][3] <= 265 for glyph in clipped_glyphs
    )
    assert {v.exact_text for v in values if v.kind == "pdf_cell"} >= {
        "Owner",
        "Station",
        "Exact Utilities",
        "1149+00",
    }
    spans = [v for v in values if v.kind == "pdf_span"]
    assert any(
        v.span_stream == "clipped" and v.exact_text == clipped_text for v in spans
    )
    outside = PdfFixture()
    outside.add_page().text((40, 80), "Standalone outside wording.")
    outside_reading = reading(outside.save(tmp_path / "outside.pdf"))
    outside_segments = native_segment_values(outside_reading)
    assert len(outside_segments) == 1
    assert outside_segments[0].exact_text == "Standalone outside wording."
    assert outside_segments[0].location_json["outside_source_indices"]
    for span in spans:
        source = first.page_text(span.page_no, stream=span.span_stream)
        assert source[span.start_offset : span.end_offset] == span.exact_text
        assert span.reader_identity == first.identity


def test_challenger_ingest_executes_for_existing_document_and_preserves_history(
    session,
    project,
    tmp_path,
    monkeypatch,
):
    path = native_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="minutes",
        images_dir=tmp_path / "legacy",
    )
    old_segments = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).all()
    old_state = [
        (s.id, s.kind, s.exact_text, s.start_offset, s.end_offset, s.content_sha256)
        for s in old_segments
    ]
    old_pages = [
        (p.id, p.text)
        for p in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    ]
    calls = []
    from corridor.token_layers import PdfiumExecutor

    original = PdfiumExecutor.read_document

    def counted(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PdfiumExecutor, "read_document", counted)
    # The retired engine is absent; no patch may import it just to prohibit it.
    monkeypatch.setattr(
        "corridor.ingest._extract_pages",
        lambda *a, **k: pytest.fail("incumbent ingest/OCR path called"),
    )
    fresh, segments = ingest_native_reader(
        session, document=document, path=path, images_dir=tmp_path / "challenger"
    )
    assert len(calls) == 1 and segments
    again, replayed = ingest_native_reader(
        session, document=document, path=path, images_dir=tmp_path / "challenger"
    )
    assert len(calls) == 2 and again.reading_sha256 == fresh.reading_sha256
    assert [s.id for s in replayed] == [s.id for s in segments]
    changed, new_rows = ingest_native_reader(
        session,
        document=document,
        path=path,
        images_dir=tmp_path / "challenger",
        dpi=37,
    )
    assert len(calls) == 3 and changed.reading_sha256 != fresh.reading_sha256
    assert not ({s.id for s in segments} & {s.id for s in new_rows})
    session.expire_all()
    assert [
        (
            session.get(SourceSegment, s[0]).id,
            session.get(SourceSegment, s[0]).kind,
            session.get(SourceSegment, s[0]).exact_text,
            session.get(SourceSegment, s[0]).start_offset,
            session.get(SourceSegment, s[0]).end_offset,
            session.get(SourceSegment, s[0]).content_sha256,
        )
        for s in old_state
    ] == old_state
    assert [
        (p.id, p.text)
        for p in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    ] == old_pages
    assert session.scalar(
        select(TokenLayerManifest.id).where(
            TokenLayerManifest.document_id == document.id,
            TokenLayerManifest.engine_json["configuration"]["reading_sha256"].astext
            == fresh.reading_sha256,
        )
    )


def test_selected_native_ingest_never_creates_new_incumbent_spans(
    session, project, tmp_path, monkeypatch
):
    path = native_pdf(tmp_path)
    document = ingest_document(
        session,
        project_id=project.id,
        path=path,
        doc_type="minutes",
        images_dir=tmp_path / "ingest",
    )
    segments = session.scalars(
        select(SourceSegment).where(SourceSegment.document_id == document.id)
    ).all()
    assert segments and {s.kind for s in segments} == {"pdf_span", "pdf_cell"}
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    for span in segments:
        if span.kind == "pdf_span" and span.span_stream == "page":
            assert page.text[span.start_offset : span.end_offset] == span.exact_text


def _retained_prose_span(session, document, *, exact_text, page_no, start, end):
    """A retained ``prose_span`` citation, as the retired reading recorded it.

    Ingest no longer writes this kind: a PDF is read by the paired-rendition
    reader and its segments are ``pdf_span``/``pdf_cell`` (#736, #741). The
    rows written before that are still in the database and still cited, so a
    test about them seeds one rather than asking today's ingest to produce
    what it can no longer produce.
    """

    (segment,) = append_source_segments(
        session,
        project_id=document.project_id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=[
            SegmentValues(
                kind="prose_span",
                exact_text=exact_text,
                content_sha256=sha256(exact_text.encode("utf-8")).hexdigest(),
                ordinal=1,
                page_no=page_no,
                start_offset=start,
                end_offset=end,
            )
        ],
    )
    return segment


def test_a_retained_prose_locator_refuses_by_name_and_never_reaches_the_new_reader(
    session, project, tmp_path, monkeypatch
):
    """The offsets belong to a reading that is gone, and no reader inherits them.

    This used to dereference through the incumbent, which is exactly what
    ``dereference_source_segment`` cannot do now (#741). The one thing it must
    never do instead is answer with the replacement reader: the two readings do
    not share a coordinate system, so the same offsets name a different place.
    So it refuses, and names the reader it needed.
    """

    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    legacy = _retained_prose_span(
        session, doc, exact_text="Owner", page_no=1, start=0, end=5
    )
    monkeypatch.setattr(
        "corridor.token_layers.PdfiumExecutor.read_document",
        lambda *a, **k: pytest.fail("new reader used old offset"),
    )

    with pytest.raises(FreshReadingUnavailable, match="pymupdf is not available"):
        dereference_source_segment(doc, legacy, path)

    # And it is not an integrity failure, which is what would make the Source
    # Passage Check call the passage Not found at cited location.
    with pytest.raises(FreshReadingUnavailable) as raised:
        dereference_source_segment(doc, legacy, path)
    assert not isinstance(raised.value, SourceSegmentIntegrityError)
    assert (
        source_segment_locator_validation_status(doc, legacy, path) == NOT_RE_READABLE
    )


def test_native_deref_refuses_wrong_digest_reader_or_location(
    session, project, tmp_path
):
    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    native = reading(path)
    value = next(v for v in native_segment_values(native) if v.kind == "pdf_cell")
    good = detached(doc, value)
    assert dereference_source_segment(doc, good, path) == value.exact_text
    for changed in (
        replace(value, table_index=99),
        replace(
            value, location_json={**value.location_json, "table_box": [0, 0, 1, 1]}
        ),
    ):
        with pytest.raises(SourceSegmentLocatorMismatch):
            dereference_source_segment(doc, detached(doc, changed), path)
    with pytest.raises(SourceSegmentDigestMismatch):
        dereference_source_segment(
            doc, detached(doc, replace(value, content_sha256="a" * 64)), path
        )
    with pytest.raises(SourceDocumentDigestMismatch):
        dereference_source_segment(
            doc, good, native_pdf(tmp_path, name="other.pdf", rotation=90)
        )


def test_native_replay_separates_an_unavailable_reading_from_a_missing_passage(
    session, project, tmp_path
):
    """Three outcomes, three answers: two availability refusals and one integrity.

    All three used to raise ``SourceSegmentLocatorMismatch``, so a reader
    configuration this build does not have, and a reading that no longer
    reproduces, both reported *Not found at cited location* -- an assertion
    that a reader went to the customer's page and the passage was not there.
    Neither of them opened a page. Only the third did.
    """

    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    value = next(v for v in native_segment_values(reading(path)) if v.kind == "pdf_cell")
    assert dereference_source_segment(doc, detached(doc, value), path) == value.exact_text

    # 1. The recorded reader configuration is not the one installed here.
    elsewhere = detached(doc, replace(
        value, reader_identity={**value.reader_identity, "pypdf_version": "0.0.0"}
    ))
    with pytest.raises(NativeReaderUnavailable) as unavailable:
        dereference_source_segment(doc, elsewhere, path)
    assert not isinstance(unavailable.value, SourceSegmentIntegrityError)
    assert (
        source_segment_locator_validation_status(doc, elsewhere, path) == NOT_RE_READABLE
    )

    # 2. That reader is installed and ran, and did not return the reading these
    # offsets are positions inside.
    superseded = detached(doc, replace(value, reading_sha256="f" * 64))
    with pytest.raises(RecordedReadingNotReproduced) as stale:
        dereference_source_segment(doc, superseded, path)
    assert not isinstance(stale.value, SourceSegmentIntegrityError)
    assert (
        source_segment_locator_validation_status(doc, superseded, path) == NOT_RE_READABLE
    )

    # 3. The recorded reading reproduced, the locator was followed into it, and
    # it reached different content. That is a statement about the source.
    moved = detached(doc, replace(value, table_index=99))
    with pytest.raises(SourceSegmentLocatorMismatch) as mismatch:
        dereference_source_segment(doc, moved, path)
    assert isinstance(mismatch.value, SourceSegmentIntegrityError)
    assert source_segment_locator_validation_status(doc, moved, path) == INVALID

    # Nothing an unavailable reading excuses: the registered bytes are proved
    # before any reader is asked, so a changed source still fails on its digest.
    with pytest.raises(SourceDocumentDigestMismatch):
        dereference_source_segment(
            doc, superseded, native_pdf(tmp_path, name="other.pdf", rotation=90)
        )


def test_cell_ids_are_scoped_and_never_accept_a_model_literal(
    session, project, tmp_path
):
    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    native = reading(path)
    rows = append_native_segments(session, doc, native)
    cell = next(s for s in rows if s.kind == "pdf_cell" and s.exact_text == "1149+00")
    address = pdf_cell_id(
        doc,
        native.reading_sha256,
        cell.page_no,
        cell.table_index,
        cell.cell_row,
        cell.cell_column,
    )
    scope = dict(
        session=session,
        document=doc,
        index=NativeCellIndex(doc, native),
        page_no=cell.page_no,
        table_index=cell.table_index,
        cell_id=address,
    )
    assert select_pdf_cell_segment(**scope).exact_text == "1149+00"
    for overrides in (
        {"page_no": 2},
        {"table_index": cell.table_index + 1},
        {"cell_id": address.replace(native.reading_sha256, "a" * 64)},
        {"index": NativeCellIndex(doc, reading(path, dpi=37))},
    ):
        with pytest.raises(SourceSegmentLocatorMismatch):
            select_pdf_cell_segment(**{**scope, **overrides})
    with pytest.raises(TypeError):
        select_pdf_cell_segment(**scope, exact_text="1148+00")
    other = Project(slug="other-native", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    other_doc = registered(session, other, path)
    append_native_segments(session, other_doc, native)
    other_index = NativeCellIndex(other_doc, native)
    other_id = pdf_cell_id(
        other_doc,
        native.reading_sha256,
        cell.page_no,
        cell.table_index,
        cell.cell_row,
        cell.cell_column,
    )
    assert other_id != address
    with pytest.raises(SourceSegmentLocatorMismatch):
        select_pdf_cell_segment(
            **{**scope, "document": other_doc, "index": other_index}
        )
    with pytest.raises(SourceSegmentLocatorMismatch):
        select_pdf_cell_segment(**{**scope, "document": other_doc})


@pytest.mark.parametrize(
    "field,value",
    [
        ("rendition_sha256", "0" * 64),
        ("reading_sha256", None),
        ("cell_row", None),
        ("reader_identity", {}),
    ],
)
def test_database_append_refuses_incomplete_or_forged_native_identity(
    session, project, tmp_path, field, value
):
    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    native = reading(path)
    cell = next(v for v in native_segment_values(native) if v.kind == "pdf_cell")
    with pytest.raises(DBAPIError):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=doc.id,
            recorded_verbal_origin_id=None,
            segments=[replace(cell, **{field: value})],
        )


def test_database_native_replay_is_immutable_and_runtime_has_no_raw_insert(
    session, project, tmp_path
):
    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    native = reading(path)
    rows = append_native_segments(session, doc, native)
    assert not session.scalar(
        text("select has_table_privilege('corridor_worker','source_segments','INSERT')")
    )
    assert (
        session.scalar(
            text(
                "select pg_get_userbyid(proowner) from pg_proc where proname='append_source_segments'"
            )
        )
        == "corridor_source_append"
    )
    changed = replace(
        next(v for v in native_segment_values(native) if v.kind == "pdf_cell"),
        location_json={"glyphs": []},
    )
    with pytest.raises(DBAPIError, match="already bound"):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=doc.id,
            recorded_verbal_origin_id=None,
            segments=[changed],
        )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_original_glyph_locations_land_on_rendered_pixels_after_crop_and_rotation(
    session, project, tmp_path, rotation
):
    path = native_pdf(tmp_path, rotation=rotation)
    doc = registered(session, project, path)
    native = reading(path)
    value = next(
        v
        for v in native_segment_values(native)
        if v.kind == "pdf_span" and "OUT" == v.exact_text
    )
    segment = detached(doc, value)
    assert (
        native_segment_pdf_boxes(segment)[0][0] >= 370
    )  # 340 authored + nonzero crop origin
    derivative = render_page_derivative(
        pdf_path=path,
        page_number=1,
        profile_name="review",
        output_dir=tmp_path / "renders",
        rasterizer="pdfium",
    )
    image = Image.open(derivative.artifact_path).convert("L")
    for box in native_segment_render_boxes(segment, derivative):
        x0, y0, x1, y1 = box
        pixels = image.crop((int(x0), int(y0), int(x1) + 1, int(y1) + 1))
        assert min(pixels.tobytes()) < 80
        assert pixels.width > 1 and pixels.height > 1
    # This exercises the actual crop pipeline, not merely an inverse matrix.
    from corridor.render_profiles import PageBox

    boxes = [g["display_box"] for g in value.location_json["glyphs"]]
    clip = PageBox(
        x0=int((min(b[0] for b in boxes) - 8) * 1000),
        y0=int((min(b[1] for b in boxes) - 8) * 1000),
        x1=int((max(b[2] for b in boxes) + 8) * 1000),
        y1=int((max(b[3] for b in boxes) + 8) * 1000),
    )
    clipped = render_page_derivative(
        pdf_path=path,
        page_number=1,
        profile_name="cell_detail",
        output_dir=tmp_path / "detail",
        clip_page_box=clip,
        rasterizer="pdfium",
    )
    image = Image.open(clipped.artifact_path).convert("L")
    for box in native_segment_render_boxes(segment, clipped):
        x0, y0, x1, y1 = box
        assert 0 <= x0 < x1 <= image.width + 1
        assert 0 <= y0 < y1 <= image.height + 1
        assert (
            min(image.crop((int(x0), int(y0), int(x1) + 1, int(y1) + 1)).tobytes()) < 80
        )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_native_locator_follows_actual_nonzero_deskew_pixels(
    session, project, tmp_path, monkeypatch, rotation
):
    import math
    from corridor.render_profiles import load_render_profile_bundle

    fixture = PdfFixture()
    page = fixture.add_page(
        width=500, height=400, rotation=rotation, cropbox=(30, 20, 450, 330)
    )
    angle = math.radians(3)
    page._content.append(
        f"q {math.cos(angle)} {math.sin(angle)} {-math.sin(angle)} {math.cos(angle)} 0 0 cm\n"
    )
    for y in (55, 105, 155, 205):
        page.line((25, y), (335, y))
    page.text((160, 130), "SOURCE")
    page._content.append("Q\n")
    path = fixture.save(tmp_path / "skewed.pdf")
    doc = registered(session, project, path)
    native = reading(path)
    value = next(
        v
        for v in native_segment_values(native)
        if v.kind == "pdf_span" and v.exact_text == "SOURCE"
    )
    segment = detached(doc, value)
    bundle = load_render_profile_bundle()
    # This declared proof profile preserves text pixels after the actual
    # production deskew step; table morphology intentionally removes text.
    table = bundle.profiles["table_cv"].model_copy(
        update={
            "preprocessing": ("grayscale",),
            "profile_id": "test-native-deskew-grayscale-v1",
        }
    )
    monkeypatch.setattr(
        "corridor.render_profiles.load_render_profile_bundle",
        lambda: bundle.model_copy(
            update={
                "profiles": {**bundle.profiles, "table_cv": table},
            }
        ),
    )
    derivative = render_page_derivative(
        pdf_path=path,
        page_number=1,
        profile_name="table_cv",
        output_dir=tmp_path / "deskew",
        rasterizer="pdfium",
    )
    step = next(step for step in derivative.transforms if step.name == "deskew")
    assert abs(step.parameters["angle_degrees"]) > 1
    image = Image.open(derivative.artifact_path).convert("L")
    projected = native_segment_render_boxes(segment, derivative)
    wrong = derivative.model_copy(
        update={
            "transforms": tuple(s for s in derivative.transforms if s.name != "deskew")
        }
    )
    before = native_segment_render_boxes(segment, wrong)
    assert any(
        abs(a - b) > 2
        for actual, unmoved in zip(projected, before, strict=True)
        for a, b in zip(actual, unmoved, strict=True)
    )
    for x0, y0, x1, y1 in projected:
        ink = image.crop((int(x0) - 1, int(y0) - 1, int(x1) + 2, int(y1) + 2))
        assert min(ink.tobytes()) < 80


def test_statement_wording_must_resolve_to_one_exact_reader_span(
    session, project, tmp_path
):
    """The reader's page spans are what a statement resolves against now.

    This used to refuse the handoff outright, because the incumbent's spans
    were the selected ones and #447 owned changing that. #447 recorded the
    maintainer's acceptance (ADR-0095) and #741 removed the incumbent, so the
    only refusal left is the real one: wording that does not resolve to one
    exact span.
    """

    from corridor.facts import append_statement_wording_facts
    from corridor.materializer import FactValidationError
    from corridor.models import Candidate, ExtractionRun

    path = native_pdf(tmp_path)
    doc = registered(session, project, path)
    append_native_segments(session, doc, reading(path))
    candidate = Candidate(
        kind="event",
        source_document_id=doc.id,
        payload_json={
            "fields": {
                "description": "Exact Utilities",
                "stated_party": "Exact Utilities",
            },
            "citations": [
                {
                    "document_id": doc.id,
                    "page": 1,
                    "quote": "Exact Utilities",
                    "verified": True,
                }
            ],
        },
    )
    # Test the public handoff before any unmapped Candidate is materialized.
    with pytest.raises(
        FactValidationError, match="must resolve to one exact prose span"
    ):
        append_statement_wording_facts(session, doc, ExtractionRun(), (candidate,))


def test_clipped_stream_is_persisted_separately_and_same_stream_overlap_is_refused(
    session, project, tmp_path
):
    path = native_pdf(tmp_path, clipped=True)
    document = registered(session, project, path)
    native = reading(path)
    rows = append_native_segments(session, document, native)
    hidden = next(row for row in rows if row.span_stream == "clipped")
    visible = next(row for row in rows if row.span_stream == "page")
    assert hidden.start_offset == visible.start_offset == 0
    exact_projection = native.page_text(1, stream="clipped")
    assert [glyph["text"] for glyph in hidden.location_json["glyphs"]] == list("HIDDEN")
    assert hidden.exact_text == exact_projection
    assert hidden.content_sha256 == sha256(exact_projection.encode()).hexdigest()
    assert dereference_source_segment(document, hidden, path) == exact_projection
    assert reading(path).page_text(1, stream="clipped") == exact_projection
    source = next(
        value
        for value in native_segment_values(native)
        if value.kind == "pdf_span" and value.span_stream == "page"
    )
    overlapping_text = source.exact_text[1:]
    overlapping = replace(
        source,
        ordinal=99,
        start_offset=1,
        exact_text=overlapping_text,
        content_sha256=sha256(overlapping_text.encode()).hexdigest(),
    )
    with pytest.raises(DBAPIError, match="prose source segments cannot overlap"):
        append_source_segments(
            session,
            project_id=project.id,
            document_id=document.id,
            recorded_verbal_origin_id=None,
            segments=[overlapping],
        )


def test_native_reading_cannot_be_constructed_or_replaced_with_literal_text(tmp_path):
    from corridor.token_layers import NativePdfReading

    path = native_pdf(tmp_path)
    native = reading(path)
    with pytest.raises(
        SourceSegmentLocatorMismatch, match="must come from registered PDF bytes"
    ):
        NativePdfReading(
            native.rendition_sha256, native.identity_json, native.pages_json
        )
    pages = json.loads(native.pages_json)
    pages[0]["characters"]["value"][0]["text"] = "MODEL LITERAL"
    with pytest.raises(
        SourceSegmentLocatorMismatch, match="must come from registered PDF bytes"
    ):
        replace(native, pages_json=json.dumps(pages))
    # Shallow-frozen token models must never be retained mutable reading state.
    layer = native.token_layers[0]
    original = native_segment_values(native)
    layer.quality["token_source_indices"][0] = []
    layer.identity.configuration["reader_engine"] = "impostor"
    assert native_segment_values(native) == original


def test_cell_index_is_built_once_and_does_not_retain_mutable_values(
    session, project, tmp_path, monkeypatch
):
    path = native_pdf(tmp_path)
    document = registered(session, project, path)
    native = reading(path)
    cells = [
        row
        for row in append_native_segments(session, document, native)
        if row.kind == "pdf_cell"
    ]
    calls = []
    from corridor.reader_segments import native_segment_values as project_values

    def counted(reading):
        calls.append(1)
        return project_values(reading)

    monkeypatch.setattr("corridor.reader_segments.native_segment_values", counted)
    index = NativeCellIndex(document, native)
    for cell in cells:
        address = pdf_cell_id(
            document,
            native.reading_sha256,
            cell.page_no,
            cell.table_index,
            cell.cell_row,
            cell.cell_column,
        )
        returned = index.expected(address)
        returned.location_json["glyphs"] = []
        assert (
            select_pdf_cell_segment(
                session,
                document=document,
                index=index,
                page_no=cell.page_no,
                table_index=cell.table_index,
                cell_id=address,
            ).id
            == cell.id
        )
    assert len(calls) == 1


def test_unrelated_rollback_and_measurement_files_do_not_define_native_replay(
    tmp_path, monkeypatch
):
    from corridor.token_layers import native_integration_digest

    path = native_pdf(tmp_path)
    first = reading(path)
    original = Path.read_bytes
    irrelevant_reads = []

    def changed_unrelated(self):
        content = original(self)
        if self.name in {"LOOP-LOG.md", "token_layers.py"}:
            irrelevant_reads.append(self.name)
            return (
                content
                + b"\nUnrelated retained engine removal or holdout log append.\n"
            )
        return content

    monkeypatch.setattr(Path, "read_bytes", changed_unrelated)
    assert native_integration_digest() == first.identity["integration_sha256"]
    assert reading(path).reading_sha256 == first.reading_sha256
    assert irrelevant_reads == []


def test_changed_clipped_projection_cannot_rebind_a_stored_reading(
    session, project, tmp_path, monkeypatch
):
    from corridor_pdf_reader.execution import PdfiumExecutor

    path = native_pdf(tmp_path, clipped=True)
    document = registered(session, project, path)
    original = reading(path)
    hidden = next(
        row
        for row in append_native_segments(session, document, original)
        if row.span_stream == "clipped"
    )
    stored = (hidden.exact_text, hidden.content_sha256, hidden.reading_sha256)

    class DifferentClippedBreaks:
        def read_document(self, source, **kwargs):
            result = PdfiumExecutor().read_document(source, **kwargs)
            for glyph in result["pages"][0]["clipped"]["value"]:
                glyph["break_before"] = False
            return result

    # Model a later reader result with different clipped-word boundaries,
    # keeping original bytes, glyph text and physical boxes unchanged. A text
    # projection change may never retarget an earlier stored locator.
    monkeypatch.setattr("corridor.token_layers.PdfiumExecutor", DifferentClippedBreaks)
    alternate = reading(path)
    assert [g["text"] for g in alternate.pages[0]["clipped"]["value"]] == list("HIDDEN")
    assert alternate.page_text(1, stream="clipped") != original.page_text(
        1, stream="clipped"
    )
    assert alternate.reading_sha256 != original.reading_sha256
    # The reader is here and it ran; what is gone is the reading these offsets
    # index, so this is an availability refusal and the Source Passage Check
    # says the location cannot be re-read rather than that the passage is not
    # at it.
    with pytest.raises(
        RecordedReadingNotReproduced, match="the recorded native reading is not available"
    ) as refusal:
        dereference_source_segment(document, hidden, path)
    assert not isinstance(refusal.value, SourceSegmentIntegrityError)
    assert (
        source_segment_locator_validation_status(document, hidden, path)
        == NOT_RE_READABLE
    )
    assert (hidden.exact_text, hidden.content_sha256, hidden.reading_sha256) == stored


@pytest.mark.parametrize(
    "field,refusal",
    [
        ("reader_identity", NativeReaderUnavailable),
        ("reading_sha256", RecordedReadingNotReproduced),
    ],
)
def test_the_prose_batch_gives_the_same_two_availability_answers(
    session, project, tmp_path, field, refusal
):
    """A batch proof may not blur the two refusals the scalar reader separates.

    `replay_native_segments` verifies one coherent prose reading, and it used
    to answer "prose reading does not reproduce" as a
    ``SourceSegmentLocatorMismatch`` whether the recorded reader was absent or
    present-and-different. Both mean no page was opened, so both belong to the
    ``FreshReadingUnavailable`` family; the caller still refuses either way.
    """

    from corridor.reader_segments import replay_native_segments

    path = native_pdf(tmp_path)
    document = registered(session, project, path)
    values = [
        value for value in native_segment_values(reading(path)) if value.kind == "pdf_span"
    ]
    assert values
    changes = {
        "reader_identity": {**values[0].reader_identity, "pypdf_version": "0.0.0"},
        "reading_sha256": "f" * 64,
    }
    segments = [detached(document, replace(value, **{field: changes[field]})) for value in values]
    with pytest.raises(refusal) as raised:
        replay_native_segments(document, segments, path)
    assert not isinstance(raised.value, SourceSegmentIntegrityError)
    assert isinstance(raised.value, FreshReadingUnavailable)


def _without(key):
    """A reader result missing one top-level key of the child-process payload."""

    def mutate(result):
        return {name: value for name, value in result.items() if name != key}

    return mutate


def _page_without(key):
    """A reader result whose first page is missing one key the projection reads."""

    def mutate(result):
        pages = [dict(page) for page in result["pages"]]
        pages[0].pop(key)
        return {**result, "pages": pages}

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_without("pages"), id="pages"),
        pytest.param(_without("version"), id="version"),
        pytest.param(_without("native_version"), id="native_version"),
        pytest.param(_page_without("tables"), id="page-tables"),
    ],
)
def test_an_incomplete_reader_result_refuses_as_availability_not_integrity(
    session, project, tmp_path, monkeypatch, mutate
):
    """A reader result this build cannot read is availability, not integrity.

    ``read_native_pdf`` sat inside the ``try`` whose ``except (KeyError,
    TypeError)`` said "native segment reader identity is incomplete", so a key
    the child-process payload did not carry was reported as the integrity
    verdict *Not found at cited location* — an assertion that a reader went to
    the cited location and the passage was not there, when no page was opened
    at all. Only the identity extraction from the stored configuration belongs
    in that ``try``; a result the projection cannot read is a reading this
    build cannot obtain.
    """

    from corridor_pdf_reader.execution import PdfiumExecutor

    path = native_pdf(tmp_path)
    document = registered(session, project, path)
    stored = append_native_segments(session, document, reading(path))
    segment = next(row for row in stored if row.kind == "pdf_cell")
    spans = [row for row in stored if row.kind == "pdf_span"]
    assert spans

    class IncompleteResult:
        def read_document(self, source, **kwargs):
            return mutate(PdfiumExecutor().read_document(source, **kwargs))

    monkeypatch.setattr("corridor.token_layers.PdfiumExecutor", IncompleteResult)

    with pytest.raises(NativeReaderUnavailable) as scalar:
        dereference_source_segment(document, segment, path)
    assert not isinstance(scalar.value, SourceSegmentIntegrityError)
    assert (
        source_segment_locator_validation_status(document, segment, path)
        == NOT_RE_READABLE
    )

    # The batch shape owes the same answer: it read the reading inside the
    # identity ``try`` too.
    from corridor.reader_segments import replay_native_segments

    with pytest.raises(NativeReaderUnavailable) as batch:
        replay_native_segments(document, spans, path)
    assert not isinstance(batch.value, SourceSegmentIntegrityError)
