"""Describe each PDF page before choosing native text or OCR.

The old ingest path used one proxy — fewer than fifty extracted characters —
and silently kept thin text when OCR failed. Character count cannot distinguish
a legitimate short page, a scanned table, mixed native/image content, or broken
Unicode. This module records the visible layers first, then makes a replayable
region decision from that inventory. It never performs OCR or writes storage.

Two inventories lived here until #741, one schema between them (ADR-0094,
#734), and a setting chose which one ingest recorded. The incumbent's facts
came from the retired engine, so the incumbent inventory went with it and the
reader-backed one is the only one left; the routing *rules* were always one
function and still are.

`coordinate_frame` survives that removal and is worth keeping. The reader
reports one frame per page — displayed crop, PDF points, top-left origin, its
tables' own words, and the frame the render derivative an OCR region is cut
from is in. The incumbent reported glyph and image boxes unrotated and table
boxes displayed, so a rotated page's incumbent inventory mixed two frames.
Every retained inventory says which frame it is in rather than leaving a
consumer to infer it from the engine, which is exactly what a reader of an old
row now needs.

Two facts the routing rules need are not in the reader's page result at all:
the embedded image regions and the drawn vector count. `read_page_facts` takes
them from PDFium directly, in the same guarded process as the reader's own
read, rather than editing the reader, which is imported unchanged from its
measured commit (#729).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Literal
import ctypes
import json
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union

from corridor_pdf_reader.execution import PdfiumExecutor, pdfium_entry, read_document
from corridor_pdf_reader.replacement.layout import rotate_box


FIXED_POINT_SCALE = 1_000
MIN_IMAGE_REGION_COVERAGE = 0.02
MIN_VECTOR_DENSITY_FOR_OCR = 0.000_01

# The reader-backed inventory and the route decided from it (#734). The router
# is versioned separately from the incumbent because a recorded decision must
# say which facts decided it, even though the rules below are one function.
READER_ROUTER_VERSION = "page-inventory-router-reader-v1"
# What a persisted routing decision may say, current values and retained ones
# together. The retired engine's name and its router's version are on every
# decision recorded before #741, and a model that refused them would make those
# decisions unreadable rather than making the engine gone. They are held as
# data, in `retained_identities.json`, so no source file names a retired engine
# and nothing in the product can write one: `route_reader_page` is the only
# writer here and it names Textract.
_RETAINED_IDENTITIES = json.loads(
    (Path(__file__).resolve().parent / "retained_identities.json").read_text()
)["page_routing"]
OCR_ENGINES = tuple(
    _RETAINED_IDENTITIES["ocr_engines"]["current"]
    + _RETAINED_IDENTITIES["ocr_engines"]["retained"]
)
ROUTER_VERSIONS = tuple(
    _RETAINED_IDENTITIES["router_versions"]["current"]
    + _RETAINED_IDENTITIES["router_versions"]["retained"]
)
# ADR-0094: scanned pages and image regions read through Amazon Textract. The
# route names the engine; the adapter that calls it records the configuration
# of the call (#732, #739), and this decision fixes none.
TEXTRACT_ENGINE = "textract"
# The reader's own words for the frame its tables are in, reused so the two
# cannot drift apart.
READER_COORDINATE_FRAME = "displayed crop, PDF points, top-left origin"
INCUMBENT_COORDINATE_FRAME = "incumbent page space"


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
    # How many of the region's cells the reading recovered text from. `None`
    # is "this inventory did not count", which is the incumbent's answer: it
    # records that a table is there and never reads it. Zero is the fact #739
    # needs — a table region the reading found and read nothing out of.
    cells_with_text: int | None = Field(default=None, ge=0)


class PageInventory(InventoryModel):
    schema_version: Literal["corridor.pdf-page-inventory.v1"] = (
        "corridor.pdf-page-inventory.v1"
    )
    # Which frame every box below is in. Two engines produce this schema and
    # they do not agree on a rotated page, so an inventory says its frame
    # rather than leaving a consumer to infer it.
    coordinate_frame: str = INCUMBENT_COORDINATE_FRAME
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


class StructuralTrigger(InventoryModel):
    """A structural reason to send a text-layer page to the OCR engine.

    Recorded, and nothing more. The decision this carries is #739's to act on;
    writing it here keeps the reason in the record beside the routing it
    belongs to rather than making a later ticket re-derive it. A missing or
    poor column mapping is deliberately not one of these: that is a semantic
    refusal in Tier 1, and it never justifies an OCR call on its own.
    """

    region_id: str
    box: PdfRect
    trigger: Literal["table_region_without_table_read"]
    engine: Literal["textract"] = TEXTRACT_ENGINE
    reason: str


class PageRoutingDecision(InventoryModel):
    schema_version: Literal["corridor.pdf-page-routing.v1"] = (
        "corridor.pdf-page-routing.v1"
    )
    router_version: str = READER_ROUTER_VERSION
    page_mode: Literal["native", "ocr", "both"]
    reason: str
    ocr_engine: str = TEXTRACT_ENGINE
    ocr_configuration: dict[str, int | str] = Field(default_factory=dict)

    @field_validator("router_version")
    @classmethod
    def _known_router(cls, value: str) -> str:
        if value not in ROUTER_VERSIONS:
            raise ValueError(f"unknown page inventory router {value!r}")
        return value

    @field_validator("ocr_engine")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        if value not in OCR_ENGINES:
            raise ValueError(f"unknown OCR engine {value!r}")
        return value
    regions: tuple[RoutingRegion, ...]
    structural_triggers: tuple[StructuralTrigger, ...] = ()


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


# ---- the reader-backed inventory (ADR-0094, #734) ---------------------------


def read_page_facts(
    source: Path | str, pages: Sequence[int] | None = None
) -> dict[str, Any]:
    """Every page fact the inventory needs, from one guarded PDFium session.

    The reader's own document result, plus the embedded image regions and the
    drawn path count it does not return, read in the same entry rather than in
    a second process. `pdfium_entry` nests on one thread, so the reader's own
    guard inside `read_document` is satisfied by this one. `pages=None` reads
    every page, which is what ingest wants; a measurement that declared its
    pages in advance names them and pays for no others.

    This is the function `PdfiumExecutor` runs in a child; it is module-level
    for that reason. A caller already alone in its process — a test, a
    single-threaded command — may call it directly.
    """

    path = Path(source)
    with pdfium_entry():
        facts = read_document(path, pages)
        facts["objects"] = _pdfium_page_objects(
            path, [page["number"] for page in facts["pages"]]
        )
    return facts


def _pdfium_page_objects(
    path: Path, numbers: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """Embedded image bounds and painted path counts, per 1-based page.

    The imported reader returns glyphs, boxes, rules and reconstructed tables,
    and it is imported unchanged from its measured commit (#729), so these two
    facts are read beside it instead of being added to it. Bounds are PDFium's
    own, in PDF bottom-left user space; the caller puts them in the page's
    frame, because that conversion needs the crop box and rotation the
    reader's geometry already carries.

    A path with neither a fill nor a stroke mode paints nothing — it is a clip
    or an invisible construction — and is not visible content.
    """

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    facts: dict[int, dict[str, Any]] = {}
    document = pdfium.PdfDocument(path)
    try:
        for number in numbers:
            page = document[number - 1]
            images: list[list[float]] = []
            painted = 0
            try:
                for drawn in page.get_objects(
                    filter=[raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH]
                ):
                    if drawn.type == raw.FPDF_PAGEOBJ_IMAGE:
                        images.append([float(value) for value in drawn.get_bounds()])
                        continue
                    fill, stroke = ctypes.c_int(), ctypes.c_int()
                    if raw.FPDFPath_GetDrawMode(drawn, fill, stroke) and (
                        fill.value or stroke.value
                    ):
                        painted += 1
            finally:
                page.close()
            facts[number] = {"images": images, "painted_paths": painted}
    finally:
        document.close()
    return facts


def read_reader_page_inventories(
    source: Path | str,
    pages: Sequence[int] | None = None,
    *,
    executor: PdfiumExecutor | None = None,
) -> dict[int, PageInventory]:
    """One inventory per page, from the reader, in one isolated process.

    The whole document is read once, for the reason #733's token layers are:
    the isolation unit is the PDFium call, and ingest runs in a threaded
    worker, where the cheaper in-process guard would refuse the second
    document outright.
    """

    return reader_page_inventories(
        (executor or PdfiumExecutor()).run(
            read_page_facts, Path(source), None if pages is None else list(pages)
        )
    )


def reader_page_inventories(facts: dict[str, Any]) -> dict[int, PageInventory]:
    """The reader's facts as one inventory per 1-based page number."""

    objects = facts.get("objects") or {}
    return {
        page["number"]: _reader_page_inventory(page, objects.get(page["number"], {}))
        for page in facts["pages"]
    }


def _reader_page_inventory(
    page: dict[str, Any], objects: dict[str, Any]
) -> PageInventory:
    geometry = page["geometry"]
    rotation = int(geometry["rotation"]) % 360
    crop, media = geometry["crop_box"], geometry["media_box"]
    width, height = float(geometry["width"]), float(geometry["height"])
    displayed = (height, width) if rotation % 180 else (width, height)
    page_area = displayed[0] * displayed[1]
    text = page["text"]["value"]
    characters = page["characters"]["value"]
    glyphs = [
        character["display_box"]
        for character in characters
        if character["text"].strip()
    ]
    image_boxes = [
        _displayed_box(bounds, crop, width, height, rotation)
        for bounds in objects.get("images", ())
    ]
    image_boxes = [box for box in image_boxes if _area(box) > 0]
    crop_box = PdfRect(
        x0=0,
        y0=0,
        x1=round(displayed[0] * FIXED_POINT_SCALE),
        y1=round(displayed[1] * FIXED_POINT_SCALE),
    )
    return PageInventory(
        coordinate_frame=READER_COORDINATE_FRAME,
        native_text_length=len(text.strip()),
        native_glyph_count=len(glyphs),
        native_glyph_coverage=_box_coverage(glyphs, page_area),
        embedded_image_coverage=_box_coverage(image_boxes, page_area),
        vector_density=(
            round(int(objects.get("painted_paths", 0)) / page_area, 10)
            if page_area
            else 0.0
        ),
        unicode_quality=_unicode_quality(text),
        suspicious_text_signals=_suspicious_signals(text),
        rotation_degrees=rotation,
        boxes=PageBoxes(
            media=_fixed_box(_displayed_box(media, crop, width, height, rotation)),
            crop=crop_box,
            # The reader reports the media and crop boxes and no others. These
            # three are the PDF's own default for an undeclared box — the crop
            # box — recorded as the default rather than as a reading.
            trim=crop_box,
            bleed=crop_box,
            art=crop_box,
        ),
        native_regions=_reader_native_regions(characters, page_area),
        image_regions=tuple(
            InventoryRegion(
                region_id=f"image-{index}",
                kind="embedded_image",
                box=_fixed_box(box),
                coverage=(
                    round(min(1.0, _area(box) / page_area), 8) if page_area else 0
                ),
            )
            for index, box in enumerate(image_boxes, start=1)
        ),
        table_regions=tuple(
            TableRegionEvidence(
                region_id=f"table-{index}",
                box=_fixed_box(table["box"]),
                row_count=int(table["row_count"]),
                column_count=int(table["column_count"]),
                cells_with_text=sum(
                    1
                    for cell in table["structured_cells"]
                    if str(cell.get("text", "")).strip()
                ),
            )
            for index, table in enumerate(page["tables"]["value"], start=1)
        ),
    )


def _reader_native_regions(
    characters: list[dict[str, Any]], page_area: float
) -> tuple[InventoryRegion, ...]:
    """One region per text object the page draws, in the object's own order.

    The reader groups glyphs by the text object that drew them, which is the
    nearest thing it has to the incumbent's text blocks; a glyph the page
    hides behind a clip path is not in `characters` and so contributes no
    region.
    """

    boxes: dict[int, list[float]] = {}
    for character in characters:
        if not character["text"].strip():
            continue
        box = character["display_box"]
        known = boxes.get(character["object_id"])
        boxes[character["object_id"]] = (
            box
            if known is None
            else [
                min(known[0], box[0]),
                min(known[1], box[1]),
                max(known[2], box[2]),
                max(known[3], box[3]),
            ]
        )
    return tuple(
        InventoryRegion(
            region_id=f"native-{index}",
            kind="native_text",
            box=_fixed_box(box),
            coverage=round(min(1.0, _area(box) / page_area), 8) if page_area else 0,
        )
        for index, box in enumerate(
            (boxes[key] for key in sorted(boxes)), start=1
        )
    )


def _displayed_box(
    bounds, crop, width: float, height: float, rotation: int
) -> list[float]:
    """PDF bottom-left user-space bounds as the reader's displayed-crop box."""

    left, bottom, right, top = (float(value) for value in bounds)
    return rotate_box(
        [left - crop[0], crop[3] - top, right - crop[0], crop[3] - bottom],
        width,
        height,
        rotation,
    )


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _box_coverage(boxes, page_area: float) -> float:
    shapes = [shapely_box(*box) for box in boxes if _area(box) > 0]
    if not shapes or page_area <= 0:
        return 0.0
    return round(min(1.0, unary_union(shapes).area / page_area), 8)


def _fixed_box(box) -> PdfRect:
    x0, y0, x1, y1 = (round(value * FIXED_POINT_SCALE) for value in box)
    return PdfRect(x0=x0, y0=y0, x1=x1, y1=y1)


def route_reader_page(inventory: PageInventory) -> PageRoutingDecision:
    """The incumbent routing rules, over the reader's facts, naming Textract.

    The rules are `route_page`'s and are not restated here: a second copy of
    them would make a replay difference ambiguous between the facts and the
    rules, and the whole point of the Stage 1 replay is that it is not. What
    the reader's facts change is who reads an OCR region — ADR-0094 makes that
    Amazon Textract — and what the record can say about a table region the
    reading found and read nothing out of.
    """

    decision = route_page(inventory)
    return PageRoutingDecision(
        router_version=READER_ROUTER_VERSION,
        page_mode=decision.page_mode,
        reason=decision.reason,
        ocr_engine=TEXTRACT_ENGINE,
        ocr_configuration={},
        regions=decision.regions,
        structural_triggers=_structural_triggers(inventory),
    )


def _structural_triggers(inventory: PageInventory) -> tuple[StructuralTrigger, ...]:
    """Table regions on a text-layer page that the reading read no cells from.

    The ticket's one structural trigger, and deliberately the only one: a
    missing or poor column mapping stays a semantic refusal in Tier 1 and does
    not reach this function, because a refusal to interpret a table that was
    read is not evidence that the page needs OCR.
    """

    if inventory.native_glyph_count == 0:
        return ()
    return tuple(
        StructuralTrigger(
            region_id=region.region_id,
            box=region.box,
            trigger="table_region_without_table_read",
            reason=(
                "the page draws a table region the native reading recovered no "
                "cell text from"
            ),
        )
        for region in inventory.table_regions
        if region.cells_with_text == 0
    )
