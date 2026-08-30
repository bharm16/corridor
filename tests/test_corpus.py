import hashlib
import io
import json
from pathlib import Path
import re
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
    assert m.ingest_by_default is True
    assert m.sealed is False
    assert len(m.sources) == 2
    first = m.sources[0]
    assert first.doc_type == "matrix"
    assert first.role == "spine"
    assert first.doc_date == date(2026, 2, 13)
    assert m.sources[1].doc_date is None


def test_nhhip_manifest_declares_the_five_revision_chain_from_the_rid_index():
    manifest = load_manifest("corpus/manifest.yaml")
    declarations = [
        source.supersession
        for source in manifest.sources
        if source.supersession is not None
        and source.supersession.predecessor_registry_id.startswith("nhhip-ucm-")
    ]

    assert [
        (
            declaration.predecessor_registry_id,
            declaration.successor_registry_id,
            declaration.replacement_date,
            declaration.source_registry_id,
            declaration.source_page,
        )
        for declaration in declarations
    ] == [
        (
            "nhhip-ucm-2025-06-20",
            "nhhip-ucm-2025-07-22",
            date(2025, 7, 22),
            "nhhip-rid-index-2026-05-01",
            4,
        ),
        (
            "nhhip-ucm-2025-07-22",
            "nhhip-ucm-2025-10-24",
            date(2025, 10, 31),
            "nhhip-rid-index-2026-05-01",
            4,
        ),
        (
            "nhhip-ucm-2025-10-24",
            "nhhip-ucm-2025-12-15",
            date(2025, 12, 15),
            "nhhip-rid-index-2026-05-01",
            4,
        ),
        (
            "nhhip-ucm-2025-12-15",
            "nhhip-ucm-2026-02-13",
            date(2026, 2, 13),
            "nhhip-rid-index-2026-05-01",
            4,
        ),
    ]


def test_nhhip_manifest_tracks_the_current_rid_and_supported_utility_sources():
    manifest = load_manifest("corpus/manifest.yaml")
    by_registry = {
        source.registry_id: source
        for source in manifest.sources
        if source.registry_id is not None
    }

    current_rid = by_registry["nhhip-rid-index-2026-08-14"]
    assert current_rid.url.endswith("nhhip-3c2-rid-index-20260814.pdf")
    assert current_rid.doc_date == date(2026, 8, 14)

    expected = {
        "nhhip-utility-strip-map-2025-12-15",
        "nhhip-utility-strip-map-2026-02-13",
        "nhhip-utilities-its-transtar-2026-02-13",
        "nhhip-utilities-storm-drain-2026-02-13",
        "nhhip-utilities-txdot-electric-2026-02-13",
    }
    assert expected <= set(by_registry)
    assert all(
        "shared_name=51c7p764gr6pxivj2ati0qktrz7bq6w3" in by_registry[key].url
        for key in expected
    )
    assert all(by_registry[key].doc_type == "plan" for key in expected)

    strip_map = by_registry["nhhip-utility-strip-map-2025-12-15"]
    assert strip_map.supersession is not None
    assert strip_map.supersession.successor_registry_id == (
        "nhhip-utility-strip-map-2026-02-13"
    )
    assert strip_map.supersession.source_registry_id == (
        "nhhip-rid-index-2026-08-14"
    )

def test_supersession_declarations_survive_into_the_generated_lockfile(tmp_path):
    manifest_text = """
project: registry-test
agency: TxDOT
sources:
  - registry_id: rid-index
    url: https://example.gov/index.pdf
    doc_type: other
    role: evidence
    title: RID index
  - registry_id: matrix-r1
    url: https://example.gov/r1.pdf
    doc_type: matrix
    role: spine
    title: Matrix R1
    supersession:
      successor: matrix-r2
      replacement_date: 2026-02-13
      source:
        document: rid-index
        page: 4
  - registry_id: matrix-r2
    url: https://example.gov/r2.pdf
    doc_type: matrix
    role: spine
    title: Matrix R2
"""
    manifest_path = write_manifest(tmp_path, manifest_text)
    bodies = {
        "https://example.gov/index.pdf": b"RID index",
        "https://example.gov/r1.pdf": b"matrix r1",
        "https://example.gov/r2.pdf": b"matrix r2",
    }

    fetch_all(
        load_manifest(manifest_path),
        store=tmp_path / "files",
        lock_path=tmp_path / "manifest.lock.json",
        client=httpx.Client(transport=transport(bodies)),
        delay=0.0,
    )

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    predecessor = lock["sources"]["https://example.gov/r1.pdf"]
    assert predecessor["registry_id"] == "matrix-r1"
    assert predecessor["supersession"] == {
        "predecessor_registry_id": "matrix-r1",
        "successor_registry_id": "matrix-r2",
        "replacement_date": "2026-02-13",
        "source_registry_id": "rid-index",
        "source_page": 4,
    }


def test_manifest_accepts_real_yaml_booleans_for_policy_fields(tmp_path):
    manifest = "ingest_by_default: false\nsealed: true\n" + MANIFEST
    parsed = load_manifest(write_manifest(tmp_path, manifest))
    assert parsed.ingest_by_default is False
    assert parsed.sealed is True


@pytest.mark.parametrize("field", ["ingest_by_default", "sealed"])
@pytest.mark.parametrize("value", ['"false"', "0", "1", "null"])
def test_manifest_rejects_non_boolean_policy_fields(tmp_path, field, value):
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(f"{field}: {value}\n" + MANIFEST)

    with pytest.raises(
        ValueError,
        match=re.escape(f"{manifest_path}: {field} must be a YAML boolean"),
    ):
        load_manifest(manifest_path)


def test_manifest_rejects_an_unknown_doc_type(tmp_path):
    bad = MANIFEST.replace("doc_type: matrix", "doc_type: spreadsheet")
    with pytest.raises(ValueError, match="spreadsheet"):
        load_manifest(write_manifest(tmp_path, bad))


def test_a_manifest_is_unsealed_unless_it_says_otherwise():
    assert load_manifest("corpus/manifest.yaml").sealed is False
    assert load_manifest("corpus/sh99-grand-parkway.yaml").sealed is False


def test_sh99_structured_sue_tables_are_curated_and_locked_with_derivation():
    manifest = load_manifest("corpus/sh99-grand-parkway.yaml")
    registered = {
        source.registry_id: source
        for source in manifest.sources
        if source.registry_id and source.registry_id.startswith("sh99-sue-")
    }
    assert set(registered) == {
        "sh99-sue-probe-depth-summary-2025-01-15",
        "sh99-sue-test-hole-index-2025-01-15-xls",
    }
    assert all(source.curation_status == "proposed" for source in registered.values())
    assert {
        key: (value.doc_type, value.role, value.title, value.doc_date)
        for key, value in registered.items()
    } == {
        "sh99-sue-probe-depth-summary-2025-01-15": (
            "plan",
            "evidence",
            "SUE probe depth summary table (1/15/2025)",
            date(2025, 1, 15),
        ),
        "sh99-sue-test-hole-index-2025-01-15-xls": (
            "plan",
            "evidence",
            "SUE test hole index (original XLS, 1/15/2025)",
            date(2025, 1, 15),
        ),
    }
    assert registered[
        "sh99-sue-test-hole-index-2025-01-15-xls"
    ].conversion.registry_id == "sh99-sue-test-hole-index-2025-01-15-xlsx"

    lock = json.loads(Path("corpus/sh99-grand-parkway.lock.json").read_text())
    values = list(lock["sources"].values())
    native = next(
        value
        for value in values
        if value.get("registry_id")
        == "sh99-sue-probe-depth-summary-2025-01-15"
    )
    original = next(
        value
        for value in values
        if value.get("registry_id")
        == "sh99-sue-test-hole-index-2025-01-15-xls"
    )
    derived = next(value for value in values if value.get("derivation"))
    assert native["sha256"] == (
        "1536fd83b2c81d7d2e35cd36e18d66ef059c49b7b4d7eda440de5446b511b8c1"
    )
    assert original["sha256"] == (
        "07d6399a314cd492509a8286503b4f686aec2de993415dddf9124c2510dff889"
    )
    assert derived["derivation"] == {
        "kind": "format_conversion",
        "source_registry_id": "sh99-sue-test-hole-index-2025-01-15-xls",
        "source_sha256": original["sha256"],
        "tool": "corridor.xls-to-xlsx",
        "tool_version": "3",
    }
    assert native["curation_status"] == "proposed"
    assert original["curation_status"] == "proposed"
    assert derived["curation_status"] == "proposed"
    assert "supersession" not in derived
    assert "equivalent_to" not in derived


def test_the_spent_holdout_stays_unsealed():
    """FDOT SR 789 was the eval holdout until the M7 cold run (#52).

    It asserted `sealed is True` while that mattered, and the pre-push hook
    is what caught the unsealing rather than a reviewer — which is the
    point of putting a seal in a field instead of a comment.

    The seal is spent and cannot be un-spent, so re-sealing this document
    would claim a holdout that no longer exists. The mechanism is still
    exercised, on a fixture, for whatever gets sealed next.
    """
    assert load_manifest("corpus/fdot-sr789.yaml").sealed is False


def test_a_sealed_manifest_is_never_fetched(tmp_path, monkeypatch):
    """The seal is enforced where the fetching happens, not at the call site."""
    from pathlib import Path

    import corridor.corpus as corpus_module

    sealed = "sealed: true\n" + MANIFEST
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "held-out.yaml").write_text(sealed)

    def explode(*args, **kwargs):
        raise AssertionError("fetch_all was called for a sealed manifest")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(corpus_module, "fetch_all", explode)

    assert corpus_module.main() == 0
    assert not list(corpus_dir.glob("*.lock.json"))
    assert not Path(tmp_path / "corpus" / "files").exists()


def test_the_spent_holdout_records_what_it_was():
    """WSDOT 9540 was M7's holdout (#64). Its seal was lifted once, by a
    deliberate human act, on 2026-08-04 (#88, ADR-0008).

    This test used to assert `sealed is True` and was the tripwire that
    made the spend impossible by accident. It fired, correctly, the moment
    the seal came off — and rather than deleting it, it now asserts what
    replaced the guarantee: the manifest still carries, verbatim, the
    record of what the seal protected and why. A spent holdout whose
    manifest reads like any other file is a project that has forgotten it
    had one, and `corpus-acquisition-spec.md` §7.2 says a successor exists
    to be sealed next.
    """
    manifest = load_manifest("corpus/wsdot-9540.yaml")
    assert manifest.sealed is False
    assert manifest.sources

    text = Path("corpus/wsdot-9540.yaml").read_text()
    assert "SEAL LIFTED 2026-08-04" in text
    assert "There is no second first run." in text


def test_the_holdouts_development_sibling_is_open():
    """9424 carries the same WSDOT Appendix U and is fetched normally.

    A seal with no unsealed sibling is a seal somebody eventually breaks:
    the layout still has to be developed against something.
    """
    assert load_manifest("corpus/wsdot-9424.yaml").sealed is False


def test_a_real_corpus_run_skips_the_holdout_and_names_it(capsys, monkeypatch):
    """The whole seal, exercised over the real corpus/ directory.

    `main()` globs every manifest, so this is the loop that would spend the
    holdout — and the assertion that matters is not just that it was
    skipped but that it was *named*. A corpus step that quietly does
    nothing is indistinguishable from one that worked.
    """
    from pathlib import Path

    import corridor.corpus as corpus_module

    reached = []

    def record(manifest, **kwargs):
        reached.append(manifest.project)
        return corpus_module.Summary()

    monkeypatch.setattr(corpus_module, "fetch_all", record)
    assert corpus_module.main() == 0

    # 9540's seal was spent on 2026-08-04, so it is fetched like any
    # other project now. What this still proves is the loop: a sealed
    # manifest is skipped and named, and the fixtures above exercise that
    # mechanism against a manifest whose seal is on. The successor holdout
    # inherits it.
    assert "wsdot-9424" in reached
    assert "wsdot-9540" in reached
    # Every manifest reaches the fetcher and every one is named in the
    # output — a corpus step that quietly does nothing must stay
    # distinguishable from one that worked. The skipped-and-named half of
    # this loop is exercised by the sealed-fixture tests above; the
    # successor holdout switches it back on for a real document.
    named = capsys.readouterr().out
    assert all(f"{slug}:" in named for slug in ("wsdot-9424", "wsdot-9540"))


def test_an_explicit_manifest_argument_fetches_only_that_manifest(tmp_path, monkeypatch):
    import corridor.corpus as corpus_module

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    first = corpus_dir / "first.yaml"
    second = corpus_dir / "second.yaml"
    first.write_text(MANIFEST.replace("nhhip-3c2", "first-project"))
    second.write_text(MANIFEST.replace("nhhip-3c2", "second-project"))

    reached = []

    def record(manifest, **kwargs):
        reached.append(manifest.project)
        return corpus_module.Summary()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(corpus_module, "fetch_all", record)

    assert corpus_module.main([str(second)]) == 0
    assert reached == ["second-project"]


def test_main_without_argv_ignores_process_cli_flags(tmp_path, monkeypatch):
    import corridor.corpus as corpus_module

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "only.yaml").write_text(MANIFEST.replace("nhhip-3c2", "only-project"))

    reached = []

    def record(manifest, **kwargs):
        reached.append(manifest.project)
        return corpus_module.Summary()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(corpus_module, "fetch_all", record)
    monkeypatch.setattr(corpus_module.sys, "argv", ["pytest", "--unexpected-flag"])

    assert corpus_module.main() == 0
    assert reached == ["only-project"]


def test_run_cli_forwards_process_argv(monkeypatch):
    import corridor.corpus as corpus_module

    seen = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(corpus_module, "main", fake_main)
    monkeypatch.setattr(corpus_module.sys, "argv", ["corridor.corpus", "corpus/one.yaml"])

    assert corpus_module._run_cli() == 0
    assert seen["argv"] == ["corpus/one.yaml"]


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


def test_box_archive_probe_uses_a_real_range_not_a_one_byte_request(tmp_path):
    """Current Box archives reject bytes=0-0 but accept an ordinary 64 KiB range."""
    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES})
    inner = ranged_transport({"https://example.gov/utilities.zip": archive})
    observed: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        value = request.headers.get("range")
        observed.append(value)
        if value == "bytes=0-0":
            return httpx.Response(500, content=b"Box rejected one-byte range")
        return inner.handle_request(request)

    summary = run_with(
        tmp_path,
        ARCHIVE_MANIFEST,
        httpx.MockTransport(handler),
    )

    assert summary.failed == []
    assert summary.fetched
    assert observed[0] == "bytes=0-65535"


def test_unchanged_archive_member_is_skipped_via_crc(tmp_path):
    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES})
    bodies = {"https://example.gov/utilities.zip": archive}
    run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))
    second = run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))
    assert second.fetched == []
    assert len(second.skipped) == 1


def test_an_xls_member_retains_original_bytes_and_derives_one_stable_xlsx(
    tmp_path
):
    manifest = """
project: sh99-grand-parkway
agency: TxDOT
sources:
  - registry_id: sh99-test-hole-index-2025-01-15-xls
    url: https://example.gov/utilities.zip
    member: data/SH99_TEST_HOLE_INDEX_1-15-2025.xls
    doc_type: plan
    role: evidence
    title: SUE test hole index (original XLS)
    doc_date: 2025-01-15
    conversion:
      to: xlsx
      registry_id: sh99-test-hole-index-2025-01-15-xlsx
      title: SUE test hole index (converted XLSX rendition)
"""
    original = b"legacy-binary-workbook"
    converted = b"PK\x03\x04deterministic-xlsx"
    archive = make_zip({"data/SH99_TEST_HOLE_INDEX_1-15-2025.xls": original})
    calls = []

    def convert(value):
        calls.append(value)
        return converted

    parsed = load_manifest(write_manifest(tmp_path, manifest))
    assert parsed.sources[0].conversion.registry_id == (
        "sh99-test-hole-index-2025-01-15-xlsx"
    )
    first = fetch_all(
        parsed,
        store=tmp_path / "files",
        lock_path=tmp_path / "manifest.lock.json",
        client=httpx.Client(
            transport=ranged_transport(
                {"https://example.gov/utilities.zip": archive}
            )
        ),
        delay=0.0,
        xls_converter=convert,
    )
    second = fetch_all(
        parsed,
        store=tmp_path / "files",
        lock_path=tmp_path / "manifest.lock.json",
        client=httpx.Client(
            transport=ranged_transport(
                {"https://example.gov/utilities.zip": archive}
            )
        ),
        delay=0.0,
        xls_converter=convert,
    )

    assert len(first.fetched) == 2
    assert len(second.skipped) == 2
    assert calls == [original]
    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    records = list(lock["sources"].values())
    original_record = next(record for record in records if record.get("member", "").endswith(".xls"))
    derived_record = next(record for record in records if record.get("derivation"))
    assert Path(original_record["local_path"]).read_bytes() == original
    assert Path(derived_record["local_path"]).read_bytes() == converted
    assert derived_record["registry_id"] == "sh99-test-hole-index-2025-01-15-xlsx"
    assert derived_record["derivation"] == {
        "kind": "format_conversion",
        "source_registry_id": "sh99-test-hole-index-2025-01-15-xls",
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "tool": "corridor.xls-to-xlsx",
        "tool_version": "3",
    }
    assert "supersession" not in derived_record
    assert "equivalent_to" not in derived_record


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
        if request.headers.get("range") == "bytes=0-65535":
            probes.append(str(request.url))
        return inner.handler(request)

    summary = run_with(tmp_path, TWO_MEMBER_MANIFEST, httpx.MockTransport(handler))
    assert len(summary.fetched) == 2
    assert len(probes) == 1


NESTED_MANIFEST = """
project: sh99-grand-parkway
name: SH 99 Grand Parkway Segment B-1
agency: TxDOT
sources:
  - url: https://example.gov/utilities.zip
    member: "Utility Owner Coordination/Notes.zip::Meeting Notes/Air Liquide/2024.07.30 notes.pdf"
    doc_type: minutes
    role: stream
    title: "Air Liquide coordination notes, 7/30/2024"
    doc_date: 2024-07-30
  - url: https://example.gov/utilities.zip
    member: "Utility Owner Coordination/Notes.zip::Meeting Notes/Chevron/2024.08.13 notes.pdf"
    doc_type: minutes
    role: stream
    title: "Chevron coordination notes, 8/13/2024"
    doc_date: 2024-08-13
"""

# Incompressible and large enough that the one inner-zip read is
# unmistakably bigger than any zip-directory tail read (~64 KB max).
import random as _random

NOTE_A = b"%PDF-1.7 Air Liquide " + _random.Random(1).randbytes(300_000)
NOTE_B = b"%PDF-1.7 Chevron " + _random.Random(2).randbytes(300_000)


def make_nested_zip():
    inner = make_zip(
        {
            "Meeting Notes/Air Liquide/2024.07.30 notes.pdf": NOTE_A,
            "Meeting Notes/Chevron/2024.08.13 notes.pdf": NOTE_B,
        }
    )
    return make_zip(
        {
            "Utility Owner Coordination/Notes.zip": inner,
            "sh99-draft-ucm.pdf": b"%PDF-1.7 draft ucm",
        }
    )


def test_a_nested_member_is_extracted_and_hashed(tmp_path):
    """SH 99's meeting notes live in a zip inside the zip."""
    archive = make_nested_zip()
    summary = run_with(
        tmp_path,
        NESTED_MANIFEST,
        ranged_transport({"https://example.gov/utilities.zip": archive}),
    )
    assert len(summary.fetched) == 2

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    key = (
        "https://example.gov/utilities.zip::Utility Owner Coordination/Notes.zip"
        "::Meeting Notes/Air Liquide/2024.07.30 notes.pdf"
    )
    rec = lock["sources"][key]
    assert rec["sha256"] == hashlib.sha256(NOTE_A).hexdigest()
    from pathlib import Path

    assert Path(rec["local_path"]).read_bytes() == NOTE_A


def test_the_inner_archive_is_read_once_for_many_members(tmp_path):
    """145 notes share one 88 MB inner zip; reading it per member is 12 GB."""
    archive = make_nested_zip()
    inner_reads = []

    inner = ranged_transport({"https://example.gov/utilities.zip": archive})

    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range", "")
        inner_reads.append(rng)
        return inner.handler(request)

    summary = run_with(tmp_path, NESTED_MANIFEST, httpx.MockTransport(handler))
    assert len(summary.fetched) == 2
    # The inner zip member is one contiguous span of the outer archive; it
    # must be ranged out of it exactly once, however many nested members
    # the manifest names.
    # (Probe requests are bytes=0-65535; the directory reads are small tail
    # ranges; the inner-zip read is the only large one.)
    large = [r for r in inner_reads if r and _range_span(r) > 200_000]
    assert len(large) == 1, f"inner zip read {len(large)} times: {large}"


def _range_span(header: str) -> int:
    start, _, end = header.removeprefix("bytes=").partition("-")
    try:
        return int(end) - int(start) + 1
    except ValueError:
        return 0


def test_an_already_fetched_nested_member_skips_without_any_network(tmp_path):
    """Re-running `make corpus` must not re-download 88 MB to learn nothing.

    Top-level members re-check their CRC against the outer directory, which
    is a few hundred KB of ranges. For nested members that check would cost
    the whole inner zip, so a lock record whose file is still on disk is
    trusted instead. The outer zips are dated snapshots; TxDOT revises by
    publishing new ones, not by mutating old ones.
    """
    archive = make_nested_zip()
    run_with(
        tmp_path,
        NESTED_MANIFEST,
        ranged_transport({"https://example.gov/utilities.zip": archive}),
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    summary = run_with(tmp_path, NESTED_MANIFEST, httpx.MockTransport(handler))
    assert len(summary.skipped) == 2
    assert calls == []


def test_a_missing_nested_member_is_recorded_not_raised(tmp_path):
    manifest = NESTED_MANIFEST.replace("Chevron/2024.08.13 notes.pdf", "Nobody/nope.pdf")
    archive = make_nested_zip()
    summary = run_with(
        tmp_path,
        manifest,
        ranged_transport({"https://example.gov/utilities.zip": archive}),
    )
    assert len(summary.fetched) == 1
    assert len(summary.failed) == 1


def test_manifest_carries_project_name_for_ingest(tmp_path):
    m = load_manifest(write_manifest(tmp_path, NESTED_MANIFEST))
    assert m.project == "sh99-grand-parkway"
    assert m.name == "SH 99 Grand Parkway Segment B-1"
    assert m.agency == "TxDOT"
    lock_path = tmp_path / "manifest.lock.json"
    run_with(
        tmp_path,
        NESTED_MANIFEST,
        ranged_transport({"https://example.gov/utilities.zip": make_nested_zip()}),
    )
    lock = json.loads(lock_path.read_text())
    assert lock["project"] == "sh99-grand-parkway"
    assert lock["name"] == "SH 99 Grand Parkway Segment B-1"
    assert lock["agency"] == "TxDOT"


def test_manifest_carries_ingest_policy_into_the_lock_header(tmp_path):
    manifest = (
        "ingest_by_default: false\n"
        + NESTED_MANIFEST
    )
    m = load_manifest(write_manifest(tmp_path, manifest))
    assert m.ingest_by_default is False

    lock_path = tmp_path / "manifest.lock.json"
    run_with(
        tmp_path,
        manifest,
        ranged_transport({"https://example.gov/utilities.zip": make_nested_zip()}),
    )
    lock = json.loads(lock_path.read_text())
    assert lock["ingest_by_default"] is False


def test_polite_user_agent_is_used_first(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, content=b"x", headers={"content-type": "application/pdf"})

    run_with(tmp_path, MANIFEST, httpx.MockTransport(handler))
    assert seen and seen[0] == USER_AGENT
    assert BROWSER_UA not in seen


# --------------------------------------------------- I-35 NEX South (#365)

I35NEX_ARCHIVE = "https://example.gov/i35nexso-rid-utilities.zip"

I35NEX_MANIFEST = f"""
project: i35-nex-south
agency: TxDOT
sources:
  - registry_id: i35nexso-ucm-2022-02-07
    url: {I35NEX_ARCHIVE}
    member: "Utilities/I-35_NEX_SOUTH_UCM_22.02.07.xlsx"
    doc_type: matrix
    role: spine
    title: "Utility Conflict List (UCM, 2/7/2022)"
    doc_date: 2022-02-07
  - registry_id: i35nexso-potential-utility-conflicts
    url: {I35NEX_ARCHIVE}
    member: "Utilities/I-35 NEX_SOUTH_Potential_Utility_Conflicts.xlsx"
    doc_type: matrix
    role: spine
    title: "Potential Utility Conflict List (UCM)"
"""


def test_i35_nex_south_registers_both_workbooks_as_matrix_spines():
    """Two structured conflict workbooks, both members of the one utilities
    archive, curated as the project's matrix spine (ADR-0005, ADR-0046)."""
    manifest = load_manifest("corpus/i35-nex-south.yaml")

    assert (manifest.project, manifest.agency) == ("i35-nex-south", "TxDOT")
    registered = {source.registry_id: source for source in manifest.sources}
    assert set(registered) == {
        "i35nexso-ucm-2022-02-07",
        "i35nexso-potential-utility-conflicts",
    }
    assert all(s.doc_type == "matrix" and s.role == "spine" for s in registered.values())
    assert all(s.curation_status == "confirmed" for s in registered.values())
    # Both reach into the same archive; only the member differs.
    assert {s.url for s in registered.values()} == {
        "https://app.box.com/index.php?rm=box_download_shared_file"
        "&shared_name=nj56xaqh2dqoj88euo0zqndyk8mfw0a3&file_id=f_1793826942528"
    }
    assert registered["i35nexso-ucm-2022-02-07"].member == (
        "Utilities/I-35_NEX_SOUTH_UCM_22.02.07.xlsx"
    )
    assert registered["i35nexso-ucm-2022-02-07"].doc_date == date(2022, 2, 7)
    assert registered["i35nexso-potential-utility-conflicts"].member == (
        "Utilities/I-35 NEX_SOUTH_Potential_Utility_Conflicts.xlsx"
    )


def test_i35_nex_south_lock_records_both_verified_workbooks():
    """The committed lock is the fetch outcome: both members verified as
    stored spreadsheet bytes, by exact hash and archive CRC."""
    lock = json.loads(Path("corpus/i35-nex-south.lock.json").read_text())
    by_registry = {
        record.get("registry_id"): record for record in lock["sources"].values()
    }

    ucm = by_registry["i35nexso-ucm-2022-02-07"]
    potential = by_registry["i35nexso-potential-utility-conflicts"]
    assert ucm["sha256"] == (
        "d5895debe379e6b2dc2e7abe5d71060d2f0bef99a58c1ffaa031d1836db70c62"
    )
    assert ucm["member_crc32"] == 1841191515
    assert potential["sha256"] == (
        "3cd94fea058a3e61ac95ab1efd566e684e146f64ce6f25048d93cf9db55f83ba"
    )
    assert potential["member_crc32"] == 3920360912
    for record in (ucm, potential):
        assert record["doc_type"] == "matrix"
        assert record["role"] == "spine"
        assert record["curation_status"] == "confirmed"
        assert record["local_path"].endswith(".xlsx")


def test_i35_nex_south_archive_members_refetch_as_a_no_op(tmp_path):
    """Re-running fetch is a no-op: an unchanged member is recognised by its
    archive CRC and never re-stored (#365 acceptance)."""
    archive = make_zip(
        {
            "Utilities/I-35_NEX_SOUTH_UCM_22.02.07.xlsx": b"PK\x03\x04ucm-cells",
            "Utilities/I-35 NEX_SOUTH_Potential_Utility_Conflicts.xlsx": (
                b"PK\x03\x04potential-cells"
            ),
        }
    )
    bodies = {I35NEX_ARCHIVE: archive}

    first = run_with(tmp_path, I35NEX_MANIFEST, ranged_transport(bodies))
    second = run_with(tmp_path, I35NEX_MANIFEST, ranged_transport(bodies))

    assert len(first.fetched) == 2
    assert first.failed == []
    assert second.fetched == []
    assert len(second.skipped) == 2


# --------------------------------------------------------------------------
# Store self-healing (a wiped or partial content-addressed store)
# --------------------------------------------------------------------------


def test_missing_store_file_is_restored_on_refetch_of_unchanged_bytes(tmp_path):
    import hashlib

    run(tmp_path, dict(BODIES))
    sha = hashlib.sha256(b"matrix rev c").hexdigest()
    stored = tmp_path / "files" / sha[:2] / f"{sha}.pdf"
    stored.unlink()

    second = run(tmp_path, dict(BODIES))

    # The lock is unchanged — the bytes did not drift — but the store healed.
    assert second.fetched == []
    assert len(second.skipped) == 2
    assert stored.read_bytes() == b"matrix rev c"


def test_missing_store_file_is_reextracted_despite_matching_member_crc(tmp_path):
    from pathlib import Path

    archive = make_zip({"ucm-2-13-2026.pdf": UCM_BYTES})
    bodies = {"https://example.gov/utilities.zip": archive}
    run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))

    lock = json.loads((tmp_path / "manifest.lock.json").read_text())
    rec = lock["sources"]["https://example.gov/utilities.zip::ucm-2-13-2026.pdf"]
    stored = Path(rec["local_path"])
    stored.unlink()

    second = run_with(tmp_path, ARCHIVE_MANIFEST, ranged_transport(bodies))

    # The CRC still matches, so the lock records no drift — but the member is
    # re-read from the archive and the store file comes back.
    assert second.failed == []
    assert len(second.skipped) == 1
    assert stored.read_bytes() == UCM_BYTES
