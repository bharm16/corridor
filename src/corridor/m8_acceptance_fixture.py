"""Fixture and transformation loading for M8 acceptance.

The digest functions and the refusal class used to arrive as arguments, from a
caller that held a private copy of the canonical encoding. `corridor.digests`
owns the encoding and this module owns its own refusal, so a caller that needs
its own exception type at its public seam catches `CorruptFixture` and
re-raises.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import json
from typing import Any, Callable

from corridor import digests


class CorruptFixture(ValueError):
    """A retained capture fixture is absent, mis-shaped, or off its digest."""


def load_captured_fixture(
    fixture_path: Path,
    *,
    expected_sha256: str,
    capture_schema_version: str,
    claim_boundary: dict[str, Any],
    build_chain: Callable[[dict[str, Any], Path], Any],
) -> tuple[dict[str, Any], str, Any]:
    try:
        wrapper = json.loads(Path(fixture_path).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptFixture("captured fixture is absent or invalid") from exc
    if wrapper.get("schema_version") != capture_schema_version:
        raise CorruptFixture("captured fixture schema is unsupported")
    content = wrapper.get("content")
    if not isinstance(content, dict):
        raise CorruptFixture("captured fixture content is invalid")
    actual_sha256 = digests.canonical_sha256(content)
    if wrapper.get("fixture_sha256") != actual_sha256 or actual_sha256 != expected_sha256:
        raise CorruptFixture("captured fixture digest does not match")
    if content.get("claim_boundary") != claim_boundary:
        raise CorruptFixture("captured fixture claim boundary drifted")
    if not isinstance(content.get("sources"), list) or not isinstance(
        content.get("rid_index"), dict
    ):
        raise CorruptFixture("captured fixture source records are invalid")
    if not isinstance(content.get("runs"), list) or not isinstance(
        content.get("comparisons"), list
    ):
        raise CorruptFixture("captured fixture does not contain the exact chain")
    fixture_root = Path(fixture_path).parent.resolve()
    for record in [content["rid_index"], *content["sources"]]:
        registry_id = record.get("registry_id")
        relative = record.get("fixture_relpath")
        if not isinstance(relative, str) or not relative:
            raise CorruptFixture(f"{registry_id} has no fixture path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
            raise CorruptFixture(f"{registry_id} has an unsafe fixture path")
        path = (fixture_root / Path(*pure.parts)).resolve()
        try:
            path.relative_to(fixture_root)
        except ValueError as exc:
            raise CorruptFixture(f"{registry_id} fixture path escapes") from exc
        if path.is_symlink() or not path.is_file():
            raise CorruptFixture(f"{registry_id} fixture source is absent")
        if digests.sha256_bytes(path.read_bytes()) != record.get("sha256"):
            raise CorruptFixture(f"{registry_id} fixture source hash drifted")
    chain = build_chain(content, Path(fixture_path))
    return content, actual_sha256, chain


def load_transformations(
    path: Path,
    *,
    expected_sha256: str,
    contract: dict[str, Any],
    acceptance_error_cls: type[Exception],
) -> tuple[dict[str, Any], str]:
    try:
        value = Path(path).read_bytes()
        transformations = json.loads(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise acceptance_error_cls("controlled transformations are absent or invalid") from exc
    actual_sha256 = digests.sha256_bytes(value)
    if actual_sha256 != expected_sha256:
        raise acceptance_error_cls("controlled transformation digest does not match")
    difference = first_contract_difference(
        contract,
        transformations,
        path="transformations",
    )
    if difference is not None:
        raise acceptance_error_cls(
            "controlled transformation does not match its executable schema at "
            f"{difference}"
        )
    return transformations, actual_sha256


def first_contract_difference(
    expected: Any,
    observed: Any,
    *,
    path: str,
) -> str | None:
    """Return the first missing, extra, mistyped, or changed contract path."""

    if type(observed) is not type(expected):
        return path
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(observed))
        if missing:
            return f"{path}.{missing[0]}"
        unexpected = sorted(set(observed) - set(expected))
        if unexpected:
            return f"{path}.{unexpected[0]}"
        for key in expected:
            difference = first_contract_difference(
                expected[key], observed[key], path=f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(observed) != len(expected):
            return path
        for index, (expected_item, observed_item) in enumerate(
            zip(expected, observed, strict=True)
        ):
            difference = first_contract_difference(
                expected_item,
                observed_item,
                path=f"{path}[{index}]",
            )
            if difference is not None:
                return difference
        return None
    return None if observed == expected else path
