---
status: accepted
domain: intake
scope: current product
supersedes:
  - ADR-0059
amends:
  - ADR-0034
  - ADR-0058
  - ADR-0062
amended_by:
  - ADR-0083
  - ADR-0089
migration: the PullConnector contract (#496) and project-bound push intake (#511) are implemented, the Box adapter runs under them, and every delivery is persisted with its disposition (#599); no inbound-mail provider is recorded and the schedule import remains the hand-typed CSV path.
---

# Sources enter through project-bound connectors under one small contract

**Supersedes ADR-0059. Amends ADR-0034, ADR-0058, and ADR-0062.**

ADR-0058 gave each project an inbound email address. ADR-0059 replaced it with one global address whose project is inferred from the message's content. Content inference makes the system decide which customer a document belongs to after it has already received the bytes; for a legal-grade coordination record that is backward, and it is also the wrong shape for the first design partners, whose evidence arrives in a shared project mailbox, a SharePoint folder, a Box location, and a scheduler's export at least as often as in a forwarded email. The intake boundary has to be the customer's project, declared before the bytes arrive, and every source kind has to enter through the same contract.

## Decision

### One connector contract

Every source connector implements:

```
list_changes(cursor)
fetch_version(external_id, version)
get_metadata(external_id)
acknowledge(cursor)
```

`list_changes` returns externally identified versions since a cursor; `fetch_version` returns exact bytes for one version; `get_metadata` returns the external system's own timestamps and identifiers; `acknowledge` advances the cursor only after the fetched version is durably stored with its digest. A connector does not read, classify, or route content.

### What every connected source carries

- customer and project binding;
- connector identity and version;
- external source identity;
- external version or change token;
- original timestamps from the external system;
- byte digest;
- synchronization cursor;
- fetch attempt and terminal outcome (stored, duplicate, failed, refused);
- idempotency identity (connector, external identity, version), so a replayed change stores nothing twice.

### Initial connector kinds

- shared project mailbox;
- project-specific inbound alias;
- SharePoint or OneDrive project folder;
- Box or another document-control location;
- manual upload;
- schedule export or scheduling-system adapter.

Email remains first-class; it is not universally primary. ADR-0058's body/attachment provenance (bodies are written statements, attachments become Documents, senders are attribution evidence) and its prohibition on unrestricted personal-mailbox crawling remain sound and unchanged.

### The project boundary, in order

1. **Explicit project-bound connector or project alias.** The connector is registered to one customer and one project before it fetches anything. This is the primary boundary.
2. **Signed intake token or registered project identifier** carried by the message or file (an alias token, a registered CSJ, contract, or document-control identifier).
3. **Exact document, contract, CSJ, conflict, or thread identity** matched against the project's registries (ADR-0059's tiers, now a fallback inside an already-bound customer, never across customers).
4. **Human triage** for anything the first three leave ambiguous, as one visible card.

**Content inference is never the primary boundary between customer projects.** A message that reaches Corridor without a customer binding from tier 1 or 2 is held unrouted and visible; it is not matched across customers on content.

### Schedule connectors

Schedule connectors may consume CSV or XLSX key-date tables, Primavera P6 XER or XML exports, Microsoft Project exports, and PMIS APIs or reports. They ingest the relevant activities and dates as captured source facts (ADR-0076); a changed date becomes a `schedule date changed` delta. They do not perform CPM calculation and do not become the scheduling system (ADR-0057's self-identifying governing dates and versioned history are unchanged).

## Amendments

- **ADR-0034.** Connected ingestion under managed operations is implemented as registered connectors under this contract; the service work of registering a connector is customer-relationship work, and the customer never sees the contract.
- **ADR-0058.** "Each project gets an inbound address" is restated: each project has a bound connector, of which a project alias is one kind. The one adoption habit (CC or forward) is preserved where the customer chooses that connector.
- **ADR-0062.** Thread identity binds inside the project the connector is bound to. Header-chained threads never cross a customer boundary.

## Considered options

**Keep one global address with content routing (ADR-0059).** Rejected. Cross-customer inference on content is the failure mode this ADR exists to prevent, and it does not generalize to folders or exports, which have no message to infer from.

**A different contract per source kind.** Rejected. Four operations cover every kind the first design partners use; a per-kind interface would multiply the sync, retry, and idempotency code by the number of kinds.

## Consequences

- ADR-0059 becomes `superseded by ADR-0078`.
- Only the connectors the first design partners require are built. A connector kind listed above without a design partner behind it stays unbuilt.
- Unrouted intake is a monitored operational metric (see the operations runbook), not a silent drop.
