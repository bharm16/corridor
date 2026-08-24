import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.models import Dependency


@pytest.fixture
def conn():
    engine = create_engine(settings.database_url)
    with engine.connect() as c:
        trans = c.begin()
        yield c
        trans.rollback()


def test_dependency_has_no_mutable_lifecycle_status(conn):
    """ADR-0044: supported facts replace the unauthoritative status enum."""
    columns = {column["name"] for column in inspect(conn).get_columns("dependencies")}
    assert "status" not in columns
    assert "status" not in Dependency.__table__.c


def test_evidence_defaults_to_unverified_and_has_no_sufficiency_role(conn):
    """Neither verification nor readiness is ever assumed."""
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
            "title) values (9998, 9998, 'DEP-001', "
            "'utility_relocation', 'x')"
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
            "select evidence.verified, exists ("
            "select 1 from dependency_evidence_sufficiencies sufficiency "
            "where sufficiency.evidence_link_id = evidence.id"
            ") as has_sufficiency from evidence_links evidence "
            "where evidence.dependency_id = 9998"
        )
    ).one()
    assert row.verified is False
    assert row.has_sufficiency is False


def test_dependency_model_declares_external_org_and_milestone_foreign_keys():
    expected = {
        "external_org_id": {
            "external_orgs.id",
        },
        "milestone_id": {
            "milestones.id",
        },
        "milestone_registration_id": {
            "milestone_registrations.id",
        },
    }
    actual = {
        column_name: {
            fk.target_fullname
            for fk in Dependency.__table__.c[column_name].foreign_keys
        }
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
        ("milestone_registration_id",): {
            "name": "fk_dependencies_milestone_registration",
            "referred_table": "milestone_registrations",
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
