---
status: accepted
domain: project-record
scope: current product
---

# Required By dates use exact Key Date Versions

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Every **Key Date Version** is immutable and preserves its exact source row,
source identity, and source digest. It is one captured version of a named schedule
event and date, not an entire schedule revision. It may be the first captured
version. The event remains a Milestone in technical schedule terminology and is
shown under **Key dates**, with the event name separate from its scheduled date.

A Required By date and every value in **Constraints by key date** are Derivations
that name the exact Key Date Version and affected Constraints. That source version
is not necessarily an approved schedule baseline. A scheduled date does not prove
that the event occurred. This provenance rule does not decide when an earlier
Effect on Key Dates decision must be reviewed after a schedule change, and it
does not authorize silent relinking to a new schedule version. The retained
`MilestoneRegistration` identifier continues to identify the stored source input.
