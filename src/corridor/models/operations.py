"""Running the system: environment binding, due work, retention, sessions.

The relations an operator and the runtime need that are not part of any
project's record. Due work is a schedule, its occurrences, and a receipt per
occurrence, so a missed run is visible rather than inferred from absence.
Retention holds and manifests are here rather than with the artifacts they
cover because disposal is an operations obligation over every family at once.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "AuditLog",
    "CustomerEnvironmentBinding",
    "DueWorkOccurrence",
    "DueWorkReceipt",
    "DueWorkSchedule",
    "LegacyLedgerArchive",
    "ProcessingArtifact",
    "RetentionHold",
    "RetentionManifest",
    "RetentionManifestItem",
    "RetentionReference",
    "SignInAttempt",
    "SignInToken",
    "WebSession",
]


class CustomerEnvironmentBinding(Base):
    """Immutable local identity checked before customer content is reachable."""

    __tablename__ = "customer_environment_binding"
    __table_args__ = (CheckConstraint("singleton", name="ck_customer_environment_singleton"),)

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    customer_id: Mapped[str] = mapped_column(String(128))
    environment_id: Mapped[str] = mapped_column(String(128))
    deployment_id: Mapped[str] = mapped_column(String(128))


class DueWorkSchedule(Base):
    """One validated gate-7 declaration for a server-owned handler."""

    __tablename__ = "due_work_schedules"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "handler_key",
            "configuration_version",
            "input_identity_sha256",
            name="uq_due_work_schedule_identity",
        ),
        CheckConstraint(
            "configuration_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_configuration_sha256",
        ),
        CheckConstraint(
            "input_identity_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_schedule_input_identity_sha256",
        ),
        CheckConstraint(
            "retention_days > 0 and max_attempts > 0 and backoff_seconds >= 0 "
            "and claim_ttl_seconds > 0 and deadline_seconds > 0 "
            "and concurrency_limit > 0 and model_token_budget >= 0 "
            "and notification_budget >= 0",
            name="ck_due_work_schedule_budgets",
        ),
        CheckConstraint(
            "disabled_at is null or disabled_at >= enabled_at",
            name="ck_due_work_schedule_disable_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    handler_key: Mapped[str] = mapped_column(String(64))
    configuration_version: Mapped[str] = mapped_column(String(64))
    scope_json: Mapped[dict] = mapped_column(JSONB)
    configuration_json: Mapped[dict] = mapped_column(JSONB)
    configuration_sha256: Mapped[str] = mapped_column(String(64))
    input_identity_sha256: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cadence: Mapped[str] = mapped_column(String(32))
    timezone_name: Mapped[str] = mapped_column(String(64))
    missed_run_policy: Mapped[str] = mapped_column(String(32))
    retention_days: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    backoff_seconds: Mapped[int] = mapped_column(Integer)
    claim_ttl_seconds: Mapped[int] = mapped_column(Integer)
    deadline_seconds: Mapped[int] = mapped_column(Integer)
    concurrency_limit: Mapped[int] = mapped_column(Integer)
    model_token_budget: Mapped[int] = mapped_column(Integer)
    notification_budget: Mapped[int] = mapped_column(Integer)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DueWorkOccurrence(Base):
    """Mutable claim state for one stable scheduled occurrence."""

    __tablename__ = "due_work_occurrences"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_due_work_occurrence_key"),
        CheckConstraint(
            "state in ('pending', 'claimed', 'retry_due', 'completed', 'failed')",
            name="ck_due_work_occurrence_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_due_work_occurrence_attempt_count",
        ),
        CheckConstraint(
            "(state = 'claimed' and owner is not null and claim_token is not null "
            "and lease_expires_at is not null and claimed_at is not null "
            "and deadline_at is not null) or "
            "(state <> 'claimed' and owner is null and claim_token is null "
            "and lease_expires_at is null and claimed_at is null "
            "and deadline_at is null)",
            name="ck_due_work_occurrence_claim_shape",
        ),
        CheckConstraint(
            "(state = 'retry_due' and next_attempt_at is not null) or "
            "(state <> 'retry_due' and next_attempt_at is null)",
            name="ck_due_work_occurrence_retry_shape",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    scheduled_job_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_schedules.id"), index=True
    )
    occurrence_key: Mapped[str] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(16))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DueWorkReceipt(Base):
    """Append-only result of one bounded due-work attempt."""

    __tablename__ = "due_work_receipts"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", "attempt_number", name="uq_due_work_receipt_attempt"
        ),
        CheckConstraint(
            "execution_outcome in ('completed', 'retry_due', 'failed')",
            name="ck_due_work_receipt_outcome",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_due_work_receipt_content_sha256",
        ),
        CheckConstraint(
            "attempt_number > 0 and finished_at >= started_at",
            name="ck_due_work_receipt_attempt",
        ),
        CheckConstraint(
            "(execution_outcome = 'completed' and handler_result_json is not null "
            "and error_code is null) or "
            "(execution_outcome in ('retry_due', 'failed') "
            "and handler_result_json is null and error_code is not null)",
            name="ck_due_work_receipt_result_shape",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    handler_key: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(String(64), unique=True)
    runtime_owner: Mapped[str] = mapped_column(String(128))
    execution_outcome: Mapped[str] = mapped_column(String(16))
    handler_result_json: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_next_step: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProcessingArtifact(Base):
    """One classified file-backed intermediary with a digest remainder."""

    __tablename__ = "processing_artifacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    retention_class: Mapped[str] = mapped_column(String(16), server_default="class_b")
    storage_path: Mapped[str] = mapped_column(Text, unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionHold(Base):
    """One attributable project hold that suspends every Class B delete path."""

    __tablename__ = "retention_holds"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    placed_by: Mapped[str] = mapped_column(String(128))
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lifted_by: Mapped[str | None] = mapped_column(String(128))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionReference(Base):
    """A durable or open reference that makes intermediary content unreachable."""

    __tablename__ = "retention_references"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(64))
    source_row_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    referenced_by: Mapped[str] = mapped_column(Text)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionManifest(Base):
    """Immutable dry-run identity for one exact set of eligible Class B values."""

    __tablename__ = "retention_manifests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionManifestItem(Base):
    """One digest-pinned intermediary value named before deletion."""

    __tablename__ = "retention_manifest_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("retention_manifests.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(64))
    source_row_id: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LegacyLedgerArchive(Base):
    """One immutable receipt for retiring a development-era Ledger graph.

    The JSON owns every row needed for standalone historical readback.  The
    scalar counts and ref-code high-water mark make the destructive operation
    auditable without asking active Ledger tables that are empty afterwards.
    """

    __tablename__ = "legacy_ledger_archives"
    __table_args__ = (
        UniqueConstraint("project_id"),
        CheckConstraint(
            "jsonb_typeof(content_json) = 'object'",
            name="ck_legacy_ledger_archive_content_object",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_legacy_ledger_archive_sha256",
        ),
        CheckConstraint(
            "dependency_count >= 0 and assertion_count >= 0 "
            "and evidence_link_count >= 0 and audit_log_count >= 0",
            name="ck_legacy_ledger_archive_counts",
        ),
        CheckConstraint(
            "ref_code_high_watermark >= 0",
            name="ck_legacy_ledger_archive_ref_high_watermark",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    format_version: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[dict] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    dependency_count: Mapped[int] = mapped_column(Integer)
    assertion_count: Mapped[int] = mapped_column(Integer)
    evidence_link_count: Mapped[int] = mapped_column(Integer)
    audit_log_count: Mapped[int] = mapped_column(Integer)
    ref_code_high_watermark: Mapped[int] = mapped_column(Integer)
    retired_by: Mapped[str] = mapped_column(Text)
    retirement_report_run_watermark_id: Mapped[int | None] = mapped_column(BigInteger)
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditLog(Base):
    """Append-only. Every ledger mutation writes here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    # Null on legacy and non-Admission entries.  New Admissions store the
    # exact namespaced subject here; ``actor`` remains for honest display of
    # historical labels such as ``agent`` and ``demo`` rather than relabelling
    # them as people (ADR-0020).
    human_principal: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[int] = mapped_column(BigInteger)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SignInToken(Base):
    """One expiring, single-use magic-link secret, stored only as a hash.

    The raw token lives only in the emailed link; the column is its SHA-256 so a
    database read can never replay a link.  ``consumed_at`` is the single-use
    guard: consumption is an atomic ``UPDATE ... WHERE consumed_at IS NULL AND
    expires_at > now()`` so an expired, reused, tampered, or concurrently
    consumed link can never establish a second session (#331).
    """

    __tablename__ = "sign_in_tokens"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_sign_in_token_hash"),
        CheckConstraint("length(token_sha256) = 64", name="ck_sign_in_token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_sign_in_token_expiry"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_normalized: Mapped[str] = mapped_column(Text)
    token_sha256: Mapped[str] = mapped_column(String(64))
    redirect_path: Mapped[str | None] = mapped_column(Text, server_default=text("null"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("null")
    )


class WebSession(Base):
    """A signed-in browser session: explicit expiry, revocable, hash-stored.

    The cookie carries a random id; this row stores only its SHA-256, so a
    database read cannot resume a session.  ``expires_at`` and ``revoked_at`` are
    both checked on every request, which is why revoking a session (logout) or a
    membership takes effect immediately, with no reliance on a stale roster or an
    earlier page load (#331).  ``csrf_sha256`` is the hash of the per-session
    request-forgery token echoed by authenticated writes.
    """

    __tablename__ = "web_sessions"
    __table_args__ = (
        UniqueConstraint("session_sha256", name="uq_web_session_hash"),
        CheckConstraint("length(session_sha256) = 64", name="ck_web_session_hash"),
        CheckConstraint("length(csrf_sha256) = 64", name="ck_web_session_csrf"),
        CheckConstraint("expires_at > created_at", name="ck_web_session_expiry"),
        CheckConstraint(
            "length(trim(principal_subject)) > 0", name="ck_web_session_principal"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_sha256: Mapped[str] = mapped_column(String(64))
    csrf_sha256: Mapped[str] = mapped_column(String(64))
    principal_subject: Mapped[str] = mapped_column(String(128))
    email_normalized: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("null")
    )


class SignInAttempt(Base):
    """Append-only record of one issuance or consumption attempt, for backoff.

    Counting rows in a recent window bounds how many email or token attempts an
    unauthenticated caller can generate (#331).  The scope is the throttle key —
    a normalized email or a client address — never a claim that the email maps to
    a member, so the counter cannot be used to enumerate membership.
    """

    __tablename__ = "sign_in_attempts"
    __table_args__ = (
        Index(
            "ix_sign_in_attempt_scope",
            "scope_kind",
            "scope_value",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    scope_value: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
