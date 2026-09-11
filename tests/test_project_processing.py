"""The public project-processing pass over real PostgreSQL.

Extraction and Record Inclusion are exercised together through their committed
transactions, with the model boundary and clock controlled. The scenarios are
the hostile ones #342 requires: sole and mixed inputs, a successful zero-row
read, a failed document beside a clean sibling, a no-work pass, a duplicate
trigger, competing reconciliation, and — the load-bearing case — a completed
extraction whose Record Inclusion is finished only after a restart, with no
second model read and no duplicate project fact.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import SessionTransaction

from corridor.extract_project import extract_project
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    Document,
    DocumentQuarantine,
    ExternalOrg,
    ExtractionRun,
    PolicyRun,
    Project,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.project_processing import (
    ProcessingScopeRefused,
    process_project,
    summarize_pass,
)
from corridor.admission import reconcile_record_inclusion
from corridor.record_inclusion import record_inclusion_pending
from clock_support import ControlledClock

PIPELINE = "Tejas Pipeline Co"
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"


def _conflict(document_id, project_id, uid):
    fields = {
        "utility_id": uid,
        "external_org": PIPELINE,
        "utility_type": "Petroleum and Gaseous Materials",
        "station_from": "1102+20",
        "station_to": "1102+80",
    }
    quote = " | ".join(fields.values())
    return Candidate(
        project_id=project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": fields,
            "citations": [
                {
                    "document_id": document_id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "dedupe_hint": quote,
            "text_source": "text_layer",
        },
        source_document_id=document_id,
        source_pages=[1],
        confidence=0.99,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        citations_verified=True,
    )


class ScriptedRoute:
    """A per-document reader whose script and invocation count the test owns."""

    def __init__(self, script: dict[str, object]):
        # filename -> list[str] of conflict uids, or an Exception to raise.
        self._script = script
        self.extract_calls: dict[str, int] = {}

    def __call__(self, document: Document) -> ExtractionRoute:
        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=self._extract,
            model=MODEL,
            allow_unsealed_legacy=True,
        )

    def _extract(self, session, document):
        self.extract_calls[document.filename] = (
            self.extract_calls.get(document.filename, 0) + 1
        )
        planned = self._script[document.filename]
        if isinstance(planned, Exception):
            raise planned
        candidates = [
            _conflict(document.id, document.project_id, uid) for uid in planned
        ]
        for candidate in candidates:
            session.add(candidate)
        session.flush()
        return candidates


def _project(factory, **overrides) -> int:
    with factory() as setup:
        project = Project(
            slug=f"proc-{uuid4().hex}",
            name="Processing",
            is_synthetic=True,
            project_side_parties=["LJA Engineering"],
            **overrides,
        )
        setup.add(project)
        setup.flush([project])
        # Record Inclusion refuses to mint an External Organization (#345), so
        # the owner every conflict candidate cites must already be registered.
        setup.add(ExternalOrg(name=PIPELINE, aliases=[]))
        project_id = project.id
        setup.commit()
    return project_id


def _matrix(
    factory, project_id, filename, *, parse_status="parsed", doc_date=None,
    page=True,
) -> int:
    with factory() as setup:
        document = Document(
            project_id=project_id,
            sha256=hashlib.sha256(f"{project_id}:{filename}".encode()).hexdigest(),
            filename=filename,
            doc_type="matrix",
            parse_status=parse_status,
            pages=1,
            doc_date=doc_date,
        )
        setup.add(document)
        setup.flush([document])
        # A document the read act has not reached yet has no pages; `page=False`
        # is how a test says the read is the thing under test.
        if page:
            setup.add(DocPage(document_id=document.id, page_no=1, text="rows"))
        document_id = document.id
        setup.commit()
    return document_id


def _dependencies(session, project_id) -> set[str]:
    return {
        d.source_ref
        for d in session.scalars(
            select(Dependency).where(Dependency.project_id == project_id)
        )
    }


def _policy_run_count(session, project_id) -> int:
    return session.scalar(
        select(func.count()).select_from(PolicyRun).where(
            PolicyRun.project_id == project_id
        )
    )


def _completed_runs(session, project_id) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ExtractionRun)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, ExtractionRun.outcome == "completed")
    )


NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def test_sole_matrix_processes_and_lands_its_conflicts(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    result = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )

    assert result.extracted == 1
    assert result.eligible_document_count == 1
    assert result.reconciled is True
    assert result.admitted == 2
    assert result.processing_failures == []
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}


def test_a_failed_document_does_not_stop_a_clean_sibling(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "good.pdf", doc_date=date(2025, 1, 1))
    _matrix(factory, project_id, "bad.pdf", doc_date=date(2025, 1, 2))
    route = ScriptedRoute(
        {"good.pdf": ["PL1"], "bad.pdf": RuntimeError("reader exploded")}
    )

    result = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )

    assert result.extracted == 1
    assert len(result.processing_failures) == 1
    assert "bad.pdf" in result.processing_failures[0] or "RuntimeError" in (
        result.processing_failures[0]
    )
    # The clean sibling still reached the record.
    assert result.admitted == 1
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_successful_zero_row_read_is_completed_and_reconciled(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "empty.pdf")
    route = ScriptedRoute({"empty.pdf": []})

    first = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )
    assert first.extracted == 1
    assert first.admitted == 0
    assert first.reconciled is True  # a completed zero-row read still reconciles
    with factory() as verify:
        completed = _completed_runs(verify, project_id)
        assert completed == 1
        baseline_runs = _policy_run_count(verify, project_id)

    # A second pass skips the completed zero-row document, reconciles nothing,
    # and appends no new Policy Runs.
    second = process_project(
        factory,
        project_id=project_id,
        select_route=route,
        clock=ControlledClock(NOW),
    )
    assert second.skipped == 1
    assert second.extracted == 0
    assert second.reconciled is False
    with factory() as verify:
        assert _completed_runs(verify, project_id) == 1
        assert _policy_run_count(verify, project_id) == baseline_runs
        assert route.extract_calls["empty.pdf"] == 1  # no second model read


def test_no_work_pass_on_a_clean_project_appends_no_policy_runs(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1"]})
    process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    with factory() as verify:
        baseline = _policy_run_count(verify, project_id)

    for _ in range(3):
        result = process_project(
            factory,
            project_id=project_id,
            select_route=route,
            clock=ControlledClock(NOW),
        )
        assert result.reconciled is False
    with factory() as verify:
        assert _policy_run_count(verify, project_id) == baseline
        assert _dependencies(verify, project_id) == {"PL1"}


def test_a_duplicate_trigger_adds_no_second_project_fact(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    first = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    second = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )

    assert first.admitted == 2
    assert second.admitted == 0
    assert second.skipped == 1
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}
        assert _completed_runs(verify, project_id) == 1


def test_restart_after_extraction_finishes_the_load_without_a_second_read(
    runtime_database,
):
    """The load-bearing invariant: a crash after extraction commits but before
    Record Inclusion finishes must, on restart, reconcile the load without a
    second completed model read and without a duplicate project fact."""

    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1", "PL2"]})

    # Simulate the crash: run only extraction to a committed completion. The
    # producer coupling dirties the watermark; the load never ran.
    with factory() as extracting:
        project = extracting.get(Project, project_id)
        extract_project(
            extracting,
            project,
            select_route=route,
            commit=True,
        )
    with factory() as verify:
        assert record_inclusion_pending(verify, project_id) is True
        assert _dependencies(verify, project_id) == set()
        assert _completed_runs(verify, project_id) == 1
    assert route.extract_calls["ucm.pdf"] == 1

    # Restart: the full pass skips the completed extraction (no second read) and
    # finishes the pending load.
    restart = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    assert restart.skipped == 1
    assert restart.extracted == 0
    assert restart.reconciled is True
    assert restart.admitted == 2
    assert route.extract_calls["ucm.pdf"] == 1  # still exactly one model read
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}
        assert _completed_runs(verify, project_id) == 1
        runs_after_restart = _policy_run_count(verify, project_id)

    # And a further pass is an idle no-op.
    idle = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )
    assert idle.reconciled is False
    with factory() as verify:
        assert _policy_run_count(verify, project_id) == runs_after_restart
        assert _dependencies(verify, project_id) == {"PL1", "PL2"}


def test_competing_reconciliation_loads_once_with_no_duplicate_fact(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "ucm.pdf")
    route = ScriptedRoute({"ucm.pdf": ["PL1"]})
    with factory() as extracting:
        project = extracting.get(Project, project_id)
        extract_project(extracting, project, select_route=route, commit=True)

    ready = Barrier(2)

    def reconcile(_index):
        ready.wait(timeout=5)
        with factory() as session:
            with session.begin():
                result = reconcile_record_inclusion(session, project_id)
            return result.did_load

    with ThreadPoolExecutor(max_workers=2) as pool:
        loaded = list(pool.map(reconcile, range(2)))

    assert sorted(loaded) == [False, True]  # exactly one load; the other a no-op
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_held_superseded_and_unparsed_documents_are_excluded_before_model_work(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "good.pdf", doc_date=date(2025, 1, 1))
    quarantined = _matrix(factory, project_id, "seq.pdf", doc_date=date(2025, 1, 2))
    _matrix(factory, project_id, "unreadable.pdf", parse_status="failed",
            doc_date=date(2025, 1, 3))
    with factory() as hold:
        hold.add(DocumentQuarantine(document_id=quarantined, reason="sequencing"))
        hold.commit()

    route = ScriptedRoute(
        {
            "good.pdf": ["PL1"],
            # These must never be read; a call would raise KeyError here.
        }
    )
    result = process_project(
        factory, project_id=project_id, select_route=route, clock=ControlledClock(NOW)
    )

    assert result.eligible_document_count == 1
    assert result.extracted == 1
    assert result.excluded["held_quarantined"] == 1
    assert result.excluded["failed_parse"] == 1
    assert result.held_out == 2
    assert set(route.extract_calls) == {"good.pdf"}
    # Held-out documents are reported but are a steady state, not a failure of
    # this pass, so the pass is still healthy.
    receipt = summarize_pass(
        result, configuration_version="v", observed_at=NOW
    )
    assert receipt["health"] == "healthy"
    assert receipt["held_out"] == 2
    with factory() as verify:
        assert _dependencies(verify, project_id) == {"PL1"}


def test_a_document_the_read_act_could_not_reach_is_counted_apart_from_a_failure(
    runtime_database, monkeypatch
):
    """`awaiting_parse` is not `failed_parse`, and the receipt must not blur them (#893).

    A read that raises leaves the document exactly as it was -- `pending`, and
    selected by the next pass. Counting it as a failed parse would hand it to
    the bounded attributable re-parse, which refuses a document whose parse
    never ran; counting it as held-out would report a source that needs a
    retry as a steady state. It is neither: it is this pass's own failure, and
    it is still waiting.
    """

    import corridor.project_processing as project_processing

    factory = runtime_database.session_factory
    project_id = _project(factory)
    _matrix(factory, project_id, "good.pdf", doc_date=date(2025, 1, 1))
    waiting = _matrix(
        factory, project_id, "waiting.pdf", parse_status="pending",
        doc_date=date(2025, 1, 2),
    )

    def killed(session, *, document, images_dir):
        raise RuntimeError("worker killed mid-read")

    monkeypatch.setattr(project_processing, "parse_registered_document", killed)
    result = process_project(
        factory,
        project_id=project_id,
        select_route=ScriptedRoute({"good.pdf": ["PL1"]}),
        clock=ControlledClock(NOW),
    )

    assert result.parsed_document_count == 0
    assert result.excluded["awaiting_parse"] == 1
    assert result.excluded["failed_parse"] == 0
    assert any("waiting" in failure or "RuntimeError" in failure
               for failure in result.processing_failures)
    with factory() as verify:
        assert verify.get(Document, waiting).parse_status == "pending"
    receipt = summarize_pass(result, configuration_version="v", observed_at=NOW)
    assert receipt["health"] == "processing_attention_required"
    assert receipt["parsed"] == 0


def test_two_committed_workers_do_not_both_read_one_landed_document(
    runtime_database, monkeypatch
):
    """Two live passes meeting on one pending document, in real transactions.

    Production scheduling does not keep them apart on its own. `claim_due_work`
    locks its candidates `for update skip locked` and holds project processing
    to one live claim, but an occurrence whose lease expired is deliberately
    claimable again -- that is lease recovery -- and the worker whose lease ran
    out is not stopped, notified, or fenced: `process_project` never re-checks
    its claim token. So two passes can be inside the read act at once, and
    re-reading `pending` does not settle it, because neither has committed when
    the other looks.

    This is the shape that proves it, and a repeated sequential pass could not:
    worker A is held *inside* its own uncommitted read while worker B runs a
    whole pass over the same project. Without the per-document claim B sees
    `pending`, opens the same bytes, and the two race to write the same pages.
    """

    import corridor.project_processing as project_processing

    factory = runtime_database.session_factory
    project_id = _project(factory)
    landed = _matrix(
        factory, project_id, "landed.pdf", parse_status="pending",
        doc_date=date(2025, 1, 1), page=False,
    )

    inside = Event()
    release = Event()
    readers: list[str] = []

    def read(session, *, document, images_dir):
        first = not readers
        readers.append(document.filename)
        if first:
            inside.set()
            release.wait(timeout=10)
        document.parse_status = "parsed"
        document.pages = 1
        session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
        session.flush()
        return True

    monkeypatch.setattr(project_processing, "parse_registered_document", read)
    route = ScriptedRoute({"landed.pdf": ["PL1"]})

    def pass_over_the_project():
        return process_project(
            factory,
            project_id=project_id,
            select_route=route,
            clock=ControlledClock(NOW),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(pass_over_the_project)
        assert inside.wait(timeout=10), "worker A never reached the read"
        result_b = pass_over_the_project()
        release.set()
        result_a = worker_a.result(timeout=30)

    assert readers == ["landed.pdf"], (
        "the second worker opened bytes the first was already reading"
    )
    # B leaves the document exactly as it found it, and says so honestly: this
    # is not B's failure, and it is not a held-out steady state either.
    assert result_b.parsed_document_count == 0
    assert result_b.processing_failures == []
    assert result_b.excluded["awaiting_parse"] == 1
    assert result_b.eligible_document_count == 0
    # A's read is the one that counts, and there is one of everything after it.
    assert result_a.parsed_document_count == 1
    assert result_a.extracted == 1
    with factory() as verify:
        assert verify.get(Document, landed).parse_status == "parsed"
        assert verify.scalar(
            select(func.count()).select_from(DocPage).where(
                DocPage.document_id == landed
            )
        ) == 1
        assert _completed_runs(verify, project_id) == 1
        assert _dependencies(verify, project_id) == {"PL1"}


def test_a_read_whose_commit_fails_is_not_counted_as_a_parse(
    runtime_database, monkeypatch
):
    """The receipt counts committed reads, so the count is taken after the commit.

    A parse that succeeded and then failed to commit rolled its pages, its
    segments and its status flip back together. Incrementing inside the
    transaction leaves the pass reporting a read this database does not hold --
    and the document still `pending`, so the very next pass reads it again and
    the receipt has counted one source twice.
    """

    import corridor.project_processing as project_processing

    factory = runtime_database.session_factory
    project_id = _project(factory)
    landed = _matrix(
        factory, project_id, "landed.pdf", parse_status="pending",
        doc_date=date(2025, 1, 1), page=False,
    )

    losing: dict[str, object] = {}
    real_commit = SessionTransaction.commit

    def commit(self, *args, **kwargs):
        if self.session is losing.get("session"):
            raise RuntimeError("lost the commit")
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SessionTransaction, "commit", commit)

    def read_then_lose_the_commit(session, *, document, images_dir):
        document.parse_status = "parsed"
        document.pages = 1
        session.add(DocPage(document_id=document.id, page_no=1, text="rows"))
        session.flush()
        # The read itself succeeded; the transaction carrying it does not land.
        losing["session"] = session
        return True

    monkeypatch.setattr(
        project_processing, "parse_registered_document", read_then_lose_the_commit
    )
    result = process_project(
        factory,
        project_id=project_id,
        select_route=ScriptedRoute({}),
        clock=ControlledClock(NOW),
    )

    assert result.parsed_document_count == 0, (
        "a parse that rolled back must not be counted"
    )
    assert any("lost the commit" in failure for failure in result.processing_failures)
    receipt = summarize_pass(result, configuration_version="v", observed_at=NOW)
    assert receipt["parsed"] == 0
    assert receipt["health"] == "processing_attention_required"
    with factory() as verify:
        assert verify.get(Document, landed).parse_status == "pending"
        assert verify.scalar(
            select(func.count()).select_from(DocPage).where(
                DocPage.document_id == landed
            )
        ) == 0


def test_an_unknown_project_is_refused_before_any_model_work(runtime_database):
    factory = runtime_database.session_factory
    route = ScriptedRoute({})
    with pytest.raises(ProcessingScopeRefused):
        process_project(
            factory,
            project_id=987654321,
            select_route=route,
            clock=ControlledClock(NOW),
        )
    assert route.extract_calls == {}


def test_two_committed_workers_do_not_both_extract_one_eligible_document(
    runtime_database,
):
    """Two live passes meeting on one eligible document, in real transactions.

    #918 fenced the read act and nothing after it. The same overlap reaches
    extraction: `claim_due_work` deliberately re-claims an occurrence whose
    lease has expired, and the worker holding the old claim is not stopped, so
    a pass whose model calls outlive `claim_ttl_seconds` is still in the
    extraction loop when its successor enters it. Neither the skip check nor
    the append settles it. `already_extracted` reads committed completed runs,
    and neither worker has committed when the other looks;
    `record_extraction_run` -- the command every `EXTRACTED_PROPOSALS` route
    reaches -- takes no lock, holds no idempotency key, and `extraction_runs`
    has no unique constraint over `(document_id, prompt_version)`, because a
    redo is *supposed* to append another receipt.

    So the document pays for two model readings and carries two completed
    runs. Removing the claim below is the measurement, not a prediction: this
    exact interleaving leaves two completed runs, two Candidates for one
    conflict row, and a second run that B's Active Run declaration has already
    orphaned. Reverse the order of the two commits and the cost is the other
    one `declare_single_run_documents_by_policy` allows -- two completed runs
    and no declaration yet is its definition of ambiguous, and that keeps the
    document's conflicts off the record until a human chooses a run.

    The shape that proves it, which a repeated sequential pass could not:
    worker A is held *inside its own uncommitted extractor* while worker B runs
    a whole pass over the same project.
    """

    factory = runtime_database.session_factory
    project_id = _project(factory)
    eligible = _matrix(factory, project_id, "ucm.pdf", doc_date=date(2025, 1, 1))

    inside = Event()
    release = Event()
    readings: list[str] = []

    class HeldRoute(ScriptedRoute):
        def _extract(self, session, document):
            first = not readings
            readings.append(document.filename)
            if first:
                inside.set()
                assert release.wait(timeout=10)
            return super()._extract(session, document)

    route = HeldRoute({"ucm.pdf": ["PL1"]})

    def pass_over_the_project():
        return process_project(
            factory,
            project_id=project_id,
            select_route=route,
            clock=ControlledClock(NOW),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_a = pool.submit(pass_over_the_project)
        assert inside.wait(timeout=10), "worker A never reached the extractor"
        result_b = pass_over_the_project()
        release.set()
        result_a = worker_a.result(timeout=30)

    assert readings == ["ucm.pdf"], (
        "the second worker read a document the first was already reading"
    )
    # B leaves the document to the worker holding it and says so honestly: it
    # is not B's failure, and B did not take it to extraction either.
    assert result_b.extracted == 0
    assert result_b.processing_failures == []
    assert result_b.excluded["extracting_elsewhere"] == 1
    assert result_b.eligible_document_count == 0
    # A's reading is the one that counts, and there is one of everything after
    # it -- one paid model call, one completed run, one Candidate, one
    # conflict, and a document no human has to disambiguate.
    assert result_a.extracted == 1
    assert result_a.excluded["extracting_elsewhere"] == 0
    assert result_a.processing_failures == []
    with factory() as verify:
        assert _completed_runs(verify, project_id) == 1
        assert verify.scalar(
            select(func.count()).select_from(Candidate).where(
                Candidate.source_document_id == eligible
            )
        ) == 1
        assert _dependencies(verify, project_id) == {"PL1"}
    assert result_a.ambiguous_documents == []
    assert result_b.ambiguous_documents == []
