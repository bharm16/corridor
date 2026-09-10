"""The project, the people, and the organizations a project coordinates with.

The identities everything else keys to. External Party carries the
``ExternalOrg`` alias because the code was written against the older name before
ADR-0048 fixed the glossary, and renaming the identifier is separately scoped
work; the alias is the compatibility, not a second concept. Subject resolution
lives here rather than with the statements it resolves because it resolves a
*name in a source* to one of these identities, and its candidates and decisions
are about identity rather than about any one statement.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.models.base import Base, ORG_TYPES, _enum


__all__ = [
    "ExternalOrg",
    "ExternalParty",
    "IntakeProjectIdentifier",
    "OrganizationIdentityReceipt",
    "PersonIdentity",
    "Project",
    "ProjectContact",
    "ProjectContactImport",
    "ProjectRosterEntry",
    "StatedByPerson",
    "SubjectCandidateSuggestion",
    "SubjectResolutionAttempt",
    "SubjectResolutionCandidate",
    "SubjectResolutionDecision",
]


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    agency: Mapped[str | None] = mapped_column(Text)
    # Structural enforcement of the no-synthetic-samples rule: `make eval`
    # refuses to run against a project with this set, so the boundary
    # cannot be crossed by forgetting about it.
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # The parties that are the project's own side — its engineer, its
    # consultants. Stated configuration, never inferred: an event whose
    # actor is one of these is the project taking an action item, and it
    # can never carry an External Party's commitment (ADR-0026). SH 99's
    # minutes are mostly LJA's own commitments, which is why this exists.
    project_side_parties: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IntakeProjectIdentifier(Base):
    """One exact registered routing identifier for the shared intake address.

    A project name is deliberately absent: names are presentation text, never a
    routing key.  A caller records the agency-issued identifier (for example a
    CSJ or contract number) and the router only compares its normalized exact
    value.
    """

    __tablename__ = "intake_project_identifiers"
    __table_args__ = (
        UniqueConstraint("kind", "value_normalized", "project_id"),
        CheckConstraint("length(trim(kind)) > 0", name="ck_intake_identifier_kind"),
        CheckConstraint(
            "length(trim(value_normalized)) > 0", name="ck_intake_identifier_value"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    value_normalized: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectContactImport(Base):
    """One immutable, bound contact import and complete row accounting (#562)."""

    __tablename__ = "project_contact_imports"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_contact_import_scope"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_contact_import_key"),
        UniqueConstraint("project_id", "source_family", "source_revision", name="uq_contact_import_revision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("source_deliveries.id"))
    customer: Mapped[str] = mapped_column(Text)
    source_family: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapping_json: Mapped[dict] = mapped_column(JSONB)
    accounting_json: Mapped[dict] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class ProjectContact(Base):
    """An imported contact or attributable replacement, never a mutable address book."""

    __tablename__ = "project_contacts"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "import_id"], ["project_contact_imports.project_id", "project_contact_imports.id"]),
        UniqueConstraint("project_id", "id", name="uq_project_contact_scope"),
        ForeignKeyConstraint(["project_id", "corrects_id"], ["project_contacts.project_id", "project_contacts.id"]),
        UniqueConstraint("corrects_id", name="uq_project_contact_correction"),
        UniqueConstraint("project_id", "correction_key", name="uq_project_contact_correction_key"),
        Index("uq_project_contact_import_identity", "import_id", "source_contact_id", unique=True,
              postgresql_where=text("corrects_id is null")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    import_id: Mapped[int] = mapped_column(BigInteger)
    source_contact_id: Mapped[str] = mapped_column(Text)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    values_json: Mapped[dict] = mapped_column(JSONB)
    source_locators: Mapped[dict] = mapped_column(JSONB)
    unresolved_reason: Mapped[str | None] = mapped_column(Text)
    corrects_id: Mapped[int | None] = mapped_column(BigInteger)
    correction_key: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[str | None] = mapped_column(Text)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class ExternalParty(Base):
    """One registered External Party with every confirmed source alias."""

    __tablename__ = "external_orgs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    org_type: Mapped[str] = mapped_column(
        _enum(*ORG_TYPES, name="org_type"), default="utility", server_default="utility"
    )
    # One party is named many ways across documents — "AT&T", "AT&T Texas
    # (SWBT)", "Southwestern Bell". Merge ranking blocks on the resolved
    # party, so collapsing these is a precondition for everything else.
    # Populated during adjudication; exact-name matching only in v0.
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )


# Physical schema and older integrations used this implementation name.
ExternalOrg = ExternalParty


class OrganizationIdentityReceipt(Base):
    """One append-only resolution of source wording to a registered party.

    The ``external_orgs`` row remains the registry's current projection.  This
    receipt is the authority and provenance for an alias, a human selection, or
    a deterministic whole-row resolution: it preserves the wording, the
    evidence that was considered, and the responsible person or policy.  A
    local statement resolution is deliberately not represented here; it stays
    source-bound on the statement receipt rather than acquiring registry reach.
    """

    __tablename__ = "organization_identity_receipts"
    __table_args__ = (
        UniqueConstraint("candidate_id", "method", name="uq_organization_identity_receipt_candidate_method"),
        CheckConstraint(
            "method in ('human_confirmation', 'automatic_name_alias', "
            "'automatic_facility_class', 'automatic_contact', "
            "'automatic_revision_lineage', 'automatic_stated_alias', "
            "'human_cited_alias_confirmation', 'alias_correction')",
            name="ck_organization_identity_receipt_method",
        ),
        CheckConstraint(
            "scope = 'registry'", name="ck_organization_identity_receipt_scope"
        ),
        CheckConstraint(
            "length(trim(stated_wording)) > 0",
            name="ck_organization_identity_receipt_wording",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0",
            name="ck_organization_identity_receipt_actor",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object'",
            name="ck_organization_identity_receipt_evidence",
        ),
        CheckConstraint(
            "jsonb_typeof(facility_classes_json) = 'array'",
            name="ck_organization_identity_receipt_facility_classes",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    external_org_id: Mapped[int] = mapped_column(ForeignKey("external_orgs.id"), index=True)
    method: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32), default="registry", server_default="registry")
    stated_wording: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[dict] = mapped_column(JSONB)
    facility_classes_json: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    recorded_by: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    policy_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectRosterEntry(Base):
    """One project-team membership, and the authority it carries.

    A Work Decision keeps its human-readable owner projection for existing
    readers, but a guided save must not turn a typed project roster into a
    caller-supplied string.  The grouping receipt binds the exact roster row
    that supplied the rendered name.

    The membership is also the project-scoped access boundary (#331).  An
    ``active`` row makes the person a member who may read the project and be
    assigned work, but membership alone is not authority: each write designation
    is an explicit, independently granted flag (ADR-0034 decisions 28 and 34,
    ADR-0035).  Possessing a signed-in principal, or merely appearing on the
    roster, confers none of them by default — they fail closed.
    """

    __tablename__ = "project_roster_entries"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "principal_subject", name="uq_project_roster_principal"
        ),
        CheckConstraint(
            "length(trim(principal_subject)) > 0", name="ck_project_roster_principal"
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_project_roster_display_name"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    principal_subject: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # The four distinct designations of #331.  None is implied by another or by
    # membership: a coordinator is not a Documentation Reviewer, a reviewer may
    # not release externally, and none of them is a technical operator.
    can_coordinate: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    can_review_documentation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    can_release_externally: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    is_technical_operator: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PersonIdentity(Base):
    """The stable subject a verified email resolves to, and nothing more.

    Sign-in authenticates a person (email -> principal); membership and its
    designations decide what that person may do, per project.  This table is
    deliberately global and authority-free: creating it grants no project access
    and no organization-registry power (#331).  It is never backfilled for
    historical actors — an existing audit or decision principal keeps its own
    identity, and its email basis stays unknown unless a person enrolls (ADR-0035).
    """

    __tablename__ = "person_identities"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_person_identity_email"),
        UniqueConstraint("principal_subject", name="uq_person_identity_principal"),
        CheckConstraint(
            "length(trim(email_normalized)) > 0", name="ck_person_identity_email"
        ),
        CheckConstraint(
            "length(trim(principal_subject)) > 0",
            name="ck_person_identity_principal",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_normalized: Mapped[str] = mapped_column(Text)
    principal_subject: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatedByPerson(Base):
    """One project-scoped individual who may be the person in Stated By.

    This is Project Record identity, not authentication. A registered person
    may be an external speaker with no Corridor account; conversely a signed-in
    PersonIdentity gains no statement attribution merely by existing.
    """

    __tablename__ = "stated_by_people"
    __table_args__ = (
        CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_stated_by_people_display_name",
        ),
        CheckConstraint(
            "email_normalized is null or "
            "(length(trim(email_normalized)) > 0 and "
            "email_normalized = lower(trim(email_normalized)))",
            name="ck_stated_by_people_email",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    display_name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    email_normalized: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectResolutionAttempt(Base):
    """One provenance-bound exact lookup of a source reference.

    The row is an observation of what the released exact-alias rule saw.  It
    never doubles as the Human Record Decision that may later register an
    alias, and unresolved outcomes remain durable even after that decision.
    """

    __tablename__ = "subject_resolution_attempts"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_subject_resolution_content"),
        ForeignKeyConstraint(
            ["project_id", "source_document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_subject_resolution_segment_scope",
        ),
        CheckConstraint(
            "reference_kind in ('organization_name', 'email_sender', "
            "'email_domain', 'person_name', 'person_email', "
            "'source_identifier', 'activity_identifier', 'document_identifier')",
            name="ck_subject_resolution_reference_kind",
        ),
        CheckConstraint(
            "expected_subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_expected_type",
        ),
        CheckConstraint(
            "usage in ('identity', 'statement_speaker', 'affected_subject')",
            name="ck_subject_resolution_usage",
        ),
        CheckConstraint(
            "state in ('resolved', 'unresolved', 'conflict', 'stale', 'actor_boundary')",
            name="ck_subject_resolution_state",
        ),
        CheckConstraint(
            "length(trim(raw_reference)) > 0 and length(trim(normalized_reference)) > 0",
            name="ck_subject_resolution_reference",
        ),
        CheckConstraint(
            "rule_identity = 'exact-registered-alias-v1'",
            name="ck_subject_resolution_rule",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_subject_resolution_sha256",
        ),
        CheckConstraint(
            "(state = 'resolved' and "
            "num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, "
            "resolved_dependency_id, resolved_document_id) = 1 and "
            "((expected_subject_type = 'external_org' and resolved_external_org_id is not null) or "
            "(expected_subject_type = 'person' and resolved_stated_by_person_id is not null) or "
            "(expected_subject_type = 'constraint' and resolved_dependency_id is not null) or "
            "(expected_subject_type = 'document' and resolved_document_id is not null))) or "
            "(state <> 'resolved' and "
            "num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, "
            "resolved_dependency_id, resolved_document_id) = 0)",
            name="ck_subject_resolution_target",
        ),
        CheckConstraint(
            "(state = 'resolved' and attention_reason is null) or "
            "(state <> 'resolved' and length(trim(attention_reason)) > 0)",
            name="ck_subject_resolution_attention",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source_document_id: Mapped[int] = mapped_column(BigInteger)
    source_segment_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reference_kind: Mapped[str] = mapped_column(String(48))
    raw_reference: Mapped[str] = mapped_column(Text)
    normalized_reference: Mapped[str] = mapped_column(Text)
    expected_subject_type: Mapped[str] = mapped_column(String(32))
    usage: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24))
    attention_reason: Mapped[str | None] = mapped_column(String(64))
    rule_identity: Mapped[str] = mapped_column(String(64))
    resolved_external_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_orgs.id")
    )
    resolved_stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    resolved_dependency_id: Mapped[int | None] = mapped_column(
        ForeignKey("dependencies.id")
    )
    resolved_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectResolutionCandidate(Base):
    """One typed candidate retained on an unresolved exact lookup."""

    __tablename__ = "subject_resolution_candidates"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "id", name="uq_subject_resolution_candidate_scope"
        ),
        UniqueConstraint(
            "attempt_id", "subject_key", name="uq_subject_resolution_candidate"
        ),
        CheckConstraint(
            "subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_candidate_type",
        ),
        CheckConstraint(
            "candidate_state in ('active', 'stale')",
            name="ck_subject_resolution_candidate_state",
        ),
        CheckConstraint(
            "num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1 and "
            "((subject_type = 'external_org' and external_org_id is not null) or "
            "(subject_type = 'person' and stated_by_person_id is not null) or "
            "(subject_type = 'constraint' and dependency_id is not null) or "
            "(subject_type = 'document' and document_id is not null))",
            name="ck_subject_resolution_candidate_target",
        ),
        CheckConstraint(
            "match_source in ('registered_alias', 'human_alias_decision')",
            name="ck_subject_resolution_candidate_match_source",
        ),
        CheckConstraint(
            "length(trim(subject_key)) > 0 and length(trim(display_name)) > 0",
            name="ck_subject_resolution_candidate_text",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(96))
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    display_name: Mapped[str] = mapped_column(Text)
    candidate_state: Mapped[str] = mapped_column(String(16))
    match_source: Mapped[str] = mapped_column(String(64))


class SubjectResolutionDecision(Base):
    """One attributable Human Record Decision registering an exact alias."""

    __tablename__ = "subject_resolution_decisions"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_subject_resolution_decision_attempt"),
        UniqueConstraint("revision_id", name="uq_subject_resolution_decision_revision"),
        UniqueConstraint(
            "project_id",
            "reference_kind",
            "normalized_reference",
            name="uq_subject_resolution_registered_alias",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_document_id", "source_segment_id"],
            [
                "source_segments.project_id",
                "source_segments.document_id",
                "source_segments.id",
            ],
            name="fk_subject_resolution_decision_segment_scope",
        ),
        CheckConstraint(
            "decision_kind = 'human_alias_registration'",
            name="ck_subject_resolution_decision_kind",
        ),
        CheckConstraint(
            "reference_kind in ('organization_name', 'email_sender', "
            "'email_domain', 'person_name', 'person_email', "
            "'source_identifier', 'activity_identifier', 'document_identifier')",
            name="ck_subject_resolution_decision_reference_kind",
        ),
        CheckConstraint(
            "subject_type in ('external_org', 'person', 'constraint', 'document')",
            name="ck_subject_resolution_decision_type",
        ),
        CheckConstraint(
            "num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1 and "
            "((subject_type = 'external_org' and external_org_id is not null) or "
            "(subject_type = 'person' and stated_by_person_id is not null) or "
            "(subject_type = 'constraint' and dependency_id is not null) or "
            "(subject_type = 'document' and document_id is not null))",
            name="ck_subject_resolution_decision_target",
        ),
        CheckConstraint(
            "length(trim(recorded_by)) > 0 and length(trim(normalized_reference)) > 0",
            name="ck_subject_resolution_decision_actor",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("project_record_revisions.id"), index=True
    )
    source_document_id: Mapped[int] = mapped_column(BigInteger)
    source_segment_id: Mapped[int] = mapped_column(BigInteger)
    reference_kind: Mapped[str] = mapped_column(String(48))
    raw_reference: Mapped[str] = mapped_column(Text)
    normalized_reference: Mapped[str] = mapped_column(Text)
    decision_kind: Mapped[str] = mapped_column(String(48))
    subject_type: Mapped[str] = mapped_column(String(32))
    external_org_id: Mapped[int | None] = mapped_column(ForeignKey("external_orgs.id"))
    stated_by_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("stated_by_people.id")
    )
    dependency_id: Mapped[int | None] = mapped_column(ForeignKey("dependencies.id"))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    recorded_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubjectCandidateSuggestion(Base):
    """One read-only model ranking over already-retained candidates."""

    __tablename__ = "subject_candidate_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "rank", name="uq_subject_candidate_suggestion_rank"
        ),
        UniqueConstraint(
            "attempt_id", "candidate_id", name="uq_subject_candidate_suggestion_candidate"
        ),
        CheckConstraint("rank > 0", name="ck_subject_candidate_suggestion_rank"),
        CheckConstraint(
            "length(trim(model)) > 0 and length(trim(prompt_version)) > 0",
            name="ck_subject_candidate_suggestion_model",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "candidate_id"],
            [
                "subject_resolution_candidates.attempt_id",
                "subject_resolution_candidates.id",
            ],
            name="fk_subject_candidate_suggestion_scope",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("subject_resolution_attempts.id"), index=True
    )
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    rank: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
