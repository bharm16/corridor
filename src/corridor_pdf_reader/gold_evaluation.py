"""The frozen reader through the existing PDF evaluation contract, gold/pdf/v1 (#731).

`corridor.pdf_evaluation` is Stage 0's frozen contract: independently checked
labels in thousandths of a PDF point, layered so that geometry, text and
semantics score apart, and a CLI that refuses the holdout family without a
ledger entry. The reader package must not be reached from `src/corridor`, so
the adapter lives here and imports the contract, not the other way round.

The adapter turns what the reader reads into an `EngineRun` and invents
nothing the reader does not produce:

- Geometry is the reader's own: the table box, and each cell's text box (the
  union of its glyph boxes; the reader does not draw cell rectangles). Rows
  and columns are the bounding boxes of the cells that share a row or column
  index. Boxes are expressed in the frame the gold page declares
  (`width_points`, `height_points`), scaled from the displayed crop box when
  the two differ, and every scale is written into `adapter.json`.
- What the reader has no tier for is marked absent rather than guessed: the
  page class is the sentinel `unclassified`, no page-scoped value and no
  Extracted Proposal is emitted, no cell names a header cell or a canonical
  mapping, and a cell's state is the contract's default. Rows must carry a
  disposition for the contract to accept the cells, and the reader has none
  (#737), so every row carries the placeholder `active`; the layer is
  reported as not applicable, never as a reader result.
- Latency and peak memory are measured in the isolated reader process, one
  per document, under the PDFium execution contract.

`THRESHOLD_APPLICABILITY` says, before any run, which of the contract's
ceilings a frozen-reader run can inform; a met threshold on a layer the
reader does not produce is vacuous and is labelled so in `thresholds.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from corridor.pdf_evaluation import (
    CellPrediction,
    DocumentGold,
    DocumentPrediction,
    EngineRun,
    GoldSet,
    PageGold,
    PagePrediction,
    Point,
    Polygon,
    Split,
    TablePrediction,
    load_gold_set,
)
from corridor_pdf_reader import provenance, registry
from corridor_pdf_reader.execution import (
    MEASURED_DPI,
    MEASURED_ENGINE,
    ExecutionLimits,
    PdfiumExecutionError,
    PdfiumExecutor,
    read_document,
)
from corridor_pdf_reader.replacement.pages import cell_id, slim_page

ADAPTER_VERSION = "corridor.pdf-pairs.gold-adapter.v1"
UNCLASSIFIED = "unclassified"
ROW_DISPOSITION_PLACEHOLDER: Literal["active"] = "active"
DEFAULT_GOLD = registry.REPO_ROOT / "gold" / "pdf" / "v1" / "dataset.json"
DEFAULT_LEDGER = registry.REPO_ROOT / "gold" / "pdf" / "v1" / "holdout-access.jsonl"

# Which ceilings a frozen-reader run can inform, decided from what the reader
# produces and written before the run; nothing here depends on a result.
THRESHOLD_APPLICABILITY: dict[str, tuple[bool, str]] = {
    "page_coverage": (True, "the adapter reads every page the gold labels; coverage measures that the reader returned each"),
    "page_class_accuracy": (False, "the frozen reader has no page classifier; every page is `unclassified` (#734 owns inventory and routing)"),
    "table_f1": (True, "the reader reconstructs tables with a box; matched by IoU in the gold page's declared frame"),
    "row_f1": (True, "rows are bounding boxes of the reader's cell text boxes, not ruled bands; a row's IoU with a labelled band is bounded by that"),
    "column_f1": (True, "columns are bounding boxes of the reader's cell text boxes, not ruled bands"),
    "cell_f1": (True, "the reader's cells are text boxes, the gold's are cell rectangles, and the gold labels a sample of each table's cells; every unlabelled reader cell counts as a false positive"),
    "cell_text_exact": (True, "over matched cells only: the reader's text as read against the label; vacuous when no cell matches"),
    "row_disposition_exact": (False, "every row carries the placeholder `active`; the frozen reader has no disposition tier (#737)"),
    "cell_span_exact": (True, "over matched cells only: the reader's row and column spans"),
    "cell_topology_exact": (True, "over matched cells only, and only through matched rows and columns"),
    "header_relationships_exact": (False, "the reader names no header cells; the semantics tier was not run (#737)"),
    "canonical_mapping_exact": (False, "no canonical mapping is emitted; the semantics tier was not run (#737)"),
    "cell_state_exact": (False, "the reader has no state tier; every cell carries the contract's default `confirmed`"),
    "page_scoped_values": (False, "no page-scoped value is emitted; the semantics tier was not run"),
    "wrong_source_cited_proposals": (False, "no Extracted Proposal is emitted, so none can be wrong; met vacuously"),
    "failure_rate": (True, "documents the isolated reader process failed on"),
    "abstention_rate": (True, "the reader never abstains; every labelled page read is a page answered"),
    "latency": (True, "wall time of the isolated reader process per document, spawn included"),
    "memory": (True, "peak resident set of the isolated reader process per document"),
}


def displayed_size(geometry: dict[str, Any]) -> tuple[float, float]:
    """The crop box as displayed: rotation applied."""
    width, height = float(geometry["width"]), float(geometry["height"])
    return (height, width) if int(geometry["rotation"]) % 180 else (width, height)


class Frame:
    """The gold page's declared frame over the reader's displayed frame."""

    def __init__(self, gold_page: PageGold, geometry: dict[str, Any]) -> None:
        self.width, self.height = displayed_size(geometry)
        self.gold_width, self.gold_height = gold_page.width_points, gold_page.height_points
        self.scale_x = self.gold_width / (self.width * 1000.0)
        self.scale_y = self.gold_height / (self.height * 1000.0)
        self.clamped = 0
        self.degenerate = 0

    @property
    def scaled(self) -> bool:
        return round(self.width * 1000) != self.gold_width or round(self.height * 1000) != self.gold_height

    def polygon(self, box: Sequence[float]) -> Polygon | None:
        x0 = round(box[0] * 1000 * self.scale_x)
        y0 = round(box[1] * 1000 * self.scale_y)
        x1 = round(box[2] * 1000 * self.scale_x)
        y1 = round(box[3] * 1000 * self.scale_y)
        clamped = (
            min(max(x0, 0), self.gold_width),
            min(max(y0, 0), self.gold_height),
            min(max(x1, 0), self.gold_width),
            min(max(y1, 0), self.gold_height),
        )
        if clamped != (x0, y0, x1, y1):
            self.clamped += 1
        x0, y0, x1, y1 = clamped
        if x1 <= x0 or y1 <= y0:
            self.degenerate += 1
            return None
        return Polygon(points=(Point(x=x0, y=y0), Point(x=x1, y=y0), Point(x=x1, y=y1), Point(x=x0, y=y1)))

    def describe(self, gold_page: PageGold, geometry: dict[str, Any]) -> dict[str, Any]:
        return {
            "page_number": gold_page.page_number,
            "reader_crop_box": geometry["crop_box"],
            "reader_rotation": geometry["rotation"],
            "reader_displayed_points": [self.width, self.height],
            "gold_declared_points": [self.gold_width / 1000.0, self.gold_height / 1000.0],
            "gold_rotation": gold_page.rotation_degrees,
            "scaled": self.scaled,
            "scale": [round(self.scale_x, 6), round(self.scale_y, 6)],
            "boxes_clamped_to_page": self.clamped,
            "cells_dropped_as_degenerate": self.degenerate,
        }


def _union(boxes: list[Sequence[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def page_prediction(slim: dict[str, Any], geometry: dict[str, Any], gold_page: PageGold) -> tuple[PagePrediction, dict[str, Any]]:
    """One slim reader page as the contract's page, with the frame it was expressed in."""
    frame = Frame(gold_page, geometry)
    tables: list[TablePrediction] = []
    emitted_cells = 0
    for index, table in enumerate(slim["tables"]):
        cells_by_row: dict[int, list[Sequence[float]]] = {}
        cells_by_column: dict[int, list[Sequence[float]]] = {}
        cells: list[CellPrediction] = []
        for cell in table["cells"]:
            polygon = frame.polygon(cell["box"])
            if polygon is None:
                continue
            cells_by_row.setdefault(cell["row"], []).append(cell["box"])
            cells_by_column.setdefault(cell["column"], []).append(cell["box"])
            cells.append(
                CellPrediction(
                    cell_id=cell_id(index, cell["row"], cell["column"]),
                    row_id=f"t{index}r{cell['row']}",
                    column_id=f"t{index}c{cell['column']}",
                    polygon=polygon,
                    visible_text=cell["text"],
                    row_span=cell["row_span"],
                    column_span=cell["column_span"],
                )
            )
        table_polygon = frame.polygon(table["box"])
        if table_polygon is None or not cells:
            continue
        row_ids: list[str] = []
        row_polygons: list[Polygon] = []
        for row in sorted(cells_by_row):
            polygon = frame.polygon(_union(cells_by_row[row]))
            if polygon is not None:
                row_ids.append(f"t{index}r{row}")
                row_polygons.append(polygon)
        column_ids: list[str] = []
        column_polygons: list[Polygon] = []
        for column in sorted(cells_by_column):
            polygon = frame.polygon(_union(cells_by_column[column]))
            if polygon is not None:
                column_ids.append(f"t{index}c{column}")
                column_polygons.append(polygon)
        kept = [cell for cell in cells if cell.row_id in row_ids and cell.column_id in column_ids]
        emitted_cells += len(kept)
        tables.append(
            TablePrediction(
                table_id=f"t{index}",
                polygon=table_polygon,
                row_ids=tuple(row_ids),
                row_polygons=tuple(row_polygons),
                column_ids=tuple(column_ids),
                column_polygons=tuple(column_polygons),
                row_dispositions={row_id: ROW_DISPOSITION_PLACEHOLDER for row_id in row_ids},
                cells=tuple(kept),
            )
        )
    prediction = PagePrediction(
        page_number=gold_page.page_number,
        page_class=UNCLASSIFIED,
        abstained=False,
        page_scoped_values={},
        tables=tuple(tables),
        proposals=(),
    )
    notes = frame.describe(gold_page, geometry)
    notes.update(tables=len(tables), cells=emitted_cells, outside_strings=len(slim["outside"]), clipped_runs=len(slim["clipped"]))
    return prediction, notes


def document_prediction(
    gold: DocumentGold,
    reading: dict[str, Any] | None,
    *,
    latency_ms: int,
    peak_memory_bytes: int,
    failure: str | None = None,
) -> tuple[DocumentPrediction, list[dict[str, Any]]]:
    """The reader's pages of one gold document as the contract's document."""
    if reading is None:
        return (
            DocumentPrediction(
                document_sha256=gold.document_sha256,
                latency_ms=latency_ms,
                peak_memory_bytes=peak_memory_bytes,
                failed=True,
                failure_reason=failure or "the reader returned nothing",
            ),
            [],
        )
    by_number = {page["number"]: page for page in reading["pages"]}
    pages: list[PagePrediction] = []
    notes: list[dict[str, Any]] = []
    for gold_page in gold.pages:
        page = by_number.get(gold_page.page_number)
        if page is None:
            continue
        prediction, note = page_prediction(page["slim"], page["geometry"], gold_page)
        pages.append(prediction)
        notes.append(note)
    return (
        DocumentPrediction(
            document_sha256=gold.document_sha256,
            latency_ms=latency_ms,
            peak_memory_bytes=peak_memory_bytes,
            pages=tuple(pages),
        ),
        notes,
    )


def configuration_identity(engine: str, dpi: int) -> dict[str, Any]:
    import pypdf
    import pypdfium2

    return {
        "adapter": ADAPTER_VERSION,
        "configuration": "frozen-reader",
        "engine": engine,
        "dpi": dpi,
        "reader": {
            "commit": provenance.SOURCE_COMMIT,
            "package_digest": provenance.package_digest(),
            "matches_commit": provenance.verify() == [],
        },
        "pypdfium2": str(pypdfium2.PYPDFIUM_INFO),
        "pdfium": str(pypdfium2.PDFIUM_INFO),
        "pypdf": pypdf.__version__,
        "absent": {
            "page_class": UNCLASSIFIED,
            "row_dispositions": f"placeholder `{ROW_DISPOSITION_PLACEHOLDER}` on every row; not a reader output",
            "page_scoped_values": "none",
            "proposals": "none",
            "header_cell_ids": "none",
            "canonical_mapping": "none",
            "cell_state": "the contract's default, `confirmed`; not a reader output",
        },
    }


def configuration_sha256(identity: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def engine_run(gold: GoldSet, documents: Sequence[DocumentPrediction], identity: dict[str, Any]) -> EngineRun:
    return EngineRun(
        schema_version="corridor.pdf-engine-run.v1",
        engine="corridor_pdf_reader frozen-reader",
        engine_version=f"commit {identity['reader']['commit'][:7]}, pypdfium2 {identity['pypdfium2']} (PDFium {identity['pdfium']}), pypdf {identity['pypdf']}, engine {identity['engine']} at {identity['dpi']} dpi",
        configuration_sha256=configuration_sha256(identity),
        documents=tuple(documents),
    )


def _read_with_usage(path: Path, pages: list[int], engine: str, dpi: int) -> dict[str, Any]:
    """Runs in the isolated reader process: the slim pages, the read's wall time and the process's peak RSS."""
    started = time.perf_counter()
    document = read_document(path, pages, engine=engine, dpi=dpi, retain_images=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = int(maxrss) * (1024 if sys.platform.startswith("linux") else 1)
    return {
        "source_sha256": document["source_sha256"],
        "page_count": document["page_count"],
        "pages": [{"number": page["number"], "geometry": page["geometry"], "slim": slim_page(page)} for page in document["pages"]],
        "read_ms": elapsed_ms,
        "peak_rss_bytes": peak,
    }


def _stage_from_store(sha256: str, scratch: Path) -> Path | None:
    """The stored bytes under their digest, verified by the storage interface; the one doorway into the store."""
    from corridor.object_storage import content_store

    store = content_store()
    key = store.resolve(sha256)
    if key is None:
        return None
    return store.stage(key, scratch / f"{sha256}.pdf", sha256=sha256)


def read_gold_documents(
    gold: GoldSet,
    documents: Sequence[DocumentGold],
    *,
    engine: str,
    dpi: int,
    wall_seconds: float,
) -> tuple[list[DocumentPrediction], list[dict[str, Any]]]:
    """Every selected gold document through the isolated reader, one process each."""
    executor = PdfiumExecutor(ExecutionLimits(wall_seconds=wall_seconds))
    predictions: list[DocumentPrediction] = []
    notes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="corridor-pdf-pairs-gold-") as scratch:
        for document in documents:
            note: dict[str, Any] = {"document_sha256": document.document_sha256, "family": document.document_family, "split": document.split.value}
            path = _stage_from_store(document.document_sha256, Path(scratch))
            if path is None:
                prediction, _ = document_prediction(document, None, latency_ms=0, peak_memory_bytes=0, failure="no stored Document Rendition has this sha256")
                note["failure"] = prediction.failure_reason
                predictions.append(prediction)
                notes.append(note)
                continue
            started = time.perf_counter()
            try:
                reading = executor.run(_read_with_usage, path, [page.page_number for page in document.pages], engine, dpi)
            except PdfiumExecutionError as exc:
                latency = round((time.perf_counter() - started) * 1000)
                prediction, _ = document_prediction(document, None, latency_ms=latency, peak_memory_bytes=0, failure=f"{exc.kind}: {exc.detail}")
                note.update(failure=prediction.failure_reason, latency_ms=latency)
                predictions.append(prediction)
                notes.append(note)
                continue
            latency = round((time.perf_counter() - started) * 1000)
            if reading["source_sha256"] != document.document_sha256:
                raise RuntimeError(f"the reader read other bytes than {document.document_sha256}")
            prediction, page_notes = document_prediction(document, reading, latency_ms=latency, peak_memory_bytes=reading["peak_rss_bytes"])
            note.update(page_count=reading["page_count"], pages_read=[page.page_number for page in document.pages], latency_ms=latency, read_ms=reading["read_ms"], peak_rss_bytes=reading["peak_rss_bytes"], pages=page_notes)
            predictions.append(prediction)
            notes.append(note)
    return predictions, notes


def thresholds_record(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Each ceiling's outcome beside whether a frozen-reader run can inform it."""
    met: dict[str, bool] = evaluation["thresholds_met"]
    unknown = sorted(set(met) - set(THRESHOLD_APPLICABILITY))
    if unknown:
        raise ValueError(f"the evaluation names ceilings the adapter has not classified: {unknown}")
    return {
        "passed_as_the_contract_scores_it": evaluation["passed"],
        "thresholds": {
            name: {"met": met[name], "applies": THRESHOLD_APPLICABILITY[name][0], "why": THRESHOLD_APPLICABILITY[name][1]}
            for name in met
        },
        "informative_thresholds_met": all(met[name] for name in met if THRESHOLD_APPLICABILITY[name][0]),
        "note": (
            "`applies` is fixed before any run from what the frozen reader produces; a met threshold on a layer it "
            "does not produce is vacuous and says nothing about the reader"
        ),
    }


RETAINED_FILES = ("engine-run.json", "adapter.json", "evaluation.json", "evaluation.md", "thresholds.json")


def retain(output: Path, registry_root: Path) -> Path:
    """Copy the run's files beside the registry, under pdf-v1/<run>/; never over an existing run."""
    target = registry_root / "pdf-v1" / output.name
    if target.exists():
        raise RuntimeError(f"{target} already holds a run; choose another output name")
    target.mkdir(parents=True)
    for name in RETAINED_FILES:
        shutil.copyfile(output / name, target / name)
    return target


_CONTRACT = """\
The frozen reader through the existing PDF evaluation contract (#731): read
the gold/pdf/v1 documents from the content store, write an engine run in the
contract's shape, and evaluate it with `corridor.pdf_evaluation_cli`. The
holdout family is refused without the ledger flags, as for `make pdf-eval`:
  make pdf-reader-gold-eval ARGS="--output out/pdf-reader/gold-v1 --include-holdout --holdout-actor <actor> --holdout-reason <reason>"
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, required=True, help="a new directory outside git")
    parser.add_argument("--engine", default=MEASURED_ENGINE, choices=["tagged", "pdfium"])
    parser.add_argument("--dpi", type=int, default=MEASURED_DPI)
    parser.add_argument("--wall-seconds", type=float, default=ExecutionLimits().wall_seconds)
    parser.add_argument("--include-holdout", action="store_true", help="read the holdout family too; the evaluation CLI then records the access")
    parser.add_argument("--holdout-access-log", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--holdout-actor")
    parser.add_argument("--holdout-reason")
    parser.add_argument("--retain", action="store_true", help="copy the run's five files into the registry's pdf-v1/<run>/ directory")
    parser.add_argument("--registry", type=Path, default=registry.REGISTRY_ROOT)
    args = parser.parse_args(argv)
    if args.include_holdout and not (args.holdout_actor and args.holdout_reason):
        print("reading the holdout family requires --include-holdout, --holdout-actor and --holdout-reason (ADR-0008)", file=sys.stderr)
        return 2
    gold = load_gold_set(args.gold)
    selected = [document for document in gold.documents if args.include_holdout or document.split != Split.holdout]
    if not selected:
        print("no document selected", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=False)
    identity = configuration_identity(args.engine, args.dpi)
    predictions, notes = read_gold_documents(gold, selected, engine=args.engine, dpi=args.dpi, wall_seconds=args.wall_seconds)
    run = engine_run(gold, predictions, identity)
    predictions_path = args.output / "engine-run.json"
    predictions_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (args.output / "adapter.json").write_text(
        json.dumps(
            {
                "adapter": ADAPTER_VERSION,
                "gold": {"file": registry.relative(args.gold), "dataset_version": gold.dataset_version, "sha256": registry.sha256_file(args.gold)},
                "configuration": identity,
                "configuration_sha256": run.configuration_sha256,
                "documents_selected": [document.document_sha256 for document in selected],
                "holdout_included": args.include_holdout,
                "documents": notes,
                "limits": registry.LIMITS,
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    from corridor.pdf_evaluation_cli import main as evaluate_main

    command = [
        "evaluate",
        "--gold", str(args.gold),
        "--predictions", str(predictions_path),
        "--output-json", str(args.output / "evaluation.json"),
        "--output-report", str(args.output / "evaluation.md"),
    ]
    if args.include_holdout:
        command += ["--holdout-access-log", str(args.holdout_access_log), "--holdout-actor", args.holdout_actor, "--holdout-reason", args.holdout_reason]
    status = evaluate_main(command)
    if status != 0:
        return status
    evaluation = json.loads((args.output / "evaluation.json").read_text(encoding="utf-8"))
    record = thresholds_record(evaluation)
    (args.output / "thresholds.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({name: item["met"] for name, item in record["thresholds"].items()}, indent=1))
    print(f"passed as the contract scores it: {record['passed_as_the_contract_scores_it']}; informative thresholds met: {record['informative_thresholds_met']}")
    if args.retain:
        target = retain(args.output, args.registry)
        print(f"retained in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
