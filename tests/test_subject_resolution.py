"""Exact subject aliases and attributable human resolution on the spine."""

from hashlib import sha256
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from corridor.models import (
    Dependency,
    Document,
    ExternalOrg,
    Project,
    ProjectRecordRevision,
    StatedByPerson,
    SourceSegment,
    SubjectResolutionAttempt,
    SubjectResolutionCandidate,
    SubjectResolutionDecision,
)
from corridor.principals import HumanPrincipal
from corridor.subject_resolution import (
    SubjectResolutionRefusal,
    decide_subject_alias,
    registered_subject_candidates,
    record_subject_candidate_ranking,
    resolve_subject_reference,
    unresolved_subject_work_items,
)


ALICE = HumanPrincipal("local:subject-resolution-alice")


def _project(session, slug: str, *, project_side_parties=()):
    project = Project(
        slug=slug,
        name=slug,
        is_synthetic=True,
        project_side_parties=list(project_side_parties),
    )
    session.add(project)
    session.flush()
    return project


def _segment(session, project, exact_text: str):
    digest = sha256(f"{project.slug}:{exact_text}".encode()).hexdigest()
    document = Document(
        project_id=project.id,
        sha256=digest,
        filename=f"{project.slug}.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    segment = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="prose_span",
        exact_text=exact_text,
        content_sha256=sha256(exact_text.encode()).hexdigest(),
        ordinal=1,
        sheet_name=None,
        cell_range=None,
        page_no=1,
        start_offset=0,
        end_offset=len(exact_text),
    )
    session.add(segment)
    session.flush()
    return segment


def test_only_an_exact_registered_alias_resolves_with_policy_identity(session):
    project = _project(session, "subject-exact")
    organization = ExternalOrg(name="ABC Gas", aliases=["ABC Pipeline"])
    session.add(organization)
    session.flush()

    exact_segment = _segment(session, project, "ABC Pipeline")
    exact = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=exact_segment.id,
        reference_kind="organization_name",
        raw_reference="  abc PIPELINE ",
        expected_subject_type="external_org",
        usage="identity",
    )

    assert exact.state == "resolved"
    assert exact.subject_type == "external_org"
    assert exact.subject_id == organization.id
    assert exact.rule_identity == "exact-registered-alias-v1"
    persisted = session.get(SubjectResolutionAttempt, exact.attempt_id)
    assert persisted.rule_identity == "exact-registered-alias-v1"

    similar_segment = _segment(session, project, "ABC Gas Company")
    similar = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=similar_segment.id,
        reference_kind="organization_name",
        raw_reference="ABC Gas Company",
        expected_subject_type="external_org",
        usage="identity",
    )

    assert similar.state == "unresolved"
    assert similar.subject_id is None
    assert session.scalars(
        select(SubjectResolutionAttempt).where(
            SubjectResolutionAttempt.project_id == project.id
        )
    ).all().__len__() == 2


def test_one_human_alias_decision_clears_work_and_replays_automatically(session):
    project = _project(session, "subject-human")
    organization = ExternalOrg(name="Southwestern Bell Telephone", aliases=[])
    session.add(organization)
    session.flush()
    first_segment = _segment(
        session, project, "SWBT will relocate its facilities before September."
    )

    unresolved = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=first_segment.id,
        reference_kind="organization_name",
        raw_reference="SWBT",
        expected_subject_type="external_org",
        usage="identity",
    )

    assert unresolved.state == "unresolved"
    work = unresolved_subject_work_items(session, project.id)
    assert [(item.attempt_id, item.attention_reason) for item in work] == [
        (unresolved.attempt_id, "unregistered_subject_reference")
    ]
    duplicate_segment = _segment(
        session, project, "A later source still uses SWBT without explanation."
    )
    duplicate = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=duplicate_segment.id,
        reference_kind="organization_name",
        raw_reference="SWBT",
        expected_subject_type="external_org",
        usage="identity",
    )
    assert [item.attempt_id for item in unresolved_subject_work_items(session, project.id)] == [
        unresolved.attempt_id,
        duplicate.attempt_id,
    ]

    decision = decide_subject_alias(
        session,
        attempt_id=unresolved.attempt_id,
        subject_type="external_org",
        subject_id=organization.id,
        principal=ALICE,
    )

    assert decision.recorded_by == ALICE.subject
    assert decision.decision_kind == "human_alias_registration"
    revision = session.get(ProjectRecordRevision, decision.revision_id)
    assert revision.command_type == "register_subject_alias"
    assert revision.human_principal == ALICE.subject
    assert revision.released_policy is None
    assert unresolved_subject_work_items(session, project.id) == ()
    assert session.scalars(select(SubjectResolutionDecision)).all() == [decision]

    later_segment = _segment(
        session, project, "The minutes identify SWBT as the stated organization."
    )
    replay = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=later_segment.id,
        reference_kind="organization_name",
        raw_reference="SWBT",
        expected_subject_type="external_org",
        usage="identity",
    )

    assert replay.state == "resolved"
    assert replay.subject_id == organization.id
    assert replay.candidates[0].match_source == "human_alias_decision"
    decision.raw_reference = "tampered"
    with pytest.raises(DBAPIError, match="typed decision command"):
        session.flush()


def test_registry_enumeration_keeps_all_subject_types_and_aliases_typed(session):
    project = _project(session, "subject-enumeration")
    organization = ExternalOrg(name="CenterPoint Energy", aliases=["CNP"])
    person = StatedByPerson(
        project_id=project.id,
        display_name="Casey Coordinator",
        aliases=[],
        email_normalized="coordinator@example.com",
    )
    session.add_all([organization, person])
    session.flush()
    constraint = Dependency(
        project_id=project.id,
        ref_code="C-00001",
        source_ref="FOC14-69",
        dep_type="utility_relocation",
        title="Fiber crossing",
    )
    document = Document(
        project_id=project.id,
        sha256=sha256(b"subject-enumeration-document").hexdigest(),
        registry_id="UCM-REV-7",
        filename="ucm-rev-7.xlsx",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add_all([constraint, document])
    session.flush()

    candidates = registered_subject_candidates(session, project.id)

    by_identity = {
        (item.subject_type, item.subject_id): (item.display_name, item.aliases)
        for item in candidates
    }
    assert by_identity[("external_org", organization.id)] == (
        "CenterPoint Energy",
        ("CNP",),
    )
    assert by_identity[("person", person.id)] == (
        "Casey Coordinator",
        ("coordinator@example.com",),
    )
    assert by_identity[("constraint", constraint.id)] == (
        "C-00001",
        ("FOC14-69",),
    )
    assert by_identity[("document", document.id)] == (
        "ucm-rev-7.xlsx",
        ("UCM-REV-7",),
    )
    assert session.scalars(select(SubjectResolutionAttempt)).all() == []


def test_project_side_speaker_never_resolves_as_an_external_organization(session):
    project = _project(
        session,
        "subject-actor-boundary",
        project_side_parties=("LJA Engineering",),
    )
    organization = ExternalOrg(name="LJA Engineering", aliases=[])
    session.add(organization)
    session.flush()
    segment = _segment(
        session,
        project,
        "LJA Engineering said the plans will be ready next week.",
    )

    result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=segment.id,
        reference_kind="organization_name",
        raw_reference="LJA Engineering",
        expected_subject_type="external_org",
        usage="statement_speaker",
    )

    assert result.state == "actor_boundary"
    assert result.subject_id is None
    assert result.attention_reason == "project_side_speaker"
    with pytest.raises(
        SubjectResolutionRefusal,
        match="project-side speaker cannot be registered",
    ):
        decide_subject_alias(
            session,
            attempt_id=result.attempt_id,
            subject_type="external_org",
            subject_id=organization.id,
            principal=ALICE,
        )


def test_individual_speaker_and_affected_organization_remain_distinct(session):
    project = _project(session, "subject-individual-speaker")
    person = StatedByPerson(
        project_id=project.id,
        display_name="Dana Smith",
        aliases=["D. Smith"],
        email_normalized="dana@utility.example",
    )
    organization = ExternalOrg(name="Utility Example", aliases=[])
    session.add_all([person, organization])
    session.flush()
    speaker_segment = _segment(
        session,
        project,
        "Dana Smith said Utility Example would submit the permit package.",
    )

    speaker = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=speaker_segment.id,
        reference_kind="person_name",
        raw_reference="Dana Smith",
        expected_subject_type="person",
        usage="statement_speaker",
    )
    affected = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=speaker_segment.id,
        reference_kind="organization_name",
        raw_reference="Utility Example",
        expected_subject_type="external_org",
        usage="affected_subject",
    )

    assert (speaker.subject_type, speaker.subject_id) == ("person", person.id)
    assert (affected.subject_type, affected.subject_id) == (
        "external_org",
        organization.id,
    )


def test_email_sender_and_domain_alias_decisions_replay_exactly(session):
    project = _project(session, "subject-email-aliases")
    organization = ExternalOrg(name="Utility Example", aliases=[])
    session.add(organization)
    session.flush()

    sender_segment = _segment(
        session, project, "From: records@utility.example"
    )
    sender = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=sender_segment.id,
        reference_kind="email_sender",
        raw_reference="records@utility.example",
        expected_subject_type="external_org",
        usage="statement_speaker",
    )
    assert sender.state == "unresolved"
    decide_subject_alias(
        session,
        attempt_id=sender.attempt_id,
        subject_type="external_org",
        subject_id=organization.id,
        principal=ALICE,
    )
    replay_sender = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=_segment(
            session, project, "Sender records@utility.example replied."
        ).id,
        reference_kind="email_sender",
        raw_reference="records@utility.example",
        expected_subject_type="external_org",
        usage="statement_speaker",
    )
    assert replay_sender.subject_id == organization.id

    domain_segment = _segment(session, project, "From domain: utility.example")
    domain = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=domain_segment.id,
        reference_kind="email_domain",
        raw_reference="@utility.example",
        expected_subject_type="external_org",
        usage="statement_speaker",
    )
    assert domain.state == "unresolved"
    decide_subject_alias(
        session,
        attempt_id=domain.attempt_id,
        subject_type="external_org",
        subject_id=organization.id,
        principal=HumanPrincipal("local:subject-resolution-bob"),
    )
    replay_domain = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=_segment(
            session, project, "Domain utility.example appears again."
        ).id,
        reference_kind="email_domain",
        raw_reference="utility.example",
        expected_subject_type="external_org",
        usage="statement_speaker",
    )
    assert replay_domain.subject_id == organization.id


def test_registered_activity_and_document_identifiers_resolve_to_their_subjects(session):
    project = _project(session, "subject-registered-identifiers")
    activity = Dependency(
        project_id=project.id,
        ref_code="ACT-17",
        source_ref="ROW-17",
        dep_type="utility_relocation",
        title="Registered activity subject",
    )
    document = Document(
        project_id=project.id,
        sha256=sha256(b"registered-document-subject").hexdigest(),
        registry_id="AGR-2026-4",
        filename="agreement.pdf",
        doc_type="agreement",
        parse_status="parsed",
        pages=1,
    )
    session.add_all([activity, document])
    session.flush()

    activity_result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=_segment(session, project, "Activity ACT-17 changed.").id,
        reference_kind="activity_identifier",
        raw_reference="ACT-17",
        expected_subject_type="constraint",
        usage="affected_subject",
    )
    document_result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=_segment(
            session, project, "Document AGR-2026-4 controls."
        ).id,
        reference_kind="document_identifier",
        raw_reference="AGR-2026-4",
        expected_subject_type="document",
        usage="affected_subject",
    )

    assert (activity_result.subject_type, activity_result.subject_id) == (
        "constraint",
        activity.id,
    )
    assert (document_result.subject_type, document_result.subject_id) == (
        "document",
        document.id,
    )


def test_subject_resolution_registry_rows_are_database_append_only(session):
    project = _project(session, "subject-append-only")
    first = Dependency(
        project_id=project.id,
        ref_code="C-1",
        source_ref="DUP-1",
        dep_type="utility_relocation",
        title="First",
    )
    second = Dependency(
        project_id=project.id,
        ref_code="C-2",
        source_ref="DUP-1",
        dep_type="utility_relocation",
        title="Second",
    )
    person = StatedByPerson(
        project_id=project.id,
        display_name="Append Only Person",
        aliases=[],
        email_normalized=None,
    )
    session.add_all([first, second, person])
    session.flush()
    result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=_segment(session, project, "DUP-1").id,
        reference_kind="source_identifier",
        raw_reference="DUP-1",
        expected_subject_type="constraint",
        usage="affected_subject",
    )
    candidates = session.scalars(
        select(SubjectResolutionCandidate).where(
            SubjectResolutionCandidate.attempt_id == result.attempt_id
        )
    ).all()
    [suggestion] = record_subject_candidate_ranking(
        session,
        attempt_id=result.attempt_id,
        candidate_ids=(candidates[0].id,),
        model="append-only-ranker",
        prompt_version="append-only-v1",
    )

    mutations = (
        ("update stated_by_people set display_name = 'changed' where id = :id", person.id),
        (
            "update subject_resolution_attempts set raw_reference = 'changed' where id = :id",
            result.attempt_id,
        ),
        (
            "delete from subject_resolution_candidates where id = :id",
            candidates[0].id,
        ),
        (
            "update subject_candidate_suggestions set rank = 9 where id = :id",
            suggestion.id,
        ),
    )
    for statement, row_id in mutations:
        with pytest.raises(DBAPIError, match="registry rows are append-only"):
            with session.begin_nested():
                session.execute(text(statement), {"id": row_id})


def test_repeated_identifier_with_a_stale_subject_fails_closed_with_both(session):
    project = _project(session, "subject-stale")
    active = Dependency(
        project_id=project.id,
        ref_code="C-00001",
        source_ref="FOC14-69",
        dep_type="utility_relocation",
        title="Current crossing",
    )
    stale = Dependency(
        project_id=project.id,
        ref_code="C-00002",
        source_ref="FOC14-69",
        dep_type="utility_relocation",
        title="Dismissed duplicate",
        dismissed_at=datetime.now(timezone.utc),
    )
    session.add_all([active, stale])
    session.flush()
    segment = _segment(session, project, "Reference FOC14-69 remains in the matrix.")

    result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=segment.id,
        reference_kind="source_identifier",
        raw_reference="FOC14-69",
        expected_subject_type="constraint",
        usage="affected_subject",
    )

    assert result.state == "stale"
    assert result.subject_id is None
    assert [(item.subject_id, item.state) for item in result.candidates] == [
        (active.id, "active"),
        (stale.id, "stale"),
    ]
    assert result.attention_reason == "stale_subject_reference"


def test_model_candidate_ranking_never_resolves_a_conflicting_identifier(session):
    project = _project(session, "subject-model-ranking")
    first = Dependency(
        project_id=project.id,
        ref_code="C-00001",
        source_ref="ROW-7",
        dep_type="utility_relocation",
        title="First crossing",
    )
    second = Dependency(
        project_id=project.id,
        ref_code="C-00002",
        source_ref="ROW-7",
        dep_type="utility_relocation",
        title="Second crossing",
    )
    session.add_all([first, second])
    session.flush()
    segment = _segment(session, project, "The activity cites ROW-7.")
    result = resolve_subject_reference(
        session,
        project_id=project.id,
        source_segment_id=segment.id,
        reference_kind="activity_identifier",
        raw_reference="ROW-7",
        expected_subject_type="constraint",
        usage="affected_subject",
    )
    candidates = session.scalars(
        select(SubjectResolutionCandidate)
        .where(SubjectResolutionCandidate.attempt_id == result.attempt_id)
        .order_by(SubjectResolutionCandidate.id)
    ).all()

    suggestions = record_subject_candidate_ranking(
        session,
        attempt_id=result.attempt_id,
        candidate_ids=tuple(candidate.id for candidate in reversed(candidates)),
        model="ranking-only-test-model",
        prompt_version="subject-ranking-v1",
    )

    assert result.state == "conflict"
    assert [suggestion.rank for suggestion in suggestions] == [1, 2]
    assert session.get(SubjectResolutionAttempt, result.attempt_id).state == "conflict"
    assert session.scalars(select(SubjectResolutionDecision)).all() == []
    assert unresolved_subject_work_items(session, project.id)[0].attempt_id == result.attempt_id
