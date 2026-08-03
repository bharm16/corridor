import json
from collections import Counter
from pathlib import Path

import pytest

from corridor.extract import (
    NoMatrixFound,
    canonical_field,
    dedupe_hint,
    extract_rows,
    map_headers,
    restore_separator,
    row_quote,
    row_to_fields,
    to_candidates,
)
from corridor.verify import normalize, quote_appears_on

# The two real layouts on Project A, eight months apart.
CONFLICT_HEADERS = [
    "Utility ID", "Utility Owner", "Utility Type", "Size\n(inches,\nstrands)",
    "Material", "OH/ UG", "Baseline\nIH 10 /IH 69",
    "Parallel, Crossing\nPerpendicular\n(to Baseline)", "Alignment\n(along Roadway)",
    "Location Start\n(South or West)", "Location End\n(North or East)",
    "Start Station", "Start\nOffset", "End Station", "End Offset",
    "Offset\nL/R", "Potential\nConflict\n(Yes, No,\nAbandoned)",
    "SUE Level\n(A,B,C,D)", "Notes",
]
INVENTORY_HEADERS = [
    "UTILITY ID\nNO.", "FACILITY TYPE", "OWNER", "UG/OH", "SIZE & MATERIAL",
    "LONGITUDINAL/\nCROSSING", "BASELINE", "START STA", "START STA\nOFFSET",
    "END STA", "END STA\nOFFSET", "L/R",
]


def test_both_real_layouts_map_to_the_same_canonical_fields():
    conflict = set(map_headers(CONFLICT_HEADERS).values())
    inventory = set(map_headers(INVENTORY_HEADERS).values())
    shared = {"utility_id", "external_org", "utility_type", "station_from", "station_to"}
    assert shared <= conflict
    assert shared <= inventory


def test_columns_present_in_only_one_layout_are_mapped_where_they_exist():
    conflict = set(map_headers(CONFLICT_HEADERS).values())
    inventory = set(map_headers(INVENTORY_HEADERS).values())
    # These arrived partway through the revision history.
    assert {"potential_conflict", "sue_level"} <= conflict
    assert not {"potential_conflict", "sue_level"} & inventory


def test_the_longest_matching_header_wins():
    """`START STA OFFSET` must not be swallowed by `START STA`."""
    assert canonical_field("START STA") == "station_from"
    assert canonical_field("START STA\nOFFSET") == "offset_from"
    assert canonical_field("END STA") == "station_to"
    assert canonical_field("END STA\nOFFSET") == "offset_to"


def test_synonyms_across_layouts_resolve_to_one_field():
    assert canonical_field("Utility Owner") == canonical_field("OWNER") == "external_org"
    assert canonical_field("Utility Type") == canonical_field("FACILITY TYPE") == "utility_type"
    assert canonical_field("Start Station") == canonical_field("START STA") == "station_from"


def test_an_unrecognized_header_maps_to_nothing():
    assert canonical_field("Reimbursement Eligibility") is None
    assert canonical_field("") is None
    assert canonical_field(None) is None


def test_row_fields_are_read_by_header_not_position():
    conflict_row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG",
                    "IH 69", "Crossing", "IH 69", "Schwartz Street",
                    "Eastex Freeway FR SB", "1149+00", "303", "1153+17", "309",
                    "L/R", "Y", "B", ""]
    inventory_row = ["WW1**", "Wastewater", "City of Houston", "UG", '18" PVC',
                     "Crossing", "IH10", "1143+44.01", "NA", "NA", "NA",
                     "Crossing CL"]

    a = row_to_fields(conflict_row, map_headers(CONFLICT_HEADERS))
    b = row_to_fields(inventory_row, map_headers(INVENTORY_HEADERS))

    assert a["external_org"] == "AT&T Texas (SWBT)"
    assert a["station_from"] == "1149+00"
    assert b["external_org"] == "City of Houston"
    assert b["station_from"] == "1143+44.01"
    # Same field, opposite column index in the two layouts.
    assert a["utility_type"] == "Telecom" and b["utility_type"] == "Wastewater"


def test_empty_cells_are_omitted_not_stored_as_blanks():
    row = ["FOC1-1", "AT&T", "Telecom", "", "", "", "", "", "", "", "",
           "1149+00", "", "", "", "", "", "", ""]
    fields = row_to_fields(row, map_headers(CONFLICT_HEADERS))
    assert "size" not in fields
    assert fields["station_from"] == "1149+00"


def test_the_quote_is_the_whole_row():
    row = ["FOC1-1", "AT&T Texas (SWBT)", "Telecom", "", "FOC", "UG"]
    assert row_quote(row) == "FOC1-1 AT&T Texas (SWBT) Telecom FOC UG"


def test_a_dropped_separator_is_restored_from_the_word_boxes():
    """`City` + `of Houston` arrives from PyMuPDF as `Cityof Houston`."""
    assert restore_separator("Cityof Houston", "City of Houston") == "City of Houston"
    assert restore_separator("PrivateWater Line", "Private Water Line") == (
        "Private Water Line"
    )
    # And the opposite slip: a space that is not in the document.
    assert restore_separator("1 109+59", "1109+59") == "1109+59"


def test_a_cell_is_left_alone_unless_only_the_spacing_differs():
    """The guard, which is what makes this safe on rotated pages.

    There the word boxes and the table bboxes are in different coordinate
    spaces, so the rebuild is unrelated text rather than a respaced cell.
    """
    assert restore_separator("Utility ID", "No) Conflict Y") == "Utility ID"
    assert restore_separator("Start Offset", "") == "Start Offset"
    assert restore_separator("CenterPoint Energy", "CenterPoint Enerqy") == (
        "CenterPoint Energy"
    )
    assert restore_separator("", "anything") == ""
    assert restore_separator(None, "anything") is None


def test_dedupe_hint_is_org_type_and_station_range():
    fields = {"external_org": "AT&T Texas (SWBT)", "utility_type": "Telecom",
              "station_from": "1149+00", "station_to": "1153+17"}
    assert dedupe_hint(fields) == "AT&T Texas (SWBT)|Telecom|1149+00-1153+17"


def test_a_document_with_no_recognizable_table_raises(tmp_path):
    """The whole lesson of #2: an unhandled layout must not look empty.

    A zero-row return is indistinguishable from a matrix with no conflicts,
    so the two outcomes are kept apart.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "This document contains no utility matrix.")
    path = tmp_path / "not-a-matrix.pdf"
    doc.save(path)
    doc.close()

    with pytest.raises(NoMatrixFound):
        extract_rows(path)


def test_candidates_carry_a_verified_citation():
    rows = extract_rows.__wrapped__ if hasattr(extract_rows, "__wrapped__") else None
    from corridor.extract import MatrixRow

    row = MatrixRow(
        fields={"utility_id": "FOC1-1", "external_org": "AT&T Texas (SWBT)"},
        page_no=1,
        quote="FOC1-1 AT&T Texas (SWBT) Telecom",
    )
    page_text = {1: "NHHIP Matrix\nFOC1-1\nAT&T Texas (SWBT)\nTelecom\nFOC"}
    [candidate] = to_candidates([row], document_id=7, page_text=page_text)

    assert candidate["kind"] == "dependency"
    assert candidate["citations"][0]["document_id"] == 7
    assert candidate["citations"][0]["page"] == 1
    assert candidate["citations"][0]["verified"] is True


def test_a_candidate_with_an_unfindable_quote_is_marked_unverified():
    """Never dropped — sunk in the queue, visibly unverified."""
    from corridor.extract import MatrixRow

    row = MatrixRow(
        fields={"utility_id": "X", "external_org": "Invented Utility Co"},
        page_no=1,
        quote="Invented Utility Co agreed to relocate by June 3",
    )
    [candidate] = to_candidates([row], document_id=7, page_text={1: "unrelated text"})
    assert candidate["citations"][0]["verified"] is False


# ---------------------------------------------------------------------------
# Against the real corpus, when it is present. Skipped in a clean clone,
# where corpus/files/ is not tracked.
# ---------------------------------------------------------------------------

LOCK = Path("corpus/manifest.lock.json")
real_corpus = pytest.mark.skipif(
    not LOCK.exists(), reason="corpus not fetched; run `make corpus`"
)


def _matrix_paths():
    lock = json.loads(LOCK.read_text())
    return {
        rec["member"]: rec["local_path"]
        for rec in lock["sources"].values()
        if rec.get("member", "").startswith("nhhip-seg3c2-utilities-inventory")
        and rec.get("local_path")
    }


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
def test_every_revision_yields_rows_with_owner_and_stationing(member):
    """All five layouts, or the extractor only handles the one it was built on."""
    paths = _matrix_paths()
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")

    rows = extract_rows(paths[member])
    assert len(rows) > 400, f"{member}: only {len(rows)} rows"
    assert all(r.fields.get("external_org") for r in rows)
    assert sum(1 for r in rows if r.fields.get("station_from")) > len(rows) * 0.8

    # A matrix spans many pages and prints its header only on the first.
    # Rows from page 1 alone means continuation tables were dropped —
    # silently, because page 1 still parsed.
    pages = {r.page_no for r in rows}
    assert len(pages) > 1, f"{member}: rows only from page(s) {sorted(pages)}"


@real_corpus
@pytest.mark.parametrize(
    "member",
    [
        "nhhip-seg3c2-utilities-inventory.pdf",
        "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
    ],
)
def test_every_candidate_citation_verifies(member):
    """Citation validity is a 100% requirement, so assert it hard.

    The whole-row quote fails on ~2% of rows where PyMuPDF's reading order
    is not row-major; the fallback window must recover all of them.
    """
    import pymupdf

    paths = _matrix_paths()
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")
    path = paths[member]

    rows = extract_rows(path)
    with pymupdf.open(path) as pdf:
        text = {i + 1: page.get_text() for i, page in enumerate(pdf)}

    candidates = to_candidates(rows, document_id=1, page_text=text)
    failed = [c for c in candidates if not c["citations"][0]["verified"]]
    assert not failed, f"{len(failed)} of {len(candidates)} citations unverified"


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

    orgs = Counter(r.fields.get("external_org") for r in extract_rows(paths[member]))
    assert "Cityof Houston" not in orgs
    assert "PrivateWater Line" not in orgs
    assert orgs["City of Houston"] == 154
    assert orgs["Private Water Line"] == 2


@real_corpus
def test_every_stored_token_exists_on_its_page():
    """The invariant the citation check cannot enforce.

    Citation verification is row-level and fuzzy at 0.9, so a cell whose
    separator PyMuPDF dropped still verifies at ~0.99 inside a ~90-char row
    quote. This asserts the thing that actually matters: every token we
    store was read off the page rather than assembled into existence.
    `Cityof` is not a word on page 15 — no fuzzy row match can hide that.

    Compared against the page's *word boxes*, not `get_text()`. The text
    stream has the same span-concatenation problem being fixed here
    (`freeway1148+60`) and wrapped cells come back out of order in it, so
    it is the wrong yardstick for this on both counts.

    Scoped to this revision, where the count is zero. The other four still
    carry clipped cells from PyMuPDF (`Rothwell Street` -> `othwell Street
    (south`) — a separate defect, tracked in #44, not this bug class.
    """
    import pymupdf

    paths = _matrix_paths()
    member = "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    if member not in paths:
        pytest.skip(f"{member} not in the lockfile")
    path = paths[member]

    rows = extract_rows(path)
    with pymupdf.open(path) as pdf:
        pages = {
            i + 1: set(normalize(" ".join(w[4] for w in page.get_text("words"))).split())
            for i, page in enumerate(pdf)
        }

    unknown = [
        (r.page_no, field, value, token)
        for r in rows
        for field, value in r.fields.items()
        for token in normalize(value).split()
        # Short tokens are enum-ish (`r`, `y`, `ug`) and match anywhere.
        if len(token) >= 4 and token not in pages[r.page_no]
    ]
    assert not unknown, f"{len(unknown)} invented tokens, e.g. {unknown[:5]}"


def test_a_row_that_is_not_contiguous_falls_back_to_a_verifying_window():
    from corridor.extract import MatrixRow, best_verifiable_quote

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
    from corridor.extract import MatrixRow, best_verifiable_quote

    row = MatrixRow(fields={}, page_no=1, quote="FOC1-1 AT&T Texas Telecom", cells=())
    quote, whole_row = best_verifiable_quote(row, "x FOC1-1 AT&T Texas Telecom y")
    assert whole_row is True
    assert quote == "FOC1-1 AT&T Texas Telecom"
