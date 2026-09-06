"""A permissively licensed PDF reader with one shared drawn-table algorithm.

Run directly from this directory's isolated environment. Neither implementation
imports PyMuPDF. All returned text is captured from the selected PDF engine.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from PIL import Image

from corridor_pdf_reader.replacement.layout import (
    grid_tables,
    ordered_text,
    path_lines,
    rotate_box,
    rotate_point,
)
from corridor_pdf_reader.replacement.table_structure import refine_rows
from corridor_pdf_reader.replacement.tags import struct_cells, tag_tables
from corridor_pdf_reader.replacement.text_layout import (
    aligned_tables,
    horizontal_fragments,
    refine_drawn_alignment,
)


def pdf_box(box: Any, crop: Any) -> list[float]:
    left, bottom, right, top = box
    return [left - crop[0], crop[3] - top, right - crop[0], crop[3] - bottom]


def character(
    text: str, box: list[float], geometry: dict[str, Any], angle: float
) -> dict[str, Any]:
    return {
        "text": text,
        "box": box,
        "display_box": rotate_box(
            box, geometry["width"], geometry["height"], geometry["rotation"]
        ),
        "angle": angle % 360,
    }


def display_lines(
    lines: list[list[float]], crop: Any, geometry: dict[str, Any]
) -> list[list[float]]:
    result = []
    for x0, y0, x1, y1 in lines:
        a = rotate_point(
            x0 - crop[0],
            crop[3] - y0,
            geometry["width"],
            geometry["height"],
            geometry["rotation"],
        )
        b = rotate_point(
            x1 - crop[0],
            crop[3] - y1,
            geometry["width"],
            geometry["height"],
            geometry["rotation"],
        )
        result.append([*a, *b])
    return result


def clip_bounds(text_object: Any) -> tuple[float, float, float, float] | None:
    """Bounding box of the clip path a text object is drawn through, or None.

    Excel draws every wrapped line of a cell and hides the ones that do not fit
    the row behind a clip rectangle; the text API still returns their glyphs.
    Glyphs outside this box are not printed and must not be read.
    """
    import pypdfium2.raw as raw

    clip = raw.FPDFPageObj_GetClipPath(text_object)
    if not clip:
        return None
    points = []
    x, y = ctypes.c_float(), ctypes.c_float()
    for path_index in range(max(raw.FPDFClipPath_CountPaths(clip), 0)):
        for segment_index in range(max(raw.FPDFClipPath_CountPathSegments(clip, path_index), 0)):
            segment = raw.FPDFClipPath_GetPathSegment(clip, path_index, segment_index)
            if segment and raw.FPDFPathSegment_GetPoint(segment, x, y):
                points.append((x.value, y.value))
    if not points:
        return None
    return (
        min(px for px, _ in points),
        min(py for _, py in points),
        max(px for px, _ in points),
        max(py for _, py in points),
    )


def marked_content_id(text_object: Any) -> int:
    """The MCID a text object carries, or -1; ties glyphs to structure tags."""
    import pypdfium2.raw as raw

    value = ctypes.c_int()
    for index in range(raw.FPDFPageObj_CountMarks(text_object)):
        mark = raw.FPDFPageObj_GetMark(text_object, index)
        if raw.FPDFPageObjMark_GetParamIntValue(mark, b"MCID", value):
            return value.value
    return -1


def is_artifact(text_object: Any) -> bool:
    """Is the text object marked as an Artifact: pagination, not content."""
    import pypdfium2.raw as raw

    length = ctypes.c_ulong()
    for index in range(raw.FPDFPageObj_CountMarks(text_object)):
        mark = raw.FPDFPageObj_GetMark(text_object, index)
        if not raw.FPDFPageObjMark_GetName(mark, None, 0, length) or not length.value:
            continue
        buffer = (ctypes.c_ushort * length.value)()
        raw.FPDFPageObjMark_GetName(mark, buffer, length.value, length)
        if bytes(buffer).decode("utf-16-le", errors="replace").rstrip("\x00") == "Artifact":
            return True
    return False


def pdfium_paths(page: Any) -> list[list[float]]:
    import pypdfium2.raw as raw

    lines = []
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH]):
        matrix = obj.get_matrix()
        container = obj.container
        while container is not None:
            matrix = matrix.multiply(container.get_matrix())
            container = container.container
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDFPath_GetDrawMode(obj, fill, stroke):
            raise ValueError("PDFium could not inspect path paint mode")
        points: list[tuple[float, float]] = []
        has_curve = False
        for index in range(raw.FPDFPath_CountSegments(obj)):
            segment = raw.FPDFPath_GetPathSegment(obj, index)
            kind = raw.FPDFPathSegment_GetType(segment)
            x, y = ctypes.c_float(), ctypes.c_float()
            if not raw.FPDFPathSegment_GetPoint(segment, x, y):
                raise ValueError("PDFium could not read a path point")
            if kind == raw.FPDF_SEGMENT_MOVETO and points:
                lines.extend(
                    path_lines(
                        points,
                        closed=False,
                        filled=bool(fill.value),
                        stroked=bool(stroke.value),
                    )
                )
                points, has_curve = [], False
            if kind == raw.FPDF_SEGMENT_BEZIERTO:
                has_curve = True
            points.append(matrix.on_point(x.value, y.value))
            if raw.FPDFPathSegment_GetClose(segment):
                if not has_curve:
                    lines.extend(
                        path_lines(
                            points,
                            closed=True,
                            filled=bool(fill.value),
                            stroked=bool(stroke.value),
                        )
                    )
                points, has_curve = [], False
        if points and not has_curve:
            lines.extend(
                path_lines(
                    points, closed=False, filled=False, stroked=bool(stroke.value)
                )
            )
    return lines


def oxide_paths(doc: Any, index: int) -> list[list[float]]:
    lines = []
    for path in doc.extract_paths(index):
        points: list[tuple[float, float]] = []
        painted = path["fill_color"] is not None or path["stroke_color"] is None
        stroked = path["stroke_color"] is not None
        for op in path["operations"]:
            kind = op["op"]
            if kind == "rectangle":
                x, y, w, h = op["x"], op["y"], op["width"], op["height"]
                lines.extend(
                    path_lines(
                        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                        closed=True,
                        filled=painted,
                        stroked=stroked,
                    )
                )
            elif kind == "move_to":
                if points:
                    lines.extend(
                        path_lines(points, closed=False, filled=False, stroked=stroked)
                    )
                points = [(op["x"], op["y"])]
            elif kind == "line_to":
                points.append((op["x"], op["y"]))
            elif kind == "close_path":
                lines.extend(
                    path_lines(points, closed=True, filled=painted, stroked=stroked)
                )
                points = []
            else:
                points = []  # Curves do not establish a straight table boundary.
        if points:
            lines.extend(
                path_lines(points, closed=False, filled=False, stroked=stroked)
            )
    return lines


def finish_page(
    number: int,
    geometry: dict[str, Any],
    chars: list[dict[str, Any]],
    lines: list[list[float]],
    image: Image.Image,
    times: dict[str, float],
    *,
    retain_image: bool,
    native_tables: list[dict[str, Any]] | None = None,
    layout_state: dict[str, Any] | None = None,
    native_text: str | None = None,
    tagged_tables: list[dict[str, Any]] | None = None,
    clipped: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    tables = tagged_tables if tagged_tables else grid_tables(chars, lines)
    if native_tables is not None:
        fragments = horizontal_fragments(chars)
        tables = refine_drawn_alignment(tables, chars, lines, fragments)
    if native_tables:
        tables = refine_rows(tables, native_tables, chars, geometry)
    if native_tables is not None:
        width, height = geometry["width"], geometry["height"]
        if geometry["rotation"] % 180:
            width, height = height, width
        tables = aligned_tables(
            fragments,
            tables,
            (width, height),
            layout_state if layout_state is not None else {},
        )
        cells = [c for t in tables for c in t["structured_cells"]]
        remaining = [
            f
            for f in fragments
            if not any(
                c["box"][0] <= (f["box"][0] + f["box"][2]) / 2 <= c["box"][2]
                and c["box"][1] <= (f["box"][1] + f["box"][3]) / 2 <= c["box"][3]
                for c in cells
            )
        ]
        if len(remaining) >= 6:
            tables = aligned_tables(remaining, tables, (width, height), {})
    # The entire page remains accessible even when there is no ruled matrix.
    text = native_text if native_text is not None else ordered_text(chars)
    text_objects = []
    if native_tables is not None:
        groups: dict[int, list[dict[str, Any]]] = {}
        for c in chars:
            groups.setdefault(c["object_id"], []).append(c)
        for object_id, group in groups.items():
            boxes = [c.get("ink_display_box", c["display_box"]) for c in group]
            text_objects.append(
                {
                    "id": object_id,
                    "text": ordered_text(group),
                    "box": [
                        min(b[0] for b in boxes),
                        min(b[1] for b in boxes),
                        max(b[2] for b in boxes),
                        max(b[3] for b in boxes),
                    ],
                    "source_indices": [c["source_index"] for c in group],
                }
            )
    times["layout"] = (time.perf_counter() - started) * 1000
    rgb = image.convert("RGB")
    rendered: dict[str, Any] = {
        "size": list(rgb.size),
        "sha256": hashlib.sha256(rgb.tobytes()).hexdigest(),
    }
    if retain_image:
        rendered["image"] = rgb
    return {
        "number": number,
        "text_objects": text_objects,
        "geometry": geometry,
        "text": {"status": "ok", "value": text},
        "characters": {"status": "ok", "value": chars},
        "words": {
            "status": "unsupported",
            "reason": "This profile returns positioned characters and reconstructed cells; no native word API comparison",
        },
        "tables": {"status": "ok", "value": tables},
        "clipped": {
            "status": "ok",
            "value": clipped or [],
            "note": "glyphs the page draws but hides behind a clip; not read, kept as evidence",
        },
        "render": {"status": "ok", "value": rendered},
        "timings_ms": times,
        "capability_notes": (
            [
                "PDFium glyphs; PDFOxide vectors/native row proposals; source alignment inference",
                "Inferred structure requires verification; no OCR inference",
            ]
            if native_tables is not None
            else ["Drawn borders only; no borderless-table or OCR inference"]
        ),
    }


def read_pdf(
    source: Path,
    pages: list[int],
    *,
    engine: str = "pdfium",
    dpi: int = 150,
    retain_images: bool = False,
) -> dict[str, Any]:
    if engine not in ("pdfium", "pdf_oxide", "oxide_pdfium", "tagged"):
        raise ValueError("engine must be pdfium, pdf_oxide, oxide_pdfium or tagged")
    tagged_rows = struct_cells(source) if engine == "tagged" else {}
    if not 36 <= dpi <= 600:
        raise ValueError("DPI must be between 36 and 600")
    output: dict[str, Any] = {
        "engine": engine,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "pages": [],
    }
    if engine in ("pdfium", "tagged"):
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        try:
            doc = pdfium.PdfDocument(source)
        except pdfium.PdfiumError as exc:
            raise ValueError(f"PDF open/password failure: {exc}") from exc
        output.update(
            page_count=len(doc),
            version=str(pdfium.PYPDFIUM_INFO),
            native_version=str(pdfium.PDFIUM_INFO),
        )
    else:
        import pdf_oxide

        doc = pdf_oxide.PdfDocument(str(source))
        if doc.permissions() is not None and not doc.authenticate(""):
            raise ValueError("encrypted PDF requires a password")
        output.update(
            page_count=int(doc.page_count()),
            version=pdf_oxide.__version__,
            native_version=pdf_oxide.__version__,
        )
    render_doc = None
    if engine == "oxide_pdfium":
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        render_doc = pdfium.PdfDocument(source)
        if len(render_doc) != output["page_count"]:
            render_doc.close()
            raise ValueError("parser and renderer disagree on page count")
        output["version"] += "+" + str(pdfium.PYPDFIUM_INFO)
        output["native_version"] += "+PDFium-" + str(pdfium.PDFIUM_INFO)
    try:
        if not output["page_count"] or any(
            n < 1 or n > output["page_count"] for n in pages
        ):
            raise ValueError("PDF has no requested page")
        layout_state: dict[str, Any] = {}
        for number in pages:
            started = time.perf_counter()
            index = number - 1
            if engine in ("pdfium", "tagged"):
                page = doc[index]
                crop, media, rotation = (
                    page.get_bbox(),
                    page.get_mediabox(),
                    page.get_rotation(),
                )
            else:
                media = doc.page_media_box(index)
                requested_crop = doc.page_crop_box(index) or media
                crop = [
                    max(media[0], requested_crop[0]),
                    max(media[1], requested_crop[1]),
                    min(media[2], requested_crop[2]),
                    min(media[3], requested_crop[3]),
                ]
                rotation = doc.page_rotation(index)
            geometry = {
                "width": crop[2] - crop[0],
                "height": crop[3] - crop[1],
                "rotation": rotation,
                "crop_box": list(crop),
                "media_box": list(media),
                "box_convention": "PDF bottom-left",
            }
            if (
                geometry["width"] <= 0
                or geometry["height"] <= 0
                or (media[2] - media[0]) * (media[3] - media[1]) * (dpi / 72) ** 2
                > 25_000_000
            ):
                raise ValueError("invalid or oversized page raster")
            if render_doc is not None:
                page = render_doc[index]
                actual_crop = page.get_bbox()
                if page.get_rotation() != rotation or any(
                    abs(a - b) > 0.01 for a, b in zip(actual_crop, crop, strict=True)
                ):
                    page.close()
                    raise ValueError("parser and renderer disagree on page coordinates")
            chars = []
            clipped_chars: list[dict[str, Any]] = []
            native_text_parts: list[str] | None = None
            if engine in ("pdfium", "oxide_pdfium", "tagged"):
                # PDFOxide 0.3.77 reports an incorrect writing angle for reflected
                # text matrices and misdecodes some nonbreaking spaces. PDFium's
                # native glyph contract handles both; retain PDFOxide's vectors.
                with closing(page.get_textpage()) as textpage:
                    if engine == "oxide_pdfium":
                        native_text_parts = []
                    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
                    loose = raw.FS_RECTF()
                    pending_space = False
                    object_ids: dict[int | None, int] = {}
                    font_names: dict[int | None, str] = {}
                    mcids: dict[int | None, int] = {}
                    artifacts: dict[int | None, bool] = {}
                    clips: dict[int | None, tuple[float, float, float, float] | None] = {}
                    line_centres: dict[int | None, float] = {}
                    for i in range(textpage.count_chars()):
                        code = raw.FPDFText_GetUnicode(textpage, i)
                        if not code:
                            continue
                        if code == 2 and raw.FPDFText_IsHyphen(textpage, i) == 1:
                            code = ord("-")
                        if native_text_parts is not None:
                            native_text_parts.append(chr(code))
                        if chr(code).isspace():
                            pending_space = True
                            continue
                        if not raw.FPDFText_GetCharBox(
                            textpage, i, left, right, bottom, top
                        ):
                            raise ValueError("PDFium character geometry failed")
                        obj = raw.FPDFText_GetTextObject(textpage, i)
                        pointer = ctypes.cast(obj, ctypes.c_void_p).value
                        if pointer not in clips:
                            clips[pointer] = clip_bounds(obj)
                        clip_box = clips[pointer]
                        # A whole line hides or shows together: judge by the
                        # glyph's centre, but a glyph whose line centre is out
                        # of the clip goes with its line even when its own
                        # centre (a comma at the baseline) slips inside.
                        hidden = False
                        if clip_box is not None:
                            cx, cy = (left.value + right.value) / 2, (bottom.value + top.value) / 2
                            inside_x = clip_box[0] - 0.5 <= cx <= clip_box[2] + 0.5
                            inside_y = clip_box[1] - 0.5 <= cy <= clip_box[3] + 0.5
                            if pointer not in line_centres:
                                ol, ob, orr, ot = (ctypes.c_float() for _ in range(4))
                                raw.FPDFPageObj_GetBounds(obj, ol, ob, orr, ot)
                                line_centres[pointer] = (ob.value + ot.value) / 2
                            line_inside = clip_box[1] - 0.5 <= line_centres[pointer] <= clip_box[3] + 0.5
                            hidden = not inside_x or not (inside_y and line_inside)
                        box = pdf_box(
                            [left.value, bottom.value, right.value, top.value], crop
                        )
                        angle = (
                            math.degrees(raw.FPDFText_GetCharAngle(textpage, i))
                            + rotation
                        )
                        if pointer not in font_names:
                            size = raw.FPDFText_GetFontInfo(textpage, i, None, 0, None)
                            buffer = ctypes.create_string_buffer(size)
                            raw.FPDFText_GetFontInfo(textpage, i, buffer, size, None)
                            font_names[pointer] = buffer.value.decode(
                                "utf-8", errors="replace"
                            )
                        # Symbol-font glyphs arrive as private-use codes; the
                        # authoring application stored their byte values.
                        if 0xF020 <= code <= 0xF0FF and "symbol" in font_names[pointer].lower():
                            code -= 0xF000
                        item = character(chr(code), box, geometry, angle)
                        item["ink_display_box"] = list(item["display_box"])
                        item["source_index"] = i
                        item["object_id"] = object_ids.setdefault(
                            pointer, len(object_ids)
                        )
                        if pointer not in mcids:
                            mcids[pointer] = marked_content_id(obj)
                            artifacts[pointer] = is_artifact(obj)
                        item["mcid"] = mcids[pointer]
                        item["artifact"] = artifacts[pointer]
                        item["font_weight"] = raw.FPDFText_GetFontWeight(textpage, i)
                        item["font_name"] = font_names[pointer]
                        if hidden:
                            clipped_chars.append(item)
                            continue
                        matrix = raw.FS_MATRIX()
                        if not raw.FPDFText_GetMatrix(textpage, i, matrix):
                            raise ValueError("PDFium text matrix unavailable")
                        item["font_size"] = raw.FPDFText_GetFontSize(
                            textpage, i
                        ) * math.hypot(matrix.c, matrix.d)
                        # PDFium already distinguishes logical word boundaries.
                        # Its generated-space boxes can lie on the preceding
                        # glyph; attach the boundary to the next glyph instead.
                        item["break_before"] = pending_space
                        pending_space = False
                        if not raw.FPDFText_GetLooseCharBox(textpage, i, loose):
                            raise ValueError("PDFium character advance geometry failed")
                        item["display_box"] = rotate_box(
                            pdf_box(
                                [loose.left, loose.bottom, loose.right, loose.top], crop
                            ),
                            geometry["width"],
                            geometry["height"],
                            rotation,
                        )
                        chars.append(item)
                paths = (
                    pdfium_paths(page)
                    if engine in ("pdfium", "tagged")
                    else oxide_paths(doc, index)
                )
            else:
                # extract_page_text synthesizes wrong horizontal char advances
                # for vertical writing in 0.3.77. Use actual positioned chars.
                for c in doc.extract_chars(index):
                    if c.char in ("\r", "\n", "\t"):
                        continue
                    x, y, w, h = c.bbox
                    chars.append(
                        character(
                            c.char,
                            pdf_box([x, y, x + w, y + h], crop),
                            geometry,
                            rotation - c.rotation_degrees,
                        )
                    )
                paths = oxide_paths(doc, index)
            lines = display_lines(paths, crop, geometry)
            native_tables = (
                doc.extract_tables(index) if engine == "oxide_pdfium" else None
            )
            times = {"extract": (time.perf_counter() - started) * 1000}
            started = time.perf_counter()
            if engine in ("pdfium", "oxide_pdfium", "tagged"):
                with closing(
                    page.render(
                        scale=dpi / 72,
                        draw_annots=False,
                        rev_byteorder=True,
                        force_bitmap_format=raw.FPDFBitmap_BGRA,
                    )
                ) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
                page.close()
            else:
                pixmap = doc.render_pixmap(index, dpi=dpi)
                image = (
                    Image.frombytes("RGBa", (pixmap.width, pixmap.height), pixmap.data)
                    .convert("RGBA")
                    .convert("RGB")
                )
                mw, mh = media[2] - media[0], media[3] - media[1]
                crop_box = pdf_box(crop, media)
                displayed = rotate_box(crop_box, mw, mh, rotation)
                scale = dpi / 72
                x, y = round(displayed[0] * scale), round(displayed[1] * scale)
                w, h = geometry["width"], geometry["height"]
                if rotation % 180:
                    w, h = h, w
                image = image.crop(
                    (x, y, x + math.ceil(w * scale), y + math.ceil(h * scale))
                )
            times["render"] = (time.perf_counter() - started) * 1000
            output["pages"].append(
                finish_page(
                    number,
                    geometry,
                    chars,
                    lines,
                    image,
                    times,
                    retain_image=retain_images,
                    native_tables=native_tables,
                    layout_state=layout_state,
                    native_text="".join(native_text_parts).strip()
                    if native_text_parts is not None
                    else None,
                    tagged_tables=tag_tables(chars, lines, tagged_rows.get(index, []))
                    if engine == "tagged"
                    else None,
                    clipped=clipped_chars,
                )
            )
    finally:
        if render_doc is not None:
            render_doc.close()
        if engine in ("pdfium", "tagged"):
            doc.close()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--pages", type=int, nargs="+", default=[1])
    parser.add_argument(
        "--engine", choices=["pdfium", "pdf_oxide", "oxide_pdfium"], default="pdfium"
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = read_pdf(
        args.source, args.pages, engine=args.engine, dpi=args.dpi, retain_images=True
    )
    for page in result["pages"]:
        image = page["render"]["value"].pop("image")
        image.save(args.output / f"page-{page['number']}.png")
    (args.output / "document.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
