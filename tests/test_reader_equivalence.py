"""One reading type, proved from both sides.

ADR-0081 stage 3 asks for equivalence across the reader surfaces and stage 4 for
a reader that imports no legacy table module. Both were held here only by a
per-surface SQL regex in ``test_native_accepted_readers``, which is a dynamic
proof of one code path taken during one test. These are the static and the
shape-level halves beside it: the module that adapts the accepted projection
imports no legacy ORM class at all, and the two adapters produce one type whose
fields either carry the same value for the same seeded record or declare, in the
customer's words, why the accepted record cannot carry it.
"""

import ast
from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

from corridor import constraint_reading
from corridor.accepted_field_reading import read_accepted_field_population
from corridor.constraint_reading import (
    ACCEPTED_RECORD,
    LEGACY,
    ConstraintReading,
    NotAvailable,
    UNAVAILABLE,
    accepted_constraint_reading,
    available,
    legacy_constraint_reading,
)
from corridor.db import Session, engine
from corridor.exceptions import accepted_record_readings
from corridor.models import Dependency, Project
from corridor.operative_support import resolve_operative_support

from adopted_reader_support import adopt_ucm_workbook

TODAY = date(2026, 9, 9)

# The frozen tables ADR-0081 retires, as the ORM classes a reader could import.
# `tests/test_architecture.py` owns the project-wide ratchet; this is the one
# module that may never appear on it, because it exists to keep those shapes from
# spreading into the readers above it.
LEGACY_ORM_CLASSES = frozenset(
    {
        "Dependency",
        "DependencyEvent",
        "ExternalPartyStatement",
        "WorkDecision",
        "OperativeSupport",
        "DisputeSettlement",
        "DisputeHistoryResolution",
        "Candidate",
        "Assertion",
        "EvidenceLink",
    }
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


def test_the_accepted_projection_adapter_imports_no_legacy_orm_class():
    """ADR-0081 stage 4's criterion, as a static rule rather than a live query.

    The SQL-regex hook in ``test_native_accepted_readers`` proves that one
    exercised read path selected no legacy table. It cannot prove that a legacy
    shape has not been imported into the adapter for a path no test exercised.
    This can, and it fails at the import rather than at the query.
    """
    source = Path(constraint_reading.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "corridor"
        ):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "models":
                imported.add(node.attr)
    assert imported & LEGACY_ORM_CLASSES == set()
    # And the adapters are genuinely there, so this is not passing by vacancy.
    assert {"legacy_constraint_reading", "accepted_constraint_reading"} <= set(
        dir(constraint_reading)
    )


def test_every_reading_field_is_a_value_or_a_declared_marker(
    session, tmp_path, monkeypatch
):
    """No field of either reading is left as a bare ``None`` standing for absence.

    ``None`` is a legitimate *value* for a date nobody has stated. What it may not
    be is the answer to "does this record have an owner at all", which is what the
    eleven hard-coded class attributes on ``AcceptedConstraint`` made it.
    """
    project, _ = adopt_ucm_workbook(session, tmp_path, monkeypatch)
    population = read_accepted_field_population(session, project.id)
    readings = accepted_record_readings(session, population)
    assert readings

    declared = {
        "internal_owner",
        "next_action",
        "action_due_date",
        "milestone_id",
        "milestone_registration_id",
        "dismissed_at",
        "evidence_required",
        "is_ready",
        "readiness_lapsed",
        "last_evidenced_at",
        "contradicted_fields",
        "superseded_scopes",
    }
    for reading in readings.values():
        assert reading.mode == ACCEPTED_RECORD
        for name in declared:
            marker = getattr(reading, name)
            assert isinstance(marker, NotAvailable), name
            # The reason is what a report section publishes, so it has to be a
            # sentence a customer could be shown rather than the bare default.
            assert str(marker) != UNAVAILABLE, name
            assert len(str(marker).split()) >= 8, name
        # The two facts the accepted record *can* answer are answered.
        assert available(reading.has_supporting_documentation)
        assert available(reading.depends_on_superseded_support)


def test_the_legacy_gather_and_the_accepted_projection_agree_on_one_shape(
    session, tmp_path, monkeypatch
):
    """The equivalence the two adapters owe each other.

    Same seeded Constraint, expressed once as a legacy `dependencies` row and once
    as an accepted Project Record subject: one type, one field set, the same
    values for every fact both populations hold.
    """
    adopted_project, _ = adopt_ucm_workbook(session, tmp_path, monkeypatch)
    population = read_accepted_field_population(session, adopted_project.id)
    accepted = accepted_constraint_reading(
        next(r for r in population.open_records if r.ref_code == "UC-1"),
        support_in_use={},
    )

    legacy_project = Project(
        slug="reader-equivalence-legacy", name="Legacy", is_synthetic=True
    )
    session.add(legacy_project)
    session.flush()
    dependency = Dependency(
        project_id=legacy_project.id,
        ref_code="UC-1",
        dep_type="utility_relocation",
        title="Water — City Water",
        source_ref="UC-1",
        station_from="100+00",
        station_to="101+00",
        need_date=date(2026, 9, 20),
        resolution_strategy="relocate",
    )
    session.add(dependency)
    session.flush()
    legacy = legacy_constraint_reading(
        dependency,
        support=resolve_operative_support(session, (dependency.id,))[dependency.id],
        committed_date=date(2026, 8, 1),
    )

    assert type(legacy) is type(accepted) is ConstraintReading
    assert {f.name for f in fields(legacy)} == {f.name for f in fields(accepted)}
    assert legacy.mode == LEGACY and accepted.mode == ACCEPTED_RECORD
    for name in (
        "ref_code",
        "source_ref",
        "title",
        "dep_type",
        "station_from",
        "station_to",
        "need_date",
        "committed_date",
        "resolution_strategy",
    ):
        assert getattr(legacy, name) == getattr(accepted, name), name
    assert legacy.critical is accepted.critical is True
    assert legacy.live is accepted.live is True

    # The accepted projection with no Supporting Documentation in use says so,
    # rather than reporting the Source Passage Check the legacy rule counted.
    assert accepted.has_supporting_documentation is False
    assert accepted.checked_source_count > 0
