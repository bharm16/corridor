"""Tests for registry-backed supersession declaration and queue gating contracts."""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from corridor.db import Session, engine
from corridor.models import DocPage, Document, Project


def _supersession():
    return __import__("corridor.supersession", fromlist=["*"])


def _require_registry_id_column():
    assert "registry_id" in Document.__table__.columns, (
        "Supersession contracts depend on stable Document.registry_id identifiers."
    )


def _registry_sha(project_id: int, registry_id: str, filename: str) -> str:
    return hashlib.sha256(f"{project_id}:{registry_id}:{filename}".encode()).hexdigest()


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
    project = Project(
        slug="supersession-test", name="Supersession Test", is_synthetic=True
    )
    session.add(project)
    session.flush()
    return project


def _document(session, project, *, registry_id, filename, doc_date=None):
    _require_registry_id_column()
    document = Document(
        project_id=project.id,
        sha256=_registry_sha(project.id, registry_id, filename),
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        doc_date=doc_date,
        pages=10,
        registry_id=registry_id,
    )
    session.add(document)
    session.flush()
    return document


def _source_pointer_document(session, project, registry_id="RID-INDEX"):
    source = _document(
        session,
        project,
        registry_id=registry_id,
        filename="rid-index.xlsx",
        doc_date=date(2010, 1, 1),
    )
    for page_no in range(1, 13):
        session.add(
            DocPage(
                document_id=source.id,
                page_no=page_no,
                text=f"RID index row for page {page_no}",
            )
        )
    session.flush()
    return source


def _committed_lock_chain():
    slug = f"supersession-lock-{uuid4().hex}"
    with Session() as setup:
        project = Project(slug=slug, name="Lock test", is_synthetic=True)
        setup.add(project)
        setup.flush()
        source = _source_pointer_document(setup, project, registry_id="RID-LOCK-INDEX")
        first = _document(setup, project, registry_id="RID-LOCK-A", filename="a.pdf")
        second = _document(setup, project, registry_id="RID-LOCK-B", filename="b.pdf")
        ids = (project.id, source.registry_id, first.registry_id, second.registry_id)
        setup.commit()
    return ids


def _delete_committed_project(project_id):
    with Session() as cleanup:
        cleanup.execute(
            update(Document)
            .where(Document.project_id == project_id)
            .values(
                superseded_by=None,
                superseded_on=None,
                supersession_source_document_id=None,
                supersession_source_page=None,
            )
        )
        document_ids = select(Document.id).where(Document.project_id == project_id)
        cleanup.execute(delete(DocPage).where(DocPage.document_id.in_(document_ids)))
        cleanup.execute(delete(Document).where(Document.project_id == project_id))
        cleanup.execute(delete(Project).where(Project.id == project_id))
        cleanup.commit()


def _declaration(
    module,
    source_registry_id,
    *,
    predecessor,
    successor,
    replacement_date,
    source_page=4,
):
    return module.SupersessionDeclaration(
        predecessor_registry_id=predecessor,
        successor_registry_id=successor,
        replacement_date=replacement_date,
        source_registry_id=source_registry_id,
        source_page=source_page,
    )


def test_supersession_api_contract():
    module = _supersession()
    assert hasattr(module, "SupersessionDeclaration")
    assert hasattr(module, "register_supersessions")

    declaration = module.SupersessionDeclaration(
        predecessor_registry_id="RID-A",
        successor_registry_id="RID-B",
        replacement_date=date(2026, 2, 13),
        source_registry_id="RID-INDEX",
        source_page=7,
    )
    assert declaration.predecessor_registry_id == "RID-A"
    assert declaration.successor_registry_id == "RID-B"
    assert declaration.replacement_date == date(2026, 2, 13)
    with pytest.raises(AttributeError):
        declaration.predecessor_registry_id = "RID-Z"


def test_register_supersession_chain_uses_stable_registry_ids(session, project):
    module = _supersession()
    source = _source_pointer_document(session, project)

    predecessor = _document(
        session, project, registry_id="RID-01", filename="rev-1.pdf"
    )
    middle = _document(session, project, registry_id="RID-02", filename="rev-2.pdf")
    successor = _document(session, project, registry_id="RID-03", filename="rev-3.pdf")

    module.register_supersessions(
        session,
        (
            _declaration(
                module,
                source.registry_id,
                predecessor=predecessor.registry_id,
                successor=middle.registry_id,
                replacement_date=date(2025, 10, 13),
            ),
            module.SupersessionDeclaration(
                predecessor_registry_id=middle.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date(2025, 12, 1),
                source_registry_id=source.registry_id,
                source_page=12,
            ),
        ),
    )
    session.flush()

    predecessor_row = session.scalar(
        select(Document).where(Document.registry_id == predecessor.registry_id)
    )
    middle_row = session.scalar(
        select(Document).where(Document.registry_id == middle.registry_id)
    )

    assert predecessor_row.superseded_by == middle.id
    assert predecessor_row.superseded_on == date(2025, 10, 13)
    assert predecessor_row.supersession_source_document_id == source.id
    assert predecessor_row.supersession_source_page == 4

    assert middle_row.superseded_by == successor.id
    assert middle_row.superseded_on == date(2025, 12, 1)
    assert middle_row.supersession_source_document_id == source.id
    assert middle_row.supersession_source_page == 12


def test_register_supersession_registration_is_idempotent_for_exact_duplicates(
    session, project
):
    module = _supersession()
    Declaration = module.SupersessionDeclaration
    source = _source_pointer_document(session, project)

    predecessor = _document(
        session, project, registry_id="RID-IDEM", filename="rev-idem-prev.pdf"
    )
    successor = _document(
        session, project, registry_id="RID-IDEM2", filename="rev-idem-next.pdf"
    )
    declaration = Declaration(
        predecessor_registry_id=predecessor.registry_id,
        successor_registry_id=successor.registry_id,
        replacement_date=date(2026, 1, 19),
        source_registry_id=source.registry_id,
        source_page=3,
    )

    module.register_supersessions(session, [declaration])
    session.flush()

    state = (
        predecessor.superseded_by,
        predecessor.superseded_on,
        predecessor.supersession_source_document_id,
        predecessor.supersession_source_page,
    )
    assert state == (successor.id, date(2026, 1, 19), source.id, 3)

    module.register_supersessions(session, [declaration])
    session.flush()

    assert (
        predecessor.superseded_by,
        predecessor.superseded_on,
        predecessor.supersession_source_document_id,
        predecessor.supersession_source_page,
    ) == state


def test_documents_never_supersede_from_dates_names_or_ingestion_order(
    session, project
):
    older = _document(
        session,
        project,
        registry_id="RID-NO-INFERENCE-OLD",
        filename="matrix-rev-1-2025-01-01.pdf",
        doc_date=date(2025, 1, 1),
    )
    newer = _document(
        session,
        project,
        registry_id="RID-NO-INFERENCE-NEW",
        filename="matrix-rev-2-2026-01-01.pdf",
        doc_date=date(2026, 1, 1),
    )

    session.flush()

    assert older.superseded_by is None
    assert older.superseded_on is None
    assert newer.superseded_by is None
    assert newer.superseded_on is None


def test_concurrent_registrations_serialize_then_revalidate_the_graph():
    module = _supersession()
    project_id, source_id, first_id, second_id = _committed_lock_chain()
    first_to_second = _declaration(
        module,
        source_id,
        predecessor=first_id,
        successor=second_id,
        replacement_date=date(2026, 1, 1),
        source_page=1,
    )
    second_to_first = _declaration(
        module,
        source_id,
        predecessor=second_id,
        successor=first_id,
        replacement_date=date(2026, 1, 2),
        source_page=2,
    )

    first_session = Session()
    blocked_session = Session()
    try:
        module.register_supersessions(
            first_session, [first_to_second], project_id=project_id
        )
        blocked_session.execute(text("set local lock_timeout = '250ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            module.register_supersessions(
                blocked_session, [second_to_first], project_id=project_id
            )
        blocked_session.rollback()
        first_session.commit()

        with Session() as retry:
            with pytest.raises(ValueError, match="cycle"):
                module.register_supersessions(
                    retry, [second_to_first], project_id=project_id
                )
    finally:
        first_session.close()
        blocked_session.close()
        _delete_committed_project(project_id)


def test_registration_refreshes_documents_loaded_before_the_project_lock():
    module = _supersession()
    project_id, source_id, first_id, second_id = _committed_lock_chain()
    first_to_second = _declaration(
        module,
        source_id,
        predecessor=first_id,
        successor=second_id,
        replacement_date=date(2026, 1, 1),
        source_page=1,
    )
    second_to_first = _declaration(
        module,
        source_id,
        predecessor=second_id,
        successor=first_id,
        replacement_date=date(2026, 1, 2),
        source_page=2,
    )

    prewarmed = Session()
    try:
        prewarmed.scalars(
            select(Document).where(Document.project_id == project_id)
        ).all()
        with Session() as writer:
            module.register_supersessions(
                writer, [first_to_second], project_id=project_id
            )
            writer.commit()

        with pytest.raises(ValueError, match="cycle"):
            module.register_supersessions(
                prewarmed, [second_to_first], project_id=project_id
            )
    finally:
        prewarmed.rollback()
        prewarmed.close()
        _delete_committed_project(project_id)


def test_candidate_mutation_scope_uses_the_same_project_lock_as_registration():
    module = _supersession()
    project_id, source_id, first_id, second_id = _committed_lock_chain()
    declaration = _declaration(
        module,
        source_id,
        predecessor=first_id,
        successor=second_id,
        replacement_date=date(2026, 1, 1),
        source_page=1,
    )

    review_session = Session()
    blocked_session = Session()
    try:
        assert (
            module.actionable_candidate_for_update(
                review_session, project_id, candidate_id=-1
            )
            is None
        )
        blocked_session.execute(text("set local lock_timeout = '250ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            module.register_supersessions(
                blocked_session, [declaration], project_id=project_id
            )
        blocked_session.rollback()
    finally:
        review_session.rollback()
        review_session.close()
        blocked_session.close()
        _delete_committed_project(project_id)


def test_register_supersession_batch_rejects_invalid_and_leaves_valid_edges_unchanged(
    session, project
):
    module = _supersession()
    Declaration = module.SupersessionDeclaration
    source = _source_pointer_document(session, project)

    valid_predecessor = _document(
        session, project, registry_id="RID-VALID", filename="valid-prev.pdf"
    )
    valid_successor = _document(
        session, project, registry_id="RID-VALID-NEXT", filename="valid-succ.pdf"
    )
    invalid_predecessor = _document(
        session, project, registry_id="RID-INVALID", filename="invalid-prev.pdf"
    )

    valid = Declaration(
        predecessor_registry_id=valid_predecessor.registry_id,
        successor_registry_id=valid_successor.registry_id,
        replacement_date=date(2026, 1, 20),
        source_registry_id=source.registry_id,
        source_page=2,
    )
    invalid = Declaration(
        predecessor_registry_id=invalid_predecessor.registry_id,
        successor_registry_id="RID-MISSING",
        replacement_date=date(2026, 1, 21),
        source_registry_id=source.registry_id,
        source_page=2,
    )

    with pytest.raises(ValueError, match="missing|does not exist"):
        module.register_supersessions(session, [valid, invalid])
    session.flush()

    assert valid_predecessor.superseded_by is None
    assert valid_predecessor.superseded_on is None
    assert valid_predecessor.supersession_source_document_id is None
    assert valid_predecessor.supersession_source_page is None
    assert invalid_predecessor.superseded_by is None
    assert invalid_predecessor.superseded_on is None


def test_register_five_revision_chain_accepts_structured_source_pointer(
    session, project
):
    module = _supersession()
    source = _source_pointer_document(session, project)

    r1 = _document(session, project, registry_id="RID-01", filename="rev-1.pdf")
    r2 = _document(session, project, registry_id="RID-02", filename="rev-2.pdf")
    r3 = _document(session, project, registry_id="RID-03", filename="rev-3.pdf")
    r4 = _document(session, project, registry_id="RID-04", filename="rev-4.pdf")
    r5 = _document(session, project, registry_id="RID-05", filename="rev-5.pdf")
    declarations = [
        _declaration(
            module,
            source.registry_id,
            predecessor=r1.registry_id,
            successor=r2.registry_id,
            replacement_date=date(2025, 11, 5),
        ),
        _declaration(
            module,
            source.registry_id,
            predecessor=r2.registry_id,
            successor=r3.registry_id,
            replacement_date=date(2025, 12, 10),
        ),
        _declaration(
            module,
            source.registry_id,
            predecessor=r3.registry_id,
            successor=r4.registry_id,
            replacement_date=date(2026, 1, 8),
        ),
        _declaration(
            module,
            source.registry_id,
            predecessor=r4.registry_id,
            successor=r5.registry_id,
            replacement_date=date(2026, 2, 2),
        ),
    ]
    module.register_supersessions(session, declarations)
    session.flush()

    assert (
        session.scalar(
            select(Document).where(Document.registry_id == r1.registry_id)
        ).superseded_by
        == r2.id
    )
    assert session.scalar(
        select(Document).where(Document.registry_id == r1.registry_id)
    ).superseded_on == date(2025, 11, 5)
    r1_row = session.scalar(
        select(Document).where(Document.registry_id == r1.registry_id)
    )
    assert r1_row.supersession_source_document_id == source.id
    assert r1_row.supersession_source_page == 4
    assert (
        session.scalar(
            select(Document).where(Document.registry_id == r4.registry_id)
        ).superseded_by
        == r5.id
    )
    assert session.scalar(
        select(Document).where(Document.registry_id == r4.registry_id)
    ).superseded_on == date(2026, 2, 2)
    r4_row = session.scalar(
        select(Document).where(Document.registry_id == r4.registry_id)
    )
    assert r4_row.supersession_source_document_id == source.id
    assert r4_row.supersession_source_page == 4
    assert r5.superseded_by is None


def test_register_supersession_rejects_missing_target_or_invalid_metadata(
    session, project
):
    module = _supersession()
    Declaration = module.SupersessionDeclaration
    source = _document(session, project, registry_id="RID-S", filename="source.pdf")
    source_pointer = _source_pointer_document(session, project)

    with pytest.raises(ValueError, match="target|missing|does not exist"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=source.registry_id,
                    successor_registry_id="MISSING",
                    replacement_date=date(2026, 2, 13),
                    source_registry_id=source_pointer.registry_id,
                    source_page=2,
                )
            ],
        )
    with pytest.raises(ValueError, match="replacement_date"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=source.registry_id,
                    successor_registry_id="RID-NEW",
                    replacement_date=None,
                    source_registry_id=source_pointer.registry_id,
                    source_page=2,
                )
            ],
        )


def test_a_document_cannot_be_the_authority_for_its_own_replacement(session, project):
    """The predecessor is refused as its own source; the successor is not.

    A revision claiming "I have been replaced, and the proof is on page 1
    of me" is attesting to an event that postdates it, so the page a reader
    would check predates the fact (ADR-0015). A successor stating what it
    replaces is the ordinary way agencies declare a chain, and stays legal.
    """
    module = _supersession()
    Declaration = module.SupersessionDeclaration
    first = _document(session, project, registry_id="RID-SELF-1", filename="one.pdf")
    second = _document(session, project, registry_id="RID-SELF-2", filename="two.pdf")
    # Both carry the cited page, so a refusal can only be about who is
    # attesting — not about a page that does not exist.
    for document in (first, second):
        session.add(
            DocPage(document_id=document.id, page_no=1, text="Replaced on 2026-02-13")
        )
    session.flush()

    def declaration(source):
        return Declaration(
            predecessor_registry_id=first.registry_id,
            successor_registry_id=second.registry_id,
            replacement_date=date(2026, 2, 13),
            source_registry_id=source.registry_id,
            source_page=1,
        )

    with pytest.raises(ValueError, match="source cannot be the predecessor"):
        module.register_supersessions(session, [declaration(first)])

    session.refresh(first)
    assert first.superseded_by is None
    assert first.supersession_source_document_id is None

    module.register_supersessions(session, [declaration(second)])
    session.refresh(first)
    assert first.superseded_by == second.id
    assert first.supersession_source_document_id == second.id


def test_register_supersession_rejects_self_cycle_and_conflicting_edges(
    session, project
):
    module = _supersession()
    Declaration = module.SupersessionDeclaration
    first = _document(session, project, registry_id="RID-1", filename="first.pdf")
    second = _document(session, project, registry_id="RID-2", filename="second.pdf")
    source_pointer = _source_pointer_document(session, project)

    with pytest.raises(ValueError, match="self|cycle|predecessor"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=first.registry_id,
                    successor_registry_id=first.registry_id,
                    replacement_date=date(2026, 2, 13),
                    source_registry_id=source_pointer.registry_id,
                    source_page=1,
                )
            ],
        )

    with pytest.raises(ValueError, match="cycle|conflict|predecessor"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=first.registry_id,
                    successor_registry_id=second.registry_id,
                    replacement_date=date(2026, 2, 13),
                    source_registry_id=source_pointer.registry_id,
                    source_page=1,
                ),
                Declaration(
                    predecessor_registry_id=second.registry_id,
                    successor_registry_id=first.registry_id,
                    replacement_date=date(2026, 2, 14),
                    source_registry_id=source_pointer.registry_id,
                    source_page=2,
                ),
            ],
        )

    with pytest.raises(ValueError, match="conflict|predecessor|multiple"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=first.registry_id,
                    successor_registry_id=second.registry_id,
                    replacement_date=date(2026, 2, 13),
                    source_registry_id=source_pointer.registry_id,
                    source_page=2,
                ),
                Declaration(
                    predecessor_registry_id=first.registry_id,
                    successor_registry_id=second.registry_id,
                    replacement_date=date(2026, 2, 14),
                    source_registry_id=source_pointer.registry_id,
                    source_page=3,
                ),
            ],
        )


def test_register_supersession_rejects_cross_project_edges(session, project):
    module = _supersession()
    Declaration = module.SupersessionDeclaration

    other_project = Project(slug="supersession-other", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()

    source_project_doc = _document(
        session, project, registry_id="RID-SRC", filename="source.pdf"
    )
    other_project_doc = _document(
        session, other_project, registry_id="RID-OTHER", filename="other.pdf"
    )
    source_pointer = _source_pointer_document(session, project)
    source_pointer_id = source_pointer.registry_id

    with pytest.raises(ValueError, match="project|cross|different"):
        module.register_supersessions(
            session,
            [
                Declaration(
                    predecessor_registry_id=source_project_doc.registry_id,
                    successor_registry_id=other_project_doc.registry_id,
                    replacement_date=date(2026, 2, 13),
                    source_registry_id=source_pointer_id,
                    source_page=1,
                )
            ],
        )


def test_database_rejects_cross_project_successor_below_the_service_boundary(
    session, project
):
    other_project = Project(
        slug="supersession-db-other",
        name="Other",
        is_synthetic=True,
    )
    session.add(other_project)
    session.flush()
    predecessor = _document(
        session, project, registry_id="RID-DB-PREV", filename="previous.pdf"
    )
    foreign_successor = _document(
        session,
        other_project,
        registry_id="RID-DB-FOREIGN-NEXT",
        filename="next.pdf",
    )
    source = _source_pointer_document(session, project, registry_id="RID-DB-INDEX")

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            predecessor.superseded_by = foreign_successor.id
            predecessor.superseded_on = date(2026, 2, 13)
            predecessor.supersession_source_document_id = source.id
            predecessor.supersession_source_page = 1
            session.flush()


def test_database_rejects_cross_project_source_pointer_below_the_service_boundary(
    session, project
):
    other_project = Project(
        slug="supersession-db-source-other",
        name="Other source",
        is_synthetic=True,
    )
    session.add(other_project)
    session.flush()
    predecessor = _document(
        session, project, registry_id="RID-DB-SOURCE-PREV", filename="previous.pdf"
    )
    successor = _document(
        session, project, registry_id="RID-DB-SOURCE-NEXT", filename="next.pdf"
    )
    foreign_source = _source_pointer_document(
        session, other_project, registry_id="RID-DB-FOREIGN-INDEX"
    )

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            predecessor.superseded_by = successor.id
            predecessor.superseded_on = date(2026, 2, 13)
            predecessor.supersession_source_document_id = foreign_source.id
            predecessor.supersession_source_page = 1
            session.flush()


def test_database_rejects_unregistered_successor_below_the_service_boundary(
    session, project
):
    predecessor = _document(
        session, project, registry_id="RID-DB-UNREG-PREV", filename="previous.pdf"
    )
    unregistered_successor = _document(
        session, project, registry_id=None, filename="next.pdf"
    )
    source = _source_pointer_document(session, project, registry_id="RID-DB-UNREG-INDEX")

    with pytest.raises(IntegrityError, match="successor must already have a registry_id"):
        with session.begin_nested():
            predecessor.superseded_by = unregistered_successor.id
            predecessor.superseded_on = date(2026, 2, 13)
            predecessor.supersession_source_document_id = source.id
            predecessor.supersession_source_page = 1
            session.flush()


def test_database_rejects_unregistered_source_pointer_below_the_service_boundary(
    session, project
):
    predecessor = _document(
        session, project, registry_id="RID-DB-UNREG-SOURCE-PREV", filename="previous.pdf"
    )
    successor = _document(
        session, project, registry_id="RID-DB-UNREG-SOURCE-NEXT", filename="next.pdf"
    )
    unregistered_source = _document(
        session, project, registry_id=None, filename="source.pdf"
    )
    session.add(
        DocPage(document_id=unregistered_source.id, page_no=1, text="unregistered source")
    )
    session.flush()

    with pytest.raises(IntegrityError, match="source must already have a registry_id"):
        with session.begin_nested():
            predecessor.superseded_by = successor.id
            predecessor.superseded_on = date(2026, 2, 13)
            predecessor.supersession_source_document_id = unregistered_source.id
            predecessor.supersession_source_page = 1
            session.flush()


def test_database_rejects_registry_id_change_after_first_assignment(session, project):
    document = _document(
        session, project, registry_id="RID-DB-STABLE", filename="stable.pdf"
    )

    with pytest.raises(IntegrityError, match="registry_id is immutable once set"):
        with session.begin_nested():
            document.registry_id = "RID-DB-RENAMED"
            session.flush()

    session.refresh(document)

    with pytest.raises(IntegrityError, match="registry_id is immutable once set"):
        with session.begin_nested():
            document.registry_id = None
            session.flush()


def test_database_rejects_changes_to_referenced_registry_ids(session, project):
    predecessor = _document(
        session, project, registry_id="RID-DB-REF-PREV", filename="previous.pdf"
    )
    successor = _document(
        session, project, registry_id="RID-DB-REF-NEXT", filename="next.pdf"
    )
    source = _source_pointer_document(session, project, registry_id="RID-DB-REF-INDEX")
    predecessor.superseded_by = successor.id
    predecessor.superseded_on = date(2026, 2, 13)
    predecessor.supersession_source_document_id = source.id
    predecessor.supersession_source_page = 1
    session.flush()

    with pytest.raises(IntegrityError, match="registry_id is immutable once set"):
        with session.begin_nested():
            successor.registry_id = "RID-DB-REF-RENAMED"
            session.flush()

    session.refresh(source)

    with pytest.raises(IntegrityError, match="registry_id is immutable once set"):
        with session.begin_nested():
            source.registry_id = None
            session.flush()
