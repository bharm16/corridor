---
status: accepted
domain: reports
scope: current product
amends:
  - ADR-0071
---

# A Report Run retains the reading it published, and a Project Record revision supplies only record-owned values

**Amends ADR-0071.**

ADR-0071 gave the Project Record one time axis and removed the record's copies of itself. Among the copies it named were `report_runs.snapshot_json` and `scheduled_report_publications.snapshot_json`, and it rejected keeping them: "**Keep per-report snapshots.** Rejected for new writes: a report binding a revision id cites the same reading a snapshot copies, without the copy." Its consequences say plainly that "New Report Runs bind revision ids instead of writing `snapshot_json`."

#602 built the binding. #603 then measured what the binding could actually answer, over the retained runs themselves, and the measurement came back with a residue that no revision reference reproduces. That residue is not work left undone. It is a modelling error in ADR-0071's premise that a report's retained payload is a copy of record state.

## The three owners

| Owner | What belongs there |
|---|---|
| **Project Record revision** | Accepted values and decisions effective at that revision |
| **Report Reading occurrence** | The population included, derived alert outcomes, the documentation-requirement result, the statement-projected Promised For, and the rules and thresholds used |
| **Release package** | The exact issued bytes, artifact digests, authorization, and predecessor |

ADR-0071 had two of these. The middle one is the decision this ADR records.

## What the measurement found

#603 rebuilt every retained run's baseline from the revision it names and compared it field for field. Two fields — `resolution_strategy` and `need_date` — are record values, carried by the spine as typed structured-cell Facts; 212 field comparisons agreed with zero disagreement. The Ledger identity a run stored is a correspondence the Ledger itself answers, so the copy was not needed for it either. Three things were left, and each is left for a reason that is a property of the value rather than a gap in the pass:

- **`ready` and `exceptions`** are not record state at all. They are a *reading* of the record under one ruleset version, one threshold configuration and one evaluation date. ADR-0002 keeps the documentation-requirement result derived and ADR-0044 keeps lifecycle derived; both prohibit writing a current status onto the Constraint. Neither prohibits retaining the immutable result of a dated report occurrence, and no revision carries one, because a revision says what the record said and these say what a rule said about it on a day.
- **The population** — which subjects this report included — answers "what did this report's scope and rules cover", which is not an accepted fact about a Utility Conflict. A revision cannot answer it even in principle, because the population is a function of the report's scope, not of the record.
- **`committed_date`**, as the payload stored it, is the date projected from the external party's current statement, not the record's own `committed_date` Fact. The two carry the same name and are different quantities: the projection is `None` where no statement has been recorded, whatever the source cell says. Letting one stand in for the other would change the diff because its inputs changed shape, which is the single failure #598 was written to avoid.

## The decision

**A Report Run retains the reading it published. The retained payload is not a cache and has no expiry rule.**

1. `report_runs.snapshot_json` and `scheduled_report_publications.snapshot_json` are the immutable **Report Reading payload** of one dated occurrence. They are reclassified from "rebuildable compatibility cache" — the demotion #602 recorded in the column comments and in the code — to what they are.

2. **Reproducible means retrievable and verifiable, never silently recomputed with today's code.** A historical report retains the exact outcomes it published, the accepted revision, the evaluation date, the ruleset identity and version, the threshold configuration and its values, the provenance mode, and the population included. Re-executing a retained reading under a retained version is a *verification exercise*; it never replaces what the report actually published.

3. **The payload carries a schema version and a content digest.** The digest is computed over the payload's own canonical bytes so a retained reading can be checked against itself, independently of the release package's digest over the issued artifact. Legacy payloads stay readable exactly as written; a payload with no version key is version 1 and is never rewritten or backfilled.

4. **A statement-projected value gets its own name.** In the new payload version the projected date is `published_promised_for` (internally `statement_projected_promised_for`), bound to the statement or commitment-lineage identity it was projected from and to the projection-rule version that produced it. One key never again carries both the source-cell Fact and the statement-derived report value.

5. **The reading's governed names are used in the new payload version**: `documentation_requirement_met` for what was `ready`, `constraint_alerts` for what was `exceptions`, `published_promised_for` for what was `committed_date`. These are the terms ADR-0002 and ADR-0010 already govern for those quantities; the payload was the last surface still using the implementation words.

6. **Retention is the Report Run's retention.** The payload is retained for as long as the Report Run or the released package is retained, and its retention ends with the governing customer-environment retention. There is no cache TTL, and building one would be a defect.

7. **A record-owned field on the payload still defers to the revision reference**, which is what #602 and #603 built: the diff reads `resolution_strategy`, `need_date` and the Ledger identity through the reference. Where the reference and the retained copy disagree about a field both can answer, the copy stands — it is what the report published — and the disagreement is reported as drift rather than resolved silently.

## Considered options

**Expire the payload once the equivalence proof passed, as ADR-0071 and #602 planned.** Rejected, and this ADR exists to close it. The proof passed for exactly the fields a revision owns and could never have covered the three it does not. Expiring the payload on that proof would delete the only witness of what a past report said about readiness, alerts, and the promised date — and would do it on the strength of a measurement that never examined those values.

**Keep the payload but leave it labelled a cache with the expiry rule "not yet written".** Rejected. A column comment that calls immutable published evidence a rebuildable cache is an instruction to a future implementer to delete it, and the honest version of that comment cannot be written because there is no rule that would make it true. #598's own status note says the expiry rule "cannot be written as the record stands".

**Recompute a historical reading on demand from the retained revision and a retained ruleset version.** Rejected as a substitute; kept as a verification exercise. Recomputation depends on the code that implements the rules, not only on the recorded version of them, so a silent recomputation would let a later code change alter what a past report is said to have published. Verification compares a recomputation against the retained payload and reports a difference; it never replaces it.

**Move the population, the alerts and the projected date onto the Project Record revision.** Rejected. It would make the record assert things it does not know — that a report included a particular subject, that a rule fired on a day — and would reintroduce the mutable status ADR-0002 and ADR-0044 removed, in a new place.

**Write both the legacy and the governed key names in the new payload version.** Rejected. The whole defect is one key carrying two quantities; keeping `committed_date` beside `published_promised_for` preserves the ambiguity and adds a duplicate value to the same row. Legacy payloads are read through a translation instead, which costs one reader and rewrites nothing.

**Edit ADR-0071 in place.** Rejected under `docs/adr/README.md`. A writer complying with ADR-0071 — one that binds a revision and writes no payload — would fail review under this wording, so this is a material change and takes a new sequential number with reciprocal `amends` metadata.

## Consequences

- **ADR-0071's rejection of per-report snapshots is amended, and nothing else in it is.** Its one time axis, its projection of effective decisions, its removal of the record's copies of *record state*, and its equivalence gate all stand. What changes is the sentence rejecting the retained per-report payload for new writes: the payload is retained, because what remains in it was never record state.
- **ADR-0002, ADR-0010 and ADR-0044 are untouched.** Readiness, alerts, and lifecycle stay derived and are still stored nowhere on the Constraint. A dated occurrence retaining what it published is not a status written back onto a record.
- **ADR-0086 is untouched.** The release package still owns the issued bytes, their digests, the authorization and the predecessor. The payload digest introduced here is over the reading, not over any issued artifact, and the two are separate identities on purpose.
- **The architecture ratchet stops counting the payload as a value-copying carrier.** It is not a copy of another row's state, so the entry leaves `VALUE_COPYING_CARRIERS` in `tests/test_architecture.py` with the reason recorded beside the list. The removal must not be read as a carrier having migrated; nothing about the historical rows changed.
- **#598's first criterion is amended**: record-owned fields reference the revision, and occurrence-owned fields stay on the immutable report reading. There is no snapshot removal or expiry rule to deliver.
- **No TTL, sweeper, or retention job is built for the payload**, and adding one later requires a decision that supersedes this clause.
