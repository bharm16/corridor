"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from corridor.migrations import policy
from corridor.prompt_library import installed_prompt_path
from makefile_support import entry_point, parser_description, targets as make_targets
from ratchet_support import Relocation, assert_ratchet, assert_reviewed_relocations
from source_scan_support import (  # noqa: F401
    callers_of,
    imported_names,
    mentions_of,
    python_files,
    read_python,
    source_scan_cache,
)


REPO_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "corridor"
TEST_ROOT = REPO_ROOT / "tests"


def _module_paths(root: Path | None = None) -> tuple[Path, ...]:
    """Every module of one scanned tree. The source tree unless asked otherwise.

    The test tree is the larger of the two and was never scanned by anything
    here, so the rules that are about module shape rather than about the
    Project Record read it too (#548's lesson, applied to the guards' own
    tree).
    """
    return tuple(
        path
        for path in python_files(root if root is not None else SOURCE_ROOT)
        if path.name != "__init__.py" and "migrations" not in path.parts
    )


MODELS_PACKAGE = SOURCE_ROOT / "models"


def _models_submodules() -> tuple[Path, ...]:
    """Every family module in the schema package, `__init__.py` excluded."""
    return tuple(
        sorted(path for path in MODELS_PACKAGE.glob("*.py") if path.name != "__init__.py")
    )


def _declares_the_schema(path: Path) -> bool:
    """True for the schema declaration itself, which no rule below reads as a reader.

    Card 21 split one 11.6k-line `models.py` into one module per bounded
    context. Rules that used to exempt `models.py` by name now exempt the
    package: declaring a relation is not consuming one, whichever family module
    the declaration landed in.
    """

    return MODELS_PACKAGE in path.parents


def _tree(path: Path) -> ast.Module:
    return read_python(path).tree


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts)


def _corridor_import_edges() -> dict[tuple[str, str], tuple[int, ...]]:
    """Every import edge between two source modules, with the lines that make it."""
    paths = {_module_name(path): path for path in _module_paths()}
    edges: dict[tuple[str, str], set[int]] = {}
    for name, path in paths.items():
        for imported, lineno in imported_names(path):
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


# --- Public symbols nothing reaches (#548 ratchet shape) ---------------------
#
# The fifty-one rules in this file police the import graph, the legacy-table
# census, the engine scan, template colour, audit actions, value-copying and
# web-readable relations and ADR frontmatter. None of them could see a public
# function or class in `src/corridor` that the repository never writes again.
# There were twelve.
#
# A registered web route is reached by its decorator rather than by its name,
# and a schema family's relations are re-exported by name in
# `corridor.models.__init__`, so neither needs an entry below; the rule sees
# both without an exemption. Everything else with no second mention is a
# public interface awaiting a caller, and says so here with a reason and a
# ticket, or is deleted.
#
# Three symbols were deleted rather than listed when this rule was written:
# `proposed_deltas.StaleAcceptedRevisionRefused` (an exception never raised),
# `telemetry.current_correlation` (`dict(_correlation.get())`, which the two
# real readers already inline) and `release_authorization.receipt_identity`
# (one undocumented `sha256` line).
REGISTERED_BY_DECORATOR = frozenset(
    {"app.get", "app.post", "app.exception_handler"}
)
AWAITING_CALLER = {
    "build_native_report": (
        "report.py:512 -- the retained accepted-record entry point of the "
        "internal Report, kept as one delegation to `build_report` while "
        "ADR-0086/ADR-0091 move the customer's Coordination Report to "
        "`issue_rendering.render_weekly_report` (ADR-0081 stage 3)"
    ),
    "due_action_inbox": (
        "notifications.py:1557 -- the recipient's own due-action inbox read, "
        "scoped to one member and one project; the screen that renders it is "
        "unbuilt, and the operations delivery view beside it is the half that "
        "has a caller"
    ),
    "eligible_scan_pages": (
        "unreadable_cells.py:246 -- the pages an unreadable-cell profile "
        "would read; #739's scanned route selects its own pages, so this "
        "profile-scoped reader waits for the profile to be wired"
    ),
    "follow_up_plans_for_delta": (
        "review_packets.py:995 -- every Follow-up Plan on one Proposed "
        "Delta, in the order they were made; ADR-0081's released "
        "class-specific projection policies are what will read it"
    ),
    "pending_record_inclusion_project_ids": (
        "record_inclusion.py:70 -- the recovery drain's list of projects "
        "with unreconciled Record Inclusion work; the drain itself is unbuilt"
    ),
    "pending_revision_reconciliation_project_ids": (
        "revision_reconciliation_request.py:52 -- the same recovery drain, "
        "for unreconciled revision work; retire both entries together"
    ),
    "publish_frontend_pass_bundle": (
        "product_proving_frontend_capture.py:616 -- seals one observed "
        "frontend pass before its database is restored; the capture command "
        "that would call it is not wired into Product Proving yet"
    ),
    "replace_local_database_with_verified_clone": (
        "product_proving_database.py:1258 -- the staged/validated/finalized "
        "swap composed into one act, including the post-swap fingerprint and "
        "migration-head checks the three exported steps do not carry; callers "
        "run the steps themselves today"
    ),
}


def _route_decorated(node: ast.AST) -> bool:
    """True for a handler the web application registers by decorating it."""

    return any(
        ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator)
        in REGISTERED_BY_DECORATOR
        for decorator in getattr(node, "decorator_list", ())
    )


def _public_definitions() -> dict[str, tuple[Path, int]]:
    """Every public module-level function and class the source tree declares."""

    definitions: dict[str, tuple[Path, int]] = {}
    for path in _module_paths():
        for node in _tree(path).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_") or _route_decorated(node):
                continue
            definitions[node.name] = (path, node.lineno)
    return definitions


def test_every_public_symbol_is_written_somewhere_other_than_its_definition():
    """A public interface no line of this repository ever names again is dead.

    The weaker of the two questions `source_scan_support` answers: a name in a
    docstring, in an `__all__`, or inside the source of a probe a test runs is
    not a caller, but it is the repository writing the name. Failing here
    means nothing anywhere writes it at all, so the remedy is to delete the
    symbol -- or, when it is a seam whose caller is genuinely still to come,
    to say so above with a reason and a ticket, the way `PRODUCTION_IMPORTERS`
    and `CYCLE_EDGE_ALLOWLIST` do. The list may fall and may never rise.
    """

    definitions = _public_definitions()
    written = mentions_of(
        set(definitions),
        (REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "scripts", REPO_ROOT / "workers"),
    )
    guard = Path(__file__).resolve()
    unwritten = {}
    for name, (path, lineno) in definitions.items():
        elsewhere = {
            site: tuple(line for line in lines if (site, line) != (path, lineno))
            for site, lines in written[name].items()
            if site != guard  # the table below is a record, not a reference
        }
        if not any(elsewhere.values()):
            unwritten[name] = f"{path.relative_to(REPO_ROOT)}:{lineno}"

    assert sorted(unwritten) == sorted(AWAITING_CALLER), (
        "a public symbol nothing else in the repository names: delete it, or "
        "record it in AWAITING_CALLER with a reason and a ticket:\n"
        + "\n".join(f"{name} at {site}" for name, site in sorted(unwritten.items()))
    )
    assert all(AWAITING_CALLER.values()), "an entry without a reason is not a decision"


def test_source_modules_do_not_import_another_module_private_implementation():
    """Every import form, so `from corridor import _x` cannot slip past.

    A module may still reach its own package's private implementation: the
    schema package's family modules share `corridor.models.base`, whose `_enum`
    builds every named PostgreSQL enumeration in the schema. That is the
    boundary a package draws, and the rule is about crossing someone else's.
    """
    private_imports: list[str] = []
    for path in _module_paths():
        package = _module_name(path).rpartition(".")[0]
        own = f"corridor.{package}." if package else None
        for imported, lineno in imported_names(path):
            if not imported.startswith("corridor."):
                continue
            if not imported.split(".")[-1].startswith("_"):
                continue
            if own is not None and imported.startswith(own):
                continue
            private_imports.append(f"{path.name}:{lineno} imports {imported}")

    assert sorted(set(private_imports)) == []


# The same rule in the test tree, where it is not clean yet. Five modules
# share one collector's fixtures by reaching into each other -- reading
# `test_native_citation_coverage.py` means opening two other test modules --
# and the repository already has the seam that ends it: fifteen `*_support.py`
# modules in `tests/`, which a test may reach freely. These pairs are what is
# outstanding, each `(importer, imported private name)` rather than a line
# number so that an unrelated edit above one does not move it. It may fall and
# may never rise: move the fixture to a support module and delete the line.
TEST_PRIVATE_IMPORTS = frozenset({
    ("test_matrix_retirement_e2e.py", "test_native_provider_boundary._body"),
    ("test_matrix_retirement_e2e.py", "test_native_provider_boundary._experiment"),
    ("test_matrix_retirement_e2e.py", "test_native_provider_boundary._request"),
    ("test_native_citation_coverage.py", "test_native_accepted_readers._follow_up_plan"),
    ("test_native_follow_up_reading.py", "test_issue_rendering._baseline"),
    ("test_native_follow_up_reading.py", "test_issue_rendering._delta"),
    ("test_native_follow_up_reading.py", "test_issue_rendering._plan"),
    ("test_native_pipeline.py", "test_native_matrix._document"),
    ("test_native_reader_coverage.py", "test_native_accepted_readers._adopt_native_workbook"),
    ("test_native_release_coverage.py", "test_native_accepted_readers._adopt_native_workbook"),
    ("test_native_work_list_coverage.py", "test_native_accepted_readers._follow_up_plan"),
    ("test_pilot_measurement_receipts.py", "test_packet_review_screen._revision"),
    ("test_pipeline.py", "test_native_matrix._document"),
    ("test_pipeline.py", "test_native_pipeline._client"),
    ("test_pipeline.py", "test_native_pipeline._plan"),
    ("test_pipeline.py", "test_native_pipeline._scope"),
    ("test_pipeline.py", "test_native_provider_boundary._experiment"),
    ("test_pipeline.py", "test_native_provider_boundary._request"),
    ("test_pipeline_qualification_cli.py", "test_native_pipeline._document"),
    ("test_pipeline_qualification_cli.py", "test_native_pipeline._gate_fixture"),
    ("test_pipeline_qualification_cli.py", "test_native_pipeline._qualify"),
    ("test_pipeline_qualification_cli.py", "test_native_pipeline._scope"),
    ("test_pipeline_render_identity.py", "test_native_pipeline_geometry._author_page"),
    ("test_pipeline_render_identity.py", "test_native_pipeline_geometry._colour_deskew_profile"),
    ("test_pipeline_selection.py", "test_native_matrix._document"),
    ("test_pipeline_selection.py", "test_native_pipeline._client"),
    ("test_pipeline_selection.py", "test_native_pipeline._gate_fixture"),
    ("test_pipeline_selection.py", "test_native_pipeline._plan"),
    ("test_pipeline_selection.py", "test_native_pipeline._qualify"),
    ("test_pipeline_selection.py", "test_native_pipeline._scope"),
    ("test_pipeline_selection_guards.py", "test_native_pipeline._acceptance_fixture"),
    ("test_pipeline_selection_guards.py", "test_native_pipeline._gate_fixture"),
    ("test_pipeline_selection_guards.py", "test_native_pipeline._qualify"),
    ("test_project_portfolio.py", "test_project_workflow._cross_source"),
    ("test_project_portfolio.py", "test_project_workflow._plan_every_child"),
    ("test_project_portfolio.py", "test_project_workflow._project"),
    ("test_project_portfolio.py", "test_release_authorization._replace_output_template"),
    ("test_release_preparation.py", "test_release_candidate._preparation"),
    ("test_release_preparation.py", "test_release_candidate._prepare"),
})


def test_test_modules_do_not_import_another_test_modules_private_implementation():
    """A test module may reach its own fixtures, not another one's internals.

    The same rule the source tree already passes, on the tree that is larger
    than it. A shared fixture belongs in the `*_support.py` family, which is
    importable by anyone; a private name in a sibling `test_` module is a
    dependency between two test modules that neither declares.
    """
    modules = {path.stem for path in _module_paths(TEST_ROOT)}
    private_imports = {
        (path.name, imported)
        for path in _module_paths(TEST_ROOT)
        for imported, _ in imported_names(path)
        for owner, leaf in [imported.rpartition(".")[::2]]
        if leaf.startswith("_")
        and owner.startswith("test_")
        and owner in modules
        and owner != path.stem
    }

    assert_ratchet(
        "tests/test_architecture.py:TEST_PRIVATE_IMPORTS",
        measured=private_imports,
        recorded=set(TEST_PRIVATE_IMPORTS),
    )


def test_the_schema_package_imports_and_reexports_every_family_it_declares():
    """`corridor.models` is a package, and importing it still declares everything.

    Card 21 split the schema by bounded context. Two things had to stay true and
    neither is automatic. `Base.metadata` is only complete once every family has
    been imported -- Alembic autogenerate reads that metadata, so a family this
    package forgot would silently drop out of the schema and out of the
    fingerprint. And roughly two hundred modules import their relations from
    `corridor.models`, so every public name each family declares has to arrive
    there. This test holds both: the package star-imports exactly the family
    modules that exist, each family declares an `__all__` equal to its own
    public definitions, and the package's `__all__` is their union.
    """
    import importlib

    init = read_python(MODELS_PACKAGE / "__init__.py")
    star_imported = {
        node.module
        for node in init.nodes
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
    }
    families = _models_submodules()
    assert star_imported == {f"corridor.models.{path.stem}" for path in families}

    package = importlib.import_module("corridor.models")
    problems: list[str] = []
    union: set[str] = set()
    for path in families:
        module = importlib.import_module(f"corridor.models.{path.stem}")
        defined = {
            node.name
            for node in _tree(path).body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        } | {
            target.id
            for node in _tree(path).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        public = {name for name in defined if not name.startswith("_")}
        declared = set(getattr(module, "__all__", ()))
        if declared != public:
            problems.append(
                f"{path.name}: __all__ is {sorted(declared)}, "
                f"its public definitions are {sorted(public)}"
            )
        for name in public:
            if getattr(package, name, None) is not getattr(module, name):
                problems.append(f"corridor.models does not re-export {name}")
        union |= public

    assert problems == []
    assert set(package.__all__) == union
    assert len(package.__all__) == len(set(package.__all__))


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
    ("extract_minutes_v5", "extract_batch", _EXTRACTION_LINEAGE),
    ("extraction_runs", "extractor_lineage", _EXTRACTION_LINEAGE),
    ("extractor_lineage", "extract_agreement", _EXTRACTION_LINEAGE),
    ("extractor_lineage", "extract_minutes_v5", _EXTRACTION_LINEAGE),
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
)


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
    """The ratchet, exact in both directions and against the merge base.

    An import that closes a new cycle fails here rather than hiding inside a
    component that was already tangled, and an edge that stops closing one has
    to leave the list. `assert_ratchet` is what makes "the list can only
    shrink" a rule rather than a claim: editing the allowlist in the same
    commit no longer buys the edge, because the merge base still records the
    list without it.
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

    assert_ratchet(
        "tests/test_architecture.py:CYCLE_EDGE_ALLOWLIST",
        measured=found,
        recorded=declared,
        as_measured=lambda listed: {(source, target) for source, target, _ in listed},
    )


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
        for imported, _ in imported_names(path):
            package = imported.split(".")[0]
            if package in MODEL_CLIENT_PACKAGES:
                dependencies[name].add(f"<{package}>")
    return dependencies


# The names the production extraction path may reach for on the pipeline
# selection seam. `pipeline_qualification` is the operator's module: it records
# gates, acceptances, policies and selections, every one of them an
# attributable maintenance act. The readback is the only thing a Document read
# runs, so it is its own module and the fact path imports nothing else.
LIVE_SELECTION_NAMES = frozenset({"PipelineQualificationRefused", "selected_pipeline_configuration"})
PRODUCTION_SELECTION_READERS = ("pipeline", "native_pipeline")


def test_the_production_path_imports_only_the_selection_predicate():
    """ADR-0095's receipts are untouched by the split.

    "An acceptance is not a passing gate and is never recorded as one" -
    ADR-0095. The readback still refuses on either basis exactly as the
    selection command does; what moved is which module the fact path has to
    import to ask. A recording function reachable from the extraction route is
    the defect this test reports.
    """
    paths = {_module_name(path): path for path in _module_paths()}
    reached: dict[str, set[str]] = {}
    for name in PRODUCTION_SELECTION_READERS:
        for node in ast.walk(_tree(paths[name])):
            if isinstance(node, ast.ImportFrom) and node.module == "corridor.pipeline_qualification":
                reached.setdefault(name, set()).update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module == "corridor.pipeline_selection_readback":
                reached.setdefault(name, set()).update(alias.name for alias in node.names)

    assert reached == {name: set(LIVE_SELECTION_NAMES) for name in PRODUCTION_SELECTION_READERS}
    readback = read_python(paths["pipeline_selection_readback"]).tree
    assert {node.name for node in readback.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))} == {
        "PipelineQualificationRefused", "pipeline_receipt", "selected_pipeline_configuration",
    }
    source = paths["pipeline_selection_readback"].read_text(encoding="utf-8")
    assert "session.add" not in source and "_append(" not in source, (
        "the live readback reads what was decided and records nothing"
    )
    assert '"an acceptance is not a passing gate"' not in source.lower(), (
        "the readback quotes no verdict of its own; it reads the recorded basis"
    )


def test_the_extraction_route_turns_a_refused_selection_into_one_failure():
    """One wording, so a refusal cannot be reported two ways by two handlers."""
    route = next(
        node for node in _tree(SOURCE_ROOT / "pipeline.py").body
        if isinstance(node, ast.FunctionDef) and node.name == "extraction_route"
    )
    raises = [
        node for node in ast.walk(route)
        if isinstance(node, ast.ExceptHandler)
        and "PipelineQualificationRefused" in ast.dump(node.type or ast.Constant(None))
        and any(isinstance(inner, ast.Raise) and inner.exc is not None for inner in ast.walk(node))
    ]

    assert len(raises) == 1, "the refusal becomes an ExtractionFailed in exactly one place"


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
        if _declares_the_schema(path):
            continue
        for node in read_python(path).nodes:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ACCEPTED_AUTHORITY_MODELS
            ):
                constructors.append(f"{path.name}:{node.lineno} {node.func.id}")

    assert constructors == []


# The test tree's own copy of the rule above. `tests/harness_support.py` is the
# one module that writes an accepted-authority row directly, and every entry
# here is a module that still does so for itself. The list may fall and may
# never rise (`assert_ratchet`); each entry is one module writing one relation:
#
#   - the relation's own refusal walk, whose subject *is* the raw statement:
#     `test_baseline_adoption`, `test_database_authority`, `test_delta_resolution`,
#     `test_fact_decisions`, `test_permanent_state_deduplication`,
#     `test_operating_mode`, `test_review_packets`;
#   - `test_migration_baseline`, which seeds pre-migration rows on a disposable
#     database so a migration has something to transform;
#   - `test_project_partition_and_offboarding`, which writes as the schema owner
#     with the record guards disabled, to give a deployment-wide sweep a row in
#     every relation it must reach;
#   - the Adopt Baseline *registration* family -- baseline sources, rows,
#     formats and manifests -- in `packet_review_support` and
#     `test_release_authorization`. These are a further act nobody has lifted
#     yet, not one of the two `harness_support` already owns.
TEST_ACCEPTED_AUTHORITY_WRITES = frozenset({
    ("packet_review_support.py", "project_baseline_format_manifests"),
    ("packet_review_support.py", "project_baseline_formats"),
    ("packet_review_support.py", "project_baseline_source_rows"),
    ("packet_review_support.py", "project_baseline_sources"),
    ("test_baseline_adoption.py", "project_baseline_format_manifests"),
    ("test_baseline_adoption.py", "project_baseline_sources"),
    ("test_database_authority.py", "delta_record_decisions"),
    ("test_database_authority.py", "fact_decisions"),
    ("test_delta_resolution.py", "delta_record_decisions"),
    ("test_fact_decisions.py", "fact_decisions"),
    ("test_fact_decisions.py", "project_record_revisions"),
    ("test_migration_baseline.py", "project_baseline_format_manifests"),
    ("test_migration_baseline.py", "project_baseline_formats"),
    ("test_migration_baseline.py", "project_record_revisions"),
    ("test_operating_mode.py", "project_baseline_adoptions"),
    ("test_permanent_state_deduplication.py", "delta_deferrals"),
    ("test_permanent_state_deduplication.py", "fact_decisions"),
    ("test_permanent_state_deduplication.py", "project_record_revisions"),
    ("test_project_partition_and_offboarding.py", "fact_decisions"),
    ("test_project_partition_and_offboarding.py", "project_record_revisions"),
    ("test_release_authorization.py", "project_baseline_formats"),
    ("test_review_packets.py", "delta_review_packet_receipts"),
})


def _accepted_authority_tables() -> dict[str, str]:
    """Each accepted-authority relation, by the name a module constructs it as.

    Read off the declarations rather than retyped, so renaming a relation
    cannot leave the scan below matching a table that no longer exists.
    """
    import importlib

    models = importlib.import_module("corridor.models")
    return {
        name: getattr(models, name).__tablename__
        for name in sorted(ACCEPTED_AUTHORITY_MODELS)
    }


def _docstrings(source) -> set[ast.Constant]:
    """Every docstring node, which describes a statement rather than running one."""
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        node.body[0].value
        for node in source.nodes
        if isinstance(node, holders)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def test_a_test_module_writes_an_accepted_authority_row_only_through_the_harness():
    """The rule above, on the tree that was writing around it.

    `tests/` held sixteen hand-written inserts into `project_record_revisions`
    and `fact_decisions` across nine modules -- each one a copy of a
    record-decision command's SQL that nothing failed when the command changed,
    and one of them (`tests/packet_review_support.py`) said so in a comment.
    They live in `harness_support` now, behind the acts they were setting up,
    and this holds that gain: a module may write these relations raw only where
    the raw statement is what it is proving.

    The ORM door is scanned beside the SQL one, because a fixture that binds
    the schema owner can reach the relation either way.
    """
    tables = _accepted_authority_tables()
    relations = "|".join(sorted(set(tables.values()), key=len, reverse=True))
    pattern = re.compile(
        r"(?:insert\s+into|update|delete\s+from)\s+(?:only\s+)?\"?(" + relations + r")\b",
        re.IGNORECASE,
    )
    found: set[tuple[str, str]] = set()
    for path in _module_paths(TEST_ROOT):
        if path.name in ("harness_support.py", "test_architecture.py"):
            continue
        source = read_python(path)
        described = _docstrings(source)
        for node in source.nodes:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node not in described
            ):
                found.update(
                    (path.name, table.lower()) for table in pattern.findall(node.value)
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in tables
            ):
                found.add((path.name, tables[node.func.id]))

    assert_ratchet(
        "tests/test_architecture.py:TEST_ACCEPTED_AUTHORITY_WRITES",
        measured=found,
        recorded=TEST_ACCEPTED_AUTHORITY_WRITES,
        as_measured=lambda listed: {tuple(entry) for entry in listed},
    )


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
        "prose_interpretation", "report_diff_reference",
        "revision_change_explanation", "schedule_linking",
        "sh99_admission_acceptance", "statement_coordination",
        "statement_matcher", "statement_matching", "statement_scope_matching",
        "statement_suggestions", "subject_resolution", "supersession_review",
        "support_transfer", "support_transfer_lineage", "verbal", "web.app",
        # The queue's reading, moved out of `web.app`'s 309-line route body:
        # it resolves the one Constraint whose coordination strip is open, in
        # this project and inside the pinned lane. It reads through the legacy
        # readers rather than widening them, and retires with them.
        "web.queue", "web.queue_view",
        # The statement coordination screens' reading, moved out of `web.app`
        # for the same reason (#857): this project's open Constraints, in the
        # order the scope choices are offered. Same reads, a different file.
        "web.statement_view", "work_decisions", "work_list",
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
        "evidence_investigator_human_outcome", "evidence_investigator_runtime",
        "evidence_investigator_shadow", "external_statements",
        "extract_agreement", "extract_batch", "extract_minutes_v5", "extract_project",
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
        "support_transfer_lineage", "web.app", "web.queue",
        # Same move: the queue reading names the Extracted Proposal it opens
        # and the sibling revisions offered as merges. Nothing new is built on
        # the table; the read left `web.app` and did not grow.
        "web.queue_view", "web.statement_forms",
        # Same move: the reading names the Extracted Proposal whose
        # coordination screen it is, and the facts its source supports.
        "web.statement_view", "work_list",
    ),
    # commitment_lineages
    "CommitmentLineage": (
        "dependency_events", "document_notifications",
        "event_admission_acceptance", "external_statements", "notifications",
        "product_proving_execution", "sh99_admission_acceptance",
        "sh99_coordinator_rehearsal", "statement_coordination", "verbal",
        "web.app",
        # Same move: the residual screen's reading resolves the Commitment
        # line the statement's active grouping receipt points at.
        "web.statement_view", "work_decisions", "work_list",
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


# The one way a name may join the census above: an existing reading that moved
# from one module to another. Lifting a screen's reading out of a 7k-line route
# body adds the destination to the list, so the direction check refuses it like
# any other new consumer -- and it is not a new consumer, it is the same reads
# in a different file. Each line below is checked against the merge base by
# `assert_reviewed_relocations`, which authorizes exactly the pairs it can
# prove; nothing here is an entitlement to consume a legacy class, and writing
# a line here buys nothing on its own.
#
# A relocation is not retirement progress. ADR-0081 stage 4 exits when no
# reader imports a legacy table module, and a relocation leaves the census one
# name longer than it found it; the census above goes on saying so. It does not
# cross the freeze in either direction either: no legacy-only capability is
# built by moving a reading, and no reader reaches the spine by changing files.
#
# Each declaration is spent by the merge that uses it. At the next merge base
# the source reading no longer holds the dependency, the evidence check stops
# passing, and the line has to go -- the destination is an ordinary consumer
# from then on.
RELOCATED_LEGACY_READINGS: tuple[Relocation, ...] = (
    # The statement coordination screens' reading left `web.app` for
    # `web/statement_view.py` (#857). Card G-01 had already made it a named
    # reading -- `StatementCoordinationView`, carrying its own template name --
    # and what it could not do was give it a module, because three frozen
    # classes arriving in a new file is exactly what the census cannot tell
    # from three new dependencies.
    #
    # Two declarations for one extraction, because the unit the merge base can
    # prove is an implementation and no single implementation in `web.app`
    # named all three. The reading itself names `Candidate` (the proposal it
    # is the screen for) and `CommitmentLineage` (the line its active grouping
    # receipt points at); `Dependency` is read one call down, in the Constraint
    # ordering the scope choices are offered in. One line claiming all three
    # would be an overclaim, and `assert_reviewed_relocations` refuses it.
    Relocation(
        source="web.app",
        source_reading="_read_statement_coordination",
        destination="web.statement_view",
        destination_reading="read_statement_coordination",
        models=("Candidate", "CommitmentLineage"),
        card="#857",
    ),
    Relocation(
        source="web.app",
        source_reading="_ordered_scope_dependencies",
        destination="web.statement_view",
        destination_reading="_ordered_scope_dependencies",
        models=("Dependency",),
        card="#857",
    ),
)


def _legacy_table_consumers() -> dict[str, tuple[str, ...]]:
    """Every source module that names one of the legacy ORM classes.

    Three forms count as consuming: importing the class from `corridor.models`,
    importing it from `corridor.models.legacy` directly, and reaching it as
    `models.X` off an imported module. The schema package declares them and is
    not a consumer of them.
    """

    consumers: dict[str, list[str]] = {name: [] for name in LEGACY_TABLE_CONSUMERS}
    for path in _module_paths():
        if _declares_the_schema(path):
            continue
        found: set[str] = set()
        for node in read_python(path).nodes:
            if isinstance(node, ast.ImportFrom) and node.module in (
                "corridor.models",
                "corridor.models.legacy",
            ):
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

    The list has risen before -- 160 to 162 in one commit -- because equality
    against a constant the same commit may edit cannot see direction.
    `assert_ratchet` reads the list back out of the merge base and names the
    consumer that joined.

    One name may still join, and only one way: a reviewed relocation, where an
    existing reading moved to its own module and the merge base can be made to
    prove it. `RELOCATED_LEGACY_READINGS` above declares those, and the pairs
    that survive that proof come off both sides below so that the comparison is
    unchanged for every other name. The list itself keeps counting them.
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

    # The census above is already held exact by `problems`, so a relocation
    # never hides its destination from the list. What comes back here is only
    # the joining that the merge base proved is a move, dropped from both sides
    # so the direction check below judges every other name as before.
    relocated = assert_reviewed_relocations(
        RELOCATED_LEGACY_READINGS,
        consumers=found,
        source_root=SOURCE_ROOT.relative_to(REPO_ROOT).as_posix(),
        census="tests/test_architecture.py:LEGACY_TABLE_CONSUMERS",
    )
    pairs = lambda listed: {
        (name, module) for name, modules in listed.items() for module in modules
    }
    assert_ratchet(
        "tests/test_architecture.py:LEGACY_TABLE_CONSUMERS",
        measured={(name, module) for name in listed for module in found[name]}
        - relocated,
        recorded=pairs(listed) - relocated,
        as_measured=pairs,
    )


def test_every_frozen_relation_is_declared_in_the_legacy_family_and_nowhere_else():
    """The freeze has one address: `corridor.models.legacy` (ADR-0081).

    Before card 21 the frozen tables were declared in the middle of an
    11.6k-line module, so "do not build on these" was a comment. Collecting
    them in one family module makes it a location, which is what lets the rule
    below be a scan for an import rather than a scan for a class name. The
    satellites that cannot exist without one of them -- a statement's timing
    rows, its scope memberships, its evidence links -- live there too, and are
    allowed to; nothing else may.
    """

    declarations: dict[str, list[str]] = defaultdict(list)
    for path in _models_submodules():
        for node in _tree(path).body:
            if isinstance(node, ast.ClassDef) and node.name in LEGACY_TABLE_CONSUMERS:
                declarations[node.name].append(path.name)

    assert {name: sorted(where) for name, where in sorted(declarations.items())} == {
        name: ["legacy.py"] for name in sorted(LEGACY_TABLE_CONSUMERS)
    }


def test_no_module_outside_the_schema_package_imports_the_legacy_family():
    """ADR-0081 stage 4's exit criterion, as one import rule.

    The consumer ratchet above measures how far the readers are from the
    criterion, class by class. This is the criterion itself, and it is already
    true: nothing outside the schema package names the module the frozen
    relations live in, so a reader that wants one has to reach it through
    `corridor.models` and be counted by the ratchet. A module that imports
    `corridor.models.legacy` directly would take a legacy dependency the
    ratchet's list never had to admit to.
    """

    importers = []
    for path in _module_paths():
        if _declares_the_schema(path):
            continue
        for imported, lineno in imported_names(path):
            if imported == "corridor.models.legacy":
                importers.append(f"{_module_name(path)}:{lineno}")

    assert sorted(importers) == []


# A committed test scenario may lift the append-only guard for a purpose
# other than removing a project: to construct the corrupt pre-state the act
# under test must reject, to model the disposal path a sweep can only
# happen through, or to seed an append-only row on a disposable migration
# database. Those are classified here, with the reason, so that a new
# module reaching for the setting has to say which it is.
LIFTS_THE_APPEND_ONLY_GUARD_WITHOUT_DELETING_A_PROJECT = {
    "test_automatic_carry_forward": "builds the corrupt pre-insert state the receipt trigger must reject",
    "test_connector_polling_runtime": "models the disposal path a receipt sweep can only happen through",
    "test_migration_baseline": "seeds an append-only row on its own disposable database",
    "test_supersession_review": "simulates pre-sealed legacy history before inverting two acts",
}

_DELETES_A_PROJECT = re.compile(r"delete\(Project\)|delete\s+from\s+projects\b")


def test_a_committed_test_scenario_is_torn_down_through_one_derived_cleanup():
    """Only `committed_scenario_support` removes a committed project (#521).

    Five modules used to hand-write "delete the project I committed" — 353
    lines naming 19, 17, 7, 4 and 3 tables in five different orders, two of
    them naming the same audit entity type as a constant in one file and as
    a string literal in the other, and three of them removing no spine rows
    at all. The invariant was remembered, and remembering it is what failed.

    The derivation is the seam, so this rule is about its call sites: a
    module that deletes a project reaches the derivation rather than listing
    tables, and a module that lifts the append-only guard for some other
    purpose says which purpose here.
    """

    guard = Path(__file__).resolve()
    seam = TEST_ROOT / "committed_scenario_support.py"
    deleting = []
    for path in _module_paths(TEST_ROOT):
        if path.resolve() == guard or path == seam:
            continue
        for number, line in enumerate(read_python(path).text.splitlines(), start=1):
            if _DELETES_A_PROJECT.search(line):
                deleting.append(f"{path.name}:{number}")

    assert sorted(deleting) == [], (
        "a test module deletes a committed project by hand; call "
        "committed_scenario_support.delete_committed_project instead: "
        f"{sorted(deleting)}"
    )

    lifting = {
        path.stem
        for path in mentions_of(["session_replication_role"], (TEST_ROOT,))[
            "session_replication_role"
        ]
        if path != seam and path.resolve() != guard
    }
    unclassified = sorted(
        lifting - set(LIFTS_THE_APPEND_ONLY_GUARD_WITHOUT_DELETING_A_PROJECT)
    )
    assert unclassified == [], (
        "a test module lifts the append-only guard outside the committed-scenario "
        "cleanup; delete the project through "
        "committed_scenario_support.delete_committed_project, or classify the "
        f"other purpose in test_architecture.py: {unclassified}"
    )
    stale = sorted(set(LIFTS_THE_APPEND_ONLY_GUARD_WITHOUT_DELETING_A_PROJECT) - lifting)
    assert stale == [], (
        f"these modules no longer lift the append-only guard: {stale}"
    )


def test_every_project_scoped_table_is_covered_by_the_committed_scenario_cleanup():
    """The cleanup's table set is derived from the schema, never listed (#521).

    `place_project_tables` raises on a table it cannot place, so importing
    the seam already refuses a new table that is neither reachable from a
    project nor classified as global. This states the same criterion where
    `make check` reads it, and adds the ordering the deletion depends on:
    a dependent is selected through the rows of the parent that places it,
    so deleting the parent first would leave the dependent behind instead
    of removing it.
    """
    import importlib

    support = importlib.import_module("committed_scenario_support")
    from corridor.models import Base

    order, placements = support.place_project_tables(Base.metadata)
    position = {name: index for index, name in enumerate(order)}
    out_of_order = sorted(
        (name, parent)
        for name, found in placements.items()
        for _, parent, _ in found
        if position[name] >= position[parent]
    )
    uncovered = sorted(
        set(Base.metadata.tables) - set(order) - set(support.GLOBAL_TABLES)
    )

    assert out_of_order == [], (
        "these tables are deleted before the parent that places them can be "
        f"read: {out_of_order}"
    )
    assert uncovered == [], (
        "a new table is neither reachable from a project nor classified as "
        "global in tests/committed_scenario_support.py: "
        f"{uncovered}"
    )


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


# --- Every file under `scripts/` is reachable -------------------------------
#
# `scripts/` is the executable half of the control plane, and nothing proved a
# file in it had a door. `scripts/gate-run.sh` outlived the module it imported
# -- `corridor.extract_matrix`, retired by #768 -- without a single failure:
# no target ran it, so nothing noticed, and its only surviving trace was prose
# in `corridor.eval`. The engine scan above is the other rule that reads this
# tree and it reads `*.py` alone, so a shell file was invisible to every check
# here.

SCRIPTS_ROOT = REPO_ROOT / "scripts"

# What can start a process. The test tree is deliberately absent: a test
# imports what it measures, so admitting it would make every abandoned command
# reachable through its own test.
SCRIPT_CALLERS = ("Makefile", "Dockerfile", "docker-compose.yml", "pyproject.toml")
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"

# A file kept with no caller, and why. The rule below is exact in both
# directions, so an entry that gains a caller, loses its file, or states no
# reason fails here rather than ageing quietly.
RETAINED_WITHOUT_CALLER: dict[str, str] = {
    "scripts/sh99-cohort-wizard.sh": (
        "ADR-0027 superseded the operator step it walks, so nothing calls it, "
        "and its forty stages are the repository's only copy of sixty-five "
        "dated SH 99 commitment sentences quoted from the meeting minutes "
        "with their parties named. Deleting the file deletes customer "
        "commitment text; that is a decision to state, not a side effect."
    ),
    "scripts/test_feedback.py": (
        "the strict local verdict on a timing report, stricter than the "
        "advisory merge gate (ADR-0097). It is reachable only by typing its "
        "path, which the `make` target convention asks it not to be; the "
        "target belongs in the change that adds it."
    ),
    "scripts/test_timing.py": (
        "the second half of `make test-timing`: the target writes the JUnit "
        "file and a human converts it to duration weights by typing this "
        "path, as the target's own comment and "
        "docs/operations/test-timing-native-integration-2026-09-08.md both "
        "say. Same missing target as `test_feedback.py` above."
    ),
}


def _script_files() -> tuple[Path, ...]:
    """Every file under `scripts/`, whatever its suffix."""
    return tuple(
        sorted(
            path
            for path in SCRIPTS_ROOT.rglob("*")
            if path.is_file()
            and not any(
                part.startswith(".") or part == "__pycache__"
                for part in path.relative_to(SCRIPTS_ROOT).parts
            )
        )
    )


def _names(text: str, name: str) -> bool:
    """True when `text` names the whole of `name` rather than the end of one.

    `scripts/release_contract.py` ends with the name of
    `scripts/test_gate/contract.py`, and a plain substring search reads the
    first as a caller of the second.
    """
    return re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", text) is not None


def _prose(nodes: tuple[ast.AST, ...]) -> set[ast.AST]:
    """Every docstring constant among the nodes."""
    found: set[ast.AST] = set()
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
            found.add(first.value)
    return found


def _invoking_text(path: Path) -> str:
    """One caller's text with its prose removed.

    A comment or a docstring that still names a retired file is the stale
    reference this rule exists to catch, so neither counts as a call. A Python
    caller offers its string literals rather than its source, which is the
    form a script names a sibling resource in: `Path(__file__).parent /
    "retired_engines.json"`.
    """
    if path.suffix == ".py":
        nodes = read_python(path).nodes
        prose = _prose(nodes)
        return "\n".join(
            node.value
            for node in nodes
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in prose
        )
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _imported_scripts(path: Path) -> set[Path]:
    """The files under `scripts/` one caller's imports reach.

    `scripts/` is a regular package, so a shared definition is reached by
    `from scripts.test_gate.receipt import ...` and never by its path. Every
    package on the way is reached too, which is what gives an `__init__.py` a
    caller.
    """
    reached: set[Path] = set()
    if path.suffix != ".py":
        return reached
    for name, _ in imported_names(path):
        parts = name.split(".")
        if parts[0] != SCRIPTS_ROOT.name:
            continue
        for depth in range(1, len(parts) + 1):
            package = REPO_ROOT.joinpath(*parts[:depth])
            reached.update(
                candidate
                for candidate in (package / "__init__.py", package.with_suffix(".py"))
                if candidate.is_file()
            )
    return reached


def _callers_by_script() -> dict[str, tuple[str, ...]]:
    """Repository-relative path -> the callers naming it, for every script.

    A sibling may name a file by its bare name as well as by its path, because
    that is how a script resolves a neighbour it ships with; anything outside
    the tree spells the path.
    """
    scripts = _script_files()
    callers = tuple(
        path
        for path in [REPO_ROOT / name for name in SCRIPT_CALLERS]
        + sorted(WORKFLOW_ROOT.glob("*.yml"))
        if path.is_file()
    )
    found: dict[str, tuple[str, ...]] = {}
    for path in scripts:
        relative = path.relative_to(REPO_ROOT).as_posix()
        naming = set()
        for caller in callers + scripts:
            if caller == path:
                continue
            sibling = caller.is_relative_to(SCRIPTS_ROOT)
            text = _invoking_text(caller)
            if (
                _names(text, relative)
                or (sibling and _names(text, path.name))
                or path in _imported_scripts(caller)
            ):
                naming.add(caller.relative_to(REPO_ROOT).as_posix())
        found[relative] = tuple(sorted(naming))
    return found


def test_the_script_caller_scanner_reads_invocations_and_not_prose(tmp_path):
    """The retained list is only as honest as the scanner behind it."""

    def invoking(name: str, source: str) -> str:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return _invoking_text(path)

    commented = invoking("Makefile", "x:\n#\tuv run python scripts/test_timing.py o.xml\n")
    recipe = invoking("Makefile.ran", "x:\n\tuv run python scripts/test_timing.py o.xml\n")
    assert (
        _names(commented, "scripts/test_timing.py"),
        _names(recipe, "scripts/test_timing.py"),
    ) == (False, True)

    module = invoking(
        "caller.py",
        '"""Superseded by `gate-run.sh`."""\nPATH = "retired_engines.json"\n',
    )
    assert (
        _names(module, "gate-run.sh"),
        _names(module, "retired_engines.json"),
    ) == (False, True)

    longer = "python3 scripts/release_contract.py resolve outputs.json"
    assert (
        _names(longer, "scripts/test_gate/contract.py"),
        _names(longer, "contract.py"),
        _names(longer, "scripts/release_contract.py"),
    ) == (False, False, True)


def test_every_file_under_scripts_is_reachable_or_retained_with_a_reason():
    """An operator command with no door is a defect, and `*.sh` is a command.

    Reachability is decided here and nowhere else: a `make` target, a workflow
    step, the image build, the packaging file, or another script that runs or
    imports it. Anything else is retired, or recorded above with the reason it
    stays.
    """

    callers = _callers_by_script()
    problems: dict[str, str] = {}
    for relative, naming in sorted(callers.items()):
        reason = RETAINED_WITHOUT_CALLER.get(relative)
        if naming and reason is not None:
            problems[relative] = (
                f"is named by {', '.join(naming)}; "
                "delete its RETAINED_WITHOUT_CALLER line"
            )
        elif not naming and reason is None:
            problems[relative] = (
                "nothing runs or imports it: give it a `make` target, a "
                "workflow step or a caller, retire it, or record why it stays"
            )
        elif not naming and not reason.strip():
            problems[relative] = "is retained without a reason"
    for relative in sorted(set(RETAINED_WITHOUT_CALLER) - set(callers)):
        problems[relative] = "is retained but no longer exists; delete its line"

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
    # The families folded into the unreleased transition live one per module in
    # a package beside `baseline_versions`, not inside it. Alembic lists `*.py`
    # in the version location and does not descend into directories, so a
    # family module is not a revision candidate wherever it sits; keeping the
    # package out of the version location keeps that true of the listing too,
    # so the count above stays a count of revisions.
    families = SOURCE_ROOT / "migrations" / "source_append_commands"
    assert families.is_dir() and len(tuple(families.glob("*.py"))) > 1
    assert families.parent.name == "migrations", (
        "the family package belongs beside the executable version location, "
        "never inside it"
    )


# --- Prompt files: loaded, or retained with the reason they are kept ---------
#
# `prompts/` is executable. `scripts/classify_ci_change.py` deliberately keeps
# it out of `DOCUMENTATION_PATHS`, so every file in it runs the behavior
# shards. Eight of its nineteen files ran them while no loader named them, no
# artifact held their digest and no test opened them.
#
# A superseded prompt can still be worth keeping, and that reason was written
# nowhere: an `ExtractionRun` row stores `prompt_version` and `prompt_sha256`
# (`storage_baseline.py`), so a released run's prompt may have no preimage but
# the file it was read from. It is not hypothetical. The retained receipt under
# `artifacts/product-proving/sh99-8da8568-extraction-repeatability-failed`
# exports a run recording `prompt_version: minutes_v3` and no digest, and the
# same receipt names `matrix_tiered_v2` and `matrix_tiered_v3`, whose bytes are
# in no file here at all. Those two prompts are already unrecoverable.
#
# So the retained-revision answer above applies unchanged: one executable
# directory holding exactly what the code loads, one inert directory holding
# the retained bytes, and a registry saying what each retained file is the
# preimage of. `corridor_pdf_reader` keeps its own prompt beside its module;
# only the top-level directory is this rule's subject.

PROMPT_ROOT = REPO_ROOT / "prompts"
RETAINED_PROMPTS = REPO_ROOT / "docs" / "history" / "prompts"
PROMPT_REGISTRY = RETAINED_PROMPTS / "retained.json"


def _source_string_literals() -> frozenset[str]:
    """Every string literal `src/` spells, whatever module spells it."""
    literals: set[str] = set()
    for path in python_files(REPO_ROOT / "src"):
        literals.update(
            node.value
            for node in read_python(path).nodes
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return frozenset(literals)


def _a_loader_names(prompt: Path, literals: frozenset[str]) -> bool:
    """True when some module in `src/` names this prompt file.

    Three conventions count: a path literal ending in the file name, the bare
    version string, which `corridor_pdf_reader.replacement.semantics` joins as
    `f"{PROMPT_VERSION}.md"`, and a version `corridor.prompt_library` resolves
    to this file. That last one is asked of the loader rather than re-derived
    here, because a family may spell its version with hyphens where the file
    stem uses underscores, and one rule for that belongs in the loader.

    Equality rather than substring is what keeps a docstring that merely
    mentions a retired version from reading as a loader -- `extract_minutes_v5`
    opens by explaining what it took over from `extract_minutes_v4`, and that
    is not a use of the file.
    """
    return any(
        literal == prompt.name
        or literal.endswith("/" + prompt.name)
        or literal == prompt.stem
        or installed_prompt_path(literal).name == prompt.name
        for literal in literals
    )


def _registered_prompts() -> dict[str, dict]:
    return json.loads(PROMPT_REGISTRY.read_text())["retained"]


def test_the_prompt_directory_holds_exactly_the_files_a_loader_names():
    literals = _source_string_literals()

    unnamed = [
        path.name
        for path in sorted(PROMPT_ROOT.glob("*.md"))
        if not _a_loader_names(path, literals)
    ]
    other = [path.name for path in sorted(PROMPT_ROOT.iterdir()) if path.suffix != ".md"]

    assert unnamed == [], (
        "no module in src/ reads these, so they are not executable prompts: move "
        f"them to {RETAINED_PROMPTS.relative_to(REPO_ROOT)} and register why they "
        "are retained, rather than leaving them to run the behavior shards"
    )
    assert other == [], "prompts/ holds prompt files and nothing else"


def test_every_retained_prompt_file_is_registered_with_its_current_bytes():
    registered = _registered_prompts()
    present = sorted(path.name for path in RETAINED_PROMPTS.glob("*.md"))

    assert present == sorted(registered), (
        "a retained prompt is listed in retained.json or it is not retained; a "
        "registered file that has disappeared is a preimage that is now lost"
    )
    changed = [
        name
        for name, entry in registered.items()
        if hashlib.sha256((RETAINED_PROMPTS / name).read_bytes()).hexdigest()
        != entry["sha256"]
    ]

    assert changed == [], (
        "these retained prompts no longer hash to their registered digest -- "
        "their bytes are the preimage of a released run and cannot be edited"
    )


def test_a_registered_retention_reason_names_a_real_preimage():
    registered = _registered_prompts()
    literals = _source_string_literals()
    wrong = []
    for name, entry in registered.items():
        preimage_of = entry["preimage_of"]
        if (entry["basis"] == "receipt") != bool(preimage_of):
            wrong.append(f"{name}: basis {entry['basis']!r} disagrees with preimage_of")
        if _a_loader_names(RETAINED_PROMPTS / name, literals):
            wrong.append(f"{name}: a loader names it, so it belongs in prompts/")
        if not (PROMPT_ROOT / entry["superseded_by"]).exists():
            wrong.append(f"{name}: superseded_by names no file in prompts/")
        recorded = re.compile(
            r'"prompt_version"\s*:\s*"%s"' % re.escape(entry["prompt_version"])
        )
        for artifact in preimage_of:
            path = REPO_ROOT / artifact
            if not path.exists() or not recorded.search(path.read_text()):
                wrong.append(f"{name}: {artifact} does not record that prompt version")

    assert wrong == []



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


def _composed_templates() -> dict[Path, frozenset[Path]]:
    """Every template each template renders: its includes, imports and extends.

    Read from Jinja's own parse rather than from the text, because a template
    that names another one in a comment does not render it and a template that
    reaches the primitives through a partial does.
    """
    import jinja2

    environment = jinja2.Environment(autoescape=True)
    composition: dict[Path, frozenset[Path]] = {}
    for path in sorted(TEMPLATE_ROOT.glob("*.html")):
        composed: set[Path] = set()
        for node in environment.parse(path.read_text(encoding="utf-8")).find_all(
            (jinja2.nodes.Include, jinja2.nodes.Import, jinja2.nodes.FromImport,
             jinja2.nodes.Extends)
        ):
            if isinstance(node.template, jinja2.nodes.Const):
                composed.add(TEMPLATE_ROOT / str(node.template.value))
        composition[path] = frozenset(composed)
    return composition


def _shared_templates() -> tuple[Path, ...]:
    """The primitives, every page that renders them, and every partial in those.

    Membership used to be "this file's own text contains `_primitives.html`",
    which is not the same set as "the markup a guarded page sends": it covered
    10 of 36 templates, and the two partials composed into the guarded
    `queue.html` were never read even though their markup reaches the same
    screen. Composition decides it now, so a page cannot leave the rule by
    moving its import into a partial, and a partial cannot escape it by never
    naming the primitives itself.
    """
    composition = _composed_templates()

    def renders(path: Path) -> frozenset[Path]:
        """Every template this one renders, directly or through a partial."""
        seen: set[Path] = set()
        frontier = [path]
        while frontier:
            for composed in composition.get(frontier.pop(), frozenset()):
                if composed not in seen:
                    seen.add(composed)
                    frontier.append(composed)
        return frozenset(seen)

    shared = {PRIMITIVES_TEMPLATE}
    for path in composition:
        composed = renders(path)
        if PRIMITIVES_TEMPLATE in composed:
            shared |= {path, *composed}
    return tuple(sorted(shared))


def test_shared_templates_exist_and_include_the_first_consumer():
    """The check is worthless if it silently covers nothing.

    Its reach is the point: `ledger.html` renders the primitives directly, and
    `_evidence.html` and `_coordinate.html` reach the same screen by being
    composed into `queue.html`, which does.
    """
    names = {path.name for path in _shared_templates()}

    assert PRIMITIVES_TEMPLATE.name in names
    assert "ledger.html" in names
    assert {"_evidence.html", "_coordinate.html"} <= names, (
        "a partial rendered inside a guarded page is markup that page sends"
    )


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


# --- A template renders customer words; it never mints them -------------------
#
# `ui_primitives.py:19-20` states the rule for the Python half of the screen
# vocabulary: words come from `corridor.presentation`, "which holds the adopted
# customer vocabulary. This module never coins a domain term." The Jinja half
# had no such guard, and `dependency.html` minted six sentences — a
# documentation-state pair and one next step for each routed support-update
# destination — that appeared nowhere in `src/` and that nothing but four
# full-stack HTTP assertions pinned. A word a customer reads needs an owner:
# that is where terminology review looks, and it is what a test can assert
# without a browser.
#
# The rule is mechanical. A `{% set %}` binds names, values and identifiers; a
# string literal with whitespace inside it is prose, and prose a customer reads
# comes from a vocabulary function the template calls. It is deliberately
# narrower than "no sentence anywhere in a template": a screen's own static
# markup is where its words belong. The `{% set %}` is where a sentence gets
# *chosen* — branched on record state — and that choice belongs to the module
# that owns the state, not to the screen that shows it.

_TEMPLATE_PROSE = re.compile(r"\S\s+\S")


def _minted_sentences(path: Path) -> list[str]:
    """Every prose literal a `{% set %}` in this template composes for itself."""
    import jinja2

    tree = jinja2.Environment(autoescape=True).parse(path.read_text(encoding="utf-8"))
    minted: list[str] = []
    for assignment in tree.find_all((jinja2.nodes.Assign, jinja2.nodes.AssignBlock)):
        for node in assignment.find_all(
            (jinja2.nodes.Const, jinja2.nodes.TemplateData)
        ):
            words = node.value if isinstance(node, jinja2.nodes.Const) else node.data
            if isinstance(words, str) and _TEMPLATE_PROSE.search(words):
                minted.append(words.strip())
    return minted


def test_no_template_mints_a_customer_sentence_in_a_set():
    """A screen chooses which adopted word to render, never which one to write."""
    offenders = {
        path.name: sorted(set(minted))
        for path in sorted(TEMPLATE_ROOT.glob("*.html"))
        if (minted := _minted_sentences(path))
    }

    assert offenders == {}, (
        f"{offenders}: a template composes a customer sentence in a set tag "
        "instead of calling the module that owns that vocabulary. Move the "
        "words beside their meaning — `corridor.presentation` for an adopted "
        "label, the reader that owns the state for a sentence about it — and "
        "render the value"
    )
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
    the list -- and cannot pay for it by raising the ceiling in the same
    commit either, because `assert_ratchet` reads the ceiling recorded at the
    merge base.
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
    assert_ratchet(
        "src/corridor/access.py:NOT_YET_PARTITIONED_CEILING",
        measured=outstanding,
        recorded=access.NOT_YET_PARTITIONED_CEILING,
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


def test_every_frontend_receipt_route_is_a_route_the_application_serves():
    """A receipt contract naming a route the router lacks describes nothing.

    The receipt writer reads the template and method from the request's
    matched route, so the contract table holds only the route name and the
    statuses that route may return. This is the check that every such name is
    one the application serves, with exactly one method the receipt vocabulary
    admits; a handler renamed in its decorator with the table left alone fails
    here rather than at the first request that route receives.
    """

    from corridor.frontend_request_receipts import (
        ROUTE_CONTRACTS,
        served_route_identity,
    )
    from corridor.web.app import app

    unserved = sorted(
        name
        for name in ROUTE_CONTRACTS
        if served_route_identity(app.routes, name) is None
    )

    assert unserved == [], (
        "these frontend receipt contracts name no route the application serves "
        "with one GET or POST method; remove them from "
        "corridor.frontend_request_receipts.ROUTE_CONTRACTS or restore the route"
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


def test_the_unconfirmed_reading_append_has_exactly_its_production_caller():
    """ADR-0094's Unconfirmed reading is recorded by a seam ingest reaches.

    `scanned_reading.record_unconfirmed_readings` is the only append of a
    scanned `unconfirmed` resolution, and for a while nothing in production
    called it: the class existed in tests, `load_project`'s corroboration
    upgrade was wired, and the population it reads over was empty. Ingest's
    persistence seam is the caller. Exact, in both directions: losing the
    caller re-opens the gap, and a second caller is a second door into the
    class, reviewed as one.
    """

    sites = callers_of({"record_unconfirmed_readings"}, (SOURCE_ROOT,))

    assert {
        path.name
        for path in sites["record_unconfirmed_readings"]
        if path.name != "scanned_reading.py"
    } == {"ingest.py"}


# `make` targets that run the stack, the toolchain, or another project's test
# runner. Everything else runs a Corridor command that states its own contract,
# which is what `make <target> ARGS=--help` prints.
TOOLCHAIN_TARGETS = frozenset({
    "help", "boot", "up", "down", "psql", "check", "test-infra", "queue", "pdf-reader-node",
})

# Operator commands that still read `sys.argv` by hand and print their own
# usage line, so no parser can carry their contract and it stays in the
# Makefile comment. This list may fall and may never rise: a new command states
# its contract on its parser, where `--help` finds it.
HAND_PARSED_COMMANDS = frozenset({
    "active-run", "admission", "adr-index", "agreements", "candidate-model", "demo",
    "docs", "eval", "evidence-investigator", "evidence-shadow", "evidence-shadow-eval",
    "exceptions", "extract", "ingest", "link-deliveries", "milestones",
    "pipeline-qualification", "report",
})


def test_a_summary_belongs_to_the_target_written_directly_under_it():
    """What the guard below reads, read on a file whose answer is known.

    The rule it enforces is only as good as the attribution: a reader that
    walked up past an intervening target would hand every target the block
    above it and pass while the comments stayed detached. A `.PHONY`
    declaration and a variable default do sit between a comment and its
    target in this Makefile, and those do not end the block.
    """

    makefile = REPO_ROOT / "tests" / "fixtures" / "summary-attribution.mk"
    found = make_targets(makefile)

    assert found["described"].summary == ("Its own summary.",)
    assert found["undescribed"].summary == ()
    assert found["separated"].summary == ("Past a .PHONY and a variable.",)
    assert found["described"].recipe == ("first command", "second command with a continuation",)


def test_every_make_target_carries_its_own_summary():
    """A comment that is not directly above its target documents the wrong one.

    One comment block described `make storage`, worked invocations included,
    and the target written under it was `clean-test-databases`, whose recipe
    drops PostgreSQL databases; `storage:` three lines further down carried no
    comment at all. `CLAUDE.md` promises "the Makefile comments say what each
    one takes", and nothing read those comments, so a reader applying that
    convention read `make clean-test-databases ARGS="migrate"` as documented.
    """

    silent = sorted(
        name for name, target in make_targets().items()
        if not any(line.strip() for line in target.summary)
    )

    assert silent == [], (
        "these targets carry no summary of their own, so the nearest comment "
        f"above them describes something else: {', '.join(silent)}"
    )


def test_every_make_target_names_the_command_it_runs():
    """`make help` is an index of commands, and an index needs the command.

    A recipe that names no module is a stack or toolchain step, and those are
    listed rather than discovered: adding one is a deliberate edit here, not a
    silent exemption a new operator command can borrow.
    """

    targets = make_targets()
    assert TOOLCHAIN_TARGETS <= set(targets), (
        "TOOLCHAIN_TARGETS names a target the Makefile does not define: "
        f"{sorted(TOOLCHAIN_TARGETS - set(targets))}"
    )
    nameless, missing, claimed = [], [], []
    for name, target in targets.items():
        path = entry_point(target)
        if path is None:
            if name not in TOOLCHAIN_TARGETS:
                nameless.append(name)
        elif name in TOOLCHAIN_TARGETS:
            claimed.append(f"{name} runs {path.relative_to(REPO_ROOT)}")
        elif not path.exists():
            missing.append(f"{name} runs {path.relative_to(REPO_ROOT)}")

    assert nameless == [], (
        "these targets run no Python entry point; give them one, or record "
        f"them in TOOLCHAIN_TARGETS: {', '.join(sorted(nameless))}"
    )
    assert missing == [], (
        "these targets run a module that does not exist: " + ", ".join(sorted(missing))
    )
    assert claimed == [], (
        "these targets do run a Corridor command, so they are not toolchain "
        "steps: " + ", ".join(sorted(claimed))
    )


def test_every_operator_command_states_its_contract_on_its_parser():
    """`--help` is the contract, because a comment cannot travel with the code.

    The flag and the example that shows it have to change together, and they
    only can when they live in the same file. A parser with no description
    prints its options and says nothing about what the command is for.
    """

    silent, hand_parsed = [], set()
    for name, target in make_targets().items():
        if name in TOOLCHAIN_TARGETS:
            continue
        path = entry_point(target)
        assert path is not None and path.exists(), name
        builds_parser, description = parser_description(path)
        if not builds_parser:
            hand_parsed.add(name)
        elif not description.strip():
            silent.append(f"{name} ({path.relative_to(REPO_ROOT)})")

    assert silent == [], (
        "these commands build a parser that describes nothing, so "
        "`make <target> ARGS=--help` prints only flags: " + ", ".join(sorted(silent))
    )
    assert_ratchet(
        "tests/test_architecture.py:HAND_PARSED_COMMANDS",
        measured=hand_parsed,
        recorded=HAND_PARSED_COMMANDS,
    )

