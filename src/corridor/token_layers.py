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

ADR-0094 replaces the native engine, and #733 adds the second native adapter
here rather than beside it: `read_native_token_layers` reads the same page
through the imported paired-rendition reader and returns a layer with its own
engine identity — the reader's name, the pypdfium2/PDFium versions, its
measured engine and DPI, the imported commit, and a new adapter version. The
two native adapters are two engines, never one engine's two versions, so a
re-read writes a new layer beside the earlier one and rewrites no history.
`corridor.config.settings.native_reader_token_layer` selects which one ingest
calls; it is off, because merging an adapter is not selecting it (#447).

The reader enters PDFium through `PdfiumExecutor` — one spawned process per
document — and not through the cheaper in-process `pdfium_entry`. PDFium is
not safe to call from two threads of one process at a time, and ingest runs
in a threaded worker: `pdfium_entry` would refuse the second document
outright with `PdfiumConcurrencyError`, which is the right answer for a
diagnostic and the wrong one for a pipeline that must parse two documents at
once. The cost is a process spawn per document, paid once, not per page.

`page_text_projection` is the other half of ADR-0073's sentence. `DocPage.text`
stops being a native-vs-OCR winner and becomes a rebuildable projection over
the native layer: the tokens carry their line and their order, so the page
string is a function of the retained artifact alone and needs neither the PDF
nor the reader to rebuild. It is not offered for the incumbent's layer, whose
page text was a separate call the layer cannot reproduce.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import pytesseract

from corridor.models import Document, TokenLayerManifest
from corridor.page_inventory import FIXED_POINT_SCALE, PdfRect
from corridor.object_storage import content_store
from corridor.render_profiles import RenderDerivative
from corridor.retention import artifact_key, register_processing_artifact
from corridor.verify import normalize
from corridor_pdf_reader.execution import MEASURED_DPI, PdfiumExecutor
from corridor_pdf_reader.provenance import SOURCE_COMMIT


NATIVE_ADAPTER_VERSION = "native-pymupdf-v1"
OCR_ADAPTER_VERSION = "ocr-tesseract-v1"
# The reader-backed native adapter (#733). The engine name is the imported
# package, not one of its libraries: pypdfium2 supplies the glyphs and pypdf
# the structure tree, and it is the reader's own assembly of the two that a
# layer records.
READER_ENGINE = "corridor-pdf-reader"
READER_NATIVE_ADAPTER_VERSION = "native-reader-v1"
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



def read_native_token_layers(
    path: Path | str,
    *,
    source_sha256: str,
    executor: PdfiumExecutor | None = None,
) -> tuple[TokenLayer, ...]:
    """One native token layer per page, read through the paired-rendition reader.

    The whole document is read in one isolated child process, because the
    isolation unit is the PDFium call and a per-page process would pay the
    spawn on every page for nothing. A page that draws no glyphs still gets a
    layer, with no tokens; a page missing from the result would be a silent
    hole, and ingest refuses one.
    """

    result = (executor or PdfiumExecutor()).read_document(Path(path))
    identity = reader_native_engine_identity(result)
    return tuple(
        _reader_native_token_layer(page, identity=identity, source_sha256=source_sha256)
        for page in result["pages"]
    )


def reader_native_engine_identity(result: dict[str, Any]) -> EngineIdentity:
    """What produced these tokens, pinned to the imported reader's own run.

    The reader reports the pypdfium2 and PDFium versions of the process that
    read the page, so the identity is taken from the result rather than from
    an installed distribution's metadata: a layer records the engine that read
    it. The imported commit and the measured engine and DPI complete the
    configuration, which is what #731 measures and #447 selects.
    """

    return EngineIdentity(
        origin="native",
        engine=READER_ENGINE,
        engine_version=f"pypdfium2-{result['version']}+PDFium-{result['native_version']}",
        adapter_version=READER_NATIVE_ADAPTER_VERSION,
        dpi=MEASURED_DPI,
        configuration={
            "reader_engine": result["engine"],
            "source_commit": SOURCE_COMMIT,
        },
    )


def _reader_native_token_layer(
    page: dict[str, Any], *, identity: EngineIdentity, source_sha256: str
) -> TokenLayer:
    # A line the reader assembles from whitespace alone contributes no words,
    # and a line index with no token would make the projection guess at a gap
    # it cannot see. Such a line is dropped here, so every recorded line index
    # holds at least one token and the projection is exact over the artifact.
    lines = [line for line in _reader_word_lines(page["characters"]["value"]) if line]
    tokens: list[Token] = []
    for line_no, line in enumerate(lines):
        for raw_text, box, object_id in line:
            tokens.append(
                Token(
                    ordinal=len(tokens),
                    origin="native",
                    raw_text=raw_text,
                    normalized_text=normalize(raw_text),
                    polygon_pdf=_rect_from_points(*box),
                    block=object_id,
                    line=line_no,
                )
            )
    projected = "\n".join(" ".join(word for word, _, _ in line) for line in lines)
    return TokenLayer(
        page_no=page["number"],
        origin="native",
        source_sha256=source_sha256,
        identity=identity,
        tokens=tuple(tokens),
        quality={
            "token_count": len(tokens),
            "line_count": len(lines),
            "rotation_degrees": int(page["geometry"]["rotation"]),
            "clipped_runs": len(page.get("clipped", {}).get("value", [])),
            # The projection is the page string ingest writes, so whether it
            # reproduces the reader's own page text is a fact about this
            # layer, recorded rather than assumed.
            "projection_matches_reader_text": projected == page["text"]["value"],
        },
    )


def page_text_projection(layer: TokenLayer) -> str:
    """Rebuild a page's text from a reader-backed native layer (ADR-0073).

    Tokens carry their line and their order, so the page string is a function
    of the retained artifact and nothing else — no PDF, no reader, no second
    reading. Lines are joined by a newline and tokens within a line by a
    space, which is the reader's own page-text shape.

    The incumbent's layer is refused rather than approximated: its page text
    came from a separate call whose whitespace and block order the word boxes
    do not carry, so a projection over it would be a new reading wearing an
    old reading's name.
    """

    if layer.origin != "native" or layer.identity.engine != READER_ENGINE:
        raise ValueError(
            "page text projects only over a reader-backed native token layer"
        )
    lines: dict[int, list[str]] = {}
    for token in layer.tokens:
        lines.setdefault(token.line if token.line is not None else 0, []).append(
            token.raw_text
        )
    return "\n".join(" ".join(lines[key]) for key in sorted(lines))


def _reader_word_lines(
    characters: list[dict[str, Any]],
) -> list[list[tuple[str, list[float], int]]]:
    """The reader's characters as lines of words, each with its display box.

    This is `replacement.layout.ordered_text`'s own assembly — the same
    rotation, the same line grouping, the same degenerate-metrics and
    overlapping-whitespace rules, the same word boundaries — stopped one step
    earlier, at the words, instead of joining them into a string. Keeping the
    two in step is what lets the page text be a projection over the tokens
    rather than a second reading beside them, and
    `quality.projection_matches_reader_text` records the agreement page by
    page. The reader itself reports `words` as unsupported, so there is no
    word API to call instead.
    """

    characters = [c for c in characters if c["text"] not in ("\r", "\n", "\t")]
    visible = [c for c in characters if c["text"].strip()]
    if not visible:
        return []
    angle = statistics.mode(round(c.get("angle", 0) / 90) * 90 % 360 for c in visible)
    radians = math.radians(-angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    placed: list[tuple[list[float], dict[str, Any]]] = []
    for character in characters:
        box = character["display_box"]
        points = [
            (x * cosine - y * sine, x * sine + y * cosine)
            for x in (box[0], box[2])
            for y in (box[1], box[3])
        ]
        upright = [
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        ]
        if character["text"].isspace() and upright[2] - upright[0] <= 0.2:
            continue
        placed.append((upright, character))
    if not placed:
        return []
    height = statistics.median(max(1, box[3] - box[1]) for box, _ in placed)
    lines: list[list[tuple[list[float], dict[str, Any]]]] = []
    for box, character in sorted(
        placed, key=lambda item: ((item[0][1] + item[0][3]) / 2, item[0][0])
    ):
        centre = (box[1] + box[3]) / 2
        if not lines or abs(centre - sum(lines[-1][0][0][1::2]) / 2) > max(
            1, height * 0.35
        ):
            lines.append([])
        lines[-1].append((box, character))
    return [_line_words(line, height) for line in lines]


def _line_words(
    line: list[tuple[list[float], dict[str, Any]]], height: float
) -> list[tuple[str, list[float], int]]:
    ink = [box for box, character in line if not character["text"].isspace()]
    degenerate_metrics = bool(ink) and statistics.median(
        box[3] - box[1] for box in ink
    ) < 0.1
    # Some bindings insert a second "space" overlapping the next glyph. Keep
    # real whitespace advances; discard these overlapping placeholders.
    line = [
        (box, character)
        for box, character in line
        if degenerate_metrics
        or not character["text"].isspace()
        or not any(
            max(0, min(box[2], other[2]) - max(box[0], other[0]))
            > (box[2] - box[0]) * 0.5
            for other in ink
        )
    ]
    words: list[tuple[str, list[float], int]] = []
    text = ""
    word_box: list[float] | None = None
    object_id = 0
    previous: list[float] | None = None

    def close() -> None:
        nonlocal text, word_box
        if text and word_box is not None:
            words.append((text, word_box, object_id))
        text, word_box = "", None

    for box, character in sorted(line, key=lambda item: item[0][0]):
        boundary = character.get("break_before")
        if boundary is True or (
            boundary is None
            and not degenerate_metrics
            and previous
            and box[0] - previous[2] > max(0.5, height * 0.18)
        ):
            close()
        if character["text"].isspace():
            close()
        else:
            display = character["display_box"]
            if word_box is None:
                word_box = list(display)
                object_id = int(character.get("object_id", 0))
            else:
                word_box = [
                    min(word_box[0], display[0]),
                    min(word_box[1], display[1]),
                    max(word_box[2], display[2]),
                    max(word_box[3], display[3]),
                ]
            text += character["text"]
        previous = box
    close()
    return words


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
    """Rebuild the token layer from its retained artifact for geometry readers.

    The staged local file is read when present; otherwise the bytes come from
    the store, digest-verified, and are staged for the next reader."""

    destination = Path(manifest.artifact_path)
    if not destination.is_file():
        content_store().stage(
            artifact_key(manifest.artifact_sha256, manifest.artifact_path),
            destination,
            sha256=manifest.artifact_sha256,
        )
    return TokenLayer.model_validate_json(destination.read_bytes())


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
