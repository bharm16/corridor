"""Operator CLI for explicit Automatic Carry-Forward policy control."""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace

import pytest

from corridor.automatic_carry_forward import (
    AutomaticCarryForwardAbstention,
    AutomaticCarryForwardResult,
)
from corridor.automatic_carry_forward_cli import main
from corridor.principals import HumanPrincipal


@dataclass
class _RecordingSession:
    project: object | None
    commits: int = 0

    def scalar(self, _statement):
        return self.project

    def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self, session: _RecordingSession):
        self.session = session
        self.opens = 0

    def __call__(self):
        factory = self

        class Context:
            def __enter__(self):
                factory.opens += 1
                return factory.session

            def __exit__(self, *_):
                return False

        return Context()


def _project():
    return SimpleNamespace(id=84, slug="nhhip-3c2")


def _approval(*, approval_id: int = 12):
    return SimpleNamespace(
        id=approval_id,
        policy_version="automatic-carry-forward-v1",
        policy_sha256="ab" * 32,
        approved_by="oidc:00u123",
    )


def _status(*, enabled=True):
    return SimpleNamespace(
        enabled=enabled,
        policy_current=enabled,
        policy_approval_id=12 if enabled else None,
        policy_version=(
            "automatic-carry-forward-v1" if enabled else None
        ),
        policy_sha256="ab" * 32 if enabled else None,
        approved_by="oidc:00u123" if enabled else None,
        carried_count=9,
        eligible_count=2 if enabled else 0,
        abstention_reason_version=(
            "automatic-carry-forward-abstentions-v1"
        ),
        abstention_counts={"comparison_changed": 3},
    )


def _json_output(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_status_is_stable_read_only_json(monkeypatch, capsys):
    session = _RecordingSession(_project())
    factory = _SessionFactory(session)
    approval = _approval()
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.automatic_carry_forward_status",
        lambda db, project_id: _status(),
    )

    assert main(["status", "nhhip-3c2"], session_factory=factory) == 0
    first = capsys.readouterr()
    assert first.err == ""
    assert main(["status", "nhhip-3c2"], session_factory=factory) == 0
    second = capsys.readouterr()

    assert second.err == ""
    assert first.out == second.out
    assert json.loads(first.out) == {
        "active_policy": {
            "approval_id": 12,
            "approved_by": "oidc:00u123",
            "policy_sha256": "ab" * 32,
            "policy_version": "automatic-carry-forward-v1",
        },
        "abstentions": {
            "count": 3,
            "reason_version": "automatic-carry-forward-abstentions-v1",
            "reasons": {"comparison_changed": 3},
        },
        "carried_count": 9,
        "command": "status",
        "eligible_count": 2,
        "project_id": 84,
        "project_slug": "nhhip-3c2",
    }
    assert session.commits == 0


def test_status_reports_disabled_without_creating_policy(monkeypatch, capsys):
    session = _RecordingSession(_project())
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.automatic_carry_forward_status",
        lambda db, project_id: _status(enabled=False),
    )

    assert main(
        ["status", "nhhip-3c2"],
        session_factory=_SessionFactory(session),
    ) == 0

    output = _json_output(capsys)
    assert output["active_policy"] is None
    assert output["eligible_count"] == 0
    assert output["carried_count"] == 9
    assert output["abstentions"] == {
        "count": 3,
        "reason_version": "automatic-carry-forward-abstentions-v1",
        "reasons": {"comparison_changed": 3},
    }
    assert session.commits == 0


def test_authorize_requires_and_passes_a_stable_human_principal(
    monkeypatch, capsys
):
    session = _RecordingSession(_project())
    seen = []
    approval = _approval()

    def authorize(db, project_id, principal):
        seen.append((db, project_id, principal))
        return approval

    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.authorize_automatic_carry_forward",
        authorize,
    )

    assert main(
        ["authorize", "nhhip-3c2", "--principal", "oidc:00u123"],
        session_factory=_SessionFactory(session),
    ) == 0

    assert seen == [(session, 84, HumanPrincipal("oidc:00u123"))]
    assert _json_output(capsys) == {
        "active_policy": {
            "approval_id": 12,
            "approved_by": "oidc:00u123",
            "policy_sha256": "ab" * 32,
            "policy_version": "automatic-carry-forward-v1",
        },
        "command": "authorize",
        "project_id": 84,
        "project_slug": "nhhip-3c2",
    }
    assert session.commits == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["authorize", "nhhip-3c2"],
        ["disable", "nhhip-3c2"],
        ["authorize", "nhhip-3c2", "--principal", "reviewer"],
        ["authorize", "nhhip-3c2", "--principal", "local:reviewer"],
        ["disable", "nhhip-3c2", "--principal", "local:system"],
    ],
)
def test_policy_mutations_reject_missing_or_role_label_identity(
    argv, capsys
):
    class MustNotConnect:
        def __call__(self):
            raise AssertionError("identity validation must precede DB access")

    assert main(argv, session_factory=MustNotConnect()) == 2
    assert capsys.readouterr().err


def test_disable_commits_only_the_explicit_mutation(monkeypatch, capsys):
    session = _RecordingSession(_project())
    disabled = _approval()
    seen = []

    def disable(db, project_id, principal):
        seen.append((db, project_id, principal))
        return disabled

    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.disable_automatic_carry_forward",
        disable,
    )

    assert main(
        ["disable", "nhhip-3c2", "--principal=local:bryce"],
        session_factory=_SessionFactory(session),
    ) == 0

    assert seen == [(session, 84, HumanPrincipal("local:bryce"))]
    assert _json_output(capsys) == {
        "active_policy": None,
        "command": "disable",
        "disabled_policy": {
            "approval_id": 12,
            "approved_by": "oidc:00u123",
            "policy_sha256": "ab" * 32,
            "policy_version": "automatic-carry-forward-v1",
        },
        "project_id": 84,
        "project_slug": "nhhip-3c2",
    }
    assert session.commits == 1


def test_run_reports_sorted_receipts_and_aggregated_abstentions(
    monkeypatch, capsys
):
    session = _RecordingSession(_project())
    approval = _approval()
    result = AutomaticCarryForwardResult(
        carried=(
            SimpleNamespace(audit_log_id=19),
            SimpleNamespace(audit_log_id=7),
        ),
        abstentions=(
            AutomaticCarryForwardAbstention(
                dependency_id=3,
                predecessor_candidate_id=30,
                successor_candidate_id=31,
                comparison_id=4,
                finding_id=5,
                reason="comparison_changed",
            ),
            AutomaticCarryForwardAbstention(
                dependency_id=9,
                predecessor_candidate_id=90,
                successor_candidate_id=None,
                comparison_id=8,
                finding_id=7,
                reason="comparison_dropped",
            ),
            AutomaticCarryForwardAbstention(
                dependency_id=6,
                predecessor_candidate_id=60,
                successor_candidate_id=61,
                comparison_id=4,
                finding_id=8,
                reason="comparison_changed",
            ),
        ),
    )
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.active_carry_forward_policy",
        lambda db, project_id: approval,
    )
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.run_automatic_carry_forward",
        lambda db, project_id: result,
    )

    assert main(
        ["run", "nhhip-3c2"],
        session_factory=_SessionFactory(session),
    ) == 0

    assert _json_output(capsys) == {
        "abstentions": {
            "count": 3,
            "reason_version": "automatic-carry-forward-abstentions-v1",
            "reasons": {
                "comparison_changed": 2,
                "comparison_dropped": 1,
            },
        },
        "active_policy": {
            "approval_id": 12,
            "approved_by": "oidc:00u123",
            "policy_sha256": "ab" * 32,
            "policy_version": "automatic-carry-forward-v1",
        },
        "carried_receipt_ids": [7, 19],
        "command": "run",
        "project_id": 84,
        "project_slug": "nhhip-3c2",
    }
    assert session.commits == 1


def test_run_does_not_implicitly_authorize_a_disabled_project(
    monkeypatch, capsys
):
    session = _RecordingSession(_project())
    calls = []
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.active_carry_forward_policy",
        lambda db, project_id: None,
    )
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.run_automatic_carry_forward",
        lambda db, project_id: calls.append((db, project_id))
        or AutomaticCarryForwardResult(carried=(), abstentions=()),
    )

    assert main(
        ["run", "nhhip-3c2"],
        session_factory=_SessionFactory(session),
    ) == 0

    assert calls == [(session, 84)]
    assert _json_output(capsys) == {
        "abstentions": {
            "count": 0,
            "reason_version": "automatic-carry-forward-abstentions-v1",
            "reasons": {},
        },
        "active_policy": None,
        "carried_receipt_ids": [],
        "command": "run",
        "project_id": 84,
        "project_slug": "nhhip-3c2",
    }
    assert session.commits == 1


def test_unknown_project_fails_without_committing(capsys):
    session = _RecordingSession(None)

    assert main(
        ["status", "missing"],
        session_factory=_SessionFactory(session),
    ) == 1

    assert "does not exist" in capsys.readouterr().err
    assert session.commits == 0
