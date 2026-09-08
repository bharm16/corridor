"""Share source parsing across static guards without caching filesystem state.

Each lookup still reads the current bytes, so an edit with an unchanged size
or timestamp cannot reuse an old tree. Only the latest parse of each path is
kept within the current guard module; consumers treat its AST as read-only.
The imported module-scoped fixture releases every tree before unrelated
behavior tests run. Directory listings are always fresh and prune environment/
cache directories before descending into their installed packages.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path

import pytest


@dataclass(frozen=True, slots=True)
class PythonSource:
    text: str
    tree: ast.Module
    nodes: tuple[ast.AST, ...]


_sources: dict[Path, PythonSource] = {}


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
