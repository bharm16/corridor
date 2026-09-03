"""M8 acceptance: the controlled oracle lane.

Split from the replay and bundle suites so the three run on separate CI
runners (#548). One 1,263-line module held 258 of the slow gate's 381 seconds
and a file cannot span runners, so it set the gate's floor alone.

These three tests exercise the controlled lane's own runtime, snapshot reuse,
and contradiction export, so they share only `replay_capture`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow

import corridor.m8_acceptance_controlled as m8_acceptance_controlled_module
from corridor.m8_acceptance import (
    AcceptanceError,
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)
from corridor.revision_comparison import DEFAULT_MATCHER_VERSION

from m8_acceptance_support import (
    _capture_fixture,
    _read_export,
    _run_config,
)


@pytest.fixture(scope="module")
def replay_capture(tmp_path_factory):
    return _capture_fixture(tmp_path_factory.mktemp("m8-replay-capture"))


def test_controlled_lane_reuses_worklist_snapshots_within_unchanged_phases(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_controlled_module.build_reviewer_worklist
    calls = 0

    def counted_worklist(session, project_id):
        nonlocal calls
        calls += 1
        return original(session, project_id)

    monkeypatch.setattr(
        m8_acceptance_controlled_module,
        "build_reviewer_worklist",
        counted_worklist,
    )

    run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle"))

    # Five lifecycle transitions genuinely change state. Before-policy and
    # after-policy case projections each share one stable project snapshot.
    assert calls <= 7


def test_controlled_lane_contradiction_is_exported_in_a_verifiable_failed_bundle(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_controlled_module._compare_controlled_case
    raised = {"done": False}

    def explode_once(session, controlled_case):
        original(session, controlled_case)
        if not raised["done"]:
            raised["done"] = True
            raise AcceptanceError("forced controlled contradiction")

    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._compare_controlled_case",
        explode_once,
    )

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "failed-bundle"))

    assert summary.carried_count == 0
    assert [assertion.name for assertion in summary.assertions] == [
        "controlled_lane_claims_remain_self_consistent"
    ]
    assert [assertion.passed for assertion in summary.assertions] == [False]
    assert (
        verify_m8_acceptance_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        ).valid
        is True
    )
    controlled = _read_export(summary, "controlled-lane.json")
    assert controlled["status"] == "failed"
    assert (
        controlled["failure"]["name"] == "controlled_lane_claims_remain_self_consistent"
    )
    assert len(controlled["documents"]) > 0
    assert len(controlled["runs"]) > 0


def test_controlled_lane_freezes_one_runtime_for_normal_protocol(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    created = []
    seen = []

    class RuntimeToken:
        def canonical_policy_json(self):
            return {
                "policy_version": "test",
                "eligible_comparison_state": "unchanged",
                "field_equality": "exact-admitted-fields-v1",
                "provenance": "exactly-one-verified-citation-v1",
                "support_scope": "all-operative-scopes-v1",
                "rules_digest_method": "sha256-safety-source-files-v1",
                "rules_digest": "r" * 64,
                "matcher_version": DEFAULT_MATCHER_VERSION,
                "matcher_config": {},
                "matcher_config_sha256": "m" * 64,
            }

        def rules_digest(self):
            return "r" * 64

    def make_runtime():
        runtime = RuntimeToken()
        created.append(runtime)
        return runtime

    original_status = m8_acceptance_controlled_module.automatic_carry_forward_status
    original_run = m8_acceptance_controlled_module.run_automatic_carry_forward

    def wrapped_status(session, project_id, *, _runtime=None, **kwargs):
        seen.append(("status", id(_runtime)))
        return original_status(session, project_id, _runtime=_runtime, **kwargs)

    def wrapped_run(session, project_id, *, _runtime=None):
        seen.append(("run", id(_runtime)))
        return original_run(session, project_id, _runtime=_runtime)

    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._acceptance_carry_runtime",
        make_runtime,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled.automatic_carry_forward_status",
        wrapped_status,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled.run_automatic_carry_forward",
        wrapped_run,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._exercise_policy_drift",
        lambda _session: {"outcome": "skipped-for-runtime-test"},
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance._acceptance_assertions",
        lambda *_args: [],
    )

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle"))

    assert summary.assertions == ()
    assert len(created) == 1
    expected_runtime_id = id(created[0])
    assert seen[:3] == [
        ("status", expected_runtime_id),
        ("run", expected_runtime_id),
        ("run", expected_runtime_id),
    ]
