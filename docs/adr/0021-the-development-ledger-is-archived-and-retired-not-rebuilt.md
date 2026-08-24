---
status: accepted
---

# The development Ledger is archived and retired, not rebuilt

ADR-0020 correctly preserved human-only Admission but chose to rebuild 141 noncompliant development Dependencies through manual Adjudication. That rebuild is superseded: the records have no production identity worth preserving, and asking a maintainer to curate them creates labor without creating independent truth. Corridor seals and verifies the complete development graph and original actor labels as one immutable, independently readable **Development Ledger Archive receipt**, then physically deletes only the corresponding active Assertions, Operative Support, Evidence Links, Dependency Events, and Dependencies. The Candidate backlog remains, and the compliant Ledger starts empty. Production Admission remains attributable to a human; automation must never relabel or re-admit these development artifacts merely to make the Ledger look populated.

## Considered options

**Manual rebuild.** Rejected because it turns a development cleanup into 141 semantic decisions whose results have no production value.

**Automated rebuild under a human identity.** Rejected because it would satisfy the API shape by falsifying attribution.

**Policy Admission of the legacy rows.** Rejected because this decision does not weaken the Ledger's human-only admission boundary, and the legacy rows are not trustworthy gold for a new automated write path.

## Consequences

The active development Ledger may be empty after retirement. Demonstrations and acceptance protocols use isolated, explicitly non-production state rather than depending on legacy records. The Development Ledger Archive receipt remains readable and hash-verifiable, while the live Ledger makes no claim those development-era decisions were human.
