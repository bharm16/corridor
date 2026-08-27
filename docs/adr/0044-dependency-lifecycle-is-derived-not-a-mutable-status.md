---
status: accepted
---

# Constraint lifecycle is derived, not a mutable status

> **Terminology amended 2026-08-27 by [ADR-0047](0047-domain-language-follows-researched-construction-practice.md).** Current prose uses the adopted construction terms. Historical quotations and implementation identifiers retain their original spelling; the authority boundaries are unchanged.

A Constraint has no mutable lifecycle status. Documentation Review and its current
support, Dismissal, Completion Reported for an exact Commitment, Exceptions, and the
current Coordination Plan state separate supported facts. The old
`identified | in_progress | committed | blocked | closed` value had no authority and
could suppress or publish facts without Supporting Documentation or a Work
Decision, so Corridor preserves it only as retired legacy history.

The product names the supported outcome for the stated scope. It does not replace
the earlier Ready label with a generic Complete or Cleared status. A report that
one Commitment is complete neither meets every linked Constraint's documentation
requirement nor completes the project's Next Action.
