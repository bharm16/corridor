# PDF engine bake-off: evidence contract

**Frozen:** 2026-09-05, before any challenger result was observed  
**Base commit:** `bae6a62fc7b9149dd02e2598fa05767c62d0fead`  
**Scope:** issue #720 only; non-production evidence contract, not an engine choice

## Boundary and evidence status

This contract does not change Corridor's production PyMuPDF engine, dependencies,
Project Record authority, customer-data path, infrastructure, or #461 posture.
The commercial Artifex/PyMuPDF path remains the recorded production posture.
No challenger was run on the corpus and no holdout was accessed while freezing
this contract. Package metadata and import-time version constants reported below
were observed in the isolated Codex Cloud environment; capability mappings are
requirements to measure, not claims of successful support. Declared licenses are
recorded verbatim as package metadata, without a legal-compliance conclusion.

## Production PyMuPDF seam inventory

| Production seam | Operations observed | Required capability |
| --- | --- | --- |
| `ingest.py`, `page_inventory.py` | open and reject zero-page PDFs; page count; native text/raw characters, image regions, drawings, tables; MediaBox/CropBox/TrimBox/BleedBox and rotation | open/validation; page inventory; boxes/transforms; characters, positioned blocks/lines; vector paths; tables; malformed behavior |
| `token_layers.py`, `geometry.py`, `extract_matrix.py` | ordered word boxes with block/line/word identity; rotation matrix; table discovery, topology, cells and extracted cell text | positioned words/order; coordinate transforms; tables/rows/columns/cells |
| `render_profiles.py` and `workers/render/render_worker.py` | page and clipped-region rasterization with pinned PDF-to-pixel transforms and image identity | page/clip rendering; dimensions; transforms |
| `source_segments.py`, `source_intake_draft.py`, `storage_baseline.py` | document open/page bounds, page text (including sorted text), page count and exact span replay | open/validation; page count; text ordering; malformed behavior |
| `page_inventory.py`, `m8_acceptance.py` | displayed page dimensions, page boxes, rotation and word clustering | page geometry; rotation; positioned words/lines |
| `web/queue.py` | source-quote search and page-relative highlight geometry | text search; positioned text; page dimensions |
| `current_record.py` | normalized visible text comparison for generated PDFs | released-PDF text verification |
| `report_release.py`, `sh99_coordinator_rehearsal.py` | fail-closed open plus visible-text verification of released PDF bytes | released-PDF verification; malformed/unsupported behavior |
| `gold.py` | table enumeration/extraction used by the superseded coupled gold path | legacy table behavior only; it must not define bake-off truth |
| `extractor_lineage.py` | records the resolved PyMuPDF distribution | package/engine provenance |

The experiment must additionally exercise encryption, malformed syntax,
unsupported inputs, metadata, UserUnit, CropBox/MediaBox, all four rotations,
and PDF-space coordinate round trips because the production seams depend on
their behavior even where a specific API call is indirect.

## Capability map to the #439 Stage 0 contract

The sole semantic truth remains `gold/pdf/v1/dataset.json`, validated and scored
by `src/corridor/pdf_evaluation.py`, invoked by `pdf_evaluation_cli.py`, and
formatted by `pdf_evaluation_report.py`. `eval.py` is excluded: it is the earlier
coupled Candidate-row evaluator and shares a PyMuPDF ceiling. The versioned
corpus manifest records the Stage 0 file digest and document identities without
copying or redefining labels.

| Engine capability | Existing Stage 0 layer / added engine receipt evidence |
| --- | --- |
| open, page count, validation | page coverage and failure rate; deterministic status/error classification |
| dimensions, boxes, rotation, UserUnit, transforms | page geometry plus explicit engine page facts and round-trip fixtures |
| characters, words, lines, block/line order, positioned text | exact visible-cell text, page-scoped values and source-citation layers; normalized object geometry |
| table, row, column and cell topology | Stage 0 table/row/column/cell F1, spans, topology, headers and geometry assignment |
| vector paths | explicit capability outcome and normalized paths; supportive evidence only, never truth substitution |
| page and clipped rendering | image digest/dimensions/transform receipt; routing and reviewer-render checks |
| malformed, encrypted, unsupported behavior | failure-rate layer plus fail-closed fixture classification; never partial success |
| released-PDF text verification | exact normalized visible-text fixture comparison, separate from extraction quality |

## Frozen distributions

Python is **3.12.13** on Linux x86-64. The exact direct-wheel project and lock are
under `experiments/pdf-engine-bakeoff/`. Stable, non-yanked releases with
compatible prebuilt wheels were selected from PyPI. Exact URLs and hashes in the
lock prevent resolver substitution.

| Package | Version | Exact wheel / platform | SHA-256 | Declared license metadata | Bundled engine observed |
| --- | ---: | --- | --- | --- | --- |
| PyMuPDF | 1.28.0 (same as root lock) | `pymupdf-1.28.0-cp310-abi3-manylinux_2_28_x86_64.whl`; `manylinux_2_28_x86_64` | `44f0973f5e5edbaec95bc34b64e71d1959d4ee90b1328de1b4f4f5b4fa78673f` | `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License` | MuPDF 1.29.0 |
| pdf-oxide | 0.3.77 | `pdf_oxide-0.3.77-cp38-abi3-manylinux_2_28_x86_64.whl`; `manylinux_2_28_x86_64` | `a531e6a0281b8037a47465fdaa912dc955ff22172d3e9171c76e89f15f828a1d` | `MIT OR Apache-2.0` | native Rust engine 0.3.77; no separately versioned bundled engine reported |
| pypdfium2 | 5.13.0 | `pypdfium2-5.13.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`; `manylinux_2_17_x86_64.manylinux2014_x86_64` | `81df25c1ab4c13ff773102d3cbea1967511d079123b067fc077bd0c4d57d91d8` | `BSD-3-Clause, Apache-2.0, dependency licenses` | PDFium 153.0.7999.0 |

Source distributions, compilation, runtime binary download, and fallback to a
system engine are refused. An unavailable or incompatible wheel is a failed
maturity finding, not permission to relax the rule. The pypdfium2 lane must
import the bundled library from this wheel; any system/source configuration
invalidates the run.

## Normalized result schema

`result-schema.v1.json` is strict at every object boundary. Each result contains:

1. **`deterministic_output`**: source digest; engine/bundled-engine/adapter
   identities; whole-document status; metadata; page dimensions and boxes;
   rotation and UserUnit; characters, words and lines with PDF-space geometry
   and ordering; table/row/column/cell topology; vector paths; render digests,
   dimensions, clip and transform; normalized warnings/errors; and a list of
   unsupported capabilities.
2. **`run_observation`**: host, cgroup, CPU quota, memory limit, OS,
   architecture, Python/packages, run order, cold/warm and thread mode,
   repetition/timestamp, process/import/operation latency, and peak RSS.

Every capability is a discriminated outcome: `supported` requires a value,
`unsupported` requires a reason, and `failed` requires a classified error and
message. Thus unsupported is neither omitted nor an empty successful value, and
a failed document cannot be reported as partial success.

The repeatability digest is canonical JSON of **only** `deterministic_output`
(UTF-8, sorted keys, no insignificant whitespace). `run_observation` is excluded;
timing, RSS, timestamps and order are expected to vary and are summarized
statistically.

## Benchmark protocol frozen before results

- Verify identical source bytes and SHA-256 before every engine invocation.
- Use development and regression documents only. Issue #720 does not authorize
  opening the holdout; preserve the existing access log and policy.
- Run without database, model API, customer data, secrets or network. Pre-sync
  the frozen environment; never install or download inside a test or measured run.
- Use randomized paired blocks across engines. Record the random seed and order.
- For each declared thread mode (`single`, then engine `default`), measure
  process-cold and warm separately. A warm-up for each configuration is excluded.
- Begin with five measured repetitions per paired block. Report median, p90,
  coefficient of variation (sample standard deviation / mean), paired percentage
  difference, and a paired bootstrap 95% interval with seed and resample count.
- If either compared engine's coefficient of variation exceeds 10%, continue
  matched blocks up to fifteen repetitions. If either remains above 10%, mark
  performance inconclusive; it cannot pass the 20% throughput gate.
- Retain host/cgroup, CPU quota, memory limit, OS, architecture, Python/package
  versions, start/import/operation timings and peak RSS. Codex Cloud measurements
  are same-host comparative evidence, not Fargate capacity evidence.
- Report exact deterministic repeatability separately from Stage 0 semantic
  quality. Never mix resource observations into the repeatability digest.
- Apply time and memory limits to malformed/encrypted/unsupported fixtures;
  crash, hang, partial output, or ambiguous success fails closed.

Representative end-to-end throughput includes every relevant topology step:
open/validate, inventory, positioned native text, tables/vectors where supported,
required page/clip renders, normalized serialization, and released-PDF
verification. A text-only microbenchmark is not representative.

## Predeclared decision gates

A challenger may be recommended only if **all** applicable gates pass:

1. Stage 0 wrong-source-cited-proposals count does not increase (maximum remains
   zero), and confirmed visible-cell exact-text accuracy does not decrease.
2. Page/table geometry intersection-over-union does not fall below **0.95**;
   matched row and column IoU does not fall below **0.95**; matched cell IoU does
   not fall below **0.90**; every box edge is additionally within **1.0 PDF point**
   of gold. Existing Stage 0 assignment/scoring remains the denominator.
3. All 0/90/180/270-degree fixtures preserve rotation, MediaBox and CropBox edges
   within **0.01 PDF point**; UserUnit is exact to **1e-6**; PDF→device→PDF points
   round-trip within **0.01 PDF point** per axis.
4. Every malformed, encrypted-without-password, or unsupported input terminates
   within the declared limit as one failed/unsupported result, without crash,
   hang, pages marked successful, or partial successful output.
5. Canonical `deterministic_output` is byte-identical across every repetition for
   the declared configuration. Observations are excluded by construction.
6. Representative end-to-end throughput improves by at least **20%**, using the
   paired-noise rule above; or performance is comparable and a separately
   demonstrated material licensing/operational advantage exists. This contract
   records no such advantage and makes no legal conclusion.

Any unsupported capability required by the representative production topology
fails that topology's gate. Faster text extraction alone is insufficient. A gate
failure is evidence to report, never a reason to synthesize capability output or
change the #439 truth.
