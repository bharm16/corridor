"""Isolated raster and OpenCV derivative worker.

The core application used one Pillow/PyMuPDF image for review, OCR, and table
work. This process owns engine dependencies and emits one identified derivative
plus a complete affine manifest. It has no database access and cannot mutate the
source PDF or any reviewer rendition.

One rasterizer reaches the manifest: PDFium, through pypdfium2, the
replacement ADR-0094 decided on. `legacy_pymupdf` held the measured MuPDF path
it replaces and was imported only when a request asked for it; #741 deleted it
with the engine, so a request naming the legacy rasterizer is refused by name
rather than answered by a different engine. Which rasterizer runs is still the
caller's decision, carried in the request and never read from this process's
environment: `render_profiles.worker_environment` scrubs every `CORRIDOR_`
variable before the subprocess starts.

`LEGACY_RASTERIZER` stays, because it is the identity on every derivative
rendered before the switch and the rule that names their files: a retained
MuPDF render is named without a rasterizer suffix, and moving it would make
every stored path wrong.

**Why PDFium needs no further isolation here.** `corridor_pdf_reader.execution`
holds the rule that PDFium is unsafe to call from more than one thread of a
process, and offers two mechanisms: `pdfium_entry()`, an in-process guard for a
caller that shares its process with other threads, and `PdfiumExecutor`, which
runs a call in a spawned process. This worker is already the second mechanism.
The core starts one short-lived process per render request, hands it a request
file, and reads back a manifest file; the process never threads, renders its
requests one after another, and exits. Wrapping a spawned process in another
spawned process would buy nothing, and a lock inside a single-threaded process
guards against a caller that cannot exist. The worker is a separate uv project
and cannot import the reader package in any case - `tests/test_pdf_reader_package.py`
requires that it does not - so the contract is honoured by construction and
stated here rather than imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, __version__ as pillow_version
import pypdfium2

from raster import SCALE, RasterPage, fixed_box, flip_box


LEGACY_RASTERIZER = "pymupdf"
REPLACEMENT_RASTERIZER = "pdfium"
PDFIUM_PREPROCESSING_VERSION = "pdfium-deskew-v2"


def multiply(left, right):
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re + lc * rf + le,
        lb * re + ld * rf + lf,
    )


def inverse(matrix):
    a, b, c, d, e, f = matrix
    determinant = a * d - b * c
    if abs(determinant) < 1e-15:
        raise ValueError("render transform is not invertible")
    return (
        d / determinant,
        -b / determinant,
        -c / determinant,
        a / determinant,
        (c * f - d * e) / determinant,
        (b * e - a * f) / determinant,
    )


def apply(matrix, point):
    a, b, c, d, e, f = matrix
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def box_width(box):
    return box["x1"] - box["x0"]


def box_height(box):
    return box["y1"] - box["y0"]


def rotation_matrix(rotation, width, height):
    if rotation == 0:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if rotation == 90:
        return (0.0, 1.0, -1.0, 0.0, float(height), 0.0)
    if rotation == 180:
        return (-1.0, 0.0, 0.0, -1.0, float(width), float(height))
    if rotation == 270:
        return (0.0, -1.0, 1.0, 0.0, 0.0, float(width))
    raise ValueError(f"unsupported page rotation {rotation}")


def transform_step(name, forward, *, geometry_change=True, parameters=None):
    return {
        "name": name,
        "forward": list(forward),
        "inverse": list(inverse(forward)),
        "geometry_change": geometry_change,
        "parameters": parameters or {},
    }


def detect_skew(gray):
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 1800, threshold=100)
    if lines is None:
        return 0.0
    deviations = []
    for line in lines[:100]:
        theta = float(line[0][1])
        degrees = math.degrees(theta)
        nearest_axis = round(degrees / 90) * 90
        deviation = degrees - nearest_axis
        if abs(deviation) <= 10:
            deviations.append(deviation)
    return float(np.median(deviations)) if deviations else 0.0


def deskew(image, *, preprocessing_version=None):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    angle = detect_skew(gray)
    height, width = gray.shape[:2]
    center = (width / 2, height / 2)
    # Hough's deviation is in raster y-down coordinates. OpenCV removes it
    # with the same signed angle. Keep the historical direction for requests
    # without the replacement version so rollback pixels do not change.
    correction = angle if preprocessing_version == PDFIUM_PREPROCESSING_VERSION else -angle
    cv_matrix = cv2.getRotationMatrix2D(center, correction, 1.0)
    forward = (
        float(cv_matrix[0, 0]),
        float(cv_matrix[1, 0]),
        float(cv_matrix[0, 1]),
        float(cv_matrix[1, 1]),
        float(cv_matrix[0, 2]),
        float(cv_matrix[1, 2]),
    )
    if abs(angle) > 0.01:
        image = cv2.warpAffine(
            image,
            cv_matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    return image, angle, forward


def preprocess(image, operations):
    processed = image
    for operation in operations:
        if operation == "grayscale":
            processed = cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY)
        elif operation == "denoise":
            processed = cv2.GaussianBlur(processed, (3, 3), 0)
        elif operation == "adaptive_threshold":
            if processed.ndim == 3:
                processed = cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY)
            processed = cv2.adaptiveThreshold(
                processed,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV,
                31,
                9,
            )
        elif operation == "line_morphology":
            horizontal = cv2.morphologyEx(
                processed,
                cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1)),
            )
            vertical = cv2.morphologyEx(
                processed,
                cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25)),
            )
            processed = cv2.bitwise_or(horizontal, vertical)
        else:
            raise ValueError(f"unknown render preprocessing operation {operation}")
    return processed


def rasterise_pdfium(source_path, page_number, dpi):
    """Render one page's displayed area with PDFium, with its declared boxes.

    PDFium renders what a viewer shows: the crop box, with /Rotate applied, so
    the pixels arrive in the same frame MuPDF's do and the shared chain below
    fits both. The boxes it reports are the file's own, y upwards and in any
    corner order, so they are normalized and the crop box is flipped into
    Corridor's top-left frame here. `get_cropbox()` falls back to the media box
    when the page declares none, which is what a reader displays and what MuPDF
    reports for such a page.

    The boxes are then checked against the size PDFium says it is rendering,
    because PDFium's box accessors do not inherit /MediaBox or /CropBox from a
    parent node of the page tree while its renderer resolves them properly. No
    corpus page disagrees, and a page that did would otherwise produce a
    manifest whose affine chain quietly described a different rectangle from
    the one in the PNG. It fails the render instead.
    """

    document = pypdfium2.PdfDocument(source_path)
    try:
        page = document[page_number - 1]
        media_box = fixed_box(*page.get_mediabox())
        crop_box = fixed_box(*page.get_cropbox())
        rotation = int(page.get_rotation())
        displayed = page.get_size()
        image = page.render(scale=dpi / 72).to_pil().convert("RGB").copy()
        page.close()
    finally:
        document.close()
    width, height = box_width(crop_box), box_height(crop_box)
    if rotation in {90, 270}:
        width, height = height, width
    if abs(round(displayed[0] * SCALE) - width) > 1 or (
        abs(round(displayed[1] * SCALE) - height) > 1
    ):
        raise ValueError(
            "the page boxes PDFium reports are not the page it renders; the "
            "file may inherit /MediaBox or /CropBox from the page tree"
        )
    return RasterPage(
        media_box=media_box,
        crop_box=flip_box(crop_box, media_box["y1"]),
        page_origin_y=crop_box["y1"],
        rotation=rotation,
        image=image,
    )


def rasterise(rasterizer, source_path, page_number, dpi):
    """The requested engine's page, and the versions it was produced with."""

    if rasterizer == REPLACEMENT_RASTERIZER:
        return rasterise_pdfium(source_path, page_number, dpi), {
            "pypdfium2": pypdfium2.version.PYPDFIUM_INFO.version,
            "pdfium": pypdfium2.version.PDFIUM_INFO.version,
        }
    if rasterizer == LEGACY_RASTERIZER:
        raise ValueError(
            f"the {LEGACY_RASTERIZER} rasterizer was removed with the engine "
            "(#741); its renders are retained and are not re-rendered here"
        )
    raise ValueError(f"unknown rasterizer {rasterizer!r}")


def render(request):
    rasterizer = request["rasterizer"]
    preprocessing_version = request.get("preprocessing_version")
    if preprocessing_version is not None:
        if preprocessing_version != PDFIUM_PREPROCESSING_VERSION:
            raise ValueError(f"unsupported render preprocessing version {preprocessing_version!r}")
        if rasterizer != REPLACEMENT_RASTERIZER:
            raise ValueError("versioned PDFium preprocessing requires the PDFium rasterizer")
    source_path = Path(request["pdf_path"])
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    profile = request["profile"]
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    page, engine_versions = rasterise(
        rasterizer, source_path, int(request["page_number"]), int(profile["dpi"])
    )
    media_box = page.media_box
    crop_box = page.crop_box
    rotation = page.rotation
    image = page.image

    clip_box = request.get("clip_page_box")
    rotated_width = box_height(crop_box) if rotation in {90, 270} else box_width(crop_box)
    rotated_height = box_width(crop_box) if rotation in {90, 270} else box_height(crop_box)
    if clip_box is None:
        clip_box = {"x0": 0, "y0": 0, "x1": rotated_width, "y1": rotated_height}
    elif (
        clip_box["x0"] < 0
        or clip_box["y0"] < 0
        or clip_box["x1"] > rotated_width
        or clip_box["y1"] > rotated_height
    ):
        raise ValueError("render clip is outside rotated page bounds")
    scale = int(profile["dpi"]) / (72 * SCALE)
    pixel_crop = (
        max(0, round(clip_box["x0"] * scale)),
        max(0, round(clip_box["y0"] * scale)),
        min(image.width, round(clip_box["x1"] * scale)),
        min(image.height, round(clip_box["y1"] * scale)),
    )
    image = image.crop(pixel_crop)
    array = np.asarray(image)

    # Page space is the crop box's top-left corner with y downwards, so the
    # flip constant is the crop box's *top* edge in PDF user space. That is
    # `page_origin_y`, not `crop_box["y1"]`: the two agree on every page whose
    # media box starts at y = 0, and differ by `media_box["y0"]` on the pages
    # that do not (the retired MuPDF rasterizer).
    pdf_to_page = (
        1.0,
        0.0,
        0.0,
        -1.0,
        -float(crop_box["x0"]),
        float(page.page_origin_y),
    )
    rotate = rotation_matrix(rotation, box_width(crop_box), box_height(crop_box))
    page_to_clip = (
        1.0,
        0.0,
        0.0,
        1.0,
        -float(clip_box["x0"]),
        -float(clip_box["y0"]),
    )
    clip_to_raster = (scale, 0.0, 0.0, scale, 0.0, 0.0)
    transforms = [
        transform_step(
            "pdf_user_to_page",
            pdf_to_page,
            parameters={"page_origin_y": page.page_origin_y},
        ),
        transform_step("page_rotation", rotate, parameters={"degrees": rotation}),
        transform_step("page_to_clip", page_to_clip, parameters={"box": clip_box}),
        transform_step("clip_to_raster", clip_to_raster, parameters={"dpi": profile["dpi"]}),
    ]

    if profile["name"] == "table_cv":
        array, angle, deskew_matrix = deskew(array, preprocessing_version=preprocessing_version)
        deskew_parameters = {"angle_degrees": angle}
        if preprocessing_version is not None:
            deskew_parameters["preprocessing_version"] = preprocessing_version
        transforms.append(
            transform_step("deskew", deskew_matrix, parameters=deskew_parameters)
        )
    operations = tuple(profile.get("preprocessing", ()))
    array = preprocess(array, operations)
    # The engine joins the artifact name only when it is not the legacy one,
    # by the same rule `render_profiles._derivative_key` applies to the
    # derivative identity: a PDFium render lands beside the MuPDF render of the
    # same page and profile, and no file an earlier render wrote moves.
    identity = {"profile_id": profile["profile_id"], "clip": clip_box}
    if rasterizer != LEGACY_RASTERIZER:
        identity["rasterizer"] = rasterizer
    if preprocessing_version is not None:
        identity["preprocessing_version"] = preprocessing_version
    suffix = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    artifact_path = output_dir / (
        f"{source_sha256}-p{int(request['page_number']):04d}-"
        f"{profile['name']}-{suffix}.png"
    )
    Image.fromarray(array).save(artifact_path, format="PNG")
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    composed = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for step in transforms:
        composed = multiply(tuple(step["forward"]), composed)
    reverse = inverse(composed)
    points = (
        (float(crop_box["x0"]), float(crop_box["y0"])),
        (float(crop_box["x1"]), float(crop_box["y1"])),
        (
            (crop_box["x0"] + crop_box["x1"]) / 2,
            (crop_box["y0"] + crop_box["y1"]) / 2,
        ),
    )
    round_trip_error = max(
        math.dist(point, apply(reverse, apply(composed, point))) for point in points
    )
    return {
        "schema_version": "corridor.render-derivative.v2",
        "rasterizer": rasterizer,
        "profile_name": profile["name"],
        "profile_id": profile["profile_id"],
        "dpi": profile["dpi"],
        "preprocessing": list(operations),
        "processing_steps": [
            {
                "name": operation,
                "geometry_change": False,
                "parameters": profile["parameters"],
            }
            for operation in operations
        ],
        "retention_class": "intermediary_processing",
        "regenerable_from": {
            "source_sha256": source_sha256,
            "page_number": int(request["page_number"]),
            "profile_id": profile["profile_id"],
        },
        "artifact_path": str(artifact_path.resolve()),
        "artifact_sha256": artifact_sha256,
        "artifact_bytes": artifact_path.stat().st_size,
        "media_box": media_box,
        "crop_box": crop_box,
        "clip_box": request.get("clip_page_box"),
        "rotation_degrees": rotation,
        "raster_width": int(array.shape[1]),
        "raster_height": int(array.shape[0]),
        "transforms": transforms,
        "max_round_trip_error": round_trip_error,
        "measured_tolerance": 0.001,
        "library_versions": {
            **engine_versions,
            "opencv": cv2.__version__,
            "pillow": pillow_version,
            "numpy": np.__version__,
        },
        "parameters": {
            **profile["parameters"],
            **({"preprocessing_version": preprocessing_version} if preprocessing_version is not None else {}),
        },
    }


def main(argv=None):
    """Render every requested derivative in this one process.

    Importing OpenCV, Pillow, the rasterizer and NumPy costs about 0.27s, which is
    roughly 70% of what rendering one ordinary page costs. Ingest wants three
    profiles of the same page, so the request carries a list and the caller
    pays that import once instead of three times (#548). A one-element list is
    exactly the old behavior.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    payload = json.loads(arguments.request.read_text())
    manifests = [render(request) for request in payload["requests"]]
    arguments.manifest.parent.mkdir(parents=True, exist_ok=True)
    arguments.manifest.write_text(
        json.dumps({"manifests": manifests}, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
