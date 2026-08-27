---
status: accepted
---

# A superseded citation is a signal on operative support; documentation sufficiency needs the current revision

> **Terminology amendment, 2026-08-27 — [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses Constraint, Supporting Documentation, and Documentation Review. The former Ready condition means the documentation requirement is met; loss of current support does not establish that physical work became unfinished. Stored rule names remain unchanged.

> ADR-0022 introduced Automatic Carry-Forward for exact unchanged support; ADR-0034 made it normal processing without project authorization. Human Reconfirmation remains the path for reviewer judgment; the signal may also clear through a valid carry-forward receipt.

When a Supersession is registered (ADR-0015), the Ledger may hold records whose supporting sources cite the replaced revision. Corridor computes `SUPERSEDED_CITATION` for every Constraint whose **Operative Support** — the Supporting Documentation currently meeting its stated requirement or backing what a Report would print — relies on a non-current Document. The signal exists the moment the registry knows: extraction and comparison results annotate it (`awaiting_extraction`, `unmatched`, `unchanged`, `changed`, `dropped`) but never gate its existence. It clears only when Operative Support references the current revision through human Reconfirmation or valid Automatic Carry-Forward.

The predicate is Operative Support, never "any old link exists." Corridor preserves superseded Assertions and Supporting Documentation by design; a blanket predicate would fire on history that is doing no work, could never clear without deleting that history, and would quietly pressure users to erase provenance to silence it.

Documentation sufficiency follows the same line: when a Constraint's only satisfying source is superseded, the documentation requirement is no longer met until the established human judgment has current support through Reconfirmation or Automatic Carry-Forward. The old Supporting Documentation stays verified and preserved; only its applicability to the current conclusion changes. This extends ADR-0002's derivation and is why a registry event can change that conclusion without an edit to the Constraint.

The name is `SUPERSEDED_CITATION`, not `SUPERSEDED_EVIDENCE`: the Supporting Documentation remains verified — the quote is still on the page — and what lapsed is its currency as support.

## Consequences

"Days since superseded" reads the replacement date the index states, declared with the supersession itself (ADR-0015), never inferred from the successor's `doc_date` or ingestion time.

In the UI the signal is a provenance-review facet, not a schedule failure: registering a revision must read as "N records have Operative Support on a superseded revision," distinguishing automatically eligible rows from reviewer work rather than presenting N new overdue items.

Human Reconfirmation remains available where the current revision's row is a mechanically verified, unambiguous unchanged match. An active Carry-Forward Policy may consume its stricter exact subset automatically; policy-ineligible but Reconfirmation-safe rows remain human work, while unsafe rows route to ordinary Adjudication or Abstention.

## Considered options

**Diff-gated existence** — fire only where the new revision's extraction disagrees or drops the row. Quieter, but silent in exactly the window M8 exists to illuminate — between registration of the successor and review of its extraction — and "unchanged" still leaves proof citing a withdrawn revision.

**Upload-report-only** — surface affected records once, at registration. A one-time event a user can miss, where everything else in this system is a standing query (ADR-0010).

**Blanket predicate over all links** — permanent false positives, and preservation of history becomes the thing being punished.
