"""One versioned native reading supplies page text, tokens and PDF segments.

The first adapter for this reader, written while the reader was still an
unreleased alternative to the incumbent parser, wrote reader page text beside
incumbent prose offsets. Cells also had page-local IDs which could be mistaken
for cells in another rendition. This boundary (#736) runs the imported reader
once and binds every projection to its exact result and configuration. That
reader is now the only one in the product (ADR-0094, ADR-0095, #741); neither
a new configuration nor a replay can update an existing Source Segment.

Historical ``prose_span`` locators remain in source_segments.py. These
``pdf_span`` and ``pdf_cell`` locators replay only with the recorded reader and
only inside the recorded reading. An unavailable reader configuration and a
reading that no longer reproduces both fail closed as availability refusals,
never as a claim that the passage left the page. Clipped glyphs have a separate
span stream, so hidden text is citable without presenting it as visible page
text. Cell IDs select values; there is deliberately no model-supplied text
argument.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, SourceSegment
from corridor.source_append import SegmentValues, append_source_segments
from corridor.prose_spans import page_prose_ranges
from corridor.source_segment_errors import (
    NativeReaderUnavailable,
    RecordedReadingNotReproduced,
    SourceDocumentDigestMismatch,
    SourceSegmentLocatorMismatch,
)
from corridor.token_layers import (
    TokenLayer,
    NativePdfReading,
    PDF_SEGMENT_SCHEME,
    read_native_pdf,
    page_text_projection,
    reader_native_token_layer,
)

NATIVE_KINDS = frozenset({"pdf_span", "pdf_cell"})


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def pdf_cell_id(
    document: Document,
    reading_sha256: str,
    page_no: int,
    table_index: int,
    row: int,
    column: int,
) -> str:
    """A cell address binds its rendition/configuration/result before positions."""

    return (
        f"{PDF_SEGMENT_SCHEME}:project{document.project_id}:document{document.id}:"
        f"{reading_sha256}:p{page_no}:t{table_index}:r{row}:c{column}"
    )


def native_segment_values(reading: NativePdfReading) -> tuple[SegmentValues, ...]:
    """Project exact source segments; preserve glyph ownership without guessing."""

    values: list[SegmentValues] = []
    counts: Counter[str] = Counter()
    shared = {
        "rendition_sha256": reading.rendition_sha256,
        "reading_sha256": reading.reading_sha256,
        "reader_identity": reading.identity,
    }
    layers = {layer.page_no: layer for layer in reading.token_layers}
    for page in reading.pages:
        page_no = page["number"]
        ownership = Counter(
            index
            for table in page["tables"]["value"]
            for cell in table["structured_cells"]
            for index in cell["source_indices"]
        )
        characters = {c["source_index"]: c for c in page["characters"]["value"]}
        for table_index, table in enumerate(page["tables"]["value"]):
            for cell in table["structured_cells"]:
                if not cell["text"]:
                    continue  # empty cells remain in the reading, never an invented value
                indices = cell["source_indices"]
                # A glyph claimed by several cells remains in the page span;
                # it is never promoted into an arbitrary winner's value.
                if not indices or any(
                    ownership[index] != 1 or index not in characters
                    for index in indices
                ):
                    continue
                counts["pdf_cell"] += 1
                values.append(
                    SegmentValues(
                        kind="pdf_cell",
                        exact_text=cell["text"],
                        content_sha256=_digest(cell["text"]),
                        ordinal=counts["pdf_cell"],
                        page_no=page_no,
                        table_index=table_index,
                        cell_row=cell["row"],
                        cell_column=cell["column"],
                        row_span=cell["row_span"],
                        column_span=cell["column_span"],
                        location_json={
                            "geometry": page["geometry"],
                            "table_box": table["box"],
                            "display_box": cell["box"],
                            "cut": cell.get("cut"),
                            "glyphs": [characters[index] for index in indices],
                        },
                        **shared,
                    )
                )
        for stream in ("page", "clipped"):
            layer = (
                layers[page_no]
                if stream == "page"
                else reader_native_token_layer(
                    {**page, "characters": page["clipped"]},
                    identity=layers[page_no].identity,
                    source_sha256=reading.rendition_sha256,
                )
            )
            page_text = page_text_projection(layer)
            glyphs = (
                characters
                if stream == "page"
                else {c["source_index"]: c for c in page["clipped"]["value"]}
            )
            positioned_tokens = _token_ranges(layer)
            for start, end in page_prose_ranges(page_text):
                indices = list(
                    dict.fromkeys(
                        index
                        for token_start, token_end, token in positioned_tokens
                        if token_start < end and start < token_end
                        for index in layer.quality["token_source_indices"][
                            token.ordinal
                        ]
                    )
                )
                counts["pdf_span"] += 1
                exact = page_text[start:end]
                values.append(
                    SegmentValues(
                        kind="pdf_span",
                        exact_text=exact,
                        content_sha256=_digest(exact),
                        ordinal=counts["pdf_span"],
                        page_no=page_no,
                        span_stream=stream,
                        start_offset=start,
                        end_offset=end,
                        location_json={
                            "geometry": page["geometry"],
                            "page_text_sha256": _digest(page_text),
                            "glyphs": [glyphs[index] for index in indices],
                            "outside_source_indices": [
                                index for index in indices if not ownership[index]
                            ],
                            "ambiguous_source_indices": [
                                index for index in indices if ownership[index] > 1
                            ],
                        },
                        **shared,
                    )
                )
    return tuple(values)


def _token_ranges(layer: TokenLayer) -> list[tuple[int, int, object]]:
    positioned = []
    cursor = 0
    for index, token in enumerate(layer.tokens):
        if index:
            cursor += 1  # exactly one space or newline in the projection
        positioned.append((cursor, cursor + len(token.raw_text), token))
        cursor += len(token.raw_text)
    return positioned


def append_native_segments(
    session: Session, document: Document, reading: NativePdfReading
) -> tuple[SourceSegment, ...]:
    """Append one explicit reading; never early-return a different reading's rows."""

    if document.sha256 != reading.rendition_sha256:
        raise SourceDocumentDigestMismatch(
            "reading belongs to another Document rendition"
        )
    return append_source_segments(
        session,
        project_id=document.project_id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=native_segment_values(reading),
    )


def replay_native_segment(
    document: Document, segment: SourceSegment, path: Path | str
) -> str:
    """Only the same reader/configuration/result may answer a native locator.

    Three different refusals, because the Source Passage Check tells the
    customer three different things. The recorded reader or configuration not
    being installed here is ``NativeReaderUnavailable``; the recorded reader
    running and not returning the reading the locator indexes is
    ``RecordedReadingNotReproduced``; both are availability, feed
    ``locator_validation.NOT_RE_READABLE``, and say *Cited location cannot be
    re-read*. Only a locator that was actually followed into the recorded
    reading and reached different content is ``SourceSegmentLocatorMismatch``,
    which is the integrity failure that says *Not found at cited location*.
    All three refuse without returning text; a reader identity too incomplete
    to name any reader stays an integrity failure, because the row cannot
    describe a cited location at all.
    """

    identity = segment.reader_identity or {}
    try:
        config = identity["native_layer"]
        if identity["scheme"] != PDF_SEGMENT_SCHEME:
            raise KeyError("scheme")
        reading = read_native_pdf(
            path,
            source_sha256=document.sha256,
            engine=config["configuration"]["reader_engine"],
            dpi=config["dpi"],
        )
    except (KeyError, TypeError) as exc:
        raise SourceSegmentLocatorMismatch(
            "native segment reader identity is incomplete"
        ) from exc
    if identity != reading.identity:
        raise NativeReaderUnavailable(
            PDF_SEGMENT_SCHEME, "the recorded native reader configuration"
        )
    if segment.reading_sha256 != reading.reading_sha256:
        raise RecordedReadingNotReproduced(
            PDF_SEGMENT_SCHEME, "the recorded native reading"
        )
    for candidate in native_segment_values(reading):
        if candidate.kind == segment.kind and candidate.ordinal == segment.ordinal:
            for key in candidate.__dataclass_fields__:
                if getattr(segment, key) != getattr(candidate, key):
                    raise SourceSegmentLocatorMismatch(
                        "native source locator changed under replay"
                    )
            return candidate.exact_text
    raise SourceSegmentLocatorMismatch("native source locator does not exist")


def replay_native_segments(document, segments, path):
    """Verify one coherent prose reading once, including every retained locator.

    Refusals keep the families ``replay_native_segment`` separates: a missing
    reader configuration and a reading the installed reader does not reproduce
    are ``FreshReadingUnavailable``, since nothing opened a page; only a
    locator followed into the reproduced reading may report a mismatch against
    the source.
    """
    segments = tuple(segments)
    if sha256(Path(path).read_bytes()).hexdigest() != document.sha256:
        raise SourceDocumentDigestMismatch("prose rendition bytes changed")
    if not segments:
        return {}
    if len({segment.reading_sha256 for segment in segments}) != 1:
        raise SourceSegmentLocatorMismatch("prose capture requires one coherent native reading")
    first = segments[0]
    identity = first.reader_identity or {}
    config = identity.get("native_layer", {})
    try:
        reading = read_native_pdf(path, source_sha256=document.sha256,
            engine=config["configuration"]["reader_engine"], dpi=config["dpi"])
    except (KeyError, TypeError) as exc:
        raise SourceSegmentLocatorMismatch("prose reader identity is incomplete") from exc
    # Neither of these opened a page, so neither may speak about the source:
    # the reader this batch was captured with is absent, or it ran and did not
    # return the reading these offsets are positions inside.
    if reading.identity != identity:
        raise NativeReaderUnavailable(
            PDF_SEGMENT_SCHEME, "the recorded native reader configuration"
        )
    if reading.reading_sha256 != first.reading_sha256:
        raise RecordedReadingNotReproduced(
            PDF_SEGMENT_SCHEME, "the recorded native reading"
        )
    expected = {(item.kind, item.ordinal): item for item in native_segment_values(reading)}
    result = {}
    for segment in segments:
        value = expected.get((segment.kind, segment.ordinal))
        if (segment.project_id != document.project_id or segment.document_id != document.id or value is None
                or any(getattr(segment, field) != getattr(value, field) for field in value.__dataclass_fields__)):
            raise SourceSegmentLocatorMismatch("prose source locator changed under replay")
        result[segment.id] = value.exact_text
    return result


@dataclass(frozen=True, init=False)
class NativeCellIndex:
    """A document-scoped immutable address index, constructed once per reading.

    Canonical strings keep nested locator dictionaries out of retained state.
    Each selected cell decodes only its own expected value, so a matrix with
    thousands of fields never re-segments the document for every field.
    """

    project_id: int
    document_id: int
    rendition_sha256: str
    reading_sha256: str
    _cells: MappingProxyType

    def __init__(self, document: Document, reading: NativePdfReading):
        if not isinstance(reading, NativePdfReading):
            raise SourceSegmentLocatorMismatch("cell index needs a native PDF reading")
        if document.id is None or document.sha256 != reading.rendition_sha256:
            raise SourceSegmentLocatorMismatch(
                "cell index needs the registered reading's Document"
            )
        cells = {}
        for value in native_segment_values(reading):
            if value.kind != "pdf_cell":
                continue
            key = pdf_cell_id(
                document,
                reading.reading_sha256,
                value.page_no,
                value.table_index,
                value.cell_row,
                value.cell_column,
            )
            if key in cells:
                raise SourceSegmentLocatorMismatch(
                    "native reading repeats a cell address"
                )
            cells[key] = _canonical(asdict(value))
        object.__setattr__(self, "project_id", document.project_id)
        object.__setattr__(self, "document_id", document.id)
        object.__setattr__(self, "rendition_sha256", reading.rendition_sha256)
        object.__setattr__(self, "reading_sha256", reading.reading_sha256)
        object.__setattr__(self, "_cells", MappingProxyType(cells))

    def expected(self, cell_id: str) -> SegmentValues:
        encoded = self._cells.get(cell_id)
        if encoded is None:
            raise SourceSegmentLocatorMismatch(
                "cell ID is outside its document/rendition/reader configuration"
            )
        return SegmentValues(**json.loads(encoded))


def select_pdf_cell_segment(
    session: Session,
    *,
    document: Document,
    index: NativeCellIndex,
    page_no: int,
    table_index: int,
    cell_id: str,
) -> SourceSegment:
    """Resolve a model ID through a prebuilt index. No literal value is accepted."""

    if not isinstance(index, NativeCellIndex) or (
        document.id != index.document_id
        or document.project_id != index.project_id
        or document.sha256 != index.rendition_sha256
    ):
        raise SourceSegmentLocatorMismatch("cell index belongs to another Document")
    expected = index.expected(cell_id)
    if expected.page_no != page_no or expected.table_index != table_index:
        raise SourceSegmentLocatorMismatch(
            "cell ID is outside its declared page or table"
        )
    segment = session.scalar(
        select(SourceSegment).where(
            SourceSegment.project_id == document.project_id,
            SourceSegment.document_id == document.id,
            SourceSegment.kind == "pdf_cell",
            SourceSegment.reading_sha256 == index.reading_sha256,
            SourceSegment.page_no == page_no,
            SourceSegment.table_index == table_index,
            SourceSegment.cell_row == expected.cell_row,
            SourceSegment.cell_column == expected.cell_column,
        )
    )
    if segment is None or any(
        getattr(segment, key) != getattr(expected, key)
        for key in expected.__dataclass_fields__
    ):
        raise SourceSegmentLocatorMismatch(
            "cell does not match the indexed native reading"
        )
    return segment


def native_segment_pdf_boxes(segment: SourceSegment) -> tuple[tuple[float, ...], ...]:
    """Original PDF user-space ink boxes, including non-zero media/crop origins."""

    if segment.kind not in NATIVE_KINDS or not segment.location_json:
        raise SourceSegmentLocatorMismatch("segment has no native glyph locator")
    crop = segment.location_json["geometry"]["crop_box"]
    return tuple(
        (
            crop[0] + glyph["box"][0],
            crop[3] - glyph["box"][3],
            crop[0] + glyph["box"][2],
            crop[3] - glyph["box"][1],
        )
        for glyph in segment.location_json["glyphs"]
    )


def native_segment_render_boxes(
    segment: SourceSegment, derivative
) -> tuple[tuple[float, ...], ...]:
    """Follow the actual selected raster/crop/deskew chain, never a guessed scale."""

    if (
        derivative.regenerable_from.source_sha256 != segment.rendition_sha256
        or derivative.regenerable_from.page_number != segment.page_no
    ):
        raise SourceSegmentLocatorMismatch("render belongs to another source page")
    output = []
    for x0, y0, x1, y1 in native_segment_pdf_boxes(segment):
        points = [(x * 1000, y * 1000) for x in (x0, x1) for y in (y0, y1)]
        for transform in derivative.transforms:
            points = [transform.forward_point(point) for point in points]
        output.append(
            (
                min(p[0] for p in points),
                min(p[1] for p in points),
                max(p[0] for p in points),
                max(p[1] for p in points),
            )
        )
    return tuple(output)
