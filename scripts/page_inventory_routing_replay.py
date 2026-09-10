"""Replay the Stage 1 routing corpus under the reader-backed Page Inventory.

Why this exists. #440 froze five independently checked pages — three native
matrix pages, one image-only agreement page and one mixed native/scanned
agreement page — with the OCR-needed label each was given before any router
was measured against it, and recorded what the inventory router decided about
them (`gold/pdf/v1/stage1-routing-run.json`). ADR-0094 replaces the engine
those facts came from. This command decides the same five pages again from the
replacement reader's facts and writes the comparison, so "the routing did not
change" is a receipt rather than a recollection.

What it records, per page: the frozen OCR-needed label, the incumbent
router's decision from the frozen run, the reader-backed router's decision
observed now, and both native text lengths. Then the two rates the ticket
asks for, from the same evaluator the frozen receipt used
(`corridor.page_inventory_evaluation`): the false "OCR not needed" rate — how
often a page that needs OCR was routed native — and the unnecessary-OCR rate.

A difference is recorded as a difference. This command explains one where it
can, in the fields it writes; it never reclassifies one into a match because
the explanation is good, and it applies no threshold of its own.

The FDOT page is the spent holdout family (ADR-0008). Reading it requires an
actor and a reason and appends the access, through the one holdout ledger, to
`gold/pdf/v1/holdout-access.jsonl`,
exactly as `make pdf-eval` does; without them the command refuses the run
rather than quietly measuring four pages.

    make page-inventory-routing-replay ARGS="--output-dir artifacts/... \\
        --holdout-actor <actor> --holdout-reason <reason>"
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

from corridor import holdout_ledger
from corridor.page_inventory import (
    READER_COORDINATE_FRAME,
    READER_ROUTER_VERSION,
    TEXTRACT_ENGINE,
    read_reader_page_inventories,
    route_reader_page,
)
from corridor.page_inventory_evaluation import (
    RoutingGoldSet,
    RoutingRun,
    evaluate_stage1,
)
from corridor.pdf_evaluation import Split, load_gold_set
from corridor.storage import staged_file
from corridor_pdf_reader import provenance
from corridor_pdf_reader.execution import (
    MEASURED_DPI,
    MEASURED_ENGINE,
    PdfiumExecutor,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_ROOT = REPO_ROOT / "gold" / "pdf" / "v1"
DEFAULT_GOLD = GOLD_ROOT / "stage1-routing-gold.json"
DEFAULT_DATASET = GOLD_ROOT / "dataset.json"
DEFAULT_INCUMBENT_RUN = GOLD_ROOT / "stage1-routing-run.json"
DEFAULT_LEDGER = GOLD_ROOT / "holdout-access.jsonl"


def _named(path: Path) -> str:
    """A path as the repository names it, when it is inside the repository."""

    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def holdout_digests(dataset: Path, wanted: set[str]) -> list[str]:
    """The wanted documents that belong to the spent holdout family."""

    return sorted(
        document.document_sha256
        for document in load_gold_set(dataset).documents
        if document.split == Split.holdout and document.document_sha256 in wanted
    )


def record_holdout_access(
    ledger: Path, dataset_version: str, digests: list[str], actor: str, reason: str,
    *, run: str = "page-inventory-routing-replay", receipt: str | None = None,
) -> dict:
    """Append this access through the one holdout ledger (ADR-0008)."""
    return holdout_ledger.append(
        {
            "run": run,
            "accessed_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "purpose": (
                "replay the frozen Stage 1 routing pages from the reader-backed Page "
                "Inventory and compare them with the labels and the incumbent run (#734)"
            ),
            "dataset": {"dataset_version": dataset_version},
            "holdout": {"document_sha256s": digests, "documents": len(digests)},
            "configuration": page_inventory_identity(),
            # Recorded before the pages are read, so the receipt named here is
            # where the result of this access has to be read from.
            "result": {"status": "recorded_before_replay", "receipt": receipt},
        },
        ledger=ledger,
    )


def page_inventory_identity() -> dict:
    """What decided a page's route, independent of who is asking."""
    return {
        "router_version": READER_ROUTER_VERSION,
        "coordinate_frame": READER_COORDINATE_FRAME,
        "ocr_engine_named_by_the_route": TEXTRACT_ENGINE,
        "reader_engine": MEASURED_ENGINE,
        "dpi": MEASURED_DPI,
        "source_commit": provenance.SOURCE_COMMIT,
        "package_digest": provenance.package_digest(),
    }


def configuration_identity(executor: PdfiumExecutor) -> dict:
    return {
        "page_inventory": page_inventory_identity(),
        "execution_contract": executor.contract(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "corridor_commit": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--incumbent-run", type=Path, default=DEFAULT_INCUMBENT_RUN)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--holdout-access-log", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--holdout-actor")
    parser.add_argument("--holdout-reason")
    arguments = parser.parse_args(argv)

    gold = RoutingGoldSet.model_validate_json(arguments.gold.read_text())
    incumbent = RoutingRun.model_validate_json(arguments.incumbent_run.read_text())
    incumbent_by_page = {
        (observation.document_sha256, observation.page_number): observation
        for observation in incumbent.observations
    }
    wanted: dict[str, list[int]] = {}
    for case in gold.cases:
        wanted.setdefault(case.document_sha256, []).append(case.page_number)

    holdout = holdout_digests(arguments.dataset, set(wanted))
    ledger_entry = None
    if holdout:
        if not (arguments.holdout_actor and arguments.holdout_reason):
            print(
                "this gold set includes the spent holdout family; rerun with "
                "--holdout-actor and --holdout-reason, which are appended to "
                f"{arguments.holdout_access_log}",
                file=sys.stderr,
            )
            return 2
        ledger_entry = record_holdout_access(
            arguments.holdout_access_log,
            gold.dataset_version,
            holdout,
            arguments.holdout_actor,
            arguments.holdout_reason,
            run=arguments.output_dir.name,
            receipt=_named(arguments.output_dir / "stage1-routing-replay.json"),
        )

    started = time.perf_counter()
    executor = PdfiumExecutor()
    observations: list[dict] = []
    comparisons: list[dict] = []
    for digest in sorted(wanted):
        source = staged_file(digest)
        if source is None:
            raise SystemExit(f"{digest}: not in the content store")
        if sha256(source.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{digest}: staged bytes do not hash to the gold digest")
        pages = sorted(wanted[digest])
        inventories = read_reader_page_inventories(source, pages, executor=executor)
        for page_number in pages:
            inventory = inventories.get(page_number)
            if inventory is None:
                raise SystemExit(f"{digest} p.{page_number}: the reader returned no page")
            decision = route_reader_page(inventory)
            observations.append(
                {
                    "document_sha256": digest,
                    "page_number": page_number,
                    "page_mode": decision.page_mode,
                    "native_text_length": inventory.native_text_length,
                }
            )
            case = next(
                item
                for item in gold.cases
                if item.document_sha256 == digest and item.page_number == page_number
            )
            before = incumbent_by_page[(digest, page_number)]
            comparisons.append(
                {
                    "document_sha256": digest,
                    "page_number": page_number,
                    "page_class": case.page_class,
                    "holdout": digest in holdout,
                    "expected_ocr_needed": case.expected_ocr_needed,
                    "incumbent_page_mode": before.page_mode,
                    "reader_page_mode": decision.page_mode,
                    "page_mode_changed": before.page_mode != decision.page_mode,
                    "incumbent_native_text_length": before.native_text_length,
                    "reader_native_text_length": inventory.native_text_length,
                    "reader_reason": decision.reason,
                    "reader_regions": [
                        {"region_id": region.region_id, "mode": region.mode, "reason": region.reason}
                        for region in decision.regions
                    ],
                    "reader_structural_triggers": [
                        {
                            "region_id": trigger.region_id,
                            "trigger": trigger.trigger,
                            "engine": trigger.engine,
                        }
                        for trigger in decision.structural_triggers
                    ],
                    "reader_inventory": {
                        "native_glyph_count": inventory.native_glyph_count,
                        "native_glyph_coverage": inventory.native_glyph_coverage,
                        "embedded_image_coverage": inventory.embedded_image_coverage,
                        "vector_density": inventory.vector_density,
                        "unicode_quality": inventory.unicode_quality,
                        "suspicious_text_signals": list(
                            inventory.suspicious_text_signals
                        ),
                        "rotation_degrees": inventory.rotation_degrees,
                        "image_regions": len(inventory.image_regions),
                        "table_regions": [
                            {
                                "region_id": region.region_id,
                                "row_count": region.row_count,
                                "column_count": region.column_count,
                                "cells_with_text": region.cells_with_text,
                            }
                            for region in inventory.table_regions
                        ],
                    },
                }
            )

    run = RoutingRun(
        schema_version="corridor.pdf-stage1-run.v1",
        router_version=READER_ROUTER_VERSION,
        observations=tuple(observations),
    )
    evaluation = evaluate_stage1(gold, run)
    receipt = {
        "schema_version": "corridor.pdf-stage1-routing-replay.v1",
        "gold": _named(arguments.gold),
        "incumbent_run": _named(arguments.incumbent_run),
        "configuration": configuration_identity(executor),
        "holdout_access": ledger_entry,
        "cases": len(comparisons),
        "pages_whose_page_mode_changed": sum(
            comparison["page_mode_changed"] for comparison in comparisons
        ),
        "false_ocr_not_needed_rate": evaluation.inventory_router.false_ocr_not_needed_rate,
        "unnecessary_ocr_rate": evaluation.inventory_router.unnecessary_ocr_rate,
        "confusion": evaluation.inventory_router.confusion.model_dump(mode="json"),
        "pages": comparisons,
        "wall_seconds": None,
    }
    receipt["wall_seconds"] = round(time.perf_counter() - started, 1)

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in (
        ("stage1-routing-run.json", run.model_dump(mode="json")),
        ("stage1-routing-evaluation.json", evaluation.model_dump(mode="json")),
        ("stage1-routing-replay.json", receipt),
    ):
        path = arguments.output_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written.append(path)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
