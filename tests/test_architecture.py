"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

from corridor.migrations import policy


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


# The modules between a Source Segment and a Source Fact value, and the
# record-decision boundary above them (#446). None may reach a model client,
# directly or through anything it imports.
FACT_PATH_MODULES = (
    "materializer",
    "facts",
    "source_append",
    "source_segments",
    "fact_types",
    "fact_decisions",
)
MODEL_CLIENT_MODULES = frozenset({"llm"})
MODEL_CLIENT_PACKAGES = frozenset({"httpx", "openai"})


def _internal_dependencies() -> dict[str, set[str]]:
    paths = {_module_name(path): path for path in _module_paths()}
    dependencies: dict[str, set[str]] = {name: set() for name in paths}
    for name, path in paths.items():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("corridor."):
                    dependency = node.module.removeprefix("corridor.")
                    if dependency in paths:
                        dependencies[name].add(dependency)
                elif node.module.split(".")[0] in MODEL_CLIENT_PACKAGES:
                    dependencies[name].add(f"<{node.module.split('.')[0]}>")
            elif isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name.startswith("corridor."):
                        dependency = imported.name.removeprefix("corridor.")
                        if dependency in paths:
                            dependencies[name].add(dependency)
                    elif imported.name.split(".")[0] in MODEL_CLIENT_PACKAGES:
                        dependencies[name].add(f"<{imported.name.split('.')[0]}>")
    return dependencies


def test_fact_path_modules_cannot_reach_a_model_client():
    """A model chooses a segment; it never supplies a value (#446).

    The materializer, the fact appenders, the append commands, the segment
    reader, the contracts, and the record-decision commands must not be able
    to call a model even by accident, so the whole import closure of each is
    checked, not only its own import lines.
    """
    dependencies = _internal_dependencies()
    reached: dict[str, list[str]] = {}
    for module in FACT_PATH_MODULES:
        assert module in dependencies, f"{module} is not a source module"
        closure: set[str] = set()
        frontier = [module]
        while frontier:
            current = frontier.pop()
            for dependency in dependencies.get(current, ()):
                if dependency not in closure:
                    closure.add(dependency)
                    frontier.append(dependency)
        offending = sorted(
            name
            for name in closure
            if name in MODEL_CLIENT_MODULES or name.startswith("<")
        )
        if offending:
            reached[module] = offending

    assert reached == {}


def test_only_the_materializer_constructs_a_materialized_value():
    """The type boundary, statically: ``MaterializedValue(`` appears in one module."""
    constructors = []
    for path in _module_paths():
        if path.name == "materializer.py":
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "MaterializedValue"
            ):
                constructors.append(f"{path.name}:{node.lineno}")

    assert constructors == []


def test_model_output_schemas_carry_references_not_values():
    """A strict model output holds ids, enumerations, and dispositions only (#446).

    Free text and unbounded numbers are values; a schema that admitted one
    would give a model literal a field to land in before the materializer.
    """
    import importlib
    import typing

    from corridor.typed_output import StrictOutputModel

    for path in _module_paths():
        if "StrictOutputModel" in path.read_text(encoding="utf-8"):
            importlib.import_module(f"corridor.{_module_name(path)}")

    def subclasses(model_type):
        for child in model_type.__subclasses__():
            yield child
            yield from subclasses(child)

    def admits_only_references(annotation) -> bool:
        origin = typing.get_origin(annotation)
        if origin is typing.Literal:
            return True
        if annotation in (int, bool):
            return True
        if isinstance(annotation, type) and issubclass(annotation, StrictOutputModel):
            return True
        if origin in (tuple, list, frozenset, set, typing.Union) or (
            origin is not None and origin.__name__ == "UnionType"
        ):
            return all(
                argument is Ellipsis or argument is type(None)
                or admits_only_references(argument)
                for argument in typing.get_args(annotation)
            )
        return False

    values = []
    for model_type in subclasses(StrictOutputModel):
        if not model_type.__module__.startswith("corridor."):
            continue
        for name, info in model_type.model_fields.items():
            if not admits_only_references(info.annotation):
                values.append(f"{model_type.__name__}.{name}: {info.annotation}")

    assert values == []


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


def test_no_application_module_binds_the_schema_owner_credential():
    """An application process uses a capability login, never the owner (#492).

    `corridor.db.Session` and `corridor.db.engine` carry the migration
    credential that owns the schema. The database refuses accepted-authority
    writes to the capability logins, so a module that quietly imported the
    owner binding would hold authority the boundary is meant to deny.
    """

    offenders = []
    for path in sorted((REPO_ROOT / "src" / "corridor").rglob("*.py")):
        if path.name == "db.py":
            continue
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith("from corridor.db import"):
                continue
            imported = stripped.removeprefix("from corridor.db import")
            names = {
                part.split(" as ")[0].strip() for part in imported.split(",")
            }
            if names & {"Session", "engine"}:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: {stripped}"
                )

    assert offenders == [], (
        "application modules must import WebSession, WorkerSession, "
        "web_engine, or worker_engine: " + "; ".join(offenders)
    )


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


def test_every_spine_dependent_table_is_covered_by_the_committed_scenario_cleanup():
    """A committed test scenario deletes the spine by name pattern (#521).

    Any table that references the spine roots, directly or through another
    spine table, must match the pattern and carry project_id; otherwise a
    committed scenario could leave its rows behind for a later module.
    """
    import importlib

    spine_support = importlib.import_module("spine_support")
    from corridor.models import Base

    referencing: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            referencing.setdefault(fk.column.table.name, set()).add(table.name)
    dependent: set[str] = set()
    frontier = set(spine_support.SPINE_ROOTS)
    while frontier:
        name = frontier.pop()
        for child in referencing.get(name, ()):
            if child not in dependent:
                dependent.add(child)
                frontier.add(child)
    covered = {table.name for table in spine_support.SPINE_TABLES}

    # The entity-resolution subsystem also references source_segments but is
    # not part of the human-decision spine dual-write, and no committed test
    # scenario creates its rows; two of its tables are not even project-scoped.
    # It is classified out explicitly so that a genuinely new spine table
    # (a support-assessment, proposed-delta, or decision table) cannot be
    # added without either matching the cleanup pattern or being classified
    # here on purpose.
    resolution_subsystem = {
        "subject_resolution_attempts",
        "subject_resolution_candidates",
        "subject_resolution_decisions",
        "subject_candidate_suggestions",
    }
    uncovered = sorted(
        name
        for name in (dependent | set(spine_support.SPINE_ROOTS))
        if name not in covered and name not in resolution_subsystem
    )
    missing_project_id = sorted(
        table.name for table in spine_support.SPINE_TABLES if "project_id" not in table.c
    )

    assert uncovered == [], (
        "a new spine-dependent table is not covered by the committed-scenario "
        f"cleanup; extend SPINE_TABLE_PATTERN in tests/spine_support.py or "
        f"classify it in test_architecture.py: {uncovered}"
    )
    assert missing_project_id == []


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
    # The inert directory holds the retained source bytes of every revision
    # the executable graph no longer runs: thirteen from #423, plus the
    # twenty-three consolidated into the current baseline by #548. It only
    # grows, and never becomes executable.
    retained = tuple((SOURCE_ROOT / "migrations" / "versions").glob("*.py"))
    assert len(retained) == 36
    executable = tuple(
        (SOURCE_ROOT / "migrations" / "baseline_versions").glob("*.py")
    )
    assert len(executable) == 1 + policy.UNRELEASED_EDGES, (
        "the executable graph is one consolidated baseline plus the "
        "transitions src/corridor/migrations/policy.py records"
    )
