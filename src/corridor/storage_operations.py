"""Move existing local content into the store and reconcile it with the manifests.

Before ADR-0079 the content-addressed store was a directory, so "is every
artifact where its manifest row says" was answered by ``Path.is_file``. With
the backend behind an interface, two operational passes replace that glance.

Migration walks the local layout (``<sha[:2]>/<sha><suffix>`` under the old
store root, plus every registered artifact's staged file) and puts each file
under its own digest. It is idempotent and digest-verified: a file whose bytes
do not hash to the name it carries is reported, never uploaded under a false
digest, and a second run finds everything already present.

Reconciliation derives the expected object set from the PostgreSQL manifests
(Documents, live processing artifacts, page-render derivatives, token layers)
and compares it with the store's listing. A manifest row whose object is
missing is repaired from a verified local staged copy when one exists; an
object no row references is reported and, when asked, removed under a permit
that any active hold refuses. No reconciliation state is stored: the report is
derived from the tables and the listing each time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Document,
    PageRenderDerivative,
    ProcessingArtifact,
    TokenLayerManifest,
)
from corridor.object_storage import (
    DigestMismatch,
    ObjectConflict,
    ObjectStore,
    content_key,
    digest_file,
    local_staging_path,
    parse_key,
)
from corridor.retention import RetentionRefused, permit_unreferenced_deletion


@dataclass
class MigrationReport:
    backend: str
    migrated: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "migrated": sorted(self.migrated),
            "already_present": sorted(self.already_present),
            "mismatched": sorted(self.mismatched),
            "conflicting": sorted(self.conflicting),
        }


def migrate_local_content(
    store: ObjectStore,
    *,
    source_root: Path | str,
    session: Session | None = None,
) -> MigrationReport:
    """Put every local file under its digest; report what did not verify."""

    root = Path(source_root)
    report = MigrationReport(backend=store.backend)
    seen: set[str] = set()
    candidates: list[tuple[str, Path]] = []
    if root.is_dir():
        for shard in sorted(root.iterdir()):
            if not shard.is_dir():
                continue
            for path in sorted(shard.iterdir()):
                key = f"{shard.name}/{path.name}"
                if path.is_file() and parse_key(key):
                    candidates.append((key, path))
    if session is not None:
        for artifact in session.scalars(
            select(ProcessingArtifact).where(ProcessingArtifact.deleted_at.is_(None))
        ):
            path = Path(artifact.storage_path)
            if path.is_file():
                candidates.append(
                    (content_key(artifact.content_sha256, path.suffix), path)
                )
    for key, path in candidates:
        if key in seen:
            continue
        seen.add(key)
        parsed = parse_key(key)
        assert parsed is not None
        sha256, _suffix = parsed
        if digest_file(path) != sha256:
            report.mismatched.append(key)
            continue
        try:
            stored = store.put_file(key, path, sha256=sha256)
        except ObjectConflict:
            report.conflicting.append(key)
            continue
        (report.migrated if stored.created else report.already_present).append(key)
    return report


@dataclass
class ReconciliationReport:
    backend: str
    referenced: int = 0
    present: int = 0
    missing: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unrepairable: list[str] = field(default_factory=list)
    unreferenced: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "referenced": self.referenced,
            "present": self.present,
            "missing": sorted(self.missing),
            "restored": sorted(self.restored),
            "unrepairable": sorted(self.unrepairable),
            "unreferenced": sorted(self.unreferenced),
            "removed": sorted(self.removed),
            "refused": sorted(self.refused),
        }

    @property
    def consistent(self) -> bool:
        return not (self.missing or self.unreferenced)


def referenced_digests(session: Session) -> dict[str, list[Path]]:
    """Every digest a manifest row promises, with the local paths that may hold it."""

    expected: dict[str, list[Path]] = {}

    def promise(sha256: str | None, path: str | None) -> None:
        if not sha256:
            return
        paths = expected.setdefault(sha256, [])
        if path and Path(path) not in paths:
            paths.append(Path(path))

    for sha256 in session.scalars(select(Document.sha256)):
        promise(sha256, None)
    for artifact in session.scalars(
        select(ProcessingArtifact).where(ProcessingArtifact.deleted_at.is_(None))
    ):
        promise(artifact.content_sha256, artifact.storage_path)
    for derivative in session.scalars(select(PageRenderDerivative)):
        promise(derivative.artifact_sha256, derivative.artifact_path)
    for layer in session.scalars(select(TokenLayerManifest)):
        promise(layer.artifact_sha256, layer.artifact_path)
    return expected


def reconcile(
    session: Session,
    store: ObjectStore,
    *,
    repair: bool = False,
    remove_unreferenced: bool = False,
    issued_by: str = "storage_reconciliation",
) -> ReconciliationReport:
    """Compare manifests with the store; optionally repair both directions.

    Objects are written before the rows that reference them, so the expected
    orphan after a crash is an unreferenced object. A missing object means
    the store lost bytes out of band; it is restored from a verified staged
    copy when one exists. Removing unreferenced objects is opt-in because a
    source staged for intake is legitimately unreferenced until confirmed.
    """

    report = ReconciliationReport(backend=store.backend)
    expected = referenced_digests(session)
    report.referenced = len(expected)
    stored: dict[str, str] = {}
    for key in store.list_keys():
        parsed = parse_key(key)
        if parsed is not None:
            stored.setdefault(parsed[0], key)
    report.present = sum(1 for sha256 in expected if sha256 in stored)

    for sha256 in sorted(expected):
        if sha256 in stored:
            continue
        report.missing.append(sha256)
        if not repair:
            continue
        for candidate in _local_candidates(sha256, expected[sha256]):
            if digest_file(candidate) != sha256:
                continue
            key = content_key(sha256, candidate.suffix)
            try:
                store.put_file(key, candidate, sha256=sha256)
            except (ObjectConflict, DigestMismatch):
                continue
            report.restored.append(key)
            break
        else:
            report.unrepairable.append(sha256)

    for sha256, key in sorted(stored.items()):
        if sha256 in expected:
            continue
        report.unreferenced.append(key)
        if not remove_unreferenced:
            continue
        try:
            permit = permit_unreferenced_deletion(
                session, key=key, sha256=sha256, issued_by=issued_by
            )
            store.delete_under_policy(key, permit=permit)
        except (RetentionRefused, ObjectConflict):
            report.refused.append(key)
            continue
        report.removed.append(key)
    return report


def _local_candidates(sha256: str, recorded: list[Path]) -> list[Path]:
    candidates = [path for path in recorded if path.is_file()]
    staging = local_staging_path(content_key(sha256)).parent
    if staging.is_dir():
        candidates.extend(
            sorted(path for path in staging.glob(f"{sha256}.*") if path.is_file())
        )
    return candidates
