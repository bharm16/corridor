"""Validate the frozen bake-off JSON Schema without third-party dependencies."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("result-schema.v1.json")


class ContractError(ValueError):
    """A result does not conform to the frozen evidence contract."""


def validate_result(value: object) -> None:
    """Reject undeclared fields, missing fields, and ambiguous outcomes."""

    schema = json.loads(SCHEMA_PATH.read_text())
    _validate(value, schema, "$")
    assert isinstance(value, dict)
    deterministic = value["deterministic_output"]
    expected = repeatability_digest(deterministic)
    if value["repeatability_sha256"] != expected:
        raise ContractError("$.repeatability_sha256: does not match deterministic_output")
    status = deterministic["status"]
    pages = deterministic["pages"]
    errors = deterministic["errors"]
    if status == "success" and (errors or _contains_failed_outcome(deterministic)):
        raise ContractError("$.deterministic_output: success contains a failed outcome")
    if status == "failed" and (not errors or pages):
        raise ContractError("$.deterministic_output: failure must have errors and no partial pages")
    if status == "unsupported" and (pages or not deterministic["unsupported_capabilities"]):
        raise ContractError("$.deterministic_output: unsupported must name capabilities and no pages")


def repeatability_digest(deterministic_output: object) -> str:
    """Hash canonical deterministic output; run observations never enter."""

    encoded = json.dumps(
        deterministic_output, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _contains_failed_outcome(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("status") == "failed":
            return True
        return any(_contains_failed_outcome(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_failed_outcome(item) for item in value)
    return False


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if alternatives:
        errors = []
        matched = 0
        for candidate in alternatives:
            try:
                _validate(value, candidate, path)
                matched += 1
            except ContractError as exc:
                errors.append(str(exc))
        required_matches = 1 if "oneOf" in schema else None
        if (required_matches and matched != 1) or (not required_matches and not matched):
            raise ContractError(f"{path}: no unambiguous schema alternative: {errors}")
        return
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path}: value is not declared")
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_is_type(value, item) for item in expected):
            raise ContractError(f"{path}: wrong type")
    elif expected and not _is_type(value, expected):
        raise ContractError(f"{path}: expected {expected}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", ())) - set(value)
        if missing:
            raise ContractError(f"{path}: missing fields {sorted(missing)}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise ContractError(f"{path}: undeclared fields {sorted(extra)}")
        for name, item in value.items():
            child = properties.get(name)
            if child is None and isinstance(schema.get("additionalProperties"), dict):
                child = schema["additionalProperties"]
            if child is not None:
                _validate(item, child, f"{path}.{name}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ContractError(f"{path}: invalid item count")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ContractError(f"{path}: items must be unique")
        for index, item in enumerate(value):
            if index < len(schema.get("prefixItems", ())):
                child = schema["prefixItems"][index]
            else:
                child = schema.get("items")
            if child is False:
                raise ContractError(f"{path}: too many items")
            if isinstance(child, dict):
                _validate(item, child, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ContractError(f"{path}: string is empty")
        if pattern := schema.get("pattern"):
            if re.match(pattern, value) is None:
                raise ContractError(f"{path}: string does not match {pattern}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ContractError(f"{path}: below exclusive minimum")


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]
