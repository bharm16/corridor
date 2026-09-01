---
status: superseded by ADR-0078
domain: intake
scope: historical
amends:
  - ADR-0058
---

# One intake address; the project resolves from the message

> **Superseded by [ADR-0078](0078-sources-enter-through-project-bound-connectors-under-one-small-contract.md), 2026-09-01.** Project-bound connectors and aliases are primary; content-based routing survives only as a fallback inside an already-bound customer.

ADR-0058 gave each project its own inbound address. Reviewed the same evening and corrected: addresses don't scale as identity (a thousand projects is not a thousand mailboxes), humans misdirect mail, and two projects with similar names must never collide. The fix is the same move made everywhere else in this system: the content already carries the answer — match on it.

## Decision

**There is one intake address. The router resolves the project from the message's own data, in tiers:**

1. **Document identity — the strongest signal.** An attachment that matches a registered document (same registry identity, a revision of known bytes, a known document family) belongs to that document's project. A letter citing a conflict ID registered to exactly one project belongs to it.
2. **Registered project identifiers.** Every TxDOT job prints its CSJ number on its documents; contracts carry contract numbers. These are registered identifiers — matched exactly, never by name.
3. **Conflict, utility, and station identifiers** in the body, resolved against the registries.
4. **The sender and the thread.** A sender registered as a contact on specific projects narrows the set; once one message in a thread routes, replies follow it by thread headers, deterministically.

**Exactly one project survives → routed automatically, by exact rule, with the deciding evidence recorded.** A tie or nothing → one triage card ("which project?"), answered with one click, and the thread remembers forever.

**Names are never routing keys.** Two projects with similar names cannot collide because routing never matches on names — identifiers and document identity only, with ambiguity waiting visibly.

**The per-project suffix (`intake+slug@`) may exist as an optional hint** — one mailbox, free aliases — but routing must work on the bare address, because the moment routing depends on people picking the right suffix, misdirected mail breaks it.

## Consequences

Amends ADR-0058's per-project-address clause; everything else in ADR-0058 stands (deliberate disclosure, bodies as written statements, senders as attribution evidence, no personal-mailbox crawling). Ticket #372 gains the routing tiers and the triage-card residue in scope.
