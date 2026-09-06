"""Shared capture, replay, and bundle fixtures for the M8 acceptance suites.

The acceptance tests were one 1,263-line module holding 258 of the slow
gate's 381 seconds. A single file cannot be split across CI runners, so it
set the gate's floor on its own (#548).

The split is drawn around the module-scoped ``replay_capture`` and
``baseline_acceptance`` fixtures each suite declares over ``_capture_fixture``
and ``_run_config`` here. Those provision once per module, so a further split
costs about 7s of capture and, where the baseline bundle is wanted, about 12s
of replay. The three suites carry roughly 30 to 40 seconds of work each, which
earns that back several times over.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.slow

import corridor.m8_acceptance as m8_acceptance_module
import corridor.m8_acceptance_controlled as m8_acceptance_controlled_module
from corridor.config import settings
from corridor.page_inventory import inventory_page, route_page
from corridor.m8_acceptance import (
    AcceptanceCaptureConfig,
    AcceptanceError,
    AcceptanceRunConfig,
    AssertionResult,
    CorruptAcceptanceBundle,
    CorruptAcceptanceFixture,
    ProvisionedDatabase,
    _require_local_postgres_host,
    _acceptance_assertions,
    _load_transformations,
    _require_postgres_16,
    capture_m8_fixture,
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)
from corridor.models import Candidate, DocPage
from corridor.revision_comparison import DEFAULT_MATCHER_VERSION

from pdf_fixture_support import PdfFixture


PROMPT_VERSION = "m8-controlled-capture-v1"
SCHEMA_VERSION = "m8-controlled-candidate-shape-v1"
MODEL = "deterministic-capture-fixture-v1"
SEED_QUOTE = "SEED-1 Controlled Utility Telecom 100+00 IH-45 captured source row"
REVISION_IDS = (
    "nhhip-ucm-2025-06-20",
    "nhhip-ucm-2025-07-22",
    "nhhip-ucm-2025-10-24",
    "nhhip-ucm-2025-12-15",
    "nhhip-ucm-2026-02-13",
)
TRANSFORMATIONS = (
    Path(__file__).parent
    / "fixtures"
    / "m8_acceptance"
    / "controlled_transformations.json"
)
CLAIM_BOUNDARY = {
    "mechanical_correctness_only": True,
    "semantic_correctness": False,
    "recall": False,
    "human_review": False,
    "production_readiness": False,
    "independent_customer_validation": False,
}

class _Capture:
    def __init__(self, fixture_path: Path, fixture_sha256: str):
        self.fixture_path = fixture_path
        self.fixture_sha256 = fixture_sha256


def _stub_capture_harness(
    monkeypatch,
    tmp_path: Path,
    *,
    git_state: dict[str, str] | None = None,
    write_sources=None,
):
    seen = {
        "capture_git_state": None,
        "provisioned": False,
    }

    monkeypatch.setattr(
        "corridor.m8_acceptance._load_source_chain", lambda _path: object()
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance._git_state",
        lambda: git_state or {"revision": "test-revision", "status": "clean"},
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance._write_fixture_sources",
        write_sources or (lambda _chain, _output_dir: None),
    )

    def fake_capture_chain(
        _session,
        *,
        git_state,
        **_kwargs,
    ):
        seen["capture_git_state"] = git_state
        return {
            "capture": {},
            "runs": [
                {
                    "candidate_count": 1,
                }
            ],
        }

    monkeypatch.setattr("corridor.m8_acceptance._capture_chain", fake_capture_chain)

    @contextmanager
    def session_factory():
        class FakeSession:
            def commit(self):
                return None

        yield FakeSession()

    @contextmanager
    def provision_database(_admin_url: str):
        seen["provisioned"] = True
        yield type(
            "FakeDatabase",
            (),
            {
                "name": "corridor_m8_acceptance_" + "0" * 32,
                "session_factory": lambda self=None: session_factory(),
                "postgres_version": "16.10",
                "migration_head": "head",
            },
        )()

    seen["provision_database"] = provision_database
    return seen


def _capture_fixture(tmp_path: Path) -> _Capture:
    lock_path = _write_source_lock(tmp_path / "sources")
    result = capture_m8_fixture(
        AcceptanceCaptureConfig(
            source_lock_path=lock_path,
            output_dir=tmp_path / "capture",
            postgres_admin_url=settings.database_url,
            prompt_version=PROMPT_VERSION,
            expected_model=MODEL,
            schema_version=SCHEMA_VERSION,
            expected_clean_git_revision=None,
            unsafe_allow_unpinned_test_capture=True,
        ),
        extract=_capture_extractor,
    )
    return _Capture(result.fixture_path, result.fixture_sha256)


def _run_config(capture: _Capture, output_dir: Path) -> AcceptanceRunConfig:
    return AcceptanceRunConfig(
        fixture_path=capture.fixture_path,
        transformations_path=TRANSFORMATIONS,
        output_dir=output_dir,
        postgres_admin_url=settings.database_url,
        expected_fixture_sha256=capture.fixture_sha256,
        expected_transformations_sha256=_sha256(TRANSFORMATIONS.read_bytes()),
        expected_clean_git_revision=None,
    )


def _capture_extractor(session, document):
    quote = SEED_QUOTE
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.text.contains(quote),
        )
    )
    assert page is not None
    candidate = Candidate(
        project_id=document.project_id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "SEED-1",
                "external_org": "Controlled Utility",
                "utility_type": "Telecom",
                "station_from": "100+00",
                "baseline": "IH-45",
                "location": "captured row shape",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": page.page_no,
                    "quote": quote,
                    "verified": True,
                    "whole_row": True,
                }
            ],
            "unverified_fields": [],
            "unmapped_columns": [],
            "low_confidence_tokens": [],
            "tier": "structure",
            "dedupe_hint": "SEED-1",
            "text_source": "text_layer",
        },
        source_document_id=document.id,
        source_pages=[page.page_no],
        confidence=1.0,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        citations_verified=True,
    )
    session.add(candidate)
    session.flush([candidate])
    return [candidate]


def _write_source_lock(
    directory: Path,
    *,
    rid_replacement_dates: tuple[str, str, str, str] = (
        "7/22/2025",
        "10/31/2025",
        "12/15/2025",
        "2/13/2026",
    ),
) -> Path:
    directory.mkdir(parents=True)
    quote = SEED_QUOTE
    filenames = tuple(f"{registry_id}.pdf" for registry_id in REVISION_IDS)
    index = _write_pdf(
        directory / "rid-index.pdf",
        identity="nhhip-rid-index-2026-05-01",
        pages=[
            ["RID index page 1"],
            ["RID index page 2"],
            ["RID index page 3"],
            [
                "RID index page 4",
                f"{filenames[0]} replaced on {rid_replacement_dates[0]}",
                f"{filenames[1]} replaced on {rid_replacement_dates[1]}",
                f"{filenames[2]} replaced on {rid_replacement_dates[2]}",
                f"{filenames[3]} replaced on {rid_replacement_dates[3]}",
                filenames[4],
            ],
        ],
    )
    dates = (
        date(2025, 6, 20),
        date(2025, 7, 22),
        date(2025, 10, 24),
        date(2025, 12, 15),
        date(2026, 2, 13),
    )
    # Five revisions print the same row. Each is its own document with its
    # own digest, so the identity has to be declared: the builder's bytes are
    # a function of content alone, and nothing here may rely on a writer
    # happening to stamp files differently.
    matrices = [
        _write_pdf(directory / f"{registry_id}.pdf", [[quote]], identity=registry_id)
        for registry_id in REVISION_IDS
    ]
    sources = {
        "https://example.test/rid-index.pdf": _lock_record(
            index,
            registry_id="nhhip-rid-index-2026-05-01",
            doc_type="other",
            doc_date="2026-05-01",
        )
    }
    for ordinal, (registry_id, matrix, doc_date) in enumerate(
        zip(REVISION_IDS, matrices, dates, strict=True)
    ):
        record = _lock_record(
            matrix,
            registry_id=registry_id,
            doc_type="matrix",
            doc_date=doc_date.isoformat(),
        )
        if ordinal < len(REVISION_IDS) - 1:
            replacement_dates = (
                "2025-07-22",
                "2025-10-31",
                "2025-12-15",
                "2026-02-13",
            )
            record["supersession"] = {
                "predecessor_registry_id": registry_id,
                "successor_registry_id": REVISION_IDS[ordinal + 1],
                "replacement_date": replacement_dates[ordinal],
                "source_registry_id": "nhhip-rid-index-2026-05-01",
                "source_page": 4,
            }
        sources[f"https://example.test/{registry_id}.pdf"] = record
    lock_path = directory / "manifest.lock.json"
    lock_path.write_bytes(_canonical_json({"sources": sources}))
    return lock_path


def _lock_record(path, *, registry_id, doc_type, doc_date):
    return {
        "bytes": path.stat().st_size,
        "content_type": "application/pdf",
        "doc_date": doc_date,
        "doc_type": doc_type,
        "history": [],
        "http_status": 200,
        "local_path": str(path.resolve()),
        "registry_id": registry_id,
        "retrieved_at": "2026-08-06T00:00:00+00:00",
        "role": "evidence" if doc_type == "other" else "spine",
        "sha256": _sha256(path.read_bytes()),
        "title": registry_id,
    }


def _write_pdf(path: Path, pages: list[list[str]], *, identity: str) -> Path:
    fixture = PdfFixture(identity=identity)
    for lines in pages:
        page = fixture.add_page(width=792, height=612)
        for line_no, line in enumerate(lines, start=1):
            page.text((36, 36 + line_no * 20), line, fontsize=10)
    return fixture.save(path)


def _read_export(summary, relative_path):
    return json.loads((summary.bundle_dir / relative_path).read_text())


def _database_exists(database_name: str) -> bool:
    # Imported here, not at module scope: conftest re-exports the fixtures
    # below, and a module-level import would bind the engine to the
    # configured database before the harness assigns this worker its own.
    from corridor.db import engine

    with engine.connect() as connection:
        return bool(
            connection.scalar(
                text("select exists(select 1 from pg_database where datname=:name)"),
                {"name": database_name},
            )
        )


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

