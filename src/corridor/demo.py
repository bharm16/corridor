"""Raw files to a cited report, in one command.

The walking skeleton's exit criterion. Deliberately thin at every stage —
one document, accept-everything instead of a review queue — but it exercises the whole spine, including both provenance
classes in the output. Its purpose is to surface schema gaps while
migrations are still cheap.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from sqlalchemy import delete, select

from corridor import audit
from corridor.adjudicate import accept_candidate
from corridor.db import Session
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    Project,
)
from corridor.changes import record_run
from corridor.export import to_pdf, to_xlsx
from corridor.llm import OpenAIClient
from corridor.pipeline import ingest_and_extract, parse_doc_date
from corridor.report import build_report, render

SLUG = "nhhip-3c2"
MEMBER = "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
LOCK = Path("corpus/manifest.lock.json")
OUT = Path("out/report.html")
PDF = Path("out/report.pdf")
XLSX = Path("out/ledger.xlsx")
IMAGES = Path("out/page-images")


def _reset(session, project: Project) -> None:
    """Make the demo repeatable by clearing the ledger, not the evidence.

    Documents and pages are deliberately left alone: ingest is idempotent,
    so re-running costs nothing, and a demo that wipes the evidence store
    would destroy whatever `make ingest` loaded. Originals are never
    deleted anywhere in this system.
    """
    dep_ids = select(Dependency.id).where(Dependency.project_id == project.id)
    candidate_ids = select(Candidate.id).where(Candidate.project_id == project.id)
    session.execute(delete(Assertion).where(Assertion.dependency_id.in_(dep_ids)))
    session.execute(
        delete(EvidenceLink).where(EvidenceLink.dependency_id.in_(dep_ids))
    )
    # Scoped by entity type as well as id. `entity_id` alone is not a key —
    # the column holds Dependency, Candidate and Milestone ids in one
    # namespace, so a demo reset was deleting real Milestone history whose
    # numeric id happened to collide with a demo Dependency's.
    session.execute(
        delete(AuditLog).where(
            (AuditLog.entity_type == audit.DEPENDENCY)
            & AuditLog.entity_id.in_(dep_ids)
        )
    )
    session.execute(
        delete(AuditLog).where(
            (AuditLog.entity_type == audit.CANDIDATE)
            & AuditLog.entity_id.in_(candidate_ids)
        )
    )
    session.execute(delete(Candidate).where(Candidate.project_id == project.id))
    session.execute(delete(Dependency).where(Dependency.project_id == project.id))


def main(limit: int | None = None) -> int:
    if not LOCK.exists():
        print("no corpus/manifest.lock.json — run `make corpus` first", file=sys.stderr)
        return 1

    lock = json.loads(LOCK.read_text())
    record = next(
        (r for r in lock["sources"].values() if r.get("member") == MEMBER), None
    )
    if record is None or not record.get("local_path"):
        print(f"{MEMBER} not in the lockfile — run `make corpus`", file=sys.stderr)
        return 1

    started = time.time()
    with Session() as session:
        project = session.scalars(
            select(Project).where(Project.slug == SLUG)
        ).first()
        if project is None:
            project = Project(
                slug=SLUG,
                name="NHHIP Segment 3C-2",
                agency="TxDOT",
                is_synthetic=False,
            )
            session.add(project)
            session.flush()
        _reset(session, project)

        # Needs OPENAI_API_KEY since #63: there is one extraction path now
        # and it reads the page with a model.
        client = OpenAIClient()
        document, candidates = ingest_and_extract(
            session,
            project_id=project.id,
            path=record["local_path"],
            images_dir=IMAGES,
            client=client,
            filename=MEMBER,
            source_url=record.get("archive_url") or record.get("url"),
            retrieved_at=record.get("retrieved_at"),
            doc_date=parse_doc_date(record.get("doc_date")),
        )
        print(f"ingested   {document.filename}  {document.pages} pages")

        verified = sum(1 for c in candidates if c.citations_verified)
        print(
            f"extracted  {len(candidates)} candidates  "
            f"{verified}/{len(candidates)} citations verified"
        )

        chosen = candidates[:limit] if limit else candidates
        for candidate in chosen:
            accept_candidate(session, candidate, actor="demo")
        print(f"adjudicated {len(chosen)} accepted")

        report = build_report(session, project.id)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(render(report))
        to_xlsx(session, project.id, XLSX, evaluation=report.evaluation)
        try:
            to_pdf(OUT.read_text(), PDF)
            pdf_note = f" · {PDF}"
        except Exception as exc:  # WeasyPrint needs native libs
            pdf_note = f" · PDF skipped ({type(exc).__name__})"

        # Snapshot last, so the next report can say what changed.
        record_run(
            session,
            project.id,
            output_path=str(OUT),
            evaluation=report.evaluation,
        )
        session.commit()

    print(
        f"report     {OUT}{pdf_note} · {XLSX}\n"
        f"           {len(report.cells)} cells, every one cited  "
        f"({time.time() - started:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
