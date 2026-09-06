"""Builders for Textract responses and tiny PDFs, so tests stay readable.

Blocks are built in the AnalyzeDocument shape: ids, BlockType, Geometry
(BoundingBox and Polygon as ratios of the image), Relationships, and the
table fields RowIndex, ColumnIndex, RowSpan, ColumnSpan (1-based).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

Frame = tuple[float, float]


def geometry(x0: float, y0: float, x1: float, y1: float, frame: Frame) -> dict[str, Any]:
    """Geometry for a box given in points of a page whose displayed size is `frame`."""
    width, height = frame
    left, top, right, bottom = x0 / width, y0 / height, x1 / width, y1 / height
    return {
        "BoundingBox": {"Left": left, "Top": top, "Width": right - left, "Height": bottom - top},
        "Polygon": [{"X": left, "Y": top}, {"X": right, "Y": top}, {"X": right, "Y": bottom}, {"X": left, "Y": bottom}],
    }


def block(kind: str, box: tuple[float, float, float, float], frame: Frame, *, confidence: float = 99.0, **fields: Any) -> dict[str, Any]:
    return {"BlockType": kind, "Id": fields.pop("Id", None) or str(uuid.uuid4()), "Confidence": confidence, "Geometry": geometry(*box, frame), **fields}


def relate(parent: dict[str, Any], kind: str, children: list[dict[str, Any]]) -> None:
    parent.setdefault("Relationships", []).append({"Type": kind, "Ids": [child["Id"] for child in children]})


class Page:
    """A response under construction for one page of displayed size `frame`."""

    def __init__(self, frame: Frame = (612.0, 792.0)) -> None:
        self.frame = frame
        self.blocks: list[dict[str, Any]] = [block("PAGE", (0, 0, frame[0], frame[1]), frame)]

    def word(self, text: str, box: tuple[float, float, float, float], confidence: float = 99.0) -> dict[str, Any]:
        item = block("WORD", box, self.frame, confidence=confidence, Text=text, TextType="PRINTED")
        self.blocks.append(item)
        return item

    def line(self, words: list[dict[str, Any]]) -> dict[str, Any]:
        box = (min(w["Geometry"]["BoundingBox"]["Left"] for w in words) * self.frame[0], min(w["Geometry"]["BoundingBox"]["Top"] for w in words) * self.frame[1], max((w["Geometry"]["BoundingBox"]["Left"] + w["Geometry"]["BoundingBox"]["Width"]) for w in words) * self.frame[0], max((w["Geometry"]["BoundingBox"]["Top"] + w["Geometry"]["BoundingBox"]["Height"]) for w in words) * self.frame[1])
        item = block("LINE", box, self.frame, Text=" ".join(w["Text"] for w in words))
        relate(item, "CHILD", words)
        self.blocks.append(item)
        return item

    def cell(self, row: int, column: int, box: tuple[float, float, float, float], words: list[dict[str, Any]] | None = None, *, row_span: int = 1, column_span: int = 1, entity_types: list[str] | None = None) -> dict[str, Any]:
        item = block("CELL", box, self.frame, RowIndex=row, ColumnIndex=column, RowSpan=row_span, ColumnSpan=column_span)
        if entity_types:
            item["EntityTypes"] = entity_types
        if words:
            relate(item, "CHILD", words)
        self.blocks.append(item)
        return item

    def merged(self, row: int, column: int, box: tuple[float, float, float, float], cells: list[dict[str, Any]], *, row_span: int = 1, column_span: int = 1) -> dict[str, Any]:
        item = block("MERGED_CELL", box, self.frame, RowIndex=row, ColumnIndex=column, RowSpan=row_span, ColumnSpan=column_span)
        relate(item, "CHILD", cells)
        self.blocks.append(item)
        return item

    def table(self, box: tuple[float, float, float, float], cells: list[dict[str, Any]], merged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        item = block("TABLE", box, self.frame)
        relate(item, "CHILD", cells)
        if merged:
            relate(item, "MERGED_CELL", merged)
        self.blocks.append(item)
        return item

    def response(self) -> dict[str, Any]:
        return {"DocumentMetadata": {"Pages": 1}, "Blocks": self.blocks, "AnalyzeDocumentModelVersion": "1.0"}


def minimal_pdf(path: Path, *, width: float = 612.0, height: float = 792.0, rotate: int = 0, text: str | None = None, at: tuple[float, float] = (100.0, 700.0), size: float = 12.0) -> Path:
    """A one-page PDF with a correct xref, optionally holding one Helvetica string."""
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    resources = b"/Resources << /Font << /F1 4 0 R >> >>" if text is not None else b"/Resources << >>"
    objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Rotate %d %s /Contents 5 0 R >>" % (width, height, rotate, resources))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    content = b"BT /F1 %.1f Tf %.2f %.2f Td (%s) Tj ET" % (size, at[0], at[1], text.encode("latin-1")) if text is not None else b""
    objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path
