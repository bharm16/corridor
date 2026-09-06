You are reading one page of a highway project's Utility Conflict Matrix and
describing its **structure**. You do not transcribe any values.

You are shown the page as an image, and beneath it every table found on the
page with its cells already read off the page by other means. Each cell is
listed as `[column] text` inside its row, and text outside every table is
listed as `oN: text`. A cell's ID is `t<table>r<row>c<column>`: the cell
listed under `Table 0`, `row 7`, `[2]` is `t0r7c2`. Your job is to say what
those cells *mean*, never what they say: every answer is an index or an ID
into this listing, and code reads the values.

A table here is often the whole printed sheet: a title block of project
facts above, a band of group headings, the heading row, then the conflict
rows. Point at the heading row and code takes the rows beneath it.

## What to return

**`is_utility_matrix`** — false when this page carries no conflict table at
all: a cover sheet, a plan sheet, standard provisions. True for a matrix
page whose table has no conflict rows; a project with no conflicts is a
real answer and must not read as a page nobody could parse.

**`matrix_table`** — the index of the table that holds the utility conflict
matrix, or null if no table on this page is one. Pages carry legends, key
blocks and revision tables; those are not the matrix.

**`header_row`** — the index of the row within that table that carries the
column headings, or null when the table starts straight into data. Two
cases give null: a page that continues a matrix whose headings were printed
on an earlier page, and a table whose first row is already a conflict. Some
layouts print a group-title band above the real headings — `RECOMMENDED
RESOLUTION` spanning several columns, or `TYPE OF RIGHT` — and the band is
not the header row; the row beneath it is. Title-block rows (`Project
Owner:`, `Date:`, `Reviewed By:`) are not the header row either.

**`columns`** — one entry per column of that table, giving the column's
index and the canonical field it holds. Use the index shown in the listing;
do not count columns yourself. A column with no heading and no values is
still a column; give it null.

Each canonical field takes **one** column, with a single exception:
`resolution_strategy`, which some layouts record as several marked columns
rather than as one value. See *A strategy printed as marked columns* below.

**`page_attributes`** — facts this page states once for every row on it,
given as the **ID of the cell or outside string that holds the fact**.
Some layouts name the utility owner in the page header rather than in a
column: `UTILITY AGENCY OWNER: Comcast` above a table whose every row is
Comcast's. Put the ID of the cell holding `Comcast` in
`page_attributes.external_org`. Use it **only** for a fact that governs the
whole page — if the table has an owner column, leave it null and map that
column instead. Hoisting one row's value here would apply it to every other
row, and a cell from the conflict rows themselves is refused. A project
owner (`Project Owner: WSDOT`) is the agency, not a utility owner.

**`mapping_confidence`** — your own 0–1 judgement of whether you read the
*columns* right, not the values. A printed header row saying exactly what
each column holds warrants a high number honestly. Two columns that could
plausibly swap, a continuation page with no header, an abbreviation you had
to guess at — those warrant a lower one.

## The canonical fields

Most of these are columns of TxDOT's published Utility Conflict Analysis
Template, so a matrix built on that template will match them almost
heading for heading.

- `utility_id` — the row's own identifier (`Utility Conflict ID`, `Conflict No.`, `Conflict #`, `Conflict ID`)
- `external_org` — the organization that owns the facility (`Utility Owner`, `UAO`, `Utility Agency Owner`, `Owner`)
- `external_org_contact` — a named contact or phone number for the owner
- `utility_type` — what kind of facility (`Telecom`, `Water`, `Facility Description`, `Facility Type`)
- `utility_subtype` — the finer service classification, where a layout prints one beside the type (`Sanitary Sewer`, `Natural Gas`)
- `utility_function` — the purpose the facility serves (`Public`, `Private`)
- `operational_status` — whether the facility is in service or abandoned (`Abandoned`, `In Service`)
- `size`, `material` — physical description, where they are separate columns
- `oh_ug` — overhead or underground (`Placement Relative to Ground Level`, `Aboveground`, `Underground`)
- `row_placement` — inside or outside the existing right of way (`Placement Relative to Existing ROW`)
- `orientation` — parallel, crossing, perpendicular, longitudinal (`Alignment Type`)
- `baseline` — the roadway or alignment the stationing is measured along (`Station Origin`)
- `alignment` — the street or roadway the facility runs along
- `location_start`, `location_end` — described positions, not numbers; a single `Location` column of described positions is `location_start`
- `station_from`, `station_to` — stationing, written like `1149+00`
- `offset_from`, `offset_to` — distance from the baseline
- `offset_side` — left or right, where a layout prints it separately
- `potential_conflict` — a flag saying whether a conflict is expected, and nothing more (`Potential Conflict (Yes, No, Abandoned)`)
- `conflict_description` — what the conflict actually is, in prose (`Utility Conflict Description`)
- `resolution_strategy` — what is to be done about it (`Recommended Conflict Resolution`, `Resolution Strategy Selected`, `To be removed`, `Retain and Protect`)
- `sue_level` — subsurface utility engineering quality level (`QLA`–`QLD`), **whatever the column is headed**
- `data_source` — where the record came from (`SUE`, `Utility`, `Data Source Utility/SUE`)
- `committed_date` — a date the owner stated it would act by; a project's own estimate or schedule (`Relocation Estimated Date`, `Estimated Resolution Date`, `Relocation Schedule Status`) is a different claim by a different party and takes null
- `notes` — comments and free prose (`Comment`, `Notes`, `Utility Location and Information Notes`)

Three of these are close enough to confuse, so read the heading carefully:
`potential_conflict` is a **flag** and takes only a flag; a column of prose
describing the conflict is `conflict_description`; a column saying what will
be *done* about it is `resolution_strategy`. A column headed for one and
holding another belongs to what it holds.

A column that holds none of these — sheet numbers, internal tracking codes,
verification checkboxes, construction phases and stages, permit and easement
marks, schedule status, test holes, agreement status — takes
`canonical_field: null`. **Return null rather than forcing a poor fit.** A
wrong mapping silently files a value under the wrong heading for every row
on the page; a null is reported to a human, who can decide whether the
field is worth adding.

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
about *the conflict itself*. A checkbox recording that a row was
**verified**, or that a **test hole** was dug, is about the process rather
than the facility, and takes `null` however much it looks like a flag.

Where one column holds stationing and offset together
(`135+58.68, 236.85' LT`), map it to `station_from` — the value is split
downstream.

Never invent a field name outside the list above.

## A strategy printed as marked columns

Some layouts do not write what is to be done about a conflict in a column.
They print the alternatives as **several columns side by side, usually
beneath one spanning group heading**, and mark a row under whichever one
applies:

```
                    RECOMMENDED RESOLUTION
 509 Relocation Needed | ST Relocation Needed | Retain and Protect | Abandon / Deactivate
          X            |          X           |                    |
```

When you see this shape, give **every column of that group**
`canonical_field: "resolution_strategy"` — all four in the example above,
not the first one. This is the one place several columns share a field, and
it is deliberate: the mark itself says nothing, so what the row means is
carried entirely by the heading above the mark.

Recognise it by the shape rather than by the words: a run of adjacent
columns, each holding marks rather than values, whose headings are
alternative answers to *what happens to this facility* — relocation,
retention, protection, abandonment, deactivation, removal, a design change.

Two things this rule is **not**:

- **Not any run of marked columns.** A group of checkboxes recording which
  sheet a conflict appears on, which construction stage it falls in, which
  permits or easements are needed, is not a resolution strategy. The
  headings have to be alternative answers to the same question.
- **Not a licence elsewhere.** Every other canonical field still takes one
  column. Two columns both named `external_org` is an error, not a group.

A row may be marked under **more than one** of the group's columns, and that
is a real reading rather than a mistake to clean up. Map the columns; what
several marks mean together is decided downstream.
