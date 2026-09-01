from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from corridor.adjudicate import accept_candidate, edit_candidate, merge_candidate
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    DocPage,
    Document,
    ExternalOrg,
    EvidenceLink,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
)
from corridor.operative_support import (
    designate_publication_support,
    resolve_operative_support,
)
from corridor.principals import HumanPrincipal
from corridor.supersession import SupersessionDeclaration, register_supersessions


TEST_PRINCIPAL = HumanPrincipal("local:operative-support-reviewer")
FIELDS = {
    "utility_id": "FOC1-1",
    "external_org": "LT AT&T Texas",
    "utility_type": "Telecom",
    "station_from": "1149+00",
    "station_to": "1153+17",
}


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug="operative-support-test",
        name="Operative Support Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    session.add(ExternalOrg(name="LT AT&T Texas", aliases=[]))
    session.flush()
    return project


def _document(session, project, *, suffix: str, text: str) -> Document:
    document = Document(
        project_id=project.id,
        sha256=(suffix * 64)[:64],
        filename=f"{suffix}.pdf",
        doc_type="matrix" if suffix.startswith("matrix") else "agreement",
        parse_status="parsed",
        pages=1,
        doc_date=date(2026, 8, 5),
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=text,
            image_path="/tmp/corridor-missing-page.png",
        )
    )
    session.flush()
    return document


def _candidate(
    session,
    project,
    document,
    *,
    quote: str,
    fields: dict[str, str] | None = None,
) -> Candidate:
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields or FIELDS,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "operative-support-contract",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version=candidate.prompt_version,
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model=candidate.model,
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=TEST_PRINCIPAL)
    session.flush()
    return candidate


def _evidence_links(session, dependency_id: int) -> list[EvidenceLink]:
    return session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency_id)
        .order_by(EvidenceLink.id)
    ).all()


def test_completion_readiness_support_does_not_replace_publication_support(
    session, project
):
    matrix = _document(
        session,
        project,
        suffix="matrix-a",
        text="FOC1-1 LT AT&T Texas Telecom 1149+00 1153+17",
    )
    completion = _document(
        session,
        project,
        suffix="completion-a",
        text="FOC1-1 relocation complete and accepted",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session,
            project,
            matrix,
            quote="FOC1-1 LT AT&T Texas Telecom 1149+00 1153+17",
        ),
        principal=TEST_PRINCIPAL,
    )
    [publication] = _evidence_links(session, dependency.id)
    readiness = EvidenceLink(
        dependency_id=dependency.id,
        document_id=completion.id,
        page_no=1,
        quote="FOC1-1 relocation complete and accepted",
        verified=True,
    )
    session.add(readiness)
    session.flush()
    session.add(
        DependencyEvidenceSufficiency(
            dependency_id=dependency.id,
            evidence_link_id=readiness.id,
            scope_link_id=None,
        )
    )
    session.flush()

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == publication.id
    assert {support.evidence_link_id for support in resolved.readiness} == {
        readiness.id
    }
    assert resolved.is_ready is True
    assert set(resolved.superseded_roles) == set()


def test_database_refuses_reassigning_direct_evidence_with_a_readiness_role(
    session, project
):
    """A direct readiness judgment cannot silently follow a new owner."""
    document = _document(
        session,
        project,
        suffix="matrix-owner",
        text="FOC1-1 LT AT&T Texas Telecom 1149+00 1153+17",
    )
    first = accept_candidate(
        session,
        _candidate(
            session,
            project,
            document,
            quote="FOC1-1 LT AT&T Texas Telecom 1149+00 1153+17",
        ),
        principal=TEST_PRINCIPAL,
    )
    second = Dependency(
        project_id=project.id,
        ref_code="DEP-SECOND-OWNER",
        dep_type="utility_relocation",
        title="Second owner",
        external_org_id=first.external_org_id,
    )
    session.add(second)
    session.flush()
    evidence = _evidence_links(session, first.id)[0]
    mark_satisfies(session, first.id, evidence.id, principal=TEST_PRINCIPAL)
    session.flush()

    with pytest.raises(IntegrityError, match="direct Evidence roles"):
        with session.begin_nested():
            session.execute(
                text("update evidence_links set dependency_id = :dependency_id where id = :id"),
                {"dependency_id": second.id, "id": evidence.id},
            )
            session.execute(text("set constraints all immediate"))


def test_satisfying_but_unverified_evidence_cannot_make_a_record_ready(
    session, project
):
    matrix = _document(
        session,
        project,
        suffix="matrix-b",
        text="FOC1-1 LT AT&T Texas Telecom",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session,
            project,
            matrix,
            quote="FOC1-1 LT AT&T Texas Telecom",
        ),
        principal=TEST_PRINCIPAL,
    )
    unverified_completion = EvidenceLink(
        dependency_id=dependency.id,
        document_id=matrix.id,
        page_no=1,
        quote="not actually present",
        verified=False,
    )
    session.add(unverified_completion)
    session.flush()
    session.add(
        DependencyEvidenceSufficiency(
            dependency_id=dependency.id,
            evidence_link_id=unverified_completion.id,
            scope_link_id=None,
        )
    )
    session.flush()

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.readiness == ()
    assert resolved.is_ready is False


def test_record_and_field_publication_scopes_are_independent(session, project):
    document = _document(
        session,
        project,
        suffix="matrix-fields",
        text="FOC1-1 record claim station 1149+00",
    )
    dependency = accept_candidate(
        session,
        _candidate(session, project, document, quote="FOC1-1 record claim"),
        principal=TEST_PRINCIPAL,
    )
    [record_link] = _evidence_links(session, dependency.id)
    field_link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="station 1149+00",
        verified=True,
    )
    session.add(field_link)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        field_link.id,
        field_name="station_from",
        principal=TEST_PRINCIPAL,
    )

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == record_link.id
    assert resolved.publication_for("station_from").evidence_link_id == field_link.id
    assert resolved.publication_for("station_to") is None


def test_accept_designates_its_primary_evidence_as_publication_support(
    session, project
):
    document = _document(
        session,
        project,
        suffix="matrix-accept",
        text="FOC1-1 accepted source row",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session, project, document, quote="FOC1-1 accepted source row"
        ),
        principal=TEST_PRINCIPAL,
    )
    [link] = _evidence_links(session, dependency.id)

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == link.id


def test_edit_then_accept_designates_publication_support_in_the_same_decision(
    session, project
):
    document = _document(
        session,
        project,
        suffix="matrix-edit",
        text="FOC1-1 edited source row",
    )
    candidate = _candidate(
        session, project, document, quote="FOC1-1 edited source row"
    )
    edit_candidate(
        session,
        candidate,
        {**FIELDS, "station_to": "1154+00"},
        principal=TEST_PRINCIPAL,
    )
    dependency = accept_candidate(
        session, candidate, principal=TEST_PRINCIPAL
    )
    [link] = _evidence_links(session, dependency.id)

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == link.id


def test_merge_moves_publication_support_to_the_admitted_source(
    session, project
):
    first = _document(
        session,
        project,
        suffix="matrix-merge-a",
        text="FOC1-1 original source row",
    )
    second = _document(
        session,
        project,
        suffix="matrix-merge-b",
        text="FOC1-1 successor source row",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session, project, first, quote="FOC1-1 original source row"
        ),
        principal=TEST_PRINCIPAL,
    )
    merge_candidate(
        session,
        _candidate(
            session, project, second, quote="FOC1-1 successor source row"
        ),
        dependency,
        principal=TEST_PRINCIPAL,
    )
    links = _evidence_links(session, dependency.id)

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == links[-1].id
    assert resolved.publication.quote == "FOC1-1 successor source row"


def test_superseded_support_lapses_readiness_and_names_each_affected_role(
    session, project
):
    predecessor = _document(
        session,
        project,
        suffix="matrix-old",
        text="FOC1-1 old source row",
    )
    successor = _document(
        session,
        project,
        suffix="matrix-new",
        text="FOC1-1 new source row",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session, project, predecessor, quote="FOC1-1 old source row"
        ),
        principal=TEST_PRINCIPAL,
    )
    [link] = _evidence_links(session, dependency.id)
    assert mark_satisfies(
        session,
        dependency.id,
        link.id,
        principal=TEST_PRINCIPAL,
    ) is True
    predecessor.registry_id = "operative-matrix-old"
    successor.registry_id = "operative-matrix-new"
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date(2026, 8, 5),
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == link.id
    assert {support.evidence_link_id for support in resolved.readiness} == {link.id}
    assert resolved.current_readiness == ()
    assert resolved.is_ready is False
    assert set(resolved.superseded_roles) == {"publication", "readiness"}
    # Currency changed; historical provenance did not.
    assert link.verified is True
    assert any(
        role.evidence_link_id == link.id
        for role in session.scalars(
            select(DependencyEvidenceSufficiency).where(
                DependencyEvidenceSufficiency.dependency_id == dependency.id
            )
        )
    )


def test_superseded_publication_support_preserves_its_exact_field_scope(
    session, project
):
    predecessor = _document(
        session,
        project,
        suffix="matrix-field-old",
        text="FOC1-1 old station 1149+00",
    )
    successor = _document(
        session,
        project,
        suffix="matrix-field-current",
        text="FOC1-1 current record",
    )
    dependency = accept_candidate(
        session,
        _candidate(
            session,
            project,
            predecessor,
            quote="FOC1-1 old station 1149+00",
        ),
        principal=TEST_PRINCIPAL,
    )
    [historical] = _evidence_links(session, dependency.id)
    current = EvidenceLink(
        dependency_id=dependency.id,
        document_id=successor.id,
        page_no=1,
        quote="FOC1-1 current record",
        verified=True,
    )
    session.add(current)
    session.flush()
    designate_publication_support(
        session,
        dependency.id,
        current.id,
        principal=TEST_PRINCIPAL,
    )
    designate_publication_support(
        session,
        dependency.id,
        historical.id,
        field_name="station_from",
        principal=TEST_PRINCIPAL,
    )
    predecessor.registry_id = "operative-field-old"
    successor.registry_id = "operative-field-current"
    session.flush()
    replacement_date = date(2026, 8, 4)
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=replacement_date,
                source_registry_id=successor.registry_id,
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    resolved = resolve_operative_support(session, [dependency.id])[dependency.id]

    assert resolved.publication.evidence_link_id == current.id
    assert resolved.superseded_roles == {"publication"}
    assert len(resolved.superseded_scopes) == 1
    [scope] = resolved.superseded_scopes
    assert scope.role == "publication"
    assert scope.field_name == "station_from"
    assert scope.evidence.evidence_link_id == historical.id
    assert scope.evidence.superseded_on == replacement_date


def test_resolver_is_batched_and_empty_input_is_empty(session):
    assert resolve_operative_support(session, []) == {}


def test_designation_dual_writes_the_relationship_fact_on_the_spine(
    session, project
):
    document = _document(
        session, project, suffix="matrix-spine", text="FOC1-1 spine claim"
    )
    dependency = accept_candidate(
        session,
        _candidate(session, project, document, quote="FOC1-1 spine claim"),
        principal=TEST_PRINCIPAL,
    )
    [first_link] = _evidence_links(session, dependency.id)
    other_document = _document(
        session, project, suffix="agreement-spine", text="FOC1-1 newer support"
    )
    second_link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=other_document.id,
        page_no=1,
        quote="FOC1-1 newer support",
        verified=True,
    )
    session.add(second_link)
    session.flush()
    subject_key = f"dependency:{dependency.id}"

    designate_publication_support(
        session, dependency.id, first_link.id, principal=TEST_PRINCIPAL
    )

    def decisions():
        return session.scalars(
            select(FactDecision)
            .where(
                FactDecision.project_id == project.id,
                FactDecision.subject_key == subject_key,
                FactDecision.fact_type == "supporting_documentation_in_use",
            )
            .order_by(FactDecision.id)
        ).all()

    [first] = decisions()
    assert first.disposition == "include"
    assert first.superseded_by is None
    first_fact = session.get(Fact, first.fact_id)
    assert first_fact.document_value_id == document.id
    assert first_fact.subject_kind == "record_subject"
    revision = session.get(ProjectRecordRevision, first.revision_id)
    assert revision.command_type == "designate_support"
    assert revision.human_principal == TEST_PRINCIPAL.subject

    # An unchanged designation records no attributable no-op.
    designate_publication_support(
        session, dependency.id, first_link.id, principal=TEST_PRINCIPAL
    )
    assert len(decisions()) == 1

    # Replacing the designated document compensates the displaced member and
    # includes the new one — member-local, never a set rewrite.
    designate_publication_support(
        session, dependency.id, second_link.id, principal=TEST_PRINCIPAL
    )
    displaced, compensation, included = decisions()
    assert displaced.id == first.id
    assert displaced.superseded_by == compensation.id
    assert compensation.disposition == "restore"
    assert compensation.fact_id == first.fact_id
    assert (
        session.get(ProjectRecordRevision, compensation.revision_id).command_type
        == "resolve_support"
    )
    assert included.disposition == "include"
    assert session.get(Fact, included.fact_id).document_value_id == other_document.id

    # Designating the original again re-decides the same relationship fact.
    designate_publication_support(
        session, dependency.id, first_link.id, principal=TEST_PRINCIPAL
    )
    final = decisions()
    assert len(final) == 5
    assert final[-1].disposition == "include"
    assert final[-1].fact_id == first.fact_id
