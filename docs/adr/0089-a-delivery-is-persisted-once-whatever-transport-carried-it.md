---
status: accepted
domain: intake
scope: current product
amends:
  - ADR-0078
  - ADR-0083
migration: no unified SourceDelivery family and no ConnectorCheckpointAdvance relation exist; a pull delivery is still unpersisted and the connector cursor still lives on a completed Due Work receipt (#599).
---

# A delivery is persisted once, whatever transport carried it

**Amends ADR-0078 and ADR-0083.**

ADR-0078 decided that every source enters through a project-bound connector under one small contract, and listed what every connected source carries: a customer and project binding, connector identity, external source identity and version, the external system's own timestamps, a byte digest, a synchronization cursor, a "fetch attempt and terminal outcome (stored, duplicate, failed, refused)", and an idempotency identity so that a replayed change stores nothing twice. ADR-0083 then corrected the contract into three layers and named the `SourceEnvelope` as **the one normalized ingress record every channel produces**, whether the bytes were pulled or pushed.

The implementation did not come out that way, and #599 is the ticket that found it. #511 built push intake with the envelope persisted: `push_deliveries` is, in its own docstring, "ADR-0083's SourceEnvelope, persisted", with a unique idempotency key, a structural uniqueness constraint over project, delivery identity and content digest added by #457, and a database trigger that re-derives both digests from the row's own columns and refuses a row whose identity is not its own. #496 built the pull half with no delivery record at all. `sync_pull_connector` produces `SourceEnvelope`s and hands them on; `connector_polling` (#488) keeps only the checkpoint token, on the completed Due Work receipt of the occurrence that reached it. Idempotency there rests on two real but indirect properties — the same bytes land on the same digest in content-addressed storage, and a receipt only completes after every listed change is stored — and on nothing that can be queried, constrained, or attributed.

**That asymmetry is an accidental implementation artifact, not a domain distinction.** ADR-0083 already decided that pull and push produce the same record. Nothing about a pull delivery makes it less worth remembering than a pushed one: both name a customer, a project, a channel, an external object and version, a digest, and a moment Corridor took delivery. What differs is only where the identity comes from — a pull connector reads the location's own item id and version, a pushed delivery gets its identity from the authenticated transport — and #511's own docstring already says that is the only difference and that the two "meet only at the envelope". Persisting one and not the other means the answer to "did we receive this revision" is a re-listing of the external system for one transport and a database read for the other, and it means a delivery the intake gate (#490) refused is recorded for push and lost for pull.

This ADR decides that pull deliveries are persisted, and that they are persisted in the same family as pushed ones.

## One immutable delivery family

**One immutable `SourceDelivery` family records every delivery, pull and push alike.** Each row binds:

- the customer and the project;
- the connector or channel configuration identity, and its version;
- the transport — pull or push;
- the external object identity and external version;
- the provider's own timestamps;
- the content digest, and the object-store or quarantine reference for the bytes;
- the delivery identity and the idempotency key;
- the service identity and the run identity that took delivery;
- the disposition;
- the refusal or failure reason, where there is one; and
- the observed time.

Rows are append-only. A later attempt on the same external version is a new observation with its own disposition, never an update in place.

`SourceDelivery` is an internal technical name. It introduces no customer-facing term, so the terminology-research procedure in `docs/agents/domain.md` is not triggered. Whether `push_deliveries` becomes a member of this family by extension or by migration into it is an implementation decision for #599; what this ADR requires is that push and pull deliveries are **one queryable family under one identity rule**, not two tables a reader has to union and hope agree.

## Identity is a database constraint, never a Python replay check

**Delivery identity is enforced by a unique constraint in PostgreSQL over the declared identity columns.** It is never enforced by a Python-side check that reads rows, decides they do not match, and then writes.

This is not a stylistic preference. #457 set out to make a duplicate unrepresentable across the permanent-state families and found, in the maintainer's words on #599, "two constraints that existed but proved nothing — a nullable digest behind a partial index, and a key computed in Python and never checked against its row". A constraint over a nullable column does not fire on the rows that most need it, and a key the application computes and the database never re-derives constrains only the applications that remember to compute it the same way. #511's trigger, which re-derives the envelope digests from the row's own columns and refuses a row whose identity is not its own, is the shape that works. **Do not add a third constraint that proves nothing.**

The corollary is that the Python-side guard in `delta_generation._appended_signatures` is removed. It selects the signatures a source revision has already proposed and skips a replay that matches, but its signature ignores `accepted_value` while the Proposed Delta's own content digest includes it — so it is a second, weaker definition of an identity the database now holds. Two definitions of one identity is the defect regardless of which is currently right; the database's is the one that survives.

## Five dispositions, and what each one means for the cursor

A delivery's disposition is one of:

- **`stored`** — the exact bytes are durably in the content-addressed store under their digest;
- **`duplicate`** — this delivery identity was already taken delivery of, and nothing new was written;
- **`quarantined`** — the intake gate (#490) held the bytes rather than admitting them, and the quarantine reference says where they are;
- **`terminally_refused`** — the intake gate refused the delivery outright under the approved intake policy, and it will never be admitted; and
- **`transient_failure`** — the scanner, the object store, or the provider failed in a way that says nothing about the delivery itself.

The distinction between the last two is the reason the list exists. `terminally_refused` is a fact about the delivery. `transient_failure` is a fact about Corridor or the provider on one attempt.

### The checkpoint advance rule

ADR-0083 fixed the safe half of this: a checkpoint advances only after every change up to and including the exact token is durably stored with its digest, and never past an unstored change. That rule, read literally, has no answer for a delivery that will never be stored, because it was refused. A connector that meets a permanently refused item can then never advance again, and one poisoned object stalls the whole location forever.

The rule is therefore stated in full:

- A **`stored`** or **`duplicate`** delivery permits the checkpoint to advance past it, exactly as ADR-0083 decided.
- A **`terminally_refused`** delivery permits the checkpoint to advance past it **only when its bytes, or its exact digest together with the quarantine and refusal evidence, have been durably handled under the approved intake policy.** The evidence of the refusal is what makes the advance safe: the record can still answer what arrived, what was refused, and why, without the bytes.
- A **`quarantined`** delivery permits an advance on the same condition and no other — the quarantine reference is durable and the refusal evidence is recorded.
- A **`transient_failure` must never advance the checkpoint.** A scanner that timed out, an object store that rejected a write, or a provider that returned a 500 has told us nothing about the delivery, and advancing past it silently drops a source revision that Corridor will never re-list.

The failure modes here are asymmetric in the same way ADR-0088 described for the test gate. A cursor that fails to advance is visible: the connector re-lists, work repeats, and somebody notices. A cursor that advances past a change nobody stored is silent, and the missing evidence is discovered later, if ever, attached to a Proposed Delta that never appeared.

## Checkpoint history is its own append-only relation

**Checkpoint advances are recorded in their own append-only relation, and the current checkpoint is derived from it.** Each advance names the connector configuration, the token reached, the run that reached it, the deliveries the advance covered, and the time.

Today the cursor lives on the completed Due Work receipt of the polling occurrence that reached it. `connector_polling`'s docstring is explicit about why, and about what was rejected: "A checkpoint table was rejected: the migration window is closed (`corridor.migrations.policy`), and the runtime already writes exactly the durable, append-only, ordered record a cursor needs." The second half of that sentence is true and the first half is a scheduling constraint standing in for a design one. The consequence is that **the external cursor is a derived property of receipt retention.** #488 noted this and mitigated it by retaining those receipts for 3650 days. Retention is not identity: a receipt sweep, a retention-policy change, a disposition under ADR-0080, or an ordinary operational cleanup would reset a live connector's external cursor and cause it to re-list an entire location — or, worse, land it on a stale token. **Deleting an old Due Work receipt must never move an external cursor**, and the only way to guarantee that is for the cursor not to live there.

The migration-window objection is answered by the window's own rule rather than waived: under ADR-0087 as amended by ADR-0088, and `src/corridor/migrations/policy.py`, a migration-bearing change folds into the current unreleased transition rather than appending another revision. The window bounds how a schema change lands. It is not a reason to leave a durable identity un-modelled.

## Considered options

**Keep content-addressing plus the receipt cursor, and document the asymmetry.** Rejected, and this was the live alternative — #599 asked the question in exactly this form, because #457's finding was that the asymmetry was *undeclared*, not that a ledger was obviously right. Content-addressing genuinely does make the same bytes idempotent, and the checkpoint rule genuinely does prevent skipping an unstored change. What it cannot do is answer "did we take delivery of this external version" without re-listing the external system, record a delivery the intake gate refused, attribute a delivery to the run that took it, or survive the deletion of a Due Work receipt. Three of those four are things ADR-0078 already said every connected source carries. The design was not chosen against those requirements; it was reached under a closed migration window and then inherited.

**Persist pull deliveries in their own table beside `push_deliveries`.** Rejected. It is the cheapest change and it makes the asymmetry permanent instead of removing it. Every consumer — the intake operator's "what arrived this week", the shadow comparison in #499, the disposition inventory in #514, the analytics contract in #558 — would have to union two tables and reconcile two identity rules, and each new channel would face the same choice again. ADR-0083 decided there is one envelope; the persistence follows the envelope, not the transport.

**Extend `PullConnector` with a delivery-recording method.** Rejected for the reason #511 already gave when it declined to add a fifth method for pushed deliveries: the four methods are a cursor protocol, and a connector "does not read, classify, or route content" (ADR-0078). Recording a delivery is the runtime's act on the envelope the connector returned, not a connector responsibility, and putting it in the contract would mean every adapter could get it wrong differently.

**Keep `_appended_signatures` alongside the database constraint as defence in depth.** Rejected. Defence in depth means independent layers enforcing *the same* rule — which is exactly what ADR-0083 called for on the record-write boundary. Two layers enforcing *different* rules, where one ignores `accepted_value` and the other includes it, is not depth; it is a disagreement that will eventually be resolved by whichever layer runs first.

**Advance the checkpoint on every terminal disposition, including transient failure.** Rejected. It gives a simple monotone cursor and loses source revisions silently, which is the one failure this whole area exists to prevent.

**Never advance the checkpoint past a delivery that was not stored.** Rejected. It is ADR-0083's rule read literally, and it means one permanently refused object stalls a connector forever with no operator recourse but to edit the cursor by hand — which is the state this ADR is trying to get the cursor out of.

**Solve the receipt-retention dependency by lengthening retention further.** Rejected. 3650 days is already ten years and it did not make the dependency correct; it made the failure rare and far away. The cursor is not a fact about a Due Work occurrence.

## Consequences

- **ADR-0078's "what every connected source carries" list now has a home.** Its "fetch attempt and terminal outcome (stored, duplicate, failed, refused)" is restated as the five dispositions above, with `failed` split into `transient_failure` and `terminally_refused` because the checkpoint rule turns on that difference, and `quarantined` named separately because #490 holds bytes it neither admits nor destroys. Its idempotency identity clause is restated as a database constraint. Nothing else in ADR-0078 changes.
- **ADR-0083's checkpoint clause is extended, not replaced.** "A checkpoint advances only after every change up to and including the exact token is durably stored with its digest" continues to govern stored and duplicate deliveries; this ADR adds the refused and failed cases it did not cover. Its three-layer intake design, its `SourceEnvelope` definition, and the `PullConnector`/`PushIntake` split are unchanged.
- **#488's cursor design is superseded.** The token no longer lives on the completed Due Work receipt, and the 3650-day receipt retention is no longer load-bearing for connector correctness. The Due Work receipt remains the record of the occurrence; it stops being the record of the cursor. `connector_polling`'s module docstring records a rejected alternative that this decision reverses, and it is rewritten when #599 lands — the docstring convention in `CLAUDE.md` requires it to say what was tried before, and "a checkpoint table was rejected because the migration window was closed" is now history rather than the current design.
- **`delta_generation._appended_signatures` is removed**, and the replay guarantee it provided comes from the database-held Proposed Delta identity that #457 established.
- **A refused delivery becomes a record.** The intake gate's refusals and quarantines are queryable per project and per connector, which is what makes an intake failure an operational metric rather than an absence somebody has to notice.
- **This blocks live activation (#535) only when a pull connector is selected for a partner.** A manual-upload or push-only pilot already has the persisted family through #511 and is not held by this work. #606's first real source path — a later UCM revision uploaded through #511 — is explicitly not blocked.
- **#457 stays closed.** It delivered the write-side constraints for the five permanent-state families it named; the pull delivery family did not exist for it to constrain. This ADR creates the family that a follow-on constraint applies to.
- The schema change this requires folds into the current unreleased transition under `src/corridor/migrations/policy.py`, and does not append a revision to the window.
