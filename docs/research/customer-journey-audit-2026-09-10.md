# Customer-journey audit, 2026-09-10

Received after the 2026-09-10 architecture audit cohorts (#802 through #819) had merged.
Its verdict is that Corridor has substantial working machinery but the supported customer
journey does not yet expose and connect it: the intake routes sit outside the enforced
live-pilot boundary, Adopt Baseline and issue-profile registration have no production
caller, later-revision capture is not wired to ordinary intake, the Issue forms omit the
request-forgery token their authentication requires, package bytes cannot be inspected or
downloaded from the supported workflow, correspondence writers have no production caller,
and some work is shown with no way to complete it.

The audit reviewed `main` at `795494a8`. Every finding was re-verified against that same
commit before tickets were written; the verification table below records what was
inspected in source, what was inferred rather than reproduced, and what the audit got
slightly wrong. The maintainer's review of the first ticket breakdown, also 2026-09-10,
fixed the dispositions recorded under *Accepted recommendations* and *Explicitly
unresolved*. The result is umbrella issue #820 under #459 with children #821 through
#849.

This note is evidence. It is not an ADR and not a second backlog: the issues own scope and
acceptance, and accepted decisions change only through the successor-ADR procedure.

## How to read the verification

- **Inspected**: the claim was checked in source at `795494a8` and the cited lines say what
  the audit says.
- **Inferred**: the failure path follows from the inspected code but was not exercised. No
  browser session, deployed environment, or usability test was run for this audit.
- **Corrected**: the substance holds but a name, location or absolute wording in the audit
  is wrong; the correction is stated.

Line numbers are for `795494a8` and will drift.

## Verification at 795494a8

| Audit finding | Status | Evidence | Issue |
|---|---|---|---|
| Issue Prepare and Approve forms omit the request-forgery token; a real session's submission would be refused | Inspected; the 403 is inferred, not reproduced in a browser | `src/corridor/web/templates/project_workflow.html` 438-439, 483-484 (no token, no script); `review.html` 301-302, 400-401 (token present); `src/corridor/web/app.py` 810-833; `src/corridor/web/auth.py` 35, 48-64 | #821 |
| The end-to-end issue-path test overrides the human principal and starts from an adopted, configured project | Inspected | `tests/test_issue_path_end_to_end.py` 147-209; the same override in `tests/test_issue_section.py` 232 and `tests/test_release_preparation.py` 131; reusable sign-in helpers `tests/test_sign_in_access.py` 98-117 | #848, #849 |
| The live-pilot manifest excludes upload, confirmation, source list, inbound mail, the Review source link, page images, legacy report downloads and the operations screens | Inspected; the manifest holds sixteen entries | `src/corridor/web_boundary.py` 95-460; routes `src/corridor/web/app.py` 7024, 7046, 7106, 7146, 7209, 7244, 7269, 7299, 4988, 6380, 2507-2698, 3125-4181; enforcement 651-691 (404 when enforced, 503 when inconsistent, nothing when not declared) | #824, #847, #831, #830, #842 |
| Enabled pages link to routes outside the manifest | Inspected | `project_workflow.html` 80, 505; `record_history.html` 473; `review.html` 211, 281 | #824 |
| A product upload is not a Source Delivery; it records an arrival analytics event | Inspected | `src/corridor/source_intake.py` 40-58, 301-310, 409-506; pin in `tests/test_source_delivery.py` | #823 |
| The source-history page lists only documents with a product-intake confirmation receipt | Inspected; the page's own heading is honest, the Work-page link is not | `src/corridor/source_intake.py` 518-538; `source_uploads.html` 38-42 | #841 |
| The later-revision capture is called only from shadow processing; ordinary spreadsheet dispatch selects the generic reader | Inspected; corrected: the caller is `shadow_processing.py`, and the dispatch decides by file suffix with no session or project in scope | `src/corridor/later_revision.py` 378-390; `src/corridor/shadow_processing.py` 194; `src/corridor/pipeline.py` 433-509; `src/corridor/extract_project.py` 421-430 | #825 |
| Adopt Baseline has no web caller and enters an owner-bootstrap context | Inspected; stronger than claimed: no production caller at all, the bootstrap requires the schema owner, and customer processing requires a current activation receipt | `src/corridor/baseline_adoption.py` 286, 322-323, 385-402; `src/corridor/activation_runtime.py` 60-78, 126-134 | #826, #827 |
| Issue-profile and template registration have no production caller | Inspected | `src/corridor/issue_profile.py` 241; `src/corridor/baseline_adoption.py` 533-586 | #828, #829 |
| The upload status vocabulary exists; the operations screens are outside the boundary | Inspected | `src/corridor/source_intake.py` 585-608; `src/corridor/web/app.py` 7289-7296, 3125-4181 | #841, #842 |
| The Issue section lists digests with no download; retrieval exists unwired; legacy download routes are outside the boundary | Inspected; also found: the package-history reader for the new release family exists and is unwired | `src/corridor/web/issue_section.py` 216-223, 444-456; `src/corridor/release_authorization.py` 202, 524, 579-591; `src/corridor/web/app.py` 2507, 2536, 2611 | #830 |
| An authorized package is labelled "Approved and sent" while the Follow-up section says Corridor sends nothing | Inspected | `src/corridor/web/issue_section.py` 151; `src/corridor/web/follow_up_view.py` 153; `project_workflow.html` 296 | #830 |
| The Record view's release history reads the legacy report-release family only | Inspected | `src/corridor/record_history.py` 25, 101-104, 468 | #830 |
| No designation check before Prepare or Approve; Review requires the coordination designation | Inspected | `src/corridor/web/app.py` 4360-4369, 4382, 4462-4468, 5100, 5271; `project_workflow.html` 17-19, 331-335 | #839 |
| The cutoff is a hidden render-time value with no selection or display | Inspected | `project_workflow.html` 487; `src/corridor/web/app.py` 4433, 4486, 4563-4588 | #840 |
| No refresh or status control while a candidate is being prepared | Inspected | `project_workflow.html` 115-122; `src/corridor/web/issue_section.py` 172-177 | #840 |
| Source questions are display-only and counted as waiting work | Inspected; the questions are minutes-capture outcomes with ten reason codes | `review.html` 71-82; `src/corridor/minutes_reading.py` 13-60; `src/corridor/project_workflow.py` 521-522, 549 | #833 |
| Packet reversal is built and tested with no route | Inspected | `src/corridor/review_packets.py` 913; every caller is a test | #834 |
| Edit means another source's captured value | Inspected | `review.html` 346-349; `src/corridor/packet_review.py` 1795 | #832, #836 |
| The saved result names a count and a revision with no link | Inspected | `src/corridor/web/app.py` 5225-5239, 5405-5430; `review.html` 84-88 | #834 |
| Deferred and coordination-needed work has no return path on the work surfaces | Inspected; the return date is visible only in record history | `src/corridor/review_packet_reading.py` 745-756, 951; `src/corridor/project_workflow.py` 552; `record_history.html` 303-305 | #835 |
| Outgoing-request retention has no production caller; one plan per request; a response is a date and a recorder; the accepted #652 contract is wider | Inspected; the accepted contract is a maintainer comment on #652 dated 2026-09-04, not an ADR | `src/corridor/outgoing_requests.py` 29-34, 55-72, 122-129; `tests/test_follow_up_bundles.py` 759 | #837 |
| The Follow-up screen is read-only | Inspected; corrected: it is a section of the Work page, not a separate screen | `project_workflow.html` 198-202, 294-296; `src/corridor/web/follow_up_view.py` 40-47, 244-251 | #835, #837 |
| A contact-correction endpoint exists with no form | Inspected; it is the first manifest entry and no template mentions contacts | `src/corridor/web/app.py` 7371-7391; `src/corridor/web_boundary.py` 96-100 | #838 |
| The measurement contract still requires a mailbox or folder, the fixed four artifacts, and the #512 condition; the pinned table lacks the issue profile | Inspected; #512 closed 2026-09-03; ADR-0091 names the document as stale | `docs/pilot-success-criteria.md` 30, 35, 45-51, 63, 104; `src/corridor/pilot_measurement.py` 51-55 | #845 |
| Two material-field strata have no canonical column | Inspected; Applies To and closure are also reported as unmapped material values | `src/corridor/baseline_workbook.py` 80-102; `docs/pilot-success-criteria.md` 99 | #845 |
| Human measurement inputs have no collection surface | Inspected; corrected: the inputs live in `measurement_collection.py`, not `pilot_measurement.py`; also found: the collector has no production caller | `src/corridor/measurement_collection.py` 34-44, 82; `src/corridor/pilot_measurement.py` 217 | #846 |
| Navigation, session expiry, bulk-selection count, technical detail and history limits | Inferred from the templates; no usability test was run | `project_workflow.html` 80, 504-505; `review.html` 429-437 | #843, #844, #834, #830 |
| Not in the audit: the boundary's capability reader swallows every exception and reads as not declared | Inspected during verification; the enforced flag still enforces, so this is hardening, not a demonstrated exploit | `src/corridor/web/app.py` 624-640; `src/corridor/web_boundary.py` 684-720 | #822 |

## Accepted recommendations

From the maintainer's review of the first breakdown, 2026-09-10. Each is recorded on the
issue that carries it.

- One milestone umbrella (#820) under #459; consumers depend on the children, which own
  scope and native blocked-by edges. Work is classified as core journey acceptance,
  required when selected, measured-pilot readiness, or operations support; not every
  ticket is a universal pilot-entry gate (#845 records the classification in the roadmap).
- Adoption from the product needs a **limited onboarding authorization** recorded as an
  amendment to the activation and authority decisions before it is built (#826), and the
  adoption command is granted narrowly to the web capability with PostgreSQL proving the
  designation; schema-owner credentials stay out of the web service and the shadow
  runtime (#827).
- A product upload is a Source Delivery of transport push with a human authentication
  mode, with its whole lifecycle recorded, not a third transport (#823). The upload routes
  are admitted only after that (#824), and the optional model-assisted draft routes are
  not admitted with them.
- Machine intake keeps its own authentication class within one canonical route registry
  rather than a second independent manifest (#847).
- Finish the accepted #652 contract: one request advancing several plans, evidence-linked
  responses with completeness (#837). Plan lifecycle lives in #835, not #837.
- Source questions get a reason-to-action matrix, append-only answers, and no fabricated
  deltas or facts (#833); the correction path for a wrong extraction is a source-grounded
  correction request handed to operations and returned through Review (#832, #836).
- Project Coordination confirms coverage and requests preparation; External Release
  approves; read-only membership confers neither; enforced in the command path with a
  revocation-race test (#839). The effective cutoff is shown and coverage can be
  refreshed; earlier-cutoff selection waits until the source, record and profile
  semantics are proved (#840).
- Pilot judgments are collected in the product under the pinned configuration, the
  denominator comes from presentation records, and missing judgments stay missing (#846),
  after the measurement contract is reconciled (#845).
- Session recovery preserves input as a short-lived non-authoritative draft restored only
  after fresh authentication and membership verification, with no replayed POST (#844).
- The acceptance workstream starts now as a harness with a role, state, action, route and
  result matrix and the three mechanically enforceable guards (#848); the core journey is
  proved separately (#849); selected extensions join by their own tickets; deployed
  execution is #489's receipt.
- The unknown-capability seam refuses instead of reading as legacy (#822).

## Explicitly unresolved

- The onboarding-authorization amendment (#826) and the correction-path record (#832) are
  written by the implementing lane and approved by the maintainer at PR review; until
  merged, #827 and #836 remain blocked.
- Partner-dependent scope stays `needs-triage` until the partner input is named: minutes
  questions (#833), exact contacts (#838), mail or push ingress (#847), and per-partner
  material-field strata (#845 with #555).
- Whether the first commercial scope is manual-upload only. The measured pilot keeps at
  least one project-connected channel; a manual-upload rehearsal is reported as such and
  never as connector coverage (#845).
- Arbitrary earlier-cutoff selection (#840) is deferred, not refused.
- Whether ADR-0038's index status changes depends on the crosswalk #845 writes; a status
  flip from the classification alone was declined.
- Direct delivery to a partner document system stays #563.

## Resulting issues

| Issue | Title | Class |
|---|---|---|
| #820 | Complete the supported customer journey from onboarding to an approved, downloadable issue | umbrella |
| #821 | Issue Prepare and Approve submit under a real signed-in session | core |
| #822 | An unknown web capability refuses instead of reading as a legacy deployment | core |
| #823 | A product upload is a Source Delivery with its whole lifecycle | core, schema lane |
| #824 | Admit upload, preview, confirmation, the source register and source access to the live-pilot boundary | core |
| #825 | A confirmed UCM revision on an adopted project runs the later-revision capture with the coordinator's declarations | core |
| #826 | Record the limited onboarding authorization that precedes activation | core, decision record |
| #827 | The coordinator previews and adopts the baseline from the product | core |
| #828 | The issue set is reviewed and approved in the product | core |
| #829 | Replace the output template or mapping without re-adopting the baseline | operations support |
| #830 | Inspect and download the exact candidate and the approved package, with package history in the Record view | core |
| #831 | The exact-source view | core |
| #832 | Record the source-grounded correction path for a wrong extraction | core, decision record |
| #833 | Every source question has a permitted disposition or an accountable clarification path | required when selected |
| #834 | A packet receipt, Undo, and honest batch buttons on Review | core |
| #835 | Deferred and Needs-coordination work returns, and the plan lifecycle has one home | core |
| #836 | Report an extraction error and receive the corrected capture through Review | core |
| #837 | Record what was sent and what came back, one request to the plans it advances | required when selected, schema lane |
| #838 | Correct a shown project contact from its follow-up bundle | required when selected |
| #839 | Project Coordination prepares, External Release approves, and the page says so | core |
| #840 | The Issue section shows the effective cutoff, refreshes coverage, and names who unblocks what | core |
| #841 | One source register for every delivery the project has received | core |
| #842 | Operations repair under policy, with receipts the source register shows | operations support |
| #843 | A consistent navigation shell on every customer page | core |
| #844 | A session that expires mid-form recovers without losing or replaying input | core |
| #845 | Reconcile the measurement contract with what is built and what is selected | measured-pilot readiness |
| #846 | Pilot judgments and time observations are collected in the product without shrinking the denominator | measured-pilot readiness |
| #847 | Serve machine intake through its own authentication class of one route registry | required when selected |
| #848 | The acceptance harness, the role-state-action-route-result matrix, and the mechanical guards | core |
| #849 | The core customer journey passes end to end under the enforced boundary | core |

---

## The audit as received

Reproduced verbatim, with the citation-marker glyphs of the original removed. The
first-person voice is the reviewer's.

# Verdict

**Yes, there are meaningful gaps between the implemented backend and a complete customer workflow. Your concern is justified.**

My earlier description of the "core workflow being built" was too broad. It treated implemented domain services, some completed screens, and passing integration tests as stronger evidence of an operable customer journey than they are.

This audit found concrete problems in the **beginning, recovery paths, and end of that journey**:

- A coordinator cannot onboard and adopt a baseline through the enabled pilot interface.
- Existing upload and source-history routes are excluded from the live-pilot route boundary.
- The ordinary production intake path is not clearly connected to the specialized later-UCM-revision processor.
- The Issue screen's prepare and approve forms omit the CSRF field required by authenticated sessions.
- The Issue screen lists artifact hashes but offers no current-package preview or download.
- Several important actions, including recording sent requests, recording responses, reversing packet decisions, and configuring the issue set, exist only as backend operations.

These findings come from the pinned `main` snapshot **`795494a`**. I examined the roadmap, pilot contract, route boundary, application handlers, templates, relevant domain services, and tests. **This is a source-level audit; I have not run the deployed application or reproduced the suspected browser failures interactively.** The route exclusions and missing controls are directly visible in the implementation.

The central distinction is:

> **Corridor has substantial working machinery, but the supported customer journey does not yet expose and connect all the machinery necessary to perform the promised job.**

## The most important findings

| Priority | Finding | Practical consequence |
|---|---|---|
| **Critical** | Prepare/approve forms omit the required CSRF field. | A genuine signed-in user's normal form submission is expected to be refused, even though tests that override authentication pass. |
| **Critical** | Live-pilot routing excludes upload, source history, and the inbound-mail endpoint. | The documented ways to supply documents are not all available in the actual pilot surface. |
| **Critical** | No customer-facing Adopt Baseline flow exists. | The workflow starts from a project that someone has already adopted through backend code. |
| **Critical** | No current-package preview/download controls exist. | A user can approve a package without inspecting its files, then cannot retrieve those files through the enabled workflow. |
| **High** | Specialized later-revision processing is not wired into the ordinary intake path I inspected. | "Upload the next UCM" is not yet proved to execute the same semantics the dedicated revision tests exercise. |
| **High** | Follow-up request/response writers have no production callers. | The system cannot collect the communication history needed to make its no-response feature useful. |
| **High** | Some displayed source questions have no answering controls. | A project can show work that the coordinator cannot complete. |
| **High** | Pilot requirements and implemented scope disagree. | You cannot yet consistently define what the pilot must demonstrate or how several results are scored. |

The supporting evidence and recommended treatment for each follow.

# 1. How documents are onboarded today

## There are several mechanisms, not one completed onboarding workflow

The repository contains meaningful document-intake code. The existing manual-upload path stages a file, performs bounded validation, shows a preview, and confirms registration. It supports PDFs and supported spreadsheet formats with a 64 MiB limit. Its documentation describes project email as the primary intake route and manual upload as a fallback.

The application contains:

```text
GET  /projects/{slug}/sources/upload
POST /projects/{slug}/sources/upload
POST /projects/{slug}/sources/confirm
GET  /projects/{slug}/sources
POST /intake/inbound
```

Those are genuine handlers, not imagined capabilities. However, **they are absent from `PILOT_ROUTES`**. The route-boundary dependency is applied before these handlers, and an unlisted route returns 404 when the pilot boundary is enforced. The source-history links currently printed on the Work screen therefore lead outside the supported pilot surface. The inbound-mail handler also inherits this boundary despite having its own machine authentication.

**This is a real integration gap, not a request for a new feature.**

The fix is not to disable the boundary. Each required route needs to be deliberately admitted, given the appropriate database access, and tested under the actual pilot role.

For machine intake, either explicitly support the authenticated machine endpoint in that deployment or mount it behind a separately defined machine boundary. Do not accidentally route it through an allowlist intended only for human screens.

## Uploading a document is not adopting a baseline

`baseline_adoption.py` implements a thoughtful two-stage design:

**Operations reading:** Can Corridor safely read and return this workbook? What are its sheets, mappings, formulas, unknown columns, unsupported values, and round-trip limitations?

**Coordinator reading:** What material project scope, identity, exclusion, or interpretation questions require the coordinator's authority?

The backend then supports one attributable, atomic adoption. That separation is sound.

But I found no user-facing route or screen that presents that preview and invokes `adopt_baseline`. The inspected call sites are backend definitions and test setup. The current workflow tests create and adopt the project programmatically before the user-facing scenario begins.

Consequently, a newly provisioned project has no complete user path from:

> "Here is my current UCM."

to:

> "I have reviewed what Corridor will treat as our accepted baseline, and I approve it."

### What is needed

A small **baseline onboarding flow**, not a generic spreadsheet editor:

| Step | Person responsible | Required interaction |
|---|---|---|
| Supply the current UCM | Coordinator or operations | Upload the exact file and identify its project and intended role. |
| Validate workbook mechanics | Operations | Inspect diagnostics, mapping, supported features, and round-trip result. |
| Resolve mapping problems | Operations | Register an explicit mapping or explain why the file is unsupported. |
| Review material project questions | Coordinator | See the scope, identities, exclusions, and unresolved material interpretations, not parser diagnostics. |
| Adopt | Coordinator | One clearly scoped approval over the exact preview and source identity. |
| Confirm completion | Product | Show the adopted baseline, retained source, mapping, and next step. |

Operations-assisted onboarding is acceptable. **Having operations prepare the mapping is different from having an engineer impersonate the coordinator to make the adoption decision.**

The existing adoption code also enters an owner-bootstrap context. Exposing it safely therefore requires a deliberately designed request/authority handoff, not simply calling it from a web handler with elevated database credentials.

## There is a delivery-ledger gap for manual uploads

The source-intake module explicitly documents that human uploads do **not** yet become ordinary `SourceDelivery` records.

The current delivery schema admits pull and push transports, but pushed deliveries require a credential identity that an authenticated human uploader does not hold. The implementation correctly refuses to pretend the upload was a pull or invent a connector credential. It records an arrival observation instead.

That honesty is good, but the missing integration has consequences:

- coverage is derived from delivery records;
- later-UCM capture requires a stored delivery;
- source-arrival measurement depends on reliable identity;
- users need one place to see what arrived and what happened to it.

**Manual intake must join the canonical delivery model before it can be the first pilot's reliable front door.** Preserve the human actor, exact bytes, source identity, project, and receipt without disguising the transport.

# 2. "Upload the next UCM" is not yet a proved production path

The specialized `capture_later_revision` operation is implemented. It reuses baseline mapping and row identity, preserves accounting, compares against the accepted record, and produces Proposed Deltas without changing accepted values. It requires a retained, stored delivery and explicit declarations before absence can become an apparent-removal proposal.

But the call-site search found its production use in **shadow processing**, not the ordinary application or scheduled production intake path. The ordinary spreadsheet extraction route still selects `extract_sheet`; it does not select the dedicated later-revision reader based on an adopted project and registered workbook family.

That does **not** mean the generic path produces no facts or deltas. It means:

> Passing the dedicated later-revision tests does not prove that a normal customer upload executes that dedicated behavior.

This matters because the dedicated module exists precisely to preserve cases the generic extractor did not handle equivalently: retired rows, missing fields, ambiguous identities, and apparent removals.

### Required integration

For an adopted project, incoming documents need an explicit classification and dispatch contract:

| Incoming item | Processing path |
|---|---|
| Initial baseline | Operations compatibility, then coordinator adoption |
| Later revision of the registered UCM family | Dedicated later-revision capture |
| Another rendition of the same document | Retained rendition relationship; not automatically another revision |
| Supporting minutes or email | Their selected source-processing path |
| Schedule/date export | Selected date-table path |
| Unknown or incompatible file | Visible holding state with an owner and next action |

The user-facing confirmation must also establish facts bytes cannot prove:

- Is this the complete current matrix or a partial export?
- Which registered source family does it belong to?
- What is its source revision identity?
- Is it a replacement, a supplement, or another rendition?
- Does it use the existing mapping?

These questions should be defaulted from reliable registered context where possible, not asked repeatedly. But they cannot remain unnamed Boolean arguments available only to a developer.

# 3. Source history and processing recovery need a real home

## The current "source history" is not a complete source register

`list_confirmed_uploads` selects documents with a `CONFIRM_SOURCE_INTAKE` audit entry. It is a list of confirmed product-intake documents, not a complete view of every delivery, refusal, connector event, or source relationship. Yet the Work screen describes the destination as "every document delivered to this project."

Even after fixing the route allowlist, that description would overstate its coverage.

### Replace it with a unified Sources workspace

This can reuse existing readers and receipts. It should answer:

| User question | Required display |
|---|---|
| Did Corridor receive my file? | Delivery identity, filename, channel, received time, and duplicate status. |
| What is it? | Declared type, source family, revision/rendition relationship, and project. |
| Has it been processed? | Queued, processing, completed, held, failed, or deliberately excluded. |
| What did processing produce? | Row accounting, captured facts, proposed changes, and unresolved questions. |
| Why is it blocked? | A bounded explanation and whether the coordinator or operations must act. |
| What should happen next? | Retry, supply a corrected file, confirm identity, ask operations, or no action. |
| Can I inspect the original? | Authorized access to exact retained bytes and the cited location. |

Do not make "Processed" synonymous with "nothing else needs attention." Successful extraction can still produce unresolved identity questions or a failed comparison.

## Processing failures currently risk becoming permanent dead ends

The existing upload status vocabulary includes pending, unreadable, failed parse, processing failed, and held-unmodeled. That is a useful foundation. But the legacy operations screens are outside the pilot allowlist, while the current Work screen can report technical blockers without providing a route to resolve them.

Not every coordinator should be able to retry a model run or change an extraction policy. The necessary product behavior is:

> "This file is blocked for this reason. Operations owns the next action. Here is its current status."

A staff-only runbook can initially perform the actual repair. But the coordinator needs visibility, and operations needs an attributable, repeatable repair procedure. Otherwise the pilot's operations-time target excludes hidden engineering rescue.

# 4. The most immediate browser defect: Issue forms and CSRF

The current `project_workflow.html` contains forms for:

```text
/work/{slug}/issue/prepare
/work/{slug}/issue/authorize
```

Neither emits `{{ csrf_field() }}`. The forms have no script supplying an equivalent header. By contrast, both forms on `review.html` explicitly include the CSRF field.

The real authentication dependency requires the submitted CSRF token for unsafe methods and returns 403 when it is absent or invalid. The readable CSRF cookie alone does not satisfy that contract; the token must be echoed in the form or request header.

**Expected result:** a genuine signed-in coordinator clicking Prepare or Approve submits a form that authentication rejects.

I have not run this in a browser, so I am distinguishing a strongly supported source-level defect from an observed deployed failure.

### Why the end-to-end test did not establish otherwise

`test_issue_path_end_to_end.py` does useful work: it drives rendered forms, real commits, the production background handler, candidate creation, and authorization.

But its fixture replaces:

```python
app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
```

That removes the authentication dependency that performs the CSRF check. It also starts from an already adopted, configured project.

So the test proves an important **inner segment** of the workflow. It does not prove the first-user, real-session journey.

### Required fix

Add the actual CSRF field and test these actions through a real synthetic sign-in session. Do not "fix" the test by manually injecting a header a normal browser form never sends.

This should be the first small defect addressed.

# 5. The output workflow stops before the user receives the output

## There is no current-package preview or download control

The Issue section lists artifact type, renderer, digest, and byte count. Its `ArtifactRow` does not contain a download or preview URL. The template offers approval, but no way to inspect the actual workbook/report bytes or download the current authorized package.

A backend `retrieve_released_artifact` operation exists. The application's older report-download handlers use the legacy report-release family, and those routes are not part of the enabled pilot surface.

This is a direct gap in the main product promise. **A package hash is not a customer deliverable.**

### Required controls

| State | Required user action |
|---|---|
| Candidate prepared | Preview or download each exact candidate artifact. |
| Candidate stale | Inspect it as stale history, but cannot authorize it as current. |
| Candidate current | Approve the inspected package under the existing authorization rules. |
| Package authorized | Download the exact authorized artifacts, individually and preferably as one bundle. |
| Later record changes | Retrieve the old authorized package without regenerating it. |

Downloads should resolve retained artifact identities on the server and enforce project access. Do not accept arbitrary storage keys from the browser.

A preview of an XLSX can initially mean downloading it for Excel inspection. You do not need to build a spreadsheet viewer before testing this workflow.

## Authorization is being described as sending

The Issue view labels an authorized package:

> "Approved and sent as this issue"

But the implemented act is authorization, not delivery to an external party. The Follow-up screen separately and correctly says it sends nothing.

Use separate facts:

```text
Prepared
Approved for sharing
Downloaded
Sent or handed off, if separately recorded
```

Do not imply "delivered" without delivery evidence.

For the first pilot, **authorized download plus manual customer-system handoff is enough**. Direct SharePoint/Aconex/ProjectWise delivery can remain deferred. The download itself cannot.

## Release history still mixes old and new models

The Record/history reader imports `external_report_release_history` from the legacy report-release module. Its own documentation explicitly says that is where release history comes from. The current package-release family is therefore not automatically covered by that history screen.

Add the new package history to the existing investigation view:

- issue number;
- accepted revision;
- profile and cutoff;
- authorizer;
- predecessor;
- artifacts;
- exact downloads.

Do not create another competing release model.

# 6. Review has useful screens, but several action paths remain incomplete

The Review UI is materially more complete than onboarding. It already has source-revision batches, focused cross-source questions, child outcomes, evidence text, consequence levels, Needs coordination, and Defer. Those should be preserved.

The remaining gaps are specific.

## A. Some source questions are display-only

`reading.source_questions` renders a filename, quoted text, and reasons under "Statements to review." Those items do not receive answering controls in that section. The project workflow counts them as waiting work.

That is a dangerous user experience:

> The product says a question needs review, but provides no action that can settle it.

Each supported question class needs a defined ending: resolve identity, clarify scope, confirm an interpretation, retain it as unresolved with a Follow-up Plan, or record an explicit out-of-scope disposition.

Do not merely add a generic "Dismiss" button. The action must match the question's domain meaning and retain its evidence.

## B. Source inspection is weaker than the displayed citations suggest

The Review page prints filenames, locations, and quoted text. Its external-record links go through `/review/{slug}/source`, which is not admitted by the pilot route manifest. The legacy page-image route is likewise outside the enabled surface.

A coordinator needs to inspect surrounding context, not just trust an extracted quotation.

Provide a shared, project-authorized source viewer supporting:

- original document access;
- cited PDF page or spreadsheet sheet/cell;
- nearby context;
- current source identity and revision;
- clear distinction between original and derived rendition.

This directly supports the pilot's manual-reconstruction-time requirement. It is not decorative UX.

## C. Undo exists in the backend but not the current user journey

`reverse_review_packet` exists and has tests, including tests that reverse a Needs coordination decision. I found no corresponding action in the current Review/Work handlers or enabled pilot routes.

Provide a bounded **Undo this decision** action from the saved-result or history view, subject to the backend's stale/successor restrictions.

A coordinator must not need an engineer to correct an accidental acceptance.

## D. "Edit" currently means selecting another captured value

The focused form explicitly says an edited external fact is another source's captured value, not arbitrary typed wording. That preserves provenance and should not be replaced by unrestricted editing.

But it leaves a product question:

> What does the coordinator do when the source is clear but the extraction is wrong, and no alternative captured value exists?

Define a supported correction path that distinguishes:

- correcting an extraction against the source;
- recording a new human coordination decision;
- choosing another source;
- asking for clarification.

Do not solve this by overwriting the original Source Fact. The UI should invoke the appropriate existing correction/decision contract or explicitly hand the technical correction to operations.

## E. Post-save diagnostics need a direct route

The current save response emphasizes the number of changes and the resulting revision. Record history has useful per-delta and packet information, so traceability is not absent. However, the workflow should directly offer:

> "View the result for these changes."

That is the practical way to satisfy the pilot criterion that a coordinator can identify every child's outcome without support or a database lookup. A generic record-history search is weaker than a receipt linked from the exact action.

## F. Deferred and coordination-needed work needs a complete return path

Creating Defer or Needs coordination is implemented. The missing usability proof is what happens afterward:

- Can the coordinator find deferred work before its return date?
- Can they change an erroneous return date?
- Can they resume the question deliberately?
- Can they see why it returned?
- Can they settle it after new evidence arrives?
- Can they distinguish "someone replied" from "the record question is resolved"?

These should be tested as complete multi-visit workflows, not only as successful creation of a deferral or plan.

# 7. Follow-up is readable, but not yet operable end to end

## The request/response backend still has no production writer

`outgoing_requests.py` states this explicitly:

> No production module calls `retain_outgoing_request` or `record_outgoing_request_response` yet.

It also says a test pins that absence. Therefore, closing #652 did not make no-response tracking usable by an ordinary coordinator. It supplied the persistence operations.

The current Follow-up screen remains read-only and says it cannot send, close, or reassign a plan.

### Smallest necessary UI

Add to a follow-up bundle:

| Action | What it records |
|---|---|
| **Record sent** | What was actually sent externally, by whom, when, to whom, and the explicit expected-response date. |
| **Record response** | The received response or attributable observation, linked to the request. |
| **Return to the coordination question** | The existing question and evidence that may now be answerable. |
| **Update or cancel the follow-up** | An attributable change through the correct plan lifecycle. |

**You do not need to build email sending to do this.** A person can send from Outlook and record the sent message. The current "we are waiting for a sending system" explanation is not a reason to leave manual recording unreachable.

## There is also a backend-contract discrepancy to settle

The accepted #652 decision describes one request advancing **one or more plans**, and responses retaining evidence and whether the reply is complete, partial, or an acknowledgement.

The implementation currently accepts a singular `follow_up_plan_id`; its response operation records a date and recorder, without that richer response linkage.

Before adding forms, explicitly choose:

- finish the accepted multi-plan/evidence-linked contract; or
- narrow the first release's contract and ensure a bundle cannot imply it recorded communication for several plans when it recorded only one.

That is not merely a missing screen.

## Contact correction has an API but no visible editor

There is an admitted JSON endpoint for correcting a project contact. It expects a structured contact, reason, and idempotency key. I did not find a corresponding visible correction form in the Follow-up screen.

A small contact correction drawer is appropriate when exact contacts are part of the chosen workflow. A CRM is not.

Role-only follow-up can remain valid. But when a contact is shown and wrong, the user needs an attributable way to correct it.

# 8. Configuration and permissions need user-facing readbacks

## Issue configuration exists only below the UI

`register_issue_profile` has the domain implementation and test/setup callers, but no customer-facing configuration workflow in the inspected app.

This configuration controls what the customer receives and which questions can block issue. It should not be invisible operational magic.

The minimum screen should let the appropriate person review:

- the mandatory UCM;
- optional configured artifacts;
- approved template and mapping;
- source-coverage expectations;
- explicit pre-issue decision policies;
- effective configuration;
- previous versions.

Operations may prepare the configuration. Material approval should remain attributable to the designated person.

Do not make this a report builder or a weekly artifact picker.

## Template and mapping replacement has the same gap

`register_baseline_format` supports a replacement output template or mapping without re-adopting the baseline. It validates authority and retains template bytes. But there is no corresponding current user workflow.

Without one, a customer changing their workbook format can strand the project behind a stale or incompatible template with no visible repair path.

The appropriate split is:

> Operations prepares and validates the replacement, the coordinator reviews material semantic changes, the version is registered, affected candidates become stale, and a new candidate is prepared.

## UI permission feedback is not a competing security boundary

The Issue view intentionally avoids checking the releaser designation before offering the approval action, because PostgreSQL is authoritative. It explains the database mechanics next to the button.

That is overly literal.

The database must continue enforcing authorization. But the UI can read the **same effective capability** and tell a coordinator:

> "Ready for approval by the designated releaser."

A derived capability display is not a second security authority. It prevents predictable failed clicks and makes the handoff understandable.

Also explicitly settle who may confirm source coverage and request preparation. The current preparation route checks project access but does not require the coordination designation. That may be intentional; it needs to match the pilot's role contract, not remain an accidental difference from Review.

# 9. Cross-cutting navigation and recovery gaps

These are smaller than intake and downloads, but together they determine whether someone can use the product without guidance.

| Area | Finding or missing proof | Required treatment |
|---|---|---|
| **New-project landing** | The adopted workflow assumes adoption already happened; legacy project behavior is refused under the pilot boundary. | Show onboarding status and the next permitted action instead of an apparent missing project. |
| **Navigation** | Work links to source/history, but lacks a consistent project/portfolio/account shell. | Provide Projects, current project, Sources, Work, Record, and account/sign-out navigation consistently. |
| **Preparation progress** | The no-script page says preparation is happening but offers no clear completion notification or refresh control. | A visible Refresh/status action is sufficient initially; polling is optional. |
| **Failure recovery** | Some blockers explain why an issue cannot proceed but not how the user or operations can repair it. | Every blocking state needs an owner and next action. |
| **Cutoff** | The workflow uses the current supplied clock and hidden cutoff values; no clear reporting-cutoff selection is exposed. | Make the effective cutoff visible and define whether the user may change it before preparation. |
| **Session expiry** | Authentication can return a refusal while the user is in a task. | Test expiry after form entry; provide safe sign-in recovery without silently losing or applying stale input. |
| **Bulk selection** | Button counts are server-rendered while checkbox selection changes locally. | Avoid displaying a stale count as the number that will be submitted, or update it accessibly. |
| **Technical detail** | Digests, renderer identities, database terminology, and internal reasons dominate some task screens. | Keep project meaning primary; put technical proof in expandable details. |
| **History completeness** | Investigation views have intentional result limits. | Make limits visible and provide a reliable way to reach older relevant evidence rather than implying the first page is complete. |

The Work and Review templates substantiate the navigation, no-script, count, technical-detail, and preparation observations. These are UX recommendations based on the inspected implementation, not claims that I conducted a usability test.

# 10. The pilot contract needs reconciliation before measuring the product

This is separate from frontend work, but it affects what "complete" means.

## The contract still describes a different slice in several places

The current pilot-success document still requires:

- connecting a mailbox or folder;
- returning the fixed four artifacts;
- excluding Recorded Verbal Statements until #512 ships.

The current product direction instead uses a configured issue set, and previous development has moved past the cited verbal-origin prerequisite. The contract's pinned-parameter table also lacks explicit issue-profile identity/version.

**Decide whether the measured pilot genuinely requires a connected channel.** A public-data manual-upload rehearsal is perfectly reasonable, but it must not be reported as satisfying a contract that requires mailbox/folder capture and coverage.

Update the contract once, rather than allowing separate issues to interpret different definitions.

## Some promised material-field strata lack a baseline representation

The pilot calls out agreement/permit status and cost responsibility as material-field strata. `baseline_workbook.py` explicitly comments that these have no canonical column and therefore are absent from its material mapping set.

Retaining such columns as unknown content is useful, but **retaining a column is not the same as maintaining its meaning through accepted facts and deltas**.

For each selected partner, classify every material field as:

- fully supported;
- retained unchanged but not semantically maintained;
- unsupported and excluded from the declared slice.

Do not count unsupported semantics as a passed accuracy result.

## Human measurement inputs have no visible collection workflow

The backend accepts work observations, usefulness judgments, baseline samples, material-miss reviews, and artifact-repair observations. Those are deliberately explicit inputs rather than inferred from idle browser time.

The current Review forms do not expose the pilot's required contemporaneous usefulness and reconstruction-time inputs. The pilot requires these judgments at triage, not retrospective reconstruction.

You need either:

- small in-product measurement controls enabled for the pilot; or
- a predeclared, contemporaneous observer/worksheet process whose data enters the existing measurement collector.

Do not build a dashboard first and discover afterward that its required human inputs were never collected.

Also reconcile the packet-precision denominator across the pilot contract and the measurement/report issues before the first measured week. The contract specifically uses **interrupting packets**, not all surfaced packets.

# 11. Which backend controls actually need a frontend?

Not every backend function deserves a screen. This is the division I recommend.

| Capability | Customer-facing UI needed? | Minimum adequate solution |
|---|---|---|
| Customer environment creation | **Not necessarily** | Audited staff procedure and visible provisioning status. |
| Project creation and initial roster | **Not necessarily for pilot** | Staff-assisted setup with named owner and verified access. |
| Baseline workbook mechanics | **Operations-facing** | Diagnostic workspace or documented operator tool. |
| Material baseline approval | **Yes** | Coordinator preview and one attributable Adopt action. |
| Issue-profile approval | **Yes, or explicit assisted approval flow** | Readable configuration summary and versioned approval. |
| Routine document submission | **Yes for manual-upload scope** | Supported upload/confirmation flow. |
| Connector configuration and secrets | **Operations-facing** | Safe connection setup, test, status, and credential custody. |
| Source history and evidence inspection | **Yes** | Unified source register and exact-source viewer. |
| Proposed Delta resolution | **Yes; mostly exists** | Complete the unresolved-question and correction paths. |
| Packet reversal | **Yes** | Bounded Undo linked from the decision result/history. |
| Follow-up recording | **Yes when included in pilot** | Record sent, record response, and return to question. |
| Contact correction | **Yes when exact contacts are used** | Small attributable editor. |
| Candidate preparation | **Yes; exists but needs repair** | Correct authenticated form and progress/recovery. |
| Candidate inspection | **Yes** | Actual artifacts, not just digests. |
| Release authorization | **Yes; exists but needs repair** | Correct authenticated action and capability readback. |
| Authorized artifact retrieval | **Yes** | Exact downloads and package history. |
| Deployment, restore, retention, destruction | **Not necessarily customer UI** | Audited operations procedures with receipts and approval. |
| Pilot usefulness/time observations | **A collection interface is required** | Minimal pilot controls or declared contemporaneous external process. |
| AI configuration, policy qualification, schema migration | **No ordinary-user controls** | Restricted operations workflow. |

This keeps the product focused. The answer is not "expose all commands in an admin panel."

# 12. The minimum screen set still needed

I would organize the work into **seven cohesive surfaces**, reusing existing components.

### 1. Project onboarding

Project status, current baseline submission, operations-readiness status, material coordinator questions, Adopt action, and the approved issue configuration.

### 2. Sources

Upload, delivery history, revision/rendition identity, processing state, row accounting, pending source questions, and exact-source access.

### 3. Source viewer

A shared viewer used from Review, Record, Follow-up, and Sources. It shows retained original context and the exact citation.

### 4. Review completion controls

Not a new Review page. Add unresolved-source-question actions, correction routing, decision receipts, Undo, and the return path from deferral/coordination.

### 5. Follow-up actions

Extend the existing page with recording sent/received correspondence and returning to the underlying question. Keep email sending out of scope.

### 6. Issue inspection and retrieval

Extend the existing Issue section with candidate preview/download, authorized download, release history, proper progress/retry, and accurate approval-versus-sending language.

### 7. Operations readiness and repair

A staff-facing workflow or initial runbook-backed screen covering mapping failures, connection failures, quarantines, stuck processing, configuration changes, and repair receipts.

A consistent navigation shell should connect these. None requires a React rewrite, a generic task system, or a broad new design-system project.

# 13. What to do before the public-data rehearsal

**Do not postpone all testing until all of this is polished. But do not call the current preconfigured-fixture test a complete customer journey.**

I would use three stages.

## Stage A: remove the hard path blockers

| Work package | Completion proof |
|---|---|
| **Authenticated form correctness** | Real synthetic sign-in, then Review, Prepare, and Approve submissions succeed with browser-provided tokens; missing/invalid tokens refuse. |
| **Supported intake boundary** | Selected upload/machine routes work under the actual pilot boundary and correct database roles. |
| **Manual delivery and revision dispatch** | Upload creates a durable delivery and reaches the dedicated registered-UCM revision path. |
| **Baseline and profile onboarding** | A previously unadopted project becomes adopted/configured through the declared operations/coordinator workflow. |
| **Artifact inspection and retrieval** | User inspects the candidate and downloads the exact authorized files through the enabled UI. |

These are the highest-priority issues. They directly determine whether the promised demonstration can happen.

## Stage B: close the normal human loops

Finish source-question actions, source context, correction/Undo, follow-up recording where in scope, clear preparation recovery, permission readbacks, and navigation.

Then have someone who did not build the feature perform the workflow. Record every place they need an explanation, hidden URL, command, or engineer intervention.

## Stage C: test the actual pilot operating model

Run deployment, customer routing, project isolation, restore, operational repair, measurement collection, accessibility, and the selected source-channel behavior.

Staff-assisted setup remains acceptable, but it must be explicit and measured. Hidden maintenance scripts and direct database edits after handoff are not an acceptable substitute for a user workflow.

# 14. The end-to-end test must start earlier and finish later

The next acceptance test should begin with:

> **A provisioned but unadopted project and a person who has not yet signed in.**

It should end with:

> **That person retrieves the exact approved workbook/package, then returns for the next reporting cycle.**

Between those points, require:

```text
Sign in
→ Find the project and its onboarding state
→ Supply the baseline
→ Operations validates mechanics
→ Coordinator resolves material questions and adopts
→ Confirm the issue configuration
→ Submit a later UCM
→ See receipt and processing status
→ Inspect exact source context
→ Review routine changes and a genuine unresolved question
→ Undo one mistake
→ Record a follow-up and response, if included
→ Resolve or explicitly defer the remaining question
→ Prepare
→ Inspect actual artifacts
→ Approve as the designated releaser
→ Download exact authorized output
→ Process another revision
→ Retrieve the earlier package unchanged
```

Use real session authentication, the enforced pilot route boundary, real database capabilities, and the production worker path. A captured test email sender and controlled clock are reasonable test seams. Replacing the human-authentication dependency, pre-adopting the baseline in a fixture, or retrieving final bytes directly through Python skips the very gaps this exercise must detect.

Add architectural checks that prevent recurrence:

- every internal link/form in a supported screen resolves to an admitted route;
- every authenticated POST form supplies CSRF;
- every item labelled "needs action" has a reachable action or explicit accountable handoff;
- every claimed user workflow names its production writer and reader;
- every output labelled approved can be retrieved through the product;
- backend completion and user-workflow completion are recorded separately.

# Final assessment

**I agree that you are missing more than an end-to-end test. You are missing several end-to-end integrations and user controls that the test would immediately expose.**

The strongest evidence is not subjective UX preference:

- the intake routes are outside the enforced pilot boundary;
- adoption and profile registration are not exposed to the coordinator;
- later-revision processing is not visibly connected to ordinary intake;
- the Issue forms omit the authentication token their dependency requires;
- package bytes cannot be previewed/downloaded from the current supported workflow;
- correspondence writers explicitly have no production callers;
- some work is presented without a way to complete it.

At the same time, **you do not need to rebuild the backend or invent a broader utility-management product**. Much of the hard domain work exists. The next milestone should be:

> **Make one complete customer journey operable under the real pilot security configuration, from an unadopted project and original documents to an inspected, approved, downloadable issue.**

Once that works without hidden engineering intervention, the public SR 29/SH 99 rehearsal becomes a meaningful test of the assembled product rather than a demonstration of pre-seeded backend state.
