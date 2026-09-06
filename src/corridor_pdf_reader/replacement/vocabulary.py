"""The canonical vocabulary a Utility Conflict Matrix row can carry.

Copied from Corridor's `src/corridor/vocabulary.py` so the semantics tier can
be exercised here before it is ported; on the port this module is replaced by
Corridor's own, which is the single home of these names. Only the data the
tier needs travels: what the model may name, what it must return, and what
is a mark rather than a value.
"""

from __future__ import annotations

import re

# canonical field -> what TxDOT's published Utility Conflict Analysis
# Template calls it.
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
    "sue_level": "Utility Investigation Quality Level",
    "conflict_description": "Utility Conflict Description",
    "resolution_strategy": "Resolution Strategy Selected (from Resolution Alternatives)",
    "notes": "Comment",
}

# canonical field -> why it exists despite having no column in the template.
LOCAL_FIELDS = {
    "alignment": "the street a facility runs along",
    "location_start": "a described start position, not a station",
    "location_end": "a described end position, not a station",
    "offset_side": "left or right, printed as its own column",
    "potential_conflict": "a flag saying whether a conflict is expected",
    "data_source": "who supplied the record (SUE, the utility)",
    "committed_date": "a date the owner stated it would act by",
}

# Printed columns that deliberately get no canonical field.
DECLINED_COLUMNS = (
    "Early TxDOT Utility Activity",
    "AURL or DBA",
    "VVH (Y/N)",
    "VVH #",
    "Resolved Status",
    "Sheet No.",
    "Utility Layout/Sheet No.",
    "Verified (Y/N)",
)

# The vocabulary the model may name and the code enforces; anything outside
# it is unmapped, never stored under a guessed heading.
ROW_FIELDS = (*TEMPLATE_FIELDS, *LOCAL_FIELDS)

# A row needs both to be a dependency; either may arrive from the page.
REQUIRED = ("utility_id", "external_org")

# ...and must map at least this many fields of its own.
MIN_ROW_FIELDS = 2

# The one field several columns may claim at once: a resolution strategy
# printed as marked columns beneath a spanning heading.
MARKED_COLUMN_FIELD = "resolution_strategy"

# A cell holding this and nothing else is only a mark.
MARK_CELLS = frozenset({"x"})

# Facts a page states once for every row on it.
PAGE_FIELDS = ("external_org",)

# Phrases a form prints to retire a row of its numbering.
RETIREMENT_PHRASES = ("not used",)

_IDENTIFIER_FIELDS = frozenset({"utility_id"})

# Column headings that assert work sequencing, which is not modelled: a
# reader meeting one refuses the document whole. Normalized form.
SEQUENCING_HEADERS = frozenset({"DEPENDENT ACTIVITY"})

_WS = re.compile(r"\s+")


def normalize_header(header: str | None) -> str:
    return _WS.sub(" ", (header or "").replace("\n", " ")).strip().upper()


def is_sequencing_header(printed: str | None) -> bool:
    return normalize_header(printed) in SEQUENCING_HEADERS


def is_retired_row(fields: dict[str, str]) -> bool:
    """Every mapped value outside the identifier is a retirement phrase."""
    values = [
        value.strip()
        for field, value in fields.items()
        if field not in _IDENTIFIER_FIELDS and value and value.strip()
    ]
    if not values:
        return False
    return all(any(phrase == value.casefold() for phrase in RETIREMENT_PHRASES) for value in values)
