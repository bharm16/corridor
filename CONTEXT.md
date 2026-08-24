# Corridor Project Record

Corridor records the Dependencies in front of a highway project, the External Party facts and project decisions around them, and the provenance for every published value.

## Language

### Dependencies and commitments

**Dependency**:
A precondition owned by an External Party that must be satisfied before construction can proceed.
_Avoid_: conflict, issue, item, task, blocker

**External Party**:
An organization outside the project team that owns a Dependency or makes an attributable statement recorded by Corridor.
_Avoid_: stakeholder, third party, vendor, utility

**Project Record**:
The authoritative set of Dependencies, accepted External Party Statements, Evidence, Verbals, Work Decisions, and publication receipts for one project.
_Avoid_: database, source of truth, master record

**Ledger**:
The working set of Dependencies within the Project Record.
_Avoid_: Project Record, registry, tracker, list

**Stationing**:
A linear coordinate along the project alignment, such as `245+00`.
_Avoid_: chainage, milepost, location when a station range is meant

**Milestone**:
A dated event in the project schedule that Dependencies must be Ready for.
_Avoid_: deadline, gate, phase

**Milestone Registration**:
An immutable record of one Milestone revision with its exact source row and source digest.
_Avoid_: schedule import, mutable milestone, bare date

**Milestone Impact**:
A Work Decision that states whether a Committed Date Change affects exact registered Milestones, does not affect them, or is not yet known.
_Avoid_: impact note, free-text milestone, schedule impact without a state

**Resolution Strategy**:
The source-supported way a utility conflict will be resolved, such as relocation, removal, protection in place, or a highway design change.
_Avoid_: disposition, treatment, remedy, fix

**Criticality**:
The derived reading that a Resolution Strategy requires substantial External Party work: relocation, removal, or abandonment in place.
_Avoid_: priority, severity, importance, urgency, high

**Need Date**:
The date by which a Dependency must be Ready, derived from the Milestone Registration it serves.
_Avoid_: required date, due date, deadline

**External Party Statement**:
An attributable Commitment, Committed Date Change, or Commitment Closure preserved with its timing, source, and Commitment Scope.
_Avoid_: Dependency Event, mention, action item

**Commitment**:
An attributable External Party Statement about what that party will deliver and when.
_Avoid_: mention, invitation, project action item

**Committed Date**:
The timing an External Party stated it would deliver, preserved at the precision the statement supports.
_Avoid_: promise date, agreed date, target date

**Committed Date Change**:
An External Party Statement in which attributable timing for the same Commitment moves earlier or later.
_Avoid_: slip, delay, pushback, reschedule

**Commitment Closure**:
Verified Evidence or an attributable Verbal that states one exact Commitment is complete.
_Avoid_: completion click, closed status, finished action

**Commitment Scope**:
The one, selected, all-active, or not-yet-known Dependencies to which an External Party Statement applies.
_Avoid_: blanket assignment, affected conflicts, inferred scope

**Commitment Lineage**:
One External Party Commitment across append-only factual corrections and statement versions.
_Avoid_: mutable commitment, latest event, copied statement

### Project coordination

**Coordination Subject**:
The one Dependency or accepted Commitment Lineage that a Work Decision concerns.
_Avoid_: mixed subject, affected records, polymorphic owner

**Internal Owner**:
The project-team member accountable for the current Next Action on one Coordination Subject.
_Avoid_: owner, assignee, responsible party

**Next Action**:
The project-controlled step that must happen next for one Coordination Subject.
_Avoid_: action item, task, todo, follow-up

**Action Due Date**:
The date the project set for its own Next Action.
_Avoid_: due date, Need Date, Committed Date

**Work Decision**:
An attributable project-team decision that changes project-controlled coordination state for one Coordination Subject.
_Avoid_: workflow state, direction, audit entry

**Coordination Plan**:
The current Internal Owner, Next Action, and Action Due Date for one Coordination Subject, plus Milestone Impact when the subject is a Committed Date Change.
_Avoid_: plan record, copied assignment, External Party commitment

### Claims and provenance

**Assertion**:
One source Document's claim about one field of one Dependency.
_Avoid_: fact, value, conclusion, statement

**Dispute**:
Current verified Assertions that disagree about one Dependency field.
_Avoid_: conflict, mismatch, stored flag

**Settlement**:
An attributable human conclusion about one disputed field that preserves every Assertion it considered.
_Avoid_: override, correction, resolution

**Evidence**:
A verified quote from a specific page of a registered Document that supports a document-sourced fact.
_Avoid_: proof, backup, source, reference

**Operative Support**:
The current Evidence that supports publication or an established readiness judgment for one Dependency.
_Avoid_: active evidence, current evidence, live citation

**Derivation**:
A value computed from exact Project Record inputs under a stated ruleset and Evaluation date.
_Avoid_: assertion, aggregate, bare rollup

**Verbal**:
An External Party Statement heard by a named project person on a stated date and preserved as an append-only source.
_Avoid_: phone citation, undocumented evidence, inferred commitment

**Document of Record**:
The rendition Evidence cites when one Document is published in several formats; a structured original outranks its printed rendering.
_Avoid_: source of truth, master copy, canonical version

**Supersession**:
The registered relation that one Document replaced another as the current revision.
_Avoid_: versioning, obsolete, archived

**Numbering Scheme**:
The registered rule that gives matrix rows project-unique or per-party identity.
_Avoid_: id format, key strategy, numbering style

**Utility Conflict Matrix**:
A Document that lists utility conflicts and how each conflict will be resolved.
_Avoid_: UCM, conflict list, utility matrix

**Utility Inventory**:
A Document that lists utility features and may identify conflicts but does not state their Resolution Strategies.
_Avoid_: matrix, inventory matrix

**Retired Row**:
A Utility Conflict Matrix row whose only content is an identifier and a retirement phrase, rather than a Dependency.
_Avoid_: blank row, skipped row, empty slot

### Decisions and signals

**Ready**:
The derived condition that current verified Evidence satisfies a Dependency's Readiness Requirement after an attributable human sufficiency judgment.
_Avoid_: complete, done, cleared, resolved

**Readiness Requirement**:
The stated Evidence bar that a Dependency must satisfy to become Ready.
_Avoid_: ready flag, closure toggle, completion status

**Not Relevant**:
The reversible human disposition of a Candidate that does not belong in the Project Record.
_Avoid_: delete, reject, ignore, toss

**Dismissal**:
The reversible human act that removes an admitted junk Dependency from current work while preserving its history.
_Avoid_: delete, archive, close

**Attention Reason**:
A current derived reason that one Work Item needs human attention.
_Avoid_: alert, score, queue row, party-level Exception

**Work Item**:
One current project question anchored to a Candidate or Coordination Subject and presented with all of its Attention Reasons.
_Avoid_: duplicate task, queue record, Ledger record

**Exception**:
A derived fact that a Dependency needs attention, grouped by its rule and ordered only by that rule's own quantity.
_Avoid_: alert, flag, issue, risk, warning, severity

**Evaluation**:
One project's Exceptions computed for one date, ruleset, threshold set, and Project Record input.
_Avoid_: run, scan, pass, snapshot

### Publication

**Report**:
A structured publication view of one Evaluation and the Project Record values it presents.
_Avoid_: Briefing, live record, export

**Approved Export**:
The immutable external Report artifact that a designated project person released.
_Avoid_: live report approval, sent report, mutable export

**Briefing**:
A cited model-drafted narrative view of the Project Record that cannot change it or omit its Exceptions.
_Avoid_: summary, assessment, AI insights
