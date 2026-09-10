"""Model-assisted explanations: the configuration, and one request per run.

Each family is a configuration row plus a request row, and every request keeps
``ClassBRetentionMixin``'s retention columns because the request payload is
customer content with a disposal obligation. The five families repeat that
shape deliberately: they were one generic ``llm_request`` table first, and a
single table could not carry per-family inputs without a JSON grab bag that no
constraint could check.

What the five did not need to repeat was the authorization to spend (#811).
Each configuration used to carry the same nine columns and nine CHECKs — the
model, the input, output and time limits, one request, no retry, the
retention class, the observation context and the declaring actor — five times
over. ``SpendAuthorization`` is that declaration once: one immutable,
attributable row naming one project and one permitted operation, and each
family configuration references it by id and keeps only what is its own (the
prompt it runs, the source scope a summary reads). The reference is a composite
foreign key on ``(authorization_id, project_id, operation)``, so a declaration
made for an intake draft cannot back a Coordination Summary configuration, and
a declaration for one project cannot back a configuration in another: the
scope is unrepresentable rather than merely unchecked. The generic request
table stays rejected; only the authorization was ever common.
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
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from corridor.models.base import Base


__all__ = [
    "ClassBRetentionMixin",
    "SPEND_OPERATIONS",
    "SpendAuthorization",
    "SpendAuthorizedConfiguration",
    "CoordinationSummaryConfiguration",
    "CoordinationSummaryRequest",
    "ExtractionFailureDiagnosisConfiguration",
    "ExtractionFailureDiagnosisRequest",
    "ProductionRunExplanationConfiguration",
    "ProductionRunExplanationRequest",
    "RevisionChangeExplanationConfiguration",
    "RevisionChangeExplanationRequest",
    "SourceIntakeDraftConfiguration",
    "SourceIntakeDraftRequest",
]


class ClassBRetentionMixin:
    """Explicit TTL state shared only by intermediary assistant receipts."""

    retention_class: Mapped[str] = mapped_column(
        String(16), default="class_b", server_default="class_b"
    )
    retention_content_sha256: Mapped[str | None] = mapped_column(String(64))
    retention_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


# The one operation each family configuration is permitted to spend on. A
# declaration names exactly one, and the composite foreign key on every family
# table holds the configuration to the declaration that names its own.
SPEND_OPERATIONS = (
    "coordination_summary",
    "production_run_explanation",
    "extraction_failure_diagnosis",
    "revision_change_explanation",
    "source_intake_draft",
)
_SPEND_OPERATIONS_SQL = ", ".join(f"'{operation}'" for operation in SPEND_OPERATIONS)


class SpendAuthorization(Base):
    """One immutable, attributable authorization to spend model budget (#811).

    A row names the declaring actor, the project, the one operation it
    permits, the model, the input, output and time limits, one request, no
    retry, the retention class, the observation context, and when it took
    effect. Rows are append-only: a changed bound is another declaration, and
    two independently declared acts stay two rows however alike their limits.
    Nothing here is a wallet or a budget account; the row is the authority a
    family configuration must reference before a client is ever constructed.
    """

    __tablename__ = "spend_authorizations"
    __table_args__ = (
        CheckConstraint(
            f"operation in ({_SPEND_OPERATIONS_SQL})",
            name="ck_spend_authorization_operation",
        ),
        CheckConstraint("length(trim(model)) > 0", name="ck_spend_authorization_model"),
        CheckConstraint(
            "max_input_tokens between 1 and 200000",
            name="ck_spend_authorization_input_budget",
        ),
        CheckConstraint(
            "max_output_tokens between 1 and 20000",
            name="ck_spend_authorization_output_budget",
        ),
        CheckConstraint(
            "timeout_seconds between 1 and 600", name="ck_spend_authorization_timeout"
        ),
        CheckConstraint("max_requests = 1", name="ck_spend_authorization_one_request"),
        CheckConstraint("retry_policy = 'none'", name="ck_spend_authorization_no_retry"),
        # The database has always admitted the pre-#355 indefinite class so
        # those rows can be carried; only `class_b_30_days` authorizes a request.
        CheckConstraint(
            "retention_policy in ('retained_indefinitely', 'class_b_30_days')",
            name="ck_spend_authorization_retention",
        ),
        CheckConstraint(
            "length(trim(observation_context)) > 0",
            name="ck_spend_authorization_context",
        ),
        CheckConstraint(
            "length(trim(declared_by)) > 0", name="ck_spend_authorization_actor"
        ),
        # The target of every family's composite reference: a configuration
        # can only name a declaration whose project and operation are its own.
        UniqueConstraint(
            "id", "project_id", "operation", name="uq_spend_authorization_scope"
        ),
        Index("ix_spend_authorizations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    operation: Mapped[str] = mapped_column(String(48))
    model: Mapped[str] = mapped_column(String(128))
    max_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    max_requests: Mapped[int] = mapped_column(Integer)
    retry_policy: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(64))
    observation_context: Mapped[str] = mapped_column(String(128))
    declared_by: Mapped[str] = mapped_column(String(128))
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SpendAuthorizedConfiguration:
    """The declared bounds a family configuration reads through its reference.

    Every reader of a configuration's model, limits and retention — the
    bounded loop's budget check, the adapter factories, the receipt writers —
    reads the declaration the configuration names. Nothing here is writable:
    the declaration is immutable and the reference is the configuration's
    identity, not a copy of the declaration's values.
    """

    authorization: Mapped[SpendAuthorization]

    @property
    def model(self) -> str:
        return self.authorization.model

    @property
    def max_input_tokens(self) -> int:
        return self.authorization.max_input_tokens

    @property
    def max_output_tokens(self) -> int:
        return self.authorization.max_output_tokens

    @property
    def timeout_seconds(self) -> int:
        return self.authorization.timeout_seconds

    @property
    def max_requests(self) -> int:
        return self.authorization.max_requests

    @property
    def retry_policy(self) -> str:
        return self.authorization.retry_policy

    @property
    def retention_policy(self) -> str:
        return self.authorization.retention_policy

    @property
    def observation_context(self) -> str:
        return self.authorization.observation_context


def _authorization_reference(table: str, prefix: str, operation: str) -> tuple:
    """The composite reference every family table carries, named for its family."""
    return (
        CheckConstraint(f"operation = '{operation}'", name=f"ck_{prefix}_operation"),
        UniqueConstraint("authorization_id", name=f"uq_{table}_authorization"),
        ForeignKeyConstraint(
            ["authorization_id", "project_id", "operation"],
            [
                "spend_authorizations.id",
                "spend_authorizations.project_id",
                "spend_authorizations.operation",
            ],
            name=f"fk_{table}_authorization",
        ),
    )


class CoordinationSummaryConfiguration(SpendAuthorizedConfiguration, Base):
    """One explicit, server-owned authorization for bounded summary drafting.

    Unlike ordinary report reading, a Coordination Summary can spend model
    budget.  Therefore no supported default exists: an attributable project
    declaration names every input, model, time, retry, retention, and
    observation bound before a request is allowed.  Rows are append-only so a
    retained draft always names the rules under which it was obtained.
    """

    __tablename__ = "coordination_summary_configurations"
    __table_args__ = (
        CheckConstraint("source_scope in ('all_sources', 'documents_only')", name="ck_summary_config_source_scope"),
        CheckConstraint("length(trim(prompt_version)) > 0", name="ck_summary_config_prompt"),
        *_authorization_reference(
            "coordination_summary_configurations", "summary_config", "coordination_summary"
        ),
        Index("ix_coordination_summary_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    source_scope: Mapped[str] = mapped_column(String(32))
    authorization_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(48), default="coordination_summary")
    prompt_version: Mapped[str] = mapped_column(String(128))
    authorization: Mapped[SpendAuthorization] = relationship(lazy="joined")


class CoordinationSummaryRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative draft attempt."""

    __tablename__ = "coordination_summary_requests"
    __table_args__ = (
        UniqueConstraint("configuration_id", "reading_sha256", name="uq_summary_request_reading"),
        CheckConstraint(
            "status in ('completed', 'empty_input', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused')",
            name="ck_summary_request_status",
        ),
        CheckConstraint("reading_sha256 ~ '^[0-9a-f]{64}$'", name="ck_summary_request_reading_sha"),
        CheckConstraint("length(trim(requested_by)) > 0", name="ck_summary_request_actor"),
        Index("ix_coordination_summary_requests_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    configuration_id: Mapped[int] = mapped_column(ForeignKey("coordination_summary_configurations.id"))
    requested_by: Mapped[str] = mapped_column(String(128))
    reading_sha256: Mapped[str] = mapped_column(String(64))
    project_reading_json: Mapped[dict] = mapped_column(JSONB)
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(32))
    statement_publication_fingerprint: Mapped[str] = mapped_column(String(64))
    provenance_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    summary_markdown: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductionRunExplanationConfiguration(SpendAuthorizedConfiguration, Base):
    """One explicit, server-owned authorization for a bounded run explanation.

    Explaining competing Current Production Runs can spend model budget, so it
    has no supported default: an attributable technical-operations declaration
    names the model, prompt, and the time, input, output, retry, retention, and
    observation bounds before any explanation request is allowed. Rows are
    append-only so a retained explanation always names the rules it ran under.
    """

    __tablename__ = "production_run_explanation_configurations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_run_explanation_config_prompt"
        ),
        *_authorization_reference(
            "production_run_explanation_configurations", "run_explanation_config", "production_run_explanation"
        ),
        Index("ix_run_explanation_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    authorization_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(48), default="production_run_explanation")
    prompt_version: Mapped[str] = mapped_column(String(128))
    authorization: Mapped[SpendAuthorization] = relationship(lazy="joined")


class ProductionRunExplanationRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative run explanation.

    The receipt binds the exact competing run identities it explained, retains
    the frozen immutable snapshots it read (the context a human checks each
    explanation against), and stores only the validated explanation plus a
    redacted execution lineage — never raw source text, a chain of thought, or
    any claim of human decision authorship. It never declares a run.
    """

    __tablename__ = "production_run_explanation_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "comparison_sha256",
            name="uq_run_explanation_request_comparison",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_run_explanation_request_status",
        ),
        CheckConstraint(
            "comparison_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_run_explanation_request_comparison_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_run_explanation_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_run_explanation_request_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_run_explanation_request_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_run_explanation_request_non_auth"),
        Index("ix_run_explanation_requests_project_id", "project_id"),
        Index("ix_run_explanation_requests_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("production_run_explanation_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    comparison_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    competing_run_ids_json: Mapped[list] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen immutable run snapshots this explanation read: the checkable
    # context, not a raw source-wide trace.
    comparison_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative explanation, or null when none was kept.
    explanation_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionFailureDiagnosisConfiguration(SpendAuthorizedConfiguration, Base):
    """One explicit, server-owned authorization for a bounded failure diagnosis.

    Diagnosing an unreadable, no-matrix, quarantined, or otherwise failed
    Extraction Run can spend model budget, so it has no supported default: an
    attributable technical-operations declaration names the model, prompt, and
    the time, input, output, retry, retention, and observation bounds before any
    diagnosis request is allowed. Rows are append-only so a retained diagnosis
    always names the rules it ran under (ADR-0011, ADR-0034).
    """

    __tablename__ = "extraction_failure_diagnosis_configurations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_failure_diagnosis_config_prompt",
        ),
        *_authorization_reference(
            "extraction_failure_diagnosis_configurations", "failure_diagnosis_config", "extraction_failure_diagnosis"
        ),
        Index("ix_failure_diagnosis_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    authorization_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(48), default="extraction_failure_diagnosis")
    prompt_version: Mapped[str] = mapped_column(String(128))
    authorization: Mapped[SpendAuthorization] = relationship(lazy="joined")


class ExtractionFailureDiagnosisRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative failure diagnosis.

    The receipt binds the exact failed Extraction Run it diagnosed, retains the
    frozen deterministic failure facts and permitted-page metadata it read (the
    context a human checks the diagnosis against), and stores only the validated
    diagnosis plus a redacted execution lineage — never raw source-wide text, a
    chain of thought, or any claim of human decision authorship. It never retries
    extraction, relabels the failure, or removes a quarantine; the original
    Extraction Run outcome, error, and receipt are untouched (ADR-0011).
    """

    __tablename__ = "extraction_failure_diagnosis_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "input_sha256",
            name="uq_failure_diagnosis_request_input",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_failure_diagnosis_request_status",
        ),
        CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_failure_diagnosis_request_input_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_failure_diagnosis_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0",
            name="ck_failure_diagnosis_request_actor",
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_failure_diagnosis_request_adapter"
        ),
        CheckConstraint(
            "non_authoritative", name="ck_failure_diagnosis_request_non_auth"
        ),
        Index("ix_failure_diagnosis_requests_project_id", "project_id"),
        Index("ix_failure_diagnosis_requests_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int] = mapped_column(ForeignKey("extraction_runs.id"))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_failure_diagnosis_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    input_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen deterministic failure facts and permitted-page metadata this
    # diagnosis read: the checkable context, not a raw source-wide trace.
    source_context_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative diagnosis, or null when none was kept.
    diagnosis_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RevisionChangeExplanationConfiguration(SpendAuthorizedConfiguration, Base):
    """One explicit, server-owned authorization for a bounded revision-change
    explanation (#360).

    Explaining a verified newer-document change can spend model budget, so it
    has no supported default: an attributable technical-operations declaration
    names the model, prompt, and the time, input, output, retry, retention, and
    observation bounds before any explanation request is allowed. Rows are
    append-only so a retained explanation always names the rules it ran under.
    """

    __tablename__ = "revision_change_explanation_configurations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_rev_change_expl_cfg_prompt"
        ),
        *_authorization_reference(
            "revision_change_explanation_configurations", "rev_change_expl_cfg", "revision_change_explanation"
        ),
        Index("ix_rev_change_expl_cfg_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    authorization_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(48), default="revision_change_explanation")
    prompt_version: Mapped[str] = mapped_column(String(128))
    authorization: Mapped[SpendAuthorization] = relationship(lazy="joined")


class RevisionChangeExplanationRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative revision-change
    explanation (#360).

    The receipt binds the exact affected Constraint and the integrity-verified
    Revision Comparison finding it explained, retains the frozen sanitized
    comparison it read (the context a coordinator checks the explanation
    against), and stores only the validated explanation plus a redacted
    execution lineage — never raw source text, a chain of thought, or any claim
    of human decision authorship. It never updates support or settles anything.
    """

    __tablename__ = "revision_change_explanation_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "comparison_sha256",
            name="uq_rev_change_expl_req_comparison",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_rev_change_expl_req_status",
        ),
        CheckConstraint(
            "comparison_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_comparison_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_rev_change_expl_req_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_rev_change_expl_req_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_rev_change_expl_req_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_rev_change_expl_req_non_auth"),
        Index("ix_rev_change_expl_req_project_id", "project_id"),
        Index("ix_rev_change_expl_req_dependency_id", "dependency_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    comparison_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    finding_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_findings.id")
    )
    finding_state: Mapped[str] = mapped_column(String(32))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("revision_change_explanation_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    comparison_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen, sanitized comparison finding this explanation read: the
    # checkable context, not a raw source-wide trace.
    comparison_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative explanation, or null when none was kept.
    explanation_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceIntakeDraftConfiguration(SpendAuthorizedConfiguration, Base):
    """One explicit, server-owned authorization for a bounded intake draft (#362).

    Drafting source-bound intake metadata and replacement proposals can spend
    model budget, so it has no supported default: an attributable coordination
    declaration names the model, prompt, and the time, input, output, retry,
    retention, and observation bounds before any draft request is allowed. Rows
    are append-only so a retained draft always names the rules it ran under.
    """

    __tablename__ = "source_intake_draft_configurations"
    __table_args__ = (
        CheckConstraint(
            "length(trim(prompt_version)) > 0", name="ck_intake_draft_config_prompt"
        ),
        *_authorization_reference(
            "source_intake_draft_configurations", "intake_draft_config", "source_intake_draft"
        ),
        Index("ix_intake_draft_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    authorization_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(48), default="source_intake_draft")
    prompt_version: Mapped[str] = mapped_column(String(128))
    authorization: Mapped[SpendAuthorization] = relationship(lazy="joined")


class SourceIntakeDraftRequest(ClassBRetentionMixin, Base):
    """Immutable receipt for one bounded, non-authoritative intake draft (#362).

    The receipt binds the exact staged bytes it read (by their own hash, never a
    registered Document id — a draft identity stays distinct from a registered
    one), retains the frozen readable surface it was checked against (the
    permitted page text and the registry identities a curator verifies each
    suggestion against), and stores only the validated proposals plus a redacted
    execution lineage — never raw source-wide text, a chain of thought, or any
    claim of human decision authorship. It registers no Document and declares no
    Supersession.
    """

    __tablename__ = "source_intake_draft_requests"
    __table_args__ = (
        UniqueConstraint(
            "configuration_id",
            "source_sha256",
            name="uq_intake_draft_request_source",
        ),
        CheckConstraint(
            "status in ('completed', 'budget_exhausted', 'timeout', "
            "'transport_failure', 'validation_refused', 'stale_input')",
            name="ck_intake_draft_request_status",
        ),
        CheckConstraint(
            "staged_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_staged_sha",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_source_sha",
        ),
        CheckConstraint(
            "state_token ~ '^[0-9a-f]{64}$'",
            name="ck_intake_draft_request_state_token",
        ),
        CheckConstraint(
            "length(trim(requested_by)) > 0", name="ck_intake_draft_request_actor"
        ),
        CheckConstraint(
            "length(trim(adapter)) > 0", name="ck_intake_draft_request_adapter"
        ),
        CheckConstraint("non_authoritative", name="ck_intake_draft_request_non_auth"),
        Index("ix_intake_draft_requests_project_id", "project_id"),
        Index("ix_intake_draft_requests_staged_sha256", "staged_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The staged byte identity this draft read — the draft's own identity, kept
    # deliberately separate from any registered Document.
    staged_sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(Text)
    declared_doc_type: Mapped[str] = mapped_column(String(64))
    configuration_id: Mapped[int] = mapped_column(
        ForeignKey("source_intake_draft_configurations.id")
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    source_sha256: Mapped[str] = mapped_column(String(64))
    state_token: Mapped[str] = mapped_column(String(64))
    permitted_pages_json: Mapped[list] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # The frozen readable surface this draft read: permitted page text and the
    # registry identities, the context a human checks each proposal against.
    source_json: Mapped[dict] = mapped_column(JSONB)
    # The validated, non-authoritative proposals, or null when none were kept.
    proposals_json: Mapped[dict | None] = mapped_column(JSONB)
    # Redacted transport metadata (hashes, usage, timing) — no prompt, response,
    # or chain-of-thought text.
    execution_lineage_json: Mapped[dict | None] = mapped_column(JSONB)
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
