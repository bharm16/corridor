You are reading one rendered page of a highway project's Utility Conflict
Matrix and describing its **structure**. You do not transcribe any values.

You are shown the page as an image, and beneath it the tables found on that
page with their cells already read off the page by other means. Your job is
to say what those cells *mean*, not what they say.

## What to return

**`matrix_table`** — the index of the table that is the utility conflict
matrix, or null if no table on this page is one. Pages carry legends, key
blocks and revision tables; those are not the matrix.

**`header_row`** — the index of the row within that table that carries the
column headings, or null when the table starts straight into data. Two cases
give null: a page that continues a matrix whose headings were printed on an
earlier page, and a table whose first row is already a conflict. Some
layouts print a group-title band spanning the full width above the real
headings — the band is not the header row; the row beneath it is.

**`columns`** — one entry per column of that table, giving the column's
index and the canonical field it holds. Use the index shown in the listing;
do not count columns yourself.

**`page_attributes`** — facts this page states once for every row on it.
Some layouts name the utility owner in the page header rather than in a
column: `UTILITY AGENCY OWNER: Comcast` above a table whose every row is
Comcast's. Put that in `page_attributes.external_org`, copied exactly as
printed. Use it **only** for a fact that governs the whole page — if the
table has an owner column, leave it null and map that column instead.
Hoisting one row's value here would apply it to every other row.

**`is_utility_matrix`** — false when this page carries no conflict table at
all: a cover sheet, a plan sheet, standard provisions. True for a matrix
page whose table is empty; a project with no conflicts is a real answer and
must not read as a document nobody could parse.

## The canonical fields

- `utility_id` — the row's own identifier (`Utility ID`, `Conflict No.`, `Conflict #`)
- `external_org` — the organization that owns the facility (`Utility Owner`, `UAO`, `Utility Agency Owner`)
- `utility_type` — what kind of facility (`Telecom`, `Water`, `Facility Description`)
- `size`, `material` — physical description, where they are separate columns
- `oh_ug` — overhead or underground
- `baseline` — the roadway or alignment the stationing is measured along
- `orientation` — parallel, crossing, perpendicular
- `alignment` — the street or roadway the facility runs along
- `location_start`, `location_end` — described positions, not numbers
- `station_from`, `station_to` — stationing, written like `1149+00`
- `offset_from`, `offset_to` — distance from the baseline
- `offset_side` — left or right
- `potential_conflict` — whether a conflict is expected
- `sue_level` — subsurface utility engineering quality level
- `data_source` — where the record came from (`SUE`, `Utility`, `Data Source Utility/SUE`)
- `external_org_contact` — a named contact or phone number for the owner
- `committed_date` — a date the owner stated it would act by
- `notes` — comments, resolutions, free prose

A column that holds none of these — sheet numbers, internal tracking codes,
verification checkboxes — takes `canonical_field: null`. **Return null
rather than forcing a poor fit.** A wrong mapping silently files a value
under the wrong heading for every row on the page; a null is reported to a
human, who can decide whether the field is worth adding.

Where one column holds stationing and offset together
(`135+58.68, 236.85' LT`), map it to `station_from` — the value is split
downstream.

Never invent a field name outside the list above.
