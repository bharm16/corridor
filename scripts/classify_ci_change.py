"""Decide which CI gates a pull request must run, from the paths it changes.

`release-gate.yml` is triggered on every pull request and is the single
required status. That is the whole point of this script's existence: a
workflow skipped by a **path filter on its trigger** never reports at all,
and a required status that never reports blocks the pull request forever, so
the expensive gates cannot be selected by trigger-level `paths` /
`paths-ignore` any more. They are selected by a job-level `if` instead, whose
skip produces a `skipped` result the summary job can inspect through
``needs.<job>.result``.

Two answers come out of here, and the summary job checks the jobs against
both of them rather than accepting `success or skipped`:

``behavior_required``
    the sharded `pytest` and `slow` gates must run.
``migration_required``
    the `migration` gate must run.

**Documentation is a narrow allowlist, not a `**.md` wildcard.** The
predecessor filter ignored every Markdown file, and Corridor loads executable
model prompts from Markdown at runtime: `src/corridor/extract_agreement.py`
reads `prompts/agreement_v3.md`, `extract_minutes_v4.py` and
`extract_minutes_v5.py` read theirs, and `extractor_lineage.py` names more. A
prompt-only pull request changes extraction behavior and skipped every
behavior suite. Only the files listed in ``DOCUMENTATION_PATHS`` are
documentation; everything else runs the behavior suites.

**Renames are counted under both names.** `git diff --name-status -M` reports
a rename as one record naming the old path and the new one, and this script
classifies both. Moving `prompts/agreement_v3.md` to `docs/agreement_v3.md`
therefore still reads as a behavior change, rather than making the executable
path vanish from the classification.

**An empty or unreadable diff is a failure, not a docs-only answer.** The
whole gate hangs off these two booleans; guessing `false` for them on a diff
that could not be read is exactly the silent green this classifier exists to
prevent.

Usage:
    python3 scripts/classify_ci_change.py --base <sha> --head <sha>

Standard library only, and no repository dependency: the `classify` job runs
it straight after checkout, without `uv sync`, so it adds no package download
to a gate whose measured ceiling is what this account serves concurrently.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Every path that is documentation and nothing else. Anything absent from
# this list runs the behavior suites -- in particular `prompts/**`, `src/**`,
# `tests/**`, `scripts/**`, `workers/**`, `.github/workflows/**`,
# `pyproject.toml`, `uv.lock` and `Makefile`.
DOCUMENTATION_PATHS: tuple[str, ...] = (
    "docs/**",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "CONTEXT-MAP.md",
    "roadmap.md",
)

# The migration-adjacent set carried over unchanged from the retired
# `migration-test.yml` trigger.
MIGRATION_PATHS: tuple[str, ...] = (
    "src/corridor/migrations/**",
    "src/corridor/models.py",
    "src/corridor/db.py",
    "src/corridor/m8_acceptance_database.py",
    "tests/conftest.py",
    "tests/test_*migration*.py",
    "tests/test_m8_acceptance_integrity.py",
    "alembic.ini",
    "pyproject.toml",
    "Makefile",
    ".github/workflows/**",
)


class ClassificationError(RuntimeError):
    """The change could not be classified, so no gate may be skipped."""


@dataclass(frozen=True)
class Classification:
    behavior_required: bool
    migration_required: bool
    paths: tuple[str, ...]

    def as_outputs(self) -> str:
        return (
            f"behavior_required={str(self.behavior_required).lower()}\n"
            f"migration_required={str(self.migration_required).lower()}\n"
        )


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub-style path filter into an anchored regex.

    `*` stops at a path separator, `**` crosses them, which is what the
    workflow trigger filters these lists came from meant.
    """

    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


DOCUMENTATION_MATCHERS = tuple(_compile(pattern) for pattern in DOCUMENTATION_PATHS)
MIGRATION_MATCHERS = tuple(_compile(pattern) for pattern in MIGRATION_PATHS)


def is_documentation(path: str) -> bool:
    return any(matcher.match(path) for matcher in DOCUMENTATION_MATCHERS)


def is_migration_adjacent(path: str) -> bool:
    return any(matcher.match(path) for matcher in MIGRATION_MATCHERS)


def classify(paths: object) -> Classification:
    """Classify an already-collected set of changed paths.

    An empty set is a failure: see the module docstring.
    """

    ordered = tuple(sorted({str(path) for path in paths}))
    if not ordered:
        raise ClassificationError(
            "no changed paths: the classifier cannot prove a gate is "
            "unnecessary, so it fails instead of answering docs-only"
        )
    return Classification(
        behavior_required=any(not is_documentation(path) for path in ordered),
        migration_required=any(is_migration_adjacent(path) for path in ordered),
        paths=ordered,
    )


def _parse_name_status(payload: str) -> tuple[str, ...]:
    """Read `git diff --name-status -M -z`, keeping both names of a rename.

    The NUL-delimited form is a status token followed by one path, or -- for
    `R` and `C` -- by the old path and then the new one. Both are kept, so a
    runtime prompt moved into a documentation directory still classifies
    under its executable name.
    """

    fields = [field for field in payload.split("\0") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        follows = 2 if status[:1] in {"R", "C"} else 1
        if index + follows > len(fields):
            raise ClassificationError(
                f"truncated git diff record for status {status!r}"
            )
        paths.extend(fields[index : index + follows])
        index += follows
    return tuple(paths)


def changed_paths(base: str, head: str, repository: Path | None = None) -> tuple[str, ...]:
    """Paths the pull request itself changes, both names of every rename.

    Three-dot: the comparison is against the merge base, so commits that
    landed on the base branch after the pull request opened do not widen the
    classification.
    """

    try:
        completed = subprocess.run(
            ["git", "diff", "--name-status", "-M", "-z", f"{base}...{head}"],
            cwd=str(repository) if repository else None,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ClassificationError(
            f"could not read the diff {base}...{head}: {detail.strip()}"
        ) from error
    return _parse_name_status(completed.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="pull request base SHA")
    parser.add_argument("--head", required=True, help="pull request head SHA")
    parser.add_argument(
        "--repository",
        type=Path,
        default=None,
        help="repository to inspect (default: the working directory)",
    )
    arguments = parser.parse_args(argv)

    try:
        classification = classify(
            changed_paths(arguments.base, arguments.head, arguments.repository)
        )
    except ClassificationError as error:
        print(f"classification failed: {error}", file=sys.stderr)
        return 1

    for path in classification.paths:
        marks = []
        if not is_documentation(path):
            marks.append("behavior")
        if is_migration_adjacent(path):
            marks.append("migration")
        print(f"  {path} -> {', '.join(marks) or 'documentation'}", file=sys.stderr)

    outputs = classification.as_outputs()
    sys.stdout.write(outputs)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
