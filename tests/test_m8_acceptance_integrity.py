"""Failure cleanup and closed-world integrity for M8 acceptance artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

import corridor.m8_acceptance_database as acceptance_database
from corridor.config import settings
from corridor.db import engine
from corridor.m8_acceptance import AcceptanceError, CorruptAcceptanceBundle
from corridor.m8_acceptance_bundle import verify_bundle


REPO_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PREFIX = "corridor_m8_acceptance_"


def test_disposable_database_is_dropped_when_migration_fails(monkeypatch):
    database_name = f"{DATABASE_PREFIX}{uuid4().hex}"
    monkeypatch.setattr(
        acceptance_database,
        "_disposable_database_name",
        lambda _prefix: database_name,
    )

    def fail_migration(*_args, **_kwargs):
        assert _database_exists(database_name)
        raise AcceptanceError("forced migration failure")

    monkeypatch.setattr(
        acceptance_database,
        "_apply_schema_migrations",
        fail_migration,
    )

    try:
        with pytest.raises(AcceptanceError, match="forced migration failure"):
            with acceptance_database.provision_disposable_postgres(
                settings.database_url,
                repo_root=REPO_ROOT,
                error_cls=AcceptanceError,
                database_prefix=DATABASE_PREFIX,
            ):
                raise AssertionError("migration failure must precede yield")
        assert not _database_exists(database_name)
    finally:
        _drop_database_if_present(database_name)


def test_migration_head_is_read_from_the_disposable_database(monkeypatch):
    database_name = f"{DATABASE_PREFIX}{uuid4().hex}"
    seen_urls = []
    monkeypatch.setattr(
        acceptance_database,
        "_disposable_database_name",
        lambda _prefix: database_name,
    )
    monkeypatch.setattr(
        acceptance_database,
        "_apply_schema_migrations",
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
            error_cls=AcceptanceError,
            database_prefix=DATABASE_PREFIX,
        ) as provisioned:
            assert provisioned.name == database_name
            assert provisioned.migration_head == "test-head"
        assert [make_url(str(url)).database for url in seen_urls] == [database_name]
        assert not _database_exists(database_name)
    finally:
        _drop_database_if_present(database_name)


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
        corrupt_bundle_error_cls=CorruptAcceptanceBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    ).valid

    nested = bundle_dir / "nested"
    nested.mkdir()
    (nested / "surprise.txt").write_text("unmanifested")

    with pytest.raises(CorruptAcceptanceBundle, match="unmanifested export"):
        verify_bundle(
            bundle_dir,
            expected_integrity_manifest_sha256=_sha256(manifest_bytes),
            bundle_schema_version="test-bundle-v1",
            bundle_files=("canonical-content.json",),
            corrupt_bundle_error_cls=CorruptAcceptanceBundle,
            sha256=_sha256,
            json_sha256=_json_sha256,
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
