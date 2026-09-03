---
status: accepted
domain: supporting-documentation
scope: current product
amended_by:
  - ADR-0077
  - ADR-0082
---

# Supporting documentation in use is selected by purpose and resolved in one place

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> **Terminology note, 2026-09-03 — [ADR-0082](0082-provenance-follows-the-value-class-and-support-is-a-relation.md), #493.** The mechanical `verified` named in this ADR's prose is the **Source Passage Check**, and its result is the locator validation status `valid`, `invalid`, or `not_checked`, presented as Found at cited location, Not found at cited location, or No cited location recorded (#600) ([Corridor Operations glossary](../operations/CONTEXT.md), [state-label research](../research/source-passage-check-state-labels-2026-09-03.md)). The column name `verified` is retained as the compatibility projection `locator_validation_status == valid` and is removed under #458. This note changes no authority: the check stays mechanical, and the role-scoped resolver still may never read support out of it.

> ADR-0029 permits a deterministic Record Inclusion policy to designate initial publication support for the exact row it admits. ADR-0037 keeps the initial Documentation Review judgment human, and ADR-0034 makes Automatic Support Update normal fail-closed processing for already-established roles.

Supporting Documentation can meet a stated documentation requirement or support a value being published; those are different facts. A record can have a Coordination Report citation while its documentation requirement remains unmet. A completion letter may meet that requirement while an earlier matrix row supports the Utility Owner and Stationing. `Assertion` already binds individual fields to individual `EvidenceLink`s. So Supporting Documentation in Use (ADR-0016) is scoped by role — documentation sufficiency, published record, later published field — not expressed as one boolean.

The customer sees **Used for this value** or **Used for this review**, with the exact cited passage. In use does not mean the source is the current revision: the selection and the source's revision status are separate facts, and a replaced source keeps the warning required by ADR-0016.

The pieces keep their single meanings. `verified` stays mechanical: the quote is on the page, nothing more. `satisfies_requirement` stays specific to the Documentation Review judgment — and it is not unique, so it could never answer "which link is operative" alone: marking revision C satisfying does not make revision B historical. Publication support is a separate role-scoped designation, first set as part of an ordinary accept, edit, or merge Human Record Decision rather than as another reviewer click, or by the exact Record Inclusion policy described above. Human Support Update or valid Automatic Support Update may move the already-established publication designation and support for the Documentation Review judgment together where its own stricter eligibility rules hold.

One resolver derives Supporting Documentation in Use for every reader — documentation sufficiency, Coordination Reports, exports, Constraint Alerts — so "what does this record stand on" has exactly one implementation. The Coordination Report's "first verified link by id" policy is retired outright, not reordered: which quote backs a cell becomes a resolver answer, never an id accident.

## Considered options

**Overload `satisfies_requirement` as the unified operative mark.** Rejected: preferring satisfying links in `primary_evidence` could replace an arbitrary citation with a confidently wrong one — Supporting Documentation for completion does not prove the printed values. Also unfalsifiable against the database at the time of the decision: it held 141 verified links and zero satisfying ones (as of 2026-08-05), so the defect that unification would repair was not then observable.

**Recorded re-review events as the source of truth.** Rejected: every reader would reconstruct current truth through temporal event joins — unnecessary complexity, and the conclusion belongs in the Ledger as a stored, adjudicated fact.
