"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections import defaultdict
from pathlib import Path

from corridor.migrations import policy
from source_scan_support import python_files, read_python, source_scan_cache  # noqa: F401


REPO_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "corridor"


def _module_paths() -> tuple[Path, ...]:
    return tuple(
        path
        for path in python_files(SOURCE_ROOT)
        if path.name != "__init__.py" and "migrations" not in path.parts
    )


def _tree(path: Path) -> ast.Module:
    return read_python(path).tree


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts)


# Every absolute import form that names a Corridor module. Three forms reach
# one, and the guards below used to see only the first: `from corridor.x import
# y`, `from corridor import x` (104 sites, and the form that hides most of the
# graph), and `import corridor.x`. `from corridor.pkg import module` names a
# module too, so each imported name is offered as a submodule candidate and the
# module table decides. Relative imports do not occur in this tree and are
# skipped rather than guessed at.
def _imported_module_names(nodes: tuple[ast.AST, ...]) -> tuple[tuple[str, int], ...]:
    """(dotted module candidate, line) for every absolute import in one file."""
    names: list[tuple[str, int]] = []
    for node in nodes:
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


def _corridor_import_edges() -> dict[tuple[str, str], tuple[int, ...]]:
    """Every import edge between two source modules, with the lines that make it."""
    paths = {_module_name(path): path for path in _module_paths()}
    edges: dict[tuple[str, str], set[int]] = {}
    for name, path in paths.items():
        for imported, lineno in _imported_module_names(read_python(path).nodes):
            if not imported.startswith("corridor."):
                continue
            dependency = imported.removeprefix("corridor.")
            if dependency in paths and dependency != name:
                edges.setdefault((name, dependency), set()).add(lineno)
    return {edge: tuple(sorted(lines)) for edge, lines in sorted(edges.items())}


def _module_dependencies() -> dict[str, set[str]]:
    """The source import graph: every module, and the modules it imports."""
    dependencies: dict[str, set[str]] = {
        _module_name(path): set() for path in _module_paths()
    }
    for source, target in _corridor_import_edges():
        dependencies[source].add(target)
    return dependencies


def _edges_on_a_cycle(dependencies: dict[str, set[str]]) -> tuple[tuple[str, str], ...]:
    """Exactly the edges a cycle runs through: ones whose target reaches back.

    This is the canonical answer, not one arbitrary feedback arc set, so the
    declared list below can be compared for equality. An edge inside a
    strongly connected component qualifies; every other edge does not.
    """

    reachable: dict[str, set[str]] = {}
    for start in dependencies:
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            for dependency in dependencies[frontier.pop()]:
                if dependency not in seen:
                    seen.add(dependency)
                    frontier.append(dependency)
        reachable[start] = seen
    return tuple(
        sorted(
            (source, target)
            for source, targets in dependencies.items()
            for target in targets
            if source in reachable[target]
        )
    )


def test_every_source_module_opens_with_its_reason_for_existing():
    missing = [
        path.name for path in _module_paths() if ast.get_docstring(_tree(path)) is None
    ]

    assert missing == []


def test_control_plane_metadata_has_no_customer_content_relations():
    """The external receipt store cannot acquire a Project Record relation."""
    from corridor.control_plane_schema import CONTROL_PLANE_METADATA
    from corridor.models import Base

    tables = CONTROL_PLANE_METADATA.tables
    assert set(tables) == {
        "control_plane.customer_environments",
        "control_plane.destruction_receipts",
        "control_plane.disposition_plans",
        "control_plane.disposition_rehearsal_receipts",
    }
    assert {table.name for table in tables.values()}.isdisjoint(Base.metadata.tables)
    assert all(foreign_key.column.table.metadata is CONTROL_PLANE_METADATA for table in tables.values() for foreign_key in table.foreign_keys)
    assert all(not column.name.endswith(("_json", "_bytes", "_text")) for table in tables.values() for column in table.columns)


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
    """Every import form, so `from corridor import _x` cannot slip past."""
    private_imports: list[str] = []
    for path in _module_paths():
        for imported, lineno in _imported_module_names(read_python(path).nodes):
            if imported.startswith("corridor.") and imported.split(".")[-1].startswith("_"):
                private_imports.append(f"{path.name}:{lineno} imports {imported}")

    assert sorted(set(private_imports)) == []


# The import cycles that exist today, edge by edge (ADR-0081).
#
# Teaching the guard `from corridor import x` made two strongly connected
# components visible — 25 modules across the legacy record path and 11 across
# the release readers — that a `corridor.x`-only scan never saw. The cycles are
# real, they are the work of later cards in this audit, and this list is the
# ratchet that holds them still meanwhile: the same "may fall and may never
# rise" shape the unpartitioned-relation list uses (#548). Every edge a cycle
# runs through must be named here, and an edge that stops closing a cycle must
# be deleted from the list, so an added cycle fails the build and a repaired
# seam cannot pay for it. The reason on each edge names the seam that removes
# it; the import statement lives in the source module, so the source module's
# seam owns the edge.
_EXTRACTION_LINEAGE = (
    "extractor lineage and run recording import the extractors and admission "
    "they record, which import them back; the extraction-lineage card leaves "
    "recording downstream of the run that produced it"
)
_STATEMENT_ADMISSION = (
    "statement admission, the external-statement reader and the scope matcher "
    "all reach into each other and into dependency_events; the statement-spine "
    "card leaves admission depending on the spine alone"
)
_LEGACY_RECORD_READERS = (
    "legacy Constraint Record modules read each other's projections in both "
    "directions; ADR-0081 stage 4 moves each reader onto the spine projection "
    "and deletes the legacy import"
)
_SUPPORT_TRANSFER = (
    "supersession review, support transfer and its lineage recorder import "
    "each other, operative_support and revision_comparison; the support-"
    "assessment card gives that group one downward direction"
)
_NATIVE_READER_COVERAGE = (
    "the native-coverage and equivalence reports import the readers they "
    "measure while those readers import current_record; the reader-equivalence "
    "card measures through a declared registry instead"
)
_RELEASE_ASSEMBLY = (
    "release candidate assembly, packet review and the issue renderers import "
    "each other; the release-assembly card separates assembling a release from "
    "rendering one"
)

CYCLE_EDGE_ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    # extraction lineage
    ("admission", "dependency_admission", _EXTRACTION_LINEAGE),
    ("admission", "event_admission", _EXTRACTION_LINEAGE),
    ("admission", "extraction_runs", _EXTRACTION_LINEAGE),
    ("dependency_admission", "adjudicate", _EXTRACTION_LINEAGE),
    ("dependency_admission", "extraction_runs", _EXTRACTION_LINEAGE),
    ("extract_agreement", "extract_batch", _EXTRACTION_LINEAGE),
    ("extract_batch", "admission", _EXTRACTION_LINEAGE),
    ("extract_batch", "extraction_runs", _EXTRACTION_LINEAGE),
    ("extract_batch", "extractor_lineage", _EXTRACTION_LINEAGE),
    ("extraction_runs", "extractor_lineage", _EXTRACTION_LINEAGE),
    ("extractor_lineage", "extract_agreement", _EXTRACTION_LINEAGE),
    ("extractor_lineage", "key_date_table", _EXTRACTION_LINEAGE),
    ("key_date_table", "extraction_runs", _EXTRACTION_LINEAGE),
    ("key_date_table", "extractor_lineage", _EXTRACTION_LINEAGE),
    # statement admission
    ("event_admission", "dependency_events", _STATEMENT_ADMISSION),
    ("event_admission", "external_statements", _STATEMENT_ADMISSION),
    ("event_admission", "statement_scope_matching", _STATEMENT_ADMISSION),
    ("external_statements", "dependency_events", _STATEMENT_ADMISSION),
    ("statement_scope_matching", "dependency_events", _STATEMENT_ADMISSION),
    ("statement_scope_matching", "external_statements", _STATEMENT_ADMISSION),
    # legacy record readers
    ("accepted_field_reading", "adjudicate", _LEGACY_RECORD_READERS),
    ("adjudicate", "disputes", _LEGACY_RECORD_READERS),
    ("adjudicate", "operative_support", _LEGACY_RECORD_READERS),
    ("adjudicate", "supersession_review", _LEGACY_RECORD_READERS),
    ("check_configuration", "exceptions", _LEGACY_RECORD_READERS),
    ("condition_tracking", "dependency_events", _LEGACY_RECORD_READERS),
    ("dependency_events", "work_decisions", _LEGACY_RECORD_READERS),
    ("disputes", "notifications", _LEGACY_RECORD_READERS),
    ("disputes", "work_decisions", _LEGACY_RECORD_READERS),
    ("documentation_checklist", "condition_tracking", _LEGACY_RECORD_READERS),
    ("documentation_checklist", "work_decisions", _LEGACY_RECORD_READERS),
    ("exceptions", "accepted_field_reading", _LEGACY_RECORD_READERS),
    ("exceptions", "dependency_events", _LEGACY_RECORD_READERS),
    ("exceptions", "disputes", _LEGACY_RECORD_READERS),
    ("exceptions", "operative_support", _LEGACY_RECORD_READERS),
    ("notifications", "check_configuration", _LEGACY_RECORD_READERS),
    ("notifications", "dependency_events", _LEGACY_RECORD_READERS),
    ("notifications", "exceptions", _LEGACY_RECORD_READERS),
    ("operative_support", "dependency_events", _LEGACY_RECORD_READERS),
    ("operative_support", "documentation_checklist", _LEGACY_RECORD_READERS),
    ("work_decisions", "notifications", _LEGACY_RECORD_READERS),
    # support transfer
    ("revision_comparison", "extraction_runs", _SUPPORT_TRANSFER),
    ("supersession_review", "extraction_runs", _SUPPORT_TRANSFER),
    ("supersession_review", "operative_support", _SUPPORT_TRANSFER),
    ("supersession_review", "revision_comparison", _SUPPORT_TRANSFER),
    ("supersession_review", "support_transfer", _SUPPORT_TRANSFER),
    ("supersession_review", "support_transfer_lineage", _SUPPORT_TRANSFER),
    ("support_transfer", "extraction_runs", _SUPPORT_TRANSFER),
    ("support_transfer", "operative_support", _SUPPORT_TRANSFER),
    ("support_transfer", "revision_comparison", _SUPPORT_TRANSFER),
    ("support_transfer", "support_transfer_lineage", _SUPPORT_TRANSFER),
    ("support_transfer_lineage", "extraction_runs", _SUPPORT_TRANSFER),
    ("support_transfer_lineage", "operative_support", _SUPPORT_TRANSFER),
    ("support_transfer_lineage", "revision_comparison", _SUPPORT_TRANSFER),
    # native reader coverage
    ("current_record", "reader_equivalence", _NATIVE_READER_COVERAGE),
    ("native_reader_coverage", "packet_review", _NATIVE_READER_COVERAGE),
    ("native_reader_coverage", "release_authorization", _NATIVE_READER_COVERAGE),
    ("native_reader_coverage", "release_candidate", _NATIVE_READER_COVERAGE),
    ("native_reader_coverage", "workbook_render", _NATIVE_READER_COVERAGE),
    ("reader_equivalence", "native_reader_coverage", _NATIVE_READER_COVERAGE),
    ("workbook_render", "current_record", _NATIVE_READER_COVERAGE),
    # release assembly
    ("follow_up_bundles", "project_workflow", _RELEASE_ASSEMBLY),
    ("issue_coverage", "issue_rendering", _RELEASE_ASSEMBLY),
    ("issue_rendering", "current_record", _RELEASE_ASSEMBLY),
    ("packet_review", "issue_coverage", _RELEASE_ASSEMBLY),
    ("project_workflow", "packet_review", _RELEASE_ASSEMBLY),
    ("release_authorization", "release_candidate", _RELEASE_ASSEMBLY),
    ("release_candidate", "follow_up_bundles", _RELEASE_ASSEMBLY),
    ("release_candidate", "issue_coverage", _RELEASE_ASSEMBLY),
    ("release_candidate", "issue_rendering", _RELEASE_ASSEMBLY),
    ("release_candidate", "workbook_render", _RELEASE_ASSEMBLY),
)


def test_the_import_scanner_sees_every_form_of_dependency():
    """The graph is only as honest as the scanner behind it (#548 shape).

    `from corridor import x` is how most of this codebase imports a sibling,
    and a scanner that only understood `from corridor.x import y` reported an
    acyclic graph that was not one.
    """

    cases = {
        "from corridor.exceptions import review\n": {"corridor.exceptions", "corridor.exceptions.review"},
        "from corridor import disputes, notifications\n": {"corridor", "corridor.disputes", "corridor.notifications"},
        "import corridor.work_decisions\n": {"corridor.work_decisions"},
        "from corridor.web import app\n": {"corridor.web", "corridor.web.app"},
        "from . import sibling\n": set(),
        "import httpx\n": {"httpx"},
    }

    assert {
        source: {name for name, _ in _imported_module_names(tuple(ast.walk(ast.parse(source))))}
        for source in cases
    } == cases


def test_source_module_dependencies_are_acyclic():
    """Acyclic once the declared cycle edges are set aside, and only those.

    The allowlist above is the measured remainder; every other cycle is a
    defect this test reports as the path that closes it.
    """

    dependencies = _module_dependencies()
    declared = {(source, target) for source, target, _ in CYCLE_EDGE_ALLOWLIST}
    remaining = {
        name: {target for target in targets if (name, target) not in declared}
        for name, targets in dependencies.items()
    }

    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> list[str] | None:
        if name in visiting:
            start = visiting.index(name)
            return [*visiting[start:], name]
        if name in visited:
            return None
        visiting.append(name)
        for dependency in sorted(remaining[name]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        visiting.pop()
        visited.add(name)
        return None

    cycles = []
    for name in sorted(remaining):
        cycle = visit(name)
        if cycle is not None:
            cycles.append(" -> ".join(cycle))
            visiting.clear()

    assert cycles == []


def test_the_declared_cycle_edges_are_exactly_the_cycles_that_exist():
    """The ratchet, exact in both directions.

    An import that closes a new cycle fails here rather than hiding inside a
    component that was already tangled, and an edge that stops closing one has
    to leave the list, so the list can only shrink and only honestly.
    """

    edges = [(source, target) for source, target, _ in CYCLE_EDGE_ALLOWLIST]
    assert len(set(edges)) == len(edges), "the allowlist names each edge once"
    # One contiguous run per seam, sorted inside it, so a diff reads as a seam
    # rather than as scattered lines.
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    for source, target, reason in CYCLE_EDGE_ALLOWLIST:
        if not groups or groups[-1][0] != reason:
            groups.append((reason, []))
        groups[-1][1].append((source, target))
    assert len(groups) == len({reason for reason, _ in groups}), (
        "every edge of one seam is listed together"
    )
    assert all(members == sorted(members) for _, members in groups), (
        "each seam lists its edges in sorted order"
    )
    vague = sorted(
        f"{source} -> {target}"
        for source, target, reason in CYCLE_EDGE_ALLOWLIST
        if len(reason.split()) < 8
    )
    assert vague == [], "each declared cycle edge says which seam removes it"

    dependencies = _module_dependencies()
    unknown = sorted(
        f"{source} -> {target}"
        for source, target in edges
        if source not in dependencies or target not in dependencies
    )
    assert unknown == [], "the allowlist names modules that no longer exist"

    declared = set(edges)
    found = set(_edges_on_a_cycle(dependencies))
    sites = _corridor_import_edges()
    problems: dict[str, str] = {}
    for source, target in sorted(declared | found):
        if (source, target) not in declared:
            lines = ", ".join(str(line) for line in sites[(source, target)])
            problems[f"{source} -> {target}"] = (
                f"closes an import cycle, imported at {source}.py:{lines}; break "
                "the cycle instead of widening the list"
            )
        elif (source, target) not in found:
            problems[f"{source} -> {target}"] = (
                "no longer lies on a cycle; delete its allowlist line to hold "
                "the gain"
            )

    assert problems == {}


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
    """The source import graph plus a `<package>` node per model-client package.

    Built on the same scanner the acyclicity guard uses, so a `from corridor
    import x` edge cannot hide a path from a fact module to a model client.
    """

    dependencies = _module_dependencies()
    for path in _module_paths():
        name = _module_name(path)
        for imported, _ in _imported_module_names(read_python(path).nodes):
            package = imported.split(".")[0]
            if package in MODEL_CLIENT_PACKAGES:
                dependencies[name].add(f"<{package}>")
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
        for node in read_python(path).nodes:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "MaterializedValue"
            ):
                constructors.append(f"{path.name}:{node.lineno}")

    assert constructors == []


# The accepted Project Record is written by these ``SECURITY DEFINER``
# commands and by nothing else, and each is called from exactly one module.
# ADR-0076 requires this test; ADR-0083 makes it defense in depth behind the
# database roles that hold the same boundary (#492, #519).  Adding a writer is
# a deliberate edit here, reviewed as one.
ACCEPTED_AUTHORITY_COMMANDS = {
    "record_human_fact_decision": "fact_decisions.py",
    "include_structured_cell_fact_decision": "fact_decisions.py",
    "record_subject_alias_decision": "subject_resolution.py",
    "adopt_project_baseline": "operating_mode.py",
    "adopt_project_record_baseline": "baseline_adoption.py",
    "register_baseline_format": "baseline_adoption.py",
    # Storing what a registered mapping revision declares is part of the same
    # attributable registration (#610); it writes no accepted value and is
    # owned by the same role, so it holds the same seam.
    "attach_baseline_format_manifest": "baseline_adoption.py",
    # Configuring what a project externally issues is an attributable human
    # act owned by the same role (#640, ADR-0091); it writes no accepted value
    # and holds the same seam for the same reason as the registration above.
    "register_project_issue_profile": "issue_profile.py",
    "open_delta_resolution_revision": "delta_resolution.py",
    "resolve_proposed_delta_decision": "delta_resolution.py",
    # ADR-0084 keeps the deferral receipt with the delta lifecycle (#518);
    # `delta_resolution` decides whether the act is lawful and delegates.
    "defer_proposed_delta": "proposed_deltas.py",
    # One guided Review Packet act, its Follow-up Plan decisions, and its
    # compensating Undo (#526, ADR-0085). The packet reuses #519's validation
    # and decision construction, so it adds no writer of a semantic decision;
    # these three write the act's own receipt, plan, and compensation.
    "record_delta_follow_up_plan": "review_packets.py",
    "record_review_packet_receipt": "review_packets.py",
    "reverse_review_packet": "review_packets.py",
}

# The accepted-authority tables no application module may construct a row of.
# ``ProjectRecordRevision`` and ``FactDecision`` are read everywhere and
# written only inside the commands above.
ACCEPTED_AUTHORITY_MODELS = frozenset(
    {
        "ProjectRecordRevision",
        "FactDecision",
        "DeltaDisposition",
        "DeltaRecordDecision",
        "DeltaDecisionSupport",
        "DeltaDeferral",
        "DeltaFollowUpPlan",
        "DeltaFollowUpPlanEvidence",
        "DeltaReviewPacketReceipt",
        "DeltaReviewPacketChild",
        "DeltaReviewPacketSupport",
        "DeltaReviewPacketReversal",
        "BaselineAdoption",
        "BaselineSource",
        "BaselineSourceRow",
        "BaselineFormat",
        "BaselineFormatManifest",
    }
)


def test_only_the_declared_seam_calls_an_accepted_authority_command():
    callers: dict[str, set[str]] = defaultdict(set)
    for path in _module_paths():
        for node in read_python(path).nodes:
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "func"
                and node.attr in ACCEPTED_AUTHORITY_COMMANDS
            ):
                callers[node.attr].add(path.name)

    assert {name: sorted(modules) for name, modules in sorted(callers.items())} == {
        name: [module]
        for name, module in sorted(ACCEPTED_AUTHORITY_COMMANDS.items())
    }


def test_no_application_module_constructs_an_accepted_authority_row():
    """The ORM cannot route around the commands: nothing constructs the row."""

    constructors = []
    for path in _module_paths():
        if path.name == "models.py":
            continue
        for node in read_python(path).nodes:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ACCEPTED_AUTHORITY_MODELS
            ):
                constructors.append(f"{path.name}:{node.lineno} {node.func.id}")

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
    for path in python_files(REPO_ROOT / "src" / "corridor"):
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


# ADR-0081 stage 4 exit criterion: "no reader imports a legacy table module;
# the architecture test enforces it." Nothing enforced it, so the census below
# is the enforcement, held as a ratchet rather than as a wish: every module that
# consumes a legacy ORM class today is named, the guard asserts the set exactly,
# and stage 4 finishes when the list is empty.
#
# Which classes count as legacy was checked against ADR-0081 rather than assumed
# from a name. Six are named outright: the stage 1 exit criterion and the freeze
# both list `dependencies`, `dependency_events`, `work_decisions`,
# `operative_support` and the dispute tables. Four more are not named by any one
# sentence of the ADR but retire with those tables — each is keyed to a legacy
# row and `corridor.legacy_history_inventory.HISTORY_CLASSES`, the stage 2
# retention inventory, records each as a `compatibility` class rather than
# native lineage.
#
# `ExtractedProposal` (`extracted_proposals`) was considered and deliberately
# left out. ADR-0081 names it nowhere as retiring, the same stage 2 inventory
# records it as `native_lineage`, and the target architecture keeps it —
# extractors only ever produce Extracted Proposals. Listing it would forbid
# `source_append`, `facts` and `fact_decisions` from touching the spine's own
# proposal record, which is the opposite of stage 4.
#
# A legacy module reading its own class is on the list like any other consumer:
# the criterion is that no module imports one, and stage 6 deletes the modules
# themselves.
LEGACY_TABLE_CONSUMERS: dict[str, tuple[str, ...]] = {
    # Named by ADR-0081 stage 1's exit criterion and by its freeze.
    # dependencies
    "Dependency": (
        "adjudicate", "baseline_adoption", "briefing", "condition_tracking",
        "demo", "dependency_admission", "dependency_events", "dispute_timeline",
        "disputes", "document_notifications", "documentation_checklist",
        "email_intake", "event_admission", "event_admission_acceptance",
        "evidence_investigator", "evidence_investigator_evaluation", "exceptions",
        "external_statements", "facts", "identity", "ledger",
        "legacy_ledger_archive", "m8_acceptance", "m8_acceptance_controlled",
        "measurement_cases", "merge", "milestones", "notifications",
        "operative_support", "organization_identity", "product_proving_execution",
        "product_proving_frontend_capture", "project_contacts", "project_reading",
        "prose_interpretation", "report", "report_diff_reference",
        "revision_change_explanation", "schedule_linking",
        "sh99_admission_acceptance", "statement_coordination",
        "statement_matcher", "statement_matching", "statement_scope_matching",
        "statement_suggestions", "subject_resolution", "supersession_review",
        "support_transfer", "support_transfer_lineage", "verbal", "web.app",
        "web.queue", "work_decisions", "work_list",
    ),
    # dependency_events
    "ExternalPartyStatement": (
        "dependency_events", "document_notifications", "external_statements",
        "measurement_cases", "product_proving_execution",
        "product_proving_frontend_capture", "review_packet_reading",
        "statement_coordination", "statement_lifecycle", "work_list",
    ),
    # work_decisions
    "WorkDecision": (
        "dependency_events", "event_admission_acceptance", "external_statements",
        "notifications", "product_proving_execution",
        "product_proving_frontend_capture", "sh99_admission_acceptance",
        "statement_lifecycle", "work_decisions", "work_list",
    ),
    # operative_support
    "OperativeSupport": (
        "adjudicate", "demo", "legacy_ledger_archive", "m8_acceptance",
        "m8_acceptance_controlled", "operative_support",
        "product_proving_execution", "support_history",
    ),
    # dispute_settlements
    "DisputeSettlement": (
        "audit", "disputes", "measurement_cases", "product_proving_execution",
    ),
    # dispute_history_resolutions
    "DisputeHistoryResolution": (
        "disputes",
    ),
    # Retiring with the tables above: keyed to a legacy row, and recorded as
    # a `compatibility` class by the stage 2 retention inventory.
    # candidates
    "Candidate": (
        "adjudicate", "candidate_statement_facts", "candidates", "cohort", "demo",
        "dependency_admission", "disputes", "eval", "event_admission",
        "event_admission_acceptance", "evidence_investigator",
        "evidence_investigator_capture", "evidence_investigator_runtime",
        "evidence_investigator_shadow", "external_statements",
        "extract_agreement", "extract_batch", "extract_minutes",
        "extract_minutes_v4", "extract_minutes_v5", "extract_project",
        "extract_sheet", "extraction_runs", "fact_decisions", "facts", "gold",
        "identity", "lane", "legacy_ledger_archive", "m8_acceptance",
        "m8_acceptance_controlled", "measurement_cases", "native_matrix",
        "native_matrix_measurement", "organization_identity", "pipeline",
        "product_proving_execution", "product_proving_extraction",
        "product_proving_frontend_capture", "prose_interpretation", "report",
        "revision_comparison", "sh99_admission_acceptance",
        "sh99_coordinator_rehearsal", "statement_coordination",
        "statement_scope_matching", "statement_spine", "statement_suggestions",
        "supersession", "supersession_review", "support_transfer",
        "support_transfer_lineage", "thread_reading", "web.app", "web.queue",
        "web.statement_forms", "work_list",
    ),
    # commitment_lineages
    "CommitmentLineage": (
        "dependency_events", "document_notifications",
        "event_admission_acceptance", "external_statements", "notifications",
        "product_proving_execution", "sh99_admission_acceptance",
        "sh99_coordinator_rehearsal", "statement_coordination", "verbal",
        "web.app", "work_decisions", "work_list",
    ),
    # dependency_dismissals
    "DependencyDismissal": (
        "adjudicate", "audit", "changes", "product_proving_execution",
    ),
    # retired_dependency_statuses
    "RetiredDependencyStatus": (
        "product_proving_execution",
    ),
}


def _legacy_table_consumers() -> dict[str, tuple[str, ...]]:
    """Every source module that names one of the legacy ORM classes.

    Two forms count as consuming: importing the class from `corridor.models`,
    and reaching it as `models.X` off an imported module. `models.py` declares
    them and is not a consumer of them.
    """

    consumers: dict[str, list[str]] = {name: [] for name in LEGACY_TABLE_CONSUMERS}
    for path in _module_paths():
        if path.name == "models.py":
            continue
        found: set[str] = set()
        for node in read_python(path).nodes:
            if isinstance(node, ast.ImportFrom) and node.module == "corridor.models":
                found.update(
                    imported.name
                    for imported in node.names
                    if imported.name in consumers
                )
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "models"
                and node.attr in consumers
            ):
                found.add(node.attr)
        for name in found:
            consumers[name].append(_module_name(path))
    return {name: tuple(sorted(modules)) for name, modules in consumers.items()}


def test_the_legacy_table_consumer_list_may_fall_and_may_never_rise():
    """ADR-0081 stage 4's exit criterion, made executable (#548 ratchet shape).

    The criterion is "no reader imports a legacy table module"; the list above
    is how far that is from true, and this test holds it exact in both
    directions. A module that starts consuming a legacy class fails here, so a
    new feature cannot be built on a frozen table; a module that stops
    consuming one has to be deleted from the list, so a repaired reader cannot
    pay for a new consumer somewhere else. Stage 4 exits when every tuple is
    empty.
    """

    listed = {name: tuple(sorted(modules)) for name, modules in LEGACY_TABLE_CONSUMERS.items()}
    assert listed == LEGACY_TABLE_CONSUMERS, "each class lists its consumers sorted and once"

    from corridor import models

    unknown = sorted(name for name in listed if not hasattr(models, name))
    assert unknown == [], "the list names classes corridor.models no longer defines"

    found = _legacy_table_consumers()
    problems: dict[str, str] = {}
    for name in sorted(listed):
        added = sorted(set(found[name]) - set(listed[name]))
        gone = sorted(set(listed[name]) - set(found[name]))
        if added:
            problems[name] = (
                f"new consumers of this legacy class: {', '.join(added)}; "
                "write through the spine instead of adding one"
            )
        elif gone:
            problems[name] = (
                f"no longer consumers: {', '.join(gone)}; delete those names "
                "from LEGACY_TABLE_CONSUMERS to hold the gain"
            )

    assert problems == {}


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


def test_only_the_storage_interface_builds_a_path_into_the_content_store():
    """Every content-addressed artifact goes through `corridor.object_storage` (#487).

    Two idioms built a store path by hand before the interface existed:
    reading `settings.corpus_store` directly, and sharding a digest with
    `<sha>[:2] / ...`. Either one in another module is a new direct
    filesystem path built for the store, which is exactly what ADR-0079
    forbids once the backend may be an object store.
    """

    offenders = []
    for path in _module_paths():
        if path.name == "object_storage.py":
            continue
        for node in read_python(path).nodes:
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "corpus_store"
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
            ):
                offenders.append(f"{path.name}:{node.lineno} reads settings.corpus_store")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                for operand in (node.left, node.right):
                    if (
                        isinstance(operand, ast.Subscript)
                        and isinstance(operand.slice, ast.Slice)
                        and operand.slice.lower is None
                        and isinstance(operand.slice.upper, ast.Constant)
                        and operand.slice.upper.value == 2
                    ):
                        offenders.append(
                            f"{path.name}:{node.lineno} shards a digest into a path"
                        )

    assert offenders == []


def test_store_deletion_is_permitted_only_by_retention():
    """`delete_under_policy` is the only removal path and a hold precedes it
    (ADR-0080): the permit it needs is constructed in retention alone, and only
    retention and reconciliation may call it."""

    permit_sites = []
    delete_sites = []
    for path in _module_paths():
        if path.name == "object_storage.py":
            continue
        for node in read_python(path).nodes:
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
            if name == "DeletionPermit" and path.name != "retention.py":
                permit_sites.append(f"{path.name}:{node.lineno}")
            if name == "delete_under_policy" and path.name not in {
                "retention.py",
                "storage_operations.py",
            }:
                delete_sites.append(f"{path.name}:{node.lineno}")

    assert permit_sites == []
    assert delete_sites == []


def test_a_push_binding_is_established_only_by_the_credential_boundary():
    """A pushed delivery's customer and project come from its credential (#511).

    ADR-0059's defect was that content decided the boundary. The replacement
    holds only while the object that says "this delivery belongs to this
    customer's project" cannot be built by whatever is reading the payload, so
    `PushBinding` is constructed in `push_intake` alone, where the credential
    registry is the only input."""

    sites = []
    for path in _module_paths():
        if path.name == "push_intake.py":
            continue
        for node in read_python(path).nodes:
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.attr
                if isinstance(callee, ast.Attribute)
                else getattr(callee, "id", None)
            )
            if name == "PushBinding":
                sites.append(f"{path.name}:{node.lineno}")

    assert sites == []


def test_the_render_worker_has_no_database_or_storage_dependency():
    """The isolated render subprocess reads staged bytes and writes a local
    staging directory; the parent owns persistence (ADR-0079 as amended by
    ADR-0083). Its lockfile and imports must not reach the database or a
    storage backend."""

    worker = REPO_ROOT / "workers" / "render"
    imported = set()
    for module in sorted(worker.glob("*.py")):
        for node in read_python(module).nodes:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    forbidden = {"corridor", "sqlalchemy", "psycopg", "boto3", "botocore"}

    assert imported & forbidden == set()
    lock = (worker / "uv.lock").read_text(encoding="utf-8")
    assert not any(f'name = "{name}"' in lock for name in forbidden)


# PyMuPDF and Tesseract leave the product (ADR-0094, #727). The allowlist below
# is the measured remainder: every module that still depends on either engine,
# with the engine(s) it uses. A later ticket deletes the lines for the modules
# it moved; #741 empties the list. The decision does not remove an engine, and
# neither does this guard; it holds the remainder still while the replacement
# lands.
ENGINE_SCAN_ROOTS = ("src/corridor", "workers/render", "tests", "scripts")
PYMUPDF_PACKAGES = frozenset({"fitz", "pymupdf"})
TESSERACT_PACKAGES = frozenset({"pytesseract"})
# #766 removed the last legacy Matrix consumers. The empty guard remains
# permanent so a future import or subprocess cannot reintroduce an engine.
ENGINE_ALLOWLIST: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _engines_used(nodes: tuple[ast.AST, ...], *, absence_audit: bool = False) -> frozenset[str]:
    """The engines one module depends on.

    PyMuPDF is an import of `pymupdf` or its `fitz` alias. Tesseract is an
    import of `pytesseract`, or a string literal naming the engine: the
    executable handed to `shutil.which` and `subprocess`, the engine identity
    written on routing and provenance rows, the read identity the cell-reading
    harness enumerates, and the engine a test names when it simulates one.
    Docstrings and comments are prose, not dependencies, and are not scanned.
    """

    docstrings: set[ast.Constant] = set()
    for node in nodes:
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(first.value)
    # Activation consumes the #766 absence receipt. Only membership checks
    # against its exact observation fields, and literal absent fixture values,
    # are metadata. Imports and executable arguments remain dependencies.
    audit_literals: set[ast.Constant] = set()
    if absence_audit:
        for node in nodes:
            if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)
                and isinstance(node.left, ast.Constant)
                and node.left.value in {"tesseract", "tesseract-ocr"}
                and isinstance(node.comparators[0], ast.Subscript)
                and isinstance(node.comparators[0].slice, ast.Constant)
                and node.comparators[0].slice.value in {"executables", "system_packages"}):
                audit_literals.add(node.left)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                        and key.value in {"tesseract", "tesseract-ocr"}
                        and isinstance(value, ast.Constant) and value.value is None):
                        audit_literals.add(key)
    engines: set[str] = set()
    for node in nodes:
        packages: set[str] = set()
        if isinstance(node, ast.Import):
            packages = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            packages = {node.module.split(".")[0]}
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and node not in audit_literals
            and "tesseract" in node.value.lower()
        ):
            engines.add("tesseract")
        if packages & PYMUPDF_PACKAGES:
            engines.add("pymupdf")
        if packages & TESSERACT_PACKAGES:
            engines.add("tesseract")
    return frozenset(engines)


def _engine_uses() -> dict[str, frozenset[str]]:
    """Repository-relative path -> engines, for every scanned Python module.

    Hidden directories are skipped because the render worker's own `.venv`
    holds PyMuPDF itself, and this file is skipped because it names both
    engines in order to guard them.
    """

    uses: dict[str, frozenset[str]] = {}
    for root in ENGINE_SCAN_ROOTS:
        for path in python_files(REPO_ROOT / root):
            relative = path.relative_to(REPO_ROOT)
            if path == Path(__file__).resolve():
                continue
            engines = _engines_used(read_python(path).nodes, absence_audit=relative.as_posix() in {
                "src/corridor/activation.py", "tests/test_activation.py"})
            if engines:
                uses[relative.as_posix()] = engines
    return uses


def test_the_engine_scanner_sees_every_form_of_dependency():
    """The allowlist is only as honest as the scanner behind it."""

    cases = {
        "import fitz\n": {"pymupdf"},
        "from pymupdf import Rect\n": {"pymupdf"},
        "def ocr():\n    import pytesseract\n    return pytesseract\n": {"tesseract"},
        "OCR_ENGINE = 'tesseract'\n": {"tesseract"},
        "raise RuntimeError(f'{name}: Tesseract unavailable')\n": {"tesseract"},
        '"""Tesseract and PyMuPDF named in prose."""\n': set(),
        "def read():\n    '''Tesseract in a docstring.'''\n": set(),
        "import pypdf  # neither pymupdf nor tesseract\n": set(),
    }

    assert {
        source: set(_engines_used(tuple(ast.walk(ast.parse(source))))) for source in cases
    } == cases

    audit_cases = {
        "'tesseract' in observed['executables']": set(),
        "{'tesseract-ocr': None}": set(),
        "subprocess.run(['tesseract', 'page.png'])": {"tesseract"},
        "import pytesseract": {"tesseract"},
        "{'tesseract': executable}": {"tesseract"},
    }
    assert {source: set(_engines_used(tuple(ast.walk(ast.parse(source))), absence_audit=True))
            for source in audit_cases} == audit_cases


def test_only_allowlisted_modules_still_use_pymupdf_or_tesseract():
    """ADR-0094: PDF facts come from the paired-rendition reader, scanned pages
    from Textract, and PyMuPDF and Tesseract leave the product. The decision
    removes nothing; #741 proves the removal. Until then this guard is exact in
    both directions: a module outside the allowlist that uses an engine fails,
    and a listed module that no longer uses the engine named for it fails, so
    the list can only shrink, and only honestly.
    """

    paths = [path for path, _ in ENGINE_ALLOWLIST]
    assert paths == sorted(set(paths)), "the allowlist is sorted and names each module once"
    assert all(
        isinstance(engines, tuple) and engines for _, engines in ENGINE_ALLOWLIST
    ), "each entry names its engines as a non-empty tuple"
    listed = {path: frozenset(engines) for path, engines in ENGINE_ALLOWLIST}
    found = _engine_uses()
    problems: dict[str, str] = {}
    for path in sorted(set(listed) | set(found)):
        if path not in listed:
            problems[path] = (
                f"uses {', '.join(sorted(found[path]))} and is not on the allowlist"
            )
        elif path not in found:
            problems[path] = "no longer uses either engine; delete its allowlist line"
        elif listed[path] != found[path]:
            problems[path] = (
                f"allowlisted for {', '.join(sorted(listed[path]))} "
                f"but uses {', '.join(sorted(found[path]))}"
            )

    assert problems == {}


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


# --- Shared coordinator-screen presentation (#559) ---------------------------
#
# The Constraint log used to mark a late date in colour and font weight alone
# and said so in a comment beside the rule. That is invisible in print, in
# greyscale, and to a screen reader, and every new review screen was about to
# copy it. The rule below is mechanical: in a template that shares the
# primitives, a class may paint with grey — structure and de-emphasis — but a
# chromatic colour means state, and state belongs to the primitives, which
# always print their own words.

TEMPLATE_ROOT = SOURCE_ROOT / "web" / "templates"
PRIMITIVES_TEMPLATE = TEMPLATE_ROOT / "_primitives.html"

_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SELECTOR_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")
_CSS_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*[^)]*\)")
_HEX_COLOUR = re.compile(r"#([0-9a-fA-F]{3,8})\b")
_RGB_COLOUR = re.compile(r"rgba?\(([^)]*)\)")
_INLINE_STYLE = re.compile(r'style\s*=\s*"([^"]*)"')

# Properties that can paint. A shorthand such as `border` is included because
# it carries a colour in its value.
_COLOUR_PROPERTIES = (
    "color",
    "background",
    "border",
    "outline",
    "box-shadow",
    "fill",
    "stroke",
)
# Sub-properties whose names begin with a painting property but that never
# carry a colour themselves.
_NON_PAINTING_PROPERTIES = (
    "border-collapse",
    "border-radius",
    "border-spacing",
    "border-style",
    "border-width",
    "background-attachment",
    "background-clip",
    "background-origin",
    "background-position",
    "background-repeat",
    "background-size",
    "outline-offset",
    "outline-style",
    "outline-width",
)
# Values that name no colour at all.
_COLOURLESS_WORDS = frozenset(
    {"none", "inherit", "initial", "unset", "transparent", "currentcolor",
     "white", "black", "solid", "dashed", "dotted", "auto"}
)


def _is_chromatic(value: str, variables: dict[str, str]) -> bool:
    """True when a declaration paints with anything but a grey."""
    seen = set()
    while True:
        match = _CSS_VAR.search(value)
        if match is None or match.group(1) in seen:
            break
        seen.add(match.group(1))
        value = value.replace(match.group(0), variables.get(match.group(1), ""))
    for digits in _HEX_COLOUR.findall(value):
        if len(digits) in (3, 4):
            digits = "".join(digit * 2 for digit in digits)
        channels = tuple(
            int(digits[index : index + 2], 16) for index in range(0, 6, 2)
        )
        if len(set(channels)) > 1:
            return True
    for arguments in _RGB_COLOUR.findall(value):
        channels = [part.strip() for part in re.split(r"[,/\s]+", arguments) if part]
        numbers = [part for part in channels[:3] if re.fullmatch(r"\d+", part)]
        if len(numbers) == 3 and len(set(numbers)) > 1:
            return True
    words = {
        word
        for word in re.findall(r"[A-Za-z][A-Za-z-]*", _HEX_COLOUR.sub("", value))
        if not word.endswith("px") and not word.startswith("--")
    }
    return bool(words - _COLOURLESS_WORDS - {"var"})


def _style_sheets(markup: str) -> str:
    return _CSS_COMMENT.sub(" ", "\n".join(_STYLE_BLOCK.findall(markup)))


def _chromatic_classes(markup: str) -> set[str]:
    css = _style_sheets(markup)
    variables: dict[str, str] = {}
    for _, body in _CSS_RULE.findall(css):
        for declaration in body.split(";"):
            name, _, value = declaration.partition(":")
            if name.strip().startswith("--"):
                variables[name.strip()] = value.strip()
    painted: set[str] = set()
    for selector, body in _CSS_RULE.findall(css):
        classes = set(_SELECTOR_CLASS.findall(selector))
        if not classes:
            continue
        for declaration in body.split(";"):
            name, separator, value = declaration.partition(":")
            name = name.strip().lower()
            if not separator or not name.startswith(_COLOUR_PROPERTIES):
                continue
            if name.startswith(_NON_PAINTING_PROPERTIES):
                continue
            if _is_chromatic(value, variables):
                painted |= classes
    return painted


def _shared_templates() -> tuple[Path, ...]:
    shared = [PRIMITIVES_TEMPLATE]
    shared.extend(
        path
        for path in sorted(TEMPLATE_ROOT.glob("*.html"))
        if path != PRIMITIVES_TEMPLATE
        and PRIMITIVES_TEMPLATE.name in path.read_text(encoding="utf-8")
    )
    return tuple(shared)


def test_shared_templates_exist_and_include_the_first_consumer():
    """The check is worthless if it silently covers nothing."""
    names = {path.name for path in _shared_templates()}

    assert PRIMITIVES_TEMPLATE.name in names
    assert "ledger.html" in names


def test_no_shared_template_conveys_state_by_colour_alone():
    from corridor.web.ui_primitives import STATE_CLASSES

    offenders: dict[str, list[str]] = {}
    for path in _shared_templates():
        markup = path.read_text(encoding="utf-8")
        painted = _chromatic_classes(markup)
        if path == PRIMITIVES_TEMPLATE:
            painted -= STATE_CLASSES
        if painted:
            offenders[path.name] = sorted(painted)
        inline = [
            style
            for style in _INLINE_STYLE.findall(_STYLE_BLOCK.sub("", markup))
            if any(
                part.strip().lower().startswith(_COLOUR_PROPERTIES)
                and not part.strip().lower().startswith(_NON_PAINTING_PROPERTIES)
                for part in style.split(";")
            )
        ]
        if inline:
            offenders.setdefault(path.name, []).extend(sorted(inline))

    assert offenders == {}, (
        "a shared template paints state itself instead of rendering it through "
        "the primitives, which print the state's own words"
    )


def test_every_declared_state_class_is_defined_by_the_primitives():
    """The registry the check trusts stays tied to the stylesheet it names."""
    from corridor.web.ui_primitives import STATE_CLASSES

    defined = set(
        _SELECTOR_CLASS.findall(
            " ".join(
                selector
                for selector, _ in _CSS_RULE.findall(
                    _style_sheets(PRIMITIVES_TEMPLATE.read_text(encoding="utf-8"))
                )
            )
        )
    )

    assert STATE_CLASSES <= defined
# The families ADR-0081 converges on, and the constraint in each that makes a
# duplicate unrepresentable (#457).  A dedup identity that lives in a writer —
# a ``SECURITY DEFINER`` command's body, or the Python calling it — holds only
# while every writer remembers it, so each identity below is named here and
# asserted against the declared schema.  Adding a family, or removing one of
# these, is a deliberate edit reviewed as one.
DEDUPLICATION_IDENTITIES = {
    # Source Facts: the Fact identity digest, over every value, reference and
    # source link the command hashed.
    "facts": "uq_facts_content_sha256",
    # Proposed Deltas: the delta's own content digest, and one atomic source
    # change per source version for the group that carries it.
    "proposed_deltas": "uq_proposed_deltas_content",
    "delta_groups": "uq_delta_groups_source_change",
    # Decisions: the revision an inclusion decision belongs to decides its
    # Fact once; a Resolve Delta decision, a Follow-up Plan, a packet act and
    # its Undo each carry their command's key; a dated deferral is the delta,
    # the instant, and the person who scheduled it (ADR-0084).
    "fact_decisions": "uq_fact_decisions_revision_fact",
    "delta_record_decisions": "uq_delta_record_decisions_key",
    "delta_follow_up_plans": "uq_delta_follow_up_plans_key",
    "delta_review_packet_receipts": "uq_delta_review_packet_receipts_key",
    "delta_review_packet_reversals": "uq_delta_review_packet_reversals_key",
    "delta_deferrals": "uq_delta_deferrals_occurrence",
    # Project Record revisions: the command's idempotency key, per project.
    "project_record_revisions": "uq_project_record_revision_key",
    # Connector deliveries: ADR-0083's envelope identity, structurally, so
    # dedup does not rest on the derived digest having been derived, for both
    # transports and each outcome one delivery had (ADR-0089).
    "source_deliveries": "uq_source_deliveries_observation",
}


def test_every_deduplicated_family_declares_its_identity_in_permanent_state():
    """The identity is a constraint on the table, not a rule in a writer.

    A unique constraint over a nullable column deduplicates nothing — two
    absences are distinct by default — and a *partial* unique index is not a
    constraint on the table at all.  ``facts.content_sha256`` was exactly that
    before #457, so the check is not only that a constraint of the recorded
    name exists but that it can actually refuse a duplicate.
    """

    from sqlalchemy import UniqueConstraint

    from corridor.models import Base

    findings = []
    for table_name, constraint_name in sorted(DEDUPLICATION_IDENTITIES.items()):
        table = Base.metadata.tables[table_name]
        declared = next(
            (
                item
                for item in tuple(table.constraints) + tuple(table.indexes)
                if item.name == constraint_name
            ),
            None,
        )
        if declared is None:
            findings.append(f"{table_name}: {constraint_name} is not declared")
            continue
        unique = isinstance(declared, UniqueConstraint) or getattr(
            declared, "unique", False
        )
        if not unique:
            findings.append(f"{table_name}: {constraint_name} is not unique")
            continue
        if declared.dialect_kwargs.get("postgresql_where") is not None:
            findings.append(
                f"{table_name}: {constraint_name} is partial, so it constrains "
                "a subset rather than the family"
            )
            continue
        nullable = sorted(
            column.name for column in declared.columns if column.nullable
        )
        if nullable and not declared.dialect_kwargs.get(
            "postgresql_nulls_not_distinct"
        ):
            findings.append(
                f"{table_name}: {constraint_name} spans nullable "
                f"{', '.join(nullable)} without nulls-not-distinct, so two "
                "absences would be two rows"
            )

    assert findings == []


def _audit_record_calls() -> tuple[tuple[str, ast.Call], ...]:
    """Every `audit.record(...)` call in the source graph, with its module."""

    calls = []
    for path in _module_paths():
        module = _module_name(path)
        for node in read_python(path).nodes:
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "record"
                and isinstance(function.value, ast.Name)
                and function.value.id == "audit"
            ):
                calls.append((module, node))
    return tuple(calls)


def _named_audit_action(call: ast.Call) -> str | None:
    """The `audit.<CONSTANT>` an `audit.record` call names as its action."""

    for keyword in call.keywords:
        if keyword.arg != "action":
            continue
        value = keyword.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "audit"
        ):
            return value.attr
    return None


def test_a_referenced_audit_action_is_never_written_as_a_field_map():
    """An entry names the decision that produced it, or it copies it (#604).

    `audit.REFERENCED_ACTIONS` is the list of acts whose readable before/after
    is *derived* from the authoritative decision row. A writer that also
    passes `before=` or `after=` for one of them puts a second copy of that
    decision's own state into the trail, where it is free to disagree with it
    and answerable to nothing — the defect this ticket removed. Historical
    entries keep their field maps forever; this rule is about new writes.

    One half of #598's value-copying ratchet. The other half —
    ``test_no_new_relation_copies_quote_field_map_or_snapshot_state`` — refuses
    a new column for a copy to live in; this one refuses a new write into a
    column that already exists.
    """

    from corridor import audit

    referenced = {
        name: value
        for name, value in vars(audit).items()
        if isinstance(value, str) and value in audit.REFERENCED_ACTIONS
    }
    findings = []
    for module, call in _audit_record_calls():
        action = _named_audit_action(call)
        if action is None or action not in referenced:
            continue
        passed = {keyword.arg for keyword in call.keywords}
        copied = sorted(passed & {"before", "after"})
        if copied:
            findings.append(
                f"{module}:{call.lineno}: audit.{action} passes "
                f"{', '.join(copied)}= instead of decided_by="
            )
        elif "decided_by" not in passed:
            findings.append(
                f"{module}:{call.lineno}: audit.{action} names no decision"
            )

    assert findings == []


# The relations that already store a copy of state another row owns. A cited
# quote is owned by the Source Segment (ADR-0068); a before/after field map is
# owned by the decision that produced the change (#604); a published snapshot
# is owned by the accepted Project Record revision it was taken against
# (#602). #598 stops *new* writers from copying those; it makes no claim that
# the rows below have migrated, so each one is recorded here with the ticket
# that owns retiring it rather than quietly deleted to keep a check green.
VALUE_COPYING_CARRIERS = {
    # Audit before/after maps. #604 stopped the referenced actions from
    # writing them; the historical entries keep theirs forever.
    ("audit_log", "before_json"),
    ("audit_log", "after_json"),
    ("automatic_carry_forward_receipts", "before_json"),
    ("automatic_carry_forward_receipts", "after_json"),
    ("reconfirmation_receipts", "before_json"),
    ("reconfirmation_receipts", "after_json"),
    # Cited quotes. #605 gives new evidence a Source Segment reference; the
    # equivalence proof that would let these be rewritten is a sibling ticket.
    ("evidence_links", "quote"),
    ("key_date_draft_row_receipts", "source_quote"),
    ("unreadable_cell_resolutions", "corroboration_quote"),
    # The two report reading payloads left this list in #633, and the reason
    # is not that a carrier migrated. #602 listed them as copies and #603
    # measured them: a revision reproduces the record fields and the Ledger
    # identity, and cannot answer the population a report covered, the
    # documentation requirement and Constraint Alerts of one dated reading, or
    # the statement-projected Promised For. ADR-0092 makes those the Report
    # Reading occurrence's own evidence, so they were never a copy of state
    # another row owns and the ratchet has nothing to count. They are named in
    # REPORT_READING_PAYLOADS below rather than deleted silently, so a reader
    # cannot mistake the removal for progress on the historical rows — which
    # have not changed at all.
}

# Not carriers, and not exempt: these columns are the thing itself (ADR-0092).
# The ratchet skips them because they copy nothing, and it still refuses any
# *other* new snapshot-shaped column.
REPORT_READING_PAYLOADS = {
    ("report_runs", "snapshot_json"),
    ("scheduled_report_publications", "snapshot_json"),
}

# This is an identity checksum constrained to 64 lowercase hex characters,
# not another home for quotation text. The migrated citation references its
# original Source Segment; rollback uses this digest to verify old custody.
QUOTE_IDENTITY_DIGESTS = {
    ("legacy_history_evidence_migrations", "original_quote_sha256"),
}

# Not carriers either, and not exempt: these two columns are the reference the
# rule asks for (#640). They name the registered field-mapping revision that
# *owns* the mapping — a foreign key into `project_baseline_formats`, not a
# copy of any field map — and they match the pattern only because the
# registration kind is spelled `field_mapping`. Named exactly, so the ratchet
# still refuses any other new field-map-shaped column.
MAPPING_REGISTRATION_REFERENCES = {
    ("project_issue_profiles", "field_mapping_format_id"),
    ("project_issue_profiles", "field_mapping_kind"),
    # The same two columns on a prepared release candidate, for the same
    # reason (#529): the candidate names the registered mapping revision its
    # artifacts render through and keeps no copy of the map. The digest stays
    # on `project_baseline_formats`, and the candidate's own digested input
    # declaration binds it by value without a column duplicating it.
    ("release_candidates", "field_mapping_format_id"),
    ("release_candidates", "field_mapping_kind"),
    # And the same two on the authorized package's receipt (#533): the receipt
    # names the registered mapping revision the sealed artifacts rendered
    # through, so "was this issue made through the mapping now in force" is one
    # comparison of two ids and never a stored copy of the map itself.
    ("release_packages", "field_mapping_format_id"),
    ("release_packages", "field_mapping_kind"),
}

_VALUE_COPYING_COLUMN = re.compile(r"quote|snapshot|field_map|^(before|after)_json$")


def test_no_new_relation_copies_quote_field_map_or_snapshot_state():
    """The other half of the same ratchet as the audit rule above (#598).

    ``test_a_referenced_audit_action_is_never_written_as_a_field_map`` refuses
    a new *write* of a copy into a column that already exists. This refuses the
    column: a new relation cannot give quote, field-map or snapshot copying
    somewhere fresh to live, which is the form the defect takes when the
    existing carriers are closed off one at a time.

    The registry is frozen in both directions. An unlisted carrier fails
    because it is new; a listed one that no longer exists fails because the
    entry outlived the copy, so the list can only shrink. Neither says the
    historical rows have been migrated — they have not.

    ``REPORT_READING_PAYLOADS`` is subtracted rather than listed: those two
    columns are a dated occurrence's own published evidence, not a copy of any
    other row's state (ADR-0092), so there is nothing there for this ratchet to
    measure. ``MAPPING_REGISTRATION_REFERENCES`` is subtracted for the opposite
    reason: those two columns *are* the reference this rule asks for, a foreign
    key naming the registration that owns the mapping. Every other
    snapshot-shaped or field-map-shaped column still has to be a listed
    carrier.
    """

    from corridor.models import Base

    present = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if _VALUE_COPYING_COLUMN.search(column.name)
    } - REPORT_READING_PAYLOADS - MAPPING_REGISTRATION_REFERENCES - QUOTE_IDENTITY_DIGESTS
    introduced = sorted(
        f"{table}.{column}"
        for table, column in present - VALUE_COPYING_CARRIERS
    )
    retired = sorted(
        f"{table}.{column}"
        for table, column in VALUE_COPYING_CARRIERS - present
    )

    assert introduced == [], (
        "a new relation copies quote, field-map or snapshot state that another "
        "row already owns; reference the owner instead (#598)"
    )
    assert retired == [], (
        "VALUE_COPYING_CARRIERS names a column that no longer exists; remove "
        "the entry so the ratchet keeps measuring what is left"
    )


# Two relations exist in the migrated database and in no model: the accepted
# record's projection, which is a view, and one retired activation table the
# models never mapped.  The static ratchet below reads the models, so they are
# named here rather than being silently outside it.
UNMAPPED_WEB_READABLE_RELATIONS = frozenset(
    {
        "current_coordination_record",
        "current_project_record",
        "retired_automatic_carry_forward_policy_activations",
    }
)


def test_every_relation_the_web_capability_can_read_is_classified():
    """A new relation fails until somebody decides how the partition answers it.

    This is the ratchet #657 exists for.  Coverage established once decays the
    moment the next table lands: #531 partitioned four relations, #640, #529
    and #533 each remembered to carry their own, and 116 others accumulated
    underneath answering a direct-id lookup with another customer project's
    row.  Nothing failed, because nothing was measuring.

    The measurement is static on purpose — `make check` runs this file with no
    database — so it reads the mapped relations rather than the live catalog.
    The live half, that a relation classified as partitioned really carries the
    policy, is proved against real PostgreSQL in
    `tests/test_project_partition_and_offboarding.py`.
    """

    from corridor import access
    from corridor.models import Base

    relations = set(Base.metadata.tables) | UNMAPPED_WEB_READABLE_RELATIONS
    unclassified = access.unclassified_relations(relations)

    assert unclassified == (), (
        "these relations have no recorded answer to 'how does the project "
        "partition cover this?': " + ", ".join(unclassified) + ". Add the "
        "policy and list it in access.PARTITIONED_RELATIONS, or record in "
        "access.py which of the other classifications applies and why"
    )


def test_the_classification_names_no_relation_that_no_longer_exists():
    """A stale entry is a hole that reads as covered; the ratchet measures both ways."""

    from corridor import access
    from corridor.models import Base

    classified = (
        set(access.PARTITIONED_RELATIONS)
        | set(access.AUTHORIZATION_INPUT_RELATIONS)
        | set(access.PROTECTED_RELATIONS)
        | set(access.CUSTOMER_WIDE_RELATIONS)
        | set(access.NOT_YET_PARTITIONED_RELATIONS)
        | set(access.NON_WEB_RELATIONS)
    )
    relations = set(Base.metadata.tables) | UNMAPPED_WEB_READABLE_RELATIONS

    assert sorted(classified - relations) == []


def test_no_relation_carries_two_classifications():
    """Exactly one answer each, or the list means nothing."""

    from corridor import access

    groups = (
        set(access.PARTITIONED_RELATIONS),
        set(access.AUTHORIZATION_INPUT_RELATIONS),
        set(access.PROTECTED_RELATIONS),
        set(access.CUSTOMER_WIDE_RELATIONS),
        set(access.NOT_YET_PARTITIONED_RELATIONS),
        set(access.NON_WEB_RELATIONS),
    )
    overlaps = sorted(
        name
        for index, group in enumerate(groups)
        for other in groups[index + 1 :]
        for name in group & other
    )

    assert overlaps == []


def test_the_uncovered_list_may_fall_and_may_never_rise():
    """The same ratchet shape the migration window uses (#548).

    Naming a hole is how it gets closed, not a place to put the next one. A
    change that partitions a relation lowers the ceiling and holds the gain; a
    change that adds a project-scoped relation cannot pay for it by widening
    the list.
    """

    from corridor import access

    outstanding = len(access.NOT_YET_PARTITIONED_RELATIONS)

    assert outstanding <= access.NOT_YET_PARTITIONED_CEILING, (
        f"{outstanding} relations are project-scoped and unpartitioned, above "
        f"the recorded {access.NOT_YET_PARTITIONED_CEILING}. Partition the new "
        "relation instead of adding it to the list"
    )
    assert outstanding >= access.NOT_YET_PARTITIONED_CEILING, (
        f"the list is down to {outstanding}; lower "
        "NOT_YET_PARTITIONED_CEILING in corridor.access to hold the gain"
    )


def test_every_recorded_reason_says_something_specific():
    """A classification without a reason is a checkbox, not a decision."""

    from corridor import access

    vague = sorted(
        name
        for relations in (
            access.AUTHORIZATION_INPUT_RELATIONS,
            access.PROTECTED_RELATIONS,
            access.CUSTOMER_WIDE_RELATIONS,
            access.NOT_YET_PARTITIONED_RELATIONS,
            access.NON_WEB_RELATIONS,
        )
        for name, reason in relations.items()
        if len(reason.split()) < 8
    )

    assert vague == []


# The live-pilot web capability boundary (#680)
#
# #657 left the classification an inventory: 130 relations were recorded as
# unpartitioned and stayed directly selectable by `corridor_web`. #680 turned
# that record into a deployment boundary — the migration revokes every one of
# them from the human web role — and a boundary with two halves can drift. The
# tests below are the seam that stops it: the application may enable a route
# only if every relation that route reaches survives the revoke, and the set
# the migration takes away is derived from the classification rather than
# listed a second time.


def test_every_enabled_pilot_route_is_a_route_the_application_serves():
    """A boundary that names a route the router does not have protects nothing.

    A renamed or removed route would otherwise leave a permanently-allowed
    entry behind, and the next route to take that path would inherit it.
    """

    from starlette.routing import Route

    from corridor import web_boundary
    from corridor.web.app import app

    served = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in (route.methods or ())
    }
    missing = sorted(key for key in web_boundary.PILOT_ROUTES if key not in served)

    assert missing == [], (
        "these routes are enabled for the live pilot and the application does "
        "not serve them; remove them from corridor.web_boundary or restore "
        "the route"
    )


def test_no_enabled_pilot_route_reads_a_relation_the_boundary_revokes():
    """The two halves of the boundary, compared to each other.

    This is the drift guard. The migration revokes every relation the
    classification still calls unpartitioned; if an enabled route is recorded
    as reading one of them, the deployment would serve a page whose data
    PostgreSQL refuses to hand it. Either partition the relation and record it,
    or take the route out of the pilot — those are the two answers, and
    "leave it readable" is not one of them.
    """

    from corridor import web_boundary

    assert web_boundary.unprotected_route_relations() == ()


def test_the_denied_set_is_exactly_the_unpartitioned_classification():
    """One list of holes, not two that can disagree.

    The boundary is "the web capability holds nothing the classification calls
    unpartitioned". Deriving the denied set rather than repeating it means a
    relation that gains a policy leaves the denied set in the same edit that
    lowers the ceiling.
    """

    from corridor import access, web_boundary

    assert web_boundary.DENIED_RELATIONS == frozenset(
        access.NOT_YET_PARTITIONED_RELATIONS
    )
    assert web_boundary.DENIED_RELATIONS & web_boundary.PROTECTED_RELATIONS == frozenset()


def test_the_migration_revokes_exactly_the_relations_the_boundary_denies():
    """The block that ships is the block the boundary describes.

    `corridor.web_boundary` is what the application and the architecture tests
    read; the migration carries its own frozen copy, because a revision may not
    change meaning when a module above it is edited. That is the right shape
    and it is also how the two fall out of step, so they are compared here.
    """

    import importlib.util

    from corridor import web_boundary

    path = (
        Path(__file__).resolve().parents[1]
        / "src/corridor/migrations/baseline_versions"
        / "b2d5f8a1c4e7_source_append_commands.py"
    )
    spec = importlib.util.spec_from_file_location("_b2d5f8a1c4e7_680", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert frozenset(module.WEB_DENIED_RELATIONS) == web_boundary.DENIED_RELATIONS
    assert len(module.WEB_DENIED_RELATIONS) == len(set(module.WEB_DENIED_RELATIONS))
    assert set(module.WEB_PARTITIONED_TABLES) <= set(
        web_boundary.PROTECTED_RELATIONS
    )


def test_the_migration_and_the_boundary_agree_the_public_allowlist_is_empty():
    """#693's rule is one rule, not two copies that can drift apart.

    The migration carries its own frozen allowlist for the reason #680's
    block does: a revision may not change meaning when a module above it is
    edited. That is the right shape and it is also how the two fall out of
    step, so they are compared here — and the emptiness is asserted on both,
    because an entry means every role in the customer database, present and
    future, is meant to hold that privilege.
    """

    import importlib.util

    from corridor import web_boundary

    path = (
        Path(__file__).resolve().parents[1]
        / "src/corridor/migrations/baseline_versions"
        / "b2d5f8a1c4e7_source_append_commands.py"
    )
    spec = importlib.util.spec_from_file_location("_b2d5f8a1c4e7_693", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert (
        module.PUBLIC_RELATION_PRIVILEGE_ALLOWLIST
        == web_boundary.PUBLIC_RELATION_PRIVILEGES
    )
    assert web_boundary.PUBLIC_RELATION_PRIVILEGES == {}, (
        "an intentional PUBLIC grant needs a written reason and a decision "
        "that says so; #693 recorded that neither relation it found was one"
    )
    # The three the sweep is recorded to remove are what the downgrade hands
    # back. A pair in one and not the other is a privilege the downgrade
    # either loses or invents.
    assert set(module.PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION) == {
        ("fact_decisions", "SELECT"),
        ("project_record_revisions", "SELECT"),
        ("subject_resolution_decisions", "SELECT"),
    }
    for relation, privilege in module.PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION:
        assert (
            f"grant {privilege.lower()} on public.{relation} to public;"
            in module.PUBLIC_PRIVILEGE_RESTORE
        )


def test_the_empty_public_allowlist_still_builds_a_valid_sql_array():
    """`array[]` is a syntax error, so an empty list must render typed.

    An allowlist rendered the obvious way makes the migration fail to *build*
    when it is empty — which looks like a failing guard and is a broken
    statement. The empty case is the shipped case, so it is asserted rather
    than assumed.
    """

    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "src/corridor/migrations/baseline_versions"
        / "b2d5f8a1c4e7_source_append_commands.py"
    )
    spec = importlib.util.spec_from_file_location("_b2d5f8a1c4e7_693_sql", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module._text_array_sql(()) == "array[]::text[]"
    assert module._text_array_sql(("a", "b")) == "array['a', 'b']"
    assert "array[]::text[]" in module.PUBLIC_PRIVILEGE_REVOKE
