"""What a source or a person said about the legacy Constraint Record.

These tables key to the frozen relations in ``corridor.models.legacy`` but are
not themselves frozen accepted-value tables, so they are kept apart from them:
ADR-0081's freeze is about who may write an accepted value, and an evidence
link, a documentation confirmation or a condition resolution records a claim
rather than the record. ``Assertion`` is a table and not a column for exactly
that reason -- a ledger field value is an adjudicated conclusion, and the
assertions beneath it preserve what each source actually claimed (ADR-0001).
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base


__all__ = [
    "Assertion",
    "CandidateDisposition",
    "ConditionResolution",
    "DependencyEvidenceSufficiency",
    "DocumentationFieldConfirmation",
    "EvidenceLink",
    "EvidenceLinkSource",
    "WorkDecisionMilestoneImpact",
]


class EvidenceLink(Base):
    __tablename__ = "evidence_links"
    __table_args__ = (
        UniqueConstraint("dependency_id", "id"),
        # The rendition key an EvidenceLinkSource resolves against, so a cited
        # Source Segment cannot come from a different document (#605).
        UniqueConstraint(
            "document_id", "id", name="uq_evidence_links_document_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Direct record Evidence owns one Dependency. Statement Evidence leaves
    # this null and is owned by DependencyEventEvidence instead; neither
    # relationship is inferred from missing data.
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    page_no: Mapped[int] = mapped_column(Integer)
    # Superseded for new writes by EvidenceLinkSource (ADR-0068, #605): the
    # Source Segment owns its exact text once and this column copies it.
    # Existing rows keep their copy and stay readable through
    # ``corridor.evidence_citations.evidence_quotation``; nothing rewrites
    # them in bulk until a sibling ticket proves the two texts equivalent.
    quote: Mapped[str] = mapped_column(Text)
    # The quote appears on the cited page. Nothing more.
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EvidenceLinkSource(Base):
    """One Source Segment an Evidence Link cites, in the same rendition.

    The reference ADR-0068 asked for: an Evidence Link names the segments that
    own the words rather than carrying a second copy of them.  The row holds no
    text at all, so the cited wording has exactly one owner and can be replayed
    from the registered source bytes through the segment's typed locator.

    It mirrors ``fact_sources`` and ``support_assessment_sources`` rather than
    inventing a shape: link, segment, ordinal, and composite keys that make a
    citation of another project's or another document's segment
    unrepresentable.  Rows are append-only.
    """

    __tablename__ = "evidence_link_sources"
    __table_args__ = (
        UniqueConstraint(
            "evidence_link_id",
            "source_segment_id",
            name="uq_evidence_link_sources_segment",
        ),
        UniqueConstraint(
            "evidence_link_id",
            "ordinal",
            name="uq_evidence_link_sources_ordinal",
        ),
        ForeignKeyConstraint(
            ["document_id", "evidence_link_id"],
            ["evidence_links.document_id", "evidence_links.id"],
            name="fk_evidence_link_sources_link_scope",
        ),
        ForeignKeyConstraint(
            ["project_id", "document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_evidence_link_sources_segment_scope",
        ),
        CheckConstraint("ordinal > 0", name="ck_evidence_link_sources_ordinal"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[int] = mapped_column(BigInteger, index=True)
    evidence_link_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DependencyEvidenceSufficiency(Base):
    """A Dependency-specific sufficiency judgment on direct or event Evidence."""

    __tablename__ = "dependency_evidence_sufficiencies"
    __table_args__ = (
        UniqueConstraint(
            "scope_link_id",
            "evidence_link_id",
            name="uq_dependency_evidence_sufficiency_scope_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(ForeignKey("evidence_links.id"))
    scope_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependency_event_scopes.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentationFieldConfirmation(Base):
    """One append-only human confirmation of a cited interpretation field.

    Machine checklist fields are predicates over current cited documents and
    therefore have no stored checkmark.  This row exists only for the small
    interpretive residue ADR-0052 retains: a named person confirmed the
    system's cited reading of one exact current document.  A later source or
    supersession does not overwrite the row; it simply stops making the old
    confirmation applicable when the checklist is read.
    """

    __tablename__ = "documentation_field_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_documentation_confirmation_owned_evidence",
        ),
        CheckConstraint(
            "field_name = 'approval_interpretation'",
            name="ck_documentation_confirmation_known_field",
        ),
        CheckConstraint(
            "classification in ('approved', 'conditional')",
            name="ck_documentation_confirmation_known_classification",
        ),
        CheckConstraint(
            "conclusion = 'approved'",
            name="ck_documentation_confirmation_known_conclusion",
        ),
        CheckConstraint(
            "length(trim(confirmed_by)) > 0",
            name="ck_documentation_confirmation_actor",
        ),
        # ADR-0060's optional override counts a hedge as immaterial.  It is
        # only meaningful on a conditional letter and must durably record the
        # exact hedge it overrode (the deposition answer for why a hedged
        # letter counted as approval), beside who did it and when (#373).
        CheckConstraint(
            "condition_immaterial = false or ("
            "classification = 'conditional' and overridden_condition_text is not null "
            "and length(trim(overridden_condition_text)) > 0)",
            name="ck_documentation_confirmation_override_records_hedge",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    field_name: Mapped[str] = mapped_column(String(64))
    # This preserves the exact machine reading the person was shown; it is
    # deliberately not a grant for the model to write a project conclusion.
    classification: Mapped[str] = mapped_column(String(64))
    conclusion: Mapped[str] = mapped_column(String(64))
    confirmed_by: Mapped[str] = mapped_column(Text)
    # False for the clean-letter confirm; True only when this row is the
    # optional ADR-0060 override that counts the quoted hedge as immaterial.
    condition_immaterial: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # The exact hedge the override treated as immaterial, retained verbatim.
    overridden_condition_text: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConditionResolution(Base):
    """One append-only attributable act that resolves an open condition.

    ADR-0060 makes a condition a field in its own words: the entry itself is
    derived at read time from the conditional letter, never a stored checkmark
    (exactly like every other machine field, ADR-0052).  What *is* stored is
    the small set of acts that move an open condition toward Ready — a person
    clearing it against a cited later passage or a recorded verbal, the
    exact-and-mechanical automatic clear, and a person dismissing a
    misdetection with a reason.  Nothing here can be produced by a
    misdetection alone, and a dismissal only ever removes a spurious blocker;
    neither direction can manufacture a false Ready.

    The composite foreign key to ``evidence_links(dependency_id, id)`` is the
    database-level guarantee that a resolution is about its own Constraint's
    letter, the same bypass boundary the checklist confirmations use.
    """

    __tablename__ = "condition_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dependency_id", "evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_condition_resolution_owned_evidence",
        ),
        ForeignKeyConstraint(
            ["basis_evidence_link_id"],
            ["evidence_links.id"],
            name="fk_condition_resolution_basis_evidence",
        ),
        ForeignKeyConstraint(
            ["basis_event_id"],
            ["dependency_events.id"],
            name="fk_condition_resolution_basis_event",
        ),
        CheckConstraint(
            "kind in ('cleared', 'dismissed')",
            name="ck_condition_resolution_kind",
        ),
        CheckConstraint(
            "length(trim(condition_text)) > 0",
            name="ck_condition_resolution_text",
        ),
        CheckConstraint(
            "length(trim(resolved_by)) > 0",
            name="ck_condition_resolution_actor",
        ),
        CheckConstraint(
            "kind <> 'cleared' or basis_evidence_link_id is not null "
            "or basis_event_id is not null",
            name="ck_condition_resolution_clear_has_basis",
        ),
        CheckConstraint(
            "kind <> 'dismissed' or (reason is not null and length(trim(reason)) > 0)",
            name="ck_condition_resolution_dismissal_has_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    # The source conditional letter passage this act is about.
    evidence_link_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    # A durable verbatim copy of the exact words resolved — legible after a
    # later supersession, and never anything but stored data.
    condition_text: Mapped[str] = mapped_column(Text)
    # A clear cites its basis: a later verified passage and/or a recorded
    # verbal statement.  A dismissal carries no basis, only a reason.
    basis_evidence_link_id: Mapped[int | None] = mapped_column(BigInteger)
    basis_event_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    # The exact-and-mechanical automatic clear keeps its reproducible receipt
    # here (ADR-0050); a human act leaves it null.
    receipt_json: Mapped[dict | None] = mapped_column(JSONB)
    resolved_by: Mapped[str] = mapped_column(String(128))
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkDecisionMilestoneImpact(Base):
    """One exact registered Milestone named by an ``affects`` decision."""

    __tablename__ = "work_decision_milestone_impacts"
    __table_args__ = (
        UniqueConstraint(
            "work_decision_id",
            "milestone_id",
            name="uq_work_decision_milestone_impact",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    work_decision_id: Mapped[int] = mapped_column(ForeignKey("work_decisions.id"))
    milestone_id: Mapped[int] = mapped_column(ForeignKey("milestones.id"))


class CandidateDisposition(Base):
    """One human disposition of an Unplaced Statement Candidate.

    Candidate state is the current-work projection.  This append-only record
    preserves why a coordinator accepted a statement or marked it Not Relevant
    without treating either as a mutation of the extractor's Candidate.
    """

    __tablename__ = "candidate_dispositions"
    __table_args__ = (
        CheckConstraint(
            "disposition in ('accepted', 'not_relevant')",
            name="ck_candidate_dispositions_kind",
        ),
        CheckConstraint(
            "(disposition = 'accepted' and reason is null) or "
            "(disposition = 'not_relevant' and reason is not null)",
            name="ck_candidate_dispositions_reason",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_candidate_dispositions_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"))
    disposition: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Assertion(Base):
    __tablename__ = "assertions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dependency_id: Mapped[int] = mapped_column(ForeignKey("dependencies.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    asserted_value: Mapped[str | None] = mapped_column(Text)
    evidence_link_id: Mapped[int] = mapped_column(ForeignKey("evidence_links.id"))
    # The source document's own date, which is what orders competing claims
    # and drives last_evidenced_at.
    doc_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
