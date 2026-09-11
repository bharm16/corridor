"""Print a stored Document Rendition as the imported reader sees it (#729).

`make pdf-reader-inspect` is the one product-facing way to look at what the
reader would hand the tiers above it, before any of them is built on it:
each page's tables, every cell with the ID the semantics tier answers with
(`t0r7c2` is table 0, row 7, column 2; `o3` the third string outside every
table, from `replacement/pages.py`), the text left outside every table, and
the runs the page draws but hides behind a clip. Bytes come from Corridor's
content-addressed store through `corridor.object_storage` and nothing else,
so a rendition is named by its SHA-256 the way every other reader of the
store names it (ADR-0068, #487), or by a file path for a document that is
not stored. The read runs under the PDFium execution contract in its own
process. Nothing here writes anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from corridor_pdf_reader.execution import (
    MEASURED_DPI,
    MEASURED_ENGINE,
    ExecutionLimits,
    PdfiumExecutionError,
    PdfiumExecutor,
)
from corridor_pdf_reader.replacement.pages import cell_id, outside_id, slim_page


def _stage_from_store(sha256: str, scratch: Path) -> Path | None:
    """The stored bytes under their digest, verified by the storage interface."""
    # The only import of Corridor from this package, and the only doorway into
    # the content store (tests/test_architecture.py holds the same rule for
    # src/corridor); the package never builds a store path itself.
    from corridor.object_storage import content_store

    store = content_store()
    key = store.resolve(sha256)
    if key is None:
        return None
    return store.stage(key, scratch / f"{sha256}.pdf", sha256=sha256)


def projection(document: dict[str, Any], sha256: str) -> dict[str, Any]:
    """The slim pages with every cell and outside string carrying its ID."""
    pages = []
    for page in document["pages"]:
        slim = slim_page(page)
        for index, table in enumerate(slim["tables"]):
            for cell in table["cells"]:
                cell["id"] = cell_id(index, cell["row"], cell["column"])
        for index, item in enumerate(slim["outside"]):
            item["id"] = outside_id(index)
        pages.append(slim)
    return {
        "sha256": sha256,
        "engine": document["engine"],
        "version": document["version"],
        "native_version": document["native_version"],
        "page_count": document["page_count"],
        "pages": pages,
    }


def _box(values: Sequence[float]) -> str:
    return "[" + ", ".join(f"{value:.1f}" for value in values) + "]"


def render_text(reading: dict[str, Any]) -> str:
    lines = [
        f"rendition {reading['sha256']}",
        f"engine {reading['engine']}  pypdfium2 {reading['version']}  PDFium {reading['native_version']}"
        f"  page count {reading['page_count']}  pages read {', '.join(str(page['number']) for page in reading['pages'])}",
    ]
    for page in reading["pages"]:
        width, height = page["size"]
        lines.append(f"page {page['number']}  {width:g} x {height:g} pt  rotation {page['rotation']}")
        if not page["tables"]:
            lines.append("  tables: none")
        for index, table in enumerate(page["tables"]):
            lines.append(
                f"  table {index}  method {table['method']}  box {_box(table['box'])}  {len(table['cells'])} cells"
            )
            for cell in table["cells"]:
                span = f"span {cell['row_span']}x{cell['column_span']}"
                cut = f"  cut {cell['cut']}" if cell.get("cut") else ""
                lines.append(
                    f"    {cell['id']}  row {cell['row']} col {cell['column']}  {span}  "
                    f"box {_box(cell['box'])}  {json.dumps(cell['text'], ensure_ascii=False)}{cut}"
                )
        if page["outside"]:
            lines.append(f"  outside text: {len(page['outside'])}")
            for item in page["outside"]:
                lines.append(f"    {item['id']}  box {_box(item['box'])}  {json.dumps(item['text'], ensure_ascii=False)}")
        else:
            lines.append("  outside text: none")
        if page["clipped"]:
            lines.append(f"  clipped runs: {len(page['clipped'])}")
            for item in page["clipped"]:
                lines.append(f"    box {_box(item['box'])}  {json.dumps(item['text'], ensure_ascii=False)}")
        else:
            lines.append("  clipped runs: none")
    return "\n".join(lines) + "\n"


_CONTRACT = """\
Print a stored Document Rendition as the reader sees it: pages, tables,
cells with their semantics-tier IDs, text outside every table, clipped runs.
The read runs in a PDFium-isolated child process (corridor_pdf_reader.execution).
Address the rendition by content digest through the storage interface, or by path:
  make pdf-reader-inspect ARGS="--sha256 <sha256> --pages 1 2"
  make pdf-reader-inspect ARGS="--file corpus/files/<sha256>.pdf --json"
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sha256", help="digest of a stored Document Rendition")
    source.add_argument("--file", type=Path, help="a PDF on disk, for a document that is not stored")
    parser.add_argument("--pages", type=int, nargs="+", help="1-based page numbers; every page by default")
    parser.add_argument("--engine", default=MEASURED_ENGINE, choices=["tagged", "pdfium"])
    parser.add_argument("--dpi", type=int, default=MEASURED_DPI)
    parser.add_argument("--wall-seconds", type=float, default=ExecutionLimits().wall_seconds)
    parser.add_argument("--json", action="store_true", help="print the projection as JSON")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="corridor-pdf-reader-") as scratch:
        if args.sha256:
            path = _stage_from_store(args.sha256, Path(scratch))
            if path is None:
                print(f"no stored Document Rendition has sha256 {args.sha256}", file=sys.stderr)
                return 2
            sha256 = args.sha256
        else:
            path = args.file
            if not path.is_file():
                print(f"{path} is not a file", file=sys.stderr)
                return 2
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        executor = PdfiumExecutor(ExecutionLimits(wall_seconds=args.wall_seconds))
        try:
            document = executor.read_document(path, args.pages, engine=args.engine, dpi=args.dpi)
        except PdfiumExecutionError as exc:
            print(f"the reader could not read {sha256}: {exc}", file=sys.stderr)
            return 1
    reading = projection(document, sha256)
    if args.json:
        print(json.dumps(reading, indent=1, ensure_ascii=False))
    else:
        sys.stdout.write(render_text(reading))
    return 0


if __name__ == "__main__":
    sys.exit(main())
