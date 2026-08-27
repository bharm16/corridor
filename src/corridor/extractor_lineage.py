"""Exact extractor-time configuration and token-usage receipts.

An Extraction Run may outlive the checkout that produced it.  This module
therefore turns the deployed prompt, strict schema, semantic rule sources,
and request controls into one immutable value *before* extraction begins.
Readers compare the stored receipt; they never reopen today's files to
reconstruct yesterday's configuration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
from pathlib import Path
import platform
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MATRIX_PROMPT_PATHS = (
    "prompts/matrix_structure_v3.md",
    "prompts/matrix_v1.md",
)

# Exact code sources that can change Candidate meaning after the provider has
# returned structured JSON.  Prompts and schemas are sealed separately; this
# list owns deterministic normalization, validation, mapping, and Candidate
# construction.  Adding a semantic dependency here changes the digest without
# pretending old receipts contained a fact they did not capture.
_POSTPROCESSOR_SOURCES = {
    "matrix": (
        "src/corridor/extract_matrix.py",
        "src/corridor/pipeline.py",
        "src/corridor/geometry.py",
        "src/corridor/vocabulary.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
        "src/corridor/llm.py",
    ),
    "minutes": (
        "src/corridor/extract_minutes_v5.py",
        "src/corridor/extract_minutes_v4.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
        "src/corridor/llm.py",
    ),
    "minutes_v4": (
        "src/corridor/extract_minutes_v4.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
        "src/corridor/llm.py",
    ),
    "minutes_v3": (
        "src/corridor/extract_minutes.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
        "src/corridor/llm.py",
    ),
    "agreement": (
        "src/corridor/extract_agreement.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
        "src/corridor/llm.py",
    ),
    "sheet": (
        "src/corridor/extract_sheet.py",
        "src/corridor/pipeline.py",
        "src/corridor/sheets.py",
        "src/corridor/vocabulary.py",
        "src/corridor/geometry.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        "src/corridor/models.py",
    ),
}


@dataclass(frozen=True)
class ExtractorConfig:
    """The sealed configuration persisted on one Extraction Run."""

    prompt_version: str
    model: str | None
    schema_version: str
    prompt_sha256: str
    schema_sha256: str
    postprocessor_sha256: str
    config_json: dict[str, Any]
    config_sha256: str


def validate_config_json_shape(config_json: Mapping[str, Any]) -> None:
    """Validate independently inspectable metadata inside a config receipt."""

    if config_json.get("receipt_version") != 1:
        raise ValueError("extractor configuration receipt_version must be 1")
    extractor = config_json.get("extractor")
    if not isinstance(extractor, str) or not extractor.strip():
        raise ValueError("extractor configuration must name its extractor")
    if not isinstance(config_json.get("request_controls"), Mapping):
        raise ValueError("extractor configuration request_controls must be an object")
    runtime = config_json.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("extractor configuration runtime must be an object")
    for name in ("python_implementation", "python_version"):
        value = runtime.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"extractor configuration runtime {name} must be non-empty")
    lock_sha256 = runtime.get("dependency_lock_sha256")
    if not _is_sha256(lock_sha256):
        raise ValueError("extractor configuration runtime lock SHA-256 is invalid")
    packages = runtime.get("packages")
    if not isinstance(packages, Mapping) or any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(value, str)
        or not value.strip()
        for name, value in (packages.items() if isinstance(packages, Mapping) else ())
    ):
        raise ValueError("extractor configuration runtime packages are invalid")


def validate_runtime_config(
    config: ExtractorConfig,
    *,
    prompt_version: str,
    model: str | None,
    prompt_bytes: bytes,
    schema: Mapping[str, Any],
) -> None:
    """Refuse when a caller's actual request differs from the sealed sources."""

    if config.prompt_version != prompt_version:
        raise ValueError("runtime prompt_version does not match its extractor seal")
    if config.model != model:
        raise ValueError("runtime model does not match its extractor seal")
    if _sha256(prompt_bytes) != config.prompt_sha256:
        raise ValueError("runtime prompt bytes do not match their extractor seal")
    if _sha256(canonical_json_bytes(schema)) != config.schema_sha256:
        raise ValueError("runtime JSON schema does not match its extractor seal")


def deployed_extractor_config(
    extractor: str,
    *,
    client: object | None,
) -> ExtractorConfig:
    """Seal one current deployed extractor from the central source registry."""

    if extractor == "matrix":
        from corridor import extract_matrix

        return deployed_matrix_config(
            client=client,
            structure_system=extract_matrix.STRUCTURE_PROMPT.read_text(),
            transcribe_system=extract_matrix.TRANSCRIBE_PROMPT.read_text(),
            structure_schema=extract_matrix.STRUCTURE_SCHEMA,
            transcribe_schema=extract_matrix.TRANSCRIBE_SCHEMA,
        )
    if extractor in {"minutes", "minutes_v4", "minutes_v3"}:
        module_name = {
            "minutes": "corridor.extract_minutes_v5",
            "minutes_v4": "corridor.extract_minutes_v4",
            "minutes_v3": "corridor.extract_minutes",
        }[extractor]
        module = __import__(module_name, fromlist=["*"])
        return _deployed_config(
            extractor=extractor,
            prompt_version=module.PROMPT_VERSION,
            model=_model(client),
            schema_version=module.PROMPT_VERSION,
            prompt_bytes=(_REPO_ROOT / module.PROMPT_PATH).read_bytes(),
            schema=module.SCHEMA,
            request_controls=_model_request_controls(
                client,
                image_detail=None,
                logprobs=False,
            ),
        )
    if extractor == "agreement":
        from corridor import extract_agreement

        return _deployed_config(
            extractor=extractor,
            prompt_version=extract_agreement.PROMPT_VERSION,
            model=_model(client),
            schema_version=extract_agreement.PROMPT_VERSION,
            prompt_bytes=(_REPO_ROOT / extract_agreement.PROMPT_PATH).read_bytes(),
            schema=extract_agreement.SCHEMA,
            request_controls=_model_request_controls(
                client,
                image_detail=None,
                logprobs=False,
            ),
        )
    if extractor == "sheet":
        from corridor import extract_sheet

        if client is not None:
            raise ValueError("the native sheet extractor cannot have a model client")
        return _deployed_config(
            extractor=extractor,
            prompt_version=extract_sheet.PROMPT_VERSION,
            model=None,
            schema_version=extract_sheet.SCHEMA_VERSION,
            prompt_bytes=b"Corridor native workbook reader; no model prompt.\n",
            schema={
                "type": "native-workbook",
                "schema_version": extract_sheet.SCHEMA_VERSION,
            },
            request_controls={"provider": "native", "model_requests": 0},
        )
    raise ValueError(f"unknown deployed extractor {extractor!r}")


def deployed_matrix_config(
    *,
    client: object | None,
    structure_system: str,
    transcribe_system: str,
    structure_schema: Mapping[str, Any],
    transcribe_schema: Mapping[str, Any],
) -> ExtractorConfig:
    """Seal the exact Matrix prompts/schemas the route will pass to the client."""

    from corridor import extract_matrix

    prompt_sources = {
        _MATRIX_PROMPT_PATHS[0]: structure_system.encode("utf-8"),
        _MATRIX_PROMPT_PATHS[1]: transcribe_system.encode("utf-8"),
    }
    return _deployed_config(
        extractor="matrix",
        prompt_version=extract_matrix.PROMPT_VERSION,
        model=_model(client),
        schema_version=extract_matrix.SCHEMA_VERSION,
        prompt_bytes=_named_source_bytes(prompt_sources),
        schema={
            "structure": structure_schema,
            "transcribe": transcribe_schema,
        },
        request_controls=_model_request_controls(
            client,
            image_detail="original",
            logprobs={"structure": False, "transcribe": True},
        ),
    )


def _deployed_config(
    *,
    extractor: str,
    prompt_version: str,
    model: str | None,
    schema_version: str,
    prompt_bytes: bytes,
    schema: Mapping[str, Any],
    request_controls: Mapping[str, Any],
) -> ExtractorConfig:
    if extractor != "sheet" and model is None:
        raise ValueError("a model-backed deployed extractor must name its model")
    return injected_extractor_config(
        extractor=extractor,
        prompt_version=prompt_version,
        model=model,
        schema_version=schema_version,
        prompt_bytes=prompt_bytes,
        schema=schema,
        postprocessor_bytes=_read_sources(
            _REPO_ROOT,
            _POSTPROCESSOR_SOURCES[extractor],
        ),
        request_controls=request_controls,
    )


def _model(client: object | None) -> str | None:
    model = getattr(client, "model", None)
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("extractor model must be a non-empty string or null")
    return model


def _model_request_controls(
    client: object | None,
    *,
    image_detail: str | None,
    logprobs: bool | Mapping[str, bool],
) -> dict[str, Any]:
    if client is None:
        raise ValueError("a model-backed deployed extractor requires its client")
    effort = getattr(client, "effort", None)
    flex = getattr(client, "flex", None)
    provider_base_url = getattr(client, "base_url", None)
    if not isinstance(effort, str) or not effort:
        raise ValueError("reasoning effort must be a non-empty string")
    if not isinstance(flex, bool):
        raise ValueError("flex must be boolean")
    if not isinstance(provider_base_url, str) or not provider_base_url.strip():
        raise ValueError("provider base URL must be a non-empty string")
    parsed_provider = urlsplit(provider_base_url)
    if parsed_provider.username is not None or parsed_provider.password is not None:
        raise ValueError("provider base URL must not contain credentials")
    if (
        parsed_provider.scheme not in {"http", "https"}
        or not parsed_provider.hostname
        or parsed_provider.query
        or parsed_provider.fragment
    ):
        raise ValueError("provider base URL must be an HTTP(S) endpoint without a query")
    normalized_provider = urlunsplit(
        (
            parsed_provider.scheme.lower(),
            parsed_provider.netloc.lower(),
            parsed_provider.path.rstrip("/"),
            "",
            "",
        )
    )
    return {
        "api": "responses",
        "provider_base_url": normalized_provider,
        "reasoning_effort": effort,
        "image_detail": image_detail,
        "store": False,
        "strict": True,
        "flex": flex,
        "logprobs": logprobs,
    }


def canonical_json_bytes(value: Any) -> bytes:
    """One byte representation for digests and JSONB receipt comparisons."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def injected_extractor_config(
    *,
    extractor: str,
    prompt_version: str,
    model: str | None,
    schema_version: str,
    prompt_bytes: bytes,
    schema: Mapping[str, Any],
    postprocessor_bytes: bytes,
    request_controls: Mapping[str, Any],
    runtime: Mapping[str, Any] | None = None,
) -> ExtractorConfig:
    """Seal explicit sources, including fixtures not in the deployed registry."""

    required = {
        "extractor": extractor,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
    }
    for name, value in required.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty")
    if not isinstance(prompt_bytes, bytes) or not prompt_bytes:
        raise ValueError("prompt_bytes must be non-empty bytes")
    if not isinstance(postprocessor_bytes, bytes) or not postprocessor_bytes:
        raise ValueError("postprocessor_bytes must be non-empty bytes")
    if not isinstance(schema, Mapping):
        raise TypeError("schema must be a mapping")
    if not isinstance(request_controls, Mapping):
        raise TypeError("request_controls must be a mapping")
    runtime_values = _runtime_receipt() if runtime is None else runtime
    if not isinstance(runtime_values, Mapping):
        raise TypeError("runtime must be a mapping")

    prompt_sha256 = _sha256(prompt_bytes)
    schema_sha256 = _sha256(canonical_json_bytes(schema))
    postprocessor_sha256 = _sha256(postprocessor_bytes)
    config_json = {
        "receipt_version": 1,
        "extractor": extractor,
        "prompt_version": prompt_version,
        "model": model,
        "schema_version": schema_version,
        "prompt_sha256": prompt_sha256,
        "schema_sha256": schema_sha256,
        "postprocessor_sha256": postprocessor_sha256,
        "request_controls": json.loads(canonical_json_bytes(request_controls)),
        "runtime": json.loads(canonical_json_bytes(runtime_values)),
    }
    validate_config_json_shape(config_json)
    return ExtractorConfig(
        prompt_version=prompt_version,
        model=model,
        schema_version=schema_version,
        prompt_sha256=prompt_sha256,
        schema_sha256=schema_sha256,
        postprocessor_sha256=postprocessor_sha256,
        config_json=config_json,
        config_sha256=_sha256(canonical_json_bytes(config_json)),
    )


def usage_snapshot(client: object) -> dict[str, int] | None:
    """Copy cumulative provider counters without retaining a mutable object."""

    usage = getattr(client, "usage", None)
    if usage is None:
        return None
    result: dict[str, int] = {}
    for name in _USAGE_FIELDS:
        value = getattr(usage, name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        result[name] = value
    return result


def token_usage_delta(
    before: Mapping[str, int] | None,
    after: Mapping[str, int] | None,
    *,
    document_ids: Sequence[int],
) -> dict[str, Any]:
    """Record exact run/batch usage; never apportion a pooled counter."""

    members = list(document_ids)
    if not members or any(
        isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id <= 0
        for document_id in members
    ):
        raise ValueError(
            "token usage requires exact positive integer document membership"
        )
    if len(set(members)) != len(members):
        raise ValueError("token usage document membership cannot repeat")
    scope = "run" if len(members) == 1 else "batch"
    receipt: dict[str, Any] = {
        "scope": scope,
        "document_ids": members,
    }
    if before is None or after is None:
        receipt.update(
            {
                "measurement": "unavailable",
                "reason": "client did not expose exact cumulative token counters",
            }
        )
        return receipt

    receipt["measurement"] = "exact"
    for name in _USAGE_FIELDS:
        start = before.get(name)
        finish = after.get(name)
        if (
            isinstance(start, bool)
            or isinstance(finish, bool)
            or not isinstance(start, int)
            or not isinstance(finish, int)
            or start < 0
            or finish < 0
        ):
            raise ValueError("token usage counters must be non-negative integers")
        if finish < start:
            raise ValueError(f"token usage counter {name} moved backwards")
        receipt[name] = finish - start
    return receipt


def zero_token_usage(document_id: int) -> dict[str, Any]:
    """An exact no-provider receipt for a deterministic native reader."""

    if (
        isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id <= 0
    ):
        raise ValueError("token usage document id must be a positive integer")

    return {
        "scope": "run",
        "document_ids": [document_id],
        "measurement": "exact",
        **{name: 0 for name in _USAGE_FIELDS},
    }


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _runtime_receipt() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for distribution in ("PyMuPDF", "httpx", "openpyxl"):
        try:
            packages[distribution] = package_version(distribution)
        except PackageNotFoundError as exc:
            raise ValueError(
                f"deployed extractor dependency {distribution!r} is not installed"
            ) from exc
    lock_path = _REPO_ROOT / "uv.lock"
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "dependency_lock_sha256": _sha256(lock_path.read_bytes()),
        "packages": packages,
    }


def _named_source_bytes(sources: Mapping[str, bytes]) -> bytes:
    """Frame several named files without ambiguous concatenation."""

    framed = bytearray()
    for name, content in sorted(sources.items()):
        encoded_name = name.encode("utf-8")
        framed.extend(len(encoded_name).to_bytes(8, "big"))
        framed.extend(encoded_name)
        framed.extend(len(content).to_bytes(8, "big"))
        framed.extend(content)
    return bytes(framed)


def _read_sources(repo_root: Path, paths: Sequence[str]) -> bytes:
    sources: dict[str, bytes] = {}
    for raw_path in paths:
        path = Path(raw_path)
        resolved = path if path.is_absolute() else repo_root / path
        try:
            logical = resolved.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"deployed source {resolved} is outside the repository"
            ) from exc
        sources[logical] = resolved.read_bytes()
    return _named_source_bytes(sources)
