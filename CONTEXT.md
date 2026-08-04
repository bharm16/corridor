# Corridor

A cited system of record for external-party readiness on highway projects. Every factual assertion it makes traces to a quote on a page of a source document.

## Language

### The ledger

**Dependency**:
A precondition owned by a party outside the project organization that must be satisfied before construction can proceed. Spans utility relocations, agreements, permits, right-of-way, railroad, and access.
_Avoid_: conflict (industry synonym — acceptable in UI copy and matrix import, never in code), issue, item, task, blocker

**External Party**:
The organization outside the project that owns a Dependency — a utility, railroad, permitting agency, or consultant. Known by many names across documents, so one External Party carries all its aliases.
_Avoid_: stakeholder, third party, vendor, utility (when meaning the organization rather than the asset)

**Ledger**:
The canonical set of adjudicated Dependencies for a project. Nothing enters it except by a human decision.
_Avoid_: database, registry, tracker, list

**Stationing**:
A linear coordinate along the project alignment, written as `245+00`. The most discriminating way to say *where* a Dependency is, and the reason two records can be judged the same or different with a number rather than a guess.
_Avoid_: chainage, location (when a station range is meant), milepost

**Milestone**:
A dated event in the project schedule that Dependencies must be ready for.
_Avoid_: deadline, gate, phase

**Resolution Strategy**:
How a utility conflict is to be resolved, asserted by the source document: the facility is **relocated**, **protected in place**, resolved by **changing the highway design**, or granted an **exception to policy**. The industry's four published alternatives, not Corridor's invention. A reviewer may override the document's claim — a matrix may say protect-in-place about a duct bank under the only haul road — but nobody may invent one where the document is silent, and many documents are: an inventory records conflicts without ever saying how they resolve.
_Avoid_: disposition, treatment, remedy, action, fix

**Criticality**:
Whether a Dependency must move before construction can proceed — **a reading of the Resolution Strategy, never a stored scale**: critical means the strategy is relocation. A facility that stays where it is, however much work it needs, is not critical. Federal regulation defines relocation far more broadly, to include adjustment and protective measures; that definition bounds reimbursement, not schedule, and is not the one meant here.
_Avoid_: priority, severity, importance, urgency (severity is a property of an Exception, not of a Dependency); high (a middle value nothing can assert)

**Need Date**:
The date by which a Dependency must be Ready, derived from the Milestone it serves. A property of the project.
_Avoid_: required date, due date, deadline

**Committed Date**:
The date an External Party stated it would deliver. A claim by someone else, which may move, may be contradicted by another document, and may bear no relation to the Need Date.
_Avoid_: promise date, agreed date, target date

### Claims and proof

**Assertion**:
A single claim by one source document about one field of one Dependency — "D12 p.4 says the committed date is June 3". Sources disagree; assertions preserve every claim, and the Dependency's own field values are the adjudicated conclusion drawn from them.
_Avoid_: claim, statement, fact, value

**Evidence**:
A quote from a specific page of a registered document, verified to actually appear on that page. Evidence is the only thing that can support a factual assertion in the ledger or a report.
_Avoid_: proof, backup, reference, source (when meaning the quote itself)

**Derivation**:
A claim produced by computing over Dependencies rather than by reading a document — a count, a percentage, a rollup. Carries the ruleset version and the records it covered instead of a quote, and drills through to their Evidence. No published number is ever bare: it is an Assertion or a Derivation.
_Avoid_: aggregate, rollup, summary (when meaning the provenance class)

**Document of Record**:
When one document is published in several formats, the one Evidence cites. The structured original outranks anything printed from it: a spreadsheet states its values, a PDF of that spreadsheet only depicts them.
_Avoid_: source of truth, master copy, canonical version, original

**Utility Conflict Matrix**:
A document that lists utility conflicts *and* how each is to be resolved. The industry form, and the only kind that can assert a Resolution Strategy.
_Avoid_: UCM (acceptable in filenames and UI copy, never in code), conflict list, utility matrix

**Utility Inventory**:
A document that lists utility features, and may flag which are in conflict, but never says how a conflict resolves. Distinguishing this from a Utility Conflict Matrix matters because an inventory looks like one and cannot answer what one answers — a conflict flag says a problem exists, not that the facility moves.
_Avoid_: matrix, inventory matrix (the phrase agencies print on the cover of both)

### States and signals

**Ready**:
A derived state, not a stored one: a Dependency is ready when a reviewer has marked a verified Evidence link as meeting the bar named in its `evidence_required`. The reviewer judges sufficiency; the system refuses readiness without the evidence. Nobody can set a Dependency to ready directly — readiness is proven or it does not hold.
_Avoid_: complete, done, cleared, resolved

**Slip**:
An event in which an External Party's Committed Date moves later than a date it previously stated. Slips are recorded, never overwritten — the earlier commitment remains part of the record.
_Avoid_: delay, pushback, reschedule

**Exception**:
A condition computed over the Ledger indicating a Dependency is not on track — missing an owner, a date, or Evidence; stale, due soon, overdue, contradicted, or unlinked to a Milestone. Exceptions are always queries, never stored flags.
_Avoid_: alert, flag, issue, risk, warning

### The review pipeline

**Candidate**:
A Dependency or event proposed by an extractor, with its citations, not yet part of the Ledger. Extractors produce only Candidates; they can never write to the Ledger.
_Avoid_: suggestion, extraction, draft, proposal

**Adjudication**:
The human act of resolving a Candidate — accept, edit then accept, merge into an existing Dependency, or reject. The only path into the Ledger.
_Avoid_: review, triage, approval, curation
