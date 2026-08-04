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

The extractor is injected. The deterministic table parser is the default
today; the vision extractor swaps in at the same seam.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from corridor.docs import stored_pdf
from corridor.extract import PROMPT_VERSION as DETERMINISTIC_VERSION
from corridor.extract import NoMatrixFound, extract_rows, to_candidates
from corridor.extract_batch import already_extracted
from corridor.models import Candidate, DocPage, Document, Project

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


def deterministic(
    session: Session, document: Document, *, path: Path | str | None = None
) -> list[Candidate]:
    """The table parser (`corridor.extract`) as a project extractor."""
    path = path or stored_pdf(document)
    if path is None:
        raise NoMatrixFound(
            f"{document.filename} is not in the corpus store; "
            "run `make corpus` before extracting."
        )

    # Verify against the *stored* page text, which is what evidence display
    # will show. Verifying against a freshly re-extracted copy could pass
    # here and fail in the UI.
    page_text = {
        page.page_no: page.text
        for page in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    }

    candidates = []
    for payload in to_candidates(
        extract_rows(path), document_id=document.id, page_text=page_text
    ):
        citations = payload["citations"]
        candidate = Candidate(
            project_id=document.project_id,
            kind=payload["kind"],
            payload_json=payload,
            source_document_id=document.id,
            source_pages=sorted({c["page"] for c in citations}),
            confidence=payload["confidence"],
            prompt_version=DETERMINISTIC_VERSION,
            # No model involved, and recording that honestly matters when
            # eval compares runs.
            model=None,
            citations_verified=all(c["verified"] for c in citations),
        )
        session.add(candidate)
        candidates.append(candidate)

    session.flush()
    return candidates


def extract_project(
    session: Session,
    project: Project,
    *,
    extract: Extractor = deterministic,
    prompt_version: str = DETERMINISTIC_VERSION,
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
        if document.id in done:
            if not redo:
                outcomes.append(
                    Outcome(
                        document.id,
                        document.filename,
                        "skipped",
                        detail=f"already extracted at {prompt_version}",
                    )
                )
                continue
            _clear_pending(session, document, prompt_version)

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
            )
        )

    return outcomes


def _clear_pending(session: Session, document: Document, prompt_version: str) -> None:
    """Drop this document's un-adjudicated Candidates before re-extracting.

    Only `pending`. An accepted or merged Candidate backs a Ledger record
    and is cited by its Assertions; a rejected one is a human decision that
    re-running an extractor has no business undoing.
    """
    session.execute(
        delete(Candidate).where(
            Candidate.source_document_id == document.id,
            Candidate.prompt_version == prompt_version,
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
    """`make extract ARGS="<slug> [--vision] [--redo]"`"""
    from corridor.db import Session as SessionFactory

    args = [a for a in argv if not a.startswith("-")]
    flags = {a for a in argv if a.startswith("-")}
    unknown = flags - {"--redo", "--vision"}
    if not args or unknown:
        print(
            "usage: extract <project-slug> [--vision] [--redo]"
            + (f"\nunknown flag(s): {', '.join(sorted(unknown))}" if unknown else ""),
            file=sys.stderr,
        )
        return 2

    slug = args[0]
    client = None
    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        if "--vision" in flags:
            from corridor.extract_matrix import PROMPT_VERSION as VISION_VERSION
            from corridor.extract_matrix import extract_document
            from corridor.llm import OpenAIClient

            # One client for the run, so retry, backoff and usage accounting
            # are shared rather than reset per document.
            client = OpenAIClient()
            prompt_version = VISION_VERSION

            def extract(session, document):
                return extract_document(session, document, client=client)

            print(f"vision extraction, model {client.model}", flush=True)
        else:
            extract, prompt_version = deterministic, DETERMINISTIC_VERSION

        try:
            outcomes = extract_project(
                session,
                project,
                extract=extract,
                prompt_version=prompt_version,
                redo="--redo" in flags,
            )
        finally:
            if client is not None:
                client.close()

        print(render(project, prompt_version, outcomes))
        if client is not None:
            print(
                f"tokens: {client.usage.prompt_tokens:,} in / "
                f"{client.usage.completion_tokens:,} out"
            )

    return 1 if any(o.status == "unreadable" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
