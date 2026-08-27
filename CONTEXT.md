# Corridor Project Record

Corridor records the external conditions that affect highway construction, the commitments and project decisions around them, and the sources supporting each published value.

## Language

### Constraints and commitments

**Constraint**:
An external condition that must be addressed for a specified construction activity to start, progress, or finish as planned.
_Avoid_: scheduled activity, schedule dependency, generic task

**Utility Conflict**:
Actual or potential interference between utility facilities and proposed construction.
_Avoid_: disagreement between people, every permit or agreement, relocation already required

**External Party**:
An organization outside the project team that is responsible for a Constraint or makes an attributable statement recorded by Corridor.
_Avoid_: project-team assignee, automatically the speaker of every statement mentioning it

**Utility Owner**:
The External Party that owns the utility facilities involved in a project.
_Avoid_: assigned project person, project owner

**Project Record**:
The authoritative set of Constraints, accepted External Party Statements, Supporting Documentation, Verbals, Work Decisions, Milestone Revisions, and publication receipts for one project.
_Avoid_: only the Constraint log, only the Ledger

**Ledger**:
The working set of Constraints within the Project Record.
_Avoid_: the entire Project Record, all party-level Commitments

**Constraint Log**:
A view of the Constraints being coordinated, the people assigned to their next actions, and the relevant dates.
_Avoid_: a schedule's date restrictions, a complete construction schedule

**Stationing**:
A position along the project alignment, such as `245+00`.
_Avoid_: a station range without its alignment context

**Milestone**:
A named event in the project schedule with no duration, such as the start of a construction phase.
_Avoid_: a duration-bearing activity, a promise from an External Party

**Milestone Revision**:
An immutable record of one version of a Milestone with its exact source row and source digest.
_Avoid_: mutable milestone, bare date, automatically an approved baseline

**Effect on Milestone**:
A Work Decision that states whether a Committed Date Change affects named Milestones, does not affect them, or has an effect that is not yet known.
_Avoid_: a free-text milestone, an inferred project delay, a critical-path calculation

**Resolution Strategy**:
The recorded way a Utility Conflict will be addressed, such as relocation, removal, protection in place, or a highway design change, supported by its source or attributable human conclusion.
_Avoid_: proof the work is complete, Criticality, critical-path status

**Required By**:
The date by which a Constraint's stated requirement must be met, derived from the Milestone Revision it serves.
_Avoid_: Promised For, the internal action's due date, actual completion

**External Party Statement**:
An attributable Commitment, Committed Date Change, or report of completion preserved with its timing, source, and Commitment Scope.
_Avoid_: every mention of an organization, an internal project action

**Commitment**:
An attributable External Party Statement about what that party will deliver and when.
_Avoid_: a planning estimate, an invitation, a project action item

**Promised For**:
The timing an External Party stated it would deliver, preserved at the precision the statement supports.
_Avoid_: Required By, estimated completion, actual completion

**Committed Date Change**:
An External Party Statement replacing one attributable timing with another for the same Commitment, with direction retained when supported.
_Avoid_: a change to Required By, an invented direction, a new unrelated deliverable

**Completion Reported**:
An attributable External Party Statement that one exact Commitment is complete, supported by verified source records or preserved as a Verbal.
_Avoid_: all Constraints satisfied, internal action complete, general construction authorization

**Commitment Scope**:
The explicit set of Constraints to which an External Party Statement applies, or the statement that scope is not yet known; all-active records the set at the time of the decision.
_Avoid_: inferred scope, a set that silently expands later

**Commitment Lineage**:
One External Party Commitment across append-only factual corrections and statement versions.
_Avoid_: every promise by the same party, a copied statement per Constraint

### Project coordination

**Coordination Subject**:
The one Constraint or accepted Commitment Lineage that a Work Decision concerns.
_Avoid_: one decision silently applied to several subjects

**Assigned To**:
The named project-team member accountable for the current Next Action on one Coordination Subject.
_Avoid_: Utility Owner, automatically the person performing the external work

**Next Action**:
The project-controlled step that must happen next for one Coordination Subject.
_Avoid_: the External Party's Commitment

**Action Due Date**:
The date the project set for its own Next Action.
_Avoid_: Required By, Promised For

**Work Decision**:
An attributable project-team decision that changes project-controlled coordination state for one Coordination Subject.
_Avoid_: proof of what an External Party said or did

**Coordination Plan**:
The current assigned person, Next Action, and Action Due Date for one Coordination Subject, plus Effect on Milestone when the subject is a Committed Date Change.
_Avoid_: an External Party Commitment, a plan copied to every linked Constraint

### Claims and supporting records

**Assertion**:
One source Document's claim about one field of one Constraint.
_Avoid_: the project's settled conclusion, an independently verified physical fact

**Dispute**:
An unsettled disagreement among retained verified Assertions about one Constraint field.
_Avoid_: Utility Conflict, automatically a contractual dispute, a stored flag

**Settlement**:
An attributable human conclusion about one disputed field that preserves every Assertion it considered.
_Avoid_: deletion of the losing claim, a conclusion about unseen later claims

**Supporting Documentation**:
Verified source passages from registered Documents that support a recorded fact, with exact document and page references.
_Avoid_: a document title alone, proof that every related condition is satisfied

**Operative Support**:
The current Supporting Documentation used to publish a Constraint's facts or support an established Documentation Review.
_Avoid_: every historical citation, the most recently inserted document

**Derivation**:
A value computed from exact Project Record inputs under a stated ruleset and Evaluation date.
_Avoid_: a source Assertion, an unexplained aggregate

**Verbal**:
An External Party Statement heard by a named project person on a stated date and preserved as an append-only source.
_Avoid_: a document citation, an inferred Commitment

**Document of Record**:
The rendition that Supporting Documentation cites when one Document is published in several formats; a structured original outranks its printed rendering.
_Avoid_: every rendition treated as an independent source

**Supersession**:
The registered relation that one Document replaced another as the current revision.
_Avoid_: an inference from filename or recency

**Numbering Scheme**:
The registered rule that gives matrix rows project-unique or per-party identity.
_Avoid_: an identity rule guessed from a row number

**Utility Conflict Matrix**:
A Document that lists Utility Conflicts and how they will be addressed.
_Avoid_: Utility Inventory, a complete utility schedule

**Utility Inventory**:
A Document that lists utility features and may identify interference but does not state Resolution Strategies.
_Avoid_: Utility Conflict Matrix

**Retired Row**:
A Utility Conflict Matrix row whose only content is an identifier and a retirement phrase, rather than a Constraint.
_Avoid_: every abandoned utility facility, a deleted source row

### Documentation review and supported outcomes

**Required Documentation**:
The stated source records and content needed to demonstrate that one specified construction condition has been met.
_Avoid_: readiness to begin the external work, a universal completion checklist

**Documentation Review**:
A named person's judgment of whether exact current Supporting Documentation meets the Required Documentation for one specified condition.
_Avoid_: quote verification alone, contractual acceptance, permission to start unrelated work

**Relocation Complete**:
The supported conclusion that the specified relocation work is complete for the named utility facilities and location.
_Avoid_: relocation design complete, relocation promised, the whole project complete

**Permit Issued**:
The identified permit has been issued by its issuing authority for its stated scope and conditions.
_Avoid_: every required permit obtained, the permitted work complete

**Agreement Executed**:
The identified agreement has been signed by its required parties for its stated scope.
_Avoid_: the agreement's required work complete, an unsigned draft

**No Conflict Confirmed**:
The supported conclusion that the identified utility facilities do not interfere with the specified construction.
_Avoid_: relocation completed, every utility on the project cleared

### Decisions and signals

**Not Relevant**:
The reversible human disposition of a Candidate that does not belong in the Project Record.
_Avoid_: delete, erase

**Dismissal**:
The reversible human act that removes an admitted junk Constraint from current work while preserving its history.
_Avoid_: work completed, Constraint satisfied

**Attention Reason**:
A current derived reason that one Work Item needs human attention.
_Avoid_: a stored status, a party-level Exception

**Work Item**:
One current project question anchored to a Candidate or Coordination Subject and presented with all of its Attention Reasons.
_Avoid_: one duplicate card per reason, automatically a construction activity

**Exception**:
A derived fact that a Constraint needs attention, grouped by its rule and ordered only by that rule's own quantity.
_Avoid_: an urgency score, a critical-path finding

**Evaluation**:
One project's Exceptions computed for one date, ruleset, threshold set, and Project Record input.
_Avoid_: an extraction measurement, an unbound list of results

### Publication

**Report**:
A structured publication view of one Evaluation and the Project Record values it presents.
_Avoid_: a released artifact that can change in place

**Approved Export**:
The immutable external Report artifact that a designated project person released.
_Avoid_: approval of a live page, proof of delivery to a recipient

**Briefing**:
A cited model-drafted narrative view of the Project Record that cannot change it or omit its Exceptions.
_Avoid_: an independent authority to change the record
