"""The adapter from the reader's page to the PDF evaluation contract invents nothing (#731).

On the package's own fixture PDFs, read in this process, the adapter's
`EngineRun` validates under `corridor.pdf_evaluation`, carries the reader's
geometry in thousandths of a PDF point in the frame the gold page declares
(scaled, and the scale recorded, when the declared page differs from the
crop box), addresses cells by the reader's own IDs, and marks every layer the
frozen reader does not produce as absent: the sentinel page class, no
proposals, no page-scoped values, no header relationships, no canonical
mapping. The evaluation itself runs to a report, and the applicability of
each ceiling is fixed before any result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corridor.pdf_evaluation import (
    DocumentGold,
    GoldSet,
    PageGold,
    Point,
    Polygon,
    Split,
    TableGold,
    evaluate,
    load_engine_run,
)
from corridor_pdf_reader import gold_evaluation
from corridor_pdf_reader.execution import read_document
from corridor_pdf_reader.replacement.pages import slim_page

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "corridor_pdf_reader" / "corpus" / "fixtures"


def _box(x0: int, y0: int, x1: int, y1: int) -> Polygon:
    return Polygon(points=(Point(x=x0, y=y0), Point(x=x1, y=y0), Point(x=x1, y=y1), Point(x=x0, y=y1)))


def _reading(name: str) -> dict:
    document = read_document(FIXTURES / name, [1], engine="tagged", dpi=72)
    return {
        "source_sha256": document["source_sha256"],
        "page_count": document["page_count"],
        "pages": [{"number": page["number"], "geometry": page["geometry"], "slim": slim_page(page)} for page in document["pages"]],
    }


def _gold_page(width: int, height: int, *, rotation: int = 0, table: Polygon | None = None) -> PageGold:
    tables = ()
    if table is not None:
        tables = (
            TableGold(
                table_id="gold-table",
                polygon=table,
                row_ids=("r0",),
                column_ids=("c0",),
                row_polygons=(table,),
                column_polygons=(table,),
                row_dispositions={"r0": "active"},
                cells=(),
            ),
        )
    return PageGold(page_number=1, page_class="matrix-body", width_points=width, height_points=height, rotation_degrees=rotation, features=("digital_text",), tables=tables)


def _gold(reading: dict, page: PageGold, split: Split = Split.development) -> GoldSet:
    return GoldSet(
        schema_version="corridor.pdf-gold.v1",
        dataset_version="test.1",
        frozen_at="2026-09-06T00:00:00Z",
        frozen_by="test",
        checked_by="test",
        documents=(DocumentGold(document_sha256=reading["source_sha256"], document_family="fixture", split=split, source_title="fixture", pages=(page,)),),
    )


def test_the_reader_geometry_is_carried_in_thousandths_in_the_declared_frame():
    reading = _reading("fixture-rotation-0.pdf")
    geometry = reading["pages"][0]["geometry"]
    slim = reading["pages"][0]["slim"]
    page = _gold_page(300_000, 200_000)

    prediction, notes = gold_evaluation.page_prediction(slim, geometry, page)

    assert prediction.page_number == 1
    assert prediction.page_class == gold_evaluation.UNCLASSIFIED
    assert prediction.abstained is False
    assert prediction.page_scoped_values == {} and prediction.proposals == ()
    assert len(prediction.tables) == 1
    table = prediction.tables[0]
    box = slim["tables"][0]["box"]
    assert [point.x for point in table.polygon.points] == [round(box[0] * 1000), round(box[2] * 1000), round(box[2] * 1000), round(box[0] * 1000)]
    assert [point.y for point in table.polygon.points] == [round(box[1] * 1000), round(box[1] * 1000), round(box[3] * 1000), round(box[3] * 1000)]
    assert {cell.cell_id: cell.visible_text for cell in table.cells} == {
        "t0r0c1": "UTILITY 1149+00", "t0r1c1": "Owner", "t0r1c2": "Status", "t0r2c1": "Gas", "t0r2c2": "Open",
    }
    assert all(cell.header_cell_ids == () and cell.canonical_mapping is None and cell.state == "confirmed" for cell in table.cells)
    assert set(table.row_dispositions.values()) == {gold_evaluation.ROW_DISPOSITION_PLACEHOLDER}
    assert table.row_ids == ("t0r0", "t0r1", "t0r2") and table.column_ids == ("t0c1", "t0c2")
    owner = next(cell for cell in table.cells if cell.cell_id == "t0r1c1")
    assert owner.polygon.points[0].x == round(slim["tables"][0]["cells"][1]["box"][0] * 1000)
    assert notes["scaled"] is False and notes["scale"] == [1.0, 1.0]
    assert notes["cells"] == 5 and notes["boxes_clamped_to_page"] == 0 and notes["cells_dropped_as_degenerate"] == 0


def test_a_declared_page_that_differs_from_the_crop_box_is_scaled_and_the_scale_recorded():
    reading = _reading("fixture-rotation-0.pdf")
    geometry = reading["pages"][0]["geometry"]
    slim = reading["pages"][0]["slim"]
    page = _gold_page(600_000, 100_000)

    prediction, notes = gold_evaluation.page_prediction(slim, geometry, page)

    box = slim["tables"][0]["box"]
    polygon = prediction.tables[0].polygon
    assert polygon.points[0].x == round(box[0] * 1000 * 2) and polygon.points[2].x == round(box[2] * 1000 * 2)
    assert polygon.points[0].y == round(box[1] * 1000 * 0.5) and polygon.points[2].y == round(box[3] * 1000 * 0.5)
    assert notes["scaled"] is True and notes["scale"] == [2.0, 0.5]
    assert notes["reader_displayed_points"] == [300.0, 200.0] and notes["gold_declared_points"] == [600.0, 100.0]
    assert all(0 <= point.x <= 600_000 and 0 <= point.y <= 100_000 for cell in prediction.tables[0].cells for point in cell.polygon.points)


def test_a_rotated_page_is_expressed_in_its_displayed_frame():
    reading = _reading("fixture-rotation-90.pdf")
    geometry = reading["pages"][0]["geometry"]
    slim = reading["pages"][0]["slim"]
    assert geometry["rotation"] == 90 and (geometry["width"], geometry["height"]) == (300, 200)

    prediction, notes = gold_evaluation.page_prediction(slim, geometry, _gold_page(200_000, 300_000, rotation=90))

    assert notes["reader_displayed_points"] == [200.0, 300.0]
    assert notes["scaled"] is False
    assert all(0 <= point.x <= 200_000 and 0 <= point.y <= 300_000 for point in prediction.tables[0].polygon.points)
    assert {cell.visible_text for cell in prediction.tables[0].cells} >= {"Owner", "Status", "Gas", "Open"}


def test_the_engine_run_validates_and_evaluates_with_the_reader_table_matched(tmp_path):
    reading = _reading("fixture-rotation-0.pdf")
    slim = reading["pages"][0]["slim"]
    box = slim["tables"][0]["box"]
    table = _box(round(box[0] * 1000), round(box[1] * 1000), round(box[2] * 1000), round(box[3] * 1000))
    gold = _gold(reading, _gold_page(300_000, 200_000, table=table))
    identity = gold_evaluation.configuration_identity("tagged", 72)

    prediction, notes = gold_evaluation.document_prediction(gold.documents[0], reading, latency_ms=12, peak_memory_bytes=1024)
    run = gold_evaluation.engine_run(gold, [prediction], identity)
    path = tmp_path / "engine-run.json"
    path.write_text(run.model_dump_json())
    loaded = load_engine_run(path)

    assert loaded.configuration_sha256 == gold_evaluation.configuration_sha256(identity)
    assert loaded.engine == "corridor_pdf_reader frozen-reader"
    assert "c39363e" in loaded.engine_version and "PDFium" in loaded.engine_version
    assert identity["absent"]["page_class"] == gold_evaluation.UNCLASSIFIED
    report = evaluate(gold, loaded)
    assert report.overall.page_coverage.f1 == 1.0
    assert report.overall.tables.true_positive == 1
    assert report.overall.page_classification.accuracy == 0.0
    assert report.key_metric.opportunities == 0 and report.key_metric.emitted == 0
    assert report.overall.failure_rate == 0.0 and report.overall.abstention_rate == 0.0
    assert report.overall.latency_ms == 12 and report.overall.peak_memory_bytes == 1024

    record = gold_evaluation.thresholds_record({"thresholds_met": report.thresholds_met, "passed": report.passed})
    assert set(record["thresholds"]) == set(report.thresholds_met) == set(gold_evaluation.THRESHOLD_APPLICABILITY)
    assert record["thresholds"]["page_class_accuracy"] == {"met": False, "applies": False, "why": gold_evaluation.THRESHOLD_APPLICABILITY["page_class_accuracy"][1]}
    assert record["thresholds"]["row_disposition_exact"]["applies"] is False
    assert record["thresholds"]["table_f1"]["applies"] is True


def test_a_document_the_reader_failed_on_is_a_failed_document_with_its_reason():
    reading = _reading("fixture-rotation-0.pdf")
    gold = _gold(reading, _gold_page(300_000, 200_000))

    prediction, notes = gold_evaluation.document_prediction(gold.documents[0], None, latency_ms=3, peak_memory_bytes=0, failure="PdfiumProcessDied: exit code -9")

    assert prediction.failed is True and prediction.failure_reason == "PdfiumProcessDied: exit code -9"
    assert prediction.pages == () and notes == []


def test_the_holdout_family_is_refused_without_an_actor_and_a_reason(tmp_path, capsys):
    assert gold_evaluation.main(["--output", str(tmp_path / "run"), "--include-holdout"]) == 2
    assert "ADR-0008" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


def test_an_unclassified_ceiling_is_refused_rather_than_silently_applied():
    with pytest.raises(ValueError, match="not classified"):
        gold_evaluation.thresholds_record({"passed": True, "thresholds_met": {"table_f1": True, "novel_layer": True}})
