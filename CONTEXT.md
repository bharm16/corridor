# Corridor

Corridor surfaces the Dependencies standing in front of a highway project — each with the evidence behind it — so the project team can chase them to resolution. Every factual assertion traces to a quote on a page of a source document.

## Language

### The ledger

**Dependency**:
A precondition owned by a party outside the project organization that must be satisfied before construction can proceed. Spans utility relocations, agreements, permits, right-of-way, railroad, and access.
_Avoid_: conflict (industry synonym — acceptable in UI copy and matrix import, never in code), issue, item, task, blocker

**External Party**:
The organization outside the project that owns a Dependency — a utility, railroad, permitting agency, or consultant. Known by many names across documents, so one External Party carries all its aliases.
_Avoid_: stakeholder, third party, vendor, utility (when meaning the organization rather than the asset)

**Ledger**:
The working set of Dependencies for a project — what the queue, Exceptions and Reports read. A row enters mechanically when extraction can anchor it, carrying its verification status on the row, or by human judgment; every entry names its path in a receipt (ADR-0029).
_Avoid_: database, registry, tracker, list

**Stationing**:
A linear coordinate along the project alignment, written as `245+00`. The most discriminating way to say *where* a Dependency is, and the reason two records can be judged the same or different with a number rather than a guess.
_Avoid_: chainage, location (when a station range is meant), milepost

**Milestone**:
A dated event in the project schedule that Dependencies must be ready for.
_Avoid_: deadline, gate, phase

**Milestone Impact**:
The project's current Work Decision about whether a Committed Date Change affects registered Milestones: **affects**, **does not affect**, or **not yet known**. `Affects` names the exact Milestones; `not yet known` remains coordinated work and never counts as completion.
_Avoid_: schedule impact (without a state and exact Milestones), impact note, free-text milestone

**Resolution Strategy**:
How a utility conflict is to be resolved, asserted by the source document: the facility is **relocated**, **removed**, **abandoned in place**, **adjusted vertically** to a new grade, **protected in place**, resolved by **changing the highway design**, or granted an **exception to policy**. The industry's published alternatives, not Corridor's invention. A reviewer may override the document's claim — a matrix may say protect-in-place about a duct bank under the only haul road — but nobody may invent one where the document is silent, and many documents are: an inventory records conflicts without ever saying how they resolve.
_Avoid_: disposition, treatment, remedy, action, fix

**Criticality**:
Whether a Dependency commits its External Party to substantial work on the facility — **a reading of the Resolution Strategy, never a stored scale**. Relocation, removal and abandonment are critical; a vertical adjustment to grade, protection in place, a design change or a policy exception are not. The line is the one FDOT draws in colour on its plans, and it is about the **kind of work**, not a date: some critical relocations cannot happen before construction begins, because they wait on the highway element they attach to. Federal regulation defines relocation far more broadly, to include adjustment and protective measures; that definition is used for both cost and scheduling but does not discriminate, and is not the one meant here.
_Avoid_: priority, severity, importance, urgency (severity is retired everywhere, ADR-0010 — Criticality filters Exceptions, it never multiplies them); high (a middle value nothing can assert)

**Need Date**:
The date by which a Dependency must be Ready, derived from the Milestone it serves. A property of the project.
_Avoid_: required date, due date, deadline

**Commitment**:
An attributable statement by an identified External Party about what it will deliver and when. One supported timing is a Commitment; two attributable timings for the same commitment may establish a Committed Date Change. A Commitment may apply to one Dependency, several, all active Dependencies for the party as an explicit scope snapshot, or remain party-level while scope is not yet known.
_Avoid_: mention, invitation, project action item, affected-party statement

**Committed Date**:
The timing an identified External Party stated it would deliver, preserved at the precision the statement supports. An exact day, a month, and an approximate phrase are different claims: normalization may help order or display them but never invent a more exact date. A Committed Date may move, may be contradicted by another document, and may bear no relation to the Need Date.
_Avoid_: promise date, agreed date, target date

**Commitment Scope**:
The Dependencies an External Party commitment applies to: one named Dependency, a selected set, all Dependencies active for that party at the time of an explicit snapshot decision, or not yet known. Unknown scope preserves a real party-level statement without changing any individual Dependency; scope is never guessed or created by copying the event onto every possible row.
_Avoid_: affected conflicts (without saying which), blanket assignment, inferred scope

**Commitment Lineage**:
One External Party commitment across its append-only statement versions. Correcting attribution or timing, including supported facts that change the derived statement type, creates a linked successor in the same lineage; changing scope appends a scope decision without creating a new statement. A statement-level Coordination Plan follows the lineage and is marked for review after a material correction rather than copied to scope-linked Dependencies.
_Avoid_: mutable commitment, latest event, copied statement, Dependency plan

**Coordination Subject**:
The one thing a Work Decision is about: exactly one Dependency or one accepted External Party Commitment or Committed Date Change. A Work Decision never has both subjects, and a pending Candidate, refused statement or closure statement is not a Coordination Subject.
_Avoid_: polymorphic owner, mixed subject, affected records

**Internal Owner**:
The project-team member accountable for driving the Next Action on exactly one Dependency or accepted External Party Commitment or Committed Date Change — established only by a Work Decision. The recording principal and the Internal Owner are distinct roles, not necessarily distinct people: self-assignment is a principal assigning themselves, recorded like any other assignment. The External Party owns its work; the Internal Owner owns the project's follow-up.
_Avoid_: owner (bare — ambiguous with the External Party), assignee, responsible party

**Next Action**:
The step the project has decided must happen next for exactly one Dependency or accepted External Party Commitment or Committed Date Change, with an Action Due Date or an explicit reason its date is not yet known. Set, completed, reassigned or cancelled only by Work Decisions, which append and never overwrite.
_Avoid_: action item (meeting vocabulary — acceptable in UI copy, never in code), task, todo, follow-up

**Action Due Date**:
The date the project set for its own Next Action. A project-controlled date: neither the Need Date, which a Milestone derives, nor a Committed Date, which an External Party claimed.
_Avoid_: due date (bare), deadline

### Claims and proof

**Assertion**:
A single claim by one source document about one field of one Dependency — "D12 p.4 says the committed date is June 3". Sources disagree; assertions preserve every claim, and the Dependency's own field values are the adjudicated conclusion drawn from them.
_Avoid_: claim, statement, fact, value

**Dispute**:
Two revisions of a document disagreeing about a field of one Dependency — a query over Assertions, never a stored flag. Both claims stay on the row with their pages; the row is workable while the Dispute stands, and settling it is Adjudication taken up when it matters (ADR-0031).
_Avoid_: conflict (the industry word for a Dependency, never for documents disagreeing), contradiction (the Exception category that surfaces a Dispute), mismatch

**Not Relevant**:
The human disposition of a Candidate that does not belong in the project record, recorded with a structured reason. It creates no Dependency, External Party statement, scope decision or Coordination Plan; the Candidate remains unchanged and the disposition can be reversed (ADR-0039).
_Avoid_: delete, toss, reject, ignore

**Dismissal**:
The human act of taking a junk record off the working list with a stated reason — a duplicate, or not a conflict at all. Never a delete: the row, its Evidence, its Assertions and its history stay, so the record keeps saying why it left (ADR-0032).
_Avoid_: delete, remove, archive (the sealed retirement of the legacy Ledger is a different act), close (a Dependency closes when its work is done)

**Settlement**:
One human decision about what a disputed field concludes, recorded beside the claims rather than erasing the losing one. It names how far its judgment reaches — the claims that existed when it was made — so a later revision disagreeing again reopens the Dispute rather than inheriting a verdict nobody gave it.
_Avoid_: resolution (Resolution Strategy is what the document asserts about the facility), override, correction

**Evidence**:
A quote from a specific page of a registered document, verified to actually appear on that page. Evidence is the only thing that can support a factual assertion in the ledger or a report.
_Avoid_: proof, backup, reference, source (when meaning the quote itself)

**Operative Support**:
The Evidence doing current work for a Dependency **in a named role** — closing the readiness bar, or backing what a report prints for the record or a field. The roles are distinct facts: proof that work closed does not prove every value printed. Production Evidence is preserved; the sealed retirement of an explicitly noncompliant development Ledger is the narrow legacy exception (ADR-0021). Operative support is the subset the record presently stands on, and the only Evidence whose citing a superseded revision means anything is wrong.
_Avoid_: active evidence, current evidence, live citation, operative support with no role named

**Derivation**:
A claim produced by computing over Ledger records rather than by reading a document — a Dependency count, a percentage, a rollup, or whether an attributable party-level Commitment is past due. Carries the Evaluation date, ruleset version and exact records it covered instead of a quote, and drills through to their Evidence or Verbal provenance. No published value is ever bare: it is an Assertion, a Derivation, a Work Decision or a Verbal.
_Avoid_: aggregate, rollup, summary (when meaning the provenance class)

**Work Decision**:
An attributable project-team decision, recorded at a stated time, that establishes or changes project-controlled coordination state for exactly one subject: one Dependency or one accepted External Party Commitment or Committed Date Change, never both. It proves only what the project decided and when: it is not Evidence, states nothing about what a document or External Party said, and can never set Criticality, a Resolution Strategy, Ready, or an External Party's status or commitment. Later Work Decisions supersede but never erase earlier ones. What a report publishes is an Assertion (what a document said), a Derivation (what the rules computed), a Work Decision (what the project decided) or a Verbal (what an External Party told a recorder).
_Avoid_: workflow state, attribution, direction (carries contractual-authority weight in construction), audit entry

**Coordination Plan**:
The project's current internal response to exactly one Dependency or accepted External Party Commitment or Committed Date Change: its Internal Owner, Next Action and Action Due Date or unknown-date reason, plus Milestone Impact when the subject is a Committed Date Change. It appears as one plan but each field remains an attributable, append-only Work Decision. A statement-level plan follows that statement's lineage and is never copied to Dependencies merely because scope becomes known (ADR-0038).
_Avoid_: workflow state, plan record, copied assignment, External Party commitment

**Verbal**:
An External Party's stated commitment, heard by a named project person on a stated date and recorded as an append-only event. It is neither Evidence nor a Work Decision: it can move a Committed Date because it says what the identified party said, but it can never render as a page quote or a computation. It preserves the party's timing words and precision and may begin at party or multi-Dependency scope. A report marks its recorder, conversation date and stated party; document-only reporting excludes it and falls back to verified cited events (ADR-0033, ADR-0036).
_Avoid_: phone citation, undocumented evidence, informal date, inferred commitment

**Document of Record**:
When one document is published in several formats, the one Evidence cites. The structured original outranks anything printed from it: a spreadsheet states its values, a PDF of that spreadsheet only depicts them.
_Avoid_: source of truth, master copy, canonical version, original

**Supersession**:
The registry relation that one document replaced another as the current revision. Declared by a registered authority document and an exact page — normally an agency index (the RID index's "Replaced on" chain) or the successor's own explicit replacement statement — and recorded when the document is registered. The predecessor can never attest its own replacement: the replacement postdates it, so the page a reader would check predates the fact. Never inferred from dates or filenames, and never an Assertion: it is registry metadata like a document's date. A superseded document stays in the corpus and its pages still say what they said; supersession changes which revision is current, never what a page states.
_Avoid_: replaced (the index's own word — acceptable in UI copy, never in code), versioning, obsolete, archived

**Numbering Scheme**:
How a matrix names its rows — declared at registration like the document's date, never inferred from the data. Project-unique numbering names a conflict by its number alone (the TxDOT form, whose Retired Rows keep numbers stable); per-party numbering names it by the External Party and the number together (the FDOT form, where every party's list counts from 1). Identity under the declared scheme is what every reader groups by (ADR-0030).
_Avoid_: id format, key strategy, numbering style (a scheme is declared, a style is observed)

**Utility Conflict Matrix**:
A document that lists utility conflicts *and* how each is to be resolved. The industry form, and the only kind that can assert a Resolution Strategy.
_Avoid_: UCM (acceptable in filenames and UI copy, never in code), conflict list, utility matrix

**Retired Row**:
A row of a Utility Conflict Matrix whose only content is an identifier plus a retirement phrase (`Not Used`) — the form's bookkeeping for a number taken out of service, not a conflict. Excluded from the Ledger and from any reference enumeration by stated rule, never by a guard's side effect. A **populated** row carrying the same phrase is a conflict whose facility may be out of service: the phrase describes the facility, reaches `notes` verbatim, and Adjudication judges it (ADR-0012).
_Avoid_: blank row (it has content: the id and the phrase), skipped row, empty slot

**Utility Inventory**:
A document that lists utility features, and may flag which are in conflict, but never says how a conflict resolves. Distinguishing this from a Utility Conflict Matrix matters because an inventory looks like one and cannot answer what one answers — a conflict flag says a problem exists, not that the facility moves.
_Avoid_: matrix, inventory matrix (the phrase agencies print on the cover of both)

### States and signals

**Ready**:
A derived state, not a stored one: a Dependency is ready when verified Evidence meets the bar named in its `evidence_required`. A designated human judges that sufficiency; Automatic Carry-Forward may inherit the established judgment onto exact current Evidence but cannot originate it. Nobody can set a Dependency to ready directly — readiness is proven or it does not hold.
Proof also stands on the current revision: when every satisfying link cites a superseded document, readiness lapses until existing sufficiency moves to current Evidence through exact Automatic Carry-Forward or a new guided Ready judgment — the old Evidence stays verified and preserved; only present readiness changes.
_Avoid_: complete, done, cleared, resolved

**Committed Date Change**:
An event in which the timing an identified External Party stated for the same commitment moves earlier or later. Both statements, their original wording, their precision, and the direction remain on the record; neither is overwritten. It is a Change only when both timings are attributable — one supported timing by itself is a Commitment, not a change.
_Avoid_: slip, delay, pushback, reschedule

**Attention Reason**:
A current, derived reason one work-list item needs human attention, such as an overdue party-level Commitment, unknown Commitment Scope, a due Next Action or a Committed Date Change whose Milestone Impact is unresolved. One statement may carry several Attention Reasons but appears once; its highest-consequence reason controls ordering. An Attention Reason is not Evidence, a stored flag or necessarily a Dependency Exception.
_Avoid_: queue row, alert, score, duplicated task, party-level Exception

**Work Item**:
One current project question anchored to a Candidate or Coordination Subject and presented with all of its Attention Reasons, with the highest-consequence reason controlling its position in the immediate work list. A current Coordination Plan or explicit deferral may move it out of immediate work until a return condition, but never removes the underlying Candidate, External Party fact, Dependency, or Report entry.
_Avoid_: duplicate task, one row per reason, Ledger record, queue record

**Exception**:
A condition computed over the Ledger indicating a Dependency needs attention — missing an owner, a date, or Evidence; stale, due soon, overdue, contradicted, unlinked to a Milestone, or standing on a superseded citation. Not every category is a schedule failure: a superseded citation is a provenance fact, presented as re-review work rather than lateness. Exceptions are always queries, never stored flags. An Exception is a fact carrying its category and its quantities (days overdue, days of silence); it has no score — Exceptions are grouped, sorted by their quantities, and filtered by Criticality, never ranked by weights nobody can defend (ADR-0010). A party-level Commitment may have an Attention Reason and past-due Derivation without creating a Dependency Exception whose scope has not been proved.
_Avoid_: alert, flag, issue, risk, warning, severity (retired with the Exception score, ADR-0010)

**Evaluation**:
One project's Exceptions as computed at one moment, carrying the date they were computed against, the thresholds applied and the ruleset version — so a published number and the fact beneath it cannot disagree about what day it is. A report, its export and its recorded run all describe one Evaluation.
_Avoid_: run, scan, pass, snapshot (a snapshot is what a report was published against, and it records an Evaluation rather than being one)

**Approved Export**:
The fixed external Report artifact a designated project person explicitly releases. Its exact bytes, SHA-256 digest, Evaluation date, ruleset, provenance mode, covered records, releaser, and release time are immutable; sending those bytes through email or document control is a separate act outside Corridor (ADR-0040).
_Avoid_: live report approval, sent report, mutable export, latest PDF

**Extraction Measurement**:
A scored comparison of Candidates from explicitly named Extraction Runs against a declared reference enumeration, recording both the measured population and the reference's limits. It measures an extractor, never the Ledger or a project's Exceptions.
_Avoid_: Evaluation (reserved for computed Exceptions), eval result, benchmark

**Briefing**:
A model-drafted narrative view of the record — one Dependency or one project, read across its Evidence, Assertions and Exceptions and rendered as prose. Cited sentence by sentence to the same bar as a report cell; floored by the computed Exceptions, which it may explain but never omit; stamped with the prompt version and model that drafted it and the evaluation time and ruleset version of the Exceptions it cites; regenerable and never itself the record. Anything it surfaces that should become record enters as a Candidate through Adjudication (ADR-0011).
_Avoid_: summary, analysis, assessment, AI insights

### The review pipeline

**Active Run**:
The one production extraction run per document that current work draws from — explicitly declared in run lineage, never inferred as the newest by id or timestamp. Normal Document processing declares its single safe production result automatically. An experiment, Evaluation, acceptance replay or unrelated backfill is ineligible; ambiguity selects nothing new and alerts Corridor operations. The work list reads the Active Run; a Revision Comparison pins exact run ids; an Extraction Measurement selects exact run ids independently — three readers, one definition, no implicit "latest."
_Avoid_: latest run, newest run, current extraction

**Revision Comparison**:
The immutable record of comparing two extraction runs — a document's and its successor's: which rows correspond, which were added or dropped, which fields changed, and where the matcher could not decide. Pinned to the exact runs, matcher version and configuration that produced it; a better matcher produces a new Comparison rather than editing the one a reviewer already acted on.
_Avoid_: revision diff (implies an exactness the matcher cannot promise), change report (Report is the published weekly artifact), delta

**Cohort Receipt**:
The immutable membership of one derived rehearsal cohort — the rows of a successor Active Run that a stated rule selects from a sealed Revision Comparison for one External Party. Pinned to exact runs, the Comparison, and both rule and matcher versions under a content digest, with members named by registry identity; re-deriving from the same inputs returns the identical receipt or refuses. The queue's rehearsal lane reads exactly this set, and the set is a mutation boundary: adjudicating outside it through the lane refuses. Uncertain correspondences are excluded by rule — the cohort never adjudicates ambiguity by accident.
_Avoid_: cohort (bare, when the receipt is meant), sample, batch, worklist (Supersession Review's word)

**Lane**:
A scoped path through the queue: the set of Candidates a reviewer is working, and the gestures that set permits. The rehearsal lane reads a Cohort Receipt; the event lane reads an Event Cohort Receipt and permits accept-plus-merges as one gesture; the ordinary queue is the unscoped lane. A lane narrows what is offered *and* what a mutation will accept — the two are one rule, because a lane that offers a merge the mutation then refuses is worse than either alone.
_Avoid_: tab, view, filter (a lane is a mutation boundary, not a display), mode

**Supersession Review**:
The live worklist derived from Supersession and the operative-support resolver: which Ledger records still stand on a superseded revision, what moving each one's support requires, and where the revision workflow is incomplete (`awaiting_extraction`, `extraction_failed`, `awaiting_active_run`, `awaiting_comparison`). A Revision Comparison enriches it when one exists; the worklist never waits for one. It changes through Reconfirmation or Automatic Carry-Forward; the Comparison beneath it never does.
_Avoid_: re-review queue, stale list, migration list

**Reconfirmation**:
The internal lineage act of re-pointing a Dependency's Operative Support at the current revision after a supersession. It changes what the record stands on — never whether the record should exist, how it was merged, or what its fields conclude. It is not a customer lane or generic button: exact unchanged support moves automatically, while a meaningful difference becomes a specific question on the affected record.
_Avoid_: re-adjudication, ratification, re-review

**Automatic Carry-Forward**:
The fail-closed act of moving a Dependency's existing Operative Support to mechanically verified Evidence from an exact, unique, unchanged successor row under Corridor's versioned rules. It preserves only support roles a human already established; it never performs Admission, changes the adjudicated conclusion, or originates readiness. It is normal processing, not a customer authorization or customer task.
_Avoid_: automatic Reconfirmation, machine Adjudication, auto-approval, ratification

**Carry-Forward Policy**:
The named, versioned eligibility rules Corridor deploys for Automatic Carry-Forward. They are fail-closed, recorded by digest, and changed through engineering and release controls; a project coordinator is not asked to inspect or authorize them.
_Avoid_: blanket approval, customer authorization, silent default, reviewer bot

**Carry-Forward Run**:
The immutable receipt for one policy evaluation over a project's current Supersession Review work. It records exact carried and abstained outcomes under one policy digest and reason-vocabulary version; an identical rerun may record a zero-new-outcome Run but cannot duplicate a prior outcome.
_Avoid_: bot session, reviewer batch, transient log

**Revision Processing**:
The ordered service that creates and read-verifies one exact Revision Comparison before invoking the deployed Carry-Forward Policy. It does not turn Revision Comparison into a Ledger writer and may move support only through the policy's proved, receipted path.
_Avoid_: automatic Adjudication, comparison write-back, implicit approval

**Unplaced Statement**:
A statement from the minutes the event-admission policy could not safely scope — the reference names nothing, names several, or the stated party does not match. Kept and shown as specific work rather than dropped, because a dated commitment is what the record most needs; a coordinator uses suggested and searchable human-readable matches to select one or more Dependencies, preserves it at party level with scope not yet known, or marks it not relevant (ADR-0032, ADR-0035, ADR-0036).
_Avoid_: orphan event, unmatched event, statement queue (it is a pile a reviewer empties, never a lane)

**Candidate**:
A Dependency or event proposed by an extractor, with its citations, not yet part of the Ledger. Extractors produce only Candidates; they can never write to the Ledger.
An internal term; customer actions use plain descriptions of the decision in front of them.
_Avoid_: suggestion, extraction, draft, proposal (in customer copy)

**Abstention**:
The outcome when a named automation cannot prove a proposed write eligible under its bounded policy. An Admission Abstention leaves the Candidate pending for specific human work; an Automatic Carry-Forward Abstention leaves any successor Candidate and the Dependency's support unresolved. The Ledger is unchanged rather than forcing a conclusion, and the reason appears on the policy outcome receipt.
_Avoid_: rejection, failure, automatic Adjudication, low-confidence Admission

**Adjudication**:
The human act of settling what the machine could not — a Dispute, a flagged row, an unplaced statement — or dismissing junk with a reason. Chosen work on a live row, never a gate in front of the list (ADR-0029).
An internal term; customer actions say what the person is actually deciding.
_Avoid_: review, triage, approval, curation (as generic customer work)

**Admission**:
The entry of a record into the Ledger — mechanical for what extraction can anchor, under a named versioned policy receipt, or human, through Adjudication. A model verdict never admits; Reconfirmation never admits (ADR-0029).
_Avoid_: creation, insertion, import, promotion
