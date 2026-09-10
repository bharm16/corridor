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

from corridor.llm import RequestConfiguration


_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Exact code sources that can change Candidate meaning after the provider has
# returned structured JSON.  Prompts and schemas are sealed separately; this
# list owns deterministic normalization, validation, mapping, and Candidate
# construction.  Adding a semantic dependency here changes the digest without
# pretending old receipts contained a fact they did not capture.
# The deployed extractors that read a source deterministically and call no
# model at all. They are the only ones a config may name without a model.
NATIVE_EXTRACTORS = frozenset({"sheet", "baseline", "key_date_table"})

# The measured native-matrix request, declared once. It was a bare model
# string compared here and repeated in the provider posture, the replay
# client and the measurement adapter; a replay adapter had to reproduce the
# literal to be accepted at all. `tests/test_llm.py` holds the posture and
# this constant to the same values.
DEPLOYED_NATIVE_MATRIX_REQUEST = RequestConfiguration(
    model="gpt-5.6-luna", effort="none", flex=False,
    base_url="https://api.openai.com/v1",
)

# The schema every postprocessor writes through. Card 21 split `models.py` into
# one module per bounded context, so the sealed reading names the package rather
# than one file. It is globbed rather than listed so it keeps meaning "every byte
# of the schema": a listed set would let a family module added later fall
# silently outside the reading a run is sealed under.
_MODELS_SOURCES = tuple(
    f"src/corridor/models/{path.name}"
    for path in sorted((_REPO_ROOT / "src" / "corridor" / "models").glob("*.py"))
)

_POSTPROCESSOR_SOURCES = {
    "native_matrix": (
        "src/corridor/native_matrix.py",
        "src/corridor/native_matrix_bindings.py",
        "src/corridor/materializer.py",
        "src/corridor/fact_types.py",
        "src/corridor/facts.py",
        "src/corridor/extraction_runs.py",
        "src/corridor/source_append.py",
        "src/corridor/row_accounting.py",
        "src/corridor_pdf_reader/replacement/semantics.py",
        "src/corridor_pdf_reader/replacement/pages.py",
        "src/corridor_pdf_reader/replacement/vocabulary.py",
    ),
    "minutes": (
        "src/corridor/extract_minutes_v5.py",
        "src/corridor/statement_timing_parser.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        *_MODELS_SOURCES,
        "src/corridor/llm.py",
    ),
    "agreement": (
        "src/corridor/extract_agreement.py",
        "src/corridor/extract_batch.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        *_MODELS_SOURCES,
        "src/corridor/llm.py",
    ),
    "sheet": (
        "src/corridor/extract_sheet.py",
        "src/corridor/pipeline.py",
        "src/corridor/sheets.py",
        "src/corridor/vocabulary.py",
        "src/corridor/verify.py",
        "src/corridor/candidates.py",
        *_MODELS_SOURCES,
    ),
    # The Adopt Baseline importer (#509). It uses no model either, and its
    # reading is sealed for the same reason the native sheet reader's is: a
    # baseline adopted under one reading and a later revision read under
    # another are not comparable, and the accepted record is what is at stake.
    "baseline": (
        "src/corridor/baseline_workbook.py",
        "src/corridor/baseline_adoption.py",
        "src/corridor/sheets.py",
        "src/corridor/source_segments.py",
        "src/corridor/vocabulary.py",
        "src/corridor/materializer.py",
        *_MODELS_SOURCES,
    ),
    # The Key Date table reader (#450). No model either: a schedule export's
    # three declared columns arrive in coordination vocabulary, so nothing on
    # this path chooses what a heading means.
    "key_date_table": (
        "src/corridor/key_date_table.py",
        "src/corridor/source_segments.py",
        "src/corridor/materializer.py",
        *_MODELS_SOURCES,
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

    if extractor in {"matrix", "native_matrix"}:
        return deployed_native_matrix_config(client=client)
    if extractor == "minutes":
        from corridor import extract_minutes_v5 as module

        return _deployed_config(
            extractor=extractor,
            prompt_version=module.PROMPT_VERSION,
            model=_configuration(client).model,
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
            model=_configuration(client).model,
            schema_version=extract_agreement.PROMPT_VERSION,
            prompt_bytes=(_REPO_ROOT / extract_agreement.PROMPT_PATH).read_bytes(),
            schema=extract_agreement.SCHEMA,
            request_controls=_model_request_controls(
                client,
                image_detail=None,
                logprobs=False,
            ),
        )
    if extractor == "baseline":
        from corridor import baseline_workbook

        if client is not None:
            raise ValueError("the Adopt Baseline importer cannot have a model client")
        return _deployed_config(
            extractor=extractor,
            prompt_version=baseline_workbook.IMPORTER_VERSION,
            model=None,
            schema_version=baseline_workbook.IMPORTER_VERSION,
            prompt_bytes=b"Corridor Adopt Baseline importer; no model prompt.\n",
            schema={
                "type": "adopted-baseline",
                "schema_version": baseline_workbook.IMPORTER_VERSION,
            },
            request_controls={"provider": "native", "model_requests": 0},
        )
    if extractor == "key_date_table":
        from corridor import key_date_table

        if client is not None:
            raise ValueError("the Key Date table reader cannot have a model client")
        return _deployed_config(
            extractor=extractor,
            prompt_version=key_date_table.READER_VERSION,
            model=None,
            schema_version=key_date_table.READER_VERSION,
            prompt_bytes=b"Corridor Key Date table reader; no model prompt.\n",
            schema={
                "type": "key-date-table",
                "schema_version": key_date_table.READER_VERSION,
                "columns": list(key_date_table.DECLARED_COLUMNS),
            },
            request_controls={"provider": "native", "model_requests": 0},
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


def deployed_native_matrix_config(*, client: object) -> ExtractorConfig:
    """Seal the measured ID-mapping reader; selection is a separate boundary."""
    from corridor.native_matrix_bindings import MODEL_IMAGE_DPI, NATIVE_MATRIX_SCHEMA_VERSION
    from corridor_pdf_reader.replacement.semantics import (
        PROMPT_PATH, PROMPT_VERSION, STRUCTURE_SCHEMA,
    )

    configuration = _configuration(client)
    if configuration != DEPLOYED_NATIVE_MATRIX_REQUEST:
        raise ValueError("native matrix challenger requires its measured model and reasoning configuration")
    controls = _model_request_controls(client, image_detail="original", logprobs=False)
    return injected_extractor_config(
        extractor="native_matrix", prompt_version=PROMPT_VERSION,
        model=configuration.model, schema_version=NATIVE_MATRIX_SCHEMA_VERSION,
        prompt_bytes=PROMPT_PATH.read_bytes(), schema=STRUCTURE_SCHEMA,
        postprocessor_bytes=_read_sources(_REPO_ROOT, _POSTPROCESSOR_SOURCES["native_matrix"]),
        request_controls={
            **controls, "model_image_dpi": MODEL_IMAGE_DPI,
            "native_reader_engine": "tagged", "native_reader_dpi": 36,
            "selection": "explicit_selection_required",
        },
        runtime=_runtime_receipt(("pypdfium2", "pypdf", "Pillow", "httpx")),
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
    if extractor not in NATIVE_EXTRACTORS and model is None:
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


def _configuration(client: object | None) -> RequestConfiguration:
    """The client's own statement of what it will request.

    This used to be four `getattr` calls against an untyped object, so every
    offline adapter had to grow four attributes describing a provider it never
    reached, and a missing one produced a receipt that disagreed with the run.
    """
    if client is None:
        raise ValueError("a model-backed deployed extractor requires its client")
    stated = getattr(client, "configuration", None)
    if not callable(stated):
        raise ValueError("a model client must state its request configuration")
    configuration = stated()
    if not isinstance(configuration, RequestConfiguration):
        raise ValueError("a model client's configuration must be a RequestConfiguration")
    return configuration


def _model_request_controls(
    client: object | None,
    *,
    image_detail: str | None,
    logprobs: bool | Mapping[str, bool],
) -> dict[str, Any]:
    configuration = _configuration(client)
    parsed_provider = urlsplit(configuration.base_url)
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
        "reasoning_effort": configuration.effort,
        "image_detail": image_detail,
        "store": False,
        "strict": True,
        "flex": configuration.flex,
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


def _runtime_receipt(distributions=("pypdfium2", "pypdf", "httpx", "openpyxl")) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for distribution in distributions:
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
