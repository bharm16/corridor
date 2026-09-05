# PDF engine bake-off evidence — September 2026

**Issue:** #723<br>
**Observed:** 2026-09-05<br>
**Exact source commit:** `b7fc4ca703edcb70a8ec1e821e4033d8d8229153`<br>
**Recommendation:** **5. Evidence is insufficient.**

## Executive finding

The frozen experiment cannot produce a valid comparison from this checkout. This is an observed failure, not a recommendation to change an adapter or the frozen contract. The harness self-tests pass, but the smoke command compares deterministic failure receipts rather than engine output: its generated fixture paths are relative to the repository while workers execute with the experiment directory as their working directory. The one available manifest-bound development PDF reaches PyMuPDF, but its vector paths contain an `operation` field that the frozen result schema rejects. The run therefore stopped before measured repetitions. No candidate passed the development/regression gates, no configuration was shortlisted, and the holdout was not accessed.

This report changes no production engine, dependency, customer-data path, Project Record authority, infrastructure, ADR, or #461 posture. It preserves the failures instead of modifying #722's adapters or protocol after seeing results.

## Frozen identities

| Identity | Observed value |
|---|---|
| Source commit | `b7fc4ca703edcb70a8ec1e821e4033d8d8229153` |
| Gold set | `2026-08-31.2`; SHA-256 `73f3e0c019c22e2e5a38d873019f9b3510f632b3883a9033f7c284e8c79b2951` |
| Corpus manifest | SHA-256 `3507bbf0fa4049a4a590ed61591ed40feb59b164d80eb037f6a3661e4ef72579` |
| Candidate lock | SHA-256 `a5de808283d5bcad252de570c5b728525e667db5b3fbc7900e3c1dd21dfb5b93` |
| Candidate manifest | SHA-256 `8dbc367216e35e932ac8b4995b7df6051f35063b22118d42d8c5ae3d7128c9e1` |
| Result schema | SHA-256 `79589d134d9b7f3eeb0a60815a62bf6b092df0fd07706713685e104c40aa5d58` |
| Adapters | PyMuPDF `7bdebca9…`; PDFOxide `82c04eb1…`; PDFium `626ce0ca…` |
| Configuration | single-thread, process-cold; randomized seed planned as `72320260905` |
| Benchmark runner | `corridor-pdf-engine-bakeoff 0.1.0` |

## Host and engine inventory

| Item | Observed value |
|---|---|
| OS/kernel | Linux 6.18.35 x86-64, glibc 2.39 |
| CPU | Intel Xeon Platinum 8370C at 2.80 GHz; 3 logical CPUs |
| cgroup CPU quota | `200000 100000` (2 CPUs per 100 ms period) |
| Memory | 17,862,217,728 bytes available when inventoried; cgroup maximum 17,179,869,184 bytes; no swap |
| Python | 3.12.13 |
| PyMuPDF / MuPDF | 1.28.0 / 1.29.0 |
| pdf-oxide | 0.3.77, native Rust engine reported at the package version |
| pypdfium2 / PDFium | 5.13.0 / 153.0.7999.0 |

Every engine invocation was placed in a new network namespace with `unshare --user --map-root-user --net`; package synchronization was not run. This provides positive evidence that measured attempts could not use a network interface.

## Commands and observed outcomes

1. `make pdf-engine-bakeoff-test` passed: **21 tests**.
2. `unshare --user --map-root-user --net make pdf-engine-bakeoff` exited zero and wrote its summary, but all 30 receipts (three engines × five generated fixtures × two repetitions) contained `status: failed`, no pages, and `adapter_failure`. The equality check therefore proved only that each failure receipt repeated; it is not semantic accuracy, reliability, or engine repeatability evidence.
3. A separate seeded orchestration attempted one excluded warm-up followed by five randomized process-cold repetitions for all engines over the available development family and generated risk fixtures. It terminated during warm-up on the available 28-page NHHiP PDF because PyMuPDF returned vector paths whose declared `operation` property is absent from the frozen schema. The raw worker envelope was retained only under ignored `out/`; it was 25 MiB and is not committed.
4. No process-warm run was possible: the frozen worker accepts one request and exits, and every receipt hard-codes `process-cold`.

The coefficient-of-variation rule was never reached. There are no valid five-repetition latency samples, so no performance result can satisfy the 20% gate and escalation toward fifteen is inapplicable.

## Frozen gate results

| Frozen gate | Result | Evidence |
|---|---:|---|
| Wrong source citations do not increase; confirmed exact text does not decrease | **NOT RUN** | Receipts cannot be converted to the frozen Stage 0 `EngineRun` predictions. |
| Table/row/column/cell geometry | **NOT RUN** | Real-document receipt fails schema validation before comparison. |
| Rotation, boxes, UserUnit, coordinate round trip | **NOT RUN** | Smoke receipts contain no pages; real receipt is rejected. |
| Malformed/encrypted/unsupported fail closed | **NOT RUN** | Smoke fixtures were not opened, so their generic adapter failures do not exercise malformed or encryption behavior. |
| Exact deterministic repeatability | **NOT RUN** | Equality of generic failure receipts is explicitly not engine-output repeatability. |
| Representative end-to-end latency and memory | **NOT RUN** | No valid five-repetition samples; no warm mode. |
| **Overall** | **FAIL — insufficient evidence** | No candidate cleared development/regression. |

## Per-family and per-page-class status

| Family | Split | Status |
|---|---|---|
| TxDOT NHHIP UCM 2026 | development | Source bytes present; PyMuPDF output rejected by frozen result schema before measurement. |
| TxDOT SH 99 UCM 2025 | regression | Manifest identity present; matching PDF bytes absent from checkout. |
| WSDOT 9424 Appendix U2 | development | Manifest identity present; matching PDF bytes absent from checkout. |
| FDOT SR 789 roundabout UCM | holdout | Matching bytes absent; deliberately not accessed because no candidate passed prior gates. |
| COH municipal maintenance agreement 1969 | development | Manifest identity present; matching PDF bytes absent from checkout. |
| COH parking agreement amendment 2012 | development | Manifest identity present; matching PDF bytes absent from checkout. |
| WSDOT 9540 | required risk | Not present in the frozen corpus manifest. |

No valid page reached evaluation. Consequently matrix, rotated, continuation, repeated-header, outside-table owner/context, borderless, vector-rule, raster, malformed, encrypted, and incomplete-`find_tables()` classes all remain **not measured**. Reporting zero scores would incorrectly convert unsupported evidence into successful empty output.

## Correctness, geometry, rendering, reliability, memory, and latency

There are no valid semantic scores, geometry comparisons, render-fidelity scores, downstream OCR comparisons, crash/timeout distributions, peak-memory comparisons, or cold/warm latency summaries. The 28-page incumbent attempt did execute open, native text, positioned text, table, vector, and render operations before schema rejection, but those unvalidated observations cannot be promoted to benchmark results. Exact repeatability and semantic accuracy therefore remain separate and both unresolved.

## Unsupported capabilities and maturity observations

These are harness/configuration facts observed from the frozen implementation, not vendor capability claims:

- process-warm measurement is unsupported;
- the runner does not produce Stage 0 prediction files, so the frozen semantic evaluator cannot score it;
- the smoke command's relative paths yield generic failures while its equality-only pass condition remains green;
- real PyMuPDF vector output is incompatible with the frozen result schema;
- most corpus bytes and WSDOT 9540 are unavailable in the checkout; and
- no declarative hybrid configuration is registered. Per the issue boundary, no hybrid adapter or composition path was created after observing results.

The frozen package metadata records PyMuPDF as dual AGPL-3.0/commercial, pdf-oxide as MIT OR Apache-2.0, and pypdfium2 as BSD-3-Clause/Apache-2.0 plus dependency licenses. These are package declarations, not legal conclusions. Package maturity cannot be inferred from this failed run, and no material licensing/deployment advantage can be quantified before the Artifex quote.

## Holdout and change freeze

**Holdout access: none.** No configuration passed development and regression, so the shortlist is empty. No holdout-access log entry was added. No adapter, evaluator, threshold, fixture, normalization, or configuration was changed. A later run must first version and review repairs as a new experiment decision; it must not reuse this uncompleted attempt as authorization to inspect the holdout.

## Recommendation

**Choose option 5: evidence is insufficient.** Do not replace PyMuPDF, do not open the holdout, and do not start a production migration spike from these observations. A follow-up experiment should first provide all manifest-bound non-holdout bytes, add WSDOT 9540 to a newly versioned corpus decision if it remains required, and reconcile the already-frozen runner/schema/Stage 0 output path. A PDFOxide/PDFium hybrid remains only a recommended follow-up topology because #722 delivered no registered declarative composition configuration.

Even a successful rerun on this host would be comparative Codex Cloud evidence only. Any shortlisted topology would still require measurement in the deployable ECS batch image before a production capacity or migration decision.
