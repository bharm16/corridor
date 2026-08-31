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


def _pdf(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=420, height=320)
    page.insert_text((60, 80), "Utility Owner AT&T")
    page.insert_text((60, 140), "STA 1149+00")
    document.save(path)
    document.close()
    return path


def _scanned_pdf(path: Path) -> Path:
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 120), "CENTERPOINT ENERGY", fontsize=22)
    pixmap = page.get_pixmap(dpi=300)
    source.close()
    scanned = pymupdf.open()
    out = scanned.new_page()
    out.insert_image(out.rect, pixmap=pixmap)
    scanned.save(path)
    scanned.close()
    return path


def test_native_layer_has_positioned_tokens_and_no_confidence(tmp_path):
    pdf = _pdf(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        layer = extract_native_token_layer(
            document[0], page_no=1, source_sha256=digest
        )

    assert layer.origin == "native"
    assert layer.identity.engine == "pymupdf"
    assert layer.identity.adapter_version
    assert layer.source_sha256 == digest
    assert layer.tokens, "a text page yields native tokens"
    words = {token.raw_text for token in layer.tokens}
    assert "Utility" in words and "1149+00" in words
    for token in layer.tokens:
        assert token.origin == "native"
        assert token.confidence is None  # native readings are not estimates
        assert token.polygon_pdf.x1 > token.polygon_pdf.x0
        assert token.polygon_pdf.y1 > token.polygon_pdf.y0
        assert token.normalized_text == token.normalized_text.strip()
    assert layer.quality["token_count"] == len(layer.tokens)
    assert layer.quality["rotation_degrees"] == 0


def test_ocr_layer_carries_confidence_pdf_and_render_polygons_and_full_pinning(
    tmp_path,
):
    pdf = _scanned_pdf(tmp_path / "scanned.pdf")
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
    pdf = _scanned_pdf(tmp_path / "scanned.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        page_rect = document[0].rect
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
    assert -1000 <= x <= (page_rect.width + 1) * 1000
    assert -1000 <= y <= (page_rect.height + 1) * 1000


def test_canonical_serialization_is_deterministic(tmp_path):
    pdf = _pdf(tmp_path / "native.pdf")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        layer = extract_native_token_layer(
            document[0], page_no=1, source_sha256=digest
        )
    reparsed = TokenLayer.model_validate_json(layer.canonical_bytes())
    assert reparsed.content_sha256 == layer.content_sha256
    assert reparsed.canonical_bytes() == layer.canonical_bytes()
