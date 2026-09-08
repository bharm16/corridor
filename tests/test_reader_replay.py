"""Native PDF batches keep every replay proof while decoding each reading once."""

from dataclasses import asdict
from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extraction_runs import append_source_facts
from corridor.models import DocPage, Document, ExtractionRun, Fact, Project, SourceFactAppendReceipt, SourceSegment
from corridor.reader_replay import replay_native_segments
from corridor.reader_segments import native_segment_values
from corridor.source_segment_errors import (
    SourceDocumentDigestMismatch, SourceSegmentDigestMismatch, SourceSegmentLocatorMismatch,
)
from corridor.token_layers import read_native_pdf
from corridor_pdf_reader.execution import PdfiumExecutor
from pdf_fixture_support import PdfFixture


@pytest.fixture(scope="module")
def native_source(tmp_path_factory):
    fixture = PdfFixture()
    page = fixture.add_page(width=460, height=370, cropbox=(30, 20, 430, 310))
    for x in (20, 170, 320):
        page.line((x, 50), (x, 150))
    for y in (50, 100, 150):
        page.line((20, y), (320, y))
    for position, text in (
        ((30, 75), "Owner"), ((180, 75), "Station"),
        ((30, 125), "Exact Utilities"), ((180, 125), "1149+00"),
        ((340, 235), "OUT"),
    ):
        page.text(position, text)
    page._content.append("q 0 0 8 8 re W n\n")
    page.text((50, 260), "HIDDEN")
    page._content.append("Q\n")
    path = fixture.save(tmp_path_factory.mktemp("native-batch") / "source.pdf")
    reading = read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())
    return path, reading, native_segment_values(reading)


@pytest.fixture
def document(native_source):
    path, reading, _values = native_source
    return Document(
        id=101, project_id=202, sha256=reading.rendition_sha256,
        filename=path.name, doc_type="minutes", pages=1, parse_status="parsed",
    )


def _segments(document, values):
    return tuple(
        SourceSegment(project_id=document.project_id, document_id=document.id, **asdict(value))
        for value in values
    )


def _count_isolated_reads(monkeypatch):
    calls = []
    original = PdfiumExecutor.run

    def counted(executor, function, *args, **kwargs):
        calls.append(function.__name__)
        return original(executor, function, *args, **kwargs)

    monkeypatch.setattr(PdfiumExecutor, "run", counted)
    return calls


def test_native_batch_decodes_once_for_cells_visible_spans_and_clipped_spans(
    native_source, document, monkeypatch
):
    path, _reading, values = native_source
    segments = _segments(document, values)
    assert {segment.kind for segment in segments} == {"pdf_cell", "pdf_span"}
    assert {segment.span_stream for segment in segments if segment.kind == "pdf_span"} == {
        "page", "clipped"
    }
    calls = _count_isolated_reads(monkeypatch)
    assert replay_native_segments(document, segments, path) == tuple(
        segment.exact_text for segment in segments
    )
    assert calls == ["read_document"]


def test_native_batch_reuses_an_already_authenticated_reading(native_source, document, monkeypatch):
    path, reading, values = native_source
    calls = _count_isolated_reads(monkeypatch)
    assert replay_native_segments(
        document, _segments(document, values), path, native_reading=reading
    ) == tuple(value.exact_text for value in values)
    assert calls == []


@pytest.mark.parametrize("changed_field", [
    "reading_sha256", "reader_identity", "table_index", "location_json",
    "ordinal", "content_sha256", "exact_text", "project_id", "document_id",
])
def test_native_batch_refuses_every_changed_locator_field(native_source, document, changed_field):
    path, reading, values = native_source
    cell = next(value for value in values if value.kind == "pdf_cell")
    changes = {
        "reading_sha256": "f" * 64,
        "reader_identity": {**cell.reader_identity, "integration_sha256": "f" * 64},
        "table_index": 99,
        "location_json": {**cell.location_json, "table_box": [0, 0, 1, 1]},
        "ordinal": 99,
        "content_sha256": "f" * 64,
        "exact_text": "altered",
        "project_id": 999,
        "document_id": 999,
    }
    changed = _segments(document, (cell,))[0]
    setattr(changed, changed_field, changes[changed_field])
    if changed_field == "exact_text":
        changed.content_sha256 = sha256(changed.exact_text.encode()).hexdigest()
    refusal = SourceSegmentDigestMismatch if changed_field == "content_sha256" else SourceSegmentLocatorMismatch
    with pytest.raises(refusal):
        replay_native_segments(
            document, (*_segments(document, values), changed), path, native_reading=reading
        )


def test_native_batch_preserves_distinct_historical_configurations(native_source, document, monkeypatch):
    path, first, first_values = native_source
    second = read_native_pdf(path, source_sha256=document.sha256, dpi=37)
    batch = (*_segments(document, first_values), *_segments(document, native_segment_values(second)))
    calls = _count_isolated_reads(monkeypatch)
    assert replay_native_segments(document, batch, path) == tuple(segment.exact_text for segment in batch)
    assert calls == ["read_document", "read_document"]
    with pytest.raises(SourceSegmentLocatorMismatch, match="configuration/result"):
        replay_native_segments(document, batch, path, native_reading=first)


@pytest.mark.parametrize("mutation", ["source", "document"])
def test_native_batch_refuses_a_mutated_snapshot_before_returning_any_results(
    native_source, document, tmp_path, mutation
):
    source, reading, values = native_source
    path = tmp_path / "source.pdf"
    raw = source.read_bytes()
    path.write_bytes(raw)

    def changing_batch():
        yield from _segments(document, values)
        if mutation == "source":
            path.write_bytes(raw + b"\n% changed during replay")
        else:
            document.project_id += 1

    refusal = SourceDocumentDigestMismatch if mutation == "source" else SourceSegmentLocatorMismatch
    with pytest.raises(refusal):
        replay_native_segments(document, changing_batch(), path, native_reading=reading)


def test_native_batch_rechecks_original_bytes_and_refuses_unsealed_readings(
    native_source, document, tmp_path
):
    source, reading, values = native_source
    path = tmp_path / "source.pdf"
    path.write_bytes(source.read_bytes()[:-10])
    with pytest.raises(SourceDocumentDigestMismatch):
        replay_native_segments(document, _segments(document, values), path, native_reading=reading)
    with pytest.raises(SourceSegmentLocatorMismatch, match="sealed reading"):
        replay_native_segments(document, (), source, native_reading=object())


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


def _registered(session, path, reading):
    project = Project(slug="native-append-batch", name="Native append batch", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(project_id=project.id, sha256=reading.rendition_sha256,
                        filename=path.name, doc_type="minutes", pages=1, parse_status="parsed")
    session.add(document)
    session.flush()
    return document


def _append(session, document, path, **kwargs):
    return append_source_facts(
        session, document, idempotency_key="native-batch", prompt_version="native-batch-test",
        candidate_count=0, page_errors=0, candidates=(), source_path=path,
        allow_unsealed_legacy=True, **kwargs,
    )


@pytest.mark.parametrize("provided_reading", [False, True])
def test_source_fact_append_reuses_its_native_reading_through_validation(
    native_source, session, monkeypatch, provided_reading
):
    path, reading, values = native_source
    document = _registered(session, path, reading)
    calls = _count_isolated_reads(monkeypatch)
    result = _append(session, document, path, native_reading=reading if provided_reading else None)
    assert result.created and result.run.id is not None
    stored = session.scalars(select(SourceSegment).where(SourceSegment.document_id == document.id)).all()
    assert len(stored) == len(values)
    assert {segment.reading_sha256 for segment in stored} == {reading.reading_sha256}
    assert calls == ([] if provided_reading else ["read_document"])


def test_source_fact_append_rolls_back_native_segments_when_bytes_change(
    native_source, session, tmp_path, monkeypatch
):
    from corridor import reader_replay

    source, reading, _values = native_source
    path = tmp_path / "source.pdf"
    raw = source.read_bytes()
    path.write_bytes(raw)
    document = _registered(session, path, reading)
    original_index = reader_replay.native_replay_index

    def changing_source(native):
        result = original_index(native)
        path.write_bytes(raw + b"\n% changed during validation")
        return result

    monkeypatch.setattr(reader_replay, "native_replay_index", changing_source)
    with pytest.raises(SourceDocumentDigestMismatch):
        _append(session, document, path, native_reading=reading)
    for model in (SourceSegment, ExtractionRun, Fact, SourceFactAppendReceipt):
        scope = model.document_id == document.id if model is not SourceFactAppendReceipt else model.project_id == document.project_id
        assert session.scalars(select(model).where(scope)).all() == []


def test_native_append_with_no_segments_refuses_without_reading_a_workbook(
    session, tmp_path, monkeypatch
):
    fixture = PdfFixture()
    fixture.add_page()
    path = fixture.save(tmp_path / "empty.pdf")
    reading = read_native_pdf(path, source_sha256=sha256(path.read_bytes()).hexdigest())
    document = _registered(session, path, reading)
    monkeypatch.setattr(
        "corridor.source_segments.load_workbook",
        lambda *args, **kwargs: pytest.fail("an empty PDF is not a workbook"),
    )
    with pytest.raises(ValueError, match="requires rendition segments"):
        _append(session, document, path, native_reading=reading)
    assert session.scalars(select(SourceSegment).where(SourceSegment.document_id == document.id)).all() == []
    assert session.scalars(select(ExtractionRun).where(ExtractionRun.document_id == document.id)).all() == []


def test_native_ingest_shares_one_reading_with_its_inventory(
    native_source, session, tmp_path, monkeypatch
):
    from corridor.ingest import ingest_document
    from corridor.page_inventory import read_reader_page_inventories, route_reader_page

    path, reading, values = native_source
    expected_inventory = read_reader_page_inventories(path)[1]
    project = Project(slug="native-ingest-paired", name="Paired native ingest", is_synthetic=True)
    session.add(project)
    session.flush()
    calls = _count_isolated_reads(monkeypatch)
    document = ingest_document(
        session, project_id=project.id, path=path, doc_type="minutes",
        images_dir=tmp_path / "images",
    )
    assert document.parse_status == "parsed"
    stored = session.scalars(select(SourceSegment).where(SourceSegment.document_id == document.id)).all()
    assert len(stored) == len(values)
    assert {segment.reading_sha256 for segment in stored} == {reading.reading_sha256}
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    assert page.text == reading.page_text(1).rstrip() + "\n"
    assert page.inventory_json == expected_inventory.model_dump(mode="json")
    assert page.routing_json == route_reader_page(expected_inventory).model_dump(mode="json")
    assert calls == ["read_page_facts"]


@pytest.mark.parametrize("engine,dpi", [("pdfium", 36), ("tagged", 37)])
def test_paired_inventory_refuses_an_unmeasured_native_configuration(engine, dpi):
    from corridor.ingest import _NativeInventoryRead

    with pytest.raises(ValueError, match="measured native reader configuration"):
        _NativeInventoryRead().read_document("not-opened.pdf", engine=engine, dpi=dpi)
