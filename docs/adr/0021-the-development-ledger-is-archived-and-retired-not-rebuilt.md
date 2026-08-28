---
status: accepted
---

# The development Ledger is archived and retired, not rebuilt

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

ADR-0020 preserved the then-current human-only Record Inclusion rule but chose to rebuild 141 noncompliant development Constraints through manual Human Record Decision. That rebuild is superseded: the records have no production identity worth preserving, and asking a maintainer to curate them creates labor without creating independent truth. Corridor seals and verifies the complete development graph and original actor labels as one immutable, independently readable **Development Ledger Archive receipt**, then physically deletes only the corresponding active Assertions, Supporting Documentation in Use, Evidence Links, Dependency Events, and Dependencies, using those historical entity names. The Extracted Proposal backlog remains, and the compliant Ledger starts empty. At the time of this decision, production Record Inclusion required a human. The continuing prohibition is that automation must never relabel or re-admit these development artifacts merely to make the Ledger look populated.

## Considered options

**Manual rebuild.** Rejected because it turns a development cleanup into 141 semantic decisions whose results have no production value.

**Automated rebuild under a human identity.** Rejected because it would satisfy the API shape by falsifying attribution.

**Policy Record Inclusion of the legacy rows.** Rejected because this decision did not weaken the then-current human-only Record Inclusion boundary, and the legacy rows were not trustworthy gold for a new automated write path. ADR-0029's later exact policy authority does not change the archive decision.

## Consequences

The active development Ledger may be empty after retirement. Demonstrations and acceptance protocols use isolated, explicitly non-production state rather than depending on legacy records. The Development Ledger Archive receipt remains readable and hash-verifiable, while the live Ledger makes no claim those development-era decisions were human.
