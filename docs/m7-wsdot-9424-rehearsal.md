# WSDOT 9424 through the tiered extractor: the holdout's twin

Run 2026-08-04 (#98). WSDOT contract 9424 is SR 509 Completion Stage 1B —
the unsealed sibling of M7's holdout, fetched in order to meet 9540's
layout without spending it (ADR-0008). Three documents ingested; the matrix
is Appendix U2, 11 pages.

**9540 was not touched.** It remains sealed and absent from the database.

## It extracted with no code change

```
make extract ARGS="wsdot-9424"

    162 rows     1 unverified  U2-Existing%20Utility%20Listing.pdf
1 matrices: 1 extracted (162 rows, 1 unverified), 0 unreadable, 0 skipped
  pages: 11 read from the text layer, 0 transcribed (0.0% fell back)
tokens: 80,520 in (17,270 cached) / 3,401 out
```

This is a **28-column table with a three-row header**, including group cells
spanning several columns — a shape no document in the corpus had before,
and the one the #98 ticket predicted would be hardest. It read. That is a
real result for ADR-0006's claim and it is not the finding.

## Finding 1: the resolution signal is four columns, and only one survives

The whole reason 9424 was fetched. Its header carries, beneath a spanning
`RECOMMENDED RESOLUTION` group cell, four columns marked with `X`:

```
[10] 509 Relocation Needed | [11] ST Relocation Needed |
[12] Retain and Protect    | [13] Abandon / Deactivate
```

The extractor mapped **one** of them — `509 Relocation Needed` — onto the
canonical `resolution_strategy` field, and left the other three unmapped.
So the stored value is:

| `resolution_strategy` | rows |
|---|---:|
| `X` | 71 |
| *(blank)* | 91 |

`X` is a mark, not a strategy. It means "relocate" only because of the
column it sits under, and that column heading is exactly what the canonical
field discards. The three unmapped columns carry the other three answers —
whether the row is Sound Transit's relocation, a retain-and-protect, or an
abandonment — and their values are not stored at all.

**This blocks #88.** 9540 shares this layout, and ADR-0009 makes the
resolution strategy the thing critical recall is computed from. A gate run
today would score a document whose resolution signal is three-quarters
discarded, and the remaining quarter is an `X` that no vocabulary can read.
The safe-by-default behaviour holds — `adjudicate.RESOLUTION_VOCABULARIES`
has no WSDOT entry, so nothing is asserted rather than guessed — but that
means WSDOT contributes **no criticality at all**, which is not a gate
result.

ADR-0009 named this shape and said the table "cannot express" it. This run
is the measurement behind that sentence.

## Finding 2: three column mappings across eleven pages

The header is reprinted on every page, and the model was asked what it
means eleven separate times, giving three different answers. That is #101's
subject, measured on the holdout's twin rather than argued about: a gate
run whose column mapping is derived independently on each page produces a
result that cannot be told apart from per-page variance.

## Finding 3: twenty-one of twenty-eight columns are unmapped

Beyond the three resolution columns, the unmapped set is largely WSDOT's
own project machinery — `Utilities 1A Sheet Stage Relocated UT`,
`Phase CN`, `Stage CN`, `Staging Required`, `Permit/Franchise State`,
`Permit/Franchise City`, `Easement City`, `Parcel Easement Private`,
`Agreement Status`, `Title Report`, `Prior Relocation / Deactivation`,
`Relocation Schedule Status`, `Hole Test`.

Several of these are real Ledger concepts under other names —
`Relocation Schedule Status` and `Agreement Status` are close to a
Dependency's status, and the permit and easement columns describe the
property interest TxDOT's template calls `Property Interest Type`. None
should be mapped without a reading of what WSDOT means by them, which is a
vocabulary decision and not an extractor's to make.

Seven columns did map: `utility_id`, `external_org`, `utility_type`,
`location_start`, `operational_status`, `committed_date`, `notes`, plus the
one resolution column.

## Finding 4: 162 rows against about 173 in the geometry

Reading the page tables directly and counting body rows that carry both an
owner and a conflict id gives roughly 173. The extractor produced 162. The
difference is not yet explained — it may be the `MIN_ROW_FIELDS` guard, a
continuation row, or a genuine miss — and it is recorded here rather than
resolved because 9424 has no gold set and a number without an independent
enumeration is not a recall figure.

## What this means for #88

Three things must land before the holdout is spent, and this run is the
evidence for the first:

1. **A resolution signal that can read marked columns** — the shape ADR-0009
   described and this document proves. Filed separately.
2. **#101**, so the mapping is one recorded decision rather than eleven.
3. **A WSDOT vocabulary entry**, written from 9424 and never from 9540.
