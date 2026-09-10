"""What a retained native reading must reproduce: exact segments and cell addresses.

This is the projection half of the native reader boundary (#736, ADR-0094,
ADR-0095). One sealed ``NativePdfReading`` becomes exact ``pdf_cell`` and
``pdf_span`` values here, and a cell's address is composed here, so replaying a
retained citation means running exactly this code over exactly that reading.

**Why it is its own module.** ``token_layers.native_integration_digest`` pins the
application assembly by module bytes, and every retained ``pdf_span`` and
``pdf_cell`` carries that digest and must match it to be re-read. While this code
lived in ``reader_segments`` the digest also covered ``replay_native_segment``,
``replay_native_segments`` and ``append_native_segments`` with their docstrings,
so correcting a refusal message there changed ``integration_sha256`` and the
Source Passage Check reported *Cited location cannot be re-read* for every
retained native citation — the exact regression that digest was rewritten to
remove, one file up. Replay and append read the projection; they are not the
projection, and they are no longer hashed as if they were.

Nothing here touches the database, a session, or a ``SourceSegment`` row: it
turns one reading into values, and the caller decides what to do with them.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json

from corridor.models import Document
from corridor.prose_spans import page_prose_ranges
from corridor.source_append import SegmentValues
from corridor.token_layers import (
    TokenLayer,
    NativePdfReading,
    PDF_SEGMENT_SCHEME,
    page_text_projection,
    reader_native_token_layer,
)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: str) -> str:
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
                        content_sha256=digest(cell["text"]),
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
                        content_sha256=digest(exact),
                        ordinal=counts["pdf_span"],
                        page_no=page_no,
                        span_stream=stream,
                        start_offset=start,
                        end_offset=end,
                        location_json={
                            "geometry": page["geometry"],
                            "page_text_sha256": digest(page_text),
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
