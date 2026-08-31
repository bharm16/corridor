"""Select measured render profiles and invoke the isolated raster worker.

The former ingest module rendered one 150-DPI PNG and silently reused it for
review, OCR, model vision, and table geometry. This module exposes four versioned
purposes selected by a checked-in gold measurement. OpenCV stays behind the
worker process and lockfile; the core only validates immutable requests and
derivative manifests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, PageRenderDerivative


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_PATH = ROOT / "gold" / "pdf" / "v1" / "render-profiles.json"
DEFAULT_MEASUREMENT_PATH = (
    ROOT / "gold" / "pdf" / "v1" / "render-profile-measurement.json"
)
DEFAULT_WORKER_PROJECT = ROOT / "workers" / "render"


class RenderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageBox(RenderModel):
    """Integer thousandths of a PDF point in page coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int

    @model_validator(mode="after")
    def is_box(self) -> "PageBox":
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("render box must have positive width and height")
        return self


class RenderProfile(RenderModel):
    name: Literal["review", "ocr_layout", "table_cv", "cell_detail"]
    profile_id: str = Field(min_length=16)
    dpi: int = Field(gt=0)
    preprocessing: tuple[str, ...] = ()
    requires_clip: bool = False
    parameters: dict


class RenderProfileBundle(RenderModel):
    schema_version: Literal["corridor.render-profiles.v1"]
    pdf_gold_dataset_version: str
    measurement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profiles: dict[str, RenderProfile]

    @model_validator(mode="after")
    def profile_keys_match(self) -> "RenderProfileBundle":
        if set(self.profiles) != {profile.name for profile in self.profiles.values()}:
            raise ValueError("render profile map keys must equal profile names")
        return self


class DpiMeasurement(RenderModel):
    dpi: int
    glyph_height_p10_pixels: float
    table_stroke_p10_pixels: float
    minimum_scanned_long_edge_pixels: int
    total_megapixels: float


class RenderProfileMeasurement(RenderModel):
    schema_version: Literal["corridor.render-profile-measurement.v1"]
    pdf_gold_dataset_version: str
    measured_at: str
    tool_versions: dict[str, str]
    cases: tuple[dict, ...]
    candidate_dpis: tuple[DpiMeasurement, ...]
    selection_rules: dict[str, str]
    selected_dpis: dict[str, int]
    limitations: tuple[str, ...]


class AffineStep(RenderModel):
    name: str
    forward: tuple[float, float, float, float, float, float]
    inverse: tuple[float, float, float, float, float, float]
    geometry_change: bool
    parameters: dict

    @staticmethod
    def _apply(matrix, point: tuple[float, float]) -> tuple[float, float]:
        a, b, c, d, e, f = matrix
        x, y = point
        return (a * x + c * y + e, b * x + d * y + f)

    def forward_point(self, point: tuple[float, float]) -> tuple[float, float]:
        return self._apply(self.forward, point)

    def inverse_point(self, point: tuple[float, float]) -> tuple[float, float]:
        return self._apply(self.inverse, point)


class ProcessingStep(RenderModel):
    name: str
    geometry_change: Literal[False]
    parameters: dict


class RegenerableSource(RenderModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1)
    profile_id: str


class RenderDerivative(RenderModel):
    schema_version: Literal["corridor.render-derivative.v1"]
    profile_name: str
    profile_id: str
    dpi: int
    preprocessing: tuple[str, ...]
    processing_steps: tuple[ProcessingStep, ...]
    retention_class: Literal["intermediary_processing"]
    regenerable_from: RegenerableSource
    artifact_path: Path
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_bytes: int = Field(gt=0)
    media_box: PageBox
    crop_box: PageBox
    clip_box: PageBox | None
    rotation_degrees: Literal[0, 90, 180, 270]
    raster_width: int = Field(gt=0)
    raster_height: int = Field(gt=0)
    transforms: tuple[AffineStep, ...]
    max_round_trip_error: float = Field(ge=0)
    measured_tolerance: float = Field(gt=0)
    library_versions: dict[str, str]
    parameters: dict

    @model_validator(mode="after")
    def artifact_and_round_trip_verify(self) -> "RenderDerivative":
        if self.max_round_trip_error > self.measured_tolerance:
            raise ValueError("render transform round-trip exceeds measured tolerance")
        if not self.artifact_path.is_file():
            raise ValueError("render derivative artifact is missing")
        digest = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        if digest != self.artifact_sha256:
            raise ValueError("render derivative artifact digest is invalid")
        return self


def load_render_profile_bundle(
    path: Path | str = DEFAULT_PROFILE_PATH,
    *,
    measurement_path: Path | str = DEFAULT_MEASUREMENT_PATH,
) -> RenderProfileBundle:
    bundle = RenderProfileBundle.model_validate_json(Path(path).read_text())
    measurement_bytes = Path(measurement_path).read_bytes()
    measurement_digest = hashlib.sha256(measurement_bytes).hexdigest()
    if bundle.measurement_sha256 != measurement_digest:
        raise ValueError("render profile selection is not bound to its measurement")
    measurement = RenderProfileMeasurement.model_validate_json(measurement_bytes)
    selected = select_profile_dpis(measurement)
    if selected != measurement.selected_dpis:
        raise ValueError("recorded render profile selection does not replay")
    if {
        name: profile.dpi for name, profile in bundle.profiles.items()
    } != selected:
        raise ValueError("render profiles disagree with the measured selection")
    for profile in bundle.profiles.values():
        if profile.profile_id != render_profile_identity(profile):
            raise ValueError(f"render profile {profile.name} identity is invalid")
    return bundle


def render_profile_identity(profile: RenderProfile) -> str:
    """Bind every output-affecting profile field to one immutable identity."""

    payload = {
        "name": profile.name,
        "dpi": profile.dpi,
        "preprocessing": list(profile.preprocessing),
        "requires_clip": profile.requires_clip,
        "parameters": profile.parameters,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def select_profile_dpis(measurement: RenderProfileMeasurement) -> dict[str, int]:
    """Replay the predeclared minimum-sufficient selection rules."""

    candidates = sorted(measurement.candidate_dpis, key=lambda item: item.dpi)
    review = next(
        item.dpi
        for item in candidates
        if item.minimum_scanned_long_edge_pixels >= 2_000
    )
    ocr_layout = next(
        item.dpi
        for item in candidates
        if item.minimum_scanned_long_edge_pixels >= 3_000
    )
    table_cv = next(
        item.dpi for item in candidates if item.table_stroke_p10_pixels >= 0.75
    )
    return {
        "review": review,
        "ocr_layout": ocr_layout,
        "table_cv": table_cv,
        "cell_detail": max(item.dpi for item in candidates),
    }


def render_page_derivative(
    *,
    pdf_path: Path | str,
    page_number: int,
    profile_name: str,
    output_dir: Path | str,
    clip_page_box: PageBox | None = None,
    worker_project: Path | str = DEFAULT_WORKER_PROJECT,
) -> RenderDerivative:
    bundle = load_render_profile_bundle()
    if profile_name not in bundle.profiles:
        raise ValueError(f"unknown render profile {profile_name!r}")
    profile = bundle.profiles[profile_name]
    if profile.requires_clip and clip_page_box is None:
        raise ValueError(f"render profile {profile.name} requires a page clip")
    # Callers such as `docs ingest` may change cwd to the corpus root. The
    # worker runs from the repository root, so every IPC/artifact path crossing
    # that process boundary must already be absolute.
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    request_path = output / f".{identity}.request.json"
    manifest_path = output / f".{identity}.manifest.json"
    request = {
        "pdf_path": str(Path(pdf_path).resolve()),
        "page_number": page_number,
        "profile": profile.model_dump(mode="json"),
        "output_dir": str(output.resolve()),
        "clip_page_box": (
            clip_page_box.model_dump(mode="json") if clip_page_box else None
        ),
    }
    request_path.write_text(json.dumps(request, sort_keys=True))
    project = Path(worker_project)
    try:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(project),
                "--frozen",
                "python",
                str(project / "render_worker.py"),
                "--request",
                str(request_path),
                "--manifest",
                str(manifest_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "render worker failed: " + (completed.stderr or completed.stdout)
            )
        return RenderDerivative.model_validate_json(manifest_path.read_text())
    finally:
        request_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


def _derivative_key(document_id: int, derivative: RenderDerivative) -> str:
    identity = {
        "document_id": document_id,
        "page_number": derivative.regenerable_from.page_number,
        "profile_id": derivative.profile_id,
        "clip_box": (
            derivative.clip_box.model_dump(mode="json")
            if derivative.clip_box
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def persist_render_derivative(
    session: Session,
    document_id: int,
    derivative: RenderDerivative,
) -> PageRenderDerivative:
    """Idempotently retain one Class B manifest without copying artifact bytes."""

    document = session.get(Document, document_id)
    if document is None:
        raise ValueError("render derivative document does not exist")
    if document.sha256 != derivative.regenerable_from.source_sha256:
        raise ValueError("render derivative source digest does not match its Document")
    key = _derivative_key(document_id, derivative)
    existing = session.scalar(
        select(PageRenderDerivative).where(
            PageRenderDerivative.derivative_key == key
        )
    )
    manifest = derivative.model_dump(mode="json")
    if existing is not None:
        if existing.artifact_sha256 != derivative.artifact_sha256:
            raise ValueError("render derivative identity produced different content")
        if not Path(existing.artifact_path).is_file():
            existing.artifact_path = str(derivative.artifact_path)
            existing.artifact_bytes = derivative.artifact_bytes
            existing.manifest_json = manifest
            session.flush()
        return existing
    stored = PageRenderDerivative(
        document_id=document_id,
        page_number=derivative.regenerable_from.page_number,
        derivative_key=key,
        profile_name=derivative.profile_name,
        profile_id=derivative.profile_id,
        source_sha256=derivative.regenerable_from.source_sha256,
        artifact_path=str(derivative.artifact_path),
        artifact_sha256=derivative.artifact_sha256,
        artifact_bytes=derivative.artifact_bytes,
        manifest_json=manifest,
        retention_class=derivative.retention_class,
    )
    session.add(stored)
    session.flush()
    return stored


def regenerate_render_derivative(
    stored: PageRenderDerivative,
    *,
    pdf_path: Path | str,
    output_dir: Path | str,
) -> RenderDerivative:
    """Rebuild one derivative only from its pinned source, page, profile, and clip."""

    source = Path(pdf_path)
    if hashlib.sha256(source.read_bytes()).hexdigest() != stored.source_sha256:
        raise ValueError("regeneration source bytes do not match the retained digest")
    clip_value = stored.manifest_json.get("clip_box")
    clip = PageBox.model_validate(clip_value) if clip_value else None
    derivative = render_page_derivative(
        pdf_path=source,
        page_number=stored.page_number,
        profile_name=stored.profile_name,
        output_dir=output_dir,
        clip_page_box=clip,
    )
    if derivative.profile_id != stored.profile_id:
        raise ValueError("retained render profile is no longer available")
    return derivative


def render_path_for_page(
    session: Session,
    *,
    document_id: int,
    page_number: int,
    purpose: Literal["review", "model_vision", "ocr_layout", "table_cv"],
    legacy_image_path: str | None = None,
) -> str | None:
    """Read a purpose-specific derivative with a legacy review fallback."""

    profile_name = {
        "review": "review",
        "model_vision": "ocr_layout",
        "ocr_layout": "ocr_layout",
        "table_cv": "table_cv",
    }[purpose]
    stored = session.scalar(
        select(PageRenderDerivative).where(
            PageRenderDerivative.document_id == document_id,
            PageRenderDerivative.page_number == page_number,
            PageRenderDerivative.profile_name == profile_name,
        )
    )
    if stored is not None and Path(stored.artifact_path).is_file():
        return stored.artifact_path
    # Only human review may read the legacy reviewer rendition. Machine
    # purposes must generate their own profile or refuse; sharing the review
    # image here would keep the superseded production path alive.
    return legacy_image_path if purpose == "review" else None


def ensure_render_derivative(
    session: Session,
    *,
    document: Document,
    page_number: int,
    profile_name: Literal["ocr_layout", "table_cv", "cell_detail"],
    pdf_path: Path | str,
    output_dir: Path | str,
    clip_page_box: PageBox | None = None,
) -> PageRenderDerivative:
    """Generate and persist a missing non-review derivative from pinned bytes."""

    derivative = render_page_derivative(
        pdf_path=pdf_path,
        page_number=page_number,
        profile_name=profile_name,
        output_dir=output_dir,
        clip_page_box=clip_page_box,
    )
    return persist_render_derivative(session, document.id, derivative)
