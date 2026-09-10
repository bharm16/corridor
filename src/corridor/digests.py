"""One canonical digest for the whole codebase, and the named variants that
retained receipts still depend on.

`corridor.policy` already fought this fight once, for three modules: three
functions each called itself *the* canonical digest, and for any value
carrying a character outside ASCII they disagreed, so the same policy JSON
hashed to two different values depending on which module asked. That module's
docstring records the resolution, and consolidating three encodings into one
is the whole reason it exists.

The same copy then happened fifty more times. Fifty-three digest helpers were
defined across roughly forty modules, in at least six mutually incompatible
encodings: `ensure_ascii=False` here and the default `True` there,
`allow_nan=False` in some and unbounded floats in others, a trailing newline
appended in one, `default=str` in another, a `str` return where the neighbour
returned `bytes`. Nine modules held the same two-line
`_json_sha256 = _sha256(_canonical_json(value))` pair verbatim. Several of the
resulting digests are *persisted and later compared* — bundle manifests,
`policy_sha256`, schema fingerprints, deduplication identities — so the
encoding is not an implementation detail a module may pick for itself.

This module owns every JSON digest encoding the codebase uses and nothing
else, so it can never become the place where a decision converges. Callers
use `canonical_sha256` unless a retained receipt forces otherwise; the
retained encodings live here under explicit names, each one saying which
receipts hold it in place, so the set can only shrink and never silently
diverge again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

__all__ = [
    "ascii_escaped_json",
    "ascii_escaped_sha256",
    "canonical_json",
    "canonical_sha256",
    "coerced_ascii_escaped_sha256",
    "coerced_json",
    "coerced_sha256",
    "is_digest",
    "sha256_bytes",
    "sha256_file",
]

_CHUNK_BYTES = 1024 * 1024


def canonical_json(value: object) -> bytes:
    """The one encoding, so one value has one digest.

    `ensure_ascii=False` because escaping is a rendering choice and must not
    change what a digest covers: an External Party's name is the same name
    whether or not its accented characters arrive as `\\uXXXX`.
    `allow_nan=False` because `NaN` and `Infinity` are not JSON, and a receipt
    that cannot be re-parsed cannot be re-verified.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def canonical_sha256(value: object) -> str:
    """The digest of a policy, a configuration, or a set of fields."""
    return sha256_bytes(canonical_json(value))


def sha256_bytes(data: bytes) -> str:
    """The digest of exact bytes, whatever produced them."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """The digest of a file, read in chunks so object size is not held in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_digest(value: object, length: int = 64) -> bool:
    """Whether a value is exactly a lowercase hexadecimal digest of `length`."""
    return (
        isinstance(value, str)
        and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


# --- Retained encodings ---------------------------------------------------
#
# Each of these differs from `canonical_json` for some inputs and is kept only
# because a value it already produced is stored somewhere and compared later.
# Naming them here is the point: the difference is declared, not discovered.


def ascii_escaped_json(value: object) -> bytes:
    """`canonical_json` with non-ASCII escaped as `\\uXXXX`.

    Retained receipts that hold this in place: SH99 coordinator-rehearsal
    bundle manifests and their `canonical_content_sha256`
    (`docs/nhhip-workflow-rehearsal.md` pins published rehearsal digests),
    Adjustment-cohort membership identities (`cohort.py`, a stored
    deduplication key), email and minutes spine delivery identities
    (`email_spine.py`, `minutes_spine.py`, stored idempotency keys over
    customer text that routinely carries non-ASCII), native reader-coverage
    authority digests, and disposition/inventory receipts.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def ascii_escaped_sha256(value: object) -> str:
    """The digest of `ascii_escaped_json`; see its retained-receipt list."""
    return sha256_bytes(ascii_escaped_json(value))


def coerced_json(value: object) -> bytes:
    """`canonical_json` that stringifies whatever JSON cannot hold.

    `default=str` is not interchangeable with the canonical encoding: it
    accepts a `datetime` or a `Decimal` where the canonical encoding refuses
    one. Retained by bounded-explanation receipts and the untrusted-snapshot
    text handed to a model, both of which carry typed configuration objects.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()


def coerced_sha256(value: object) -> str:
    """The digest of `coerced_json`; see its retained-receipt list."""
    return sha256_bytes(coerced_json(value))


def coerced_ascii_escaped_sha256(value: object) -> str:
    """`ascii_escaped_sha256` that stringifies whatever JSON cannot hold.

    Retained by AWS environment-rehearsal query digests
    (`environment_rehearsal.py`, compared against digests recorded in a
    rehearsal receipt before the restore is accepted) and pilot-measurement
    receipts, both of which digest provider rows holding timestamps.
    """
    return sha256_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    )
