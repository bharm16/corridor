---
status: accepted
domain: supporting-documentation
scope: current product
---

# The queue fails closed on a registered successor, and the Current Production Run is declared, not latest

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> ADR-0022 adds Automatic Support Update before reviewer presentation for the exact policy-eligible subset. Rows that are not carried retain the two-scope routing decided here.

Once Extraction Runs and Extracted Proposals become append-preserving (ADR-0018), the default review queue is computed scope: pending Extracted Proposals of the current revision's **Current Production Run**, derived at query time from registry and run lineage. Stored retirement states would duplicate what the lineage already answers; Extracted Proposal deletion is incompatible with the Comparison's receipt. Both stay rejected.

The scope **fails closed**. The moment revision C is registered, revision B's pending Extracted Proposals leave the default actionable processing scope — even before C has a usable extraction. A reviewer offered B's rows in that interval can accept known-superseded information, minting a Ledger record whose support fires `SUPERSEDED_CITATION` at birth, over a row that may have changed or been dropped in C. The interval is not silent: Document Revision Review shows `awaiting_extraction` when C has not run and `extraction_failed` when it tried and failed, with B visible as historical context. Concretely: C has a completed run → select it; C completed with zero rows → select that empty run, never resurrect B; B's Extracted Proposals are actionable only through an explicit historical override, never by default.

The **Current Production Run is declared in run lineage, never inferred** as `MAX(id)` or `MAX(completed_at)` — otherwise an experimental prompt run or a backfilled receipt silently becomes reviewer work. The resolver returns exact run ids, which requires ADR-0018's prerequisite that every Extracted Proposal identify its `ExtractionRun`. The three readers share the completion and lineage definitions but select explicitly: the queue reads the active actionable run, a Revision Comparison pins its two run ids, and an Extraction Measurement explicitly names its exact Extraction Runs. None inherits another's default — a measurement population that tracked "latest" would change silently under a new run.

The reviewer surface is **two processing scopes behind one interface**, which is what actually prevents the two-queue incident from recurring one layer up: an unchanged successor row whose predecessor is already linked to a Constraint routes through Document Revision Review, where a valid policy may carry it automatically and otherwise it remains Human Support Update work; a new, changed, ambiguous, or never-adjudicated row routes to a Human Record Decision on the Extracted Proposal; historical rows route to neither. Raw run scoping without routing would list every unchanged row in both scopes.

## Consequences

Failed attempts must become durable run receipts. Today `NoMatrixFound` and `ExtractionFailed` append to a transient outcome list and no `ExtractionRun` row is written, so `awaiting_extraction` and `extraction_failed` are indistinguishable after the process exits. The completion rule itself stands as is: `page_errors == 0`, zero-row successful runs included (`extraction_runs.py`).

## Considered options

**Predecessor fallback** — keep showing B's Extracted Proposals until C extracts. Rejected: it trades a visible empty processing scope for silently reviewable superseded data, and Document Revision Review already prevents the silence the fallback was for.

**A stored `retired` state** — a mass rewrite on every re-extraction, undo semantics on redo, and a column duplicating what lineage derives.
