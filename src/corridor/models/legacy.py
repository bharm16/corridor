"""The tables ADR-0081 freezes: no new capability may be built on these.

Exactly the relations stage 4's exit criterion names, together with the
compatibility classes the stage 2 retention inventory records as keyed to a
legacy row, together with the satellites that cannot exist without one of them:
a statement's timing rows, its scope memberships and its evidence links retire
when the statement does. They are still mapped because the legacy readers still run for a
project that has not adopted a baseline; for a project that has, PostgreSQL
refuses every accepted-value write here (``operating_mode.py``, #520). New work
writes the spine. ``tests/test_architecture.py`` holds the consumer list exact
in both directions, so a module that starts reading one of these fails the
build.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from corridor.models.base import (
    Base,
    CANDIDATE_KINDS,
    CANDIDATE_STATES,
    DEP_TYPES,
    EVENT_SOURCE_KINDS,
    EVENT_TYPES,
    RESOLUTION_STRATEGIES,
    STATEMENT_ATTRIBUTION_STATES,
    STATEMENT_SCOPE_MODES,
    SUPPORT_ROLES,
    TIMING_CHANGE_DIRECTIONS,
    _enum,
    _statement_attribution_state,
)


__all__ = [
    "Candidate",
    "CommitmentLineage",
    "CommitmentScopeDecision",
    "CommitmentScopeMembership",
    "Dependency",
    "DependencyDismissal",
    "DependencyEvent",
    "DependencyEventEvidence",
    "DependencyEventMigrationReceipt",
    "DependencyEventScope",
    "DependencyEventScopeDecision",
    "DependencyEventTiming",
    "DisputeHistoryResolution",
    "DisputeSettlement",
    "ExternalPartyStatement",
    "OperativeSupport",
    "RetiredDependencyStatus",
    "StatementEvidence",
    "StatementTimingRecord",
    "WorkDecision",
]


class WorkDecision(Base):
    """One appended project decision about exactly one Coordination Subject.

    A Work Decision proves only what the project decided and when
    (ADR-0025): it is not Evidence, states nothing about what a document or
    External Party said, and can never set Criticality, a Resolution
    Strategy, Ready, or an External Party's status or commitment. The typed
    receipt is the record — the audit log carries only a pointer event.
    One linear chain per Dependency or Commitment Lineage and field; their
    current values are projections of the chain tails.  A statement-level
    plan belongs to durable factual lineage, never to a Dependency selected
    later by a scope decision (ADR-0038).
    """

    __tablename__ = "work_decisions"
    __table_args__ = (
        UniqueConstraint(
            "dependency_id", "id", name="uq_work_decisions_dependency_id_id"
        ),
        UniqueConstraint(
            "commitment_lineage_id",
            "id",
            name="uq_work_decisions_commitment_lineage_id_id",
        ),
        ForeignKeyConstraint(
            ["dependency_id", "predecessor_decision_id"],
            ["work_decisions.dependency_id", "work_decisions.id"],
            name="fk_work_decisions_predecessor",
        ),
        ForeignKeyConstraint(
            ["commitment_lineage_id", "predecessor_decision_id"],
            ["work_decisions.commitment_lineage_id", "work_decisions.id"],
            name="fk_work_decisions_commitment_lineage_predecessor",
        ),
        UniqueConstraint(
            "predecessor_decision_id", name="uq_work_decisions_predecessor"
        ),
        CheckConstraint(
            "(dependency_id is not null and commitment_lineage_id is null) "
            "or (dependency_id is null and commitment_lineage_id is not null)",
            name="ck_work_decisions_exactly_one_subject",
        ),
        CheckConstraint(
            "field in ('internal_owner', 'next_action', 'milestone_impact', 'deferral')",
            name="ck_work_decisions_field",
        ),
        CheckConstraint(
            "(field <> 'deferral' and deferral_reason is null and deferral_return_date is null) "
            "or (field = 'deferral' and ((after_value is null and deferral_reason is null "
            "and deferral_return_date is null) or (after_value is not null "
            "and deferral_reason in ('waiting_for_information', 'waiting_for_external_party', "
            "'assigned_to_someone_else') and deferral_return_date is not null)))",
            name="ck_work_decisions_deferral_shape",
        ),
        Index(
            "uq_work_decisions_one_root",
            "dependency_id",
            "field",
            unique=True,
            postgresql_where=text("predecessor_decision_id is null"),
        ),
        Index(
            "uq_work_decisions_commitment_lineage_one_root",
            "commitment_lineage_id",
            "field",
            unique=True,
            postgresql_where=text("predecessor_decision_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    decision_type: Mapped[str] = mapped_column(String(32))
    field: Mapped[str] = mapped_column(String(32))
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    predecessor_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    action_due_date_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    no_follow_up_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    cancellation_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    note: Mapped[str | None] = mapped_column(Text, server_default=text("null"))
    # A deferral is a distinct Work Decision: unlike an unknown Action Due
    # Date, it names both why immediate attention can wait and exactly when
    # Corridor must put the item back in front of the coordinator.
    deferral_reason: Mapped[str | None] = mapped_column(
        String(64), server_default=text("null")
    )
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
    # The factual state a future return condition was set against.  These
    # immutable observations let the work list reopen when the party's
    # statement, its scope, or its Milestone Impact changes.
    observed_statement_event_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_scope_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_milestone_impact_decision_id: Mapped[int | None] = mapped_column(
        BigInteger
    )


class Dependency(Base):
    __tablename__ = "dependencies"
    __table_args__ = (
        UniqueConstraint("project_id", "ref_code"),
        UniqueConstraint("project_id", "id", name="uq_dependencies_project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # Minted by us, unique per project. Deliberately not the source's own
    # identifier: NHHIP's 2/13/2026 matrix carries two different conflicts
    # both labelled FOC14-69 — same owner, different locations — so source
    # IDs cannot identify a ledger record.
    ref_code: Mapped[str] = mapped_column(String(32))
    # The identifier the source used, kept for display and merge matching.
    # Not assumed unique.
    source_ref: Mapped[str | None] = mapped_column(String(64))
    dep_type: Mapped[str] = mapped_column(_enum(*DEP_TYPES, name="dep_type"))
    title: Mapped[str] = mapped_column(Text)
    location_desc: Mapped[str | None] = mapped_column(Text)
    # Stationing is the most discriminating signal in merge ranking because
    # it is numeric: "245+00" and "445+00" are near-identical as strings and
    # two thousand feet apart on the ground.
    station_from: Mapped[str | None] = mapped_column(String(32))
    station_to: Mapped[str | None] = mapped_column(String(32))
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    milestone_id: Mapped[int | None] = mapped_column(ForeignKey("milestones.id"))
    milestone_registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("milestone_registrations.id"),
        deferred=True,
        server_default=text("null"),
    )
    external_contact: Mapped[str | None] = mapped_column(Text)
    internal_owner: Mapped[str | None] = mapped_column(Text)
    # The step the project decided must happen next, and either the date the
    # project set for it or the structured reason the date remains unknown —
    # all projections of Work Decision receipts (ADR-0025/0038). Neither is a
    # document claim.
    next_action: Mapped[str | None] = mapped_column(Text)
    action_due_date: Mapped[date | None] = mapped_column(Date)
    action_due_date_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
    # What the document says is to be done about the conflict, as an
    # adjudicated conclusion drawn from its Assertions. Criticality is read
    # off this rather than stored beside it (ADR-0009).
    #
    # Null means no document asserted a strategy, and most of the corpus is
    # null on purpose: an inventory records that conflicts exist without
    # ever saying how they resolve, so Project A's 3,235 rows and SH 99's
    # 1,401 assert nothing here. Only SR 789 prints a
    # `Recommended Conflict Resolution` column. Nothing defaults it,
    # because a default would be this field claiming something no document
    # said — the failure ADR-0007 was written about and then committed.
    resolution_strategy: Mapped[str | None] = mapped_column(
        _enum(*RESOLUTION_STRATEGIES, name="resolution_strategy")
    )
    committed_date: Mapped[date | None] = mapped_column(Date)
    need_date: Mapped[date | None] = mapped_column(Date)
    # A source-proven selector, not a judgment.  ADR-0052 uses it with the
    # resolution method to select the standard documentation fields that are
    # required at read time.  ``reimbursable`` is the one currently modeled
    # value; unknown or absent source wording deliberately selects nothing.
    # Deferred like milestone_registration_id: migration rehearsals load
    # Dependency rows on databases pinned before this column existed.
    cost_responsibility: Mapped[str | None] = mapped_column(
        String(64), deferred=True
    )
    # Free text in v0: what closes this. A reviewer judges whether a given
    # piece of evidence meets it. Promoting this to a typed taxonomy waits
    # until real adjudications show what closure documents look like
    # (ADR-0002).
    evidence_required: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Set when a reviewer dismisses this record as junk. A projection of
    # the newest DependencyDismissal, which is the authority — readers
    # filter on this rather than joining, the same way they read the
    # projected Committed Date (ADR-0032). Never a delete.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CommitmentLineage(Base):
    """The durable identity of one accepted External Party commitment.

    A correction to attribution or timing appends a successor statement, but
    it does not create another Coordination Plan.  The projections here are
    therefore deliberately internal project decisions, never External Party
    facts or substitutions for the statement receipts (ADR-0038).
    """

    __tablename__ = "commitment_lineages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    internal_owner: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    action_due_date: Mapped[date | None] = mapped_column(Date)
    action_due_date_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_reason: Mapped[str | None] = mapped_column(String(64))
    deferral_return_date: Mapped[date | None] = mapped_column(Date)
    milestone_impact: Mapped[str | None] = mapped_column(String(32))
    milestone_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), default=list, server_default="{}"
    )
    # A factual successor changes the fact to which a plan responds.  It
    # preserves the plan history while refusing to silently call it current.
    plan_needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExternalPartyStatement(Base):
    """One attributable External Party Statement, with scope kept separately.

    A statement may concern no known Dependency, one Dependency, or several.
    Commitment Scope membership is the only authority for that relationship.
    Timings live in their own rows so a month or approximate phrase never has
    to pretend to be one day.
    """

    __tablename__ = "dependency_events"
    __table_args__ = (
        UniqueConstraint(
            "supersedes_event_id",
            name="uq_dependency_events_supersedes_event",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(_enum(*EVENT_TYPES, name="event_type"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # Only accepted Commitments and Committed Date Changes carry this
    # durable subject identity.  Closure is a distinct External Party fact;
    # it cannot become a Coordination Subject by borrowing this key.
    commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    # A closure is never a Coordination Subject, but it must name the one
    # Commitment Lineage whose External Party fact it establishes as closed.
    # Matching only on affected party would wrongly close every unresolved
    # party-level Commitment.
    closes_commitment_lineage_id: Mapped[int | None] = mapped_column(
        ForeignKey("commitment_lineages.id"), server_default=text("null")
    )
    supersedes_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_events.id"), server_default=text("null")
    )
    affected_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    # The resolved organization who made the statement.  The raw wording is
    # retained separately because source spelling is provenance too.
    stated_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    attribution_state: Mapped[str] = mapped_column(
        _enum(
            *STATEMENT_ATTRIBUTION_STATES,
            name="statement_attribution_state",
        ),
        default=_statement_attribution_state,
        server_default="unresolved",
    )
    scope_mode: Mapped[str] = mapped_column(
        _enum(*STATEMENT_SCOPE_MODES, name="statement_scope_mode"),
        default="unknown",
        server_default="unknown",
    )
    timing_direction: Mapped[str | None] = mapped_column(
        _enum(*TIMING_CHANGE_DIRECTIONS, name="timing_change_direction")
    )
    # A source is declared rather than inferred from an absent EvidenceLink:
    # missing Evidence is how a defect looks, not how a verbal looks.
    source_kind: Mapped[str] = mapped_column(
        _enum(*EVENT_SOURCE_KINDS, name="event_source_kind"),
        default="cited",
        server_default="cited",
    )
    # The exact party wording as the source or recorder stated it.  Cited
    # events must not derive this from the affected party.
    stated_party: Mapped[str | None] = mapped_column(Text)
    # The date the event happened, which is not the date it was recorded.
    event_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    timings: Mapped[list["DependencyEventTiming"]] = relationship(
        back_populates="event",
        order_by="DependencyEventTiming.id",
        cascade="all, delete-orphan",
    )
    scope_links: Mapped[list["DependencyEventScope"]] = relationship(
        back_populates="event",
        order_by="DependencyEventScope.dependency_id",
        cascade="all, delete-orphan",
    )
    scope_decisions: Mapped[list["DependencyEventScopeDecision"]] = relationship(
        back_populates="event",
        order_by="DependencyEventScopeDecision.id",
        cascade="all, delete-orphan",
    )

    @property
    def previous_timing(self) -> "DependencyEventTiming | None":
        return next(
            (timing for timing in self.timings if timing.kind == "previous"), None
        )

    @property
    def new_timing(self) -> "DependencyEventTiming | None":
        return next((timing for timing in self.timings if timing.kind == "new"), None)


# The table keeps its historical physical name; domain-facing code can use the
# current ubiquitous language without a destructive table rename.
DependencyEvent = ExternalPartyStatement


class DependencyEventTiming(Base):
    """One source-preserving timing within an External Party statement."""

    __tablename__ = "dependency_event_timings"
    __table_args__ = (
        UniqueConstraint("event_id", "kind", name="uq_dependency_event_timing_kind"),
        CheckConstraint(
            "kind in ('previous', 'new')", name="ck_dependency_event_timing_kind"
        ),
        CheckConstraint(
            "precision in ('day', 'month', 'approximate', 'legacy_unknown')",
            name="ck_dependency_event_timing_precision",
        ),
        CheckConstraint(
            "(precision = 'day' and start_date is not null and end_date = start_date) "
            "or (precision = 'month' and start_date is not null and end_date is not null "
            "and start_date = date_trunc('month', start_date::timestamp)::date "
            "and end_date = (date_trunc('month', start_date::timestamp) "
            "+ interval '1 month - 1 day')::date) "
            "or (precision in ('approximate', 'legacy_unknown') and start_date is null and end_date is null)",
            name="ck_dependency_event_timing_bounds",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    precision: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    event: Mapped[DependencyEvent] = relationship(back_populates="timings")


class DependencyEventMigrationReceipt(Base):
    """The exact legacy event row captured before statement expansion."""

    __tablename__ = "dependency_event_migration_receipts"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_events.id", ondelete="CASCADE"), primary_key=True
    )
    original_event: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyEventScopeDecision(Base):
    """One attributable, append-only decision about a statement's scope.

    The statement and its scope do not share a lifecycle.  A later placement
    corrects or expands the scope by superseding this decision; it never edits
    the event or relocates one of this decision's links.
    """

    __tablename__ = "dependency_event_scope_decisions"
    __table_args__ = (
        UniqueConstraint(
            "supersedes_scope_decision_id",
            name="uq_dependency_event_scope_decision_supersedes",
        ),
        CheckConstraint(
            "scope_mode in ('unknown', 'selected', 'all_active', 'carried_forward')",
            name="ck_dependency_event_scope_decisions_mode",
        ),
        CheckConstraint(
            "length(trim(decided_by)) > 0",
            name="ck_dependency_event_scope_decisions_actor",
        ),
        Index(
            "uq_dependency_event_scope_decision_root",
            "event_id",
            unique=True,
            postgresql_where=text("supersedes_scope_decision_id is null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    scope_mode: Mapped[str] = mapped_column(String(16))
    supersedes_scope_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    decided_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    event: Mapped[DependencyEvent] = relationship(back_populates="scope_decisions")
    links: Mapped[list["DependencyEventScope"]] = relationship(
        back_populates="scope_decision",
        order_by="DependencyEventScope.dependency_id",
    )


class DependencyEventScope(Base):
    """One exact Dependency selected by an immutable scope decision."""

    __tablename__ = "dependency_event_scopes"
    __table_args__ = (
        UniqueConstraint(
            "scope_decision_id",
            "dependency_id",
            name="uq_dependency_event_scopes_decision_dependency",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    scope_decision_id: Mapped[int] = mapped_column(
        ForeignKey("dependency_event_scope_decisions.id")
    )
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    recorded_by: Mapped[str] = mapped_column(Text)
    event: Mapped[DependencyEvent] = relationship(back_populates="scope_links")
    scope_decision: Mapped[DependencyEventScopeDecision] = relationship(
        back_populates="links"
    )


class DependencyEventEvidence(Base):
    """One source Evidence identity owned by an External Party statement.

    Citation content remains on ``EvidenceLink`` so every existing foreign key
    keeps its stable identity.  This row establishes its single event owner
    without deriving any Dependency-specific readiness or publication role.
    """

    __tablename__ = "dependency_event_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_dependency_event_evidence_actor",
        ),
    )

    evidence_link_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_links.id"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(ForeignKey("dependency_events.id"))
    recorded_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


StatementTimingRecord = DependencyEventTiming
CommitmentScopeDecision = DependencyEventScopeDecision
CommitmentScopeMembership = DependencyEventScope
StatementEvidence = DependencyEventEvidence


class OperativeSupport(Base):
    """A human designation of which Evidence supports a publication scope.

    Readiness deliberately does not live here: it remains the independent
    dependency-specific sufficiency judgment.  An event citation can scope to
    several Dependencies, so support constrains the cited Evidence itself and
    the resolver verifies that the selected record is in the event's scope.
    The resolver combines the roles without collapsing their meanings
    (ADR-0017).
    """

    __tablename__ = "operative_support"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evidence_link_id"],
            ["evidence_links.id"],
            name="fk_operative_support_evidence_link",
        ),
        Index(
            "uq_operative_support_record_role",
            "dependency_id",
            "role",
            unique=True,
            postgresql_where=text("field_name is null"),
        ),
        Index(
            "uq_operative_support_field_role",
            "dependency_id",
            "role",
            "field_name",
            unique=True,
            postgresql_where=text("field_name is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    scope_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scopes.id")
    )
    role: Mapped[str] = mapped_column(
        _enum(*SUPPORT_ROLES, name="operative_support_role")
    )
    field_name: Mapped[str | None] = mapped_column(String(64))
    designated_by: Mapped[str] = mapped_column(Text)
    designated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RetiredDependencyStatus(Base):
    """The unauthoritative legacy status preserved when ADR-0044 retired it."""

    __tablename__ = "retired_dependency_statuses"

    dependency_id: Mapped[int] = mapped_column(
        ForeignKey("dependencies.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32))
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Candidate(Base):
    """An extractor's proposal, not yet part of the Project Record.

    Extractors write only here. Dependency and event Admission requires either
    human Adjudication or one exact deterministic policy outcome with an
    immutable receipt. Evidence Candidates are technical source-row proposals;
    they remain outside both Admission families until an explicit later linker
    uses them.
    """

    __tablename__ = "candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_document_id", "extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(_enum(*CANDIDATE_KINDS, name="candidate_kind"))
    payload_json: Mapped[dict] = mapped_column(JSONB)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    # Null only for history that cannot be tied to exactly one attempt
    # without guessing. Every new production extraction sets this.
    extraction_run_id: Mapped[int | None] = mapped_column(BigInteger)
    source_pages: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Recorded on every candidate. Without both, eval history across runs is
    # not comparable and you cannot tell which change moved the numbers.
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    # Mechanical: every citation's quote was found on its cited page, and —
    # where the extractor transcribes rather than parses — every field value
    # is text on that page too. Kept separate from `state`, which is the
    # Admission and disposition lifecycle. A Candidate that fails either check is
    # sunk in the queue, never dropped.
    citations_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    state: Mapped[str] = mapped_column(
        _enum(*CANDIDATE_STATES, name="candidate_state"),
        default="pending",
        server_default="pending",
    )
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyDismissal(Base):
    """One human decision that a record is junk, with the reason.

    Append-only, like every other decision here. Dismissing does not
    delete: the row, its Evidence, its Assertions and its history all
    stay exactly where they are, and `dependencies.dismissed_at` is the
    projection readers filter on — the shape the Committed Date
    projection already takes (ADR-0032). Anyone asking why a conflict
    left the list gets an answer with a name and a date on it.
    """

    __tablename__ = "dependency_dismissals"
    __table_args__ = (
        CheckConstraint(
            "reason in ('duplicate', 'not-a-conflict', 'wrong')",
            name="ck_dependency_dismissals_reason",
        ),
        CheckConstraint(
            "length(trim(dismissed_by)) > 0",
            name="ck_dependency_dismissals_attributable",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    reason: Mapped[str] = mapped_column(String(32))
    dismissed_by: Mapped[str] = mapped_column(Text)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DisputeSettlement(Base):
    """One human decision about what a disputed field concludes.

    A Dispute is a query, not a flag: two revisions asserting different
    verified values for one field. Assertions are append-only, so a
    settlement cannot erase the losing claim and does not try — it
    records what the record concludes and how far its judgment reaches
    (ADR-0031).

    ``covers_assertion_id`` is the newest Assertion for the field at the
    moment of settling. A later revision's claim carries a higher id, so
    it postdates the judgment and the Dispute reopens on its own: a
    reviewer settled the disagreement in front of them, never every
    disagreement that field will ever have.
    """

    __tablename__ = "dispute_settlements"
    __table_args__ = (
        CheckConstraint(
            "length(trim(settled_by)) > 0",
            name="ck_dispute_settlements_attributable",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    # What the record concludes. Null is a legitimate conclusion: a
    # reviewer may settle that the field says nothing.
    settled_value: Mapped[str | None] = mapped_column(Text)
    settled_by: Mapped[str] = mapped_column(Text)
    covers_assertion_id: Mapped[int] = mapped_column(BigInteger)
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DisputeHistoryResolution(Base):
    """One append-only ADR-0061 chronology outcome, never a human verdict.

    A physical source can be shown to be stale from the record's own change
    history.  That is a mechanical conclusion with a different authority from
    ``DisputeSettlement``: it must never look like a person chose a value.  A
    stale executed agreement is retained in this same chronology history, but
    its ``contractual_amendment`` outcome deliberately does *not* settle the
    field; it creates coordination work instead.
    """

    __tablename__ = "dispute_history_resolutions"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('physical_superseded', 'contractual_amendment')",
            name="ck_dispute_history_resolutions_outcome",
        ),
        CheckConstraint(
            "older_assertion_id <> newer_assertion_id",
            name="ck_dispute_history_resolutions_distinct_assertions",
        ),
        CheckConstraint(
            "length(trim(rule_version)) > 0",
            name="ck_dispute_history_resolutions_rule_version",
        ),
        UniqueConstraint(
            "dependency_id",
            "field_name",
            "covers_assertion_id",
            name="uq_dispute_history_resolutions_coverage",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    older_assertion_id: Mapped[int] = mapped_column(ForeignKey("assertions.id"))
    newer_assertion_id: Mapped[int] = mapped_column(ForeignKey("assertions.id"))
    # This is intentionally the exact newest Assertion the rule saw.  A later
    # assertion reopens a physical conclusion by the same coverage rule a
    # human settlement already uses.
    covers_assertion_id: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(32))
    rule_version: Mapped[str] = mapped_column(String(64))
    why: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
