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
from corridor.db import Session as DbSession, engine
from corridor.models import DocPage, Document, Project
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
    processing_failure,
    read_scanned_page,
    record_unconfirmed_readings,
    routed_textract_regions,
    scanned_cell_key,
)
from corridor.scanned_reading import TextractRescue
from corridor.principals import HumanPrincipal
from corridor.token_layers import EngineIdentity, READER_ENGINE, Token, TokenLayer
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
from corridor_pdf_reader.textract_adapter.boundary import (
    TextractProcessingFailure,
    open_boundary,
)
from corridor_pdf_reader.textract_adapter.identity import RequestConfiguration
from corridor_pdf_reader.textract_adapter.records import (
    PROVIDER_POSTURE,
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

    values = classify_region_values(
        reading,
        regions=routed,
        native_layer=native_layer(("PLACEHOLDER", (100, 100, 180, 112))),
        provenance={"rendition_sha256": RENDITION_SHA, "page_number": 1},
    )

    first, second = values
    assert (first.value, first.value_source, first.state) == (
        "PLACEHOLDER",
        "native_glyphs",
        "source_verified",
    )
    assert first.locator == PdfRect(x0=100_000, y0=100_000, x1=180_000, y1=112_000)
    assert first.confidence is None
    # The cell Textract read 'PLACEH0LDER' out of is the same cell; only the
    # characters differ, and the document's own win.
    assert (second.value, second.state) == ("42", "unconfirmed")


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

    (value,) = classify_region_values(
        reading,
        regions=routed_textract_regions(reader_decision()),
        native_layer=incumbent,
        provenance={},
    )

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
        native_layer=None,
        provenance={},
    )

    assert [value.value for value in values] == ["in"]


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
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = DbSession(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


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
