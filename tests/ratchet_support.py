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
rather than showing a total going up.

Moving an existing reading into its own module adds a name without adding a
dependency, and containment cannot tell that from a new dependency. That is
the one case `assert_reviewed_relocations` below answers, and it answers it
with evidence rather than with an allowlist: a named implementation at the
merge base, a named implementation now, and the classes that moved between
them. It authorizes exactly the pairs it can prove, the caller subtracts those
from both sides of its own comparison, and `assert_ratchet` itself is as
strict as it ever was.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
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


# A card or pull request, written the way the rest of the repository writes
# one. The reason a relocation is allowed lives there and is read by a person;
# a declaration that names nowhere is not reviewed, it is just shorter.
_CARD = re.compile(r"#\d+|https://\S+")


@dataclass(frozen=True, slots=True)
class Relocation:
    """One reviewed extraction: these reads, serving this screen, moved here.

    Every field narrows the permission to a single act. The two readings are
    named implementations, not modules, so the claim is "this function's reads
    are that function's now" and never "this new module may consume whatever
    that old one did". `models` is exactly what moved. `card` is where the move
    is explained.
    """

    source: str
    source_reading: str
    destination: str
    destination_reading: str
    models: tuple[str, ...]
    card: str

    def __str__(self) -> str:
        return (
            f"{self.source}.{self.source_reading} -> "
            f"{self.destination}.{self.destination_reading} ({self.card})"
        )


def _module_file(source_root: str, module: str) -> str:
    return f"{source_root}/{module.replace('.', '/')}.py"


def _working_tree(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _executed(node: ast.AST):
    """Every node the tree runs: all of it but the annotations, which do not.

    `annotation` and `returns` are the two fields a type is written in, on
    `arg`, on `AnnAssign` and on the function itself. Everything else --
    decorators, defaults, the body -- runs.
    """

    yield node
    for field, value in ast.iter_fields(node):
        if field in ("annotation", "returns"):
            continue
        for child in value if isinstance(value, list) else (value,):
            if isinstance(child, ast.AST):
                yield from _executed(child)


def _named_in(
    source: str | None, reading: str, models: Collection[str]
) -> tuple[int, frozenset[str], frozenset[str]]:
    """One implementation's legacy classes: how many, which named, which run.

    Scoped to the named function deliberately. The question a relocation asks
    is what *that implementation* did, and a class named somewhere else in a
    7k-line route module is not an answer to it. Both forms the legacy census
    counts are read: the class itself in scope, and `models.X` off the imported
    schema package.

    Two answers, because a relocation asks two different questions of one
    function. Whether the dependency was ever this implementation's is answered
    by an argument's type as much as by a query, so the second value is
    everything it names. Whether it still runs here is not, because an
    annotation never ran, so the third value leaves them out -- which is how a
    stub can delegate its whole implementation and still type what it is handed.
    """

    if source is None:
        return 0, frozenset(), frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, frozenset(), frozenset()
    wanted = set(models)
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == reading
    ]
    legacy = lambda nodes: frozenset(
        node.id if isinstance(node, ast.Name) else node.attr
        for node in nodes
        if (isinstance(node, ast.Name) and node.id in wanted)
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "models"
            and node.attr in wanted
        )
    )
    named: set[str] = set()
    runs: set[str] = set()
    for definition in definitions:
        named |= legacy(ast.walk(definition))
        runs |= legacy(_executed(definition))
    return len(definitions), frozenset(named), frozenset(runs)


def declarations_at_merge_base(
    name: str, *, repository: Path = REPO_ROOT
) -> dict[tuple[str, str], frozenset[tuple[str, object]]] | None:
    """Every `Relocation` in `<path>:<CONSTANT>` at the merge base, by reading.

    `recorded_at_merge_base` cannot serve here: these are constructor calls, not
    a literal, and `ast.literal_eval` refuses them. Reading the keywords instead
    keeps the comparison to what the declaration *says*, so a reordered field or
    a reformatted line is the same declaration and a changed model set is not.

    `None` means the direction is unjudged -- no base ref, or the constant did
    not exist there -- exactly as `recorded_at_merge_base` returns `None`.
    """

    path, _, constant = name.partition(":")
    base = _merge_base(repository)
    if not base:
        return None
    source = _git(repository, "show", f"{base}:{path}")
    if source is None:
        return None
    for node in ast.walk(ast.parse(source)):
        target = getattr(node, "target", None)
        targets = getattr(node, "targets", [target] if target else [])
        if not any(isinstance(t, ast.Name) and t.id == constant for t in targets):
            continue
        declared: dict[tuple[str, str], frozenset[tuple[str, object]]] = {}
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call):
                continue
            try:
                said = {
                    keyword.arg: ast.literal_eval(keyword.value)
                    for keyword in call.keywords
                    if keyword.arg
                }
            except ValueError:
                return None
            if "source" not in said or "source_reading" not in said:
                continue
            declared[(said["source"], said["source_reading"])] = frozenset(said.items())
        return declared
    return None


def assert_reviewed_relocations(
    relocations: Sequence[Relocation],
    *,
    consumers: Mapping[str, Sequence[str]],
    source_root: str,
    declared: str | None = None,
    repository: Path = REPO_ROOT,
) -> frozenset[tuple[str, str]]:
    """The `(class, module)` pairs a reviewed extraction may add, and no others.

    `assert_ratchet` refuses every name that joined since the merge base, which
    is right for a new dependency and wrong for a move: lifting one screen's
    reading out of a route module adds the destination to the consumer census
    while the repository depends on nothing it did not already depend on. This
    is the only way a name joins. It is not an exception list -- a declaration
    buys nothing on its own, and neither does recording the destination in the
    census; each `Relocation` has to survive four checks against the actual
    merge base:

    * **the dependency existed.** At the merge base, `source_reading` itself
      named those classes -- in its own body or its own signature, an argument
      it was handed being as much its dependency as a query it ran. An import
      elsewhere in the source module is not evidence about this reading.
    * **the reading landed.** `destination_reading` names them now.
    * **it did not stay.** `source_reading` no longer *runs* any of them in the
      working tree, whether it delegates or is gone; two executable copies are
      a new consumer, not a move. An argument it still types is not a second
      copy, and other uses of the same classes elsewhere in the source module
      are none of this rule's business and are left alone.
    * **the destination took nothing else.** Its whole legacy surface is the
      reviewed extraction, so a module that already consumes a legacy class
      cannot be a destination.

    Those four run for a declaration this change is *making*. A declaration
    already present at the merge base, word for word, is historical: it
    authorizes nothing, is checked against nothing, and is not an error. Editing
    one is refused outright, because broadening a landed authorization is a new
    relocation wearing an old declaration's clothes.

    None of that says the reading still *means* what it meant. This reads
    imported and referenced class names; it cannot see project scoping,
    filters, ordering, missing-data behavior, or where authorization is
    applied. That the read's contract survived is established by code review
    and by focused behavior tests, and this guard is no evidence about it
    whatsoever.

    The permission is spent by the merge that uses it: from the next merge base
    on the declaration is historical, so it authorizes nothing and the
    destination stands in the recorded census on its own, counted like every
    other consumer. Whether the declaration is then deleted or kept as
    explanation is a matter of taste, and neither choice can reintroduce the
    dependency it once authorized. What this must never do is *fail* on it --
    an earlier version refused a landed declaration, which turned every
    relocation into a red `main` between two pull requests. None of this is
    progress: ADR-0081 stage 4 exits when no reader imports a legacy table
    module, and a relocation leaves the census one name longer than it found
    it. The census goes on reporting that.

    Without a base ref there is no evidence to check and no direction check to
    authorize, so this returns no pairs, exactly as `assert_ratchet` stops
    comparing.
    """

    base = _merge_base(repository)
    if base is None:
        return frozenset()

    historical = (
        declarations_at_merge_base(declared, repository=repository)
        if declared
        else None
    )
    legacy = set(consumers)
    authorized: set[tuple[str, str]] = set()
    transferred: dict[str, set[str]] = {}
    moved: set[tuple[str, str]] = set()
    for relocation in relocations:
        assert _CARD.fullmatch(relocation.card), (
            f"{relocation}: name the card or pull request the move is "
            "explained in"
        )
        models = relocation.models
        assert models and list(models) == sorted(set(models)), (
            f"{relocation}: names the classes it transfers, sorted and once"
        )
        unknown = sorted(set(models) - legacy)
        assert not unknown, (
            f"{relocation}: {', '.join(unknown)} is not a legacy class this "
            "census tracks"
        )
        reading = (relocation.source, relocation.source_reading)
        assert reading not in moved, (
            f"{relocation}: {relocation.source}.{relocation.source_reading} is "
            "already declared relocated. One reading moves once, to one "
            "destination; a spent declaration is not reusable"
        )
        moved.add(reading)

        if historical is not None:
            said = frozenset(
                {
                    "source": relocation.source,
                    "source_reading": relocation.source_reading,
                    "destination": relocation.destination,
                    "destination_reading": relocation.destination_reading,
                    "models": relocation.models,
                    "card": relocation.card,
                }.items()
            )
            was = historical.get(reading)
            if was == said:
                # Unchanged since the merge base: historical, and inert. It
                # authorizes nothing -- the destination is in the recorded
                # census on its own by now -- and it is not an error, because
                # the question this guard answers is whether *this change*
                # introduces an unauthorized dependency. Failing on a landed
                # declaration made the branch that introduced it go red the
                # moment it merged, which is a lifecycle defect and not the
                # rule doing its job.
                continue
            assert was is None, (
                f"{relocation}: {relocation.source}.{relocation.source_reading} "
                "is already declared relocated at the merge base, and this "
                "changes what that declaration says. A landed authorization is "
                "not editable -- broadening its models, destination or reading "
                "is a new relocation and needs its own review"
            )

        defined, before, _ = _named_in(
            _git(
                repository,
                "show",
                f"{base}:{_module_file(source_root, relocation.source)}",
            ),
            relocation.source_reading,
            legacy,
        )
        assert defined == 1, (
            f"{relocation}: {relocation.source} does not define exactly one "
            f"{relocation.source_reading} at the merge base. A relocation "
            "moves a reading that was there to move"
        )
        missing = sorted(set(models) - before)
        assert not missing, (
            f"{relocation}: at the merge base {relocation.source}."
            f"{relocation.source_reading} does not name {', '.join(missing)}. "
            f"An import elsewhere in {relocation.source} is not evidence about "
            "this reading, and a relocation that has already landed is spent "
            "-- delete the declaration"
        )

        defined, after, _ = _named_in(
            _working_tree(
                repository / _module_file(source_root, relocation.destination)
            ),
            relocation.destination_reading,
            legacy,
        )
        assert defined == 1, (
            f"{relocation}: {relocation.destination} does not define exactly "
            f"one {relocation.destination_reading}"
        )
        absent = sorted(set(models) - after)
        assert not absent, (
            f"{relocation}: {relocation.destination}."
            f"{relocation.destination_reading} does not name "
            f"{', '.join(absent)}; the reading has to land in the "
            "implementation the declaration names"
        )

        _, _, running = _named_in(
            _working_tree(
                repository / _module_file(source_root, relocation.source)
            ),
            relocation.source_reading,
            legacy,
        )
        kept = sorted(running & set(models))
        assert not kept, (
            f"{relocation}: {relocation.source}.{relocation.source_reading} "
            f"still reads {', '.join(kept)}. The relocated reading is gone or "
            "delegating; two executable copies are a new consumer, not a move"
        )

        transferred.setdefault(relocation.destination, set()).update(models)
        authorized.update((model, relocation.destination) for model in models)

    for destination, approved in sorted(transferred.items()):
        measured = {
            model for model, modules in consumers.items() if destination in modules
        }
        assert measured == approved, (
            f"{destination} consumes {', '.join(sorted(measured)) or 'nothing'} "
            f"and the reviewed relocation transfers {', '.join(sorted(approved))}"
            ". A relocation destination's whole legacy surface is the reads it "
            "was approved to receive"
        )
    return frozenset(authorized)
