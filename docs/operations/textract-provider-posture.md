---
identity: aws-textract-analyze-document-tables-posture-1
status: proposed
provider: aws-textract
operation: AnalyzeDocument
feature_types: [TABLES]
region: us-east-2
permitted_purposes: [scanned-page-reading, image-region-reading, extraction-measurement]
retention: unverified
ai_services_opt_out: unverified
permissions: unverified
bound_by: src/corridor_pdf_reader/textract_adapter/records.py
---

# Amazon Textract provider posture

The reusable processing posture for Amazon Textract: decided once for the
provider and the operation, reused across customers, and separate from every
customer's own authorization. This is the Textract counterpart of the OpenAI
posture #557 recorded on 2026-09-03, in the shape ADR-0094 asks for: retention,
region, permissions, permitted purposes, and the effective AWS AI-services
opt-out configuration. A customer's #522 authorization *accepts* this posture
for named projects, source classes, purposes and a region; it does not restate
it, and this document authorizes no customer page on its own.

**Identity and digest.** The identity in the front matter is stable. The digest
the adapter binds to is the SHA-256 of this file's bytes, recorded in
`corridor_pdf_reader.textract_adapter.records.PROVIDER_POSTURE`, and
`tests/test_textract_adapter.py` fails when the file and the constant differ.
Any edit to this file is therefore a new posture: the digest changes, and every
authorization record that names the old digest is refused at the outbound
boundary until it is re-signed against the new one. That is the intended
mechanism, not an inconvenience; a posture change is exactly the event #557
said requires customer reauthorization.

**Status: proposed.** Three fields below are recorded as *unverified, to be
confirmed by the maintainer*. Nothing in the repository can read the AWS
account's organization policies or the calling role's permissions, and the
adapter does not pretend to. Until they are confirmed, no customer page may be
transmitted under this posture, because no #522 authorization can honestly
accept a retention statement that is not yet known.

## Processing purposes

| Purpose | Posture |
|---|---|
| `scanned-page-reading` | `AnalyzeDocument` with `TABLES` over the rasterized page of a scanned or image-only Document Rendition. Every value Textract alone supplies is an Unconfirmed reading until the corroboration rules establish otherwise (ADR-0064, ADR-0094); its confidence is recorded as a signal and is never proof. Only after customer authorization. |
| `image-region-reading` | The same operation over the raster of an image region on an otherwise native page. Only after customer authorization. |
| `extraction-measurement` | Retained-response replay and paired-rendition measurement on public or synthetic data (the reference corpus, the scan twins, the synthetic fixtures) under a recorded experiment scope. No customer agreement is involved and none is invented. |
| A page with a text layer | Not Textract's. The paired-rendition reader supplies the facts; Textract may supply cell geometry (the measured lane A) and never words, and the adapter's native-glyph re-map keeps it that way. |
| Anything else | Not permitted under this posture. Forms, queries, layout, signatures, expense and identity analysis, the asynchronous operations, and S3-input submission are outside it. |

## Provider configuration

- Provider Amazon Textract; operation `AnalyzeDocument`, synchronous; the
  document submitted as bytes in the request, so the adapter's path uploads
  nothing to S3 and needs no bucket.
- `FeatureTypes` `["TABLES"]`, the measured feature set. A different feature set
  is a different request configuration and a different cache identity.
- Region `us-east-2`, the region the rung was measured in (`TEXTRACT-RESULTS.md`).
  A different region is a different posture.
- Retries: `ThrottlingException`, `ProvisionedThroughputExceededException` and
  `InternalServerError` with exponential backoff, eight attempts; any other
  error is a Processing Failure. A page budget bounds what one Extraction Run
  may send.
- Rasterization: pypdfium2 at a stated resolution (300 dpi measured; 200 dpi for
  the degraded lane), grayscale, deterministic PNG bytes, under Textract's
  synchronous limits. The request and preprocessing configuration is part of
  every cache entry's identity.
- **No pinned base model.** `AnalyzeDocument` exposes no version selector for
  the TABLES model. The adapter records the `AnalyzeDocumentModelVersion` each
  response reports (`1.0` on all 116 retained responses of 2026-09-05 and
  2026-09-06) and promises nothing about the next call. A changed reported
  version is a new reading identity with its own manifest, never a silent
  selection.

## Retention: state it accurately

The Amazon Textract FAQ states that AWS may store the document and image inputs
the service processes and use them to maintain the service and to improve and
develop Textract and other Amazon machine-learning and artificial-intelligence
technologies, and that an account opts out of that use through an AWS
Organizations AI-services opt-out policy. Under the AWS service terms for AI
services, content processed without that opt-out may also be stored and
processed outside the Region in which the service was used; ADR-0094 records
the same warning. The Textract API reference states that when the account is
opted out, unencrypted customer content is deleted immediately and permanently
after processing and no copy of the output is retained by the service.

Two consequences follow, and neither may be softened:

- **A private bucket establishes nothing.** The experiment's scratch bucket
  (`corridor-textract-rung-9593`, public access blocked) governs where *our*
  copy of the PNGs sat; it says nothing about what the service retained. The
  retention that matters is the provider's, and only the opt-out policy
  changes it.
- **Without the opt-out, a customer page sent to Textract may be retained,
  used for service improvement and stored outside `us-east-2`.** A #522
  authorization signed under this posture must disclose that, or the opt-out
  must be confirmed first.

**Effective AWS AI-services opt-out configuration: unverified, to be confirmed
by the maintainer.** What has to be checked is the effective opt-out policy on
the account (or on the organization root, inherited) for the `textract`
service, in the account that will make the calls; the standalone experiment's
calls of 2026-09-05 and 2026-09-06 ran through the Claude AWS connector's
sandbox in an account whose policy was never inspected, so those 116 pages are
recorded as *possibly retained by the provider*. They were public or synthetic
reference material, not customer pages.

**Retention: unverified** for the same reason. Once the opt-out is confirmed,
this section records the confirmed state, the account, the date and who
checked it, and the digest changes.

## Permissions

The adapter's path needs `textract:AnalyzeDocument` on the calling principal and
nothing else: bytes submission needs no S3 read, and no asynchronous operation
is used. **The calling role's actual policy is unverified, to be confirmed by
the maintainer**; the nonproduction account and its task role (#601, #685) were
provisioned without a Textract statement (nothing under `infra/` names the
service), and the experiment's calls ran through the Claude AWS connector's
sandbox after the personal `corridor` profile's session had expired
(`TEXTRACT-RESULTS.md`); neither is a Corridor runtime identity.

## Where the customer authorization takes over

This posture is accepted, per customer, by a #522 instance that names the
customer and projects, the permitted source classes and purposes, the stage
(compatibility, shadow and authoritative are authorized separately), the
region, and this posture's identity and digest. The adapter checks that
record against every request at the outbound boundary, before any network
call, and a missing or mismatched record is a Processing Failure with the
reason and zero outbound requests (`corridor_pdf_reader.textract_adapter`).
Public or synthetic experiment data follows its own recorded scope record
naming the dataset, purpose and scope; it needs no fictional customer
agreement and cannot authorize a customer stage.

## Change authority

Parallel to #557: a change to the provider, the operation, the feature set,
the region, the retention statement, the opt-out state, the permissions or
the permitted purposes requires a named maintainer or security approver, a
measurement on the retained lanes for anything that changes the reading, a
new posture identity and digest, and customer reauthorization whenever the
provider, retention, region or subprocessor set changes. Enabling the
run-mate rescue variant or a polygon margin is a named normalization change
with its own measurement (ADR-0094); it changes the reading identity, not this
posture.

## Pending human acts, in order

1. Confirm the effective AI-services opt-out policy for the calling account
   and record it here, with the account, date and checker. If no opt-out
   exists, record that instead; the retention statement and every customer
   disclosure then say so.
2. Confirm the calling role's permissions and record them here.
3. Accept this posture (`status: accepted`), recompute the digest, and update
   `PROVIDER_POSTURE` in `records.py`.
4. Record the experiment scope for any further live measurement calls on the
   reference corpus, so those calls are made under a record and not by hand.
5. Sign the first #522 instance naming this identity and digest. Only then
   can a customer page be transmitted at all.
