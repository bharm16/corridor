"""Deterministic PDF page inventory and routing decisions."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from corridor.page_inventory import (
    OCR_ENGINE,
    READER_COORDINATE_FRAME,
    READER_ROUTER_VERSION,
    TEXTRACT_ENGINE,
    inventory_page,
    read_page_facts,
    read_reader_page_inventories,
    reader_page_inventories,
    route_page,
    route_reader_page,
)
from corridor_pdf_reader.execution import pdfium_entered

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


# ---- the reader-backed inventory and routing (#734) -------------------------


def _reader_facts(fixture: PdfFixture, tmp_path) -> dict:
    """The reader's own reading of the fixture, in this process.

    The executor's spawned process is exercised once, below; every other case
    reads in process so a routing rule is not paid for with a fork.
    """

    directory = Path(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    return read_page_facts(fixture.save(directory / "reader.pdf"))


def test_the_reader_backed_inventory_records_the_readers_visible_layers(tmp_path):
    """Every inventory field comes from the reader's facts, in its own frame."""

    scan = scan_image(200, 100, dpi=150, lines=(((20, 50), "SCANNED", 11),))
    fixture = PdfFixture()
    drawn = fixture.add_page(width=400, height=300)
    drawn.text((40, 60), "Utility Owner")
    drawn.rect((30, 90, 370, 200))
    drawn.line((200, 90), (200, 200))
    drawn.line((30, 140), (370, 140))
    drawn.text((45, 120), "Owner")
    drawn.text((215, 120), "Conflict")
    embedded = drawn.image((30, 210, 200, 280), scan)

    (inventory,) = reader_page_inventories(_reader_facts(fixture, tmp_path)).values()

    assert inventory.schema_version == "corridor.pdf-page-inventory.v1"
    assert inventory.coordinate_frame == READER_COORDINATE_FRAME
    assert inventory.native_glyph_count == len(
        "".join(word.text for word in drawn.expected_words)
    )
    assert inventory.native_text_length > 0
    assert 0 < inventory.native_glyph_coverage < 1
    assert inventory.vector_density > 0
    assert inventory.unicode_quality == 1
    assert inventory.suspicious_text_signals == ()
    assert inventory.rotation_degrees == 0
    # The crop box is the origin of the reader's frame, and the media box is
    # recorded in that same frame rather than in a second one.
    assert (inventory.boxes.crop.x0, inventory.boxes.crop.y0) == (0, 0)
    assert inventory.boxes.crop.width == 400_000
    assert inventory.boxes.crop.height == 300_000
    assert inventory.boxes.media == inventory.boxes.crop
    [image_region] = inventory.image_regions
    assert (
        image_region.box.x0,
        image_region.box.y0,
        image_region.box.x1,
        image_region.box.y1,
    ) == tuple(round(value * 1000) for value in embedded.box)
    assert 0 < inventory.embedded_image_coverage < 1
    assert inventory.native_regions
    assert inventory.table_regions


def test_the_reader_backed_inventory_reports_a_rotated_page_as_it_is_displayed(
    tmp_path,
):
    """A rotated page's boxes, glyphs, images and tables share one frame.

    The incumbent does not: its glyph and image boxes stay unrotated while
    `find_tables` reports the displayed box, so a rotated page's inventory
    mixes two frames. The reader's facts are all displayed-crop, which is also
    the frame the render derivative an OCR region is cut from is in.
    """

    scan = scan_image(100, 60, dpi=150, lines=(((10, 30), "SCAN", 9),))
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300, rotation=90)
    page.text((40, 60), "Utility Owner")
    page.image((30, 210, 200, 280), scan)

    (inventory,) = reader_page_inventories(_reader_facts(fixture, tmp_path)).values()

    assert inventory.rotation_degrees == 90
    assert inventory.boxes.crop.width == 300_000
    assert inventory.boxes.crop.height == 400_000
    [image_region] = inventory.image_regions
    # The declared box is unrotated; displayed, a 90-degree page maps
    # (x, y) to (height - y, x).
    assert (
        image_region.box.x0,
        image_region.box.y0,
        image_region.box.x1,
        image_region.box.y1,
    ) == (300_000 - 280_000, 30_000, 300_000 - 210_000, 200_000)


def test_the_reader_backed_route_decides_as_the_incumbent_router_and_names_textract(
    tmp_path,
):
    """The rules are the incumbent's; only the facts and the engine differ."""

    scan = scan_image(200, 100, dpi=150, lines=(((20, 50), "SCANNED TABLE", 11),))
    image_only = PdfFixture()
    image_only.add_page(width=400, height=300).image((0, 0, 400, 300), scan)
    (image_inventory,) = reader_page_inventories(
        _reader_facts(image_only, tmp_path / "image")
    ).values()
    image_decision = route_reader_page(image_inventory)

    assert image_inventory.embedded_image_coverage == 1
    assert image_decision.page_mode == "ocr"
    assert [region.mode for region in image_decision.regions] == ["ocr"]
    assert image_decision.ocr_engine == "textract"
    assert image_decision.router_version == READER_ROUTER_VERSION
    # ADR-0094 makes Textract the OCR provider; the configuration of the call
    # belongs to the adapter that makes it (#739), and this route fixes none.
    assert image_decision.ocr_configuration == {}

    mixed = PdfFixture()
    mixed_page = mixed.add_page(width=400, height=300)
    mixed_page.text((20, 30), "Native page heading")
    mixed_page.image((20, 70, 380, 270), scan)
    (mixed_inventory,) = reader_page_inventories(
        _reader_facts(mixed, tmp_path / "mixed")
    ).values()
    mixed_decision = route_reader_page(mixed_inventory)

    assert mixed_decision.page_mode == "both"
    assert {region.mode for region in mixed_decision.regions} == {"native", "ocr"}
    # Same inventory, same regions: the reader route changes who reads an OCR
    # region, never which regions are sent.
    assert route_page(mixed_inventory).regions == mixed_decision.regions
    assert route_page(mixed_inventory).ocr_engine == OCR_ENGINE != TEXTRACT_ENGINE

    native = PdfFixture()
    native.add_page(width=400, height=300).text((40, 60), "OK")
    (native_inventory,) = reader_page_inventories(
        _reader_facts(native, tmp_path / "native")
    ).values()
    native_decision = route_reader_page(native_inventory)

    assert native_decision.page_mode == "native"
    assert native_decision.reason == "clean_native_text"
    assert native_decision.structural_triggers == ()


def test_a_table_region_the_reader_read_no_cells_from_is_recorded_not_acted_on(
    tmp_path,
):
    """The structural trigger #739 consumes, recorded and nothing more.

    A drawn grid whose contents are a scan is a table region the reader finds
    and reads nothing out of. That is the structural reason to send a
    text-layer page to Textract; it is recorded on the routing decision here,
    and acting on it is #739's.
    """

    scan = scan_image(300, 100, dpi=150, lines=(((20, 50), "SCANNED CELLS", 11),))
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300)
    page.text((30, 40), "Utility Conflict Matrix")
    page.rect((30, 60, 370, 260))
    page.line((200, 60), (200, 260))
    page.line((30, 160), (370, 160))
    page.image((32, 62, 368, 258), scan)

    (inventory,) = reader_page_inventories(_reader_facts(fixture, tmp_path)).values()
    decision = route_reader_page(inventory)

    assert [region.cells_with_text for region in inventory.table_regions] == [0]
    [trigger] = decision.structural_triggers
    assert trigger.trigger == "table_region_without_table_read"
    assert trigger.engine == "textract"
    assert trigger.region_id == inventory.table_regions[0].region_id
    # Recorded, not acted on: no region was added and no mode was changed.
    assert decision.regions == route_page(inventory).regions


def test_the_reader_backed_inventory_reads_the_document_in_one_isolated_process(
    tmp_path,
):
    """The executor contract #733 chose, reused: ingest runs in a threaded
    worker, so PDFium is entered in a spawned process, not in this one."""

    fixture = PdfFixture()
    fixture.add_page(width=400, height=300).text((40, 60), "First page")
    fixture.add_page(width=400, height=300).text((40, 60), "Second page")
    path = fixture.save(tmp_path / "two-pages.pdf")

    inventories = read_reader_page_inventories(path)

    assert sorted(inventories) == [1, 2]
    assert not pdfium_entered()
    assert all(
        inventory.coordinate_frame == READER_COORDINATE_FRAME
        for inventory in inventories.values()
    )
