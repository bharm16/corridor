"""One scenario runner for the supported customer journey (#848).

The acceptance proof this repository already had is
``tests/test_issue_path_end_to_end.py``: it walks the coordinator's week from
a confirmed coverage reading to an authorized package, through the real
routes, the real Due Work registry and a real migrated database. What it
cannot do is start where a customer starts. It overrides
``get_human_principal`` so nobody signs in, it hands itself an adopted project
through a fixture, and it never asks whether the bytes a customer was promised
can be fetched back. The customer-journey audit of 2026-09-10 says in as many
words that those are the gaps the exercise has to detect, not the ones it may
assume away: "Replacing the human-authentication dependency, pre-adopting the
baseline in a fixture, or retrieving final bytes directly through Python skips
the very gaps this exercise must detect."

So this is the runner for a journey that begins with **a provisioned but
unadopted project and a person who has not yet signed in** and ends with
**that person retrieving the exact approved package and returning for the next
cycle**. Two things about its shape are deliberate.

**Every seam is declared, and a scenario says which ones it has.** The runner
itself replaces nothing. A scenario's own seams are written down where the
scenario is: the core journey declares three -- a controlled clock, so no step
races the wall clock; a non-sending mail delivery, so a sign-in link is
captured instead of posted; and one test-only extraction fault, so that
correcting a capture that is genuinely wrong is reachable without changing
what the retained source says. Nothing else is replaced: authentication is the
real magic-link path, authorization is the real membership gate, the reads run
as the real live-pilot database login under the enforced boundary, and
preparation is the production dispatch and worker. A seam added and not
written down is a gap the exercise silently stops detecting, which is why the
list is declared rather than left to whatever a fixture happened to override.

**What a run of a scenario is evidence of is the scenario's to state.** This
runner walks steps and reports them; it establishes nothing about an
environment. The core journey's own module docstring says what its passing run
may be described as, and what it may not.

**A step that cannot pass yet is declared, not omitted.** The whole journey
(#849) is written out, and each step whose owning ticket has not landed says
so and names the ticket. The runner walks the steps in order and stops at the
first one that fails, because the journey is linear -- nothing downstream of
an upload that cannot happen is a real observation -- and reports every step
it did not reach as blocked by the step and ticket that stopped it. The report
is therefore a frontier: as each ticket lands the frontier moves and one more
step reports a pass. An expected-to-fail step that *passes* fails the run, in
the same spirit as the ratchets in ``tests/ratchet_support.py``: the record of
what does not work yet may fall, and the way it falls is that somebody removes
the marking in the pull request that made it pass.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime


#: A step ran and did what the journey says it should.
PASSED = "passed"
#: A step ran, failed, and nothing said it was allowed to.
FAILED = "failed"
#: A step ran, failed, and its owning ticket has not landed.
EXPECTED_FAIL = "expected-fail"
#: A step marked expected-to-fail passed. Its ticket landed; unmark it.
UNEXPECTED_PASS = "unexpected-pass"
#: A step never ran, because an earlier step did not pass.
BLOCKED = "blocked"


class ControlledClock:
    """The first declared seam: one instant at a time, never the wall clock.

    Both halves of the product read their time through a caller-supplied
    callable already -- ``get_review_clock`` for a request, the Due Work
    runtime's ``clock`` for an occurrence -- so this is the same object handed
    to both, and a journey advances time by saying so.
    """

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance_to(self, moment: datetime) -> None:
        self.moment = moment


@dataclass(frozen=True)
class Step:
    """One act of the journey, and who owes the product the ability to do it.

    ``sentence`` is the step in the words the journey is written in, because
    the report is read by a person deciding what is left. ``owner`` is the
    ticket that makes the step pass: for a step that passes today it is the
    ticket that made it pass, so the report says why each step works as well
    as why each one does not.
    """

    name: str
    sentence: str
    owner: str
    run: Callable[..., None]
    expected_to_fail: bool = False


@dataclass(frozen=True)
class StepResult:
    """What happened to one step, and the sentence that explains it."""

    step: Step
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class ScenarioReport:
    """Every step of one scenario run, in the order the journey happens in."""

    name: str
    results: tuple[StepResult, ...]

    @property
    def failures(self) -> tuple[StepResult, ...]:
        return tuple(one for one in self.results if one.outcome == FAILED)

    @property
    def unexpected_passes(self) -> tuple[StepResult, ...]:
        return tuple(one for one in self.results if one.outcome == UNEXPECTED_PASS)

    @property
    def passed(self) -> tuple[StepResult, ...]:
        return tuple(one for one in self.results if one.outcome == PASSED)

    def outcome_of(self, step_name: str) -> str:
        for one in self.results:
            if one.step.name == step_name:
                return one.outcome
        raise KeyError(f"{self.name} declares no step named {step_name!r}")

    def render(self) -> str:
        """The report as a person reads it: one line of prose per step.

        Deliberately sentences rather than a table of codes. Somebody reading
        this is deciding what is left to build, and "the coordinator cannot
        open the project and read what Corridor needs next yet; #827 owns
        that" answers the question a column of statuses does not.
        """

        lines = [f"{self.name}: {len(self.passed)} of {len(self.results)} steps pass."]
        for one in self.results:
            lines.append(f"  {_prose(one)}")
        return "\n".join(lines)


def _prose(result: StepResult) -> str:
    step = result.step
    if result.outcome == PASSED:
        return f"{step.sentence} -- works ({step.owner})."
    if result.outcome == EXPECTED_FAIL:
        return (
            f"{step.sentence} -- does not work yet; {step.owner} owns it. "
            f"What happened: {result.detail}"
        )
    if result.outcome == BLOCKED:
        return f"{step.sentence} -- not reached. {result.detail}"
    if result.outcome == UNEXPECTED_PASS:
        return (
            f"{step.sentence} -- works now, and is still marked as waiting on "
            f"{step.owner}. Remove the marking."
        )
    return f"{step.sentence} -- FAILED, and nothing said it could. {result.detail}"


def run_scenario(name: str, steps: Sequence[Step], context) -> ScenarioReport:
    """Walk the journey in order, stopping at the first step that fails.

    ``context`` is whatever the scenario's steps take; this runner does not
    look inside it. The stop is not timidity: a journey is linear, so a step
    after the one that failed would be reading state the product never
    reached, and reporting whatever it happened to answer as a result would be
    inventing evidence.
    """

    results: list[StepResult] = []
    stopped_by: Step | None = None
    for step in steps:
        if stopped_by is not None:
            results.append(
                StepResult(
                    step,
                    BLOCKED,
                    f"The journey stopped at \"{stopped_by.sentence}\", which "
                    f"{stopped_by.owner} owns.",
                )
            )
            continue
        try:
            step.run(context)
        except Exception as failure:  # noqa: BLE001 - the outcome is the subject
            detail = _failure_detail(failure)
            if step.expected_to_fail:
                results.append(StepResult(step, EXPECTED_FAIL, detail))
            else:
                results.append(StepResult(step, FAILED, detail))
            stopped_by = step
        else:
            outcome = UNEXPECTED_PASS if step.expected_to_fail else PASSED
            results.append(StepResult(step, outcome))
    return ScenarioReport(name, tuple(results))


def _failure_detail(failure: BaseException) -> str:
    """One readable line for what went wrong, without a whole traceback.

    An assertion's own message is the thing worth printing -- a scenario's
    steps write theirs as sentences -- so it is kept whole and only its first
    line is taken, because `assert` rewriting appends the expression it
    evaluated.
    """

    text = str(failure).strip() or failure.__class__.__name__
    first, _, rest = text.partition("\n")
    return first if not rest else f"{first} ..."
