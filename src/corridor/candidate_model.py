"""Measure a candidate model against the current one, repeatably.

A model swap is otherwise a leap of faith: `llm_model` and the prompt version
are sealed on every Extracted Proposal, and changing either makes the numbers
incomparable (the reason Extraction Measurement pins exact run receipts). This
module turns "should we adopt model X" into one measured afternoon.

One command runs a named candidate model over exactly the documents the current
model already read, then scores both readings against the *same* Reference
Dataset and emits one immutable comparison receipt — current versus candidate,
per reference, with each side's model, prompt, sealed configuration, and the
shared reference identity recorded so the comparison reproduces byte for byte.

It is not a second measurement path. Both sides are ordinary Extraction
Measurements (`corridor.eval.measure`); this module only names the two
populations, runs the candidate reader, and pairs the results. ADR-0049
governs the separation: candidate runs are experiments, so they execute only
against an explicit disposable database — the guard refuses production — and
the command never declares an Active Run, so a candidate reading can never
become a Current Production Run or be resumed as production work. The quarterly
cadence and the "switch on a measured win" adoption rule are operating
practice, documented in docs/operations/candidate-model-comparison.md, not code.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.eval import (
    ArtifactCollision,
    Measurement,
    NothingToMeasure,
    artifact,
    exit_code as measurement_exit_code,
    measure,
    write_measurement_artifact,
)
from corridor.experimental_database import (
    DatabaseGuard,
    ProductionDatabaseRefusal,
    experimental_session,
    require_experimental_database,
)
from corridor.extract_project import RouteSelector, extract_project
from corridor.measurement_cases import CasePredictionError
from corridor.models import Document, ExtractionRun, Project
from corridor.receipts import identity

COMPARISON_SCHEMA = "corridor.extraction-measurement-comparison.v1"


class CandidateExtractionIncomplete(NothingToMeasure):
    """The candidate model did not cleanly read every measured document.

    A comparison over a smaller document set than the current reading would
    pin a different input scope, and two measurements on different inputs are
    not comparable. A candidate that cannot read the corpus is itself a
    measured result — surfaced loudly here, never as a quietly narrower win.
    """


@dataclass(frozen=True)
class RunConfiguration:
    """The sealed extractor identity of one measured Extraction Run."""

    extraction_run_id: int
    document_id: int
    document_sha256: str
    model: str | None
    prompt_version: str
    schema_version: str | None
    extractor_config_sha256: str | None
    prompt_sha256: str | None
    schema_sha256: str | None
    postprocessor_sha256: str | None

    def as_dict(self) -> dict:
        return {
            "extraction_run_id": self.extraction_run_id,
            "document_id": self.document_id,
            "document_sha256": self.document_sha256,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "extractor_config_sha256": self.extractor_config_sha256,
            "prompt_sha256": self.prompt_sha256,
            "schema_sha256": self.schema_sha256,
            "postprocessor_sha256": self.postprocessor_sha256,
        }


@dataclass(frozen=True)
class CandidateComparison:
    """Two Extraction Measurements of one corpus, scored against one reference."""

    project: str
    candidate_model: str
    current: Measurement
    candidate: Measurement
    current_configuration: tuple[RunConfiguration, ...]
    candidate_configuration: tuple[RunConfiguration, ...]

    @property
    def current_models(self) -> tuple[str | None, ...]:
        return tuple(config.model for config in self.current_configuration)


def run_candidate_comparison(
    session: Session,
    slug: str,
    *,
    current_extraction_run_ids: set[int],
    candidate_route_selector: RouteSelector,
    candidate_model: str,
    gold_path: str | Path | None = None,
    reference_manifest_path: str | Path | None = None,
    case_predictions_path: str | Path | None = None,
    prompt_version: str | None = None,
) -> CandidateComparison:
    """Score the current reading and a fresh candidate reading of one corpus.

    ``current_extraction_run_ids`` names the baseline exactly, one completed
    run per measured matrix, exactly as Extraction Measurement requires — no
    prompt, recency, or Active Run inference selects the population. The
    candidate model then reads precisely those same documents (via the injected
    ``candidate_route_selector``), and both readings are scored against the one
    Reference Dataset the caller supplies so the two numbers are comparable.

    The candidate must complete on every measured document; a candidate that
    cannot read one raises :class:`CandidateExtractionIncomplete` rather than
    silently comparing a narrower corpus. No Active Run is declared, so the
    candidate reading never becomes a Current Production Run.
    """
    if not candidate_model or candidate_model != candidate_model.strip():
        raise NothingToMeasure("a candidate model name is required")

    current = measure(
        session,
        slug,
        gold_path=gold_path,
        reference_manifest_path=reference_manifest_path,
        prompt_version=prompt_version,
        extraction_run_ids=set(current_extraction_run_ids),
        case_predictions_path=case_predictions_path,
    )

    project = session.scalars(select(Project).where(Project.slug == slug)).one()
    measured_documents = _measured_documents(session, current)

    candidate_run_ids: set[int] = set()
    incomplete: list[str] = []
    for document in measured_documents:
        [outcome] = extract_project(
            session,
            project,
            select_route=candidate_route_selector,
            redo=True,
            commit=False,
            document_sha256=document.sha256,
        )
        if outcome.status != "extracted" or outcome.extraction_run_id is None:
            incomplete.append(
                f"{document.registry_id or document.filename} "
                f"({outcome.status}: {outcome.detail or 'no run'})"
            )
            continue
        candidate_run_ids.add(outcome.extraction_run_id)

    if incomplete:
        raise CandidateExtractionIncomplete(
            f"candidate model {candidate_model!r} did not cleanly read "
            + ", ".join(incomplete)
        )

    candidate = measure(
        session,
        slug,
        gold_path=gold_path,
        reference_manifest_path=reference_manifest_path,
        extraction_run_ids=candidate_run_ids,
        case_predictions_path=case_predictions_path,
    )

    candidate_configuration = _run_configurations(session, candidate_run_ids)
    ran_models = {config.model for config in candidate_configuration}
    if ran_models != {candidate_model}:
        raise CandidateExtractionIncomplete(
            "candidate runs recorded model(s) "
            + ", ".join(sorted(str(model) for model in ran_models))
            + f", not the named candidate model {candidate_model!r}"
        )

    return CandidateComparison(
        project=slug,
        candidate_model=candidate_model,
        current=current,
        candidate=candidate,
        current_configuration=_run_configurations(
            session, set(current.extraction_run_ids)
        ),
        candidate_configuration=candidate_configuration,
    )


def _measured_documents(
    session: Session, measurement: Measurement
) -> list[Document]:
    """The exact documents the current reading covered, ordered by id."""

    document_ids = [run.document_id for run in measurement.extraction_runs]
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_(document_ids or {0}))
        ).all()
    }
    return [documents[document_id] for document_id in sorted(document_ids)]


def _run_configurations(
    session: Session, extraction_run_ids: set[int]
) -> tuple[RunConfiguration, ...]:
    rows = session.execute(
        select(ExtractionRun, Document.sha256)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(ExtractionRun.id.in_(extraction_run_ids or {0}))
        .order_by(ExtractionRun.document_id)
    ).all()
    return tuple(
        RunConfiguration(
            extraction_run_id=run.id,
            document_id=run.document_id,
            document_sha256=document_sha256,
            model=run.model,
            prompt_version=run.prompt_version,
            schema_version=run.schema_version,
            extractor_config_sha256=run.extractor_config_sha256,
            prompt_sha256=run.prompt_sha256,
            schema_sha256=run.schema_sha256,
            postprocessor_sha256=run.postprocessor_sha256,
        )
        for run, document_sha256 in rows
    )


def _metric_delta(current: float | None, candidate: float | None) -> dict:
    """Current, candidate, and their difference — null where one is not scored.

    A metric that is not comparable (an unmeasurable precision, an unlabelled
    critical set) reports the two values it has and a null delta, never a
    fabricated zero that would read as "no change".
    """
    delta = (
        candidate - current
        if isinstance(current, (int, float)) and isinstance(candidate, (int, float))
        else None
    )
    return {"current": current, "candidate": candidate, "delta": delta}


def _comparison_block(comparison: CandidateComparison) -> dict:
    """Per-reference deltas plus the arithmetic list of regressions.

    Adoption is a human read of these numbers (the documented "measured win"
    practice); this block only states what moved, and which way. Lower is
    better for field-token failures and human-ruling mismatches, so their
    "improved" test is inverted.
    """
    current = comparison.current.result
    candidate = comparison.candidate.result
    current_cases = comparison.current.case_measurement
    candidate_cases = comparison.candidate.case_measurement

    metrics = {
        "recall": _metric_delta(current.recall, candidate.recall),
        "precision": _metric_delta(
            None if current.partial_coverage else current.precision,
            None if candidate.partial_coverage else candidate.precision,
        ),
        "coverage": _metric_delta(current.coverage, candidate.coverage),
        "critical_recall": _metric_delta(
            None if current.critical_unmeasurable else current.critical_recall,
            None if candidate.critical_unmeasurable else candidate.critical_recall,
        ),
        "field_failures": _metric_delta(
            current.field_failures, candidate.field_failures
        ),
    }
    human_ruling = {
        "current": {
            "matched": current_cases.matched if current_cases else 0,
            "mismatched": current_cases.mismatched if current_cases else 0,
        },
        "candidate": {
            "matched": candidate_cases.matched if candidate_cases else 0,
            "mismatched": candidate_cases.mismatched if candidate_cases else 0,
        },
    }

    improvements: list[str] = []
    regressions: list[str] = []
    for name in ("recall", "precision", "coverage", "critical_recall"):
        delta = metrics[name]["delta"]
        if delta is None:
            continue
        if delta > 0:
            improvements.append(name)
        elif delta < 0:
            regressions.append(name)
    field_delta = metrics["field_failures"]["delta"]
    if field_delta is not None and field_delta < 0:
        improvements.append("field_failures")
    elif field_delta is not None and field_delta > 0:
        regressions.append("field_failures")
    new_mismatches = (
        human_ruling["candidate"]["mismatched"] - human_ruling["current"]["mismatched"]
    )
    if new_mismatches > 0:
        regressions.append("human_ruling_cases")
    elif new_mismatches < 0:
        improvements.append("human_ruling_cases")

    return {
        "metrics": metrics,
        "human_ruling_cases": human_ruling,
        "improvements": improvements,
        "regressions": regressions,
    }


def comparison_artifact(
    comparison: CandidateComparison, *, ran_at: datetime
) -> dict:
    """The immutable record of one current-versus-candidate comparison.

    It embeds both sides' full measurement artifacts (each already carrying its
    model, prompt, reference scope, and its own identity hash), the sealed
    configuration identity of every run, and the per-reference deltas. The
    ``comparison_identity`` excludes ``ran_at`` alone, so repeating the exact
    command resolves to the same immutable file.
    """
    current_artifact = artifact(
        comparison.current.result,
        reference_description=comparison.current.reference_description,
        ran_at=ran_at,
        extraction_runs=comparison.current.extraction_runs,
        reference_scope=comparison.current.reference_scope,
        case_measurement=comparison.current.case_measurement,
    )
    candidate_artifact = artifact(
        comparison.candidate.result,
        reference_description=comparison.candidate.reference_description,
        ran_at=ran_at,
        extraction_runs=comparison.candidate.extraction_runs,
        reference_scope=comparison.candidate.reference_scope,
        case_measurement=comparison.candidate.case_measurement,
    )
    # One timestamp for the whole comparison. The embedded per-side artifacts
    # were scored in this same command at this same instant, and their own
    # ``ran_at`` would otherwise be a second varying field that defeats the
    # immutable-rerun check (which strips only the top-level timestamp). Their
    # identity hashes already exclude time, so nothing provenance-bearing is
    # lost by dropping the redundant copy.
    current_artifact.pop("ran_at", None)
    candidate_artifact.pop("ran_at", None)
    written = {
        "schema_version": COMPARISON_SCHEMA,
        "project": comparison.project,
        "candidate_model": comparison.candidate_model,
        "ran_at": ran_at.isoformat(),
        "current": current_artifact,
        "candidate": candidate_artifact,
        "current_configuration": [
            config.as_dict() for config in comparison.current_configuration
        ],
        "candidate_configuration": [
            config.as_dict() for config in comparison.candidate_configuration
        ],
        "comparison": _comparison_block(comparison),
    }
    identity_material = {
        "schema_version": COMPARISON_SCHEMA,
        "project": comparison.project,
        "candidate_model": comparison.candidate_model,
        "current_identity": current_artifact["artifact_identity"],
        "candidate_identity": candidate_artifact["artifact_identity"],
        "current_configuration": written["current_configuration"],
        "candidate_configuration": written["candidate_configuration"],
    }
    written["comparison_identity"] = identity(identity_material)
    return written


def render_comparison(comparison: CandidateComparison) -> str:
    current = comparison.current.result
    candidate = comparison.candidate.result
    block = _comparison_block(comparison)
    current_model = ", ".join(
        sorted({str(model) for model in comparison.current_models})
    )
    lines = [
        f"{comparison.project} — current versus candidate",
        f"  current model:   {current_model or '—'}",
        f"  candidate model: {comparison.candidate_model}",
        f"  scored over {len(comparison.candidate_configuration)} matrix/matrices "
        "against one reference",
    ]
    for name in ("recall", "precision", "coverage", "critical_recall"):
        lines.append(_metric_line(name, block["metrics"][name]))
    lines.append(
        _metric_line(
            "field-token failures", block["metrics"]["field_failures"], integer=True
        )
    )
    human = block["human_ruling_cases"]
    lines.append(
        "  human ruling cases   "
        f"current {human['current']['matched']}✓/{human['current']['mismatched']}✗   "
        f"candidate {human['candidate']['matched']}✓/{human['candidate']['mismatched']}✗"
    )
    if block["improvements"]:
        lines.append("  improved:  " + ", ".join(block["improvements"]))
    if block["regressions"]:
        lines.append("  regressed: " + ", ".join(block["regressions"]))
    if not block["improvements"] and not block["regressions"]:
        lines.append("  no measured change on any comparable metric")
    lines.append(
        "  A measured win adopts the candidate only when it does not regress; "
        "adoption is a human read of this receipt, not this command's."
    )
    if current.unmeasurable or candidate.unmeasurable:
        lines.append(
            "  NOTE: at least one side is unmeasurable; the reference could not "
            "score it and this is not a win."
        )
    return "\n".join(lines)


def _metric_line(name: str, delta: dict, *, integer: bool = False) -> str:
    def fmt(value):
        if value is None:
            return "—"
        return f"{value:d}" if integer else f"{value:.1%}"

    signed = ""
    if delta["delta"] is not None:
        arrow = "+" if delta["delta"] >= 0 else ""
        signed = (
            f"  ({arrow}{delta['delta']:d})"
            if integer
            else f"  ({arrow}{delta['delta']:.1%})"
        )
    return (
        f"  {name:<20} current {fmt(delta['current'])}   "
        f"candidate {fmt(delta['candidate'])}{signed}"
    )


def exit_code(comparison: CandidateComparison) -> int:
    """A comparison whose reference could not score a side is not a clean run."""

    return (
        1
        if measurement_exit_code(comparison.current.result)
        or measurement_exit_code(comparison.candidate.result)
        else 0
    )


def main(
    argv: list[str],
    *,
    session_factory=None,
    database_guard: DatabaseGuard = require_experimental_database,
    candidate_route_factory=None,
    output_dir: Path | str | None = None,
    ran_at: datetime | None = None,
) -> int:
    """`candidate-model <slug> [reference.csv] --candidate-model=NAME \
--current-extraction-run=N [...] --database-url=DISPOSABLE_URL`.

    ``--current-extraction-run`` names the current reading exactly, one per
    matrix. ``--candidate-model`` names the model to try; it reads the same
    documents and is scored against the same reference. ``--reference-manifest``
    binds a machine reference to its author-time scope; ``--prompt-version`` and
    ``--case-predictions`` behave exactly as they do for `make eval`. The named
    database must be a disposable copy and is refused if it is production.
    """
    known = (
        "--current-extraction-run=",
        "--candidate-model=",
        "--prompt-version=",
        "--reference-manifest=",
        "--case-predictions=",
        "--database-url=",
    )
    flags = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    prompt_version = _single_flag(flags, "--prompt-version=")
    candidate_model = _single_flag(flags, "--candidate-model=")
    reference_manifests = [
        f.split("=", 1)[1] for f in flags if f.startswith("--reference-manifest=")
    ]
    case_prediction_paths = [
        f.split("=", 1)[1] for f in flags if f.startswith("--case-predictions=")
    ]
    database_urls = [
        f.split("=", 1)[1] for f in flags if f.startswith("--database-url=")
    ]
    current_runs = [
        f.split("=", 1)[1] for f in flags if f.startswith("--current-extraction-run=")
    ]
    if (
        not args
        or len(args) > 2
        or len(reference_manifests) > 1
        or len(case_prediction_paths) > 1
        or any(not f.startswith(known) for f in flags)
    ):
        return _usage()
    if not candidate_model:
        print("--candidate-model=NAME is required", file=sys.stderr)
        return 2
    try:
        parsed_run_ids = [int(n) for n in current_runs]
    except ValueError:
        print("--current-extraction-run takes integer ids", file=sys.stderr)
        return 2
    current_extraction_run_ids = set(parsed_run_ids)
    if len(parsed_run_ids) != len(current_extraction_run_ids):
        print("duplicate --current-extraction-run ids are not allowed", file=sys.stderr)
        return 2
    if not current_extraction_run_ids or any(
        run_id <= 0 for run_id in current_extraction_run_ids
    ):
        print(
            "one or more positive --current-extraction-run ids are required; "
            "prompt and document selectors cannot choose a measurement population",
            file=sys.stderr,
        )
        return 2
    if len(database_urls) != 1 or not database_urls[0]:
        print(
            "one explicit --database-url is required for a candidate comparison",
            file=sys.stderr,
        )
        return 2

    slug = args[0]
    gold_path = args[1] if len(args) > 1 else None
    reference_manifest_path = reference_manifests[0] if reference_manifests else None
    if candidate_route_factory is None:
        candidate_route_factory = _openai_candidate_route

    try:
        with experimental_session(
            database_urls[0],
            session_factory=session_factory,
            database_guard=database_guard,
        ) as session:
            with candidate_route_factory(candidate_model) as candidate_route_selector:
                comparison = run_candidate_comparison(
                    session,
                    slug,
                    current_extraction_run_ids=current_extraction_run_ids,
                    candidate_route_selector=candidate_route_selector,
                    candidate_model=candidate_model,
                    gold_path=gold_path,
                    reference_manifest_path=reference_manifest_path,
                    prompt_version=prompt_version,
                    case_predictions_path=(
                        case_prediction_paths[0] if case_prediction_paths else None
                    ),
                )
    except (CasePredictionError, NothingToMeasure, ProductionDatabaseRefusal) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(render_comparison(comparison))

    out = Path(output_dir) if output_dir is not None else Path("out")
    out.mkdir(exist_ok=True)
    written = comparison_artifact(
        comparison, ran_at=ran_at or datetime.now(timezone.utc)
    )
    path = out / (
        f"candidate-comparison-{slug}-{written['comparison_identity'][:16]}.json"
    )
    try:
        created = write_measurement_artifact(path, written)
    except ArtifactCollision as exc:
        print(str(exc), file=sys.stderr)
        return 1
    suffix = "" if created else " (existing immutable comparison preserved)"
    print(f"\n{path}{suffix}")
    return exit_code(comparison)

def _single_flag(flags: list[str], prefix: str) -> str | None:
    return next((f.split("=", 1)[1] for f in flags if f.startswith(prefix)), None)


def _usage() -> int:
    print(
        "usage: candidate-model <project-slug> [reference.csv] "
        "--candidate-model=NAME "
        "--current-extraction-run=N [--current-extraction-run=N ...] "
        "--database-url=DISPOSABLE_POSTGRESQL_URL "
        "[--reference-manifest=scope.json] "
        "[--case-predictions=outputs.json] [--prompt-version=X]",
        file=sys.stderr,
    )
    return 2


def _openai_candidate_route(candidate_model: str):
    """Build a production route selector that runs one named candidate model."""

    from contextlib import contextmanager

    @contextmanager
    def _route():
        from corridor.llm import OpenAIClient
        from corridor.pipeline import extraction_route

        client = OpenAIClient(model=candidate_model)
        print(f"candidate model {client.model}", flush=True)
        try:
            yield lambda document: extraction_route(document, client=client)
        finally:
            client.close()

    return _route()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
