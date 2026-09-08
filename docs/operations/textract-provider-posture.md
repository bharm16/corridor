---
identity: aws-textract-analyze-document-tables-posture-2
status: proposed
provider: aws-textract
operation: AnalyzeDocument
feature_types: [TABLES]
region: us-east-2
permitted_purposes: [scanned-page-reading, image-region-reading, native-table-geometry-assistance, extraction-measurement]
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

**Identity and digest.** The identity names this exact proposed revision. The
digest the adapter binds to is the SHA-256 of this file's bytes, recorded in
`corridor_pdf_reader.textract_adapter.records.PROVIDER_POSTURE`, and
`tests/test_textract_adapter.py` fails when the file and the constant differ.
Any edit requires a new identity and digest, updated together. Before changing
these bytes, preserve them and their binding in the append-only
[posture history](textract-provider-postures/history.json). The original
`aws-textract-analyze-document-tables-posture-1` remains there with its original
digest and proposed status; it is historical evidence, never an alternative
authorization accepted by the current boundary. An authorization naming an
old identity or digest is refused until re-signed against the current posture.
Neither later verification nor a new signature changes an old processing act.

**Status: proposed.** Retention, effective opt-out and workload permissions
remain **unverified**. The first customer posture requires verified effective
Textract opt-out on the **actual calling account** and verified permission on
the **actual application worker/task role**. Customer processing is refused
while any of these facts is unknown or absent, even if a record has been signed
or the status is mistakenly changed to accepted. Disclosure without opt-out is
not permitted by this posture. The adapter enforces the recorded states; a
named maintainer or security approver must verify the underlying AWS evidence.

## Processing purposes

| Purpose | Posture |
|---|---|
| `scanned-page-reading` | `AnalyzeDocument` with `TABLES` over a scanned page. Every value Textract alone supplies is an Unconfirmed reading until the corroboration rules establish otherwise (ADR-0064, ADR-0094); its confidence is recorded as a signal and is never proof. Only after customer authorization. |
| `image-region-reading` | The same operation for an image-only region, including a scanned table on a page with a native header. A native text layer elsewhere on the page does not exclude this purpose. The consuming route selects values per relevant region, preserving usable native glyphs wherever they exist and treating Textract-only values as Unconfirmed readings. Only after customer authorization. |
| `native-table-geometry-assistance` | The same TABLES/bytes-only operation supplies cell geometry for native text. Both the request and its authorization must name this purpose. The adapter requires usable native glyphs and re-maps them into the polygons; no cell or outside-string value falls back to Textract words. A cell without native glyphs stays empty in this reading. Only after customer authorization. |
| `extraction-measurement` | Retained-response replay and paired-rendition measurement on public or synthetic data (the reference corpus, the scan twins, the synthetic fixtures). New transmissions require a recorded experiment scope; offline replay does not transmit. Measurement may compare geometry with native glyphs and Textract-only readings as distinct readings. No customer agreement is involved and none is invented. |
| Anything else | Not permitted under this posture. Forms, queries, layout, signatures, expense and identity analysis, the asynchronous operations, and S3-input submission are outside it. |

These purpose names are internal processing scope identifiers. Native versus
OCR value selection is a **region** rule, not a page-wide text-layer test. The
adapter preserves the provider response as evidence; returning its words does
not authorize a consuming route to replace usable native text with them. The
geometry re-map is a separate all-native reading, not a mixed-region merger or
proof that every part of a page contains usable native text. Routing,
corroboration and production selection remain their own gates (#736, #739).

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

The [Amazon Textract FAQ, Data Privacy](https://aws.amazon.com/textract/faqs/)
states that AWS may retain inputs for service operation and improvement, and
that without opt-out some content may be stored in another Region for
improvement. It identifies an AWS Organizations opt-out policy as the way to
exclude that improvement use and cross-region storage. This is a provider
statement, not evidence of a particular account's effective configuration.
This revision does not claim an independently verified deletion time or
blanket zero retention for the intended workload. The applicable retention
terms and account evidence must be reviewed before acceptance.

Two consequences follow, and neither may be softened:

- **A private bucket establishes nothing.** The experiment's scratch bucket
  (`corridor-textract-rung-9593`, public access blocked) governs where *our*
  copy of the PNGs sat; it says nothing about what the service retained. The
  retention that matters is the provider's, and only the opt-out policy
  changes it.
- **Verified effective opt-out is mandatory before the first customer call.**
  An absent policy, `optIn`, a failed inspection or unknown state cannot be
  replaced by a disclosure or a customer signature. The posture stays
  proposed and customer pages stay refused.

**Effective AWS AI-services opt-out configuration: unverified, to be confirmed
by the maintainer.** Inspect the policy effective for the account that will
make the calls. A root, OU or attached policy alone is insufficient because
inherited and directly attached settings combine. AWS
[DescribeEffectivePolicy](https://docs.aws.amazon.com/organizations/latest/APIReference/API_DescribeEffectivePolicy.html)
with `PolicyType=AISERVICES_OPT_OUT_POLICY` returns that combined account
policy; account targets, not root or OU targets, are supported. Record the
target account, returned policy bytes/digest and timestamps, checker, and the
effective `textract` result, resolving the `default` and service-specific
settings according to AWS's
[opt-out syntax and inheritance](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_ai-opt-out_syntax.html).
The required effective value is `optOut`. Organizations administration belongs
to the policy administrator, not the workload role.

The standalone experiment's calls of 2026-09-05 and 2026-09-06 ran through the
Claude AWS connector's sandbox in an account whose policy was never inspected,
so those 116 pages remain recorded as *possibly retained by the provider*.
They were public or synthetic reference material, not customer pages. Later
opt-out does not retroactively establish how those historical calls were
processed; their original response and measurement receipts remain unchanged.

**Retention: unverified** for the same reason. Once the applicable terms and
effective policy have been reviewed, record the
confirmed statement, the calling account, region, date, checker and evidence
references in a new posture revision.

## Permissions

The [Textract service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_textract.html)
maps this operation to `textract:AnalyzeDocument`. The fixed
[AnalyzeDocument request](https://docs.aws.amazon.com/textract/latest/APIReference/API_AnalyzeDocument.html)
uses `Document.Bytes` and `FeatureTypes=["TABLES"]`; this path needs no S3
input permission and invokes no asynchronous operation.

**Actual workload permissions: unverified.** Identify the deployed worker/task
role and its account first. Verify its effective permission for the declared
operation and `us-east-2`, including applicable identity policies, permissions
boundary, session restrictions, organization controls and explicit denies.
An attached allow statement or a successful call from a personal principal
does not prove the workload's effective permission. AWS's
[policy evaluation rules](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html)
explain why restrictions can override an allow. Record role identity,
deployment binding, policy evidence, tested context, checker and timestamp.
Do not grant Organizations administration to the workload role.

## Operational verification record, 2026-09-08

At `2026-09-08T04:23:47Z`, Codex inspected the connected AWS account through the
AWS connector, read-only. This was an IAM user session; the intended deployment
account and application worker/task role were **not established**.

- `DescribeEffectivePolicy(AISERVICES_OPT_OUT_POLICY)` for the connected
  account returned `AWSOrganizationsNotInUseException`. AWS documents that
  response as an account outside an organization. This is not effective
  opt-out evidence for an intended workload account.
- `ListRoles` returned only `AWSServiceRoleForSupport` and
  `AWSServiceRoleForTrustedAdvisor`. Neither establishes the Corridor workload
  role or its effective Textract permission.
- No AWS policy, account, role or infrastructure setting was changed. No
  Textract request was made.

The complete receipt remains in the maintainer's local evidence directory as
`aws-connected-account.json`, SHA-256
`0cafc3649c12e3fa2624de9b8894237fb48c991619bc64e13d638ef7dbb0fda7`.
Personal principal and account identifiers remain in that receipt rather than
this reusable document. These scoped observations do not change the three
unverified fields or proposed status above.

## Where the customer authorization takes over

This posture is accepted, per customer, by a #522 instance that names the
customer and projects, the permitted source classes and purposes, the stage
(compatibility, shadow and authoritative are authorized separately), the
region, and this posture's identity and digest. The adapter checks that
record against every request at the outbound boundary, before any network
call, and a missing or mismatched record is a Processing Failure with the
reason and zero outbound requests (`corridor_pdf_reader.textract_adapter`).
Public or synthetic experiment data follows its own recorded scope. Every new
live experiment names its dataset and digest, account/role, purpose, region,
call/page/spend budget and retention scope before transmission. The existing
`ExperimentScope.scope` carries the operational scope; the caller must keep
the corresponding evidence and enforce its budgets. This is distinct from
the adapter's page budget and is not an assertion that the adapter verifies
the AWS principal or enforces a spend limit. Offline retained-response replay
needs no new transmission approval. An experiment record cannot authorize a
customer stage.

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

1. Identify the actual calling account, deployment and worker/task role.
2. Verify effective Textract `optOut` for that account and the applicable
   retention terms. If evidence is absent, negative or unknown, record that
   result and keep this posture proposed; policy remediation is a separate
   authorized administrative act.
3. Verify the actual workload role's effective permissions and record its
   restrictions and evidence.
4. A named maintainer or security approver accepts the verified posture in a
   new revision, preserving this revision first. Set `status: accepted`,
   `retention: verified`, `ai_services_opt_out: optOut` and
   `permissions: verified` only with the evidence above. Update identity,
   digest and `PROVIDER_POSTURE` together, and run outbound-boundary tests.
   These state values are acceptance assertions backed by this document,
   not an automatic AWS verification mechanism. #732 remains open
   `ready-for-human` until this act is complete.
5. For each customer, separately sign its #522 instance naming the accepted
   identity and digest. That signature gates that customer's pages; it does
   not hold #732 open after reusable posture acceptance. ADR acceptance,
   provider posture acceptance and production pipeline selection are also
   separate acts.
