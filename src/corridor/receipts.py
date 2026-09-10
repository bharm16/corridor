"""One writer for every measurement receipt that must not be quietly changed.

Five durability policies were invented separately for what every docstring
called an "immutable receipt". `eval.write_measurement_artifact` opened the
file with `"x"` and, on collision, accepted a rerun whose only difference was
`ran_at`; the shadow cohort manifest opened with `"x"` and refused any rerun;
the Statement Review Assistant evaluation receipt, which gates promotion and
is digested into a database row, was a plain `write_text` that a second run
could overwrite; three CLIs held verbatim copies of an mkstemp-and-replace
body; `activation`, `shadow_cli` and `shadow_reconciliation` each staged a
private temporary file, fsynced it and hard-linked it into place with their
own comparison on collision. The rule "identity excludes the timestamp" lived
only as keys popped inside `eval.artifact` and
`candidate_model.comparison_artifact`.

This module owns two named policies and the identity rule, and nothing about
what a receipt says:

- **sealed** (`write_sealed`): written once. The bytes are staged owner-only
  beside the destination, fsynced, and hard-linked into place, so a reader
  never sees a partial receipt and two writers cannot both win. A rerun that
  produces the same receipt is accepted; one that differs is refused with
  `ArtifactCollision`, and so is anything at the path this policy did not
  write (a symlink, a group- or world-readable file). When the receipt names
  volatile fields, both sides are read as JSON and compared without them;
  otherwise the bytes must match exactly, which is how a Markdown twin is
  sealed under the same rule as its JSON.
- **private snapshot** (`write_private_snapshot`): the latest result replaces
  the previous one atomically (mkstemp, fsync, `os.replace`), owner-only, so
  a failure leaves the earlier snapshot intact and never a half-written file.

Callers render their own bytes: the on-disk encodings this module absorbed
differ (indentation, key order, `default=str`) and each is already read by
something, so the policy owns durability and the caller owns the text.
`publish_directory_once` in `m8_acceptance_publication` is the directory form
of sealed and stays there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
import stat
import tempfile

from corridor import digests

__all__ = [
    "ArtifactCollision",
    "identity",
    "write_private_snapshot",
    "write_sealed",
]


class ArtifactCollision(Exception):
    """An immutable measurement artifact already occupies this identity."""


# Retained encoding: an Extraction Measurement artifact's filename carries the
# first sixteen characters of its identity, and repeating an exact command must
# resolve to the file an earlier run wrote. `eval` and `candidate_model` hashed
# ASCII-escaped JSON, so the identity keeps that encoding.
_identity_sha256 = digests.ascii_escaped_sha256


def identity(payload: Mapping[str, object], *, volatile: Iterable[str] = ()) -> str:
    """The digest of a receipt without its volatile fields.

    A timestamp records when a run happened, not what it measured, so two
    runs of the same command share one identity and resolve to one file.
    """
    excluded = set(volatile)
    return _identity_sha256(
        {key: value for key, value in payload.items() if key not in excluded}
    )


def write_sealed(
    path: Path, body: bytes | str, *, volatile: Iterable[str] = ()
) -> bool:
    """Create a receipt once; accept an identical rerun; refuse anything else.

    Returns True when this call created the file and False when an identical
    receipt already occupied it. The first receipt's bytes are the evidence:
    an accepted rerun changes nothing on disk, not even a volatile field.
    """
    path = Path(path)
    data = body.encode("utf-8") if isinstance(body, str) else body
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            _accept_existing(path, data, set(volatile))
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_private_snapshot(path: Path, body: bytes | str) -> None:
    """Replace the previous snapshot atomically, owner-only."""
    path = Path(path)
    data = body.encode("utf-8") if isinstance(body, str) else body
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _accept_existing(path: Path, data: bytes, volatile: set[str]) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ArtifactCollision(
            f"refusing to accept {path} as an immutable measurement artifact: "
            "not a private regular file"
        )
    if volatile:
        try:
            existing = _without(json.loads(path.read_bytes()), volatile)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactCollision(
                f"refusing to overwrite unreadable measurement artifact {path}: {exc}"
            ) from exc
        identical = existing == _without(json.loads(data), volatile)
    else:
        identical = path.read_bytes() == data
    if not identical:
        raise ArtifactCollision(
            f"refusing divergent overwrite of immutable measurement artifact {path}"
        )


def _without(value: object, volatile: set[str]) -> object:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in volatile}
