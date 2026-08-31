"""The PDF gold contract and experiment runner are independent of extraction.

These tests exercise the public JSON contract, evaluator, and CLI.  They do not
reach into a table engine: Stage 0 must be frozen before any challenger engine
can shape the denominator it will later be measured against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from corridor.pdf_evaluation import (
    CellGold,
    CellPrediction,
    DocumentGold,
    DocumentPrediction,
    EngineRun,
    GoldSet,
    PageGold,
    PagePrediction,
    Point,
    Polygon,
    ProposalCaseGold,
    ProposalPrediction,
    Split,
    TableGold,
    TablePrediction,
    evaluate,
    load_gold_set,
)
from corridor.pdf_evaluation_cli import main


def box(x0: int, y0: int, x1: int, y1: int) -> Polygon:
    return Polygon(
        points=(
            Point(x=x0, y=y0),
            Point(x=x1, y=y0),
            Point(x=x1, y=y1),
            Point(x=x0, y=y1),
        )
    )


def document(
    digest: str,
    family: str,
    split: Split,
    *,
    page_class: str = "matrix",
    state: str = "confirmed",
) -> DocumentGold:
    cell = CellGold(
        cell_id="cell-1",
        row_id="row-1",
        column_id="column-1",
        polygon=box(10, 10, 90, 30),
        visible_text="1149+00",
        canonical_mapping="station_begin",
        state=state,
    )
    return DocumentGold(
        document_sha256=digest,
        document_family=family,
        split=split,
        source_title=family,
        pages=(
            PageGold(
                page_number=1,
                page_class=page_class,
                width_points=612_000,
                height_points=792_000,
                features=("digital_text",),
                tables=(
                    TableGold(
                        table_id="table-1",
                        polygon=box(0, 0, 100, 100),
                        row_ids=("row-1",),
                        column_ids=("column-1",),
                        row_polygons=(box(0, 0, 100, 50),),
                        column_polygons=(box(0, 0, 100, 100),),
                        row_dispositions={"row-1": "active"},
                        cells=(cell,),
                    ),
                ),
                proposal_cases=(
                    ProposalCaseGold(
                        case_id="proposal-1",
                        expected_value="1149+00",
                        allowed_source_cell_ids=("cell-1",),
                        must_abstain=state != "confirmed",
                    ),
                ),
            ),
        ),
    )


def gold_set(*documents: DocumentGold) -> GoldSet:
    return GoldSet(
        schema_version="corridor.pdf-gold.v1",
        dataset_version="2026-08-31.1",
        frozen_at="2026-08-31T00:00:00Z",
        frozen_by="gold-author@example.test",
        checked_by="gold-checker@example.test",
        documents=documents,
    )


def prediction(
    gold: DocumentGold,
    *,
    table_polygon: Polygon | None = None,
    cell_text: str = "1149+00",
    proposal: ProposalPrediction | None = None,
) -> DocumentPrediction:
    page = gold.pages[0]
    return DocumentPrediction(
        document_sha256=gold.document_sha256,
        latency_ms=125,
        peak_memory_bytes=4_096,
        pages=(
            PagePrediction(
                page_number=1,
                page_class=page.page_class,
                abstained=False,
                tables=(
                    TablePrediction(
                        table_id="predicted-table",
                        polygon=table_polygon or page.tables[0].polygon,
                        row_ids=("predicted-row",),
                        column_ids=("predicted-column",),
                        row_polygons=(box(0, 0, 100, 50),),
                        column_polygons=(box(0, 0, 100, 100),),
                        row_dispositions={"predicted-row": "active"},
                        cells=(
                            CellPrediction(
                                cell_id="predicted-cell",
                                row_id="predicted-row",
                                column_id="predicted-column",
                                polygon=page.tables[0].cells[0].polygon,
                                visible_text=cell_text,
                                canonical_mapping="station_begin",
                            ),
                        ),
                    ),
                ),
                proposals=(
                    proposal
                    or ProposalPrediction(
                        case_id="proposal-1",
                        value="1149+00",
                        source_cell_id="predicted-cell",
                    ),
                ),
            ),
        ),
    )


def engine_run(*documents: DocumentPrediction) -> EngineRun:
    return EngineRun(
        schema_version="corridor.pdf-engine-run.v1",
        engine="fixture-engine",
        engine_version="1.0",
        configuration_sha256="c" * 64,
        documents=documents,
    )


def test_geometry_is_fixed_integer_pdf_points_and_rejects_pixels():
    with pytest.raises(ValidationError, match="valid integer"):
        Point(x=1.25, y=2)

    with pytest.raises(ValidationError, match="non-zero area"):
        Polygon(points=(Point(x=0, y=0), Point(x=1, y=1), Point(x=2, y=2)))


def test_a_document_family_cannot_leak_across_splits():
    with pytest.raises(ValidationError, match="document family.*more than one split"):
        gold_set(
            document("a" * 64, "same-template", Split.development),
            document("b" * 64, "same-template", Split.holdout),
        )


def test_the_checked_in_gold_set_covers_every_frozen_layout_class():
    gold = load_gold_set(Path("gold/pdf/v1/dataset.json"))

    assert {document.split for document in gold.documents} == set(Split)
    assert all(document.author and document.checker for document in gold.documents)
    features = {
        feature
        for document in gold.documents
        for page in document.pages
        for feature in page.features
    }
    assert {
        "digital_text",
        "rotated_page",
        "continuation_page",
        "repeated_header",
        "group_band",
        "merged_header",
        "multiline_header",
        "retired_row",
        "blank_row",
        "negative_page",
        "page_scoped_owner",
        "unfamiliar_headers",
        "marked_resolution",
    } <= features
    assert gold.adjudications, "a representative double-labelled subset is required"


def test_assignment_matching_reports_each_layer_and_does_not_match_by_order():
    gold = gold_set(document("a" * 64, "project-a", Split.development))
    page = gold.documents[0].pages[0]
    predicted = prediction(
        gold.documents[0],
        table_polygon=box(1, 1, 99, 99),
    )
    result = evaluate(gold, engine_run(predicted))

    assert result.overall.page_classification.accuracy == 1
    assert result.overall.tables.true_positive == 1
    assert result.overall.cells.true_positive == 1
    assert result.overall.cell_text_exact.accuracy == 1
    assert result.overall.row_disposition_exact.accuracy == 1
    assert result.overall.cell_span_exact.accuracy == 1
    assert result.overall.cell_topology_exact.accuracy == 1
    assert result.overall.header_relationships_exact.accuracy == 1
    assert result.overall.canonical_mapping_exact.accuracy == 1
    assert result.overall.cell_state_exact.accuracy == 1
    assert result.overall.page_scoped_values.f1 == 1
    assert result.by_document[gold.documents[0].document_sha256].pages == 1
    assert result.by_page_class[page.page_class].pages == 1


def test_wrong_source_cited_proposals_are_counted_not_hidden_in_vendor_scores():
    readable = document("a" * 64, "project-a", Split.development)
    unreadable = document(
        "b" * 64,
        "hard-pages",
        Split.regression,
        state="unreadable",
    )
    wrong = ProposalPrediction(
        case_id="proposal-1",
        value="invented",
        source_cell_id="predicted-cell",
    )
    abstained = ProposalPrediction(case_id="proposal-1", abstained=True)

    result = evaluate(
        gold_set(readable, unreadable),
        engine_run(
            prediction(readable, proposal=wrong),
            prediction(unreadable, proposal=abstained),
        ),
    )

    assert result.key_metric.name == "wrong_source_cited_proposals_avoided"
    assert result.key_metric.opportunities == 2
    assert result.key_metric.avoided == 1
    assert result.key_metric.emitted == 1
    assert result.key_metric.rate == 0.5


def test_semantic_layers_have_independent_scores_and_frozen_ceilings():
    gold_document = document("a" * 64, "project-a", Split.development)
    gold_document = gold_document.model_copy(
        update={
            "pages": (
                gold_document.pages[0].model_copy(
                    update={"page_scoped_values": {"external_org": "Owner A"}}
                ),
            )
        }
    )
    run_document = prediction(gold_document)
    predicted_table = run_document.pages[0].tables[0]
    wrong_table = predicted_table.model_copy(
        update={
            "row_dispositions": {"predicted-row": "retired"},
            "cells": (
                predicted_table.cells[0].model_copy(
                    update={
                        "row_span": 2,
                        "header_cell_ids": ("predicted-cell",),
                        "canonical_mapping": "station_end",
                        "state": "unconfirmed",
                    }
                ),
            ),
        }
    )
    run_document = run_document.model_copy(
        update={
            "pages": (
                run_document.pages[0].model_copy(update={"tables": (wrong_table,)}),
            )
        }
    )
    run_document = run_document.model_copy(
        update={
            "pages": (
                run_document.pages[0].model_copy(
                    update={"page_scoped_values": {"external_org": "Owner B"}}
                ),
            )
        }
    )

    result = evaluate(gold_set(gold_document), engine_run(run_document))

    assert result.overall.row_disposition_exact.accuracy == 0
    assert result.overall.cell_span_exact.accuracy == 0
    assert result.overall.header_relationships_exact.accuracy == 0
    assert result.overall.canonical_mapping_exact.accuracy == 0
    assert result.overall.cell_state_exact.accuracy == 0
    assert result.overall.page_scoped_values.f1 == 0
    assert result.thresholds_met["row_disposition_exact"] is False
    assert result.thresholds_met["cell_span_exact"] is False
    assert result.thresholds_met["header_relationships_exact"] is False
    assert result.thresholds_met["canonical_mapping_exact"] is False
    assert result.thresholds_met["cell_state_exact"] is False
    assert result.thresholds_met["page_scoped_values"] is False
    assert result.passed is False


def test_extra_predicted_pages_and_their_tables_are_false_positives():
    gold_document = document("a" * 64, "project-a", Split.development)
    run_document = prediction(gold_document)
    extra_page = PagePrediction(
        page_number=99,
        page_class="matrix",
        abstained=False,
        tables=(
            TablePrediction(
                table_id="phantom-table",
                polygon=box(0, 0, 100, 100),
            ),
        ),
    )
    run_document = run_document.model_copy(
        update={"pages": (*run_document.pages, extra_page)}
    )

    result = evaluate(gold_set(gold_document), engine_run(run_document))

    assert result.overall.page_coverage.false_positive == 1
    assert result.overall.tables.false_positive == 1
    assert result.overall.page_classification.accuracy == 0.5
    assert result.thresholds_met["page_coverage"] is False
    assert result.passed is False


def test_cli_requires_and_records_holdout_access(tmp_path, capsys):
    held_out = document("d" * 64, "held-out-template", Split.holdout)
    gold = gold_set(held_out)
    run = engine_run(prediction(held_out))
    gold_path = tmp_path / "gold.json"
    run_path = tmp_path / "run.json"
    output_json = tmp_path / "metrics.json"
    output_markdown = tmp_path / "metrics.md"
    access_log = tmp_path / "holdout-access.jsonl"
    gold_path.write_text(gold.model_dump_json(indent=2))
    run_path.write_text(run.model_dump_json(indent=2))

    assert main([
        "evaluate",
        "--gold", str(gold_path),
        "--predictions", str(run_path),
        "--output-json", str(output_json),
        "--output-report", str(output_markdown),
    ]) == 2
    assert "holdout" in capsys.readouterr().err.lower()

    assert main([
        "evaluate",
        "--gold", str(gold_path),
        "--predictions", str(run_path),
        "--output-json", str(output_json),
        "--output-report", str(output_markdown),
        "--holdout-access-log", str(access_log),
        "--holdout-actor", "checker@example.test",
        "--holdout-reason", "one predeclared release measurement",
    ]) == 0

    machine = json.loads(output_json.read_text())
    [access] = [json.loads(line) for line in access_log.read_text().splitlines()]
    assert machine["key_metric"]["name"] == "wrong_source_cited_proposals_avoided"
    assert machine["key_metric"]["rate"] == 1
    assert machine["overall"]["tables"]["f1"] == 1
    assert machine["overall"]["failure_rate"] == 0
    assert machine["overall"]["abstention_rate"] == 0
    assert "per page class" in output_markdown.read_text().lower()
    assert "Peak memory bytes" in output_markdown.read_text()
    assert access["dataset_version"] == gold.dataset_version
    assert access["document_sha256s"] == [held_out.document_sha256]
    assert access["actor"] == "checker@example.test"
    assert access["reason"] == "one predeclared release measurement"
