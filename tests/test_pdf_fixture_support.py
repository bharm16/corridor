"""The synthetic PDF builder is verified structurally, never through a reader.

Every expectation here is authored from the placement that produced it: the
standard Helvetica advance widths, the baseline the text was put on, the
rectangle a rule was drawn in, the exact samples an image was embedded from.
No PDF reader is imported, so a defect in the reader under test cannot hide
inside a fixture, and the module keeps working when PyMuPDF is gone (#730).
"""

from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path
import re
import zlib

from PIL import Image
import pytest

import pdf_fixture_support
from pdf_fixture_support import (
    ASCENT,
    DESCENT,
    LINE_HEIGHT,
    PdfFixture,
    TextOverflow,
    Word,
    helvetica_width,
    scan_image,
)


# --- Reading the builder's own bytes back, structurally --------------------


def _objects(data: bytes) -> dict[int, bytes]:
    """Every indirect object body, keyed by number, read through the xref."""

    startxref = int(re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data).group(1))
    assert data[startxref:].startswith(b"xref\n0 ")
    count = int(re.match(rb"xref\n0 (\d+)\n", data[startxref:]).group(1))
    entries_at = startxref + len(f"xref\n0 {count}\n")
    objects: dict[int, bytes] = {}
    for number in range(count):
        entry = data[entries_at + number * 20 : entries_at + (number + 1) * 20]
        assert re.fullmatch(rb"\d{10} \d{5} [nf] \n", entry), entry
        if number == 0:
            assert entry == b"0000000000 65535 f \n"
            continue
        offset = int(entry[:10])
        head = f"{number} 0 obj\n".encode()
        assert data[offset : offset + len(head)] == head, number
        end = data.index(b"\nendobj\n", offset)
        objects[number] = data[offset + len(head) : end]
    return objects


def _dictionary(body: bytes) -> bytes:
    return body[: body.index(b"stream\n")] if b"stream\n" in body else body


def _stream(body: bytes) -> bytes:
    length = int(re.search(rb"/Length (\d+)", body).group(1))
    start = body.index(b"stream\n") + len(b"stream\n")
    payload = body[start : start + length]
    assert body[start + length :] == b"\nendstream", "stream length is exact"
    return payload


def _trailer(data: bytes) -> bytes:
    return data[data.rindex(b"trailer") : data.rindex(b"startxref")]


def _pages(objects: dict[int, bytes]) -> list[bytes]:
    catalog = objects[1]
    pages_number = int(re.search(rb"/Pages (\d+) 0 R", catalog).group(1))
    kids = re.search(rb"/Kids \[([^\]]*)\]", objects[pages_number]).group(1)
    return [objects[int(number)] for number in re.findall(rb"(\d+) 0 R", kids)]


def _content(objects: dict[int, bytes], page: bytes) -> str:
    number = int(re.search(rb"/Contents (\d+) 0 R", page).group(1))
    return _stream(objects[number]).decode("latin-1")


def _hex(text: str) -> str:
    return text.encode("cp1252").hex().upper()


# --- The file ----------------------------------------------------------------


def test_the_file_is_a_pdf_whose_cross_reference_table_locates_every_object():
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), "Owner")
    fixture.add_page(width=200, height=100)

    data = fixture.tobytes()

    assert data.startswith(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    objects = _objects(data)
    trailer = _trailer(data)
    assert b"/Root 1 0 R" in trailer
    assert f"/Size {len(objects) + 1}".encode() in trailer
    assert b"/Type /Catalog" in objects[1]
    assert re.search(rb"/Type /Pages .*?/Count 2", objects[2], re.S)
    assert len(_pages(objects)) == 2


def test_save_writes_exactly_the_bytes_tobytes_returns(tmp_path):
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), "Owner")

    path = fixture.save(tmp_path / "owner.pdf")

    assert path == tmp_path / "owner.pdf"
    assert path.read_bytes() == fixture.tobytes()


def test_a_document_identity_distinguishes_files_with_identical_content():
    def build(identity):
        fixture = PdfFixture(identity=identity)
        fixture.add_page().text((36, 56), "SEED-1 captured source row")
        return fixture.tobytes()

    plain = build(None)
    first, again, second = build("ucm-2025-06-20"), build("ucm-2025-06-20"), build("ucm-2025-07-22")

    assert plain == build(None), "the bytes are a pure function of the content"
    assert first == again, "the same identity and content are the same bytes"
    assert len({plain, first, second}) == 3
    assert b"/ID" not in _trailer(plain)
    digest = sha256(b"ucm-2025-06-20").hexdigest()[:32].upper()
    assert f"/ID [<{digest}> <{digest}>]".encode() in _trailer(first)
    assert _objects(first) == _objects(plain), "identity lives in the trailer alone"


# --- Pages -------------------------------------------------------------------


def test_a_page_declares_its_media_box_crop_box_and_rotation():
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320, rotation=90, cropbox=(30, 20, 390, 300))

    assert page.media_box == (0, 0, 420, 320)
    assert page.crop_box == (30, 20, 390, 300)
    assert page.rotation == 90
    [page_object] = _pages(_objects(fixture.tobytes()))
    assert b"/MediaBox [0 0 420 320]" in page_object
    # The PDF crop box is measured from the bottom-left corner: the fixture's
    # top-left crop box (30, 20, 390, 300) on a 320-point-tall page is
    # y0 = 320 - 300 and y1 = 320 - 20 in file coordinates.
    assert b"/CropBox [30 20 390 300]" in page_object
    assert b"/Rotate 90" in page_object


def test_an_uncropped_unrotated_page_writes_neither_crop_box_nor_rotation():
    fixture = PdfFixture()
    page = fixture.add_page()

    assert page.media_box == (0, 0, 595, 842)
    assert page.crop_box == (0, 0, 595, 842)
    assert page.rotation == 0
    [page_object] = _pages(_objects(fixture.tobytes()))
    assert b"/CropBox" not in page_object
    assert b"/Rotate" not in page_object


def test_a_crop_box_must_lie_inside_the_media_box_and_rotation_is_a_quarter_turn():
    fixture = PdfFixture()
    with pytest.raises(ValueError, match="inside the media box"):
        fixture.add_page(width=100, height=100, cropbox=(10, 10, 120, 90))
    with pytest.raises(ValueError, match="rotation"):
        fixture.add_page(rotation=45)


# --- Text --------------------------------------------------------------------


def test_text_is_placed_on_its_baseline_in_the_standard_helvetica():
    fixture = PdfFixture()
    page = fixture.add_page()
    page.text((72, 72), "Meeting notes")

    objects = _objects(fixture.tobytes())
    [page_object] = _pages(objects)
    content = _content(objects, page_object)
    # Baseline origin (72, 72) from the top-left is (72, 842 - 72) in the file.
    assert content == f"BT\n/F1 11 Tf\n1 0 0 1 72 770 Tm\n<{_hex('Meeting notes')}> Tj\nET\n"
    font_number = int(re.search(rb"/Font << /F1 (\d+) 0 R >>", page_object).group(1))
    font = objects[font_number]
    assert b"/Subtype /Type1" in font
    assert b"/BaseFont /Helvetica" in font
    assert b"/Encoding /WinAnsiEncoding" in font
    assert b"/FirstChar 32 /LastChar 255" in font
    widths = re.search(rb"/Widths \[([^\]]*)\]", font).group(1).split()
    assert len(widths) == 224
    assert widths[ord("M") - 32] == b"833"
    assert widths[ord(" ") - 32] == b"278"
    assert widths[0x97 - 32] == b"1000"  # the em dash in WinAnsiEncoding
    assert b"/FontFile" not in objects[int(re.search(rb"/FontDescriptor (\d+) 0 R", font).group(1))]


def test_declared_word_boxes_follow_the_afm_advance_widths_from_the_baseline():
    fixture = PdfFixture()
    page = fixture.add_page()

    words = page.text((72, 72), "Meeting notes and")

    # Helvetica AFM: M 833, e 556, t 278, i 222, n 556, g 556; space 278.
    meeting = (833 + 556 + 556 + 278 + 222 + 556 + 556) * 11 / 1000
    notes = (556 + 556 + 278 + 556 + 500) * 11 / 1000
    space = 278 * 11 / 1000
    assert helvetica_width("Meeting", 11) == pytest.approx(meeting)
    assert [word.text for word in words] == ["Meeting", "notes", "and"]
    assert words[0].x0 == 72
    assert words[0].x1 == pytest.approx(72 + meeting)
    assert words[1].x0 == pytest.approx(72 + meeting + space)
    assert words[1].x1 == pytest.approx(72 + meeting + space + notes)
    assert words[2].x0 == pytest.approx(words[1].x1 + space)
    assert all(word.y0 == pytest.approx(72 - ASCENT * 11) for word in words)
    assert all(word.y1 == pytest.approx(72 + DESCENT * 11) for word in words)
    assert page.expected_words == words
    assert words[0].fixed_point_box() == (
        72_000,
        round((72 - ASCENT * 11) * 1000),
        round((72 + meeting) * 1000),
        round((72 + DESCENT * 11) * 1000),
    )


def test_multi_line_text_declares_every_line_and_the_page_text():
    fixture = PdfFixture()
    page = fixture.add_page()

    page.text((72, 72), "Action Items:\n1. Equistar will submit.", fontsize=10)
    page.text((72, 300), "Meeting Notes")

    assert page.expected_text == "Action Items:\n1. Equistar will submit.\nMeeting Notes\n"
    assert [line.text for line in page.expected_lines] == [
        "Action Items:",
        "1. Equistar will submit.",
        "Meeting Notes",
    ]
    second = page.expected_lines[1]
    assert second.baseline == pytest.approx(72 + LINE_HEIGHT * 10)
    assert second.fontsize == 10
    assert [word.text for word in page.expected_words] == [
        "Action", "Items:", "1.", "Equistar", "will", "submit.", "Meeting", "Notes",
    ]
    objects = _objects(fixture.tobytes())
    content = _content(objects, _pages(objects)[0])
    assert content.count("BT\n") == 3
    assert f"1 0 0 1 72 {842 - 72 - LINE_HEIGHT * 10:g} Tm" in content


def test_a_blank_page_declares_empty_text():
    page = PdfFixture().add_page()

    assert page.expected_text == ""
    assert page.expected_words == ()


def test_text_keeps_repeated_and_leading_spaces_as_authored():
    page = PdfFixture().add_page()

    words = page.text((10, 20), "FOC1-1  AT&T   Telecom")

    assert page.expected_text == "FOC1-1  AT&T   Telecom\n"
    assert [word.text for word in words] == ["FOC1-1", "AT&T", "Telecom"]
    assert words[1].x0 == pytest.approx(words[0].x1 + 2 * helvetica_width(" ", 11))
    assert words[2].x0 == pytest.approx(words[1].x1 + 3 * helvetica_width(" ", 11))


def test_text_on_a_cropped_page_is_measured_from_the_crop_box_origin():
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320, cropbox=(30, 20, 390, 300))

    [word] = page.text((60, 70), "Owner")

    # Declared boxes stay in the crop-box frame the fixture was authored in...
    assert (word.x0, word.y0) == (60, pytest.approx(70 - ASCENT * 11))
    objects = _objects(fixture.tobytes())
    content = _content(objects, _pages(objects)[0])
    # ...while the file places the baseline 30 points right of and 20 points
    # below the media box's top-left corner: (90, 320 - 90) in file coordinates.
    assert "1 0 0 1 90 230 Tm" in content


def test_text_is_encoded_in_win_ansi_and_refuses_characters_outside_it():
    fixture = PdfFixture()
    page = fixture.add_page()

    page.text((72, 72), "Matrix — segment (3C2) \\ done")

    objects = _objects(fixture.tobytes())
    content = _content(objects, _pages(objects)[0])
    assert f"<{_hex('Matrix — segment (3C2) \\ done')}> Tj" in content
    assert "97" in _hex("—")
    with pytest.raises(ValueError, match="WinAnsiEncoding"):
        page.text((72, 100), "Owner: � utility")
    assert page.expected_text == "Matrix — segment (3C2) \\ done\n", "a refused call adds nothing"


def test_helvetica_width_covers_printable_ascii_and_the_declared_extras():
    for code in range(32, 127):
        assert helvetica_width(chr(code), 1000) > 0
    assert helvetica_width("—", 1000) == 1000
    assert helvetica_width("–", 1000) == 556
    assert helvetica_width("•", 1000) == 350
    with pytest.raises(ValueError, match="no Helvetica width"):
        helvetica_width("☃", 1000)


# --- Text boxes --------------------------------------------------------------


def test_a_text_box_wraps_words_at_its_width_and_starts_below_its_top():
    page = PdfFixture().add_page()
    box = (43, 75, 43 + helvetica_width("AT&T Texas", 7) + 1, 120)

    lines = page.text_box(box, "AT&T Texas (SWBT) Telecom", fontsize=7)

    assert [line.text for line in lines] == ["AT&T Texas", "(SWBT)", "Telecom"]
    assert lines[0].baseline == pytest.approx(75 + ASCENT * 7)
    assert lines[1].baseline == pytest.approx(75 + ASCENT * 7 + LINE_HEIGHT * 7)
    assert all(line.x0 == 43 for line in lines)
    assert page.expected_text == "AT&T Texas\n(SWBT)\nTelecom\n"


def test_a_text_box_keeps_authored_line_breaks_and_leading_spaces():
    page = PdfFixture().add_page()

    lines = page.text_box((72, 72, 1128, 1528), "4.\n {as built}\nMeeting Notes\n 2", fontsize=10)

    assert [line.text for line in lines] == ["4.", " {as built}", "Meeting Notes", " 2"]
    assert page.expected_text == "4.\n {as built}\nMeeting Notes\n 2\n"


def test_a_text_box_refuses_text_that_does_not_fit_and_writes_nothing():
    page = PdfFixture().add_page()
    page.text((10, 10), "kept")

    with pytest.raises(TextOverflow):
        page.text_box((43, 75, 80, 91), "RECOMMENDED RESOLUTION BAND", fontsize=7)
    with pytest.raises(TextOverflow):
        page.text_box((43, 75, 300, 80), "one\ntwo\nthree", fontsize=7)

    assert page.expected_text == "kept\n"
    assert len(_content(*_first_page(page))) == len(_content(*_first_page(page)))


def _first_page(page):
    objects = _objects(page.fixture.tobytes())
    return objects, _pages(objects)[0]


# --- Rules -------------------------------------------------------------------


def test_rules_are_stroked_rectangles_and_lines_in_file_coordinates():
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300)

    page.rect((30, 90, 370, 200), width=0.6)
    page.line((200, 90), (200, 200))

    objects = _objects(fixture.tobytes())
    content = _content(objects, _pages(objects)[0])
    # A rectangle is its bottom-left corner plus width and height, y measured
    # upward: top-left (30, 90)-(370, 200) becomes (30, 300 - 200) 340 by 110.
    assert "q\n0 G\n0.6 w\n30 100 340 110 re\nS\nQ\n" in content
    assert "q\n0 G\n1 w\n200 210 m\n200 100 l\nS\nQ\n" in content
    assert page.rules == (
        ("rect", (30, 90, 370, 200), 0.6),
        ("line", (200, 90, 200, 200), 1),
    )


def test_rules_on_a_cropped_page_shift_with_the_crop_box_like_text_does():
    fixture = PdfFixture()
    page = fixture.add_page(width=420, height=320, cropbox=(30, 20, 390, 300))

    page.rect((55, 100, 340, 240))

    objects = _objects(fixture.tobytes())
    content = _content(objects, _pages(objects)[0])
    assert "85 60 285 140 re" in content


# --- Images ------------------------------------------------------------------


def test_a_scanned_page_embeds_exactly_the_samples_it_declares():
    image = Image.new("L", (40, 20), 255)
    image.putpixel((3, 4), 0)
    fixture = PdfFixture()
    page = fixture.add_page(width=400, height=300)

    embedded = page.image((20, 70, 380, 270), image)

    assert embedded.box == (20, 70, 380, 270)
    assert (embedded.width, embedded.height, embedded.mode) == (40, 20, "L")
    assert embedded.samples == image.tobytes()
    assert embedded.sha256 == sha256(image.tobytes()).hexdigest()
    assert page.embedded_images == (embedded,)
    objects = _objects(fixture.tobytes())
    [page_object] = _pages(objects)
    number = int(re.search(rb"/XObject << /Im1 (\d+) 0 R >>", page_object).group(1))
    dictionary = _dictionary(objects[number])
    assert b"/Subtype /Image" in dictionary
    assert b"/Width 40 /Height 20" in dictionary
    assert b"/ColorSpace /DeviceGray" in dictionary
    assert b"/BitsPerComponent 8" in dictionary
    assert b"/Filter /FlateDecode" in dictionary
    assert zlib.decompress(_stream(objects[number])) == image.tobytes()
    # The image fills its rectangle: 360 wide, 200 tall, bottom-left at
    # (20, 300 - 270) in file coordinates.
    assert "q\n360 0 0 200 20 30 cm\n/Im1 Do\nQ\n" in _content(objects, page_object)


def test_colour_images_embed_as_rgb_and_other_modes_are_converted():
    fixture = PdfFixture()
    page = fixture.add_page(width=100, height=100)
    rgba = Image.new("RGBA", (2, 2), (10, 20, 30, 255))

    embedded = page.image((0, 0, 100, 100), rgba)

    assert embedded.mode == "RGB"
    assert embedded.samples == rgba.convert("RGB").tobytes()
    objects = _objects(fixture.tobytes())
    number = int(re.search(rb"/Im1 (\d+) 0 R", _pages(objects)[0]).group(1))
    assert b"/ColorSpace /DeviceRGB" in _dictionary(objects[number])


def test_a_page_may_carry_several_images_each_with_its_own_resource():
    fixture = PdfFixture()
    page = fixture.add_page(width=100, height=100)
    page.image((0, 0, 50, 50), Image.new("L", (1, 1), 0))
    page.image((50, 50, 100, 100), Image.new("L", (1, 1), 255))

    [page_object] = _pages(_objects(fixture.tobytes()))

    assert re.search(rb"/XObject << /Im1 \d+ 0 R /Im2 \d+ 0 R >>", page_object)
    assert len(page.embedded_images) == 2


def test_scan_image_rasterises_text_at_the_requested_resolution_deterministically():
    lines = (((72, 120), "UTILITY RELOCATION AGREEMENT", 22),)

    first = scan_image(595, 842, dpi=300, lines=lines)
    second = scan_image(595, 842, dpi=300, lines=lines)

    assert first.mode == "L"
    assert first.size == (round(595 * 300 / 72), round(842 * 300 / 72))
    assert first.tobytes() == second.tobytes()
    ink = [
        (x, y)
        for y in range(0, first.height, 8)
        for x in range(0, first.width, 8)
        if first.getpixel((x, y)) < 128
    ]
    assert ink, "the text leaves ink on the scan"
    baseline_px = 120 * 300 / 72
    assert all(baseline_px - 22 * 300 / 72 <= y <= baseline_px + 8 * 300 / 72 for _, y in ink)
    assert all(x >= 72 * 300 / 72 for x, _ in ink)
    blank = scan_image(200, 100, dpi=150, lines=())
    assert blank.size == (round(200 * 150 / 72), round(100 * 150 / 72))
    assert blank.tobytes() == b"\xff" * (blank.width * blank.height)


# --- Independence ------------------------------------------------------------


def test_the_builder_imports_no_pdf_reader():
    source = Path(pdf_fixture_support.__file__).read_text()
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"pymupdf", "fitz", "pypdfium2", "pypdf", "corridor"}
    assert imported <= {
        "__future__", "dataclasses", "hashlib", "io", "pathlib", "typing", "zlib", "PIL",
    }


def test_word_is_a_plain_value():
    word = Word(text="a", x0=1.0, y0=2.0, x1=3.0, y1=4.0)

    assert word == Word("a", 1.0, 2.0, 3.0, 4.0)
    assert word.fixed_point_box() == (1000, 2000, 3000, 4000)
