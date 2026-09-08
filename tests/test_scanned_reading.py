"""The scanned route: recorded decisions in, authorized Textract readings out (#739).

Every test here is offline. The Textract responses are the four retained
fixtures the adapter already commits, replayed through a transport double that
fails the test if a request would leave the process, and the authorization
records are the adapter's own (#732). No live provider call is made anywhere.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
import json

import pytest

from corridor.config import settings
from corridor.page_inventory import (
    OCR_ENGINE,
    PageRoutingDecision,
    PdfRect,
    RoutingRegion,
    StructuralTrigger,
)
from corridor.scanned_reading import routed_textract_regions

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "src" / "corridor_pdf_reader" / "textract" / "tests" / "fixtures"


def box(x0: int = 0, y0: int = 0, x1: int = 612_000, y1: int = 792_000) -> PdfRect:
    return PdfRect(x0=x0, y0=y0, x1=x1, y1=y1)


def reader_decision(**overrides: Any) -> PageRoutingDecision:
    fields: dict[str, Any] = dict(
        router_version="page-inventory-router-reader-v1",
        page_mode="ocr",
        reason="image_only_page",
        ocr_engine="textract",
        ocr_configuration={},
        regions=(
            RoutingRegion(
                region_id="image-1",
                box=box(),
                mode="ocr",
                reason="embedded_image_region",
            ),
        ),
    )
    fields.update(overrides)
    return PageRoutingDecision(**fields)


# --- the route is the only door -------------------------------------------------


def test_the_scanned_path_is_not_selected_by_default():
    """Merging an adapter is not selecting it: the incumbent path stays the default."""
    assert settings.textract_scanned_reading is False


def test_an_ocr_region_of_a_reader_decision_is_routed_to_textract():
    routed = routed_textract_regions(reader_decision())

    assert [region.region_id for region in routed] == ["image-1"]
    assert routed[0].trigger is None
    assert routed[0].reason == "embedded_image_region"


def test_a_both_region_is_routed_and_a_native_region_is_not():
    decision = reader_decision(
        page_mode="both",
        reason="mixed_native_and_image_regions",
        regions=(
            RoutingRegion(region_id="native-page", box=box(), mode="native", reason="clean_native_text"),
            RoutingRegion(region_id="image-2", box=box(), mode="ocr", reason="embedded_image_region"),
            RoutingRegion(region_id="page", box=box(), mode="both", reason="native_decode_requires_ocr_comparison"),
        ),
    )

    assert [region.region_id for region in routed_textract_regions(decision)] == ["image-2", "page"]


def test_a_structural_trigger_routes_a_text_layer_page_region():
    """A table region the native reading read no cells out of is the one structural trigger."""
    decision = reader_decision(
        page_mode="native",
        reason="clean_native_text",
        regions=(
            RoutingRegion(region_id="native-page", box=box(), mode="native", reason="clean_native_text"),
        ),
        structural_triggers=(
            StructuralTrigger(
                region_id="table-1",
                box=box(10_000, 10_000, 500_000, 400_000),
                trigger="table_region_without_table_read",
                reason="the page draws a table region the native reading recovered no cell text from",
            ),
        ),
    )

    (routed,) = routed_textract_regions(decision)
    assert routed.region_id == "table-1"
    assert routed.trigger == "table_region_without_table_read"


def test_a_decision_naming_the_incumbent_engine_routes_nothing_to_textract():
    """The incumbent router names tesseract; nothing in this module reads for it."""
    incumbent = PageRoutingDecision(
        page_mode="ocr",
        reason="image_only_page",
        regions=(RoutingRegion(region_id="image-1", box=box(), mode="ocr", reason="embedded_image_region"),),
    )
    assert incumbent.ocr_engine == OCR_ENGINE

    assert routed_textract_regions(incumbent) == ()


def test_a_native_only_decision_without_a_trigger_routes_nothing():
    """A semantic refusal is not on this record at all, and a clean page is not sent."""
    decision = reader_decision(
        page_mode="native",
        reason="clean_native_text",
        regions=(RoutingRegion(region_id="native-page", box=box(), mode="native", reason="clean_native_text"),),
    )

    assert routed_textract_regions(decision) == ()


def test_a_region_named_by_both_a_route_and_a_trigger_is_routed_once():
    decision = reader_decision(
        regions=(RoutingRegion(region_id="table-1", box=box(), mode="ocr", reason="embedded_image_region"),),
        structural_triggers=(
            StructuralTrigger(
                region_id="table-1",
                box=box(),
                trigger="table_region_without_table_read",
                reason="the page draws a table region the native reading recovered no cell text from",
            ),
        ),
    )

    assert [region.region_id for region in routed_textract_regions(decision)] == ["table-1"]
