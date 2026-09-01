---
status: accepted
domain: project-record
scope: current product
amended_by:
  - ADR-0080
---

# Nothing the machine surfaced is lost, and nothing is deleted

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Mechanical record inclusion (ADR-0029) changed what the residue is made of. Two kinds now arrive that the old sign-off flow never had to answer for: statements from the minutes the policy could not place on any record, and junk that reaches the record because rows enter without a human first agreeing to each one. Decision: **both are handled in the open, and neither by deletion.**

A Statement Needing Clarification is kept in **one visible list**, with the missing speaker, timing, or affected-Constraint facts stated in the reviewer's language. A reviewer resolves only the fact they are authorized to decide or chooses **Do Not Add** with a reason. Attaching supplies the missing link to a record; it does not establish who spoke or what timing the source states. Everything the policy would still have refused is refused here too — unsupported timing, a type outside the policy, and above all the actor boundary, because a project-side actor stating a delivery date is an internal action item and never an External Organization's commitment (ADR-0026). A human naming a record does not change who spoke. The list remains discoverable even when no other work is pending. An already recorded statement with accepted unknown scope is not an unrecorded proposal; its remaining coordination work follows ADR-0042.

A junk record uses **Remove from Active Log** with a stated reason — duplicate, not-a-conflict, wrong. Removal is not deletion or completion of construction. The row, Supporting Documentation, Assertions, and history remain; an append-only `dependency_dismissals` row carries the reason and name, and `dependencies.dismissed_at` is the projection the working list and constraint check engine filter on. The record's page remains reachable, so anyone asking why it left the list gets an answer with a name and date. It stops raising Constraint Alerts because it was an incorrect entry, not because its construction condition was met. A real constraint resolved through work or a design change needs its supported outcome, not this removal action.

## Considered options

**Drop statements the policy cannot place.** Rejected outright: a dated promise from a meeting is the thing this product exists to catch, and the failure mode — losing one silently — is the worst one available.

**A second statements queue.** Rejected: the session that produced ADR-0029 spent its length removing places a reviewer has to find before working. One visible list retains the work; a second queue would require another place to check.

**Let attaching bypass the policy's checks, since a human is asking.** Rejected for the actor boundary specifically. A human supplies the judgment the policy lacked — which record — and gains no authority to make LJA's engineer into AT&T.

**Hard-delete a dismissed record.** Rejected: it is the one operation this system cannot walk back, and the question "why is this conflict not on my list?" has no answer afterwards. Append-only also makes restoring possible later without a schema change, if anyone ever wants it.

## Consequences

`event_admission` grows the human `attach_statement` beside the policy that does the same write, so the actor boundary and the date parsing stay in one file. A `/statements/{slug}` page is the pile; the queue header and the empty-queue page both point at it with a count. `adjudicate` grows `dismiss_dependency` and `DISMISS_REASONS`. `ledger.browse` and `exceptions.evaluate` filter dismissed records; every other reader still sees them, which is what makes the history reachable. Reject gained an optional same-app return path so tossing a statement returns to the pile.
