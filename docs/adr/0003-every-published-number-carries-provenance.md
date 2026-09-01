---
status: accepted
domain: supporting-documentation
scope: current product
amended_by:
  - ADR-0013
  - ADR-0077
  - ADR-0082
---

# Every published number has source or calculation provenance

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> ADR-0025 adds **Coordination Decision** and ADR-0033 adds **Recorded Verbal Statement**. "No cell is bare" is unchanged. The four retained technical provenance classes are `Assertion`, `Derivation`, `WorkDecision`, and `Verbal`; their explanatory labels are source field value, calculated result, Coordination Decision, and Recorded Verbal Statement. This amendment changes no serialized class identifier.

The weekly report's headline cells are aggregates — key-date summaries, counts by constraint alert rule, "% with verified evidence". No document contains those numbers, so the rule "zero uncited assertions, enforced by the verifier" was unenforceable for exactly the figures an audience reads first. The rule would have had to be quietly exempted, leaving the Definition of Done stronger than what was actually checked.

This ADR introduced two provenance classes, later extended to the four listed above:

- an **Assertion**, explained as a source field value — document, page, and verified quote
- a **Derivation**, explained as a calculated result — the ruleset version plus the record IDs aggregated, drilling through to those records' Supporting Documentation

The verifier enforces that **no cell is bare**. This generalizes the original rule rather than weakening it.

## Consequences

Coordination Report generation must retain the record set behind every aggregate, not just its computed value. The ruleset version must be recorded alongside it, so that a number which changed between two weekly reports can be attributed to a rule change rather than a data change — without it, the "changes since last report" section cannot distinguish the project moving from the rules moving.

Making a percentage clickable down to the Supporting Documentation beneath it is also the strongest moment in the Coordination Report: the first question any skeptical reader asks is where the number came from.
