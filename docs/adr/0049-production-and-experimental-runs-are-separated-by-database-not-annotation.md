---
status: accepted
---

# Production and experimental runs are separated by database, not annotation

An Extraction Run is one recorded attempt to read one Document. A test read must never become the reading a project uses. ADR-0034 required the implementation to distinguish production runs from experiments before automatic Current Production Run selection ships. The planned fix was a purpose label on every run, plus a migration to decide what old unlabeled runs mean. The decision ruling is on [#323](https://github.com/bharm16/corridor/issues/323).

## Decision

Separate by location, not by label.

- The production database contains only production work. That is the rule, not an observation.
- Experiments, model tryouts, measurements, and replays run only in disposable database copies. Every experiment surface in this repository already works this way (acceptance, proving, rehearsal, eval).
- Experiment commands must name an explicit non-default database. They refuse the production one.
- Old runs need no treatment. They are in the production database. Therefore they are production, by the same rule. No label column. No migration. No per-run claims about the past.
- Every existing Current Production Run selection stays as it is. Old completed runs count as completed work, so no Document is re-read to satisfy this decision.
- If a person later finds that one old run was a test, that person excludes that one run, with their name on the exclusion.

## Rejected

The purpose label. It writes "production" onto old records nobody can vouch for, threads a column through every entry point, and filters every selection — all for the same guarantee location gives for free. The only reason to want labels is wanting test runs inside the production database. No such case exists.

## Consequences

- ADR-0034's distinguish-production prerequisite is satisfied by location. This amends that consequence; it does not weaken it.
- ADR-0024 already kept acceptance captures out of production lineage. This decision generalizes that pattern.
- Ticket #341 becomes the enforcement guard plus its tests. Ticket #368 runs candidate models in disposable copies and no longer waits on #341.
