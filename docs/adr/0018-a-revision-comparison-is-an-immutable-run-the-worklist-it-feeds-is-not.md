---
status: accepted
---

# A Revision Comparison is an immutable run; the worklist it feeds is not

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

M8's "re-extraction diff" is two domain objects with two lifecycles, not one. A **Revision Comparison** owns row correspondence and field differences between a document's extraction run and its successor's — and it is historically stable. A **Document Revision Review** is the live, Ledger-facing worklist derived from the supersession registry plus the resolver for supporting documentation in use (ADR-0017), enriched by a Comparison when one exists — it must show `awaiting_extraction` and `extraction_failed` before any Comparison can exist, so it never waits for one — and it changes as affected work is resolved through Human Support Update, Automatic Support Update, or Human Record Decision. One screen may show both; one object may not be both. Combined, the artifact either goes stale or history rewrites itself.

The Comparison persists as an append-only run receipt: predecessor and successor document ids, the exact extraction-run ids compared, extraction schema, prompt and model versions, matcher version and configuration, generated time, and the row correspondences with their ambiguities and field differences. The rendering is regenerable from those pinned inputs, but once the result has been resolved and cited, the receipt and findings persist — a later matcher produces a *new* run rather than silently changing what justified an earlier action.

It is not made of Derivations. A Derivation drills through to the Supporting Documentation beneath cited Constraints, or names a run when no record can answer (`report.py`); source rows without an admitted Constraint have no Constraint ids, so the Comparison's own findings cannot honestly wear that class. The composition runs the other way: a published cell *about* a comparison — "12 rows dropped since the current revision" — is an ordinary Derivation naming the `RevisionComparisonRun` it covers, exactly the escape hatch ADR-0013 already built.

The matcher is its own algorithm with its own version. It shares identity primitives with `merge.py` — Stationing, normalization — but not `rank_matches()`, which suggests matches from an Extracted Proposal to existing Constraints: it excludes placeholder organizations, excludes records already citing the source document, returns independent top matches rather than a global one-to-one correspondence, and carries thresholds tuned for merge suggestions. The canonical findings are `added`, `dropped`, `unchanged`, `changed`, `ambiguous`, and `unmatched`. Fan-in and fan-out remain `ambiguous` until real evidence justifies additional states. Nothing is `dropped` unless successor extraction completed successfully: a failed extraction plus no match is not evidence a row disappeared.

## Consequences

Extraction must become run-scoped and immutable first: `ExtractionRun` records only document, prompt version and counts today, Extracted Proposals are not linked to their run, and `_clear_pending` deletes un-adjudicated Extracted Proposals on re-extraction — under which a durable comparison of never-adjudicated rows is impossible. How the review queue keeps its one-queue property once nothing is deleted is a separate decision.

## Considered options

**One regenerable view whose claims are Derivations.** Rejected: rows without an admitted Constraint cannot drill to its Supporting Documentation, and a regenerable justification trail is no trail — what a reviewer acted on must persist as acted on.

**Naming it Revision Diff.** Rejected: "diff" implies an exactness of correspondence the matcher cannot always establish. "Change report" is worse — Coordination Report already means the published weekly artifact.
