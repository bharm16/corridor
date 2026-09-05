"""Generate synthetic, non-customer PDFs shared by the bake-off tickets."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf


def generate(directory: Path) -> dict[str, dict[str, object]]:
    directory.mkdir(parents=True, exist_ok=True)
    fixtures: dict[str, dict[str, object]] = {}
    doc = pymupdf.open()
    for rotation in (0, 90, 180, 270):
        page = doc.new_page(width=420, height=300)
        page.insert_text((30, 30), f"Rotation {rotation} page-scoped outside table")
        page.draw_rect((30, 70, 390, 180)); page.draw_line((30, 105), (390, 105)); page.draw_line((180, 70), (180, 180))
        page.insert_textbox((35, 75, 175, 103), "Merged\nheader", fontsize=9)
        page.insert_text((185, 92), "Column B"); page.insert_text((35, 130), "row one"); page.insert_text((185, 130), "confirmed cell")
        if rotation == 0:
            page.set_cropbox((10, 5, 410, 295))
        page.set_rotation(rotation)
    path = directory / "geometry-and-tables.pdf"; doc.save(path); doc.close()
    fixtures[path.name] = {"clip": [20, 20, 200, 160], "kind": "text-rotations-crop-vector-table"}

    doc = pymupdf.open(); page = doc.new_page(width=300, height=200)
    page.insert_text((20, 30), "Borderless table"); page.insert_text((20, 70), "A1"); page.insert_text((150, 70), "B1")
    page.insert_text((20, 100), "A2"); page.insert_text((150, 100), "B2")
    path = directory / "borderless.pdf"; doc.save(path); doc.close(); fixtures[path.name] = {"kind": "borderless-negative-no-matrix"}

    doc = pymupdf.open(); page = doc.new_page(width=240, height=180)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 60), False); pix.clear_with(230)
    page.insert_image(page.rect, pixmap=pix)
    path = directory / "raster-only.pdf"; doc.save(path); doc.close(); fixtures[path.name] = {"kind": "raster-only"}

    source = (directory / "borderless.pdf").read_bytes()
    malformed = directory / "malformed-truncated.pdf"; malformed.write_bytes(source[: max(20, len(source) // 3)])
    fixtures[malformed.name] = {"kind": "malformed"}

    doc = pymupdf.open(); page = doc.new_page(); page.insert_text((72, 72), "encrypted content")
    encrypted = directory / "encrypted.pdf"
    doc.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="corridor")
    doc.close(); fixtures[encrypted.name] = {"kind": "encrypted", "password": "corridor"}
    (directory / "manifest.json").write_text(json.dumps(fixtures, indent=2, sort_keys=True) + "\n")
    return fixtures


if __name__ == "__main__":
    generate(Path(__file__).parents[2] / "out/pdf-engine-bakeoff/fixtures")

