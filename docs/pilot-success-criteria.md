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
defining the review and release surfaces under test.
Rewritten 2026-09-01 after the [realignment review](research/adr-code-open-issue-realignment-review-2026-09-01.md)
so that every criterion is falsifiable, and again 2026-09-02 (#538) so that the
result is a **project checkpoint** rather than a project-wide stop condition.
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
→ Connect one mailbox or folder
→ Observe new sources
→ Produce Proposed Deltas
→ Review them as packets
→ Resolve them (accept / edit / reject; defer schedules)
→ Return the updated UCM, change summary, chase list, and weekly report
→ Measure net customer and Corridor operations time
```

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
| Feature flags | the complete flag state of the pilot environment, including every flag left off |

An unpinned parameter is a reporting defect: if the value cannot be stated,
the affected criterion is reported as unmeasured rather than passed.

## Pilot shape, declared before the first baseline is adopted

| Parameter | Declared value |
|---|---|
| Design partners | at least 2, each with a signed pilot agreement |
| Active projects | at least 4 in total, at least 2 per partner, each with a current UCM |
| Duration | 8 consecutive weeks of live evidence per project, after a 2-week onboarding period that is measured but not gated |
| Source classes | at least the partner's UCM revisions and one connected mailbox or folder; minutes and schedule exports if the partner produces them. Recorded Verbal Statements are **excluded from the gated source population**: they may remain available as legacy-compatible project context but count toward neither coverage nor delta accuracy until their spine-native source origin (#512) ships |
| Source events | at least 40 captured source arrivals per partner over the 8 weeks, or the pilot is extended until reached |
| Comparison method | same coordinator, same projects, same weekly artifact; Corridor time logged in the product, partner time logged by the coordinator |

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
| Accept-without-edit, material-field stratum | at least 70%, reported separately for Utility Owner, conflict identity, Promised For, Required By linkage, closure, Applies To, agreement or permit status, cost responsibility |
| Material false writes | zero automatic projections to a material field that a person later reverses as a confirmed policy error. Any occurrence fails that policy-class criterion, and it stays failed even when the pilot continues for diagnosis |
| Material misses | at most 5% of sampled material changes absent from Proposed Deltas |
| Coverage | at least 95% of source arrivals in the connected channel captured within the latency window; the remainder listed by cause |
| False or low-value exceptions | at most 20% of surfaced exceptions judged low-value by the coordinator in weeks 7 through 10, and at least a 30% relative reduction from weeks 3 through 4 |
| **Packet interruption precision** | of the packets Corridor placed at "must handle before this issue" (ADR-0085), at least 80% judged by the coordinator, at the moment of triage, to have genuinely needed handling before that issue. Denominator is **interrupting packets**, never all packets and never deltas |
| **Child-delta diagnostics** | in at least 95% of multi-child packet acts, the coordinator could identify every child delta's own outcome from the product alone, with no support request and no database lookup. Denominator is **multi-child packet acts** |
| **Client-ready package rate** | at least 90% of prepared release candidates authorized with no manual repair of any artifact outside Corridor. Denominator is **prepared candidates** |
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

## Reporting

The pilot health dashboard in the
[observability runbook](operations/observability-runbook.md) carries the
inputs. The pilot closes with one written finding per criterion in #498, one
recorded checkpoint outcome, and this file is then marked closed with a link
to those findings.
