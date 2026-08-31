"""Reading a spreadsheet source natively (ADR-0005, #60).

No model anywhere in here, and that is the claim: the structure is explicit
in the file, so running a vision model over it would be recovering from
pixels what the cells already state.

Fixtures are workbooks written at test time, so openpyxl does its actual
work without depending on a fetched corpus — the same bargain the PDF
fixtures make. The one test that reads the real published template skips
when the corpus has not been fetched, because that check is only worth
anything against the real bytes.
"""

from pathlib import Path

import pytest
from openpyxl import Workbook

from corridor.extract_matrix import TEMPLATE_FIELDS
from corridor.sheets import (
    NoConflictSheet,
    column_mapping,
    conflict_sheet,
    header_row,
    read_workbook,
    sheet_text,
)

# The published template's own shape: a title band on row 1 and the real
# headings on row 2. A reader that takes row 1 for the header finds one
# populated cell and no columns at all.
CONFLICT_HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Start Station",
    "End Station",
    "Utility Conflict Description",
]


def write_workbook(path, sheets):
    """A real .xlsx, so the reader does its actual work."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(name)
        for row in rows:
            worksheet.append(row)
    workbook.save(path)
    return path


def conflict_rows(extra=()):
    return [
        ["Utility Conflict Management (UCM) - Utility Conflicts"],
        CONFLICT_HEADINGS,
        ["UC-1", "CenterPoint", "Electric", "1149+00", "1150+00", "Pole in ROW"],
        ["UC-2", "AT&T", "Communications", "1151+00", "1152+00", "Duct bank"],
        *extra,
    ]


@pytest.fixture
def workbook(tmp_path):
    return write_workbook(
        tmp_path / "ucm.xlsx", {"Project Information": [["Category", "Item"]],
                                "Utility Conflicts": conflict_rows()}
    )


# ------------------------------------------------------- reading the file


def test_a_sheet_is_read_as_rows_of_strings(workbook):
    sheets = read_workbook(workbook)

    assert [s.name for s in sheets] == ["Project Information", "Utility Conflicts"]
    conflicts = sheets[1]
    assert conflicts.rows[1] == tuple(CONFLICT_HEADINGS)
    assert conflicts.rows[2][0] == "UC-1"


def test_an_empty_cell_reads_as_empty_text_not_none(tmp_path):
    """Every downstream reader treats a cell as text. `None` leaking through
    would make every one of them defend against it separately."""
    path = write_workbook(tmp_path / "gaps.xlsx", {"S": [["a", None, "c"]]})

    assert read_workbook(path)[0].rows[0] == ("a", "", "c")


def test_a_number_reads_as_the_text_the_cell_shows(tmp_path):
    """Stationing and offsets arrive as numbers, and a citation is text.

    `1149` must not become `1149.0`: the value is quoted back to a reviewer
    and compared against the sheet's own generated text, so a float
    repr nobody typed would be a value that appears nowhere.
    """
    path = write_workbook(tmp_path / "nums.xlsx", {"S": [["UC-1", 1149, 236.85]]})

    assert read_workbook(path)[0].rows[0] == ("UC-1", "1149", "236.85")


# --------------------------------------------------- finding the headings


def test_the_header_row_is_found_beneath_a_title_band(workbook):
    """Row 1 is a merged title, row 2 is the schema. The template ships
    this way, so a reader that assumes row 1 reads no columns at all."""
    conflicts = read_workbook(workbook)[1]

    assert header_row(conflicts) == 1


def test_a_sheet_with_no_recognisable_headings_has_no_header_row(tmp_path):
    """Declining is the answer, not guessing at the first populated row."""
    path = write_workbook(
        tmp_path / "other.xlsx", {"S": [["Category", "Item", "Information"]]}
    )

    assert header_row(read_workbook(path)[0]) is None


def test_headings_map_to_canonical_fields_by_name(workbook):
    conflicts = read_workbook(workbook)[1]

    assert column_mapping(conflicts.rows[header_row(conflicts)]) == {
        0: "utility_id",
        1: "external_org",
        2: "utility_type",
        3: "station_from",
        4: "station_to",
        5: "conflict_description",
    }


def test_a_heading_the_vocabulary_does_not_carry_maps_to_nothing(tmp_path):
    """Same rule the page extractor follows: a column nobody has ruled on
    is reported, never filed under a guessed heading."""
    rows = [["Utility Conflict ID", "Utility Owner", "Parcel U-Number"]]
    path = write_workbook(tmp_path / "extra.xlsx", {"S": rows})

    mapping = column_mapping(read_workbook(path)[0].rows[0])

    assert mapping == {0: "utility_id", 1: "external_org"}


# ------------------------------------------ which sheet is the matrix


def test_the_conflict_sheet_is_the_one_carrying_conflict_ids(tmp_path):
    """A workbook holds several tables and only one is the conflict matrix.

    ADR-0009's distinction, mechanised: an inventory records what is there,
    a conflict matrix records conflicts, and the conflict id is what says
    which you are looking at. `Utility Feature ID` is not a conflict id.
    """
    path = write_workbook(tmp_path / "both.xlsx", {
        "Utility Inventory": [[
            "Utility Feature ID", "Utility Owner", "Utility Type", "Size",
            "Material", "Station Origin", "Start Station",
        ]],
        "Utility Conflicts": conflict_rows(),
    })

    assert conflict_sheet(read_workbook(path)).sheet.name == "Utility Conflicts"


def test_the_fuller_sheet_wins_over_its_printed_view(tmp_path):
    """ADR-0005 applied inside one workbook.

    The published template carries both `Utility Conflicts` and `Utility
    Conflict List-Print`, and both head a column `Utility Conflict ID`. The
    print sheet is a rendering of the other — narrower by construction —
    and the ADR's whole subject is that a rendering does not outrank the
    structured original.
    """
    path = write_workbook(tmp_path / "print.xlsx", {
        "Utility Conflict List-Print": [
            ["Utility Conflict ID", "Utility Owner", "Start Station"],
        ],
        "Utility Conflicts": conflict_rows(),
    })

    assert conflict_sheet(read_workbook(path)).sheet.name == "Utility Conflicts"


def test_a_workbook_with_no_conflict_sheet_says_so(tmp_path):
    """`NoConflictSheet` rather than an empty result, for the reason #59
    story 2 gives: a workbook nobody could read must not report as a
    workbook with no conflicts."""
    path = write_workbook(tmp_path / "none.xlsx", {"S": [["Category", "Item"]]})

    with pytest.raises(NoConflictSheet):
        conflict_sheet(read_workbook(path))


def test_a_blank_form_is_a_conflict_sheet_with_no_rows(workbook, tmp_path):
    """The published template is a blank form, and that is a real answer.

    Same distinction the FDOT I-75 matrix exists to test on the PDF side: a
    form with a schema and no conflicts is not a form nobody could read.
    """
    path = write_workbook(tmp_path / "blank.xlsx", {
        "Utility Conflicts": [
            ["Utility Conflict Management (UCM) - Utility Conflicts"],
            CONFLICT_HEADINGS,
        ],
    })

    chosen = conflict_sheet(read_workbook(path))
    assert chosen.header_index == 1
    assert chosen.rows == ()


# --------------------------------------------------------- the rendering


def test_sheet_text_carries_every_cell_a_citation_could_quote(workbook):
    """A sheet has no page image, so its text is the rendering a reviewer
    checks a quote against. Every stored value has to appear in it."""
    conflicts = read_workbook(workbook)[1]

    text = sheet_text(conflicts)

    for value in ("UC-1", "CenterPoint", "1149+00", "Pole in ROW", "AT&T"):
        assert value in text


def test_sheet_text_keeps_rows_apart(workbook):
    """Row boundaries are real. Flattening them would let a quote match
    across two conflicts that never appeared together."""
    text = sheet_text(read_workbook(workbook)[1])

    assert "UC-1" in text.splitlines()[2]
    assert "UC-2" not in text.splitlines()[2]


# ------------------------------- the published template, when it is fetched

TEMPLATE = Path(
    "corpus/files/7b/"
    "7b8eb2b7e101816ec5473f24c6802d31533ef10e92be663d3197db754442c1f1.xlsx"
)

needs_corpus = pytest.mark.skipif(
    not TEMPLATE.exists(), reason="run `make corpus` to fetch the TxDOT template"
)


@needs_corpus
def test_every_canonical_field_names_a_column_the_template_really_has():
    """#97's traceability, checkable at last (#60).

    `TEMPLATE_FIELDS` asserts that each canonical field holds a named
    column of TxDOT's published template, and until the form was in
    `corpus/` nothing in the repo could check a single one of those names.
    The claim ran one way and was uncitable.

    Matched against the whole workbook — the conflict sheet's headings, the
    inventory's, the dropdown lists and the data dictionary's 113 defined
    columns — because the template states a name in more than one place and
    the claim is that it exists, not that it is printed on one sheet.

    Its first run found `resolution_strategy` naming `Resolution Strategy
    Selected`, which is the data dictionary's name for the column and not
    the heading the form prints. A spreadsheet reader matching on it finds
    no resolution column at all.
    """
    names = set()
    for sheet in read_workbook(TEMPLATE):
        for row in sheet.rows[:6]:
            names.update(_norm(cell) for cell in row if cell)

    missing = {
        field: column
        for field, column in TEMPLATE_FIELDS.items()
        if _norm(column) not in names
    }

    assert not missing, (
        "these canonical fields name something the published template does "
        f"not carry: {missing}"
    )


@needs_corpus
def test_only_sue_level_is_named_by_a_vocabulary_rather_than_a_column():
    """The one place the traceability claim is weaker than it reads.

    Every other canonical field names a column some sheet heads.
    `sue_level` names `Utility Investigation Quality Level`, which the
    template heads only as a `Drop-Down Lists` vocabulary — the conflict
    sheet's neighbouring columns are `Utility Investigation Completed` and
    `Utility Investigation Needed`, and `TEMPLATE_FIELDS` declines the
    first of those deliberately.

    Asserted so the exception stays one exception. A second field drifting
    into naming a vocabulary would be the traceability quietly weakening,
    which is the failure the check exists to prevent.
    """
    sheets = {s.name: s for s in read_workbook(TEMPLATE)}
    headed = {
        _norm(cell)
        for name, sheet in sheets.items()
        if name != "Drop-Down Lists"
        for row in sheet.rows[:6]
        for cell in row
        if cell
    }

    vocabulary_only = {
        field
        for field, column in TEMPLATE_FIELDS.items()
        if _norm(column) not in headed
    }

    assert vocabulary_only == {"sue_level"}


@needs_corpus
def test_the_template_is_a_blank_form_with_its_schema_intact():
    """What the corpus entry claims about the file, asserted against it."""
    sheets = read_workbook(TEMPLATE)
    conflicts = conflict_sheet(sheets)

    assert conflicts.sheet.name == "Utility Conflicts"
    assert len(conflicts.headings) == 40
    # A blank form: schema, no conflicts.
    assert not [row for row in conflicts.rows if any(cell for cell in row)]


def _norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def test_the_conflict_sheet_carries_the_reading_that_chose_it(tmp_path):
    """The selector computed the header index and mapping to choose; the
    caller then recomputed both, and used `header_row`'s `int | None`
    unguarded — correct only because the selection had already proved it
    non-None, which the return type could not say."""
    path = write_workbook(tmp_path / "w.xlsx", {
        "Cover": [["Utility Conflict Management (UCM)"]],
        "Utility Conflicts": [
            ["Utility Conflict Management (UCM) - Utility Conflicts"],
            CONFLICT_HEADINGS,
            ["UC-1", "AT&T", "Telecom", "1149+00"],
        ],
    })

    chosen = conflict_sheet(read_workbook(path))

    assert chosen.header_index == 1
    assert list(chosen.headings) == list(CONFLICT_HEADINGS)
    assert chosen.mapping == column_mapping(CONFLICT_HEADINGS)
    assert [row[0] for row in chosen.rows] == ["UC-1"]


def test_the_page_number_is_the_one_ingest_gave_the_same_sheet(tmp_path):
    """Every spreadsheet citation's page number used to rest on two
    modules independently agreeing about workbook order."""
    from corridor.ingest import _extract_sheets

    path = write_workbook(tmp_path / "w.xlsx", {
        "Cover": [["Utility Conflict Management (UCM)"]],
        "Notes": [["Anything"]],
        "Utility Conflicts": [
            ["Utility Conflict Management (UCM) - Utility Conflicts"],
            CONFLICT_HEADINGS,
        ],
    })

    chosen = conflict_sheet(read_workbook(path))
    ingested = {page.page_no for page in _extract_sheets(path)}

    assert chosen.page_no == 3
    assert chosen.page_no in ingested


# --------- a second published TxDOT form: "UCM - Utility Conflict List" (#365)

# I-35 NEX South's workbook, header for header. A title, five rows of project
# identification, a blank, then the column header on row 8 (index 7) — deeper
# than the template's row 2, and named by the form's own data dictionary
# rather than by the Utility Conflict Analysis Template.
UCM_LIST_HEADINGS = [
    "Utility Company",
    "Utility Company Contact",
    "Utility Conflict ID",
    "Drawing or Sheet No.",
    "Line Style",
    "Utility Type",
    "Size and/or Material",
    "Base or Ultimate",
    "Utility Conflict Description",
    "Longitudinal or Crossing",
    "Utility Placement in Relation to Existing TxDOT Right of Way",
    "Highway\nAlignment",
    "Station Origin",
    "Start Station",
    "Start Offset",
    "End Station",
    "End Offset",
    "Level of Utility Investigation  Needed",
    "Test Hole No.",
    "Test Hole Depth",
    "Recommended Action or Resolution",
    "Estimated Resolution Date",
    "Resolution Status",
    "Comments",
]

# The 15 columns the vocabulary rules on, mapped by this form's exact names.
UCM_LIST_MAPPING = {
    0: "external_org",
    1: "external_org_contact",
    2: "utility_id",
    5: "utility_type",
    8: "conflict_description",
    9: "orientation",
    10: "row_placement",
    12: "baseline",
    13: "station_from",
    14: "offset_from",
    15: "station_to",
    16: "offset_to",
    17: "sue_level",
    20: "resolution_strategy",
    23: "notes",
}


def ucm_list_rows(extra=()):
    return [
        ["TxDOT Utility Conflict Management (UCM) - Utility Conflict List"],
        [""],
        ["Project Owner:", "TxDOT"],
        ["CCSJ/RCSJ.:", "0016-05-111"],
        ["Project Description:", "I-35 NEX South"],
        ["Highway or Route:", "I-35 From FM 1103 to AT&T Center Drive"],
        [""],
        UCM_LIST_HEADINGS,
        [
            "CPS Electric", "John Offer", "41", "N/A", "N/A", "Electric",
            "Pullbox (2B)", "I-35 NEX South",
            "In conflict with proposed sidewalk improvements", "Longitudinal",
            "Inside", "IH-35", "RT", "327869.22", "131.97", "-", "-", "QLC",
            "N/A", "N/A", "Accommodate - Relocation", "", "", "IH 35 E ROW",
        ],
        *extra,
    ]


def test_the_ucm_conflict_list_header_is_found_beneath_the_project_preamble(
    tmp_path,
):
    """The template's header is on row 2; this form's is five rows deeper.

    A reader bounded at six rows (the template's depth) reports the whole
    structured original unreadable, which is the failure this exists to
    prevent (#365, ADR-0005).
    """
    path = write_workbook(tmp_path / "ucm.xlsx", {"UCM-Conflict List": ucm_list_rows()})

    assert header_row(read_workbook(path)[0]) == 7


def test_the_ucm_conflict_list_form_headings_map_to_canonical_fields(tmp_path):
    """The form's own names, read straight from its `Field_Column
    Descriptions` sheet — an exact published-form column name, never a
    synonym."""
    path = write_workbook(tmp_path / "ucm.xlsx", {"UCM-Conflict List": ucm_list_rows()})
    conflicts = read_workbook(path)[0]

    assert column_mapping(conflicts.rows[header_row(conflicts)]) == UCM_LIST_MAPPING


def test_the_ucm_conflict_list_extra_columns_stay_unmapped(tmp_path):
    """Nine columns this form carries have no canonical field, and are
    reported rather than guessed — including the two the vocabulary
    deliberately declines: a document's `Resolution Status` (workflow state,
    ADR-0002) and its `Estimated Resolution Date` (the project's own
    estimate, not a committed date)."""
    path = write_workbook(tmp_path / "ucm.xlsx", {"UCM-Conflict List": ucm_list_rows()})
    conflicts = read_workbook(path)[0]
    header = conflicts.rows[header_row(conflicts)]
    mapping = column_mapping(header)

    unmapped = [
        " ".join(header[i].split())
        for i in range(len(header))
        if header[i].strip() and i not in mapping
    ]

    assert unmapped == [
        "Drawing or Sheet No.",
        "Line Style",
        "Size and/or Material",
        "Base or Ultimate",
        "Highway Alignment",
        "Test Hole No.",
        "Test Hole Depth",
        "Estimated Resolution Date",
        "Resolution Status",
    ]


def test_conflict_sheet_reads_the_ucm_conflict_list_form(tmp_path):
    """The whole workbook: the conflict sheet is chosen over the form's data
    dictionary and dropdown sheets, and read on its own exact terms."""
    path = write_workbook(
        tmp_path / "ucm.xlsx",
        {
            "UCM-Conflict List": ucm_list_rows(),
            "Field_Column Descriptions": [["Field", "Description"]],
            "Drop-Down Lists": [["Drop Down Lists"]],
        },
    )

    chosen = conflict_sheet(read_workbook(path))

    assert chosen.sheet.name == "UCM-Conflict List"
    assert chosen.header_index == 7
    assert chosen.mapping == UCM_LIST_MAPPING
    assert [row[2] for row in chosen.rows] == ["41"]
