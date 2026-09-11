"""What `record_counts` reads, and what the hands it replaces did not.

The argument for a derived reading is only worth as much as the contrast,
so the table sets the seven retired helpers hand-picked are recorded here,
and one write is proved invisible to every one of them and visible to the
derivation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Column, ForeignKey, MetaData, Table, Text

from committed_scenario_support import PROJECT_GRAPH_ORDER, place_project_tables
from corridor.models import Base, Dependency, Document, WorkDecision
from record_counts import nothing_written, project_record_counts

# The table sets the private helpers this module replaces watched, as they
# stood when they were retired. Every one was written by hand, and no two
# agree; they are kept as the measure of what a hand-picked set misses.
RETIRED_HAND_PICKED_SETS: dict[str, frozenset[str]] = {
    "test_adjudicate._ledger_counts": frozenset(
        {"evidence_links", "assertions", "audit_log"}
    ),
    "test_baseline_adoption._spine_counts": frozenset(
        {
            "project_record_revisions",
            "facts",
            "fact_decisions",
            "project_baseline_sources",
            "project_baseline_source_rows",
            "project_baseline_formats",
            "support_assessments",
        }
    ),
    "test_human_principals._project_counts": frozenset(
        {"candidates", "dependencies", "assertions", "evidence_links", "audit_log"}
    ),
    "test_project_portfolio._spine_counts": frozenset(
        {
            "project_record_revisions",
            "delta_record_decisions",
            "delta_dispositions",
            "delta_follow_up_plans",
            "delta_review_packet_receipts",
        }
    ),
    "test_project_workflow._spine_counts": frozenset(
        {
            "project_record_revisions",
            "delta_record_decisions",
            "delta_dispositions",
            "delta_follow_up_plans",
            "delta_review_packet_receipts",
        }
    ),
    "test_record_history._write_counts": frozenset(
        {
            "project_record_revisions",
            "delta_record_decisions",
            "delta_dispositions",
            "delta_follow_up_plans",
            "delta_review_packet_receipts",
            "audit_log",
        }
    ),
    "test_review_packets._spine_counts": frozenset(
        {
            "project_record_revisions",
            "fact_decisions",
            "delta_dispositions",
            "delta_record_decisions",
            "delta_deferrals",
            "delta_follow_up_plans",
            "delta_review_packet_receipts",
            "delta_review_packet_children",
        }
    ),
}


def _dependency(session, project) -> Dependency:
    row = Dependency(
        project_id=project.id,
        ref_code="RC-1",
        dep_type="utility_relocation",
        title="A subject to decide about",
    )
    session.add(row)
    session.flush()
    return row


# --- the set is the schema's, not this module's ----------------------------


def test_the_reading_covers_every_table_the_project_graph_derives(session, project):
    """No family is chosen here, so none can be left out here either."""

    assert set(project_record_counts(session, project.id)) == set(PROJECT_GRAPH_ORDER)
    assert len(PROJECT_GRAPH_ORDER) > 200


def test_a_new_project_scoped_table_joins_the_reading_with_no_list_edited():
    """The derivation places a table this module has never heard of.

    Proved against a copy of the real metadata rather than a toy one, so
    what joins is a table beside the two hundred that are already there.
    """

    extended = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(extended)
    Table(
        "hypothetical_project_notes",
        extended,
        Column("id", BigInteger, primary_key=True),
        Column("project_id", BigInteger, ForeignKey("projects.id"), nullable=False),
        Column("note", Text),
    )

    order, placements = place_project_tables(extended)

    assert "hypothetical_project_notes" in order
    assert placements["hypothetical_project_notes"] == (
        ("project_id", "projects", "id"),
    )


# --- what a hand-picked set misses -----------------------------------------


def test_a_write_no_retired_helper_watched_is_seen(session, project):
    """The whole argument for the card, as a contrast.

    A Work Decision is one project's coordination decision -- who owns the
    subject and what happens next. Not one of the seven retired helpers
    counted it, so a refused act that recorded one would have left every
    one of them green.
    """

    dependency = _dependency(session, project)
    watched = frozenset().union(*RETIRED_HAND_PICKED_SETS.values())

    assert "work_decisions" not in watched
    assert "work_decisions" in PROJECT_GRAPH_ORDER

    with pytest.raises(AssertionError, match="work_decisions 0 -> 1"):
        with nothing_written(session, project.id):
            session.add(
                WorkDecision(
                    dependency_id=dependency.id,
                    decision_type="assign_internal_owner",
                    field="internal_owner",
                    after_value="Dana Fields",
                    recorded_by="local:test",
                )
            )
            session.flush()


def test_every_retired_hand_picked_set_was_a_fraction_of_the_record(session, project):
    """Three to eight tables each, out of the two hundred a project holds."""

    held = set(project_record_counts(session, project.id))
    sizes = {name: len(tables) for name, tables in RETIRED_HAND_PICKED_SETS.items()}

    assert all(tables <= held for tables in RETIRED_HAND_PICKED_SETS.values()), sizes
    assert max(sizes.values()) <= 8
    assert len(held) > 25 * max(sizes.values())


# --- the check cannot be skipped -------------------------------------------


def test_a_write_far_from_the_spine_is_seen(session, project):
    """A Document is neither spine nor ledger, and it is still the record."""

    with pytest.raises(AssertionError, match="documents 0 -> 1"):
        with nothing_written(session, project.id):
            session.add(
                Document(
                    project_id=project.id,
                    sha256="e" * 64,
                    filename="refused.pdf",
                    doc_type="matrix",
                    parse_status="parsed",
                    pages=1,
                )
            )
            session.flush()


def test_the_reading_is_taken_even_when_the_refused_act_raises(session, project):
    """Half the call sites raise out of the body; the check still runs.

    A context manager that reads after a bare ``yield`` is silently skipped
    by an exception, which is the shape of guard that passes without the
    behaviour it guards.
    """

    with pytest.raises(AssertionError, match="documents 0 -> 1"):
        with nothing_written(session, project.id):
            session.add(
                Document(
                    project_id=project.id,
                    sha256="f" * 64,
                    filename="refused-then-raised.pdf",
                    doc_type="matrix",
                    parse_status="parsed",
                    pages=1,
                )
            )
            session.flush()
            raise LookupError("the act under test refused after it had written")


def test_an_act_that_writes_nothing_passes(session, project):
    _dependency(session, project)

    with nothing_written(session, project.id):
        project_record_counts(session, project.id)


def test_a_second_project_is_only_read_when_it_is_named(session, project):
    """A refusal that reaches across projects has to say which two."""

    from conftest import synthetic_project

    other = synthetic_project(session)

    with pytest.raises(AssertionError, match=f"project {other.id} documents 0 -> 1"):
        with nothing_written(session, project.id, other.id):
            session.add(
                Document(
                    project_id=other.id,
                    sha256="0" * 64,
                    filename="other-project.pdf",
                    doc_type="matrix",
                    parse_status="parsed",
                    pages=1,
                )
            )
            session.flush()


def test_a_named_exception_excuses_only_the_table_it_names(session, project):
    """`apart_from` is an exception stated, not a set chosen.

    A page view records a request receipt; it does not decide anything. The
    test that tolerates the receipt still watches the other two hundred
    tables, which is the difference from a helper that watched five.
    """

    dependency = _dependency(session, project)

    with nothing_written(session, project.id, apart_from={"documents"}):
        session.add(
            Document(
                project_id=project.id,
                sha256="1" * 64,
                filename="excused.pdf",
                doc_type="matrix",
                parse_status="parsed",
                pages=1,
            )
        )
        session.flush()

    with pytest.raises(AssertionError, match="work_decisions 0 -> 1"):
        with nothing_written(session, project.id, apart_from={"documents"}):
            session.add(
                WorkDecision(
                    dependency_id=dependency.id,
                    decision_type="assign_internal_owner",
                    field="internal_owner",
                    after_value="Dana Fields",
                    recorded_by="local:test",
                )
            )
            session.flush()


def test_a_reading_with_no_project_is_refused(session):
    with pytest.raises(TypeError, match="at least one project"):
        with nothing_written(session):
            pass
