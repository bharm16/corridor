"""Run the frozen PDF experiment and publish comparable local receipts.

Pytest protects the contract; this CLI is the experiment runner.  It reads an
engine-produced JSON artifact and never invokes an engine itself, which keeps
holdout access explicit and leaves challenger dependencies outside Corridor's
application environment.

The discarded shape was a pytest target that both ran and judged an engine.
That hid experiment inputs in test fixtures and could not record intentional
holdout access.  This command consumes an immutable engine-run artifact instead.

Rendering the receipt lives here too. It was a module of its own with this one
caller, a pass-through that could only ever be reached by publishing a run, and
a separate module implied a second consumer that never appeared. It still
accepts nothing but the frozen result model, so publishing cannot recalculate a
score or change which experiment passed. Recording the holdout access belongs to
`corridor.holdout_ledger`, the one ledger both spent datasets are written to
(ADR-0008).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from corridor import holdout_ledger
from corridor.pdf_evaluation import (
    AggregateMetrics,
    EvaluationReport,
    Split,
    evaluate,
    load_engine_run,
    load_gold_set,
)


_METRIC_HEADERS = (
    "Pages",
    "Coverage F1",
    "Class accuracy",
    "Table F1",
    "Row F1",
    "Column F1",
    "Cell F1",
    "Exact text",
    "Row disposition",
    "Cell spans",
    "Cell topology",
    "Header relationships",
    "Canonical mapping",
    "Cell state",
    "Page-scoped values F1",
    "Abstention",
    "Failure",
    "Latency ms",
    "Peak memory bytes",
)


def report_json(report: EvaluationReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _metric_values(metrics: AggregateMetrics) -> tuple[str, ...]:
    return (
        str(metrics.pages),
        f"{metrics.page_coverage.f1:.3f}",
        f"{metrics.page_classification.accuracy:.3f}",
        f"{metrics.tables.f1:.3f}",
        f"{metrics.rows.f1:.3f}",
        f"{metrics.columns.f1:.3f}",
        f"{metrics.cells.f1:.3f}",
        f"{metrics.cell_text_exact.accuracy:.3f}",
        f"{metrics.row_disposition_exact.accuracy:.3f}",
        f"{metrics.cell_span_exact.accuracy:.3f}",
        f"{metrics.cell_topology_exact.accuracy:.3f}",
        f"{metrics.header_relationships_exact.accuracy:.3f}",
        f"{metrics.canonical_mapping_exact.accuracy:.3f}",
        f"{metrics.cell_state_exact.accuracy:.3f}",
        f"{metrics.page_scoped_values.f1:.3f}",
        f"{metrics.abstention_rate:.3f}",
        f"{metrics.failure_rate:.3f}",
        str(metrics.latency_ms),
        str(metrics.peak_memory_bytes),
    )


def _table_header(first_column: str) -> list[str]:
    return [
        "| " + " | ".join((first_column, *_METRIC_HEADERS)) + " |",
        "|---|" + "---:|" * len(_METRIC_HEADERS),
    ]


def _table_row(name: str, metrics: AggregateMetrics) -> str:
    return "| " + " | ".join((name, *_metric_values(metrics))) + " |"


def report_markdown(report: EvaluationReport) -> str:
    """Render every frozen layer per page class and per document."""

    lines = [
        f"# PDF extraction evaluation — {report.engine} {report.engine_version}",
        "",
        f"Dataset: `{report.dataset_version}`",
        f"Configuration: `{report.configuration_sha256}`",
        f"Result: **{'PASS' if report.passed else 'FAIL'}**",
        "",
        "## Source-citation safety",
        "",
        (
            f"Wrong source-cited Extracted Proposals avoided: "
            f"{report.key_metric.avoided}/{report.key_metric.opportunities}; "
            f"wrong proposals emitted: {report.key_metric.emitted}."
        ),
        "",
        "## Per page class",
        "",
        *_table_header("Page class"),
    ]
    for name, metrics in report.by_page_class.items():
        lines.append(_table_row(name, metrics))
    lines.extend(["", "## Per document", "", *_table_header("SHA-256")])
    for digest, metrics in report.by_document.items():
        lines.append(_table_row(f"`{digest}`", metrics))
    lines.extend(["", "## Acceptance ceilings", ""])
    for name, met in report.thresholds_met.items():
        lines.append(f"- [{'x' if met else ' '}] {name}")
    return "\n".join(lines) + "\n"


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


def _record_holdout_access(arguments, gold, run, digests: list[str]) -> bool:
    """Append this access in the one ledger schema, before anything is scored."""
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
    holdout_ledger.append(
        {
            "run": arguments.output_json.stem,
            "accessed_at": datetime.now(timezone.utc).isoformat(),
            "actor": arguments.holdout_actor,
            "reason": arguments.holdout_reason,
            "purpose": (
                "score an engine run against the frozen Stage 0 page/cell contract "
                "through corridor.pdf_evaluation_cli"
            ),
            "dataset": {
                "dataset_version": gold.dataset_version,
                "gold": str(arguments.gold),
            },
            "holdout": {
                "document_sha256s": sorted(digests),
                "documents": len(digests),
            },
            "configuration": {
                "engine": run.engine,
                "engine_version": run.engine_version,
                "configuration_sha256": run.configuration_sha256,
                "predictions": str(arguments.predictions),
            },
            # The engine artifact this command reads was produced before it ran,
            # so the access is already spent; the receipt it is about to write is
            # what the result has to be read from.
            "result": {
                "status": "recorded_before_scoring",
                "receipt": str(arguments.output_json),
            },
        },
        ledger=arguments.holdout_access_log,
    )
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
    if holdout and not _record_holdout_access(arguments, gold, run, holdout):
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
