---
status: accepted
---

# Every published number is an Assertion or a Derivation

> **Terminology amendment, 2026-08-27 — [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Supporting Documentation is the current domain term for Evidence. Historical report-label quotations remain unchanged.

> ADR-0025 adds **Work Decision** and ADR-0033 adds **Verbal**. "No cell is bare" is unchanged; the current four provenance classes are Assertion, Derivation, Work Decision, and Verbal.

The weekly report's headline cells are aggregates — milestone rollups, counts by exception rule, "% with verified evidence". No document contains those numbers, so the rule "zero uncited assertions, enforced by the verifier" was unenforceable for exactly the figures an audience reads first. The rule would have had to be quietly exempted, leaving the Definition of Done stronger than what was actually checked.

This ADR introduced two provenance classes, later extended to the four listed above:

- an **Assertion** — document, page, and verified quote
- a **Derivation** — the ruleset version plus the record IDs aggregated, drilling through to those records' Supporting Documentation

The verifier enforces that **no cell is bare**. This generalizes the original rule rather than weakening it.

## Consequences

Report generation must retain the record set behind every aggregate, not just its computed value. The ruleset version must be recorded alongside it, so that a number which changed between two weekly reports can be attributed to a rule change rather than a data change — without it, the "changes since last report" section cannot distinguish the project moving from the rules moving.

Making a percentage clickable down to the Supporting Documentation beneath it is also the strongest moment in the Report: the first question any skeptical reader asks is where the number came from.
