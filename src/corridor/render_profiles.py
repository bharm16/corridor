"""Select measured render profiles and invoke the isolated raster worker.

The former ingest module rendered one 150-DPI PNG and silently reused it for
review, OCR, model vision, and table geometry. This module exposes four versioned
purposes selected by a checked-in gold measurement. OpenCV stays behind the
worker process and lockfile; the core only validates immutable requests and
derivative manifests.

Which rasterizer the worker runs is decided here too (#735). It is one
deployment setting, off, and the request carries the answer to the worker;
the manifest records the engine that ran and the derivative identity carries
it, so PDFium renders accumulate beside the MuPDF renders already retained
rather than replacing them. A request may name only an engine the worker still
has an adapter for: #741 removed MuPDF's, and a request naming it was refused
inside the spawned process until the domain narrowed to what is served.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from corridor.typed_output import ClosedModel
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.models import Document, PageRenderDerivative
from corridor.page_inventory import (
    PageInventory, READER_COORDINATE_FRAME, RoutingRegion,
)
from corridor.object_storage import content_store
from corridor.retention import artifact_key, register_processing_artifact


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_PATH = ROOT / "gold" / "pdf" / "v1" / "render-profiles.json"
DEFAULT_MEASUREMENT_PATH = (
    ROOT / "gold" / "pdf" / "v1" / "render-profile-measurement.json"
)
DEFAULT_WORKER_PROJECT = ROOT / "workers" / "render"

# The two rasterizer identities a derivative can record (#735). The legacy
# engine is the one every derivative recorded before #735 was rendered with;
# the replacement is the engine ADR-0094 decided on. Only the replacement has
# an adapter - #741 deleted MuPDF's with the engine - so the legacy name is an
# identity a retained manifest carries, never a rasterizer a request may name.
# `SERVED_RASTERIZERS` is that request domain.
LEGACY_RASTERIZER = "pymupdf"
REPLACEMENT_RASTERIZER = "pdfium"
SERVED_RASTERIZERS = (REPLACEMENT_RASTERIZER,)

# `render_worker.rasterise` states this same refusal, because the worker is a
# separate uv project that cannot import this module and a request file it is
# handed may come from anywhere. Stating it here too is what makes a retired
# request cost a sentence instead of a process start.
RETIRED_RASTERIZER_REFUSAL = (
    f"the {LEGACY_RASTERIZER} rasterizer was removed with the engine (#741); "
    "its renders are retained and are not re-rendered here"
)


def selected_rasterizer() -> str:
    """The engine this deployment renders with.

    One setting chose between the two until #741 removed the MuPDF one, so
    there is nothing left to select and this is a constant. It stays a
    function because the manifest and the artifact name both ask what
    rendered a page, and a retained derivative answers differently.
    The worker cannot read a setting - `worker_environment` scrubs every `CORRIDOR_`
    variable - so the choice travels in the request, where the manifest
    records which engine actually ran.
    """

    return REPLACEMENT_RASTERIZER


class RenderModel(ClosedModel):
    """Every render profile and derivative shape whose identity is sealed."""


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
    schema_version: Literal["corridor.render-derivative.v2"]
    rasterizer: Literal["pymupdf", "pdfium"]
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


class RoutedRegionCrop(RenderModel):
    """A masked crop of existing pixels, with the permitted source extent intact."""

    derivative: RenderDerivative
    region_id: str
    mode: Literal["native", "ocr", "both"]
    requested_display_box: PageBox
    visible_display_box: PageBox
    source_polygon: tuple[tuple[float, float], ...]
    raster_polygon: tuple[tuple[float, float], ...]
    pixel_crop: tuple[int, int, int, int]


def crop_routed_region(
    *, derivative: RenderDerivative, inventory: PageInventory,
    region: RoutingRegion, output_dir: Path | str,
) -> RoutedRegionCrop:
    """Crop a reader region through the actual PDFium crop/deskew transform.

    Inventory boxes are displayed fixed-point coordinates. The derivative's
    first two steps establish that frame; all later steps, including deskew,
    map it to existing pixels. Intersect first, then mask the transformed
    polygon by pixel centres. Its bounding rectangle never grants neighboring
    pixels permission. No raster is re-rendered and no OCR trust is conferred.
    """
    derivative = RenderDerivative.model_validate(derivative.model_dump())
    if (derivative.rasterizer != REPLACEMENT_RASTERIZER
            or inventory.coordinate_frame != READER_COORDINATE_FRAME
            or inventory.rotation_degrees != derivative.rotation_degrees
            or [step.name for step in derivative.transforms[:2]]
            != ["pdf_user_to_page", "page_rotation"]):
        raise ValueError("routed crop requires the reader's displayed PDFium frame")
    width = derivative.crop_box.x1 - derivative.crop_box.x0
    height = derivative.crop_box.y1 - derivative.crop_box.y0
    if derivative.rotation_degrees % 180:
        width, height = height, width
    frame = inventory.boxes.crop
    if (frame.x0, frame.y0, frame.x1, frame.y1) != (0, 0, width, height):
        raise ValueError("inventory and derivative displayed bounds differ")
    requested = PageBox.model_validate(region.box.model_dump())
    coordinates = (max(0, requested.x0), max(0, requested.y0),
                   min(width, requested.x1), min(height, requested.y1))
    if coordinates[2] <= coordinates[0] or coordinates[3] <= coordinates[1]:
        raise ValueError("routed region has an empty displayed intersection")
    visible = PageBox(**dict(zip(("x0", "y0", "x1", "y1"), coordinates)))
    points = ((visible.x0, visible.y0), (visible.x1, visible.y0),
              (visible.x1, visible.y1), (visible.x0, visible.y1))
    source_points = points
    for step in reversed(derivative.transforms[:2]):
        source_points = tuple(step.inverse_point(point) for point in source_points)
    raster_points = points
    for step in derivative.transforms[2:]:
        raster_points = tuple(step.forward_point(point) for point in raster_points)
    pixel_crop = (
        max(0, math.floor(min(x for x, _ in raster_points))),
        max(0, math.floor(min(y for _, y in raster_points))),
        min(derivative.raster_width, math.ceil(max(x for x, _ in raster_points))),
        min(derivative.raster_height, math.ceil(max(y for _, y in raster_points))),
    )
    left, top, right, bottom = pixel_crop
    if right <= left or bottom <= top:
        raise ValueError("routed region has an empty raster intersection")
    mask = Image.new("L", (right - left, bottom - top), 0)
    draw = ImageDraw.Draw(mask)
    # Scan each pixel-centre row across the convex transformed rectangle.
    edges = tuple(zip(raster_points, (*raster_points[1:], raster_points[0])))
    for y in range(top, bottom):
        centre = y + 0.5
        crossings = sorted(
            x0 + (centre - y0) * (x1 - x0) / (y1 - y0)
            for (x0, y0), (x1, y1) in edges
            if min(y0, y1) <= centre < max(y0, y1)
        )
        if len(crossings) == 2:
            start = max(left, math.ceil(crossings[0] - 0.5))
            end = min(right, math.ceil(crossings[1] - 0.5))
            if end > start:
                draw.line((start - left, y - top, end - left - 1, y - top), fill=255)
    if mask.getbbox() is None:
        raise ValueError("routed region contains no permitted pixel centres")
    with Image.open(derivative.artifact_path) as image:
        if image.size != (derivative.raster_width, derivative.raster_height):
            raise ValueError("derivative raster dimensions disagree with its manifest")
        cropped = image.convert("RGB").crop(pixel_crop)
    cropped = Image.composite(cropped, Image.new("RGB", cropped.size, "white"), mask)
    parameters = {
        "parent_artifact_sha256": derivative.artifact_sha256,
        "requested_display_box": requested.model_dump(),
        "visible_display_box": visible.model_dump(),
        "raster_polygon": raster_points, "pixel_crop": pixel_crop,
        "mask": "transformed-visible-region-pixel-centres-v1",
        "region": region.model_dump(mode="json"),
    }
    identity = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"routed-{identity}.png"
    # First-write publication: an old artifact never gets replaced on retry.
    import io
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    data = buffer.getvalue()
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("routed crop identity already names different bytes")
    else:
        with path.open("xb") as target:
            target.write(data)
    step = AffineStep(
        name="routed_region_crop", forward=(1, 0, 0, 1, -left, -top),
        inverse=(1, 0, 0, 1, left, top), geometry_change=True, parameters=parameters,
    )
    payload = derivative.model_dump()
    payload.update(
        artifact_path=path, artifact_sha256=hashlib.sha256(data).hexdigest(),
        artifact_bytes=len(data), raster_width=cropped.width, raster_height=cropped.height,
        transforms=(*derivative.transforms, step),
        parameters={**derivative.parameters, "routed_crop": parameters},
    )
    return RoutedRegionCrop(
        derivative=RenderDerivative.model_validate(payload), region_id=region.region_id,
        mode=region.mode, requested_display_box=requested, visible_display_box=visible,
        source_polygon=source_points, raster_polygon=raster_points, pixel_crop=pixel_crop,
    )


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


@dataclass(frozen=True)
class RenderExecutionLimits:
    """One worker's wall deadline and Linux address-space ceiling."""

    wall_seconds: float = 120.0
    memory_bytes: int = 4 * 1024**3

    def __post_init__(self):
        if not math.isfinite(self.wall_seconds) or self.wall_seconds <= 0 or self.memory_bytes <= 0:
            raise ValueError("render execution limits must be positive and finite")


def _run_worker(command, *, environment, limits: RenderExecutionLimits):
    # The group includes uv and the Python worker. Killing only the launcher
    # leaves a native renderer alive after the caller releases its claim.
    with subprocess.Popen(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=limits.wall_seconds)
        except BaseException as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise TimeoutError(f"render worker exceeded {limits.wall_seconds} seconds and was stopped") from exc
            raise
        if process.returncode:
            raise RuntimeError("render worker failed: " + (stderr or stdout))


def render_page_derivative(
    *,
    pdf_path: Path | str,
    page_number: int,
    profile_name: str,
    output_dir: Path | str,
    clip_page_box: PageBox | None = None,
    worker_project: Path | str = DEFAULT_WORKER_PROJECT,
    rasterizer: str | None = None,
    limits: RenderExecutionLimits = RenderExecutionLimits(),
) -> RenderDerivative:
    return render_page_derivatives(
        pdf_path=pdf_path,
        page_number=page_number,
        profile_names=(profile_name,),
        output_dir=output_dir,
        clip_page_box=clip_page_box,
        worker_project=worker_project,
        rasterizer=rasterizer,
        limits=limits,
    )[0]


def render_page_derivatives(
    *,
    pdf_path: Path | str,
    page_number: int,
    profile_names: Sequence[str],
    output_dir: Path | str,
    clip_page_box: PageBox | None = None,
    worker_project: Path | str = DEFAULT_WORKER_PROJECT,
    rasterizer: str | None = None,
    limits: RenderExecutionLimits = RenderExecutionLimits(),
) -> list[RenderDerivative]:
    """Render several profiles of one page in a single worker process.

    Each worker start re-imports OpenCV, Pillow, PyMuPDF and NumPy, which
    measured 0.27s against 0.38s for the whole invocation on an ordinary page:
    process startup, not rasterising, was most of what ingest paid. Ingest
    wants three profiles of every page, so asking for them together turns
    three process starts into one and left every manifest byte-identical
    (#548). The derivatives come back in the order requested.
    """

    if not profile_names:
        raise ValueError("render requires at least one profile")
    engine = rasterizer or selected_rasterizer()
    if engine == LEGACY_RASTERIZER:
        raise ValueError(RETIRED_RASTERIZER_REFUSAL)
    if engine not in SERVED_RASTERIZERS:
        raise ValueError(f"unknown rasterizer {engine!r}")
    source = Path(pdf_path).resolve()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    bundle = load_render_profile_bundle()
    profiles = []
    for profile_name in profile_names:
        if profile_name not in bundle.profiles:
            raise ValueError(f"unknown render profile {profile_name!r}")
        profile = bundle.profiles[profile_name]
        if profile.requires_clip and clip_page_box is None:
            raise ValueError(f"render profile {profile.name} requires a page clip")
        profiles.append(profile)
    # Callers such as `docs ingest` may change cwd to the corpus root. The
    # worker runs from the repository root, so every IPC/artifact path crossing
    # that process boundary must already be absolute.
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = Path(worker_project)
    worker_identity = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((*project.glob("*.py"), project / "uv.lock"))
    }
    namespace = hashlib.sha256(json.dumps(worker_identity, sort_keys=True).encode()).hexdigest()
    raster_output = output / f"worker-{namespace}"
    identity = uuid4().hex
    request_path = output / f".{identity}.request.json"
    manifest_path = output / f".{identity}.manifest.json"
    requests = [
        {
            "pdf_path": str(Path(pdf_path).resolve()),
            "page_number": page_number,
            "profile": profile.model_dump(mode="json"),
            "rasterizer": engine,
            "preprocessing_version": "pdfium-deskew-v2",
            "output_dir": str(raster_output.resolve()),
            "clip_page_box": (
                clip_page_box.model_dump(mode="json") if clip_page_box else None
            ),
        }
        for profile in profiles
    ]
    request_path.write_text(json.dumps({"requests": requests, "memory_limit_bytes": limits.memory_bytes}, sort_keys=True))
    try:
        _run_worker(
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
            environment=worker_environment(),
            limits=limits,
        )
        payload = json.loads(manifest_path.read_text())
        manifests = payload.get("manifests") if isinstance(payload, dict) else None
        if not isinstance(manifests, list) or len(manifests) != len(profiles):
            raise ValueError("render worker did not return the complete requested profile set")
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
            raise ValueError("render source bytes changed during the request")
        if worker_identity != {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((*project.glob("*.py"), project / "uv.lock"))
        }:
            raise ValueError("render worker bytes changed while the request ran")
        derivatives = []
        for profile, manifest in zip(profiles, manifests, strict=True):
            derivative = RenderDerivative.model_validate(manifest)
            if (derivative.profile_name, derivative.profile_id, derivative.dpi,
                derivative.preprocessing, derivative.rasterizer, derivative.clip_box,
                derivative.regenerable_from.source_sha256, derivative.regenerable_from.page_number,
                derivative.regenerable_from.profile_id) != (
                    profile.name, profile.profile_id, profile.dpi, profile.preprocessing, engine, clip_page_box,
                    source_sha256, page_number, profile.profile_id):
                raise ValueError("render response does not match its requested source, page and profile")
            if not derivative.artifact_path.resolve().is_relative_to(raster_output.resolve()):
                raise ValueError("render artifact is outside this worker's output directory")
            if derivative.artifact_path.stat().st_size != derivative.artifact_bytes:
                raise ValueError("render artifact size does not match its manifest")
            with Image.open(derivative.artifact_path) as rendered_image:
                rendered_image.load()
                if rendered_image.format != "PNG" or rendered_image.size != (derivative.raster_width, derivative.raster_height):
                    raise ValueError("render artifact dimensions or format do not match its manifest")
            derivatives.append(derivative.model_copy(update={"parameters": {
                **derivative.parameters, "worker_runtime_sha256s": worker_identity,
            }}))
        return derivatives
    finally:
        request_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


_DENIED_WORKER_VARIABLES = ("DATABASE_URL", "WEB_DATABASE_URL", "WORKER_DATABASE_URL")
_DENIED_WORKER_PREFIXES = ("CORRIDOR_", "AWS_", "PG")


def worker_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """The render subprocess's environment: no database, no storage credentials.

    The worker reads locally staged bytes and writes to a local staging
    directory (ADR-0079 as amended by ADR-0083). Scrubbing the inherited
    environment is what makes that true rather than merely intended: the
    database URLs and capability passwords, every Corridor setting, the AWS
    credential chain, and libpq's own connection variables never reach it.
    """

    source = os.environ if environment is None else environment
    return {
        name: value
        for name, value in source.items()
        if name not in _DENIED_WORKER_VARIABLES
        and not name.startswith(_DENIED_WORKER_PREFIXES)
    }


def _derivative_key(document_id: int, derivative: RenderDerivative) -> str:
    """The identity a derivative is retained under, engine included.

    The rasterizer joins it only when it is not the legacy engine, which keeps
    every derivative written before #735 addressable at the identity it was
    written under. A PDFium render of a page MuPDF has already rendered is a
    new identity beside the old row, never an overwrite of it: two rasterizers
    produce different pixels for the same page, and ADR-0072 keeps a
    regenerable intermediary's manifest honest about which reading it holds.
    """

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
    if derivative.rasterizer != LEGACY_RASTERIZER:
        identity["rasterizer"] = derivative.rasterizer
        if "worker_runtime_sha256s" in derivative.parameters:
            identity["worker_runtime_sha256s"] = derivative.parameters["worker_runtime_sha256s"]
    if "routed_crop" in derivative.parameters:
        identity["routed_crop"] = derivative.parameters["routed_crop"]
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
        _classify_derivative_for_retention(session, document, existing.artifact_path)
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
    _classify_derivative_for_retention(session, document, stored.artifact_path)
    return stored


def _classify_derivative_for_retention(
    session: Session, document: Document, artifact_path: str
) -> None:
    """Class B by construction: every retained render — any profile, region crop
    included — is retention-classified here, at the one seam every render path
    passes through, so no call site can persist a render that TTL cannot see.
    The manifest keeps the digest; the render itself is a regenerable
    intermediary (ADR-0072), never a citation anchor (ADR-0068)."""

    register_processing_artifact(
        session,
        project_id=document.project_id,
        kind="page_render",
        path=artifact_path,
        terminal_at=datetime.now(timezone.utc),
    )


def regenerate_render_derivative(
    stored: PageRenderDerivative,
    *,
    pdf_path: Path | str,
    output_dir: Path | str,
) -> RenderDerivative:
    """Rebuild one derivative only from its pinned source, page, profile, clip
    and rasterizer.

    The engine comes from the retained manifest, not from what this deployment
    currently selects: a derivative regenerated with the other rasterizer would
    be different pixels under the same identity, which is the one thing
    regeneration must never produce. A manifest written before #735 names no
    engine and is MuPDF's, so regenerating one is refused by the same rule that
    refuses any request for a retired engine — its pixels are retained, and
    rendering them again with PDFium would put different pixels under the
    identity the retained row already holds.
    """

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
        rasterizer=stored.manifest_json.get("rasterizer", LEGACY_RASTERIZER),
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
    if stored is not None:
        staged = staged_derivative_path(stored)
        if staged is not None:
            return str(staged)
    # Only human review may read the legacy reviewer rendition. Machine
    # purposes must generate their own profile or refuse; sharing the review
    # image here would keep the superseded production path alive.
    return legacy_image_path if purpose == "review" else None


def staged_derivative_path(stored: PageRenderDerivative) -> Path | None:
    """The retained render as a local file, fetched from the store when the
    staged copy is gone; ``None`` when the store no longer holds it (retention
    deleted it, or it predates the store and was never migrated)."""

    destination = Path(stored.artifact_path)
    if destination.is_file():
        return destination
    store = content_store()
    key = artifact_key(stored.artifact_sha256, stored.artifact_path)
    if not store.exists(key):
        return None
    return store.stage(key, destination, sha256=stored.artifact_sha256)


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
