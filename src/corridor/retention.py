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

from sqlalchemy import func, select, text
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
    DeletionUncertain,
    StorageError,
    content_key,
    content_store,
    digest_file,
)
from corridor.principals import HumanPrincipal, require_human_principal


CLASS_B_DAYS = 30
OPEN_REFERENCE_DAYS = 90

# How many due items one execution transaction removes before releasing the
# ordering boundary and rechecking (#956 C). A hold placed while a sweep runs
# waits behind at most one batch rather than the whole unbounded pass.
DEFAULT_RETENTION_BATCH_SIZE = 50

# The versioned acknowledgement a bounded execution returns (#956 E): what was
# requested, what the boundary let it enforce, and what a hold or an uncertain
# object-store outcome left failed or pending.
RETENTION_EXECUTION_SCHEMA_VERSION = "retention-execution-v1"

# The one approved sentence a placed hold is acknowledged with (#956 E). It is
# printed only after the ordering boundary is won, and it says exactly what
# winning the boundary licenses and no more: a batch already authorized before
# the hold took effect is not recovered by it.
HOLD_ENFORCEMENT_ACKNOWLEDGEMENT = (
    "The hold is active. New deletion batches within its scope are blocked. "
    "A batch authorized before the hold took effect may already have completed."
)

# The one environment-level ordering boundary hold activation and a sign-in
# record deletion batch both pass through (ADR-0102). A string rather than a
# magic number so the lock says what it is in `pg_locks`; `hashtextextended`
# makes the bigint key, the same idiom `shadow_processing` uses.
HOLD_ORDERING_LOCK = "retention-hold-ordering"


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


def take_hold_ordering_lock(session: Session) -> None:
    """Take the one boundary hold activation and a deletion batch share (ADR-0102).

    A hold predicate evaluated inside a ``DELETE`` is not complete
    serialization: it decides a hold committed *before* the statement began,
    and leaves a hold arriving while the statement runs to chance. The boundary
    is what makes that outcome a stated one rather than a race. Both sides take
    this lock, and the deleting side reads hold state only after it holds it,
    so exactly one of two things happened and the caller can say which:

    - hold activation won, and every deletion batch that begins after it
      commits refuses;
    - a deletion batch won, and it may finish before the hold is enforced.

    Neither ordering recovers a row a batch already removed, and no caller may
    claim it does.

    It is ``pg_advisory_xact_lock``, so the acquiring transaction holds it until
    it commits: that is the property the protocol needs, because it closes the
    window between a hold's ``INSERT`` and its ``COMMIT`` in which a batch could
    otherwise read the holds table and see nothing. An advisory lock is scoped
    to the database, which is scoped to the customer environment, and it needs
    no schema and no grant -- ``corridor_web`` and ``corridor_worker`` can
    already execute it.

    A hold written by something that does not call this -- an operator issuing
    ``INSERT`` into ``retention_holds`` by hand -- is outside the protocol and
    falls back to the ``DELETE`` predicate alone. ``place_hold`` is the only
    application path that activates a hold, and it takes this first.
    """

    session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": HOLD_ORDERING_LOCK},
    )


def place_hold(
    session: Session,
    *,
    project_id: int,
    reason: str,
    principal: HumanPrincipal,
) -> RetentionHold:
    """Append an attributable project hold; one active hold is sufficient.

    Takes the ordering boundary before anything else, so this returns only once
    the hold has won it. What that licenses the caller to say is bounded: every
    deletion batch beginning after this transaction commits is refused. It does
    not say that a batch already running stopped, and it does not say that rows
    a batch removed come back.
    """

    actor = require_human_principal(principal).subject
    if not reason.strip():
        raise RetentionRefused("a retention hold requires a reason")
    # The write is the `place_retention_hold` command's, not the runtime
    # capability's (#956 B): no ordinary login holds a direct write on
    # `retention_holds`. The command takes the ordering boundary as its first
    # act, so the returned hold has already won it.
    hold_id = session.scalar(
        select(func.place_retention_hold(project_id, reason.strip(), actor))
    )
    # The command inserted through raw SQL; the identity map must load the row
    # rather than serve a stale absence.
    session.expire_all()
    return session.get_one(RetentionHold, int(hold_id))


def lift_hold(
    session: Session,
    *,
    hold_id: int,
    principal: HumanPrincipal,
    lifted_at: datetime | None = None,
) -> RetentionHold:
    """Record the separate attributable act that resumes retention work.

    The write is the `lift_retention_hold` command's (#956 B). The worker
    holds no execute on it: lifting a hold is an attributable human act, not
    machine maintenance, so a worker cannot remove the restriction by any route.
    """

    actor = require_human_principal(principal).subject
    if session.get(RetentionHold, hold_id) is None:
        raise RetentionRefused("retention hold does not exist")
    session.scalar(select(func.lift_retention_hold(hold_id, actor, lifted_at)))
    session.expire_all()
    return session.get_one(RetentionHold, int(hold_id))


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
    limit: int | None = None,
) -> RetentionManifest:
    """Remove up to ``limit`` still-pending items under the ordering boundary.

    The destructive operations now take the ``retention-hold-ordering``
    boundary before they read hold state (#956 A), the same one ``place_hold``
    and the sign-in sweep take, so a hold that commits first is honoured and a
    batch that begins first may finish -- a stated outcome rather than a race.

    ``limit`` bounds how many due items one transaction removes (#956 C).
    ``None`` removes every remaining item, which is the whole-manifest behaviour
    an operator's single ``execute`` keeps; ``execute_retention_in_batches``
    passes a bound and commits between batches so a hold does not wait behind an
    entire sweep. The call is re-entrant: an item whose content is already gone,
    or whose object-store outcome is uncertain, is skipped, and the manifest is
    marked ``executed`` only once nothing pending remains.
    """

    # The boundary first, and hold state only after it (ADR-0102): a hold that
    # reached it first is committed by the time the lock is granted here, and
    # one that did not now waits for this batch instead of overlapping it.
    take_hold_ordering_lock(session)

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

    pending = [item for item in items if _item_pending(session, item)]
    if not pending:
        _mark_executed(session, manifest, when)
        return manifest

    batch = pending if limit is None else pending[:limit]

    # Recheck every item in this batch before removing any of it, so a drifted
    # or newly-held item aborts the batch before an irreversible delete.
    for item in batch:
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
    for item in batch:
        if item.family == "processing_artifact":
            _expire_processing_artifact(session, store, item, manifest_id=manifest.id, when=when)
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

    if not any(_item_pending(session, item) for item in items):
        _mark_executed(session, manifest, when)
    else:
        session.flush()
    return manifest


def _expire_processing_artifact(
    session: Session,
    store,
    item: RetentionManifestItem,
    *,
    manifest_id: int,
    when: datetime,
) -> None:
    """Remove one artifact's object, or record the outcome as uncertain (#956 D).

    The object *is* the artifact's content, and a database rollback does not
    restore it, so the store's answer is recorded rather than assumed. A
    confirmed delete sets ``deleted_at`` and drops the staged copy; a delete the
    store could not acknowledge sets ``deletion_uncertain_at`` and leaves the
    staged bytes in place, so reconciliation resolves it later against the store
    instead of a caller pretending it went or that it did not.
    """

    artifact = session.get(ProcessingArtifact, item.source_row_id)
    assert artifact is not None
    key = artifact_key(item.content_sha256, artifact.storage_path)
    permit = permit_deletion(
        session,
        project_id=item.project_id,
        key=key,
        sha256=item.content_sha256,
        issued_by=f"retention_manifest:{manifest_id}",
    )
    try:
        store.delete_under_policy(key, permit=permit)
    except DeletionUncertain:
        artifact.deletion_uncertain_at = when
        return
    except StorageError as exc:
        raise RetentionRefused(
            "processing artifact could not be deleted under policy"
        ) from exc
    # The staged local copy is not the object of record; it goes too.
    Path(artifact.storage_path).unlink(missing_ok=True)
    artifact.deleted_at = when


def _mark_executed(
    session: Session, manifest: RetentionManifest, when: datetime
) -> None:
    manifest.status = "executed"
    manifest.executed_at = when
    session.flush()


def _item_pending(session: Session, item: RetentionManifestItem) -> bool:
    """Whether one manifest item's content is still present and not uncertain."""

    if item.family == "processing_artifact":
        artifact = session.get(ProcessingArtifact, item.source_row_id)
        return (
            artifact is not None
            and artifact.deleted_at is None
            and artifact.deletion_uncertain_at is None
        )
    spec = _BY_FAMILY[item.family]
    deleted_at = session.execute(
        text(
            f"select retention_deleted_at from {spec.table} where id = :row_id"
        ),
        {"row_id": item.source_row_id},
    ).scalar_one_or_none()
    return deleted_at is None


def execute_retention_in_batches(
    session_factory,
    *,
    manifest_id: int,
    expected_sha256: str,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
    executed_at: datetime | None = None,
) -> dict:
    """Run one manifest to completion in committed, boundary-bounded batches.

    Each batch is its own transaction: it takes the ordering boundary, removes
    up to ``batch_size`` items, and commits, releasing the boundary before the
    next batch re-takes it and rechecks holds (#956 C). A hold that commits
    between batches stops the ones that follow without undoing a batch already
    committed. The returned acknowledgement distinguishes the three enforcement
    states -- requested, enforced, failed-or-pending -- and is built only after
    the batches, so nothing prints a claim the boundary has not settled (#956 E).
    """

    if batch_size < 1:
        raise ValueError("a retention batch removes at least one item")
    refusal = ""
    while True:
        with session_factory() as session:
            try:
                manifest = execute_retention(
                    session,
                    manifest_id=manifest_id,
                    expected_sha256=expected_sha256,
                    executed_at=executed_at,
                    limit=batch_size,
                )
                done = manifest.status == "executed"
                session.commit()
            except RetentionRefused as exc:
                session.rollback()
                refusal = _refusal_code(exc)
                break
        if done:
            break
    return _execution_acknowledgement(session_factory, manifest_id, refusal)


def _refusal_code(exc: RetentionRefused) -> str:
    """A bounded reason code for the acknowledgement; the message names details."""

    message = str(exc)
    if "hold" in message:
        return "hold_active"
    if "referenced" in message or "reachable" in message:
        return "content_still_reachable"
    if "changed" in message or "unreadable" in message or "disappeared" in message:
        return "content_changed_after_dry_run"
    return "retention_refused"


def _execution_acknowledgement(
    session_factory, manifest_id: int, refusal: str
) -> dict:
    """The versioned receipt (#956 E), read from committed state after the run."""

    with session_factory() as session:
        manifest = session.get(RetentionManifest, manifest_id)
        items = session.scalars(
            select(RetentionManifestItem).where(
                RetentionManifestItem.manifest_id == manifest_id
            )
        ).all()
        requested = len(items)
        pending = [item for item in items if _item_pending(session, item)]
        uncertain = sum(
            1
            for item in items
            if item.family == "processing_artifact"
            and _artifact_uncertain(session, item.source_row_id)
        )
        enforced = requested - len(pending) - uncertain
        failed_or_pending = requested - enforced
        if failed_or_pending == 0:
            outcome = "enforced"
        elif refusal:
            outcome = "blocked"
        else:
            outcome = "partial"
        return {
            "schema_version": RETENTION_EXECUTION_SCHEMA_VERSION,
            "manifest_public_id": manifest.public_id if manifest is not None else "",
            "manifest_status": manifest.status if manifest is not None else "",
            "requested": requested,
            "enforced": enforced,
            "failed_or_pending": failed_or_pending,
            "uncertain": uncertain,
            "outcome": outcome,
            "refusal": refusal,
        }


def _artifact_uncertain(session: Session, artifact_id: int) -> bool:
    artifact = session.get(ProcessingArtifact, artifact_id)
    return artifact is not None and artifact.deletion_uncertain_at is not None


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
