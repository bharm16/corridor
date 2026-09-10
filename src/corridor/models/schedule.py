"""Milestones, key dates, and the schedule links derived from a source.

A milestone registration is a human act; a governing derivation and a schedule
link receipt record what the system computed from a schedule source and why.
Keeping the derivation as its own row is what lets a link be re-derived and
compared instead of overwritten, which the earlier in-place update prevented.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "KeyDateDraftReceipt",
    "KeyDateDraftRowReceipt",
    "Milestone",
    "MilestoneRegistration",
    "ScheduleGoverningDerivation",
    "ScheduleLinkReceipt",
]


class Milestone(Base):
    """A dated event in the project schedule that Dependencies must be ready for.

    A Dependency's need date is derived from the Milestone it serves — a
    property of the *project*. That is a different thing from its committed
    date, which is what an external party said it would do, and keeping the
    two apart is most of the point of the ledger.
    """

    __tablename__ = "milestones"
    __table_args__ = (UniqueConstraint("project_id", "code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    need_date: Mapped[date | None] = mapped_column(Date)
    # Where this came from — a CSV filename in v0, a P6 XER export in M9.
    source: Mapped[str | None] = mapped_column(Text)
    current_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "milestone_registrations.id",
            name="fk_milestones_current_registration",
            use_alter=True,
        ),
        deferred=True,
        server_default=text("null"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MilestoneRegistration(Base):
    """One immutable registered revision of a project Milestone."""

    __tablename__ = "milestone_registrations"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_registration_id",
            name="uq_milestone_registrations_predecessor",
        ),
        CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_milestone_registrations_source_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(source_row_json) = 'object'",
            name="ck_milestone_registrations_source_row",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_milestone_registrations_recorded_by",
        ),
        Index(
            "uq_milestone_registrations_one_root",
            "milestone_id",
            unique=True,
            postgresql_where=text("predecessor_registration_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"), index=True)
    source_name: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_row_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    predecessor_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestone_registrations.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KeyDateDraftReceipt(Base):
    """One bounded, source-bound, non-authoritative Key date drafting attempt.

    This receipt deliberately has no relationship to ``Milestone`` or
    ``Dependency``.  A model can leave a draft here, but only the existing
    human import and linking commands can change the Project Record.
    """

    __tablename__ = "key_date_draft_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "source_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_key_date_draft_receipts_source_same_project",
        ),
        CheckConstraint(
            "status in ('drafted', 'abstained', 'failed')",
            name="ck_key_date_draft_receipts_status",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_key_date_draft_receipts_source_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_pages_json) = 'array' and "
            "jsonb_typeof(configuration_json) = 'object' and "
            "jsonb_typeof(budget_json) = 'object' and "
            "jsonb_typeof(usage_json) = 'object' and "
            "jsonb_typeof(unresolved_json) = 'array' and "
            "jsonb_typeof(sequencing_json) = 'array'",
            name="ck_key_date_draft_receipts_json",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64))
    allowed_pages_json: Mapped[list] = mapped_column(JSONB)
    requested_by: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    configuration_json: Mapped[dict] = mapped_column(JSONB)
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    unresolved_json: Mapped[list] = mapped_column(JSONB)
    sequencing_json: Mapped[list] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KeyDateDraftRowReceipt(Base):
    """One validated day-precise row retained beside its draft receipt."""

    __tablename__ = "key_date_draft_row_receipts"
    __table_args__ = (
        UniqueConstraint("receipt_id", "ordinal", name="uq_key_date_draft_row_ordinal"),
        CheckConstraint("precision = 'day'", name="ck_key_date_draft_row_precision"),
        CheckConstraint("source_page > 0", name="ck_key_date_draft_row_source_page"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("key_date_draft_receipts.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(Text)
    need_date: Mapped[date] = mapped_column(Date)
    precision: Mapped[str] = mapped_column(String(16))
    source_page: Mapped[int] = mapped_column(Integer)
    source_quote: Mapped[str] = mapped_column(Text)


class ScheduleGoverningDerivation(Base):
    """One immutable record of which schedule activities govern utility work.

    ADR-0057: governing dates identify themselves. When a schedule imports,
    activities whose codes and names match utility conventions flag themselves
    as the governing set with no human step, and this row records exactly which
    codes and names matched (``matches_json``). Only when the coding is too poor
    to read does a person pick, once — a ``human_pick`` row under their own
    subject. Append-only: a re-derivation that changes nothing writes nothing.
    """

    __tablename__ = "schedule_governing_derivations"
    __table_args__ = (
        CheckConstraint(
            "method in ('coded', 'awaiting_pick', 'human_pick')",
            name="ck_schedule_governing_method",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_schedule_governing_recorded_by",
        ),
        CheckConstraint(
            "jsonb_typeof(matches_json) = 'array'",
            name="ck_schedule_governing_matches",
        ),
        CheckConstraint(
            "source_sha256 is null or source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_governing_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_name: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(24))
    recorded_by: Mapped[str] = mapped_column(String(128))
    matches_json: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScheduleLinkReceipt(Base):
    """The deciding values behind one Constraint-to-key-date link.

    The binding itself is written by ``milestones.link_dependency`` (unchanged),
    which copies the exact Key Date Version onto the Constraint and audits it.
    This receipt retains *why* that activity was chosen — the Constraint's own
    station text and the governing activity's coverage, verbatim from both
    sources (ADR-0057, ADR-0051's exact-rule discipline). ``basis`` says whether
    an exact rule fired (``exact_station_containment``), a person resolved a tie
    or confirmed a single candidate (``human_choice``), or a schedule revision
    advanced an existing link (``flow_through``). ``audit_log_id`` ties this to
    the exact ``LINK_MILESTONE`` audit entry so the two can never drift.
    """

    __tablename__ = "schedule_link_receipts"
    __table_args__ = (
        UniqueConstraint(
            "audit_log_id", name="uq_schedule_link_receipts_audit"
        ),
        CheckConstraint(
            "basis in ('exact_station_containment', 'human_choice', 'flow_through')",
            name="ck_schedule_link_receipts_basis",
        ),
        CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_schedule_link_receipts_decided_by",
        ),
        CheckConstraint(
            "jsonb_typeof(deciding_values_json) = 'object'",
            name="ck_schedule_link_receipts_values",
        ),
        CheckConstraint(
            "policy_sha256 is null or policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_schedule_link_receipts_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), index=True
    )
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"))
    milestone_registration_id: Mapped[int] = mapped_column(
        ForeignKey("milestone_registrations.id")
    )
    audit_log_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"))
    basis: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(128))
    # Null for a human choice: only the automatic exact rule stands on a
    # fingerprinted, replay-gated policy version.
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    deciding_values_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
