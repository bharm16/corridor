import json
import re
from collections import Counter
from pathlib import Path

import pytest

from corridor.geometry import (
    cell_text,
    dedupe_hint,
    page_tables,
    readings_agree,
    row_quote,
    row_to_fields,
    split_station_offset,
)
from corridor.verify import normalize, quote_appears_on

# The two real layouts on Project A, eight months apart. Since #63 the
# mapping arrives from outside this module — a model reads the printed
# headers and says what they mean — so these are what it returns for the
# two layouts, and the same field sits at a different index in each.
CONFLICT_MAPPING = {
    0: "utility_id", 1: "external_org", 2: "utility_type", 3: "size",
    4: "material", 5: "oh_ug", 6: "baseline", 7: "orientation",
    8: "alignment", 9: "location_start", 10: "location_end",
    11: "station_from", 12: "offset_from", 13: "station_to", 14: "offset_to",
    15: "offset_side", 16: "potential_conflict", 17: "sue_level", 18: "notes",
}
INVENTORY_MAPPING = {
    0: "utility_id", 1: "utility_type", 2: "external_org", 3: "oh_ug",
    4: "size", 5: "orientation", 6: "baseline", 7: "station_from",
    8: "offset_from", 9: "station_to", 10: "offset_to", 11: "offset_side",
}

_UNUSED_CONFLICT_HEADERS = [
    "Utility ID", "Utility Owner", "Utility Type", "Size\n(inches,\nstrands)",
    "Material", "OH/ UG", "Baseline\nIH 10 /IH 69",
    "Parallel, Crossing\nPerpendicular\n(to Baseline)", "Alignment\n(along Roadway)",
    "Location Start\n(South or West)", "Location End\n(North or East)",
    "Start Station", "Start\nOffset", "End Station", "End Offset",
    "Offset\nL/R", "Potential\nConflict\n(Yes, No,\nAbandoned)",
    "SUE Level\n(A,B,C,D)", "Notes",
]
_UNUSED_INVENTORY_HEADERS = [
    "UTILITY ID\nNO.", "FACILITY TYPE", "OWNER", "UG/OH", "SIZE & MATERIAL",
    "LONGITUDINAL/\nCROSSING", "BASELINE", "START STA", "START STA\nOFFSET",
    "END STA", "END STA\nOFFSET", "L/R",
]












def test_row_fields_are_read_by_header_not_position():
    conflict_row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG",
                    "IH 69", "Crossing", "IH 69", "Schwartz Street",
                    "Eastex Freeway FR SB", "1149+00", "303", "1153+17", "309",
                    "L/R", "Y", "B", ""]
    inventory_row = ["WW1**", "Wastewater", "City of Houston", "UG", '18" PVC',
                     "Crossing", "IH10", "1143+44.01", "NA", "NA", "NA",
                     "Crossing CL"]

    a = row_to_fields(conflict_row, CONFLICT_MAPPING)
    b = row_to_fields(inventory_row, INVENTORY_MAPPING)

    assert a["external_org"] == "AT&T Texas (SWBT)"
    assert a["station_from"] == "1149+00"
    assert b["external_org"] == "City of Houston"
    assert b["station_from"] == "1143+44.01"
    # Same field, opposite column index in the two layouts.
    assert a["utility_type"] == "Telecom" and b["utility_type"] == "Wastewater"


def test_empty_cells_are_omitted_not_stored_as_blanks():
    row = ["FOC1-1", "AT&T", "Telecom", "", "", "", "", "", "", "", "",
           "1149+00", "", "", "", "", "", "", ""]
    fields = row_to_fields(row, CONFLICT_MAPPING)
    assert "size" not in fields
    assert fields["station_from"] == "1149+00"


def test_the_quote_is_the_whole_row():
    row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG"]
    assert row_quote(row) == "FOC1-1 AT&T Texas (SWBT) Telecom FOC UG"


def test_the_word_box_reading_wins_when_the_two_disagree():
    """Word boxes are the page's own tokenization; spans are not.

    `table.extract()` is sloppy at cell boundaries in both directions — it
    drops a leading character (`Rothwell` -> `othwell`) and absorbs the
    first character of the next cell (`Canal Street` -> `Canal Street o`).
    Word boxes are cut on glyph gaps and assigned to exactly one cell, so
    they cannot do either.
    """
    assert cell_text("Cityof Houston", "City of Houston") == "City of Houston"
    assert cell_text("1 109+59", "1109+59") == "1109+59"
    assert cell_text("othwell Street (south", "Rothwell Street (south") == (
        "Rothwell Street (south"
    )
    assert cell_text("Canal Street o", "Canal Street") == "Canal Street"


def test_a_cell_with_no_word_boxes_keeps_what_the_span_reader_found():
    """Absence of words is no evidence, so it must not blank the cell."""
    assert cell_text("Start Offset", "") == "Start Offset"
    assert cell_text("", "anything") == "anything"
    assert cell_text(None, "") is None


def test_a_table_whose_readings_broadly_disagree_is_left_alone():
    """The guard that replaces the per-cell one, and why it moved.

    Trusting word boxes cell-by-cell is only safe while both readings
    describe the same table. When they do not — the coordinate-space
    mismatch that made rotated pages disagree on 98% of cells before the
    rotation matrix was applied — every cell would be confidently
    overwritten with unrelated text. Agreement is therefore checked once
    per table, and a table that fails is not touched at all.
    """
    aligned = [("Utility ID", "Utility ID"), ("AT&T", "AT&T"), ("Telecom", "Telecom")]
    assert readings_agree(aligned)

    misaligned = [
        ("Utility ID", "No) Conflict Y"),
        ("Utility Owner", "Y Y"),
        ("Utility Type", "Y Y"),
    ]
    assert not readings_agree(misaligned)

    # Spacing-only differences are still agreement: that is the common case.
    assert readings_agree([("Cityof Houston", "City of Houston")] * 3)


def test_dedupe_hint_is_org_type_and_station_range():
    fields = {"external_org": "AT&T Texas (SWBT)", "utility_type": "Telecom",
              "station_from": "1149+00", "station_to": "1153+17"}
    assert dedupe_hint(fields) == "AT&T Texas (SWBT)|Telecom|1149+00-1153+17"








# ---------------------------------------------------------------------------
# Against the real corpus, when it is present. Skipped in a clean clone,
# where corpus/files/ is not tracked.
# ---------------------------------------------------------------------------

LOCK = Path("corpus/manifest.lock.json")


def _locked_corpus_is_present(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    lock = json.loads(lock_path.read_text())
    paths = [
        Path(record["local_path"])
        for record in lock.get("sources", {}).values()
        if record.get("local_path")
    ]
    return bool(paths) and all(path.exists() for path in paths)


real_corpus = pytest.mark.skipif(
    not _locked_corpus_is_present(LOCK),
    reason="corpus not fetched; run `make corpus`",
)


_PUNCT = re.compile(r"[,;]")


def _tokens(text: str) -> set[str]:
    """Page or field text as tokens, cut on punctuation as well as space.

    Some layouts combine stationing and offset in one column, and the page's
    own word boxes keep them together — `1112+90,713.40'` is a single box.
    Splitting that cell into two real fields is correct, and both halves are
    on the page, so the comma must not read as invented text.

    Cutting on punctuation rather than matching substrings is what keeps
    this able to catch #44: `othwell` is still not `rothwell`.
    """
    out = set()
    for word in normalize(text).split():
        for piece in _PUNCT.split(word):
            piece = piece.strip(".,;:()[]'\"")
            if piece:
                out.add(piece)
    return out


def _cells(path):
    """Every non-empty cell of every table in the document.

    What Tier 1 stores, before anything decides what the columns mean —
    so these assertions now sit directly on the reading the extractor
    depends on rather than on a parser's interpretation of it.
    """
    import pymupdf

    with pymupdf.open(path) as pdf:
        return [
            (cell or "").strip()
            for page in pdf
            for grid in page_tables(page)
            for row in grid
            for cell in row
            if (cell or "").strip()
        ]


def _matrix_paths():
    lock = json.loads(LOCK.read_text())
    return {
        rec["member"]: rec["local_path"]
        for rec in lock["sources"].values()
        if rec.get("member", "").startswith("nhhip-seg3c2-utilities-inventory")
        and rec.get("local_path")
    }






@real_corpus
def test_the_city_of_houston_split_is_repaired():
    """One owner, not two.

    Pages 15 and 20 of this revision split `City of Houston` across text
    spans, minting a second `external_orgs` row and splitting the owner's
    rows 111/43 across it.
    """
    paths = _matrix_paths()
    member = "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")

    orgs = Counter(_cells(paths[member]))
    assert "Cityof Houston" not in orgs
    assert "PrivateWater Line" not in orgs
    assert orgs["City of Houston"] == 154
    assert orgs["Private Water Line"] == 2


@real_corpus
@pytest.mark.parametrize(
    "member",
    [
        "nhhip-seg3c2-utilities-inventory.pdf",
        "nhhip-seg3c2-utilities-inventory-7-22-2025.pdf",
        "nhhip-seg3c2-utilities-inventory-10-24-2025.pdf",
        "nhhip-seg3c2-utilities-inventory-12-15-2025.pdf",
        "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
    ],
)
def test_every_stored_token_exists_on_its_page(member):
    """The invariant the citation check cannot enforce.

    Citation verification is row-level and fuzzy at 0.9, so a cell PyMuPDF
    mangled still verifies at ~0.99 inside a ~90-char row quote. This
    asserts the thing that actually matters: every token we store was read
    off the page rather than assembled into existence. `Cityof` is not a
    word on page 15 — no fuzzy row match can hide that.

    Compared against the page's *word boxes*, not `get_text()`. The text
    stream has the same span-concatenation problem (`freeway1148+60`) and
    returns wrapped cells out of order, so it is the wrong yardstick twice.

    All five revisions, including the two rotated 90 degrees. This covered
    one revision until #44, because the others carried clipped cells.
    """
    import pymupdf

    paths = _matrix_paths()
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")
    path = paths[member]

    unknown = []
    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            on_page = _tokens(" ".join(w[4] for w in page.get_text("words")))
            for grid in page_tables(page):
                for row in grid:
                    for cell in row:
                        for token in _tokens(cell or ""):
                            # Short tokens are enum-ish (`r`, `y`, `ug`) and
                            # match anywhere.
                            if len(token) >= 4 and token not in on_page:
                                unknown.append((index + 1, cell, token))
    assert not unknown, f"{len(unknown)} invented tokens, e.g. {unknown[:5]}"


@real_corpus
def test_a_clipped_cell_is_read_whole():
    """`Rothwell Street (south...` arrived from PyMuPDF as `othwell Street`.

    The leading character is dropped on a rotated page, and the value still
    verified as a citation because a one-character slip inside a ~90-char
    row quote clears the 0.9 threshold.
    """
    paths = _matrix_paths()
    member = "nhhip-seg3c2-utilities-inventory-10-24-2025.pdf"
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")

    values = set(_cells(paths[member]))
    clipped = [v for v in values if v.startswith("othwell") or v.startswith("ovidence")]
    assert not clipped, f"still clipped: {clipped}"
    assert any(v.startswith("Rothwell Street") for v in values)


@real_corpus
def test_a_character_bleeding_from_the_next_cell_is_not_kept():
    """`Canal Street` arrived as `Canal Street o`.

    The `o` is the first letter of the next cell (`on Belt and Terminal
    RR`). Word boxes belong to exactly one cell, so they cannot bleed.
    """
    paths = _matrix_paths()
    member = "nhhip-seg3c2-utilities-inventory-10-24-2025.pdf"
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")

    values = set(_cells(paths[member]))
    assert "Canal Street o" not in values
    assert "Canal Street" in values


# ---------------------------------------------------------------------------
# Cross-agency layouts (#50). Neither of these is Project B — FDOT SR 789 is
# the sealed holdout. These are §7.4's other two filled matrices.
# ---------------------------------------------------------------------------

CROSS = Path("corpus/cross-agency.lock.json")
cross_agency = pytest.mark.skipif(
    not CROSS.exists(), reason="cross-agency sources not fetched; run `make corpus`"
)


def _cross_paths():
    lock = json.loads(CROSS.read_text())
    return {
        rec["title"].split()[0]: rec["local_path"]
        for rec in lock["sources"].values()
        if rec.get("local_path")
    }


def test_a_combined_station_and_offset_column_is_split():
    """CDOT and FDOT put stationing and offset in one column.

    `135+58.68, 236.85' LT` is one cell. TxDOT spreads the same information
    across four, so a header map alone cannot reconcile them.
    """
    fields = split_station_offset(
        {"station_from": "135+58.68, 236.85' LT", "station_to": "138+44.23, 199.94' RT"}
    )
    assert fields["station_from"] == "135+58.68"
    assert fields["offset_from"] == "236.85"
    assert fields["station_to"] == "138+44.23"
    assert fields["offset_to"] == "199.94"
    # One column cannot carry two sides; the first wins and is recorded.
    assert fields["offset_side"] == "LT"


def test_txdot_stationing_is_untouched_by_the_split():
    """The split must be a no-op on the layout that already works."""
    original = {
        "station_from": "1149+00",
        "station_to": "1153+17",
        "offset_from": "303",
        "offset_to": "309",
        "offset_side": "R",
    }
    assert split_station_offset(dict(original)) == original


def test_a_station_without_an_offset_is_left_as_stationing():
    assert split_station_offset({"station_from": "1143+44.01"}) == {
        "station_from": "1143+44.01"
    }


def test_a_value_that_does_not_parse_is_left_whole_rather_than_guessed():
    """One CDOT row reads `169+83.31, 131+45' RT` — the offset is a station.

    Splitting on the comma alone would record an offset of 131 feet from a
    value that plainly does not mean that. Leaving it intact keeps the
    anomaly visible to a reviewer instead of laundering it into a number.
    """
    odd = {"station_from": "169+83.31, 131+45' RT"}
    assert split_station_offset(dict(odd)) == odd


def test_a_trailing_period_does_not_defeat_the_split():
    fields = split_station_offset({"station_from": "152+76.81, 160.66' LT."})
    assert fields["station_from"] == "152+76.81"
    assert fields["offset_from"] == "160.66"








def test_a_row_that_is_not_contiguous_falls_back_to_a_verifying_window():
    from corridor.geometry import MatrixRow, best_verifiable_quote

    # The real shape: a leading `Data Source` cell and the trailing station
    # cells both land elsewhere in reading order, so the row is present but
    # not contiguous.
    row = MatrixRow(
        fields={"utility_id": "E1", "external_org": "CenterPoint Energy"},
        page_no=1,
        quote=(
            "SUE E1 CenterPoint Energy Electric Dist OH IH 10 Crossing "
            "IH 10 Nance Street Buck Street 1142+69 267"
        ),
        cells=(),
    )
    page = (
        "E1 CenterPoint Energy Electric Dist OH IH 10 Crossing IH 10 Nance Street\n"
        + "\n".join(["unrelated legend content"] * 10)
        + "\nSUE\nBuck Street\n1142+69\n267"
    )

    quote, whole_row = best_verifiable_quote(row, page)
    assert whole_row is False
    assert quote_appears_on(quote, page)
    assert quote.startswith("E1 CenterPoint Energy")


def test_a_verifiable_whole_row_is_preferred():
    from corridor.geometry import MatrixRow, best_verifiable_quote

    row = MatrixRow(fields={}, page_no=1, quote="FOC1-1 AT&T Texas Telecom", cells=())
    quote, whole_row = best_verifiable_quote(row, "x FOC1-1 AT&T Texas Telecom y")
    assert whole_row is True
    assert quote == "FOC1-1 AT&T Texas Telecom"
