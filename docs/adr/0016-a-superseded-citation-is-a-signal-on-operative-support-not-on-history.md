# A superseded citation is a signal on operative support, not on history — and readiness stands on the current revision

> ADR-0022 adds policy-authorized Automatic Carry-Forward for exact unchanged support. Human Reconfirmation remains the path for reviewer judgment; the signal may also clear through a valid carry-forward receipt.

When a supersession is registered (ADR-0015), the Ledger may hold records whose proof cites the replaced revision. Corridor computes `SUPERSEDED_CITATION` for every Dependency whose **operative support** — the Evidence currently making it Ready or backing what a report would print — relies on a non-current document. The signal exists the moment the registry knows: extraction and comparison results annotate it (`awaiting_extraction`, `unmatched`, `unchanged`, `changed`, `dropped`) but never gate its existence. It clears only when operative support references the current revision through human Reconfirmation or valid Automatic Carry-Forward.

The predicate is operative support, never "any old link exists." Corridor preserves superseded Assertions and Evidence by design; a blanket predicate would fire on history that is doing no work, could never clear without deleting that history, and would quietly pressure users to erase provenance to silence it.

Readiness follows the same line: when a Dependency's only satisfying proof cites a superseded document, it no longer evaluates Ready until existing sufficiency moves to current Evidence through Reconfirmation or Automatic Carry-Forward. The old Evidence stays verified and preserved; only present readiness changes. This extends ADR-0002's derivation and is why readiness can lapse on a registry event with no edit to the record.

The name is `SUPERSEDED_CITATION`, not `SUPERSEDED_EVIDENCE`: the Evidence remains verified — the quote is still on the page — and what lapsed is its currency as support.

## Consequences

"Days since superseded" reads the replacement date the index states, declared with the supersession itself (ADR-0015), never inferred from the successor's `doc_date` or ingestion time.

In the UI the signal is a provenance-review facet, not a schedule failure: registering a revision must read as "N records have Operative Support on a superseded revision," distinguishing automatically eligible rows from reviewer work rather than presenting N new overdue items.

Human Reconfirmation remains available where the current revision's row is a mechanically verified, unambiguous unchanged match. An active Carry-Forward Policy may consume its stricter exact subset automatically; policy-ineligible but Reconfirmation-safe rows remain human work, while unsafe rows route to ordinary Adjudication or Abstention.

## Considered options

**Diff-gated existence** — fire only where the new revision's extraction disagrees or drops the row. Quieter, but silent in exactly the window M8 exists to illuminate — between registration of the successor and review of its extraction — and "unchanged" still leaves proof citing a withdrawn revision.

**Upload-report-only** — surface affected records once, at registration. A one-time event a user can miss, where everything else in this system is a standing query (ADR-0010).

**Blanket predicate over all links** — permanent false positives, and preservation of history becomes the thing being punished.
