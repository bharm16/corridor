"""Run the frozen PDF experiment and publish comparable local receipts.

Pytest protects the contract; this CLI is the experiment runner.  It reads an
engine-produced JSON artifact and never invokes an engine itself, which keeps
holdout access explicit and leaves challenger dependencies outside Corridor's
application environment.

The discarded shape was a pytest target that both ran and judged an engine.
That hid experiment inputs in test fixtures and could not record intentional
holdout access.  This command consumes an immutable engine-run artifact instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from corridor.pdf_evaluation import (
    Split,
    evaluate,
    load_engine_run,
    load_gold_set,
)
from corridor.pdf_evaluation_report import report_json, report_markdown


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corridor-pdf-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("evaluate")
    command.add_argument("--gold", type=Path, required=True)
    command.add_argument("--predictions", type=Path, required=True)
    command.add_argument("--output-json", type=Path, required=True)
    command.add_argument("--output-report", type=Path, required=True)
    command.add_argument("--holdout-access-log", type=Path)
    command.add_argument("--holdout-actor")
    command.add_argument("--holdout-reason")
    return parser


def _record_holdout_access(arguments, gold, digests: list[str]) -> bool:
    required = (
        arguments.holdout_access_log,
        arguments.holdout_actor,
        arguments.holdout_reason,
    )
    if not all(required):
        print(
            "holdout evaluation requires --holdout-access-log, "
            "--holdout-actor, and --holdout-reason",
            file=sys.stderr,
        )
        return False
    arguments.holdout_access_log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema_version": "corridor.pdf-holdout-access.v1",
        "accessed_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": gold.dataset_version,
        "document_sha256s": sorted(digests),
        "actor": arguments.holdout_actor,
        "reason": arguments.holdout_reason,
    }
    with arguments.holdout_access_log.open("a") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    gold = load_gold_set(arguments.gold)
    run = load_engine_run(arguments.predictions)
    selected = {document.document_sha256 for document in run.documents}
    holdout = [
        document.document_sha256
        for document in gold.documents
        if document.split == Split.holdout and document.document_sha256 in selected
    ]
    if holdout and not _record_holdout_access(arguments, gold, holdout):
        return 2
    report = evaluate(gold, run)
    arguments.output_json.parent.mkdir(parents=True, exist_ok=True)
    arguments.output_report.parent.mkdir(parents=True, exist_ok=True)
    arguments.output_json.write_text(report_json(report))
    arguments.output_report.write_text(report_markdown(report))
    print(arguments.output_json)
    print(arguments.output_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
