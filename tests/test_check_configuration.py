"""Per-project check-threshold configuration on real PostgreSQL.

These cover the domain module directly: validation and refusal, attributable
append-only versions, project isolation, that the declared configuration
actually reaches the shared Evaluation interfaces for a new reading, and that a
preview evaluates without writing anything.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text, update

from corridor.check_configuration import (
    InvalidCheckConfiguration,
    ConfigurationPreview,
    configuration_history,
    effective_configuration,
    effective_thresholds,
    preview_configuration,
    save_configuration,
    validate_proposed_thresholds,
)
from corridor.changes import record_run
from corridor.db import Session, engine
from corridor.exceptions import (
    RULESET_VERSION,
    Thresholds,
    evaluate_project,
)
from corridor.ledger import load_dependency
from corridor.models import Dependency, Project, ProjectCheckConfiguration, ReportRun
from corridor.principals import HumanPrincipal
from corridor.project_reading import freeze_project_reading

TEST_PRINCIPAL = HumanPrincipal("local:ops-reviewer")


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
    p = Project(slug="checks-cfg", name="Checks Config", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _make_dep(session, project, ref, **kw):
    d = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type="utility_relocation",
        title="Telecom — Example",
        **kw,
    )
    session.add(d)
    session.flush()
    return d


def _reader_rules(session, project):
    """The rules a product reader raises — through the shared reading funnel."""
    return {e.rule for e in freeze_project_reading(session, project.id).evaluation.found}


def _config_count(session, project_id):
    return session.scalar(
        select(func.count())
        .select_from(ProjectCheckConfiguration)
        .where(ProjectCheckConfiguration.project_id == project_id)
    )


# --------------------------------------------------------------- defaults


def test_no_declaration_uses_the_supported_defaults(session, project):
    effective = effective_configuration(session, project.id)
    assert effective.is_declared is False
    assert effective.configuration_id is None
    assert effective.declared_by is None
    assert effective.thresholds == Thresholds()
    assert effective.ruleset_version == RULESET_VERSION
    assert effective_thresholds(session, project.id) == Thresholds()


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "bad",
    [
        {"stale_days": 10, "due_soon_days": 20},  # incomplete
        {
            "stale_days": 10,
            "due_soon_days": 20,
            "action_due_soon_days": 5,
            "extra": 1,
        },  # unknown key
        {"stale_days": 0, "due_soon_days": 20, "action_due_soon_days": 5},  # < min
        {
            "stale_days": 10,
            "due_soon_days": 99999,
            "action_due_soon_days": 5,
        },  # > max
        {"stale_days": "abc", "due_soon_days": 20, "action_due_soon_days": 5},
        {"stale_days": "", "due_soon_days": 20, "action_due_soon_days": 5},
        {"stale_days": "3.5", "due_soon_days": 20, "action_due_soon_days": 5},
        {"stale_days": True, "due_soon_days": 20, "action_due_soon_days": 5},
    ],
)
def test_invalid_or_incomplete_is_refused(bad):
    with pytest.raises(InvalidCheckConfiguration):
        validate_proposed_thresholds(bad)


def test_a_refused_save_writes_no_configuration(session, project):
    with pytest.raises(InvalidCheckConfiguration):
        save_configuration(
            session,
            project.id,
            {"stale_days": 0, "due_soon_days": 20, "action_due_soon_days": 5},
            principal=TEST_PRINCIPAL,
        )
    assert _config_count(session, project.id) == 0
    # The effective configuration is untouched: still the supported default.
    assert effective_configuration(session, project.id).is_declared is False


def test_form_supplied_author_is_never_accepted(session, project):
    """Authority comes from the typed principal, not a free-text label."""
    with pytest.raises(Exception):
        save_configuration(
            session,
            project.id,
            {"stale_days": 10, "due_soon_days": 20, "action_due_soon_days": 5},
            principal="local:someone",  # a string is not a HumanPrincipal
        )
    assert _config_count(session, project.id) == 0


# ---------------------------------------------------------- attributable


def test_saving_creates_a_new_attributable_identity(session, project):
    row = save_configuration(
        session,
        project.id,
        {"stale_days": 10, "due_soon_days": 15, "action_due_soon_days": 3},
        principal=TEST_PRINCIPAL,
    )
    assert row.id is not None
    assert row.created_by == TEST_PRINCIPAL.subject
    assert row.ruleset_version == RULESET_VERSION
    assert row.created_at is not None

    effective = effective_configuration(session, project.id)
    assert effective.is_declared is True
    assert effective.configuration_id == row.id
    assert effective.declared_by == TEST_PRINCIPAL.subject
    assert effective.thresholds == Thresholds(
        stale_days=10, due_soon_days=15, action_due_soon_days=3
    )


def test_history_is_append_only_and_earlier_versions_remain(session, project):
    first = save_configuration(
        session,
        project.id,
        {"stale_days": 10, "due_soon_days": 15, "action_due_soon_days": 3},
        principal=TEST_PRINCIPAL,
    )
    second = save_configuration(
        session,
        project.id,
        {"stale_days": 20, "due_soon_days": 25, "action_due_soon_days": 6},
        principal=HumanPrincipal("local:ops-two"),
    )
    # Both retained; the newest is effective, the earlier one still readable.
    history = configuration_history(session, project.id)
    assert [row.id for row in history] == [second.id, first.id]
    assert effective_configuration(session, project.id).configuration_id == second.id
    assert history[-1].stale_days == 10  # the earlier declaration is intact


def test_append_only_trigger_blocks_update_and_delete(session, project):
    row = save_configuration(
        session,
        project.id,
        {"stale_days": 10, "due_soon_days": 15, "action_due_soon_days": 3},
        principal=TEST_PRINCIPAL,
    )
    with pytest.raises(Exception):
        with session.begin_nested():
            session.execute(
                update(ProjectCheckConfiguration)
                .where(ProjectCheckConfiguration.id == row.id)
                .values(stale_days=99)
            )
    with pytest.raises(Exception):
        with session.begin_nested():
            session.execute(
                text("delete from project_check_configurations where id = :i"),
                {"i": row.id},
            )
    # The row is unchanged and still there.
    session.expire_all()
    kept = session.get(ProjectCheckConfiguration, row.id)
    assert kept is not None and kept.stale_days == 10


# --------------------------------------------- reaches the shared readers


def test_declared_configuration_reaches_the_product_readers(session, project):
    """A Need Date 20 days out fires DUE_SOON at 30d but not at 10d."""
    dep = _make_dep(
        session, project, "DUE-1", need_date=date.today() + timedelta(days=20)
    )
    # Default (due_soon_days=30): the reading funnel and the record page fire it.
    assert "DUE_SOON" in _reader_rules(session, project)
    assert "DUE_SOON" in {e.rule for e in load_dependency(session, dep.id).exceptions}

    save_configuration(
        session,
        project.id,
        {"stale_days": 14, "due_soon_days": 10, "action_due_soon_days": 7},
        principal=TEST_PRINCIPAL,
    )

    # The declared 10-day horizon now reaches the shared reading and the record
    # page — neither ignores it — and the Evaluation stamps what it used.
    reading = freeze_project_reading(session, project.id)
    assert reading.evaluation.thresholds.due_soon_days == 10
    assert "DUE_SOON" not in {e.rule for e in reading.evaluation.found}
    assert "DUE_SOON" not in {
        e.rule for e in load_dependency(session, dep.id).exceptions
    }


def test_the_engine_applies_defaults_when_a_caller_states_none(session, project):
    """The engine is foundational: readers compose the declared config in.

    A direct evaluate_project without thresholds uses the supported defaults
    even when a declaration exists — the resolution belongs to the readers, so
    the engine never silently depends on the configuration layer.
    """
    _make_dep(session, project, "DUE-1", need_date=date.today() + timedelta(days=20))
    save_configuration(
        session,
        project.id,
        {"stale_days": 14, "due_soon_days": 10, "action_due_soon_days": 7},
        principal=TEST_PRINCIPAL,
    )
    plain = evaluate_project(session, project.id)
    assert plain.thresholds.due_soon_days == 30
    assert "DUE_SOON" in {e.rule for e in plain.found}


def test_an_explicit_threshold_argument_still_wins(session, project):
    """A caller that pins thresholds keeps them despite a declaration."""
    save_configuration(
        session,
        project.id,
        {"stale_days": 14, "due_soon_days": 10, "action_due_soon_days": 7},
        principal=TEST_PRINCIPAL,
    )
    pinned = evaluate_project(
        session, project.id, thresholds=Thresholds(due_soon_days=25)
    )
    assert pinned.thresholds.due_soon_days == 25


def test_configuration_is_project_scoped(session):
    a = Project(slug="cfg-a", name="A", is_synthetic=True)
    b = Project(slug="cfg-b", name="B", is_synthetic=True)
    session.add_all((a, b))
    session.flush()
    _make_dep(session, a, "A-1", need_date=date.today() + timedelta(days=20))
    _make_dep(session, b, "B-1", need_date=date.today() + timedelta(days=20))

    save_configuration(
        session,
        a.id,
        {"stale_days": 14, "due_soon_days": 10, "action_due_soon_days": 7},
        principal=TEST_PRINCIPAL,
    )

    # Project A tightened; project B is untouched by A's declaration.
    assert effective_configuration(session, b.id).is_declared is False
    assert effective_thresholds(session, b.id) == Thresholds()
    assert "DUE_SOON" not in _reader_rules(session, a)
    assert "DUE_SOON" in _reader_rules(session, b)


# ------------------------------------------------------- preview no-writes


def test_preview_evaluates_without_writing_anything(session, project):
    _make_dep(session, project, "DUE-1", need_date=date.today() + timedelta(days=20))
    configs_before = _config_count(session, project.id)
    runs_before = session.scalar(
        select(func.count()).select_from(ReportRun).where(
            ReportRun.project_id == project.id
        )
    )

    preview = preview_configuration(
        session,
        project.id,
        {"stale_days": 14, "due_soon_days": 10, "action_due_soon_days": 7},
    )
    assert isinstance(preview, ConfigurationPreview)
    assert preview.proposed_thresholds.due_soon_days == 10
    # DUE_SOON is absent under the proposed 10-day horizon.
    assert "DUE_SOON" not in {facet.rule for facet in preview.facets}

    session.expire_all()
    # No configuration row, no report run, and the effective config is unmoved.
    assert _config_count(session, project.id) == configs_before == 0
    assert (
        session.scalar(
            select(func.count()).select_from(ReportRun).where(
                ReportRun.project_id == project.id
            )
        )
        == runs_before
    )
    assert effective_configuration(session, project.id).is_declared is False
    # And a real new reading still uses the effective default, not the preview.
    assert "DUE_SOON" in _reader_rules(session, project)


def test_preview_refuses_an_invalid_proposal(session, project):
    with pytest.raises(InvalidCheckConfiguration):
        preview_configuration(
            session,
            project.id,
            {"stale_days": 0, "due_soon_days": 20, "action_due_soon_days": 5},
        )
