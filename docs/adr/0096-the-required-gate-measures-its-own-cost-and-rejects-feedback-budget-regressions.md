---
status: accepted
domain: testing
scope: current product
amends:
  - ADR-0087
  - ADR-0088
  - ADR-0093
amended_by:
  - ADR-0097
---

# The required gate measures its own cost and rejects feedback-budget regressions

**Amends ADR-0087, ADR-0088, and ADR-0093.**

ADR-0087 established a feedback budget. ADR-0088 retained complete behavior
coverage on every code change and required the cost to be reduced instead of
inferring a smaller test set from changed paths. ADR-0093 made the result a
server-required, fail-closed status. These decisions prevented missing tests
and missing checks, but none rejected a gate whose tests had become too slow.
The numbers were prose again. New expensive proofs accumulated, local timing
refreshes became additional full runs, and the CI configuration still assumed
four cores on standard private Linux runners that provide two.

## The decision

The required gate measures its own execution, publishes the evidence, and
checks the feedback budget before reporting success. The existing exact match
between classifier answers and job results runs first and remains unchanged.
Timing evidence cannot excuse a failed, cancelled, or unexpectedly skipped job.

Every required behavior shard and migration command writes local JUnit and a
receipt with run, revision, attempt, suite and shard identity, elapsed time,
test count, exit status, and per-file timings. It emits that receipt through
a uniquely named matrix job output and as a `CORRIDOR_TEST_RECEIPT` JSON line
in its log. The summary requires exactly the expected set
of successful receipts and rejects missing, duplicate, stale, or inconsistent
evidence. An empty shard is valid only when the complete suite still has
executed tests; successful status alone cannot prove coverage. GitHub job
metadata supplies total gate timing, including setup, instead of pretending
that the sum of overlapping test durations is elapsed developer waiting time.

`tests/feedback-budget.json` is the machine-readable policy. ADR-0087's
three-minute rolling median, five-minute 90th-percentile target, and
45-second migration-command budget retain their values. A current gate at or
above five minutes fails independently of history. The rolling 90th percentile
is reported; this current-run ceiling enforces its limit on every new run
without making old slow samples block a faster repair. Median enforcement
begins with five validated samples in a ten-run window. A current gate below
the three-minute target may pass while the historical median remains slow.
Before five usable samples exist, each current behavior-test command must
finish below three minutes and the complete run below five minutes. A rolling
median is not a per-run queue-time cutoff. Unavailable historical log access
therefore cannot disable the budget: current job-output proof and execution
limits remain required.
Raising a limit is a policy change that requires an explicit successor
decision; ordinary timing refreshes cannot move these limits.

The classifier reads previous validated aggregate reports from completed
release-gate job logs and emits one compact `timing_weights` job output. Every
shard receives that same output through `CORRIDOR_CI_WEIGHTS`. Its measured
per-file durations become the next partition's weights automatically; the
checked-in duration files remain the bootstrap when no usable report exists.
The summary reads current receipts directly from completed job outputs,
validates their identities against GitHub job metadata, writes the summary, and emits one
`CORRIDOR_TEST_FEEDBACK` JSON line even when the budget fails. These existing
logs retain the evidence without a separate upload that can fail after the
tests have already passed.
Current proof does not depend on log downloading or CLI formatting behavior.
Only historical completed-workflow logs are read through the API; colored logs
are captured for machine parsing without printing raw terminal controls.
Pytest children do not inherit Actions command-file
paths; the owning runner alone publishes its receipt after pytest exits.
Unavailable historical logs produce a visible diagnostic and use the validated
bootstrap weights. These initial weights were calibrated from all nine successful
test commands in run 34257502898; no extra local suite was run to produce them.
This removes the requirement to run two additional local timing suites after
every change that alters test cost. Missing history starts a visible bootstrap;
missing current receipts fails closed. No classifier or summary package
installation, additional runner, or side workflow is introduced.

## Scheduling must account for actual work

Standard private Linux runners use two xdist workers. Ordinary behavior tests
keep `worksteal`; the expensive slow complement uses `loadfile`, so a module's
shared acceptance fixture is built once on its runner. A universal distribution
mode is no longer required: the scheduler must avoid duplicated setup as well
as balance independent tests. Local worker counts remain configurable.

The 2026-09-08 receipts in runs `34280898163` and `34285214380` exposed an
allocation imbalance within the existing nine test runners. The latter
recorded 1,231 ordinary case-seconds and 362 slow case-seconds; its slowest
ordinary command took 154.3 seconds while the slow complement took 91.3.
The complete gate remained over budget at 208.1 seconds after bounded fixture
work was reduced. A seven/two trial in run `34286519239` brought the ordinary
command to 113.5 seconds but crowded indivisible slow modules into a
166.6-second command. Six ordinary and three slow runners brought the gate to
183.6 seconds in run `34287893996`, still above target. Run `34290598598`
measured a 128.6-second ordinary command and a 211.1-second gate, including a
late-starting runner. The next capacity trial added one ordinary runner:
seven ordinary and three slow, with twelve downloading jobs when check and
migration are required. [Run `34291250899`](https://github.com/bharm16/corridor/actions/runs/34291250899)
passed on 2026-09-08 with a 164.92-second required gate, a 112.41-second
ordinary command, a 113.15-second slow command, and 31.48 seconds for migration.
This is the measured result of that allocation; subsequent PRs measure it
again. Earlier package saturation predates dependency changes, so the previous
ceiling must be checked against current execution.
Two workers per runner, the `worksteal`/`loadfile` distinction, complete test
coverage, and every feedback threshold remain unchanged. The required gate
measures and accepts or rejects this allocation through the same receipts;
additional capacity does not grant a budget exception.

Slow commands also use `--no-loadscope-reorder`. The pinned xdist scheduler
otherwise replaces the supplied measured-duration order with case-count order,
so an expensive one-case replay can be postponed behind cheap many-case files.
A tiny real two-worker regression demonstrates that reversal and verifies
that the measured file priorities reach both workers. File grouping and
fixture isolation remain unchanged.

The complete 1,018-Fact workbook replay belongs to the required slow complement,
matching the declared slow marker's real-corpus scope. Its existing extraction,
replay, inclusion and projection assertions all remain. It ran for 484 seconds
inside the ordinary diagnostic while isolated runs took 20–44 seconds; mixing
that scale proof into a rollback-heavy developer lane made feedback depend on
the worker's prior database workload. The required slow file scheduler gives
that proof its own bounded execution context without dropping it from PR CI.
Test connections default to JIT disabled, avoiding compilation overhead for
short transactional queries; the server and production defaults are unchanged.

The migration target collects only its owning migration file and runs its
independent cases on two workers. The genuine fresh-install proof still builds
from scratch. Supported-upgrade cases clone a migrated predecessor template,
then seed their own rows and execute every real transition and assertion.
The production-identity comparison reads the original configured source through
a read-only connection instead of building another empty worker database just
to compare identities. The eleven-case command measured 21.65 seconds locally
after these changes; the preceding CI run took 50.05 seconds. Whole-tree
collection supplies no additional migration proof.

`make check` owns `tests/test_architecture.py` and
`tests/test_source_scan_support.py`. These source checks are omitted from
ordinary CI shard execution because the unconditional required check job has
already run them. The partition still accounts for every file, and the union
of check-owned tests and behavior tests preserves the complete required proof.
Local broad and scheduled full-suite targets retain those tests. No behavior
proof moves to an optional schedule, and no changed-path dependency inference
is introduced.

The local loop must start a database only when the selected tests need one.
`make check` and pure focused tests run with no reachable PostgreSQL service.
Documentation-only revisions rerun the source and documentation checks and
preserve earlier behavior-test results. For behavior changes, focused checks
provide local feedback and required PR CI owns the complete release proof.
A local broad run is blocked by both the entrypoint and pytest controller
unless it names one of two diagnostic purposes: reproducing an observed failure
that focused tests cannot reproduce, or an explicit performance investigation.
CI is automatically authorized. Routine edits, commits, reviews and pre-push
reassurance are not exceptions. Refusal precedes database provisioning.
Validation is owned once per worktree and follows completed behavior changes,
not individual edits, plan steps or local commits. Overlapping checks requested
by workers are combined by the coordinator. Intermediate commits stay local
until the reviewed change is ready for its required CI run.

Local test commands wait for the actual child process and retain a running or
completed JSON result with its exit code. They do not infer completion from
pytest's human-readable summary, whose optional warning and skip counts broke
the previous indefinite polling loop. A bounded deadline terminates only that
command's own process group; it does not signal another task or its tests.
Focused entrypoints use a 30-second default deadline and stop at the first
failure; deliberate diagnostics can name a different deadline or failure limit.

GitHub partial reruns may reuse successful receipts from earlier attempts of
the same run and tested SHA. In [run `34292528190`](https://github.com/bharm16/corridor/actions/runs/34292528190),
GitHub assigned reused jobs new IDs and the latest attempt number while
preserving their original execution intervals and earlier receipt outputs.
The summary accepts that reuse only when the inventory also contains the
successful original-attempt job with the same name and exact start and end
times. Missing, failed or ambiguous original evidence and changed intervals
fail closed. Receipt identities remain unchanged, and the summary preserves
the jobs' full measured critical-path cost. A summary-only retry therefore
cannot erase expensive test work, and a failed job can be repaired without
needlessly rerunning its successful siblings.

The separate CDK toolchain is installed, asserted, and synthesized only for
infrastructure inputs: the infrastructure project, deployment workflows and
scripts, runtime configuration names, container and build configuration, and
their lockfiles. The check job classifies its own diff without waiting for the
behavior classifier and verifies each infrastructure step's exact expected
outcome. Its pure workflow and deployment-runbook assertions run unconditionally
in `make check`, including documentation-only revisions. Ordinary application
changes still run both complete behavior suites; they do not install CDK to
repeat unrelated infrastructure assertions.

## Considered options

**Rely on another local timing refresh.** Rejected. It duplicates required
execution and needs recurring manual maintenance without rejecting growth.
CI already pays to run the tests and can retain its own measurements.

**Transport receipts through uploaded artifacts.** Rejected after artifact
finalization returned HTTP 403 despite every test passing. Timing evidence
travels through the job logs GitHub already retains and the classifier's job
output; a separate metadata upload must not force successful tests to repeat.

**Add runners or keep four workers per private runner.** Rejected as the
initial repair. The measured package-download ceiling remains in force and
extra processes do not add CPU cores. Remove duplicated work first; a future
capacity change needs measurements on the actual environment.

**Fail every change until historical percentiles recover.** Rejected. A slow
history would prevent the first fast repair from landing. The current hard
limit remains binding, and a repair within the target can retire that history
without changing the threshold.

**Relax limits or make the budget informational.** Rejected. Both recreate the
condition that allowed the slowdown to return. A budget breach fails the same
status the server already requires, with a report identifying its cost.

**Select fewer behavior tests from changed paths.** Rejected for this repair.
ADR-0088's coverage decision remains in force; scheduling, shared reads, and
repeated fixture work can be improved while retaining the existing proofs.

## Consequences

ADR-0087's feedback budget gains an executable gate and an explicit bootstrap
and repair rule. ADR-0088's full-coverage requirement remains intact, with
check-owned source tests executed once and scheduling chosen per suite.
ADR-0093's summary gains a second required decision after its unchanged job
result check. Its always-triggered workflow, narrow documentation allowlist,
exact result matching, and migration path scoping remain in force.

The run summary and its `CORRIDOR_TEST_FEEDBACK` log record become the ordinary
timing evidence. A developer investigates the measured files when the budget
fails and uses local timing targets
only when they answer a specific diagnostic question. The implementation is
validated by executing receipt rejection and budget cases and by testing the
actual workflow's wiring; future performance claims still require a measured
run of the revised gate.
