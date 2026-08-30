"""ADR-0061's dispute timeline is bounded evidence display, never a decider."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import hashlib

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.dispute_timeline import build_dispute_timeline
from corridor.models import Assertion, Dependency, DocPage, Document, EvidenceLink, Project


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def dependency(session):
    project = Project(slug="timeline", name="Timeline", is_synthetic=True)
    session.add(project)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="TIMELINE-1",
        dep_type="utility_relocation",
        title="Gas main",
    )
    session.add(dependency)
    session.flush()
    return dependency


def _mention(session, dependency, *, order, value, doc_type, quote=None):
    quote = quote or f"The main is {value}."
    document = Document(
        project_id=dependency.project_id,
        sha256=hashlib.sha256(f"{order}:{quote}".encode()).hexdigest(),
        filename=f"source-{order}.pdf",
        doc_type=doc_type,
        doc_date=date(2025, order, 1),
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=f"Header. {quote}"))
    link = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote=quote,
        verified=True,
    )
    session.add(link)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="station_from",
            asserted_value=value,
            evidence_link_id=link.id,
            doc_date=document.doc_date,
        )
    )
    session.flush()


def test_timeline_orders_and_reverifies_each_row_scoped_mention(session, dependency):
    _mention(session, dependency, order=1, value="12-inch", doc_type="matrix")
    _mention(session, dependency, order=2, value="12-inch", doc_type="minutes")
    _mention(session, dependency, order=3, value="16-inch", doc_type="matrix")
    _mention(session, dependency, order=4, value="16-inch", doc_type="email")
    _mention(session, dependency, order=5, value="20-inch", doc_type="email")
    # The stored verification flag is not blindly trusted for a model-visible
    # packet: this stale claim's quote no longer exists on the page.
    _mention(
        session,
        dependency,
        order=6,
        value="99-inch",
        doc_type="email",
        quote="Quote that is not on the page",
    )
    bad_page = session.scalars(
        select(DocPage).order_by(DocPage.id.desc())
    ).first()
    assert bad_page is not None
    bad_page.text = "A different page after source correction."
    session.flush()

    packet = build_dispute_timeline(session, dependency.id, "station_from")

    assert [mention.value for mention in packet.mentions] == [
        "12-inch",
        "12-inch",
        "16-inch",
        "16-inch",
        "20-inch",
    ]
    assert [mention.classification for mention in packet.mentions] == [
        "measurement",
        "restatement",
        "correction",
        "restatement",
        "correction",
    ]
    assert all(mention.quote in mention.page_text for mention in packet.mentions)
    assert packet.apparent_current_position == "20-inch"
    assert "machine reading" in packet.drafted_reading.lower()

    rendered = asdict(packet)
    assert "settled_value" not in rendered
    assert "selected_value" not in rendered
    assert "coordination" not in rendered
