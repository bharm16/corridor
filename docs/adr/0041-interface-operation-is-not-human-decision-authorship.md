---
status: accepted
---

# Interface operation is not human decision authorship

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> **Clarified by ADR-0042.** This boundary forbids agent impersonation. It does
> not forbid a named deterministic policy from writing an enumerated exact class
> under its own system identity and immutable receipt.

A software agent may navigate Corridor and enter the exact Human Record Decision or Coordination Decision that a named human explicitly chooses, but operating the interface does not make the agent that human and does not authorize the agent to originate a human-gated Ledger write under the human's identity. An agent-only exercise must stop at the consequential write boundary or remain a visibly non-authoritative test fixture; inventing a human principal to gain unattended coverage is rejected because it would make the provenance receipt false. This trades unattended end-to-end testing for truthful decision authority and preserves the distinction between automation assistance and the human act recorded by ADR-0025, ADR-0029, and ADR-0039.

## Consequences

Supervised computer-use testing pauses before every Human Record Decision and Coordination Decision so the named human can approve or revise the exact choice. The software agent may then act as the human's input instrument, while the test records each approval and any additional coaching separately. Agent navigation and timing can support internal UI evidence, but they cannot satisfy a human-only rehearsal or practitioner-validation claim.
