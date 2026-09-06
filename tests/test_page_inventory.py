"""Deterministic PDF page inventory and routing decisions."""

from __future__ import annotations

import pymupdf

from corridor.page_inventory import inventory_page, route_page

from pdf_fixture_support import PdfFixture, scan_image


def _open(fixture: PdfFixture) -> pymupdf.Document:
    """The reader under inventory reads the fixture's bytes; nothing else does."""

    return pymupdf.open(stream=fixture.tobytes(), filetype="pdf")


def test_inventory_records_visible_layers_rotation_boxes_and_table_evidence(capsys):
    fixture = PdfFixture()
    drawn = fixture.add_page(width=400, height=300)
    drawn.text((40, 60), "Utility Owner")
    drawn.rect((30, 90, 370, 200))
    drawn.line((200, 90), (200, 200))
    drawn.line((30, 140), (370, 140))
    drawn.text((45, 120), "Owner")
    drawn.text((215, 120), "Conflict")
    drawn.text((45, 175), "AT&T")
    drawn.text((215, 175), "UC-1")
    document = _open(fixture)
    page = document[0]

    inventory = inventory_page(page)

    assert inventory.schema_version == "corridor.pdf-page-inventory.v1"
    assert inventory.native_text_length == len(drawn.expected_text.strip())
    assert inventory.native_glyph_count == len(
        "".join(word.text for word in drawn.expected_words)
    )
    assert 0 < inventory.native_glyph_coverage < 1
    assert inventory.embedded_image_coverage == 0
    assert inventory.vector_density > 0
    assert inventory.unicode_quality == 1
    assert inventory.suspicious_text_signals == ()
    assert inventory.rotation_degrees == 0
    assert inventory.boxes.media.width == 400_000
    assert inventory.boxes.crop.height == 300_000
    assert inventory.table_regions
    assert capsys.readouterr().out == ""
    document.close()


def test_short_clean_native_text_is_not_sent_to_ocr_by_character_count():
    fixture = PdfFixture()
    fixture.add_page(width=400, height=300).text((40, 60), "OK")
    document = _open(fixture)
    page = document[0]

    decision = route_page(inventory_page(page))

    assert decision.page_mode == "native"
    assert [region.mode for region in decision.regions] == ["native"]
    assert decision.reason == "clean_native_text"
    document.close()


def test_image_only_and_mixed_pages_route_by_region():
    scan = scan_image(200, 100, dpi=150, lines=(((20, 50), "SCANNED TABLE", 11),))

    image_only_fixture = PdfFixture()
    image_only_fixture.add_page(width=400, height=300).image((0, 0, 400, 300), scan)
    image_only = _open(image_only_fixture)
    image_inventory = inventory_page(image_only[0])
    image_decision = route_page(image_inventory)
    assert image_inventory.embedded_image_coverage == 1
    assert image_decision.page_mode == "ocr"
    assert [region.mode for region in image_decision.regions] == ["ocr"]

    mixed_fixture = PdfFixture()
    mixed_drawn = mixed_fixture.add_page(width=400, height=300)
    mixed_drawn.text((20, 30), "Native page heading")
    embedded = mixed_drawn.image((20, 70, 380, 270), scan)
    mixed = _open(mixed_fixture)
    mixed_inventory = inventory_page(mixed[0])
    mixed_decision = route_page(mixed_inventory)
    [image_region] = mixed_inventory.image_regions
    assert (
        image_region.box.x0,
        image_region.box.y0,
        image_region.box.x1,
        image_region.box.y1,
    ) == tuple(round(value * 1000) for value in embedded.box)
    assert mixed_decision.page_mode == "both"
    assert {region.mode for region in mixed_decision.regions} == {"native", "ocr"}

    image_only.close()
    mixed.close()


def test_suspicious_unicode_routes_both_instead_of_trusting_native_glyphs():
    fixture = PdfFixture()
    # A replacement character is the explicit signal that native decoding lost
    # content. The router keeps the native layer and asks OCR for a second read.
    # The page carries ordinary glyphs; the suspect reading arrives as the
    # native text the caller already extracted, which is where the signal lives.
    fixture.add_page(width=400, height=300).text((40, 60), "Owner: ? utility")
    document = _open(fixture)
    page = document[0]

    inventory = inventory_page(page, native_text="Owner: \ufffd utility")
    decision = route_page(inventory)

    assert "replacement_character" in inventory.suspicious_text_signals
    assert decision.page_mode == "both"
    assert [region.mode for region in decision.regions] == ["both"]
    document.close()
