"""Share source parsing and the import graph across static guards.

Each guard used to re-implement "which modules import X" from raw `ast` nodes
and the copies did not agree on what an import is, so a rule could be evaded
by an import form its own scanner never learned. The graph lives here beside
the parse cache instead: `imported_names` knows every form once, and each
guard states only its policy.

Filesystem state is never cached. Each lookup still reads the current bytes,
so an edit with an unchanged size or timestamp cannot reuse an old tree. Only
the latest parse of each path is kept within the current guard module;
consumers treat its AST as read-only.
The imported module-scoped fixture releases every tree before unrelated
behavior tests run. Directory listings are always fresh and prune environment/
cache directories before descending into their installed packages.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import re

import pytest


@dataclass(frozen=True, slots=True)
class PythonSource:
    text: str
    tree: ast.Module
    nodes: tuple[ast.AST, ...]


_sources: dict[Path, PythonSource] = {}

_WORD = re.compile(r"[A-Za-z_]\w*")


@pytest.fixture(scope="module", autouse=True)
def source_scan_cache():
    """Share parses inside one guard module, then release its entire graph."""
    try:
        yield
    finally:
        _sources.clear()


def read_python(path: Path) -> PythonSource:
    """Parse the current contents once; never infer freshness from metadata."""
    text = path.read_text(encoding="utf-8")
    previous = _sources.get(path)
    if previous is not None and previous.text == text:
        return previous
    tree = ast.parse(text, filename=str(path))
    source = PythonSource(text, tree, tuple(ast.walk(tree)))
    _sources[path] = source
    return source


def _raise_walk_error(error: OSError) -> None:
    raise error


def python_files(root: Path) -> tuple[Path, ...]:
    """List every visible Python file, without entering hidden/cache trees."""
    paths = []
    for directory, children, files in os.walk(root, onerror=_raise_walk_error):
        children[:] = [
            name for name in children
            if not name.startswith(".") and name != "__pycache__"
        ]
        paths.extend(
            Path(directory) / name for name in files
            if name.endswith(".py") and not name.startswith(".")
        )
    return tuple(sorted(paths))


# Every absolute import form that names a module. Three forms reach one, and a
# hand-written scanner keeps learning only some of them: #548 found a guard
# seeing only `from pkg.module import name`, which "reported an acyclic graph
# that was not one", and the provider-spend guard written after it still saw
# two, so `from corridor_pdf_reader import textract_adapter` reached the
# adapter unseen. `from pkg import module` names a module too, so each
# imported name is offered as a submodule candidate and the caller's policy
# decides. Relative imports do not occur in the scanned trees and are skipped
# rather than guessed at.
def imported_names(path: Path) -> tuple[tuple[str, int], ...]:
    """(dotted module candidate, line) for every absolute import in one file."""
    names: list[tuple[str, int]] = []
    for node in read_python(path).nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names.append((node.module, node.lineno))
            names.extend(
                (f"{node.module}.{imported.name}", node.lineno)
                for imported in node.names
            )
        elif isinstance(node, ast.Import):
            names.extend((alias.name, node.lineno) for alias in node.names)
    return tuple(names)


def importers_of(
    prefix: str, roots: tuple[Path, ...]
) -> dict[Path, tuple[tuple[str, int], ...]]:
    """Every file under the roots that imports `prefix` or a module beneath it.

    The caller states which tree and which policy; it never learns what an
    `ast.ImportFrom` is. Each value holds the matching names and their lines,
    so a policy that allows one submodule and refuses its siblings can say so.
    """
    found: dict[Path, tuple[tuple[str, int], ...]] = {}
    for root in roots:
        for path in python_files(root):
            matches = tuple(
                (name, lineno)
                for name, lineno in imported_names(path)
                if name == prefix or name.startswith(f"{prefix}.")
            )
            if matches:
                found[path] = matches
    return found


def callers_of(
    symbols: Iterable[str], roots: tuple[Path, ...]
) -> dict[str, dict[Path, tuple[int, ...]]]:
    """Every site that calls, imports or otherwise names each symbol.

    The answer four per-symbol pins used to each walk the AST for. A name
    read, an attribute of that name, and an import of it are all references;
    the defining statement is not, so a symbol nothing reaches comes back with
    an empty mapping rather than with its own definition.
    """
    wanted = set(symbols)
    found: dict[str, dict[Path, list[int]]] = {name: {} for name in wanted}
    for root in roots:
        for path in python_files(root):
            for node in read_python(path).nodes:
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.alias):
                    name = node.name.rpartition(".")[2]
                else:
                    continue
                if name in wanted:
                    found[name].setdefault(path, []).append(node.lineno)
    return {
        name: {path: tuple(sorted(set(lines))) for path, lines in sorted(sites.items())}
        for name, sites in found.items()
    }


def mentions_of(
    symbols: Iterable[str], roots: tuple[Path, ...]
) -> dict[str, dict[Path, tuple[int, ...]]]:
    """Every line whose text writes each symbol as a whole word.

    Weaker than `callers_of`, deliberately. A name in a docstring, in an
    `__all__`, or inside the source of a probe another process runs is not a
    caller, but it is the repository writing the name. A rule whose failure
    means "delete this" asks the weaker question, so that it can only ever be
    wrong by letting something live.
    """
    wanted = set(symbols)
    found: dict[str, dict[Path, list[int]]] = {name: {} for name in wanted}
    for root in roots:
        for path in python_files(root):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for word in set(_WORD.findall(line)) & wanted:
                    found[word].setdefault(path, []).append(lineno)
    return {
        name: {path: tuple(lines) for path, lines in sorted(sites.items())}
        for name, sites in found.items()
    }
