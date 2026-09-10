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
import hashlib

import pytest

from corridor.admission import load_project
from corridor.config import settings
from corridor.models import DocPage, Document, Project, ScannedPageObservation, UnreadableCellResolution
from corridor.page_inventory import (
    OCR_ENGINES,
    TEXTRACT_ENGINE,
    PageRoutingDecision,
    PdfRect,
    RoutingRegion,
    StructuralTrigger,
)
from corridor.scanned_reading import (
    ScannedRoutingRefused,
    check_transcription_against_reading,
    classify_region_values,
    derive_native_assignment,
    processing_failure,
    read_scanned_page,
    record_unconfirmed_readings,
    routed_textract_regions,
    scanned_cell_key,
)
from corridor.scanned_reading import TextractRescue
from corridor.principals import HumanPrincipal
from corridor.provider_authorization import TransmissionApproval
from corridor.token_layers import (
    EngineIdentity,
    READER_ENGINE,
    Token,
    TokenLayer,
    reader_native_token_layer,
)
from corridor.unreadable_cells import (
    CellReadingRefused,
    contributes_to_ready,
    current_resolution,
    declare_profile,
    display_reading,
    rescue_page,
)
from sqlalchemy import select
from corridor_pdf_reader.textract.tests.helpers import Page, minimal_pdf
from corridor_pdf_reader.textract_adapter.assignment import assign_native_glyphs
from corridor_pdf_reader.textract_adapter.boundary import (
    TextractProcessingFailure,
    open_boundary,
)
from corridor_pdf_reader.textract_adapter.identity import (
    NativeGlyphs,
    RequestConfiguration,
    normalize,
)
from corridor_pdf_reader.textract_adapter.records import (
    NATIVE_GEOMETRY_PURPOSE,
    PROVIDER_POSTURE,
    CustomerAuthorization,
    ExperimentScope,
    RequestBoundary,
)
from corridor_pdf_reader.textract_adapter.rendering import rasterize_page


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


def test_the_scanned_path_has_no_setting_because_it_has_no_alternative():
    """The incumbent local engine the setting chose instead is gone (#741)."""
    assert not hasattr(settings, "textract_scanned_reading")


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
    """A decision recorded before #741 names the retired engine, and is read.

    Nothing in this module reads for that engine, and the removal did not
    change what an old row says: the retained identity is still a value the
    model accepts, held as data rather than named in source.
    """
    retained_engine, = (name for name in OCR_ENGINES if name != TEXTRACT_ENGINE)
    incumbent = PageRoutingDecision(
        ocr_engine=retained_engine,
        page_mode="ocr",
        reason="image_only_page",
        regions=(RoutingRegion(region_id="image-1", box=box(), mode="ocr", reason="embedded_image_region"),),
    )
    assert incumbent.ocr_engine != TEXTRACT_ENGINE

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


# --- the read goes through the authorization check ------------------------------

ACCEPTED_POSTURE = replace(
    PROVIDER_POSTURE,
    status="accepted",
    retention="verified",
    ai_services_opt_out="optOut",
    permissions="verified",
    # The test's approvals, not the document's (ADR-0098): the recorded posture has neither.
    experimental_approval=TransmissionApproval(
        source_classes=("public-reference-corpus", "synthetic"),
        purposes=("extraction-measurement",),
        unverified=("retention", "ai_services_opt_out", "permissions"),
        approved_by="a named maintainer, in this test only",
        approved_on="2026-09-10",
    ),
    customer_processing_approval=TransmissionApproval(
        source_classes=("scanned-pdf",),
        purposes=PROVIDER_POSTURE.permitted_purposes,
        unverified=(),
        approved_by="a named maintainer, in this test only",
        approved_on="2026-09-10",
    ),
)
CONFIGURATION = RequestConfiguration(dpi=36)
DATASET = "pdf-reader-comparison true-pairs/exact ten development pairs"
SOURCE_SHA = "a" * 64
RENDITION_SHA = "b" * 64


class FailingService:
    """The transport double: any outbound request fails the test."""

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        pytest.fail("an outbound Textract request was made")


class RecordedService:
    """Answers every request with one retained response and counts the requests."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.calls += 1
        return self.response


def experiment_scope(**overrides: Any) -> ExperimentScope:
    fields: dict[str, Any] = dict(
        record_id="exp-0001",
        dataset=DATASET,
        dataset_digest="ca68b55a",
        purpose="extraction-measurement",
        scope="the ten pairs' pages and their scan twins, lanes A, B and C",
        source_classes=frozenset({"public-reference-corpus", "synthetic"}),
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        posture_digest=PROVIDER_POSTURE.digest,
        recorded_by="a named person",
        recorded_on="2026-09-06",
    )
    fields.update(overrides)
    return ExperimentScope(**fields)


def experiment_request(**overrides: Any) -> RequestBoundary:
    fields: dict[str, Any] = dict(
        project=DATASET,
        source_class="synthetic",
        purpose="extraction-measurement",
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        stage="experiment",
    )
    fields.update(overrides)
    return RequestBoundary(**fields)


def customer_authorization(**overrides: Any) -> CustomerAuthorization:
    """A synthetic customer record; by default it names the read and the derivation both."""
    fields: dict[str, Any] = dict(
        record_id="auth-0810",
        customer="a synthetic customer",
        projects=frozenset({"project-1"}),
        source_classes=frozenset({"scanned-pdf"}),
        purposes=frozenset({"scanned-page-reading", NATIVE_GEOMETRY_PURPOSE}),
        stages=frozenset({"shadow"}),
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        posture_digest=PROVIDER_POSTURE.digest,
        signed_by="a named person",
        signed_on="2026-09-10",
    )
    fields.update(overrides)
    return CustomerAuthorization(**fields)


def customer_request(**overrides: Any) -> RequestBoundary:
    fields: dict[str, Any] = dict(
        project="project-1",
        source_class="scanned-pdf",
        purpose="scanned-page-reading",
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        stage="shadow",
    )
    fields.update(overrides)
    return RequestBoundary(**fields)


def glyphs_of(text: str, x0: float, y0: float = 100.0, *, width: float = 12.0, height: float = 12.0, object_id: int = 7) -> list[dict[str, Any]]:
    """The reader's glyphs for one printed word, `width` points each from `x0`."""
    return [
        {"text": char, "display_box": [x0 + width * index, y0, x0 + width * (index + 1), y0 + height], "object_id": object_id, "source_index": index}
        for index, char in enumerate(text)
    ]


def reader_page_of(glyphs: list[dict[str, Any]], text: str) -> dict[str, Any]:
    return {
        "number": 1,
        "geometry": {"rotation": 0},
        "text": {"value": text},
        "characters": {"value": glyphs},
        "clipped": {"value": []},
    }


def reader_layer_of(page: dict[str, Any]) -> TokenLayer:
    return reader_native_token_layer(page, identity=native_layer().identity, source_sha256=SOURCE_SHA)


def opened(tmp_path: Path, service: Any, record: Any = ..., **options: Any):
    return open_boundary(
        experiment_scope() if record is ... else record,
        options.pop("boundary", None) or experiment_request(),
        extraction_run="run-739",
        cache_root=tmp_path / "cache",
        service=service,
        configuration=CONFIGURATION,
        posture=ACCEPTED_POSTURE,
        sleep=lambda seconds: None,
        **options,
    )


def raster_of(tmp_path: Path):
    return rasterize_page(minimal_pdf(tmp_path / "page.pdf", text="Total"), 1, CONFIGURATION)


def scanned_response() -> dict[str, Any]:
    """A page holding one two-column row: 'PLACEHOLDER 42' at known points."""
    page = Page()
    first = page.word("PLACEHOLDER", (100, 100, 180, 112))
    second = page.word("42", (220, 100, 240, 112))
    page.line([first, second])
    left = page.cell(1, 1, (90, 95, 200, 118), [first])
    right = page.cell(1, 2, (200, 95, 260, 118), [second])
    page.table((90, 95, 260, 118), [left, right])
    return page.response()


def native_layer(*words: tuple[str, tuple[float, float, float, float]]) -> TokenLayer:
    """A reader-backed native layer with the given words, in displayed-crop points."""
    return TokenLayer(
        page_no=1,
        origin="native",
        source_sha256=SOURCE_SHA,
        identity=EngineIdentity(
            origin="native",
            engine=READER_ENGINE,
            engine_version="5.13.0",
            adapter_version="native-reader-v2",
        ),
        tokens=tuple(
            Token(
                ordinal=index,
                origin="native",
                raw_text=text,
                normalized_text=text.lower(),
                polygon_pdf=PdfRect(
                    x0=round(box[0] * 1000),
                    y0=round(box[1] * 1000),
                    x1=round(box[2] * 1000),
                    y1=round(box[3] * 1000),
                ),
            )
            for index, (text, box) in enumerate(words)
        ),
        quality={},
    )


def test_a_page_no_recorded_decision_routes_here_is_refused_before_any_call(tmp_path):
    service = FailingService()
    adapter = opened(tmp_path, service)
    native_only = reader_decision(
        page_mode="native",
        reason="clean_native_text",
        regions=(RoutingRegion(region_id="native-page", box=box(), mode="native", reason="clean_native_text"),),
    )

    with pytest.raises(ScannedRoutingRefused):
        read_scanned_page(
            adapter,
            raster_of(tmp_path),
            routing=native_only,
            page_no=1,
            rendition_sha256=RENDITION_SHA,
            source_sha256=SOURCE_SHA,
        )

    assert adapter.receipt.outbound_requests == 0


def test_the_read_is_refused_without_an_authorization_record_and_sends_nothing(tmp_path):
    """The check is the adapter's; this asserts the caller cannot get past it."""
    service = FailingService()

    with pytest.raises(TextractProcessingFailure) as raised:
        opened(tmp_path, service, record=None)

    failure = processing_failure(
        raised.value,
        page_no=1,
        regions=routed_textract_regions(reader_decision()),
        rendition_sha256=RENDITION_SHA,
        configuration=CONFIGURATION.as_dict(),
    )
    assert failure.engine == "textract"
    assert failure.reason == "authorization-absent"
    assert failure.outbound_requests == 0
    assert failure.scope == {
        "page_number": 1,
        "rendition_sha256": RENDITION_SHA,
        "region_ids": ["image-1"],
    }
    assert failure.configuration["operation"] == "AnalyzeDocument"
    assert not (tmp_path / "cache").exists()


def test_a_routed_page_reads_through_the_boundary_and_carries_the_provider_identity(tmp_path):
    service = RecordedService(scanned_response())
    adapter = opened(tmp_path, service)

    reading = read_scanned_page(
        adapter,
        raster_of(tmp_path),
        routing=reader_decision(),
        page_no=1,
        rendition_sha256=RENDITION_SHA,
        source_sha256=SOURCE_SHA,
    )

    assert service.calls == 1
    layer = reading.token_layer
    assert layer.origin == "ocr"
    assert layer.identity.engine == "textract"
    assert layer.identity.provider == "textract"
    assert layer.identity.provider_model_version == "1.0"
    assert layer.identity.raw_response_sha256 == reading.provenance["raw_response_digest"]
    assert layer.identity.reading_sha256 == reading.provenance["normalized_reading_digest"]
    assert layer.identity.configuration["operation"] == "AnalyzeDocument"
    # One token per cell that holds text; the line's words are all owned by
    # cells, so nothing is outside the table and nothing is counted twice.
    assert [token.raw_text for token in layer.tokens] == ["PLACEHOLDER", "42"]
    assert layer.quality["outside_lines"] == 0
    assert all(token.origin == "ocr" for token in layer.tokens)
    assert adapter.receipt.calls == 1


def test_a_second_read_of_the_same_raster_is_a_hit_and_not_a_second_charge(tmp_path):
    service = RecordedService(scanned_response())
    adapter = opened(tmp_path, service)
    for _ in range(2):
        read_scanned_page(
            adapter,
            raster_of(tmp_path),
            routing=reader_decision(),
            page_no=1,
            rendition_sha256=RENDITION_SHA,
            source_sha256=SOURCE_SHA,
        )

    assert service.calls == 1
    assert (adapter.receipt.calls, adapter.receipt.hits) == (1, 1)


# --- the three cases ------------------------------------------------------------


def test_textract_words_without_a_native_layer_are_unconfirmed_readings(tmp_path):
    service = RecordedService(scanned_response())
    adapter = opened(tmp_path, service)

    reading = read_scanned_page(
        adapter,
        raster_of(tmp_path),
        routing=reader_decision(),
        page_no=1,
        rendition_sha256=RENDITION_SHA,
        source_sha256=SOURCE_SHA,
    )

    assert [(value.value, value.value_source, value.state) for value in reading.values] == [
        ("PLACEHOLDER", "textract_words", "unconfirmed"),
        ("42", "textract_words", "unconfirmed"),
    ]
    unconfirmed = reading.unconfirmed_readings[0]
    assert unconfirmed.confidence == pytest.approx(0.99)
    assert unconfirmed.locator is None
    assert unconfirmed.provenance["source"]["region_id"] == "image-1"
    assert unconfirmed.provenance["source"]["rendition_sha256"] == RENDITION_SHA
    assert unconfirmed.provenance["processing"]["engine"] == "textract"
    assert unconfirmed.provenance["processing"]["raw_response_digest"]
    assert unconfirmed.provenance["processing"]["normalized_reading_digest"]


def test_a_cell_with_re_mapped_native_glyphs_takes_the_source_verification_path():
    """Geometry is Textract's; the value and the locator are the document's own."""
    reading = {
        "tables": [
            {
                "cells": [
                    {"row": 0, "column": 0, "box": [90, 95, 200, 118], "text": "PLACEH0LDER", "confidence": 99.0},
                    {"row": 0, "column": 1, "box": [200, 95, 260, 118], "text": "42", "confidence": 99.0},
                ]
            }
        ],
        "outside": [],
    }
    routed = routed_textract_regions(reader_decision())

    assignment = assign_native_glyphs(reading, NativeGlyphs(characters=glyphs_of("PLACEHOLDER", 100.0, width=7.0)))
    values = classify_region_values(
        reading,
        regions=routed,
        assignment=assignment,
        provenance={"rendition_sha256": RENDITION_SHA, "page_number": 1},
    )

    first, second = values
    assert (first.value, first.value_source, first.state) == (
        "PLACEHOLDER",
        "native_glyphs",
        "source_verified",
    )
    # The locator is the union of the eleven assigned glyphs' own boxes, and
    # the same assignment supplied the text: no second reconstruction.
    assert first.locator == PdfRect(x0=100_000, y0=100_000, x1=177_000, y1=112_000)
    assert assignment.cells[0].locator == (100.0, 100.0, 177.0, 112.0)
    assert first.confidence is None
    # The cell Textract read 'PLACEH0LDER' out of is the same cell; only the
    # characters differ, and the document's own win.
    assert (second.value, second.state) == ("42", "unconfirmed")


def test_the_route_takes_the_measured_lane_a_assignment_cell_for_cell():
    """One document, one rule: the cell that used to differ now agrees.

    ADR-0094 measured lane A with `remap_page`: each glyph goes to the Textract
    polygon holding its own ink-box centre and the cell's text is `ordered_text`
    over those glyphs, so a word printed across a cell border is split there.
    Before #810 the route assigned the reader's *word tokens* by their centres
    and kept that word whole; this test's predecessor pinned TOTAL against
    TOT / AL. The route now composes from the shared assignment
    (`textract_adapter.assignment`), so both cells carry the split the
    measured rule makes, each with a locator that is the union of its own
    assigned glyphs' boxes — the evidence `remap_page`'s glyph count could not
    supply.
    """
    # Textract drew two cells split at x=200 and read the word, with a zero
    # for the O, wholly into the left one.
    page = Page()
    word = page.word("T0TAL", (165, 100, 225, 112))
    page.line([word])
    left = page.cell(1, 1, (90, 95, 200, 118), [word])
    right = page.cell(1, 2, (200, 95, 260, 118))
    page.table((90, 95, 260, 118), [left, right])
    response = page.response()
    # The document's own glyphs: TOTAL at 12 points per glyph from x=165, so
    # T, O, T have their centres left of the border and A, L right of it.
    glyphs = glyphs_of("TOTAL", 165.0)
    layer = reader_layer_of(reader_page_of(glyphs, "TOTAL"))
    assert [token.raw_text for token in layer.tokens] == ["TOTAL"], "the reader assembles one word across the border"

    measured = normalize(response, number=1, size=(612.0, 792.0), rotation=0, glyphs=NativeGlyphs(characters=glyphs))
    words = normalize(response, number=1, size=(612.0, 792.0), rotation=0)
    assignment = assign_native_glyphs(words, NativeGlyphs(characters=glyphs))
    routed = classify_region_values(
        words,
        regions=routed_textract_regions(reader_decision()),
        assignment=assignment,
        provenance={},
    )

    assert measured["text_source"] == "pdfium-glyphs"
    assert [(cell["text"], cell["glyphs"]) for cell in measured["tables"][0]["cells"]] == [("TOT", 3), ("AL", 2)]
    assert [(cell.text, len(cell.glyphs)) for cell in assignment.cells] == [("TOT", 3), ("AL", 2)]
    assert [(value.column, value.value, value.value_source) for value in routed] == [(0, "TOT", "native_glyphs"), (1, "AL", "native_glyphs")]
    assert [value.locator for value in routed] == [
        PdfRect(x0=165_000, y0=100_000, x1=201_000, y1=112_000),
        PdfRect(x0=201_000, y0=100_000, x1=225_000, y1=112_000),
    ]
    # The observation keeps its own words beside the assignment.
    assert words["tables"][0]["cells"][0]["text"] == "T0TAL" and words["tables"][0]["cells"][0]["word_ids"]
    assert (assignment.cells[0].ocr_text, assignment.cells[1].ocr_text) == ("T0TAL", "")


def test_the_incumbent_native_layer_is_not_a_usable_native_layer():
    """Its word boxes are in another frame; re-mapping over it would mix two."""
    reading = {
        "tables": [{"cells": [{"row": 0, "column": 0, "box": [90, 95, 200, 118], "text": "PLACEHOLDER", "confidence": 99.0}]}],
        "outside": [],
    }
    incumbent = native_layer(("PLACEHOLDER", (100, 100, 180, 112)))
    incumbent = incumbent.model_copy(
        update={"identity": incumbent.identity.model_copy(update={"engine": "pymupdf"})}
    )
    glyphs = glyphs_of("PLACEHOLDER", 100.0, width=7.0)

    derivation = derive_native_assignment(
        reading,
        native_layer=incumbent,
        reader_page=reader_page_of(glyphs, "PLACEHOLDER"),
        record=customer_authorization(),
        posture=ACCEPTED_POSTURE,
    )
    (value,) = classify_region_values(
        reading,
        regions=routed_textract_regions(reader_decision()),
        assignment=derivation.assignment,
        provenance={},
    )

    assert derivation.assignment is None
    assert derivation.record == {"purpose": NATIVE_GEOMETRY_PURPOSE, "derived": False, "reason": "no-usable-native-layer"}
    assert (value.value_source, value.state) == ("textract_words", "unconfirmed")


def test_a_cell_outside_every_routed_region_is_not_read_here():
    """On a mixed page the native regions keep their native values."""
    reading = {
        "tables": [
            {
                "cells": [
                    {"row": 0, "column": 0, "box": [90, 95, 200, 118], "text": "in", "confidence": 99.0},
                    {"row": 0, "column": 1, "box": [500, 600, 560, 620], "text": "out", "confidence": 99.0},
                ]
            }
        ],
        "outside": [],
    }
    decision = reader_decision(
        page_mode="both",
        reason="mixed_native_and_image_regions",
        regions=(
            RoutingRegion(region_id="native-page", box=box(), mode="native", reason="clean_native_text"),
            RoutingRegion(
                region_id="image-1",
                box=box(80_000, 90_000, 300_000, 200_000),
                mode="ocr",
                reason="embedded_image_region",
            ),
        ),
    )

    values = classify_region_values(
        reading,
        regions=routed_textract_regions(decision),
        assignment=None,
        provenance={},
    )

    assert [value.value for value in values] == ["in"]


# --- per-region composition over one retained response --------------------------


def mixed_response() -> dict[str, Any]:
    """One row of two cells: the left over printed text, the right over an image.

    Textract reads both. The document's glyphs cover only the left one, so the
    left cell can take its native value and the right cell cannot.
    """
    page = Page()
    printed = page.word("T0TAL", (100, 100, 160, 112))
    pictured = page.word("42", (220, 100, 240, 112))
    page.line([printed, pictured])
    left = page.cell(1, 1, (90, 95, 200, 118), [printed])
    right = page.cell(1, 2, (200, 95, 260, 118), [pictured])
    page.table((90, 95, 260, 118), [left, right])
    return page.response()


def mixed_decision() -> PageRoutingDecision:
    return reader_decision(
        page_mode="both",
        reason="mixed_native_and_image_regions",
        regions=(
            RoutingRegion(region_id="page", box=box(), mode="both", reason="native_decode_requires_ocr_comparison"),
        ),
    )


def _mixed_page_read(tmp_path, record: Any, boundary: RequestBoundary):
    service = RecordedService(mixed_response())
    adapter = opened(tmp_path, service, record, boundary=boundary)
    glyphs = glyphs_of("TOTAL", 100.0)
    reader_page = reader_page_of(glyphs, "TOTAL")
    reading = read_scanned_page(
        adapter,
        raster_of(tmp_path),
        routing=mixed_decision(),
        page_no=1,
        rendition_sha256=RENDITION_SHA,
        source_sha256=SOURCE_SHA,
        native_layer=reader_layer_of(reader_page),
        reader_page=reader_page,
    )
    return reading, service, adapter


def test_a_mixed_page_composes_native_and_unconfirmed_cells_from_one_retained_response(tmp_path):
    """Native where the glyphs are, Unconfirmed where they are not, one request, both purposes recorded."""
    reading, service, adapter = _mixed_page_read(tmp_path, customer_authorization(), customer_request())

    native, unconfirmed = reading.values
    assert (native.value, native.value_source, native.state) == ("TOTAL", "native_glyphs", "source_verified")
    assert native.locator == PdfRect(x0=100_000, y0=100_000, x1=160_000, y1=112_000)
    assert (unconfirmed.value, unconfirmed.value_source, unconfirmed.state) == ("42", "textract_words", "unconfirmed")
    assert unconfirmed.locator is None and unconfirmed.confidence == pytest.approx(0.99)
    assert reading.unconfirmed_readings == (unconfirmed,)
    # The derivation ran over the response the read returned: one request,
    # one observation, and both purposes on the reading and on every cell.
    assert service.calls == 1 and adapter.receipt.calls == 1
    assert reading.provenance["purposes"] == ["scanned-page-reading", NATIVE_GEOMETRY_PURPOSE]
    assert reading.provenance["native_geometry"] == {
        "purpose": NATIVE_GEOMETRY_PURPOSE,
        "derived": True,
        "text_source": "pdfium-glyphs",
        "assigned_cells": 1,
        "empty_cells": 1,
        "unassigned_glyphs": 0,
        "clipped_runs": 0,
    }
    for value in reading.values:
        assert value.provenance["processing"]["purposes"] == reading.provenance["purposes"]
        assert value.provenance["processing"]["raw_response_digest"] == reading.provenance["raw_response_digest"]
        assert value.provenance["processing"]["normalized_reading_digest"] == reading.provenance["normalized_reading_digest"]
    # The observation itself still holds Textract's words for the native cell.
    assert reading.reading["tables"][0]["cells"][0]["text"] == "T0TAL"
    assert reading.reading["tables"][0]["cells"][0]["word_ids"]


@pytest.mark.parametrize(
    ("record", "boundary", "expected"),
    [
        pytest.param(
            customer_authorization(purposes=frozenset({"scanned-page-reading"})),
            customer_request(),
            f"purpose: {NATIVE_GEOMETRY_PURPOSE!r} is not named by record 'auth-0810' (scanned-page-reading)",
            id="customer-record-names-only-the-read",
        ),
        pytest.param(
            experiment_scope(),
            experiment_request(),
            f"purpose: {NATIVE_GEOMETRY_PURPOSE!r} is not the purpose of experiment scope 'exp-0001' ('extraction-measurement')",
            id="experiment-scope-names-another-purpose",
        ),
    ],
)
def test_an_authorization_that_does_not_name_the_derivation_gets_no_native_values(tmp_path, record, boundary, expected):
    """The posture listing the purpose does not extend a record; the cells stay Textract's, unconfirmed."""
    reading, service, _ = _mixed_page_read(tmp_path, record, boundary)

    assert [(value.value, value.value_source, value.state) for value in reading.values] == [
        ("T0TAL", "textract_words", "unconfirmed"),
        ("42", "textract_words", "unconfirmed"),
    ]
    assert service.calls == 1
    assert reading.provenance["purposes"] == [boundary.purpose]
    assert reading.provenance["native_geometry"] == {
        "purpose": NATIVE_GEOMETRY_PURPOSE,
        "derived": False,
        "reason": "authorization-does-not-name-the-purpose",
        "mismatches": [expected],
    }


# --- Tier 2 disagreement --------------------------------------------------------


def reading_layer(tmp_path, response: dict[str, Any] | None = None) -> TokenLayer:
    service = RecordedService(response or scanned_response())
    adapter = opened(tmp_path, service)
    return read_scanned_page(
        adapter,
        raster_of(tmp_path),
        routing=reader_decision(),
        page_no=1,
        rendition_sha256=RENDITION_SHA,
        source_sha256=SOURCE_SHA,
    ).token_layer


def test_a_transcription_the_reading_does_not_hold_is_a_detected_disagreement(tmp_path):
    check = check_transcription_against_reading("PLACEHOLDER 43", reading_layer(tmp_path))

    assert check.verdict == "disagreement-detected"
    assert check.disagrees is True
    assert check.disagreed == ("43",)
    assert check.agreed == ("placeholder",)


def test_agreement_with_the_reading_is_never_corroboration(tmp_path):
    """Both readings can be wrong the same way; the model was shown the same page."""
    check = check_transcription_against_reading("PLACEHOLDER 42", reading_layer(tmp_path))

    assert check.verdict == "no-disagreement-detected"
    assert check.disagrees is False
    assert check.corroborates is False


def test_a_transcription_is_only_checked_against_the_provider_that_read_the_page(tmp_path):
    with pytest.raises(ValueError, match="read by"):
        check_transcription_against_reading("anything", native_layer(("x", (0, 0, 1, 1))))


# --- an unconfirmed reading: flagged, never Ready, upgraded on corroboration ----


@pytest.fixture
def project(session):
    project = Project(slug="scanned-textract-test", name="Scanned Textract Test", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _document(session, project, *, filename, text, text_source):
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{filename}{text}".encode()).hexdigest(),
        filename=filename,
        doc_type="matrix" if text_source == "ocr" else "other",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(document_id=document.id, page_no=1, text=text, text_source=text_source)
    )
    session.flush()
    return document


def _scanned_reading(tmp_path, response: dict[str, Any] | None = None):
    service = RecordedService(response or scanned_response())
    adapter = opened(tmp_path, service)
    return read_scanned_page(
        adapter,
        raster_of(tmp_path),
        routing=reader_decision(),
        page_no=1,
        rendition_sha256=RENDITION_SHA,
        source_sha256=SOURCE_SHA,
    )


def _corroborating_response() -> dict[str, Any]:
    """The same page shape, holding a value a readable sibling source also states."""
    page = Page()
    word = page.word("CenterPoint", (100, 100, 190, 112))
    page.line([word])
    cell = page.cell(1, 1, (90, 95, 200, 118), [word])
    page.table((90, 95, 200, 118), [cell])
    return page.response()


def test_a_textract_only_value_is_recorded_as_an_unconfirmed_reading(session, project, tmp_path):
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    reading = _scanned_reading(tmp_path)

    appended = record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )

    assert [row.value for row in appended] == ["PLACEHOLDER", "42"]
    assert {row.state for row in appended} == {"unconfirmed"}
    assert {row.policy_version for row in appended} == {"scanned-textract-reading-v1"}
    # Each row resolves to the observation that produced it (#809): the
    # adapter's binding, held as references, and the routed region the cell
    # fell in. `run_id` is the harness's and stays null on this route.
    [observation] = session.scalars(select(ScannedPageObservation)).all()
    assert {row.observation_id for row in appended} == {observation.id}
    assert {row.source_region_id for row in appended} == {"image-1"}
    assert {row.run_id for row in appended} == {None}
    assert {row.observation_unbound_reason for row in appended} == {None}
    binding = reading.provenance
    assert observation.document_id == document.id
    assert observation.page_no == 1
    assert observation.rendition_sha256 == RENDITION_SHA == binding["rendition_sha256"]
    assert observation.authorization_record_id == "exp-0001" == binding["record_id"]
    assert observation.scope_digest == binding["scope_digest"]
    assert observation.raster_sha256 == binding["raster_sha256"]
    assert observation.raw_response_sha256 == binding["raw_response_digest"]
    assert observation.reading_sha256 == binding["normalized_reading_digest"]
    assert observation.provider_model_version == binding["model_version"]
    assert observation.provider_request_id == binding["request_identity"]["request_id"]
    assert observation.observed_at.isoformat(timespec="seconds") == binding["recorded_at"]


def test_on_a_mixed_page_only_the_image_cell_is_recorded_and_it_binds_to_the_shared_observation(session, project, tmp_path):
    """The native cell took its value from the same response, but it is on the source-verification path, not in this class."""
    document = _document(session, project, filename="matrix/mixed.pdf", text="TOTAL", text_source="ocr")
    reading, _, _ = _mixed_page_read(tmp_path, customer_authorization(), customer_request())

    appended = record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )

    [row] = appended
    assert (row.value, row.state, row.source_region_id) == ("42", "unconfirmed", "page")
    assert row.cell_key == "scan:p1:t0:r0:c1"
    [observation] = session.scalars(select(ScannedPageObservation)).all()
    assert row.observation_id == observation.id
    assert observation.authorization_record_id == "auth-0810"
    assert observation.raw_response_sha256 == reading.provenance["raw_response_digest"]
    assert observation.reading_sha256 == reading.provenance["normalized_reading_digest"]
    native = reading.values[0]
    assert native.state == "source_verified"
    assert native.provenance["processing"]["raw_response_digest"] == observation.raw_response_sha256


def test_an_unconfirmed_reading_displays_flagged(session, project, tmp_path):
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    reading = _scanned_reading(tmp_path)
    record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )

    resolution = current_resolution(
        session,
        document_id=document.id,
        page_no=1,
        cell_key=scanned_cell_key(reading.unconfirmed_readings[0], page_no=1),
    )
    shown = display_reading(resolution)

    assert shown.flagged is True
    assert shown.state == "unconfirmed"
    assert shown.value == "PLACEHOLDER"
    assert "no reading of it is proven" in shown.label
    # A flag, not a control: nothing on the display offers a person a
    # transcription to accept.
    assert not any(
        name in {"confirm", "accept", "correct", "approve", "review"}
        for name in vars(shown)
    )


def test_an_unconfirmed_reading_never_contributes_to_ready(session, project, tmp_path):
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    reading = _scanned_reading(tmp_path)
    record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )

    for value in reading.unconfirmed_readings:
        resolution = current_resolution(
            session,
            document_id=document.id,
            page_no=1,
            cell_key=scanned_cell_key(value, page_no=1),
        )
        assert contributes_to_ready(resolution) is False
        assert display_reading(resolution).contributes_to_ready is False


def test_an_unconfirmed_reading_upgrades_automatically_when_corroboration_lands(
    session, project, tmp_path
):
    """No human step, and the upgrade keeps its relation to the original reading."""
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    reading = _scanned_reading(tmp_path, _corroborating_response())
    record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )
    cell_key = scanned_cell_key(reading.unconfirmed_readings[0], page_no=1)
    before = current_resolution(
        session, document_id=document.id, page_no=1, cell_key=cell_key
    )
    assert before.state == "unconfirmed" and before.value == "CenterPoint"

    readable = _document(
        session,
        project,
        filename="sue/level-a.xlsx",
        text="SUE Level A\nOwner: CenterPoint\nUtility: 16-inch gas main",
        text_source="cells",
    )
    load_project(session, project.id)

    after = current_resolution(
        session, document_id=document.id, page_no=1, cell_key=cell_key
    )
    assert after.state == "corroborated"
    assert after.origin == "corroboration_upgrade"
    assert after.corroboration_document_id == readable.id
    assert "CenterPoint" in after.corroboration_quote
    assert after.value == before.value
    # Still flagged, and still out of Ready: corroborated is not admitted.
    assert display_reading(after).flagged is True
    assert contributes_to_ready(after) is False


def test_re_reading_the_same_page_does_not_bury_an_upgraded_cell(session, project, tmp_path):
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    reading = _scanned_reading(tmp_path, _corroborating_response())
    record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=reading
    )
    _document(
        session,
        project,
        filename="sue/level-a.xlsx",
        text="SUE Level A\nOwner: CenterPoint",
        text_source="cells",
    )
    load_project(session, project.id)

    assert (
        record_unconfirmed_readings(
            session, project_id=project.id, document_id=document.id, reading=reading
        )
        == ()
    )
    cell_key = scanned_cell_key(reading.unconfirmed_readings[0], page_no=1)
    assert (
        current_resolution(
            session, document_id=document.id, page_no=1, cell_key=cell_key
        ).state
        == "corroborated"
    )


def _second_observation_response() -> dict[str, Any]:
    """The same cell, read differently: Textract reused row 1, column 1."""
    page = Page()
    word = page.word("CenterPoynt", (100, 100, 190, 112))
    page.line([word])
    cell = page.cell(1, 1, (90, 95, 200, 118), [word])
    page.table((90, 95, 200, 118), [cell])
    return page.response()


def test_a_new_observation_of_a_cell_appends_beside_a_corroboration_and_inherits_nothing(
    session, project, tmp_path
):
    """Observation versus resolution (#809).

    A second provider observation of the same cell key is a new observation.
    It does not overwrite the first, it does not erase the corroboration the
    first earned — that row stays bound to the observation it was checked
    against — and it does not inherit that corroboration because Textract
    reused a row and column index. The new reading is unconfirmed until it
    earns its own.
    """
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    first = _scanned_reading(tmp_path, _corroborating_response())
    record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=first
    )
    cell_key = scanned_cell_key(first.unconfirmed_readings[0], page_no=1)
    _document(
        session,
        project,
        filename="sue/level-a.xlsx",
        text="SUE Level A\nOwner: CenterPoint",
        text_source="cells",
    )
    load_project(session, project.id)
    corroborated = current_resolution(
        session, document_id=document.id, page_no=1, cell_key=cell_key
    )
    assert corroborated.state == "corroborated"

    # A different response for the same raster, read under its own scope
    # directory so the retained first response is not what answers.
    second = _scanned_reading(tmp_path / "again", _second_observation_response())
    assert scanned_cell_key(second.unconfirmed_readings[0], page_no=1) == cell_key
    [appended] = record_unconfirmed_readings(
        session, project_id=project.id, document_id=document.id, reading=second
    )
    load_project(session, project.id)

    observations = session.scalars(
        select(ScannedPageObservation).order_by(ScannedPageObservation.id)
    ).all()
    assert len(observations) == 2
    assert observations[0].raw_response_sha256 != observations[1].raw_response_sha256
    assert corroborated.observation_id == observations[0].id
    assert appended.observation_id == observations[1].id
    assert (appended.state, appended.value) == ("unconfirmed", "CenterPoynt")
    assert appended.corroboration_document_id is None
    # The earlier corroboration is untouched, and still the only one.
    rows = session.scalars(
        select(UnreadableCellResolution)
        .where(
            UnreadableCellResolution.document_id == document.id,
            UnreadableCellResolution.cell_key == cell_key,
        )
        .order_by(UnreadableCellResolution.id)
    ).all()
    assert [(row.state, row.observation_id) for row in rows] == [
        ("unconfirmed", observations[0].id),
        ("corroborated", observations[0].id),
        ("unconfirmed", observations[1].id),
    ]
    assert current_resolution(
        session, document_id=document.id, page_no=1, cell_key=cell_key
    ).id == appended.id
    # Recording the second observation again is idempotent per observation.
    assert (
        record_unconfirmed_readings(
            session, project_id=project.id, document_id=document.id, reading=second
        )
        == ()
    )


# --- ADR-0064's mechanical rescue, read by Textract ----------------------------


def _rescue(tmp_path, adapter, **overrides: Any) -> TextractRescue:
    fields: dict[str, Any] = dict(
        source=minimal_pdf(tmp_path / "pinned.pdf", text="Total"),
        page_number=1,
        rendition_sha256=RENDITION_SHA,
        page_image_sha256="pin-1",
        read_identities=("textract",),
    )
    fields.update(overrides)
    return TextractRescue(adapter, **fields)


def test_a_profile_that_did_not_declare_the_textract_read_cannot_be_rescued_by_it(tmp_path):
    """The harness may never widen its own scope; the refusal is before it runs."""
    adapter = opened(tmp_path, FailingService())

    with pytest.raises(CellReadingRefused) as raised:
        _rescue(tmp_path, adapter, read_identities=("vision_model_a",))

    assert raised.value.reason == "undeclared_read_identity"


def test_the_rescue_reads_the_pinned_page_through_the_boundary(tmp_path):
    """Through the boundary, and through nothing else.

    That the replacement path never reaches the incumbent engine is proved by
    `tests/test_architecture.py::test_only_allowlisted_modules_still_use_pymupdf_or_tesseract`
    rather than restated here: `src/corridor/scanned_reading.py` is on neither
    engine's allowlist, and that guard is exact in both directions, so it fails
    the moment this module imports either engine or names one.
    """
    service = RecordedService(scanned_response())
    adapter = opened(tmp_path, service)
    rescue = _rescue(tmp_path, adapter)

    recovered = rescue.rescue(image_sha256="pin-1", image_path=None, ops=())

    assert recovered == "PLACEHOLDER\n42"
    assert service.calls == 1
    assert rescue.provenance["raw_response_digest"]
    assert adapter.receipt.calls == 1


def test_the_rescue_refuses_a_page_the_harness_pinned_differently(tmp_path):
    adapter = opened(tmp_path, FailingService())
    rescue = _rescue(tmp_path, adapter)

    with pytest.raises(CellReadingRefused) as raised:
        rescue.rescue(image_sha256="pin-2", image_path=None, ops=())

    assert raised.value.reason == "stale_pinned_page"


def test_the_rescue_satisfies_the_harness_preprocessor_and_leaves_it_unchanged(
    session, project, tmp_path
):
    """The harness runs its own unchanged `rescue_page` over this preprocessor."""
    profile = declare_profile(
        session,
        project_id=project.id,
        principal=HumanPrincipal("local:scanned-textract-test"),
        min_readable_text_chars=5,
        page_scope=("matrix",),
        image_op_identities=("deskew",),
        read_identities=("textract",),
        max_cells_per_page=200,
        max_image_ops_per_cell=4,
        max_reads_per_cell=6,
        max_corpus_reads_per_cell=6,
        timeout_seconds=30,
    )
    document = _document(session, project, filename="matrix/scan.pdf", text="", text_source="ocr")
    page = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ).one()
    adapter = opened(tmp_path, RecordedService(scanned_response()))
    rescue = _rescue(
        tmp_path, adapter, page_image_sha256=hashlib.sha256(b"").hexdigest()
    )

    result = rescue_page(
        session,
        document=document,
        page=page,
        profile=profile,
        preprocessor=rescue,
    )

    assert result.rescued is True
    assert result.recovered_text == "PLACEHOLDER\n42"
    assert result.run.terminal_state == "rescued"
    # The provider applied none of the profile's declared operations, and the
    # run says so rather than crediting the reading with a deskew nothing ran.
    assert result.run.outcome_json["declared_ops"] == ["deskew"]
    assert result.run.outcome_json["applied_ops"] == []
