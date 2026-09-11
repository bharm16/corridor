# Pilot success criteria

A temporary product-validation contract for the first paid slice, not an
architecture decision. It expires when the pilot closes and its findings are
recorded. The product it validates is
[ADR-0075](adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md)
as corrected by [ADR-0083](adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md);
the mechanism is
[ADR-0076](adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md),
with [ADR-0084](adr/0084-deferral-is-scheduling-and-adopted-projects-read-the-spine-natively.md),
[ADR-0085](adr/0085-the-adopted-project-work-list-is-a-derived-reading-of-adaptive-review-packets.md),
and [ADR-0086](adr/0086-one-authorized-release-package-is-the-external-issue-unit.md)
as amended by
[ADR-0091](adr/0091-the-externally-issued-set-is-configured-per-project-and-only-the-ucm-is-mandatory.md)
defining the review and release surfaces under test.
Rewritten 2026-09-01 after the [realignment review](research/adr-code-open-issue-realignment-review-2026-09-01.md)
so that every criterion is falsifiable, and again 2026-09-02 (#538) so that the
result is a **project checkpoint** rather than a project-wide stop condition.
Reconciled 2026-09-11 (#845) against what is built and what is selected:
ADR-0091's configured issue set replaces the fixed four artifacts, artifacts
are pinned by format and usability rather than by name, Recorded Verbal
Statements are a per-partner selection rather than a wait on an unbuilt
dependency, the issue profile joins the pinned parameters, and the
material-field strata are classified by what the code actually maintains.
Amended 2026-09-11 (#887) so that the packet-precision basis is no longer
selectable: the criterion is evaluated on interrupting packets, the
all-surfaced rate is a printed diagnostic, and "interrupting" is pinned to a
declared presentation rather than to the first reading's consequence level.
The pilot report issue (#498) carries the result.

**What this contract governs.** A failed criterion gates what Corridor may
claim, which customers it may expand to, whether it may roll out more broadly,
and whether higher-risk or generalized capabilities may be enabled. It does
not decide whether ordinary Corridor development continues; [roadmap.md](../roadmap.md)
classifies which work is parallel to the pilot.

## The slice under test

```
Import one customer UCM
→ Adopt one exact baseline
→ Connect at least one project channel — a mailbox, a folder, or another
  project-bound connector
→ Observe new sources
→ Produce Proposed Deltas
→ Review them as packets
→ Resolve them (accept / edit / reject; defer schedules)
→ Return the project's configured issue set (ADR-0091): the updated native UCM
  always, plus the artifacts that project is configured to issue
→ Measure net customer and Corridor operations time
```

### The measured pilot keeps a connected channel; a rehearsal is not the pilot

The third step is a **genuine project-connected channel** on a real project —
a mailbox, a document folder, or another project-bound connector under
[ADR-0078](adr/0078-sources-enter-through-project-bound-connectors-under-one-small-contract.md).
Coverage and source-arrival latency are measured on that channel, and every
partner in the measured cohort has at least one. Nothing else substitutes for
it, because the thing those two criteria measure is a source arriving without
a person carrying it.

**The core public-data rehearsal does not depend on that channel, and is never
reported as it.** The rehearsal of the customer journey on public documents —
the acceptance harness (#848) and the core journey scenario (#849) — proves
that a person can sign in, supply a baseline, adopt, review, prepare, approve
and download. It runs on manual upload on purpose, so that proving the journey
does not wait on a partner's mail system. A manual-upload run is reported as a
manual-upload run: it is not connector coverage, not automated capture, and not
evidence for the coverage or latency criteria, because no part of it observes a
source arriving on its own.

**If the first commercial scope turns out to be manual-upload only**, this
contract's automation and coverage claims are amended before the first partner
is enrolled. That is a recorded change to this file that restates or withdraws
the coverage criterion; it is not a judgment made afterwards while reading the
results.

## The measured cohort, pinned before the first baseline is adopted

Measurement is meaningless if the thing measured drifts. Every value below is
**recorded in #498 before the first measured week begins**, and any change to
one is governed by "Changes during the measurement window".

| Pinned parameter | Recorded value |
|---|---|
| Product revision | the exact commit of `main` deployed to the pilot environment |
| Packetizer rule version | the ADR-0085 packet-keying and consequence-level rule version |
| Source configuration | per project: which connected channels, which source classes, which polling cadence |
| Template and mapping identities | per project: the customer's UCM template identity and the field mapping version (ADR-0086) |
| Issue profile | per project: the configured issue set's profile identity, its version, and the SHA-256 digest of its declaration (ADR-0091). These are the three values the measurement software already pins on every period as `issue_profile_identity`, `issue_profile_version` and `issue_profile_sha256` (`src/corridor/pilot_measurement.py`), together with the artifacts that profile requires and the artifacts the partner already performed before adoption |
| Feature flags | the complete flag state of the pilot environment, including every flag left off |

An unpinned parameter is a reporting defect: if the value cannot be stated,
the affected criterion is reported as unmeasured rather than passed.

## Pilot shape, declared before the first baseline is adopted

| Parameter | Declared value |
|---|---|
| Design partners | at least 2, each with a signed pilot agreement |
| Active projects | at least 4 in total, at least 2 per partner, each with a current UCM |
| Duration | 8 consecutive weeks of live evidence per project, after a 2-week onboarding period that is measured but not gated |
| Source classes | at least the partner's UCM revisions and **one genuine project-connected channel**; minutes and schedule exports if the partner produces them. Recorded Verbal Statements stay **outside the initial gated source population**, and count toward neither coverage nor delta accuracy, unless that partner **explicitly selects** them and the end-to-end capture and review path is supported for what they selected. Technical availability is not selection: the spine-native verbal source origin (#512) has shipped, and whether a built capability enters a measured cohort is a separate decision, recorded here per partner |
| Source events | at least 40 captured source arrivals per partner over the 8 weeks, or the pilot is extended until reached |
| Comparison method | same coordinator, same projects, same weekly artifact; Corridor time logged in the product, partner time logged by the coordinator |

### The configured issue set, and what client-ready means for each artifact

The pilot returns the artifact set each project is **configured** to issue
under [ADR-0091](adr/0091-the-externally-issued-set-is-configured-per-project-and-only-the-ucm-is-mandatory.md),
which amended ADR-0086's fixed four. The updated native UCM is mandatory in
every package. The accepted-change summary, the chase list, the weekly
Coordination Report and any provenance sidecar are per-project configuration,
fixed until an attributable configuration change. Corridor does not manufacture
an artifact the partner did not issue before adoption, and cannot claim time
saved on one.

**Naming an artifact does not pin it.** "The chase list was produced" is
satisfied by a file nobody can send to a client. Each configured artifact is
therefore pinned by two things: the **format** Corridor actually produces, and
the **usability standard** that partner's own obligation sets. The formats
below are what the release module declares today — `ARTIFACT_SUFFIXES` in
`src/corridor/release_candidate.py`, which fixes the extension the sealed bytes
keep in the store:

| Configured artifact | Format Corridor produces today | What must be pinned per partner before measurement |
|---|---|---|
| **Updated native UCM** (mandatory) | `.xlsx` — the customer's own approved template, written through the registered field mapping revision | the template identity and mapping revision, and the statement that the partner's client accepts that workbook as issued |
| Accepted-change summary | `.txt` | whether the partner's obligation is plain text, and if it is not, exactly what format and layout their client receives and who produces it today |
| Weekly Coordination Report | `.txt` | the same, and in particular whether the partner's client expects a PDF or a formatted document, which Corridor does not produce for this artifact |
| Chase list | `.json` | whether a JSON file is usable by whoever receives it, and if not, what that recipient actually receives today |
| Provenance sidecar | **none** — it is a configurable artifact type with no registered renderer contract at any version, so a profile that selects it refuses preparation as an unsupported issue configuration rather than rendering a guess | whether any partner needs one at all; if one does, that is a new build, not a configuration switch |

Three rules follow, and they are narrow on purpose:

- **A downloadable file in Corridor's declared format is not evidence that the
  partner's deliverable is client-ready.** A JSON chase list does not establish
  that an Excel or PDF obligation is met. Where the format Corridor produces
  and the format the partner's client receives differ, the difference is
  recorded, the gap is counted as manual repair outside Corridor, and the
  client-ready package rate is measured against the partner's obligation rather
  than against Corridor's output.
- **The partner's actual obligation is validated, not assumed.** #555 records,
  per partner, which artifacts that partner already issues, the templates they
  follow, the approval path inside the partner's organization, and where each
  is delivered. Until it does, the format rows above are what Corridor
  produces and the obligation column is unfilled.
- **No new format is mandated speculatively.** Nothing here requires a PDF, a
  formatted document, or a sidecar renderer to be built. It requires that where
  one is genuinely needed, the gap is visible before the measurement starts
  rather than discovered as a failed criterion afterwards.

### The pre-adoption baseline is a minimum, not automatically representative

Two weeks of pre-adoption measurement is the **floor**, not the definition of
a valid baseline. A baseline period counts only when it covers **all** of:

- at least one **complete reporting cycle** — whatever the partner's own
  weekly or biweekly issue rhythm is, end to end;
- **ordinary and no-change work**, so the baseline is not composed entirely of
  eventful weeks; and
- at least one **substantive source revision** — a revised UCM, a material
  schedule change, or an equivalent event that forces real record maintenance.

If two weeks do not contain all three, the baseline is **extended** until they
do, or a **matched historical event** is used: the same coordinator's logged
time on a comparable prior revision, identified and agreed before adoption.
A baseline that meets the floor but not the three conditions is reported as
such, and every criterion derived from it is reported as weakly evidenced.

## Pass thresholds

Every threshold is evaluated per partner over weeks 3 through 10 (the 8 live
weeks). A pilot passes only if every criterion passes for both partners.

| Criterion | Pass threshold |
|---|---|
| Net coordinator time | weekly record-maintenance plus report-preparation minutes per active project at most 50% of the pre-adoption baseline |
| Coordinator review burden | median at most 15 minutes per active project per week; 90th percentile at most 30 |
| Corridor operations time | setup, triage, connector maintenance, and support at most 15 minutes per active project per week from week 3 onward |
| Source-arrival to Proposed Delta latency | median at most 24 hours; 90th percentile at most 3 business days |
| Proposed Delta to decision latency | reported; no threshold, because it is the coordinator's cadence |
| Baseline adoption accuracy | a random sample of 100 adopted rows per project (all rows if fewer), every material field checked against the source workbook: at least 99% accuracy where the denominator is **checked populated material fields** (not sampled rows), and zero silently discarded rows or columns |
| Accept-without-edit, routine deltas | at least 80% |
| Accept-without-edit, material-field stratum | at least 70%, reported separately for Utility Owner, conflict identity, Promised For, Required By linkage, closure, Applies To, agreement or permit status, cost responsibility. Each stratum is classified for each partner before measurement, and an unsupported semantic is never a passed result — see "Material-field strata: what is supported, and for whom" |
| Material false writes | zero automatic projections to a material field that a person later reverses as a confirmed policy error. Any occurrence fails that policy-class criterion, and it stays failed even when the pilot continues for diagnosis |
| Material misses | at most 5% of sampled material changes absent from Proposed Deltas |
| Coverage | at least 95% of source arrivals in the connected channel captured within the latency window; the remainder listed by cause |
| False or low-value exceptions | at most 20% of surfaced exceptions judged low-value by the coordinator in weeks 7 through 10, and at least a 30% relative reduction from weeks 3 through 4 |
| **Packet interruption precision** | of the packets Corridor placed at "must handle before this issue" (ADR-0085), at least 80% judged by the coordinator, at the moment of triage, to have genuinely needed handling before that issue. Denominator is **interrupting packets**, never all packets and never deltas |
| **Child-delta diagnostics** | in at least 95% of multi-child packet acts, the coordinator could identify every child delta's own outcome from the product alone, with no support request and no database lookup. Denominator is **multi-child packet acts** |
| **Client-ready package rate** | at least 90% of prepared release candidates authorized with no manual repair of any artifact outside Corridor, measured against **that partner's recorded obligation** — every artifact its issue profile configures, in the format its client actually receives, not merely in the format Corridor produces. Denominator is **prepared candidates** |
| **Manual reconstruction time** | median at most 3 minutes per interrupting packet spent rebuilding context outside Corridor — spreadsheet, email search, file browsing — to answer it. Reported per packet, never folded into the per-project-week time criteria |
| Compute and provider cost | at most 25 USD per active project-week, all providers included |
| Commercial | both partners state in writing that they will pay for continued or expanded use |

### Strata and denominators

- **Every rate criterion names its own denominator, and unlike units are
  never blended into one.** Per-delta, per-packet, per-candidate, per-arrival,
  and per-project-week counts are reported separately. A ratio whose numerator
  and denominator come from different units is a reporting defect, not a
  result.
- **Project-weeks are classified into three volume strata** by captured source
  arrivals, with the boundaries fixed and recorded before measurement starts:
  **quiet**, **ordinary**, and **burst**. Time, review-burden, latency,
  interruption-precision, and reconstruction criteria are reported **per
  stratum as well as pooled.** A pooled pass that depends on quiet weeks
  carrying failing burst weeks is reported as a failure of the burst stratum.
- A stratum with too few project-weeks to support its criteria is reported as
  insufficient evidence, never merged into an adjacent stratum.
- **The packet-precision denominator is interrupting packets, and it has been
  checked against the reader that computes it.** `_period_report` in
  `src/corridor/pilot_measurement.py` builds one entry for every distinct
  packet item key surfaced in the period, whatever became of it — accepted,
  edited, kept current, sent to coordination, deferred, ignored, superseded, or
  still open at close — and marks an entry `interrupting` by the rule below. It
  then emits `interrupting_packet_denominator`, `necessary_interrupting_packets`
  and `unjudged_interrupting_packets` beside the separate all-packet
  `packet_denominator`, and it computes manual reconstruction over interrupting
  packets alone. #424's and #498's rule that "every surfaced packet enters the
  denominator" is a rule against dropping the interruptions nobody answered —
  their own enumeration is a list of *outcomes*, not of consequence levels — so
  it agrees with this contract rather than naming a second denominator.
- **An interruption is a presentation, not a standing property of the
  difference underneath it.** A packet is interrupting for a period when that
  period retains at least one `packet_surfacing` record that declared it at
  ADR-0085's **"Must handle before this issue"**. A level is derived afresh for
  each reading from the issue content configured at that cutoff
  (`src/corridor/consequence_levels.py`), so the same packet can be shown as
  informational on Monday and as required triage on Wednesday. Three
  consequences follow, and each is tested: a packet **promoted** to that
  heading after an informational reading is interrupting, because somebody was
  in fact interrupted; **repeated** presentations of one packet are one
  interruption rather than several; and a packet that was **never** shown at
  that heading is not interrupting however urgent it looks. The interrupting
  presentation is printed with the packet as `interrupting_presentation`, whose
  evidence reference is the same occurrence identity a judgment at triage
  carries (`src/corridor/pilot_observations.py`), so the denominator and the
  judgments that fill it reconcile against one record.
- **The basis is not selectable, and an old result keeps its own label.**
  `src/corridor/pilot_report.py` computes both an `interrupting` and an
  `all_surfaced` rate; `src/corridor/pilot_checkpoint.py` evaluates the 80%
  threshold against the interrupting basis, prints the all-surfaced rate beside
  it as a diagnostic, and reads the pilot's predeclared
  `packet_precision_basis` only as the label its result keeps (#887). A pilot
  that predeclared `all_surfaced` has not measured this criterion: it is
  reported as unmeasured rather than passed, under the basis it was declared
  under, and is never retrospectively relabelled as compliant. A measured
  failure on this contract's basis stays a failure.
- **Zero eligible packets is unmeasured, never 100%.** An interrupting
  denominator of zero reports insufficient evidence, pooled and in every
  stratum. A week nobody was interrupted in is not a week this criterion
  passed.

### Material-field strata: what is supported, and for whom

A material field can be **retained** without being **maintained**. A column
Corridor carries through adoption as unknown content is preserved, but no
canonical field carries its meaning into accepted values and Proposed Deltas,
so nothing about it can be accepted, edited or rejected and nothing about it
can be right or wrong. **An unsupported semantic never counts as a passed
accuracy result.** Each stratum is classified for each partner as one of:

- **Fully supported** — captured at adoption where the partner's workbook
  populates it, compared against later sources as a Proposed Delta, and
  maintained as an accepted value.
- **Retained but not semantically maintained** — the source value is preserved
  and visible, but no canonical field carries its meaning; it is excluded from
  the accuracy and accept-without-edit denominators and reported as retained.
- **Excluded from the declared slice** — not measured at all, and named as
  excluded in the pilot report.

**#555 has not yet named either design partner**, so the per-partner column
below is unfilled for both, and that is the honest state rather than an
omission. The middle column is what the released code supports today,
independently of any partner; the right-hand column is what #555 fills in for
each named partner, before that partner's first measured week.

| Material-field stratum | What the released code supports today | Per-partner classification |
|---|---|---|
| Utility Owner | `external_org` is a material field, is captured at adoption, and is one of the external facts a delta may carry | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Conflict identity | `utility_id` is a material field and is captured at adoption | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Promised For | `committed_date` is a material field, is captured at adoption, and is an external fact a delta may carry | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Required By linkage | `need_date` is a material field, is captured at adoption, and is an external fact a delta may carry; the *linkage* half resolves against that project's registered Key Date Versions (ADR-0045), so a project with no registered key dates has the date without the linkage | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Closure | two different things carry this name. `marked_resolution` — completion as the source marked it — is a material field and is captured at adoption. `closure_result` is **not**: its values are references into a Project Record that the adoption act is itself establishing, so a populated closure cell in an adopted workbook is reported as an **unmapped material value** rather than resolved (`src/corridor/baseline_workbook.py`) | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Applies To | `applies_to` is a material field and an external fact a delta may carry, but it is **not captured at adoption** for the same reason as closure, and a populated Applies To in an adopted workbook is reported as an unmapped material value | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Agreement or permit status | **no canonical column exists.** `baseline_workbook.py` states it plainly: agreement or permit status "has no canonical column and so appears nowhere here". In delta resolution the recorded `operational_status` field stands in until a released Fact type exists, which is a stand-in and not that field's meaning | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |
| Cost responsibility | **no canonical column exists**, on the same statement in `baseline_workbook.py`. The only `cost_responsibility` in the code is a column on the legacy `dependencies` table, which ADR-0081 freezes and which an adopted project cannot write | **Partner 1: still unnamed (#555). Partner 2: still unnamed (#555).** |

Two consequences. First, **the last two rows cannot be classified "fully
supported" for any partner** on today's code, whatever that partner turns out
to issue; if a partner's obligation genuinely depends on either, that is a
build decision taken before enrollment, not a measurement question. Second, a
stratum a partner's workbook never populates is **excluded** for that partner
and reported as excluded — not passed, and not silently dropped from the
material-stratum criterion.

## Sampling rules

- **Material change sample.** Each week, when a partner has 20 or fewer eligible source arrivals, every one is inspected; when more than 20 arrive, 20 are drawn at random without replacement. The reader is a person who did not resolve the arrivals' deltas. Every material change found is checked against the Proposed Deltas produced; misses and their causes are logged. Across the full pilot, sampling continues until at least **30 eligible material-change cases per partner** have been reviewed; if that minimum is not reached by week 10 the pilot is extended, or the report states insufficient evidence for the miss criterion. Sampling is without replacement within the measurement period.
- **Low-value exception sample.** Each week the coordinator marks every surfaced exception they acted on as useful or low-value at the moment of triage; no retrospective relabeling.
- **Interrupting-packet sample.** The coordinator marks each interrupting packet as genuinely required or not at the moment of triage, on the same no-retrospective-relabeling rule, and logs reconstruction minutes for it then.
- **Baseline sample.** Drawn once per project at adoption, before any delta is resolved, and checked within the onboarding period.

## Changes during the measurement window

Corridor keeps being developed while the pilot runs. Two classes of change,
handled differently:

**Nonstructural fixes enter with a change-log entry and no measurement
boundary.** Security fixes, crash fixes, data-loss fixes, accessibility fixes,
and behavior-restoring fixes — a repair that makes the product do what it
already claimed to do. Each is logged in #498 with its date, commit, and the
criterion it could plausibly touch.

**Material changes create a recorded measurement boundary**, and results on
either side are reported separately rather than pooled. A change is material
when it alters:

- delta **grouping** or the packet-keying rule;
- the **actions** offered on a packet or delta;
- **navigation** or the default workload the coordinator lands in;
- **release blockers**;
- **artifact generation** or the template and mapping identities;
- **follow-up authority**; or
- **automation** — any change to what Corridor does without a person.

A material change mid-window is permitted. Concealing one, or reporting
across one as a single number, is not. If a boundary leaves either side with
too little evidence, the affected criteria are reported as insufficiently
evidenced and the pilot is extended rather than pooled.

## Predeclared responses

| Failed criterion | Response |
|---|---|
| Net coordinator time or review burden | Narrow the slice to the two source classes with the best accept-without-edit rate; re-run 4 weeks; if still failing, stop and record |
| Material false write | Disable automatic projection for that policy class for the rest of the pilot; the class returns to human delta decisions; pilot continues |
| Material misses or coverage | Freeze new source classes; fix capture for the failing class; extend the pilot by the weeks lost |
| Latency | Treat as an operations defect; fix and re-measure; not a product-stop condition by itself |
| Baseline adoption accuracy | Stop adoption for that workbook family until the importer passes on a fresh sample |
| Packet interruption precision | Retune the consequence-level rule, record the new packetizer version as a measurement boundary, and re-measure the remaining weeks |
| Child-delta diagnostics | Treat as a product defect in the packet resolution surface; fix before the next reporting cycle |
| Client-ready package rate | Identify the repaired artifact class and fix its generation; release authorization stays manual until it passes |
| Manual reconstruction time | Record what the coordinator had to leave the product to find, and treat it as missing packet context rather than coordinator slowness |
| Cost | Reduce model use for the highest-cost stage; if the ceiling cannot be met, record it as a pricing constraint in the pilot report |
| Commercial | Record the stated reason; the program does not proceed to a second cohort without a paying partner |

## The checkpoint outcome

#498 closes with one written finding per criterion and exactly one outcome:

- **Continue** — the cohort proceeds and expansion may be planned.
- **Revise** — a named part of the slice is changed and re-measured.
- **Extend evidence** — the criteria are sound but the evidence is thin;
  the window lengthens.
- **Narrow** — the slice is reduced to the source classes or projects where it
  demonstrably works.
- **Delay broader rollout** — the slice works for the cohort, but expansion
  waits on a named condition.
- **Keep higher-risk features disabled** — the roadmap's pilot-informed
  capabilities stay behind their boundary pending further evidence.

Each outcome names what it gates. None of them is an instruction to stop
unrelated engineering.

## What does not count

- ADR-0046's simulated Product Test Run. Its automated practitioner does not prove real-user validation, and ADR-0046 says so.
- A demo on the development corpus.
- Time saved on work the partner did not previously do.
- Accept-without-edit measured only on routine deltas while the material stratum fails.
- Any criterion whose cohort parameters were not pinned before measurement.
- A manual-upload rehearsal reported as connector coverage, as automated capture, or as evidence for the coverage or latency criteria.
- An artifact counted as client-ready because Corridor produced a file, when what the partner's client receives is a different format that somebody assembles outside Corridor.
- An accuracy or accept-without-edit result on a material-field stratum no canonical field maintains.

## Reporting

The pilot health dashboard in the
[observability runbook](operations/observability-runbook.md) carries the
inputs. The pilot closes with one written finding per criterion in #498, one
recorded checkpoint outcome, and this file is then marked closed with a link
to those findings.
