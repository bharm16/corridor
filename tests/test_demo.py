import pytest
from sqlalchemy import func, select

from corridor import audit
from corridor.adjudicate import accept_candidate
from corridor.demo import _reset
from corridor.db import Session, engine
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import AuditLog, Candidate, Dependency, DocPage, Document, ExternalOrg, Project
from corridor.principals import HumanPrincipal
from corridor.demo import DEMO_SLUG, DemoIsolationError

DECLARER = HumanPrincipal("local:demo-declarer")


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
def demo_project(session):
    project = Project(
        slug=DEMO_SLUG, name="Demo Project", agency="TxDOT", is_synthetic=True
    )
    session.add(project)
    session.flush()
    session.add(ExternalOrg(name="AT&T Texas (SWBT)", aliases=[]))
    session.flush()
    return project


@pytest.fixture
def real_project(session):
    project = Project(
        slug="real-project", name="Real Project", agency="TxDOT", is_synthetic=False
    )
    session.add(project)
    session.flush()
    return project


def make_document(session, project, *, sha):
    doc = Document(
        project_id=project.id,
        sha256=sha,
        filename=f"{sha}.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
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


def make_candidate(session, document):
    candidate = Candidate(
        project_id=document.project_id,
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
    session.flush()
    declare_active_run(session, document.id, run.id, principal=DECLARER)
    session.flush()
    return candidate


def _project_counts(session, project_id):
    return (
        session.scalar(
            select(func.count()).select_from(Candidate).where(
                Candidate.project_id == project_id
            )
        ),
        session.scalar(
            select(func.count()).select_from(Dependency).where(
                Dependency.project_id == project_id
            )
        ),
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                (AuditLog.entity_type == audit.DEPENDENCY)
                & (AuditLog.entity_id.in_(
                    select(Dependency.id).where(Dependency.project_id == project_id)
                ))
            )
        ),
    )


def test_reset_only_clears_demonstration_project_rows(session, demo_project, real_project):
    """Demo rollback should be project-scoped, not a global delete."""
    demo_doc = make_document(session, demo_project, sha="a" * 64)
    real_doc = make_document(session, real_project, sha="b" * 64)

    accept_candidate(
        session,
        make_candidate(session, demo_doc),
        principal=HumanPrincipal("local:demo-tester"),
    )
    accept_candidate(
        session,
        make_candidate(session, real_doc),
        principal=HumanPrincipal("local:real-tester"),
    )

    before_demo = _project_counts(session, demo_project.id)
    before_real = _project_counts(session, real_project.id)

    with pytest.raises(DemoIsolationError, match="organization identity history"):
        _reset(session, demo_project)

    assert _project_counts(session, demo_project.id) == before_demo
    assert _project_counts(session, real_project.id) == before_real
    assert before_demo == (1, 1, 1)


def test_reset_refuses_a_real_project_without_changing_it(session, real_project):
    document = make_document(session, real_project, sha="c" * 64)
    session.add(ExternalOrg(name="AT&T Texas (SWBT)", aliases=[]))
    session.flush()
    accept_candidate(
        session,
        make_candidate(session, document),
        principal=HumanPrincipal("local:real-reviewer"),
    )
    before = _project_counts(session, real_project.id)

    with pytest.raises(DemoIsolationError):
        _reset(session, real_project)

    assert _project_counts(session, real_project.id) == before
