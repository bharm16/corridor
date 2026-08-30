"""Discover and process documents from one connected location (#350).

Two kinds of test live here. Discovery coalescing, authorization, parse recovery,
and the operations projection run on an ordinary rollback-scoped ``session``. The
bounded discovery-and-fetch pass owns committed transactions, so it uses the
harness-owned ``runtime_database`` with controlled HTTP/archive fixtures
(``httpx.MockTransport``) and a controlled clock — no live account, no real
holdout, no model spend, and no shared-record mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
from uuid import uuid4
import zipfile

import httpx
import pymupdf
import pytest
from sqlalchemy import func, select

from corridor import audit
from corridor.config import settings
from corridor.db import Session, engine
from corridor.due_work import (
    DueWorkRefusal,
    HANDLER_LOCATION_DISCOVERY,
    LocationDiscoveryDeclaration,
    configure_location_discovery,
    enqueue_due_work,
    run_due_work_once,
)
from corridor import location_discovery as ld
from corridor.location_discovery import (
    DiscoveryBudgets,
    IndexEntry,
    LocationDiscoveryRefused,
    LocationScope,
    authorize_reference,
    discover_and_process,
    effective_source_url,
    observe_references,
    operations_view,
    parse_index,
    recover_document_parse,
    reference_key,
)
from corridor.models import (
    AuditLog,
    DiscoveredReference,
    Document,
    DocumentQuarantine,
    Project,
    SourceFetchAttempt,
)
from corridor.principals import HumanPrincipal

PRINCIPAL = HumanPrincipal("local:operator")
LOCATION = "txdot-loc"
HOST = "docs.example.gov"
INDEX_URL = f"https://{HOST}/index.json"
NOW = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)


# --- fixtures & builders ----------------------------------------------------


class FixedClock:
    def __init__(self, value: datetime = NOW):
        self.value = value

    def now(self) -> datetime:
        return self.value


class AdvancingClock:
    def __init__(self, start: datetime, step_seconds: float):
        self._t = start - timedelta(seconds=step_seconds)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        self._t = self._t + self._step
        return self._t


def _pdf(marker: str = "Utility Conflict Matrix") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), marker)
    page.insert_text((72, 130), "FOC1-1  AT&T Texas  Telecom  STA 1149+00 to 1153+17")
    body = doc.tobytes()
    doc.close()
    return body


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buf.getvalue()


def _response(body: bytes, *, status: int = 200, content_type: str = "application/pdf",
              headers: dict | None = None) -> httpx.Response:
    merged = {"content-type": content_type}
    merged.update(headers or {})
    return httpx.Response(status, content=body, headers=merged)


def _client(routes) -> httpx.Client:
    """A MockTransport client. ``routes`` maps a URL to a Response or callable."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        route = routes.get(url)
        if route is None:
            return httpx.Response(404, content=b"missing")
        if callable(route):
            return route(request)
        return route

    return httpx.Client(transport=httpx.MockTransport(handler))


def _index(entries: list[dict]) -> httpx.Response:
    import json

    return _response(
        json.dumps({"entries": entries}).encode(),
        content_type="application/json",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


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
    p = Project(slug=f"loc-{uuid4().hex[:8]}", name="Location Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _scope(project_id: int, *, sealed: bool = False, index_url: str = INDEX_URL,
           hosts: frozenset[str] = frozenset({HOST})) -> LocationScope:
    return LocationScope(
        location_id=LOCATION,
        project_id=project_id,
        index_url=index_url,
        authorized_hosts=hosts,
        sealed=sealed,
        source_manifest_id="txdot-manifest",
        adapter_identity="http-index-v1",
    )


def _budgets(**overrides) -> DiscoveryBudgets:
    base = DiscoveryBudgets.default()
    return DiscoveryBudgets(**{**base.__dict__, **overrides})


# --- Discovery: observe & coalesce (rollback session) -----------------------


def test_parse_index_reads_entries_and_refuses_malformed():
    entries = parse_index(
        b'{"entries":[{"url":"https://x/y.pdf","title":"T","type_hint":"matrix"}]}'
    )
    assert entries == (IndexEntry(url="https://x/y.pdf", title="T", type_hint="matrix"),)
    for bad in (b"not json", b"[]", b'{"entries":[{"title":"no url"}]}'):
        with pytest.raises(LocationDiscoveryRefused):
            parse_index(bad)


def test_discovery_records_new_references_as_proposed_intake(session, project):
    scope = _scope(project.id)
    entries = (
        IndexEntry(url="https://docs.example.gov/a.pdf", title="Matrix A", type_hint="matrix"),
        IndexEntry(url="https://docs.example.gov/b.pdf", title="Minutes B"),
    )
    new, repeat, known, new_archives = observe_references(session, scope, entries, now=NOW)

    assert (new, repeat, known, new_archives) == (2, 0, 0, 0)
    rows = session.scalars(
        select(DiscoveredReference).where(DiscoveredReference.project_id == project.id)
    ).all()
    assert {r.state for r in rows} == {"proposed"}
    a = next(r for r in rows if r.source_url.endswith("a.pdf"))
    assert a.observed_title == "Matrix A"
    assert a.observed_type_hint == "matrix"          # advisory, not an accepted fact
    assert a.authorized_doc_type is None and a.registered_document_id is None
    assert a.observed_count == 1 and a.first_observed_at == a.last_observed_at
    # Discovery is not registration authority: nothing was registered.
    assert session.scalar(
        select(func.count()).select_from(Document).where(Document.project_id == project.id)
    ) == 0


def test_repeated_discovery_coalesces_into_one_identity(session, project):
    scope = _scope(project.id)
    entries = (IndexEntry(url="https://docs.example.gov/a.pdf", title="A"),)
    observe_references(session, scope, entries, now=NOW)
    later = NOW + timedelta(hours=1)
    new, repeat, _known, _new_arch = observe_references(session, scope, entries, now=later)

    assert (new, repeat) == (0, 1)
    rows = session.scalars(
        select(DiscoveredReference).where(DiscoveredReference.project_id == project.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].observed_count == 2
    assert rows[0].last_observed_at == later and rows[0].first_observed_at == NOW


def test_newly_published_archive_url_is_a_discovery(session, project):
    scope = _scope(project.id)
    entries = (
        IndexEntry(url="https://docs.example.gov/2026-08.zip", member="utilities.pdf"),
    )
    new, _repeat, _known, new_archives = observe_references(session, scope, entries, now=NOW)

    assert new == 1 and new_archives == 1
    row = session.scalar(select(DiscoveredReference).where(DiscoveredReference.project_id == project.id))
    assert row.archive_url == "https://docs.example.gov/2026-08.zip"
    assert row.member == "utilities.pdf"
    assert row.source_url == "https://docs.example.gov/2026-08.zip::utilities.pdf"


def test_refetch_of_an_already_declared_snapshot_member_is_not_discovery(session, project, store):
    # A member already registered as a Document (declared snapshot) is known, not new.
    canonical = effective_source_url("https://docs.example.gov/2026-08.zip", "utilities.pdf")
    session.add(
        Document(
            project_id=project.id,
            sha256=hashlib.sha256(b"x").hexdigest(),
            filename="utilities.pdf",
            doc_type="matrix",
            source_url=canonical,
            parse_status="parsed",
            pages=1,
        )
    )
    session.flush()
    scope = _scope(project.id)
    entries = (IndexEntry(url="https://docs.example.gov/2026-08.zip", member="utilities.pdf"),)
    new, repeat, known, _new_arch = observe_references(session, scope, entries, now=NOW)

    assert (new, repeat, known) == (0, 0, 1)
    assert session.scalar(
        select(func.count()).select_from(DiscoveredReference).where(
            DiscoveredReference.project_id == project.id
        )
    ) == 0


# --- Authorization (rollback session) ---------------------------------------


def _proposed(session, project, url="https://docs.example.gov/a.pdf", member=None):
    scope = _scope(project.id)
    observe_references(session, scope, (IndexEntry(url=url, member=member),), now=NOW)
    return reference_key(scope.location_id, effective_source_url(url, member))


def test_authorize_declares_kind_and_records_an_attributable_receipt(session, project):
    key = _proposed(session, project)
    result = authorize_reference(
        session, project_id=project.id, reference_key=key, doc_type="matrix",
        principal=PRINCIPAL, registry_id="ucm-2026-08", now=NOW,
    )
    row = session.get(DiscoveredReference, result.reference_id)
    assert row.state == "authorized"
    assert row.authorized_doc_type == "matrix"
    assert row.authorized_registry_id == "ucm-2026-08"
    assert row.authorized_by == PRINCIPAL.subject and row.authorized_at is not None
    entry = session.get(AuditLog, result.audit_id)
    assert entry.action == audit.AUTHORIZE_DISCOVERED_REFERENCE
    assert entry.human_principal == PRINCIPAL.subject


def test_authorize_refuses_unknown_kind_reference_and_double_authorization(session, project):
    key = _proposed(session, project)
    with pytest.raises(LocationDiscoveryRefused) as bad_kind:
        authorize_reference(session, project_id=project.id, reference_key=key,
                            doc_type="manifest", principal=PRINCIPAL, now=NOW)
    assert bad_kind.value.reason == "unknown_doc_type"
    with pytest.raises(LocationDiscoveryRefused) as unknown:
        authorize_reference(session, project_id=project.id, reference_key="0" * 64,
                            doc_type="matrix", principal=PRINCIPAL, now=NOW)
    assert unknown.value.reason == "unknown_reference"
    authorize_reference(session, project_id=project.id, reference_key=key,
                        doc_type="matrix", principal=PRINCIPAL, now=NOW)
    with pytest.raises(LocationDiscoveryRefused) as again:
        authorize_reference(session, project_id=project.id, reference_key=key,
                            doc_type="matrix", principal=PRINCIPAL, now=NOW)
    assert again.value.reason == "not_proposed"


# --- Parse recovery (rollback session) --------------------------------------


def _failed_document(session, project, body: bytes, *, filename="d.pdf") -> Document:
    from pathlib import Path

    sha = hashlib.sha256(body).hexdigest()
    shard = Path(settings.corpus_store) / sha[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f"{sha}.pdf").write_bytes(body)
    document = Document(
        project_id=project.id, sha256=sha, filename=filename, doc_type="matrix",
        parse_status="failed", pages=0,
    )
    session.add(document)
    session.flush()
    return document


def test_parse_recovery_reparses_a_failed_document(session, project, store):
    document = _failed_document(session, project, _pdf())
    result = recover_document_parse(
        session, document_id=document.id, principal=PRINCIPAL, now=NOW,
    )
    assert result.outcome == "recovered" and result.pages >= 1
    assert document.parse_status == "parsed"
    assert session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == audit.RECOVER_DOCUMENT_PARSE,
            AuditLog.entity_id == document.id,
        )
    ) == 1


def test_parse_recovery_never_rewrites_a_successfully_parsed_document(session, project, store):
    document = _failed_document(session, project, _pdf())
    document.parse_status = "parsed"
    document.pages = 5
    session.flush()
    result = recover_document_parse(session, document_id=document.id, principal=PRINCIPAL, now=NOW)
    assert result.outcome == "skipped_parsed"
    assert document.pages == 5


def test_parse_recovery_on_unparseable_bytes_stays_failed(session, project, store):
    document = _failed_document(session, project, b"%PDF-1.4 not really a pdf")
    result = recover_document_parse(session, document_id=document.id, principal=PRINCIPAL, now=NOW)
    assert result.outcome == "still_failed"
    assert document.parse_status == "failed"


def test_parse_recovery_is_refused_for_a_sealed_location(session, project, store):
    configure_location_discovery(
        session,
        LocationDiscoveryDeclaration.released_hourly(
            project_id=project.id, configuration_version="loc-v1", location_id=LOCATION,
            adapter_identity="http-index-v1", source_manifest_id="txdot-manifest",
            index_url=INDEX_URL, authorized_hosts=(HOST,), sealed=True, starts_at=NOW,
        ),
        now=NOW,
    )
    document = _failed_document(session, project, _pdf())
    result = recover_document_parse(session, document_id=document.id, principal=PRINCIPAL, now=NOW)
    assert result.outcome == "refused_sealed"
    assert document.parse_status == "failed"    # the frozen population is not altered


# --- Operations visibility (rollback session) -------------------------------


def test_operations_view_keeps_cases_visible_with_reason_and_next_step(session, project, store):
    _proposed(session, project, url="https://docs.example.gov/proposed.pdf")
    # A quarantined schedule document — its reason must stay a schedule, not a matrix.
    schedule_doc = Document(
        project_id=project.id, sha256=hashlib.sha256(b"sched").hexdigest(),
        filename="schedule.pdf", doc_type="schedule", parse_status="parsed", pages=1,
    )
    session.add(schedule_doc)
    session.flush()
    session.add(DocumentQuarantine(document_id=schedule_doc.id, reason="work sequencing is not modeled"))
    _failed_document(session, project, _pdf(), filename="broken.pdf")
    session.flush()

    view = operations_view(session, project.id)
    assert [i.kind for i in view.proposed_intake] == ["proposed_intake"]
    kinds = {i.kind for i in view.held_documents}
    assert {"held_quarantined", "parse_failed"} <= kinds
    quarantined = next(i for i in view.held_documents if i.kind == "held_quarantined")
    assert "sequencing" in quarantined.permitted_next_step and "matrix" not in quarantined.reason
    parse_failed = next(i for i in view.held_documents if i.kind == "parse_failed")
    assert parse_failed.permitted_next_step == "run the bounded parse recovery"


# --- The bounded pass over committed transactions ---------------------------


def _project_in(factory) -> int:
    with factory() as s:
        p = Project(slug=f"loc-{uuid4().hex[:8]}", name="Loc", is_synthetic=True)
        s.add(p)
        s.commit()
        return p.id


def _authorize_in(factory, project_id, *, url, member=None, doc_type="matrix", registry_id=None):
    scope = _scope(project_id)
    canonical = effective_source_url(url, member)
    key = reference_key(scope.location_id, canonical)
    with factory() as s:
        s.add(
            DiscoveredReference(
                project_id=project_id, location_id=LOCATION, reference_key=key,
                source_url=canonical, archive_url=url if member else None, member=member,
                first_observed_at=NOW, last_observed_at=NOW, observed_count=1,
                state="authorized", authorized_doc_type=doc_type,
                authorized_registry_id=registry_id, authorized_by=PRINCIPAL.subject,
                authorized_at=NOW,
            )
        )
        s.commit()
    return key


def test_pass_discovers_authorizes_and_registers_end_to_end(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    doc_url = "https://docs.example.gov/ucm.pdf"
    body = _pdf("UCM rev A")
    routes = {INDEX_URL: _index([{"url": doc_url, "title": "UCM"}]), doc_url: _response(body)}

    # Pass 1: discovery only — the reference is proposed, nothing registered.
    result1 = discover_and_process(
        factory, scope=_scope(project_id), budgets=_budgets(), client=_client(routes), clock=FixedClock(),
    )
    assert result1.new_references == 1 and result1.registered == 0
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Document).where(Document.project_id == project_id)) == 0

    # A person authorizes the proposed reference (attributable), then the pass registers it.
    key = reference_key(LOCATION, doc_url)
    with factory() as s:
        with s.begin():
            authorize_reference(s, project_id=project_id, reference_key=key, doc_type="matrix",
                                principal=PRINCIPAL, now=NOW)
    result2 = discover_and_process(
        factory, scope=_scope(project_id), budgets=_budgets(), client=_client(routes), clock=FixedClock(),
    )
    assert result2.registered == 1 and result2.health == "healthy"
    with factory() as s:
        document = s.scalar(select(Document).where(Document.project_id == project_id))
        assert document.sha256 == hashlib.sha256(body).hexdigest()
        assert document.source_url == doc_url and document.retrieved_at is not None
        assert document.parse_status == "parsed"      # handed to the standing pass, extractable
        ref = s.scalar(select(DiscoveredReference).where(DiscoveredReference.reference_key == key))
        assert ref.state == "registered" and ref.registered_document_id == document.id
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.reference_key == key))
        assert attempt.outcome == "registered" and attempt.sha256 == document.sha256


def test_repeated_unchanged_pass_creates_no_new_document_or_attempt(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    doc_url = "https://docs.example.gov/ucm.pdf"
    routes = {INDEX_URL: _index([]), doc_url: _response(_pdf())}
    _authorize_in(factory, project_id, url=doc_url)

    discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(), client=_client(routes), clock=FixedClock())
    discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(), client=_client(routes), clock=FixedClock())

    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Document).where(Document.project_id == project_id)) == 1
        assert s.scalar(
            select(func.count()).select_from(SourceFetchAttempt).where(
                SourceFetchAttempt.project_id == project_id,
                SourceFetchAttempt.outcome == "registered",
            )
        ) == 1


def test_changed_checksum_under_a_registry_identity_is_retained_operations_work(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    url_a = "https://docs.example.gov/ucm-a.pdf"
    url_b = "https://docs.example.gov/ucm-b.pdf"
    body_a, body_b = _pdf("rev A"), _pdf("rev B")
    _authorize_in(factory, project_id, url=url_a, registry_id="ucm")
    _authorize_in(factory, project_id, url=url_b, registry_id="ucm")
    routes = {INDEX_URL: _index([]), url_a: _response(body_a), url_b: _response(body_b)}

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 1 and result.drifted == 1
    with factory() as s:
        docs = s.scalars(select(Document).where(Document.project_id == project_id)).all()
        assert len(docs) == 1                          # the old registered document was not overwritten
        assert docs[0].sha256 == hashlib.sha256(body_a).hexdigest()
        assert docs[0].superseded_by is None           # no inferred Supersession
        drift = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.outcome == "drift"))
        assert drift.prior_sha256 == hashlib.sha256(body_a).hexdigest()
        assert drift.sha256 == hashlib.sha256(body_b).hexdigest()
        # Both byte identities are retained on disk.
        assert (store / hashlib.sha256(body_a).hexdigest()[:2]).exists()
        assert (store / hashlib.sha256(body_b).hexdigest()[:2]).exists()
        view = operations_view(s, project_id)
        assert [i.kind for i in view.open_drifts] == ["drift"]


def test_sealed_location_is_excluded_before_any_fetch(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    _authorize_in(factory, project_id, url="https://docs.example.gov/held.pdf")
    requested: list[str] = []

    def record(request):
        requested.append(str(request.url))
        return _response(_pdf())

    client = _client({INDEX_URL: record, "https://docs.example.gov/held.pdf": record})
    result = discover_and_process(factory, scope=_scope(project_id, sealed=True), budgets=_budgets(),
                                  client=client, clock=FixedClock())
    assert result.sealed_excluded and result.health == "held_sealed"
    assert requested == []                             # nothing was fetched
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Document).where(Document.project_id == project_id)) == 0
        assert s.scalar(select(func.count()).select_from(SourceFetchAttempt).where(
            SourceFetchAttempt.project_id == project_id)) == 0


@pytest.mark.parametrize(
    "route,reason_fragment",
    [
        (_response(b"<html>error</html>", content_type="text/html"), "error_page"),
        (_response(b"%PDF-1.4 partial", headers={"x-archive-orig-x-crawler-content-length": "9999"}), "truncated"),
        (_response(b"not a pdf at all", content_type="application/pdf"), "content_mismatch"),
        (httpx.Response(500, content=b"boom"), "http_status"),
    ],
)
def test_a_bad_response_never_masquerades_as_a_source_document(runtime_database, store, route, reason_fragment):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    doc_url = "https://docs.example.gov/bad.pdf"
    _authorize_in(factory, project_id, url=doc_url)
    routes = {INDEX_URL: _index([]), doc_url: route}

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 0 and result.fetch_failures == 1
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Document).where(Document.project_id == project_id)) == 0
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.project_id == project_id))
        assert attempt.outcome == "failed" and reason_fragment in attempt.reason
        ref = s.scalar(select(DiscoveredReference).where(DiscoveredReference.project_id == project_id))
        assert ref.state == "authorized"              # not registered; retained for retry


def test_a_failed_attempt_is_retained_beside_a_later_success(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    doc_url = "https://docs.example.gov/flaky.pdf"
    _authorize_in(factory, project_id, url=doc_url)

    down = {INDEX_URL: _index([]), doc_url: httpx.Response(503, content=b"down")}
    discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(), client=_client(down), clock=FixedClock())
    up = {INDEX_URL: _index([]), doc_url: _response(_pdf())}
    discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(), client=_client(up), clock=FixedClock())

    with factory() as s:
        outcomes = s.scalars(
            select(SourceFetchAttempt.outcome).where(SourceFetchAttempt.project_id == project_id)
            .order_by(SourceFetchAttempt.id)
        ).all()
        assert outcomes == ["failed", "registered"]   # both attempts preserved, ordered


def test_an_unauthorized_redirect_is_refused_and_an_authorized_one_is_followed(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    off = "https://docs.example.gov/leaves.pdf"
    inbound = "https://docs.example.gov/redirects.pdf"
    target = "https://docs.example.gov/real.pdf"
    _authorize_in(factory, project_id, url=off)
    _authorize_in(factory, project_id, url=inbound)
    routes = {
        INDEX_URL: _index([]),
        off: httpx.Response(302, headers={"location": "https://evil.example.net/x.pdf"}),
        inbound: httpx.Response(302, headers={"location": target}),
        target: _response(_pdf()),
    }
    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 1 and result.fetch_failures == 1
    with factory() as s:
        failed = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.outcome == "failed"))
        assert "unauthorized_host" in failed.reason


# --- Archives & budgets -----------------------------------------------------


def test_an_archive_member_is_extracted_and_registered(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    archive_url = "https://docs.example.gov/2026-08.zip"
    member = "utilities.pdf"
    body = _pdf("member matrix")
    _authorize_in(factory, project_id, url=archive_url, member=member)
    routes = {INDEX_URL: _index([]), archive_url: _response(_zip({member: body}), content_type="application/zip")}

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 1
    with factory() as s:
        document = s.scalar(select(Document).where(Document.project_id == project_id))
        assert document.sha256 == hashlib.sha256(body).hexdigest()


def test_a_missing_archive_member_does_not_masquerade_as_a_document(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    archive_url = "https://docs.example.gov/2026-08.zip"
    _authorize_in(factory, project_id, url=archive_url, member="absent.pdf")
    routes = {INDEX_URL: _index([]), archive_url: _response(_zip({"other.pdf": _pdf()}), content_type="application/zip")}

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 0 and result.fetch_failures == 1
    with factory() as s:
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.project_id == project_id))
        assert attempt.outcome == "failed" and "member_not_found" in attempt.reason


def test_nested_archive_depth_budget_stops_before_fetch(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    archive_url = "https://docs.example.gov/outer.zip"
    _authorize_in(factory, project_id, url=archive_url, member="inner.zip::deep.pdf")
    fetched: list[str] = []

    def record(request):
        fetched.append(str(request.url))
        return _response(_zip({"inner.zip": _zip({"deep.pdf": _pdf()})}), content_type="application/zip")

    client = _client({INDEX_URL: _index([]), archive_url: record})
    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(nested_archive_depth=1),
                                  client=client, clock=FixedClock())
    assert result.registered == 0 and result.fetch_failures == 1
    assert fetched == []                               # depth is refused before any download
    with factory() as s:
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.project_id == project_id))
        assert "archive_too_deep" in attempt.reason


def test_member_decompressed_size_budget_is_enforced(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    archive_url = "https://docs.example.gov/big.zip"
    member = "huge.pdf"
    big = _pdf() + b"\0" * (2 * 1024 * 1024)
    _authorize_in(factory, project_id, url=archive_url, member=member)
    routes = {INDEX_URL: _index([]), archive_url: _response(_zip({member: big}), content_type="application/zip")}

    result = discover_and_process(
        factory, scope=_scope(project_id),
        budgets=_budgets(max_decompressed_bytes=1024 * 1024, max_compressed_bytes=64 * 1024 * 1024),
        client=_client(routes), clock=FixedClock(),
    )
    assert result.registered == 0 and result.fetch_failures == 1
    with factory() as s:
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.project_id == project_id))
        assert "member_too_large" in attempt.reason


def test_archive_compressed_size_budget_refuses_an_oversized_download(runtime_database, store):
    import os

    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    archive_url = "https://docs.example.gov/oversized.zip"
    _authorize_in(factory, project_id, url=archive_url, member="x.pdf")
    oversized = os.urandom(2 * 1024 * 1024)          # incompressible, > 1 MiB budget
    routes = {INDEX_URL: _index([]), archive_url: _response(oversized, content_type="application/zip")}

    result = discover_and_process(
        factory, scope=_scope(project_id), budgets=_budgets(max_compressed_bytes=1024 * 1024),
        client=_client(routes), clock=FixedClock(),
    )
    assert result.registered == 0 and result.fetch_failures == 1
    with factory() as s:
        attempt = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.project_id == project_id))
        assert "archive_too_large" in attempt.reason


def test_document_budget_exhaustion_is_a_retained_resumable_failure(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    urls = [f"https://docs.example.gov/{i}.pdf" for i in range(3)]
    for u in urls:
        _authorize_in(factory, project_id, url=u)
    routes = {INDEX_URL: _index([])}
    for u in urls:
        routes[u] = _response(_pdf(u))

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(max_documents=1),
                                  client=_client(routes), clock=FixedClock())
    assert result.registered == 1 and result.budget_exhausted == 1
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Document).where(Document.project_id == project_id)) == 1
        exhausted = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.outcome == "budget_exhausted"))
        assert exhausted.resumable is True and "document_budget" in exhausted.reason
        # The remaining authorized references are held, ready for a resumed pass.
        assert s.scalar(select(func.count()).select_from(DiscoveredReference).where(
            DiscoveredReference.project_id == project_id, DiscoveredReference.state == "authorized")) == 2


def test_enumeration_budget_truncates_discovery_and_is_recorded(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    entries = [{"url": f"https://docs.example.gov/{i}.pdf"} for i in range(5)]
    routes = {INDEX_URL: _index(entries)}

    result = discover_and_process(factory, scope=_scope(project_id), budgets=_budgets(max_references=2),
                                  client=_client(routes), clock=FixedClock())
    assert result.references_observed == 2 and result.budget_exhausted == 1
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(DiscoveredReference).where(
            DiscoveredReference.project_id == project_id)) == 2
        exhausted = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.outcome == "budget_exhausted"))
        assert "enumeration_budget" in exhausted.reason and exhausted.resumable is True


def test_time_budget_exhaustion_holds_remaining_work(runtime_database, store):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    _authorize_in(factory, project_id, url="https://docs.example.gov/slow.pdf")
    routes = {INDEX_URL: _index([]), "https://docs.example.gov/slow.pdf": _response(_pdf())}

    result = discover_and_process(
        factory, scope=_scope(project_id), budgets=_budgets(time_budget_seconds=1),
        client=_client(routes), clock=AdvancingClock(NOW, step_seconds=5),
    )
    assert result.registered == 0 and result.budget_exhausted == 1
    with factory() as s:
        exhausted = s.scalar(select(SourceFetchAttempt).where(SourceFetchAttempt.outcome == "budget_exhausted"))
        assert "time_budget" in exhausted.reason and exhausted.resumable is True


# --- Gate-7 configuration and the Due Work runtime --------------------------


def test_configuration_is_gate7_validated_and_refuses_bad_scope(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)

    def declare(**overrides):
        base = dict(
            project_id=project_id, configuration_version="loc-v1", location_id=LOCATION,
            adapter_identity="http-index-v1", source_manifest_id="txdot-manifest",
            index_url=INDEX_URL, authorized_hosts=(HOST,), starts_at=NOW,
        )
        base.update(overrides)
        return LocationDiscoveryDeclaration.released_hourly(**base)

    with factory() as s:
        with s.begin():
            schedule = configure_location_discovery(s, declare(), now=NOW)
            assert schedule.handler_key == HANDLER_LOCATION_DISCOVERY
            assert schedule.disabled_at is None
            assert schedule.scope_json["sealed"] is False

    # An index url whose host is not authorized is refused (no schedule written).
    with factory() as s:
        with s.begin():
            with pytest.raises(DueWorkRefusal):
                configure_location_discovery(
                    s, declare(index_url="https://elsewhere.example.net/index.json"), now=NOW
                )
    # An empty authorized host set is refused.
    with factory() as s:
        with s.begin():
            with pytest.raises(DueWorkRefusal):
                configure_location_discovery(s, declare(authorized_hosts=()), now=NOW)


def test_enqueue_validates_the_persisted_location_schedule(runtime_database):
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    with factory() as s:
        with s.begin():
            configure_location_discovery(
                s,
                LocationDiscoveryDeclaration.released_hourly(
                    project_id=project_id, configuration_version="loc-v1", location_id=LOCATION,
                    adapter_identity="http-index-v1", source_manifest_id="txdot-manifest",
                    index_url=INDEX_URL, authorized_hosts=(HOST,), starts_at=NOW,
                ),
                now=NOW,
            )
    # enqueue_due_work re-validates every stored schedule; a valid one enqueues cleanly.
    with factory() as s:
        with s.begin():
            occurrences = enqueue_due_work(s, now=NOW + timedelta(hours=1))
    assert any(o.public_id for o in occurrences)


def test_due_work_runs_the_sealed_location_handler_end_to_end(runtime_database, store):
    # A sealed schedule proves the full runtime path (claim -> handler -> receipt)
    # with no network at all: the handler refuses before any fetch.
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    with factory() as s:
        with s.begin():
            configure_location_discovery(
                s,
                LocationDiscoveryDeclaration.released_hourly(
                    project_id=project_id, configuration_version="loc-v1", location_id=LOCATION,
                    adapter_identity="http-index-v1", source_manifest_id="txdot-manifest",
                    index_url=INDEX_URL, authorized_hosts=(HOST,), sealed=True, starts_at=NOW,
                ),
                now=NOW,
            )
    with factory() as s:
        with s.begin():
            enqueue_due_work(s, now=NOW + timedelta(hours=1))
    result = run_due_work_once(factory, clock=FixedClock(NOW + timedelta(hours=1)), owner="runtime:test")
    assert result is not None
    assert result.handler_key == HANDLER_LOCATION_DISCOVERY
    assert result.execution_outcome == "completed"
    assert result.handler_result["health"] == "held_sealed"
    assert result.handler_result["sealed_excluded"] is True
    assert result.safe_next_step == "none"


def test_due_work_handler_fetches_and_registers_through_the_runtime(runtime_database, store, monkeypatch):
    # Prove the handler's own path registers a document, with the network faked at
    # the httpx.Client boundary (no live account, ADR-0046).
    factory = runtime_database.session_factory
    project_id = _project_in(factory)
    doc_url = "https://docs.example.gov/ucm.pdf"
    body = _pdf("through the runtime")
    key = _authorize_in(factory, project_id, url=doc_url)
    routes = {INDEX_URL: _index([]), doc_url: _response(body)}

    real_client = httpx.Client
    real_mock_transport = httpx.MockTransport

    def fake_client(**kwargs):
        def handler(request):
            route = routes.get(str(request.url))
            return route if route is not None else httpx.Response(404)
        return real_client(transport=real_mock_transport(handler))

    monkeypatch.setattr(httpx, "Client", fake_client)
    with factory() as s:
        with s.begin():
            configure_location_discovery(
                s,
                LocationDiscoveryDeclaration.released_hourly(
                    project_id=project_id, configuration_version="loc-v1", location_id=LOCATION,
                    adapter_identity="http-index-v1", source_manifest_id="txdot-manifest",
                    index_url=INDEX_URL, authorized_hosts=(HOST,), starts_at=NOW,
                ),
                now=NOW,
            )
        with s.begin():
            enqueue_due_work(s, now=NOW + timedelta(hours=1))
    result = run_due_work_once(factory, clock=FixedClock(NOW + timedelta(hours=1)), owner="runtime:test")
    assert result is not None and result.execution_outcome == "completed"
    assert result.handler_result["registered"] == 1
    with factory() as s:
        document = s.scalar(select(Document).where(Document.project_id == project_id))
        assert document is not None and document.sha256 == hashlib.sha256(body).hexdigest()
        ref = s.scalar(select(DiscoveredReference).where(DiscoveredReference.reference_key == key))
        assert ref.state == "registered"
