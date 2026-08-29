---
status: accepted
---

# Setup asks only questions the system cannot answer

Project setup had four human confirmation acts: acknowledge the setup screen, confirm drafted requirement checklists, set a standing assignee per organization, and confirm the key-dates import. Review on 2026-08-28 found most of that is ceremony — a person approving things the data already determines. This ADR keeps only the genuine questions.

## Decision

- **The setup acknowledgment act is removed for now.** It gates nothing mechanical, so the click does nothing. The setup view and the correction path stay — seeing what operations configured is useful. A recorded customer acknowledgment returns only when there is a customer relationship to protect.
- **Standard requirement checklists apply by themselves.** The row's selector fields (resolution strategy, cost responsibility) pick each Constraint's checklist with no per-conflict setup — 500 conflicts, zero confirmations. A person rules only on **deviations**: clauses in the project's own contract documents that add or change documentation requirements, found and flagged by the model, each accepted or rejected with the clause displayed. A standard contract needs zero requirement setup.
- **A structured schedule file imports itself**, versioned, with no confirmation — the same rule as every structured file. The human questions that remain: pick once which schedule activities govern utility work (the file does not say), and approve moved deadlines explicitly when a re-import shifts a date (the repinning rule stays open as #322 decision gate 5). The hand-typed CSV stopgap keeps its preview, because a person typed it and people typo.
- **Assignment works like any ticket system.** Every Work Item is assignable, reassignable, and claimable at any time — that per-item layer already exists, append-only and attributable. The per-organization standing rule is only a **default assignee**, like a component default in a ticket tracker: it exists so a statement that records itself at 2 a.m. lands on someone's list. One coordinator on the roster means zero setup. A wrong default is corrected at the rule.

## What setup becomes

Pick the governing schedule dates. Set a default assignee only if the roster has more than one coordinator. Rule on the contract's flagged extras. On a small project with a standard contract: almost nothing.

## Consequences

Amends ADR-0052 (setup confirmation of requirements narrows to deviations only) and refines ADR-0054's standing-rule wording (a default, not a mandate). Reshapes tickets #336 and #337. The CSV preview in #337 and the drafting assist in #363 are unchanged where a human authored the input.
