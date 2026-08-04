"""The canonical vocabulary: what a conflict row can say.

Kept apart from any one reader, because there is more than one and they
must not drift. A matrix reaches this system as a printed page or as the
spreadsheet the page was printed from (ADR-0005), and the whole argument of
that ADR is that those are one document in two forms — so a field means the
same thing whichever way it arrived.

The two readers use these differently, and the difference is the point.
`extract_matrix` shows the model the field *names* and asks which printed
column holds each, because a printout does not carry the form's own names.
`sheets` looks the *column names* up directly, because a worksheet does.
One table, read from both ends.
"""

from __future__ import annotations

# The canonical vocabulary, and where each field comes from.
#
# It was originally read off Project A, whose document is a Utility
# *Inventory* rather than a Utility Conflict Matrix (ADR-0009). SH 99 turned
# out to print TxDOT's published Utility Conflict Analysis Template
# verbatim, and six of that template's standard fields had nowhere to go —
# so they were dropped on every page, and the model wavered over them
# differently on each one (#91).
#
# Every field now answers to one of two tables: a column of the published
# template, or a documented local exception. A field belonging to neither is
# a field nobody can defend.

# canonical field -> what TxDOT's published Utility Conflict Analysis
# Template calls it (ADR-0009). Field names are kept as they were; the
# template supplies the meaning, not the spelling, and renaming them would
# rewrite three projects' stored payloads to no purpose.
#
# The template names all but one of these as a **column**. `sue_level` is
# the exception and is named as a controlled vocabulary — the template's
# `Drop-Down Lists` sheet heads a `Utility Investigation Quality Level`
# list, and no sheet heads a column with that name. That is deliberate and
# is argued at the field below; it is recorded here because "the column it
# holds" was the claim this comment used to make, and for that one field it
# was not true.
#
# Every name is checked against the fetched form by
# `test_every_canonical_field_names_a_column_the_template_really_has`.
# Before the template was in `corpus/` nothing could check any of them, and
# the first run of that test found this dictionary naming a heading the
# form does not print (#60).
TEMPLATE_FIELDS = {
    "utility_id": "Utility Conflict ID",
    "external_org": "Utility Owner",
    "external_org_contact": "Utility Owner Contact Name",
    "utility_type": "Utility Type",
    "utility_subtype": "Utility Subtype",
    "utility_function": "Utility Function",
    "operational_status": "Operational Status",
    "size": "Size",
    "material": "Material",
    "oh_ug": "Placement Relative to Ground Level",
    "row_placement": "Placement Relative to Existing ROW",
    "orientation": "Alignment Type",
    "baseline": "Station Origin",
    "station_from": "Start Station",
    "station_to": "End Station",
    "offset_from": "Start Offset",
    "offset_to": "End Offset",
    # The template also defines `Utility Investigation Completed`, and it is
    # deliberately absent here. The only column in this corpus carrying that
    # heading — SH 99's draft UCM — holds `QLB`/`QLC`/`QLD`, which are
    # quality levels and belong in `sue_level`. Offering both fields split
    # the same data by project: Project A's levels under `sue_level`, SH
    # 99's under the other, so a corpus-wide read of either silently missed
    # a project. That is the divergence ADR-0009 was written about,
    # reproduced in a new field.
    "sue_level": "Utility Investigation Quality Level",
    "conflict_description": "Utility Conflict Description",
    # The heading the form actually prints. Its data dictionary defines the
    # same column under the shorter `Resolution Strategy Selected`, which
    # is what this said until the form could be read — and a spreadsheet
    # reader matching on the short name finds no resolution column at all.
    # ADR-0009 quotes the printed form in full.
    "resolution_strategy": "Resolution Strategy Selected (from Resolution Alternatives)",
    "notes": "Comment",
}

# canonical field -> why it exists despite having no column in the template.
# Each is printed by a document in this corpus and says something the
# template has no place for.
LOCAL_FIELDS = {
    "alignment": (
        "Project A prints `Alignment (along Roadway)` — the street a facility "
        "runs along. The template's `Alignment Type` is longitudinal-versus-"
        "crossing and is mapped to `orientation`; the two share a word and "
        "not a meaning."
    ),
    "location_start": (
        "Project A prints described endpoints (`Location Start (South or "
        "West)`). The template locates a conflict by station and offset alone."
    ),
    "location_end": (
        "The other end of the same described range, for the same reason as "
        "`location_start`."
    ),
    "offset_side": (
        "The template carries the side inside the offset value itself — its "
        "own example is `57.24 (L)`. Project A prints `Offset L/R` as a "
        "separate column, so it needs somewhere to go."
    ),
    "potential_conflict": (
        "Project A's inventory flag, `Potential Conflict (Yes, No, "
        "Abandoned)`. The template has no such column, because a conflict "
        "matrix lists conflicts rather than flagging them (ADR-0009)."
    ),
    "data_source": (
        "Project A prints `Data Source Utility/SUE`. The template records an "
        "investigation quality level and completion dates instead, which say "
        "how good the record is rather than who supplied it."
    ),
    "committed_date": (
        "A date the External Party stated it would act by. The template's "
        "`Estimated Resolution Date` is the project's own estimate — a "
        "different claim by a different party, and the distinction is the "
        "point of tracking commitments at all."
    ),
}

# Printed columns seen in this corpus that deliberately get no canonical
# field. Declining is a decision, so it carries an argument — and a column
# named here is one a reviewer has already considered, not one nobody
# noticed.
DECLINED_COLUMNS = {
    "Early TxDOT Utility Activity": (
        "SH 99. Says TxDOT will do the utility work early — a fact about how "
        "the project is delivered, not about the facility or what happens to "
        "it. Reading it as a conflict flag put 60 rows under "
        "`potential_conflict` that did not belong there (#85)."
    ),
    "AURL or DBA": (
        "SH 99. Says who performs the relocation — TxDOT in advance, or the "
        "design-builder under the DBA — not whether one is required. Every "
        "populated value is a delivery assignment."
    ),
    "VVH (Y/N)": (
        "FDOT SR 789. A vacuum-verification-hole checkbox: an artifact of the "
        "investigation process rather than a property of the facility."
    ),
    "VVH #": (
        "FDOT SR 789. The reference number of that test hole, for the same "
        "reason."
    ),
    "Resolved Status": (
        "FDOT SR 789. Workflow state. The Ledger owns whether a Dependency is "
        "Ready and derives it from adjudicated evidence (ADR-0002); importing "
        "a document's view of it would let an extractor write that state."
    ),
    "Sheet No.": (
        "Where the conflict is drawn, not what it is. The template carries "
        "`Utility Layout/Sheet No.` and it is declined for the same reason: "
        "it points at another document rather than describing this row."
    ),
    "Utility Layout/Sheet No.": (
        "The template's own name for the sheet reference. Declined for the "
        "same reason as `Sheet No.`: it points at another document rather "
        "than describing this row."
    ),
    "Verified (Y/N)": (
        "Project A. Whether somebody checked the row, not what the row says. "
        "A Y/N checkbox looks exactly like a conflict flag, which is why "
        "this list has to reach the model rather than sit in the code."
    ),
}

# The vocabulary the model may name, and the code enforces. Anything outside
# the set is treated as unmapped rather than stored, because a new field is
# a deliberate change and not an extractor's improvisation.
ROW_FIELDS = (*TEMPLATE_FIELDS, *LOCAL_FIELDS)


# A row needs both to be a Dependency: an identifier to be tracked by, and
# an External Party to be owed by. Either may arrive from the page rather
# than the row. Also guards a legend table the model mistook for the matrix.
REQUIRED = ("utility_id", "external_org")

# ...and a row must map at least this many fields of its own, whichever
# they are.
#
# The deterministic parser got this for free by requiring both fields from
# the row, which page-scoped inheritance then took away: FDOT prints a
# full-width group-title band mid-table (`FROM C/L CONST GULF OF MEXICO
# DR.`), and a band inheriting the page's External Party satisfies both
# required fields off a single cell. That is nine phantom rows on SR 789,
# one per page. A band has one non-empty cell of ten; a conflict row has
# eight.
MIN_ROW_FIELDS = 2
