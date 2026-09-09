# Project-bound email capture

`#511` takes delivery, establishes the customer/project boundary, stores raw MIME
through the content-addressed store and registers the message and attachments.
`#455` reads that exact retained `SourceEnvelope`. The normal `make extract`
route for a bound email Document now captures the thread through `email_spine`.
It creates Source Facts and Proposed Deltas, with no accepted-record write.

The source format follows [RFC 5322 identification fields](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4)
for header threading and [RFC 2045](https://www.rfc-editor.org/rfc/rfc2045) for MIME
transfer decoding. Header-only threading remains owned by intake. The semantic
reader never joins threads or selects a different customer/project from content.

## Source and reading contracts

`email_span` is an internal locator kind. Its versioned locator identifies the
original rendition digest, MIME part path, section, header name/occurrence and
character range in that decoded part. Replay reconstructs the part from the
retained original and checks the locator, exact text and digest. It has no
invented PDF page. Headers, body paragraphs, quoted history, signatures, drafts,
HTML and attachment boundaries remain separately addressable. MIME transfer
encoding is decoded; raw original bytes remain unchanged.

`email_header` and `email_attachment` are internal Source Fact types for transport
evidence, never automatic accepted fields. An attachment boundary records the
digest of the decoded bytes. Its separately registered Document and own parser
own attachment-derived facts and rendition provenance; an outer email cannot
attribute an attachment's content to the sender as a concluded body statement.
Unsupported attachments retain the intake refusal and their bytes in raw MIME.

The bounded provider receives only retained segment references and untrusted
text. Its strict response selects one closing segment as `concluded` or
`unresolved` and lists read segment IDs. Every authored turn must be accounted
for; duplicate, foreign, omitted or nonclosing references refuse. A concluded
statement can cite only authored plain text and that message's own From header.
The application materializes the exact words; the provider has no output field
for invented values, identities, acceptance or writes. An unresolved reading
retains the closing source words as the question and creates no statement Fact.

The source-bound subject is `email-thread:<id>`. A previously accepted statement
for that thread is compared to the incoming wording. Unknown subjects produce
one proposed subject carrying `statement_wording`; a known subject produces a
field delta only when the wording differs. Person/organization interpretation
and acceptance remain human decisions. The accepted revision and reader version
are bound to the delta. Metadata Facts alone never create deltas.

One transaction captures the Facts, reading and supersessions. A later conclusion
supersedes that thread's unaccepted delta. A question, or a return to the accepted
wording, can supersede it without inventing a replacement Fact or delta:
`DeltaSupersession.source_reading_id` names the replacement source reading.
Existing delta queries and Resolve Delta use that same supersession relation.
Independent threads and already accepted decisions remain separate; all earlier
readings and facts remain readable. Historical legacy Candidates are unchanged.

## Replay and limits

```sh
make email-source ARGS="inspect 123"
make email-source ARGS="capture 123 --response response.json"
make test-focused ARGS="tests/test_email_spine.py"
```

`inspect` exposes the actual segment IDs; `response.json` follows this shape,
using IDs from that reading:

```json
{"resolution":"concluded","segment_id":17,"read_segment_ids":[1,2,3,17]}
```

The offline adapter is recorded as `retained-response`. Normal processing uses
the configured `StructuredClient` boundary and retains its model, prompt, schema,
postprocessor and token usage. No fixture calls an external provider. Repeating
an unchanged thread returns its retained outcome before invoking a provider.
A crash before commit rolls back the capture; a crash after commit reuses it.
Missing stored bytes, mismatched envelopes, malformed MIME and invalid provider
responses fail the attempt; they never count as an empty successful extraction.

The reader allows 20 turns, 200 MIME parts per message, nesting up to 20, 400,000
decoded characters per message and 120,000 across the complete thread. It never
truncates a conversation into a false conclusion. Quoted history uses explicit
`>`/reply-history delimiters, signatures the `-- ` delimiter, and drafts the
`X-Unsent: 1` header. The bounded reader must still distinguish tentative or
draft wording inside an authored line. HTML is retained as exact source and
stays unresolved in v1; it is not silently stripped into a statement. A conclusion
that spans multiple independently segmented paragraphs stays an explicit question.

## Delivery boundary

Reusable capture, replay, comparison and retry software is fixture-backed now.
Actual mailbox credentials/account configuration, deployment, signed processing
authorization, customer runs and pilot findings remain later work. Fixture
results do not qualify an actual customer mailbox or a live model configuration.
#456 fixes the five minutes capabilities and #562 fixes `project-contacts-v1`;
both can be implemented with fixtures without waiting for partner selection.
