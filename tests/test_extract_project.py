"""Extraction over a whole project, from one command.

The defect this closes is silence: a project could be fully ingested and
produce no Candidates at all, with nothing in the output saying so. So
these tests are mostly about what the *report* distinguishes, not about
extraction quality — quality is measured by a real run, not a unit test.
"""

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_matrix import ExtractionFailed
from corridor.extract_sheet import PROMPT_VERSION as SHEET_PROMPT_VERSION
from corridor.extraction_runs import (
    active_run_for_document,
    extractor_configuration,
    record_extraction_run,
)
from corridor.extraction_errors import NoMatrixFound
from corridor.extract_project import (
    Outcome,
    UnextractableDocument,
    UnknownDocument,
    extract_project,
    main,
    render,
)
from corridor.extract_matrix import SequencingSemanticsDetected
from corridor.extractor_lineage import injected_extractor_config
from corridor.models import (
    Candidate,
    Document,
    DocumentQuarantine,
    ExtractionRun,
    Project,
)
from corridor.pipeline import ExtractionRoute
from corridor.row_accounting import (
    AccountedCandidates,
    RowAccounting,
    RowAccountingFailure,
)

PROMPT_VERSION = "test_v1"
ACCOUNTED_PROMPT_VERSION = "matrix_tiered_v4"


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
    p = Project(slug="xp-test", name="Extract Project Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def add_matrix(session, project, name, sha):
    doc = Document(
        project_id=project.id,
        sha256=sha,
        filename=name,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    return doc


def candidate(document, *, verified=True, state="pending"):
    return Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={"kind": "dependency", "fields": {"utility_id": "E92"}},
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        citations_verified=verified,
        state=state,
    )


def extractor(*, prompt_version=PROMPT_VERSION, **by_filename):
    """An extractor scripted per document: a list of Candidates, or an error."""

    def extract(session, document):
        result = by_filename[document.filename]
        if isinstance(result, Exception):
            raise result
        candidates = [candidate(document, verified=v) for v in result]
        for c in candidates:
            c.prompt_version = prompt_version
            session.add(c)
        session.flush()
        return candidates

    return extract


def accounted_route(*, fail: bool = False):
    def select_route(document):
        def extract(session, target):
            accounting = RowAccounting(
                reader_version=ACCOUNTED_PROMPT_VERSION,
                reader_path="page_geometry_and_transcription",
            )
            accounting.detect("structure:1:table:0:row:1", page=1, row_number=1)
            if fail:
                accounting.detect(
                    "structure:1:table:0:row:2",
                    page=1,
                    row_number=2,
                )
                accounting.account(
                    "structure:1:table:0:row:1",
                    disposition="blank",
                    reason="blank_source_row",
                )
                accounting.finish([])
            made = candidate(target)
            made.prompt_version = ACCOUNTED_PROMPT_VERSION
            session.add(made)
            session.flush([made])
            accounting.account(
                "structure:1:table:0:row:1",
                disposition="extracted",
                reason="candidate_recorded",
            )
            return accounting.finish([made])

        return ExtractionRoute(
            effective_prompt_version=ACCOUNTED_PROMPT_VERSION,
            schema_version="matrix_candidate_shape_v1",
            extract=extract,
            model=None,
            extractor_config=injected_extractor_config(
                extractor="accounted-matrix-fixture",
                prompt_version=ACCOUNTED_PROMPT_VERSION,
                model=None,
                schema_version="matrix_candidate_shape_v1",
                prompt_bytes=b"accounted matrix fixture",
                schema={"type": "object"},
                postprocessor_bytes=b"accounted matrix fixture rules",
                request_controls={"strict": True},
            ),
        )

    return select_route


def route_selector(**by_filename):
    """An injected production-like route seam: version first, then extractor."""

    def select_route(document):
        schema_version = None
        configured_model = None
        item = by_filename[document.filename]
        if len(item) == 2:
            prompt_version, result = item
        elif len(item) == 3:
            prompt_version, schema_version, result = item
        elif len(item) == 4:
            prompt_version, schema_version, configured_model, result = item
        else:
            raise AssertionError("route selector test fixture is invalid")

        def extract(session, target):
            if isinstance(result, Exception):
                raise result
            candidates = [candidate(target, verified=v) for v in result]
            for c in candidates:
                c.prompt_version = prompt_version
                session.add(c)
            session.flush()
            if prompt_version in {"sheet_native_v2", "matrix_tiered_v4"}:
                accounting = RowAccounting(
                    reader_version=prompt_version,
                    reader_path=(
                        "spreadsheet_cells"
                        if prompt_version.startswith("sheet_")
                        else "page_geometry_and_transcription"
                    ),
                )
                for row_number, _candidate in enumerate(candidates, start=1):
                    row_id = f"fixture:1:row:{row_number}"
                    accounting.detect(
                        row_id,
                        page=1,
                        row_number=row_number,
                    )
                    accounting.account(
                        row_id,
                        disposition="extracted",
                        reason="candidate_recorded",
                    )
                return accounting.finish(candidates)
            return candidates

        return ExtractionRoute(
            effective_prompt_version=prompt_version,
            schema_version=schema_version or prompt_version,
            extract=extract,
            model=configured_model,
            extractor_config=injected_extractor_config(
                extractor="matrix-route-fixture",
                prompt_version=prompt_version,
                model=configured_model,
                schema_version=schema_version or prompt_version,
                prompt_bytes=b"matrix route fixture",
                schema={"type": "object"},
                postprocessor_bytes=b"matrix route fixture rules",
                request_controls={"strict": True},
            ),
        )

    return select_route


def test_it_reports_rows_and_unverified_citations_per_document(session, project):
    a = add_matrix(session, project, "a.pdf", "a" * 64)
    b = add_matrix(session, project, "b.pdf", "b" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True, True, False], "b.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {o.filename: o for o in outcomes}
    assert by_name["a.pdf"].status == "extracted"
    assert (by_name["a.pdf"].rows, by_name["a.pdf"].unverified) == (3, 1)
    assert (by_name["b.pdf"].rows, by_name["b.pdf"].unverified) == (1, 0)


def test_project_extraction_includes_registered_plan_spreadsheets(
    session, project
):
    table = add_matrix(session, project, "sue-table.xlsx", "7" * 64)
    table.doc_type = "plan"
    session.flush([table])

    [outcome] = extract_project(
        session,
        project,
        select_route=route_selector(
            **{"sue-table.xlsx": (SHEET_PROMPT_VERSION, [])}
        ),
        commit=False,
    )

    assert outcome.status == "extracted"
    assert outcome.effective_prompt_version == SHEET_PROMPT_VERSION


def test_unreadable_is_a_different_outcome_from_no_rows(session, project):
    """The distinction the whole ticket exists for.

    A document with no conflicts is a correct empty answer. A document the
    extractor cannot read is an unhandled layout. Reporting both as "0
    rows" is how a broken pipeline passes for a quiet one.
    """
    empty = add_matrix(session, project, "empty.pdf", "c" * 64)
    broken = add_matrix(session, project, "broken.pdf", "d" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{
            "empty.pdf": [],
            "broken.pdf": NoMatrixFound("no table with utility-matrix headers"),
        }),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {o.filename: o for o in outcomes}
    assert by_name["empty.pdf"].status == "extracted"
    assert by_name["empty.pdf"].rows == 0
    assert by_name["broken.pdf"].status == "unreadable"
    assert "utility-matrix headers" in by_name["broken.pdf"].detail
    assert [run.outcome for run in _runs(session, empty)] == ["completed"]
    assert [run.outcome for run in _runs(session, broken)] == ["no_matrix"]


def test_terminal_outcomes_distinguish_extracted_failed_and_unreadable(session, project):
    empty = add_matrix(session, project, "empty.pdf", "e" * 64)
    failed = add_matrix(session, project, "failed.pdf", "f" * 64)
    broken = add_matrix(session, project, "broken.pdf", "g" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(
            **{
                "empty.pdf": [],
                "failed.pdf": ExtractionFailed("503 upstream"),
                "broken.pdf": NoMatrixFound("no table with utility-matrix headers"),
            }
        ),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {o.filename: o for o in outcomes}
    assert by_name["empty.pdf"].status == "extracted"
    assert by_name["empty.pdf"].rows == 0
    assert by_name["failed.pdf"].status == "failed"
    assert "503 upstream" in by_name["failed.pdf"].detail
    assert by_name["broken.pdf"].status == "unreadable"
    assert "utility-matrix headers" in by_name["broken.pdf"].detail
    assert [run.outcome for run in _runs(session, empty)] == ["completed"]
    assert [run.outcome for run in _runs(session, failed)] == ["failed"]
    assert [run.outcome for run in _runs(session, broken)] == ["no_matrix"]

    rendered = render(project, PROMPT_VERSION, outcomes)
    assert "FAILED" in rendered
    assert "UNREADABLE" in rendered


def test_terminal_outcomes_name_the_exact_extraction_run_receipt(session, project):
    completed = add_matrix(session, project, "completed.pdf", "1" * 64)
    failed = add_matrix(session, project, "failed.pdf", "2" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(
            **{
                "completed.pdf": [True],
                "failed.pdf": ExtractionFailed("controlled failure"),
            }
        ),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    by_name = {outcome.filename: outcome for outcome in outcomes}
    assert by_name["completed.pdf"].extraction_run_id == _runs(
        session, completed
    )[0].id
    assert by_name["failed.pdf"].extraction_run_id == _runs(session, failed)[0].id


def test_completed_accounted_reader_retains_every_row_disposition(
    session, project
):
    document = add_matrix(session, project, "accounted.pdf", "3" * 64)

    [outcome] = extract_project(
        session,
        project,
        select_route=accounted_route(),
        commit=False,
    )

    assert outcome.status == "extracted"
    [run] = _runs(session, document)
    assert run.prompt_version == ACCOUNTED_PROMPT_VERSION
    assert run.row_accounting_json["detected_row_count"] == 1
    assert run.row_accounting_json["rows"][0]["reason"] == "candidate_recorded"


def test_unaccounted_reader_failure_retains_discrepancy_and_no_candidates(
    session, project
):
    document = add_matrix(session, project, "dropped.pdf", "4" * 64)

    [outcome] = extract_project(
        session,
        project,
        select_route=accounted_route(fail=True),
        commit=False,
    )

    assert outcome.status == "failed"
    assert "unaccounted" in outcome.detail
    [run] = _runs(session, document)
    assert run.outcome == "failed"
    assert run.candidate_count == 0
    assert run.row_accounting_json["detected_row_count"] == 2
    assert run.row_accounting_json["accounted_row_count"] == 1
    assert run.row_accounting_json["unaccounted_rows"] == [
        "structure:1:table:0:row:2"
    ]
    assert _count(session, document) == 0


def test_bumped_accounted_reader_never_pools_with_historical_reading(
    session, project
):
    document = add_matrix(session, project, "versioned.pdf", "5" * 64)
    record_extraction_run(
        session,
        document,
        prompt_version="matrix_tiered_v3",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )

    [outcome] = extract_project(
        session,
        project,
        select_route=accounted_route(),
        commit=False,
    )

    assert outcome.status == "extracted"
    assert [run.prompt_version for run in _runs(session, document)] == [
        "matrix_tiered_v3",
        "matrix_tiered_v4",
    ]


def test_bumped_sheet_reader_never_pools_with_historical_reading(
    session, project
):
    document = add_matrix(session, project, "versioned.xlsx", "6" * 64)
    record_extraction_run(
        session,
        document,
        prompt_version="sheet_native_v1",
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )

    [outcome] = extract_project(
        session,
        project,
        select_route=route_selector(
            **{"versioned.xlsx": (SHEET_PROMPT_VERSION, [])}
        ),
        commit=False,
    )

    assert outcome.status == "extracted"
    assert [run.prompt_version for run in _runs(session, document)] == [
        "sheet_native_v1",
        "sheet_native_v2",
    ]


def test_a_second_run_does_not_double_the_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    extract = extractor(**{"a.pdf": [True, True]})

    extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )
    outcomes = extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )

    assert outcomes[0].status == "skipped"
    assert _count(session, doc) == 2


def test_first_successful_run_becomes_active_but_redo_does_not_retarget(
    session, project
):
    doc = add_matrix(session, project, "a.pdf", "z" * 64)

    first = extract_project(
        session,
        project,
        extract=extractor(prompt_version="v1", **{"a.pdf": [True]}),
        prompt_version="v1",
        commit=False,
    )
    second = extract_project(
        session,
        project,
        extract=extractor(prompt_version="v2", **{"a.pdf": [True, True]}),
        prompt_version="v2",
        redo=True,
        commit=False,
    )

    assert first[0].status == "extracted"
    assert second[0].status == "extracted"
    assert [run.prompt_version for run in _runs(session, doc)] == ["v1", "v2"]
    assert active_run_for_document(session, doc.id) is None


def test_a_zero_row_document_is_skipped_after_a_completed_empty_attempt(
    session, project
):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    extract = extractor(**{"a.pdf": []})

    first = extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )
    second = extract_project(
        session, project, extract=extract, prompt_version=PROMPT_VERSION, commit=False
    )

    assert first[0].status == "extracted"
    assert first[0].rows == 0
    assert second[0].status == "skipped"
    assert [
        (run.prompt_version, run.candidate_count, run.page_errors)
        for run in _runs(session, doc)
    ] == [(PROMPT_VERSION, 0, 0)]


def test_a_model_backed_zero_row_run_keeps_the_configured_model(session, project):
    doc = add_matrix(session, project, "model-empty.pdf", "0" * 64)

    def select_route(document):
        assert document.id == doc.id
        return ExtractionRoute(
            effective_prompt_version=PROMPT_VERSION,
            schema_version="schema-zero-row-v1",
            extract=lambda session, target: [],
            model="gpt-zero-row",
            extractor_config=injected_extractor_config(
                extractor="zero-row-fixture",
                prompt_version=PROMPT_VERSION,
                model="gpt-zero-row",
                schema_version="schema-zero-row-v1",
                prompt_bytes=b"zero row fixture",
                schema={"type": "object"},
                postprocessor_bytes=b"zero row fixture rules",
                request_controls={"strict": True},
            ),
        )

    outcomes = extract_project(
        session,
        project,
        select_route=select_route,
        prompt_version="ignored-by-route",
        commit=False,
    )

    assert outcomes[0].rows == 0
    [run] = _runs(session, doc)
    assert run.model == "gpt-zero-row"


def test_a_zero_row_spreadsheet_is_recorded_and_skipped_at_its_effective_prompt_version(
    session, project
):
    doc = add_matrix(session, project, "matrix.xlsx", "a" * 64)
    select_route = route_selector(
        **{"matrix.xlsx": (SHEET_PROMPT_VERSION, [])}
    )

    first = extract_project(
        session,
        project,
        select_route=select_route,
        prompt_version=PROMPT_VERSION,
        commit=False,
    )
    second = extract_project(
        session,
        project,
        select_route=select_route,
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert first[0].status == "extracted"
    assert first[0].rows == 0
    assert first[0].effective_prompt_version == SHEET_PROMPT_VERSION
    assert second[0].status == "skipped"
    assert second[0].effective_prompt_version == SHEET_PROMPT_VERSION
    assert second[0].detail == f"already extracted at {SHEET_PROMPT_VERSION}"
    assert [
        (run.prompt_version, run.candidate_count, run.page_errors)
        for run in _runs(session, doc)
    ] == [(SHEET_PROMPT_VERSION, 0, 0)]
    assert f"[{SHEET_PROMPT_VERSION}]" in render(project, PROMPT_VERSION, second)


def test_a_candidate_without_a_run_does_not_make_a_document_skip(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(candidate(doc))
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert outcomes[0].status == "extracted"
    assert len(_runs(session, doc)) == 1


def test_redo_appends_pending_candidates(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)

    extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True, True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )
    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        redo=True,
        commit=False,
    )

    assert outcomes[0].status == "extracted"
    assert _count(session, doc) == 3
    assert len(_runs(session, doc)) == 2


def test_redo_never_disturbs_an_accepted_candidate(session, project):
    """Accepted Candidates back Ledger records, and Assertions cite them."""
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(candidate(doc, state="accepted"))
    session.add(candidate(doc, state="pending"))
    session.flush()

    extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        redo=True,
        commit=False,
    )

    states = sorted(
        c.state
        for c in session.scalars(
            select(Candidate).where(Candidate.source_document_id == doc.id)
        )
    )
    assert states == ["accepted", "pending", "pending"]


def test_a_document_ingest_could_not_parse_is_reported_not_skipped(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    doc.parse_status = "failed"
    session.flush()

    outcomes = extract_project(
        session, project, extract=extractor(), prompt_version=PROMPT_VERSION, commit=False
    )

    assert outcomes[0].status == "unreadable"
    assert "failed" in outcomes[0].detail
    assert [run.outcome for run in _runs(session, doc)] == ["unreadable"]


def test_non_spreadsheet_non_matrix_documents_are_ineligible(session, project):
    add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(
        Document(
            project_id=project.id,
            sha256="f" * 64,
            filename="agreement.pdf",
            doc_type="agreement",
            parse_status="parsed",
            pages=1,
        )
    )
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert [o.filename for o in outcomes] == ["a.pdf"]


def _count(session, document) -> int:
    return len(
        session.scalars(
            select(Candidate).where(Candidate.source_document_id == document.id)
        ).all()
    )


def _runs(session, document) -> list[ExtractionRun]:
    return session.scalars(
        select(ExtractionRun)
        .where(ExtractionRun.document_id == document.id)
        .order_by(ExtractionRun.id)
    ).all()


# --------------------------- retiring a superseded reading (#105)


def test_a_prompt_bump_appends_prior_pending_candidates(
    session, project
):
    """The trap this repo has cleared by hand twice.

    Nothing pruned superseded pending Candidates, because the clear was
    scoped to the version being run: bump the version and no document
    counts as already extracted, so the clear never fired and the old
    version's rows stayed in the queue beside the new. It doubled the
    review queue on `txdot_ucm`, and again when #97 merged, where 4,702
    rows had to be deleted by hand.

    Re-reading a document supersedes every earlier *pending* reading of it,
    whichever prompt produced them. A reviewer has one queue, not one per
    prompt version.
    """
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    superseded = candidate(doc)  # written at PROMPT_VERSION
    session.add(superseded)
    session.flush()
    superseded_id = superseded.id

    outcomes = extract_project(
        session,
        project,
        extract=extractor(prompt_version="test_v2", **{"a.pdf": [True]}),
        prompt_version="test_v2",
        commit=False,
    )

    assert outcomes[0].status == "extracted"
    # Append the new run; preserve prior pending rows.
    remaining = session.scalars(
        select(Candidate).where(Candidate.source_document_id == doc.id)
    ).all()
    assert len(remaining) == 2
    assert any(c.id == superseded_id for c in remaining)


def test_a_prompt_bump_never_disturbs_an_adjudicated_candidate(session, project):
    """Accepted and rejected are decisions, and a re-read does not undo one.

    Same rule as `redo`, and it has to survive the widening: an accepted
    Candidate backs a Ledger record its Assertions cite, and a rejected one
    is a human's answer that an extractor re-running has no business
    reversing.
    """
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    for state in ("accepted", "rejected", "pending"):
        session.add(candidate(doc, state=state))
    session.flush()
    superseded = {
        c.id
        for c in session.scalars(
            select(Candidate).where(Candidate.source_document_id == doc.id)
        )
        if c.state == "pending"
    }

    extract_project(
        session,
        project,
        extract=extractor(prompt_version="test_v2", **{"a.pdf": [True]}),
        prompt_version="test_v2",
        commit=False,
    )

    surviving = session.scalars(
        select(Candidate).where(Candidate.source_document_id == doc.id)
    ).all()
    assert sorted(c.state for c in surviving) == [
        "accepted",
        "pending",
        "pending",
        "rejected",
    ]
    # Keep one adjudicated history and the prior un-adjudicated reading.
    assert superseded & {c.id for c in surviving}


def test_an_unreadable_document_keeps_the_reading_it_already_had(session, project):
    """Nothing is retired when nothing replaces it.

    A document that fails its parse check produces no Candidates, so
    clearing its queue rows would delete a reading and put nothing in its
    place — leaving the project quieter than before the run rather than
    more current.
    """
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    doc.parse_status = "failed"
    session.add(candidate(doc))
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True]}),
        prompt_version="test_v2",
        commit=False,
    )

    assert outcomes[0].status == "unreadable"
    assert _count(session, doc) == 1


def test_a_document_that_cannot_be_read_keeps_the_reading_it_had(session, project):
    """`NoMatrixFound` must not cost a document its queue rows.

    The savepoint rolls back the Candidates a failed read half-wrote, but a
    clear that ran outside it survives — so the document ends the run with
    its old rows deleted and no new ones, quieter than before rather than
    more current. Retiring a reading and writing its replacement are one
    step or they are a data-loss bug.
    """
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    session.add(candidate(doc))
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": NoMatrixFound("no utility-matrix headers")}),
        prompt_version="test_v2",
        commit=False,
    )

    assert outcomes[0].status == "unreadable"
    assert _count(session, doc) == 1
    assert [
        (run.outcome, run.candidate_count, run.page_errors)
        for run in _runs(session, doc)
    ] == [("no_matrix", 0, 1)]


def test_a_transient_failure_does_not_block_a_clean_sibling(session, project):
    failed = add_matrix(session, project, "a.pdf", "a" * 64)
    clean = add_matrix(session, project, "b.pdf", "b" * 64)
    session.add(candidate(failed))
    session.flush()

    outcomes = extract_project(
        session,
        project,
        select_route=route_selector(
            **{
                "a.pdf": (PROMPT_VERSION, ExtractionFailed("503 upstream")),
                "b.pdf": (PROMPT_VERSION, [True]),
            }
        ),
        prompt_version="ignored_by_route",
        commit=False,
    )

    assert [(o.filename, o.status) for o in outcomes] == [
        ("a.pdf", "failed"),
        ("b.pdf", "extracted"),
    ]
    assert outcomes[0].effective_prompt_version == PROMPT_VERSION
    assert _count(session, failed) == 1
    assert [run.outcome for run in _runs(session, failed)] == ["failed"]
    assert [
        (run.document_id, run.prompt_version, run.candidate_count, run.page_errors)
        for run in _runs(session, clean)
    ] == [(clean.id, PROMPT_VERSION, 1, 0)]
    report = render(project, PROMPT_VERSION, outcomes)
    assert "FAILED" in report
    assert "1 failed, 0 unreadable" in report


def test_a_route_records_schema_version_independently_from_prompt(session, project):
    document = add_matrix(session, project, "a.pdf", "r" * 64)

    extract_project(
        session,
        project,
        select_route=route_selector(
            **{"a.pdf": ("prompt-v3", "candidate-shape-v7", [True])}
        ),
        prompt_version="ignored_by_route",
        commit=False,
    )

    [run] = _runs(session, document)
    assert run.prompt_version == "prompt-v3"
    assert run.schema_version == "candidate-shape-v7"
    # The receipt is stored once by digest and referenced (#605).
    assert run.extractor_config_json is None
    assert extractor_configuration(session, run)["prompt_version"] == "prompt-v3"
    assert run.token_usage_json == {
        "scope": "run",
        "document_ids": [document.id],
        "measurement": "exact",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
    }


def test_a_failed_document_writes_a_receipt_and_retries_on_the_next_run(
    session, project
):
    document = add_matrix(session, project, "a.pdf", "a" * 64)
    superseded = candidate(document)
    session.add(superseded)
    session.flush()

    attempts = iter(
        [
            ExtractionFailed("every page of a.pdf failed: 1 of 2. This says nothing about the document."),
            [True, True],
        ]
    )

    def select_route(doc):
        result = next(attempts)

        def extract(session, target):
            if isinstance(result, Exception):
                raise result
            candidates = [candidate(target, verified=v) for v in result]
            for made in candidates:
                session.add(made)
            session.flush()
            return candidates

        return ExtractionRoute(
            effective_prompt_version=PROMPT_VERSION,
            schema_version="schema-transient-v1",
            extract=extract,
            allow_unsealed_legacy=True,
        )

    first = extract_project(
        session,
        project,
        select_route=select_route,
        prompt_version="ignored_by_route",
        commit=False,
    )
    assert first[0].status == "failed"
    assert [run.outcome for run in _runs(session, document)] == ["failed"]
    assert _count(session, document) == 1

    second = extract_project(
        session,
        project,
        select_route=select_route,
        prompt_version="ignored_by_route",
        commit=False,
    )

    assert _count(session, document) == 3
    surviving = session.scalars(
        select(Candidate).where(Candidate.source_document_id == document.id)
    ).all()
    assert superseded.id in {c.id for c in surviving}
    assert second[0].status == "extracted"
    assert [
        (run.prompt_version, run.outcome, run.candidate_count, run.page_errors)
        for run in _runs(session, document)
    ] == [
        (PROMPT_VERSION, "failed", 0, 1),
        (PROMPT_VERSION, "completed", 2, 0),
    ]


def test_newly_persisted_candidates_always_reference_a_run(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)

    extract_project(
        session,
        project,
        extract=extractor(**{"a.pdf": [True, True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    candidates = session.scalars(
        select(Candidate).where(Candidate.source_document_id == doc.id)
    ).all()
    assert len(candidates) == 2
    assert all(
        c.extraction_run_id is not None for c in candidates
    ), "every newly persisted candidate should reference one run"


def test_an_unexpected_runtime_error_still_propagates(session, project):
    doc = add_matrix(session, project, "a.pdf", "a" * 64)

    with pytest.raises(RuntimeError, match="bug in extractor"):
        extract_project(
            session,
            project,
            select_route=route_selector(
                **{"a.pdf": (PROMPT_VERSION, RuntimeError("bug in extractor"))}
            ),
            prompt_version="ignored_by_route",
            commit=False,
        )

    assert _count(session, doc) == 0
    runs = _runs(session, doc)
    assert [(run.outcome, run.candidate_count) for run in runs] == [("failed", 0)]
    assert runs[0].error_detail == "RuntimeError: bug in extractor"


def test_main_returns_nonzero_when_any_document_failed(
    session, project, monkeypatch, capsys
):
    class StubClient:
        model = "stub-model"

        class _Usage:
            prompt_tokens = 10
            cached_tokens = 0
            completion_tokens = 5
            reasoning_tokens = 0

        usage = _Usage()

        def close(self):
            pass

    class Scoped:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("corridor.db.WorkerSession", lambda: Scoped())
    monkeypatch.setattr("corridor.llm.OpenAIClient", StubClient)
    monkeypatch.setattr(
        "corridor.extract_project.extract_project",
        lambda *args, **kwargs: [
            Outcome(
                document_id=1,
                filename="a.pdf",
                status="failed",
                effective_prompt_version=PROMPT_VERSION,
                detail="503 upstream",
            )
        ],
    )

    assert main([project.slug]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "1 failed, 0 unreadable, 0 skipped" in out


def tiered_extractor(tiers, *, disagreements=0):
    """An extractor that records how it read the document, as a real one does."""

    def extract(session, document):
        document.extraction_tiers = dict(tiers)
        document.header_disagreements = disagreements
        c = candidate(document)
        session.add(c)
        session.flush()
        return [c]

    return extract


def test_how_a_document_was_read_survives_the_run_that_read_it(session, project):
    """`extraction_tiers` was an ad-hoc attribute, read back with a
    `getattr` default. It never reached the database, so nothing outside
    one process could say how a document had been read."""
    doc = add_matrix(session, project, "a.pdf", "a" * 64)

    extract_project(
        session,
        project,
        extract=tiered_extractor({"structure": 3, "transcribe": 1}, disagreements=2),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )
    session.expire(doc)

    assert doc.extraction_tiers == {"structure": 3, "transcribe": 1}
    assert doc.header_disagreements == 2


def test_a_resumed_run_still_reports_the_fallback_share(session, project):
    """The skip is the normal case, and it used to report no tiers at all.

    A report that under-states the fallback on every resumed run is the
    failure this module's own comment names: a fallback nobody counts is
    a fallback nobody notices.
    """
    doc = add_matrix(session, project, "a.pdf", "a" * 64)
    extract_project(
        session,
        project,
        extract=tiered_extractor({"structure": 3, "transcribe": 1}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    # Second run: the document is already extracted at this version.
    outcomes = extract_project(
        session,
        project,
        extract=tiered_extractor({}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    [outcome] = outcomes
    assert outcome.status == "skipped"
    assert outcome.tiers == {"structure": 3, "transcribe": 1}
    assert "25.0% fell back" in render(project, PROMPT_VERSION, outcomes)


def test_the_fallback_share_is_stated_even_when_nothing_fell_back(session, project):
    add_matrix(session, project, "a.pdf", "a" * 64)
    outcomes = extract_project(
        session,
        project,
        extract=tiered_extractor({"structure": 4}),
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    report = render(project, PROMPT_VERSION, outcomes)
    assert "4 read from the text layer, 0 transcribed (0.0% fell back)" in report
    assert "every page agreed" in report


# --- Document-scoped extraction (#168) --------------------------------------


def test_a_named_document_extracts_alone(session, project):
    first = add_matrix(session, project, "feb.pdf", "a" * 64)
    first.registry_id = "ucm-feb"
    second = add_matrix(session, project, "dec.pdf", "b" * 64)
    second.registry_id = "ucm-dec"
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"feb.pdf": [True, True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
        document_registry_id="ucm-feb",
    )

    assert [o.filename for o in outcomes] == ["feb.pdf"]
    assert outcomes[0].status == "extracted"
    runs = session.scalars(
        select(ExtractionRun).where(
            ExtractionRun.document_id.in_([first.id, second.id])
        )
    ).all()
    assert [run.document_id for run in runs] == [first.id]


def test_an_exact_document_content_identity_extracts_alone(session, project):
    first = add_matrix(session, project, "legacy-without-registry-id.pdf", "a" * 64)
    second = add_matrix(session, project, "other.pdf", "b" * 64)

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"legacy-without-registry-id.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
        document_sha256=first.sha256,
    )

    assert [outcome.document_id for outcome in outcomes] == [first.id]
    runs = session.scalars(
        select(ExtractionRun).where(
            ExtractionRun.document_id.in_([first.id, second.id])
        )
    ).all()
    assert [run.document_id for run in runs] == [first.id]


def test_document_selection_refuses_two_competing_identities(session, project):
    document = add_matrix(session, project, "matrix.pdf", "a" * 64)
    document.registry_id = "matrix-registry"

    with pytest.raises(ValueError, match="one Document selector"):
        extract_project(
            session,
            project,
            extract=extractor(),
            commit=False,
            document_registry_id=document.registry_id,
            document_sha256=document.sha256,
        )


def test_naming_an_unknown_document_refuses(session, project):
    add_matrix(session, project, "feb.pdf", "a" * 64)

    with pytest.raises(UnknownDocument, match="no-such-registry-id"):
        extract_project(
            session,
            project,
            extract=extractor(**{"feb.pdf": [True]}),
            commit=False,
            document_registry_id="no-such-registry-id",
        )


def test_naming_an_unextractable_document_refuses(session, project):
    agreement = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="mou.pdf",
        doc_type="agreement",
        parse_status="parsed",
        pages=1,
    )
    agreement.registry_id = "coh-mou"
    session.add(agreement)
    session.flush()

    with pytest.raises(UnextractableDocument, match="agreement"):
        extract_project(
            session,
            project,
            extract=extractor(),
            commit=False,
            document_registry_id="coh-mou",
        )


def test_a_named_document_keeps_the_skip_receipt_semantics(session, project):
    doc = add_matrix(session, project, "feb.pdf", "a" * 64)
    doc.registry_id = "ucm-feb"
    session.flush()

    first = extract_project(
        session,
        project,
        extract=extractor(**{"feb.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
        document_registry_id="ucm-feb",
    )
    assert first[0].status == "extracted"

    second = extract_project(
        session,
        project,
        extract=extractor(**{"feb.pdf": [True]}),
        prompt_version=PROMPT_VERSION,
        commit=False,
        document_registry_id="ucm-feb",
    )
    assert second[0].status == "skipped"
    runs = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == doc.id)
    ).all()
    assert len(runs) == 1


def test_a_named_document_records_failure_receipts_like_the_sweep(
    session, project
):
    doc = add_matrix(session, project, "feb.pdf", "a" * 64)
    doc.registry_id = "ucm-feb"
    session.flush()

    outcomes = extract_project(
        session,
        project,
        extract=extractor(**{"feb.pdf": ExtractionFailed("model unavailable")}),
        prompt_version=PROMPT_VERSION,
        commit=False,
        document_registry_id="ucm-feb",
    )

    assert [o.status for o in outcomes] == ["failed"]
    run = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == doc.id)
    ).one()
    assert run.outcome == "failed"
    assert "model unavailable" in run.error_detail


# --- The sequencing quarantine boundary (#171) ------------------------------


def test_detected_sequencing_semantics_quarantine_the_document_whole(
    session, project
):
    doc = add_matrix(session, project, "uws-as-matrix.pdf", "d" * 64)

    def refuse(inner_session, document):
        raise SequencingSemanticsDetected(
            "column 'Dependent Activity' asserts work sequencing"
        )

    outcomes = extract_project(
        session,
        project,
        extract=refuse,
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert [o.status for o in outcomes] == ["quarantined"]
    run = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == doc.id)
    ).one()
    assert run.outcome == "quarantined"
    assert "Dependent Activity" in run.error_detail
    assert (
        session.scalars(
            select(Candidate).where(Candidate.source_document_id == doc.id)
        ).all()
        == []
    )
    quarantine = session.get(DocumentQuarantine, doc.id)
    assert quarantine is not None
    assert "Dependent Activity" in quarantine.reason
    assert "QUARANTINED" in render(project, PROMPT_VERSION, outcomes)
