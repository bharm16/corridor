# Corridor roadmap

**Effective 2026-09-03.** Replaces the Phase 1 roadmap, now historical at
[docs/history/phase-1-roadmap-2026-08.md](docs/history/phase-1-roadmap-2026-08.md).
The product is [ADR-0075](docs/adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md)
as corrected by [ADR-0083](docs/adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md);
the mechanism is [ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md),
with [ADR-0084](docs/adr/0084-deferral-is-scheduling-and-adopted-projects-read-the-spine-natively.md),
[ADR-0085](docs/adr/0085-the-adopted-project-work-list-is-a-derived-reading-of-adaptive-review-packets.md),
and [ADR-0086](docs/adr/0086-one-authorized-release-package-is-the-external-issue-unit.md)
carrying the review and release shape. Three decisions of 2026-09-03 amend
that set: [ADR-0089](docs/adr/0089-a-delivery-is-persisted-once-whatever-transport-carried-it.md)
persists one delivery family for pull and push alike,
[ADR-0090](docs/adr/0090-the-accepted-record-has-its-own-alerts-not-the-legacy-task-systems.md)
fixes which Constraint Alert rules the accepted record keeps, and
[ADR-0091](docs/adr/0091-the-externally-issued-set-is-configured-per-project-and-only-the-ucm-is-mandatory.md)
makes the externally issued artifact set per-project configuration with only
the updated UCM mandatory. The competitive review of 2026-09-02
([docs/research/competitive-review-2026-09-02.md](docs/research/competitive-review-2026-09-02.md))
positions Corridor as the reconciliation and weekly-close layer beside
URMS, KURTS, UTrak, Delasoft, and document control, not a replacement for
them; its ticket consequences are folded in below.
The program issue is #459. The measurement contract is
[docs/pilot-success-criteria.md](docs/pilot-success-criteria.md).

**Goal:** one paid vertical slice, measured with two design partners.

> Import one customer UCM → adopt one exact baseline → observe one new source → produce one or more Proposed Deltas → resolve them → return the customer's updated UCM, plus whichever of the change summary, chase list and weekly report that project is configured to issue → measure net customer and Corridor operations time.

## Rules

1. Every new product capability writes the spine first. A compatibility write
   may keep a legacy reader working during migration; no capability may exist
   only in legacy tables (ADR-0081 freeze).
2. **The measured pilot is a project checkpoint, not a project-wide stop
   condition.** It governs what Corridor may *claim*, which customers it may
   *expand to*, whether it may *roll out more broadly*, and whether
   higher-risk or generalized capabilities may be *enabled*. It does not
   decide whether ordinary Corridor development continues. Work is classified
   below; only the pilot-entry class blocks starting the pilot.
3. Each phase's exit is the listed issue set closed, not a date.
4. Issue numbers are the authority for scope; this file orders and classifies
   them. #459 tracks the same set as an issue hierarchy and is not a second,
   editable backlog — when the two disagree, the issues are right and this
   file is stale.

## How work is classified

Every open issue falls in exactly one class.

| Class | Meaning | Blocks the pilot starting? | In the measured cohort? |
|---|---|---|---|
| **Required for pilot entry** | A live measured pilot cannot safely or meaningfully start without it. | Yes | Usually |
| **Included in the measured pilot** | The coordinator-facing behavior the measurement is *about*. Changing it mid-pilot creates a measurement boundary. | — | Yes |
| **Parallel development permitted** | May proceed before, during, or after the pilot. Not measured, and not gated on the checkpoint. | No | No |
| **Pilot-informed** | May be designed, prototyped, and built behind a boundary; *final enablement* waits for pilot evidence. | No | No |
| **Required for broader rollout** | Needed only before a second cohort or general availability. | No | No |

### What pilot entry protects

The pilot-entry boundary exists for five reasons, and nothing outside them
belongs in it: **accepted-record safety**, **live customer data**, **a
complete measured workflow**, **release integrity**, and **interpretable
instrumentation**.

Pilot entry requires all seven of the following. Nothing else may be added to
this list without a recorded reason:

| Pilot-entry requirement | Issues |
|---|---|
| **Accepted-record boundary** — the database refuses application-role writes to accepted authority | #492, #530, #446 |
| **Adopted-baseline operating mode** — no legacy path silently replaces an accepted value | #520, #509 |
| **Adaptive review, packet resolution, and one linear workflow** — the coordinator can review and resolve real volume, and the week's work is one path | #518, #519, #494, #559, #526, #527, #528, #536, #537 |
| **Accepted-authority follow-up** — chase work derives from accepted decisions, not free text | #425 done; #658 renders it, #652 retains the correspondence that makes silence a fact |
| **Complete customer release** — the artifact set each project is configured to issue (ADR-0086 as amended by ADR-0091), sealed and authorized as one unit | #495, #534, #596, #597 done; #640 models the configured set, then #529 and #533 |
| **Live-data activation gates** — real customer data is admitted only behind them | #561, #564, #535 in that order, and the gates #535 owns |
| **Measurement readiness** — instrumentation exists before the first measured week | #491, #558, #532 |

Three consequences follow, and they are narrow on purpose:

- **#520 (adopted-baseline operating mode) is a hard pilot-entry safety
  requirement**, because a project whose legacy paths can still silently
  replace an accepted value is not safe to measure or to show a customer. It
  is *not* a blocker on all project work — reliability, connector,
  operations, template, migration, and accessibility work proceed while it is
  open.
- **Live-data activation is three receipted stages, and #535 is the last.**
  #561 admits one partner's real workbook bytes to an isolated lane for
  deterministic, no-model compatibility parsing that creates no Source Fact,
  Proposed Delta, or accepted-record state. #564 runs captured sources through
  a shadow lane that produces frozen, non-authoritative Proposed Deltas
  against a shadow baseline with no record-decision or release credential.
  #535 is authoritative measured activation behind every gate. Pre-activation
  processing stays prohibited outside those two lanes. None of the three
  gates synthetic staging, development against fixtures, or parallel work.
- **The externally issued set is per-project configuration, and only the
  updated UCM is mandatory.** #560 is decided and recorded as ADR-0091: the
  change summary, chase list, weekly Coordination Report, and any sidecar are
  issued when the project is configured to issue them, reflecting what that
  partner already sends their own client. The set is fixed until an
  attributable configuration change; the coordinator never assembles an issue
  week by week, and a set change is a material change under the measurement
  contract. Corridor does not manufacture an artifact the partner did not
  issue before adoption, and cannot claim time saved on one. #555 still
  determines each partner's actual configured set, templates, approval path,
  and delivery destination.

## Open human decisions

The program map in #459 carries the same list, and these are the questions no
agent can settle. Everything else on the board is buildable work.

- **#428 — buyer, budget, sponsor, procurement, and pricing.** This is
  evidence work, not a choice waiting to be made: interviews and a synthesis
  in #486 decide whether there is a pilot to run and what may be claimed.
- **#461 — PyMuPDF licensing.** The option is chosen: the commercial Artifex
  licence, because PyMuPDF runs through ingest, page inventory, source
  segmentation, geometry, verification, rendering, and the review surfaces,
  and a hurried replacement would carry document-fidelity risk into the pilot.
  What remains is **procurement** — written terms requested, then signed — so
  the ticket closes on the signed licence, not on the choice. Until it is
  held, no live customer PDF processing is enabled, and a first pilot may be
  scoped to XLSX and other non-PDF sources; #606's later-UCM path is exactly
  that shape. It gates #489 and #535.
- **#601 — the nonproduction AWS account and GitHub deployment identity.**
  Account creation, root MFA, a billing budget, and an OIDC role are acts only
  the account owner can perform. #489 waits on it.
- **#555 — each partner's actual configured issue set**, now that ADR-0091 has
  fixed the architecture: which artifacts each partner already issues, their
  templates, the approval path inside the partner's organization, and the
  delivery destination.

**Closed by decision, and no longer open questions:** #503 identity and
authorization (settled, and #531 has now *implemented* the pilot half), #557
model-provider processing posture (the approved posture is recorded), and
#560 the release artifact contract (recorded as ADR-0091, and #640 models it).

**The release chain, in order:** #640 issue profile → #641 consequence levels
and #529 candidate → #533 authorization → #636 portfolio readiness → finish
#536 → finish #537 → #532 measurement. That sequence completes the paid
vertical slice; the one-unreleased-edge window means its migration-bearing
steps run one at a time.

## Phase 0 — correct the constitution

Complete.

1. #505 ADR-0082 supersedes ADR-0077; #507 roadmap and measurement contract
   published; #508 glossary entries for Source Fact, Adopt Baseline, Proposed
   Delta, Resolve Delta; #516 main restored to green.
2. #506 ADR index authority semantics and lifecycle validation fixed;
   `make check` runs as a standalone status. Server-side enforcement needs a
   ruleset this private free-plan repository cannot create; the manual
   all-green merge rule in AGENTS.md applies instead.
3. ADR-0083 recorded the ADR-0078/0080/0081 corrections; ADR-0084 recorded the
   late contract review's three corrections.
4. #524 and #525 recorded the review and release decisions as ADR-0085 and
   ADR-0086.
5. ADR-0087 and ADR-0088 fixed the migration window and the feedback budget as
   enforced numbers and settled that the required gate runs the whole suite in
   parallel. ADR-0089, ADR-0090, and ADR-0091 recorded the 2026-09-03
   decisions on #599, #596, and #560.

## Phase 1 — secure the design partner and deploy the safe shell

**Required for pilot entry** unless marked otherwise.

1. #428 buyer, budget, sponsor, procurement, and pricing validation. *(Not a
   code gate; it decides whether there is a pilot to run.)* #555 observes and
   prototype-tests real weekly closes before the production screens lock;
   #556 recorded the primary-source incumbent evidence in
   [docs/research/adr-0075-incumbent-evidence-2026-09-02.md](docs/research/adr-0075-incumbent-evidence-2026-09-02.md),
   so #486's synthesis in Phase 3 waits only on the interview findings. #560
   is decided and recorded as ADR-0091; #555 now fills in each partner's
   configured set rather than choosing the contract.
2. #492 least-privileged database write authority — **done**. Separate
   deployment credentials, NOLOGIN function owners, no PUBLIC execute,
   command-only appends; the database itself refuses application-role writes
   to accepted authority.
3. #487 storage interface and object storage and #491 (#491A) operational
   telemetry are **done**, and #558 landed the analytics event contract so
   every later primitive emits its events as it ships. #490 staged
   untrusted-intake hardening is **done**; it gates #561, the first lane that
   receives partner bytes, and never gated #489. #489 synthetic environment
   foundation is open and now waits on **#601**, the human ticket that
   provisions the dedicated nonproduction AWS account, the billing budget and
   alarms, the recorded region, and the GitHub OIDC deployment identity —
   agents cannot create those.
4. #496 PullConnector and normalized SourceEnvelope and #511 project-bound
   push intake are **done** for the channels the partner needs. #599 is
   **done**: one `source_deliveries` family persists both transports under
   ADR-0089, the connector cursor lives in its own append-only
   `connector_checkpoint_advances` relation rather than on a Due Work receipt,
   and a refused or failed delivery is recorded with its digest and its reason
   instead of being lost. It no longer blocks #535 for a pull connector.
5. #488 pilot-critical Due Work handlers — **done**: connector polling, delta
   generation, report preparation, retention sweep. Its cursor-on-the-receipt
   design was superseded by ADR-0089 and replaced in #599.
6. Live-activation gates, all owned by #535: **#461** PyMuPDF commercial
   licence procurement, #522 customer authorization and data handling, #514
   customer-environment disposition, and #531 pilot identity, authorization,
   and deprovisioning. #503 and #557 are **closed by decision** — the identity
   scope is settled with #531 carrying the pilot half, and the approved
   model-provider processing posture is recorded.
7. #561 compatibility intake on the partner's real workbook, behind #522's
   customer-authorization half, #487, and #490 only. It does not wait for
   #489 or the accepted-record work.

**Exit:** #489 synthetic environment with one customer database and one
restored backup, on the account #601 provisions; intake hardened; #535's gates
identified and tracked; one partner's connector registered and its deliveries
persisted (#599, done); #561's compatibility report recorded for the partner's
workbook family.

## Phase 2 — build the paid vertical slice on the spine

**Required for pilot entry**, and **included in the measured cohort** once
shipped. Non-authoritative #509 parsing, preview, and fixtures may proceed
earlier.

1. #530 Support Assessment relation (ADR-0082), on #492 — **done**.
2. #446 replayable Source Fact materialization — **done**; a model literal
   never becomes a Source Fact.
3. #518 (#510A) Proposed Delta identity, groups, and lifecycle — **done**.
4. #520 (#510C) adopted-baseline operating mode and database refusal —
   **done**, and it landed before #509 as required.
5. #509 Adopt Baseline — **done**, invoking the #520 transition atomically.
6. #519 (#510B) typed Resolve Delta commands — **done**. Accept, edit, and
   reject are semantic; defer is Work List scheduling (ADR-0084). #510 closes
   with #518, #519, #520, and #526.
7. #494 shared query and exactly-once partition shell, #526 (#510D) atomic
   packet resolution, and #559 accessible packet and workflow primitives are
   **done**, and so is **#527**, the source-revision packet that is the first
   coordinator review screen: one bounded item per authoritative revision, with
   only the opened item carrying decision controls. **#528** is **done** too:
   `delta-partition-v2` produces all three of ADR-0085's packet keys, splits a
   candidate question whose sources propose materially different actions, and
   names an identity contradiction as the question it is; the focused screen
   answers a cross-source question and a shared commitment child by child,
   committing one act through #526. External-system identifiers deep-link
   read-only from #509, #527, and #528; a map waits for partner data.
8. #425 accepted-authority follow-up and chase list, addressed to a resolved
   contact where #562 supplies one and to the responsible role otherwise.
   #562 is parallel unless the partner requires an exact address. Under
   ADR-0090 #425 also owns the two superseded action-date alerts and the
   source-backed no-response rule that replaces `STALE`; under ADR-0091 the
   follow-up bundles and chase view are core internal behaviour whether or
   not the chase list is externally issued. **#425 is done**, and shipped with
   no screen — **#658** renders the bundles in the linear workflow. Its
   no-response rule is implemented and *unreachable*: Corridor retains no
   outgoing request or expected-response boundary, which **#652** adds.
   **#659** adds the missing path to Needs coordination on a focused
   single-source question.
9. #564 shadow processing on the partner's captured sources once #561, #489,
   #509, #518, #446, and the partner's ingress and source class exist; it
   feeds #499 with real inputs before authoritative activation. **#606** is
   its first and least assumption-heavy source path: capture a later UCM
   revision as Source Facts and Proposed Deltas against the adopted baseline,
   with no model, no Microsoft 365, and no partner mailbox.
10. #495 UCM export and #534 change summary and weekly report are **done**,
    both from one frozen revision (ADR-0086). Three follow-ons remain:
    **#597** and **#596** are **done** — #597 makes the registered mapping
    revision the authority a template is read through, catching a column that
    silently splits or combines a material value, which no digest over the
    template's bytes can; #596 completed the accepted record's own check set
    under ADR-0090, porting `MISSING_EVIDENCE` and `SUPERSEDED_CITATION`,
    declaring the seven rules ADR-0090 supersedes or retires rather than
    dropping them silently, and advancing the declared set to
    `accepted_record_checks_v2`. **#613** is **done**: the maintainer
    approved the replacement `MISSING_EVIDENCE` customer label on
    2026-09-03, and the glossary now carries it. #529 release
    candidate
    and #533 release authorization seal and authorize whichever artifacts the
    project is configured to issue (ADR-0091). Delivery to the partner's
    document system is #563, separate from authorization and
    partner-triggered.
11. #536 linear project workflow; #537 derived weekly portfolio reading,
    required before the measured multi-project cohort, not before #535.
12. #532 (#491B) product and pilot measurement, consuming the events each
    primitive already emits through #558 — instrumentation is in place
    *before* the first measured week because it landed with each primitive.
13. #450 schedule source, #455 email source, #456 minutes source, **only for
    the source classes the partner actually produces**. Other source classes
    are parallel work.

**Exit:** the slice runs end to end on one partner project with the partner's
own workbook and one connected source, and the adopted project runs in
baseline/delta operating mode with no legacy automatic accepted-value update.
The spine lifecycle itself — #509, #518, #519, #520, #526 — is already built;
what remains is the coordinator-facing surface, the release package, and the
first real source path.

Recorded Verbal Statements sat outside the gated pilot source population
"until their spine-native source origin (#512) ships". **#512 has shipped**, so
that condition is met and the exclusion no longer rests on an unbuilt
dependency. Whether verbals enter a given partner's gated source population is
now a per-partner scoping question for #555 and
[docs/pilot-success-criteria.md](docs/pilot-success-criteria.md), whose source-class
row still carries the old conditional wording. ADR-0084 keeps #512 mandatory
before full cutover (ADR-0081 stages 4 through 6) either way.

## Phase 3 — run the measured pilot and reach the checkpoint

1. #424 net economics measurement; #499 shadow comparison against the
   customer's frozen later matrix revisions; #498 checkpoint report.
2. #486 synthesizes the evidence record for ADR-0075 from #556's incumbent
   evidence and #428's interview findings, and qualifies buyer claims.
3. Freeze each baseline and incoming-source event before coordinator action.
4. Measure against [docs/pilot-success-criteria.md](docs/pilot-success-criteria.md),
   whose cohort pinning and measurement-boundary rules apply for the whole
   window.

**Exit:** #498 closed with one finding per criterion and one recorded
checkpoint outcome.

## Phase 4 — finish the canonical cutover (ADR-0081)

**Parallel development permitted.** This is migration work on the legacy
tables; it neither blocks the pilot nor is measured by it.

1. #512 spine-native verbal and source origin (stage 1) — **done**. The
   legacy statement key survives only as a compatibility mapping.
2. #513 historical backfill preserving original authorship (stage 2).
3. #457 permanent-state dedup — **done** on its write-side half: a duplicate
   is now unrepresentable in PostgreSQL across the five permanent-state
   families. **#598** carries the other half, referencing permanent state by
   identity instead of copying it, through four children: **#602** binds
   Report Runs and scheduled publications to a Project Record revision,
   **#603** computes report diffing from revision references and proves it
   semantically equivalent to the snapshot baseline, **#604** references
   decision identities from audit instead of copying before/after field maps,
   and **#605** stores extractor configuration once by digest and cites
   Source Segments for evidence. **All four have landed and #598 is closed**,
   its first criterion amended by ADR-0092: a Report Run *retains the reading
   it published*, so the payload was never a cache to expire. ADR-0089 adds the delivery family that a
   later constraint of the same kind applies to.
4. Coverage-aware semantic-equivalence gate and reader switch (stages 3, 4).
5. #458 writer switch, bounded shadow comparison, rollback decision, legacy
   retirement (stages 5, 6). ADR-0090 keeps the released legacy ruleset
   computing all twelve Constraint Alert rules for legacy projects until this
   completes stage 6.

**Exit:** ADR-0074 marked superseded by ADR-0081.

## Parallel development permitted

None of the following waits for the pilot, and none of it is measured by the
pilot. It may run before, during, or after the measurement window, subject
only to the change-control rule in the measurement contract when it touches a
cohort surface.

- **Reliability and performance** work anywhere in the pipeline, including
  **#595**, which brought the required pull-request gate's median under
  ADR-0087's three minutes without deselecting a test (ADR-0088). The cause was
  not a bad partition: the gate's wall clock is a max over nine jobs, so every
  run sampled the worst setup draw taken in it. Running setup concurrently and
  taking PostgreSQL from the runner image rather than a `services:` container
  put four measured runs at 2m22s–2m53s, a median of 2m34s against 3m14s
  before. #595 stays open only until a fifth run completes its recorded
  median.
- **Additional connectors** beyond the partner's own: #497 Microsoft 365
  SharePoint, OneDrive, and shared project mailbox.
- **Partner-triggered, `needs-triage` until named:** #562 project contact
  resolution, #563 delivery of a sealed release to the partner's document
  system, and read-only spatial context (station, small map, project limits)
  once a partner's data confirms usable coordinates. None blocks the pilot.
- **Operations tooling**, runbooks, and support ergonomics.
- **Template succession** — new customer UCM templates and mapping versions.
- **Accessibility** work on any surface.
- **Deployment and observability** beyond #491A's pilot minimum.
- **Backup, restore, and recovery** hardening.
- **Migration** — all of Phase 4. #512, #457 and #598 (with its children
  #602-#605) are done; #513, #458 and #645 remain.
- **Optional output formats** beyond the artifacts a project is configured to
  issue (ADR-0086 as amended by ADR-0091 keeps participation configurable).
- **Deeper Record views** behind the Work List (ADR-0085 keeps the full
  Project Record one step away).
- **Future-feature prototypes behind boundaries** — flags, branches, or
  non-authoritative environments, provided no adopted project's accepted
  record is reachable.
- #493 Source Passage Check presentation and locator validation (ADR-0082) —
  **done**; **#600** replaces its placeholder Passed / Failed / Not run
  wording with "Found at cited location", "Not found at cited location", and
  "No cited location recorded". Presentation only: no stored identifier,
  authority rule, or lifecycle behaviour changes, so it needs no successor ADR.

## Pilot-informed: build behind a boundary, enable on evidence

ADR-0075 stops *expansion* of the following until the baseline-plus-delta
pilot is **running** — not until it returns a verdict. The earlier version of
this roadmap escalated that into "nothing is built until the pilot gate
returns a decision", which was stricter than the decision it implements. The
accurate rule is that these are **excluded from the measured cohort**, and
their broad enablement uses pilot evidence:

- the universal documentation-readiness system (ADR-0052, ADR-0056, ADR-0060);
- the generalized task-management surface (ADR-0035, as amended by ADR-0085
  for adopted projects; ADR-0090 retires the alerts that demanded an owner and
  a task for every Utility Conflict, and Corridor does not recreate the legacy
  task system);
- phone and offline functionality;
- broad notification and escalation machinery;
- additional automatic Record Inclusion classes;
- global content-inferred email routing;
- new legacy-table capabilities (ADR-0081);
- report formats beyond the customer's UCM and that project's configured
  issue set;
- the optional Statement Review Assistant display program;
- the multi-engine PDF extraction platform; and
- record-generated coordination paperwork.

Those ADRs keep their status; their scope stays `optional module` in the
decision index. Customer-format export of the updated UCM and one real
connector are **not** in this list — ADR-0083 puts them in the slice.

## Required for broader rollout

Needed only before a second cohort or general availability, and tracked when
the checkpoint says to proceed:

- The enterprise half of the identity decision recorded in #503 — company
  single sign-on, org administration, and role management beyond the pilot's
  identity needs (#531 covers the pilot).
- Capacity and multi-tenant performance beyond two partners.
- A formal security review of the customer-facing surface.
- Connector breadth beyond the partners' own channels.

## The checkpoint

#498 records one finding per criterion and one outcome: **continue**,
**revise**, **extend evidence**, **narrow**, **delay broader rollout**, or
**keep higher-risk features disabled**. A failed criterion gates claims,
cohort expansion, rollout, or feature enablement. It does not stop unrelated
engineering.
