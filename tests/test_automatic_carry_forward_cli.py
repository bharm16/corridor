"""Operator CLI for the released Automatic Support Update Rules."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from corridor.automatic_carry_forward import (
    AutomaticCarryForwardAbstention,
    AutomaticCarryForwardResult,
)
from corridor.automatic_carry_forward_cli import main
from cli_support import json_output


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

    def __call__(self):
        session = self.session

        class Context:
            def __enter__(self):
                return session

            def __exit__(self, *_):
                return False

        return Context()


def _project():
    return SimpleNamespace(id=84, slug="nhhip-3c2")


def _status():
    return SimpleNamespace(
        policy_version="automatic-carry-forward-v2",
        policy_sha256="ab" * 32,
        carried_count=9,
        eligible_count=2,
        abstention_reason_version="automatic-carry-forward-abstentions-v1",
        abstention_counts={"comparison_changed": 3},
    )


def test_help_explains_support_updates_without_changing_the_command_or_opening_db(
    capsys,
):
    class MustNotConnect:
        def __call__(self):
            raise AssertionError("help must not open a database")

    assert main(["--help"], session_factory=MustNotConnect()) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    help_text = " ".join(captured.out.split())
    assert "automatic-carry-forward" in help_text
    assert "Automatic Support Update" in help_text
    assert "recorded conclusion unchanged" in help_text
    assert "JSON fields and reason codes retain their existing names" in help_text


def test_status_is_stable_read_only_released_policy_json(monkeypatch, capsys):
    session = _RecordingSession(_project())
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.automatic_carry_forward_status",
        lambda db, project_id: _status(),
    )

    assert main(["status", "nhhip-3c2"], session_factory=_SessionFactory(session)) == 0

    assert json_output(capsys) == {
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
        "released_policy": {
            "policy_sha256": "ab" * 32,
            "policy_version": "automatic-carry-forward-v2",
        },
    }
    assert session.commits == 0


@pytest.mark.parametrize("command", ["authorize", "disable"])
def test_project_policy_mutation_commands_do_not_exist(command, capsys):
    class MustNotConnect:
        def __call__(self):
            raise AssertionError("invalid commands must not open a database")

    assert main([command, "nhhip-3c2"], session_factory=MustNotConnect()) == 2
    assert capsys.readouterr().err


def test_run_reports_receipts_abstentions_and_released_policy(monkeypatch, capsys):
    session = _RecordingSession(_project())
    result = AutomaticCarryForwardResult(
        carried=(SimpleNamespace(audit_log_id=19), SimpleNamespace(audit_log_id=7)),
        abstentions=(
            AutomaticCarryForwardAbstention(
                dependency_id=3,
                predecessor_candidate_id=30,
                successor_candidate_id=31,
                comparison_id=4,
                finding_id=5,
                reason="comparison_changed",
            ),
        ),
    )
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.run_automatic_carry_forward",
        lambda db, project_id: result,
    )
    monkeypatch.setattr(
        "corridor.automatic_carry_forward_cli.automatic_carry_forward_status",
        lambda db, project_id: _status(),
    )

    assert main(["run", "nhhip-3c2"], session_factory=_SessionFactory(session)) == 0

    output = json_output(capsys)
    assert output["carried_receipt_ids"] == [7, 19]
    assert output["released_policy"]["policy_version"] == "automatic-carry-forward-v2"
    assert output["abstentions"]["reasons"] == {"comparison_changed": 1}
    assert session.commits == 1


def test_unknown_project_fails_without_committing(capsys):
    session = _RecordingSession(None)

    assert main(["status", "missing"], session_factory=_SessionFactory(session)) == 1

    assert "does not exist" in capsys.readouterr().err
    assert session.commits == 0
