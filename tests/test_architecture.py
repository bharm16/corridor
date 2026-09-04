"""Executable module-interface rules for the Corridor source graph."""

from __future__ import annotations

import ast
import importlib.util
import re
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
        for node in ast.walk(_tree(path)):
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
        for node in ast.walk(_tree(path)):
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
        for node in ast.walk(_tree(path)):
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
        for node in ast.walk(_tree(path)):
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
        for node in ast.walk(_tree(path)):
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
    tree = ast.parse((worker / "render_worker.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"corridor", "sqlalchemy", "psycopg", "boto3", "botocore"}

    assert imported & forbidden == set()
    lock = (worker / "uv.lock").read_text(encoding="utf-8")
    assert not any(f'name = "{name}"' in lock for name in forbidden)


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
        for node in ast.walk(_tree(path)):
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
    } - REPORT_READING_PAYLOADS - MAPPING_REGISTRATION_REFERENCES
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
