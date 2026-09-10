"""The two named receipt policies and the identity rule, at the seam a caller uses.

Lifted from the collision tests `tests/test_eval.py` wrote against
`write_measurement_artifact`, then widened to the properties every absorbed
writer had proved separately: an identical rerun is accepted, a divergent one
is refused with the same sentence, a snapshot replaces atomically and stays
owner-only, and an identity ignores the fields named as volatile.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from corridor import digests
from corridor.receipts import (
    ArtifactCollision,
    identity,
    write_private_snapshot,
    write_sealed,
)

FIRST = {"artifact_identity": "same-name", "ran_at": "2026-08-06T00:00:00+00:00", "matched": 1}
RERUN = {"artifact_identity": "same-name", "ran_at": "2026-08-07T00:00:00+00:00", "matched": 1}
DIVERGENT = {"artifact_identity": "same-name", "ran_at": "2026-08-07T00:00:00+00:00", "matched": 0}


def _text(value: dict) -> str:
    return json.dumps(value, indent=2) + "\n"


def test_a_sealed_receipt_is_created_once_and_an_identical_rerun_changes_nothing(tmp_path):
    path = tmp_path / "measurement.json"
    previous = os.umask(0o022)
    try:
        assert write_sealed(path, _text(FIRST), volatile=("ran_at",)) is True
    finally:
        os.umask(previous)
    first_bytes = path.read_bytes()
    assert first_bytes == _text(FIRST).encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert write_sealed(path, _text(RERUN), volatile=("ran_at",)) is False

    assert path.read_bytes() == first_bytes
    assert json.loads(first_bytes)["ran_at"] == FIRST["ran_at"]
    assert sorted(item.name for item in tmp_path.iterdir()) == ["measurement.json"]


def test_a_divergent_rerun_can_never_overwrite_sealed_evidence(tmp_path):
    path = tmp_path / "measurement.json"
    write_sealed(path, _text(FIRST), volatile=("ran_at",))
    first_bytes = path.read_bytes()

    with pytest.raises(ArtifactCollision, match="divergent overwrite"):
        write_sealed(path, _text(DIVERGENT), volatile=("ran_at",))

    assert path.read_bytes() == first_bytes
    assert sorted(item.name for item in tmp_path.iterdir()) == ["measurement.json"]


def test_a_twin_without_volatile_fields_is_sealed_on_exact_bytes(tmp_path):
    path = tmp_path / "summary.md"
    assert write_sealed(path, "# Summary\n") is True
    assert write_sealed(path, "# Summary\n") is False
    with pytest.raises(ArtifactCollision, match="divergent overwrite"):
        write_sealed(path, "# Summary\n\nchanged\n")
    assert path.read_text() == "# Summary\n"


def test_an_unreadable_receipt_is_refused_rather_than_compared(tmp_path):
    path = tmp_path / "measurement.json"
    path.write_bytes(b"{not json")
    path.chmod(0o600)
    with pytest.raises(ArtifactCollision, match="unreadable measurement artifact"):
        write_sealed(path, _text(FIRST), volatile=("ran_at",))
    assert path.read_bytes() == b"{not json"


def test_an_identical_artifact_written_before_this_module_is_accepted_whatever_its_mode(tmp_path):
    # The old writers took the umask mode; an identical rerun over one of their
    # artifacts is accepted, and the first bytes and mode stay as they were.
    shared = tmp_path / "shared.json"
    shared.write_text(_text(FIRST))
    shared.chmod(0o644)
    assert write_sealed(shared, _text(FIRST), volatile=("ran_at",)) is False
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644
    with pytest.raises(ArtifactCollision, match="divergent overwrite"):
        write_sealed(shared, _text({**FIRST, "matched": 2}), volatile=("ran_at",))


def test_a_private_only_writer_refuses_a_file_the_policy_did_not_write(tmp_path):
    shared = tmp_path / "shared.json"
    shared.write_text(_text(FIRST))
    shared.chmod(0o644)
    with pytest.raises(ArtifactCollision, match="not a private regular file"):
        write_sealed(shared, _text(FIRST), volatile=("ran_at",), private_only=True)

    original = tmp_path / "original.json"
    original.write_text(_text(FIRST))
    original.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(original)
    with pytest.raises(ArtifactCollision, match="not a private regular file"):
        write_sealed(link, _text(FIRST), volatile=("ran_at",))
    assert link.is_symlink()
    assert original.read_text() == _text(FIRST)


def test_a_private_snapshot_replaces_the_previous_one_owner_only(tmp_path):
    path = tmp_path / "nested" / "report.json"
    previous = os.umask(0o022)
    try:
        write_private_snapshot(path, _text(FIRST))
        write_private_snapshot(path, _text(DIVERGENT))
    finally:
        os.umask(previous)
    assert path.read_text() == _text(DIVERGENT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(item.name for item in path.parent.iterdir()) == ["report.json"]


def test_a_failed_snapshot_publication_preserves_the_previous_bytes(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_bytes(b"previous complete snapshot")

    def interrupted(*_):
        raise OSError("publication interrupted")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="publication interrupted"):
        write_private_snapshot(path, _text(FIRST))
    assert path.read_bytes() == b"previous complete snapshot"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["report.json"]


def test_an_identity_excludes_the_named_volatile_fields():
    assert identity(FIRST, volatile=("ran_at",)) == identity(RERUN, volatile=("ran_at",))
    assert identity(FIRST, volatile=("ran_at",)) != identity(DIVERGENT, volatile=("ran_at",))
    assert identity(FIRST, volatile=("ran_at",)) == digests.ascii_escaped_sha256(
        {"artifact_identity": "same-name", "matched": 1}
    )
    assert identity(FIRST) != identity(RERUN)
