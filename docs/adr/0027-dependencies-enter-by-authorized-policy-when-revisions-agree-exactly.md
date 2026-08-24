---
status: superseded by ADR-0029
---

# Dependencies enter by authorized policy when revisions agree exactly

> The accountable-human authorization and the two-document agreement gate are superseded by ADR-0029: one matrix admits mechanically, revision agreement is a confirmation signal, and disagreement rides the row as a Dispute. The deterministic checks and the receipt discipline remain in force.

ADR-0026 refined Admission's bar for events: a record enters by a human decision or by a named deterministic policy a human authorized, and a model may only demote. This decision finishes the thought, because stopping at events produced a product whose cold start was bulk human data entry — an operator clicking through every near-certain row of a matrix before the tool says anything useful. The operator refused that shape twice in one day, and the second refusal was of the product itself: a system of record that costs a bulk load of clerical clicking at every onboarding is not a system anyone adopts. Decision: **a dependency Candidate may enter the Ledger under a named, versioned dependency-admission policy an accountable principal authorizes, and the eligibility proof is exact agreement between stated revisions** — the policy names the agreement documents (pinned by content hash), and a conflict admits mechanically when each named document's declared Active Run holds exactly one candidate for its identifier, their fields are byte-identical, the citations are verified on their pages, and the party is unambiguous. Two revisions of the record independently asserting the identical row is stronger evidence than one reviewer glancing at a card; where revisions disagree is exactly where human judgment pays, and that disagreement — with the ambiguous, the missing, and the changed — is all Adjudication ever sees.

The measured case that forced this: SH 99's 36-conflict date cohort. The February and May matrices state byte-identical rows for 34 of the 36; one row changed, one is missing from a dated revision, and one conflict was already excluded for a party re-attribution. Under this decision the cold start is two authorizations and a handful of judgments, and the weekly state is the same machinery running on deltas — events auto-admitting from new minutes, unchanged rows carrying forward under the policy already established (ADR-0022), changed rows queuing for a human. Nobody bulk-clicks at any point in the product's life. The human decision moves up a level, from clicking rows to authorizing rules and judging disagreements, which is where a coordinator's judgment belongs.

The guarantees ride along unchanged from the family this joins: the authorization covers the rules including the deployed bytes of the deciding code, so editing any check pauses the policy until a principal re-authorizes; every run is an immutable receipt of exact outcomes; abstention leaves the Candidate pending and the Ledger unforced; and no model verdict appears anywhere in the path. Adjudication remains the only *judgment* that writes the Ledger, and adjudicate remains the only writer in code: the policy service decides eligibility and the write itself goes through the same materialization the human path uses, under a machine actor the audit log names.

## Considered options

**Keep dependency admission human and bounded (the lane as shipped).** Rejected as the product's answer: it works for 36 rows once, and is exactly wrong at 4,489 or at every new customer. The lane survives — as the residue flow.

**Elect the newest revision and admit its rows.** Rejected: newest-by-date is the inference this system categorically forbids (Active Run, Supersession), and it silently adjudicates every disagreement in favor of recency.

**A model verifies each row against the page and admits.** Rejected in ADR-0026 and equally here: not replayable, and it trades away the provenance story that differentiates the record.

## Consequences

The rehearsal plan's step "the operator admits the cohort through the lane" is superseded; the lane presents only what the policy abstained on. The dependency-admission machinery mirrors the event-admission family — approval, run, outcome tables, immutable, with the same digest discipline — and the two policies are the onboarding story: point at documents, authorize twice, judge the residue, read the report. docs/sh99-date-rehearsal.md is amended alongside this ADR.
