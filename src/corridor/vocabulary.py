"""The canonical vocabulary: what a conflict row can say.

Kept apart from any one reader, because there is more than one and they
must not drift. A matrix reaches this system as a printed page or as the
spreadsheet the page was printed from (ADR-0005), and the whole argument of
that ADR is that those are one document in two forms — so a field means the
same thing whichever way it arrived.

The two readers use these differently, and the difference is the point.
The page reader shows the model the field *names* and asks which printed
column holds each, because a printout does not carry the form's own names.
`sheets` looks the *column names* up directly, because a worksheet does.
One table, read from both ends.
"""

from __future__ import annotations

import re

from corridor.candidates import dedupe_hint as join_hint

_WS = re.compile(r"\s+")


def normalize_header(header: str | None) -> str:
    """One printed column heading, reduced to the form the tables are keyed by.

    Defined here because this module is the single home of these names.
    `corridor_pdf_reader/replacement/vocabulary.py` carries a deliberate copy
    of this function and of `SEQUENCING_HEADERS`: its own docstring says it
    was copied from here so the semantics tier could be exercised before the
    port, and that on the port it is replaced by Corridor's. ADR-0094 freezes
    that package, so the copy stays until the port removes it, and this
    definition does not import from it - that would invert the ownership the
    copy itself declares.
    """
    return _WS.sub(" ", (header or "").replace("\n", " ")).strip().upper()


def dedupe_hint(fields: dict[str, str]) -> str:
    """What discriminates one matrix row from another: party, kind, where.

    The join itself is `candidates.dedupe_hint`, shared with the extractors
    that read prose and discriminate on different parts. `merge` blocks on
    the result, so the separator is one rule even where the parts are not.
    """
    return join_hint(
        fields.get("external_org", ""),
        fields.get("utility_type", ""),
        f"{fields.get('station_from', '')}-{fields.get('station_to', '')}",
    )


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

# A second published TxDOT UCM form, by its exact printed column headings.
#
# `TEMPLATE_FIELDS` names the columns of TxDOT's *Utility Conflict Analysis
# Template* — the blank form in `corpus/cross-agency.yaml`. I-35 NEX South
# publishes an earlier TxDOT workbook, "Utility Conflict Management (UCM) -
# Utility Conflict List" (#365), whose columns carry the same meanings under
# different printed names. The workbook's own `Field_Column Descriptions`
# sheet defines each one, so this is the same kind of fact `TEMPLATE_FIELDS`
# already records — a published form's exact column name, read from the form's
# data dictionary — and it is added on the same terms `sheets.column_mapping`
# states: an exact name, never a fuzzy synonym. It lets the native reader read
# the structured original (ADR-0005) instead of reporting it unreadable.
#
# printed heading -> canonical field, each read straight from that data
# dictionary:
#   "Utility Company"                       owner of the facility  -> external_org
#   "Utility Company Contact"               facility point of contact
#                                                                  -> external_org_contact
#   "Longitudinal or Crossing"              runs along vs crosses  -> orientation
#   "Utility Placement in Relation to       inside/outside existing
#     Existing TxDOT Right of Way"            ROW                  -> row_placement
#   "Level of Utility Investigation Needed" QLB/QLC/QLD level      -> sue_level
#   "Recommended Action or Resolution"      next step to resolve   -> resolution_strategy
#     (the recommended resolution the document records, ADR-0009 — not a status)
#   "Comments"                              additional information -> notes
#
# Columns this form also carries that stay unmapped on purpose, reported
# rather than guessed (#365):
#   "Resolution Status"        — workflow state; the Ledger derives readiness
#                                and nothing imports a document's status (ADR-0002),
#                                the same reason FDOT's "Resolved Status" is declined.
#   "Estimated Resolution Date" — the project's own estimate, a different claim
#                                than an External Party's committed date (see
#                                `committed_date` above).
#   "Drawing or Sheet No."     — points at another document (declined like the
#                                template's own "Utility Layout/Sheet No.").
#   "Size and/or Material", "Base or Ultimate", "Line Style",
#   "Highway Alignment", "Test Hole No.", "Test Hole Depth"
#                              — no canonical field names them; carried as unmapped.
UCM_CONFLICT_LIST_HEADINGS = {
    "Utility Company": "external_org",
    "Utility Company Contact": "external_org_contact",
    "Longitudinal or Crossing": "orientation",
    "Utility Placement in Relation to Existing TxDOT Right of Way": "row_placement",
    "Level of Utility Investigation Needed": "sue_level",
    "Recommended Action or Resolution": "resolution_strategy",
    "Comments": "notes",
}

# Exact structured headings added by the source-to-Project-Record program.
# These are record inputs rather than aliases for one another: each date answers
# a different coordination question, and a source's resolution mark remains a
# source fact rather than writing record-level closure (ADRs 0067 and 0070).
STRUCTURED_RECORD_HEADINGS = {
    "Promised For": "committed_date",
    "Action Due Date": "action_due_date",
    "Required By": "need_date",
    "Resolution Status": "marked_resolution",
    "Resolved Status": "marked_resolution",
    "Applies To": "applies_to",
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


# Phrases a form prints to retire a row of its numbering. Enumerated from
# the corpus, not imagined: WSDOT 9424 prints the phrase 102 times — as
# `Not Used` 79 times and `Not used` 23 — and nothing else in this corpus
# retires a row at all. Case-insensitive because the spelling split falls
# across blank rows either way and carries no signal (#128).
RETIREMENT_PHRASES = ("not used",)

# Fields that identify a row rather than describe a facility. A retired
# row keeps its printed number; what it never has is content.
_IDENTIFIER_FIELDS = frozenset({"utility_id"})


def is_retired_row(fields: dict[str, str]) -> bool:
    """Does this row's own content amount to "this number is not in use"?

    True when every mapped value outside the identifier is a retirement
    phrase — the shape of 101 rows on WSDOT 9424, where a printed conflict
    number carries `Not Used` and nothing else. Those are the form's
    bookkeeping, not conflicts, and they enter neither the Ledger nor a
    gold denominator (ADR-0012).

    False the moment any real content appears beside the phrase. 9424's
    row 210 prints an owner, a facility, a location and a selected
    resolution next to `Not used` — somebody analysed that row, and a dead
    facility is still a Dependency (ADR-0009 puts abandonment on the
    critical side for exactly this reason). The phrase reaches the record
    verbatim in `notes`, and Adjudication judges what it means.

    A stated rule rather than a side effect, because the side effect was
    layout luck: on 9424 the blank rows died on the REQUIRED guard only
    because that form has an owner column. On a layout whose owner arrives
    from the page header, the same rows inherit it and sail through both
    guards as phantoms (#128).
    """
    values = [
        value.strip()
        for field, value in fields.items()
        if field not in _IDENTIFIER_FIELDS and value and value.strip()
    ]
    if not values:
        return False
    return all(
        any(phrase == value.casefold() for phrase in RETIREMENT_PHRASES)
        for value in values
    )


# Column headings that assert document-asserted work sequencing — one work
# item must complete before another may start. Corridor has no model for
# that relation (#149), and reading rows past such a column would shunt the
# relationship into `unmapped_columns`: rows ingested, sequencing dropped.
# Out of scope must mean unsupported, never lossy, so a reader that meets
# one of these refuses the document whole. Normalized form (see
# `normalize_header` above).
SEQUENCING_HEADERS = frozenset({"DEPENDENT ACTIVITY"})


def is_sequencing_header(printed: str | None) -> bool:
    """Does this printed heading assert a work-sequencing relation?"""
    return normalize_header(printed) in SEQUENCING_HEADERS
