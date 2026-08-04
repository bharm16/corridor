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
