"""M8 acceptance: model-free replay identity and the real-chain contradiction.

Split from the bundle and controlled-lane suites so the three run on separate
CI runners (#548). One 1,263-line module held 258 of the slow gate's 381
seconds and a file cannot span runners, so it set the gate's floor alone.

Both tests here replay the captured real chain into their own disposable
database, so they share only `replay_capture` and never build the baseline
bundle.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow

import corridor.m8_acceptance as m8_acceptance_module
from corridor.m8_acceptance import (
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)

from m8_acceptance_support import (
    CLAIM_BOUNDARY,
    REVISION_IDS,
    _capture_fixture,
    _database_exists,
    _read_export,
    _run_config,
)


@pytest.fixture(scope="module")
def replay_capture(tmp_path_factory):
    return _capture_fixture(tmp_path_factory.mktemp("m8-replay-capture"))


def test_equivalent_model_free_replays_have_one_normalized_identity(
    replay_capture,
    tmp_path,
):
    first = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle-a"))
    second = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle-b"))

    assert (
        first.fixture_sha256 == second.fixture_sha256 == replay_capture.fixture_sha256
    )
    assert first.canonical_content_sha256 == second.canonical_content_sha256
    assert len(first.canonical_content_sha256) == 64
    assert (
        verify_m8_acceptance_bundle(
            first.bundle_dir,
            expected_integrity_manifest_sha256=first.integrity_manifest_sha256,
        ).valid
        is True
    )
    assert (
        verify_m8_acceptance_bundle(
            second.bundle_dir,
            expected_integrity_manifest_sha256=second.integrity_manifest_sha256,
        ).valid
        is True
    )
    assert all(assertion.passed for assertion in first.assertions)
    real = _read_export(first, "real-chain.json")
    assert [item["registry_id"] for item in real["documents"]] == [
        "nhhip-rid-index-2026-05-01",
        *REVISION_IDS,
    ]
    assert len(real["runs"]) == 5
    assert len(real["active_run_declarations"]) == 5
    assert len(real["comparisons"]) == 4
    assert real["ledger_counts"] == {
        "dependencies": 0,
        "assertions": 0,
        "evidence_links": 0,
        "operative_support": 0,
        "carry_forward_receipts": 0,
    }
    assert real["claims"] == CLAIM_BOUNDARY
    exported_text = "".join(
        path.read_text() for path in first.bundle_dir.iterdir() if path.is_file()
    )
    assert "postgresql://" not in exported_text
    assert "postgresql+psycopg://" not in exported_text
    assert first.database_name != second.database_name
    assert not _database_exists(first.database_name)
    assert not _database_exists(second.database_name)


def test_real_replay_contradiction_is_exported_in_a_verifiable_failed_bundle(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_module._captured_extractor

    def drifted_extractor(run_record):
        extract = original(run_record)

        def wrapped(session, document):
            candidates = extract(session, document)
            if run_record["registry_id"] == REVISION_IDS[0]:
                fields = candidates[0].payload_json["fields"]
                fields["external_org"] = "Replay Drift Utility"
            return candidates

        return wrapped

    monkeypatch.setattr("corridor.m8_acceptance._captured_extractor", drifted_extractor)

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "failed-bundle"))

    assert summary.carried_count == 0
    assert [assertion.name for assertion in summary.assertions] == [
        "real_chain_replay_exact_inputs_match_capture"
    ]
    assert [assertion.passed for assertion in summary.assertions] == [False]
    assert (
        verify_m8_acceptance_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        ).valid
        is True
    )
    exported_real = _read_export(summary, "real-chain.json")
    assert exported_real["replay_failure"]["name"] == (
        "real_chain_replay_exact_inputs_match_capture"
    )
    assert exported_real["replay_failure"]["observed"]["registry_id"] == REVISION_IDS[0]
    assert exported_real["runs"] == []
    exported_assertions = _read_export(summary, "assertions.json")
    assert exported_assertions["assertions"][0]["detail"] == (
        f"replayed exact inputs drifted for {REVISION_IDS[0]}"
    )
