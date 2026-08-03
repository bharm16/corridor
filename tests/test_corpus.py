import hashlib
import io
import json
import zipfile
from datetime import date

import httpx
import pytest

from corridor.corpus import BROWSER_UA, USER_AGENT, fetch_all, load_manifest

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


# --------------------------------------------------------------------------
# Host quirks and archive members (#6)
#
# Every quirk below returns something that *looks* like an answer rather than
# erroring, which is why each one needs a test rather than a comment.
# --------------------------------------------------------------------------

ARCHIVE_MANIFEST = """
project: nhhip-3c2
agency: TxDOT
sources:
  - url: https://example.gov/utilities.zip
    member: ucm-2-13-2026.pdf
    doc_type: matrix
    role: spine
    title: "Utility Conflict Matrix, Rev 2/13/2026"
    doc_date: 2026-02-13
"""

UCM_BYTES = b"%PDF-1.6 utility conflict matrix rev 2-13-2026"


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def ranged_transport(bodies, extra_headers=None, ua_required=None):
    """Serves byte ranges, and can gate a URL behind a specific user agent."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if ua_required and url in ua_required:
            if request.headers.get("user-agent") != ua_required[url]:
                return httpx.Response(404, content=b"<html>not found</html>")
        if url not in bodies:
            return httpx.Response(404, content=b"<html>not found</html>")

        body = bodies[url]
        headers = {"content-type": "application/octet-stream"}
        headers.update((extra_headers or {}).get(url, {}))

        rng = request.headers.get("range")
        if rng:
            start, _, end = rng.removeprefix("bytes=").partition("-")
            start = int(start)
            end = int(end) if end else len(body) - 1
            chunk = body[start : end + 1]
            headers["content-range"] = f"bytes {start}-{end}/{len(body)}"
            return httpx.Response(206, content=chunk, headers=headers)

        return httpx.Response(200, content=body, headers=headers)

    return httpx.MockTransport(handler)


def run_with(tmp_path, manifest_text, transport):
    client = httpx.Client(transport=transport)
    return fetch_all(
        load_manifest(write_manifest(tmp_path, manifest_text)),
        store=tmp_path / "files",
        lock_path=tmp_path / "manifest.lock.json",
        client=client,
        delay=0.0,
    )


def test_a_browser_ua_retry_recovers_a_box_style_404(tmp_path):
    """Box 404s a non-browser user agent, and the 404 body is real HTML."""
    gated = "https://example.gov/ucm.pdf"
    summary = run_with(
        tmp_path,
        MANIFEST,
        ranged_transport(dict(BODIES), ua_required={gated: BROWSER_UA}),
    )
    assert gated in summary.fetched
    assert summary.failed == []


def test_an_html_error_page_is_never_stored_as_a_document(tmp_path):
    """A 200 is not enough. Wayback and TxDOT both serve HTML error bodies."""
    bodies = dict(BODIES)
    bodies["https://example.gov/ucm.pdf"] = b"<html>Page not found</html>"
    summary = run_with(
        tmp_path,
        MANIFEST,
        ranged_transport(
            bodies,
            extra_headers={
                "https://example.gov/ucm.pdf": {"content-type": "text/html"}
            },
        ),
    )
    assert summary.failed == ["https://example.gov/ucm.pdf"]
    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    assert lock["sources"]["https://example.gov/ucm.pdf"]["sha256"] is None


def test_a_truncated_capture_is_rejected(tmp_path):
    """Wayback serves partial captures with a 200 and the true size in a header."""
    summary = run_with(
        tmp_path,
        MANIFEST,
        ranged_transport(
            dict(BODIES),
            extra_headers={
                "https://example.gov/ucm.pdf": {
                    "x-archive-orig-x-crawler-content-length": "999999"
                }
            },
        ),
    )
    assert summary.failed == ["https://example.gov/ucm.pdf"]


def test_archive_member_is_extracted_and_hashed(tmp_path):
    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES, "other.pdf": b"unrelated"})
    summary = run_with(
        tmp_path,
        ARCHIVE_MANIFEST,
        ranged_transport({"https://example.gov/utilities.zip": archive}),
    )
    assert len(summary.fetched) == 1

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    rec = lock["sources"]["https://example.gov/utilities.zip::ucm-2-13-2026.pdf"]

    # The stored artifact is the member, not the archive.
    assert rec["sha256"] == hashlib.sha256(UCM_BYTES).hexdigest()
    assert rec["member"] == "ucm-2-13-2026.pdf"
    assert rec["bytes"] == len(UCM_BYTES)
    from pathlib import Path

    assert Path(rec["local_path"]).read_bytes() == UCM_BYTES


def test_unchanged_archive_member_is_skipped_via_crc(tmp_path):
    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES})
    bodies = {"https://example.gov/utilities.zip": archive}
    run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))
    second = run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))
    assert second.fetched == []
    assert len(second.skipped) == 1


def test_a_missing_archive_member_is_reported(tmp_path):
    archive = make_zip({"something-else.pdf": b"nope"})
    summary = run_with(
        tmp_path,
        ARCHIVE_MANIFEST,
        ranged_transport({"https://example.gov/utilities.zip": archive}),
    )
    assert len(summary.failed) == 1
    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    rec = lock["sources"]["https://example.gov/utilities.zip::ucm-2-13-2026.pdf"]
    assert rec["sha256"] is None
    assert "ucm-2-13-2026.pdf" in rec["last_error"]


TWO_MEMBER_MANIFEST = ARCHIVE_MANIFEST + """  - url: https://example.gov/utilities.zip
    member: other.pdf
    doc_type: matrix
    role: spine
    title: "Second matrix from the same archive"
"""


def test_one_archive_is_opened_once_for_many_members(tmp_path):
    """Project A names seven members of one 208 MB zip.

    Re-reading its central directory per member turns a no-op run into
    thirty seconds of range requests.
    """
    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES, "other.pdf": b"second matrix"})
    probes = []

    inner = ranged_transport({"https://example.gov/utilities.zip": archive})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range") == "bytes=0-0":
            probes.append(str(request.url))
        return inner.handler(request)

    summary = run_with(tmp_path, TWO_MEMBER_MANIFEST, httpx.MockTransport(handler))
    assert len(summary.fetched) == 2
    assert len(probes) == 1


def test_polite_user_agent_is_used_first(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, content=b"x", headers={"content-type": "application/pdf"})

    run_with(tmp_path, MANIFEST, httpx.MockTransport(handler))
    assert seen and seen[0] == USER_AGENT
    assert BROWSER_UA not in seen
