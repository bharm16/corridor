"""The pooled runner — the path production actually takes.

Both `make agreements` and `make minutes` ran 96 lines of wiring that
existed twice and was verified zero times: resume filtering, the limit,
per-document reporting, the error tally, and `client.close()` in a
`finally`. Each module's suite drove a sequential `extract_document` that
nothing in `src/` calls.
"""

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_batch import Noun, run_extraction
from corridor.models import Candidate, DocPage, Document, ExtractionRun, Project

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


class StubClient:
    """Two adapters justify the seam: this one, and OpenAIClient."""

    max_workers = 2
    model = "stub-model"

    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        self.closed = False
        self.calls = 0

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5

    usage = _Usage()

    def complete(self, *, system, user, schema, **kw):
        self.calls += 1
        if self.fail_on and self.fail_on in user:
            raise RuntimeError("503 upstream")
        return {"events": [{"description": "AT&T will relocate in August."}]}

    def close(self):
        self.closed = True


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

    assert second.calls == 0
    assert len(_candidates(session, project)) == 2
    assert "nothing to do: all 2 already extracted" in capsys.readouterr().out


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
    assert second.calls == 0
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
    assert [
        (run.document_id, run.candidate_count, run.page_errors) for run in first_runs
    ] == [
        (failed.id, 0, 1),
        (clean.id, 1, 0),
    ]
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
    assert second.calls == 1
    assert len(_runs(session, project)) == 2
    out = capsys.readouterr().out
    assert "1 page errors" in out


def test_a_document_with_no_eligible_pages_is_recorded_as_an_error_not_done(
    session, project, capsys
):
    add_note(session, project, "notes-a.pdf", "a" * 64, text="")
    client = StubClient()

    assert _run(session, project, client) == 0

    assert client.calls == 0
    assert _candidates(session, project) == []
    [run] = _runs(session, project)
    assert (run.prompt_version, run.candidate_count, run.page_errors) == (
        PROMPT_VERSION,
        0,
        1,
    )

    second = StubClient()
    assert _run(session, project, second) == 0
    assert second.calls == 0
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


def test_a_failed_document_does_not_block_clean_siblings(
    session, project, capsys
):
    """A failed document retries later, but clean siblings still commit now."""
    add_note(session, project, "notes-a.pdf", "a" * 64, text="page one about AT&T")
    add_note(session, project, "notes-b.pdf", "b" * 64, text="page two about PSE")
    client = StubClient(fail_on="page one")

    assert _run(session, project, client) == 0

    assert len(_candidates(session, project)) == 1
    [failed_run, clean_run] = _runs(session, project)
    assert (failed_run.candidate_count, failed_run.page_errors) == (0, 1)
    assert (clean_run.candidate_count, clean_run.page_errors) == (1, 0)
    out = capsys.readouterr().out
    assert "1 page errors" in out
    assert "1 pages failed" in out
    assert client.closed is True


def test_an_unknown_project_is_refused(session, project, capsys):
    client = StubClient()
    assert _run(session, project, client, argv=["no-such-project"]) == 1
    assert client.calls == 0


def test_the_client_is_closed_even_when_extraction_raises(session, project):
    """`client.close()` sits in a `finally`, and nothing checked it."""
    add_note(session, project, "notes-a.pdf", "a" * 64)

    class Exploding(StubClient):
        def complete(self, **kw):
            raise KeyboardInterrupt("operator stopped the run")

    client = Exploding()
    with pytest.raises(KeyboardInterrupt):
        _run(session, project, client)

    assert client.closed is True
