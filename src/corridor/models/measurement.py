"""Receipts that measure the pipeline rather than change the record.

Pipeline observation, comparison, qualification, acceptance and selection share
``PipelineReceiptMixin`` because they are five stages of the same measurement
and were five diverging column sets before. Shadow execution, cohort receipts
and the evidence-investigation runs are here for the same reason: none of them
is allowed to make anything effective, so grouping them keeps that boundary
visible instead of scattered among the tables that do.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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
    "CohortReceipt",
    "EventCohortReceipt",
    "EvidenceInvestigationCandidateReviewStart",
    "EvidenceInvestigationCaptureContract",
    "EvidenceInvestigationCaptureResult",
    "EvidenceInvestigationEvaluationReceipt",
    "EvidenceInvestigationPacketReceipt",
    "EvidenceInvestigationReviewObservation",
    "EvidenceInvestigationRun",
    "EvidenceInvestigationShadowCase",
    "EvidenceInvestigationShadowExecution",
    "EvidenceInvestigationShadowOutcome",
    "EvidenceInvestigationStepReceipt",
    "ExtractionMeasurementCaseState",
    "PipelineAcceptance",
    "PipelineComparison",
    "PipelineConfiguration",
    "PipelineObservation",
    "PipelineQualification",
    "PipelineQualificationPolicy",
    "PipelineReceiptMixin",
    "PipelineSelection",
    "ShadowProject",
    "ShadowRun",
]


class PipelineQualificationPolicy(Base):
    """Native metric contracts and rules frozen before their observations."""

    __tablename__ = "pipeline_qualification_policies"
    policy_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_sha256: Mapped[str] = mapped_column(String(64))
    policy_text: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class PipelineConfiguration(Base):
    """One immutable full-chain configuration; registration selects nothing."""

    __tablename__ = "pipeline_configurations"
    __table_args__ = (CheckConstraint(
        "configuration_sha256 = encode(sha256(convert_to(configuration_text, 'UTF8')), 'hex')",
        name="ck_pipeline_configurations_digest",
    ),)
    configuration_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    configuration_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineReceiptMixin:
    """Permanent exact bytes, separately indexed by their scope/configuration."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    configuration_sha256: Mapped[str] = mapped_column(ForeignKey("pipeline_configurations.configuration_sha256"))
    scope_sha256: Mapped[str] = mapped_column(String(64))
    receipt_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    receipt_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineObservation(PipelineReceiptMixin, Base):
    """A complete, refused or failed shadow attempt, never an active run declaration."""

    __tablename__ = "pipeline_observations"
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    extraction_run_id: Mapped[int | None] = mapped_column(ForeignKey("extraction_runs.id"))


class PipelineComparison(PipelineReceiptMixin, Base):
    """Repeatability and quality are distinct immutable observations."""

    __tablename__ = "pipeline_comparisons"
    kind: Mapped[str] = mapped_column(String(24))


class PipelineQualification(PipelineReceiptMixin, Base):
    """A numeric, chain-bound gate result; incomplete evidence remains incomplete."""

    __tablename__ = "pipeline_qualifications"
    status: Mapped[str] = mapped_column(String(24))


class PipelineAcceptance(PipelineReceiptMixin, Base):
    """ADR-0095's recorded maintainer acceptance, a selection basis of its own.

    It is never a gate result and carries no status: an incomplete or failed
    qualification stays exactly that in its own receipt. Only the maintainer's
    own principal may append here, and no runtime login holds an insert grant.
    """

    __tablename__ = "pipeline_acceptances"
    implementation_revision: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(Text)


class PipelineSelection(PipelineReceiptMixin, Base):
    """One maintainer's append-only routing selection, with a CAS predecessor.

    Its basis is exactly one of a passing qualification or a recorded
    acceptance (ADR-0095); the two never read alike. This relation does not
    declare an Active Extraction Run, reconcile an old cohort or write accepted
    values. Restoring an older configuration appends another selection; its
    original observations remain intact.
    """

    __tablename__ = "pipeline_selections"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "deployment", "previous_selection_id",
            name="uq_pipeline_selections_successor", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(qualification_id is null) <> (acceptance_id is null)",
            name="ck_pipeline_selections_one_basis",
        ),
    )
    deployment: Mapped[str] = mapped_column(Text)
    qualification_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_qualifications.id"))
    acceptance_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_acceptances.id"))
    previous_selection_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_selections.id"))
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)


class CohortReceipt(Base):
    """The immutable membership of one derived rehearsal cohort.

    Membership is a pure function of a sealed Revision Comparison, the
    verification state in its successor inputs snapshot, one External Party
    name, and one rule version — so re-deriving yields identical members
    and an identical digest, and the receipt can be checked rather than
    trusted. Members are registry identities, never database ids. The
    queue's rehearsal lane reads exactly this set, and mutations outside
    it refuse (#173, #175).
    """

    __tablename__ = "cohort_receipts"
    __table_args__ = (
        UniqueConstraint(
            "revision_comparison_run_id",
            "rule_version",
            "external_org",
            name="uq_cohort_receipts_one_per_rule",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    revision_comparison_run_id: Mapped[int] = mapped_column(
        ForeignKey("revision_comparison_runs.id")
    )
    predecessor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    successor_extraction_run_id: Mapped[int] = mapped_column(BigInteger)
    external_org: Mapped[str] = mapped_column(Text)
    rule_version: Mapped[str] = mapped_column(String(64))
    matcher_version: Mapped[str] = mapped_column(String(64))
    members: Mapped[list] = mapped_column(JSONB)
    member_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EventCohortReceipt(Base):
    """The immutable membership of one derived event cohort.

    A sibling of CohortReceipt for cohorts no Revision Comparison selects
    (docs/sh99-date-rehearsal.md): membership is a pure function of the
    declared Active Runs the rule reads and one rule version, derived from
    the event Candidate stream. Members are document identities — conflict
    refs — never database ids; the lane that reads the set resolves them
    at read time against the pinned input runs, and mutations outside the
    set refuse, exactly as the rehearsal receipt works (#173, #175).
    """

    __tablename__ = "event_cohort_receipts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "rule_version",
            name="uq_event_cohort_receipts_one_per_rule",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    rule_version: Mapped[str] = mapped_column(String(64))
    input_run_ids: Mapped[list] = mapped_column(JSONB)
    members: Mapped[list] = mapped_column(JSONB)
    member_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShadowProject(Base):
    """An isolated shadow environment's project binding, unavailable to the web login."""

    __tablename__ = "shadow_projects"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    environment: Mapped[str] = mapped_column(Text)
    customer: Mapped[str] = mapped_column(Text)
    database_name: Mapped[str] = mapped_column(Text)
    bootstrap_operator: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShadowRun(Base):
    """Frozen native predictions and their custody, never accepted record authority."""

    __tablename__ = "shadow_runs"
    identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("shadow_projects.project_id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    output_sha256: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExtractionMeasurementCaseState(Base):
    """One immutable state in a human-ruling measurement case.

    The stable ``case_key`` groups corrections to the same ruling subject.
    Each correction or reversal appends a successor row; no row here changes
    the Project Record or rewrites an earlier human conclusion.
    """

    __tablename__ = "extraction_measurement_case_states"
    __table_args__ = (
        CheckConstraint(
            "kind in ('candidate_correction', 'source_discrepancy_settlement', "
            "'do_not_add', 'statement_fact_correction', "
            "'statement_scope_correction')",
            name="ck_extraction_measurement_case_states_kind",
        ),
        CheckConstraint(
            "state in ('active', 'reversed')",
            name="ck_extraction_measurement_case_states_state",
        ),
        CheckConstraint(
            "length(trim(case_key)) > 0 and length(trim(recorded_by)) > 0 "
            "and ruling_id > 0",
            name="ck_extraction_measurement_case_states_identity",
        ),
        CheckConstraint(
            "jsonb_typeof(source_identity_json) = 'object' and "
            "source_identity_json ?& array["
            "'candidate_id', 'extraction_run_id', 'documents'] and "
            "jsonb_typeof(source_identity_json -> 'documents') = 'array' and "
            "jsonb_array_length(source_identity_json -> 'documents') > 0",
            name="ck_extraction_measurement_case_states_source",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_json) = 'object' and "
            "jsonb_typeof(expected_json -> 'scoring_rule') = 'string' and "
            "length(trim(expected_json ->> 'scoring_rule')) > 0",
            name="ck_extraction_measurement_case_states_expected",
        ),
        UniqueConstraint(
            "ruling_type",
            "ruling_id",
            name="uq_extraction_measurement_case_states_ruling",
        ),
        Index(
            "uq_extraction_measurement_case_states_root",
            "case_key",
            unique=True,
            postgresql_where=text("predecessor_state_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    case_key: Mapped[str] = mapped_column(String(160), index=True)
    predecessor_state_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_measurement_case_states.id"), unique=True
    )
    kind: Mapped[str] = mapped_column(String(48))
    state: Mapped[str] = mapped_column(String(16))
    ruling_type: Mapped[str] = mapped_column(String(64))
    ruling_id: Mapped[int] = mapped_column(BigInteger)
    source_identity_json: Mapped[dict] = mapped_column(JSONB)
    expected_json: Mapped[dict] = mapped_column(JSONB)
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceInvestigationRun(Base):
    """One immutable terminal attempt by the non-authoritative investigator."""

    __tablename__ = "evidence_investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "terminal_status in ('options_available', 'human_judgment_needed', "
            "'abstained', 'failed')",
            name="ck_evidence_investigation_runs_terminal_status",
        ),
        CheckConstraint(
            "length(candidate_payload_sha256) = 64 and "
            "length(transport_gate_sha256) = 64",
            name="ck_evidence_investigation_runs_hashes",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id")
    )
    terminal_status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    transport_gate_sha256: Mapped[str] = mapped_column(String(64))
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    read_fingerprint: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationStepReceipt(Base):
    """Redacted ordered transport/tool metadata for one investigation."""

    __tablename__ = "evidence_investigation_step_receipts"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_investigation_step_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    opaque_references_json: Mapped[list] = mapped_column(JSONB)
    normalized_arguments_json: Mapped[dict] = mapped_column(JSONB)
    result_summary_json: Mapped[dict] = mapped_column(JSONB)
    usage_json: Mapped[dict] = mapped_column(JSONB)
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    request_sha256: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))


class EvidenceInvestigationPacketReceipt(Base):
    """Validated structured packet; explicitly never Ledger authority."""

    __tablename__ = "evidence_investigation_packet_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), unique=True
    )
    packet_json: Mapped[dict] = mapped_column(JSONB)
    validator_outcome: Mapped[str] = mapped_column(String(32))
    packet_sha256: Mapped[str] = mapped_column(String(64))
    non_authoritative: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )


class EvidenceInvestigationShadowCase(Base):
    """Exact prospective model-visible case frozen before human review."""

    __tablename__ = "evidence_investigation_shadow_cases"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "read_fingerprint",
            "model",
            "prompt_version",
            "prompt_sha256",
            "adapter_contract_version",
            "tool_contract_version",
            name="uq_evidence_investigation_shadow_case_identity",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id")
    )
    candidate_payload_sha256: Mapped[str] = mapped_column(String(64))
    read_fingerprint: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter_contract_version: Mapped[str | None] = mapped_column(String(128))
    tool_contract_version: Mapped[str | None] = mapped_column(String(128))
    transport_gate_sha256: Mapped[str | None] = mapped_column(String(64))
    budget_json: Mapped[dict | None] = mapped_column(JSONB)
    case_json: Mapped[dict] = mapped_column(JSONB)
    registered_evidence_json: Mapped[list] = mapped_column(JSONB)
    option_population_json: Mapped[dict] = mapped_column(JSONB)
    option_population_sha256: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationShadowExecution(Base):
    """Immutable association of one frozen case with its later terminal run."""

    __tablename__ = "evidence_investigation_shadow_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), unique=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_runs.id"), unique=True
    )
    execution_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceInvestigationReviewObservation(Base):
    """Server-observed review boundary, separate from runtime/waiting time."""

    __tablename__ = "evidence_investigation_review_observations"
    __table_args__ = (
        UniqueConstraint(
            "shadow_case_id", "boundary", name="uq_shadow_review_boundary"
        ),
        CheckConstraint(
            "boundary in ('start', 'end')", name="ck_shadow_review_boundary"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), index=True
    )
    boundary: Mapped[str] = mapped_column(String(16))
    principal: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationCandidateReviewStart(Base):
    """First ordinary coordinator review observed before any shadow freeze."""

    __tablename__ = "evidence_investigation_candidate_review_starts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id"), unique=True, index=True
    )
    principal: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationShadowOutcome(Base):
    """Later independent human label associated without touching the run."""

    __tablename__ = "evidence_investigation_shadow_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id"), unique=True
    )
    human_outcome_identity: Mapped[str] = mapped_column(String(64), unique=True)
    candidate_disposition: Mapped[str | None] = mapped_column(String(32))
    scope_mode: Mapped[str | None] = mapped_column(String(32))
    selected_dependency_ids_json: Mapped[list] = mapped_column(JSONB)
    correction: Mapped[bool] = mapped_column(Boolean)
    undo: Mapped[bool] = mapped_column(Boolean)
    unresolved: Mapped[bool] = mapped_column(Boolean)
    outcome_identities_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[list] = mapped_column(JSONB)
    review_seconds: Mapped[float | None] = mapped_column(Float)
    outcome_sha256: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationEvaluationReceipt(Base):
    """Versioned, immutable deterministic shadow evaluation receipt."""

    __tablename__ = "evidence_investigation_evaluation_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    evaluation_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    selected_run_ids_json: Mapped[list] = mapped_column(JSONB)
    identity_json: Mapped[dict] = mapped_column(JSONB)
    metrics_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[dict] = mapped_column(JSONB)
    human_scores_json: Mapped[dict] = mapped_column(JSONB)
    gates_json: Mapped[dict] = mapped_column(JSONB)
    limitations_json: Mapped[list] = mapped_column(JSONB)
    summary_markdown: Mapped[str] = mapped_column(Text)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvidenceInvestigationCaptureContract(Base):
    """Declared gate-7 contract for delayed cutoff-correct outcome capture.

    One approved observation contract names an exact frozen cohort, the sealed
    configuration identities it may associate, the observation window and its
    intended cutoff, the protection end, and the retained-history coverage the
    reconstruction is allowed to trust.  It is content-addressed and append-only:
    a different cutoff, membership, or identity is a different contract, never a
    rewrite of this one, and an incomplete or unapproved declaration is never
    written at all (the capture stays disabled).
    """

    __tablename__ = "evidence_investigation_capture_contracts"
    __table_args__ = (
        CheckConstraint(
            "missing_label_policy = 'remain_missing'",
            name="ck_capture_contract_missing_label_policy",
        ),
        CheckConstraint(
            "length(trim(declared_by)) > 0",
            name="ck_capture_contract_actor",
        ),
        CheckConstraint(
            "window_start <= cutoff_at",
            name="ck_capture_contract_window_before_cutoff",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    cohort_id: Mapped[str] = mapped_column(String(128))
    contract_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    adapter_contract_version: Mapped[str] = mapped_column(String(128))
    tool_contract_version: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(128))
    baseline_identity: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    protection_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history_retained_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    missing_label_policy: Mapped[str] = mapped_column(String(32))
    member_case_public_ids_json: Mapped[list] = mapped_column(JSONB)
    contract_json: Mapped[dict] = mapped_column(JSONB)
    declared_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceInvestigationCaptureResult(Base):
    """One cutoff-correct association of a frozen case's independent outcome.

    The association records the intended cutoff and the actual execution time
    separately, binds the exact project, frozen case, execution run, human
    outcome, and source-receipt identities, and states its completeness.  It
    never rewrites the frozen case, its run, or the immutable one-time capture,
    and a reconstruction that retained history cannot support exactly is kept as
    ``incomplete`` with its reason rather than labelled as cutoff-time truth.
    """

    __tablename__ = "evidence_investigation_capture_results"
    __table_args__ = (
        UniqueConstraint(
            "capture_contract_id",
            "shadow_case_id",
            name="uq_capture_result_case",
        ),
        CheckConstraint(
            "completeness in ('complete', 'incomplete')",
            name="ck_capture_result_completeness",
        ),
        CheckConstraint(
            "(completeness = 'complete' and incomplete_reason is null) or "
            "(completeness = 'incomplete' and incomplete_reason is not null)",
            name="ck_capture_result_incomplete_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    capture_contract_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_capture_contracts.id"), index=True
    )
    shadow_case_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_investigation_shadow_cases.id")
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_investigation_runs.id")
    )
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completeness: Mapped[str] = mapped_column(String(16))
    incomplete_reason: Mapped[str | None] = mapped_column(String(64))
    candidate_disposition: Mapped[str | None] = mapped_column(String(32))
    scope_mode: Mapped[str | None] = mapped_column(String(32))
    selected_dependency_ids_json: Mapped[list] = mapped_column(JSONB)
    correction: Mapped[bool] = mapped_column(Boolean)
    undo: Mapped[bool] = mapped_column(Boolean)
    unresolved: Mapped[bool] = mapped_column(Boolean)
    human_outcome_identity: Mapped[str | None] = mapped_column(String(64))
    outcome_identities_json: Mapped[dict] = mapped_column(JSONB)
    strata_json: Mapped[list] = mapped_column(JSONB)
    review_seconds: Mapped[float | None] = mapped_column(Float)
    association_sha256: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
