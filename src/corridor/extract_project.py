"""Extraction over every matrix in a project, from one command.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extract_batch import already_extracted
from corridor.extraction_runs import record_extraction_run
from corridor.extract_matrix import ExtractionFailed, PROMPT_VERSION
from corridor.geometry import NoMatrixFound
from corridor.models import Candidate, Document, Project
from corridor.pipeline import ExtractionRoute, extraction_route

# An extractor reads one Document and returns the Candidates it produced,
# already added to the session. It raises `NoMatrixFound` when it cannot
# read the document at all, and records how it read the document on
# `Document.extraction_tiers` and `Document.header_disagreements` — real
# columns, so the answer survives the run that produced it.
Extractor = Callable[[Session, Document], list[Candidate]]
RouteSelector = Callable[[Document], ExtractionRoute]


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


def extract_project(
    session: Session,
    project: Project,
    *,
    extract: Extractor | None = None,
    select_route: RouteSelector | None = None,
    prompt_version: str = PROMPT_VERSION,
    redo: bool = False,
    commit: bool = True,
) -> list[Outcome]:
    """Extract every matrix in the project, one Outcome per document.

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
                extract=extract,
            )

    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project.id, Document.doc_type == "matrix")
        .order_by(Document.doc_date, Document.id)
    ).all()

    done_by_version: dict[str, set[int]] = {}
    outcomes = []

    for document in documents:
        route = select_route(document)
        effective_prompt_version = route.effective_prompt_version
        done = done_by_version.get(effective_prompt_version)
        if done is None:
            done = already_extracted(session, project.id, effective_prompt_version)
            done_by_version[effective_prompt_version] = done

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
            record_extraction_run(
                session,
                document,
                prompt_version=effective_prompt_version,
                candidate_count=0,
                page_errors=1,
                outcome="unreadable",
                schema_version=effective_prompt_version,
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
                )
            )
            continue

        try:
            # A savepoint, so a document that raises partway through leaves
            # no half-extracted Candidates behind — which is what the skip
            # on the next run depends on being impossible.
            with session.begin_nested():
                candidates = route.extract(session, document)
                record_extraction_run(
                    session,
                    document,
                    prompt_version=effective_prompt_version,
                    candidate_count=len(candidates),
                    page_errors=0,
                    outcome="completed",
                    candidates=tuple(candidates),
                    model=_run_model(candidates),
                    schema_version=effective_prompt_version,
                )
        except NoMatrixFound as exc:
            record_extraction_run(
                session,
                document,
                prompt_version=effective_prompt_version,
                candidate_count=0,
                page_errors=1,
                outcome="no_matrix",
                schema_version=effective_prompt_version,
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
                )
            )
            continue
        except ExtractionFailed as exc:
            record_extraction_run(
                session,
                document,
                prompt_version=effective_prompt_version,
                candidate_count=0,
                page_errors=1,
                outcome="failed",
                schema_version=effective_prompt_version,
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
                )
            )
            continue
        except Exception as exc:
            record_extraction_run(
                session,
                document,
                prompt_version=effective_prompt_version,
                candidate_count=0,
                page_errors=1,
                outcome="failed",
                schema_version=effective_prompt_version,
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
            )
        )

    return outcomes


def _run_model(candidates: list[Candidate]) -> str | None:
    """Return the one model used by a run, rejecting mixed provenance."""
    models = {candidate.model for candidate in candidates if candidate.model}
    if len(models) > 1:
        raise ValueError("one extraction run cannot contain multiple models")
    return next(iter(models), None)


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
        else:
            lines.append(
                f"  skipped                     {versioned_name} ({outcome.detail})"
            )

    extracted = [o for o in outcomes if o.status == "extracted"]
    failed = [o for o in outcomes if o.status == "failed"]
    unreadable = [o for o in outcomes if o.status == "unreadable"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    lines.append(
        f"{len(outcomes)} matrices: {len(extracted)} extracted "
        f"({sum(o.rows for o in extracted):,} rows, "
        f"{sum(o.unverified for o in extracted):,} unverified), "
        f"{len(failed)} failed, {len(unreadable)} unreadable, "
        f"{len(skipped)} skipped"
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
    from corridor.db import Session as SessionFactory
    from corridor.llm import OpenAIClient

    args = [a for a in argv if not a.startswith("-")]
    flags = {a for a in argv if a.startswith("-")}
    unknown = flags - {"--redo"}
    if not args or unknown:
        print(
            "usage: extract <project-slug> [--redo]"
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

        # One client for the run, so retry, backoff and usage accounting are
        # shared rather than reset per document.
        client = OpenAIClient()
        print(f"model {client.model}", flush=True)

        try:
            outcomes = extract_project(
                session,
                project,
                # Routed per document: a project may publish its matrix as
                # a spreadsheet, as a printout of one, or as both, and
                # which reader runs is the document's property rather than
                # this command's (ADR-0005).
                select_route=lambda document: extraction_route(document, client=client),
                redo="--redo" in flags,
            )
        finally:
            client.close()

        print(render(project, PROMPT_VERSION, outcomes))

        # Cached and reasoning are broken out because neither is recoverable
        # from the totals afterwards, and both move the bill: cached input
        # bills at a tenth, reasoning bills as output. A run that quietly
        # reasoned is a run whose cost nobody can explain.
        usage = client.usage
        print(
            f"tokens: {usage.prompt_tokens:,} in "
            f"({usage.cached_tokens:,} cached) / "
            f"{usage.completion_tokens:,} out "
            f"({usage.reasoning_tokens:,} reasoning)"
        )

    return 1 if any(o.status in {"failed", "unreadable"} for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
