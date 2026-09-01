# Observability and operations runbook

The production design in
[ADR-0079](../adr/0079-one-application-one-worker-managed-postgres-and-one-database-per-customer.md)
requires structured logs, correlation identifiers, the metrics below, alerting,
and operator runbooks. This document holds the operational detail so that an
operational counter never needs an ADR. Change thresholds and contacts here;
change what is measured only with the ADR.

Status on 2026-09-01: none of this is instrumented. No structured logging,
application container, staging or production environment, inbound-mail
provider, or outbound email adapter exists. Every metric name below is the
name the implementation must use so dashboards and alerts can be written once.

## Correlation

Every log line carries `environment`, `customer`, `project`, `request_id` (web)
or `job_id` (worker), and the ADR-0079 run provenance when inside a run:
`run_id`, `purpose`, `authoritative`.

## Metrics

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `corridor_source_sync_failures_total` | counter | connector, customer, project, outcome | `list_changes`/`fetch_version` attempts with a failed or refused terminal outcome (ADR-0078). |
| `corridor_intake_unrouted` | gauge | customer | Items held without a project binding awaiting human triage (ADR-0078 tier 4). |
| `corridor_extraction_latency_seconds` | histogram | stage, source_kind | Wall time per document from stored bytes to captured facts. |
| `corridor_ocr_latency_seconds` | histogram | provider | Wall time per page for OCR. |
| `corridor_processing_backlog` | gauge | queue | Jobs waiting in the worker queue. |
| `corridor_model_cost_usd_total` | counter | provider, model, purpose | LLM and OCR spend. |
| `corridor_policy_abstentions_total` | counter | policy, reason | Released-policy abstentions by reason code. |
| `corridor_policy_failures_total` | counter | policy | Policy runs that ended in a Processing Failure. |
| `corridor_delta_outcomes_total` | counter | delta_type, outcome | Resolve Delta outcomes: accepted, edited, rejected, deferred (ADR-0076). |
| `corridor_delta_open_age_seconds` | histogram | delta_type | Age of unresolved proposed deltas at resolution. |
| `corridor_false_write_total` | counter | policy, field | Automatic projections later reversed by a human (ADR-0075 false-write rate). |
| `corridor_report_release_failures_total` | counter | customer, project, reason | Report generation or release that did not seal an artifact (ADR-0040). |
| `corridor_disposition_holds_active` | gauge | customer | Active legal holds (ADR-0080). |

## Dashboards

- **Pilot health** (per customer): operator minutes per active project per
  week, source arrival to accepted update, accept-without-edit rate, false-write
  rate, open deltas by band. These are the ADR-0075 measures and the
  [pilot success criteria](../pilot-success-criteria.md) inputs.
- **Pipeline**: backlog, extraction and OCR latency, sync failures, cost.
- **Policy**: abstentions and failures by policy and reason; false writes.
- **Release**: report release failures; holds.

## Alerts

| Alert | Condition | Severity | Runbook |
|---|---|---|---|
| Source sync failing | `corridor_source_sync_failures_total` for one connector increases for 3 consecutive sync intervals | page | [Sync failure](#sync-failure) |
| Unrouted intake aging | any `corridor_intake_unrouted` item older than 1 business day | notify | [Unrouted intake](#unrouted-intake) |
| Backlog stalled | `corridor_processing_backlog` > 0 and no job completion for 30 minutes | page | [Backlog stalled](#backlog-stalled) |
| Policy failure burst | `corridor_policy_failures_total` > 5 in 1 hour for one policy | notify | [Policy failure](#policy-failure) |
| False write | any increase in `corridor_false_write_total` | notify | [False write](#false-write) |
| Report release failed | any increase in `corridor_report_release_failures_total` | page | [Release failure](#release-failure) |
| Cost spike | `corridor_model_cost_usd_total` daily rate > 3× trailing 7-day average | notify | [Cost spike](#cost-spike) |
| Deletion attempted under hold | any `delete_under_policy` refusal logged with an active hold | page | [Hold refusal](#hold-refusal) |

Thresholds are initial values; adjust here with the date and reason.

## Escalation

| Role | Contact | When |
|---|---|---|
| Operator on call | Bryce Harmon | every page |
| Customer contact | per customer, recorded at connector registration | any alert that delays a customer's weekly artifact |

## Runbooks

### Sync failure
1. Read the connector's last terminal outcomes (`fetch attempt and terminal outcome`, ADR-0078).
2. Credential or permission failure: re-register the connector with the customer contact; never widen scope to a personal mailbox (ADR-0058).
3. Transient provider error: the cursor has not advanced; the next sync retries. Do not acknowledge by hand.
4. Persistent: hold the connector, notify the customer contact, and record the gap so the next weekly artifact states which sources were not read.

### Unrouted intake
1. Open the triage card. Bind to a project only on tier 2 or 3 evidence (ADR-0078); never on similarity of names.
2. Nothing binds: leave it held and tell the customer contact. Do not discard.

### Backlog stalled
1. Check the worker container is running and can reach PostgreSQL and object storage.
2. Inspect the oldest job's last log line by `job_id`.
3. A poisoned job: mark it failed with the reason; it becomes a Processing Failure, visible in the product. Never delete it.

### Policy failure
1. Group failures by reason code. A new reason after a deploy is a regression: roll back the policy version (ADR-0050 re-proof still holds for the prior version).
2. A source-quality reason: leave the abstentions; they are the product's honest answer.

### False write
1. Identify the policy and field. Compare against its released false-write limit (ADR-0076).
2. Over the limit: disable the policy's automatic projection for that field; captured facts continue; the field returns to human delta decisions.
3. Record the reversal in the pilot health dashboard.

### Release failure
1. Nothing was sealed; the last released report is still the baseline (ADR-0053).
2. Regenerate after the cause is fixed. Never patch a sealed artifact.

### Cost spike
1. Attribute by `purpose`. An experiment in a non-production database is fine; production spend that tripled means a connector loop or a re-extraction storm. Check `list_changes` cursor advancement.

### Hold refusal
1. This is the system working. Confirm the hold scope with the customer contact and leave the data in place (ADR-0080).
