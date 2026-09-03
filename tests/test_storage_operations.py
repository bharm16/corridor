"""Manifests and the store agree, on both backends, or the disagreement is named.

Registration writes the object before the row, retention deletes only under a
permit the hold check issues, migration is idempotent and digest-verified,
and reconciliation reports and repairs orphans in both directions. Every case
runs against the filesystem backend and the S3-compatible backend through the
same `content_store()` the application uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import boto3
from moto import mock_aws
import pytest
from sqlalchemy import select

from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import ProcessingArtifact, Project
from corridor.object_storage import (
    ObjectConflict,
    content_key,
    content_store,
    store_bytes,
)
from corridor.principals import HumanPrincipal
from corridor.retention import (
    RetentionRefused,
    execute_retention,
    lift_hold,
    permit_deletion,
    place_hold,
    plan_retention,
    register_processing_artifact,
)
from corridor.storage import staged_file
from corridor.storage_operations import migrate_local_content, reconcile


ACTOR = HumanPrincipal("local:storage-operator")
AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(params=["filesystem", "s3"])
def backend(request, tmp_path, monkeypatch):
    """Point `content_store()` at a fresh backend; corpus_store stays local staging."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "staging"))
    if request.param == "filesystem":
        monkeypatch.setattr(settings, "storage_backend", "filesystem")
        yield "filesystem"
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        bucket = f"corridor-{uuid4().hex[:12]}"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        monkeypatch.setattr(settings, "storage_backend", "s3")
        monkeypatch.setattr(settings, "storage_s3_bucket", bucket)
        monkeypatch.setattr(settings, "storage_s3_prefix", "customer-a")
        yield "s3"


@pytest.fixture
def project(session):
    row = Project(slug=f"storage-{uuid4().hex[:8]}", name="Storage", is_synthetic=True)
    session.add(row)
    session.flush()
    return row


def _render(tmp_path: Path, name: str = "page-1.png", body: bytes = b"rendered page") -> Path:
    path = tmp_path / "renders" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _lose(store, key: str) -> None:
    """Remove the object out of band, as a wiped volume or bucket would."""

    if store.backend == "filesystem":
        (store.root / key).unlink()
    else:
        store.client.delete_object(Bucket=store.bucket, Key=store.prefix + key)


def test_registration_persists_the_object_before_the_manifest_row(
    session, project, tmp_path, backend
):
    render = _render(tmp_path)
    digest = sha256(render.read_bytes()).hexdigest()
    store = content_store()
    assert store.backend == backend

    artifact = register_processing_artifact(
        session, project_id=project.id, kind="page_render", path=render, terminal_at=AS_OF
    )

    assert store.get(content_key(digest, ".png"), sha256=digest) == b"rendered page"
    assert artifact.content_sha256 == digest
    assert artifact.storage_path == str(render)


def test_a_failed_object_write_leaves_no_manifest_row(
    session, project, tmp_path, backend, monkeypatch
):
    render = _render(tmp_path)
    store = content_store()

    def refuse(*_args, **_kwargs):
        raise ObjectConflict("simulated backend failure")

    monkeypatch.setattr(type(store), "put_file", refuse)
    with pytest.raises(ObjectConflict):
        register_processing_artifact(
            session, project_id=project.id, kind="page_render", path=render, terminal_at=AS_OF
        )

    assert session.scalars(select(ProcessingArtifact)).all() == []


def test_source_bytes_are_staged_locally_from_the_store(tmp_path, backend):
    body = b"%PDF-1.7 source"
    digest = sha256(body).hexdigest()

    staged = store_bytes(body, sha256=digest, suffix=".pdf")

    assert staged == Path(settings.corpus_store) / digest[:2] / f"{digest}.pdf"
    assert staged.read_bytes() == body
    if backend == "s3":
        # A wiped staging directory refills from the object store on the next read.
        staged.unlink()
        refilled = staged_file(digest)
        assert refilled == staged and refilled.read_bytes() == body


def test_retention_deletes_through_the_store_identically(
    session, project, tmp_path, backend
):
    render = _render(tmp_path)
    digest = sha256(render.read_bytes()).hexdigest()
    key = content_key(digest, ".png")
    store = content_store()
    register_processing_artifact(
        session,
        project_id=project.id,
        kind="page_render",
        path=render,
        terminal_at=AS_OF - timedelta(days=31),
    )

    manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    execute_retention(
        session, manifest_id=manifest.id, expected_sha256=manifest.content_sha256, executed_at=AS_OF
    )

    assert not store.exists(key)
    assert not render.exists()


def test_a_held_object_cannot_be_deleted_on_either_backend(
    session, project, tmp_path, backend
):
    render = _render(tmp_path)
    digest = sha256(render.read_bytes()).hexdigest()
    key = content_key(digest, ".png")
    store = content_store()
    register_processing_artifact(
        session,
        project_id=project.id,
        kind="page_render",
        path=render,
        terminal_at=AS_OF - timedelta(days=31),
    )
    manifest = plan_retention(session, as_of=AS_OF, principal=ACTOR)
    hold = place_hold(session, project_id=project.id, reason="litigation", principal=ACTOR)

    with pytest.raises(RetentionRefused):
        permit_deletion(
            session, project_id=project.id, key=key, sha256=digest, issued_by="test"
        )
    with pytest.raises(RetentionRefused):
        execute_retention(
            session,
            manifest_id=manifest.id,
            expected_sha256=manifest.content_sha256,
            executed_at=AS_OF,
        )
    assert store.exists(key) and render.exists()

    lift_hold(session, hold_id=hold.id, principal=ACTOR)
    execute_retention(
        session, manifest_id=manifest.id, expected_sha256=manifest.content_sha256, executed_at=AS_OF
    )
    assert not store.exists(key)


def test_migration_is_idempotent_and_refuses_bytes_that_do_not_verify(
    session, tmp_path, backend
):
    old_root = tmp_path / "old-store"
    good = b"%PDF-1.7 migrated"
    good_digest = sha256(good).hexdigest()
    (old_root / good_digest[:2]).mkdir(parents=True)
    (old_root / good_digest[:2] / f"{good_digest}.pdf").write_bytes(good)
    liar = "e" * 64
    (old_root / liar[:2]).mkdir(parents=True, exist_ok=True)
    (old_root / liar[:2] / f"{liar}.pdf").write_bytes(b"not the bytes the name claims")
    store = content_store()

    first = migrate_local_content(store, source_root=old_root, session=session)
    second = migrate_local_content(store, source_root=old_root, session=session)

    good_key = content_key(good_digest, ".pdf")
    assert first.as_dict() == {
        "backend": backend,
        "migrated": [good_key],
        "already_present": [],
        "mismatched": [content_key(liar, ".pdf")],
        "conflicting": [],
    }
    assert second.migrated == [] and second.already_present == [good_key]
    assert store.get(good_key, sha256=good_digest) == good
    assert not store.exists(content_key(liar, ".pdf"))


def test_reconciliation_reports_and_repairs_orphans_in_both_directions(
    session, project, tmp_path, backend
):
    render = _render(tmp_path)
    digest = sha256(render.read_bytes()).hexdigest()
    key = content_key(digest, ".png")
    store = content_store()
    register_processing_artifact(
        session, project_id=project.id, kind="page_render", path=render, terminal_at=AS_OF
    )
    stray = b"object written before its row, then the process died"
    stray_digest = sha256(stray).hexdigest()
    stray_key = content_key(stray_digest, ".json")
    store.put(stray_key, stray, sha256=stray_digest)
    _lose(store, key)

    report = reconcile(session, store)
    assert report.missing == [digest]
    assert report.unreferenced == [stray_key]
    assert not report.consistent

    repaired = reconcile(session, store, repair=True)
    assert repaired.restored == [key]
    assert store.get(key, sha256=digest) == b"rendered page"

    hold = place_hold(session, project_id=project.id, reason="audit", principal=ACTOR)
    refused = reconcile(session, store, remove_unreferenced=True)
    assert refused.refused == [stray_key] and store.exists(stray_key)

    lift_hold(session, hold_id=hold.id, principal=ACTOR)
    removed = reconcile(session, store, remove_unreferenced=True)
    assert removed.removed == [stray_key]
    assert reconcile(session, store).consistent
