# The queue fails closed on a registered successor, and the Active Run is declared, not latest

> ADR-0022 adds Automatic Carry-Forward before reviewer presentation for the exact policy-eligible subset. Rows that are not carried retain the two-lane routing decided here.

Once Extraction Runs and Candidates become append-preserving (ADR-0018), the default review queue is computed scope: pending Candidates of the current revision's **Active Run**, derived at query time from registry and run lineage. Stored retirement states would duplicate what the lineage already answers; Candidate deletion is incompatible with the Comparison's receipt. Both stay rejected.

The scope **fails closed**. The moment revision C is registered, revision B's pending Candidates leave the default actionable lane — even before C has a usable extraction. A reviewer offered B's rows in that interval can accept known-superseded information, minting a Ledger record whose support fires `SUPERSEDED_CITATION` at birth, over a row that may have changed or been dropped in C. The interval is not silent: Supersession Review shows `awaiting_extraction` when C has not run and `extraction_failed` when it tried and failed, with B visible as historical context. Concretely: C has a completed run → select it; C completed with zero rows → select that empty run, never resurrect B; B's Candidates are actionable only through an explicit historical override, never by default.

The **Active Run is declared in run lineage, never inferred** as `MAX(id)` or `MAX(completed_at)` — otherwise an experimental prompt run or a backfilled receipt silently becomes reviewer work. The resolver returns exact run ids, which requires ADR-0018's prerequisite that every Candidate identify its `ExtractionRun`. The three readers share the completion and lineage definitions but select explicitly: the queue reads the active actionable run, a Revision Comparison pins its two run ids, and an Extraction Measurement explicitly names its exact Extraction Runs. None inherits another's default — a measurement population that tracked "latest" would change silently under a new run.

The reviewer surface is **two lanes behind one interface**, which is what actually prevents the two-queue incident from recurring one layer up: an unchanged successor row whose predecessor is already linked to a Dependency routes through Supersession Review, where a valid policy may carry it automatically and otherwise it remains human Reconfirmation work; a new, changed, ambiguous, or never-adjudicated row routes to Candidate Adjudication; historical rows route to neither. Raw run scoping without routing would list every unchanged row in both lanes.

## Consequences

Failed attempts must become durable run receipts. Today `NoMatrixFound` and `ExtractionFailed` append to a transient outcome list and no `ExtractionRun` row is written, so `awaiting_extraction` and `extraction_failed` are indistinguishable after the process exits. The completion rule itself stands as is: `page_errors == 0`, zero-row successful runs included (`extraction_runs.py`).

## Considered options

**Predecessor fallback** — keep showing B's Candidates until C extracts. Rejected: it trades a visible empty lane for silently reviewable superseded data, and Supersession Review already prevents the silence the fallback was for.

**A stored `retired` state** — a mass rewrite on every re-extraction, undo semantics on redo, and a column duplicating what lineage derives.
