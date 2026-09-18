"""One contract, two backends: the filesystem store and an S3-compatible store.

Every case runs against both `LocalFilesystemStore` and `S3ObjectStore` (the
latter over moto's in-process S3), so a backend cannot pass by being the one
the developer happens to run. The render-subprocess isolation checks live
here too: the worker sees staged local bytes and nothing else.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import boto3
from moto import mock_aws
import pytest

from corridor.object_storage import (
    DeletionPermit,
    DigestMismatch,
    LocalFilesystemStore,
    ObjectConflict,
    ObjectMissing,
    S3ObjectStore,
    StorageError,
    content_key,
    parse_key,
)
from corridor.render_profiles import worker_environment


BODY = b"%PDF-1.7 exact source bytes"
DIGEST = sha256(BODY).hexdigest()
KEY = content_key(DIGEST, ".pdf")


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(params=["filesystem", "s3"])
def store(request, tmp_path):
    if request.param == "filesystem":
        yield LocalFilesystemStore(tmp_path / "store")
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = f"corridor-{uuid4().hex[:12]}"
        client.create_bucket(Bucket=bucket)
        yield S3ObjectStore(bucket=bucket, prefix="customer-a", client=client)


def _corrupt(store, key: str, replacement: bytes) -> None:
    """Change the stored bytes behind the store's back, as bit rot would."""

    if isinstance(store, LocalFilesystemStore):
        (store.root / key).write_bytes(replacement)
    else:
        store.client.put_object(Bucket=store.bucket, Key=store.prefix + key, Body=replacement)


def test_put_get_exists_and_the_key_layout(store):
    assert not store.exists(KEY)

    stored = store.put(KEY, BODY, sha256=DIGEST)

    assert stored.created is True
    assert stored.size == len(BODY)
    assert store.exists(KEY)
    assert store.get(KEY, sha256=DIGEST) == BODY
    assert store.resolve(DIGEST) == KEY
    assert list(store.list_keys()) == [KEY]
    assert parse_key(KEY) == (DIGEST, ".pdf")
    assert KEY == f"{DIGEST[:2]}/{DIGEST}.pdf"


def test_streaming_read_verifies_the_digest_at_the_end(store):
    store.put(KEY, BODY, sha256=DIGEST)

    assert b"".join(store.open(KEY, sha256=DIGEST)) == BODY

    _corrupt(store, KEY, b"%PDF-1.7 rotted bytes")
    with pytest.raises(DigestMismatch):
        list(store.open(KEY, sha256=DIGEST))
    with pytest.raises(DigestMismatch):
        store.get(KEY, sha256=DIGEST)


def test_write_verifies_the_digest_before_anything_is_stored(store):
    with pytest.raises(DigestMismatch):
        store.put(KEY, b"other bytes", sha256=DIGEST)

    assert not store.exists(KEY)


def test_put_if_absent_is_idempotent_for_identical_bytes(store):
    first = store.put(KEY, BODY, sha256=DIGEST)
    second = store.put(KEY, BODY, sha256=DIGEST)

    assert (first.created, second.created) == (True, False)
    assert store.get(KEY, sha256=DIGEST) == BODY


def test_an_existing_key_is_never_overwritten_with_different_bytes(store):
    store.put(KEY, BODY, sha256=DIGEST)
    _corrupt(store, KEY, b"%PDF-1.7 rotted bytes")

    with pytest.raises(ObjectConflict):
        store.put(KEY, BODY, sha256=DIGEST)

    assert b"".join(store._chunks(KEY) if hasattr(store, "_chunks") else [
        (store.root / KEY).read_bytes()
    ]) == b"%PDF-1.7 rotted bytes"


def test_put_file_and_stage_round_trip_through_a_local_path(store, tmp_path):
    source = tmp_path / "staging" / "render.png"
    source.parent.mkdir()
    source.write_bytes(BODY)
    key = content_key(DIGEST, ".png")

    stored = store.put_file(key, source, sha256=DIGEST)
    staged = store.stage(key, tmp_path / "elsewhere" / "render.png", sha256=DIGEST)

    assert stored.created is True
    assert staged.read_bytes() == BODY
    # Staging is idempotent: an existing destination is returned untouched.
    assert store.stage(key, staged, sha256=DIGEST) == staged
    with pytest.raises(DigestMismatch):
        store.put_file(key, source, sha256="0" * 64)


def test_stage_refuses_a_corrupted_object_and_leaves_no_partial_file(store, tmp_path):
    store.put(KEY, BODY, sha256=DIGEST)
    _corrupt(store, KEY, b"%PDF-1.7 rotted bytes")
    destination = tmp_path / "staged" / "source.pdf"

    with pytest.raises(DigestMismatch):
        store.stage(KEY, destination, sha256=DIGEST)

    assert not destination.exists()
    assert not destination.parent.is_dir() or list(destination.parent.iterdir()) == []


def test_the_probe_answers_for_a_provisioned_store(store):
    store.put(KEY, BODY, sha256=DIGEST)

    assert store.probe() is None


def test_the_probe_refuses_a_store_that_was_never_provisioned(tmp_path):
    """A missing root and a missing bucket are unreachable, not empty (#491A)."""

    with pytest.raises(StorageError):
        LocalFilesystemStore(tmp_path / "never-created").probe()

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        with pytest.raises(StorageError):
            S3ObjectStore(bucket="corridor-never-created", client=client).probe()


def test_missing_objects_are_reported_not_invented(store):
    with pytest.raises(ObjectMissing):
        store.get(KEY, sha256=DIGEST)
    assert store.resolve(DIGEST) is None
    with pytest.raises(ValueError):
        store.put("not/a-content-key", BODY, sha256=DIGEST)


def test_delete_under_policy_needs_a_matching_permit_and_unchanged_bytes(store):
    store.put(KEY, BODY, sha256=DIGEST)

    with pytest.raises(StorageError):
        store.delete_under_policy(KEY, permit=None)
    other = DeletionPermit(key=content_key("f" * 64, ".pdf"), sha256=DIGEST, issued_by="t")
    with pytest.raises(StorageError):
        store.delete_under_policy(KEY, permit=other)
    assert store.exists(KEY)

    stale = DeletionPermit(key=KEY, sha256="0" * 64, issued_by="t")
    with pytest.raises(ObjectConflict):
        store.delete_under_policy(KEY, permit=stale)
    assert store.exists(KEY)

    store.delete_under_policy(
        KEY, permit=DeletionPermit(key=KEY, sha256=DIGEST, issued_by="t")
    )
    assert not store.exists(KEY)
    with pytest.raises(ObjectMissing):
        store.delete_under_policy(
            KEY, permit=DeletionPermit(key=KEY, sha256=DIGEST, issued_by="t")
        )


def test_the_render_worker_environment_carries_no_database_or_storage_access():
    inherited = {
        "PATH": "/usr/bin",
        "HOME": "/home/worker",
        "DATABASE_URL": "postgresql+psycopg://owner@db/corridor",
        "WORKER_DATABASE_URL": "postgresql+psycopg://worker@db/corridor",
        "CORRIDOR_WORKER_DB_PASSWORD": "secret",
        "CORRIDOR_STORAGE_BACKEND": "s3",
        "CORRIDOR_S3_BUCKET": "customer-a",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "token",
        "PGPASSWORD": "secret",
        "PGHOST": "db",
    }

    assert worker_environment(inherited) == {"PATH": "/usr/bin", "HOME": "/home/worker"}


def test_the_render_worker_reads_staged_bytes_and_writes_a_staging_directory():
    worker = Path(__file__).parents[1] / "workers" / "render" / "render_worker.py"
    source = worker.read_text(encoding="utf-8")

    assert '"pdf_path"' in source and '"output_dir"' in source
    for forbidden in ("corridor", "sqlalchemy", "psycopg", "boto3", "botocore"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def test_cached_stage_must_still_match_the_requested_digest(store, tmp_path):
    store.put(KEY, BODY, sha256=DIGEST)
    destination = tmp_path / "cached.pdf"
    destination.write_bytes(b"unrelated bytes")
    with pytest.raises(DigestMismatch):
        store.stage(KEY, destination, sha256=DIGEST)
    assert destination.read_bytes() == b"unrelated bytes"


def test_staging_and_input_reuse_cannot_mutate_the_stored_object(store, tmp_path):
    source = tmp_path / "worker.png"
    source.write_bytes(BODY)
    store.put_file(KEY, source, sha256=DIGEST)
    source.write_bytes(b"next render")
    assert store.get(KEY, sha256=DIGEST) == BODY
    destination = store.stage(KEY, tmp_path / "reader.pdf", sha256=DIGEST)
    destination.write_bytes(b"reader changed its copy")
    assert store.get(KEY, sha256=DIGEST) == BODY


def test_extensionless_objects_resolve_on_every_backend(store):
    key = content_key(DIGEST)
    store.put(key, BODY, sha256=DIGEST)
    assert store.resolve(DIGEST) == key


def test_local_staging_the_object_itself_verifies_its_bytes(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    store.put(KEY, BODY, sha256=DIGEST)
    (tmp_path / KEY).write_bytes(b"corrupted")
    with pytest.raises(DigestMismatch):
        store.stage(KEY, tmp_path / KEY, sha256=DIGEST)


def test_a_competing_stage_destination_is_verified_before_return(store, tmp_path, monkeypatch):
    import os
    store.put(KEY, BODY, sha256=DIGEST)
    destination = tmp_path / "competing.pdf"
    link = os.link
    def competing_link(source, target, **kwargs):
        if Path(target) == destination:
            destination.write_bytes(b"competing invalid bytes")
        return link(source, target, **kwargs)
    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(DigestMismatch):
        store.stage(KEY, destination, sha256=DIGEST)
    assert destination.read_bytes() == b"competing invalid bytes"


@pytest.mark.parametrize("store", ["s3"], indirect=True)
def test_s3_upload_owns_verified_bytes_before_the_sdk_consumes_them(store, tmp_path):
    source = tmp_path / 'upload.pdf'
    source.write_bytes(BODY)
    def reuse_input(**kwargs):
        source.write_bytes(b'caller reused its staging file')
    store.client.meta.events.register('before-parameter-build.s3.PutObject', reuse_input)
    stored = store.put_file(KEY, source, sha256=DIGEST)
    assert stored.size == len(BODY)
    assert store.get(KEY, sha256=DIGEST) == BODY
