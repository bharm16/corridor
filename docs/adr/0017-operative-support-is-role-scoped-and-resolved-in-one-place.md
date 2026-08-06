# Operative support is role-scoped, designated at adjudication, and resolved in one place

> ADR-0022 permits an authorized Automatic Carry-Forward to inherit already-designated scopes onto exact unchanged successor Evidence. Initial designation remains a human Adjudication decision.

"This Evidence closes the readiness bar" and "this Evidence supports the value being printed" are different facts, and the model already knows it: every non-Ready record with a report citation is operative-but-not-satisfying, a completion letter may satisfy readiness while an earlier matrix row supports the owner and stationing, and `Assertion` already binds individual fields to individual `EvidenceLink`s. So operative support (ADR-0016) is scoped by role — readiness, published record, later published field — not expressed as one boolean.

The pieces keep their single meanings. `verified` stays mechanical: the quote is on the page, nothing more. `satisfies_requirement` stays readiness-specific — and it is not unique, so it could never answer "which link is operative" alone: marking revision C satisfying does not make revision B historical. Publication support is a new role-scoped designation, first set as part of ordinary accept/edit/merge Adjudication rather than as another reviewer click. Human Reconfirmation or valid Automatic Carry-Forward may move the already-established publication designation and readiness support together where its own stricter eligibility rules hold.

One resolver derives operative support for every reader — readiness, reports, exports, Exceptions — so "what does this record stand on" has exactly one implementation. The report's "first verified link by id" policy is retired outright, not reordered: which quote backs a cell becomes a resolver answer, never an id accident.

## Considered options

**Overload `satisfies_requirement` as the unified operative mark.** Rejected: preferring satisfying links in `primary_evidence` could replace an arbitrary citation with a confidently wrong one — proof that work closed does not prove the printed values. Also unfalsifiable as a fix today: the live database holds 141 verified links and zero satisfying ones (as of 2026-08-05), so the defect that unification would repair is not presently observable.

**Recorded re-review events as the source of truth.** Rejected: every reader would reconstruct current truth through temporal event joins — unnecessary complexity, and the conclusion belongs in the Ledger as a stored, adjudicated fact.
