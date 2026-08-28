---
status: accepted
---

# One approvals table, one runs table; outcomes stay with their family

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> ADR-0029 and ADR-0034 remove project approval as authority for Record Inclusion and Automatic Support Update. `policy_approvals` now preserves historical approvals; current Corridor-managed Policy Runs bind their released policy version and digest directly.

Three policy families write records or move support under an accountable principal's authorization: Automatic Support Update (ADR-0022), event record inclusion (ADR-0026), record inclusion of Constraints (ADR-0027). Each arrived with a private copy of the same three tables — approvals, runs, outcomes — because each ADR said "mirror the family you joined," and mirroring meant copying. The copies have already rotted measurably: the Automatic Support Update runs and outcomes tables carry a third trigger — a deferred counts-reconciliation check nobody re-created for the record inclusion families — so a record inclusion run's stored counts could silently disagree with its own outcome rows while an Automatic Support Update run's cannot. Nobody decided that difference. And a fourth family is already forecast by our own refusal message: agreement documents "need their own eligibility rule."

Decision: **the approval tables join into one `policy_approvals` and the run tables join into one `policy_runs`, each row naming its family; the outcome tables stay with their families.** The dividing rule is the one the analysis stated: join where it makes the enforcement stronger, keep apart where it would weaken it. The approval and run shapes were column-for-column identical across families (one count column's name aside — now the neutral `applied_count`), so three copies bought three chances at drift and zero enforcement. The outcome shapes genuinely differ — Automatic Support Update's records a support move with nine foreign keys; the record inclusion families' record a Record Inclusion with three — and that difference does work: the columns are the receipt.

Two guarantees move up a level with the join rather than being lost. The family rule — an event outcome may point only at an event run — stays in the schema: outcome tables carry their family as a literal-checked column, and their run reference is a composite foreign key on `(family, run_id)`, so a cross-family pointer is a constraint violation, not a code-review hope. And the counts-reconciliation trigger that only Automatic Support Update had becomes the shared table's trigger, dispatching on family to the family's own outcome table — the drift is not merely stopped, the missing enforcement is retrofitted to every family at once.

The moment is chosen deliberately: the record inclusion tables are empty and the Automatic Support Update tables hold one approval and one run. The migration moves two rows today; after the operator signs the loading policies it would move hundreds of immutable receipts by dropping and rebuilding their guards. The same reshaping next month is records surgery. Joining the *code* was already done (the shared digest and authorization module); this closes the schema half, and the stored Automatic Support Update approval pauses by design — its digest covers `models.py`, which this changes — so the operator re-signs once, which is the system working.

## Considered options

**Keep the tables separate; share a migration helper for the triggers.** Defensible and zero-risk today, and this repo's taste runs to explicit named things. Rejected because it leaves the drift standing (the record inclusion families still lack the counts check until someone copies it), re-pays three tables and their guards per future family, and the explicitness it preserves duplicates what `policy_json` and the family column already state.

**Join the outcome tables too.** Rejected outright: a single outcomes table needs nullable columns for every family's foreign keys and a code-enforced rule about which may be filled — replacing nine database-enforced references with a convention. Enforcement would weaken, which is the rule's own stop sign.

## Consequences

A new policy family now costs its rules, its outcome table, and two triggers — not three tables and six. `policy.py` gains the family dimension; Automatic Support Update keeps its explicit active-policy pointer and audit re-validation (a decision, not drift — ADR-0022's re-validation reads the audit log, and newest-matching would weaken it). The queue's setup card reads one runs table. The old six tables' migrations remain as history; the join migration records the id remapping of the two moved rows in its own body.
