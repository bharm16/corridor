"""What is published outward: issue profiles, release candidates, reports.

A release candidate is immutable and comes from one coherent reading (#529), so
the preparation request, each attempt, and the reading it used are separate
rows: an earlier design mutated a candidate in place and lost which reading
produced the published artifact. The coverage declaration records what an issue
was prepared under, so a report cannot silently claim sources it never read.
"""

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from corridor.models.base import Base


__all__ = [
    "BLOCKED",
    "CONFIGURED_ARTIFACT_TYPES",
    "COVERAGE_LINE_STATES",
    "ExternalReportArtifact",
    "ExternalReportRelease",
    "IssueCoverageDeclaration",
    "IssueProfile",
    "IssueProfileArtifact",
    "PREPARATION_ATTEMPT_OUTCOMES",
    "PREPARATION_REFUSAL_REASONS",
    "ProjectCheckConfiguration",
    "READY",
    "READY_WITH_EXCEPTIONS",
    "RELEASE_READINESS_STATES",
    "ReleaseCandidate",
    "ReleaseCandidateArtifact",
    "ReleasePackage",
    "ReleasePackageArtifact",
    "ReleasePreparationAttempt",
    "ReleasePreparationPublication",
    "ReleasePreparationReading",
    "ReleasePreparationRefusal",
    "ReleasePreparationRequest",
    "ReportRun",
    "ScheduledReportPublication",
]


# ADR-0091's configured members. The mandatory updated UCM is deliberately not
# among them: it is a column on ``IssueProfile``, so a profile can neither omit
# it nor carry it twice.
CONFIGURED_ARTIFACT_TYPES = (
    "accepted_change_summary",
    "chase_list",
    "weekly_coordination_report",
    "provenance_sidecar",
)

_CONFIGURED_ARTIFACT_TYPES_SQL = ", ".join(
    f"'{name}'" for name in CONFIGURED_ARTIFACT_TYPES
)


class IssueProfile(Base):
    """One version of what a project externally issues (#640, ADR-0091).

    ADR-0091 made the issued set per-project configuration with only the
    updated UCM mandatory, and recorded that the configuration was not
    modelled. This row is it, and every version of it is kept: a profile change
    never rewrites what an earlier reporting cutoff was configured to issue.

    The chain is the timing rule. Each version names its predecessor through a
    composite foreign key carrying that predecessor's project, identity,
    version and effective instant, and ``ck_project_issue_profiles_succession``
    requires the version to be exactly one higher and the effective instant to
    be strictly later — so monotonic, non-backdatable, single-lineage,
    append-only history is a database invariant rather than a Python
    comparison. ``uq_project_issue_profiles_successor`` refuses a fork and the
    partial unique index refuses a second lineage in one project.

    The updated UCM is a column rather than an artifact row because its
    participation is not configurable; ``IssueProfileArtifact`` holds exactly
    what ADR-0091 made a per-project choice.
    """

    __tablename__ = "project_issue_profiles"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_project_issue_profiles_key"
        ),
        UniqueConstraint(
            "project_id",
            "profile_identity",
            "profile_version",
            name="uq_project_issue_profiles_version",
        ),
        UniqueConstraint("id", "project_id", name="uq_project_issue_profiles_row"),
        UniqueConstraint(
            "id",
            "project_id",
            "profile_identity",
            "profile_version",
            "effective_from",
            name="uq_project_issue_profiles_chain",
        ),
        # The key a release candidate binds itself to (#529): the row, its
        # project, its identity and its version, so a candidate cannot name
        # one profile row while recording another profile's version.
        UniqueConstraint(
            "id",
            "project_id",
            "profile_identity",
            "profile_version",
            name="uq_project_issue_profiles_binding",
        ),
        UniqueConstraint(
            "supersedes_id", name="uq_project_issue_profiles_successor"
        ),
        ForeignKeyConstraint(
            [
                "supersedes_id",
                "project_id",
                "profile_identity",
                "supersedes_version",
                "supersedes_effective_from",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
                "project_issue_profiles.effective_from",
            ],
            name="fk_project_issue_profiles_supersedes",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_project_issue_profiles_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_project_issue_profiles_mapping",
        ),
        CheckConstraint(
            "length(btrim(profile_identity)) > 0",
            name="ck_project_issue_profiles_identity",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_project_issue_profiles_key",
        ),
        CheckConstraint(
            "length(btrim(registered_by_principal)) > 0",
            name="ck_project_issue_profiles_principal",
        ),
        CheckConstraint(
            "length(btrim(declaration_schema_version)) > 0",
            name="ck_project_issue_profiles_schema",
        ),
        CheckConstraint(
            "profile_version >= 1", name="ck_project_issue_profiles_version"
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0",
            name="ck_project_issue_profiles_ucm",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(declaration, 'utf8')), 'hex') "
            "= content_sha256",
            name="ck_project_issue_profiles_digest",
        ),
        CheckConstraint(
            "(supersedes_id is null and profile_version = 1 "
            "and supersedes_version is null "
            "and supersedes_effective_from is null) "
            "or (supersedes_id is not null and supersedes_version is not null "
            "and supersedes_effective_from is not null "
            "and profile_version = supersedes_version + 1 "
            "and effective_from > supersedes_effective_from)",
            name="ck_project_issue_profiles_succession",
        ),
        Index(
            "uq_project_issue_profiles_root",
            "project_id",
            unique=True,
            postgresql_where=text("supersedes_id is null"),
        ),
        Index(
            "ix_project_issue_profiles_effective", "project_id", "effective_from"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    profile_identity: Mapped[str] = mapped_column(String(160))
    # An integer, so a blank version is not a value this column can hold.
    profile_version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    # The exact canonical bytes the digest is taken over, not a re-encoding of
    # them: `jsonb` would normalize key order, whitespace and numbers, and the
    # digest could no longer be checked against what came back.
    declaration: Mapped[str] = mapped_column(Text)
    declaration_schema_version: Mapped[str] = mapped_column(String(64))
    # The business instant the caller supplied, never a clock reading.
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    # A stored constant, so the composite key above can require the named
    # registration to be a template and not a mapping.
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger)
    supersedes_version: Mapped[int | None] = mapped_column(Integer)
    supersedes_effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    registered_by_principal: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IssueProfileArtifact(Base):
    """One configured member of a project's issued set (#640, ADR-0091).

    Only what ADR-0091 made a per-project choice lives here. ``updated_ucm`` is
    not an admitted type: the mandatory member is ``IssueProfile``'s own
    column, so it can be neither dropped from a profile nor entered twice.
    """

    __tablename__ = "project_issue_profile_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "artifact_type",
            name="uq_project_issue_profile_artifacts_type",
        ),
        ForeignKeyConstraint(
            ["profile_id", "project_id"],
            ["project_issue_profiles.id", "project_issue_profiles.project_id"],
            name="fk_project_issue_profile_artifacts_profile",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_project_issue_profile_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_project_issue_profile_artifacts_renderer",
        ),
        Index("ix_project_issue_profile_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))


# --- #675 The confirmed coverage declaration one issue is prepared under ----

# The four states one coverage line may be in, spelled where the database
# check reads them. They are ``issue_rendering``'s own vocabulary, respelled
# nowhere: ``read`` is the only non-exception, and the other three are the
# honest ways a source is not part of what was read for this issue.
COVERAGE_LINE_STATES = ("read", "failed", "excluded", "late")


class IssueCoverageDeclaration(Base):
    """One coordinator's confirmation of the coverage reading Corridor derived.

    ADR-0086 makes "one declared coverage state" a shared input of every
    artifact in an issue, and #529 bound it as an in-memory value the caller
    supplied. That let any caller state a coverage nobody confirmed, so #675
    gives the declaration its own identity: the candidate now names this row
    by foreign key, and the only thing that can be prepared is coverage a
    person put their name to.

    **The machine's half and the human's half are separately digested.**
    ``derived_reading`` holds the exact canonical bytes Corridor derived from
    the effective issue profile, the persisted Source Delivery ledger, the
    processing receipts and the declared cutoff, and ``derived_reading_digest``
    is the SHA-256 of them; ``declaration`` repeats that digest and adds only
    what a person may add -- bounded annotations and permitted exclusions with
    their reasons -- and ``declaration_digest`` is the SHA-256 of *that*. A
    coordinator who confirmed one reading therefore cannot be recorded as
    having confirmed another, and the two questions "what did Corridor say"
    and "what did the person declare" keep two separate answers.

    **The boundary is an append-only watermark, never a clock comparison.**
    ``through_source_delivery_id`` is the highest ``source_deliveries`` row
    this issue includes, and every delivery after it is outside the issue by
    identity. ``cutoff_at`` is the human-readable instant the reading was taken
    at and is frozen beside it, but membership is the watermark: #641 recorded
    that a Proposed Delta has no trustworthy source-arrival instant and that
    ``created_at`` is server-assigned, and this is the record that lets the
    question be asked without one. ``None`` is the honest watermark of a
    project that has taken no delivery at all, and is not "everything".

    **Who ``confirmed_by_principal`` may name is PostgreSQL's rule** (#839).
    ``enforce_coordination_designation`` fires before every insert and reads
    the roster: a person the project does not designate to coordinate cannot
    be recorded as having confirmed its coverage, whatever the caller passed.
    """

    __tablename__ = "issue_coverage_declarations"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_issue_coverage_declarations_row"
        ),
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_issue_coverage_declarations_key",
        ),
        # One row per confirmed declaration. A repeated confirmation of the
        # same reading with the same annotations converges here rather than
        # appending a second identical declaration.
        UniqueConstraint(
            "project_id",
            "declaration_digest",
            name="uq_issue_coverage_declarations_identity",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_issue_coverage_declarations_profile",
        ),
        ForeignKeyConstraint(
            ["through_source_delivery_id", "project_id"],
            ["source_deliveries.id", "source_deliveries.project_id"],
            name="fk_issue_coverage_declarations_delivery",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(derived_reading, 'utf8')), 'hex') "
            "= derived_reading_digest",
            name="ck_issue_coverage_declarations_reading_digest",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(declaration, 'utf8')), 'hex') "
            "= declaration_digest",
            name="ck_issue_coverage_declarations_declaration_digest",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_issue_coverage_declarations_identity_text",
        ),
        CheckConstraint(
            "length(btrim(confirmed_by_principal)) > 0",
            name="ck_issue_coverage_declarations_principal",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_issue_coverage_declarations_version",
        ),
        Index(
            "ix_issue_coverage_declarations_project", "project_id", "cutoff_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    through_source_delivery_id: Mapped[int | None] = mapped_column(BigInteger)
    derived_reading: Mapped[str] = mapped_column(Text)
    derived_reading_digest: Mapped[str] = mapped_column(String(64))
    declaration: Mapped[str] = mapped_column(Text)
    declaration_digest: Mapped[str] = mapped_column(String(64))
    coverage_identity: Mapped[str] = mapped_column(String(160))
    confirmed_by_principal: Mapped[str] = mapped_column(String(128))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- #529 One immutable release candidate, from one coherent reading --------

# Every reason preparation may refuse for, as a closed vocabulary. A refusal a
# person has to read a paragraph to classify is a refusal nobody counts.
PREPARATION_REFUSAL_REASONS = (
    "unsupported_issue_configuration",
    "renderer_failed",
    "artifact_missing",
    "storage_failed",
    "digest_mismatch",
    "inputs_changed_while_rendering",
    "mixed_reading",
    "candidate_identity_conflict",
)

_PREPARATION_REFUSAL_REASONS_SQL = ", ".join(
    f"'{reason}'" for reason in PREPARATION_REFUSAL_REASONS
)

# The three outcomes one finished preparation attempt may record (#675).
# There is deliberately no ``running`` or ``queued`` member: an attempt row is
# appended when the attempt *finishes*, so the relation stays append-only and
# the immutability trigger covers it whole. "Preparing" is the derived answer
# for a request no attempt has finished yet, which is exactly what the Issue
# section says while a worker is at it.
PREPARATION_ATTEMPT_OUTCOMES = ("prepared", "refused", "failed")

_PREPARATION_ATTEMPT_OUTCOMES_SQL = ", ".join(
    f"'{outcome}'" for outcome in PREPARATION_ATTEMPT_OUTCOMES
)

# ADR-0086's three derived outcomes for a candidate that exists.
READY = "ready"
READY_WITH_EXCEPTIONS = "ready_with_exceptions"
BLOCKED = "blocked"
RELEASE_READINESS_STATES = (READY, READY_WITH_EXCEPTIONS, BLOCKED)

_RELEASE_READINESS_SQL = ", ".join(
    f"'{state}'" for state in RELEASE_READINESS_STATES
)


class ReleasePackage(Base):
    """One authorized external issue, and the receipt that binds it (#533).

    #529 created this relation empty so a candidate could name a predecessor;
    #533 is what writes it, and #635 is why it carries so many columns. The
    receipt binds the candidate, the accepted revision, the previous authorized
    package or an explicit none, the source cutoff, the coverage identity and
    digest, the issue-profile identity and version, the template and mapping
    registrations, every artifact identity and digest, the releaser and the
    release time (ADR-0086).

    **The candidate binding is composite.** ``(candidate_id, project_id,
    accepted_revision_id)`` references ``release_candidates (id, project_id,
    accepted_revision_id)``, so the receipt states the revision it released
    explicitly *and* proves it is the revision the candidate was prepared from.
    A receipt whose revision disagrees with its candidate's is not a row this
    schema can hold — which is precisely what ``external_report_releases``
    could not say, and why that table is never a predecessor.

    **The predecessor is a chain, never a clock.** ``sequence_number`` is the
    predecessor's own number plus one, bound to the predecessor row by a
    composite key. One root per project and one successor per package: a first
    release has no predecessor and invents none, and a later release has
    exactly one. Nothing orders releases by ``authorized_at``.

    **The identity is the digest of the receipt bytes.** ``package_identity``
    is the SHA-256 of ``receipt_declaration``, checked by the database, so a
    receipt that does not digest to what it claims cannot be stored.
    """

    __tablename__ = "release_packages"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_release_packages_row"),
        UniqueConstraint(
            "project_id", "package_identity", name="uq_release_packages_identity"
        ),
        # One receipt per candidate: a candidate cannot be attached to two
        # divergent releases, and a replay converges on the row that exists.
        UniqueConstraint("candidate_id", name="uq_release_packages_candidate"),
        UniqueConstraint(
            "id", "project_id", "sequence_number", name="uq_release_packages_sequence"
        ),
        # One successor per package, so the history is a chain and not a fork.
        UniqueConstraint(
            "previous_package_id", name="uq_release_packages_successor"
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_packages_revision",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id", "accepted_revision_id"],
            [
                "release_candidates.id",
                "release_candidates.project_id",
                "release_candidates.accepted_revision_id",
            ],
            name="fk_release_packages_candidate",
            # The candidate names its predecessor package and the package names
            # its candidate, so the two relations reference each other. The
            # migration creates them in order and this cycle exists only in the
            # metadata graph; `use_alter` tells SQLAlchemy which edge to ignore
            # when it sorts tables, so table-ordered readers (the committed
            # scenario cleanup, the architecture ratchets) keep a usable order
            # instead of silently dropping every foreign key between the two.
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id", "previous_sequence_number"],
            [
                "release_packages.id",
                "release_packages.project_id",
                "release_packages.sequence_number",
            ],
            name="fk_release_packages_previous",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_packages_profile",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_packages_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_packages_mapping",
        ),
        CheckConstraint(
            "length(btrim(package_identity)) > 0",
            name="ck_release_packages_identity",
        ),
        CheckConstraint(
            "length(btrim(authorized_by_principal)) > 0",
            name="ck_release_packages_principal",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(receipt_declaration, 'utf8')), 'hex') "
            "= package_identity",
            name="ck_release_packages_receipt_digest",
        ),
        CheckConstraint(
            "length(btrim(receipt_schema_version)) > 0",
            name="ck_release_packages_receipt_schema",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_release_packages_coverage",
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0 "
            "and length(btrim(ucm_storage_key)) > 0 "
            "and ucm_byte_count > 0",
            name="ck_release_packages_ucm",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_packages_profile_version",
        ),
        CheckConstraint(
            "(previous_package_id is null and sequence_number = 1 "
            "and previous_sequence_number is null) "
            "or (previous_package_id is not null "
            "and previous_sequence_number is not null "
            "and sequence_number = previous_sequence_number + 1)",
            name="ck_release_packages_succession",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    package_identity: Mapped[str] = mapped_column(String(160))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    authorized_by_principal: Mapped[str] = mapped_column(String(128))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    candidate_identity: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    previous_sequence_number: Mapped[int | None] = mapped_column(Integer)
    sequence_number: Mapped[int] = mapped_column(Integer)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_identity: Mapped[str] = mapped_column(String(160))
    coverage_sha256: Mapped[str] = mapped_column(String(64))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    issue_profile_sha256: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    ucm_content_sha256: Mapped[str] = mapped_column(String(64))
    ucm_storage_key: Mapped[str] = mapped_column(String(160))
    ucm_byte_count: Mapped[int] = mapped_column(BigInteger)
    receipt_declaration: Mapped[str] = mapped_column(Text)
    receipt_schema_version: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePackageArtifact(Base):
    """One configured member of an authorized package's artifact set (#533).

    Copied from the candidate's own rows by the authorization command, never
    supplied by a caller, so the receipt's enumeration cannot disagree with the
    candidate it seals. ``updated_ucm`` is not an admitted type here for #529's
    reason: the mandatory member is ``ReleasePackage``'s own columns, so a
    package with no UCM and a package with two are both unrepresentable.
    """

    __tablename__ = "release_package_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "artifact_type", name="uq_release_package_artifacts_type"
        ),
        UniqueConstraint(
            "package_id", "position", name="uq_release_package_artifacts_position"
        ),
        ForeignKeyConstraint(
            ["package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_package_artifacts_package",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_release_package_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_release_package_artifacts_renderer",
        ),
        CheckConstraint(
            "length(btrim(storage_key)) > 0 and byte_count > 0",
            name="ck_release_package_artifacts_bytes",
        ),
        CheckConstraint(
            "position >= 1", name="ck_release_package_artifacts_position"
        ),
        Index("ix_release_package_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    package_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(160))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer)


class ReleaseCandidate(Base):
    """One prepared, immutable issue for review (#529, ADR-0086, ADR-0091).

    ``candidate_identity`` is the SHA-256 of ``input_declaration``, which holds
    every bound input: the project, the accepted revision, the previous
    authorized package or an explicit none, the source cutoff, the coverage
    identity and digest, the issue-profile row, identity, version and digest,
    the template and mapping registrations and digests, the configured artifact
    types, the renderer identities and versions, the product and code revision,
    and the enabled feature flags. ``content_sha256`` digests a declaration
    that repeats all of that and adds the ordered artifact identities and their
    own digests, so a candidate identity answers "were these the same inputs"
    and a content digest answers "is this the same issue".

    The updated UCM is a set of columns rather than an artifact row, carrying
    #640's structural decision forward: a candidate with no UCM and a candidate
    with two are both unrepresentable.
    """

    __tablename__ = "release_candidates"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_release_candidates_row"),
        UniqueConstraint(
            "project_id",
            "candidate_identity",
            name="uq_release_candidates_identity",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_candidates_revision",
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_candidates_previous",
        ),
        # The confirmed declaration this candidate was prepared under (#675),
        # by identity rather than by the coverage identity and digest alone:
        # those two say what the coverage was, and this says whose confirmation
        # of which derived reading authorised preparing under it.
        ForeignKeyConstraint(
            ["coverage_declaration_id", "project_id"],
            [
                "issue_coverage_declarations.id",
                "issue_coverage_declarations.project_id",
            ],
            name="fk_release_candidates_coverage",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_candidates_profile",
        ),
        ForeignKeyConstraint(
            ["output_template_format_id", "project_id", "output_template_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_candidates_template",
        ),
        ForeignKeyConstraint(
            ["field_mapping_format_id", "project_id", "field_mapping_kind"],
            [
                "project_baseline_formats.id",
                "project_baseline_formats.project_id",
                "project_baseline_formats.format_kind",
            ],
            name="fk_release_candidates_mapping",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(input_declaration, 'utf8')), 'hex') "
            "= candidate_identity",
            name="ck_release_candidates_identity_digest",
        ),
        CheckConstraint(
            "encode(sha256(convert_to(content_declaration, 'utf8')), 'hex') "
            "= content_sha256",
            name="ck_release_candidates_content_digest",
        ),
        CheckConstraint(
            "length(btrim(input_schema_version)) > 0",
            name="ck_release_candidates_schema",
        ),
        CheckConstraint(
            "length(btrim(coverage_identity)) > 0",
            name="ck_release_candidates_coverage",
        ),
        CheckConstraint(
            "length(btrim(ucm_renderer_identity)) > 0 "
            "and length(btrim(ucm_renderer_version)) > 0 "
            "and length(btrim(ucm_storage_key)) > 0 "
            "and ucm_byte_count > 0",
            name="ck_release_candidates_ucm",
        ),
        CheckConstraint(
            f"readiness in ({_RELEASE_READINESS_SQL})",
            name="ck_release_candidates_readiness",
        ),
        CheckConstraint(
            "length(btrim(prepared_by_principal)) > 0",
            name="ck_release_candidates_principal",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_candidates_profile_version",
        ),
        Index(
            "ix_release_candidates_project_prepared", "project_id", "prepared_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    candidate_identity: Mapped[str] = mapped_column(String(64))
    input_declaration: Mapped[str] = mapped_column(Text)
    input_schema_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_declaration: Mapped[str] = mapped_column(Text)
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_declaration_id: Mapped[int] = mapped_column(BigInteger)
    coverage_identity: Mapped[str] = mapped_column(String(160))
    coverage_sha256: Mapped[str] = mapped_column(String(64))
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    issue_profile_sha256: Mapped[str] = mapped_column(String(64))
    output_template_format_id: Mapped[int] = mapped_column(BigInteger)
    output_template_kind: Mapped[str] = mapped_column(
        String(32), Computed("'output_template'", persisted=True)
    )
    field_mapping_format_id: Mapped[int] = mapped_column(BigInteger)
    field_mapping_kind: Mapped[str] = mapped_column(
        String(32), Computed("'field_mapping'", persisted=True)
    )
    code_revision: Mapped[str] = mapped_column(String(160))
    product_revision: Mapped[str] = mapped_column(String(64))
    ucm_renderer_identity: Mapped[str] = mapped_column(String(160))
    ucm_renderer_version: Mapped[str] = mapped_column(String(64))
    ucm_content_sha256: Mapped[str] = mapped_column(String(64))
    ucm_storage_key: Mapped[str] = mapped_column(String(160))
    ucm_byte_count: Mapped[int] = mapped_column(BigInteger)
    readiness: Mapped[str] = mapped_column(String(32))
    prepared_by_principal: Mapped[str] = mapped_column(String(128))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleaseCandidateArtifact(Base):
    """One configured member of a prepared candidate's artifact set (#529).

    Only what ADR-0091 made a per-project choice lives here. ``updated_ucm`` is
    not an admitted type: the mandatory member is ``ReleaseCandidate``'s own
    columns, so it can be neither dropped from a candidate nor entered twice.
    The composite foreign key carries ``project_id``, so an artifact of one
    project cannot be attached to another project's candidate.
    """

    __tablename__ = "release_candidate_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "artifact_type",
            name="uq_release_candidate_artifacts_type",
        ),
        UniqueConstraint(
            "candidate_id",
            "position",
            name="uq_release_candidate_artifacts_position",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id"],
            ["release_candidates.id", "release_candidates.project_id"],
            name="fk_release_candidate_artifacts_candidate",
        ),
        CheckConstraint(
            f"artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})",
            name="ck_release_candidate_artifacts_type",
        ),
        CheckConstraint(
            "length(btrim(renderer_identity)) > 0 "
            "and length(btrim(renderer_version)) > 0",
            name="ck_release_candidate_artifacts_renderer",
        ),
        CheckConstraint(
            "length(btrim(storage_key)) > 0 and byte_count > 0",
            name="ck_release_candidate_artifacts_bytes",
        ),
        CheckConstraint(
            "position >= 1", name="ck_release_candidate_artifacts_position"
        ),
        Index("ix_release_candidate_artifacts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    artifact_type: Mapped[str] = mapped_column(String(48))
    renderer_identity: Mapped[str] = mapped_column(String(160))
    renderer_version: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(160))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer)


class ReleasePreparationRefusal(Base):
    """The receipt a refused preparation leaves, and the only thing it leaves.

    A failed preparation writes no candidate and no artifact row, so this is
    where the reason lives. ``reason_code`` is one of a closed vocabulary and
    ``reason`` is a sentence a person reads, never a captured traceback.
    """

    __tablename__ = "release_preparation_refusals"
    __table_args__ = (
        CheckConstraint(
            f"reason_code in ({_PREPARATION_REFUSAL_REASONS_SQL})",
            name="ck_release_preparation_refusals_code",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0 and length(reason) <= 2000",
            name="ck_release_preparation_refusals_reason",
        ),
        CheckConstraint(
            "length(btrim(refused_by_principal)) > 0",
            name="ck_release_preparation_refusals_principal",
        ),
        Index(
            "ix_release_preparation_refusals_project", "project_id", "refused_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    candidate_identity: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(Text)
    refused_by_principal: Mapped[str] = mapped_column(String(128))
    refused_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- #675 What one preparation was asked for, and what each attempt did -----

class ReleasePreparationRequest(Base):
    """One idempotent request that a worker prepare this project's next issue.

    #536 promised Review -> Follow-up -> Issue as one executable path and had
    no way to make a candidate, because #529 renders outside its own short
    transactions and needs a session *factory*: running it inside the HTTP
    request would be long, fragile, and ambiguous to retry. This row is what
    the request leaves behind instead. It records what the coordinator was
    looking at when they asked -- the accepted revision, the issue profile
    version, the source cutoff, and the confirmed coverage declaration -- so
    the worker prepares the issue that was confirmed rather than whatever the
    project happens to hold when it gets round to it.

    There is deliberately no status column and no mutable "weekly close"
    object. Status is derived from this relation and
    ``release_preparation_attempts``, which is the same discipline ADR-0085
    used to refuse a stored packet lifecycle and #537 used to refuse a stored
    cross-project queue: a second authority beside the append-only records
    would be stale the moment one of them moved, and somebody would have to
    tick it.

    **Who ``requested_by_principal`` may name is PostgreSQL's rule** (#839).
    ``enforce_coordination_designation`` fires before every insert and reads
    the roster, so asking for a customer's issue to be built is the Project
    Coordination act the maintainer settled it as, and not something project
    membership carries.
    """

    __tablename__ = "release_preparation_requests"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_requests_row"
        ),
        # The idempotency the ticket asks for: a coordinator who submits twice,
        # or a retried POST, converges on the request already recorded rather
        # than queueing a second preparation of the same issue.
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_release_preparation_requests_key",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_preparation_requests_revision",
        ),
        ForeignKeyConstraint(
            [
                "issue_profile_id",
                "project_id",
                "issue_profile_identity",
                "issue_profile_version",
            ],
            [
                "project_issue_profiles.id",
                "project_issue_profiles.project_id",
                "project_issue_profiles.profile_identity",
                "project_issue_profiles.profile_version",
            ],
            name="fk_release_preparation_requests_profile",
        ),
        ForeignKeyConstraint(
            ["coverage_declaration_id", "project_id"],
            [
                "issue_coverage_declarations.id",
                "issue_coverage_declarations.project_id",
            ],
            name="fk_release_preparation_requests_coverage",
        ),
        CheckConstraint(
            "length(btrim(requested_by_principal)) > 0",
            name="ck_release_preparation_requests_principal",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_release_preparation_requests_key",
        ),
        CheckConstraint(
            "issue_profile_version >= 1",
            name="ck_release_preparation_requests_version",
        ),
        Index(
            "ix_release_preparation_requests_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_id: Mapped[int] = mapped_column(BigInteger)
    issue_profile_identity: Mapped[str] = mapped_column(String(160))
    issue_profile_version: Mapped[int] = mapped_column(Integer)
    coverage_declaration_id: Mapped[int] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_by_principal: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationAttempt(Base):
    """What one finished attempt at one preparation request produced (#675).

    Exactly one of the three outcomes, and the check constraint makes the other
    two unrepresentable in the same row: a ``prepared`` attempt names its
    candidate and no reason, and a ``refused`` or ``failed`` attempt names a
    bounded reason and no candidate. That is #529's own three-outcome rule
    carried into the record a coordinator's screen is derived from, so
    "preparation left a partial candidate" is not a state this relation can
    describe.

    ``started_at`` and ``finished_at`` are both declared by the worker and both
    recorded on the one row, which is why a row is appended when an attempt
    finishes rather than claimed when it begins. A relation that were claimed
    first and completed later would need an UPDATE, and every release relation
    beside it refuses one.
    """

    __tablename__ = "release_preparation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_attempts_row"
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_attempts_request",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "project_id"],
            ["release_candidates.id", "release_candidates.project_id"],
            name="fk_release_preparation_attempts_candidate",
        ),
        CheckConstraint(
            f"outcome in ({_PREPARATION_ATTEMPT_OUTCOMES_SQL})",
            name="ck_release_preparation_attempts_outcome",
        ),
        CheckConstraint(
            "(outcome = 'prepared' and candidate_id is not null "
            "and refusal_code is null and reason is null) "
            "or (outcome = 'refused' and candidate_id is null "
            "and refusal_code is not null and reason is not null) "
            "or (outcome = 'failed' and candidate_id is null "
            "and refusal_code is null and reason is not null)",
            name="ck_release_preparation_attempts_result",
        ),
        CheckConstraint(
            f"refusal_code is null or refusal_code in "
            f"({_PREPARATION_REFUSAL_REASONS_SQL})",
            name="ck_release_preparation_attempts_code",
        ),
        CheckConstraint(
            "reason is null or (length(btrim(reason)) > 0 "
            "and length(reason) <= 2000)",
            name="ck_release_preparation_attempts_reason",
        ),
        CheckConstraint(
            "finished_at >= started_at",
            name="ck_release_preparation_attempts_span",
        ),
        Index(
            "ix_release_preparation_attempts_request", "request_id", "id"
        ),
        Index(
            "ix_release_preparation_attempts_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(32))
    candidate_id: Mapped[int | None] = mapped_column(BigInteger)
    refusal_code: Mapped[str | None] = mapped_column(String(48))
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationReading(Base):
    """The one report-preparation reading one request is prepared under (#690).

    Not "the latest completed receipt for this project", and the difference is
    the whole point. The weekly pass keeps completing receipts, so a request
    that resolved the newest one whenever a worker got round to it would
    prepare a different window from the one the coordinator confirmed, and a
    preparation that failed could advance the next reading's floor. The
    binding is written once, is unique per request, and every retry of that
    request reuses it.

    The window is a watermark pair and never a pair of timestamps (#488). The
    **ceilings** are frozen here at the moment of binding; the **floors** come
    from the reading bound to the previous *authorized* package, or zero for a
    first issue, because ADR-0086 is explicit that only an authorized package
    advances the external comparison baseline. A candidate that was merely
    prepared, blocked or refused moves nothing.

    The result itself stays owned by ``DueWorkReceipt.handler_result_json``.
    This row says *which* retained reading was used and what its bytes
    digested to; copying the reading here would copy state another row already
    owns, which is the defect #598's ratchet refuses.
    """

    __tablename__ = "release_preparation_readings"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_readings_row"
        ),
        UniqueConstraint(
            "request_id", name="uq_release_preparation_readings_request"
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_readings_request",
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_release_preparation_readings_revision",
        ),
        ForeignKeyConstraint(
            ["previous_package_id", "project_id"],
            ["release_packages.id", "release_packages.project_id"],
            name="fk_release_preparation_readings_previous",
        ),
        CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_release_preparation_readings_digest",
        ),
        CheckConstraint(
            "handler_key = 'report_preparation'",
            name="ck_release_preparation_readings_handler",
        ),
        CheckConstraint(
            "length(btrim(result_schema_version)) > 0",
            name="ck_release_preparation_readings_schema",
        ),
        CheckConstraint(
            "prior_delta_floor >= 0 and prior_disposition_floor >= 0",
            name="ck_release_preparation_readings_floors",
        ),
        CheckConstraint(
            "through_delta_id >= prior_delta_floor "
            "and through_disposition_id >= prior_disposition_floor",
            name="ck_release_preparation_readings_window",
        ),
        Index(
            "ix_release_preparation_readings_project", "project_id", "id"
        ),
        Index("ix_release_preparation_readings_receipt", "receipt_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    request_id: Mapped[int] = mapped_column(BigInteger)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("due_work_receipts.id"))
    handler_key: Mapped[str] = mapped_column(String(64))
    result_schema_version: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))
    accepted_revision_id: Mapped[int] = mapped_column(BigInteger)
    source_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_package_id: Mapped[int | None] = mapped_column(BigInteger)
    prior_delta_floor: Mapped[int] = mapped_column(BigInteger)
    prior_disposition_floor: Mapped[int] = mapped_column(BigInteger)
    through_delta_id: Mapped[int] = mapped_column(BigInteger)
    through_disposition_id: Mapped[int] = mapped_column(BigInteger)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReleasePreparationPublication(Base):
    """One Due Work occurrence, one preparation request (#690).

    Occurrences are otherwise coalesced from a cadence slot, and a slot cannot
    say which request it is for. One opaque occurrence processing every pending
    request would also give them one shared lease, one shared retry budget and
    one shared receipt, so a single unlucky request would burn the lot. Unique
    both ways: a reclaimed occurrence resumes the request it was published for
    and no other.
    """

    __tablename__ = "release_preparation_publications"
    __table_args__ = (
        UniqueConstraint(
            "id", "project_id", name="uq_release_preparation_publications_row"
        ),
        UniqueConstraint(
            "request_id", name="uq_release_preparation_publications_request"
        ),
        UniqueConstraint(
            "occurrence_id",
            name="uq_release_preparation_publications_occurrence",
        ),
        ForeignKeyConstraint(
            ["request_id", "project_id"],
            [
                "release_preparation_requests.id",
                "release_preparation_requests.project_id",
            ],
            name="fk_release_preparation_publications_request",
        ),
        Index(
            "ix_release_preparation_publications_project", "project_id", "id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    request_id: Mapped[int] = mapped_column(BigInteger)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id")
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReportRun(Base):
    """A published report, kept so the next one can say what changed.

    `ruleset_version` is stored per run for a specific reason: a figure that
    moved between two weekly reports must be attributable to a *rule* change
    or a *data* change, and those call for opposite responses. Tightening
    STALE from 14 days to 10 looks identical to a project falling behind
    unless the report remembers which ruleset produced each number.

    `revision_id` is the accepted Project Record revision the reading was
    taken against (#602). It is the authority for every value the *record*
    owns. `snapshot_json` beside it is the immutable Report Reading payload:
    what this dated occurrence published, which is a different ownership and
    not a copy of the revision (ADR-0092).
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_report_runs_revision",
        ),
        Index("ix_report_runs_revision_id", "revision_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The accepted revision this reading was taken against.  Nullable only
    # because rows written before #602 are not rewritten, and because a
    # project with no accepted revision at all has no identity to name; a
    # trigger refuses a new row that omits one when the project has any.
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    retirement_archive_id: Mapped[int | None] = mapped_column(ForeignKey("legacy_ledger_archives.id"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ruleset_version: Mapped[str] = mapped_column(String(32))
    # The immutable Report Reading payload (ADR-0092): the population this
    # report covered, its derived documentation-requirement results and
    # Constraint Alerts, the statement-projected Promised For, and the rules
    # and thresholds that produced them.  None of those is a revision's to
    # answer, so this is the occurrence's own evidence rather than a cache of
    # `revision_id` above; it is retained as long as the run is and expires on
    # no cache TTL.  `report_reading` owns its schema version, content digest
    # and the translation that keeps a version 1 payload readable.
    snapshot_json: Mapped[dict] = mapped_column(JSONB)
    output_path: Mapped[str | None] = mapped_column(Text)
    # Document-only reports are a separate comparison lineage: comparing one
    # against the ordinary report would leak a verbal date through its old
    # snapshot into an otherwise citation-only surface.
    document_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class ProjectCheckConfiguration(Base):
    """One declared, retained per-project configuration of the check thresholds.

    The exception engine's thresholds — how many days of document silence is
    STALE, how near a Need Date is DUE_SOON, how near a Next Action is
    ACTION_DUE_SOON — were fixed module constants, varied only by a test
    passing a ``Thresholds`` into ``evaluate*``.  A project that runs on a
    different cadence had no supported way to declare its own horizons.

    Each save is a new identity, never an edit: the effective configuration
    is the newest row for the project, and every earlier row — with the
    person who declared it and when — stays readable so a report published
    under it remains explainable.  No row means the supported module
    defaults, unchanged.  The values only parameterize the existing rules;
    this table introduces no new rule, urgency, or model behavior.  The
    append-only guarantee is enforced by a trigger, matching the other
    provenance tables (ADR-0044 keeps the reading derived — nothing here
    rewrites a past Evaluation).
    """

    __tablename__ = "project_check_configurations"
    __table_args__ = (
        CheckConstraint(
            "stale_days between 1 and 3650",
            name="ck_project_check_configurations_stale_days",
        ),
        CheckConstraint(
            "due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_due_soon_days",
        ),
        CheckConstraint(
            "action_due_soon_days between 1 and 3650",
            name="ck_project_check_configurations_action_due_soon_days",
        ),
        CheckConstraint(
            "length(trim(ruleset_version)) > 0",
            name="ck_project_check_configurations_ruleset_version",
        ),
        CheckConstraint(
            "length(trim(created_by)) > 0",
            name="ck_project_check_configurations_created_by",
        ),
        Index("ix_project_check_configurations_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # The ruleset the declared thresholds parameterize, recorded so a later
    # reader never reads these day-counts against a different rule meaning.
    ruleset_version: Mapped[str] = mapped_column(String(32))
    stale_days: Mapped[int] = mapped_column(Integer)
    due_soon_days: Mapped[int] = mapped_column(Integer)
    action_due_soon_days: Mapped[int] = mapped_column(Integer)
    # The stable human subject who declared it, from the deployment identity
    # seam — never a form-supplied author (M8; production auth is #331).
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExternalReportArtifact(Base):
    """One immutable, already-rendered External Report PDF.

    Rendering is deliberately separate from human release.  This table owns
    the exact PDF and frozen Report context a project person can later choose;
    a new release receipt references this immutable owner so retention never
    depends on an artifact URL, path, or regenerating ReportRun.  Legacy
    receipts may still retain their historical copies.
    """

    __tablename__ = "external_report_artifacts"
    __table_args__ = (
        CheckConstraint("format = 'pdf'", name="ck_external_report_artifacts_pdf_only"),
        CheckConstraint(
            "pdf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_external_report_artifacts_pdf_sha256",
        ),
        CheckConstraint(
            "octet_length(pdf_bytes) > 5",
            name="ck_external_report_artifacts_nonempty_pdf",
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_external_report_artifacts_provenance_mode",
        ),
        CheckConstraint(
            "jsonb_typeof(evaluation_context_json) = 'object'",
            name="ck_external_report_artifacts_evaluation_object",
        ),
        CheckConstraint(
            "jsonb_typeof(record_context_json) = 'object'",
            name="ck_external_report_artifacts_context_object",
        ),
        CheckConstraint(
            "length(trim(artifact_name)) > 0",
            name="ck_external_report_artifacts_artifact_name",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    artifact_name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), server_default="pdf")
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    evaluation_context_json: Mapped[dict] = mapped_column(JSONB)
    provenance_mode: Mapped[str] = mapped_column(String(32))
    record_context_json: Mapped[dict] = mapped_column(JSONB)
    rendered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def digest_is_valid(self) -> bool:
        """Whether the retained bytes still match the rendered artifact digest."""
        return sha256(self.pdf_bytes).hexdigest() == self.pdf_sha256


class ExternalReportRelease(Base):
    """One immutable authorization of one fixed External Report artifact.

    A working ``ReportRun`` lets the next internal report describe change;
    it is deliberately not an artifact authority.  This receipt instead
    references the immutable artifact that owns the PDF and frozen context.
    Legacy receipts may still own their copied bytes and context directly;
    the read properties below preserve that history without copying new
    releases (ADR-0040, ADR-0072).
    """

    __tablename__ = "external_report_releases"
    __table_args__ = (
        CheckConstraint("format = 'pdf'", name="ck_external_report_releases_pdf_only"),
        CheckConstraint(
            "pdf_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_external_report_releases_pdf_sha256",
        ),
        CheckConstraint(
            "pdf_bytes is null or octet_length(pdf_bytes) > 5",
            name="ck_external_report_releases_nonempty_pdf",
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_external_report_releases_provenance_mode",
        ),
        CheckConstraint(
            "record_context_json is null or jsonb_typeof(record_context_json) = 'object'",
            name="ck_external_report_releases_context_object",
        ),
        CheckConstraint(
            "evaluation_context_json is null or "
            "jsonb_typeof(evaluation_context_json) = 'object'",
            name="ck_external_report_releases_evaluation_object",
        ),
        CheckConstraint(
            "length(trim(artifact_name)) > 0",
            name="ck_external_report_releases_artifact_name",
        ),
        CheckConstraint(
            "length(trim(released_by)) > 0",
            name="ck_external_report_releases_released_by",
        ),
        CheckConstraint(
            "released_by_display is not null and length(trim(released_by_display)) > 0",
            name="ck_external_report_releases_released_by_display",
        ),
        CheckConstraint(
            "(content_storage = 'legacy' and pdf_bytes is not null "
            "and record_context_json is not null) or "
            "(content_storage = 'artifact' and artifact_id is not null "
            "and pdf_bytes is null and evaluation_context_json is null "
            "and record_context_json is null)",
            name="ck_external_report_releases_content_owner",
        ),
        UniqueConstraint("artifact_id", name="uq_external_report_releases_artifact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_report_artifacts.id")
    )
    artifact_name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), server_default="pdf")
    content_storage: Mapped[str] = mapped_column(
        String(16), server_default="artifact"
    )
    _legacy_pdf_bytes: Mapped[bytes | None] = mapped_column("pdf_bytes", LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    evaluated_on: Mapped[date] = mapped_column(Date)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    _legacy_evaluation_context_json: Mapped[dict | None] = mapped_column(
        "evaluation_context_json", JSONB
    )
    provenance_mode: Mapped[str] = mapped_column(String(32))
    _legacy_record_context_json: Mapped[dict | None] = mapped_column(
        "record_context_json", JSONB
    )
    released_by: Mapped[str] = mapped_column(String(128))
    released_by_display: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    _artifact: Mapped[ExternalReportArtifact | None] = relationship(lazy="joined")

    @property
    def pdf_bytes(self) -> bytes:
        """Read new artifact bytes or the retained bytes of a legacy release."""
        if self._legacy_pdf_bytes is not None:
            return self._legacy_pdf_bytes
        if self._artifact is None:
            raise ValueError("released External Report has no retained artifact")
        return self._artifact.pdf_bytes

    @pdf_bytes.setter
    def pdf_bytes(self, value: bytes | None) -> None:
        """Accept legacy-row construction without making new services copy bytes."""
        self._legacy_pdf_bytes = value

    @property
    def evaluation_context_json(self) -> dict | None:
        """Read frozen Evaluation context from its one durable owner."""
        if self._legacy_evaluation_context_json is not None:
            return deepcopy(self._legacy_evaluation_context_json)
        if self._artifact is None:
            return None
        return deepcopy(self._artifact.evaluation_context_json)

    @evaluation_context_json.setter
    def evaluation_context_json(self, value: dict | None) -> None:
        """Accept legacy-row construction without copying new release context."""
        self._legacy_evaluation_context_json = deepcopy(value)

    @property
    def record_context_json(self) -> dict:
        """Read frozen Project Record context from its one durable owner."""
        if self._legacy_record_context_json is not None:
            return deepcopy(self._legacy_record_context_json)
        if self._artifact is None:
            raise ValueError("released External Report has no retained record context")
        return deepcopy(self._artifact.record_context_json)

    @record_context_json.setter
    def record_context_json(self, value: dict | None) -> None:
        """Accept legacy-row construction without copying new release context."""
        self._legacy_record_context_json = deepcopy(value)

    @property
    def digest_is_valid(self) -> bool:
        """Whether the receipt and its one retained byte owner share a digest."""
        if self._legacy_pdf_bytes is not None:
            return sha256(self._legacy_pdf_bytes).hexdigest() == self.pdf_sha256
        return (
            self._artifact is not None
            and self._artifact.pdf_sha256 == self.pdf_sha256
            and self._artifact.digest_is_valid
        )


class ScheduledReportPublication(Base):
    """One retained weekly reading a scheduled Due Work occurrence produced.

    A scheduled publication supplements the working view; it never becomes a
    ``ReportRun`` and so never advances the internal report's comparison
    baseline (ADR-0053).  Each row binds one occurrence to one coherent project
    reading, the predecessor it compared against — the last Report Approved for
    Release at observation time, ``NULL`` before a project's first release — and,
    when the schedule declared external preparation, the exact prepared PDF
    artifact awaiting a separate human release (ADR-0040).

    The unique occurrence binding is the convergence guarantee: repeated
    triggers, competing workers, abandoned claims, and retries all resolve to
    this one row rather than a second snapshot or artifact.  The row is
    append-only; a refreshed working view or a later release cannot rewrite the
    predecessor or comparison window it recorded.
    """

    __tablename__ = "scheduled_report_publications"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", name="uq_scheduled_report_publication_occurrence"
        ),
        CheckConstraint(
            "provenance_mode in ('all-supported-sources', 'document-only')",
            name="ck_scheduled_report_publication_provenance_mode",
        ),
        CheckConstraint(
            "(predecessor_release_id is null) = (window_start is null)",
            name="ck_scheduled_report_publication_predecessor_window",
        ),
        CheckConstraint(
            "(window_start is null) = (comparison_window_days is null)",
            name="ck_scheduled_report_publication_window_days",
        ),
        CheckConstraint(
            "comparison_window_days is null or comparison_window_days >= 0",
            name="ck_scheduled_report_publication_window_nonneg",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot_json) = 'object'",
            name="ck_scheduled_report_publication_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(thresholds_json) = 'object'",
            name="ck_scheduled_report_publication_thresholds_object",
        ),
        ForeignKeyConstraint(
            ["revision_id", "project_id"],
            [
                "project_record_revisions.id",
                "project_record_revisions.project_id",
            ],
            name="fk_scheduled_report_publications_revision",
        ),
        Index("ix_scheduled_report_publications_revision_id", "revision_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("due_work_occurrences.id")
    )
    schedule_id: Mapped[int] = mapped_column(ForeignKey("due_work_schedules.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    configuration_version: Mapped[str] = mapped_column(String(64))
    provenance_mode: Mapped[str] = mapped_column(String(32))
    # Reference ids into two append-only stores.  They are plain ids, not
    # foreign keys, so this retained row never changes the truncate or deletion
    # semantics of the release and artifact tables it points at; those rows are
    # immutable and never removed, so a dangling reference cannot arise.
    predecessor_release_id: Mapped[int | None] = mapped_column(BigInteger)
    prepared_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    # The accepted revision this reading was taken against (#602), under the
    # same rule and the same trigger as ``ReportRun.revision_id``.  Unlike the
    # two reference ids above it does carry a foreign key, a composite one to
    # ``(id, project_id)``: what has to be unrepresentable here is not a
    # dangling row but a reading bound to some other project's revision, and
    # only a key can say that.
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    evaluated_on: Mapped[date] = mapped_column(Date)
    window_start: Mapped[date | None] = mapped_column(Date)
    comparison_window_days: Mapped[int | None] = mapped_column(Integer)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    thresholds_json: Mapped[dict] = mapped_column(JSONB)
    # The immutable Report Reading payload, exactly as on ``ReportRun``: the
    # occurrence's own population, derived outcomes and projected Promised For,
    # retained for as long as this row and its released package are, and never
    # a cache of ``revision_id`` (ADR-0092).
    snapshot_json: Mapped[dict] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
