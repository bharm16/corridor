"""A run in the harness layout: reads per pair, receipts, lanes A and B on one page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from corridor_pdf_reader.textract.client import TextractClient
from corridor_pdf_reader.textract.read import read_document, receipts_file
from corridor_pdf_reader.textract.tests.helpers import Page, minimal_pdf


class OneResponse:
    """A service that answers every page with the same recorded response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.calls += 1
        return self.response


def response_around_total() -> dict[str, Any]:
    """Textract's view of a page whose only text is 'Total' at (100, 700) PDF points."""
    page = Page()
    word = page.word("TOTAL", (100, 82, 132, 93))
    page.line([word])
    cell = page.cell(1, 1, (90, 75, 160, 100), [word])
    page.cell(1, 2, (160, 75, 230, 100))
    page.table((90, 75, 230, 100), [cell, page.blocks[-1]])
    return page.response()


def test_lane_b_keeps_textract_words_and_lane_a_the_documents_glyphs(tmp_path: Path) -> None:
    pdf = minimal_pdf(tmp_path / "total.pdf", text="Total")
    service = OneResponse(response_around_total())
    client = TextractClient(tmp_path / "cache", service, sleep=lambda s: None)
    lane_b = read_document(pdf, lane="B", client=client, dpi=72, pngs=tmp_path / "png")
    assert lane_b["engine"] == "textract-B" and lane_b["page_count"] == 1 and lane_b["pending"] == [] and lane_b["page_errors"] == []
    page = lane_b["pages"][0]
    assert page["tables"][0]["cells"][0]["text"] == "TOTAL"
    assert page["source"]["dpi"] == 72 and page["source"]["cached"] is False and page["source"]["pixels"] == [612, 792]
    assert (tmp_path / "png" / f"{page['source']['png_sha256']}.png").exists()
    lane_a = read_document(pdf, lane="A", client=client, dpi=72, pngs=tmp_path / "png", original=pdf)
    assert lane_a["pages"][0]["tables"][0]["cells"][0]["text"] == "Total"
    assert lane_a["pages"][0]["tables"][0]["cells"][1]["text"] == ""
    assert lane_a["pages"][0]["text_source"] == "pdfium-glyphs" and lane_a["pages"][0]["source"]["cached"] is True
    assert service.calls == 1, "the same raster is one Textract call for both lanes"


def test_an_offline_client_writes_pending_pages(tmp_path: Path) -> None:
    pdf = minimal_pdf(tmp_path / "blank.pdf")
    client = TextractClient(tmp_path / "cache", offline=True)
    document = read_document(pdf, lane="B", client=client, dpi=36, pngs=tmp_path / "png")
    page = document["pages"][0]
    assert page["tables"] == [] and page["outside"] == [] and page["pending"] == document["pending"][0]
    assert page["size"] == [612.0, 792.0] and page["rotation"] == 0


def test_a_failed_call_is_a_page_failure_and_the_run_goes_on(tmp_path: Path) -> None:
    class Refusing:
        def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
            raise RuntimeError("UnsupportedDocumentException")

    pdf = minimal_pdf(tmp_path / "blank.pdf")
    client = TextractClient(tmp_path / "cache", Refusing(), sleep=lambda s: None)
    document = read_document(pdf, lane="C", client=client, dpi=36, pngs=tmp_path / "png")
    assert document["page_errors"] == [{"page": 1, "error": document["pages"][0]["error"]}]
    assert "UnsupportedDocumentException" in document["pages"][0]["error"] and document["pages"][0]["tables"] == []


def test_receipts_have_the_harness_shape_and_the_bill(tmp_path: Path) -> None:
    client = TextractClient(tmp_path / "cache", OneResponse({"Blocks": []}), sleep=lambda s: None)
    client.analyze(b"a")
    client.analyze(b"a")
    receipts = [{"key": "k1", "pages": 2, "seconds": 1.0, "sent": 1, "cached": 1, "pending": 0, "page_errors": []}, {"key": "k0", "seconds": 0.1, "error": "FileNotFoundError: twin", "trace": ""}]
    summary = receipts_file("B", 300, client, receipts, 1.2, "/twins")
    assert summary["engine"] == "textract-B" and summary["pages_sent"] == 1 and summary["pages_cached"] == 1 and summary["dollars"] == 0.015
    assert {r["key"]: "error" in r for r in summary["receipts"]} == {"k1": False, "k0": True}
    assert json.loads(json.dumps(summary)) == summary
