from datetime import date

import pytest
from sqlalchemy import select

from corridor.milestones import (
    CHANGED,
    NEW,
    NEW_SOURCE_VERSION,
    UNCHANGED,
    MalformedMilestoneCsv,
    MalformedScheduleXer,
    StaleMilestoneImport,
    confirm_import,
    import_csv,
    import_xer,
    link_all,
    link_dependency,
    main,
    preview_import,
)
from corridor.models import AuditLog, Dependency, Milestone, MilestoneRegistration, Project
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.report import build_report

ALICE = HumanPrincipal("local:alice")

CSV = """code,name,need_date
UTIL-CLEAR,Utility clearance,2026-11-01
ROW-CLEAR,Right of way clear,2026-09-15
LET,Letting,2027-01-20
"""


def registration_ids(session, project):
    return set(
        session.scalars(
            select(MilestoneRegistration.id)
            .join(Milestone, MilestoneRegistration.milestone_id == Milestone.id)
            .where(Milestone.project_id == project.id)
        ).all()
    )


def milestone_codes(session, project):
    return set(
        session.scalars(
            select(Milestone.code).where(Milestone.project_id == project.id)
        ).all()
    )


def _seed(session, project, content=CSV, source_name="typed.csv", principal=ALICE):
    """Register key dates through the attributable CSV path, returning the preview."""
    preview = preview_import(
        session, project_id=project.id, content=content.encode(), source_name=source_name
    )
    confirm_import(
        session,
        project_id=project.id,
        content=content.encode(),
        source_name=source_name,
        expected_sha256=preview.source_sha256,
        expected_predecessors=preview.predecessors,
        principal=principal,
    )
    return preview


def xer(task_rows, *, include_task=True, header=True, taskpred=True):
    """Build a minimal but shaped P6 XER export (tab-delimited table dump)."""
    fields = (
        "task_id",
        "proj_id",
        "task_code",
        "task_name",
        "task_type",
        "target_start_date",
        "target_end_date",
    )
    lines = []
    if header:
        lines.append(
            "\t".join(
                ["ERMHDR", "19.12", "2026-08-29", "Project", "admin", "admin", "db", "US", "USD"]
            )
        )
    lines += [
        "\t".join(["%T", "PROJECT"]),
        "\t".join(["%F", "proj_id", "proj_short_name"]),
        "\t".join(["%R", "1", "SEG3C2"]),
    ]
    if include_task:
        lines.append("\t".join(["%T", "TASK"]))
        lines.append("\t".join(["%F", *fields]))
        for row in task_rows:
            lines.append("\t".join(["%R", *row]))
    if taskpred:
        lines += [
            "\t".join(["%T", "TASKPRED"]),
            "\t".join(["%F", "task_pred_id", "task_id", "pred_task_id", "pred_type"]),
            "\t".join(["%R", "900", "102", "101", "PR_FS"]),
        ]
    lines.append("%E")
    return "\n".join(lines).encode("cp1252")


XER_ROWS = [
    ("101", "1", "A1000", "Mobilize", "TT_Task", "2026-09-01 00:00", "2026-09-30 00:00"),
    ("102", "1", "UTIL-CLEAR", "Utility clearance complete", "TT_FinMile", "", "2026-11-01 00:00"),
    ("103", "1", "ROW-START", "Right of way start", "TT_Mile", "2026-09-15 00:00", ""),
    ("104", "1", "", "Blank code milestone", "TT_Mile", "2026-10-01 00:00", ""),
]


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


# --- Hand-typed CSV preview (ADR-0055/0057: the stopgap keeps its preview) ---


def test_preview_of_a_fresh_import_is_all_new_and_writes_nothing(session, project):
    before = registration_ids(session, project)
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    assert {row.classification for row in preview.rows} == {NEW}
    assert preview.new_count == 3
    assert preview.source_name == "typed.csv"
    assert len(preview.source_sha256) == 64
    assert preview.predecessors == {"UTIL-CLEAR": None, "ROW-CLEAR": None, "LET": None}
    # Read-only: no Milestone or Key Date Version was created.
    assert milestone_codes(session, project) == set()
    assert registration_ids(session, project) == before


def test_preview_after_identical_confirm_is_all_unchanged(session, project):
    _seed(session, project)
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    assert {row.classification for row in preview.rows} == {UNCHANGED}
    assert preview.unchanged_count == 3


def test_preview_separates_a_moved_date_from_an_unchanged_new_version(session, project):
    """A revised file touches three rows but only one date moved; the counts
    must not present the untouched rows as moved dates."""
    _seed(session, project)
    revised = CSV.replace("2026-11-01", "2026-12-15")  # only UTIL-CLEAR moves
    preview = preview_import(
        session, project_id=project.id, content=revised.encode(), source_name="typed.csv"
    )
    util = next(row for row in preview.rows if row.code == "UTIL-CLEAR")
    assert util.classification == CHANGED
    assert util.prior_need_date == date(2026, 11, 1)
    assert util.need_date == date(2026, 12, 15)
    others = {row.classification for row in preview.rows if row.code != "UTIL-CLEAR"}
    assert others == {NEW_SOURCE_VERSION}
    assert preview.changed_count == 1
    assert preview.new_version_count == 2
    assert preview.unchanged_count == 0


def test_preview_of_identical_rows_from_a_new_source_is_a_new_version(session, project):
    _seed(session, project, source_name="typed.csv")
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="other.csv"
    )
    assert {row.classification for row in preview.rows} == {NEW_SOURCE_VERSION}


def test_preview_refuses_a_malformed_sheet_without_writing(session, project):
    with pytest.raises(MalformedMilestoneCsv, match="need_date"):
        preview_import(
            session,
            project_id=project.id,
            content=b"code,name\nX,No date column\n",
            source_name="bad.csv",
        )
    assert milestone_codes(session, project) == set()


def test_preview_keeps_an_unknown_date_visibly_unknown(session, project):
    preview = preview_import(
        session,
        project_id=project.id,
        content=b"code,name,need_date\nTBD,Not scheduled,\n",
        source_name="typed.csv",
    )
    [row] = preview.rows
    assert row.need_date is None
    assert row.classification == NEW


# --- Attributable CSV confirm ---


def test_confirm_records_the_person_never_a_generic_import_label(session, project):
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    result = confirm_import(
        session,
        project_id=project.id,
        content=CSV.encode(),
        source_name="typed.csv",
        expected_sha256=preview.source_sha256,
        expected_predecessors=preview.predecessors,
        principal=ALICE,
    )
    assert len(result.created) == 3
    registrations = session.scalars(
        select(MilestoneRegistration)
        .join(Milestone, MilestoneRegistration.milestone_id == Milestone.id)
        .where(Milestone.project_id == project.id)
    ).all()
    assert {r.recorded_by for r in registrations} == {"local:alice"}
    actors = session.scalars(
        select(AuditLog.actor).where(AuditLog.entity_type == "milestone")
    ).all()
    assert set(actors) == {"local:alice"}
    assert "import" not in set(actors)


def test_confirm_refuses_a_free_text_actor(session, project):
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    with pytest.raises(InvalidHumanPrincipal):
        confirm_import(
            session,
            project_id=project.id,
            content=CSV.encode(),
            source_name="typed.csv",
            expected_sha256=preview.source_sha256,
            expected_predecessors=preview.predecessors,
            principal="alice",
        )
    assert milestone_codes(session, project) == set()


def test_confirm_refuses_changed_content_and_imports_nothing(session, project):
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    tampered = CSV.replace("2026-11-01", "2026-12-15")
    with pytest.raises(StaleMilestoneImport) as excinfo:
        confirm_import(
            session,
            project_id=project.id,
            content=tampered.encode(),
            source_name="typed.csv",
            expected_sha256=preview.source_sha256,
            expected_predecessors=preview.predecessors,
            principal=ALICE,
        )
    assert milestone_codes(session, project) == set()
    # The refusal carries a fresh preview of the state that actually holds now.
    assert excinfo.value.preview.rows


def test_confirm_refuses_stale_project_state_and_refuses_the_whole_operation(
    session, project
):
    preview = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    # A concurrent import lands UTIL-CLEAR between preview and confirm.
    concurrent = "code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-11-01\n"
    _seed(session, project, content=concurrent, source_name="concurrent.csv", principal=HumanPrincipal("local:bob"))
    with pytest.raises(StaleMilestoneImport):
        confirm_import(
            session,
            project_id=project.id,
            content=CSV.encode(),
            source_name="typed.csv",
            expected_sha256=preview.source_sha256,
            expected_predecessors=preview.predecessors,
            principal=ALICE,
        )
    # The whole operation was refused: the other rows were not partially created.
    assert milestone_codes(session, project) == {"UTIL-CLEAR"}


def test_confirm_is_idempotent_and_makes_no_redundant_versions(session, project):
    first = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    confirm_import(
        session,
        project_id=project.id,
        content=CSV.encode(),
        source_name="typed.csv",
        expected_sha256=first.source_sha256,
        expected_predecessors=first.predecessors,
        principal=ALICE,
    )
    after_first = registration_ids(session, project)
    again = preview_import(
        session, project_id=project.id, content=CSV.encode(), source_name="typed.csv"
    )
    assert {row.classification for row in again.rows} == {UNCHANGED}
    confirm_import(
        session,
        project_id=project.id,
        content=CSV.encode(),
        source_name="typed.csv",
        expected_sha256=again.source_sha256,
        expected_predecessors=again.predecessors,
        principal=ALICE,
    )
    assert registration_ids(session, project) == after_first


def test_each_key_date_version_preserves_its_source_and_predecessor(session, project):
    _seed(session, project, source_name="typed-v1.csv")
    util = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project.id, Milestone.code == "UTIL-CLEAR"
        )
    ).one()
    first_id = util.current_registration_id
    revised = CSV.replace("2026-11-01", "2026-12-15")
    _seed(session, project, content=revised, source_name="typed-v2.csv")
    session.refresh(util)
    second = session.get(MilestoneRegistration, util.current_registration_id)
    first = session.get(MilestoneRegistration, first_id)
    assert second.id != first.id
    assert second.predecessor_registration_id == first.id
    assert second.source_name == "typed-v2.csv"
    assert second.source_row_json["need_date"] == "2026-12-15"
    # The earlier version is immutable and keeps its own date and source.
    assert first.source_name == "typed-v1.csv"
    assert first.source_row_json["need_date"] == "2026-11-01"


# --- Structured P6 XER import (imports itself, versioned, no confirmation) ---


def test_xer_registers_only_milestone_activities_as_versioned_key_dates(session, project):
    result = import_xer(
        session, project_id=project.id, content=xer(XER_ROWS), source_name="seg3c2.xer"
    )
    # Milestone activities only: the ordinary task and the blank-code row are not key dates.
    assert {m.code for m in result.created} == {"UTIL-CLEAR", "ROW-START"}
    assert result.skipped_blank == 1
    util = next(m for m in result.created if m.code == "UTIL-CLEAR")
    assert util.name == "Utility clearance complete"
    assert util.need_date == date(2026, 11, 1)  # finish milestone -> target_end_date
    row_start = next(m for m in result.created if m.code == "ROW-START")
    assert row_start.need_date == date(2026, 9, 15)  # start milestone -> target_start_date
    registration = session.get(MilestoneRegistration, util.current_registration_id)
    assert registration.source_name == "seg3c2.xer"
    assert len(registration.source_sha256) == 64
    # A structured self-import is a system label, not a fabricated human identity.
    assert registration.recorded_by == "import:xer"
    assert registration.source_row_json == {
        "code": "UTIL-CLEAR",
        "name": "Utility clearance complete",
        "need_date": "2026-11-01",
    }


def test_xer_reimport_of_identical_bytes_is_idempotent(session, project):
    import_xer(session, project_id=project.id, content=xer(XER_ROWS), source_name="seg3c2.xer")
    before = registration_ids(session, project)
    import_xer(session, project_id=project.id, content=xer(XER_ROWS), source_name="seg3c2.xer")
    assert registration_ids(session, project) == before


def test_xer_revision_creates_a_new_version_preserving_the_predecessor(session, project):
    import_xer(session, project_id=project.id, content=xer(XER_ROWS), source_name="seg3c2.xer")
    util = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project.id, Milestone.code == "UTIL-CLEAR"
        )
    ).one()
    first_id = util.current_registration_id
    moved = [
        row if row[2] != "UTIL-CLEAR" else (*row[:6], "2026-12-15 00:00")
        for row in XER_ROWS
    ]
    import_xer(session, project_id=project.id, content=xer(moved), source_name="seg3c2-r2.xer")
    session.refresh(util)
    assert util.need_date == date(2026, 12, 15)
    assert util.current_registration_id != first_id
    second = session.get(MilestoneRegistration, util.current_registration_id)
    assert second.predecessor_registration_id == first_id


def test_xer_ignores_sequencing_and_task_activities(session, project):
    """Only named key dates enter the record; no sequencing or task rows."""
    import_xer(session, project_id=project.id, content=xer(XER_ROWS), source_name="seg3c2.xer")
    assert milestone_codes(session, project) == {"UTIL-CLEAR", "ROW-START"}
    assert "A1000" not in milestone_codes(session, project)


def test_xer_without_a_header_refuses(session, project):
    with pytest.raises(MalformedScheduleXer, match="ERMHDR"):
        import_xer(
            session,
            project_id=project.id,
            content=b"this is not a schedule export",
            source_name="bad.xer",
        )
    assert milestone_codes(session, project) == set()


def test_xer_without_a_task_table_refuses(session, project):
    with pytest.raises(MalformedScheduleXer, match="TASK"):
        import_xer(
            session,
            project_id=project.id,
            content=xer([], include_task=False),
            source_name="empty.xer",
        )


def test_xer_milestone_without_a_date_stays_unknown(session, project):
    rows = [("105", "1", "TBD-MILE", "Not yet scheduled", "TT_Mile", "", "")]
    result = import_xer(
        session, project_id=project.id, content=xer(rows, taskpred=False), source_name="s.xer"
    )
    [milestone] = result.created
    assert milestone.code == "TBD-MILE"
    assert milestone.need_date is None


def test_xer_with_an_unreadable_milestone_date_refuses(session, project):
    rows = [("106", "1", "BAD-DATE", "Bad", "TT_FinMile", "", "next week")]
    with pytest.raises(MalformedScheduleXer, match="target_end_date"):
        import_xer(
            session, project_id=project.id, content=xer(rows, taskpred=False), source_name="s.xer"
        )
    assert milestone_codes(session, project) == set()
