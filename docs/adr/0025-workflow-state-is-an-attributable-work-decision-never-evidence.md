---
status: accepted
---

# Workflow state is an attributable Coordination Decision, never Supporting Documentation

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

The pilot's coordination loop assigns a project person, sets a Next Action and an Action Due Date, and the Coordination Report must show Assigned To and Next Action — the build spec promised those two and never built them; the Action Due Date is new with this decision — while ADR-0003 admitted exactly two provenance classes: an Assertion (what a document said) and a Derivation (what the rules computed). Decision: project-controlled coordination state is a third class, the **Coordination Decision** — an attributable project-team decision recorded at a stated time, proving only what the project decided and when. This amends ADR-0003 the way ADR-0003 amended the original zero-uncited-assertions rule: "no cell is bare" is unchanged in force and generalized in class — the verifier accepts exactly three classes, and the build spec's report section moves with it. Queryable current values live on the Coordination Decision's exact subject, and every change writes a typed, immutable Coordination Decision receipt carrying the recording principal, the timestamp, exact before/after values, and its predecessor decision — the generic audit log is not the record. Later decisions supersede, never erase.

The boundaries are the decision. The recording principal and the assigned person are different facts on the receipt — who decided versus who is accountable — and one person may lawfully be both: self-assignment is recorded like any other assignment, never implied. The Action Due Date is neither the Required By date nor the timing shown as Promised For. A document naming a person responsible for follow-up remains an Assertion; adopting that assignment operationally is a separate Coordination Decision. A missing assignment or action is a Derivation over the absence of a current Coordination Decision, never a stored flag. A Coordination Decision can never change a Utility Conflict Resolution Method or its derived work-type filter, perform a Documentation Review, or establish an External Organization's statement or performance — attribution proves who decided, not that the work occurred. Document-sourced factual Assertions require Supporting Documentation (`CONTEXT.md`, Supporting Documentation); a Recorded Verbal Statement has its own provenance class under ADR-0033.

Publication tightens with it rather than merely gaining three columns: a report cell carries field-exact provenance — the exact Coordination Decision ids it displays, the Assertion behind each document-sourced value, or the Derivation that computed it — replacing the record-level citation the report currently reuses across unrelated fields (`report.py`).

**Implementation-name boundary.** Coordination Decision is the adopted domain name. The retained model and publication provenance class are `WorkDecision`; historical receipts keep that identity. This ADR introduced the third class; ADR-0033 later added `Verbal` as the fourth. The current verifier is not restricted to the three classes that existed when this decision was made.

## Considered options

**Keep the record pure and workflow outside it.** Rejected: the Phase-1 exit criterion requires a practitioner to run the weekly meeting from the tool alone; a companion worklist outside the record makes the report a partial artifact and the build spec's owner and action columns a dead letter.

**Route assignments through Assertions.** Rejected: honest only when a document actually names the owner; most assignments are made in Corridor itself and would have to invent a citing document.

## Amendment: a Coordination Decision may follow an External Organization statement

ADR-0038 broadens this ADR's original Constraint-only ownership rule. A Coordination Decision now has exactly one subject: one Constraint or one accepted External Organization
Commitment or Change to Promised Timing, never both. This is necessary when a real
party-level statement needs a project response before its Constraint scope is
known. The statement's Follow-up Plan follows that statement lineage and is not
copied to Constraints later linked by a scope decision. The provenance boundary
above is unchanged: a Coordination Decision records only the project's response and can
never establish or close an External Organization fact.

## Consequences

The Phase 1/Phase 2 boundary is redrawn explicitly rather than dissolved: Phase 1 carries single-operator, attributable internal Coordination Decisions; Phase 2 carries multi-user assignment, acknowledgment, approvals, notifications, and external-party response flows (`phase-1-roadmap.md`, amended alongside this ADR). The M5 bar — zero uncited assertions — survives unchanged, because a Coordination Decision is provenance without being a source: nothing about the world is claimed, so nothing about the world needs a quote.
