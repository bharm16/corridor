"""Match every gate job and step result against what the classifier asked for.

This is the fail-closed rule the required `release-gate` status rests on:
a job the classifier said was required must report `success`, a job it said
was not required must report `skipped`, and an answer that is neither `true`
nor `false` is a classifier the gate cannot trust, so it fails rather than
picking a default. **Never weaken this to `success or skipped -> pass`** --
that is how a behavior job skipped by a broken `if` condition reports green,
and a red gate that looks green is worse than no required check at all
(ADR-0093).

The rule used to be two hand-written bash blocks inside
`.github/workflows/release-gate.yml`: one in the `check` job for the four
conditional CDK steps, one in the `release-gate` summary job for the five
gate jobs. They shared no code, so the one rule that must never be weakened
could be weakened in two places independently, and each needed its own test
harness that pulled the `run:` string out of the YAML and re-executed it
under `bash -c`. Both harnesses had separately invented the same
`"${{" not in run` guard to prove the executed text was the shipped text.
Here the rule is a function two adapters call, the ten fail-closed cases are
ordinary unit tests, and the infrastructure matcher inherits every one of
them instead of restating a subset.

The summary job runs this before it has anything else on disk, so
`actions/checkout` moved above the matcher step. Standard library only, and
no repository dependency: both jobs run it on the bare `python3` of the
runner without `uv sync`, so it adds no package download to a gate whose
measured ceiling is what this account serves concurrently.

Usage:
    python3 scripts/gate_results.py summary
    python3 scripts/gate_results.py infrastructure
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
import sys

# The GitHub workflow-command prefix that turns a line into an annotation.
ERROR = "::error::"
# What an empty result or answer is called, so a missing value reads as a
# missing value rather than as a blank in the middle of a sentence.
ABSENT = "<absent>"

REQUIRED_ANSWER = "true"
NOT_REQUIRED_ANSWER = "false"
REQUIRED_RESULT = "success"
NOT_REQUIRED_RESULT = "skipped"
# No GitHub job result or step outcome equals this, so a classifier answer
# the gate cannot read matches nothing.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Answer:
    """One classifier output: the name it is published under, and what it said."""

    label: str
    value: str


@dataclass(frozen=True)
class Expectation:
    """One job or step result, and the classifier answer that decides it."""

    label: str
    answer: Answer
    result: str


def always_required(label: str, result: str) -> Expectation:
    """A gate job that runs on every pull request, whatever the classifier said."""

    return Expectation(label, Answer(label, REQUIRED_ANSWER), result)


def expected_result(answer: Answer) -> str | None:
    """The result this answer demands, or `None` when the answer is unreadable."""

    if answer.value == REQUIRED_ANSWER:
        return REQUIRED_RESULT
    if answer.value == NOT_REQUIRED_ANSWER:
        return NOT_REQUIRED_RESULT
    return None


def report(pairs: Sequence[Expectation]) -> list[str]:
    """Every line the matcher prints, in order; a failure carries `ERROR`.

    An unreadable answer is reported once, against the classifier output that
    produced it, and then every result it decides is matched against a value
    no result can equal. The gate therefore fails on the answer itself even
    if every dependent result happened to agree with it.
    """

    lines: list[str] = []
    reported: set[str] = set()
    for pair in pairs:
        expected = expected_result(pair.answer)
        if expected is None:
            if pair.answer.label not in reported:
                reported.add(pair.answer.label)
                lines.append(
                    f"{ERROR}{pair.answer.label} is "
                    f"{pair.answer.value or ABSENT}; "
                    "the classifier must answer true or false"
                )
            expected = UNCLASSIFIED
        if pair.result == expected:
            lines.append(f"{pair.label} = {pair.result}")
        else:
            lines.append(
                f"{ERROR}{pair.label} is {pair.result or ABSENT}; "
                f"the release gate requires {expected}"
            )
    return lines


def match(pairs: Sequence[Expectation]) -> list[str]:
    """Every failure message; an empty list means the gate is satisfied."""

    return [line for line in report(pairs) if line.startswith(ERROR)]


# The environment each adapter reads, so the workflow's `env:` block and the
# adapter cannot drift apart unnoticed: tests/test_ci_policy.py asserts the
# two are equal.
SUMMARY_ENVIRONMENT: tuple[str, ...] = (
    "BEHAVIOR_REQUIRED",
    "MIGRATION_REQUIRED",
    "CLASSIFY_RESULT",
    "CHECK_RESULT",
    "PYTEST_RESULT",
    "SLOW_RESULT",
    "MIGRATION_RESULT",
)
INFRASTRUCTURE_ENVIRONMENT: tuple[str, ...] = (
    "INFRASTRUCTURE_REQUIRED",
    "NODE_RESULT",
    "INSTALL_RESULT",
    "ASSERTIONS_RESULT",
    "SYNTH_RESULT",
)
# Each conditional CDK step, named as the workflow names it.
INFRASTRUCTURE_STEPS: tuple[tuple[str, str], ...] = (
    ("infra_node", "NODE_RESULT"),
    ("infra_install", "INSTALL_RESULT"),
    ("infra_assertions", "ASSERTIONS_RESULT"),
    ("infra_synth", "SYNTH_RESULT"),
)


def summary_expectations(environment: Mapping[str, str]) -> list[Expectation]:
    """The five gate jobs the required summary matches."""

    behavior = Answer("behavior_required", environment.get("BEHAVIOR_REQUIRED", ""))
    migration = Answer("migration_required", environment.get("MIGRATION_REQUIRED", ""))
    return [
        # `classify` and `check` run on every pull request, so the gate
        # requires them whatever the classifier answered -- and a dead
        # classifier leaves every answer empty, which is the case that must
        # never read as documentation-only.
        always_required("classify", environment.get("CLASSIFY_RESULT", "")),
        always_required("check", environment.get("CHECK_RESULT", "")),
        Expectation("pytest", behavior, environment.get("PYTEST_RESULT", "")),
        Expectation("slow", behavior, environment.get("SLOW_RESULT", "")),
        Expectation("migration", migration, environment.get("MIGRATION_RESULT", "")),
    ]


def infrastructure_expectations(environment: Mapping[str, str]) -> list[Expectation]:
    """The four conditional CDK steps inside the independent `check` job."""

    required = Answer(
        "infrastructure_required", environment.get("INFRASTRUCTURE_REQUIRED", "")
    )
    return [
        Expectation(label, required, environment.get(name, ""))
        for label, name in INFRASTRUCTURE_STEPS
    ]


SURFACES = {
    "infrastructure": infrastructure_expectations,
    "summary": summary_expectations,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "surface",
        choices=sorted(SURFACES),
        help="which set of results to match against the classifier",
    )
    arguments = parser.parse_args(argv)

    pairs = SURFACES[arguments.surface](os.environ)
    for line in report(pairs):
        print(line)
    # The exit code goes through `match`, the function the unit tests
    # exercise, so a case proven there is the case CI decides on.
    if match(pairs):
        print(f"{ERROR}release gate failed closed")
        return 1
    print("release gate satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
