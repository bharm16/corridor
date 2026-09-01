---
status: accepted
domain: intake
scope: current product
amended_by:
  - ADR-0078
---

# A conversation produces one claim, or one task

An email thread can argue with itself: "it's a 12" — "no it's a 16" — "actually maybe a 20" — "no you're right, it has to be 16." Recording each turn as an independent claim would manufacture four conflicting source values from one conversation's drafts. Decided 2026-08-28 (night): the thread is the unit, not the message.

## Decision

- **Thread identity is deterministic.** Email headers (Message-ID, In-Reply-To, References) chain replies into threads — the same data every mail client threads by. No content guessing.
- **A thread binds to its subject once, by content.** The first message that resolves — a cited conflict ID, identifying language, a reply to a recorded ask — binds the thread to that row; replies inherit the binding through the headers. A broken chain is simply a new thread that binds on its own content. Threads are never merged or reconstructed.
- **Turns are not claims.** Within a thread, mentions of a value are drafts of each other. The bounded reader (the same caged harness, quote-verified per turn, speakers from senders) reads the arc — assertion, contradiction, speculation, concession — and finds the conversation's own verdict in the participants' closing words.
- **A concluded conversation produces one claim**: the outcome, sourced to the whole exchange with the closing turn as its citation and the earlier turns retained as context.
- **An unresolved conversation produces zero claims and one open question**: "size under discussion — thread ended on 'maybe a 20' — pin it down," a task in the thread's own words.
- **The row is the junction.** All threads and documents about one Constraint meet at its row; the timeline read for any disputed fact (ADR-0061) is row-scoped and spans threads, so a question left open in one thread and closed in another resolves visibly at the row.

## Consequences

Ticket #372 (email intake) carries thread storage, binding inheritance, and the conversation-outcome rule. ADR-0061's timeline packet consumes the per-turn record. The trust boundary is unchanged: turns are quoted data; nothing in a conversation instructs the system; claims still enter the record only through the ordinary admission rules.
