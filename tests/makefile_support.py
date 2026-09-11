"""Read the Makefile the way an operator reads it: a name, a summary, a recipe.

`CLAUDE.md` calls the Makefile the discovery surface — "Every entry point is a
`make` target" — so tests that hold the operator interface to its promises
have to read it. Two of them reached in with their own
`split(f"\\n{target}:\\n")[1].split("\\n\\n")[0]`, which silently returns the
wrong block for a target written with a prerequisite, stops at the first blank
line inside a recipe, and cannot see the comment above a target at all. That
last blind spot is how the comment block documenting `make storage` came to
sit above `clean-test-databases`, which drops PostgreSQL databases, with
nothing to notice.

This module parses the file once into every target's summary comment and its
recipe. It reads the subset of GNU Make syntax this repository uses: targets
at column zero, recipes on tab-indented lines with backslash continuations,
and a summary written as the comment lines directly above the target, past any
`.PHONY` declaration or variable default written between the two.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"

_TARGET = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*:(?!=)")
_SKIPPED_ABOVE = re.compile(r"^\.PHONY\s*:|^[A-Za-z_][A-Za-z0-9_]*\s*[?:+]?=")
_MODULE = re.compile(r"python(?:3)? -m ([A-Za-z_][\w.]*)")
_SCRIPT = re.compile(r"python(?:3)? (scripts/[\w./-]+\.py)")
# Running pytest or compileall is running the toolchain, not a Corridor command.
_TOOLCHAIN_MODULES = frozenset({"pytest", "compileall"})


@dataclass(frozen=True)
class Target:
    """One `make` target: where it is, what it says it does, what it runs."""

    name: str
    line: int
    summary: tuple[str, ...]
    recipe: tuple[str, ...]

    @property
    def recipe_text(self) -> str:
        return "\n".join(self.recipe)


def _summary_above(lines: list[str], index: int) -> tuple[str, ...]:
    collected: list[str] = []
    cursor = index - 1
    while cursor >= 0:
        line = lines[cursor]
        if line.startswith("#"):
            collected.append(line.removeprefix("#").removeprefix(" ").rstrip())
        elif not _SKIPPED_ABOVE.match(line):
            break
        cursor -= 1
    return tuple(reversed(collected))


def _recipe_below(lines: list[str], index: int) -> tuple[str, ...]:
    collected: list[str] = []
    continued = False
    cursor = index + 1
    while cursor < len(lines) and lines[cursor].startswith("\t"):
        body = lines[cursor][1:].strip()
        continues = body.endswith("\\")
        body = body.removesuffix("\\").strip()
        if continued:
            collected[-1] = f"{collected[-1]} {body}"
        else:
            collected.append(body)
        continued = continues
        cursor += 1
    return tuple(collected)


def targets(makefile: Path = MAKEFILE) -> dict[str, Target]:
    """Every target in the file, in the order it declares them."""
    lines = makefile.read_text(encoding="utf-8").splitlines()
    found: dict[str, Target] = {}
    for index, line in enumerate(lines):
        match = _TARGET.match(line)
        if match is None:
            continue
        name = match.group("name")
        found[name] = Target(
            name=name,
            line=index + 1,
            summary=_summary_above(lines, index),
            recipe=_recipe_below(lines, index),
        )
    return found


def recipe(name: str, makefile: Path = MAKEFILE) -> str:
    """The recipe of one target, one command per line, continuations joined."""
    return targets(makefile)[name].recipe_text


def entry_point(target: Target, root: Path = ROOT) -> Path | None:
    """The Python file this target's recipe runs, or `None` when it runs none.

    A missing file is returned rather than hidden: a target that names a
    retired module must fail the check that calls this, not disappear from it.
    """
    match = _MODULE.search(target.recipe_text)
    if match and match.group(1) not in _TOOLCHAIN_MODULES:
        module = match.group(1).replace(".", "/")
        package = root / "src" / module / "__init__.py"
        return package if package.exists() else root / "src" / f"{module}.py"
    match = _SCRIPT.search(target.recipe_text)
    return root / match.group(1) if match else None


def parser_description(path: Path) -> tuple[bool, str]:
    """Whether the module builds an argparse parser, and the description it gives.

    `description=__doc__` and `description=__doc__.split(...)[0]` both resolve
    to the module docstring, which is the text `--help` actually prints.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    call = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
            == "ArgumentParser"
        ),
        None,
    )
    if call is None:
        return False, ""
    for keyword in call.keywords:
        if keyword.arg != "description":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return True, keyword.value.value
        segment = ast.get_source_segment(source, keyword.value) or ""
        if segment.startswith("__doc__"):
            return True, ast.get_docstring(tree) or ""
        return True, segment
    return True, ""
