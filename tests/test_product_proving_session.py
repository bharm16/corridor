"""Final publication preserves the exact two-pass database transition chain."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.product_proving_database import DatabaseFingerprint
from corridor.product_proving_execution import GitCheckoutObservation
from corridor.product_proving_run import ExpectedPreflight, ObservedPreflight
from corridor.product_proving_session import (
    ObservedProductProvingPublicationConfig,
    publish_observed_product_proving_session,
)


SOURCE = {
    "backend": "postgresql",
    "host": "localhost",
    "port": 5433,
    "database": "corridor",
    "username": "local",
}
CONNECTION = {
    "database": "corridor",
    "username": "local",
    "server_address": "127.0.0.1",
    "server_port": "5432",
    "system_identifier": "123456789",
    "postgres_version": "16.15",
}


def _expected() -> ExpectedPreflight:
    return ExpectedPreflight(
        source_revision="a" * 40,
        origin_main_revision="b" * 40,
        migration_head="b317c5d7e9f2",
        policy_digests={"dependency-admission": "c" * 64},
        documents={1311: "d" * 64},
        baseline_runs={1311: 1},
        milestone_sources={"DESIGN": "e" * 64},
        baseline_fingerprint="f" * 64,
    )


def _observed(expected: ExpectedPreflight) -> ObservedPreflight:
    return ObservedPreflight(
        source_revision=expected.source_revision,
        origin_main_revision=expected.origin_main_revision,
        clean_worktree=True,
        migration_head=expected.migration_head,
        policy_digests=expected.policy_digests,
        documents=expected.documents,
        baseline_runs=expected.baseline_runs,
        milestone_sources=expected.milestone_sources,
        baseline_fingerprint=expected.baseline_fingerprint,
    )


def _evidence():
    expected = _expected()
    observed = _observed(expected)
    fingerprint = DatabaseFingerprint((), (), expected.baseline_fingerprint)
    baseline = SimpleNamespace(
        manifest_sha256="1" * 64,
        dump_sha256="2" * 64,
        fingerprint=fingerprint,
        baseline={
            "checkout": {
                "revision": expected.source_revision,
                "migration_head": expected.migration_head,
            },
            "source_database": SOURCE,
            "source_connection": CONNECTION,
        },
    )
    restore_one_manifest = "9" * 64
    restore_one_canonical = "a" * 64
    restore_one_id = "11111111-1111-4111-8111-111111111111"

    def frontend(number: int):
        return SimpleNamespace(
            valid=True,
            integrity_manifest_sha256=str(number + 2) * 64,
            canonical_content_sha256=str(number + 4) * 64,
            terminal_database_state_sha256=str(number + 6) * 64,
            product_proving_pass=SimpleNamespace(pass_number=number),
            pass_number=number,
            execution_id=(
                "22222222-2222-4222-8222-222222222222"
                if number == 1
                else "33333333-3333-4333-8333-333333333333"
            ),
            started_at=(
                "2026-08-26T12:00:00+00:00"
                if number == 1
                else "2026-08-26T12:00:03+00:00"
            ),
            prior_restore_operation_id=(None if number == 1 else restore_one_id),
            prior_restore_bundle_manifest_sha256=(
                None if number == 1 else restore_one_manifest
            ),
            prior_restore_bundle_canonical_sha256=(
                None if number == 1 else restore_one_canonical
            ),
            expected=expected,
            observed=observed,
            database_baseline_manifest_sha256=baseline.manifest_sha256,
            database_baseline_dump_sha256=baseline.dump_sha256,
            database_baseline_state_sha256=fingerprint.state_sha256,
            database_baseline_schema_sha256=fingerprint.schema_sha256,
            database_source_identity=SOURCE,
            database_source_connection_identity=CONNECTION,
        )

    first = frontend(1)
    second = frontend(2)

    def restoration(number: int, frontend_pass):
        return SimpleNamespace(
            valid=True,
            integrity_manifest_sha256=(
                restore_one_manifest if number == 1 else "b" * 64
            ),
            canonical_content_sha256=(
                restore_one_canonical if number == 1 else "c" * 64
            ),
            receipt=SimpleNamespace(
                restore_operation_id=(
                    restore_one_id
                    if number == 1
                    else "44444444-4444-4444-8444-444444444444"
                ),
                pass_number=number,
                pass_execution_id=frontend_pass.execution_id,
                pass_bundle_manifest_sha256=(
                    frontend_pass.integrity_manifest_sha256
                ),
                pass_bundle_canonical_sha256=(
                    frontend_pass.canonical_content_sha256
                ),
                terminal_database_state_sha256=(
                    frontend_pass.terminal_database_state_sha256
                ),
                database_baseline_manifest_sha256=baseline.manifest_sha256,
                database_baseline_dump_sha256=baseline.dump_sha256,
                database_baseline_state_sha256=fingerprint.state_sha256,
                database_baseline_schema_sha256=fingerprint.schema_sha256,
                source_database_identity=SOURCE,
                source_connection_identity=CONNECTION,
                previous_state_sha256=frontend_pass.terminal_database_state_sha256,
                restored_state_sha256=fingerprint.state_sha256,
                started_at=(
                    "2026-08-26T12:00:01+00:00"
                    if number == 1
                    else "2026-08-26T12:00:04+00:00"
                ),
                completed_at=(
                    "2026-08-26T12:00:02+00:00"
                    if number == 1
                    else "2026-08-26T12:00:05+00:00"
                ),
            ),
        )

    return (
        expected,
        fingerprint,
        baseline,
        first,
        second,
        restoration(1, first),
        restoration(2, second),
    )


def _config(tmp_path, baseline) -> ObservedProductProvingPublicationConfig:
    return ObservedProductProvingPublicationConfig(
        database_baseline_dir=Path("baseline"),
        database_baseline_manifest_sha256=baseline.manifest_sha256,
        pass_one_dir=Path("pass-1"),
        pass_one_manifest_sha256="3" * 64,
        pass_two_dir=Path("pass-2"),
        pass_two_manifest_sha256="4" * 64,
        restore_one_dir=Path("restore-1"),
        restore_one_manifest_sha256="9" * 64,
        restore_two_dir=Path("restore-2"),
        restore_two_manifest_sha256="b" * 64,
        source_database_url="postgresql://local@localhost:5433/corridor",
        repo_root=tmp_path,
        output_dir=tmp_path / "published",
    )


def _patch_live(monkeypatch, module, evidence):
    expected, fingerprint, baseline, first, second, restore_one, restore_two = evidence
    monkeypatch.setattr(
        module, "verify_product_proving_database_baseline", lambda *_a, **_k: baseline
    )
    passes = iter((first, second))
    monkeypatch.setattr(
        module, "verify_frontend_pass_bundle", lambda *_a, **_k: next(passes)
    )
    restores = iter((restore_one, restore_two))
    monkeypatch.setattr(
        module,
        "verify_product_proving_restore_bundle",
        lambda *_a, **_k: next(restores),
    )
    monkeypatch.setattr(module, "fingerprint_database_url", lambda _url: fingerprint)
    monkeypatch.setattr(
        module,
        "observe_database_connection_identity",
        lambda _url: SimpleNamespace(as_dict=lambda: CONNECTION),
    )
    monkeypatch.setattr(
        module,
        "observe_git_checkout",
        lambda _root: GitCheckoutObservation(
            expected.source_revision,
            expected.origin_main_revision,
            True,
        ),
    )
    monkeypatch.setattr(
        module, "read_migration_head", lambda *_a, **_k: expected.migration_head
    )


def test_final_publication_joins_two_executions_and_exact_restores(monkeypatch, tmp_path):
    import corridor.product_proving_session as module

    evidence = _evidence()
    _patch_live(monkeypatch, module, evidence)
    captured = []
    monkeypatch.setattr(module, "verify_two_pass_capture", captured.append)
    marker = object()
    monkeypatch.setattr(
        module, "publish_product_proving_bundle", lambda *_a, **_k: marker
    )

    result = publish_observed_product_proving_session(_config(tmp_path, evidence[2]))

    assert result is marker
    [capture] = captured
    assert capture.final_session_evidence.pass_two.prior_restore_operation_id == (
        capture.final_session_evidence.restore_one.restore_operation_id
    )
    assert capture.final_session_evidence.final_state.database_schema_sha256 == (
        evidence[1].schema_sha256
    )


def test_final_publication_refuses_a_pass_from_another_baseline(monkeypatch, tmp_path):
    import corridor.product_proving_session as module

    evidence = list(_evidence())
    evidence[3].database_baseline_manifest_sha256 = "0" * 64
    _patch_live(monkeypatch, module, tuple(evidence))

    with pytest.raises(ValueError, match="verified database baseline"):
        publish_observed_product_proving_session(_config(tmp_path, evidence[2]))


def test_final_publication_refuses_one_execution_replayed_as_pass_two(
    monkeypatch, tmp_path
):
    import corridor.product_proving_session as module

    evidence = list(_evidence())
    evidence[4].execution_id = evidence[3].execution_id
    _patch_live(monkeypatch, module, tuple(evidence))

    with pytest.raises(ValueError, match="replayed as both passes"):
        publish_observed_product_proving_session(_config(tmp_path, evidence[2]))


def test_final_publication_refuses_restore_from_another_baseline(monkeypatch, tmp_path):
    import corridor.product_proving_session as module

    evidence = list(_evidence())
    evidence[5].receipt.database_baseline_dump_sha256 = "0" * 64
    _patch_live(monkeypatch, module, tuple(evidence))

    with pytest.raises(ValueError, match="restore 1.*verified database baseline"):
        publish_observed_product_proving_session(_config(tmp_path, evidence[2]))
