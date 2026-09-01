---
status: accepted
domain: operations
scope: current product
supersedes:
  - ADR-0049
amends:
  - ADR-0034
  - ADR-0050
  - ADR-0068
  - ADR-0073
migration: no application container, worker container, staging or production environment, object-storage backend, structured logging, or alerting exists yet; runs do not record environment, customer, purpose, or authoritative status.
---

# One application, one worker, managed PostgreSQL, object storage, and one database per customer

**Supersedes ADR-0049. Amends ADR-0034, ADR-0050, ADR-0068, and ADR-0073.**

Corridor has no defined deployment topology. There is no application container, no staging or production environment, no structured logging, no recorded inbound-mail provider, and no real outbound email adapter. ADR-0049 separated production from experimental runs by database location and then concluded that every run already in the production database is production "by the same rule." Location is a sound enforcement boundary; it is not historical proof about rows that predate the rule. Before a paying customer's record lives in this system, the topology, the isolation boundary, run provenance, storage backend, and observability have to be decided together.

## Decision

### Topology

```
One web application container
One background worker container
One migration job
Managed PostgreSQL
Object storage
One staging environment
One production environment
Backups and point-in-time recovery
```

Do not add Kubernetes, microservices, a graph database, or a separate workflow platform before a measured need is recorded.

### Customer isolation

For initial commercial deployments: **one database and one object-storage namespace per customer, with multiple projects inside them.** This is safer than retrofitting shared-row tenancy through 149 tables, and it makes the ADR-0078 project boundary a database boundary as well. Shared-row tenancy is not planned; it would need its own ADR with a measured cost that justifies it.

### Run provenance

Every run (extraction, policy, connector sync, report generation, migration) explicitly records:

- environment (development, staging, production);
- customer;
- project;
- purpose (production, experiment, replay, measurement, rehearsal);
- source revision;
- code and policy version;
- authoritative versus shadow status;
- initiating actor or scheduled operation.

**Location remains an enforcement boundary, but it is not historical proof.** ADR-0049's claim that an old run is production merely because it resides in the production database is retired. Runs that predate this ADR carry `purpose: unrecorded` until a named person records otherwise; nothing about them is inferred. ADR-0049's rule that experiment commands must name an explicit non-default database and refuse the production one is kept.

### Storage backend

The content-addressed design (ADR-0068 segments, ADR-0073 token-layer manifests) is unchanged conceptually. Its backend becomes an interface:

```
put(bytes) -> digest
open(digest)
exists(digest)
delete_under_policy(digest)
```

The local filesystem is the development implementation; object storage is the deployed implementation. `delete_under_policy` is the only removal path and is governed by ADR-0080.

### Observability

The production design includes:

- structured logs;
- request and job correlation identifiers;
- source-sync failures (ADR-0078);
- extraction and OCR latency;
- processing backlog;
- LLM and OCR cost;
- policy abstentions and failures;
- proposal outcomes (ADR-0076 delta accept, edit, reject, defer);
- report-release failures;
- alerting and operator runbooks.

Metric names, dashboards, alert thresholds, escalation contacts, and runbooks live in [docs/operations/observability-runbook.md](../operations/observability-runbook.md). An operational counter does not need an ADR.

## Amendments

- **ADR-0034.** "Corridor needs operations alerting and a runbook" is satisfied by the observability list above and the runbook document. Managed operations run in the topology above.
- **ADR-0050.** A policy's recorded history carries the run provenance fields; a re-proof replays only runs whose purpose is production and whose status is authoritative.
- **ADR-0068 and ADR-0073.** Segment text and token-layer artifacts are stored through the storage interface; the PostgreSQL manifest keeps the digest and the backend resolves it.

## Considered options

**Shared-row tenancy from the start.** Rejected. Every one of 149 tables would need a customer column, every query a filter, and every test a cross-tenant leak check, before the first customer exists.

**Keep ADR-0049's location-only rule.** Rejected for the historical claim only. Location as enforcement is kept.

**Kubernetes or a workflow engine now.** Rejected. One worker and one queue table cover the current load; the operational cost of the platform exceeds the product.

## Consequences

- ADR-0049 becomes `superseded by ADR-0079`.
- Implementation: containerize the application and worker, add the storage interface and an object-storage backend, provision staging and production with backups and point-in-time recovery, add provider adapters for inbound and outbound mail, add structured logging and the alerts in the runbook.
- The migration job is the only writer of schema changes in any deployed environment.
