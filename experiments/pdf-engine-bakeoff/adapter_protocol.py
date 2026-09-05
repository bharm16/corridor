"""Durable adapter boundary for isolated PDF-engine evidence.

Adapters return contract-shaped deterministic data and variable observations
separately.  They never write Corridor state and are always invoked by the
subprocess runner, never in the parent experiment process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AdapterRequest:
    source: Path
    source_sha256: str
    password: str | None = None
    clip: tuple[float, float, float, float] | None = None
    render_dpis: tuple[int, ...] = (150, 200, 300)


@dataclass(frozen=True)
class AdapterResponse:
    deterministic_output: dict[str, Any]
    operation_ms: dict[str, float]
    output_bytes: int


@runtime_checkable
class PdfEngineAdapter(Protocol):
    """The adapter shape challenger implementations must retain."""

    engine: str
    adapter_version: str

    def execute(self, request: AdapterRequest) -> AdapterResponse: ...

