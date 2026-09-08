# Supervised Due Work runtime

Corridor runs one supervised process for scheduled operational work. Feature
modules register bounded handlers in the server-owned allowlist; they do not
start schedulers, polling threads, retry loops, imports, commands, or arbitrary
destinations from persisted job data.

The first handler is `processing_health`. It reads stored Document and
Extraction Run facts for one project. It has zero model-token and notification
budget, does not change the Project Record, and reports either:

- a healthy execution that observed healthy processing; or
- a healthy execution that observed failed document processing, with the safe
  next step `inspect_failed_document_processing`.

Configure every gate-7 field with `make due-work ARGS="configure-health ..."`
using the exact Makefile example. Missing or unsupported cadence, timezone,
missed-run, retention, retry, lease, deadline, concurrency, model, notification,
scope, or version data is refused before the job is enabled.

Release preparation (#690) is declared per project and runs on request. Run
this through the worker capability with the actual synthetic project slug and
an hour-aligned UTC start time before exercising the deployed preparation path:

```bash
make due-work ARGS="configure-release-preparation <project-slug> --configuration-version=release-preparation-v1 --starts-at=<UTC-hour> --cadence=on_request --timezone=UTC --missed-run-policy=every_occurrence --retention-days=365 --max-attempts=3 --backoff-seconds=60 --claim-ttl-seconds=900 --deadline-seconds=600 --concurrency-limit=1 --model-token-budget=0 --notification-budget=0"
```

The request, attempt, and terminal receipt prove execution. A configured
declaration or a healthy idle supervisor alone does not.

Run `make due-work ARGS="supervise --poll-seconds=5"` in a process separate
from the web server. Each invocation generates a distinct runtime owner;
`--owner=runtime:<worker-id>` remains available for an explicitly named
operator process. Never give two live processes the same owner. `tick`,
`run-once`, `recover`, and `status` expose the same stored interfaces for bounded
operations and diagnosis.

`make due-work ARGS=health` checks the worker database, object storage, and
the existing durable heartbeat reading, and exits nonzero when degraded. It
does not add heartbeat rows or customer decisions. The ECS worker service
invokes the same command through the image entrypoint, because ECS health
checks receive the task definition's environment rather than the URL PID 1
composed. Its 120-second shutdown allowance lets the supervisor stop claiming
work; any unfinished lease follows the existing recovery contract.

Connected TxDOT document discovery uses `txdot-rid-box-v1`. Its declaration
names the official RID page and one exact visible link such as `Utilities`.
The adapter follows that link to the public Box shared-file page, binds the Box
file identity stated there, and reads the ZIP directory and authorized members
with bounded byte ranges. A changed Box share therefore becomes a new observed
source without scraping arbitrary pages or treating a filename as a Document
kind. New references remain proposed intake until Corridor Operations records
the existing attributable authorization.

Each hourly UTC `latest_only` occurrence has a stable identity. Claims have a
bounded lease and deadline. A crashed worker's attempt is retained before a new
owner retries it; a stale owner cannot finalize after recovery. Backoff and
attempt ceilings end in a retained Processing Failure. Occurrence deduplication
does not promise exactly-once external effects: any future effectful handler
must declare and implement its own idempotency and reconciliation contract.

Shutdown stops taking new work. Existing claims remain recoverable after their
lease expires. Terminal receipts and referenced history are retained for at
least the configured retention period; this runtime performs no silent history
deletion.
