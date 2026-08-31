"""Deterministic PDF page inventory and routing decisions."""

from __future__ import annotations

import pymupdf

from corridor.page_inventory import inventory_page, route_page


def test_inventory_records_visible_layers_rotation_boxes_and_table_evidence(capsys):
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.insert_text((40, 60), "Utility Owner")
    page.draw_rect(pymupdf.Rect(30, 90, 370, 200))
    page.draw_line((200, 90), (200, 200))
    page.draw_line((30, 140), (370, 140))
    page.insert_text((45, 120), "Owner")
    page.insert_text((215, 120), "Conflict")
    page.insert_text((45, 175), "AT&T")
    page.insert_text((215, 175), "UC-1")

    inventory = inventory_page(page)

    assert inventory.schema_version == "corridor.pdf-page-inventory.v1"
    assert inventory.native_glyph_count > 0
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
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.insert_text((40, 60), "OK")

    decision = route_page(inventory_page(page))

    assert decision.page_mode == "native"
    assert [region.mode for region in decision.regions] == ["native"]
    assert decision.reason == "clean_native_text"
    document.close()


def test_image_only_and_mixed_pages_route_by_region():
    source = pymupdf.open()
    source_page = source.new_page(width=200, height=100)
    source_page.insert_text((20, 50), "SCANNED TABLE")
    pixmap = source_page.get_pixmap(dpi=150)
    source.close()

    image_only = pymupdf.open()
    image_page = image_only.new_page(width=400, height=300)
    image_page.insert_image(image_page.rect, pixmap=pixmap)
    image_decision = route_page(inventory_page(image_page))
    assert image_decision.page_mode == "ocr"
    assert [region.mode for region in image_decision.regions] == ["ocr"]

    mixed = pymupdf.open()
    mixed_page = mixed.new_page(width=400, height=300)
    mixed_page.insert_text((20, 30), "Native page heading")
    mixed_page.insert_image(pymupdf.Rect(20, 70, 380, 270), pixmap=pixmap)
    mixed_decision = route_page(inventory_page(mixed_page))
    assert mixed_decision.page_mode == "both"
    assert {region.mode for region in mixed_decision.regions} == {"native", "ocr"}

    image_only.close()
    mixed.close()


def test_suspicious_unicode_routes_both_instead_of_trusting_native_glyphs():
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    # A replacement character is the explicit signal that native decoding lost
    # content. The router keeps the native layer and asks OCR for a second read.
    page.insert_text((40, 60), "Owner: \ufffd utility")

    inventory = inventory_page(page, native_text="Owner: \ufffd utility")
    decision = route_page(inventory)

    assert "replacement_character" in inventory.suspicious_text_signals
    assert decision.page_mode == "both"
    assert [region.mode for region in decision.regions] == ["both"]
    document.close()
