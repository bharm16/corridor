"""Stage 1 page-routing metrics compare inventory routing with the retired rule."""

from __future__ import annotations

import json
from pathlib import Path

from corridor.page_inventory_evaluation import (
    RoutingCase,
    RoutingGoldSet,
    RoutingObservation,
    RoutingRun,
    evaluate_stage1,
    main,
)
from corridor.pdf_evaluation import load_gold_set


def labels() -> RoutingGoldSet:
    return RoutingGoldSet(
        schema_version="corridor.pdf-stage1-gold.v1",
        dataset_version="2026-08-31.1",
        pdf_gold_dataset_version="2026-08-31.1",
        author="labeler",
        checker="checker",
        cases=(
            RoutingCase(
                document_sha256="a" * 64,
                page_number=1,
                page_class="short-native",
                expected_ocr_needed=False,
            ),
            RoutingCase(
                document_sha256="b" * 64,
                page_number=1,
                page_class="image-only",
                expected_ocr_needed=True,
            ),
            RoutingCase(
                document_sha256="c" * 64,
                page_number=1,
                page_class="mixed",
                expected_ocr_needed=True,
            ),
        ),
    )


def run() -> RoutingRun:
    return RoutingRun(
        schema_version="corridor.pdf-stage1-run.v1",
        router_version="page-inventory-router-v1",
        observations=(
            RoutingObservation(
                document_sha256="a" * 64,
                page_number=1,
                page_mode="native",
                native_text_length=2,
            ),
            # The retired character rule misses this image-backed page because
            # corrupt native extraction happened to produce 80 characters.
            RoutingObservation(
                document_sha256="b" * 64,
                page_number=1,
                page_mode="ocr",
                native_text_length=80,
            ),
            RoutingObservation(
                document_sha256="c" * 64,
                page_number=1,
                page_mode="both",
                native_text_length=120,
            ),
        ),
    )


def test_stage1_records_confusion_and_the_two_routing_error_rates():
    report = evaluate_stage1(labels(), run())

    assert report.inventory_router.confusion.true_positive == 2
    assert report.inventory_router.confusion.true_negative == 1
    assert report.inventory_router.false_ocr_not_needed_rate == 0
    assert report.inventory_router.unnecessary_ocr_rate == 0
    assert report.retired_character_rule.confusion.false_positive == 1
    assert report.retired_character_rule.confusion.false_negative == 2
    assert report.retired_character_rule.false_ocr_not_needed_rate == 1
    assert report.retired_character_rule.unnecessary_ocr_rate == 1
    assert set(report.by_page_class) == {"short-native", "image-only", "mixed"}


def test_stage1_cli_writes_a_reproducible_json_receipt(tmp_path):
    gold_path = tmp_path / "gold.json"
    run_path = tmp_path / "run.json"
    output = tmp_path / "stage1.json"
    gold_path.write_text(labels().model_dump_json(indent=2))
    run_path.write_text(run().model_dump_json(indent=2))

    assert main([
        "--gold", str(gold_path),
        "--run", str(run_path),
        "--output", str(output),
    ]) == 0

    receipt = json.loads(output.read_text())
    assert receipt["schema_version"] == "corridor.pdf-stage1-evaluation.v1"
    assert receipt["inventory_router"]["false_ocr_not_needed_rate"] == 0
    assert receipt["retired_character_rule"]["unnecessary_ocr_rate"] == 1


def test_checked_in_stage1_receipt_is_bound_to_pdf_gold_and_reproducible():
    root = Path("gold/pdf/v1")
    stage1_gold = RoutingGoldSet.model_validate_json(
        (root / "stage1-routing-gold.json").read_text()
    )
    stage1_run = RoutingRun.model_validate_json(
        (root / "stage1-routing-run.json").read_text()
    )
    recorded = json.loads((root / "stage1-routing-evaluation.json").read_text())
    pdf_gold = load_gold_set(root / "dataset.json")
    pdf_pages = {
        (document.document_sha256, page.page_number)
        for document in pdf_gold.documents
        for page in document.pages
    }

    assert {
        (case.document_sha256, case.page_number) for case in stage1_gold.cases
    } <= pdf_pages
    assert stage1_gold.pdf_gold_dataset_version == pdf_gold.dataset_version
    assert evaluate_stage1(stage1_gold, stage1_run).model_dump(mode="json") == recorded
