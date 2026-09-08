# Product and pilot measurement

`make pilot-measurement` implements #532. It reads the versioned #558
interaction log and the built workflow's immutable receipts and writes a
reproducible JSON report plus its exact input snapshot. It creates no domain
state, decision, authorization, analytics database or infrastructure metric.
The customer findings and economics remain #424/#498 work. A fixture is
software proof; it supplies no customer outcome.

## Reproduce the fixture

```bash
make pilot-measurement ARGS="--input tests/fixtures/pilot-measurement.json --output out/pilot-measurement.json"
make pilot-measurement ARGS="--input out/pilot-measurement.inputs.json --output out/pilot-measurement-replayed.json"
```

The reports reproduce byte for byte. The input fixture contains 72 versioned,
synthetic events and three predeclared project-weeks. Its first week has one
surfaced interrupting packet, ten child diagnostic entries, mixed child
outcomes, a confirmed sampling miss outside those ten children, a failed
preparation followed by unchanged-declaration reuse, and one authorized
candidate containing **only the configured updated UCM and weekly report**.
It records $0.30 of actual provider cost separately from $99.00 of historical
experiment cost. Unrecorded reconstruction time is unavailable. The second
week is quiet and proves zero project clicks from a portfolio presentation and
complete declared interaction observation; the third has 12 captured source
arrivals and falls in the predeclared burst stratum. Every report explicitly
leaves customer findings as `insufficient_evidence`.

The tests also exercise the actual project Work page with ten Proposed
Deltas. That page emits one packet presentation and ten child identities
before the review screen is opened. Explicit external source links pass
through a project-authorized redirect that resolves the link from the existing
packet reading. No URL supplied by a caller is accepted, and inline source
presentation does not become an evidence click.

## Inputs required before a measured week (#535)

Copy the fixture's input shape, replacing all synthetic values with the
agreed measurement declaration. Each `MeasurementPeriod` names one customer,
one partner, one local project ID, and one half-open UTC project-week window
`[start, end)`. A material change splits that week into separate periods;
periods for the same customer/project may not overlap. Volume boundaries and
the declaration instant must precede the window. `quiet_max` and
`ordinary_max` count captured source arrivals, never coordinator activity.
Record the actual capture latency window, populated material-field codes,
required artifact membership, and the artifacts the partner previously
performed. No savings are credited for newly introduced work.

Customer/project IDs are not globally interchangeable. The mandatory origin
is the actual database's immutable #656 customer/environment/deployment
attestation, read with ordinary `SELECT`. The report's `environment` is that
customer environment ID, and `database_identity` is the stable digest returned
by `observed_database_identity(session)`. Physical host addresses and database
login credentials are not identities in an analytical report. Joins and event
deduplication use this origin plus local project ID. Unknown origins, a
different customer on the same origin, or an export connected to another
origin are refused. A blank historical binding is not relabeled as today's
customer. `WorkerSession` opens through the registered customer route; no
schema-owner or monitor privilege is granted to analytics.

Set `CORRIDOR_ANALYTICS_BINDING_FILE` to a JSON serialization of the #558
`AnalyticsBinding` for the measured deployment. It contains the actual code
and product revision, packetizer version, source and connector configuration
identities/versions, `identity:version` template and mapping identities,
enabled flags, and issue-profile identity/version/digest. Include its customer
origin. Put configuration **identities** in this file, never passwords,
credentials or customer content. The running image's `CORRIDOR_CODE_REVISION`
overrides a stale code revision in that file. Runtime presentations, packet
decisions and preparation bind the actual routed database attestation over
the file's customer origin. Retained candidate/profile/configuration values
take precedence when reading receipts. Unknown code/configuration values
remain explicit `cohort_problems`; differing recorded material cohorts require
separate periods instead of a blended result.

Enable governed structured product-log collection for the whole declared
window before setting `interaction_capture_complete`. Verify the connected
source-event census before setting `source_population_complete`. These flags
are evidence assertions made at period close, not permission to infer a zero
from a missing stream. A missing census leaves volume and coverage unavailable;
a missing interaction observation leaves a zero-click result unavailable.
Human review minutes never come from elapsed browser sessions.

## Collect and export

`measurement_collection.collect_observation(event)` validates explicit
`work_observation`, `artifact_repair`, `measurement_sample`, and
`provider_usage` inputs and emits them through the same #558 sink. Retain the
original observed time, event identity and evidence reference. Human entries
also identify the actor. Time may be a measured nonnegative number, including
an explicit zero, or `None` with an unavailable reason. Setup, triage,
connector maintenance, support and failure recovery remain separate categories
from coordinator review, record maintenance and preparation. Repair and
reconstruction can name their packet or candidate. Sampling keeps packet
usefulness, child usefulness, material changes, baseline fields and confirmed
policy false writes separate; a case cannot be retrospectively relabeled.

Provider entries identify purpose, project-week, source class, provider/model,
prompt/policy versions, exact usage receipt and actual billed USD amount.
`actual_call`, `retry`, `cache_reuse` and `historical_experiment` are separate
modes. A replay of a stored answer does not become another actual call. Sub-cent
costs are retained exactly. An extraction run with token counters but no
reconciled provider receipt remains visible as unclassified/unpriced usage;
token totals do not prove price, retries, cache origin or production purpose.

Export after the declared observation period has closed:

```bash
make pilot-measurement ARGS="--input /governed/periods.json --events /governed/product-events.jsonl --database-receipts --output /governed/project-week.json"
```

`--events` accepts native #558 JSONL and the application's structured log
envelope. Operational log lines are not product events. Domain receipts cover
source delivery/capture, Proposed Deltas, decisions, deferrals, supersession,
packet Saves, Follow-up Plans, coverage confirmations, preparation requests
and attempts, candidates and authorized packages. Logs show attempted or
refused interactions; only retained successful domain receipts establish a
committed act. The exporter never runs extraction or a provider call.

Preparation starts are recorded explicitly. The worker samples its supplied
completion clock **after** rendering/attachment; `ReleaseCandidate.prepared_at`
is the frozen input instant and cannot stand in for completion. Request-to-
candidate latency uses the completed attempt, and candidate availability uses
its retained registration time. Latencies use only matching source, delta,
decision and release identities; a missing or reversed instant is unavailable.

## Read the result and protect the data

Each period retains every surfaced packet through close, including never
opened, deferred, superseded and unresolved packets. Repeat presentations do
not increase the packet denominator. The exact child identities and separate
outcomes survive beside it; sampling misses do not increase that child count.
Release readiness uses the project's configured artifact membership, artifact
digests, common accepted revision and candidate/package identity. An absence
of repair events alone does not prove client readiness: an attributable
readiness observation and authorization are required.

The report is structured customer analytical data under the named customer
access and retention policies, including legal holds/disposition requirements
of ADR-0080. Keep real inputs, logs, reports and snapshots inside the existing
governed customer storage/access boundary, outside the repository. Output
files are written atomically with owner-only permissions (`0600`); their
snapshot and input digest make the calculation reproducible. This command
does not establish a retention schedule or authorize copying records out of
that boundary. Customer, project, principal, packet, source and candidate IDs
remain structured payload fields; the existing metric-label guard rejects
customer/project identities as infrastructure labels.

An implementation fixture, missing inputs, or unpinned historic records cannot
close a real pilot or mark a customer-savings criterion passed. Those outcomes
remain recorded through #424, #498 and #499.
