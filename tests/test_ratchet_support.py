"""A ratchet that cannot see direction is a record of the last measurement."""

from __future__ import annotations

import subprocess

import pytest

from ratchet_support import assert_ratchet, recorded_at_merge_base


def _repository(tmp_path, recorded: str):
    root = tmp_path / "repository"
    root.mkdir()
    guard = root / "guard.py"
    guard.write_text(f"REASON = 'one seam'\nRECORDED = {recorded}\n", encoding="utf-8")
    run = lambda *args: subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "guard@example.invalid")
    run("config", "user.name", "Guard")
    run("add", "guard.py")
    run("commit", "-q", "-m", "record the baseline")
    run("checkout", "-q", "-b", "change")
    return root, guard, run


def test_a_count_may_fall_and_may_never_rise_against_the_merge_base(tmp_path, monkeypatch):
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"

    assert recorded_at_merge_base(name, repository=root) == 3
    assert_ratchet(name, measured=3, recorded=3, repository=root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 2\n", encoding="utf-8")
    assert_ratchet(name, measured=2, recorded=2, repository=root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 4\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="rose to 4 from the 3 recorded at"):
        assert_ratchet(name, measured=4, recorded=4, repository=root)


def test_a_name_may_leave_and_may_never_join(tmp_path, monkeypatch):
    """The unit is the name, so the failure says which one joined."""
    root, guard, _ = _repository(tmp_path, "(('reader', 'legacy', REASON),)")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"
    edges = lambda value: {(source, target) for source, target, _ in value}

    assert recorded_at_merge_base(name, repository=root) == (("reader", "legacy", "one seam"),)
    assert_ratchet(
        name, measured={("reader", "legacy")}, recorded={("reader", "legacy")},
        as_measured=edges, repository=root,
    )
    assert_ratchet(name, measured=set(), recorded=set(), as_measured=edges, repository=root)

    with pytest.raises(AssertionError, match="joined since the merge base: .*second"):
        assert_ratchet(
            name,
            measured={("reader", "legacy"), ("second", "legacy")},
            recorded={("reader", "legacy"), ("second", "legacy")},
            as_measured=edges,
            repository=root,
        )


def test_the_working_tree_check_still_runs_when_no_base_ref_resolves(tmp_path, monkeypatch):
    """Local runs and shallow CI clones lose the history, not the guard."""
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "no-such-branch")
    monkeypatch.chdir(root)
    name = "guard.py:RECORDED"

    assert recorded_at_merge_base(name, repository=root) is None
    assert_ratchet(name, measured=9, recorded=9, repository=root)
    with pytest.raises(AssertionError, match="measures 9 against the 3 recorded"):
        assert_ratchet(name, measured=9, recorded=3, repository=root)


def test_a_constant_absent_from_the_merge_base_leaves_the_direction_unjudged(tmp_path, monkeypatch):
    """A ratchet introduced by this very change has no baseline to fall from."""
    root, guard, _ = _repository(tmp_path, "3")
    monkeypatch.setenv("CORRIDOR_RATCHET_BASE_REF", "main")
    monkeypatch.chdir(root)

    guard.write_text("REASON = 'one seam'\nRECORDED = 3\nADDED = 12\n", encoding="utf-8")

    assert recorded_at_merge_base("guard.py:ADDED", repository=root) is None
    assert recorded_at_merge_base("guard.py:MISSING", repository=root) is None
    assert_ratchet("guard.py:ADDED", measured=12, recorded=12, repository=root)
