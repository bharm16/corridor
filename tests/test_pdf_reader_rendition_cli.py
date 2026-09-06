"""`make pdf-reader-inspect` prints a stored Document Rendition as the reader sees it (#729).

The command names a rendition by its content digest and gets the bytes from
`corridor.object_storage`, the one doorway into the store, or by a path for
a document that is not stored. Either way the read runs in its own process
under the PDFium execution contract, and the output lists pages, tables,
cells with their semantics-tier IDs, the text outside every table, and the
clipped runs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from corridor.config import settings
from corridor.object_storage import content_key, content_store
from corridor_pdf_reader import rendition_cli

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "corridor_pdf_reader" / "corpus" / "fixtures"
FIXTURE = FIXTURES / "fixture-rotation-0.pdf"


def test_a_file_is_printed_with_cell_ids_outside_text_and_clipped_runs(capsys):
    assert rendition_cli.main(["--file", str(FIXTURE), "--dpi", "72"]) == 0
    output = capsys.readouterr().out

    sha256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert output.startswith(f"rendition {sha256}\n")
    assert "engine tagged  pypdfium2 5.13.0" in output
    assert "page 1  300 x 200 pt  rotation 0" in output
    assert "table 0  method drawn-grid+runs-v1" in output
    assert 't0r0c1  row 0 col 1  span 1x1  box [' in output and '"UTILITY 1149+00"' in output
    assert 't0r1c1  row 1 col 1' in output and '"Owner"' in output
    assert "outside text: none" in output
    assert "clipped runs: none" in output


def test_the_drawn_grid_engine_lists_the_sentence_as_outside_text(capsys):
    assert rendition_cli.main(["--file", str(FIXTURE), "--engine", "pdfium", "--dpi", "72"]) == 0
    output = capsys.readouterr().out

    assert "table 0  method drawn-grid-v1" in output
    assert "outside text: 1" in output
    assert 'o0  box [' in output and '"UTILITY 1149+00"' in output


def test_a_stored_rendition_is_read_through_the_storage_interface(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    data = FIXTURE.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    content_store().put(content_key(sha256, ".pdf"), data, sha256=sha256)

    assert rendition_cli.main(["--sha256", sha256, "--json", "--dpi", "72"]) == 0
    reading = json.loads(capsys.readouterr().out)

    assert reading["sha256"] == sha256
    assert reading["engine"] == "tagged"
    assert reading["page_count"] == 1
    page = reading["pages"][0]
    assert {cell["id"]: cell["text"] for cell in page["tables"][0]["cells"]} == {
        "t0r0c1": "UTILITY 1149+00",
        "t0r1c1": "Owner",
        "t0r1c2": "Status",
        "t0r2c1": "Gas",
        "t0r2c2": "Open",
    }
    assert page["outside"] == [] and page["clipped"] == []


def test_an_unknown_digest_is_refused_without_reading_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    missing = "0" * 64

    assert rendition_cli.main(["--sha256", missing]) == 2
    assert f"no stored Document Rendition has sha256 {missing}" in capsys.readouterr().err


def test_a_document_the_reader_rejects_is_reported_not_raised(capsys):
    assert rendition_cli.main(["--file", str(FIXTURES / "fixture-encrypted.pdf")]) == 1
    assert "password" in capsys.readouterr().err


@pytest.mark.parametrize("pages", [["1"], ["1", "1"]])
def test_named_pages_are_the_pages_read(capsys, pages):
    assert rendition_cli.main(["--file", str(FIXTURE), "--pages", *pages, "--json", "--dpi", "72"]) == 0
    reading = json.loads(capsys.readouterr().out)

    assert [page["number"] for page in reading["pages"]] == [int(page) for page in pages]
