import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.models import DEP_STATUSES


@pytest.fixture
def conn():
    engine = create_engine(settings.database_url)
    with engine.connect() as c:
        trans = c.begin()
        yield c
        trans.rollback()


def test_ready_is_not_a_dependency_status():
    """ADR-0002: readiness is computed from evidence, never stored."""
    assert "ready" not in DEP_STATUSES


def test_database_rejects_a_ready_status(conn):
    """The invariant is enforced by the schema, not by convention.

    If `ready` becomes settable, the milestone readiness rollup quietly
    reverts from an evidence claim to an opinion survey, and nothing else
    in the system would notice.
    """
    conn.execute(
        text(
            "insert into projects (id, slug, name, is_synthetic) "
            "values (9999, 'invariant-test', 'Invariant Test', false)"
        )
    )
    with pytest.raises(DBAPIError):
        conn.execute(
            text(
                "insert into dependencies "
                "(project_id, ref_code, dep_type, title, status, criticality) "
                "values (9999, 'DEP-001', 'utility_relocation', 'x', "
                "'ready', 'normal')"
            )
        )


def test_evidence_defaults_are_false(conn):
    """Neither verification nor sufficiency is ever assumed."""
    conn.execute(
        text(
            "insert into projects (id, slug, name, is_synthetic) "
            "values (9998, 'defaults-test', 'Defaults Test', false)"
        )
    )
    conn.execute(
        text(
            "insert into documents (id, project_id, sha256, filename, "
            "doc_type, parse_status) values (9998, 9998, 'abc', 'f.pdf', "
            "'matrix', 'parsed')"
        )
    )
    conn.execute(
        text(
            "insert into dependencies (id, project_id, ref_code, dep_type, "
            "title, status, criticality) values (9998, 9998, 'DEP-001', "
            "'utility_relocation', 'x', 'identified', 'normal')"
        )
    )
    conn.execute(
        text(
            "insert into evidence_links (dependency_id, document_id, "
            "page_no, quote) values (9998, 9998, 1, 'a quote')"
        )
    )
    row = conn.execute(
        text(
            "select verified, satisfies_requirement from evidence_links "
            "where dependency_id = 9998"
        )
    ).one()
    assert row.verified is False
    assert row.satisfies_requirement is False
