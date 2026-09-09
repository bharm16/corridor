"""Customer history publication must preserve private custody on failure."""

import os

import pytest

from corridor.legacy_history_cli import _write_export


def test_history_export_is_private_under_ordinary_umask(tmp_path):
    destination = tmp_path / "history.json"
    previous = os.umask(0o022)
    try:
        _write_export(destination, {"canonical_history": "retained source and actor"})
    finally:
        os.umask(previous)
    assert destination.stat().st_mode & 0o077 == 0


def test_history_export_refuses_symlink_and_preserves_target(tmp_path):
    target = tmp_path / "prior.json"
    target.write_bytes(b"original custody")
    destination = tmp_path / "history.json"
    destination.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        _write_export(destination, {"canonical_history": "replacement"})
    assert target.read_bytes() == b"original custody"
    assert destination.is_symlink()


def test_history_export_publication_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    destination = tmp_path / "history.json"
    destination.write_bytes(b"previous complete export")
    destination.chmod(0o600)

    def interrupted(*_):
        raise OSError("publication interrupted")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="publication interrupted"):
        _write_export(destination, {"canonical_history": "new complete export"})
    assert destination.read_bytes() == b"previous complete export"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["history.json"]
