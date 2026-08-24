"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[1] / "src" / "corridor"


def _module_paths() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path.name != "__init__.py" and "migrations" not in path.parts
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_every_source_module_opens_with_its_reason_for_existing():
    missing = [path.name for path in _module_paths() if ast.get_docstring(_tree(path)) is None]

    assert missing == []


def test_no_module_silently_replaces_a_top_level_interface_name():
    duplicates: dict[str, list[str]] = {}
    for path in _module_paths():
        definitions: dict[str, list[int]] = defaultdict(list)
        for node in _tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions[node.name].append(node.lineno)
        repeated = {
            name: lines for name, lines in definitions.items() if len(lines) > 1
        }
        if repeated:
            duplicates[path.name] = [
                f"{name}:{','.join(map(str, lines))}"
                for name, lines in sorted(repeated.items())
            ]

    assert duplicates == {}


def test_source_modules_do_not_import_another_module_private_implementation():
    private_imports: list[str] = []
    for path in _module_paths():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.module or not node.module.startswith("corridor."):
                continue
            for imported in node.names:
                if imported.name.startswith("_"):
                    private_imports.append(
                        f"{path.name}:{node.lineno} imports "
                        f"{node.module}.{imported.name}"
                    )

    assert private_imports == []


def test_source_module_dependencies_are_acyclic():
    paths = {path.stem: path for path in _module_paths()}
    dependencies: dict[str, set[str]] = {name: set() for name in paths}
    for name, path in paths.items():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.module or not node.module.startswith("corridor."):
                continue
            dependency = node.module.split(".", 1)[1].split(".", 1)[0]
            if dependency in paths:
                dependencies[name].add(dependency)

    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> list[str] | None:
        if name in visiting:
            start = visiting.index(name)
            return [*visiting[start:], name]
        if name in visited:
            return None
        visiting.append(name)
        for dependency in sorted(dependencies[name]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        visiting.pop()
        visited.add(name)
        return None

    cycles = []
    for name in sorted(paths):
        cycle = visit(name)
        if cycle is not None:
            cycles.append(" -> ".join(cycle))
            visiting.clear()

    assert cycles == []
