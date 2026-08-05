import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.models import DEP_STATUSES, Dependency


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
    # Matched on the constraint name, not merely on DBAPIError. A typo in
    # this statement — a renamed or dropped column, say — also raises
    # DBAPIError, so the loose assertion would go green while testing
    # nothing at all. #96 dropped a column named here and this is how that
    # stayed honest.
    with pytest.raises(DBAPIError, match="dep_status"):
        conn.execute(
            text(
                "insert into dependencies "
                "(project_id, ref_code, dep_type, title, status) "
                "values (9999, 'DEP-001', 'utility_relocation', 'x', 'ready')"
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
            "title, status) values (9998, 9998, 'DEP-001', "
            "'utility_relocation', 'x', 'identified')"
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


def test_dependency_model_declares_external_org_and_milestone_foreign_keys():
    expected = {
        "external_org_id": {
            "external_orgs.id",
        },
        "milestone_id": {
            "milestones.id",
        },
    }
    actual = {
        column_name: {fk.target_fullname for fk in Dependency.__table__.c[column_name].foreign_keys}
        for column_name in expected
    }
    assert actual == expected


def test_database_has_named_dependency_foreign_keys(conn):
    expected = {
        ("external_org_id",): {
            "name": "fk_dependencies_external_org_id_external_orgs_id",
            "referred_table": "external_orgs",
            "referred_columns": ("id",),
        },
        ("milestone_id",): {
            "name": "fk_dependencies_milestone_id_milestones_id",
            "referred_table": "milestones",
            "referred_columns": ("id",),
        },
    }
    actual = {
        tuple(fk["constrained_columns"]): {
            "name": fk["name"],
            "referred_table": fk["referred_table"],
            "referred_columns": tuple(fk["referred_columns"]),
        }
        for fk in inspect(conn).get_foreign_keys("dependencies")
        if tuple(fk["constrained_columns"]) in expected
    }
    assert actual == expected


def test_extraction_runs_require_a_prompt_version(conn):
    conn.execute(
        text(
            "insert into projects (id, slug, name, is_synthetic) "
            "values (9997, 'run-test', 'Run Test', false)"
        )
    )
    conn.execute(
        text(
            "insert into documents (id, project_id, sha256, filename, "
            "doc_type, parse_status) values (9997, 9997, 'def', 'g.pdf', "
            "'minutes', 'parsed')"
        )
    )
    with pytest.raises(DBAPIError, match="prompt_version"):
        conn.execute(
            text(
                "insert into extraction_runs "
                "(document_id, prompt_version, candidate_count, page_errors) "
                "values (9997, null, 0, 0)"
            )
        )
