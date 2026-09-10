"""Native and OCR token layers, kept separately, with coordinates and origin.

Ingestion used to fold a page into one `DocPage.text` string by concatenating
native text and per-region OCR text. That single winner threw away coordinates
and could not tell a trustworthy native reading from a suspect OCR one, which
is exactly what cell-level fusion needs (research 2026-08-30 §D/§F: "more text
is not better text"). This module produces two coordinate-bearing token layers
instead — native tokens from the paired-rendition reader, and OCR tokens from
the provider that reads scanned pages.

Token layers are Class B intermediary data (ADR-0072, ADR-0073): high volume,
TTL-eligible, and never the owner of a citation. A cited reading is promoted
into a `source_segment` that carries its own exact text and digest (ADR-0068),
so deleting an expired token layer leaves every promoted segment usable. Raw
OCR confidence is recorded as a signal, never treated as a calibrated
probability.

There were two native adapters and two OCR adapters here until #741. The
incumbent halves are gone: ADR-0094 replaced them, ADR-0095 recorded the
maintainer's acceptance, and #741 removed the engines from the product. What
they wrote is untouched. A retained token layer keeps the engine identity it
was written with — `native-pymupdf-v1`, `ocr-tesseract-v1`, the executable
version, the traineddata digests — because `EngineIdentity.engine` and
`adapter_version` are free-form strings that record what produced a layer
rather than a vocabulary of what may produce one. An old layer still loads,
still validates and still says truthfully which engine read it; what no longer
exists is the code that could write another one.

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

from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version as distribution_version
import json
import hmac
import math
from pathlib import Path
import statistics
import secrets
from typing import Any, Literal

from pydantic import Field

from corridor.typed_output import ClosedModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, TokenLayerManifest
from corridor.page_inventory import FIXED_POINT_SCALE, PdfRect
from corridor.object_storage import content_store
from corridor.render_profiles import RenderDerivative
from corridor.retention import artifact_key, register_processing_artifact
from corridor.verify import normalize
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE, PdfiumExecutor
from corridor_pdf_reader.provenance import SOURCE_COMMIT, PACKAGE_ROOT
from corridor.source_segment_errors import (
    NativeReaderUnavailable,
    SourceDocumentDigestMismatch,
    SourceSegmentLocatorMismatch,
)


# The replacement OCR adapter (ADR-0094, #739). The engine name is the
# provider, because that is what a scanned reading is answerable to: a
# retained response, its reported model version and its request identity, not
# a Python package or a local executable.
TEXTRACT_ENGINE = "textract"
TEXTRACT_OCR_ADAPTER_VERSION = "ocr-textract-v1"
# The reader-backed native adapter (#733). The engine name is the imported
# package, not one of its libraries: pypdfium2 supplies the glyphs and pypdf
# the structure tree, and it is the reader's own assembly of the two that a
# layer records.
READER_ENGINE = "corridor-pdf-reader"
READER_NATIVE_ADAPTER_VERSION = "native-reader-v2"
PDF_SEGMENT_SCHEME = "corridor.pdf-segments.v1"
# The declared version of this module's half of the native reading assembly:
# the page-text projection and the word/line rules it is built from. Bump it in
# the same change as any behaviour change to ``page_text_projection``,
# ``_reader_word_lines`` or ``_line_words``. See ``native_integration_digest``.
NATIVE_INTEGRATION_VERSION = "native-integration-v1"
_NATIVE_READING_SEAL_KEY = secrets.token_bytes(32)


class TokenModel(ClosedModel):
    """Every retained reading shape whose engine identity is sealed."""


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
    # A cloud OCR provider pins nothing a local executable version could pin,
    # so ADR-0094 names what must be retained instead: the provider, the
    # version it reported for itself, the identity of the request, and the
    # digests of the raw response and of the normalized reading taken from it.
    # These are what makes a promoted Textract reading verifiable later; a
    # local engine leaves them all None (#739).
    provider: str | None = None
    provider_model_version: str | None = None
    provider_request_id: str | None = None
    raw_response_sha256: str | None = None
    reading_sha256: str | None = None


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


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NativePdfReading:
    """A sealed value: mutable reader dictionaries never escape as its state."""

    rendition_sha256: str
    identity_json: str
    pages_json: str
    seal: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hmac.compare_digest(self.seal, _native_reading_seal(
            self.rendition_sha256, self.identity_json, self.pages_json
        )):
            raise SourceSegmentLocatorMismatch("a native reading must come from registered PDF bytes")

    @property
    def identity(self) -> dict:
        return json.loads(self.identity_json)

    @property
    def pages(self) -> tuple[dict, ...]:
        return tuple(json.loads(self.pages_json))

    @cached_property
    def reading_sha256(self) -> str:
        return _digest(_canonical({
            "rendition_sha256": self.rendition_sha256,
            "identity": self.identity,
            "pages": self.pages,
        }))

    @property
    def token_layers(self) -> tuple[TokenLayer, ...]:
        identity = EngineIdentity.model_validate(self.identity["native_layer"])
        identity = identity.model_copy(update={"configuration": {
            **identity.configuration,
            "reading_sha256": self.reading_sha256,
            "segment_scheme": PDF_SEGMENT_SCHEME,
        }})
        return tuple(
            reader_native_token_layer(
                page, identity=identity, source_sha256=self.rendition_sha256
            ) for page in self.pages
        )

    def page_text(self, page_no: int, *, stream: str = "page") -> str:
        layer = self.layer(page_no, stream=stream)
        return page_text_projection(layer)

    def layer(self, page_no: int, *, stream: str = "page") -> TokenLayer:
        if stream not in {"page", "clipped"}:
            raise SourceSegmentLocatorMismatch("unknown native span stream")
        pages = {page["number"]: page for page in self.pages}
        if page_no not in pages:
            raise SourceSegmentLocatorMismatch("native reading page does not exist")
        layer = next(layer for layer in self.token_layers if layer.page_no == page_no)
        if stream == "page":
            return layer
        page = pages[page_no]
        return reader_native_token_layer(
            {**page, "characters": page["clipped"]},
            identity=layer.identity, source_sha256=self.rendition_sha256,
        )


def read_native_pdf(
    path: Path | str,
    *,
    source_sha256: str,
    executor: PdfiumExecutor | None = None,
    engine: str = MEASURED_ENGINE,
    dpi: int = MEASURED_DPI,
) -> NativePdfReading:
    """Execute the native reader explicitly, including previously ingested bytes.

    **A result this build cannot read is an availability refusal.** The keys
    below belong to the child process's payload, and a payload that does not
    carry them means no page was read here at all. Every caller of this
    function used to let that ``KeyError`` escape into its own ``except
    (KeyError, TypeError)`` around the *stored* reader identity, so an
    unreadable result was reported as ``SourceSegmentLocatorMismatch`` — the
    integrity verdict *Not found at cited location*, which asserts that a
    reader went to a cited location and the passage was not there. The refusal
    belongs here rather than in each caller, because only this function knows
    which fields are the reader result contract and which are the locator's.
    """

    original = Path(path)
    if sha256(original.read_bytes()).hexdigest() != source_sha256:
        raise SourceDocumentDigestMismatch("native reading bytes do not match the rendition")
    if engine not in {"tagged", "pdfium"} or not isinstance(dpi, int) or not 1 <= dpi <= 300:
        raise SourceSegmentLocatorMismatch("unsupported native reader configuration")
    result = (executor or PdfiumExecutor()).read_document(original, engine=engine, dpi=dpi)
    try:
        read_sha256 = result["source_sha256"]
        engine_identity = reader_native_engine_identity(result)
        # Timing, wall-clock and diagnostic raster artifacts are not reading
        # identity. The actual characters, clipping and reconstructed cells are.
        pages = [
            {key: page[key] for key in ("number", "geometry", "text", "characters", "tables", "clipped")}
            for page in result["pages"]
        ]
    except (KeyError, TypeError) as exc:
        raise NativeReaderUnavailable(
            PDF_SEGMENT_SCHEME, "a complete result from the recorded native reader"
        ) from exc
    if (read_sha256 != source_sha256
            or sha256(original.read_bytes()).hexdigest() != source_sha256):
        raise SourceDocumentDigestMismatch("source bytes changed during the native reading")
    identity = engine_identity.model_copy(update={"dpi": dpi})
    identity_data = {
        "scheme": PDF_SEGMENT_SCHEME,
        "native_layer": identity.model_dump(mode="json"),
        "reader_runtime_sha256": _digest(_canonical({
            name: sha256((PACKAGE_ROOT / "replacement" / name).read_bytes()).hexdigest()
            for name in ("reader.py", "layout.py", "table_structure.py", "tags.py", "text_layout.py")
        })),
        "pypdf_version": distribution_version("pypdf"),
        # Pin the application assembly alongside the untouched imported
        # reader, so a projection change cannot silently retarget an earlier
        # stored locator; an unavailable earlier assembly refuses replay as an
        # availability refusal, never as a missing passage.
        "integration_sha256": native_integration_digest(),
    }
    if [page["number"] for page in pages] != list(range(1, len(pages) + 1)):
        raise SourceSegmentLocatorMismatch("native reader returned an incomplete page sequence")
    identity_json, pages_json = _canonical(identity_data), _canonical(pages)
    return NativePdfReading(
        source_sha256, identity_json, pages_json,
        _native_reading_seal(source_sha256, identity_json, pages_json),
    )


def _native_reading_seal(rendition_sha256: str, identity_json: str, pages_json: str) -> bytes:
    return hmac.new(
        _NATIVE_READING_SEAL_KEY,
        _canonical((rendition_sha256, identity_json, pages_json)).encode("utf-8"),
        sha256,
    ).digest()


def native_integration_digest() -> str:
    """Pin the native assembly without coupling replay to unrelated rollback code.

    **Why this no longer hashes source text.** It used to hash
    ``inspect.getsource`` of ten functions in this module. ``getsource``
    returns the text as written, so a reflow, a renamed local, or a corrected
    comment changed ``integration_sha256`` while the projection it was meant to
    pin behaved identically. Every retained ``pdf_span`` and ``pdf_cell``
    citation is bound to the assembly digest recorded with it, so an edit of
    that kind made all of them unreplayable: ``replay_native_segment`` saw a
    reader identity it could not obtain and the Source Passage Check reported
    *Cited location cannot be re-read* for a change that altered nothing a
    customer could see. A digest that fires on comments is not a proof of the
    projection; it is a scheduled loss of every citation.

    What replaces it is what the second half of this dictionary already did:
    module bytes for the two files that own the segment and prose projections,
    plus ``NATIVE_INTEGRATION_VERSION`` for this module's half. This module's
    own bytes deliberately stay out -- it also carries the OCR layers, the
    Textract reading, retention and persistence, and a change to any of those
    must not invalidate a native locator; ``test_reader_segments`` asserts that
    those bytes are never read here.

    The cost is that the version constant is declared rather than derived: a
    behaviour change to ``page_text_projection``, ``_reader_word_lines`` or
    ``_line_words`` must bump it in the same change, exactly as a migration
    revision or a prompt version must be advanced deliberately. Nothing else in
    the identity weakens: ``reading_sha256`` still covers the reader's actual
    result and ``reader_runtime_sha256`` still covers the imported reader's own
    files, byte for byte.
    """
    return _digest(_canonical({
        "native_integration_version": NATIVE_INTEGRATION_VERSION,
        "native_modules": {
            name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("reader_segments.py", "prose_spans.py")
        },
    }))



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

    # The shared reading is also the owner of prose offsets and cell IDs.
    return read_native_pdf(
        path, source_sha256=source_sha256, executor=executor
    ).token_layers


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


def reader_native_token_layer(
    page: dict[str, Any], *, identity: EngineIdentity, source_sha256: str
) -> TokenLayer:
    # A line the reader assembles from whitespace alone contributes no words,
    # and a line index with no token would make the projection guess at a gap
    # it cannot see. Such a line is dropped here, so every recorded line index
    # holds at least one token and the projection is exact over the artifact.
    lines = [line for line in _reader_word_lines(page["characters"]["value"]) if line]
    tokens: list[Token] = []
    for line_no, line in enumerate(lines):
        for raw_text, box, object_id, _ in line:
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
    projected = "\n".join(" ".join(word for word, _, _, _ in line) for line in lines)
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
            "token_source_indices": [indices for line in lines for _, _, _, indices in line],
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
) -> list[list[tuple[str, list[float], int, list[int]]]]:
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
) -> list[tuple[str, list[float], int, list[int]]]:
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
    words: list[tuple[str, list[float], int, list[int]]] = []
    source_indices: list[int] = []
    text = ""
    word_box: list[float] | None = None
    object_id = 0
    previous: list[float] | None = None

    def close() -> None:
        nonlocal text, word_box, source_indices
        if text and word_box is not None:
            words.append((text, word_box, object_id, source_indices))
        text, word_box = "", None
        source_indices = []

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
            source_indices.append(character["source_index"])
        previous = box
    close()
    return words


def render_point_to_pdf(
    derivative: RenderDerivative, point: tuple[float, float]
) -> tuple[float, float]:
    """Map a raster-pixel point back to PDF page coordinates by inverting the
    derivative's recorded transform chain (raster→page is the chain reversed)."""

    current = point
    for step in reversed(derivative.transforms):
        current = step.inverse_point(current)
    return current


def textract_reading_tokens(reading: dict[str, Any]) -> tuple[Token, ...]:
    """The normalized Textract page as positioned OCR tokens, in reading order.

    The reading the adapter returns is already in the reader's own frame —
    displayed crop, PDF points, top-left origin — because Textract answers in
    ratios of the image it was sent and the boundary multiplies them by the
    page size it recorded for that raster. So a token box needs the fixed-point
    scale and nothing else; there is no render transform left to invert, which
    is the difference between this engine and the local one below it.

    Every table cell that holds text is one token, then every line the reading
    found outside a table. Textract does not return a word list beside its
    cells, and inventing one by splitting a cell's text would hand each
    fragment a box it was never measured at. Confidence is the provider's own
    score rescaled to 0..1 and is a recorded signal, never proof: 695 of the
    901 mismatched cells in the measured clean-scan lane carried a mean word
    confidence of 95 or more (ADR-0094).
    """

    tokens: list[Token] = []
    for table_no, table in enumerate(reading.get("tables") or ()):
        for cell in table.get("cells") or ():
            text = str(cell.get("text") or "")
            if not text.strip():
                continue
            tokens.append(
                Token(
                    ordinal=len(tokens),
                    origin="ocr",
                    raw_text=text,
                    normalized_text=normalize(text),
                    polygon_pdf=_rect_from_points(*cell["box"]),
                    confidence=provider_confidence(cell.get("confidence")),
                    block=table_no,
                    line=int(cell["row"]),
                )
            )
    for item in reading.get("outside") or ():
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        tokens.append(
            Token(
                ordinal=len(tokens),
                origin="ocr",
                raw_text=text,
                normalized_text=normalize(text),
                polygon_pdf=_rect_from_points(*item["box"]),
                confidence=provider_confidence(item.get("confidence")),
            )
        )
    return tuple(tokens)


def provider_confidence(value: object) -> float | None:
    """Textract reports 0..100; a token layer records 0..1, or nothing at all."""

    if value is None:
        return None
    return max(0.0, min(1.0, float(value) / 100.0))


def textract_token_layer(
    reading: dict[str, Any],
    *,
    page_no: int,
    source_sha256: str,
    provenance: dict[str, Any],
    configuration: dict[str, Any],
    render_profile_id: str | None = None,
) -> TokenLayer:
    """One OCR token layer read by Textract, pinned by the provider's own identity.

    The layer's `origin` stays `ocr` — that is the class of reading the
    manifest column records and geometry readers filter on, and it is `native`
    or `ocr` in the database's own check constraint. What says the reading came
    from Textract is the engine identity: `engine` is the provider, and beside
    it sit the four things ADR-0094 requires a cloud reading to retain — the
    model version the provider reported, the request identity, the digest of
    the raw response and the digest of the normalized reading. A local engine's
    executable version and traineddata digests have no counterpart here, so the
    layer records the provider's answers rather than pretending to pin an
    executable it never ran.

    `provenance` is the adapter's binding for this page (#732): it is the only
    place those digests come from, so a layer cannot claim a response it was
    not read out of.
    """

    tokens = textract_reading_tokens(reading)
    confidences = [token.confidence for token in tokens if token.confidence is not None]
    identity = EngineIdentity(
        origin="ocr",
        engine=TEXTRACT_ENGINE,
        # The provider reports its own version per response; a layer read from
        # a response that did not report one records that, rather than a
        # version it guessed.
        engine_version=str(provenance.get("model_version") or "unreported"),
        adapter_version=TEXTRACT_OCR_ADAPTER_VERSION,
        dpi=(provenance.get("frame") or {}).get("dpi"),
        preprocessing_profile=(provenance.get("normalization") or {}).get("parser"),
        render_profile_id=render_profile_id,
        configuration=dict(configuration),
        provider=TEXTRACT_ENGINE,
        provider_model_version=provenance.get("model_version"),
        provider_request_id=(provenance.get("request_identity") or {}).get("request_id"),
        raw_response_sha256=provenance.get("raw_response_digest"),
        reading_sha256=provenance.get("normalized_reading_digest"),
    )
    return TokenLayer(
        page_no=page_no,
        origin="ocr",
        source_sha256=source_sha256,
        identity=identity,
        tokens=tokens,
        quality={
            "token_count": len(tokens),
            "mean_confidence": (
                sum(confidences) / len(confidences) if confidences else None
            ),
            "low_confidence_tokens": sum(1 for value in confidences if value < 0.5),
            "tables": len(reading.get("tables") or ()),
            "cells": sum(len(table.get("cells") or ()) for table in reading.get("tables") or ()),
            "outside_lines": len(reading.get("outside") or ()),
            "raster_sha256": provenance.get("raster_sha256"),
            "response_was_cached": provenance.get("cached"),
        },
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
