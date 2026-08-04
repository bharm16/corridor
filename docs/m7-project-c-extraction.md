# Project C through the tiered extractor, unmodified

Run 2026-08-04 (#85). Project C is `sh99-grand-parkway`, TxDOT SH 99 Grand
Parkway Segment B-1: three ingested Utility Conflict Matrices, 47 pages,
**zero** previously extracted. Phase 1's exit asks the pipeline to run on
three projects with no project-specific code, and this is the untested
third.

It is also the rehearsal for the holdout. An unseen layout is where the
tiered extractor might break, and ADR-0008 spends WSDOT 9540 once.

## Headline: no code changed

```
make extract ARGS="sh99-grand-parkway"

    489 rows     0 unverified  sh99-draft-ucm.pdf
    457 rows     0 unverified  SH 99 Seg B_UCM_to_RIDs_2.23.25.pdf
    455 rows     0 unverified  SH 99 Seg B_UCM_to_RIDs_5.5.25.pdf
3 matrices: 3 extracted (1,401 rows, 0 unverified), 0 unreadable, 0 skipped
  pages: 47 read from the text layer, 0 transcribed (0.0% fell back)
```

No canonical field was added, no prompt line changed, nothing was
configured. Every one of the 1,401 rows carries an attributed External
Party — 38 distinct — and every stored token of every field value is text
on its cited page. Project A (3,376) and Project B (66) were untouched.

Two layouts, not one, and neither is Project A's. `UCM_to_RIDs`
(1310/1311) prints 16 columns headed `Utility Conflict ID | Utility Owner |
Utility Type | Utility Subtype | Size | Abandoned | …`. The draft UCM
(1468) prints 17, headed differently, under a full-width title band that
puts the real header on row 1 — the FDOT band case, handled.

**ADR-0006's claim that a new document costs no engineering holds.** That
is not the finding.

## Finding 1: the column mapping is not stable across identical pages

Each page is mapped independently, and on SH 99 the same printed header
produced different answers on different pages of the same document.
Counting distinct mappings per document, read from the `unmapped_columns`
the model's own answer produces:

| project | document | pages | distinct mappings |
|---|---|---:|---:|
| A | all five revisions | 110 | **1** each |
| B | SR 789 | 9 | **1** |
| C | draft UCM | 13 | **4** |
| C | `UCM_to_RIDs` 2.23.25 | 17 | **3** |
| C | `UCM_to_RIDs` 5.5.25 | 17 | **4** |

On 1310 every one of the 17 pages prints the same 16 headings. Twelve
pages left `Early TxDOT Utility Activity` unmapped; four mapped it. Page 12
alone mapped `Utility Subtype` and dropped `Utility Location and
Information Notes`, so `notes` on that page holds a different printed
column than it does anywhere else in the document.

The wobble is confined to columns with no canonical home. Project A and
Project B waver on nothing because their headings map cleanly; SH 99's
`Utility Subtype`, `Abandoned`, `Early TxDOT Utility Activity` and
`Placement Relative to Existing ROW` have nowhere to go, and the model
makes a fresh judgement call about them on every page. A wrong mapping is
silent and applies to every row on its page — the risk `_column_mapping`
was written to bound — but bounding it per page is what lets one document
carry four.

`Placement Relative to Existing ROW` (Inside/Outside) was unmapped on all
47 pages. It is a vocabulary question, not a defect.

## Finding 2: `potential_conflict` now means three different things

ADR-0007 makes `Potential Conflict = Y` TxDOT's criticality signal. SH 99
is also TxDOT, and prints no such column. What reached the canonical field
instead:

| project | source column | values |
|---|---|---|
| A | `Potential Conflict` | `Y` 1,419 / `N` 513 / `A` 54 |
| B | *(a proposed-feature column)* | `Prop. Storm pipe (Possible)`, `Water Valve`, … |
| C — 1468 | `Utility Conflict Description` | `Poles are within proposed ROW`, … (165 rows) |
| C — 1310/1311 | `Early TxDOT Utility Activity` | `Yes` (60 rows) |

Project B's is pre-existing and was not produced by this run.

The last row is the one that matters. Those 60 rows say TxDOT will do the
utility work early — a different claim from "this facility conflicts" —
and they exist on 4 of 1310's 17 pages and 2 of 1311's, selected by nothing
but the per-page wobble in Finding 1. 225 of Project C's 1,401 rows carry
`potential_conflict` at all.

**So a criticality implementation that reads `fields["potential_conflict"]`
would mark 60 SH 99 rows critical off a column that does not mean that, on
pages chosen at random.** ADR-0007 already forbids this — the per-layout
signal is "a table somebody edits, not a heuristic", and a layout with no
identified signal produces no Assertion. This run is the concrete
demonstration of why: the canonical field name is not a safe carrier for
the signal, because two layouts from the same agency fill it from different
printed columns. #86 must key its table to a printed column on a layout.

## Finding 3: the eval's enumeration silently under-counts, and it will spoil the holdout

```
make eval ARGS="sh99-grand-parkway --prompt-version=matrix_tiered_v1"
sh99-grand-parkway — recall 100.0%  precision 36.0%
  gold 504   extracted 1401   matched 504
  spurious  897
```

36% precision is not an extraction result. `eval._UTILITY_ID` hardcodes the
prefixes `FOC|WW|UN|SS|E|W|G|T`, and SH 99 numbers its conflicts by utility
type using prefixes nobody added:

| visible to the enumeration | | invisible to it | |
|---|---:|---|---:|
| `WW` | 209 | `C` | 448 |
| `W` | 178 | `PL` | 184 |
| `E` | 117 | `OH C` | 134 |
| | | `CP` | 116 |
| | | `ET` | 15 |
| **total** | **504** | **total** | **897** |

The gold total is 504 exactly, and the spurious count is 897 exactly. Every
"spurious" row is a real conflict the enumeration cannot see, and recall of
100% is real but measured over the third of the document it can.

The defect is general, not SH 99's. Project A carries 71 rows prefixed
`UNKNOWN`, `Unknown`, `OFOC` or `No ID` — 2.2%, small enough to have read
as noise. SH 99 makes it 64%.

`gold_from_page_text` tries the sequential shape only when the prefixed
shape finds **nothing**. Here the prefixed shape found 504, so the fallback
never ran and a partial enumeration passed for a whole one. #82 taught the
eval to say NOT MEASURED when it finds nothing; this is the sibling case —
**it finds some and reports it as all**, which reads as a precision
collapse.

On a holdout that is unrecoverable. If WSDOT 9540 numbers its rows with any
prefix outside that list, the gate reports a precision figure that is an
artifact of the regex, and ADR-0008 says the document cannot be re-spent to
correct it.

## Cost

47 pages: **271,738 input tokens (49,320 cached) / 9,547 output, 0
reasoning** — about **$0.06**, or $0.0013 a page.

Derived, not measured: no price rate is recorded anywhere in the repo, so
this is scaled from the $0.15/110-page figure in
`docs/m6-validation-gate.md`, which was itself derived by hand. Consistent
with that run's $0.0014 a page.

**Prompt caching does work on the structure tier**, contradicting the note
carried into this session. 49,320 of 271,738 input tokens were cached —
roughly the system prompt on 44 of 47 requests. `prompts/matrix_structure_v1.md`
is about 1,100 tokens, over the provider's 1,024-token minimum rather than
under it.
