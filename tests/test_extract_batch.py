"""The pooled runner — the path production actually takes.

Both `make agreements` and `make minutes` ran 96 lines of wiring that
existed twice and was verified zero times: resume filtering, the limit,
per-document reporting, the error tally, and `client.close()` in a
`finally`. Each module's suite drove a sequential `extract_document` that
nothing in `src/` calls.
"""

import threading

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extraction_runs import (
    active_run_for_document,
    declare_active_run,
    record_extraction_run,
)
from corridor.extract_batch import Noun, extract_documents, run_extraction
from corridor.extractor_lineage import injected_extractor_config
from corridor.llm import Usage
from corridor.models import Candidate, DocPage, Document, ExtractionRun, Project
from corridor.principals import HumanPrincipal

from corridor.llm import RequestConfiguration

from model_client_support import FakeModelClient

PROMPT_VERSION = "batch_test_v1"
SCHEMA = {"type": "object"}


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
    p = Project(slug="batch-test", name="Batch Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def add_note(session, project, name, sha, *, text="AT&T will relocate in August."):
    doc = Document(
        project_id=project.id,
        sha256=sha,
        filename=name,
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    session.add(DocPage(document_id=doc.id, page_no=1, text=text))
    session.flush()
    return doc


def add_page(session, document, page_no, text):
    session.add(DocPage(document_id=document.id, page_no=page_no, text=text))
    session.flush()


class StubClient(FakeModelClient):
    """Two adapters justify the seam: this one, and OpenAIClient.

    The call shape comes from the shared double; only the answer and the
    fixed counters are this module's.
    """

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5

    # Deliberately a constant, not a meter: a run that reports the same
    # snapshot before and after records a zero delta.
    usage = _Usage()

    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        super().__init__(
            self.answer,
            configuration=RequestConfiguration(model="stub-model"),
            max_workers=2,
        )

    def answer(self, call):
        if self.fail_on and self.fail_on in call.user:
            raise RuntimeError("503 upstream")
        return {"events": [{"description": "AT&T will relocate in August."}]}


def _to_candidate(document, page, item, model):
    return Candidate(
        project_id=document.project_id,
        kind="event",
        payload_json={"kind": "event", "fields": {"description": item["description"]}},
        source_document_id=document.id,
        source_pages=[page.page_no],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model=model,
        citations_verified=True,
    )


def _run(session, project, client, argv=None, **kw):
    config = kw.pop("extractor_config", None) or injected_extractor_config(
        extractor="batch-fixture",
        prompt_version=PROMPT_VERSION,
        model=client.model,
        schema_version=PROMPT_VERSION,
        prompt_bytes=b"s",
        schema=SCHEMA,
        postprocessor_bytes=b"batch fixture rules",
        request_controls={"strict": True},
    )
    return run_extraction(
        argv if argv is not None else [project.slug],
        doc_type="minutes",
        default_slug=project.slug,
        prompt_version=PROMPT_VERSION,
        system="s",
        schema=SCHEMA,
        min_page_chars=1,
        to_candidate=_to_candidate,
        items_key="events",
        noun=Noun("notes", "events"),
        extractor_config=config,
        client_factory=lambda: client,
        session_factory=lambda: _Scoped(session),
        **kw,
    )


class _Scoped:
    """Hand the runner the test's transaction rather than a real one."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def _candidates(session, project):
    return session.scalars(
        select(Candidate).where(Candidate.project_id == project.id)
    ).all()


def _runs(session, project):
    return session.scalars(
        select(ExtractionRun)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project.id)
        .order_by(ExtractionRun.id)
    ).all()


def _run_outcomes_by_document(runs):
    """Batch outcomes are identified by Document, never completion/id order."""

    return {
        run.document_id: (run.candidate_count, run.page_errors) for run in runs
    }


def test_the_runner_extracts_and_closes_its_client(session, project, capsys):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    client = StubClient()

    assert _run(session, project, client) == 0

    assert len(_candidates(session, project)) == 1
    [run] = _runs(session, project)
    assert (run.prompt_version, run.candidate_count, run.page_errors) == (
        PROMPT_VERSION,
        1,
        0,
    )
    assert client.closed is True
    out = capsys.readouterr().out
    assert "1 notes at 2-way concurrency" in out
    assert "1 events, 1 verified (100.0%)" in out


def test_pooled_run_records_one_exact_batch_usage_receipt_per_member(
    session, project
):
    first = add_note(session, project, "notes-a.pdf", "a" * 64)
    second = add_note(session, project, "notes-b.pdf", "b" * 64)

    class MeteredClient(StubClient):
        """A metered stub that accumulates usage the way the real client does.

        `run_extraction` drives a `ThreadPoolExecutor`, so these four counters
        are incremented from several worker threads at once. `OpenAIClient`
        holds `_usage_lock` across exactly this accumulation (`llm.py`); a stub
        that does not is a lost update away from reporting half the tokens it
        was handed, which is a flake in the batch receipt rather than a fault
        the test is meant to find. The lock is on the meter alone: `complete`
        itself stays concurrent, because pooled execution is the property this
        test exists to prove.
        """

        def __init__(self):
            super().__init__()
            self.usage = Usage()
            self._meter = threading.Lock()

        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            with self._meter:
                self.usage.prompt_tokens += 100
                self.usage.completion_tokens += 20
                self.usage.reasoning_tokens += 3
                self.usage.cached_tokens += 40
            return result

    _run(session, project, MeteredClient())

    runs = _runs(session, project)
    runs_by_document = {run.document_id: run for run in runs}
    assert set(runs_by_document) == {first.id, second.id}
    expected = {
        "scope": "batch",
        "document_ids": [first.id, second.id],
        "measurement": "exact",
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "reasoning_tokens": 6,
        "cached_tokens": 80,
    }
    assert [runs_by_document[document_id].token_usage_json for document_id in (first.id, second.id)] == [
        expected,
        expected,
    ]
    assert all(run.extractor_config_sha256 for run in runs)


def test_runner_refuses_when_the_seal_does_not_match_the_runtime_prompt(
    session, project
):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    client = StubClient()
    wrong = injected_extractor_config(
        extractor="batch-fixture",
        prompt_version=PROMPT_VERSION,
        model=client.model,
        schema_version=PROMPT_VERSION,
        prompt_bytes=b"different prompt",
        schema=SCHEMA,
        postprocessor_bytes=b"batch fixture rules",
        request_controls={"strict": True},
    )

    with pytest.raises(ValueError, match="prompt bytes"):
        _run(session, project, client, extractor_config=wrong)

    assert client.calls == []
    assert client.closed is True


def test_direct_batch_seam_refuses_mismatched_runtime_sources(session, project):
    document = add_note(session, project, "notes-a.pdf", "a" * 64)
    client = StubClient()
    wrong = injected_extractor_config(
        extractor="batch-fixture",
        prompt_version=PROMPT_VERSION,
        model=client.model,
        schema_version=PROMPT_VERSION,
        prompt_bytes=b"not the runtime prompt",
        schema=SCHEMA,
        postprocessor_bytes=b"batch fixture rules",
        request_controls={"strict": True},
    )

    with pytest.raises(ValueError, match="prompt bytes"):
        extract_documents(
            session,
            [document],
            client=client,
            system="s",
            schema=SCHEMA,
            min_page_chars=1,
            to_candidate=_to_candidate,
            items_key="events",
            prompt_version=PROMPT_VERSION,
            extractor_config=wrong,
            commit=False,
        )

    assert client.calls == []


def test_reading_a_document_once_declares_that_reading(session, project):
    """ADR-0029 puts the load in the pipeline, so a document with one
    completed reading no longer waits for someone to name it. Naming the
    only reading is a fact; choosing between several is not, and stays a
    human act."""
    add_note(session, project, "notes-a.pdf", "a" * 64)
    _run(session, project, StubClient())

    [run] = _runs(session, project)
    declared = active_run_for_document(session, run.document_id)
    assert declared is not None and declared.id == run.id


def test_a_later_reading_never_takes_the_declaration_by_being_newer(
    session, project
):
    """Choosing between completed readings is exactly the inference
    Active Runs exist to forbid, and the pipeline stage does not do it."""
    from corridor.admission import load_project
    from corridor.extraction_runs import record_extraction_run

    document = add_note(session, project, "notes-a.pdf", "a" * 64)
    _run(session, project, StubClient())
    [first] = _runs(session, project)

    record_extraction_run(
        session,
        document,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        allow_unsealed_legacy=True,
    )
    session.flush()
    load_project(session, project.id)

    assert active_run_for_document(session, document.id).id == first.id


def test_a_resumed_run_skips_documents_already_extracted(
    session, project, capsys
):
    """The invariant the whole commit-per-document design exists for."""
    add_note(session, project, "notes-a.pdf", "a" * 64)
    add_note(session, project, "notes-b.pdf", "b" * 64)

    _run(session, project, StubClient())
    assert len(_candidates(session, project)) == 2

    second = StubClient()
    assert _run(session, project, second) == 0

    assert second.calls == []
    assert len(_candidates(session, project)) == 2
    assert "nothing to do: all 2 already extracted" in capsys.readouterr().out


def test_redo_appends_a_fresh_exact_document_run_without_retargeting_active(
    session, project
):
    document = add_note(session, project, "notes-a.pdf", "a" * 64)
    _run(
        session,
        project,
        StubClient(),
        argv=[project.slug, "--document-id", str(document.id)],
    )
    [baseline] = _runs(session, project)

    _run(
        session,
        project,
        StubClient(),
        argv=[project.slug, "--document-id", str(document.id), "--redo"],
    )

    runs = _runs(session, project)
    assert len(runs) == 2
    assert len(_candidates(session, project)) == 2
    assert active_run_for_document(session, document.id).id == baseline.id


def test_a_zero_row_document_is_still_marked_done_for_resume(
    session, project, capsys
):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    client = StubClient()
    client.complete = lambda **kw: {"events": []}

    assert _run(session, project, client) == 0

    assert _candidates(session, project) == []
    [run] = _runs(session, project)
    assert (run.document_id, run.prompt_version, run.candidate_count, run.page_errors) == (
        session.scalars(select(Document.id).where(Document.project_id == project.id)).one(),
        PROMPT_VERSION,
        0,
        0,
    )

    second = StubClient()
    assert _run(session, project, second) == 0
    assert second.calls == []
    assert "nothing to do: all 1 already extracted" in capsys.readouterr().out


def test_a_document_with_any_failed_page_persists_no_partial_candidates_and_retries_cleanly(
    session, project, capsys
):
    from corridor.eval import extracted_documents
    from corridor.extract_batch import already_extracted

    failed = add_note(
        session, project, "notes-a.pdf", "a" * 64, text="page one about AT&T"
    )
    add_page(session, failed, 2, "page two about PSE")
    clean = add_note(session, project, "notes-b.pdf", "b" * 64, text="page three about OUC")

    first = StubClient(fail_on="page one")
    assert _run(session, project, first) == 0

    assert [c.source_document_id for c in _candidates(session, project)] == [clean.id]
    first_runs = _runs(session, project)
    # Reverse the observed completion order explicitly: no public batch seam
    # promises which concurrent document receives the first sequence value.
    assert _run_outcomes_by_document(reversed(first_runs)) == {
        failed.id: (0, 1),
        clean.id: (1, 0),
    }
    assert already_extracted(session, project.id, PROMPT_VERSION) == {clean.id}
    assert extracted_documents(session, project.id, prompt_version=PROMPT_VERSION) == {
        clean.id
    }

    second = StubClient()
    assert _run(session, project, second) == 0

    failed_candidates = [
        c for c in _candidates(session, project) if c.source_document_id == failed.id
    ]
    assert len(failed_candidates) == 2
    assert {tuple(c.source_pages) for c in failed_candidates} == {(1,), (2,)}
    assert already_extracted(session, project.id, PROMPT_VERSION) == {
        failed.id,
        clean.id,
    }
    assert extracted_documents(session, project.id, prompt_version=PROMPT_VERSION) == {
        failed.id,
        clean.id,
    }
    out = capsys.readouterr().out
    assert "1 page errors" in out
    assert "2 events, 2 verified (100.0%)" in out


def test_an_all_page_failed_zero_row_attempt_is_not_marked_done_for_resume(
    session, project, capsys
):
    add_note(session, project, "notes-a.pdf", "a" * 64, text="page one about AT&T")
    first = StubClient(fail_on="page one")

    assert _run(session, project, first) == 0

    assert _candidates(session, project) == []
    [run] = _runs(session, project)
    assert (run.prompt_version, run.candidate_count, run.page_errors) == (
        PROMPT_VERSION,
        0,
        1,
    )

    second = StubClient(fail_on="page one")
    assert _run(session, project, second) == 0
    assert len(second.calls) == 1
    assert len(_runs(session, project)) == 2
    out = capsys.readouterr().out
    assert "1 page errors" in out


def test_a_document_with_no_eligible_pages_is_recorded_as_an_error_not_done(
    session, project, capsys
):
    add_note(session, project, "notes-a.pdf", "a" * 64, text="")
    client = StubClient()

    assert _run(session, project, client) == 0

    assert client.calls == []
    assert _candidates(session, project) == []
    [run] = _runs(session, project)
    assert (run.prompt_version, run.candidate_count, run.page_errors) == (
        PROMPT_VERSION,
        0,
        1,
    )

    second = StubClient()
    assert _run(session, project, second) == 0
    assert second.calls == []
    assert len(_runs(session, project)) == 2
    out = capsys.readouterr().out
    assert "1 page errors" in out


def test_candidate_presence_alone_does_not_mark_a_document_done(session, project):
    doc = add_note(session, project, "notes-a.pdf", "a" * 64)
    session.add(
        Candidate(
            project_id=project.id,
            kind="event",
            payload_json={"kind": "event", "fields": {"description": "legacy only"}},
            source_document_id=doc.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version=PROMPT_VERSION,
            model="stub",
            citations_verified=True,
        )
    )
    session.flush()

    from corridor.extract_batch import already_extracted

    assert already_extracted(session, project.id, PROMPT_VERSION) == set()


def test_the_limit_stops_after_n_documents(session, project, capsys):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    add_note(session, project, "notes-b.pdf", "b" * 64)
    add_note(session, project, "notes-c.pdf", "c" * 64)

    _run(session, project, StubClient(), argv=[project.slug, "2"])

    assert len(_candidates(session, project)) == 2


def test_explicit_document_ids_bound_the_minutes_run(session, project):
    first = add_note(session, project, "notes-a.pdf", "a" * 64)
    selected = add_note(session, project, "notes-b.pdf", "b" * 64)
    skipped = add_note(session, project, "notes-c.pdf", "c" * 64)

    _run(
        session,
        project,
        StubClient(),
        argv=[project.slug, "--document-id", str(selected.id), "--document-id", str(first.id)],
    )

    assert {c.source_document_id for c in _candidates(session, project)} == {
        first.id,
        selected.id,
    }
    assert skipped.id not in {c.source_document_id for c in _candidates(session, project)}


def test_new_prompt_version_appends_history_without_replacing_the_active_run(
    session, project
):
    document = add_note(session, project, "historical-notes.pdf", "a" * 64)
    historical = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"kind": "event", "fields": {"description": "Old reading"}},
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="minutes_v1",
        model="historical-model",
        citations_verified=True,
    )
    historical_run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(historical,),
        model="historical-model",
        schema_version="minutes_v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(
        session,
        document.id,
        historical_run.id,
        principal=HumanPrincipal("local:minutes-v1-owner"),
    )
    original_payload = historical.payload_json.copy()

    assert _run(session, project, StubClient()) == 0

    runs = _runs(session, project)
    assert [(run.prompt_version, run.model) for run in runs] == [
        ("minutes_v1", "historical-model"),
        (PROMPT_VERSION, "stub-model"),
    ]
    session.refresh(historical)
    assert historical.payload_json == original_payload
    assert historical.prompt_version == "minutes_v1"
    assert active_run_for_document(session, document.id).id == historical_run.id


@pytest.mark.parametrize(
    "argv_builder",
    [
        pytest.param(
            lambda project, docs: [
                project.slug,
                "--document-id",
                str(docs["selected"].id),
                "--document-id",
                str(docs["selected"].id),
            ],
            id="duplicate-selection",
        ),
        pytest.param(
            lambda project, docs: [
                project.slug,
                "--document-id",
                str(docs["missing_id"]),
            ],
            id="missing-selection",
        ),
        pytest.param(
            lambda project, docs: [
                project.slug,
                "--document-id",
                str(docs["wrong_type"].id),
            ],
            id="wrong-type-selection",
        ),
        pytest.param(
            lambda project, docs: [
                project.slug,
                "--document-id",
                str(docs["other_project"].id),
            ],
            id="cross-project-selection",
        ),
    ],
)
def test_invalid_document_selection_is_refused_before_model_calls(
    session, project, argv_builder
):
    selected = add_note(session, project, "notes-a.pdf", "a" * 64)
    wrong_type = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="agreement.pdf",
        doc_type="agreement",
        parse_status="parsed",
        pages=1,
    )
    session.add(wrong_type)
    other_project = Project(slug="other-project", name="Other", is_synthetic=True)
    session.add(other_project)
    session.flush()
    other_document = add_note(session, other_project, "notes-b.pdf", "b" * 64)
    client = StubClient()

    result = _run(
        session,
        project,
        client,
        argv=argv_builder(
            project,
            {
                "selected": selected,
                "wrong_type": wrong_type,
                "other_project": other_document,
                "missing_id": other_document.id + 9999,
            },
        ),
    )

    assert result == 1
    assert client.calls == []
    assert _candidates(session, project) == []


def test_a_failed_document_does_not_block_clean_siblings(
    session, project, capsys
):
    """A failed document retries later, but clean siblings still commit now."""
    failed = add_note(
        session, project, "notes-a.pdf", "a" * 64, text="page one about AT&T"
    )
    clean = add_note(
        session, project, "notes-b.pdf", "b" * 64, text="page two about PSE"
    )
    client = StubClient(fail_on="page one")

    assert _run(session, project, client) == 0

    assert len(_candidates(session, project)) == 1
    assert _run_outcomes_by_document(reversed(_runs(session, project))) == {
        failed.id: (0, 1),
        clean.id: (1, 0),
    }
    out = capsys.readouterr().out
    assert "1 page errors" in out
    assert "1 pages failed" in out
    assert client.closed is True


def test_an_unknown_project_is_refused(session, project, capsys):
    client = StubClient()
    assert _run(session, project, client, argv=["no-such-project"]) == 1
    assert client.calls == []


def test_the_client_is_closed_even_when_extraction_raises(session, project):
    """`client.close()` sits in a `finally`, and nothing checked it."""
    add_note(session, project, "notes-a.pdf", "a" * 64)

    class Exploding(StubClient):
        def answer(self, call):
            raise KeyboardInterrupt("operator stopped the run")

    client = Exploding()
    with pytest.raises(KeyboardInterrupt):
        _run(session, project, client)

    assert client.closed is True
