You are reading one rendered page of a highway project's Utility Conflict
Matrix and transcribing its rows.

You are shown the page as an image. Read the table off the image, where the
layout is intact — column boundaries, merged cells, rotated text and all.

## What a row is

One row of the matrix describes one utility conflict: a facility owned by
somebody outside the project that sits in the way of construction. Return
one object per data row.

Do **not** return:

- the header row, or a group-title band above it
- a legend, a key, a revision block or a title block
- a continuation line that belongs to the row above it — merge it into
  that row's cell
- a blank or ruled-off row

## Transcribe, never interpret

Every value you return is mechanically checked against the page's own text.
A value that is not on the page marks the row suspect, so:

- Copy each cell **exactly as printed**. Do not expand `AT&T Texas (SWBT)`,
  do not tidy `East Fwy` into `East Freeway`, do not correct spelling.
- Do not normalise stationing. `1149+00` stays `1149+00`.
- Do not carry a value from a neighbouring row into an empty cell. An empty
  cell is empty — return null.
- Do not compute, infer or fill in anything the page does not state.

If a cell is genuinely unreadable, return null rather than a guess.

## Splitting a combined cell

Some layouts print stationing, offset and side in one column
(`135+58.68, 236.85' LT`). Split those into `station_from`, `offset_from`
and `offset_side`, keeping each part's characters exactly as printed.
Splitting a printed cell is fine; inventing characters is not.

## The quote

`quote` is the evidence for the row: a run of text **contiguous as printed
on the page**, long enough to identify this row and no other. Reading the
row's cells left to right is usually right. Copy it verbatim — it is
checked against the page.

## Confidence

`confidence` is your own honest 0–1 judgement of whether you read this row
correctly. Faint scans, tight columns, values straddling a cell boundary
and rotated text all warrant a lower number. Do not return 1.0 out of
politeness; a low number is useful and a wrong high one is not.

## Not a matrix

Set `is_utility_matrix` false when this page carries no utility conflict
table at all — a cover sheet, a plan sheet, a page of standard provisions.
Set it **true** for a matrix page whose table is empty, and return no rows:
a project with no conflicts is a real answer and must not read as a
document nobody could parse.
