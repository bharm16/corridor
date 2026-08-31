"""Render stable receipts from an already-computed PDF evaluation.

The evaluator originally formatted JSON and Markdown beside contract validation
and assignment matching.  That made presentation changes touch the measurement
module.  This formatter accepts only the frozen result model, so it cannot
recalculate a score or change which experiment passed.
"""

from __future__ import annotations

import json

from corridor.pdf_evaluation import AggregateMetrics, EvaluationReport


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
