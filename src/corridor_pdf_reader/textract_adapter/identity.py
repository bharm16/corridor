"""What a cached response is identified by, and how a reading is digested (#732, ADR-0094).

The imported client keys its cache on the PNG's SHA-256 and nothing else. That
was right for one experiment under one profile in one region, and wrong for
Corridor, where the same raster bytes can arrive under two customers' terms,
two feature sets or two adapter versions and must not share a response
across them. So a cache entry here lives in a *scope*: the authorization
boundary (record kind, project, source class, purpose, stage), the operation
and feature set, the region, the request and preprocessing configuration, and
the local adapter version with the rasterizer it ran. The raster digest is
the sixth field, per entry. The scope digest names the directory the
imported client is handed, so its own sha-only lookup is confined to entries
made under the same identity without the client changing at all.

The normalization lives here too, beside the digests, so that "the
normalized-reading digest" can only ever mean the digest of what `normalize`
produced. Both measured lane A variants (polygon margin, run-mate rescue) are
carried as configuration and are off by default, as measured.

Two things the identity deliberately does not carry: the authorization
record's own id (a re-signed record over the same boundary reuses the
retained responses rather than re-transmitting the pages, and the id is
written on every binding instead), and a pinned provider model, because
AnalyzeDocument has no selector for the TABLES model and the version each
response reports is recorded rather than promised.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass, field
from typing import Any

from corridor_pdf_reader import provenance
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE
from corridor_pdf_reader.textract.blocks import METHOD, page_from_blocks
from corridor_pdf_reader.textract.remap import TEXT_SOURCE, remap_page
from corridor_pdf_reader.textract_adapter.records import (
    FEATURE_TYPES,
    OPERATION,
    PROVIDER,
    AuthorizationRecord,
    RequestBoundary,
)

ADAPTER_VERSION = "corridor-textract-adapter-1"
MODEL_VERSION_FIELD = "AnalyzeDocumentModelVersion"
TEXTRACT_WORDS = "textract-words"
NATIVE_GLYPHS = TEXT_SOURCE


def canonical_json(value: Any) -> bytes:
    """One byte string per value: sorted keys, no whitespace, ASCII escapes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_of(value: Any) -> str:
    return sha256_hex(canonical_json(value))


@dataclass(frozen=True)
class RequestConfiguration:
    """The request parameters and the preprocessing that made the bytes sent.

    The operation and feature set are the imported client's constants and are
    not configurable here on purpose: the client sends `TABLES` and nothing
    else, and an identity that claimed otherwise would be a lie.
    """

    dpi: int = 300
    mode: str = "L"

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": OPERATION,
            "feature_types": list(FEATURE_TYPES),
            "submission": "bytes",
            "dpi": self.dpi,
            "mode": self.mode,
            "render": "pypdfium2 page.render(scale=dpi/72, draw_annots=False, rev_byteorder=True, grayscale=mode=='L')",
            "png_encoding": "PIL PNG optimize=False compress_level=6",
        }


@dataclass(frozen=True)
class NormalizationConfiguration:
    """How a response becomes the reader's page. Both measured variants off by default."""

    remap_margin: float = 0.0
    rescue_runs: bool = False

    def as_dict(self, text_source: str) -> dict[str, Any]:
        return {
            "text_source": text_source,
            "parser": METHOD,
            "remap_margin": self.remap_margin,
            "rescue_runs": self.rescue_runs,
        }


@dataclass(frozen=True)
class NativeGlyphs:
    """The reader's visible glyphs and hidden runs for one page, for the lane A re-map."""

    characters: list[dict[str, Any]] = field(default_factory=list)
    clipped: list[dict[str, Any]] = field(default_factory=list)


def rasterizer_identity() -> dict[str, Any]:
    import pypdfium2

    return {
        "module": "corridor_pdf_reader.textract.render.render_page",
        "pypdfium2": importlib.metadata.version("pypdfium2"),
        "pdfium": str(pypdfium2.PDFIUM_INFO),
        "pillow": importlib.metadata.version("pillow"),
    }


def adapter_identity() -> dict[str, Any]:
    """The local reader, rasterizer, parser and re-map, by module and version, with what is not pinned."""
    return {
        "adapter_version": ADAPTER_VERSION,
        "source_commit": provenance.SOURCE_COMMIT,
        "provider": PROVIDER,
        "operation": OPERATION,
        "feature_types": list(FEATURE_TYPES),
        "local_reader": {
            "module": "corridor_pdf_reader.replacement.reader.read_pdf",
            "engine": MEASURED_ENGINE,
            "dpi": MEASURED_DPI,
        },
        "rasterizer": rasterizer_identity(),
        "parser": {"module": "corridor_pdf_reader.textract.blocks.page_from_blocks", "method": METHOD},
        "remap": {"module": "corridor_pdf_reader.textract.remap.remap_page", "text_source": NATIVE_GLYPHS},
        "provider_model": {
            "pinned": False,
            "reported_as": MODEL_VERSION_FIELD,
            "note": (
                "AnalyzeDocument exposes no version selector for the TABLES model; "
                "the version each response reports is recorded, never promised"
            ),
        },
    }


@dataclass(frozen=True)
class CacheScope:
    """The five fields every entry in one directory shares, and their digest."""

    fields: dict[str, Any]
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.fields, "digest": self.digest}


def cache_scope(
    record: AuthorizationRecord,
    request: RequestBoundary,
    configuration: RequestConfiguration,
) -> CacheScope:
    fields = {
        "authorization_boundary": {"kind": record.kind, **request.as_dict()},
        "operation": OPERATION,
        "feature_types": list(FEATURE_TYPES),
        "region": request.region,
        "request": configuration.as_dict(),
        "adapter": {
            "adapter_version": ADAPTER_VERSION,
            "source_commit": provenance.SOURCE_COMMIT,
            "rasterizer": rasterizer_identity(),
        },
    }
    return CacheScope(fields, digest_of(fields))


def raw_response_digest(response: dict[str, Any]) -> str:
    """The digest of the retained response exactly as retained, transport metadata included."""
    return digest_of(response)


def model_version(response: dict[str, Any]) -> str | None:
    value = response.get(MODEL_VERSION_FIELD)
    return None if value is None else str(value)


def request_id(response: dict[str, Any]) -> str | None:
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict) and metadata.get("RequestId"):
        return str(metadata["RequestId"])
    return None


def normalize(
    response: dict[str, Any],
    *,
    number: int,
    size: tuple[float, float],
    rotation: int,
    glyphs: NativeGlyphs | None = None,
    normalization: NormalizationConfiguration = NormalizationConfiguration(),
) -> dict[str, Any]:
    """The response as the reader's page; with glyphs, the lane A re-map over them."""
    page = page_from_blocks(response.get("Blocks") or [], number=number, size=size, rotation=rotation)
    if glyphs is None:
        return page
    return remap_page(
        page,
        glyphs.characters,
        glyphs.clipped,
        normalization.remap_margin,
        normalization.rescue_runs,
    )


def reading_digest(page: dict[str, Any]) -> str:
    return digest_of(page)


def reading_counts(page: dict[str, Any]) -> dict[str, int]:
    return {
        "tables": len(page["tables"]),
        "cells": sum(len(table["cells"]) for table in page["tables"]),
        "outside": len(page["outside"]),
        "clipped": len(page["clipped"]),
    }


def normalization_record(normalization: NormalizationConfiguration, glyphs: NativeGlyphs | None) -> dict[str, Any]:
    return normalization.as_dict(NATIVE_GLYPHS if glyphs is not None else TEXTRACT_WORDS)

