"""M8 acceptance: capture pinning, publication, and preconditions.

Split from the replay suite so the two run on separate CI runners (#548).
None of these tests uses the module-scoped provisioning, which is what makes
this split free rather than a second provisioning cost.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import pymupdf
import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.slow

import corridor.m8_acceptance as m8_acceptance_module
import corridor.m8_acceptance_controlled as m8_acceptance_controlled_module
from corridor.config import settings
from corridor.db import engine
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

from m8_acceptance_support import (  # noqa: F401
    _Capture,
    _canonical_json,
    _capture_extractor,
    _capture_fixture,
    _database_exists,
    _lock_record,
    _read_export,
    _run_config,
    _sha256,
    _stub_capture_harness,
    _write_pdf,
    _write_source_lock,
)


def test_capture_pins_the_rid_and_five_fresh_exact_run_snapshots(tmp_path):
    lock_path = _write_source_lock(tmp_path / "sources")

    capture = capture_m8_fixture(
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

    fixture = json.loads(capture.fixture_path.read_text())
    assert capture.fixture_sha256 == fixture["fixture_sha256"]
    assert fixture["content"]["rid_index"]["registry_id"] == (
        "nhhip-rid-index-2026-05-01"
    )
    assert [run["registry_id"] for run in fixture["content"]["runs"]] == list(
        REVISION_IDS
    )
    assert [run["candidate_count"] for run in fixture["content"]["runs"]] == [
        1,
        1,
        1,
        1,
        1,
    ]
    assert all(run["candidate_inputs"] for run in fixture["content"]["runs"])
    assert all(run["capture_run_id"] > 0 for run in fixture["content"]["runs"])
    assert len(fixture["content"]["comparisons"]) == 4
    assert fixture["content"]["selected_seed"]["selection_sha256"]
    assert fixture["content"]["claim_boundary"] == CLAIM_BOUNDARY
    for source in [
        fixture["content"]["rid_index"],
        *fixture["content"]["sources"],
    ]:
        assert "local_path" not in source
        assert not Path(source["fixture_relpath"]).is_absolute()
        assert (capture.fixture_path.parent / source["fixture_relpath"]).is_file()
    assert capture.database_name.startswith("corridor_m8_acceptance_")
    assert not _database_exists(capture.database_name)


def test_authoritative_capture_requires_a_clean_revision_pin(tmp_path):
    lock_path = _write_source_lock(tmp_path / "sources")

    with pytest.raises(
        AcceptanceError,
        match="authoritative capture requires an expected clean Git revision",
    ):
        capture_m8_fixture(
            AcceptanceCaptureConfig(
                source_lock_path=lock_path,
                output_dir=tmp_path / "capture",
                postgres_admin_url=settings.database_url,
                prompt_version=PROMPT_VERSION,
                expected_model=MODEL,
                schema_version=SCHEMA_VERSION,
                expected_clean_git_revision=None,
            ),
            extract=_capture_extractor,
        )


def test_authoritative_capture_accepts_a_matching_clean_revision_pin(
    monkeypatch, tmp_path
):
    seen = _stub_capture_harness(
        monkeypatch, tmp_path, git_state={"revision": "abc123", "status": "clean"}
    )

    summary = capture_m8_fixture(
        AcceptanceCaptureConfig(
            source_lock_path=tmp_path / "sources.lock.json",
            output_dir=tmp_path / "capture",
            postgres_admin_url=settings.database_url,
            prompt_version=PROMPT_VERSION,
            expected_model=MODEL,
            schema_version=SCHEMA_VERSION,
            expected_clean_git_revision="abc123",
        ),
        extract=_capture_extractor,
        provision_database=seen["provision_database"],
    )

    assert summary.fixture_path == tmp_path / "capture" / "fixture.json"
    assert summary.run_count == 1
    assert summary.candidate_count == 1
    assert seen["capture_git_state"] == {
        "revision": "abc123",
        "status": "clean",
    }
    fixture = json.loads(summary.fixture_path.read_text())
    assert fixture["content"]["capture"]["authoritative"] is True


def test_authoritative_capture_rejects_revision_mismatch_before_provisioning(
    monkeypatch, tmp_path
):
    seen = _stub_capture_harness(
        monkeypatch, tmp_path, git_state={"revision": "def456", "status": "clean"}
    )

    with pytest.raises(
        AcceptanceError, match="capture Git revision does not match the pin"
    ):
        capture_m8_fixture(
            AcceptanceCaptureConfig(
                source_lock_path=tmp_path / "sources.lock.json",
                output_dir=tmp_path / "capture",
                postgres_admin_url=settings.database_url,
                prompt_version=PROMPT_VERSION,
                expected_model=MODEL,
                schema_version=SCHEMA_VERSION,
                expected_clean_git_revision="abc123",
            ),
            extract=_capture_extractor,
            provision_database=seen["provision_database"],
        )

    assert seen["provisioned"] is False


def test_authoritative_capture_rejects_a_dirty_repository_before_provisioning(
    monkeypatch, tmp_path
):
    seen = _stub_capture_harness(
        monkeypatch, tmp_path, git_state={"revision": "abc123", "status": "dirty"}
    )

    with pytest.raises(
        AcceptanceError, match="authoritative capture requires a clean repository"
    ):
        capture_m8_fixture(
            AcceptanceCaptureConfig(
                source_lock_path=tmp_path / "sources.lock.json",
                output_dir=tmp_path / "capture",
                postgres_admin_url=settings.database_url,
                prompt_version=PROMPT_VERSION,
                expected_model=MODEL,
                schema_version=SCHEMA_VERSION,
                expected_clean_git_revision="abc123",
            ),
            extract=_capture_extractor,
            provision_database=seen["provision_database"],
        )

    assert seen["provisioned"] is False


def test_capture_pins_schema_independently_from_prompt(tmp_path):
    lock_path = _write_source_lock(tmp_path / "sources")

    capture = capture_m8_fixture(
        AcceptanceCaptureConfig(
            source_lock_path=lock_path,
            output_dir=tmp_path / "capture",
            postgres_admin_url=settings.database_url,
            prompt_version=PROMPT_VERSION,
            expected_model=MODEL,
            schema_version="candidate-shape-v2",
            expected_clean_git_revision=None,
            unsafe_allow_unpinned_test_capture=True,
        ),
        extract=_capture_extractor,
    )

    fixture = json.loads(capture.fixture_path.read_text())
    assert {run["prompt_version"] for run in fixture["content"]["runs"]} == {
        PROMPT_VERSION
    }
    assert {run["schema_version"] for run in fixture["content"]["runs"]} == {
        "candidate-shape-v2"
    }


def test_capture_publication_is_first_write_only(monkeypatch, tmp_path):
    seen = _stub_capture_harness(monkeypatch, tmp_path)

    first = capture_m8_fixture(
        AcceptanceCaptureConfig(
            source_lock_path=tmp_path / "sources.lock.json",
            output_dir=tmp_path / "capture",
            postgres_admin_url=settings.database_url,
            prompt_version=PROMPT_VERSION,
            expected_model=MODEL,
            schema_version=SCHEMA_VERSION,
            expected_clean_git_revision=None,
            unsafe_allow_unpinned_test_capture=True,
        ),
        extract=_capture_extractor,
        provision_database=seen["provision_database"],
    )
    original_bytes = first.fixture_path.read_bytes()

    with pytest.raises(
        AcceptanceError, match="fixture output directory already exists"
    ):
        capture_m8_fixture(
            AcceptanceCaptureConfig(
                source_lock_path=tmp_path / "sources.lock.json",
                output_dir=tmp_path / "capture",
                postgres_admin_url=settings.database_url,
                prompt_version=PROMPT_VERSION,
                expected_model=MODEL,
                schema_version=SCHEMA_VERSION,
                expected_clean_git_revision=None,
                unsafe_allow_unpinned_test_capture=True,
            ),
            extract=_capture_extractor,
            provision_database=seen["provision_database"],
        )

    assert first.fixture_path.read_bytes() == original_bytes


def test_capture_publication_cleans_up_a_failed_stage(monkeypatch, tmp_path):
    def write_sources_then_fail(_chain, output_dir: Path):
        sources = output_dir / "sources"
        sources.mkdir(parents=True)
        (sources / "partial.txt").write_text("partial")
        raise RuntimeError("injected publication failure")

    seen = _stub_capture_harness(
        monkeypatch,
        tmp_path,
        write_sources=write_sources_then_fail,
    )

    with pytest.raises(RuntimeError, match="injected publication failure"):
        capture_m8_fixture(
            AcceptanceCaptureConfig(
                source_lock_path=tmp_path / "sources.lock.json",
                output_dir=tmp_path / "capture",
                postgres_admin_url=settings.database_url,
                prompt_version=PROMPT_VERSION,
                expected_model=MODEL,
                schema_version=SCHEMA_VERSION,
                expected_clean_git_revision=None,
                unsafe_allow_unpinned_test_capture=True,
            ),
            extract=_capture_extractor,
            provision_database=seen["provision_database"],
        )

    assert not (tmp_path / "capture").exists()
    assert list(tmp_path.glob(".corridor-m8-capture-*")) == []


def test_rid_filename_and_replacement_date_must_be_on_the_same_row(tmp_path):
    lock_path = _write_source_lock(
        tmp_path / "sources",
        rid_replacement_dates=(
            "10/31/2025",
            "7/22/2025",
            "12/15/2025",
            "2/13/2026",
        ),
    )

    with pytest.raises(CorruptAcceptanceFixture, match="RID row"):
        capture_m8_fixture(
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


def test_acceptance_requires_postgres_16():
    _require_postgres_16("16.14")

    with pytest.raises(AcceptanceError, match="PostgreSQL 16"):
        _require_postgres_16("17.2")


def test_acceptance_rejects_remote_postgres_hosts():
    for host in (None, "localhost", "127.0.0.1", "::1"):
        _require_local_postgres_host(host)

    with pytest.raises(AcceptanceError, match="local PostgreSQL"):
        _require_local_postgres_host("prod-db.internal")


def test_synthetic_matrix_pages_do_not_fall_through_to_ocr(tmp_path):
    lock_path = _write_source_lock(tmp_path / "sources")
    sources = json.loads(lock_path.read_text())["sources"].values()

    for source in sources:
        if source["doc_type"] != "matrix":
            continue
        with pymupdf.open(source["local_path"]) as document:
            assert route_page(inventory_page(document[0])).page_mode == "native"


def test_transformations_reject_unknown_nested_behavior(tmp_path):
    transformations = json.loads(TRANSFORMATIONS.read_text())
    transformations["cases"][0]["successor"]["citation_shape"] = "guess"
    path = tmp_path / "controlled.json"
    path.write_bytes(_canonical_json(transformations))

    with pytest.raises(AcceptanceError, match="citation_shape"):
        _load_transformations(path, expected_sha256=_sha256(path.read_bytes()))

