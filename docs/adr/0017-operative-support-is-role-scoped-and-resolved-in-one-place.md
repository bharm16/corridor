---
status: accepted
---

# Operative support is role-scoped, designated at adjudication, and resolved in one place

> **Terminology amendment, 2026-08-27 — [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Documentation Review and Supporting Documentation replace the earlier readiness and Evidence terms. The documentation role remains distinct from publication support; code identifiers retain their original names.

> ADR-0029 permits a deterministic Admission policy to designate initial publication support for the exact row it admits. ADR-0037 keeps the initial Documentation Review judgment human, and ADR-0034 makes Automatic Carry-Forward normal fail-closed processing for already-established roles.

Supporting Documentation can meet a stated documentation requirement or support a value being published; those are different facts. A record can have a Report citation while its documentation requirement remains unmet. A completion letter may meet that requirement while an earlier matrix row supports the Utility Owner and Stationing. `Assertion` already binds individual fields to individual `EvidenceLink`s. So Operative Support (ADR-0016) is scoped by role — documentation sufficiency, published record, later published field — not expressed as one boolean.

The pieces keep their single meanings. `verified` stays mechanical: the quote is on the page, nothing more. `satisfies_requirement` stays specific to the Documentation Review judgment — and it is not unique, so it could never answer "which link is operative" alone: marking revision C satisfying does not make revision B historical. Publication support is a separate role-scoped designation, first set as part of ordinary accept/edit/merge Adjudication rather than as another reviewer click, or by the exact Admission policy described above. Human Reconfirmation or valid Automatic Carry-Forward may move the already-established publication designation and support for the Documentation Review judgment together where its own stricter eligibility rules hold.

One resolver derives Operative Support for every reader — documentation sufficiency, Reports, exports, Exceptions — so "what does this record stand on" has exactly one implementation. The Report's "first verified link by id" policy is retired outright, not reordered: which quote backs a cell becomes a resolver answer, never an id accident.

## Considered options

**Overload `satisfies_requirement` as the unified operative mark.** Rejected: preferring satisfying links in `primary_evidence` could replace an arbitrary citation with a confidently wrong one — Supporting Documentation for completion does not prove the printed values. Also unfalsifiable against the database at the time of the decision: it held 141 verified links and zero satisfying ones (as of 2026-08-05), so the defect that unification would repair was not then observable.

**Recorded re-review events as the source of truth.** Rejected: every reader would reconstruct current truth through temporal event joins — unnecessary complexity, and the conclusion belongs in the Ledger as a stored, adjudicated fact.
