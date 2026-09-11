"""Failure cleanup and closed-world integrity for M8 acceptance artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import corridor.m8_acceptance_database as acceptance_database
from corridor.config import settings
from corridor.db import engine
from corridor.m8_acceptance import AcceptanceError
from corridor.m8_acceptance_bundle import CorruptBundle, verify_bundle


REPO_ROOT = Path(__file__).resolve().parents[1]
DATABASE_LABEL = "m8_acceptance"
DATABASE_PREFIX = acceptance_database.disposable_database_prefix(DATABASE_LABEL)


def test_disposable_database_name_carries_its_process_owner():
    database_name = acceptance_database.disposable_database_name("due_work_test")

    assert re.fullmatch(
        rf"corridor_disposable_due_work_test_{os.getpid()}_[0-9a-f]{{12}}",
        database_name,
    )
    assert acceptance_database.is_disposable_database_name(database_name)
    assert (
        acceptance_database.disposable_database_owner_pid(database_name)
        == os.getpid()
    )


def test_a_name_outside_the_namespace_is_not_reclaimable():
    for name in (
        "corridor",
        "corridor_m8_acceptance_0e5b0adcb1d54d45a164e63baa8f3841",
        "corridor_disposable_due_work_test_legacy",
    ):
        assert not acceptance_database.is_disposable_database_name(name)
        assert acceptance_database.disposable_database_owner_pid(name) is None


def test_a_label_too_long_for_postgresql_is_refused():
    with pytest.raises(acceptance_database.DisposableDatabaseRefused):
        acceptance_database.disposable_database_name("x" * 40)

    with pytest.raises(acceptance_database.DisposableDatabaseRefused):
        acceptance_database.disposable_database_prefix("Not_A_Label")


def test_abandoned_copy_reclamation_keeps_live_process_databases(monkeypatch):
    dropped = []
    connection = _DatabaseCatalogConnection(
        (
            "corridor_disposable_due_work_test_101_deadbeefcafe",
            "corridor_disposable_due_work_test_202_feedfacecafe",
            "corridor_disposable_due_work_test_legacy",
        ),
        dropped,
    )
    monkeypatch.setattr(
        acceptance_database,
        "process_is_running",
        lambda pid: pid == 202,
    )

    acceptance_database.reclaim_abandoned_database_copies(
        connection,
        "due_work_test",
    )

    assert dropped == ["corridor_disposable_due_work_test_101_deadbeefcafe"]


class _DatabaseCatalogConnection:
    def __init__(self, names, dropped):
        self._names = names
        self._dropped = dropped

    def scalars(self, _statement, _parameters):
        return self._names

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if sql.startswith("select pg_terminate_backend"):
            return None
        match = re.search(r'drop database if exists "([^"]+)"', sql)
        if match:
            self._dropped.append(match.group(1))
        return None


def test_reusable_template_lifecycle_uses_the_stable_maintenance_database(
    monkeypatch,
):
    seen_databases = []

    def stop_after_observing_admin_url(_engine, admin_url, **_kwargs):
        seen_databases.append(admin_url.database)
        raise AcceptanceError("observed template admin URL")

    monkeypatch.setattr(
        acceptance_database,
        "_ensure_migrated_template",
        stop_after_observing_admin_url,
    )

    with pytest.raises(AcceptanceError, match="observed template admin URL"):
        with acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=REPO_ROOT,
            label="due_work_test",
            reuse_migrated_template=True,
        ):
            raise AssertionError("template setup must precede yield")

    assert seen_databases == ["postgres"]


def test_reusable_template_honors_a_historical_revision(monkeypatch):
    seen_revisions = []

    def stop_after_observing_revision(
        _engine,
        _admin_url,
        *,
        migration_revision,
        **_kwargs,
    ):
        seen_revisions.append(migration_revision)
        raise AcceptanceError("observed historical template revision")

    monkeypatch.setattr(
        acceptance_database,
        "_ensure_migrated_template",
        stop_after_observing_revision,
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda *_args, **_kwargs: pytest.fail(
            "historical reusable provisioning replayed migrations per copy"
        ),
    )

    with pytest.raises(
        AcceptanceError,
        match="observed historical template revision",
    ):
        with acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=REPO_ROOT,
            label="migration_test",
            migration_revision="b4d1e2f3a5c6",
            reuse_migrated_template=True,
        ):
            raise AssertionError("template setup must precede yield")

    assert seen_revisions == ["b4d1e2f3a5c6"]


def test_reusable_template_cache_separates_migration_revisions(monkeypatch):
    migrated = []
    reclaimed_prefixes = []
    admin_engine = _TemplateAdminEngine()
    admin_url = make_url(settings.database_url).set(database="postgres")
    acceptance_database._migrated_templates.clear()
    monkeypatch.setattr(
        acceptance_database,
        "_drop_abandoned_templates",
        lambda _connection: None,
    )
    monkeypatch.setattr(
        acceptance_database,
        "reclaim_abandoned_database_copies",
        lambda _connection, prefix: reclaimed_prefixes.append(prefix),
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda _url, *, revision, **_kwargs: migrated.append(revision),
    )
    monkeypatch.setattr(acceptance_database.atexit, "register", lambda *_args: None)

    try:
        head_template = acceptance_database._ensure_migrated_template(
            admin_engine,
            admin_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
            label="migration",
            migration_revision="head",
        )
        predecessor_template = acceptance_database._ensure_migrated_template(
            admin_engine,
            admin_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
            label="historical",
            migration_revision="b4d1e2f3a5c6",
        )
        repeated_head_template = acceptance_database._ensure_migrated_template(
            admin_engine,
            admin_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
            label="migration",
            migration_revision="head",
        )
    finally:
        acceptance_database._migrated_templates.clear()

    assert migrated == ["head", "b4d1e2f3a5c6"]
    assert reclaimed_prefixes == ["migration", "historical", "migration"]
    assert head_template != predecessor_template
    assert repeated_head_template == head_template


def test_a_caller_owned_template_is_copied_without_migrating_another(monkeypatch):
    """The pytest harness migrates one schema per run and hands it over here.

    Without this, the harness's template and this module's per-process template
    were two migrated copies of the same schema, and neither could see the
    other.
    """

    database_name = acceptance_database.disposable_database_name(DATABASE_LABEL)
    monkeypatch.setattr(
        acceptance_database,
        "disposable_database_name",
        lambda _label: database_name,
    )
    monkeypatch.setattr(
        acceptance_database,
        "_ensure_migrated_template",
        lambda *_args, **_kwargs: pytest.fail("a second template was migrated"),
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda *_args, **_kwargs: pytest.fail("a copied schema was migrated again"),
    )
    copied = []
    monkeypatch.setattr(
        acceptance_database,
        "_copy_migrated_template",
        lambda _engine, template, name, **_kwargs: copied.append((template, name)),
    )
    monkeypatch.setattr(
        acceptance_database,
        "read_migration_head",
        lambda *_args, **_kwargs: "test-head",
    )
    monkeypatch.setattr(
        acceptance_database,
        "require_experimental_database",
        lambda *_args, **_kwargs: None,
    )

    with acceptance_database.provision_disposable_postgres(
        settings.database_url,
        repo_root=REPO_ROOT,
        label=DATABASE_LABEL,
        template_database="corridor_pytest_101_deadbeef_tmpl",
    ) as provisioned:
        assert provisioned.name == database_name

    assert copied == [("corridor_pytest_101_deadbeef_tmpl", database_name)]


def test_a_caller_owned_template_refuses_a_revision_it_cannot_prove(monkeypatch):
    """A template carries the revision it was migrated to, and says nothing more."""

    monkeypatch.setattr(
        acceptance_database,
        "disposable_database_name",
        lambda _label: pytest.fail("the refusal must precede naming a database"),
    )

    with pytest.raises(
        acceptance_database.DisposableDatabaseRefused,
        match="historical revision needs its own migration",
    ):
        with acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=REPO_ROOT,
            label=DATABASE_LABEL,
            migration_revision="b4d1e2f3a5c6",
            template_database="corridor_pytest_101_deadbeef_tmpl",
        ):
            raise AssertionError("the refusal must precede the yield")


def test_a_template_another_worker_is_copying_is_waited_out(monkeypatch):
    """One template shared across xdist workers is contended by construction.

    A per-process template is only ever copied by the process that made it, so
    this collision first became reachable when the harness started handing its
    own run template over.
    """

    monkeypatch.setattr(acceptance_database.time, "sleep", lambda _seconds: None)
    busy = _BusyTemplateEngine(busy_attempts=2)

    acceptance_database._copy_migrated_template(
        busy, "shared_tmpl", "copy_name", error_cls=AcceptanceError
    )

    assert busy.attempts == 3

    exhausted = _BusyTemplateEngine(busy_attempts=1_000)
    with pytest.raises(AcceptanceError, match="stayed busy"):
        acceptance_database._copy_migrated_template(
            exhausted, "shared_tmpl", "copy_name", error_cls=AcceptanceError
        )
    assert exhausted.attempts == acceptance_database._TEMPLATE_COPY_ATTEMPTS

    failing = _BusyTemplateEngine(busy_attempts=0, error="permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        acceptance_database._copy_migrated_template(
            failing, "shared_tmpl", "copy_name", error_cls=AcceptanceError
        )
    assert failing.attempts == 1


class _BusyTemplateEngine:
    """An admin engine whose first ``busy_attempts`` copies find the template held."""

    def __init__(self, *, busy_attempts, error=None):
        self.attempts = 0
        self._busy_attempts = busy_attempts
        self._error = error

    def connect(self):
        self.attempts += 1
        busy = self.attempts <= self._busy_attempts
        return _BusyTemplateConnection(
            self._error
            or ("source database is being accessed by other users" if busy else None)
        )


class _BusyTemplateConnection:
    def __init__(self, error):
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _statement, _parameters=None):
        if self._error is not None:
            raise RuntimeError(self._error)
        return None


class _TemplateAdminEngine:
    def connect(self):
        return _TemplateAdminConnection()


class _TemplateAdminConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _statement, _parameters=None):
        return None


def test_disposable_database_is_dropped_when_migration_fails(monkeypatch):
    database_name = acceptance_database.disposable_database_name(DATABASE_LABEL)
    monkeypatch.setattr(
        acceptance_database,
        "disposable_database_name",
        lambda _label: database_name,
    )

    def fail_migration(*_args, **_kwargs):
        assert _database_exists(database_name)
        raise AcceptanceError("forced migration failure")

    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        fail_migration,
    )

    try:
        with pytest.raises(AcceptanceError, match="forced migration failure"):
            with acceptance_database.provision_disposable_postgres(
                settings.database_url,
                repo_root=REPO_ROOT,
                label=DATABASE_LABEL,
                reuse_migrated_template=False,
            ):
                raise AssertionError("migration failure must precede yield")
        assert not _database_exists(database_name)
    finally:
        _drop_database_if_present(database_name)


def test_migration_head_is_read_from_the_disposable_database(monkeypatch):
    database_name = acceptance_database.disposable_database_name(DATABASE_LABEL)
    seen_urls = []
    monkeypatch.setattr(
        acceptance_database,
        "disposable_database_name",
        lambda _label: database_name,
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda *_args, **_kwargs: None,
    )

    def read_head(database_url, **_kwargs):
        seen_urls.append(database_url)
        return "test-head"

    monkeypatch.setattr(acceptance_database, "read_migration_head", read_head)

    try:
        with acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=REPO_ROOT,
            label=DATABASE_LABEL,
            reuse_migrated_template=False,
        ) as provisioned:
            assert provisioned.name == database_name
            assert provisioned.migration_head == "test-head"
        assert [make_url(str(url)).database for url in seen_urls] == [database_name]
        assert not _database_exists(database_name)
    finally:
        _drop_database_if_present(database_name)


def test_disposable_provisioning_applies_the_production_database_guard(monkeypatch):
    database_name = acceptance_database.disposable_database_name(DATABASE_LABEL)
    seen = []
    monkeypatch.setattr(
        acceptance_database,
        "disposable_database_name",
        lambda _label: database_name,
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        acceptance_database,
        "read_migration_head",
        lambda *_args, **_kwargs: "test-head",
    )
    monkeypatch.setattr(
        acceptance_database,
        "require_experimental_database",
        lambda database_url, *, session=None: seen.append(
            (make_url(str(database_url)).database, session is not None)
        ),
    )

    try:
        with acceptance_database.provision_disposable_postgres(
            settings.database_url,
            repo_root=REPO_ROOT,
            label=DATABASE_LABEL,
            reuse_migrated_template=False,
        ) as provisioned:
            assert provisioned.name == database_name
        assert seen == [(database_name, True)]
    finally:
        _drop_database_if_present(database_name)


def test_guarded_upgrade_moves_only_the_named_disposable_database(monkeypatch):
    label = "sh99_coordinator"
    name = acceptance_database.disposable_database_name(label)
    provisioned = acceptance_database.ProvisionedDatabase(
        name=name,
        session_factory=sessionmaker(),
        postgres_version="16.9",
        migration_head="e255a7c4d9e2",
    )
    heads = iter(("e255a7c4d9e2", "f255b7c4d9e3"))
    seen = []

    monkeypatch.setattr(
        acceptance_database,
        "read_migration_head",
        lambda database_url, **_kwargs: (seen.append(("read", database_url)), next(heads))[1],
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda database_url, **kwargs: seen.append(
            ("upgrade", database_url.render_as_string(hide_password=False), kwargs)
        ),
    )

    receipt = acceptance_database.upgrade_provisioned_postgres(
        provisioned,
        admin_url="postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
        repo_root=REPO_ROOT,
        error_cls=AcceptanceError,
        label=label,
        expected_current_revision="e255a7c4d9e2",
        target_revision="f255b7c4d9e3",
    )

    assert receipt == acceptance_database.DatabaseUpgradeReceipt(
        database_name=name,
        from_revision="e255a7c4d9e2",
        to_revision="f255b7c4d9e3",
        verified_revision="f255b7c4d9e3",
    )
    assert all(make_url(item[1]).database == name for item in seen)
    assert seen[1][0] == "upgrade"
    assert seen[1][2]["revision"] == "f255b7c4d9e3"


def test_guarded_upgrade_rejects_a_database_outside_the_disposable_prefix(monkeypatch):
    provisioned = acceptance_database.ProvisionedDatabase(
        name="corridor",
        session_factory=sessionmaker(),
        postgres_version="16.9",
        migration_head="e255a7c4d9e2",
    )
    monkeypatch.setattr(
        acceptance_database,
        "apply_schema_migrations",
        lambda *_args, **_kwargs: pytest.fail("shared database migration was attempted"),
    )

    with pytest.raises(AcceptanceError, match="outside the disposable namespace"):
        acceptance_database.upgrade_provisioned_postgres(
            provisioned,
            admin_url=settings.database_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
            label="sh99_coordinator",
            expected_current_revision="e255a7c4d9e2",
            target_revision="f255b7c4d9e3",
        )


def test_read_migration_head_rejects_multiple_current_revisions(monkeypatch):
    monkeypatch.setattr(
        acceptance_database,
        "create_engine",
        lambda *_args, **_kwargs: _StampedEngine(("aaa111", "bbb222")),
    )

    with pytest.raises(AcceptanceError, match="exactly one Alembic revision"):
        acceptance_database.read_migration_head(
            settings.database_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
        )


def test_read_migration_head_returns_the_single_stamped_revision(monkeypatch):
    monkeypatch.setattr(
        acceptance_database,
        "create_engine",
        lambda *_args, **_kwargs: _StampedEngine(("aaa111",)),
    )

    assert (
        acceptance_database.read_migration_head(
            settings.database_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
        )
        == "aaa111"
    )


def test_read_migration_head_reports_an_unreadable_database(monkeypatch):
    monkeypatch.setattr(
        acceptance_database,
        "create_engine",
        lambda *_args, **_kwargs: _StampedEngine(None),
    )

    with pytest.raises(AcceptanceError, match="could not read Alembic head"):
        acceptance_database.read_migration_head(
            settings.database_url,
            repo_root=REPO_ROOT,
            error_cls=AcceptanceError,
        )


class _StampedConnection:
    """One connection reporting the rows an ``alembic_version`` table holds."""

    def __init__(self, revisions):
        self._revisions = revisions

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def scalars(self, _statement):
        if self._revisions is None:
            raise RuntimeError("relation \"alembic_version\" does not exist")
        return iter(self._revisions)


class _StampedEngine:
    def __init__(self, revisions):
        self._revisions = revisions
        self.disposed = False

    def connect(self):
        return _StampedConnection(self._revisions)

    def dispose(self):
        self.disposed = True


def test_bundle_verifier_rejects_unmanifested_nested_content(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    canonical = {"claim": "mechanical-only"}
    canonical_bytes = _canonical_json(canonical) + b"\n"
    (bundle_dir / "canonical-content.json").write_bytes(canonical_bytes)
    manifest = {
        "schema_version": "test-bundle-v1",
        "canonical_content_sha256": _json_sha256(canonical),
        "files": {
            "canonical-content.json": {
                "bytes": len(canonical_bytes),
                "sha256": _sha256(canonical_bytes),
            }
        },
    }
    manifest_bytes = _canonical_json(manifest) + b"\n"
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)

    assert verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=_sha256(manifest_bytes),
        bundle_schema_version="test-bundle-v1",
        bundle_files=("canonical-content.json",),
    ).valid

    nested = bundle_dir / "nested"
    nested.mkdir()
    (nested / "surprise.txt").write_text("unmanifested")

    with pytest.raises(CorruptBundle, match="unmanifested export"):
        verify_bundle(
            bundle_dir,
            expected_integrity_manifest_sha256=_sha256(manifest_bytes),
            bundle_schema_version="test-bundle-v1",
            bundle_files=("canonical-content.json",),
        )


def _database_exists(database_name: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.scalar(
                text("select exists(select 1 from pg_database where datname=:name)"),
                {"name": database_name},
            )
        )


def _drop_database_if_present(database_name: str) -> None:
    if not _database_exists(database_name):
        return
    admin_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname=:name and pid <> pg_backend_pid()"
                ),
                {"name": database_name},
            )
            connection.execute(text(f'drop database if exists "{database_name}"'))
    finally:
        admin_engine.dispose()


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value) -> str:
    return _sha256(_canonical_json(value))
