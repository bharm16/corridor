"""Acceptance coverage for the real SH 99 Admission operation boundary."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.slow

from corridor.config import settings
from corridor.m8_acceptance_bundle import publish_verified_bundle
from corridor.rehearsal_environment import SealedRehearsalEnvironment
from corridor.sh99_admission_acceptance import (
    BUNDLE_FILES,
    BUNDLE_SCHEMA_VERSION,
    CorruptSH99AdmissionBundle,
    SH99AdmissionAcceptanceConfig,
    _canonical_json,
    _json_sha256,
    _sha256,
    read_rehearsal_project_state,
    run_sh99_admission_acceptance,
    verify_sh99_admission_bundle,
)


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
    ).stdout.strip()


def _worktree_is_clean() -> bool:
    return not subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
    ).stdout


# This is the lifecycle signature recorded by the sealed #248 pre-state.  It
# gates that one-time replay; matching counts do not prove dataset identity.
_EXPECTED_PRE_ADMISSION_LIFECYCLE_SIGNATURE = {
    "document_count": 159,
    "active_run_count": 148,
    "active_run_declaration_count": 148,
    "candidate_count": 3030,
    "pending_candidate_count": 3030,
    "dependency_count": 0,
    "dependency_event_count": 0,
    "policy_run_count": 0,
    "dependency_admission_outcome_count": 0,
    "event_admission_outcome_count": 0,
    "admission_audit_count": 0,
}

_ADMISSION_ARTIFACT_FIELDS = (
    "dependency_count",
    "dependency_event_count",
    "policy_run_count",
    "dependency_admission_outcome_count",
    "event_admission_outcome_count",
    "admission_audit_count",
)


def _classify_sh99_admission_lifecycle_signature(
    lifecycle_signature: dict[str, int],
) -> str:
    if lifecycle_signature == _EXPECTED_PRE_ADMISSION_LIFECYCLE_SIGNATURE:
        return "expected-pre-admission-signature"
    if any(lifecycle_signature[name] for name in _ADMISSION_ARTIFACT_FIELDS):
        return "admission-artifacts-present"
    return "other"


def _source_sh99_lifecycle_gate(database_url: str | None = None) -> str:
    """Read the one-time #248 lifecycle signature without mutating its source."""
    try:
        state = read_rehearsal_project_state(
            database_url or settings.database_url,
            "sh99-grand-parkway",
        )
    except ValueError as error:
        if str(error) == "no project with slug 'sh99-grand-parkway'":
            return "missing"
        raise
    lifecycle = {
        "document_count": len(state["documents"]),
        "active_run_count": len(state["active_runs"]),
        "active_run_declaration_count": len(state["active_run_declarations"]),
        "candidate_count": len(state["candidates"]),
        "pending_candidate_count": sum(
            candidate["state"] == "pending" for candidate in state["candidates"]
        ),
        "dependency_count": len(state["ledger"]["dependencies"]),
        "dependency_event_count": len(state["ledger"]["dependency_events"]),
        "policy_run_count": len(state["policy_runs"]),
        "dependency_admission_outcome_count": len(
            state["dependency_admission_outcomes"]
        ),
        "event_admission_outcome_count": len(state["event_admission_outcomes"]),
        "admission_audit_count": len(state["admission_audits"]),
    }
    return _classify_sh99_admission_lifecycle_signature(lifecycle)


def _require_expected_pre_admission_lifecycle_signature(
    database_url: str | None = None,
) -> None:
    lifecycle_gate = (
        _source_sh99_lifecycle_gate()
        if database_url is None
        else _source_sh99_lifecycle_gate(database_url)
    )
    if lifecycle_gate == "expected-pre-admission-signature":
        if not _worktree_is_clean():
            pytest.skip(
                "the real SH 99 replay needs a clean checkout before using "
                "the expected pre-Admission lifecycle signature"
            )
        return
    if lifecycle_gate == "missing":
        pytest.skip(
            "the real SH 99 replay needs the separately managed source in the "
            "expected #248 pre-Admission lifecycle state"
        )
    if lifecycle_gate == "admission-artifacts-present":
        pytest.skip(
            "the shared SH 99 source has Admission artifacts and cannot satisfy "
            "the consumed one-time #248 gate; the sealed #248 bundle remains "
            "the evidence for that gate"
        )
    pytest.skip(
        "the shared SH 99 source does not have the expected #248 "
        "pre-Admission lifecycle signature"
    )


def _config(
    tmp_path,
    *,
    expected_clean_git_revision: str,
    source_database_url: str | None = None,
) -> SH99AdmissionAcceptanceConfig:
    return SH99AdmissionAcceptanceConfig(
        project_slug="sh99-grand-parkway",
        source_database_url=source_database_url or settings.database_url,
        expected_clean_git_revision=expected_clean_git_revision,
        output_dir=tmp_path / "bundle",
        postgres_admin_url=settings.database_url,
    )


def test_expected_pre_admission_counts_are_labeled_only_as_a_signature():
    classification = _classify_sh99_admission_lifecycle_signature(
        _EXPECTED_PRE_ADMISSION_LIFECYCLE_SIGNATURE
    )

    assert classification == "expected-pre-admission-signature"
    assert "exact" not in classification


@pytest.mark.parametrize(
    "artifact_field",
    [
        "dependency_count",
        "dependency_event_count",
        "policy_run_count",
        "dependency_admission_outcome_count",
        "event_admission_outcome_count",
        "admission_audit_count",
    ],
)
def test_admission_artifacts_fail_closed_without_claiming_a_complete_state(
    artifact_field,
):
    lifecycle_signature = {
        **_EXPECTED_PRE_ADMISSION_LIFECYCLE_SIGNATURE,
        artifact_field: 1,
    }

    assert _classify_sh99_admission_lifecycle_signature(
        lifecycle_signature
    ) == "admission-artifacts-present"


def test_admission_artifact_skip_names_the_consumed_gate_without_claiming_completion(
    monkeypatch,
):
    monkeypatch.setitem(
        globals(),
        "_source_sh99_lifecycle_gate",
        lambda: "admission-artifacts-present",
    )

    with pytest.raises(pytest.skip.Exception) as skipped:
        _require_expected_pre_admission_lifecycle_signature()

    reason = str(skipped.value)
    assert "has Admission artifacts" in reason
    assert "cannot satisfy the consumed one-time #248 gate" in reason
    assert "post-Admission" not in reason


def test_real_sh99_clone_replays_the_exact_shared_operation_twice(
    shared_source_database_url,
    tmp_path,
):
    """The 3,030-Candidate SH 99 operation is proved only on an isolated clone."""
    _require_expected_pre_admission_lifecycle_signature(
        shared_source_database_url
    )

    summary = run_sh99_admission_acceptance(
        _config(
            tmp_path,
            expected_clean_git_revision=_git_revision(),
            source_database_url=shared_source_database_url,
        )
    )

    verified = verify_sh99_admission_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )
    assert verified.valid is True
    assert verified.canonical_content_sha256 == summary.canonical_content_sha256

    receipt = json.loads((summary.bundle_dir / "receipt.json").read_bytes())
    source = receipt["source_snapshot"]
    assert source["project_slug"] == "sh99-grand-parkway"
    assert source["source"]["revision"] == _git_revision()
    assert len(source["source_dump_sha256"]) == 64
    assert len(source["before"]["candidates"]) == 3030
    assert source["before"]["ledger"]["dependencies"] == []
    assert source["before"]["ledger"]["dependency_events"] == []

    expected_command = ["make", "admission", "ARGS=load sh99-grand-parkway"]
    assert receipt["first_run"]["operation"]["argv"] == expected_command
    assert receipt["second_run"]["operation"]["argv"] == expected_command
    assert receipt["protected_cases"]["7587"]["state"] == "pending"
    assert receipt["protected_cases"]["7587"]["event_outcome"]["outcome"] == "abstained"
    assert receipt["protected_cases"]["7587"]["event_outcome"]["dependency_event_id"] is None
    for candidate_id in ("7587", "7129", "7296"):
        protected = receipt["protected_cases"][candidate_id]
        assert protected["state"] == "pending"
        assert protected["merged_into"] is None
        assert protected["statement_facts"] == []
        assert protected["scope_decision_ids"] == []
        assert protected["scope_ids"] == []
        assert protected["timing_ids"] == []
        assert protected["statement_evidence_ids"] == []
        assert protected["committed_date_projections"] == []
        assert protected["past_due_dependency_ids"] == []
    assert not any(receipt["second_run"]["created_ledger_identities"].values())
    assert receipt["late_refusal"]["ledger_and_candidate_state_unchanged"] == {
        "candidates": True,
        "ledger": True,
    }
    assert receipt["late_refusal"]["operation"] == {
        "argv": expected_command,
        "returncode": 2,
        "stdout": "uv run python -m corridor.admission_cli load sh99-grand-parkway",
        "expected_failure_marker": "forced SH 99 acceptance dependency refusal",
    }
    assert receipt["human_approval_gate"] == (
        "No shared SH 99 database was changed. A designated human must separately "
        "approve any shared-database Admission operation."
    )


def test_replay_refuses_a_checkout_that_does_not_match_the_caller_pin(monkeypatch, tmp_path):
    """The shared source is not even inspected when the caller pin is wrong."""

    monkeypatch.setattr(
        "corridor.rehearsal_environment._git",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )

    with pytest.raises(ValueError, match="caller-provided pin"):
        run_sh99_admission_acceptance(
            _config(tmp_path, expected_clean_git_revision="b" * 40)
        )
    assert not (tmp_path / "bundle").exists()


def test_replay_refuses_a_shared_database_at_a_different_migration_head(monkeypatch, tmp_path):
    """A source/head mismatch fails before pg_dump or disposable provisioning."""

    monkeypatch.setattr(
        "corridor.rehearsal_environment._git",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        "corridor.rehearsal_environment._source_migration_head",
        lambda _root: "source-head",
    )
    monkeypatch.setattr(
        "corridor.rehearsal_environment.read_migration_head",
        lambda *args, **kwargs: "shared-head",
    )

    with pytest.raises(ValueError, match="shared database migration head"):
        run_sh99_admission_acceptance(
            _config(tmp_path, expected_clean_git_revision="a" * 40)
        )
    assert not (tmp_path / "bundle").exists()


def _sealed_environment(tmp_path) -> SealedRehearsalEnvironment:
    return SealedRehearsalEnvironment(
        source_database_url="postgresql://corridor:secret@localhost:5433/corridor",
        checkout={"revision": "a" * 40},
        checkout_migration_head="head",
        database_migration_head="head",
        source_database={"database": "corridor", "username": "corridor"},
        repo_root=tmp_path,
        compose_root=tmp_path,
    )


def _record_client(monkeypatch, seen: dict, *, compose_running: bool):
    """Record one captured client invocation on a chosen PostgreSQL route."""

    monkeypatch.setattr(
        "corridor.rehearsal_environment._compose_service_is_running",
        lambda compose_root: compose_running,
    )

    def fake_run(*args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        seen["env"] = kwargs.get("env")
        seen["argv"] = kwargs["args"][0] if "args" in kwargs else args[0]
        if "stdin" in kwargs:
            seen["stdin_bytes"] = kwargs["stdin"].read()
        else:
            kwargs["stdout"].write(b"dump-bytes")
        return subprocess.CompletedProcess(
            seen["argv"], 0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr("corridor.rehearsal_environment.subprocess.run", fake_run)


def test_dump_source_database_uses_the_explicit_runtime_compose_root(
    monkeypatch, tmp_path
):
    dump_path = tmp_path / "source.dump"
    seen: dict[str, object] = {}
    _record_client(monkeypatch, seen, compose_running=True)

    _sealed_environment(tmp_path).capture(dump_path)

    assert seen["cwd"] == tmp_path.resolve()
    assert seen["argv"][:5] == ["docker", "compose", "exec", "-T", "postgres"]
    assert dump_path.read_bytes() == b"dump-bytes"


def test_restore_source_database_uses_the_explicit_runtime_compose_root(
    monkeypatch, tmp_path
):
    dump_path = tmp_path / "source.dump"
    dump_path.write_bytes(b"dump-bytes")
    seen: dict[str, object] = {}
    _record_client(monkeypatch, seen, compose_running=True)

    _sealed_environment(tmp_path).restore(dump_path, "corridor_clone")

    assert seen["cwd"] == tmp_path.resolve()
    assert seen["argv"][:5] == ["docker", "compose", "exec", "-T", "postgres"]
    assert seen["stdin_bytes"] == b"dump-bytes"


def test_capture_reaches_the_configured_server_directly_without_compose(
    monkeypatch, tmp_path
):
    """No Compose service is not "no PostgreSQL" — it is the CI arrangement.

    The runner image serves the configured URL itself (#595), so the rehearsal
    addresses that URL with the client on PATH rather than skipping (#639).
    """

    dump_path = tmp_path / "source.dump"
    seen: dict[str, object] = {}
    _record_client(monkeypatch, seen, compose_running=False)

    _sealed_environment(tmp_path).capture(dump_path)

    argv = seen["argv"]
    assert argv[0] == "pg_dump"
    assert "docker" not in argv
    assert argv[-8:] == [
        "--host",
        "localhost",
        "--port",
        "5433",
        "--username",
        "corridor",
        "--dbname",
        "corridor",
    ]
    assert seen["env"]["PGPASSWORD"] == "secret"


def test_restore_reaches_the_configured_server_directly_without_compose(
    monkeypatch, tmp_path
):
    dump_path = tmp_path / "source.dump"
    dump_path.write_bytes(b"dump-bytes")
    seen: dict[str, object] = {}
    _record_client(monkeypatch, seen, compose_running=False)

    _sealed_environment(tmp_path).restore(dump_path, "corridor_clone")

    argv = seen["argv"]
    assert argv[0] == "pg_restore"
    assert "docker" not in argv
    assert argv[-8:] == [
        "--host",
        "localhost",
        "--port",
        "5433",
        "--username",
        "corridor",
        "--dbname",
        "corridor_clone",
    ]
    assert seen["stdin_bytes"] == b"dump-bytes"
    assert seen["env"]["PGPASSWORD"] == "secret"


def test_public_verifier_rejects_a_tampered_real_sh99_receipt(tmp_path):
    """Receipt verification remains database-free and digest-pinned."""

    bundle = tmp_path / "bundle"
    canonical = {"schema_version": BUNDLE_SCHEMA_VERSION, "claim": "real SH 99"}
    _, manifest_sha256, _ = publish_verified_bundle(
        bundle,
        exports={
            "environment.json": {"schema_version": BUNDLE_SCHEMA_VERSION},
            "receipt.json": {"schema_version": BUNDLE_SCHEMA_VERSION, "claim": "real SH 99"},
            "canonical-content.json": canonical,
        },
        canonical_content=canonical,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="test-sh99-admission-bundle",
        self_verification_failure="test bundle self-verification failed",
    )
    receipt = bundle / "receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b"\n")

    with pytest.raises(CorruptSH99AdmissionBundle, match="receipt.json"):
        verify_sh99_admission_bundle(
            bundle, expected_integrity_manifest_sha256=manifest_sha256
        )
