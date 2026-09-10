"""Expire only classified intermediary content through a manifest gate.

Before ADR-0072, assistant inputs and processing artifacts were either retained
forever or deleted ad hoc by their owner.  This module replaces both shapes with
one allowlisted Class B boundary: it can see only the five non-authoritative
assistant receipt families, emits an exact digest manifest first, rechecks holds
and reachability at execution, and leaves receipt identity plus digests behind.
Project Record tables are absent from the catalog, so no caller can ask this
module to delete them.

File-backed artifacts live in the content-addressed store (ADR-0079). This
module is the only issuer of the ``DeletionPermit`` the store's
``delete_under_policy`` requires, and it issues one only after the hold check,
so a held object cannot be deleted on either backend (ADR-0080).
"""

from __future__ import annotations

from corridor import digests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.models import (
    ProcessingArtifact,
    RetentionHold,
    RetentionManifest,
    RetentionManifestItem,
    RetentionReference,
)
from corridor.object_storage import (
    DeletionPermit,
    StorageError,
    content_key,
    content_store,
    digest_file,
)
from corridor.principals import HumanPrincipal, require_human_principal


CLASS_B_DAYS = 30
OPEN_REFERENCE_DAYS = 90


@dataclass(frozen=True)
class FamilySpec:
    family: str
    table: str
    content_columns: tuple[str, ...]


FAMILY_SPECS = (
    FamilySpec(
        "coordination_summary",
        "coordination_summary_requests",
        ("project_reading_json", "summary_markdown"),
    ),
    FamilySpec(
        "production_run_explanation",
        "production_run_explanation_requests",
        (
            "competing_run_ids_json", "comparison_json", "explanation_json",
            "execution_lineage_json", "budget_json", "usage_json",
        ),
    ),
    FamilySpec(
        "extraction_failure_diagnosis",
        "extraction_failure_diagnosis_requests",
        (
            "source_context_json", "diagnosis_json", "execution_lineage_json",
            "budget_json", "usage_json",
        ),
    ),
    FamilySpec(
        "revision_change_explanation",
        "revision_change_explanation_requests",
        (
            "comparison_json", "explanation_json", "execution_lineage_json",
            "budget_json", "usage_json",
        ),
    ),
    FamilySpec(
        "source_intake_draft",
        "source_intake_draft_requests",
        (
            "permitted_pages_json", "source_json", "proposals_json",
            "execution_lineage_json", "budget_json", "usage_json",
        ),
    ),
)
_BY_FAMILY = {spec.family: spec for spec in FAMILY_SPECS}


class RetentionRefused(ValueError):
    """A hold, reference, drift, or non-Class-B target prevented deletion."""


def register_processing_artifact(
    session: Session,
    *,
    project_id: int,
    kind: str,
    path: str | Path,
    terminal_at: datetime,
) -> ProcessingArtifact:
    """Classify one page render, raw response, trace, or working artifact.

    Idempotent on `storage_path` so the persistence seam can register without
    the caller first checking: re-ingesting the same document, or persisting a
    derivative twice, returns the existing classification rather than colliding
    on the unique path. A registration that names a live path with a different
    project, kind, or content digest is drift and is refused.

    `path` is the locally staged file the producer wrote. The bytes are
    persisted to the content-addressed store before the row exists, so a crash
    between the two leaves an unreferenced object for reconciliation rather
    than a manifest row without its bytes.
    """

    source = Path(path)
    storage_path = str(source)
    digest = digest_file(source)
    content_store().put_file(artifact_key(digest, storage_path), source, sha256=digest)
    existing = session.scalar(
        select(ProcessingArtifact).where(
            ProcessingArtifact.storage_path == storage_path
        )
    )
    if existing is not None:
        if (
            existing.project_id != project_id
            or existing.kind != kind
            or existing.content_sha256 != digest
            or existing.deleted_at is not None
        ):
            raise RetentionRefused(
                f"processing artifact registration conflicts at {storage_path}"
            )
        return existing
    artifact = ProcessingArtifact(
        project_id=project_id,
        kind=kind,
        retention_class="class_b",
        storage_path=storage_path,
        content_sha256=digest,
        terminal_at=terminal_at,
    )
    session.add(artifact)
    session.flush()
    return artifact


def artifact_key(content_sha256: str, storage_path: str) -> str:
    """The store key of a registered artifact: its digest with its staged suffix."""

    return content_key(content_sha256, Path(storage_path).suffix)


def permit_deletion(
    session: Session, *, project_id: int, key: str, sha256: str, issued_by: str
) -> DeletionPermit:
    """Issue the one permit the store accepts, after the hold check.

    A hold suspends every deletion path (ADR-0080), and the store cannot see
    holds; this is the seam that joins the two, so both backends refuse a held
    object identically.
    """

    if _active_hold(session, project_id):
        raise RetentionRefused("active retention hold suspends every deletion path")
    return DeletionPermit(key=key, sha256=sha256, issued_by=issued_by)


def permit_unreferenced_deletion(
    session: Session, *, key: str, sha256: str, issued_by: str
) -> DeletionPermit:
    """Permit removing an object no manifest row references (reconciliation).

    An unreferenced object cannot be attributed to a project, so any active
    hold anywhere refuses it: the object might be the held project's.
    """

    if session.scalar(
        select(RetentionHold.id).where(RetentionHold.lifted_at.is_(None)).limit(1)
    ) is not None:
        raise RetentionRefused("an active retention hold suspends unreferenced cleanup")
    return DeletionPermit(key=key, sha256=sha256, issued_by=issued_by)


def place_hold(
    session: Session,
    *,
    project_id: int,
    reason: str,
    principal: HumanPrincipal,
) -> RetentionHold:
    """Append an attributable project hold; one active hold is sufficient."""

    actor = require_human_principal(principal).subject
    if not reason.strip():
        raise RetentionRefused("a retention hold requires a reason")
    active = session.scalar(
        select(RetentionHold).where(
            RetentionHold.project_id == project_id,
            RetentionHold.lifted_at.is_(None),
        )
    )
    if active is not None:
        return active
    hold = RetentionHold(project_id=project_id, reason=reason.strip(), placed_by=actor)
    session.add(hold)
    session.flush()
    return hold


def lift_hold(
    session: Session,
    *,
    hold_id: int,
    principal: HumanPrincipal,
    lifted_at: datetime | None = None,
) -> RetentionHold:
    """Record the separate attributable act that resumes retention work."""

    actor = require_human_principal(principal).subject
    hold = session.get(RetentionHold, hold_id)
    if hold is None:
        raise RetentionRefused("retention hold does not exist")
    if hold.lifted_at is None:
        hold.lifted_by = actor
        hold.lifted_at = lifted_at or datetime.now(timezone.utc)
        session.flush()
    return hold


def open_reference(
    session: Session,
    *,
    project_id: int,
    family: str,
    source_row_id: int,
    kind: str,
    referenced_by: str,
) -> RetentionReference:
    """Register a cited segment/decision/review/release/failure reachability edge."""

    if family not in _BY_FAMILY and family != "processing_artifact":
        raise RetentionRefused("retention references can name only Class B families")
    reference = RetentionReference(
        project_id=project_id,
        family=family,
        source_row_id=source_row_id,
        kind=kind,
        referenced_by=referenced_by,
    )
    session.add(reference)
    session.flush()
    return reference


def close_reference(
    session: Session, reference_id: int, *, closed_at: datetime | None = None
) -> RetentionReference:
    reference = session.get(RetentionReference, reference_id)
    if reference is None:
        raise RetentionRefused("retention reference does not exist")
    reference.closed_at = closed_at or datetime.now(timezone.utc)
    session.flush()
    return reference


def plan_retention(
    session: Session,
    *,
    as_of: datetime,
    principal: HumanPrincipal,
    project_id: int | None = None,
) -> RetentionManifest:
    """Persist the exact dry-run candidate list and every content digest.

    `project_id` narrows the candidate list to one project. A Due Work
    schedule is declared per project (`retention_sweep`), so a scheduled sweep
    must be able to plan its own project's expiry without listing another's;
    omitting it keeps the operator command's whole-database scope.
    """

    actor = require_human_principal(principal).subject
    candidates: list[dict] = []
    for spec in FAMILY_SPECS:
        for row in _due_rows(session, spec, as_of, project_id):
            if _active_hold(session, row["project_id"]):
                continue
            refs = _open_references(session, spec.family, row["id"])
            age = as_of - row["completed_at"]
            if any(ref.kind in {"segment", "decision", "release"} for ref in refs):
                raise RetentionRefused("durably referenced intermediary content is not deletable")
            if refs and age < timedelta(days=OPEN_REFERENCE_DAYS):
                continue
            if refs:
                raise RetentionRefused("reachable intermediary content is not deletable")
            payload = {column: row[column] for column in spec.content_columns}
            digest = _digest(payload)
            candidates.append(
                {
                    "project_id": row["project_id"],
                    "family": spec.family,
                    "source_row_id": row["id"],
                    "content_sha256": digest,
                    "terminal_at": row["completed_at"],
                    "delete_after": row["completed_at"] + timedelta(days=CLASS_B_DAYS),
                }
            )
    artifact_query = select(ProcessingArtifact).where(
        ProcessingArtifact.retention_class == "class_b",
        ProcessingArtifact.deleted_at.is_(None),
        ProcessingArtifact.terminal_at <= as_of - timedelta(days=CLASS_B_DAYS),
    )
    if project_id is not None:
        artifact_query = artifact_query.where(
            ProcessingArtifact.project_id == project_id
        )
    for artifact in session.scalars(artifact_query).all():
        if _active_hold(session, artifact.project_id):
            continue
        refs = _open_references(session, "processing_artifact", artifact.id)
        age = as_of - artifact.terminal_at
        if any(ref.kind in {"segment", "decision", "release"} for ref in refs):
            raise RetentionRefused("durably referenced intermediary content is not deletable")
        if refs and age < timedelta(days=OPEN_REFERENCE_DAYS):
            continue
        if refs:
            raise RetentionRefused("reachable intermediary content is not deletable")
        candidates.append(
            {
                "project_id": artifact.project_id,
                "family": "processing_artifact",
                "source_row_id": artifact.id,
                "content_sha256": artifact.content_sha256,
                "terminal_at": artifact.terminal_at,
                "delete_after": artifact.terminal_at
                + timedelta(days=CLASS_B_DAYS),
            }
        )
    candidates.sort(key=lambda item: (item["family"], item["source_row_id"]))
    content_sha = _digest([_json_value(item) for item in candidates])
    manifest = RetentionManifest(
        public_id=str(uuid4()),
        as_of=as_of,
        status="dry_run",
        content_sha256=content_sha,
        created_by=actor,
    )
    session.add(manifest)
    session.flush()
    for item in candidates:
        session.add(RetentionManifestItem(manifest_id=manifest.id, **item))
    session.flush()
    return manifest


def execute_retention(
    session: Session,
    *,
    manifest_id: int,
    expected_sha256: str,
    executed_at: datetime | None = None,
) -> RetentionManifest:
    """Execute one unchanged dry-run after repeating every safety check."""

    manifest = session.get(RetentionManifest, manifest_id)
    if manifest is None or manifest.status != "dry_run":
        raise RetentionRefused("retention manifest is not executable")
    if manifest.content_sha256 != expected_sha256:
        raise RetentionRefused("retention manifest digest does not match")
    when = executed_at or datetime.now(timezone.utc)
    store = content_store()
    items = session.scalars(
        select(RetentionManifestItem)
        .where(RetentionManifestItem.manifest_id == manifest.id)
        .order_by(RetentionManifestItem.id)
    ).all()
    for item in items:
        if _active_hold(session, item.project_id):
            raise RetentionRefused("active retention hold suspends the TTL job")
        if _open_references(session, item.family, item.source_row_id):
            raise RetentionRefused("reachable intermediary content is not deletable")
        if item.family == "processing_artifact":
            artifact = session.get(ProcessingArtifact, item.source_row_id)
            if artifact is None or artifact.deleted_at is not None:
                raise RetentionRefused("processing artifact disappeared after dry-run")
            try:
                store.get(
                    artifact_key(item.content_sha256, artifact.storage_path),
                    sha256=item.content_sha256,
                )
            except StorageError as exc:
                raise RetentionRefused(
                    "processing artifact is unreadable or changed after dry-run"
                ) from exc
        else:
            spec = _BY_FAMILY[item.family]
            row = _one_row(session, spec, item.source_row_id)
            payload = {column: row[column] for column in spec.content_columns}
            if _digest(payload) != item.content_sha256:
                raise RetentionRefused("intermediary content changed after dry-run")
    session.execute(
        text("select set_config('corridor.retention_manifest_id', :manifest_id, true)"),
        {"manifest_id": str(manifest.id)},
    )
    for item in items:
        if item.family == "processing_artifact":
            artifact = session.get(ProcessingArtifact, item.source_row_id)
            assert artifact is not None
            key = artifact_key(item.content_sha256, artifact.storage_path)
            permit = permit_deletion(
                session,
                project_id=item.project_id,
                key=key,
                sha256=item.content_sha256,
                issued_by=f"retention_manifest:{manifest.id}",
            )
            try:
                store.delete_under_policy(key, permit=permit)
            except StorageError as exc:
                raise RetentionRefused(
                    "processing artifact could not be deleted under policy"
                ) from exc
            # The staged local copy is not the object of record; it goes too.
            Path(artifact.storage_path).unlink(missing_ok=True)
            artifact.deleted_at = when
            continue
        spec = _BY_FAMILY[item.family]
        assignments = ", ".join(f"{column} = null" for column in spec.content_columns)
        session.execute(
            text(
                f"update {spec.table} set {assignments}, "
                "retention_content_sha256 = :digest, retention_deleted_at = :deleted_at "
                "where id = :row_id and retention_class = 'class_b'"
            ),
            {"digest": item.content_sha256, "deleted_at": when, "row_id": item.source_row_id},
        )
    manifest.status = "executed"
    manifest.executed_at = when
    session.flush()
    return manifest


def delete_rebuildable_page_data(session: Session) -> None:
    """Clear Class C page projections without touching sources or segments."""

    session.execute(text("update doc_pages set inventory_json = null, routing_json = null"))
    session.execute(text("delete from page_processing_failures"))


def _due_rows(
    session: Session,
    spec: FamilySpec,
    as_of: datetime,
    project_id: int | None = None,
) -> list[dict]:
    cutoff = as_of - timedelta(days=CLASS_B_DAYS)
    columns = ", ".join(spec.content_columns)
    scope = "" if project_id is None else " and project_id = :project_id"
    parameters: dict = {"cutoff": cutoff}
    if project_id is not None:
        parameters["project_id"] = project_id
    return list(
        session.execute(
            text(
                f"select id, project_id, completed_at, {columns} from {spec.table} "
                "where retention_class = 'class_b' and retention_deleted_at is null "
                f"and completed_at <= :cutoff{scope}"
            ),
            parameters,
        ).mappings()
    )


def _one_row(session: Session, spec: FamilySpec, row_id: int) -> dict:
    columns = ", ".join(spec.content_columns)
    row = session.execute(
        text(f"select {columns} from {spec.table} where id = :row_id"),
        {"row_id": row_id},
    ).mappings().one_or_none()
    if row is None:
        raise RetentionRefused("intermediary row disappeared after dry-run")
    return dict(row)


def _active_hold(session: Session, project_id: int) -> bool:
    return session.scalar(
        select(RetentionHold.id).where(
            RetentionHold.project_id == project_id,
            RetentionHold.lifted_at.is_(None),
        ).limit(1)
    ) is not None


def _open_references(session: Session, family: str, source_row_id: int) -> tuple:
    return tuple(
        session.scalars(
            select(RetentionReference).where(
                RetentionReference.family == family,
                RetentionReference.source_row_id == source_row_id,
                RetentionReference.closed_at.is_(None),
            )
        ).all()
    )


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _digest(value) -> str:
    # Retained encoding: retained deletion receipts were sealed with
    # non-ASCII escaped.
    return digests.ascii_escaped_sha256(_json_value(value))
