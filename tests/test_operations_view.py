"""The operator's reading, read without a browser (#344's two moved predicates).

``explanation_offered`` and ``record_inclusion_pending`` were spelled in the web
adapter beside the identical rules in ``production_run_explanation`` and
``record_inclusion``. These state them against
``corridor.web.operations_view`` and against those owners in the same breath, so
the two can no longer disagree quietly.
"""

from hashlib import sha256


from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import Document, RecordInclusionRequest
from corridor.principals import HumanPrincipal
from corridor.production_run_explanation import (
    competing_production_runs,
    competing_runs_state_token,
    runs_compete,
)
from corridor.record_inclusion import record_inclusion_pending
from corridor.web.operations_view import operations_view

OPERATOR = HumanPrincipal("local:operations")


def _document(session, project, filename):
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.id}:{filename}".encode()).hexdigest(),
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _completed_run(session, document, prompt_version):
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=0,
        page_errors=0,
        model="test-model",
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run


def test_an_explanation_is_offered_only_where_the_runs_actually_compete(
    session, project
):
    """Two completed runs are a choice; one is the reading, and none is nothing.

    ``production_run_explanation.runs_compete`` is the one authority, and it is
    the same answer its request refusal gives.
    """
    document = _document(session, project, "utility-matrix.pdf")

    assert operations_view(session, project_id=project.id).documents[
        0
    ].explanation_offered is False

    _completed_run(session, document, "operations-view-v1")
    row = operations_view(session, project_id=project.id).documents[0]
    assert row.explanation_offered is False
    assert runs_compete(competing_production_runs(session, document.id)) is False

    _completed_run(session, document, "operations-view-v2")
    row = operations_view(session, project_id=project.id).documents[0]
    assert row.explanation_offered is True
    assert runs_compete(competing_production_runs(session, document.id)) is True
    assert len(row.competing_run_ids) == 2


def test_the_state_token_the_screen_offers_is_the_one_the_refusal_recomputes(
    session, project
):
    """The reading composes the digest from rows it already holds.

    It must still be byte-identical to what the writing path recomputes from the
    database, or every control on the screen would refuse as stale.
    """
    document = _document(session, project, "utility-matrix.pdf")
    _completed_run(session, document, "operations-view-v1")
    run = _completed_run(session, document, "operations-view-v2")

    row = operations_view(session, project_id=project.id).documents[0]
    assert row.offer_state == competing_runs_state_token(session, document.id)
    assert row.explain_state == row.offer_state

    declare_active_run(session, document.id, run.id, principal=OPERATOR)
    session.flush()
    refreshed = operations_view(session, project_id=project.id).documents[0]
    # A declaration changes the state every control was offered under.
    assert refreshed.offer_state != row.offer_state
    assert refreshed.offer_state == competing_runs_state_token(session, document.id)
    assert refreshed.active_run_id == run.id


def test_record_inclusion_pending_is_the_handoffs_own_answer(session, project):
    """Declaring a Current Production Run leaves an unreconciled load.

    The screen used to compare ``dirty_seq`` with ``reconciled_seq`` itself; the
    predicate belongs to ``record_inclusion``, which owns the two counters.
    """
    document = _document(session, project, "utility-matrix.pdf")
    run = _completed_run(session, document, "operations-view-v1")
    # Drain the handoff the completed run itself requested, so what is asserted
    # below is the consequence of the declaration and nothing earlier.
    handoff = session.get(RecordInclusionRequest, project.id)
    assert handoff is not None
    handoff.reconciled_seq = handoff.dirty_seq
    session.flush()

    assert record_inclusion_pending(session, project.id) is False
    assert (
        operations_view(session, project_id=project.id).record_inclusion_pending
        is False
    )

    declare_active_run(session, document.id, run.id, principal=OPERATOR)
    session.flush()

    assert record_inclusion_pending(session, project.id) is True
    assert (
        operations_view(session, project_id=project.id).record_inclusion_pending
        is True
    )


def test_the_reading_is_bounded_rather_than_a_query_per_document(session, project):
    """Every per-document answer comes from rows read once for the project.

    The screen used to add six queries per document plus one per failed attempt.
    The reading's query count must therefore not grow with the number of
    documents, which is what this states.
    """
    for index in range(2):
        document = _document(session, project, f"matrix-{index}.pdf")
        _completed_run(session, document, f"operations-view-{index}a")
        _completed_run(session, document, f"operations-view-{index}b")
    session.flush()

    counted: list[str] = []
    from sqlalchemy import event

    def record(conn, cursor, statement, parameters, context, executemany):
        counted.append(statement)

    event.listen(session.get_bind(), "before_cursor_execute", record)
    try:
        two = operations_view(session, project_id=project.id)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", record)
    assert len(two.documents) == 2
    two_document_queries = len(counted)

    for index in range(2, 6):
        document = _document(session, project, f"matrix-{index}.pdf")
        _completed_run(session, document, f"operations-view-{index}a")
        _completed_run(session, document, f"operations-view-{index}b")
    session.flush()

    counted.clear()
    event.listen(session.get_bind(), "before_cursor_execute", record)
    try:
        six = operations_view(session, project_id=project.id)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", record)
    assert len(six.documents) == 6
    assert len(counted) == two_document_queries
