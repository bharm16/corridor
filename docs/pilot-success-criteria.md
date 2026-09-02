# Pilot success criteria

A temporary product-validation gate for the first paid slice, not an
architecture decision. It expires when the pilot closes and its findings are
recorded. The product it validates is
[ADR-0075](adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md)
as corrected by [ADR-0083](adr/0083-corrections-to-the-consolidation-set-after-the-realignment-review.md);
the mechanism is
[ADR-0076](adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md).
Rewritten 2026-09-01 after the [realignment review](research/adr-code-open-issue-realignment-review-2026-09-01.md)
so that every criterion is falsifiable. The pilot report issue (#498) carries
the result.

## The slice under test

```
Import one customer UCM
→ Adopt one exact baseline
→ Connect one mailbox or folder
→ Observe new sources
→ Produce Proposed Deltas
→ Resolve them (accept / edit / reject / defer)
→ Return the updated UCM, change summary, chase list, and weekly report
→ Measure net customer and Corridor operations time
```

## Pilot shape, declared before the first baseline is adopted

| Parameter | Declared value |
|---|---|
| Design partners | at least 2, each with a signed pilot agreement |
| Active projects | at least 4 in total, at least 2 per partner, each with a current UCM |
| Duration | 8 consecutive weeks of live evidence per project, after a 2-week onboarding period that is measured but not gated |
| Source classes | at least the partner's UCM revisions and one connected mailbox or folder; minutes and schedule exports if the partner produces them. Recorded Verbal Statements are **excluded from the gated source population**: they may remain available as legacy-compatible project context but count toward neither coverage nor delta accuracy until their spine-native source origin (#512) ships |
| Source events | at least 40 captured source arrivals per partner over the 8 weeks, or the pilot is extended until reached |
| Baseline measurement | partner's own maintenance and report-preparation minutes, logged per project-week for the 2 weeks before adoption, by the coordinator who does the work |
| Comparison method | same coordinator, same projects, same weekly artifact; Corridor time logged in the product, partner time logged by the coordinator |

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
| Compute and provider cost | at most 25 USD per active project-week, all providers included |
| Commercial | both partners state in writing that they will pay for continued or expanded use |

## Sampling rules

- **Material change sample.** Each week, when a partner has 20 or fewer eligible source arrivals, every one is inspected; when more than 20 arrive, 20 are drawn at random without replacement. The reader is a person who did not resolve the arrivals' deltas. Every material change found is checked against the Proposed Deltas produced; misses and their causes are logged. Across the full pilot, sampling continues until at least **30 eligible material-change cases per partner** have been reviewed; if that minimum is not reached by week 10 the pilot is extended, or the report states insufficient evidence for the miss criterion. Sampling is without replacement within the measurement period.
- **Low-value exception sample.** Each week the coordinator marks every surfaced exception they acted on as useful or low-value at the moment of triage; no retrospective relabeling.
- **Baseline sample.** Drawn once per project at adoption, before any delta is resolved, and checked within the onboarding period.

## Predeclared responses

| Failed criterion | Response |
|---|---|
| Net coordinator time or review burden | Narrow the slice to the two source classes with the best accept-without-edit rate; re-run 4 weeks; if still failing, stop and record |
| Material false write | Disable automatic projection for that policy class for the rest of the pilot; the class returns to human delta decisions; pilot continues |
| Material misses or coverage | Freeze new source classes; fix capture for the failing class; extend the pilot by the weeks lost |
| Latency | Treat as an operations defect; fix and re-measure; not a product-stop condition by itself |
| Baseline adoption accuracy | Stop adoption for that workbook family until the importer passes on a fresh sample |
| Cost | Reduce model use for the highest-cost stage; if the ceiling cannot be met, record it as a pricing constraint in the pilot report |
| Commercial | Record the stated reason; the program does not proceed to a second cohort without a paying partner |

## What does not count

- ADR-0046's simulated Product Test Run. Its automated practitioner does not prove real-user validation, and ADR-0046 says so.
- A demo on the development corpus.
- Time saved on work the partner did not previously do.
- Accept-without-edit measured only on routine deltas while the material stratum fails.

## Reporting

The pilot health dashboard in the
[observability runbook](operations/observability-runbook.md) carries the
inputs. The pilot closes with one written finding per criterion in #498, and
this file is then marked closed with a link to those findings.
