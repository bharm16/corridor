# Corridor roadmap

**Effective 2026-09-02.** Replaces the Phase 1 roadmap, now historical at
[docs/history/phase-1-roadmap-2026-08.md](docs/history/phase-1-roadmap-2026-08.md).
The product is [ADR-0075](docs/adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md)
as corrected by [ADR-0083](docs/adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md);
the mechanism is [ADR-0076](docs/adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md),
with [ADR-0084](docs/adr/0084-deferral-is-scheduling-and-adopted-projects-read-the-spine-natively.md),
[ADR-0085](docs/adr/0085-the-adopted-project-work-list-is-a-derived-reading-of-adaptive-review-packets.md),
and [ADR-0086](docs/adr/0086-one-authorized-release-package-is-the-external-issue-unit.md)
carrying the review and release shape. The competitive review of 2026-09-02
([docs/research/competitive-review-2026-09-02.md](docs/research/competitive-review-2026-09-02.md))
positions Corridor as the reconciliation and weekly-close layer beside
URMS, KURTS, UTrak, Delasoft, and document control, not a replacement for
them; its ticket consequences are folded in below.
The program issue is #459. The measurement contract is
[docs/pilot-success-criteria.md](docs/pilot-success-criteria.md).

**Goal:** one paid vertical slice, measured with two design partners.

> Import one customer UCM → adopt one exact baseline → observe one new source → produce one or more Proposed Deltas → resolve them → return the customer's updated UCM, change summary, chase list, and weekly report → measure net customer and Corridor operations time.

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
| **Accepted-authority follow-up** — chase work derives from accepted decisions, not free text | #425 |
| **Complete customer release** — the configured artifact set of ADR-0086, sealed and authorized as one unit | #495, #534, #529, #533; the set itself is decided by #560 |
| **Live-data activation gates** — real customer data is admitted only behind them | #561, #564, #535 in that order, and the gates #535 owns |
| **Measurement readiness** — instrumentation exists before the first measured week | #491, #558, #532 |

Two consequences follow, and they are narrow on purpose:

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
- **The release artifact set is ADR-0086's until #560 decides otherwise.**
  #555 observes which artifacts each partner already issues; #560 records
  whether the fixed four-artifact slice stays (and partners are selected for
  it) or the UCM becomes mandatory with the rest a configured set. No ticket
  or roadmap edit changes the set ahead of that decision and its successor
  ADR. Corridor does not manufacture an artifact the partner did not issue
  before adoption, whichever way it goes.

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

## Phase 1 — secure the design partner and deploy the safe shell

**Required for pilot entry** unless marked otherwise.

1. #428 buyer, budget, sponsor, procurement, and pricing validation. *(Not a
   code gate; it decides whether there is a pilot to run.)* #555 observes and
   prototype-tests real weekly closes before the production screens lock;
   #556 records the primary-source incumbent evidence now, so #486's
   synthesis in Phase 3 waits only on the interview findings. #560 decides
   the release artifact contract from #555's observations.
2. #492 least-privileged database write authority — the first substantive code
   PR. Separate deployment credentials, NOLOGIN function owners, no PUBLIC
   execute, command-only appends; the database itself refuses application-role
   writes to accepted authority.
3. #487 storage interface and object storage; #491 (#491A) operational
   telemetry; then #489 synthetic environment foundation. No customer data,
   so #490 staged untrusted-intake hardening does not gate #489; it gates
   #561, the first lane that receives partner bytes. #558 lands the analytics
   event contract here so every later primitive emits its events as it ships.
4. #496 PullConnector and normalized SourceEnvelope; #511 project-bound push
   intake, built only for channels the partner needs.
5. #488 pilot-critical Due Work handlers: connector polling, delta generation,
   report preparation, retention sweep.
6. Live-activation gates, all owned by #535: #461 PyMuPDF licensing, #522
   customer authorization and data handling, #557 model-provider posture,
   #503 → #531 identity, authorization, and deprovisioning, #514
   customer-environment disposition.
7. #561 compatibility intake on the partner's real workbook, behind #522's
   customer-authorization half, #487, and #490 only. It does not wait for
   #489 or the accepted-record work.

**Exit:** #489 synthetic environment with one customer database and one
restored backup; intake hardened; #535's gates identified and tracked; one
partner's connector registered; #561's compatibility report recorded for the
partner's workbook family.

## Phase 2 — build the paid vertical slice on the spine

**Required for pilot entry**, and **included in the measured cohort** once
shipped. Non-authoritative #509 parsing, preview, and fixtures may proceed
earlier.

1. #530 Support Assessment relation (ADR-0082), on #492.
2. #446 replayable Source Fact materialization — a model literal never becomes
   a Source Fact.
3. #518 (#510A) Proposed Delta identity, groups, and lifecycle.
4. #520 (#510C) adopted-baseline operating mode and database refusal, **before
   #509**.
5. #509 Adopt Baseline, invoking the #520 transition atomically.
6. #519 (#510B) typed Resolve Delta commands. Accept, edit, and reject are
   semantic; defer is Work List scheduling (ADR-0084).
7. #494 shared query and exactly-once partition shell (no interim
   one-card-per-delta inbox); #526 (#510D) atomic packet resolution; #559
   accessible packet and workflow primitives; then #527 source-revision
   packet as the first review screen and #528 cross-source coordination
   packet (ADR-0085). External-system identifiers deep-link read-only from
   #509, #527, and #528; a map waits for partner data.
8. #425 accepted-authority follow-up and chase list, addressed to a resolved
   contact where #562 supplies one and to the responsible role otherwise.
   #562 is parallel unless the partner requires an exact address.
9. #564 shadow processing on the partner's captured sources once #561, #489,
   #509, #518, #446, and the partner's ingress and source class exist; it
   feeds #499 with real inputs before authoritative activation.
10. #495 UCM export, #534 change summary and weekly report, #529 release
    candidate, #533 release authorization — all from one frozen revision
    (ADR-0086). Delivery to the partner's document system is #563, separate
    from authorization and partner-triggered.
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

Recorded Verbal Statements are outside the gated pilot source population until
#512 ships; they remain available as legacy-compatible context.

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

1. #512 spine-native verbal and source origin (stage 1).
2. #513 historical backfill preserving original authorship (stage 2).
3. #457 permanent-state dedup.
4. Coverage-aware semantic-equivalence gate and reader switch (stages 3, 4).
5. #458 writer switch, bounded shadow comparison, rollback decision, legacy
   retirement (stages 5, 6).

**Exit:** ADR-0074 marked superseded by ADR-0081.

## Parallel development permitted

None of the following waits for the pilot, and none of it is measured by the
pilot. It may run before, during, or after the measurement window, subject
only to the change-control rule in the measurement contract when it touches a
cohort surface.

- **Reliability and performance** work anywhere in the pipeline.
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
- **Migration** — all of Phase 4 (#512, #513, #457, #458).
- **Optional output formats** beyond the four released artifacts, where a
  customer configures them (ADR-0086 keeps participation configurable).
- **Deeper Record views** behind the Work List (ADR-0085 keeps the full
  Project Record one step away).
- **Future-feature prototypes behind boundaries** — flags, branches, or
  non-authoritative environments, provided no adopted project's accepted
  record is reachable.
- #493 Source Passage Check presentation and locator validation (ADR-0082).

## Pilot-informed: build behind a boundary, enable on evidence

ADR-0075 stops *expansion* of the following until the baseline-plus-delta
pilot is **running** — not until it returns a verdict. The earlier version of
this roadmap escalated that into "nothing is built until the pilot gate
returns a decision", which was stricter than the decision it implements. The
accurate rule is that these are **excluded from the measured cohort**, and
their broad enablement uses pilot evidence:

- the universal documentation-readiness system (ADR-0052, ADR-0056, ADR-0060);
- the generalized task-management surface (ADR-0035, as amended by ADR-0085
  for adopted projects);
- phone and offline functionality;
- broad notification and escalation machinery;
- additional automatic Record Inclusion classes;
- global content-inferred email routing;
- new legacy-table capabilities (ADR-0081);
- report formats beyond the customer's UCM and weekly artifact;
- the optional Statement Review Assistant display program;
- the multi-engine PDF extraction platform; and
- record-generated coordination paperwork.

Those ADRs keep their status; their scope stays `optional module` in the
decision index. Customer-format export of the updated UCM and one real
connector are **not** in this list — ADR-0083 puts them in the slice.

## Required for broader rollout

Needed only before a second cohort or general availability, and tracked when
the checkpoint says to proceed:

- #503's enterprise half — company single sign-on, org administration, and
  role management beyond the pilot's identity needs (#531 covers the pilot).
- Capacity and multi-tenant performance beyond two partners.
- A formal security review of the customer-facing surface.
- Connector breadth beyond the partners' own channels.

## The checkpoint

#498 records one finding per criterion and one outcome: **continue**,
**revise**, **extend evidence**, **narrow**, **delay broader rollout**, or
**keep higher-risk features disabled**. A failed criterion gates claims,
cohort expansion, rollout, or feature enablement. It does not stop unrelated
engineering.
