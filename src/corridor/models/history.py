"""Retained legacy history and native coordination history, storage only.

Core ``Table`` objects rather than mapped classes, deliberately: native services
consume typed readers over these relations and no ORM writer should exist for
them, so there is no class for one to be reached through. They are last in the
package's import order for the same reason they were last in the single module
-- they only need ``Base.metadata`` to exist -- and they carry no relationships.
"""

import sqlalchemy as _legacy_sa
from sqlalchemy.dialects import postgresql as _legacy_pg

from corridor.models.base import Base


# Integrate at the end of models.py. These are storage-only receipt mappings;
# native services deliberately consume typed readers rather than ORM writers.
# Must follow the existing Base declaration. No migration helper is imported.
_legacy_history_batches = _legacy_sa.Table(
    "legacy_history_batches", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("run_key", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("executor", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("code_revision", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("inventory_version", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("content_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("payload", _legacy_pg.JSONB, nullable=False),
    _legacy_sa.Column("counts", _legacy_pg.JSONB, nullable=False),
    _legacy_sa.Column("captured_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("project_id", "run_key"),
    _legacy_sa.CheckConstraint("length(btrim(run_key))>0"),
    _legacy_sa.CheckConstraint("length(btrim(executor))>0"),
    _legacy_sa.CheckConstraint("length(btrim(code_revision))>0"),
    _legacy_sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("jsonb_typeof(payload)='object'"),
    _legacy_sa.CheckConstraint("jsonb_typeof(counts)='object'"),
)

_legacy_history_reversals = _legacy_sa.Table(
    "legacy_history_reversals", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False, unique=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("reversed_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reversed_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("transaction_timestamp()")),
    _legacy_sa.CheckConstraint("length(btrim(reversed_by))>0"),
    _legacy_sa.CheckConstraint("length(btrim(reason))>0"),
)

_legacy_history_evidence_migrations = _legacy_sa.Table(
    "legacy_history_evidence_migrations", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("legacy_evidence_link_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("evidence_link_source_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("evidence_link_sources.id")),
    _legacy_sa.Column("source_segment_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("source_segments.id")),
    _legacy_sa.Column("original_quote_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("outcome", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("created_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("transaction_timestamp()")),
    _legacy_sa.UniqueConstraint("batch_id", "legacy_evidence_link_id"),
    _legacy_sa.CheckConstraint("original_quote_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("outcome in ('segment_reference','already_native','retained_quote')"),
)

_coordination_record_subjects = _legacy_sa.Table(
    "coordination_record_subjects", Base.metadata,
    _legacy_sa.Column("id", _legacy_pg.UUID(as_uuid=True), primary_key=True, server_default=_legacy_sa.text("gen_random_uuid()")),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_kind", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("created_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("project_id", "id"),
    _legacy_sa.CheckConstraint("subject_kind in ('constraint','commitment')"),
)

_coordination_subject_lineage = _legacy_sa.Table(
    "coordination_subject_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_dependency_id", _legacy_sa.BigInteger, unique=True),
    _legacy_sa.Column("legacy_commitment_lineage_id", _legacy_sa.BigInteger, unique=True),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.CheckConstraint("num_nonnulls(legacy_dependency_id,legacy_commitment_lineage_id)=1"),
)

_coordination_history_activations = _legacy_sa.Table(
    "coordination_history_activations", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id"), nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.UniqueConstraint("subject_id", "history_batch_id"),
)

_coordination_record_decisions = _legacy_sa.Table(
    "coordination_record_decisions", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("revision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("project_record_revisions.id"), nullable=False),
    _legacy_sa.Column("field", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("decision_type", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("value_text", _legacy_sa.Text),
    _legacy_sa.Column("action_due_date", _legacy_sa.Date),
    _legacy_sa.Column("action_due_date_reason", _legacy_sa.Text),
    _legacy_sa.Column("milestone_ids", _legacy_pg.ARRAY(_legacy_sa.BigInteger), nullable=False, server_default=_legacy_sa.text("'{}'")),
    _legacy_sa.Column("deferral_reason", _legacy_sa.Text),
    _legacy_sa.Column("deferral_return_date", _legacy_sa.Date),
    _legacy_sa.Column("no_follow_up_reason", _legacy_sa.Text),
    _legacy_sa.Column("cancellation_reason", _legacy_sa.Text),
    _legacy_sa.Column("note", _legacy_sa.Text),
    _legacy_sa.Column("recorded_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.Column("predecessor_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), unique=True),
    _legacy_sa.UniqueConstraint("project_id", "id"),
    _legacy_sa.ForeignKeyConstraint(["project_id", "subject_id"], ["coordination_record_subjects.project_id", "coordination_record_subjects.id"]),
    _legacy_sa.CheckConstraint("field in ('internal_owner','next_action','milestone_impact','deferral')"),
    _legacy_sa.CheckConstraint("decision_type in ('assign_internal_owner','set_next_action','complete_next_action','cancel_next_action','set_milestone_impact','defer_work','resume_work','undo_follow_up_plan')"),
    _legacy_sa.CheckConstraint("valid_coordination_history_actor(recorded_by)"),
)
_legacy_sa.Index("uq_coordination_record_root", _coordination_record_decisions.c.subject_id,
                 _coordination_record_decisions.c.field, unique=True,
                 postgresql_where=_legacy_sa.text("predecessor_id is null"))

_coordination_decision_lineage = _legacy_sa.Table(
    "coordination_decision_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_work_decision_id", _legacy_sa.BigInteger, nullable=False, unique=True),
    _legacy_sa.Column("history_batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id")),
    _legacy_sa.Column("original_content_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("observation_lineage", _legacy_pg.JSONB, nullable=False, server_default=_legacy_sa.text("'{}'")),
    _legacy_sa.CheckConstraint("original_content_sha256 ~ '^[0-9a-f]{64}$'"),
)

_coordination_record_reversals = _legacy_sa.Table(
    "coordination_record_reversals", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_decisions.id"), nullable=False, unique=True),
    _legacy_sa.Column("revision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("project_record_revisions.id"), nullable=False),
    _legacy_sa.Column("recorded_by", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.CheckConstraint("valid_coordination_history_actor(recorded_by)"),
)

_coordination_reversal_lineage = _legacy_sa.Table(
    "coordination_reversal_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("reversal_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("coordination_record_reversals.id"), nullable=False, unique=True),
    _legacy_sa.Column("legacy_statement_reversal_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.UniqueConstraint("legacy_statement_reversal_id", "reversal_id"),
)


_support_scope_lineage = _legacy_sa.Table(
    "support_scope_lineage", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("subject_id", _legacy_pg.UUID(as_uuid=True), _legacy_sa.ForeignKey("coordination_record_subjects.id"), nullable=False),
    _legacy_sa.Column("legacy_dependency_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("field_name", _legacy_sa.Text),
    _legacy_sa.Column("fact_subject_key", _legacy_sa.Text, nullable=False),
    _legacy_sa.UniqueConstraint("project_id", "legacy_dependency_id", "field_name", postgresql_nulls_not_distinct=True),
    _legacy_sa.UniqueConstraint("project_id", "fact_subject_key"),
)

_support_history_receipts = _legacy_sa.Table(
    "support_history_receipts", Base.metadata,
    _legacy_sa.Column("id", _legacy_sa.BigInteger, primary_key=True),
    _legacy_sa.Column("project_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("projects.id"), nullable=False),
    _legacy_sa.Column("batch_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("legacy_history_batches.id")),
    _legacy_sa.Column("scope_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("support_scope_lineage.id"), nullable=False),
    _legacy_sa.Column("legacy_support_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("legacy_evidence_link_id", _legacy_sa.BigInteger, nullable=False),
    _legacy_sa.Column("original_scope_sha256", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("fact_decision_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("fact_decisions.id")),
    _legacy_sa.Column("source_segment_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("source_segments.id")),
    _legacy_sa.Column("outcome", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("reason", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("original_actor", _legacy_sa.Text, nullable=False),
    _legacy_sa.Column("original_time", _legacy_sa.DateTime(timezone=True), nullable=False),
    _legacy_sa.Column("policy_run_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("policy_runs.id")),
    _legacy_sa.Column("policy_approval_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("policy_approvals.id")),
    _legacy_sa.Column("recorded_at", _legacy_sa.DateTime(timezone=True), nullable=False, server_default=_legacy_sa.text("clock_timestamp()")),
    _legacy_sa.Column("predecessor_receipt_id", _legacy_sa.BigInteger, _legacy_sa.ForeignKey("support_history_receipts.id")),
    _legacy_sa.UniqueConstraint("scope_id", "batch_id", "original_scope_sha256", "predecessor_receipt_id", "outcome", postgresql_nulls_not_distinct=True),
    _legacy_sa.CheckConstraint("original_scope_sha256 ~ '^[0-9a-f]{64}$'"),
    _legacy_sa.CheckConstraint("outcome in ('native','retained_compatibility')"),
)
