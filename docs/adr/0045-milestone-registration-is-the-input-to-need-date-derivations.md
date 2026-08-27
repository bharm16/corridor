---
status: accepted
---

# Required By dates use exact Milestone Revisions

> **Terminology amended 2026-08-27 by [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses the adopted construction terms. Historical quotations and implementation identifiers retain their original spelling; the authority boundaries are unchanged.

Every **Milestone Revision** is immutable and preserves its exact source row,
source identity, and source digest. A Required By date and every Milestone Report
value are Derivations that name the exact Milestone Revision and affected
Constraints. A revision is not necessarily an approved schedule baseline. This
provenance rule does not decide when an earlier Effect on Milestone decision must
be reviewed after a schedule change.
