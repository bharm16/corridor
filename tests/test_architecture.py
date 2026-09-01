"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "corridor"


def _module_paths() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path.name != "__init__.py" and "migrations" not in path.parts
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts)


def test_every_source_module_opens_with_its_reason_for_existing():
    missing = [
        path.name for path in _module_paths() if ast.get_docstring(_tree(path)) is None
    ]

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
    paths = {_module_name(path): path for path in _module_paths()}
    dependencies: dict[str, set[str]] = {name: set() for name in paths}
    for name, path in paths.items():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.module or not node.module.startswith("corridor."):
                continue
            dependency = node.module.removeprefix("corridor.")
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


def test_context_map_names_both_bounded_context_glossaries():
    context_map = (REPO_ROOT / "CONTEXT-MAP.md").read_text()

    assert "[Project Record](./CONTEXT.md)" in context_map
    assert "[Corridor Operations](./docs/operations/CONTEXT.md)" in context_map
    assert (REPO_ROOT / "CONTEXT.md").is_file()
    assert (REPO_ROOT / "docs" / "operations" / "CONTEXT.md").is_file()


def _adr_index_module():
    spec = importlib.util.spec_from_file_location(
        "adr_index", REPO_ROOT / "scripts" / "adr_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_adr_declares_machine_readable_status():
    """The lifecycle rules in docs/adr/README.md, mechanically."""
    adr_index = _adr_index_module()
    problems = []
    for path in sorted((REPO_ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        lines = path.read_text().splitlines()
        if len(lines) < 3 or lines[0] != "---" or not lines[1].startswith("status: "):
            problems.append(f"{path.name}: frontmatter must open with status")
    problems.extend(adr_index.validate(adr_index.load_adrs()))

    assert problems == []


def test_adr_lifecycle_graph_has_no_cycles():
    adr_index = _adr_index_module()
    adrs = adr_index.load_adrs()
    edges = defaultdict(set)
    for adr in adrs.values():
        for older in adr.supersedes + adr.amends:
            edges[older[4:]].add(adr.number)
    visiting: list[str] = []
    done: set[str] = set()
    cycles = []

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join(visiting[visiting.index(node):] + [node]))
            return
        visiting.append(node)
        for successor in sorted(edges[node]):
            visit(successor)
        visiting.pop()
        done.add(node)

    for number in sorted(adrs):
        visit(number)

    assert cycles == []


def test_adr_index_matches_frontmatter():
    adr_index = _adr_index_module()
    adrs = adr_index.load_adrs()
    expected = adr_index.render(adrs, adr_index.citing_modules(adrs))

    assert adr_index.INDEX_PATH.read_text(encoding="utf-8") == expected, (
        "docs/adr/INDEX.md is stale; run `make adr-index`"
    )


def test_database_upgrade_tests_are_one_explicitly_marked_baseline_contract():
    paths = sorted((REPO_ROOT / "tests").glob("test_*migration*.py"))

    assert [path.name for path in paths] == ["test_migration_baseline.py"]
    missing = [
        path.name
        for path in paths
        if "pytest.mark.migration" not in path.read_text()
    ]

    assert missing == []


def test_released_policy_sources_are_outside_executable_migration_history():
    config = (REPO_ROOT / "alembic.ini").read_text()
    assert (
        "version_locations = %(here)s/src/corridor/migrations/baseline_versions"
        in config
    )
    assert len(
        tuple((SOURCE_ROOT / "migrations" / "versions").glob("*.py"))
    ) == 13
