"""Native and OCR token layers carry coordinates, origin, and engine pinning."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from corridor.render_profiles import render_page_derivative
from corridor.token_layers import (
    OcrRequest,
    TesseractEngine,
    Token,
    TokenLayer,
    extract_native_token_layer,
    render_point_to_pdf,
)

from pdf_fixture_support import PdfFixture, scan_image


def _native_fixture() -> PdfFixture:
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320)
    page.text((60, 80), "Utility Owner AT&T")
    page.text((60, 140), "STA 1149+00")
    return fixture


def _scanned_fixture() -> PdfFixture:
    scan = scan_image(595, 842, dpi=300, lines=(((72, 120), "CENTERPOINT ENERGY", 22),))
    fixture = PdfFixture()
    fixture.add_page().image((0, 0, 595, 842), scan)
    return fixture


def test_native_layer_has_positioned_tokens_and_no_confidence(tmp_path):
    fixture = _native_fixture()
    pdf = fixture.save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        layer = extract_native_token_layer(
            document[0], page_no=1, source_sha256=digest
        )

    assert layer.origin == "native"
    assert layer.identity.engine == "pymupdf"
    assert layer.identity.adapter_version
    assert layer.source_sha256 == digest
    declared = fixture.pages[0].expected_words
    assert [token.raw_text for token in layer.tokens] == [
        word.text for word in declared
    ]
    for token, word in zip(layer.tokens, declared, strict=True):
        assert token.origin == "native"
        assert token.confidence is None  # native readings are not estimates
        assert token.normalized_text == token.normalized_text.strip()
        x0, y0, x1, y1 = word.fixed_point_box()
        # Horizontal extents follow the AFM advance widths the fixture also
        # writes into its font, so the reader agrees to the thousandth of a
        # point. Vertical extents are the reader's substitute-face convention
        # against the fixture's AFM bounding box: within two points at 11 pt.
        assert abs(token.polygon_pdf.x0 - x0) <= 1
        assert abs(token.polygon_pdf.x1 - x1) <= 1
        assert abs(token.polygon_pdf.y0 - y0) <= 2_000
        assert abs(token.polygon_pdf.y1 - y1) <= 2_000
    assert layer.quality["token_count"] == len(layer.tokens) == len(declared)
    assert layer.quality["rotation_degrees"] == 0


def test_ocr_layer_carries_confidence_pdf_and_render_polygons_and_full_pinning(
    tmp_path,
):
    pdf = _scanned_fixture().save(tmp_path / "scanned.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="ocr_layout",
        output_dir=tmp_path / "renders",
    )
    engine = TesseractEngine()
    layer = engine.recognize(
        OcrRequest(
            page_no=1,
            source_sha256=digest,
            image_path=derivative.artifact_path,
            derivative=derivative,
        )
    )

    assert layer.origin == "ocr"
    assert layer.tokens, "the scanned page yields OCR tokens"
    assert "CENTERPOINT" in " ".join(t.raw_text for t in layer.tokens).upper()

    # Full engine pinning (AC1): binary version, tessdata digests, parameters.
    identity = layer.identity
    assert identity.engine == "tesseract"
    assert identity.engine_version  # tesseract executable version
    assert identity.language == "eng"
    assert identity.oem is not None and identity.psm is not None
    assert identity.dpi == derivative.dpi
    assert identity.render_profile_id == derivative.profile_id
    assert identity.traineddata.get("eng.traineddata")  # filename -> sha256
    assert all(len(d) == 64 for d in identity.traineddata.values())

    for token in layer.tokens:
        assert token.origin == "ocr"
        assert 0.0 <= token.confidence <= 1.0  # a signal, not a probability
        assert token.polygon_render is not None
        assert token.polygon_pdf.x1 > token.polygon_pdf.x0
    assert layer.quality["mean_confidence"] is not None


def test_pdf_polygon_maps_inside_the_page_via_the_render_transform(tmp_path):
    fixture = _scanned_fixture()
    pdf = fixture.save(tmp_path / "scanned.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    _, _, page_width, page_height = fixture.pages[0].media_box
    derivative = render_page_derivative(
        pdf_path=pdf,
        page_number=1,
        profile_name="ocr_layout",
        output_dir=tmp_path / "renders",
    )
    # The render transform's PDF side is fixed-point thousandths, so a mid-raster
    # point inverts to within the page's bounds scaled by 1000.
    x, y = render_point_to_pdf(
        derivative, (derivative.raster_width / 2, derivative.raster_height / 2)
    )
    assert -1000 <= x <= (page_width + 1) * 1000
    assert -1000 <= y <= (page_height + 1) * 1000


def test_canonical_serialization_is_deterministic(tmp_path):
    pdf = _native_fixture().save(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        layer = extract_native_token_layer(
            document[0], page_no=1, source_sha256=digest
        )
    reparsed = TokenLayer.model_validate_json(layer.canonical_bytes())
    assert reparsed.content_sha256 == layer.content_sha256
    assert reparsed.canonical_bytes() == layer.canonical_bytes()
