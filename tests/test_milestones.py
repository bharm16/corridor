from datetime import date

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.milestones import (
    MalformedMilestoneCsv,
    import_csv,
    link_all,
    link_dependency,
    main,
)
from corridor.models import Dependency, Milestone, MilestoneRegistration, Project
from corridor.report import build_report

CSV = """code,name,need_date
UTIL-CLEAR,Utility clearance,2026-11-01
ROW-CLEAR,Right of way clear,2026-09-15
LET,Letting,2027-01-20
"""


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
    p = Project(slug="ms-test", name="Milestone Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def write(tmp_path, text=CSV, name="milestones.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


def make_dep(session, project, ref, **kw):
    d = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type=kw.pop("dep_type", "utility_relocation"),
        title="x",
        **kw,
    )
    session.add(d)
    session.flush()
    return d


def test_command_usage_explains_key_dates_and_preserves_the_technical_argument(capsys):
    assert main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[milestone-code]" in captured.err
    assert "key dates" in captured.err
    assert "constraints" in captured.err


def test_import_creates_milestones(session, project, tmp_path):
    result = import_csv(session, project_id=project.id, path=write(tmp_path))
    assert len(result.created) == 3
    codes = {m.code for m in result.created}
    assert codes == {"UTIL-CLEAR", "ROW-CLEAR", "LET"}
    util = next(m for m in result.created if m.code == "UTIL-CLEAR")
    assert util.need_date == date(2026, 11, 1)
    assert util.source == "milestones.csv"
    [registration] = session.scalars(
        select(MilestoneRegistration).where(
            MilestoneRegistration.milestone_id == util.id
        )
    ).all()
    assert registration.source_name == "milestones.csv"
    assert len(registration.source_sha256) == 64
    assert registration.source_row_json == {
        "code": "UTIL-CLEAR",
        "name": "Utility clearance",
        "need_date": "2026-11-01",
    }


def test_reimport_updates_in_place(session, project, tmp_path):
    """A schedule revision is normal; a second row per code would split
    every dependency already linked to it."""
    import_csv(session, project_id=project.id, path=write(tmp_path))
    revised = CSV.replace("2026-11-01", "2026-12-15")
    result = import_csv(session, project_id=project.id, path=write(tmp_path, revised))

    assert result.created == []
    assert len(result.updated) == 3

    all_ms = session.scalars(
        select(Milestone).where(Milestone.project_id == project.id)
    ).all()
    assert len(all_ms) == 3
    util = next(m for m in all_ms if m.code == "UTIL-CLEAR")
    assert util.need_date == date(2026, 12, 15)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-01", date(2026, 11, 1)),
        ("11/01/2026", date(2026, 11, 1)),
        ("01-Nov-2026", date(2026, 11, 1)),
    ],
)
def test_common_date_formats_are_accepted(session, project, tmp_path, value, expected):
    text = f"code,name,need_date\nX,Example,{value}\n"
    [milestone] = import_csv(
        session, project_id=project.id, path=write(tmp_path, text)
    ).created
    assert milestone.need_date == expected


def test_a_blank_need_date_is_allowed(session, project, tmp_path):
    """Not every milestone has a date yet; that is a fact, not an error."""
    text = "code,name,need_date\nTBD,Not scheduled,\n"
    [milestone] = import_csv(
        session, project_id=project.id, path=write(tmp_path, text)
    ).created
    assert milestone.need_date is None


def test_a_missing_column_refuses_the_whole_import(session, project, tmp_path):
    """Half a schedule would put DUE_SOON and ORPHAN quietly wrong."""
    text = "code,name\nX,No date column\n"
    with pytest.raises(MalformedMilestoneCsv, match="need_date"):
        import_csv(session, project_id=project.id, path=write(tmp_path, text))


def test_an_unreadable_date_refuses_the_whole_import(session, project, tmp_path):
    text = "code,name,need_date\nX,Example,next Tuesday\n"
    with pytest.raises(MalformedMilestoneCsv, match="line 2"):
        import_csv(session, project_id=project.id, path=write(tmp_path, text))


def test_linking_copies_the_need_date_onto_the_dependency(session, project, tmp_path):
    """Copied, not joined: a schedule revision must not silently rewrite
    the date on records already reported against the old one."""
    [util, *_] = [
        m
        for m in import_csv(
            session, project_id=project.id, path=write(tmp_path)
        ).created
        if m.code == "UTIL-CLEAR"
    ]
    dep = make_dep(session, project, "DEP-1")
    link_dependency(session, dep, util, actor="tester")

    assert dep.milestone_id == util.id
    assert dep.milestone_registration_id is not None
    assert dep.need_date == date(2026, 11, 1)


def test_report_derivations_name_the_exact_milestone_registration(
    session, project, tmp_path
):
    [util, *_] = [
        milestone
        for milestone in import_csv(
            session, project_id=project.id, path=write(tmp_path)
        ).created
        if milestone.code == "UTIL-CLEAR"
    ]
    dependency = make_dep(session, project, "DEP-REGISTRATION")
    link_dependency(session, dependency, util, actor="local:scheduler")

    report = build_report(session, project.id)
    milestone_section = next(
        section for section in report.sections if section.title == "Constraints by key date"
    )
    [row] = milestone_section.rows
    expected = (f"Milestone Registration MR{dependency.milestone_registration_id}",)

    assert row[0].provenance.input_refs == expected
    assert row[1].provenance.input_refs == expected


def test_linking_across_projects_is_refused(session, project, tmp_path):
    [util, *_] = [
        m
        for m in import_csv(
            session, project_id=project.id, path=write(tmp_path)
        ).created
        if m.code == "UTIL-CLEAR"
    ]
    other = Project(slug="ms-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray = make_dep(session, other, "DEP-x")
    with pytest.raises(ValueError):
        link_dependency(session, stray, util, actor="tester")


def test_bulk_link_only_touches_unlinked_records(session, project, tmp_path):
    milestones = import_csv(
        session, project_id=project.id, path=write(tmp_path)
    ).created
    util = next(m for m in milestones if m.code == "UTIL-CLEAR")
    row = next(m for m in milestones if m.code == "ROW-CLEAR")

    a = make_dep(session, project, "DEP-a")
    b = make_dep(session, project, "DEP-b")
    link_dependency(session, b, row, actor="tester")

    count = link_all(
        session, project_id=project.id, milestone_code="UTIL-CLEAR", actor="tester"
    )
    assert count == 1
    assert a.milestone_id == util.id
    assert b.milestone_id == row.id


def test_bulk_link_can_be_scoped_to_a_dependency_type(session, project, tmp_path):
    import_csv(session, project_id=project.id, path=write(tmp_path))
    utility = make_dep(session, project, "DEP-u", dep_type="utility_relocation")
    permit = make_dep(session, project, "DEP-p", dep_type="permit")

    link_all(
        session,
        project_id=project.id,
        milestone_code="UTIL-CLEAR",
        dep_type="utility_relocation",
        actor="tester",
    )
    assert utility.milestone_id is not None
    assert permit.milestone_id is None


def test_bulk_link_to_an_unknown_milestone_raises(session, project):
    with pytest.raises(LookupError):
        link_all(session, project_id=project.id, milestone_code="NOPE", actor="tester")
