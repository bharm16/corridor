"""Every written-down name has exactly one owning module (ADR-0047, ADR-0048).

ADR-0047 gave each domain term one recorded definition and ADR-0048 finished
adopting them, but a *string* is not a term until one module owns the bytes.
Four strings had drifted into being restated at their point of use: the
``adopted_baseline`` operating-mode value, the six database role and login
names, the "Applies To: not yet known" Attention Reason label, and — the same
failure in a different vocabulary — the one naive clock call left in the
product. Each is compared or displayed somewhere far from where it is defined,
so a rename that missed a copy would not fail a test; it would silently mean a
project is in no mode, a capability holds no privilege, or a label reads
differently on two screens.

The guard matches **whole string constants** rather than substrings. Prose in
a docstring, an issue reference in a comment, and a role name inlined inside a
``grant ...`` statement are all legitimate: none of them is a program deciding
something by comparing against a copy of the name. Only a constant whose entire
value is the name is a restatement, and that is what each test below counts.

The scan covers ``src`` only. ``scripts/container_entrypoint.py`` also names
the two capability logins, and deliberately so: it runs as the container's
entrypoint *before* the application exists, imports nothing from ``corridor``,
and assembles the URL the application will later read. A standalone bootstrap
that imported the package to learn a login would defeat its own purpose.

Migrations are a declared region rather than a consumer. A revision is
replayable released history: its DDL and PL/pgSQL inline every role name in SQL
text, and ``src/corridor/migrations/versions`` holds inert source bytes retained
for released policy fingerprints. So the migration package keeps its own copies,
and ``test_migration_role_constants_do_not_drift`` asserts the copies are equal
instead of asserting there is one.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

from corridor import control_plane_schema, db_roles, operating_mode, statement_values
from source_scan_support import python_files, read_python, source_scan_cache  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORRIDOR = SRC / "corridor"
MIGRATIONS = CORRIDOR / "migrations"

DB_ROLES_OWNER = CORRIDOR / "db_roles.py"
CONTROL_PLANE_OWNER = CORRIDOR / "control_plane_schema.py"
OPERATING_MODE_OWNER = CORRIDOR / "operating_mode.py"
UNKNOWN_SCOPE_OWNER = CORRIDOR / "statement_values.py"


# Each entry says which module may restate a name and why it is not a
# restatement of the owned vocabulary. An empty reason is not accepted.
ROLE_NAME_ALLOWLIST: dict[tuple[str, str], str] = {
    ("corridor/config.py", "corridor_web"): (
        "the local-clone default *password* for the web capability, which "
        "happens to equal its login; binding it to the login constant would "
        "make renaming the role rotate a credential"
    ),
    ("corridor/config.py", "corridor_worker"): (
        "the local-clone default *password* for the worker capability, as above"
    ),
}

ADOPTED_BASELINE_ALLOWLIST: dict[str, str] = {
    "corridor/activation.py": (
        "an activation gate name in BASE_GATES, not the operating-mode value: "
        "the gate is named after the capability it proves and is matched "
        "against evidence filenames, so it must not move when the mode does"
    ),
}

UNKNOWN_SCOPE_ALLOWLIST: dict[str, str] = {}

NAIVE_CLOCK_ALLOWLIST: dict[str, str] = {}


def _application_files() -> tuple[Path, ...]:
    """Every product module outside the migration package."""
    return tuple(
        path for path in python_files(SRC)
        if MIGRATIONS not in path.parents and path.parent != MIGRATIONS
    )


def _whole_string_constants(path: Path, wanted: frozenset[str]) -> list[tuple[int, str]]:
    """Lines where a string constant's entire value is one of ``wanted``."""
    return [
        (node.lineno, node.value)
        for node in read_python(path).nodes
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in wanted
    ]


def _relative(path: Path) -> str:
    return str(path.relative_to(SRC))


def test_every_allowlist_entry_states_a_reason():
    """An allowlist without reasons is a suppression list."""
    reasons = [
        *ROLE_NAME_ALLOWLIST.values(),
        *ADOPTED_BASELINE_ALLOWLIST.values(),
        *UNKNOWN_SCOPE_ALLOWLIST.values(),
        *NAIVE_CLOCK_ALLOWLIST.values(),
    ]
    assert all(len(reason.split()) >= 5 for reason in reasons), reasons


def test_database_role_and_login_names_are_owned_by_db_roles():
    """One module writes each capability's login; every caller imports it."""
    names = db_roles.DATABASE_ROLE_NAMES
    assert names, "db_roles must publish the set it owns"

    restated = []
    for path in _application_files():
        if path == DB_ROLES_OWNER:
            continue
        relative = _relative(path)
        for line, value in _whole_string_constants(path, names):
            if ROLE_NAME_ALLOWLIST.get((relative, value)):
                continue
            restated.append(f"{relative}:{line} restates {value!r}")

    assert restated == [], (
        "import the name from corridor.db_roles instead:\n" + "\n".join(restated)
    )


def test_control_plane_role_names_are_owned_by_control_plane_schema():
    """The control plane is a separate database (ADR-0079) and names its own roles."""
    names = frozenset(
        {control_plane_schema.OPERATIONS_ROLE, control_plane_schema.RESOLVER_ROLE}
    )

    restated = []
    for path in _application_files():
        if path == CONTROL_PLANE_OWNER:
            continue
        for line, value in _whole_string_constants(path, names):
            restated.append(f"{_relative(path)}:{line} restates {value!r}")

    assert restated == [], (
        "import the name from corridor.control_plane_schema instead:\n"
        + "\n".join(restated)
    )


def test_migration_role_constants_do_not_drift():
    """A revision keeps its own copies, and they must equal the owned names.

    A migration is replayed released history, so it states its role names
    itself rather than importing today's application vocabulary. That is only
    safe while the two agree, which is what this asserts: every constant a
    migration module binds to a ``corridor_`` name binds it to a name the
    product still owns, spelled identically.
    """
    owned = set(db_roles.DATABASE_ROLE_NAMES) | {
        control_plane_schema.OPERATIONS_ROLE,
        control_plane_schema.RESOLVER_ROLE,
        # Created and granted only by migrations: the operations role for the
        # retained legacy history relations has no application caller.
        "corridor_history_operations",
    }

    unknown = []
    for path in python_files(MIGRATIONS):
        for node in read_python(path).nodes:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if not value.value.startswith("corridor_"):
                continue
            # A grant list ("corridor_web, corridor_worker") is still a list of
            # these names, so check each part rather than the joined fragment.
            named = [part.strip() for part in value.value.split(",")]
            if not set(named) <= owned:
                unknown.append(
                    f"{path.relative_to(SRC)}:{node.lineno} names "
                    + repr(sorted(set(named) - owned))
                )

    assert unknown == [], (
        "a migration names a role the product no longer owns:\n" + "\n".join(unknown)
    )


def test_adopted_baseline_mode_value_is_owned_by_operating_mode():
    """The mode a project is in is decided by one string, defined once.

    Only ``adopted_baseline`` is guarded. Its partner value, ``legacy``, is an
    ordinary English word that three unrelated vocabularies already use — the
    legacy *reading source* in ``constraint_reading``, and the legacy *side* of
    the reader and rasterizer comparisons — so owning the bare word would
    rename things that were never the operating mode. The pair is defined
    together in ``operating_mode``, and the one place that validates a reported
    mode against both now reads both from there.
    """
    names = frozenset({operating_mode.ADOPTED_BASELINE})

    restated = []
    for path in _application_files():
        if path == OPERATING_MODE_OWNER:
            continue
        relative = _relative(path)
        if ADOPTED_BASELINE_ALLOWLIST.get(relative):
            continue
        for line, value in _whole_string_constants(path, names):
            restated.append(f"{relative}:{line} restates {value!r}")

    assert restated == [], (
        "import the value from corridor.operating_mode instead:\n" + "\n".join(restated)
    )


def test_unknown_scope_label_lives_with_its_detector():
    """The label and the detector that recognizes it are one decision.

    ``states_unknown_scope`` is the regular expression that decides a statement
    reports unknown scope; ``UNKNOWN_SCOPE_LABEL`` is what the screens then
    show. CONTEXT.md records that this is *current Project Record state* rather
    than a question to ask, so the wording is load-bearing: a screen that typed
    its own copy could show a phrasing the detector no longer recognizes. They
    live in ``statement_values``, the dependency-free module both the readers
    and the writers already import.
    """
    label = statement_values.UNKNOWN_SCOPE_LABEL
    assert statement_values.states_unknown_scope(label), (
        "the owned label must be recognized by the detector beside it"
    )

    restated = []
    for path in _application_files():
        if path == UNKNOWN_SCOPE_OWNER:
            continue
        relative = _relative(path)
        if UNKNOWN_SCOPE_ALLOWLIST.get(relative):
            continue
        for line, _ in _whole_string_constants(path, frozenset({label})):
            restated.append(f"{relative}:{line} restates the label")

    assert restated == [], (
        "import UNKNOWN_SCOPE_LABEL from corridor.statement_values instead:\n"
        + "\n".join(restated)
    )


def test_no_module_reads_a_naive_clock():
    """``datetime.now()`` with no timezone reads the machine's local wall clock.

    Every recorded instant in the product is stored ``timestamptz`` and written
    as ``datetime.now(timezone.utc)``. One route-triage resolution wrote
    ``datetime.now().astimezone()`` instead, which produces an aware value only
    by assuming the *host's* zone — so the same act recorded on a developer's
    laptop and on a UTC runner carries different offsets for no stated reason.
    """
    naive = []
    for path in python_files(SRC):
        relative = str(path.relative_to(SRC))
        if NAIVE_CLOCK_ALLOWLIST.get(relative):
            continue
        for node in read_python(path).nodes:
            if not isinstance(node, ast.Call) or node.args or node.keywords:
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "now":
                owner = function.value
                name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
                if name == "datetime":
                    naive.append(f"{relative}:{node.lineno}")

    assert naive == [], (
        "pass an explicit timezone, as every other write seam does:\n"
        + "\n".join(naive)
    )


def test_importing_corridor_db_builds_no_engine_and_opens_no_connection():
    """``import corridor.db`` must resolve nothing until a capability is asked for.

    The test harness rewrites ``DATABASE_URL`` in ``pytest_configure`` and
    provisions the worker database on its first DBAPI connection, so an engine
    built during import would bind whatever URL happened to be configured then
    and would pull the customer router — which reads deployment configuration —
    into every command that only wanted a session factory. Importing with an
    unreachable address proves nothing is resolved or connected at import.
    """
    probe = (
        "import sys\n"
        "import corridor.db as db\n"
        "assert 'corridor.customer_routing_runtime' not in sys.modules, 'router imported'\n"
        "assert db.engine_state() == (), db.engine_state()\n"
        "print('imported')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": "postgresql+psycopg://nobody:nobody@127.0.0.1:1/absent",
            "PYTHONPATH": str(SRC),
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout
