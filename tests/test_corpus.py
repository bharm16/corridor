import json
from datetime import date

import httpx
import pytest

from corridor.corpus import fetch_all, load_manifest

MANIFEST = """
project: nhhip-3c2
agency: TxDOT
sources:
  - url: https://example.gov/ucm.pdf
    doc_type: matrix
    role: spine
    title: "Utility Conflict Matrix, Rev C"
    doc_date: 2026-02-13
    notes: "Appendix D of the RID"
  - url: https://example.gov/minutes.pdf
    doc_type: minutes
    role: stream
    title: "Utility coordination meeting"
"""


def write_manifest(tmp_path, text=MANIFEST):
    p = tmp_path / "manifest.yaml"
    p.write_text(text)
    return p


def transport(bodies: dict[str, bytes], status: int = 200):
    """Serves `bodies` by URL. Mutate the dict between runs to force drift."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in bodies:
            return httpx.Response(404)
        return httpx.Response(
            status, content=bodies[url], headers={"content-type": "application/pdf"}
        )

    return httpx.MockTransport(handler)


def run(tmp_path, bodies, status=200):
    client = httpx.Client(transport=transport(bodies, status))
    return fetch_all(
        load_manifest(write_manifest(tmp_path)),
        store=tmp_path / "files",
        lock_path=tmp_path / "manifest.lock.json",
        client=client,
        delay=0.0,
    )


BODIES = {
    "https://example.gov/ucm.pdf": b"matrix rev c",
    "https://example.gov/minutes.pdf": b"minutes text",
}


def test_manifest_parses_sources(tmp_path):
    m = load_manifest(write_manifest(tmp_path))
    assert m.project == "nhhip-3c2"
    assert m.agency == "TxDOT"
    assert len(m.sources) == 2
    first = m.sources[0]
    assert first.doc_type == "matrix"
    assert first.role == "spine"
    assert first.doc_date == date(2026, 2, 13)
    assert m.sources[1].doc_date is None


def test_manifest_rejects_an_unknown_doc_type(tmp_path):
    bad = MANIFEST.replace("doc_type: matrix", "doc_type: spreadsheet")
    with pytest.raises(ValueError, match="spreadsheet"):
        load_manifest(write_manifest(tmp_path, bad))


def test_fetch_stores_content_addressed_and_records_provenance(tmp_path):
    summary = run(tmp_path, dict(BODIES))
    assert len(summary.fetched) == 2

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    rec = lock["sources"]["https://example.gov/ucm.pdf"]

    import hashlib

    expected = hashlib.sha256(b"matrix rev c").hexdigest()
    assert rec["sha256"] == expected
    assert rec["bytes"] == len(b"matrix rev c")
    assert rec["http_status"] == 200
    assert rec["content_type"] == "application/pdf"
    assert rec["retrieved_at"]

    # Content-addressed: sharded by the first two hex chars, extension kept
    # so the store stays browsable.
    stored = tmp_path / "files" / expected[:2] / f"{expected}.pdf"
    assert stored.read_bytes() == b"matrix rev c"
    assert rec["local_path"].endswith(f"{expected}.pdf")


def test_refetch_of_unchanged_bytes_is_a_noop(tmp_path):
    run(tmp_path, dict(BODIES))
    second = run(tmp_path, dict(BODIES))
    assert second.fetched == []
    assert len(second.skipped) == 2


def test_drift_keeps_both_files_and_records_history(tmp_path):
    import hashlib

    run(tmp_path, dict(BODIES))
    old_sha = hashlib.sha256(b"matrix rev c").hexdigest()

    revised = dict(BODIES)
    revised["https://example.gov/ucm.pdf"] = b"matrix rev d"
    summary = run(tmp_path, revised)
    new_sha = hashlib.sha256(b"matrix rev d").hexdigest()

    assert summary.drifted == ["https://example.gov/ucm.pdf"]

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    rec = lock["sources"]["https://example.gov/ucm.pdf"]
    assert rec["sha256"] == new_sha
    assert [h["sha256"] for h in rec["history"]] == [old_sha]

    # Nothing is ever deleted: the superseded revision is still on disk.
    # This is what makes M8's supersession work possible.
    assert (tmp_path / "files" / old_sha[:2] / f"{old_sha}.pdf").exists()
    assert (tmp_path / "files" / new_sha[:2] / f"{new_sha}.pdf").exists()


def test_a_failed_source_is_recorded_without_aborting_the_run(tmp_path):
    partial = {"https://example.gov/minutes.pdf": b"minutes text"}
    summary = run(tmp_path, partial)

    assert summary.failed == ["https://example.gov/ucm.pdf"]
    assert len(summary.fetched) == 1

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    assert lock["sources"]["https://example.gov/ucm.pdf"]["http_status"] == 404
    # A failure must not masquerade as a successful retrieval.
    assert lock["sources"]["https://example.gov/ucm.pdf"]["sha256"] is None
