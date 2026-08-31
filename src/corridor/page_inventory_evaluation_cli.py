"""Write the Stage 1 page-routing comparison receipt.

The old path left OCR behavior implicit in ingest tests. This command consumes
explicit gold and observation artifacts, refuses partial membership, and emits
one deterministic JSON record. It does not open source documents or rerun OCR,
so recording a measurement cannot accidentally spend a holdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corridor.page_inventory_evaluation import (
    RoutingGoldSet,
    RoutingRun,
    evaluate_stage1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corridor-page-inventory-eval")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    gold = RoutingGoldSet.model_validate_json(arguments.gold.read_text())
    run = RoutingRun.model_validate_json(arguments.run.read_text())
    report = evaluate_stage1(gold, run)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
