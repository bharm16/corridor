---
status: accepted
domain: supporting-documentation
scope: current product
---

# A superseded citation is a signal on supporting documentation in use; documentation sufficiency needs the current revision

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> ADR-0022 introduced Automatic Support Update for exact unchanged support; ADR-0034 made it normal processing without project authorization. Human Support Update remains the path for reviewer judgment; the signal may also clear through a valid automatic support-update receipt.

When a Supersession is registered (ADR-0015), the Ledger may hold records whose supporting sources cite the replaced revision. Corridor computes `SUPERSEDED_CITATION` for every Constraint whose **Supporting Documentation in Use** — the Supporting Documentation currently meeting its stated requirement or backing what a Coordination Report would print — relies on a non-current Document. The signal exists the moment the registry knows: extraction and comparison results annotate it (`awaiting_extraction`, `unmatched`, `unchanged`, `changed`, `dropped`) but never gate its existence. It clears only when Supporting Documentation in Use references the current revision through Human Support Update or valid Automatic Support Update.

The predicate is Supporting Documentation in Use, never "any old link exists." Corridor preserves superseded Assertions and Supporting Documentation by design; a blanket predicate would fire on history that is doing no work, could never clear without deleting that history, and would quietly pressure users to erase provenance to silence it.

Documentation sufficiency follows the same line: when a Constraint's only satisfying source is superseded, the documentation requirement is no longer met until the established human judgment has current support through Human Support Update or Automatic Support Update. The old Supporting Documentation stays verified and preserved; only its applicability to the current conclusion changes. This extends ADR-0002's derivation and is why a registry event can change that conclusion without an edit to the Constraint.

The name is `SUPERSEDED_CITATION`, not `SUPERSEDED_EVIDENCE`: the Supporting Documentation remains verified — the quote is still on the page — and what lapsed is its currency as support.

## Consequences

"Days since superseded" reads the replacement date the index states, declared with the supersession itself (ADR-0015), never inferred from the successor's `doc_date` or ingestion time.

In the UI the signal concerns supporting documents, not a schedule failure: show the number of records **using a replaced supporting document**, distinguishing automatically eligible rows from reviewer work rather than presenting that number as new overdue items.

Human Support Update remains available where the current revision's row is a mechanically verified, unambiguous unchanged match. The active Automatic Support Update Rules may apply to their stricter exact subset automatically. Rows that are ineligible for the automatic update but safe for a Human Support Update remain human work; unsafe rows require a Human Record Decision or Abstention.

## Considered options

**Diff-gated existence** — fire only where the new revision's extraction disagrees or drops the row. Quieter, but silent in exactly the window M8 exists to illuminate — between registration of the successor and review of its extraction — and "unchanged" still leaves proof citing a withdrawn revision.

**Upload-report-only** — surface affected records once, at registration. A one-time event a user can miss, where everything else in this system is a standing query (ADR-0010).

**Blanket predicate over all links** — permanent false positives, and preservation of history becomes the thing being punished.
