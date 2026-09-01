---
status: accepted
domain: operations
scope: current product
amends:
  - ADR-0055
amended_by:
  - ADR-0076
---

# Schedule dates flow through and link by the schedule's own data

ADR-0055 still had a person picking the governing schedule dates from a list and approving moved deadlines. Review on 2026-08-28 (late evening) found both are the same mistake caught three times already: a human placed where the data answers. The schedule is a living document; new information about a specific conflict is the point, not a threat.

## Decision

**Governing dates identify themselves.** Schedule activities carry codes, names, and locations ("UTIL-RELO-B — utility relocations complete, Segment B"). Activities whose codes and names match utility conventions flag themselves as the governing set. A person rules only when the schedule's coding is too poor to read.

**Conflicts link by location.** A conflict at station 102+50 links to the activity covering stations 100+00–150+00 — an exact rule when exactly one activity matches, a card showing candidates when there is a tie, the same matching pattern as company names (ADR-0051) and statement scope (ADR-0054).

**New dates apply automatically, per row.** When a schedule revision carries a new date for a conflict, that conflict's row updates the moment the file lands. No flag-and-approve gate. Nobody blesses reality. The system's job is to surface what the change means — a conflict that had months now has weeks, chasing that is now slack, a decision that referenced the old date — as attention, never as approval.

**Why no gate is safe:** every date version is retained (history cannot be rewritten) and approved reports are frozen bytes (published claims cannot be rewritten). With those two guarantees the approval gate protected nothing.

**One boundary stays.** The schedule states what the project needs (Required By). A statement states what the organization promised (Promised For). A new schedule updates the first automatically and never touches the second.

## Consequences

Amends ADR-0055's key-dates clauses (the pick-once list and the explicit-repin approval are gone; the hand-typed CSV preview stays). Resolves #322 decision gate 5 (schedule-change reassessment): automatic adoption, surfaced impact, versioned history. Reshapes #337; the linking matcher is its own ticket.
