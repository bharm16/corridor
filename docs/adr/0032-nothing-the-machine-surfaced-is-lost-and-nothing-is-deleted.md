---
status: accepted
---

# Nothing the machine surfaced is lost, and nothing is deleted

> **Terminology amended 2026-08-27 by [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses the adopted construction terms. Historical quotations and implementation identifiers retain their original spelling; the authority boundaries are unchanged.

Mechanical admission (ADR-0029) changed what the residue is made of. Two kinds now arrive that the old sign-off flow never had to answer for: statements from the minutes the policy could not place on any record, and junk that reaches the record because rows enter without a human first agreeing to each one. Decision: **both are handled in the open, and neither by deletion.**

An unplaced statement is kept and shown in **one pile** — not a lane, not a second queue — with the reason it could not be placed rendered in the reviewer's terms rather than the check's. A reviewer names the record it belongs to, or tosses it with a reason. Attaching is a human act that supplies exactly the one thing the policy lacked: which record the statement names. Everything the policy would still have refused is refused here too — an unreadable date, a type outside the policy, and above all the actor boundary, because a project-side actor stating a delivery date is an internal action item and never an External Party's commitment (ADR-0026). A human naming a record does not change who spoke. The queue points at the pile and never becomes it, including when the queue is empty, because an empty queue is not an empty desk.

A junk record is **dismissed with a stated reason** — duplicate, not-a-conflict, wrong — and dismissal is not a delete. The row, its Supporting Documentation, its Assertions and its history all stay exactly where they are; an append-only `dependency_dismissals` row carries the reason and the name, and `dependencies.dismissed_at` is the projection the working list and the exception engine filter on, the shape the Promised For projection already takes. A dismissed record's page stays reachable, so anyone asking why a conflict left the list gets an answer with a name and a date on it. It also stops raising Exceptions, because a record nobody is working should not generate findings about nobody working it.

## Considered options

**Drop statements the policy cannot place.** Rejected outright: a dated promise from a meeting is the thing this product exists to catch, and the failure mode — losing one silently — is the worst one available.

**A statements lane in the queue.** Rejected: the session that produced ADR-0029 spent its length removing places a reviewer has to find before working. One pile the queue points at is a list; a lane is a second desk.

**Let attaching bypass the policy's checks, since a human is asking.** Rejected for the actor boundary specifically. A human supplies the judgment the policy lacked — which record — and gains no authority to make LJA's engineer into AT&T.

**Hard-delete a dismissed record.** Rejected: it is the one operation this system cannot walk back, and the question "why is this conflict not on my list?" has no answer afterwards. Append-only also makes restoring possible later without a schema change, if anyone ever wants it.

## Consequences

`event_admission` grows the human `attach_statement` beside the policy that does the same write, so the actor boundary and the date parsing stay in one file. A `/statements/{slug}` page is the pile; the queue header and the empty-queue page both point at it with a count. `adjudicate` grows `dismiss_dependency` and `DISMISS_REASONS`. `ledger.browse` and `exceptions.evaluate` filter dismissed records; every other reader still sees them, which is what makes the history reachable. Reject gained an optional same-app return path so tossing a statement returns to the pile.
