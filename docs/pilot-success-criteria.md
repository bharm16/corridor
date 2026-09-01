# Pilot success criteria

A temporary product-validation gate for the first paid slice, not an
architecture decision. It expires when the pilot ends and its findings are
recorded. The product it validates is
[ADR-0075](adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md);
the mechanism is
[ADR-0076](adr/0076-the-record-changes-by-captured-fact-adopted-baseline-and-resolved-delta.md).

## The slice under test

```
Import existing UCM
→ Adopt baseline
→ Connect one mailbox/folder
→ Process one new source
→ Show proposed deltas
→ Accept/reject/edit
→ Export updated UCM and report
```

## Criteria, defined before the pilot starts

Measured per design partner, per active project, against that partner's own
baseline measured in the two weeks before adoption.

| Criterion | Target |
|---|---|
| Weekly record-maintenance and report-preparation time | at least 50% reduction |
| Median review burden | below roughly 10 to 15 minutes per active project per week |
| Accept-without-edit rate, routine proposed deltas | at least 80% |
| Material commitments, dates, ownership, conflict identity | stricter than routine: every miss and every false write is reviewed individually, and the false-write rate on these fields is reported on its own |
| Missed material changes | sampled weekly against the partner's inbox and folder; rate reported |
| False or low-value exception rate | reported; a falling trend across the pilot |
| Commercial | at least two design partners willing to pay for continued or expanded use |

## What does not count

- ADR-0046's simulated Product Test Run. Its automated practitioner does not
  prove real-user validation, and ADR-0046 says so.
- A demo on the development corpus.
- Time saved on work the partner did not previously do.

## Reporting

The pilot health dashboard in the
[observability runbook](operations/observability-runbook.md) carries the
inputs. The pilot closes with one written finding per criterion, recorded in
the issue tracker, and this file is then marked closed with a link to those
findings.
