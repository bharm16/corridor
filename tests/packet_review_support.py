"""One adopted project with a later source revision, built for #527's screen.

The screen under test needs more than Proposed Deltas: an accepted baseline the
incoming values are compared against, the customer's own source rows with the
identifiers their utility-management system printed, a registered output
template so "which customer artifacts would change" has an answer, and the
captured Source Facts and Support Assessments that make an Apply lawful.

Every time is supplied by the caller. Nothing here reads a clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.baseline_adoption import FormatIdentity, effective_baseline_formats
from corridor.field_mapping_manifest import (
    ONE_VALUE_PER_COLUMN,
    FieldMappingManifest,
    MaterialMapping,
)
from corridor.models import (
    BaselineFormat,
    BaselineSource,
    BaselineSourceRow,
    Document,
    ExternalPartyStatement,
    Fact,
    Project,
    ProposedDelta,
    SourceSegment,
)
from corridor.issue_content import (
    CHANGE_SUMMARY_IDENTITY,
    CHANGE_SUMMARY_VERSION,
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
)
from corridor.issue_profile import (
    ArtifactEntry,
    IssueProfileDeclaration,
    RendererRevision,
    register_issue_profile,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
)
from corridor.support_assessments import FactProposition, record_support_assessment
from harness_support import adopt_baseline_fact, as_record_decision_role
from source_capture_support import SHEET, Rendition


ADOPTER = HumanPrincipal("local:adopter")
ASSESSOR = HumanPrincipal("local:assessor")
ASSESSED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
SOURCE_FAMILY = "ucm-workbook"


def subject(row_number: int) -> str:
    """The Project Record subject key one adopted source row resolves to."""

    return f"{SHEET}!{row_number}"


def accept_baseline_fact(session: Session, project: Project, fact: Fact) -> int:
    """One accepted decision for a subject and field, at its own revision."""

    return adopt_baseline_fact(session, project, fact)


def register_source_row(
    session: Session,
    project: Project,
    baseline: BaselineSource,
    *,
    row_number: int,
    business_identity: str,
    external_system_id: str | None = None,
    source_url: str | None = None,
) -> BaselineSourceRow:
    """Append one adopted source row as the record-decision role does (#492)."""

    values = {
        "project_id": project.id,
        "baseline_source_id": baseline.id,
        "source_row_key": f"{SHEET}!{row_number}",
        "sheet_name": SHEET,
        "row_number": row_number,
        "business_identity": business_identity,
        "record_subject_key": subject(row_number),
        "external_system_id": external_system_id,
        "source_url": source_url,
    }
    session.flush()
    with as_record_decision_role(session):
        row_id = session.scalar(
            text(
                "insert into project_baseline_source_rows ("
                "project_id, baseline_source_id, source_row_key, sheet_name,"
                " row_number, business_identity, record_subject_key,"
                " external_system_id, source_url"
                ") values (:project_id, :baseline_source_id, :source_row_key,"
                " :sheet_name, :row_number, :business_identity,"
                " :record_subject_key, :external_system_id, :source_url)"
                " returning id"
            ),
            values,
        )
    return session.get(BaselineSourceRow, int(row_id))


def register_baseline(
    session: Session,
    project: Project,
    document: Document,
    revision_id: int,
) -> BaselineSource:
    """Append the adopted data-baseline identity under its own writer role."""

    values = {
        "project_id": project.id,
        "revision_id": revision_id,
        "document_id": document.id,
        "content_sha256": document.sha256,
        "byte_size": 4096,
        "filename": document.filename,
        "source_identity": "UCM 2026-08",
        "customer": "Test District",
        "source_kind": "ucm_workbook",
        "importer_identity": "packet_review_fixture",
        "importer_version": "v1",
        "preview_fingerprint": sha256(b"preview").hexdigest(),
        "adopted_by_principal": ADOPTER.subject,
        "idempotency_key": f"adopt:{uuid4().hex[:10]}",
    }
    session.flush()
    with as_record_decision_role(session):
        baseline_id = session.scalar(
            text(
                "insert into project_baseline_sources ("
                "project_id, revision_id, document_id, content_sha256, byte_size,"
                " filename, source_identity, customer, source_kind, worksheet_scope,"
                " unknown_columns, coordinator_questions, operations_summary,"
                " importer_identity, importer_version, preview_fingerprint,"
                " adopted_by_principal, idempotency_key"
                ") values (:project_id, :revision_id, :document_id, :content_sha256,"
                " :byte_size, :filename, :source_identity, :customer, :source_kind,"
                " '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb,"
                " :importer_identity, :importer_version, :preview_fingerprint,"
                " :adopted_by_principal, :idempotency_key)"
                " returning id"
            ),
            values,
        )
    return session.get(BaselineSource, int(baseline_id))


def register_output_template(
    session: Session, project: Project, *, identity: str, version: str
) -> BaselineFormat:
    """Register the approved output template the workbook is rendered through."""

    values = {
        "project_id": project.id,
        "format_kind": "output_template",
        "format_identity": identity,
        "format_version": version,
        "content_sha256": sha256(f"{identity}:{version}".encode()).hexdigest(),
        "registered_by_principal": ADOPTER.subject,
        "idempotency_key": f"format:{uuid4().hex[:10]}",
    }
    session.flush()
    with as_record_decision_role(session):
        format_id = session.scalar(
            text(
                "insert into project_baseline_formats ("
                "project_id, format_kind, format_identity, format_version,"
                " content_sha256, registered_by_principal, idempotency_key"
                ") values (:project_id, :format_kind, :format_identity,"
                " :format_version, :content_sha256, :registered_by_principal,"
                " :idempotency_key) returning id"
            ),
            values,
        )
    return session.get(BaselineFormat, int(format_id))


def field_mapping(
    *fields: str, identity: str = "district-ucm-mapping", version: str = "v3"
) -> FieldMappingManifest:
    """One mapping revision targeting exactly the canonical fields named.

    The updated UCM's registered content contract is "the fields this mapping
    targets" (#641), so a test that wants a change to reach the workbook — or
    deliberately not to — says which fields the customer's form carries here.
    """

    return FieldMappingManifest(
        identity=identity,
        version=version,
        mappings=tuple(
            MaterialMapping(
                source_columns=(field.replace("_", " ").title(),),
                target_fields=(field,),
                composition=ONE_VALUE_PER_COLUMN,
            )
            for field in fields
        ),
    )


def register_field_mapping(
    session: Session,
    project: Project,
    manifest: FieldMappingManifest,
    *,
    store_declaration: bool = True,
) -> BaselineFormat:
    """Register the approved field mapping revision, declaration and all (#610).

    Adopt Baseline writes both rows in one act; this fixture writes the same
    two, because the registration alone proves *which* revision was approved
    and only the stored declaration says what that revision targets.
    """

    values = {
        "project_id": project.id,
        "format_kind": "field_mapping",
        "format_identity": manifest.identity,
        "format_version": manifest.version,
        "content_sha256": manifest.content_sha256,
        "registered_by_principal": ADOPTER.subject,
        "idempotency_key": f"mapping:{uuid4().hex[:10]}",
    }
    session.flush()
    with as_record_decision_role(session):
        format_id = session.scalar(
            text(
                "insert into project_baseline_formats ("
                "project_id, format_kind, format_identity, format_version,"
                " content_sha256, registered_by_principal, idempotency_key"
                ") values (:project_id, :format_kind, :format_identity,"
                " :format_version, :content_sha256, :registered_by_principal,"
                " :idempotency_key) returning id"
            ),
            values,
        )
        if not store_declaration:
            # A registration written before #610 stored declarations: it proves
            # which revision was approved and not what that revision declared.
            return session.get(BaselineFormat, int(format_id))
        session.execute(
            text(
                "insert into project_baseline_format_manifests ("
                "format_id, project_id, format_identity, format_version,"
                " content_sha256, manifest_schema_version, declaration"
                ") values (:format_id, :project_id, :format_identity,"
                " :format_version, :content_sha256, :schema, :declaration)"
            ),
            {
                "format_id": int(format_id),
                "project_id": project.id,
                "format_identity": manifest.identity,
                "format_version": manifest.version,
                "content_sha256": manifest.content_sha256,
                "schema": manifest.schema_version,
                "declaration": manifest.declaration_json,
            },
        )
    return session.get(BaselineFormat, int(format_id))


# The registered renderer revisions a project may be configured with, under
# the names ``issue_content`` registers them. A fixture that wants a supported
# configuration names one of these; a fixture that wants an unsupported one
# invents a version, which is exactly the case #641 must surface rather than
# resolve.
UCM_RENDERER = RendererRevision(UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION)
SUMMARY_RENDERER = RendererRevision(CHANGE_SUMMARY_IDENTITY, CHANGE_SUMMARY_VERSION)
REPORT_RENDERER = RendererRevision(WEEKLY_REPORT_IDENTITY, WEEKLY_REPORT_VERSION)
CHASE_RENDERER = RendererRevision(CHASE_LIST_IDENTITY, CHASE_LIST_VERSION)


def configure_issue(
    session: Session,
    project: Project,
    *,
    principal: HumanPrincipal,
    effective_from: datetime,
    ucm: RendererRevision = UCM_RENDERER,
    artifacts: tuple[ArtifactEntry, ...] = (),
    coverage: tuple = (),
    policies: tuple = (),
    mapping: FormatIdentity | None = None,
    key: str | None = None,
):
    """Register what this project externally issues, from what it registered.

    The declaration names the output template and field mapping revisions
    actually in force for the project, because a profile naming a mapping the
    project does not render through describes an issue nobody would receive.
    """

    registered = effective_baseline_formats(session, project.id)
    return register_issue_profile(
        session,
        project_id=project.id,
        profile_identity="customer-issue",
        declaration=IssueProfileDeclaration(
            updated_ucm=ucm,
            output_template=FormatIdentity(
                kind="output_template",
                identity=registered["output_template"].format_identity,
                version=registered["output_template"].format_version,
                content_sha256=registered["output_template"].content_sha256,
            ),
            field_mapping=mapping
            or FormatIdentity(
                kind="field_mapping",
                identity=registered["field_mapping"].format_identity,
                version=registered["field_mapping"].format_version,
                content_sha256=registered["field_mapping"].content_sha256,
            ),
            configured_artifacts=tuple(artifacts),
            coverage_requirements=tuple(coverage),
            decision_blocking_policies=tuple(policies),
        ),
        effective_from=effective_from,
        principal=principal,
        idempotency_key=key or f"profile:{uuid4().hex[:10]}",
    )


def support(
    session: Session,
    project: Project,
    fact: Fact,
    segment: SourceSegment,
    *,
    assessment: str = "supported",
    evidence_role: str = "value_support",
):
    return record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[segment.id],
        evidence_role=evidence_role,
        assessment=assessment,
        authority=ASSESSOR,
        assessed_at=ASSESSED_AT,
    )


def append_deltas(
    session: Session,
    project: Project,
    rendition: Rendition,
    *,
    source_revision: str,
    values: list[ProposedDeltaValues],
    source_family: str = SOURCE_FAMILY,
    is_complete_enumerative_source: bool = True,
    row_accounting_sealed: bool = True,
) -> tuple[ProposedDelta, ...]:
    return create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family=source_family,
        source_revision=source_revision,
        document_id=rendition.document.id,
        deltas=values,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )


def modify(
    *,
    subject_key: str,
    field_name: str,
    accepted_value: object,
    proposed_value: object,
    baseline_revision: int | None,
    change_type: str = "modify",
) -> ProposedDeltaValues:
    return ProposedDeltaValues(
        change_type=change_type,
        target=ExistingSubjectTarget(subject_identity=subject_key, field=field_name),
        accepted_value=accepted_value,
        proposed_value=proposed_value,
        accepted_baseline_revision=(
            f"revision:{baseline_revision}" if baseline_revision is not None else None
        ),
    )


def new_subject(
    *, subject_key: str, fields: tuple[str, ...], baseline_revision: int | None
) -> ProposedDeltaValues:
    return ProposedDeltaValues(
        change_type="add",
        target=ProposedSubjectTarget(
            subject_identity=subject_key, proposed_fields=fields
        ),
        accepted_value=None,
        proposed_value={name: "new" for name in fields},
        accepted_baseline_revision=(
            f"revision:{baseline_revision}" if baseline_revision is not None else None
        ),
    )


def move_accepted_value(
    session: Session, project: Project, fact: Fact
) -> int:
    """Supersede the standing decision for this subject and field with a newer one.

    The accepted record moving under a coordinator mid-review is the exact
    condition #519 refuses on, so a test needs to reproduce it honestly: one
    later revision, one new effective decision, and the predecessor marked
    superseded rather than replaced.
    """

    values = {
        "project_id": project.id,
        "fact_id": fact.id,
        "subject_key": fact.subject_key,
        "fact_type": fact.fact_type,
    }
    session.flush()
    with as_record_decision_role(session):
        session.execute(text("set constraints all deferred"))
        revision_id = session.scalar(
            text(
                "insert into project_record_revisions ("
                "project_id, command_type, human_principal, idempotency_key"
                ") values (:project_id, 'resolve_delta', 'local:corrector', :key)"
                " returning id"
            ),
            {"project_id": project.id, "key": f"move:{uuid4().hex[:12]}"},
        )
        predecessor = session.scalar(
            text(
                "select max(id) from fact_decisions where project_id = :project_id"
                " and subject_key = :subject_key and fact_type = :fact_type"
                " and superseded_by is null"
            ),
            values,
        )
        # The same order the authorized command uses: claim the successor's id,
        # retire the predecessor against it, then insert. The partial unique index
        # on the effective decision is not deferrable, so the other order fails.
        successor = int(session.scalar(text("select nextval('fact_decisions_id_seq')")))
        if predecessor is not None:
            session.execute(
                text("update fact_decisions set superseded_by = :successor where id = :id"),
                {"successor": successor, "id": int(predecessor)},
            )
        session.execute(
            text(
                "insert into fact_decisions ("
                "id, project_id, fact_id, subject_key, fact_type, revision_id,"
                " disposition) values (:id, :project_id, :fact_id, :subject_key,"
                " :fact_type, :revision_id, 'include')"
            ),
            {**values, "id": successor, "revision_id": revision_id},
        )
    session.expire_all()
    return int(revision_id)


def record_statement(
    session: Session,
    project: Project,
    *,
    scope_mode: str = "selected",
    description: str = "the utility committed to the whole block",
) -> ExternalPartyStatement:
    """One attributable External Party Statement at a declared scope mode.

    ``selected`` is a settled Applies To; ``unknown`` is the `not yet known`
    record state that is never a bounded decision (ADR-0035, ADR-0039).
    """

    row = ExternalPartyStatement(
        event_type="commitment",
        project_id=project.id,
        scope_mode=scope_mode,
        source_kind="cited",
        description=description,
        created_by="local:recorder",
    )
    session.add(row)
    session.flush()
    return row


def append_statement_deltas(
    session: Session,
    project: Project,
    statement: ExternalPartyStatement,
    *,
    source_revision: str,
    values: list[ProposedDeltaValues],
    source_family: str = "recorded-statement",
) -> tuple[ProposedDelta, ...]:
    """One statement-bound delta group: the spine's own commitment link."""

    return create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family=source_family,
        source_revision=source_revision,
        statement_id=statement.id,
        deltas=values,
    )
