# Every published number is an Assertion or a Derivation

> ADR-0025 adds a third class: a **Work Decision** — what the project decided, attributed to a principal at a stated time. "No cell is bare" is unchanged; the classes are three.

The weekly report's headline cells are aggregates — milestone rollups, counts by exception rule, "% with verified evidence". No document contains those numbers, so the rule "zero uncited assertions, enforced by the verifier" was unenforceable for exactly the figures an audience reads first. The rule would have had to be quietly exempted, leaving the Definition of Done stronger than what was actually checked.

Every published cell now carries one of two provenance classes:

- an **Assertion** — document, page, and verified quote
- a **Derivation** — the ruleset version plus the record IDs aggregated, drilling through to those records' Evidence

The verifier enforces that **no cell is bare**. This generalizes the original rule rather than weakening it.

## Consequences

Report generation must retain the record set behind every aggregate, not just its computed value. The ruleset version must be recorded alongside it, so that a number which changed between two weekly reports can be attributed to a rule change rather than a data change — without it, the "changes since last report" section cannot distinguish the project moving from the rules moving.

Making a percentage clickable down to the evidence beneath it is also the strongest moment in the report: the first question any skeptical reader asks is where the number came from.
