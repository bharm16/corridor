"""Describe each PDF page before choosing native text or OCR.

The old ingest path used one proxy — fewer than fifty extracted characters —
and silently kept thin text when OCR failed. Character count cannot distinguish
a legitimate short page, a scanned table, mixed native/image content, or broken
Unicode. This module records the visible layers first, then makes a replayable
region decision from that inventory. It never performs OCR or writes storage.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Literal
import unicodedata

import pymupdf
from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union


FIXED_POINT_SCALE = 1_000
ROUTER_VERSION = "page-inventory-router-v1"
OCR_ENGINE = "tesseract"
OCR_CONFIGURATION = {"language": "eng", "page_segmentation_mode": 6}
MIN_IMAGE_REGION_COVERAGE = 0.02
MIN_VECTOR_DENSITY_FOR_OCR = 0.000_01


class InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfRect(InventoryModel):
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


class PageBoxes(InventoryModel):
    media: PdfRect
    crop: PdfRect
    trim: PdfRect
    bleed: PdfRect
    art: PdfRect


class InventoryRegion(InventoryModel):
    region_id: str
    kind: Literal["native_text", "embedded_image", "table", "page"]
    box: PdfRect
    coverage: float = Field(ge=0, le=1)


class TableRegionEvidence(InventoryModel):
    region_id: str
    box: PdfRect
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)


class PageInventory(InventoryModel):
    schema_version: Literal["corridor.pdf-page-inventory.v1"] = (
        "corridor.pdf-page-inventory.v1"
    )
    native_text_length: int = Field(ge=0)
    native_glyph_count: int = Field(ge=0)
    native_glyph_coverage: float = Field(ge=0, le=1)
    embedded_image_coverage: float = Field(ge=0, le=1)
    vector_density: float = Field(ge=0)
    unicode_quality: float = Field(ge=0, le=1)
    suspicious_text_signals: tuple[str, ...]
    rotation_degrees: Literal[0, 90, 180, 270]
    boxes: PageBoxes
    native_regions: tuple[InventoryRegion, ...]
    image_regions: tuple[InventoryRegion, ...]
    table_regions: tuple[TableRegionEvidence, ...]


class RoutingRegion(InventoryModel):
    region_id: str
    box: PdfRect
    mode: Literal["native", "ocr", "both"]
    reason: str


class PageRoutingDecision(InventoryModel):
    schema_version: Literal["corridor.pdf-page-routing.v1"] = (
        "corridor.pdf-page-routing.v1"
    )
    router_version: Literal["page-inventory-router-v1"] = ROUTER_VERSION
    page_mode: Literal["native", "ocr", "both"]
    reason: str
    ocr_engine: Literal["tesseract"] = OCR_ENGINE
    ocr_configuration: dict[str, int | str] = Field(
        default_factory=lambda: dict(OCR_CONFIGURATION)
    )
    regions: tuple[RoutingRegion, ...]


def _fixed_rect(value) -> PdfRect:
    rectangle = pymupdf.Rect(value)
    return PdfRect(
        x0=round(rectangle.x0 * FIXED_POINT_SCALE),
        y0=round(rectangle.y0 * FIXED_POINT_SCALE),
        x1=round(rectangle.x1 * FIXED_POINT_SCALE),
        y1=round(rectangle.y1 * FIXED_POINT_SCALE),
    )


def _coverage(rectangles: list[pymupdf.Rect], page_area: float) -> float:
    shapes = [
        shapely_box(rect.x0, rect.y0, rect.x1, rect.y1)
        for rect in rectangles
        if rect.get_area() > 0
    ]
    if not shapes or page_area <= 0:
        return 0.0
    return round(min(1.0, unary_union(shapes).area / page_area), 8)


def _suspicious_signals(text: str) -> tuple[str, ...]:
    signals: list[str] = []
    if "\ufffd" in text:
        signals.append("replacement_character")
    if "\x00" in text:
        signals.append("null_character")
    if any(unicodedata.category(char) == "Co" for char in text):
        signals.append("private_use_character")
    if any(
        unicodedata.category(char) == "Cc" and char not in "\n\r\t"
        for char in text
    ):
        signals.append("control_character")
    return tuple(signals)


def _unicode_quality(text: str) -> float:
    material = [char for char in text if not char.isspace()]
    if not material:
        return 1.0
    suspicious = sum(
        char == "\ufffd"
        or char == "\x00"
        or unicodedata.category(char) in {"Co", "Cc"}
        for char in material
    )
    return round(max(0.0, 1 - suspicious / len(material)), 8)


def inventory_page(page: pymupdf.Page, *, native_text: str | None = None) -> PageInventory:
    """Take one deterministic inventory in canonical PDF coordinates."""

    text = page.get_text() if native_text is None else native_text
    raw = page.get_text("rawdict")
    native_block_rectangles = [
        pymupdf.Rect(block["bbox"])
        for block in raw.get("blocks", ())
        if block.get("type") == 0 and block.get("bbox")
    ]
    glyph_rectangles = [
        pymupdf.Rect(character["bbox"])
        for block in raw.get("blocks", ())
        for line in block.get("lines", ())
        for span in line.get("spans", ())
        for character in span.get("chars", ())
        if character.get("c", "").strip()
    ]
    image_rectangles = [
        pymupdf.Rect(image["bbox"])
        for image in page.get_image_info(xrefs=True)
        if image.get("bbox")
    ]
    page_area = page.rect.get_area()
    native_regions = tuple(
        InventoryRegion(
            region_id=f"native-{index}",
            kind="native_text",
            box=_fixed_rect(rectangle),
            coverage=(
                round(min(1.0, rectangle.get_area() / page_area), 8)
                if page_area
                else 0
            ),
        )
        for index, rectangle in enumerate(native_block_rectangles, start=1)
    )
    image_regions = tuple(
        InventoryRegion(
            region_id=f"image-{index}",
            kind="embedded_image",
            box=_fixed_rect(rectangle),
            coverage=(
                round(min(1.0, rectangle.get_area() / page_area), 8)
                if page_area
                else 0
            ),
        )
        for index, rectangle in enumerate(image_rectangles, start=1)
    )
    table_regions = []
    try:
        # PyMuPDF prints an optional-layout-package advertisement on some
        # platforms. Inventory is used inside JSON CLIs, so dependency chatter
        # must never corrupt the adapter's stdout contract.
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            found_tables = page.find_tables().tables
    except (AttributeError, ValueError):
        found_tables = ()
    for index, table in enumerate(found_tables, start=1):
        table_regions.append(
            TableRegionEvidence(
                region_id=f"table-{index}",
                box=_fixed_rect(table.bbox),
                row_count=int(getattr(table, "row_count", 0)),
                column_count=int(getattr(table, "col_count", 0)),
            )
        )
    drawing_count = sum(
        max(1, len(drawing.get("items", ()))) for drawing in page.get_drawings()
    )
    return PageInventory(
        native_text_length=len(text.strip()),
        native_glyph_count=len(glyph_rectangles),
        native_glyph_coverage=_coverage(glyph_rectangles, page_area),
        embedded_image_coverage=_coverage(image_rectangles, page_area),
        vector_density=(
            round(drawing_count / page_area, 10) if page_area else 0.0
        ),
        unicode_quality=_unicode_quality(text),
        suspicious_text_signals=_suspicious_signals(text),
        rotation_degrees=int(page.rotation),
        boxes=PageBoxes(
            media=_fixed_rect(page.mediabox),
            crop=_fixed_rect(page.cropbox),
            trim=_fixed_rect(page.trimbox),
            bleed=_fixed_rect(page.bleedbox),
            art=_fixed_rect(page.artbox),
        ),
        native_regions=native_regions,
        image_regions=image_regions,
        table_regions=tuple(table_regions),
    )


def route_page(inventory: PageInventory) -> PageRoutingDecision:
    """Choose native, OCR, or both for each visible content region."""

    full_page = inventory.boxes.crop
    suspicious = bool(inventory.suspicious_text_signals) or (
        inventory.unicode_quality < 0.98
    )
    significant_images = tuple(
        region
        for region in inventory.image_regions
        if region.coverage >= MIN_IMAGE_REGION_COVERAGE
    )
    has_native = inventory.native_glyph_count > 0
    if suspicious and has_native:
        return PageRoutingDecision(
            page_mode="both",
            reason="suspicious_native_text",
            regions=(
                RoutingRegion(
                    region_id="page",
                    box=full_page,
                    mode="both",
                    reason="native_decode_requires_ocr_comparison",
                ),
            ),
        )
    if has_native and significant_images:
        regions = [
            RoutingRegion(
                region_id="native-page",
                box=full_page,
                mode="native",
                reason="clean_native_text",
            )
        ]
        regions.extend(
            RoutingRegion(
                region_id=region.region_id,
                box=region.box,
                mode="ocr",
                reason="embedded_image_region",
            )
            for region in significant_images
        )
        return PageRoutingDecision(
            page_mode="both",
            reason="mixed_native_and_image_regions",
            regions=tuple(regions),
        )
    if significant_images:
        return PageRoutingDecision(
            page_mode="ocr",
            reason="image_only_page",
            regions=tuple(
                RoutingRegion(
                    region_id=region.region_id,
                    box=region.box,
                    mode="ocr",
                    reason="embedded_image_region",
                )
                for region in significant_images
            ),
        )
    if not has_native and inventory.vector_density >= MIN_VECTOR_DENSITY_FOR_OCR:
        return PageRoutingDecision(
            page_mode="ocr",
            reason="vector_only_visible_content",
            regions=(
                RoutingRegion(
                    region_id="page",
                    box=full_page,
                    mode="ocr",
                    reason="visible_vectors_without_native_glyphs",
                ),
            ),
        )
    return PageRoutingDecision(
        page_mode="native",
        reason="clean_native_text" if has_native else "empty_page_no_visible_content",
        regions=(
            RoutingRegion(
                region_id="native-page",
                box=full_page,
                mode="native",
                reason="clean_native_text" if has_native else "no_ocr_evidence",
            ),
        ),
    )
