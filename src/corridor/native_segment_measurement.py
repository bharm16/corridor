"""Score the actual typed native Source Segment handoff against paired renditions.

The frozen reader's score cannot prove that its nonempty cells survive
``native_segment_values``. This measurement driver (#736) calls that public
boundary after ``read_native_pdf``, then supplies the unchanged paired scorer
with values from the resulting SegmentValues only. Missing, ambiguous or
invalid cell segments become empty-cell omissions; the reader's original
cell string is never a fallback. Empty source cells require no segment.

Reader metadata supplies outside/clipped scorer evidence. Generated page and
clipped source spans are retained separately with exact text, offsets, digest
and glyph membership, without restoring whitespace. This is an in-memory
typed handoff measurement, not a database append, customer-citation audit or
production selection. Database append and ID-selection proofs live in the
integration tests. No workbook, model or remote service enters this driver.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import json
from pathlib import Path
import time
import traceback
from typing import Any

from corridor.reader_segments import native_segment_values
from corridor.source_append import SegmentValues
from corridor.token_layers import (
    NativePdfReading,
    PDF_SEGMENT_SCHEME,
    READER_NATIVE_ADAPTER_VERSION,
    read_native_pdf,
)
from corridor_pdf_reader.bootstrap.corpus import Pair, load_pairs
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE
from corridor_pdf_reader.replacement.pages import slim_page

DRIVER = "corridor.native_segment_measurement"
DRIVER_VERSION = "native-segments-measurement-v1"
SCOPE = (
    "Actual read_native_pdf -> native_segment_values -> typed cell handoff; "
    "no database persistence or customer-citation audit. Outside and clipped "
    "scorer evidence comes from reader metadata; generated source spans are "
    "retained separately without whitespace restoration. Source addresses "
    "name readings, not model-facing registered Document IDs."
)
METRICS = (
    "pages", "nonempty_reader_cells", "empty_reader_cells",
    "generated_cell_segments", "materialized_nonempty_cells",
    "omitted_nonempty_cells", "missing_cell_segments", "ambiguous_cell_segments",
    "invalid_cell_segments", "cell_segment_records_not_used",
    "generated_page_spans", "generated_clipped_spans", "source_characters",
    "outside_source_characters", "ambiguously_assigned_source_characters",
    "characters_assigned_to_materialized_cells", "characters_not_in_materialized_cells",
    "page_characters_in_source_spans", "clipped_source_characters",
    "clipped_characters_in_source_spans",
)


def configuration_identity() -> dict[str, Any]:
    """Pin the measurement adapter and the actual segment/reading implementation."""

    root = Path(__file__).resolve().parents[2]
    files = {
        name: sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "src/corridor/native_segment_measurement.py",
            "src/corridor/reader_segments.py",
            "src/corridor/token_layers.py",
            "src/corridor/prose_spans.py",
            "src/corridor/source_append.py",
            "src/corridor_pdf_reader/measurement.py",
        )
    }
    identity = {
        "driver": DRIVER,
        "version": DRIVER_VERSION,
        "segment_scheme": PDF_SEGMENT_SCHEME,
        "native_adapter_version": READER_NATIVE_ADAPTER_VERSION,
        "implementation_files": files,
    }
    return {
        **identity,
        "identity_sha256": sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _cell_address(value: SegmentValues) -> tuple[int | None, ...]:
    return value.page_no, value.table_index, value.cell_row, value.cell_column


def _matches_cell(
    value: SegmentValues,
    *,
    reading: NativePdfReading,
    identity: dict[str, Any],
    cell: dict[str, Any],
    characters: dict[int, dict[str, Any]],
    ownership: Counter[int],
) -> bool:
    """A row supplies a source value only with its own source glyph membership."""

    if (
        value.rendition_sha256 != reading.rendition_sha256
        or value.reading_sha256 != reading.reading_sha256
        or value.reader_identity != identity
        or not value.exact_text
        or sha256(value.exact_text.encode()).hexdigest() != value.content_sha256
        or not isinstance(value.row_span, int) or value.row_span < 1
        or not isinstance(value.column_span, int) or value.column_span < 1
    ):
        return False
    location = value.location_json or {}
    glyphs = location.get("glyphs", [])
    indices = [glyph.get("source_index") for glyph in glyphs]
    return (
        bool(indices)
        and len(indices) == len(set(indices))
        and set(indices) == set(cell["source_indices"])
        and all(ownership[index] == 1 and characters.get(index) == glyph
                for index, glyph in zip(indices, glyphs, strict=True))
        and isinstance(location.get("display_box"), list)
        and len(location["display_box"]) == 4
    )


def segment_reading(reading: NativePdfReading, *, key: str, pdf: str) -> dict[str, Any]:
    """Build scorer input from this execution's actual SegmentValues."""

    values = native_segment_values(reading)
    identity = reading.identity
    cells: dict[tuple[int | None, ...], list[SegmentValues]] = defaultdict(list)
    spans: dict[int | None, list[SegmentValues]] = defaultdict(list)
    metrics = Counter({name: 0 for name in METRICS})
    for value in values:
        if value.kind == "pdf_cell":
            cells[_cell_address(value)].append(value)
            metrics["generated_cell_segments"] += 1
        elif value.kind == "pdf_span":
            spans[value.page_no].append(value)

    output_pages = []
    source_spans = []
    for page in reading.pages:
        page_no = page["number"]
        metrics["pages"] += 1
        projected = slim_page(page)
        characters = {glyph["source_index"]: glyph for glyph in page["characters"]["value"]}
        ownership = Counter(index for table in page["tables"]["value"]
                            for cell in table["structured_cells"] for index in cell["source_indices"])
        assigned: set[int] = set()
        for table_index, (raw_table, table) in enumerate(zip(
            page["tables"]["value"], projected["tables"], strict=True
        )):
            for raw_cell, cell in zip(raw_table["structured_cells"], table["cells"], strict=True):
                had_text = bool(raw_cell["text"])
                cell["text"] = ""  # no reader-string fallback, under any failure
                if not had_text:
                    metrics["empty_reader_cells"] += 1
                    continue
                metrics["nonempty_reader_cells"] += 1
                matches = cells.get((page_no, table_index, cell["row"], cell["column"]), [])
                omission = None
                if not matches:
                    omission = "missing_cell_segments"
                elif len(matches) != 1:
                    omission = "ambiguous_cell_segments"
                elif not _matches_cell(
                    matches[0], reading=reading, identity=identity, cell=raw_cell,
                    characters=characters, ownership=ownership,
                ):
                    omission = "invalid_cell_segments"
                if omission:
                    metrics[omission] += 1
                    metrics["omitted_nonempty_cells"] += 1
                    cell["source_segment_omission"] = omission
                    continue
                value = matches[0]
                location = value.location_json or {}
                cell.update(
                    text=value.exact_text, row_span=value.row_span, column_span=value.column_span,
                    box=[round(coordinate, 2) for coordinate in location["display_box"]],
                    source_segment={
                        "source_address": {
                            "reading_sha256": reading.reading_sha256,
                            "page_no": page_no, "table_index": table_index,
                            "row": cell["row"], "column": cell["column"],
                        },
                        "ordinal": value.ordinal, "content_sha256": value.content_sha256,
                    },
                )
                cell.pop("cut", None)
                if location.get("cut"):
                    cell["cut"] = location["cut"]
                assigned.update(glyph["source_index"] for glyph in location["glyphs"])
                metrics["materialized_nonempty_cells"] += 1
        covered = {"page": set(), "clipped": set()}
        for value in spans.get(page_no, []):
            location = value.location_json or {}
            indices = [glyph["source_index"] for glyph in location.get("glyphs", [])]
            covered[value.span_stream].update(indices)
            metrics[f"generated_{value.span_stream}_spans"] += 1
            source_spans.append({
                "kind": value.kind, "ordinal": value.ordinal, "page_no": value.page_no,
                "span_stream": value.span_stream, "start_offset": value.start_offset,
                "end_offset": value.end_offset, "exact_text": value.exact_text,
                "content_sha256": value.content_sha256, "source_indices": indices,
                "outside_source_indices": location.get("outside_source_indices", []),
                "ambiguous_source_indices": location.get("ambiguous_source_indices", []),
            })
        metrics["source_characters"] += len(characters)
        metrics["outside_source_characters"] += sum(not ownership[index] for index in characters)
        metrics["ambiguously_assigned_source_characters"] += sum(ownership[index] > 1 for index in characters)
        metrics["characters_assigned_to_materialized_cells"] += len(assigned)
        metrics["characters_not_in_materialized_cells"] += len(set(characters) - assigned)
        metrics["page_characters_in_source_spans"] += len(covered["page"])
        metrics["clipped_source_characters"] += len(page["clipped"]["value"])
        metrics["clipped_characters_in_source_spans"] += len(covered["clipped"])
        output_pages.append(projected)
    metrics["cell_segment_records_not_used"] = (
        metrics["generated_cell_segments"] - metrics["materialized_nonempty_cells"]
    )
    return {
        "key": key, "pdf": pdf, "engine": identity["native_layer"]["configuration"]["reader_engine"],
        "source_sha256": reading.rendition_sha256, "pages": output_pages,
        "source_segment_handoff": {
            "scope": SCOPE, "reading_sha256": reading.reading_sha256,
            "reader_identity": identity, "metrics": dict(metrics), "source_spans": source_spans,
        },
    }


def read_one(task: tuple[Pair, Path, str]) -> dict[str, Any]:
    """One PDF through the production isolated reader and source-value boundary."""

    pair, output, engine = task
    started = time.perf_counter()
    try:
        reading = read_native_pdf(pair.pdf_path, source_sha256=pair.pdf_sha256, engine=engine, dpi=MEASURED_DPI)
        result = segment_reading(reading, key=pair.key, pdf=pair.pdf)
        (output / f"{pair.key}.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
        return {
            "key": pair.key, "pages": len(result["pages"]),
            "seconds": round(time.perf_counter() - started, 2),
            "reading_sha256": reading.reading_sha256,
            "source_segment_metrics": result["source_segment_handoff"]["metrics"],
        }
    except Exception as exc:
        return {
            "key": pair.key, "seconds": round(time.perf_counter() - started, 2),
            "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", choices=[MEASURED_ENGINE], default=MEASURED_ENGINE)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--keys", nargs="*")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    reads = args.output / "reads"
    reads.mkdir(parents=True, exist_ok=False)
    selected = set(args.keys) if args.keys else None
    pairs = [pair for pair in load_pairs() if selected is None or pair.key in selected]
    receipts = []
    started = time.perf_counter()
    # PDFium stays in PdfiumExecutor's spawned child. Threads here allow those
    # production calls to overlap without nesting children in a daemon Pool.
    with ThreadPoolExecutor(max_workers=args.jobs) as workers:
        futures = [workers.submit(read_one, (pair, reads, args.engine)) for pair in pairs]
        for future in as_completed(futures):
            receipt = future.result()
            receipts.append(receipt)
            if "error" in receipt:
                print("ERROR", receipt["key"], receipt["error"], flush=True)
            else:
                print(f"READ {receipt['key']} {receipt['pages']} pages", flush=True)
    receipts.sort(key=lambda receipt: receipt["key"])
    metrics = Counter({name: 0 for name in METRICS})
    for receipt in receipts:
        metrics.update(receipt.get("source_segment_metrics", {}))
    result = {
        "engine": args.engine, "seconds": round(time.perf_counter() - started, 1),
        "receipts": receipts, "source_segment_scope": SCOPE,
        "source_segment_metrics": dict(metrics),
    }
    (args.output / "read-receipts.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"{len(receipts)} documents, {metrics['pages']} pages, "
          f"{sum('error' in receipt for receipt in receipts)} failed -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
