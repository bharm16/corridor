"""One content-addressed storage interface behind every artifact write and read.

Registered source files, page renders, token layers, and raw-OCR receipts were
each written straight to a local path by whichever module produced them: the
email path wrote raw ``.eml`` bytes under ``settings.corpus_store``, intake
staged uploads there, the render worker wrote PNGs under ``corpus_images``, and
retention unlinked files by path. ADR-0079 makes the backend an interface with
the local filesystem as the development implementation and object storage as
the deployed one; ADR-0080 makes ``delete_under_policy`` the only removal path.

Every key keeps the store's SHA-256 layout, ``<sha[:2]>/<sha><suffix>``, so a
PostgreSQL manifest that records a digest resolves the same object on either
backend and the filesystem backend's root is byte-for-byte the store that
existed before this module. Writes are conditional and idempotent: a put of
bytes already present is a no-op, and a put of different bytes under an
existing key is refused rather than overwritten. Every write and read verifies
the digest.

The isolated render subprocess never sees this module. It reads locally staged
input bytes and writes to a local staging directory; the parent stages inputs
with ``stage`` and persists outputs with ``put_file``. That ordering is what
makes a crash survivable: the object is written before the manifest row that
references it, so a crash between the two leaves an unreferenced object, never
a row without its bytes (``storage_operations.reconcile`` reports and repairs
both directions).

Staging verifies cache hits as well as downloads. Files handed to the store or
staged elsewhere are copied onto separately owned inodes: hard-linking a
worker's output let its next write mutate an already retained object.

A ``DeletionPermit`` is constructed only by ``corridor.retention`` after the
hold check, which is how a held object cannot be deleted on either backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256 as _sha256
import os
from pathlib import Path
import re
import shutil
from typing import Iterator, Protocol
from uuid import uuid4

from corridor import digests
from corridor.config import settings


CHUNK_BYTES = 1024 * 1024
_KEY = re.compile(r"^(?P<shard>[0-9a-f]{2})/(?P<sha256>[0-9a-f]{64})(?P<suffix>\.[^/]+)?$")


class StorageError(RuntimeError):
    """The store refused a request; the message says why."""


class DigestMismatch(StorageError):
    """Bytes did not hash to the digest they were written or read under."""


class ObjectConflict(StorageError):
    """The key already holds different bytes, or its bytes moved under a permit."""


class ObjectMissing(StorageError):
    """The key holds no object."""


class DeletionUncertain(StorageError):
    """A delete could not be confirmed: no acknowledgement, or it timed out.

    The object may or may not be gone (#956). A caller records the outcome as
    uncertain until reconciled rather than treating it as either a completed
    deletion or a rolled-back no-op, because a database rollback does not
    restore an object the store may already have removed.
    """


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size: int
    created: bool


@dataclass(frozen=True)
class DeletionPermit:
    """Authority to remove exactly one object with exactly these bytes.

    Issued only by ``corridor.retention`` once the hold check has passed
    (``tests/test_architecture.py`` enforces the construction site). The store
    refuses the deletion if the object's bytes no longer match ``sha256``.
    """

    key: str
    sha256: str
    issued_by: str


def content_key(sha256: str, suffix: str = "") -> str:
    """The one key layout: two-character shard, full digest, preserved suffix."""

    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("a content key needs a lowercase hex SHA-256 digest")
    if suffix and (not suffix.startswith(".") or "/" in suffix):
        raise ValueError("a content key suffix is a bare extension such as '.pdf'")
    return f"{sha256[:2]}/{sha256}{suffix}"


def parse_key(key: str) -> tuple[str, str] | None:
    """``(sha256, suffix)`` for a key in the store layout, else ``None``."""

    match = _KEY.match(key)
    if match is None:
        return None
    return match.group("sha256"), match.group("suffix") or ""


digest_bytes = digests.sha256_bytes
digest_file = digests.sha256_file


def local_staging_path(key: str) -> Path:
    """Where the parent process stages an object for local readers and workers.

    With the filesystem backend this is the object itself; with the object
    store it is the local cache the store fills on demand.
    """

    return Path(settings.corpus_store) / key


class ObjectStore(Protocol):
    backend: str

    def put(self, key: str, data: bytes, *, sha256: str) -> StoredObject: ...

    def put_file(self, key: str, path: Path, *, sha256: str) -> StoredObject: ...

    def get(self, key: str, *, sha256: str) -> bytes: ...

    def open(self, key: str, *, sha256: str) -> Iterator[bytes]: ...

    def exists(self, key: str) -> bool: ...

    def resolve(self, sha256: str) -> str | None: ...

    def list_keys(self, prefix: str = "") -> Iterator[str]: ...

    def stage(self, key: str, destination: Path, *, sha256: str) -> Path: ...

    def delete_under_policy(self, key: str, *, permit: DeletionPermit) -> None: ...

    # Prove the store answers, without reading or writing an object. Each
    # backend knows what unreachable means for it: a missing directory is not
    # an empty listing, and a missing bucket is not a missing key.
    def probe(self) -> None: ...


def _verified(data: bytes, sha256: str) -> None:
    if digest_bytes(data) != sha256:
        raise DigestMismatch("bytes do not hash to the digest they were written under")


def _verified_file(path: Path, sha256: str) -> None:
    if digest_file(path) != sha256:
        raise DigestMismatch(f"{path} does not hash to {sha256}")


def _verified_stream(chunks: Iterator[bytes], sha256: str) -> Iterator[bytes]:
    digest = _sha256()
    for chunk in chunks:
        digest.update(chunk)
        yield chunk
    if digest.hexdigest() != sha256:
        raise DigestMismatch("streamed bytes do not hash to the recorded digest")


class LocalFilesystemStore:
    """The store as it existed before the interface: ``root/<shard>/<sha><suffix>``."""

    backend = "filesystem"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if parse_key(key) is None:
            raise ValueError(f"{key!r} is not a content key")
        return self.root / key

    def put(self, key: str, data: bytes, *, sha256: str) -> StoredObject:
        _verified(data, sha256)
        target = self._path(key)
        if target.exists():
            return self._existing(key, target, sha256, len(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid4().hex}.part"
        staging.write_bytes(data)
        try:
            os.link(staging, target)
        except FileExistsError:
            return self._existing(key, target, sha256, len(data))
        finally:
            staging.unlink(missing_ok=True)
        return StoredObject(key, sha256, len(data), True)

    def put_file(self, key: str, path: Path, *, sha256: str) -> StoredObject:
        source = Path(path)
        _verified_file(source, sha256)
        target = self._path(key)
        size = source.stat().st_size
        if target.exists():
            return self._existing(key, target, sha256, size)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.{uuid4().hex}.part"
        try:
            shutil.copyfile(source, staging)
            # Verify the copied bytes, not just the earlier source reading.
            _verified_file(staging, sha256)
            size = staging.stat().st_size
            try:
                os.link(staging, target)
            except FileExistsError:
                return self._existing(key, target, sha256, size)
        finally:
            staging.unlink(missing_ok=True)
        return StoredObject(key, sha256, size, True)

    def _existing(self, key: str, target: Path, sha256: str, size: int) -> StoredObject:
        if digest_file(target) != sha256:
            raise ObjectConflict(f"{key} already holds different bytes")
        return StoredObject(key, sha256, size, False)

    def get(self, key: str, *, sha256: str) -> bytes:
        return b"".join(self.open(key, sha256=sha256))

    def open(self, key: str, *, sha256: str) -> Iterator[bytes]:
        target = self._path(key)
        if not target.is_file():
            raise ObjectMissing(f"{key} holds no object")

        def chunks() -> Iterator[bytes]:
            with target.open("rb") as handle:
                yield from iter(lambda: handle.read(CHUNK_BYTES), b"")

        return _verified_stream(chunks(), sha256)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def resolve(self, sha256: str) -> str | None:
        shard = self.root / sha256[:2]
        found = sorted(
            path.name for path in shard.glob(f"{sha256}*")
            if path.is_file() and parse_key(f"{sha256[:2]}/{path.name}")
        )
        return f"{sha256[:2]}/{found[0]}" if found else None

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        if not self.root.is_dir():
            return
        for shard in sorted(self.root.iterdir()):
            if not shard.is_dir():
                continue
            for path in sorted(shard.iterdir()):
                key = f"{shard.name}/{path.name}"
                if path.is_file() and parse_key(key) and key.startswith(prefix):
                    yield key

    def stage(self, key: str, destination: Path, *, sha256: str) -> Path:
        destination = Path(destination)
        target = self._path(key)
        if destination.exists():
            _verified_file(destination, sha256)
            return destination
        if not target.is_file():
            raise ObjectMissing(f"{key} holds no object")
        _download(self.open(key, sha256=sha256), destination, sha256=sha256)
        return destination

    def delete_under_policy(self, key: str, *, permit: DeletionPermit) -> None:
        _check_permit(key, permit)
        target = self._path(key)
        if not target.is_file():
            raise ObjectMissing(f"{key} holds no object")
        if digest_file(target) != permit.sha256:
            raise ObjectConflict(f"{key} no longer holds the permitted bytes")
        target.unlink()

    def probe(self) -> None:
        """The root directory is the store; a missing one is not an empty one."""

        if not self.root.is_dir():
            raise StorageError(f"{self.root} is not a readable store root")


class S3ObjectStore:
    """An S3-compatible bucket; keys keep the same layout under an optional prefix.

    Credentials come from the standard AWS environment or instance role, never
    from Corridor settings, so the render subprocess can be denied them by
    scrubbing its environment. A put is conditional (``If-None-Match: *``) so
    two writers of the same key cannot race past each other.
    """

    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client=None,
    ) -> None:
        if not bucket:
            raise ValueError("the s3 storage backend needs a bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region_name or None,
                config=Config(retries={"max_attempts": 5, "mode": "standard"}),
            )
        self.client = client

    def _name(self, key: str) -> str:
        if parse_key(key) is None:
            raise ValueError(f"{key!r} is not a content key")
        return self.prefix + key

    def put(self, key: str, data: bytes, *, sha256: str) -> StoredObject:
        _verified(data, sha256)
        return self._put_body(key, data, sha256, len(data))

    def put_file(self, key: str, path: Path, *, sha256: str) -> StoredObject:
        source = Path(path)
        _verified_file(source, sha256)
        with source.open("rb") as handle:
            return self._put_body(key, handle, sha256, source.stat().st_size)

    def _put_body(self, key: str, body, sha256: str, size: int) -> StoredObject:
        from botocore.exceptions import ClientError

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._name(key),
                Body=body,
                IfNoneMatch="*",
                Metadata={"sha256": sha256},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "ConditionalRequestConflict", "412"}:
                raise
            if self._digest(key) != sha256:
                raise ObjectConflict(f"{key} already holds different bytes") from exc
            return StoredObject(key, sha256, size, False)
        return StoredObject(key, sha256, size, True)

    def _digest(self, key: str) -> str | None:
        """The digest of the stored bytes themselves, not the recorded metadata."""

        try:
            chunks = self._chunks(key)
        except ObjectMissing:
            return None
        digest = _sha256()
        for chunk in chunks:
            digest.update(chunk)
        return digest.hexdigest()

    def _chunks(self, key: str) -> Iterator[bytes]:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._name(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise ObjectMissing(f"{key} holds no object") from exc
            raise
        return response["Body"].iter_chunks(CHUNK_BYTES)

    def get(self, key: str, *, sha256: str) -> bytes:
        return b"".join(self.open(key, sha256=sha256))

    def open(self, key: str, *, sha256: str) -> Iterator[bytes]:
        return _verified_stream(self._chunks(key), sha256)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._name(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def resolve(self, sha256: str) -> str | None:
        found = sorted(self.list_keys(f"{sha256[:2]}/{sha256}"))
        return found[0] if found else None

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for item in page.get("Contents", []):
                key = item["Key"].removeprefix(self.prefix)
                if parse_key(key):
                    yield key

    def stage(self, key: str, destination: Path, *, sha256: str) -> Path:
        destination = Path(destination)
        self._name(key)
        if destination.exists():
            _verified_file(destination, sha256)
            return destination
        _download(self.open(key, sha256=sha256), destination, sha256=sha256)
        return destination

    def delete_under_policy(self, key: str, *, permit: DeletionPermit) -> None:
        from botocore.exceptions import (
            ConnectionError as BotoConnectionError,
            ReadTimeoutError,
        )

        _check_permit(key, permit)
        current = self._digest(key)
        if current is None:
            raise ObjectMissing(f"{key} holds no object")
        if current != permit.sha256:
            raise ObjectConflict(f"{key} no longer holds the permitted bytes")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._name(key))
        except (ReadTimeoutError, BotoConnectionError) as exc:
            # The request went out but no acknowledgement came back: the object
            # may or may not be gone. Record it as uncertain (#956), never as a
            # confirmed deletion the reconciler need not revisit.
            raise DeletionUncertain(
                f"{key} deletion was not acknowledged; the outcome is uncertain"
            ) from exc

    def probe(self) -> None:
        """One bounded list: it fails on a missing bucket or a denied credential."""

        try:
            self.client.list_objects_v2(
                Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1
            )
        except Exception as exc:
            raise StorageError(f"bucket {self.bucket} is not reachable") from exc


def _check_permit(key: str, permit: DeletionPermit) -> None:
    if not isinstance(permit, DeletionPermit):
        raise StorageError("deletion requires a permit issued by retention")
    if permit.key != key:
        raise StorageError(f"the permit names {permit.key}, not {key}")


def _download(chunks: Iterator[bytes], destination: Path, *, sha256: str) -> None:
    """Publish verified bytes once, or verify a concurrent winner's bytes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid4().hex}.part"
    try:
        with staging.open("wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
        try:
            os.link(staging, destination)
        except FileExistsError:
            _verified_file(destination, sha256)
    finally:
        staging.unlink(missing_ok=True)


def default_storage_root() -> Path:
    """The default filesystem root used when migrating or checking local content."""

    return Path(settings.corpus_store)


def content_store() -> ObjectStore:
    """The deployment's store, from settings; the filesystem backend by default."""

    return _store(
        settings.storage_backend,
        settings.corpus_store,
        settings.storage_s3_bucket,
        settings.storage_s3_prefix,
        settings.storage_s3_endpoint_url,
        settings.storage_s3_region,
    )


@lru_cache(maxsize=8)
def _store(
    backend: str, root: str, bucket: str, prefix: str, endpoint_url: str, region: str
) -> ObjectStore:
    if backend == "filesystem":
        return LocalFilesystemStore(root)
    if backend == "s3":
        return S3ObjectStore(
            bucket=bucket, prefix=prefix, endpoint_url=endpoint_url, region_name=region
        )
    raise ValueError(
        f"unknown storage backend {backend!r}; CORRIDOR_STORAGE_BACKEND is "
        "'filesystem' or 's3'"
    )


def store_bytes(data: bytes, *, sha256: str, suffix: str) -> Path:
    """Persist source bytes and return their locally staged path.

    Intake, email, and the corpus fetcher all need both: the durable object
    and a local path the parser reads. The put happens first, so a crash
    before staging leaves the object, never a path without one.
    """

    key = content_key(sha256, suffix)
    store = content_store()
    store.put(key, data, sha256=sha256)
    return store.stage(key, local_staging_path(key), sha256=sha256)
