---
status: accepted
---

# The report baseline is the last released report

A Coordination Report has a "changes since the last report" section. The comparison used the newest retained report of any kind, so an extra midweek PDF silently ate part of the week's changes, and scheduling internal snapshots would have made it worse. The decision ruling is on [#327](https://github.com/bharm16/corridor/issues/327).

## Decision

**Every report's comparison baseline is the last Report Approved for Release. Nothing else moves that marker.**

- Prepared but unapproved PDFs, weekly snapshots, and ad hoc reports never advance the baseline.
- Before a project's first release, there is no comparison. The report shows current state only.
- The baseline is approval-driven, not calendar-driven. A missed week widens the window, stated honestly ("changes in the last 14 days"). No false report occurrence is created.
- If check settings changed between the baseline and now, the report says so. A settings change is not a project change.
- Unchanged principles: the internal view refreshes freely; an approved copy never changes (ADR-0040).

## Why

The audience's last view is the last approved PDF. "What changed" must mean changed since that. One baseline also deletes the series-taxonomy question entirely: there is one series, and release is what advances it.

## Consequences

One-predicate code change: the comparison selects the newest released report run instead of the newest run. Resolves #322's decision gate 6. Ticket #354's scheduled snapshots and preparation need no series machinery.
