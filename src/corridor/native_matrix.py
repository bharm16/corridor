"""Explicit native matrix challenger with the measured model input (#737).

The incumbent matrix extractor combines word geometry and transcription. This
seam instead uses the unchanged imported ID listing, strict structure schema
and prompt, with the measured 110-dpi image. Its native cells come from the
sealed tagged/36-dpi reading. It never calls the incumbent or OCR on refusal,
and importing it does not select a production route.

Mapping is outside the append transaction. The pure binding boundary seals
source references, and extraction_runs owns the atomic Source Fact append
before compatibility proposals. A client must be explicitly injected: the
offline measurement uses retained answers and creates no provider client.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import math
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy.orm import Session

from corridor.extractor_lineage import (
    deployed_native_matrix_config,
    token_usage_delta,
    usage_snapshot,
)
from corridor.models import Candidate, Document, ExtractionRun, Fact
from corridor.native_matrix_bindings import (
    MODEL_IMAGE_DPI,
    NATIVE_MATRIX_SCHEMA_VERSION,
    NativeMatrixMapping,
    NativeMatrixRefused,
    bind_native_matrix,
)
from corridor.token_layers import NativePdfReading
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE, PdfiumExecutor
from corridor_pdf_reader.replacement import semantics
from corridor_pdf_reader.replacement.pages import slim_page

PROMPT_VERSION = semantics.PROMPT_VERSION
SCHEMA_VERSION = NATIVE_MATRIX_SCHEMA_VERSION


@dataclass(frozen=True)
class NativeMatrixExtraction:
    mapping: NativeMatrixMapping
    run: ExtractionRun
    facts: tuple[Fact, ...]
    candidates: tuple[Candidate, ...]
    field_outcomes: tuple[dict[str, Any], ...]
    created: bool


def render_native_matrix_context(
    source_path: Path | str, output_dir: Path | str, page_numbers: list[int],
) -> dict[int, Path]:
    """The exact measured image operation, isolated from the calling thread."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    return PdfiumExecutor().run(
        semantics._render, Path(source_path), list(page_numbers), directory, MODEL_IMAGE_DPI,
    )


def _image_digest(page: dict[str, Any], image: Path) -> str:
    if not image.is_file():
        raise NativeMatrixRefused(f"missing measured image for page {page['number']}")
    with Image.open(image) as opened:
        size = list(page["size"])
        if page["rotation"] % 180:
            size.reverse()
        expected = tuple(math.ceil(value * MODEL_IMAGE_DPI / 72) for value in size)
        if opened.size != expected:
            raise NativeMatrixRefused(
                f"page {page['number']} image is {opened.size}, expected {expected} at {MODEL_IMAGE_DPI} dpi"
            )
    return sha256(image.read_bytes()).hexdigest()


def _structure_answer(raw: Any) -> dict[str, Any]:
    """Check the measured strict shape even when the client is a test double."""
    if not isinstance(raw, dict):
        raise NativeMatrixRefused("matrix structure answer is not an object")
    answer = {key: value for key, value in raw.items() if key != "_meta"}
    if set(answer) != set(semantics.STRUCTURE_SCHEMA["required"]):
        raise NativeMatrixRefused("matrix structure answer differs from the measured strict schema")
    if type(answer["is_utility_matrix"]) is not bool:
        raise NativeMatrixRefused("matrix classification must be boolean")
    for key in ("matrix_table", "header_row"):
        if answer[key] is not None and type(answer[key]) is not int:
            raise NativeMatrixRefused(f"{key} must be an integer or null")
    if not isinstance(answer["columns"], list):
        raise NativeMatrixRefused("column mappings must be a list")
    for column in answer["columns"]:
        if (not isinstance(column, dict) or set(column) != {"index", "canonical_field"}
                or type(column["index"]) is not int
                or (column["canonical_field"] is not None and not isinstance(column["canonical_field"], str))):
            raise NativeMatrixRefused("column mapping differs from the measured strict schema")
    attributes = answer["page_attributes"]
    if (not isinstance(attributes, dict)
            or set(attributes) != set(semantics.STRUCTURE_SCHEMA["properties"]["page_attributes"]["required"])
            or any(value is not None and not isinstance(value, str) for value in attributes.values())):
        raise NativeMatrixRefused("page attributes must be the measured source IDs")
    confidence = answer["mapping_confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence):
        raise NativeMatrixRefused("mapping confidence must be a finite number")
    return answer


def map_native_matrix(
    document: Document, *, reading: NativePdfReading, client,
    images: dict[int, Path], document_label: str,
) -> NativeMatrixMapping:
    """Map structure with unchanged prompt/listing/images and bind every value."""
    if (not isinstance(reading, NativePdfReading) or document.id is None
            or document.sha256 != reading.rendition_sha256):
        raise NativeMatrixRefused("native matrix needs its registered source reading")
    native = reading.identity["native_layer"]
    if (native["configuration"]["reader_engine"] != MEASURED_ENGINE
            or native["dpi"] != MEASURED_DPI):
        raise NativeMatrixRefused("matrix reading differs from the measured tagged/36 configuration")
    if not isinstance(document_label, str) or not document_label:
        raise NativeMatrixRefused("the measured listing needs its document label")
    pages = [slim_page(page) for page in reading.pages]
    if set(images) != {page["number"] for page in pages}:
        raise NativeMatrixRefused("every native page needs its measured image context")
    image_digests = {page["number"]: _image_digest(page, Path(images[page["number"]])) for page in pages}
    config = deployed_native_matrix_config(client=client)
    system = semantics.PROMPT_PATH.read_text()
    carried = None
    mapped = []
    for page in pages:
        image = Path(images[page["number"]])
        raw = semantics.map_page(client, page, document_label, image, system)
        evidence = {"number": page["number"], "structure": raw,
                    "listing": semantics.listing(page, document_label),
                    "image_sha256": image_digests[page["number"]]}
        try:
            answer = _structure_answer(raw)
        except NativeMatrixRefused as exc:
            raise NativeMatrixRefused(str(exc), pages=[*mapped, evidence]) from exc
        if sha256(image.read_bytes()).hexdigest() != image_digests[page["number"]]:
            raise NativeMatrixRefused("model image bytes changed while the mapping ran")
        try:
            result, carried = semantics.read_page(page, answer, carried)
        except semantics.SequencingSemanticsDetected as exc:
            raise NativeMatrixRefused(str(exc), pages=[*mapped, evidence]) from exc
        if result.is_utility_matrix and (result.matrix_table is None or result.mapping is None):
            raise NativeMatrixRefused(
                "; ".join(result.refused) or "matrix structure was refused",
                pages=[*mapped, {**evidence, "reading": semantics.reading_dict(result)}],
            )
        mapped.append({
            "number": page["number"], "structure": answer,
            "reading": semantics.reading_dict(result),
            "listing": semantics.listing(page, document_label),
            "image_sha256": image_digests[page["number"]],
        })
    try:
        return bind_native_matrix(document, reading, mapped, asdict(config))
    except NativeMatrixRefused as exc:
        raise NativeMatrixRefused(str(exc), pages=mapped) from exc


def extract_native_matrix(
    session: Session, document: Document, *, reading: NativePdfReading,
    source_path: Path | str, client, images: dict[int, Path], document_label: str,
    idempotency_key: str | None = None, fail_after_stage: str | None = None,
) -> NativeMatrixExtraction:
    """The explicit challenger: map, then atomically append captured source facts."""
    from corridor.extraction_runs import append_native_matrix_source_facts

    if sha256(Path(source_path).read_bytes()).hexdigest() != document.sha256:
        raise NativeMatrixRefused("native matrix source bytes do not match its Document")
    before = usage_snapshot(client)
    mapping = map_native_matrix(
        document, reading=reading, client=client, images=images, document_label=document_label,
    )
    usage = token_usage_delta(before, usage_snapshot(client), document_ids=[document.id])
    result = append_native_matrix_source_facts(
        session, document, mapping=mapping, source_path=source_path,
        idempotency_key=idempotency_key, token_usage=usage, fail_after_stage=fail_after_stage,
    )
    return NativeMatrixExtraction(mapping=mapping, **result)
