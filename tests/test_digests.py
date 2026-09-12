"""One digest per value, and a declared list of the encodings still elsewhere.

`corridor.policy` consolidated three canonical-JSON encodings because for any
value carrying a character outside ASCII they disagreed, so the same policy
JSON hashed to two values depending on which module asked. The same copy had
happened fifty more times by the time `corridor.digests` was written. These
prove the consolidation holds at the seams that matter — one digest across
callers, `NaN` refused, bytes and text agreeing — and the guard at the end
keeps the count of remaining private encodings falling rather than climbing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from corridor import (
    digests,
    disposition_contracts,
    m8_acceptance,
    object_storage,
    pipeline_contracts,
    policy,
    reference_methods,
    sh99_coordinator_rehearsal,
)
from source_scan_support import python_files, read_python, source_scan_cache  # noqa: F401

_SRC = Path(__file__).resolve().parents[1] / "src" / "corridor"
NON_ASCII = {"party": "Cañada de los Alamos", "note": "±3 días", "count": 2}


def test_one_non_ascii_value_has_one_digest_across_every_caller():
    """The exact failure `policy.py` records, asserted across four modules."""

    expected = digests.canonical_sha256(NON_ASCII)

    assert policy.canonical_sha256(NON_ASCII) == expected
    assert m8_acceptance._json_sha256(NON_ASCII) == expected
    assert pipeline_contracts.content_digest(NON_ASCII) == expected
    assert digests.canonical_json(NON_ASCII).decode().count("\\u") == 0


def test_the_retained_escaped_encoding_is_a_different_digest_by_declaration():
    """Kept only where a stored value depends on it, and never confusable.

    `artifacts/product-proving/sh99-8da8568-extraction-repeatability-failed`
    holds a bundle whose `canonical_content_sha256` only the escaped encoding
    reproduces, which is why the variant exists at all.
    """

    escaped = digests.ascii_escaped_sha256(NON_ASCII)

    assert escaped != digests.canonical_sha256(NON_ASCII)
    assert "\\u" in digests.ascii_escaped_json(NON_ASCII).decode()
    assert sh99_coordinator_rehearsal._json_sha256(NON_ASCII) == escaped
    assert disposition_contracts.json_digest(NON_ASCII) == escaped


def test_an_ascii_value_hashes_identically_under_both_encodings():
    """Why most call sites could be moved at all: the bytes do not change."""

    value = {"revision": "e255a7c4d9e2", "carried": 3, "ready": True}

    assert digests.ascii_escaped_sha256(value) == digests.canonical_sha256(value)


def test_a_value_json_cannot_hold_is_refused_rather_than_written():
    for value in ({"ratio": float("nan")}, {"ratio": float("inf")}, [float("-inf")]):
        with pytest.raises(ValueError):
            digests.canonical_json(value)
        with pytest.raises(ValueError):
            digests.canonical_sha256(value)
        with pytest.raises(ValueError):
            digests.ascii_escaped_sha256(value)
        with pytest.raises(ValueError):
            policy.canonical_json(value)


def test_bytes_and_text_round_trip_to_the_same_digest(tmp_path):
    raw = digests.canonical_json(NON_ASCII)

    assert digests.sha256_bytes(raw) == digests.canonical_sha256(NON_ASCII)
    assert policy.canonical_json(NON_ASCII).encode() == raw

    path = tmp_path / "canonical.json"
    path.write_bytes(raw)

    assert digests.sha256_file(path) == digests.sha256_bytes(raw)
    assert object_storage.digest_bytes(raw) == digests.sha256_bytes(raw)
    assert object_storage.digest_file(path) == digests.sha256_bytes(raw)


def test_a_digest_is_recognised_by_one_predicate():
    digest = digests.canonical_sha256(NON_ASCII)

    assert digests.is_digest(digest)
    assert reference_methods.is_digest(digest)
    assert not digests.is_digest(digest.upper())
    assert not digests.is_digest(digest[:63])
    assert digests.is_digest("a" * 40, 40)


# --- The guard ------------------------------------------------------------
#
# Every entry below is a module that still computes a canonical-JSON digest
# without `corridor.digests`. The list is asserted exactly, so a new private
# encoding fails this test and a converted one has to be deleted from here.
#
# All of these are the ASCII-escaped encoding over a value that is stored and
# compared later — a content fingerprint, an identity column, a receipt digest
# — so each is a `digests.ascii_escaped_sha256` (or, where `default=str`
# appears, `coerced_ascii_escaped_sha256`) call that has not been made yet, not
# a disagreement to resolve. They are listed with what holds them here.
PRIVATE_JSON_DIGESTS = {
    # Stored fingerprints and identity columns on domain readings.
    "accepted_field_reading.py": "AcceptedFieldPopulation fingerprint",
    "baseline_adoption.py": "baseline binding and preview fingerprints",
    "coordination_summary.py": "publication fingerprint",
    "facts.py": "fact digest input",
    "follow_up_bundles.py": "bundle body content_sha256",
    "issue_rendering.py": "preparation payload digest",
    "key_date_table.py": "key-date reader identity",
    "later_revision.py": "importer reading identity",
    "measurement_collection.py": "customer-database identity",
    "release_candidate.py": "release-candidate state_sha256",
    "release_preparation_supervisor.py": "supervised occurrence identity",
    "report_release.py": "released report snapshot_sha256",
    "product_proving_frontend_capture.py": "retained report snapshot_sha256",
    "subject_resolution.py": "resolution payload digest",
    "unreadable_cells.py": "unreadable-cell payload digest (default=str)",
    "evidence_investigator.py": "investigation payload digest (default=str)",
    "web/operations_view.py": "recorded policy-choice digest",
    # Digests behind a module's own value normalizer, where the normalization
    # and not the encoding is what the module owns.
    "disposition_contracts.py": "dataclass sha256 property over asdict",
    "legacy_ledger_archive.py": "_canonical_bytes over JSONB-normalized rows",
    "native_matrix_measurement.py": "canonical_bytes over measurement rows",
    "native_reference.py": "rules_sha256 over normalized method rules",
    "pipeline_comparison.py": "expected/actual comparison digests",
    "shadow_comparison.py": "frozen payload content_sha256",
    "shadow_processing.py": "shadow canonical_bytes digest",
    "token_layers.py": "token-layer content_sha256",
    "workbook_render.py": "retirement content_sha256",
    # Provider and evaluation receipts.
    "connectors/microsoft365.py": "replay transport content_sha256",
    "m365_replay.py": "Graph replay configuration digest",
    "render_profiles.py": "routed render identity in an output filename",
    # Retained released migration bytes; Alembic never loads this directory
    # and the recorded policy fingerprint depends on the exact bytes.
    "migrations/baseline_versions/b2d5f8a1c4e7_source_append_commands.py": (
        "released migration source bytes"
    ),
    # A frozen version digest of the released heading vocabulary, embedded in
    # the capture-correction command so "which vocabulary said so" is stable
    # released history rather than a helper that could re-encode it (#945).
    "migrations/source_append_commands/capture_correction_retirement.py": (
        "heading vocabulary version digest"
    ),
}


def _function_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _modules_computing_their_own_json_digest() -> dict[str, list[int]]:
    """Where a `sha256` call's own arguments still build canonical JSON.

    A module-level function that calls `json.dumps` counts as building it, so a
    two-line `_sha256(_canonical_json(value))` pair is found the same way the
    inline expression is.
    """

    found: dict[str, list[int]] = {}
    for path in python_files(_SRC):
        source = read_python(path)
        encoders = {
            node.name
            for node in ast.walk(source.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(inner, ast.Call) and _function_name(inner.func) == "dumps"
                for inner in ast.walk(node)
            )
        }
        lines = [
            node.lineno
            for node in source.nodes
            if isinstance(node, ast.Call)
            and _function_name(node.func) == "sha256"
            and any(
                isinstance(inner, ast.Call)
                and _function_name(inner.func) in {"dumps", *encoders}
                for inner in ast.walk(node)
            )
        ]
        if lines:
            found[path.relative_to(_SRC).as_posix()] = sorted(lines)
    return found


def test_no_module_outside_digests_grows_its_own_canonical_json_digest():
    found = _modules_computing_their_own_json_digest()

    undeclared = sorted(set(found) - set(PRIVATE_JSON_DIGESTS))
    stale = sorted(set(PRIVATE_JSON_DIGESTS) - set(found))

    assert (undeclared, stale) == ([], []), (
        "the declared allowlist drifted from the source: "
        f"undeclared={undeclared}, already-converted={stale}"
    )


def test_the_owning_modules_hold_the_only_json_dumps_calls():
    """One module encodes; `policy` keeps its public names and delegates."""

    encoding_modules = {
        path.relative_to(_SRC).as_posix()
        for path in python_files(_SRC)
        for node in read_python(path).nodes
        if isinstance(node, ast.Call) and _function_name(node.func) == "dumps"
        and any(
            keyword.arg == "sort_keys" for keyword in node.keywords
        )
    } & {"digests.py", "policy.py"}

    assert encoding_modules == {"digests.py"}
