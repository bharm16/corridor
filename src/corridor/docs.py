"""Reading the evidence store back.

M1's bar is that every file is retrievable with its page text and page
image. This is the read side of that, plus the CLI that loads a whole
manifest.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.db import Session as SessionFactory
from corridor.models import DocPage, Document, Project

LOCK = Path("corpus/manifest.lock.json")
IMAGES = Path("out/page-images")

# Suffixes that make a document the structured original rather than a
# rendering of one (ADR-0005). Kept beside the precedence rule rather than
# imported from `ingest`, because the two lists answer different questions:
# that one is "can this be read as sheets", this one is "is this the form
# the printout was made from".
STRUCTURED_SUFFIXES = {".xlsx", ".xlsm", ".csv"}


@dataclass
class DocumentSummary:
    id: int
    filename: str
    doc_type: str
    parse_status: str
    pages: int
    ocr_pages: int
    doc_date: str | None
    source_url: str | None


def list_documents(session: Session, project_id: int) -> list[DocumentSummary]:
    ocr_counts = dict(
        session.execute(
            select(DocPage.document_id, func.count())
            .where(DocPage.text_source == "ocr")
            .group_by(DocPage.document_id)
        ).all()
    )
    page_counts = dict(
        session.execute(
            select(DocPage.document_id, func.count()).group_by(DocPage.document_id)
        ).all()
    )

    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.doc_date, Document.filename)
    ).all()

    return [
        DocumentSummary(
            id=d.id,
            filename=d.filename,
            doc_type=d.doc_type,
            parse_status=d.parse_status,
            pages=page_counts.get(d.id, 0),
            ocr_pages=ocr_counts.get(d.id, 0),
            doc_date=d.doc_date.isoformat() if d.doc_date else None,
            source_url=d.source_url,
        )
        for d in documents
    ]


def get_page(session: Session, document_id: int, page_no: int) -> DocPage:
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document_id, DocPage.page_no == page_no
        )
    ).first()
    if page is None:
        raise LookupError(f"no page {page_no} of document {document_id}")
    return page


def document_of_record(documents) -> Document | None:
    """Which of these forms of one document Evidence should cite (ADR-0005).

    Two rules, and the order is the substance rather than a detail:

    1. **Supersession by date.** A February printout beats a spreadsheet
       from the previous June, because the project replaced that revision.
    2. **Then format.** Among documents of the same date, the structured
       original outranks its printed rendering — every defect ADR-0004
       catalogues is damage done in the printing, and the spreadsheet is
       the thing it was done to.

    Applied the other way round, a stale spreadsheet outranks a current
    PDF and the Ledger cites a revision the project has already replaced.
    ADR-0005 says so outright, and this function is that sentence.

    An undated document sorts below every dated one. Treating a missing
    date as today's would let a file nobody dated supersede the revision
    the project actually issued.

    **What this does not do is decide which documents are forms of one
    document.** That is supersession, it needs the RID index's "Replaced
    on" chain, and it is M8's — `documents.superseded_by` exists and
    nothing populates it yet. Callers pass a set they already know renders
    the same thing; this only ranks it.
    """
    return max(documents, key=_precedence, default=None)


def _precedence(document) -> tuple:
    return (
        # `date.min` rather than None, so an undated document is comparable
        # and always loses.
        document.doc_date or date.min,
        Path(document.filename or "").suffix.lower() in STRUCTURED_SUFFIXES,
    )


def stored_file(document: Document) -> Path | None:
    """Resolve a Document back to the file in the content-addressed store.

    Found by hash rather than by extension: the store preserves whatever
    suffix the source had so it stays browsable, and since ADR-0005 that is
    no longer always `.pdf`. One hash, one file — the name is the hash, so
    a glob cannot match two different documents.
    """
    if not document or not document.sha256:
        return None
    shard = Path(settings.corpus_store) / document.sha256[:2]
    return next(iter(sorted(shard.glob(f"{document.sha256}.*"))), None)


def stored_pdf(document: Document) -> Path | None:
    """The stored file, when it really is a PDF.

    Callers that render pages or read word boxes need this rather than
    `stored_file`: handing a workbook to PyMuPDF raises somewhere deep
    instead of saying the document is the wrong kind.
    """
    path = stored_file(document)
    return path if path and path.suffix.lower() == ".pdf" else None


def _project(session: Session, slug: str) -> Project:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise LookupError(f"no project {slug!r}")
    return project


def main(argv: list[str]) -> int:
    # Imported here rather than at module scope: `pipeline` routes a
    # document to its reader and needs `stored_file` from this module, so
    # a top-level import is a cycle. Only the CLI wants it anyway.
    from corridor.pipeline import ingest_manifest

    command = argv[0] if argv else "list"

    with SessionFactory() as session:
        if command == "ingest":
            # Every lockfile in corpus/, one project each. The Project row is
            # created from the lock header when it does not exist yet.
            only = argv[1] if len(argv) > 1 else None
            total = 0
            for lock_path in sorted(Path("corpus").glob("*.lock.json")):
                header = __import__("json").loads(lock_path.read_text())
                slug = header.get("project")
                if not slug or (only and slug != only):
                    continue
                project = session.scalars(
                    select(Project).where(Project.slug == slug)
                ).first()
                if project is None:
                    project = Project(
                        slug=slug,
                        name=header.get("name") or slug,
                        agency=header.get("agency"),
                        is_synthetic=False,
                    )
                    session.add(project)
                    session.flush()
                documents = ingest_manifest(
                    session,
                    project_id=project.id,
                    lock_path=lock_path,
                    images_dir=IMAGES,
                )
                session.commit()
                parsed = sum(1 for d in documents if d.parse_status == "parsed")
                print(
                    f"{slug}: {parsed}/{len(documents)} parsed from {lock_path.name}",
                    flush=True,
                )
                total += len(documents)
            print(f"{total} documents total")
            return 0

        if command == "list":
            slug = argv[1] if len(argv) > 1 else "nhhip-3c2"
            rows = list_documents(session, _project(session, slug).id)
            print(f"{'id':>5} {'pages':>6} {'ocr':>5}  {'type':<14} {'date':<11} file")
            for r in rows:
                print(
                    f"{r.id:>5} {r.pages:>6} {r.ocr_pages:>5}  {r.doc_type:<14} "
                    f"{(r.doc_date or '—'):<11} {r.filename}"
                )
            return 0

        if command == "page":
            document_id, page_no = int(argv[1]), int(argv[2])
            page = get_page(session, document_id, page_no)
            print(f"document {document_id} page {page_no}  [{page.text_source}]")
            print(f"image: {page.image_path}")
            print("-" * 60)
            print(page.text[:2000])
            return 0

    print("usage: python -m corridor.docs [ingest|list|page <doc> <page>]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
