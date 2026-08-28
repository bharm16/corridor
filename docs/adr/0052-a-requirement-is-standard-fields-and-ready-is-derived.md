---
status: accepted
---

# A requirement is standard fields, and Ready is derived

A Constraint's Required Documentation was a free-text sentence, and readiness was one per-instance human act (ADR-0037) standing in front of a per-passage sufficiency mark. In practice the requirement text was never written: 598 Constraints in the development database carry zero requirement texts. Meanwhile the system already collects and checks the evidence, then asks a person to redo the whole comparison by hand. This ADR supersedes the shape of ADR-0037's per-instance act. The decision ruling is on [#326](https://github.com/bharm16/corridor/issues/326).

## Decision

**The requirement is a standard set of fields. The system fills every field it can prove, with the source attached. A person answers only interpretation fields. Ready is derived, never clicked.**

- Every field stores two things: the value, and the exact source it came from (document, page or cell). An empty required field means not done.
- **Machine fields are predicates, never stored checkmarks.** "An as-built covering this location is on file" is computed when read, against the current record, carrying its proof. Replace the document and the predicate simply evaluates against the new state. This matches ADR-0044: lifecycle is derived, not a mutable status.
- **Human answers are the only stored records**: who answered, what they answered, the exact documents the answer was about, when — append-only. If one of those documents is superseded, the answer stops binding and the question returns on its own. The interpretive residue is small and deliberate: reading whether a letter is an actual unconditional approval. One click, source displayed.
- **Ready = all required fields are filled.** No ready button. No global sufficiency judgment. In TxDOT's own status ladder (Identified, Confirmed, Resolved, Cleared), Ready is the Cleared concept.
- Two fields the extraction already carries **select** which other fields are required: the resolution strategy (relocate needs as-built plus approval; abandonment needs abandonment documentation; protect in place needs nothing external) and the cost responsibility (reimbursable work needs an executed agreement reference). A genuinely special condition is an added entry, visibly marked. Rare by design.

## The core is agency-neutral

Fields are defined once and validated against four agencies' printed forms in this corpus — TxDOT, WSDOT, FDOT, CDOT. Each agency gets a name mapping, the same pattern ADR-0009 established for extraction. Core concepts, each present in at least two agencies' standards or federal rule (23 CFR 645, FHWA SHRP2): conflict identity; owner; facility type, size, material; location; resolution strategy; lifecycle status; property interest (permit versus easement, with the parcel ladder where easements apply); cost responsibility; agreement or permit reference; the three dates (project estimate, the organization's promise, actually achieved); investigation quality.

Growth rule: a field enters the core only with a published source in more than one agency's standard or federal regulation. A one-agency concept stays a documented local exception (today: CDOT's outage tolerance). One recorded exclusion: relocation sequencing appears in two agencies and would qualify, but Corridor deliberately does not model work order — declined knowingly, not overlooked.

## What this replaces, and what it keeps

- The free-text requirement field: unused, retired without migration.
- The per-passage sufficiency toggle: legacy marks stay in history and keep their effect on a Constraint until its first structured requirement takes over. Never deleted, never converted.
- The human judgment moves earlier and becomes durable: confirm the standard field requirements at setup (a model drafts, a person confirms), then answer interpretation fields per conflict.
- Documentation state, Completion Reported, and Contract Acceptance remain three separate facts. A lapsed field never claims physical work is undone.

## Consequences

Ticket #347 implements this model. Ticket #353's support-loss notification lapses per field. Replaces #322's decision gate 4. The amending relationship to ADR-0037 is this document.
