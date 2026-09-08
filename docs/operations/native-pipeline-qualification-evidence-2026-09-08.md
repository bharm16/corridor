# Native pipeline qualification evidence — 2026-09-08

The native configuration is **not qualified or selected for production**.
The implementation sequence through #738 is merged, and the evidence below
supports integration and identifies remaining qualification work. A completed
adapter, a completed Extraction Run, a reproduced measurement, or a valid
receipt does not establish production readiness or accepted-record authority.

The initial #447 candidate scope is native utility matrices. Native Minutes
semantics, scanned extraction and mixed regions requiring OCR remain outside
that scope until their complete paths are implemented and measured. Scope
must be enforced before execution and selection; a global setting cannot
stand in for that qualification. All replacement defaults remain disabled.

## What each observation establishes

| Evidence | Observation | Limit |
|---|---|---|
| #736 typed native handoff | All 621,990 nonempty reader cells survive the typed handoff. The paired-corpus comparison preserves the frozen reader's exact counts and known failing pair. | Reader/cell integration, not full semantic extraction quality. The holdout has prior recorded access. |
| #737 seven-case retained-answer replay | 466 exact required rows; 2,466 persisted Facts replay; 2,701 field outcomes include 224 fields not extracted and 11 explicit date refusals. | Cached historical answers, not fresh model observations or independent field gold. Four duplicated FRANCHISE diagnostics still differ; full-reading parity is false. |
| #738 references | Original WSDOT triplets remain unchanged, with explicit legacy/native method contracts and separate validity notes. | Source-reference multiplicities and critical-subset coverage do not prove physical occurrence or independent field/disposition accuracy. WSDOT 9540 remains spent. |
| #734 routing | No missed OCR-positive page out of two and no unnecessary OCR call out of three OCR-negative pages. | Five pages, not a general zero-error estimate. |
| #735 original raster comparison | 541 of 576 comparisons meet every original tolerance. | Three profiles on page 1 of 192 PDFs; 35 tolerance failures and three excluded large PDFs remain. Execution success is separate from fidelity. |
| #447 bounded raster reconstruction | All 35 outlier comparisons reproduce every original metric exactly. | All 35 still fail unchanged tolerances. New rasters are new observations, not recovered historical PNGs. |
| #447 actual local citation census | Zero persisted rows in all 30 inspected tables and zero links in seven direct Source Segment reference tables. | No retained customer population was available. No original-byte or locator replay was possible. |

The #737 receipts describe their recorded revisions. #738 moved the canonical
completion queries from `extraction_runs.py` into an engine-independent owner;
that changes a file included in the native matrix configuration fingerprint.
Old receipts remain immutable historical evidence and cannot be relabeled as
measurements of the current full-chain configuration.

The raw #447 preflight reports and inventories are preserved byte-for-byte in
[`artifacts/pipeline-qualification/preflight-2026-09-08/`](../../artifacts/pipeline-qualification/preflight-2026-09-08/manifest.json).
That manifest binds the copied files and their original external paths. The
70 reconstructed rasters and their complete returned manifests remain at the
recorded external paths; they are Class B processing evidence, not permanent
accepted-record content. The durable qualification decision must retain its
metrics, scope, configuration, evidence identities and limitations separately.

## Raster failures and actual transform differences

The reconstruction covers 35 comparisons from 28 original, digest-matched
PDFs: ten review, ten OCR-layout and fifteen table-CV comparisons. Eight exceed
only mean absolute difference, 25 only one-pixel ink coverage, and two both.
All original metrics reproduced under matching render implementation blobs,
profiles and library versions, with network and environment synchronization
disabled. No tolerance was widened.

The [reconstruction receipt](../../artifacts/pipeline-qualification/preflight-2026-09-08/raster-reconstruction.json)
has SHA-256
`2c477c94f916ddb789681078a1e668388decbc392d909004ba9c53186b3107e4`.
The [35-row inventory](../../artifacts/pipeline-qualification/preflight-2026-09-08/raster-outliers.csv)
retains the original failures rather than reporting only examples.

Equal affine step names concealed eleven historical matrix differences:
nine source-origin cases and two deskew cases. Composed transforms move
corresponding PDF-user CropBox corners up to 3.6394 pixels apart for
`fb748d4a` and 1.8197 pixels for `d0f1a75d`. The `e619a4ab` origin difference
produces 49.8417 pixels of displacement at 300 dpi for identical source
coordinates. These measurements establish geometry differences; they do not
identify a particular lost glyph or incorrect cell.

Representative reconstructed pairs show heavier PDFium text/rules and darker
faint linework. Two inspected review examples show no gross layout shift at
the displayed scale; the processed agreement pair has differing sparse line
fragments. These observations do not establish that all outliers are harmless
or that either engine is correct. Source-anchored glyph visibility, cell/rule
boundaries and edge clipping through the actual transforms still need proof,
together with their downstream extraction effects.

The old `0c0cde2d` narrative names the wrong unmatched-ink reference set:
5.498756% of PDFium ink lacks nearby MuPDF ink, while the reverse fraction is
0.002478%. This correction does not determine the correct renderer. The old
receipt is retained unchanged. One surviving METRO legacy table-CV PNG matches
its reconstruction exactly; the other original primary PNG hashes were not
recorded. Reproduced scalar metrics cannot replace those missing identities.

Later pages remain outside the historical 576-comparison measurement. The
three excluded plans are still uncovered: SUE test-hole data `2611bbf6`
(32.56 MB), utility strip maps `375b4c36` (99.40 MB), and League City exhibit
`92f456c2` (55.39 MB).

## Actual retained citations

The [census](../../artifacts/pipeline-qualification/preflight-2026-09-08/citation-census.json)
is an actual database observation, not reconstructed corpus segmentation.
Its SHA-256 is
`0a6d5e46b1565c03dc65af64e72ca0d7979b604e70141b491f271d61eaac15bc`.
At 2026-09-08 08:53:48 UTC, the configured local schema/web/worker endpoint
was PostgreSQL 16.15 on loopback port 5433. A server-enforced read-only,
repeatable-read transaction counted the retained entities and their direct
reference paths. Row-security restrictions did not hide the rows. The
transaction was rolled back and no database state was changed.

Projects, documents, Source Segments, Facts, accepted decisions, evidence
links, reports, release candidates and authorized packages were all absent.
Consequently there were no cited source bytes to check. No remote deployment,
customer database, backup, other local database or actual release-artifact
population was inspected. This is **unavailable customer-history evidence**,
not a vacuous preservation pass.

Schema staleness is a separate finding: Alembic reports `b2d5f8a1c4e7`, but
ten native reading/locator columns and native append/Fact-transform support
are missing. The label alone does not establish the deployed executable
schema. This investigation did not migrate or repair it. Implementation tests
use harness-owned disposable databases instead.

The earlier compatibility experiment reconstructed 159,531 incumbent prose
spans over 192/195 registered PDFs. Its 9,756 incompatibilities included
6,649 strings absent from the replacement page and 3,107 present but ambiguous
strings. Text equality, a sole replacement occurrence, equal occurrence
counts, and ordinal alone cannot prove the same physical occurrence. No
historical Source Segment identity, original bytes, offsets, digest or
accepted reference is rewritten. #741 still owns engine-free investigation
and replay proof for the actual retained cited population.

## Evidence access and remaining gates

The raster preflight read split metadata from a file that also contains gold
labels. An append-only [holdout-access record](../../gold/pdf/v1/holdout-access.jsonl)
discloses that exposure and the absence of an exact earlier access timestamp.
No held-out source was newly rendered, no new holdout score or tuning was
performed, and none of the 35 reconstructed outliers uses the holdout document.
The earlier #735 full raster comparison did include that document's first
page; a separate #735 access entry was not located. This disclosure does not
create a fresh holdout or a new generalization claim.

Production qualification still needs an exact full-chain candidate and
declared deployment scope, applicable independent quality/provenance evidence,
failure/abstention/unconfirmed-reading yields, measured latency/memory and
processing cost, and available handling-burden evidence with every unmeasured
quantity named. Historical replay usage and its zero new provider calls must
remain separate from fresh-call/cache/retry cost. Local component execution
times are not deployed full-chain cost. Partner savings and ROI remain
unvalidated; their wider commercial validation belongs to #424/#498.

The intended Textract account and actual workload role are still unidentified
and unverified. The posture remains proposed; effective opt-out and runtime
permissions must be verified before acceptance and re-digesting. Customer
authorization remains #522. Those operational gaps do not prevent building
native deterministic mechanisms, and they do not authorize any provider call.

An explicit, attributable maintainer selection remains a separate act after
review of a concrete passing qualification receipt. It must not trigger
accepted-record updates. No selection is performed in this continuation.
