---
status: accepted
domain: testing
scope: current product
amends:
  - ADR-0065
amended_by:
  - ADR-0088
migration: the required pull-request gate's median is 3m14s against the three-minute median recorded here, over the twelve pull-request runs after the five-way split (#548); the 90th percentile and the 45s `make test-migrations` number are met. #595 removed the repeated per-job setup and owes the five-run measurement of the result.
---

# The migration window and the feedback budget are enforced numbers, not prose

**Amends ADR-0065.**

ADR-0065 decided that test gates preserve feedback without weakening release proof, and bounded the executable migration history to "one blank-database baseline, one supported released-head-to-current transition", closing with: "The migration inventory is two revisions and four permanent tests until a new released successor advances the window."

Both halves of that decision were correct and both were lost in the same way: they were written as prose and guarded by tests that could not fail.

- The executable chain grew from **2 revisions to 23**. The guard was a test that enumerated the permitted filenames, so each migration-bearing change added its name and the test kept passing. It did not prevent accumulation; it recorded it.
- "The supported upgrade window is the released head and its immediate successor" became, in practice, the *previous merge*. Each change advanced a predecessor constant by one and left every older development revision executable. No recorded release explains 21 successors as 21 supported releases.
- The required pull-request gate still runs the whole non-slow suite and the whole non-migration slow complement on every code change. The test asserting CI runs at the "smallest scope" asserts only that each suite runs *once*, with no claim that either is necessary for the changed files and no time budget at all.

ADR-0065's principles are unchanged. What changes is that the numbers are now recorded where a test can read them, and that the gate is selected rather than exhaustive.

## The window is data, and the ratchet only turns one way

`src/corridor/migrations/policy.py` records the fresh-install builder, the compatibility marker, the supported revision, the single head, the target of **one** unreleased transition, and the count the graph carries today.

The guard asserts against Alembic's revision graph rather than a directory listing: exactly one head, the supported revision reaches it, no unrelated executable history, and the transition count **at or below** the recorded number. The recorded number may fall and may never rise. A change that needs another revision folds into the current unreleased transition, or consolidates the chain and lowers the number first.

This is deliberately weaker than "exactly two revisions forever" and deliberately stronger than a filename list. The invariant is: **only the minimum executable revisions needed for fresh installation and the one supported upgrade transition may exist.** The recorded debt is the distance from that, and it is visible in one file instead of spread across a test's expected set.

## Feedback has a budget

A suite with no budget regresses again after every cleanup, which is what happened here. The budget:

- focused implementation feedback under **30 seconds**;
- required pull-request checks median under **3 minutes**, 90th percentile under **5 minutes**;
- `make test-migrations` under **45 seconds**.

Measurement comes before removal. `make test-timing` publishes per-file totals and a base-branch comparison, because a total runtime says a job got slower without saying which file did it. The first run found one file at **42.7%** of the non-slow suite, which no amount of reasoning about test counts would have located.

## The required gate is selected, not exhaustive

The required gate is `make check`, a small critical suite (database authority, transaction boundaries, public command contracts, schema and model consistency, fail-closed behavior), integration tests for the affected component, and migration tests only on migration-sensitive paths. The full non-slow and slow suites run on the weekly and manual workflow, before a release, and on a change whose component or shared infrastructure requires them. `make test-full` already backs the scheduled and manual gate, so exhaustive coverage keeps a home.

Path selection **fails wide**. A change to `models.py`, `db.py`, dependency configuration, shared persistence, or a migration selects the broad suites. The failure this decision guards against is a slow gate; the failure it must not create is a gate that skips affected tests.

## Considered options

**Restore "exactly two revisions" as a hard assertion.** Rejected: it fails today, so it would be merged red or immediately suppressed, which is how the previous guard became an allowlist. A ratchet that can only fall reaches the same place and can be enforced on the way.

**Keep the filename list and review it more carefully.** Rejected: it passed 21 times. A guard that requires vigilance to work is not a guard.

**Delete the expensive tests.** Rejected, and explicitly so: the timing data did not exist when the suite was slow, and the one file at 42.7% is dominated by wall clock under `--dist loadfile`, not by redundant coverage. Measure, then choose.

**Amend ADR-0065 in place.** Rejected under the lifecycle rules in `docs/adr/README.md`: code complying with ADR-0065's gate clause would fail review under this wording, and the normative rule changes, so this is a new sequential decision with reciprocal metadata rather than an edit.

## Consequences

- ADR-0065's migration-history principle and its four-test permanent contract stand. Its inventory sentence is superseded by the recorded policy, and its implicit "every PR runs both full suites" gate is replaced by the selected gate above.
- The chain still carries 21 unreleased transitions against a target of 1. Consolidating them is the debt this decision makes visible and blocks from growing; it is not paid here.
- The gate change and the distribution-mode change are separately tracked in #548, the latter blocked on an order-dependent test that `--dist loadfile` was masking.
