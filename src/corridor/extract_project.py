"""Extraction over every matrix in a project, from one command.

Extraction was reachable only through the scripted demo path, so a project
could be fully ingested and produce no Candidates at all with nothing in
the output saying so. `make ingest` reports `1/1 parsed` and a reader
reasonably concludes the pipeline ran. Four of Project A's five ingested
matrices had never been extracted, and the only reason that was visible is
that `make eval` counts them.

Three outcomes, deliberately distinct:

- **extracted** — the extractor read the document. Zero rows here is a
  correct answer: a matrix with no conflicts.
- **unreadable** — the extractor could not read it. Reporting this as
  "0 rows" is exactly how a broken pipeline passes for a quiet one, which
  is why `NoMatrixFound` is an exception and not an empty list.
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

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from corridor.extract_batch import already_extracted
from corridor.extract_matrix import PROMPT_VERSION, extract_document
from corridor.geometry import NoMatrixFound
from corridor.models import Candidate, Document, Project

# An extractor reads one Document and returns the Candidates it produced,
# already added to the session. It raises `NoMatrixFound` when it cannot
# read the document at all.
Extractor = Callable[[Session, Document], list[Candidate]]


@dataclass(frozen=True)
class Outcome:
    document_id: int
    filename: str
    status: str
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
    extract: Extractor,
    prompt_version: str = PROMPT_VERSION,
    redo: bool = False,
    commit: bool = True,
) -> list[Outcome]:
    """Extract every matrix in the project, one Outcome per document.

    Committing on document boundaries is what makes resume safe: a killed
    run then leaves each document either wholly extracted or not started,
    which is the invariant the skip relies on. "Has candidates" has to mean
    "finished" or skipping them would drop half a document's rows.
    """
    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project.id, Document.doc_type == "matrix")
        .order_by(Document.doc_date, Document.id)
    ).all()

    done = already_extracted(session, project.id, prompt_version)
    outcomes = []

    for document in documents:
        if document.id in done and not redo:
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "skipped",
                    detail=f"already extracted at {prompt_version}",
                )
            )
            continue

        if document.parse_status != "parsed":
            outcomes.append(
                Outcome(
                    document.id,
                    document.filename,
                    "unreadable",
                    detail=f"ingest parse_status is {document.parse_status!r}",
                )
            )
            continue

        try:
            # A savepoint, so a document that raises partway through leaves
            # no half-extracted Candidates behind — which is what the skip
            # on the next run depends on being impossible.
            with session.begin_nested():
                # Retiring the old reading and writing its replacement are
                # one step, inside the savepoint together. Outside it, a
                # document that raised kept the delete and lost the rows
                # the rollback took back — ending the run quieter than it
                # started rather than more current, and doing so only when
                # some *later* document committed on its behalf.
                _clear_pending(session, document)
                candidates = extract(session, document)
        except NoMatrixFound as exc:
            outcomes.append(
                Outcome(document.id, document.filename, "unreadable", detail=str(exc))
            )
            continue

        if commit:
            session.commit()
        outcomes.append(
            Outcome(
                document.id,
                document.filename,
                "extracted",
                rows=len(candidates),
                unverified=sum(1 for c in candidates if not c.citations_verified),
                tiers=dict(getattr(document, "extraction_tiers", {}) or {}),
                header_disagreements=int(
                    getattr(document, "header_disagreements", 0) or 0
                ),
            )
        )

    return outcomes


def _clear_pending(session: Session, document: Document) -> None:
    """Drop this document's un-adjudicated Candidates before re-extracting.

    Only `pending`. An accepted or merged Candidate backs a Ledger record
    and is cited by its Assertions; a rejected one is a human decision that
    re-running an extractor has no business undoing.

    Whatever prompt produced them, deliberately (#105). Scoped to the
    version being run, this only fired on a `redo` — a bump made
    `already_extracted` return nothing, so no document reached the clear
    and the superseded version's rows stayed in the queue beside the new
    ones. That doubled the review queue on `txdot_ucm` and again when #97
    merged, where 4,702 rows were deleted by hand. A reviewer has one
    queue, not one per prompt version.
    """
    session.execute(
        delete(Candidate).where(
            Candidate.source_document_id == document.id,
            Candidate.state == "pending",
        )
    )
    session.flush()


def render(project: Project, prompt_version: str, outcomes: list[Outcome]) -> str:
    lines = [f"{project.name} — {prompt_version}"]
    for outcome in outcomes:
        name = outcome.filename.split("/")[-1][:56]
        if outcome.status == "extracted":
            lines.append(
                f"  {outcome.rows:>5} rows  {outcome.unverified:>4} unverified  {name}"
            )
        elif outcome.status == "unreadable":
            lines.append(f"  UNREADABLE                  {name}: {outcome.detail}")
        else:
            lines.append(f"  skipped                     {name} ({outcome.detail})")

    extracted = [o for o in outcomes if o.status == "extracted"]
    unreadable = [o for o in outcomes if o.status == "unreadable"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    lines.append(
        f"{len(outcomes)} matrices: {len(extracted)} extracted "
        f"({sum(o.rows for o in extracted):,} rows, "
        f"{sum(o.unverified for o in extracted):,} unverified), "
        f"{len(unreadable)} unreadable, {len(skipped)} skipped"
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
                extract=lambda s, d: extract_document(s, d, client=client),
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

    return 1 if any(o.status == "unreadable" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
