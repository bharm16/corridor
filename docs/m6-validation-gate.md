# Validation gate: the vision extractor against Project A's baseline

Run 2026-08-03 (#68). Project A is `nhhip-3c2`, TxDOT NHHIP Segment 3C-2:
five dated revisions of one Utility Conflict Matrix, 110 pages. It is the
fair test because it is the layout the deterministic parser handles best.

Both paths' Candidates are in the database. Nothing was migrated and no
Ledger record was touched — that decision is #61, and this is the number
it was waiting on.

## Headline

| | deterministic parser | vision extractor |
|---|---|---|
| `prompt_version` | `txdot_ucm_v1` | `matrix_vision_v1` |
| `model` | none | `gpt-5.6-luna` |
| rows | 3,235 | 3,240 |
| recall | **99.2%** | **98.9%** |
| precision | **97.7%** | **97.4%** |

Recall and precision are against the enumeration read off the page-text
stream, the same independent path used for the baseline, scoped to the same
five documents. Within 0.3 points on both, which is a wash.

That is not the finding.

## The finding: the same rows, less accurately transcribed

Field-token verification — every token of every stored field value must be
text on the cited page — applied to both paths:

| | rows with a value not on the page |
|---|---|
| deterministic parser | **3 of 3,235 (0.1%)** |
| vision extractor | **164 of 3,240 (5.1%)** |

A further 83 vision rows (2.6%) carry a quote that is not contiguous text
on the page. 228 rows in total are marked unverified and sunk in the queue.

The failures are real misreadings, not artifacts of the checker:

| wrote | page says |
|---|---|
| `1146+71.02` | `1146+71.825` |
| `1143+77.787` | `1143+17.787` |
| `1090+86.937` | `1090+86.993` |
| `1140+49.671` | `1140+94.671` |
| `1102+3.339` | `1102+93.339` |

Digits dropped, digits transposed. Exactly the class ADR-0004 predicted the
field check would catch and a fuzzy row-level quote match never could —
every one of these sits inside a row quote that verifies.

**They land in the worst fields.** `station_from` 39, `station_to` 22,
`location_end` 21, `offset_to` 18, `offset_from` 18, `external_org` 17.
Stationing is the most discriminating signal in merge ranking, and it is
the field the model gets wrong most often.

**Confidence does not predict them.** Of the 164 failures, 62 carry
confidence 0.99 and 96 carry 0.98. Story 8 — "a low-confidence row draws my
attention" — does not hold for this failure mode. The field check is what
catches it, and without it these rows would have reached a reviewer sorted
to the top of the queue wearing a green check.

**It scales with numeric density, not with rotation.**

| revision | page rotation | rows | field failures |
|---|---:|---:|---:|
| (undated, oldest) | 90° | 665 | 81 (12.2%) |
| 7/22/2025 | 0° | 590 | 29 (4.9%) |
| 12/15/2025 | 0° | 563 | 19 (3.4%) |
| 10/24/2025 | 90° | 715 | 21 (2.9%) |
| 2/13/2026 | 0° | 707 | 14 (2.0%) |

Both rotated revisions are not alike — 12.2% and 2.9%. What separates the
worst one is that it prints stationing to three decimal places
(`1146+71.825`) where the newest prints `1149+00`. More digits, more
transcription surface, more misreads.

## Cost

110 pages, 3,240 rows, 8m22s wall clock at 8-way concurrency.

| | tokens |
|---|---|
| run total | 540,995 in / 677,757 out |
| per page | ~4,918 in / ~6,161 out |
| per row | ~167 in / ~209 out |

Every matrix in the corpus is about 170 pages, so re-extracting all of them
costs roughly **840k in / 1.05M out** and about 13 minutes. Multiply by the
model's rate for a figure in currency; the token counts are what this code
can measure.

## What this says, and what it does not

It does **not** say the vision extractor is worse. It says its advantage is
reach, not transcription accuracy, and now that is measured rather than
assumed:

- On the layout the parser was built for, the parser transcribes better by
  a factor of fifty.
- On FDOT SR 789, where the External Party is named in a page header rather
  than a column, the parser extracts **zero** rows and the vision path
  extracts **66 across 9 owners** — the count recorded independently during
  corpus verification — every one field-token verified.

Two honest caveats in opposite directions. The parser's 0.1% is measured
*after* #43, #44 and #22; before those fixes it was far worse and entirely
silent for months, and nothing in the pipeline would have told anyone. The
vision path's 5.1% is visible the day it is made, named per field in
`payload_json["unverified_fields"]`, and sunk in the review queue. A
flagged error rate and a silent one are not the same kind of number.

## Read on #61

The evidence supports **forward-only** over re-extract-and-re-adjudicate.
Re-extracting Project A would trade 3,235 rows at 0.1% field error for
3,240 rows at 5.1%, on a layout where the parser already works — a
regression bought for nothing, and concentrated in the stationing fields
merge ranking depends on. The vision path earns its place on documents the
parser cannot read at all.

The decision is #61's. This is the number.

---

# Second gate: the tiered extractor (ADR-0006)

Run 2026-08-03 (#76), after #73–#75. Same corpus, same eval command, same
independent enumeration. The design under test is the one the first gate
argued for: the model reads structure, the page's word boxes supply values.

## Project A — exact parity with the parser

| | parser | transcription vision | **tiered** |
|---|---|---|---|
| `prompt_version` | `txdot_ucm_v1` | `matrix_vision_v1` | `matrix_tiered_v1` |
| rows | 3,235 | 3,240 | **3,235** |
| recall | 99.2% | 98.9% | **99.2%** |
| precision | 97.7% | 97.4% | **97.7%** |
| rows with a value not on the page | 3 (0.09%) | 164 (5.1%) | **3 (0.09%)** |
| rows with an unverifiable quote | — | 83 (2.6%) | **0** |
| pages that fell back to transcription | — | — | **0 of 110** |

Compared as a multiset of complete rows — every field, every page —
**3,235 of 3,235 rows are identical to the parser's, with zero rows on
either side that the other did not produce.** Not almost: exactly. The
three remaining field-token failures are the same three the parser has,
the known span-concatenation artifact (`freeway1148+60`).

So the transcription error class is gone, and nothing was traded for it.

## FDOT SR 789 — 66 rows where the parser reads none

66 rows, 9 owners, 0 unverified, every External Party attributed from the
page header. That matches the count taken independently during corpus
verification. The deterministic parser finds the table and maps **zero**
of its columns.

The eval command cannot score this document: its enumeration recognises
TxDOT-style identifiers (`FOC1-23`, `E92`, `WW1**`) and SR 789 numbers its
conflicts `1, 2, 3`, which no regex can distinguish from every other
integer on the page. It reports 0% against an empty gold set. The
comparison here is therefore by row and owner count against the pre-seal
count, and the enumeration gap is filed rather than papered over.

## Cost

| | input | output | cost | per page |
|---|---|---|---|---|
| transcription vision | 540,995 | 677,757 | $0.92 | $0.0084 |
| **tiered** | 618,029 | **23,571** | **$0.15** | **$0.0014** |

Output tokens fall **96.5%** because the model returns a column mapping
instead of 300 transcribed cells; input rises slightly because the prompt
now carries the header cells geometry read. Six times cheaper overall, and
zero reasoning tokens — the first gate's run was silently paying for
`medium` reasoning on every page.

Re-extracting **every matrix in the corpus** (~170 pages) costs about
**955k in / 36k out, or $0.24**. At transcription rates the same work is
$1.42.

## Four defects, all found by running it

Every one was surfaced by the gate rather than by a reviewer, which is
what the gate is for.

**A group-title band read as a conflict row.** FDOT prints
`FROM C/L CONST GULF OF MEXICO DR.` full-width mid-table. Inheriting the
page's External Party, a band satisfies both required fields off its single
cell — nine phantom rows on SR 789, one per page. The parser excluded bands
for free by demanding both fields from the row itself; page-scoped
inheritance took that away. Fixed in #74 (`MIN_ROW_FIELDS`).

**A continuation page mapped from data instead of a header.** The last page
of Project A's oldest revision carries 44 rows and no header. Every owner
cell on it reads `NA`, which the model reasonably took for a size — so the
owner column mapped to the wrong field and all 44 rows were dropped. A
printed header outranks an inference from data, so the mapping now carries
forward to same-width continuation pages, exactly as the parser does.

**A canonical field missing from the extractor's vocabulary.**
`Data Source Utility/SUE` is in the parser's synonym table and was not in
the tiered extractor's field list. It came back **reported as unmapped on
1,299 rows rather than guessed at** — which is the design working, not
failing: the extractor refused to file a value under a heading it was not
sure of, a human read the report, and the vocabulary was extended
deliberately. That is the intended loop for every new field, and it closed
in one run.

**86 rows in the baseline whose External Party is `NA`.** Both paths
reproduce them faithfully, because it is what the document says. But `NA`
is not an organization, and the parser has been minting Dependencies owned
by it since #65. Neither path invents this; it is a data-quality defect in
Project A's oldest revision that the gate happened to expose. Filed
separately.

`Verified (Y/N)` remains unmapped on 584 rows — the parser does not map it
either. Correctly surfaced, and a candidate for the vocabulary whenever
somebody decides what it means for the Ledger.

## Reproducing these numbers

`make eval ARGS="<slug> --prompt-version=<version>"` now reports the
field-token failure count beside recall and precision, so the figure the
whole comparison turns on comes out of the same command as the rest rather
than an ad-hoc script:

    nhhip-3c2 — recall 99.2%  precision 97.7%
      gold 3186   extracted 3235   matched 3160
      field-token failures  3 (0.09% of extracted rows carry a value not on their page)

Two things measured here were not in the code at the time of the run and do
not affect any number above. `mapping_confidence` was added to the Tier 1
schema afterwards, so the Candidates this run stored carry a null
confidence; it is verified live and populates on the next extraction. And
prompt caching does nothing on the structure tier — the shared prefix is
the instructions alone, which fall just under the provider's 1,024-token
minimum, and the 110-page run cached zero. Worth a penny, left alone.

## Read on #61

The first gate's finding was forward-only: do not re-extract Project A,
because it would trade 0.1% field error for 5.1%. **That reasoning no
longer holds, because the trade no longer exists.** The tiered path
reproduces the parser's output exactly on Project A, so re-extraction is
now a no-op there — which also means it buys nothing, and forward-only
remains the cheapest correct answer for records that already exist.

What has changed is the argument for the *future*: there is no longer a
quality reason to keep the deterministic parser on any document. It stays
only to generate baselines (#63 tracks its removal), and new projects have
one path that costs no engineering.


---

## Note, after #63

The deterministic parser has since been deleted. Its baseline above cannot
be regenerated from the code — what survives is these recorded numbers and
the `txdot_ucm_v1` Candidates in the database, which is why both are
written down here in full rather than left to be re-derived.

The half of that module which reads cells off word boxes was not deleted.
It is `corridor.geometry`, and Tier 1 depends on it for every value it
stores, so the 3-in-3,235 figure above is now a property of the live path
rather than of a retired one.
