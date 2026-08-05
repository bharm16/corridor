import pytest
from sqlalchemy import func, select

from corridor import audit
from corridor.adjudicate import accept_candidate, edit_candidate, merge_candidate
from corridor.db import Session, engine
from corridor.extraction_runs import (
    active_run_for_document,
    declare_active_run,
    record_extraction_run,
)
from corridor.models import (
    AuditLog,
    Assertion,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(slug="human-principal-project", name="Human Principal", is_synthetic=True)
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def document(session, project):
    doc = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=2,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text="AT&T Texas (SWBT) 1149+00",
        )
    )
    session.flush()
    return doc


def make_candidate(session, project, document):
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC-1",
                "external_org": "AT&T Texas (SWBT)",
                "utility_type": "Telecom",
                "station_from": "1149+00",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "AT&T Texas (SWBT) 1149+00",
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "confidence": 1.0,
            "unverified_fields": [],
            "low_confidence_tokens": [],
            "dedupe_hint": "AT&T Texas (SWBT)|Telecom|1149+00",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=True,
    )
    session.add(candidate)
    session.flush()
    run = active_run_for_document(session, document.id)
    if run is None:
        run = record_extraction_run(
            session,
            document,
            prompt_version=candidate.prompt_version,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model=candidate.model,
        )
        declare_active_run(session, document.id, run.id)
    else:
        assert run.prompt_version == candidate.prompt_version
        assert run.model == candidate.model
        candidate.extraction_run_id = run.id
        run.candidate_count += 1
    session.flush()
    return candidate


def _human_principal(name: str) -> HumanPrincipal:
    return HumanPrincipal(f"local:{name}")


def _project_counts(session, project_id):
    dependency_ids = select(Dependency.id).where(Dependency.project_id == project_id)
    candidate_ids = select(Candidate.id).where(Candidate.project_id == project_id)
    return (
        session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.project_id == project_id)
        ),
        session.scalar(
            select(func.count())
            .select_from(Dependency)
            .where(Dependency.project_id == project_id)
        ),
        session.scalar(
            select(func.count())
            .select_from(Assertion)
            .where(Assertion.dependency_id.in_(dependency_ids))
        ),
        session.scalar(
            select(func.count())
            .select_from(EvidenceLink)
            .where(EvidenceLink.dependency_id.in_(dependency_ids))
        ),
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                (AuditLog.entity_type == audit.DEPENDENCY)
                & (AuditLog.entity_id.in_(dependency_ids))
                | (AuditLog.entity_type == audit.CANDIDATE)
                & (AuditLog.entity_id.in_(candidate_ids))
            )
        ),
    )


@pytest.mark.parametrize("principal", ["reviewer", "agent", "demo"])
def test_acceptance_requires_a_stable_human_principal(
    session, project, document, principal
):
    """Role/free-text actors are not admissible principal identities."""

    candidate = make_candidate(session, project, document)
    before = _project_counts(session, project.id)

    with pytest.raises(InvalidHumanPrincipal):
        accept_candidate(session, candidate, principal=principal)

    assert candidate.state == "pending"
    assert _project_counts(session, project.id) == before


def test_edit_then_accept_refuses_a_role_actor(session, project, document):
    """A role actor cannot complete an admission via edit-then-accept."""

    editor = _human_principal("alice")
    candidate = make_candidate(session, project, document)
    edit_candidate(
        session,
        candidate,
        {
            "utility_id": "FOC-1",
            "external_org": "AT&T Texas (SWBT)",
            "station_from": "1150+00",
        },
        principal=editor,
    )

    before = _project_counts(session, project.id)

    with pytest.raises(InvalidHumanPrincipal):
        accept_candidate(session, candidate, principal="demo")

    assert candidate.state == "pending"
    assert _project_counts(session, project.id) == before


@pytest.mark.parametrize("principal", ["reviewer", "agent", "demo"])
def test_merge_requires_a_stable_human_principal(
    session, project, document, principal
):
    """Merge must come from a stable human principal, not a role label."""

    target = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Telecom — AT&T",
        status="identified",
    )
    session.add(target)
    session.flush()

    candidate = make_candidate(session, project, document)
    before = _project_counts(session, project.id)

    with pytest.raises(InvalidHumanPrincipal):
        merge_candidate(session, candidate, target, principal=principal)

    assert candidate.state == "pending"
    assert candidate.merged_into is None
    assert _project_counts(session, project.id) == before


def test_successful_admission_records_exact_principal_identity(session, project, document):
    """Audit identity is the exact principal identity, not a role label."""

    principal = _human_principal("alice")
    candidate = make_candidate(session, project, document)

    dependency = accept_candidate(session, candidate, principal=principal)
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == dependency.id,
            AuditLog.action == audit.ACCEPT_CANDIDATE,
        )
    ).one()

    assert entry.actor == principal.subject
    assert entry.human_principal == principal.subject


def test_successful_merge_records_exact_principal_identity(session, project, document):
    """Merge admission keeps the actor identity exact in audit history."""

    principal = _human_principal("bryce")
    target = Dependency(
        project_id=project.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="Telecom — AT&T",
        status="identified",
    )
    session.add(target)
    session.flush()

    candidate = make_candidate(session, project, document)
    merge_candidate(session, candidate, target, principal=principal)

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == target.id,
            AuditLog.action == audit.MERGE_CANDIDATE,
        )
    ).one()

    assert entry.actor == principal.subject
    assert entry.human_principal == principal.subject
