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

**`mapping_confidence`** — your own 0–1 judgement of whether you read the
*columns* right, not the values. A printed header row saying exactly what
each column holds warrants a high number honestly. Two columns that could
plausibly swap, a continuation page with no header, an abbreviation you had
to guess at — those warrant a lower one. Every row read under this mapping
inherits the number, so it is the one place your uncertainty can reach a
reviewer.

**`is_utility_matrix`** — false when this page carries no conflict table at
all: a cover sheet, a plan sheet, standard provisions. True for a matrix
page whose table is empty; a project with no conflicts is a real answer and
must not read as a document nobody could parse.

## The canonical fields

Most of these are columns of TxDOT's published Utility Conflict Analysis
Template, so a matrix built on that template will match them almost
heading for heading.

- `utility_id` — the row's own identifier (`Utility Conflict ID`, `Conflict No.`, `Conflict #`)
- `external_org` — the organization that owns the facility (`Utility Owner`, `UAO`, `Utility Agency Owner`)
- `external_org_contact` — a named contact or phone number for the owner
- `utility_type` — what kind of facility (`Telecom`, `Water`, `Facility Description`)
- `utility_subtype` — the finer service classification, where a layout prints one beside the type (`Sanitary Sewer`, `Natural Gas`, `Highly Volatile Liquid`, `Crude Oil`)
- `utility_function` — the purpose the facility serves (`Public`, `Private`)
- `operational_status` — whether the facility is in service or abandoned (`Abandoned`, `In Service`, `Abandoned in place`)
- `size`, `material` — physical description, where they are separate columns
- `oh_ug` — overhead or underground (`Placement Relative to Ground Level`, `Aboveground`, `Underground`)
- `row_placement` — inside or outside the existing right of way (`Placement Relative to Existing ROW`)
- `orientation` — parallel, crossing, perpendicular, longitudinal (`Alignment Type`)
- `baseline` — the roadway or alignment the stationing is measured along (`Station Origin`)
- `alignment` — the street or roadway the facility runs along
- `location_start`, `location_end` — described positions, not numbers
- `station_from`, `station_to` — stationing, written like `1149+00`
- `offset_from`, `offset_to` — distance from the baseline
- `offset_side` — left or right, where a layout prints it separately
- `potential_conflict` — a flag saying whether a conflict is expected, and nothing more (`Potential Conflict (Yes, No, Abandoned)`)
- `conflict_description` — what the conflict actually is, in prose (`Utility Conflict Description`, `Poles are within proposed ROW`)
- `resolution_strategy` — what is to be done about it (`Recommended Conflict Resolution`, `Resolution Strategy Selected`, `To be removed`, `Retain and Protect`)
- `sue_level` — subsurface utility engineering quality level (`QLA`–`QLD`), **whatever the column is headed**: a column headed `Utility Investigation Completed` whose cells read `QLC` is reporting the quality level, and belongs here
- `data_source` — where the record came from (`SUE`, `Utility`, `Data Source Utility/SUE`)
- `committed_date` — a date the owner stated it would act by
- `notes` — comments and free prose (`Comment`, `Utility Location and Information Notes`)

Three of these are close enough to confuse, so read the heading carefully:
`potential_conflict` is a **flag** and takes only a flag; a column of prose
describing the conflict is `conflict_description`; a column saying what will
be *done* about it is `resolution_strategy`. A column headed for one and
holding another belongs to what it holds.

A column that holds none of these — sheet numbers, internal tracking codes,
verification checkboxes — takes `canonical_field: null`. **Return null
rather than forcing a poor fit.** A wrong mapping silently files a value
under the wrong heading for every row on the page; a null is reported to a
human, who can decide whether the field is worth adding.

## Columns already ruled on

These have been seen before and deliberately given no canonical field.
Return `null` for them and do not reconsider:

- `VVH (Y/N)`, `VVH #` — vacuum-verification-hole checkbox and its number
- `Verified (Y/N)` — whether somebody checked the row
- `Resolved Status` — workflow state, which the Ledger owns
- `Sheet No.`, `Utility Layout/Sheet No.` — where the conflict is drawn
- `Early TxDOT Utility Activity` — that the agency will act early
- `AURL or DBA` — who performs a relocation, not whether one is needed

**Not every `Y/N` column is a conflict flag, and not every conflict flag is
spelled the same.** `potential_conflict` takes a column whose heading is
about *the conflict itself* — `Potential Conflict (Yes, No, Abandoned)`,
`Conflict (Yes, No)`, `Potential Conflict`. Map those. A checkbox recording
that a row was **verified**, or that a **test hole** was dug, is about the
process rather than the facility, and takes `null` however much it looks
like a flag.

Where one column holds stationing and offset together
(`135+58.68, 236.85' LT`), map it to `station_from` — the value is split
downstream.

Never invent a field name outside the list above.
