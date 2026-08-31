---
status: accepted
---

# Only intermediary data expires; the record has no delete path

Five assistant request families hard-code `retained_indefinitely`; page renders, raw OCR responses, and copied prompt context accumulate without classification; and the earlier research proposed 30/90-day retention as unlabeled hypotheses. The 2026-08-30 records-retention research grounded the classes in the schedules that bind the customer's world.

Decision: **four retention classes, one hold mechanism, and no deletion machinery for the record itself.**

**Class A — the Project Record: no delete code path exists.** Sources and their bytes, cited segments, facts, typed decisions and actors, statements, released policy identity, revisions, and each Report Approved for Release are permanent by default. This matches the domain need (preserve corrections and history — ADR-0032's posture extended to storage), agency reality (TxDOT's certified schedule keeps utility agreements seven years after the instrument closes, per Tex. Gov't Code §441.1855, and TxDOT's own manual directs permanent retention of executed agreements), and risk: legal-hold exposure concentrates only where deletion exists. Every legal clock in this domain is event-triggered (project acceptance, agreement close, final payment) — never "N years after ingest" — so early deletion buys nothing. Per NARA's AI-records guidance, the record is the output as used, not the machinery that produced it: the `llm_model` and prompt-version provenance on every Extracted Proposal is part of the record series and lives as long as the proposal. It can never ride in a deletable class.

**Class B — intermediary processing data: bounded TTL, labeled as product policy.** Page renders, raw OCR output, alternate table hypotheses, unselected model responses, copied prompt context, agent traces, evaluation working data, abandoned report preparation, and the assistant request families' copied inputs. This is what Texas retention rules call transitory information (destroy when the purpose is fulfilled, no destruction log required) and the federal GRS calls intermediary records (destroy upon creation of the final record). Defaults: 30 days after terminal state; 90 while an open Processing Failure or review references it. The numbers are product policy and are labeled as such. Deletion emits a dry-run manifest first, verifies reachability (nothing cited by a segment, decision, open review, or release is deletable), and leaves digests behind.

**Class C — rebuildable indexes: no promise.** Search vectors, text-search columns, materialized projections. Delete and rebuild freely with their source rows.

**Class D — cost and reimbursement records: declared, empty.** Federal-aid cost records carry event-triggered floors (three years from final payment under 23 CFR 645.117 / final report under 2 CFR 200.334). Corridor holds none today; the class exists so nothing lands there unclassified.

**Legal holds are non-negotiable and cross-class.** A hold suspends every deletion path — the TTL job and cascade deletes — with documented scope, actor, and time. The trigger set in this domain is broader than private-sector practice: litigation, claims, audits, and open-records requests (Tex. Gov't Code §441.187, FRCP 37(e)). A hold that stops the scheduled job but not a cascade is the sanctionable failure mode.

## Considered options

**TTL for record data with long periods.** Rejected: building deletion for Class A creates the machinery legal holds must then police, to save storage the schedules say to keep. A future records policy or contract can add an explicit, hold-aware disposition feature; nothing today asks for it.

**Retain everything indefinitely (status quo).** Rejected for Class B: the five `retained_indefinitely` assistant families and unclassified render/OCR accumulation are cost and discovery surface with no records value — the exact category both Texas and federal rules permit destroying without ceremony.

## Consequences

ADR-0032 is untouched: everything it protects is Class A or a Class A projection. Released PDFs are stored once as content-addressed artifacts with the release referencing the digest — the alternative ADR-0040 itself sanctions ("the exact PDF bytes or their immutable content-addressed object"); the current double-store (`models.py:2939` and `models.py:3012`) stops for new releases, and existing rows are not rewritten. New assistant requests reference immutable inputs by id and digest instead of copying them.
