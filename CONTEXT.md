# Corridor Project Record

Corridor records external conditions that affect highway construction, the commitments and project decisions around them, and their supporting sources. This is the project's coordination record, not its complete construction or contract record.

## Language

[ADR-0048](docs/adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md) records the adopted language and compatibility boundaries. Customer labels explain retained internal concepts without changing their meaning.

### Constraints and construction

**Constraint**:
An external condition that can prevent specified construction work from starting, progressing, or finishing as planned; Corridor tracks this external subset of the broader construction-planning term.
_Customer label_: Construction constraint, with the affected work named
_Avoid_: scheduled activity, schedule dependency, proof that all construction is blocked

**Utility Conflict**:
Actual or potential incompatibility between a Utility Facility and proposed construction, including required clearances and access.
_Avoid_: disagreement between people, every permit or agreement, relocation already required

**Utility Facility**:
The physical utility infrastructure being discussed, such as a pole, pipe, conduit, cable, or related structure.
_Avoid_: its owner, the conflict, an implied asset register

**External Organization**:
An organization outside the project team that is responsible for a tracked condition or is the source of a recorded statement.
_Customer label_: Organization, or the known role such as Utility owner or Permitting agency
_Avoid_: every stakeholder, project-team assignee, automatically the author of a statement mentioning it

**Utility Owner**:
The External Organization that owns the specified Utility Facility.
_Avoid_: assigned project person, project owner, automatically the facility operator

**Project Record**:
The coordination facts, supporting documents, decisions, schedule references, and publication history recorded for one project in Corridor.
_Avoid_: only the Constraint Log, every construction or contract record

**Constraint Records**:
The accepted Constraint records within the larger Project Record.
_Customer label_: Constraints, or Utility conflicts when all records concern utilities
_Avoid_: the entire Project Record, all organization-level Commitments

**Constraint Log**:
A view of conditions affecting planned work, the people handling them, the required dates, and the commitments to address them.
_Avoid_: a schedule's date restrictions, a complete construction schedule

**Activity**:
A defined piece of work that takes time, unlike its start or finish event.
_Avoid_: Milestone, a complete activity network implied by this definition

**Alignment**:
The named line used to describe a roadway's route and locate its stations; horizontal alignment describes the route in plan view.
_Avoid_: an unnamed station reference, a coordinate system

**Stationing**:
The system that numbers positions along a named Alignment; a Station identifies a position and a Station Range identifies a length of that Alignment.
_Customer label_: Location, with the Alignment and Station or Station Range
_Avoid_: coordinates, station numbers without units or applicable station-equation context

**Utility Conflict Resolution Method**:
The recorded approach to addressing a Utility Conflict, such as relocating the facility, protecting it, or changing the highway design, supported by its source or a named person's conclusion.
_Customer label_: Resolution method, followed by the actual action
_Avoid_: proof the work is complete, schedule criticality, authority to start field work

### Key dates and commitments

**Milestone**:
A named schedule event with no duration, such as construction starting or a phase finishing; the event and its scheduled date are separate.
_Customer label_: Key dates
_Avoid_: an Activity, a promise from an External Organization, proof the event happened

**Key Date Version**:
One preserved version of a named key date, including its exact source row and source fingerprint; earlier versions remain available.
_Customer label_: Key date version, with the source schedule revision
_Avoid_: the whole schedule revision, a bare date, automatically an approved baseline

**Effect on Key Dates**:
A named project person's answer about whether a Change to Promised Timing affects identified key dates: affects, does not affect, or not yet known, with the assessed schedule context retained.
_Customer label_: Does this changed promise affect a key date?
_Avoid_: a free-text milestone, an inferred project delay, a critical-path calculation

**Required By**:
The date by which a specified construction condition must be met, calculated from the exact Key Date Version that the Constraint serves.
_Avoid_: Promised For, Action Due Date, actual completion, automatically a contractual deadline

**External Party Statement**:
A recorded promise, change to its timing, or report of completion attributed to the External Organization that made it, with its source, timing, and applicable Constraints preserved.
_Customer label_: Statement from [organization]
_Avoid_: every mention of an organization, an internal project action, a Source Field Value

**Stated By**:
The organization that made the statement, and the individual speaker when identified by the source; organization-only documentary attribution is permitted.
_Avoid_: the organization merely mentioned or affected, the project person recording a verbal statement

**Commitment**:
A promise attributable to an External Organization about what it will deliver and when, preserving the words and timing the source supports.
_Avoid_: a forecast, a request, an invitation, a project action item

**Deliverable**:
The result, document, service, or defined work the organization committed to provide.
_Customer label_: Promised work or Promised document when that describes the result
_Avoid_: every promise by the same organization, an implied work-breakdown structure

**Promised For**:
When the External Organization said it would deliver, shown with the same precision and qualifications as its statement.
_Avoid_: Required By, Estimated Completion Date, Actual Completion Date

**Timing Precision**:
How specifically the source states timing, such as an exact day or a month, with approximate wording preserved separately.
_Avoid_: an invented exact day, treating approximate timing as exact timing

**Change to Promised Timing**:
An External Party Statement replacing that organization's earlier delivery timing for the same Commitment, with both versions retained and direction recorded only when supported.
_Customer label_: Promise changed: [previous timing] to [new timing]
_Avoid_: a change to Required By, an invented direction, a contractual extension, a new unrelated Deliverable

**Completion Reported**:
A supported statement from the External Organization that one identified Commitment is complete, preserved with its documentary or oral source.
_Customer label_: Reported complete by [organization]
_Avoid_: physical inspection, all Constraints satisfied, internal action complete, Contract Acceptance

**Applies To**:
The explicitly recorded Constraints covered by a statement, or a recorded finding that those links are not yet known; an all-active selection preserves its membership at the time of the decision.
_Avoid_: inferred links, a set that silently expands later, unknown links meaning no Commitment exists

**Commitment Lineage**:
The identity of one Commitment across its preserved sequence of statements and factual corrections, with earlier records retained.
_Customer label_: Commitment history
_Avoid_: every promise by the same organization, a copied statement per Constraint, a mutable history without identity

### Project coordination

**Coordination Subject**:
The single Constraint or accepted Commitment identity to which a Coordination Decision belongs; this is an internal concept.
_Customer label_: For: [constraint or commitment name]
_Avoid_: one decision silently applied to several subjects

**Assigned To**:
The named project-team member responsible for the current internal follow-up action on one Constraint or Commitment.
_Avoid_: Utility Owner, automatically the person performing or accepting the external work

**Next Action**:
The next step the project team will take for one named Constraint or Commitment.
_Avoid_: the External Organization's Commitment

**Action Due Date**:
The date the project team sets for completing its own current Next Action; when the date is not yet known, its absence has a stated reason.
_Avoid_: Required By, Promised For

**Coordination Decision**:
A recorded, attributable project-team choice about its own coordination response for one identified Constraint or Commitment.
_Avoid_: an External Party Statement, Documentation Review, contractual approval

**Follow-up Plan**:
The current project response for one Constraint or Commitment: Assigned To, Next Action, Action Due Date, and any required Effect on Key Dates decision.
_Avoid_: an External Party Commitment, a plan copied to every linked Constraint

### Source values and supporting records

**Assertion**:
The value one Document states for one Constraint field, retained with its source reference and kept separate from the project's conclusion; this is an internal concept.
_Customer label_: Source field value
_Avoid_: External Party Statement, the project's settled conclusion, a verified physical fact

**Source Fact**:
What one incoming source states: a typed value with its Source Segment, the mapping that produced it, and the identity it resolves to, captured before any decision about the Project Record; generalizes Assertion to every typed fact on the spine ([ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md); research in [docs/research/baseline-delta-terminology-2026-09-01.md](docs/research/baseline-delta-terminology-2026-09-01.md)).
_Customer label_: What the source says
_Avoid_: the project's conclusion, a verified physical fact, an accepted value, an automatic update

**Source Discrepancy**:
Two or more retained, verified Source Field Values give incompatible answers for the same Constraint field, and no applicable human conclusion resolves them.
_Customer label_: Sources disagree
_Avoid_: Utility Conflict, automatically a contractual dispute, a stored flag

**Discrepancy Resolution**:
A named person's conclusion for one field with conflicting Source Field Values, preserving the sources considered and the decision's scope.
_Customer label_: Record conclusion
_Avoid_: contract settlement, deletion of the losing value, a conclusion about unseen later sources

**Supporting Documentation**:
The verified parts of identified source Documents used to support a recorded fact, with exact document and page or row references.
_Avoid_: a document title alone, proof that every related condition is satisfied

**Cited Passage**:
The exact part of a source Document shown beside a recorded fact, with its document and page or row locator.
_Avoid_: an entire attachment, a Documentation Review judgment

**Supporting Documentation in Use**:
The supporting passages designated for a current published value or Documentation Review, with the purpose identified; selected support can still refer to a superseded Document.
_Customer label_: Used for this value or Used for this review
_Avoid_: every historical citation, automatically the current document revision

**Derivation**:
A result produced from identified Project Record inputs by a stated calculation or rule for the date being assessed, including a nonnumeric rule result.
_Customer label_: Calculated result; How this was calculated
_Avoid_: a source Assertion, an unexplained aggregate

**Recorded Verbal Statement**:
What an External Organization said, recorded by the named project person who heard it, with the conversation date, timing as stated, and explicit Applies To information, including links not yet known.
_Customer label_: Reported by phone or in conversation
_Avoid_: a document or audio recording, written utility confirmation, an inferred Commitment

**Preferred Source File**:
The registered file used for citations when one Document has several renditions; Corridor prefers the structured original to its printed rendering.
_Customer label_: File used for citations
_Avoid_: legal record authority, each rendition treated as independent corroboration

**Supersession**:
The recorded relationship that identifies the Document Revision replacing an earlier revision for current use.
_Customer label_: Replaces or Replaced by
_Avoid_: an inference from filename or upload time, document cancellation without a successor

**Row Identification Rule**:
The recorded rule that says whether a matrix row is identified by its conflict number alone or by its owner and conflict number together.
_Customer label_: How matrix rows are identified
_Avoid_: an identity rule guessed from the printed row number

**Utility Conflict Matrix**:
A table that records Utility Facilities, their relationship to proposed construction, and the investigation or action needed to address conflicts; it can include potential-conflict and no-conflict entries without a final resolution.
_Avoid_: every row requiring relocation, every resolution already known, a complete utility schedule

**Utility Inventory**:
A record of Utility Facilities and their owners within a defined project area, with available location and descriptive information; conflict notes do not by themselves establish a selected resolution.
_Avoid_: a prohibition on resolution columns, proof that all conflicts are resolved

**Retired Matrix Row**:
A source matrix row containing only an explicit retirement or unused-row marker, optionally with its identifier, and no substantive conflict information.
_Customer label_: Row marked not used, or the actual retirement wording
_Avoid_: a populated row merely containing a retirement phrase, a blank row, an abandoned utility facility

**Audit Trail**:
The chronological record of changes and their authors, times, and affected records.
_Avoid_: proof every attempted operation was logged, proof a recorded change was correct

**Provenance**:
Information linking a recorded result to its sources, processing steps, and responsible people or systems. For a processing run, this includes exact inputs, configuration, outputs, and execution identity.
_Customer label_: Source traceability
_Avoid_: certification of physical truth, a source title without traceable identity

### Documentation review and supported outcomes

**Required Documentation**:
The stated records and information needed to demonstrate that one specified construction condition has been met.
_Avoid_: readiness to begin the external work, a universal completion checklist

**Documentation Review**:
A named person's judgment of whether the exact current supporting passages satisfy the stated documentation requirement for one condition, with the requirement and sources reviewed preserved.
_Customer label_: Do these documents meet this requirement?
_Avoid_: Source Passage Check alone, Contract Acceptance, permission to start unrelated work

**Relocation Complete**:
Current reviewed documents support that the specified utility relocation work at the named location is finished.
_Avoid_: relocation design complete, relocation promised, the whole project complete

**Permit Issued**:
The responsible authority issued this identified permit for the activities and conditions stated in it.
_Avoid_: every required permit obtained, the permitted work complete, proof of current validity from issuance alone

**Agreement Executed**:
The identified agreement has met the execution formalities required for that agreement, with the relevant version and supporting record identified.
_Customer label_: Agreement signed by all required parties when signatures establish full execution
_Avoid_: an effective date inferred from signatures, required work complete, an unsigned draft

**No Utility Conflict**:
Reviewed documentation supports that the identified Utility Facilities do not interfere with the specified construction design, with location and design revision identified.
_Avoid_: relocation completed, every utility on the project cleared

### Decisions and signals

**Adopt Baseline**:
A named person's approval of one exact UCM workbook or existing-system export, identified by digest, as the initial accepted Project Record in one atomic revision; the baseline is adopted from the customer's artifact, not authored ([ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md), [ADR-0083](docs/adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md)).
_Customer label_: Adopt baseline; Adopt this matrix as the starting record
_Avoid_: import alone, project approval, sign-off, hundreds of row-level confirmations

**Proposed Delta**:
The typed difference between a Source Fact and the current accepted record, awaiting a decision: new conflict, field changed, promise moved, organization changed, schedule date changed, existing support superseded, apparent removal, or source contradiction. The accepted record is unchanged while it is open; this is an internal name ([ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md)).
_Customer label_: Proposed change, with its type
_Avoid_: change order, change request, contract modification, an automatic update, an urgency score

**Resolve Delta**:
A named person's, or a separately released narrow policy's, decision that closes a Proposed Delta by accepting, editing, rejecting, or deferring it, creating the next Project Record revision ([ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md)).
_Customer label_: Decide this change
_Avoid_: approve alone, close, deletion of the incoming value, contract settlement

**Do Not Add**:
A person's reversible decision that a proposed entry does not belong in the Project Record, with the proposal, reason, actor, and history preserved.
_Customer label_: Do not add to project record
_Avoid_: deletion, rejected contract work, a no-conflict conclusion

**Remove from Active Log**:
A person's reversible removal of an incorrectly admitted Constraint from current work while preserving its record and history.
_Customer label_: Remove incorrect entry from active log
_Avoid_: hiding a real Constraint, work completed, Constraint satisfied

**Attention Reason**:
A current derived condition explaining why one coordination question or follow-up action needs a person's attention; this is an internal concept.
_Customer label_: Why this needs attention
_Avoid_: only document review, a stored status, an urgency score

**Work Item**:
One current coordination question or follow-up need tied to an Extracted Proposal or Coordination Subject and shown with all its Attention Reasons; this is an internal concept.
_Customer label_: The actual question or Next Action; Coordination item when a type label is needed
_Avoid_: one duplicate card per reason, only document review, a construction Activity

**Constraint Alert**:
A condition identified by an automatic check on a Constraint that needs attention, grouped by the rule and ordered only by that rule's own quantity.
_Avoid_: an urgency score, defective construction, a critical-path finding, an agency rules exception

**Evaluation**:
The results from applying one selected set of checks to one project's recorded facts for one specified date, with exact rules, thresholds, and inputs identified; this is an internal concept.
_Customer label_: Constraint Check; Checks as of [date]
_Avoid_: Extraction Measurement, a professional audit, the source schedule's progress date

### Publication

**Coordination Report**:
A dated presentation of recorded Constraints, Commitments, project decisions, and results from the same set of checks; an internal view can refresh, while an approved copy remains fixed.
_Customer label_: Constraint status report
_Avoid_: automatically an agency Utility Status Report, a released artifact that can change in place

**Report Approved for Release**:
The exact retained PDF that a named authorized project person approved for external sharing, with the approver and time recorded.
_Customer label_: Approved to share
_Avoid_: approval of a live page, Contract Acceptance, proof of sending or receipt

**Coordination Summary**:
A source-linked AI draft narrative of the Project Record that cannot change facts or decisions and retains every required Constraint Alert, individually or through a bucket preserving all its members.
_Customer label_: Coordination summary — AI draft
_Avoid_: an independent authority, implied human review, mentioning a category while omitting its alerts

### Related industry concepts and boundaries

These definitions distinguish related construction and document-control concepts. They do not imply that Corridor captures these dates, performs these acts, or provides asset, schedule, delivery, or contract-administration workflows.

**Schedule Data Date**:
The cutoff through which a source schedule includes actual progress, separating recorded progress from its forecast.
_Avoid_: import time, schedule issue date, the date of a Constraint Check

**Actual Completion Date**:
The supported date when the specified work was completed.
_Avoid_: Promised For, Estimated Completion Date, report date, review timestamp

**Estimated Completion Date**:
A prediction of when specified work will finish, with its source and basis.
_Avoid_: a Commitment, actual completion, a forecast invented by Corridor

**Contract Acceptance**:
The formal act by the authorized project or oversight party accepting contract work under the governing contract.
_Avoid_: Documentation Review, Completion Reported, approval to share a report

**Document Transmittal**:
A recorded sending of identified Document Revisions from a sender to named recipients, for a stated purpose and date.
_Avoid_: Report Approved for Release, proof of receipt, an existing Corridor sending workflow

**Receipt Acknowledgment**:
Evidence that an intended recipient acknowledged receiving an identified document transmission.
_Avoid_: permission to share, acceptance of document content, an existing Corridor receipt workflow

**Utility Accommodation Rules Exception**:
An agency decision allowing a specified utility accommodation that departs from an identified requirement of its accommodation rules.
_Avoid_: a Constraint Alert, an automatic waiver, permission granted by Corridor

**Right of Way (ROW)**:
Land or a property interest acquired or devoted to transportation use.
_Avoid_: a permit, a temporary right of entry, an inferred title or legal clearance decision
