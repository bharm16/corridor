---
status: accepted
domain: retention
scope: current product
supersedes:
  - ADR-0072
amends:
  - ADR-0032
amended_by:
  - ADR-0083
migration: no retention schedule, legal-hold registry, disposition approval, dry-run manifest, or deletion receipt exists; Class A data has no disposition path.
---

# Governed records disposition replaces the missing delete path

**Supersedes ADR-0072. Amends ADR-0032.**

ADR-0072 established four retention classes and decided that Class A, the Project Record, has no delete code path. Its reasoning about event-triggered legal clocks, Class B intermediaries, and cross-class legal holds is sound and is kept. Its conclusion is not: **a missing delete path is not a retention control. It is an inability to satisfy future customer policy.** A consultant's contract, an agency's certified retention schedule, or a records-disposition order will one day require that a customer's record be exported and removed after its retention obligation ends, and ADR-0072 itself acknowledges that a future policy could require a hold-aware disposition feature. That feature is designed now, before commercial records accumulate, rather than retrofitted under a deadline.

## Decision

Two separate rules are preserved and must not be confused:

1. **Ordinary product operations never destructively edit record history.** No product screen, command, policy, or connector deletes or rewrites a Class A row. ADR-0032's posture is unchanged.
2. **Governed records disposition may remove data after its retention obligation ends.** Disposition is an operations act under a recorded policy, attributable, receipted, and suspended by any hold.

### What disposition requires

- **Retention schedules** declared by customer, project, contract, record class, and triggering event (project acceptance, agreement close, final payment, contract termination). No schedule runs on "N years after ingest."
- **Legal holds** that suspend every deletion path: the Class B TTL job, cascade deletes, storage-backend `delete_under_policy` (ADR-0079), and disposition itself. A hold records scope, actor, trigger (litigation, claim, audit, open-records request), and time.
- **Export or transfer requirements** satisfied before disposition: the customer's record, sources, decisions, and released artifacts are delivered in the agreed format and the delivery is receipted.
- **Attributable disposition approval** by a named person with authority under the customer relationship.
- **Dry-run manifest** listing every digest, row, and artifact the disposition would remove, reviewed before execution.
- **Deletion receipt** containing the policy, the scope, the actor, the date, and the digests removed. The receipt is itself retained.
- **Independent retention** for raw sources, record decisions, released artifacts, and intermediary processing data, each with its own schedule. Disposing of raw sources does not dispose of the decisions that cited them, and a released report survives the disposition of the working data behind it unless its own schedule has ended.

### Classes restated

Class B intermediaries may still expire automatically under ADR-0072's labeled product-policy TTLs, with the dry-run manifest and reachability check. Class C rebuildable indexes carry no promise. Class D cost records remain declared and empty. **Class A data is not "permanent"; it is retained until the applicable policy authorizes disposition.**

## Amendments

- **ADR-0032.** "Nothing is deleted" is restated as rule 1 above: nothing is deleted by product operations. Governed disposition is not a product operation.

## Considered options

**Keep "no delete path" and handle disposition by hand when asked.** Rejected. Hand deletion in a database with 149 tables and content-addressed storage cannot produce a trustworthy receipt or honor a hold, and the first request will arrive with a deadline.

**Per-row TTL on Class A.** Rejected, as in ADR-0072. Retention is event-triggered and policy-scoped; a TTL column is the wrong shape.

## Consequences

- ADR-0072 becomes `superseded by ADR-0080`.
- The disposition feature is designed and built before the first commercial record is adopted (ADR-0076 Adopt Baseline), so every commercial record has a schedule from day one.
- Storage-backend deletion is only ever `delete_under_policy`, and a hold check precedes it.
