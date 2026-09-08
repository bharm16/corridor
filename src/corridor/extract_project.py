"""Extraction over every matrix and registered plan spreadsheet in a project.

Extraction was reachable only through the scripted demo path, so a project
could be fully ingested and produce no Candidates at all with nothing in
the output saying so. `make ingest` reports `1/1 parsed` and a reader
reasonably concludes the pipeline ran. Four of Project A's five ingested
matrices had never been extracted, and the only reason that was visible is
that `make eval` counts them.

Four outcomes, deliberately distinct:

- **extracted** — the extractor read the document. Zero rows here is a
  correct answer: a matrix with no conflicts.
- **unreadable** — the extractor could not read it. Reporting this as
  "0 rows" is exactly how a broken pipeline passes for a quiet one, which
  is why `NoMatrixFound` is an exception and not an empty list.
- **failed** — the extractor could not finish the attempt. This says the
  run failed, not that the layout is unhandled, so a sibling document still
  proceeds and a later retry may succeed unchanged.
- **skipped** — already extracted at this prompt version. Extraction
  inserts unconditionally, so without this a second run doubles every
  Candidate a reviewer then has to clear by hand.

The extractor is injected so tests can drive this without a model. There
is one in production: the tiered extractor (ADR-0006).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extract_batch import already_extracted
from corridor.extractor_lineage import usage_snapshot
from corridor.extract_matrix import (
    ExtractionFailed,
    PROMPT_VERSION,
    SequencingSemanticsDetected,
)
from corridor.geometry import NoMatrixFound
from corridor.ingest import SPREADSHEET_SUFFIXES
from corridor.models import (
    Candidate,
    Document,
    DocumentQuarantine,
    ExtractionRun,
    PipelineObservation,
    Project,
)
from corridor.pipeline import ExtractionRoute, extraction_attempt, record_routed_run
from corridor.row_accounting import RowAccountingFailure

# An extractor reads one Document and returns the Candidates it produced,
# already added to the session. It raises `NoMatrixFound` when it cannot
# read the document at all, and records how it read the document on
# `Document.extraction_tiers` and `Document.header_disagreements` — real
# columns, so the answer survives the run that produced it.
Extractor = Callable[[Session, Document], list[Candidate]]
RouteSelector = Callable[[Document], ExtractionRoute]


class UnknownDocument(ValueError):
    """The named registry id resolves to no document in this project."""


class UnextractableDocument(ValueError):
    """The named document exists but is not a type extraction reads."""


@dataclass(frozen=True)
class Outcome:
    document_id: int
    filename: str
    status: str
    effective_prompt_version: str
    rows: int = 0
    unverified: int = 0
    detail: str = ""
    # Pages per extraction tier, where the extractor reports it. A document
    # that fell back to transcription says so here rather than looking like
    # one that read cleanly.
    tiers: dict[str, int] = field(default_factory=dict)
    # Printed headers this document read more than one way before majority
    # resolution (#101). Zero is the expected answer and the one worth
    # stating: a document that disagreed with itself about what its own
    # columns mean is one whose mapping a reviewer should look at.
    header_disagreements: int = 0
    # The exact terminal receipt produced by this attempt. A skip produces no
    # new receipt and therefore leaves this unset.
    extraction_run_id: int | None = None


def extract_project(
    session: Session,
    project: Project,
    *,
    extract: Extractor | None = None,
    select_route: RouteSelector | None = None,
    prompt_version: str = PROMPT_VERSION,
    redo: bool = False,
    commit: bool = True,
    document_registry_id: str | None = None,
    document_sha256: str | None = None,
) -> list[Outcome]:
    """Extract every supported document in the project, one Outcome each.

    ``document_registry_id`` names one document — by registry identity,
    never database id — and extracts it alone: a two-document rehearsal
    must not pay for five extractions. The named path refuses loudly where
    the project sweep would silently cover zero documents.

    Committing each document's Candidates and ExtractionRun together makes
    resume safe: a killed run cannot expose a completion receipt without the
    results from that attempt, and a successful zero-row read still has a
    durable receipt to skip next time.
    """
    if (extract is None) == (select_route is None):
        raise TypeError("pass exactly one of extract= or select_route=")
    if select_route is None:
        assert extract is not None

        def select_route(document: Document) -> ExtractionRoute:
            return ExtractionRoute(
                effective_prompt_version=prompt_version,
                schema_version=prompt_version,
                extract=extract,
                allow_unsealed_legacy=True,
            )

    if document_registry_id is not None and document_sha256 is not None:
        raise ValueError("pass exactly one Document selector")
    if document_registry_id is not None or document_sha256 is not None:
        criterion = (
            Document.registry_id == document_registry_id
            if document_registry_id is not None
            else Document.sha256 == document_sha256
        )
        named = session.scalars(
            select(Document).where(Document.project_id == project.id, criterion)
        ).first()
        if named is None:
            identity = (
                repr(document_registry_id)
                if document_registry_id is not None
                else str(document_sha256)
            )
            raise UnknownDocument(
                f"no document in {project.slug!r} carries identity {identity}"
            )
        if not extractable_document(named):
            raise UnextractableDocument(
                f"{document_registry_id!r} is {named.doc_type!r}; extraction "
                "reads matrices and registered plan spreadsheets"
            )
        documents = [named]
    else:
        registered = session.scalars(
            select(Document)
            .where(Document.project_id == project.id)
            .order_by(Document.doc_date, Document.id)
        ).all()
        documents = [document for document in registered if extractable_document(document)]

    done_by_version: dict[str, set[int]] = {}
    outcomes = []

    for document in documents:
        route = select_route(document)
        usage_before = usage_snapshot(route.usage_client)
        effective_prompt_version = route.effective_prompt_version
        try:
            if route.validate is not None:
                route.validate(session, document)
        except ExtractionFailed as exc:
            run = record_routed_run(
                session, document, route, usage_before, candidate_count=0,
                page_errors=1, outcome="failed", model=route.model, error_detail=str(exc),
            )
            if commit:
                session.commit()
            outcomes.append(Outcome(document.id, document.filename, "failed",
                effective_prompt_version=effective_prompt_version, detail=str(exc),
                extraction_run_id=run.id))
            continue
        done = done_by_version.get(effective_prompt_version)
        if done is None:
            done = already_extracted(session, project.id, effective_prompt_version)
            done_by_version[effective_prompt_version] = done

        if route.pipeline_configuration_sha256 is not None:
            # A same-prompt run from another code/configuration is not this
            # selected reading. This also handles completed zero-row captures.
            done = set(session.scalars(
                select(ExtractionRun.document_id).join(PipelineObservation,
                    PipelineObservation.extraction_run_id == ExtractionRun.id).where(
                    ExtractionRun.document_id == document.id,
                    ExtractionRun.outcome == "completed",
                    PipelineObservation.configuration_sha256 == route.pipeline_configuration_sha256,
                )
            ))

        if document.id in done and not redo:
            # A skipped document reports the tiers it was read at, not
            # nothing. Reporting nothing made every resumed run — the
            # normal case the skip exists for — under-state the fallback
            # share, which is the one number this report exists to carry.
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "skipped",
                    effective_prompt_version=effective_prompt_version,
                    detail=f"already extracted at {effective_prompt_version}",
                    tiers=dict(document.extraction_tiers or {}),
                    header_disagreements=document.header_disagreements or 0,
                )
            )
            continue

        if document.parse_status != "parsed":
            detail = f"ingest parse_status is {document.parse_status!r}"
            run = record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="unreadable",
                model=route.model,
                error_detail=detail,
            )
            if commit:
                session.commit()
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "unreadable",
                    effective_prompt_version=effective_prompt_version,
                    detail=detail,
                    extraction_run_id=run.id,
                )
            )
            continue

        try:
            # A savepoint, so a document that raises partway through leaves
            # no half-extracted Candidates behind — which is what the skip
            # on the next run depends on being impossible.
            with extraction_attempt(session):
                candidates = route.extract(session, document)
                run = record_routed_run(
                    session,
                    document,
                    route,
                    usage_before,
                    candidate_count=len(candidates),
                    page_errors=0,
                    outcome="completed",
                    candidates=candidates,
                    model=_run_model(candidates, route.model),
                    row_accounting_json=getattr(
                        candidates, "row_accounting", None
                    ),
                )
        except RowAccountingFailure as exc:
            run = record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="failed",
                model=route.model,
                error_detail=str(exc),
                row_accounting_json=exc.receipt,
            )
            if commit:
                session.commit()
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "failed",
                    effective_prompt_version=effective_prompt_version,
                    detail=str(exc),
                    extraction_run_id=run.id,
                )
            )
            continue
        except SequencingSemanticsDetected as exc:
            run = record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="quarantined",
                model=route.model,
                error_detail=str(exc),
            )
            if session.get(DocumentQuarantine, document.id) is None:
                session.add(
                    DocumentQuarantine(document_id=document.id, reason=str(exc))
                )
            if commit:
                session.commit()
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "quarantined",
                    effective_prompt_version=effective_prompt_version,
                    detail=str(exc),
                    extraction_run_id=run.id,
                )
            )
            continue
        except NoMatrixFound as exc:
            run = record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="no_matrix",
                model=route.model,
                error_detail=str(exc),
            )
            if commit:
                session.commit()
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "unreadable",
                    effective_prompt_version=effective_prompt_version,
                    detail=str(exc),
                    extraction_run_id=run.id,
                )
            )
            continue
        except ExtractionFailed as exc:
            run = record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="failed",
                model=route.model,
                error_detail=str(exc),
            )
            if commit:
                session.commit()
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "failed",
                    effective_prompt_version=effective_prompt_version,
                    detail=str(exc),
                    extraction_run_id=run.id,
                )
            )
            continue
        except Exception as exc:
            record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=0,
                page_errors=1,
                outcome="failed",
                model=route.model,
                error_detail=f"{type(exc).__name__}: {exc}",
            )
            if commit:
                session.commit()
            raise

        if commit:
            session.commit()
        outcomes.append(
            Outcome(
                document.id,
                document.filename,
                "extracted",
                effective_prompt_version=effective_prompt_version,
                rows=len(candidates),
                unverified=sum(1 for c in candidates if not c.citations_verified),
                tiers=dict(document.extraction_tiers or {}),
                header_disagreements=document.header_disagreements or 0,
                extraction_run_id=run.id,
            )
        )

    return outcomes


def extractable_document(document: Document) -> bool:
    # An "email" Document is a routed inbound message body (#372, ADR-0058):
    # written evidence that flows through the ordinary prose statement path,
    # so the standing pass is its durable handoff too.
    return document.doc_type in ("matrix", "email") or (
        document.doc_type == "plan"
        and Path(document.filename).suffix.lower() in SPREADSHEET_SUFFIXES
    )


def _run_model(
    candidates: list[Candidate], configured_model: str | None
) -> str | None:
    """Resolve one configured/observed model without losing zero-row lineage."""
    models = {candidate.model for candidate in candidates if candidate.model}
    if len(models) > 1:
        raise ValueError("one extraction run cannot contain multiple models")
    if configured_model is not None and models and models != {configured_model}:
        raise ValueError("Candidate model does not match the configured extraction model")
    return configured_model or next(iter(models), None)


def render(project: Project, prompt_version: str, outcomes: list[Outcome]) -> str:
    lines = [f"{project.name} — {prompt_version}"]
    for outcome in outcomes:
        versioned_name = (
            f"{outcome.filename.split('/')[-1][:56]} "
            f"[{outcome.effective_prompt_version}]"
        )
        if outcome.status == "extracted":
            lines.append(
                "  "
                f"{outcome.rows:>5} rows  {outcome.unverified:>4} unverified  "
                f"{versioned_name}"
            )
        elif outcome.status == "failed":
            lines.append(f"  FAILED                      {versioned_name}: {outcome.detail}")
        elif outcome.status == "unreadable":
            lines.append(
                f"  UNREADABLE                  {versioned_name}: {outcome.detail}"
            )
        elif outcome.status == "quarantined":
            lines.append(
                f"  QUARANTINED                 {versioned_name}: {outcome.detail}"
            )
        else:
            lines.append(
                f"  skipped                     {versioned_name} ({outcome.detail})"
            )

    extracted = [o for o in outcomes if o.status == "extracted"]
    failed = [o for o in outcomes if o.status == "failed"]
    unreadable = [o for o in outcomes if o.status == "unreadable"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    quarantined = [o for o in outcomes if o.status == "quarantined"]
    lines.append(
        f"{len(outcomes)} documents: {len(extracted)} extracted "
        f"({sum(o.rows for o in extracted):,} rows, "
        f"{sum(o.unverified for o in extracted):,} unverified), "
        f"{len(failed)} failed, {len(unreadable)} unreadable, "
        f"{len(skipped)} skipped"
        + (f", {len(quarantined)} quarantined" if quarantined else "")
    )
    if quarantined:
        lines.append(
            "  A quarantined document asserts relationships Corridor does "
            "not model (#149). No row was read: out of scope means "
            "unsupported, never lossy."
        )
    if failed:
        lines.append(
            "  A failed document did not finish extraction. Retry it; do not "
            "treat the failure as evidence the layout is unsupported."
        )
    if unreadable:
        lines.append(
            "  An unreadable document is an unhandled layout, not an empty "
            "matrix. Do not read it as a project with no conflicts."
        )

    disagreed = sum(o.header_disagreements for o in outcomes)
    if extracted:
        lines.append(
            f"  headers: {disagreed} read more than one way across pages "
            "and resolved by majority"
            + ("" if disagreed else " — every page agreed")
        )

    # Named even when it is zero, because "no fallback" is the claim worth
    # making. A fallback nobody counts is a fallback nobody notices.
    fell_back = sum(o.tiers.get("transcribe", 0) for o in outcomes)
    read_cleanly = sum(o.tiers.get("structure", 0) for o in outcomes)
    if fell_back or read_cleanly:
        share = 100 * fell_back / (fell_back + read_cleanly)
        lines.append(
            f"  pages: {read_cleanly} read from the text layer, "
            f"{fell_back} transcribed ({share:.1f}% fell back)"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """`make extract ARGS="<slug> [--redo]"`"""
    from corridor.admission import load_and_report
    from corridor.db import WorkerSession as SessionFactory
    from corridor.pipeline import production_extraction_routes

    args = [a for a in argv if not a.startswith("-")]
    flags = {a for a in argv if a.startswith("-")}
    document_registry_id = None
    document_sha256 = None
    for flag in sorted(flags):
        if flag.startswith("--document="):
            document_registry_id = flag.removeprefix("--document=")
            flags.discard(flag)
        elif flag.startswith("--document-sha256="):
            document_sha256 = flag.removeprefix("--document-sha256=")
            flags.discard(flag)
    unknown = flags - {"--redo"}
    if (
        not args
        or unknown
        or document_registry_id == ""
        or (
            document_sha256 is not None
            and (
                len(document_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in document_sha256
                )
            )
        )
        or (document_registry_id is not None and document_sha256 is not None)
    ):
        print(
            "usage: extract <project-slug> "
            "[--document=<registry-id>|--document-sha256=<sha256>] [--redo]"
            + (f"\nunknown flag(s): {', '.join(sorted(unknown))}" if unknown else ""),
            file=sys.stderr,
        )
        return 2

    slug = args[0]
    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        with production_extraction_routes() as select_route:
            outcomes = extract_project(
                session,
                project,
                # Routed per document: a project may publish its matrix as
                # a spreadsheet, as a printout of one, or as both, and
                # which reader runs is the document's property rather than
                # this command's (ADR-0005).
                select_route=select_route,
                redo="--redo" in flags,
                document_registry_id=document_registry_id,
                document_sha256=document_sha256,
            )
        print(render(project, PROMPT_VERSION, outcomes))
        print(load_and_report(session, project))
        # The sealed runs own the actual clients' usage, including native
        # provider calls. Skipped documents incur no new usage in this pass.
        runs = [session.get(ExtractionRun, outcome.extraction_run_id)
                for outcome in outcomes if outcome.extraction_run_id is not None]
        receipts = [run.token_usage_json if run is not None else None for run in runs]
        if all(receipt and receipt.get("measurement") == "exact" for receipt in receipts):
            totals = {name: sum(receipt[name] for receipt in receipts)
                      for name in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens")}
            print(f"tokens: {totals['prompt_tokens']:,} in ({totals['cached_tokens']:,} cached) / "
                  f"{totals['completion_tokens']:,} out ({totals['reasoning_tokens']:,} reasoning)")
        else:
            print("token usage is not fully measured; see the individual Extraction Run receipts")

    return 1 if any(o.status in {"failed", "unreadable"} for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
