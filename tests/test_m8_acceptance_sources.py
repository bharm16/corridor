"""What an M8 acceptance capture reads from its source PDFs, checked against
what the retained capture recorded and against fixtures that declare their own
geometry (#740).

The NHHIP capture under tests/fixtures/m8_acceptance froze every source's page
sizes and bound each RID-index declaration to one visual row; those readings
were made with the reader #727 retires, so they are the comparison here. The
synthetic fixtures pin the two properties a wrong reading would miss: a page's
displayed size follows its crop box and its rotation, and a row is the words
that share a baseline, in reading order, whatever order the file drew them in.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path

from pdf_fixture_support import PdfFixture

from corridor.m8_acceptance import (
    _date_spellings,
    _filename_token,
    _page_dimensions,
    _rid_rows,
)

FIXTURE = Path(__file__).parent / "fixtures" / "m8_acceptance" / "nhhip-five-revision-v1"


def test_page_dimensions_reproduce_what_the_retained_capture_recorded():
    fixture = json.loads((FIXTURE / "fixture.json").read_text())["content"]
    sources = {source["registry_id"]: source for source in (fixture["rid_index"], *fixture["sources"])}

    for observation in fixture["observations"]:
        path = FIXTURE / sources[observation["registry_id"]]["fixture_relpath"]
        assert dict(_page_dimensions(path)) == observation["page_dimensions"], observation["registry_id"]
        assert sum(_page_dimensions(path).values()) == observation["pages"]


def test_page_dimensions_follow_the_crop_box_and_the_rotation():
    fixture = PdfFixture()
    fixture.add_page(595, 842)
    fixture.add_page(595, 842, rotation=90)
    fixture.add_page(595, 842, rotation=270)
    fixture.add_page(600, 800, cropbox=(50, 40, 550, 700))
    fixture.add_page(600, 800, cropbox=(50, 40, 550, 700), rotation=180)
    path = FIXTURE.parent / "page-dimensions.pdf"
    try:
        fixture.save(path)
        assert _page_dimensions(path) == Counter(
            {"595.0x842.0": 1, "842.0x595.0": 2, "500.0x660.0": 2}
        )
    finally:
        path.unlink(missing_ok=True)


def test_rid_rows_bind_each_declared_filename_to_its_replacement_date():
    fixture = json.loads((FIXTURE / "fixture.json").read_text())["content"]
    rid_index = FIXTURE / fixture["rid_index"]["fixture_relpath"]
    rows = {page_no: _rid_rows(rid_index, page_no) for page_no in (1, 2, 3, 4, 5)}

    assert {page_no: len(page_rows) for page_no, page_rows in rows.items()} == {
        1: 64, 2: 70, 3: 74, 4: 27, 5: 0
    }
    assert (
        "Utility Conflict Matrix (Updated 7/22/2025) "
        "nhhip-seg3c2-utilities-inventory-7-22-2025.pdf 7/22/2025 7/22/2025 "
        "Replaced on 10/31/2025"
    ) in rows[4]
    # Matched the way _verify_rid_declarations matches: the predecessor's
    # filename token inside the row's, and one spelling of the date on it.
    for edge in fixture["supersession_edges"]:
        predecessor = next(
            source
            for source in fixture["sources"]
            if source["registry_id"] == edge["predecessor_registry_id"]
        )
        token = _filename_token(predecessor["filename"])
        [row] = [row for row in rows[edge["source_page"]] if token in _filename_token(row)]
        replaced_on = date.fromisoformat(edge["replacement_date"])
        assert any(spelling in row.casefold() for spelling in _date_spellings(replaced_on))


def test_rid_rows_are_words_sharing_a_baseline_in_reading_order(tmp_path):
    fixture = PdfFixture()
    page = fixture.add_page(612, 792)
    page.text((300, 100), "second", fontsize=10)
    page.text((100, 101.5), "first", fontsize=10)
    page.text((100, 120), "next row", fontsize=10)
    page.text((450, 120), "end", fontsize=10)
    path = tmp_path / "rows.pdf"
    fixture.save(path)

    assert _rid_rows(path, 1) == ("first second", "next row end")
    assert _rid_rows(path, 2) == ()
    assert _rid_rows(path, 0) == ()
