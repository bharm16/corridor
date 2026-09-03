"""Isolated raster and OpenCV derivative worker.

The core application used one Pillow/PyMuPDF image for review, OCR, and table
work. This process owns engine dependencies and emits one identified derivative
plus a complete affine manifest. It has no database access and cannot mutate the
source PDF or any reviewer rendition.
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
import pymupdf


SCALE = 1_000


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


def fixed_box(rect):
    value = pymupdf.Rect(rect)
    return {
        "x0": round(value.x0 * SCALE),
        "y0": round(value.y0 * SCALE),
        "x1": round(value.x1 * SCALE),
        "y1": round(value.y1 * SCALE),
    }


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


def deskew(image):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    angle = detect_skew(gray)
    height, width = gray.shape[:2]
    center = (width / 2, height / 2)
    cv_matrix = cv2.getRotationMatrix2D(center, -angle, 1.0)
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


def render(request):
    source_path = Path(request["pdf_path"])
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    profile = request["profile"]
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source_path) as document:
        page = document[int(request["page_number"]) - 1]
        media_box = fixed_box(page.mediabox)
        crop_box = fixed_box(page.cropbox)
        rotation = int(page.rotation)
        pixmap = page.get_pixmap(dpi=int(profile["dpi"]), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

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

    pdf_to_page = (
        1.0,
        0.0,
        0.0,
        -1.0,
        -float(crop_box["x0"]),
        float(crop_box["y1"]),
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
        transform_step("pdf_user_to_page", pdf_to_page),
        transform_step("page_rotation", rotate, parameters={"degrees": rotation}),
        transform_step("page_to_clip", page_to_clip, parameters={"box": clip_box}),
        transform_step("clip_to_raster", clip_to_raster, parameters={"dpi": profile["dpi"]}),
    ]

    if profile["name"] == "table_cv":
        array, angle, deskew_matrix = deskew(array)
        transforms.append(
            transform_step("deskew", deskew_matrix, parameters={"angle_degrees": angle})
        )
    operations = tuple(profile.get("preprocessing", ()))
    array = preprocess(array, operations)
    suffix = hashlib.sha256(
        json.dumps(
            {"profile_id": profile["profile_id"], "clip": clip_box},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
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
        "schema_version": "corridor.render-derivative.v1",
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
            "pymupdf": pymupdf.VersionBind,
            "opencv": cv2.__version__,
            "pillow": pillow_version,
            "numpy": np.__version__,
        },
        "parameters": profile["parameters"],
    }


def main(argv=None):
    """Render every requested derivative in this one process.

    Importing OpenCV, Pillow, PyMuPDF and NumPy costs about 0.27s, which is
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
