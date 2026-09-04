---
status: accepted
domain: human-work
scope: current product
amends:
  - ADR-0035
migration: the four primary decisions, the dated Defer receipt, the one atomic packet transaction that commits them, the derived reading that keys the packets and partitions every open delta exactly once, the shared accessible primitives, the source-revision review screen, and the cross-source coordination screen exist (#519, #526, #494, #559, #527, #528); all three packet keys are produced by `delta-partition-v2`, which also splits materially different actions and names an identity contradiction as one; the three visible consequence levels are derived from the per-project configured issue content and its executable customer policy (#640, #641), and are absent with a stated configuration problem rather than guessed where that configuration cannot be executed.
---

# The adopted-project Work List is a derived reading of adaptive Review Packets

**Amends ADR-0035.**

ADR-0035 decided that human work is attributable, guided, and reversible, and that a coordinator opens Corridor to a short work list ordered by project consequence. It wrote that list against the legacy path, where a Work Item is one Extracted Proposal or Coordination Subject. An adopted-baseline project (ADR-0076, #520) generates Proposed Deltas instead, and one authoritative source revision can produce dozens of them at once. Presenting each delta as its own Work Item would make the coordinator answer the same question repeatedly, and presenting the source revision as the only unit would hide the deltas that genuinely need separate decisions.

This ADR amends **only** ADR-0035's customer-facing Work List presentation for adopted-baseline projects. Everything else in ADR-0035 — individual attribution, guided Save, the Follow-up Plan form, structured cancellation, recorded correction, Undo, the phone and offline boundaries, the interruption classes, and the escalation contact — is unchanged and still governs.

## The smallest coherent human decision is a Review Packet

A **Review Packet** is the smallest set of Proposed Deltas a coordinator can sensibly decide at once. It is a **derived presentation over Proposed Deltas**, not a new authoritative record, not a queue, and not a lifecycle with its own open and closed states. Packets are recomputed from the current set of open deltas; nothing is stored that a later reading cannot rebuild.

`Review Packet` is an internal technical name. The coordinator sees the packet's own subject in project language — the source revision, the coordination question, or the commitment it is keyed by — never the words "review packet".

### Packet keying is adaptive

A packet may be keyed by:

- **one authoritative source revision**, when a revised UCM, schedule, or permit produces many independent and compatible exact changes;
- **one cross-source coordination question**, when several sources bear on the same real question, such as one Utility Conflict whose Promised For date is contradicted by a later email; or
- **one shared commitment**, when several deltas move the same External Organization commitment.

Neither key alone is sufficient. Source-only grouping forces the coordinator to reconstruct the same Utility Conflict once per arriving document, because the sources that disagree arrive separately. Conflict-only grouping destroys the batch case, forcing dozens of separate decisions on one revision whose changes are exact, independent, and compatible. The keying rule therefore selects per packet, and the selected key is shown.

### Every open delta is actionable in exactly one packet

Each open Proposed Delta is **actionable in exactly one packet**. A delta may be *referenced* elsewhere as context — a coordination question may show a neighbouring delta to explain the disagreement — but a reference is read-only and offers no decision. Two packets never both offer to resolve the same delta, so the same work never appears twice and no decision races another.

Packet composition is deterministic for a given set of open deltas and keying-rule version, so two readings of the same state produce the same packets in the same order.

## Three visible consequence levels

The adopted-project Work List shows three levels:

1. **Must handle before this issue** — the delta changes something the next customer issue must state correctly.
2. **Affects this issue** — the delta changes reported content but does not block the issue.
3. **Can wait** — the delta changes neither.

These three are a **projection of, not a replacement for, the auditable internal consequence reasons and bands**. Every internal Attention Reason remains recorded, remains queryable, and remains visible on the item; the level is the heading the reasons are grouped under. Ordering within and across levels is deterministic and derived from the internal bands, so the same state always presents in the same order.

**Apparent removal keeps its own explicit internal consequence identity and its own explanation** even when it projects into one of the three levels. A row that vanished from a revised matrix is never collapsed into a generic "changed" — the coordinator is told that the source no longer contains the row and what that means, because a silent removal and an edited value fail in different ways.

## Four primary decisions, with Defer secondary

A packet offers four primary decisions in project language:

- **Apply the shown change** (or changes, for a multi-delta packet);
- **Keep current** — the accepted value stands;
- **Edit and apply** — subject to ADR-0084's constraint that an edit may not turn unsupported free text into a source-backed value; and
- **Needs coordination** — the coordinator cannot settle it yet, so it becomes an open Work Item with an assigned person and a Next Action.

These are the customer-facing surface of ADR-0084's `accept`, `reject`, and `edit` dispositions plus ADR-0035's **Needs clarification**. Corridor never requires a false resolution.

**Dated Defer is secondary.** ADR-0035 already names deferral with a return date, and ADR-0084 already decides that deferral is Work List scheduling rather than a semantic disposition. This ADR keeps that verb rather than introducing a second customer-facing word for the same act; **"Snooze" is rejected as a synonym** because a coordinator would otherwise have to learn two names for one behaviour, and the glossary already carries the deferral sense.

A Defer:

- is an **attributable Work List scheduling receipt** carrying the actor, time, reason, and a return date or wake condition;
- **leaves the Proposed Delta open**;
- **creates no Follow-up Plan** by itself;
- **writes no Project Record revision**; and
- returns the item to immediate work on its date or on ADR-0084's wake conditions.

**Merely leaving a packet records no decision.** Navigating away, closing the tab, or scrolling past is not an act; the packet is untouched and returns unchanged.

## One packet act, several identified decisions

A guided packet act may contain several separately identified semantic decisions committed in **one atomic Project Record revision**. ADR-0035 already requires that one Save must not collapse the identity of the distinct domain acts inside it, and that rule holds here: each child delta keeps its own disposition, its own actor attribution, and its own history entry, even though one transaction commits them (#526).

A **scheduling-only act creates no Project Record revision.** A packet in which every child was deferred writes only the scheduling receipts.

## The full record stays one step away

The complete Project Record remains searchable and reachable from any item, but it is **not the default weekly workload**. The coordinator's default reading is the derived Work List for the current reporting period. A Record view is a secondary destination, not the landing screen.

## Terminology

This decision introduces **no new customer-facing term**. It reuses Work Item, Attention Reason, Proposed Delta, Follow-up Plan, Next Action, and Defer as the glossary already defines them, so the terminology-research procedure in `docs/agents/domain.md` is not triggered. `Review Packet` is an internal technical name and is never shown to a customer; should a customer-facing label for the packet subject ever be wanted, that research runs first.

## Considered options

**One Work Item per Proposed Delta.** Rejected. One revised matrix would produce dozens of identical-looking questions, and the coordinator would answer the same conflict once per changed field.

**One Work Item per source document.** Rejected. The disagreements that actually need judgment span sources, so the coordinator would reconstruct the same Utility Conflict every time another document arrived.

**A stored packet with its own open/closed lifecycle.** Rejected. It would be a second authoritative record competing with the Proposed Delta set, it would go stale the moment a new source arrived mid-review, and it would need its own reconciliation, supersession, and cleanup machinery for no gain over recomputing.

**Replace the internal consequence bands with the three visible levels.** Rejected. The bands carry the exact reasons an auditor and a support engineer need; three headings are a reading aid, not the record.

**Introduce "Snooze" as a separate secondary action.** Rejected. It is ADR-0035's deferral under a second name, and ADR-0048 requires reusing existing Project Record vocabulary where it fits.

## Consequences

- ADR-0035's "One work list, in project language" section applies unchanged to legacy projects. For adopted-baseline projects, the packet keying, the three visible levels, and the four primary decisions in this ADR replace its item-per-record presentation; its ordering-by-consequence principle survives as the derivation behind the levels.
- ADR-0035's deferral rules are unchanged; this ADR only fixes Defer's position as secondary and confirms ADR-0084's receipt semantics.
- #494 carries the derived Work List reading, #526 the atomic packet resolution, #527 the source-revision packet, and #528 the cross-source coordination packet.
- Nothing here changes the phone, offline, notification, escalation, or generalized task-management clauses of ADR-0035.
