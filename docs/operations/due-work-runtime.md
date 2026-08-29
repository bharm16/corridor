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

Run `make due-work ARGS="supervise --owner=runtime:<worker-id>
--poll-seconds=5"` in a process separate from the web server. `tick`,
`run-once`, `recover`, and `status` expose the same stored interfaces for bounded
operations and diagnosis.

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
