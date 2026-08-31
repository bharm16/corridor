"""Native and OCR token layers, kept separately, with coordinates and origin.

Ingestion used to fold a page into one `DocPage.text` string by concatenating
native text and per-region OCR text. That single winner threw away coordinates
and could not tell a trustworthy native reading from a suspect OCR one, which
is exactly what cell-level fusion needs (research 2026-08-30 §D/§F: "more text
is not better text"). This module produces two coordinate-bearing token layers
instead — native tokens from PyMuPDF words with rotation recorded, and OCR
tokens behind a pluggable engine protocol whose first implementation is
Tesseract 5 emitting word boxes and confidences.

Token layers are Class B intermediary data (ADR-0072, ADR-0073): high volume,
TTL-eligible, and never the owner of a citation. A cited reading is promoted
into a `source_segment` that carries its own exact text and digest (ADR-0068),
so deleting an expired token layer leaves every promoted segment usable. Raw
OCR confidence is recorded as a signal, never treated as a calibrated
probability. The OCR engine is pinned as an engine — executable version,
traineddata filenames and digests, language, OEM/PSM, DPI, preprocessing
profile, render profile, and adapter version — not as a Python package, so a
layer is reproducible from what it records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import pytesseract

from corridor.models import Document, TokenLayerManifest
from corridor.page_inventory import FIXED_POINT_SCALE, PdfRect
from corridor.render_profiles import RenderDerivative
from corridor.retention import register_processing_artifact
from corridor.verify import normalize


NATIVE_ADAPTER_VERSION = "native-pymupdf-v1"
OCR_ADAPTER_VERSION = "ocr-tesseract-v1"
TESSERACT_OEM = 3
TESSERACT_PSM = 6


class TokenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Token(TokenModel):
    """One positioned reading. Native tokens have no confidence; OCR tokens
    record their engine confidence as a signal and a render-space polygon."""

    ordinal: int = Field(ge=0)
    origin: Literal["native", "ocr"]
    raw_text: str
    normalized_text: str
    polygon_pdf: PdfRect
    polygon_render: PdfRect | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    block: int | None = None
    line: int | None = None


class EngineIdentity(TokenModel):
    """Everything that determines a layer's tokens, pinned as an engine.

    For native tokens only the library identity matters. For OCR the pin is the
    executable version, the traineddata filenames with their SHA-256 digests,
    the language set, OEM/PSM, DPI, the preprocessing profile, and the render
    profile the tokens were read from — none of which a Python package version
    captures.
    """

    origin: Literal["native", "ocr"]
    engine: str
    engine_version: str
    adapter_version: str
    language: str | None = None
    oem: int | None = None
    psm: int | None = None
    dpi: int | None = None
    preprocessing_profile: str | None = None
    render_profile_id: str | None = None
    traineddata: dict[str, str] = Field(default_factory=dict)
    configuration: dict = Field(default_factory=dict)


class TokenLayer(TokenModel):
    schema_version: Literal["corridor.token-layer.v1"] = "corridor.token-layer.v1"
    page_no: int = Field(ge=1)
    origin: Literal["native", "ocr"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: EngineIdentity
    tokens: tuple[Token, ...]
    quality: dict

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization; the artifact digest is taken over this."""

        return (
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


def _rect_from_points(x0: float, y0: float, x1: float, y1: float) -> PdfRect:
    """Native words and render pixels arrive in whole units; scale to fixed point."""

    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return PdfRect(
        x0=round(left * FIXED_POINT_SCALE),
        y0=round(top * FIXED_POINT_SCALE),
        x1=round(right * FIXED_POINT_SCALE),
        y1=round(bottom * FIXED_POINT_SCALE),
    )


def _rect_from_fixed(x0: float, y0: float, x1: float, y1: float) -> PdfRect:
    """The render transform's PDF side is already fixed-point thousandths, so a
    point inverted back from raster space is not scaled again."""

    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    return PdfRect(x0=round(left), y0=round(top), x1=round(right), y1=round(bottom))


def extract_native_token_layer(
    page, *, page_no: int, source_sha256: str
) -> TokenLayer:
    """One native token per PyMuPDF word, with its rotation-aware page box.

    `get_text("words")` returns boxes in the page's displayed coordinate space,
    so the recorded rotation is a fact about the tokens, not a transform still
    owed. Native readings never carry a confidence — they are the authoritative
    character tokens, not an estimate.
    """

    words = page.get_text("words")
    tokens: list[Token] = []
    for ordinal, word in enumerate(sorted(words, key=lambda w: (w[5], w[6], w[7]))):
        x0, y0, x1, y1, raw_text, block_no, line_no, _word_no = word[:8]
        if not raw_text.strip():
            continue
        tokens.append(
            Token(
                ordinal=ordinal,
                origin="native",
                raw_text=raw_text,
                normalized_text=normalize(raw_text),
                polygon_pdf=_rect_from_points(x0, y0, x1, y1),
                block=int(block_no),
                line=int(line_no),
            )
        )
    identity = EngineIdentity(
        origin="native",
        engine="pymupdf",
        engine_version=_pymupdf_version(),
        adapter_version=NATIVE_ADAPTER_VERSION,
    )
    quality = {
        "token_count": len(tokens),
        "rotation_degrees": int(page.rotation),
        "empty_word_tokens": len(words) - len(tokens),
    }
    return TokenLayer(
        page_no=page_no,
        origin="native",
        source_sha256=source_sha256,
        identity=identity,
        tokens=tuple(tokens),
        quality=quality,
    )


class OcrRequest(TokenModel):
    """One recognition request: a render to read and where it came from."""

    page_no: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_path: Path
    derivative: RenderDerivative


class OcrEngine(Protocol):
    """recognize(request) -> token layer. The engine owns its own pinning."""

    def recognize(self, request: OcrRequest) -> TokenLayer: ...


def render_point_to_pdf(
    derivative: RenderDerivative, point: tuple[float, float]
) -> tuple[float, float]:
    """Map a raster-pixel point back to PDF page coordinates by inverting the
    derivative's recorded transform chain (raster→page is the chain reversed)."""

    current = point
    for step in reversed(derivative.transforms):
        current = step.inverse_point(current)
    return current


class TesseractEngine:
    """Tesseract 5 OCR emitting TSV word boxes and confidences.

    Confidence is a recorded signal in 0..1, never a calibrated probability.
    The engine pins its executable version and the SHA-256 of every traineddata
    file in the resolved language set, so a layer records what produced it.
    """

    def __init__(
        self,
        *,
        language: str = "eng",
        oem: int = TESSERACT_OEM,
        psm: int = TESSERACT_PSM,
    ) -> None:
        self.language = language
        self.oem = oem
        self.psm = psm

    def _identity(self, derivative: RenderDerivative) -> EngineIdentity:
        return EngineIdentity(
            origin="ocr",
            engine="tesseract",
            engine_version=str(pytesseract.get_tesseract_version()),
            adapter_version=OCR_ADAPTER_VERSION,
            language=self.language,
            oem=self.oem,
            psm=self.psm,
            dpi=derivative.dpi,
            preprocessing_profile="|".join(derivative.preprocessing) or "none",
            render_profile_id=derivative.profile_id,
            traineddata=_traineddata_digests(self.language),
            configuration={"tessdata_dir": _tessdata_dir()},
        )

    def recognize(self, request: OcrRequest) -> TokenLayer:
        config = f"--oem {self.oem} --psm {self.psm}"
        data = pytesseract.image_to_data(
            str(request.image_path),
            lang=self.language,
            config=config,
            output_type=pytesseract.Output.DICT,
        )
        tokens: list[Token] = []
        confidences: list[float] = []
        for index in range(len(data["text"])):
            raw_text = data["text"][index]
            if not raw_text.strip():
                continue
            conf_raw = float(data["conf"][index])
            if conf_raw < 0:
                continue
            confidence = max(0.0, min(1.0, conf_raw / 100.0))
            confidences.append(confidence)
            left = float(data["left"][index])
            top = float(data["top"][index])
            width = float(data["width"][index])
            height = float(data["height"][index])
            render_box = _rect_from_points(left, top, left + width, top + height)
            px0, py0 = render_point_to_pdf(request.derivative, (left, top))
            px1, py1 = render_point_to_pdf(
                request.derivative, (left + width, top + height)
            )
            tokens.append(
                Token(
                    ordinal=len(tokens),
                    origin="ocr",
                    raw_text=raw_text,
                    normalized_text=normalize(raw_text),
                    polygon_pdf=_rect_from_fixed(px0, py0, px1, py1),
                    polygon_render=render_box,
                    confidence=confidence,
                    block=int(data["block_num"][index]),
                    line=int(data["line_num"][index]),
                )
            )
        quality = {
            "token_count": len(tokens),
            "mean_confidence": (
                sum(confidences) / len(confidences) if confidences else None
            ),
            "low_confidence_tokens": sum(1 for c in confidences if c < 0.5),
        }
        return TokenLayer(
            page_no=request.page_no,
            origin="ocr",
            source_sha256=request.source_sha256,
            identity=self._identity(request.derivative),
            tokens=tuple(tokens),
            quality=quality,
        )


def _layer_key(document_id: int, layer: TokenLayer) -> str:
    return sha256(
        f"{document_id}:{layer.page_no}:{layer.origin}:{layer.content_sha256}".encode()
    ).hexdigest()


def persist_token_layer(
    session: Session,
    document_id: int,
    layer: TokenLayer,
    *,
    output_dir: Path | str,
) -> TokenLayerManifest:
    """Retain one token layer: a content-addressed JSON artifact, a PostgreSQL
    manifest row, and a Class B `ProcessingArtifact` registration (ADR-0073).

    Idempotent on the layer key so re-ingesting identical bytes reuses the
    artifact rather than duplicating it. The artifact is never a citation
    anchor — deleting it through the retention TTL leaves promoted source
    segments untouched (ADR-0068).
    """

    document = session.get(Document, document_id)
    if document is None:
        raise ValueError("token layer document does not exist")
    if document.sha256 != layer.source_sha256:
        raise ValueError("token layer source digest does not match its Document")

    content = layer.canonical_bytes()
    digest = layer.content_sha256
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / f"{layer.page_no:04d}-{layer.origin}-tokens-{digest}.json"
    if artifact_path.exists():
        if artifact_path.read_bytes() != content:
            raise ValueError(f"token-layer content-address collision at {artifact_path}")
    else:
        artifact_path.write_bytes(content)

    key = _layer_key(document_id, layer)
    existing = session.scalar(
        select(TokenLayerManifest).where(TokenLayerManifest.layer_key == key)
    )
    if existing is not None:
        _classify_layer_for_retention(session, document, existing.artifact_path)
        return existing

    stored = TokenLayerManifest(
        document_id=document_id,
        page_no=layer.page_no,
        origin=layer.origin,
        layer_key=key,
        source_sha256=layer.source_sha256,
        engine_json=layer.identity.model_dump(mode="json"),
        token_count=len(layer.tokens),
        quality_json=layer.quality,
        artifact_path=str(artifact_path),
        artifact_sha256=digest,
        artifact_bytes=len(content),
    )
    session.add(stored)
    session.flush()
    _classify_layer_for_retention(session, document, stored.artifact_path)
    return stored


def _classify_layer_for_retention(
    session: Session, document: Document, artifact_path: str
) -> None:
    """Class B by construction (ADR-0072): every token-layer artifact is
    retention-classified at its one persistence seam."""

    register_processing_artifact(
        session,
        project_id=document.project_id,
        kind="token_layer",
        path=artifact_path,
        terminal_at=datetime.now(timezone.utc),
    )


def load_token_layer(manifest: TokenLayerManifest) -> TokenLayer:
    """Rebuild the token layer from its retained artifact for geometry readers."""

    return TokenLayer.model_validate_json(Path(manifest.artifact_path).read_bytes())


def _pymupdf_version() -> str:
    import pymupdf

    return getattr(pymupdf, "__version__", "unknown")


def _tessdata_dir() -> str:
    """Resolve the tessdata directory Tesseract will actually read from."""

    prefix = _env_tessdata_prefix()
    if prefix:
        return prefix
    executable = shutil.which("tesseract")
    if executable is not None:
        completed = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (completed.stderr + completed.stdout).splitlines():
            marker = 'available languages in "'
            if marker in line:
                return line.split(marker, 1)[1].split('"', 1)[0]
    return ""


def _env_tessdata_prefix() -> str:
    import os

    return os.environ.get("TESSDATA_PREFIX", "")


def _traineddata_digests(language: str) -> dict[str, str]:
    directory = Path(_tessdata_dir()) if _tessdata_dir() else None
    digests: dict[str, str] = {}
    if directory is None or not directory.is_dir():
        return digests
    for code in language.split("+"):
        traineddata = directory / f"{code}.traineddata"
        if traineddata.is_file():
            digests[traineddata.name] = sha256(traineddata.read_bytes()).hexdigest()
    return digests
