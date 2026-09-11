"""Let a "may fall and may never rise" ratchet see which way it moved.

Five guards describe themselves that way and none of them could tell. Each
asserts *equality* between what it measures now and a constant recorded in the
repository, so a change that edits the constant in the same commit passes: the
number is a record of the last measurement, not a floor. It has happened --
`LEGACY_TABLE_CONSUMERS` went 160 to 162 in one commit -- and
`src/corridor/migrations/policy.py` says the guard it replaced failed exactly
this way, "every migration-bearing change simply added its name".

So the comparison gains a second side. The recorded constant is read back out
of the merge base, and the measurement may not have risen against *that*. The
working-tree equality check is unchanged and always runs; the direction check
needs a base ref, which the `check` job's `fetch-depth: 0` clone supplies and
a shallow clone or a local run does not. Losing the history loses the
direction check, never the guard.

Where the unit is a name rather than a count the comparison is containment: a
name may leave and a name may not join, and the failure says which one joined
rather than showing a total going up. A module split does add a name, so it
fails here too -- as the two named lines it is, reviewed as one.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repository: Path, *arguments: str) -> str | None:
    """Run one read-only git command, or `None` when git cannot answer it."""
    try:
        finished = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return finished.stdout.strip() if finished.returncode == 0 else None


def _merge_base(repository: Path) -> str | None:
    """The commit this change forked from, when the clone still holds it.

    `CORRIDOR_RATCHET_BASE_REF` names the branch to compare against;
    `GITHUB_BASE_REF` carries it on a pull request, and `main` is the local
    default. A shallow clone has neither the branch nor the merge base, which
    is the case this returns `None` for.
    """
    branch = (
        os.environ.get("CORRIDOR_RATCHET_BASE_REF")
        or os.environ.get("GITHUB_BASE_REF")
        or "main"
    )
    for reference in (f"origin/{branch}", branch):
        base = _git(repository, "merge-base", "HEAD", reference)
        if base:
            return base
    return None


def _value(node: ast.AST, constants: dict[str, Any]) -> Any:
    """One literal from the base revision, resolving module-level names.

    `CYCLE_EDGE_ALLOWLIST` names its seam constants rather than repeating the
    reason on every line, so a plain `ast.literal_eval` cannot read it.
    """
    if isinstance(node, ast.Name):
        if node.id not in constants:
            raise ValueError(node.id)
        return constants[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_value(element, constants) for element in node.elts)
    if isinstance(node, (ast.List, ast.Set)):
        kind = list if isinstance(node, ast.List) else set
        return kind(_value(element, constants) for element in node.elts)
    if isinstance(node, ast.Dict):
        return {
            _value(key, constants): _value(value, constants)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    return ast.literal_eval(node)


def recorded_at_merge_base(name: str, *, repository: Path = REPO_ROOT) -> Any:
    """The value of `<path>:<CONSTANT>` at the merge base, or `None`.

    `None` means the direction is unjudged: no base ref resolved, the file or
    the constant did not exist there, or its value is not a literal this can
    read. A ratchet introduced by the very change under test lands here.
    """
    path, _, constant = name.partition(":")
    base = _merge_base(repository)
    if not base:
        return None
    source = _git(repository, "show", f"{base}:{path}")
    if source is None:
        return None
    try:
        tree = ast.parse(source, filename=f"{base}:{path}")
    except SyntaxError:
        return None
    constants: dict[str, Any] = {}
    found: ast.AST | None = None
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value
            else []
        )
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                constants[target.id] = _value(node.value, constants)
            except (ValueError, TypeError, SyntaxError):
                continue
            if target.id == constant:
                found = node.value
    if found is None:
        return None
    return constants[constant]


def assert_ratchet(
    name: str,
    *,
    measured: Any,
    recorded: Any,
    as_measured: Callable[[Any], Any] | None = None,
    repository: Path = REPO_ROOT,
) -> None:
    """Hold `measured` equal to `recorded`, and no higher than the merge base's.

    `name` is `<path>:<CONSTANT>`, the constant this guard records its last
    measurement in. `as_measured` is how the call site turns that constant
    into the shape it measures, for a ratchet whose recorded form carries more
    than the measurement does.
    """
    if isinstance(measured, (set, frozenset)) and isinstance(recorded, (set, frozenset)):
        difference = sorted(map(str, measured ^ set(recorded)))
        assert not difference, (
            f"{name} and what this guard measures differ on: "
            + ", ".join(difference)
        )
    else:
        assert measured == recorded, (
            f"{name} measures {measured} against the {recorded} recorded in "
            "the working tree"
        )
    base = recorded_at_merge_base(name, repository=repository)
    if base is None:
        return
    base = as_measured(base) if as_measured is not None else base
    if isinstance(measured, (set, frozenset)):
        joined = sorted(measured - set(base))
        assert not joined, (
            f"{name} has entries that joined since the merge base: "
            f"{', '.join(map(str, joined))}. This list may fall and may never "
            "rise; remove the cause instead of recording it"
        )
        return
    assert measured <= base, (
        f"{name} rose to {measured} from the {base} recorded at the merge "
        "base. This number may fall and may never rise; lower the cause "
        "instead of raising the record"
    )
