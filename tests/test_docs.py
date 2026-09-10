import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from corridor.docs import get_page, list_documents
from corridor.models import (
    Document,
    DocumentQuarantine,
    DocumentRenditionDerivation,
    Project,
)
from corridor.pipeline import ingest_manifest

from pdf_fixture_support import PdfFixture


@pytest.fixture
def project(session):
    p = Project(slug="docs-test", name="Docs Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def make_pdf(path, lines):
    fixture = PdfFixture()
    # Size this synthetic canvas to its authored lines. Full-page whitespace
    # added rendering cost to every manifest/navigation test without adding
    # a document behavior assertion.
    page = fixture.add_page(height=max(180, 120 + 30 * len(lines)))
    for i, line in enumerate(lines):
        page.text((72, 100 + 30 * i), line)
    return fixture.save(path)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def lockfile(tmp_path):
    good = make_pdf(
        tmp_path / "matrix.pdf",
        [
            "Utility Owner: AT&T Texas (SWBT) - Telecom - underground fiber",
            "STA 1149+00 to STA 1153+17, offset 303 L/R, crossing IH 69",
        ],
    )
    other = make_pdf(
        tmp_path / "agreement.pdf",
        ["Joint Project Agreement between TxDOT and CenterPoint Energy, 1963"],
    )
    lock = {
        "project": "docs-test",
        "sources": {
            "https://example.gov/utilities.zip::matrix.pdf": {
                "sha256": file_sha256(good),
                "local_path": str(good),
                "member": "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
                "archive_url": "https://example.gov/utilities.zip",
                "doc_type": "matrix",
                "doc_date": "2026-02-13",
                "retrieved_at": "2026-08-02T20:00:00+00:00",
            },
            "https://example.gov/agreement.pdf": {
                "sha256": file_sha256(other),
                "local_path": str(other),
                "doc_type": "agreement",
                "doc_date": None,
                "retrieved_at": "2026-08-02T20:00:00+00:00",
            },
            # A failed fetch: present in the lockfile, nothing on disk.
            "https://example.gov/missing.pdf": {
                "sha256": None,
                "local_path": None,
                "http_status": 404,
                "doc_type": "minutes",
            },
        },
    }
    path = tmp_path / "manifest.lock.json"
    path.write_text(json.dumps(lock))
    return path


def test_ingesting_a_manifest_loads_every_fetched_source(
    session, project, lockfile, tmp_path
):
    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    assert len(documents) == 2
    assert all(d.parse_status == "parsed" for d in documents)


def test_ingest_registers_original_and_converted_rendition_once(
    session, project, tmp_path
):
    from openpyxl import Workbook

    original = tmp_path / "test-hole-index.xls"
    original.write_bytes(b"retained legacy XLS bytes")
    converted = tmp_path / "test-hole-index.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["TEST HOLE #", "UTILITY OWNER", "STATION"])
    sheet.append(["169-A", "VERIZON", "147+64.72"])
    book.save(converted)
    source_sha = file_sha256(original)
    derived_sha = file_sha256(converted)
    lock = {
        "project": project.slug,
        "sources": {
            "https://example.gov/archive.zip::test-hole-index.xls": {
                "sha256": source_sha,
                "local_path": str(original),
                "member": "test-hole-index.xls",
                "archive_url": "https://example.gov/archive.zip",
                "doc_type": "plan",
                "registry_id": "test-hole-index-xls",
                "curation_status": "proposed",
            },
            "derived:test-hole-index:xlsx": {
                "sha256": derived_sha,
                "local_path": str(converted),
                "member": "test-hole-index.xlsx",
                "doc_type": "plan",
                "registry_id": "test-hole-index-xlsx",
                "curation_status": "proposed",
                "derivation": {
                    "kind": "format_conversion",
                    "source_registry_id": "test-hole-index-xls",
                    "source_sha256": source_sha,
                    "tool": "corridor.xls-to-xlsx",
                    "tool_version": "1",
                },
            },
        },
    }
    lock_path = tmp_path / "renditions.lock.json"
    lock_path.write_text(json.dumps(lock))

    assert ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
    ) == []
    first = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
        include_proposed=True,
    )
    second = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
        include_proposed=True,
    )

    assert len(first) == len(second) == 2
    source = session.scalar(
        select(Document).where(Document.registry_id == "test-hole-index-xls")
    )
    derived = session.scalar(
        select(Document).where(Document.registry_id == "test-hole-index-xlsx")
    )
    assert source.parse_status == "failed"
    assert derived.parse_status == "parsed"
    assert source.superseded_by is None and derived.superseded_by is None
    [receipt] = session.scalars(
        select(DocumentRenditionDerivation).where(
            DocumentRenditionDerivation.project_id == project.id
        )
    ).all()
    assert receipt.source_document_id == source.id
    assert receipt.derived_document_id == derived.id
    assert receipt.source_sha256 == source_sha
    assert receipt.derived_sha256 == derived_sha
    assert receipt.tool == "corridor.xls-to-xlsx"


def test_ingest_rejects_lockfile_hash_mismatch_before_binding_registry_id(
    session, project, tmp_path
):
    document_path = make_pdf(tmp_path / "swapped.pdf", ["Unexpected bytes"])
    lock_path = tmp_path / "swapped.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "project": project.slug,
                "sources": {
                    "https://example.gov/expected.pdf": {
                        "sha256": "0" * 64,
                        "local_path": str(document_path),
                        "doc_type": "matrix",
                        "registry_id": "matrix-stable-id",
                    }
                },
            }
        )
    )

    with pytest.raises(ValueError, match="lockfile sha256"):
        ingest_manifest(
            session,
            project_id=project.id,
            lock_path=lock_path,
            images_dir=tmp_path / "images",
        )

    assert session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).all() == []


def test_ingest_registers_structured_supersession_after_all_documents_exist(
    session, project, tmp_path
):
    index = make_pdf(tmp_path / "index.pdf", ["R1 Replaced on 2026-02-13"])
    first = make_pdf(tmp_path / "r1.pdf", ["Matrix revision one"])
    second = make_pdf(tmp_path / "r2.pdf", ["Matrix revision two"])
    lock = {
        "project": project.slug,
        "sources": {
            "https://example.gov/index.pdf": {
                "sha256": file_sha256(index),
                "local_path": str(index),
                "doc_type": "other",
                "registry_id": "rid-index",
            },
            "https://example.gov/r1.pdf": {
                "sha256": file_sha256(first),
                "local_path": str(first),
                "doc_type": "matrix",
                "registry_id": "matrix-r1",
                "supersession": {
                    "predecessor_registry_id": "matrix-r1",
                    "successor_registry_id": "matrix-r2",
                    "replacement_date": "2026-02-13",
                    "source_registry_id": "rid-index",
                    "source_page": 1,
                },
            },
            "https://example.gov/r2.pdf": {
                "sha256": file_sha256(second),
                "local_path": str(second),
                "doc_type": "matrix",
                "registry_id": "matrix-r2",
            },
        },
    }
    lock_path = tmp_path / "registry.lock.json"
    lock_path.write_text(json.dumps(lock))

    ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
    )

    predecessor = session.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.registry_id == "matrix-r1",
        )
    )
    successor = session.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.registry_id == "matrix-r2",
        )
    )
    source = session.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.registry_id == "rid-index",
        )
    )
    assert predecessor.superseded_by == successor.id
    assert predecessor.superseded_on == date(2026, 2, 13)
    assert predecessor.supersession_source_document_id == source.id
    assert predecessor.supersession_source_page == 1


@pytest.mark.parametrize(
    ("omitted", "message"),
    [
        ("source_registry_id", "source_registry_id must be non-empty"),
        ("source_page", "source_page must be positive"),
    ],
)
def test_ingest_rejects_a_supersession_with_no_source_pointer(
    session, project, tmp_path, omitted, message
):
    """The declaration names the index document and page, or it is refused.

    The chain is checkable rather than cited (ADR-0015): the pointer is what
    keeps the index one click away, so an edge that carries none is a guess
    about lineage — the one thing supersession may never be.
    """
    first = make_pdf(tmp_path / "r1.pdf", ["Matrix revision one"])
    declaration = {
        "predecessor_registry_id": "matrix-r1",
        "successor_registry_id": "matrix-r2",
        "replacement_date": "2026-02-13",
        "source_registry_id": "rid-index",
        "source_page": 1,
    }
    del declaration[omitted]
    lock_path = tmp_path / "uncited.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "project": project.slug,
                "sources": {
                    "https://example.gov/r1.pdf": {
                        "sha256": file_sha256(first),
                        "local_path": str(first),
                        "doc_type": "matrix",
                        "registry_id": "matrix-r1",
                        "supersession": declaration,
                    }
                },
            }
        )
    )

    with pytest.raises(ValueError, match=message):
        ingest_manifest(
            session,
            project_id=project.id,
            lock_path=lock_path,
            images_dir=tmp_path / "images",
        )


def test_reingesting_an_unidentified_project_backfills_ids_and_the_chain(
    session, project, tmp_path
):
    """The live case: documents ingested before the lockfile named them.

    NHHIP's revisions were ingested when the lockfile carried no registry
    ids and no chain, so the run that registers supersession is a re-run
    over documents that already exist. It backfills the identities onto
    those same rows, declares each edge once, and says the same thing again
    on a third pass.
    """
    index = make_pdf(tmp_path / "index.pdf", ["R1 Replaced on 2026-02-13"])
    first = make_pdf(tmp_path / "r1.pdf", ["Matrix revision one"])
    second = make_pdf(tmp_path / "r2.pdf", ["Matrix revision two"])

    def source(path, **extra):
        return {
            "sha256": file_sha256(path),
            "local_path": str(path),
            "doc_type": "matrix",
            **extra,
        }

    unidentified = {
        "project": project.slug,
        "sources": {
            "https://example.gov/index.pdf": source(index, doc_type="other"),
            "https://example.gov/r1.pdf": source(first),
            "https://example.gov/r2.pdf": source(second),
        },
    }
    identified = {
        "project": project.slug,
        "sources": {
            "https://example.gov/index.pdf": source(
                index, doc_type="other", registry_id="rid-index"
            ),
            "https://example.gov/r1.pdf": source(
                first,
                registry_id="matrix-r1",
                supersession={
                    "predecessor_registry_id": "matrix-r1",
                    "successor_registry_id": "matrix-r2",
                    "replacement_date": "2026-02-13",
                    "source_registry_id": "rid-index",
                    "source_page": 1,
                },
            ),
            "https://example.gov/r2.pdf": source(second, registry_id="matrix-r2"),
        },
    }

    def ingest(lock, name):
        lock_path = tmp_path / name
        lock_path.write_text(json.dumps(lock))
        return ingest_manifest(
            session,
            project_id=project.id,
            lock_path=lock_path,
            images_dir=tmp_path / "images",
        )

    def registry():
        return [
            (
                row.id,
                row.registry_id,
                row.superseded_by,
                row.superseded_on,
                row.supersession_source_document_id,
                row.supersession_source_page,
            )
            for row in session.execute(
                select(
                    Document.id,
                    Document.registry_id,
                    Document.superseded_by,
                    Document.superseded_on,
                    Document.supersession_source_document_id,
                    Document.supersession_source_page,
                )
                .where(Document.project_id == project.id)
                .order_by(Document.id)
            ).all()
        ]

    before = ingest(unidentified, "unidentified.lock.json")
    assert [document.registry_id for document in before] == [None, None, None]

    ingest(identified, "identified.lock.json")
    after = registry()

    # The same three rows, now identified — a re-ingest never mints a second
    # document for bytes already in the store.
    assert [row[0] for row in after] == [document.id for document in before]
    assert [row[1] for row in after] == ["rid-index", "matrix-r1", "matrix-r2"]
    predecessor = next(row for row in after if row[1] == "matrix-r1")
    successor = next(row for row in after if row[1] == "matrix-r2")
    source_document = next(row for row in after if row[1] == "rid-index")
    assert predecessor[2] == successor[0]
    assert predecessor[3] == date(2026, 2, 13)
    assert predecessor[4] == source_document[0]
    assert predecessor[5] == 1

    ingest(identified, "identified-again.lock.json")
    assert registry() == after


def test_missing_supersession_participant_defers_edges_without_losing_documents(
    session, project, tmp_path
):
    index = make_pdf(tmp_path / "index.pdf", ["R1 Replaced on 2026-02-13"])
    first = make_pdf(tmp_path / "r1.pdf", ["Matrix revision one"])
    lock_path = tmp_path / "incomplete-registry.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "project": project.slug,
                "sources": {
                    "https://example.gov/index.pdf": {
                        "sha256": file_sha256(index),
                        "local_path": str(index),
                        "doc_type": "other",
                        "registry_id": "rid-index",
                    },
                    "https://example.gov/r1.pdf": {
                        "sha256": file_sha256(first),
                        "local_path": str(first),
                        "doc_type": "matrix",
                        "registry_id": "matrix-r1",
                        "supersession": {
                            "predecessor_registry_id": "matrix-r1",
                            "successor_registry_id": "matrix-r2",
                            "replacement_date": "2026-02-13",
                            "source_registry_id": "rid-index",
                            "source_page": 1,
                        },
                    },
                    "https://example.gov/r2.pdf": {
                        "sha256": None,
                        "local_path": None,
                        "http_status": 503,
                        "doc_type": "matrix",
                        "registry_id": "matrix-r2",
                    },
                },
            }
        )
    )

    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
    )

    assert {document.registry_id for document in documents} == {
        "rid-index",
        "matrix-r1",
    }
    predecessor = session.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.registry_id == "matrix-r1",
        )
    )
    assert predecessor.superseded_by is None
    assert predecessor.superseded_on is None
    assert predecessor.supersession_source_document_id is None
    assert predecessor.supersession_source_page is None


def test_missing_source_page_defers_edges_without_losing_documents(
    session, project, tmp_path
):
    index = tmp_path / "broken-index.pdf"
    index.write_bytes(b"not a parseable PDF")
    first = make_pdf(tmp_path / "r1.pdf", ["Matrix revision one"])
    second = make_pdf(tmp_path / "r2.pdf", ["Matrix revision two"])
    lock_path = tmp_path / "unparsed-registry.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "project": project.slug,
                "sources": {
                    "https://example.gov/index.pdf": {
                        "sha256": file_sha256(index),
                        "local_path": str(index),
                        "doc_type": "other",
                        "registry_id": "rid-index",
                    },
                    "https://example.gov/r1.pdf": {
                        "sha256": file_sha256(first),
                        "local_path": str(first),
                        "doc_type": "matrix",
                        "registry_id": "matrix-r1",
                        "supersession": {
                            "predecessor_registry_id": "matrix-r1",
                            "successor_registry_id": "matrix-r2",
                            "replacement_date": "2026-02-13",
                            "source_registry_id": "rid-index",
                            "source_page": 1,
                        },
                    },
                    "https://example.gov/r2.pdf": {
                        "sha256": file_sha256(second),
                        "local_path": str(second),
                        "doc_type": "matrix",
                        "registry_id": "matrix-r2",
                    },
                },
            }
        )
    )

    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lock_path,
        images_dir=tmp_path / "images",
    )

    assert len(documents) == 3
    source = next(
        document for document in documents if document.registry_id == "rid-index"
    )
    assert source.parse_status == "failed"
    predecessor = next(
        document for document in documents if document.registry_id == "matrix-r1"
    )
    assert predecessor.superseded_by is None
    assert predecessor.supersession_source_document_id is None


def test_a_failed_fetch_is_not_ingested_as_though_it_worked(
    session, project, lockfile, tmp_path
):
    """The 404 stays visible in the lockfile rather than becoming a document."""
    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    assert "missing.pdf" not in {d.filename for d in documents}


def test_provenance_survives_the_ingest_boundary(session, project, lockfile, tmp_path):
    ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )
    rows = list_documents(session, project.id)
    matrix = next(r for r in rows if r.doc_type == "matrix")

    # The archive member name, not the content-addressed hash on disk.
    assert matrix.filename == "nhhip-seg3c2-utilities-inventory-2-13-2026.pdf"
    assert matrix.doc_date == "2026-02-13"
    assert matrix.source_url == "https://example.gov/utilities.zip"


def test_a_nested_member_records_only_its_leaf_path(session, project, tmp_path):
    """The inner zip's own name is noise in a citation."""
    import json as _json

    pdf = make_pdf(
        tmp_path / "note.pdf",
        ["Air Liquide coordination meeting notes, biweekly cadence check-in"],
    )
    lock = {
        "project": "docs-test",
        "sources": {
            "https://example.gov/u.zip::Coordination/Notes.zip::Meeting Notes/Air Liquide/2024.07.30 notes.pdf": {
                "sha256": file_sha256(pdf),
                "local_path": str(pdf),
                "member": "Coordination/Notes.zip::Meeting Notes/Air Liquide/2024.07.30 notes.pdf",
                "archive_url": "https://example.gov/u.zip",
                "doc_type": "minutes",
                "doc_date": "2024-07-30",
                "retrieved_at": "2026-08-03T16:00:00+00:00",
            },
        },
    }
    path = tmp_path / "nested.lock.json"
    path.write_text(_json.dumps(lock))

    [doc] = ingest_manifest(
        session, project_id=project.id, lock_path=path, images_dir=tmp_path / "i"
    )
    assert doc.filename == "Meeting Notes/Air Liquide/2024.07.30 notes.pdf"


def test_reingesting_a_manifest_is_a_noop(session, project, lockfile, tmp_path):
    first = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    second = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    assert [d.id for d in first] == [d.id for d in second]
    assert len(list_documents(session, project.id)) == 2


def test_bulk_ingest_skips_locks_opted_out_of_default_materialization(
    session, tmp_path, monkeypatch, capsys
):
    import corridor.docs as docs_module

    tracked = make_pdf(
        tmp_path / "tracked.pdf",
        ["Tracked project matrix text long enough to stay above OCR fallback"],
    )
    layout = make_pdf(
        tmp_path / "layout.pdf",
        ["Layout evidence text long enough to stay above OCR fallback"],
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "tracked.lock.json").write_text(
        json.dumps(
            {
                "project": "tracked-project",
                "name": "Tracked Project",
                "agency": "TxDOT",
                "ingest_by_default": True,
                "sources": {
                    "tracked": {
                        "sha256": file_sha256(tracked),
                        "local_path": str(tracked),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )
    (corpus_dir / "cross-agency.lock.json").write_text(
        json.dumps(
            {
                "project": "layout-evidence",
                "name": "Layout Evidence",
                "agency": "various",
                "ingest_by_default": False,
                "sources": {
                    "layout": {
                        "sha256": file_sha256(layout),
                        "local_path": str(layout),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        docs_module, "WorkerSession", sessionmaker(bind=session.get_bind())
    )

    assert docs_module.main(["ingest"]) == 0

    out = capsys.readouterr().out
    assert "tracked-project: 1/1 parsed from tracked.lock.json" in out
    assert "layout-evidence: skipped bulk ingest (ingest_by_default is false)" in out
    assert "1 documents total" in out

    projects = {p.slug for p in session.scalars(select(Project))}
    assert "tracked-project" in projects
    assert "layout-evidence" not in projects


def test_explicit_slug_ingest_overrides_the_default_skip_policy(
    session, tmp_path, monkeypatch, capsys
):
    import corridor.docs as docs_module

    layout = make_pdf(
        tmp_path / "layout.pdf",
        ["Layout evidence text long enough to stay above OCR fallback"],
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "cross-agency.lock.json").write_text(
        json.dumps(
            {
                "project": "layout-evidence",
                "name": "Layout Evidence",
                "agency": "various",
                "ingest_by_default": False,
                "sources": {
                    "layout": {
                        "sha256": file_sha256(layout),
                        "local_path": str(layout),
                        "doc_type": "matrix",
                        "retrieved_at": "2026-08-05T00:00:00+00:00",
                    }
                },
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        docs_module, "WorkerSession", sessionmaker(bind=session.get_bind())
    )

    assert docs_module.main(["ingest", "layout-evidence"]) == 0

    out = capsys.readouterr().out
    assert "layout-evidence: 1/1 parsed from cross-agency.lock.json" in out
    assert "skipped bulk ingest" not in out

    projects = {p.slug for p in session.scalars(select(Project))}
    assert "layout-evidence" in projects


def test_listing_reports_pages_and_ocr_counts(session, project, lockfile, tmp_path):
    ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    rows = list_documents(session, project.id)
    assert all(r.pages > 0 for r in rows)
    # These have real text layers; none should be OCR.
    assert all(r.ocr_pages == 0 for r in rows)


def test_any_page_is_retrievable_with_text_and_image(
    session, project, lockfile, tmp_path
):
    """M1's bar: every file retrievable with page text and page image."""
    documents = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    page = get_page(session, documents[0].id, 1)
    assert page.text.strip()
    assert page.image_path and open(page.image_path, "rb").read(4) == b"\x89PNG"
    assert page.text_source in ("text_layer", "ocr")


def test_asking_for_a_page_that_does_not_exist_raises(
    session, project, lockfile, tmp_path
):
    documents = ingest_manifest(
        session, project_id=project.id, lock_path=lockfile, images_dir=tmp_path / "i"
    )
    with pytest.raises(LookupError):
        get_page(session, documents[0].id, 999)


# ------------------ which form of a document is cited (ADR-0005, #60)


def test_the_spreadsheet_outranks_its_own_printout(session, project):
    """ADR-0005's rule: the structured original is the Document of Record.

    The PDF is a printout of the spreadsheet, and every defect ADR-0004
    catalogues is damage done in the printing. Where both render the same
    document, Evidence cites the one the damage did not happen to.
    """
    from corridor.docs import document_of_record

    printout = _doc(session, project, "ucm.pdf", "a", date(2026, 2, 13))
    original = _doc(session, project, "ucm.xlsx", "b", date(2026, 2, 13))

    assert document_of_record([printout, original]) is original


def test_a_newer_printout_outranks_a_stale_spreadsheet():
    """The rendition ordering ADR-0005 is explicit about.

    "A stale spreadsheet must not outrank a newer PDF. Precedence is by
    format only where both render the same registered document; rendition
    date wins first, and the two rules have to be applied in that order."

    Applied the other way round, a February PDF loses to a spreadsheet from
    the previous June. Declared Supersession is a separate registry relation;
    this helper only ranks caller-supplied equivalent forms.
    """
    from corridor.docs import document_of_record

    stale = _fake("ucm.xlsx", date(2025, 6, 20))
    current = _fake("ucm.pdf", date(2026, 2, 13))

    assert document_of_record([stale, current]) is current


def test_format_breaks_a_tie_only_within_one_date():
    from corridor.docs import document_of_record

    old_sheet = _fake("old.xlsx", date(2025, 6, 20))
    new_sheet = _fake("new.xlsx", date(2026, 2, 13))
    new_pdf = _fake("new.pdf", date(2026, 2, 13))

    assert document_of_record([old_sheet, new_pdf, new_sheet]) is new_sheet


def test_a_document_with_no_date_never_outranks_a_dated_one():
    """An undated document is not a current one. Sorting it as though its
    date were today would let a file nobody dated supersede the revision
    the project actually issued."""
    from corridor.docs import document_of_record

    undated = _fake("ucm.xlsx", None)
    dated = _fake("ucm.pdf", date(2025, 6, 20))

    assert document_of_record([undated, dated]) is dated


def test_no_documents_is_no_record():
    from corridor.docs import document_of_record

    assert document_of_record([]) is None


class _fake:
    """A Document-shaped stand-in: the rule reads two attributes."""

    def __init__(self, filename, doc_date):
        self.filename = filename
        self.doc_date = doc_date


def _doc(session, project, filename, sha, doc_date):
    from corridor.models import Document

    document = Document(
        project_id=project.id,
        sha256=sha * 64,
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=doc_date,
    )
    session.add(document)
    session.flush()
    return document


def test_a_registered_schedule_document_carries_a_durable_quarantine(
    session, project, tmp_path
):
    """Out of scope means unsupported, never lossy (#149).

    A Utility Work Schedule's rows relate to each other; Corridor has no
    model for that relation, and the project record says so durably rather
    than leaving the fact in an operator's memory.
    """
    schedule = make_pdf(tmp_path / "uws.pdf", ["Activity  Dependent Activity"])
    lock_path = tmp_path / "schedule.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "project": project.slug,
                "sources": {
                    "https://example.gov/uws.pdf": {
                        "sha256": file_sha256(schedule),
                        "local_path": str(schedule),
                        "doc_type": "schedule",
                    }
                },
            }
        )
    )

    for _ in range(2):
        documents = ingest_manifest(
            session,
            project_id=project.id,
            lock_path=lock_path,
            images_dir=tmp_path / "images",
        )

    [document] = documents
    quarantines = session.scalars(
        select(DocumentQuarantine).where(
            DocumentQuarantine.document_id == document.id
        )
    ).all()
    assert len(quarantines) == 1
    assert "sequencing" in quarantines[0].reason


def test_a_lock_entry_whose_store_file_is_missing_fails_soft_per_document(
    session, project, lockfile, tmp_path
):
    from pathlib import Path

    lock = json.loads(lockfile.read_text())
    rec = lock["sources"]["https://example.gov/agreement.pdf"]
    Path(rec["local_path"]).unlink()
    lockfile.write_text(json.dumps(lock))

    documents = ingest_manifest(
        session,
        project_id=project.id,
        lock_path=lockfile,
        images_dir=tmp_path / "images",
    )

    # The whole project still ingests; the hole is registered and visible.
    assert len(documents) == 2
    failed = [d for d in documents if d.parse_status == "failed"]
    parsed = [d for d in documents if d.parse_status == "parsed"]
    assert len(failed) == 1 and len(parsed) == 1
    assert failed[0].sha256 == rec["sha256"]
    assert failed[0].pages == 0
