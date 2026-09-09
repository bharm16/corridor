# Microsoft 365 recorded-response adapter

The #497 offline build implements the four-method PullConnector contract for
one explicitly configured SharePoint/OneDrive library or one shared-mailbox
folder. It has no credential discovery or network implementation. Its registered
offline factory refuses automatic execution. Personal mailboxes are refused. The replay command accepts
only synthetic projects and recorded transports.

```bash
make m365-replay ARGS="tests/fixtures/m365-mail-recording.json --project-id 1 --customer fixture --run-identity graph-example-1"
```

Use an existing synthetic project's ID. Output is printed only after the
database transaction commits. A disabled connector configuration owns the
normal append-only checkpoint history, including the covered delivery IDs.
The next recorded round starts at that durable checkpoint; optional `--cursor`
must agree with it. A failed transaction records and prints no advance.
The normal Source Delivery ledger retains each outcome;
retrying the same recording converges on the same delivery and source records.

The recording contains `location`, a `pages` map keyed by exact Graph request
URL, and `versions`, each with native `item_id`, `version_id`, `body_base64`,
and `sha256`. Library records also require a `doc_types` map from native file ID
to the configured source type. Optional `attachment_doc_types` maps native
message IDs to a per-message map of MIME part digests and configured source
types. Unknown messages or absent part digests refuse. MIME is the exact recorded response
to the message's `$value` request, never reconstructed from Graph JSON.

Each item retains its tenant/resource/item identity, native eTag or changeKey,
original service timestamps, and metadata. Temporary download URLs are omitted.
Library bytes enter `ingest_document`; mailbox bytes enter the existing bound
MIME parser and ordinary attachment intake. Headers cannot change the bound
customer/project. Source delivery versions remain separately queryable even
when identical bytes converge on one Document. No source revision implies an
accepted-record change or automatic supersession.

Pagination consumes at most 100 pages and 10,000 distinct items. Cursors must
remain on the exact configured Graph collection. Missing exact versions,
provider errors/resync requests, malformed responses, drafts, remote items, and
deletion markers refuse the round without advancing. Folder containers are not
source documents. Deletion handling needs an explicit retained removal-event
contract before this adapter can operate unattended over a live collection;
never erase retained customer sources in response to Graph deletion markers.

## Live configuration still required

The selected partner, tenant, library, shared mailbox, ingress authorization,
source mappings, deletion handling, and exact-version transport must be recorded
before live activation. A live transport must fetch the requested native version
or refuse when it can no longer be proved; it may never return the latest bytes
under an older eTag/changeKey. Request Outlook immutable IDs throughout delta
and MIME reads. Record whether a page contains `nextLink` or final `deltaLink`;
never synthesize either token. These requirements follow Microsoft's
[drive delta](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0),
[message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0),
[immutable ID](https://learn.microsoft.com/en-us/graph/outlook-immutable-id), and
[MIME](https://learn.microsoft.com/en-us/graph/outlook-get-mime-message) contracts.

For library authorization, use a Selected permission with a resource-specific
`read` assignment at the approved library/list boundary, and prove both access
to that library and refusal outside it using the actual deployed endpoints.
Selected consent alone grants no resource access. Do not silently substitute
tenant-wide Files.Read.All if the selected deployment cannot satisfy these
checks. See [Selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview).

For the shared mailbox, scope the application's mail-read role through
[Exchange Online application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)
to that mailbox. Remove overlapping unscoped mail grants: Entra and Exchange
application permissions can be additive. Prove the application cannot access
an unselected mailbox and cannot send or write. Customer administration and
those live permission tests remain outside the offline build.
