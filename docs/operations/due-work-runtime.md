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
