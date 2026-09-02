---
status: accepted
domain: record-inclusion
scope: current product
supersedes:
  - ADR-0029
amends:
  - ADR-0001
  - ADR-0010
  - ADR-0026
  - ADR-0031
  - ADR-0035
  - ADR-0042
  - ADR-0054
  - ADR-0055
  - ADR-0057
  - ADR-0070
  - ADR-0071
  - ADR-0034
amended_by:
  - ADR-0083
  - ADR-0084
migration: Adopt Baseline, Propose Delta, and Resolve Delta commands do not exist yet; the record-write boundary is not yet an enforced architecture test.
---

# The record changes by captured source fact, adopted baseline, proposed delta, and resolved delta

**Supersedes ADR-0029. Amends ADR-0001, ADR-0010, ADR-0026, ADR-0031, ADR-0035, ADR-0042, ADR-0054, ADR-0055, ADR-0057, ADR-0070, and ADR-0071.**

ADR-0029 decided that a row enters the record mechanically when the machine can anchor it and that nothing stands in front of the list. That was the right answer to the wrong product: a record-keeper that starts from nothing needs unattended writes to show anything on day one. ADR-0075 changes the starting point. The customer already has an accepted record, and the product's job is to keep it current from new evidence with less operator work than today. Under that premise an unattended write to a material field is the failure the customer fears most, and the current code embodies the split: ADR-0029 says one matrix suffices, while `dependency_admission` still requires that the declared Active Run of each revision agree exactly before a row enters. ADR-0042 lets an automatic policy record an unknown-scope Commitment; ADR-0070 says obligations and Commitments read from prose are always Human Record Decisions. This ADR replaces both sides of each contradiction with one model.

## Decision

The Project Record changes through **four explicit operations**, and no other path.

**1. Capture Source Fact.** Record exactly what an incoming source says: the typed value, the segment it came from (ADR-0068), the mapping that produced it, and the identity it resolves to. Capture may be automatic where the locator, value, mapping, and identity are mechanically proven. A captured fact is a statement about the source, never about the record (ADR-0067, ADR-0071). It is immediately visible and searchable.

**2. Adopt Baseline.** A named project person adopts one exact UCM workbook or existing-system export as the initial accepted Project Record. The act binds the source digest, the sheets or records included, the importer identity and version, the project, the time, and every exclusion. It is **one bulk project decision**, not hundreds of row-level clicks, and it is **atomic**: the complete baseline is adopted or nothing is. It may create one Project Record revision containing many initial decisions (ADR-0071's one-revision-per-atomic-change rule holds; the atomic change is the adoption).

**3. Propose Delta.** Compare newly captured source facts against the current accepted record and produce a typed proposal. The initial delta types are: new conflict; field changed; promise moved; organization changed; schedule date changed; existing support superseded; apparent removal; source contradiction. A proposal names the current accepted value, the incoming value with its source, and the delta type. **The current record remains unchanged while a proposed delta is unresolved.** The incoming value remains visible and searchable as a captured fact.

**4. Resolve Delta.** A human, or a separately released narrow policy, accepts, edits, rejects, defers, or resolves the delta. That action creates the next Project Record revision under ADR-0071's rules: one revision, one human principal or one released policy, append-only, reversible by a later decision and never by editing history (ADR-0035, ADR-0039).

### What may project into the accepted record automatically

Automatic record projection is restricted to:

- exact unchanged support transfer (ADR-0022 as amended by ADR-0034);
- exact no-semantic-change normalization (whitespace, casing, and format canonicalization that a replay proves value-preserving);
- baseline adoption under the attributable bulk command above;
- separately released, class-specific policies with replayable proof (ADR-0050) and measured false-write limits recorded before release.

**A structured cell automatically becomes a source fact. It does not automatically replace the accepted record merely because it came from a structured cell.** This narrows ADR-0070: the segment kind still determines what may be captured automatically; it no longer determines what may be projected automatically.

### Material fields

The following normally require a delta decision, not automatic projection:

- Utility Owner;
- conflict identity;
- facility size, type, material, or location when the change affects matching;
- resolution method;
- Promised For;
- Required By linkage;
- completion or closure;
- Applies To;
- agreement or permit status;
- cost responsibility.

A class-specific policy may be released for one of these fields only with its own proof and false-write limit, and its release is recorded as a policy version, never as a change to this list.

### Presentation of proposed deltas and exceptions

ADR-0010 prohibits a total order across categories; ADR-0035 orders the Work List by five consequence groups. Both stand, reconciled as **transparent priority bands**: the bands are ADR-0035's groups, each band is ordered by one declared quantity as ADR-0010 requires, and no numeric severity score exists. A proposed delta joins the band its delta type implies (a moved promise sits with Changes to Promised Timing; an apparent removal sits with statements Corridor could not place until a person resolves it).

### The record-write boundary is enforced mechanically

"Only `adjudicate` writes Constraint Records" is today a convention held up by module structure and review. This ADR requires an architecture test in `tests/test_architecture.py` that fails when any module other than the declared record-writing seam issues an insert or update against the accepted-record tables. The test names the allowed writers explicitly; adding a writer is a change to the test, reviewed as such.

## Contradictions this resolves

- **ADR-0029 versus `dependency_admission`.** One matrix does not admit rows into the accepted record, and exact agreement across revisions does not either. One attributable baseline adoption establishes the accepted record; every later matrix revision is captured as source facts and compared as proposed deltas. Corroboration and Source Discrepancy (ADR-0031) become delta types (`field changed` with agreeing sources; `source contradiction`) rather than admission outcomes.
- **ADR-0042 versus ADR-0070.** Three distinct things were conflated: automatic source-fact capture (permitted whenever mechanically proven), automatic accepted-record projection (permitted only for the exact classes above), and ordinary human delta decisions (everything else). ADR-0042's unknown-scope Commitment class survives as an automatic capture with a proposed delta, not as an automatic projection; its "Applies To: not yet known" state remains current Project Record state once a person resolves the delta.
- **ADR-0010 versus ADR-0035.** Resolved by the priority bands above.

## Amendments, by ADR

- **ADR-0001.** A source field value (Assertion) is a captured source fact; the recorded conclusion changes only through a resolved delta or an adopted baseline.
- **ADR-0026.** Event Record Inclusion by policy is capture plus a proposed delta. The policy's authorization and receipt machinery is unchanged.
- **ADR-0031.** A Source Discrepancy is the `source contradiction` delta type; it is still carried on the row and settled, never erased.
- **ADR-0042.** Policy writes exact cases to the captured-fact layer; projection into the accepted record for a material field requires a released class-specific policy or a human decision.
- **ADR-0054, ADR-0055, ADR-0057.** Scope resolution, standing rules, and self-identifying schedule dates continue to determine what the proposal says. They no longer write the accepted record directly; a schedule date change is a `schedule date changed` delta.
- **ADR-0070.** Segment kind governs automatic capture, not automatic projection.
- **ADR-0071.** Adopt Baseline is one atomic revision with many decisions.
- **ADR-0010 and ADR-0035.** Priority bands as above.

## Considered options

**Keep mechanical admission and add a review queue in front of exports.** Rejected. The record would already be wrong between admission and review; every reader (alerts, reports, chase lists) would read the wrong value; and the review queue is ADR-0029's rejected two-worlds tax reintroduced at the exit instead of the entry.

**Row-level baseline acceptance.** Rejected. A 500-row UCM would need 500 attributable clicks to establish a record the customer already accepts. One bulk adoption binding the exact digest is both less work and better provenance.

**Automatic projection for every structured cell, with reversals.** Rejected. Reversal after a false write to Promised For has already reached a chase list or a report. The false-write rate on material fields is the metric ADR-0075 says the product is sold on; unattended projection makes it unmeasurable at the moment it matters.

## Consequences

- ADR-0029 becomes `superseded by ADR-0076`; the amended ADRs record `amended_by` metadata and keep their bodies.
- Source Fact, Adopt Baseline, Proposed Delta, and Resolve Delta are new product terms. Their glossary entries follow the terminology procedure in `docs/agents/domain.md`; this ADR fixes their meaning, not their customer labels.
- Existing automatic inclusion policies (structured-cell inclusion, unknown-scope Commitment capture) continue to run against the captured-fact layer. Their projection into the accepted record stops being automatic when the delta commands land, and that cut-over is part of the ADR-0081 migration, not a separate feature.
- This ADR precedes new UI or extraction work, because it changes what an automatic extraction is allowed to do.
