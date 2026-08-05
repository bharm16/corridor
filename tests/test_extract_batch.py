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
from corridor.models import Candidate, DocPage, Document, Project

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


def test_the_runner_extracts_and_closes_its_client(session, project, capsys):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    client = StubClient()

    assert _run(session, project, client) == 0

    assert len(_candidates(session, project)) == 1
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


def test_the_limit_stops_after_n_documents(session, project, capsys):
    add_note(session, project, "notes-a.pdf", "a" * 64)
    add_note(session, project, "notes-b.pdf", "b" * 64)
    add_note(session, project, "notes-c.pdf", "c" * 64)

    _run(session, project, StubClient(), argv=[project.slug, "2"])

    assert len(_candidates(session, project)) == 2


def test_a_failing_page_is_counted_and_the_run_continues(
    session, project, capsys
):
    """One page lost, reported, run continues — untested until now."""
    add_note(session, project, "notes-a.pdf", "a" * 64, text="page one about AT&T")
    add_note(session, project, "notes-b.pdf", "b" * 64, text="page two about PSE")
    client = StubClient(fail_on="page one")

    assert _run(session, project, client) == 0

    assert len(_candidates(session, project)) == 1
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
