"""Engine-neutral deterministic and cross-engine evaluators."""

from __future__ import annotations

import copy
import math
import subprocess
from typing import Any


def compare(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Compare deterministic layers; performance observations are excluded."""
    if "deterministic_output" not in left or "deterministic_output" not in right:
        layers = {
            "containment_status": left.get("containment_status") == right.get("containment_status"),
            "error_class": left.get("error_class") == right.get("error_class"),
        }
        return {
            "equal": (
                all(layers.values())
                and left.get("containment_status") is not None
                and left.get("error_class") is not None
                and "deterministic_output" not in left
                and "deterministic_output" not in right
            ),
            "layers": layers,
        }
    ld, rd = left["deterministic_output"], right["deterministic_output"]
    layers = {
        "source_digest": ld.get("source_sha256") == rd.get("source_sha256"),
        "engine_identity": all(ld.get(k) == rd.get(k) and bool(ld.get(k)) for k in ("engine", "engine_version", "bundled_engine_version", "adapter", "adapter_version")),
        "error_class": [e.get("classification") for e in ld.get("errors", [])] == [e.get("classification") for e in rd.get("errors", [])],
        "capabilities": ld.get("unsupported_capabilities") == rd.get("unsupported_capabilities"),
        "page_geometry": _project(ld, ("width_points", "height_points", "media_box", "crop_box", "rotation_degrees", "user_unit")) == _project(rd, ("width_points", "height_points", "media_box", "crop_box", "rotation_degrees", "user_unit")),
        "text": _project(ld, ("characters", "words", "lines")) == _project(rd, ("characters", "words", "lines")),
        "tables": _project(ld, ("tables",)) == _project(rd, ("tables",)),
        "vectors": _project(ld, ("vector_paths",)) == _project(rd, ("vector_paths",)),
        "renders": _project(ld, ("renders",)) == _project(rd, ("renders",)),
    }
    return {"equal": all(layers.values()) and left["repeatability_sha256"] == right["repeatability_sha256"], "layers": layers}


def _project(output: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{key: page[key] for key in keys} for page in output.get("pages", [])]


def validate_capability_claims(receipt: dict[str, Any]) -> list[str]:
    errors = []
    output = receipt["deterministic_output"]
    unsupported = set(output["unsupported_capabilities"])
    for page in output.get("pages", []):
        for name in ("characters", "words", "lines", "tables", "vector_paths", "renders"):
            outcome = page[name]
            if outcome["status"] == "unsupported" and name not in unsupported:
                errors.append(f"{name}: unsupported outcome absent from capability list")
            if outcome["status"] == "supported" and name in unsupported:
                errors.append(f"{name}: reported both supported and unsupported")
    return errors


def changed_copy(receipt: dict[str, Any], path: tuple[object, ...], value: Any) -> dict[str, Any]:
    result = copy.deepcopy(receipt); target: Any = result
    for part in path[:-1]: target = target[part]
    target[path[-1]] = value
    return result


def evaluate_expected(receipt: dict[str, Any], expected: dict[str, Any]) -> dict[str, bool]:
    """Score frozen fixture facts without substituting them for Stage 0 gold."""
    output = receipt["deterministic_output"]
    pages = output.get("pages", [])
    visible = "\n".join(line["text"] for page in pages for line in page["lines"].get("value", []))
    cells = [cell["text"] for page in pages for table in page["tables"].get("value", []) for cell in table["cells"]]
    return {
        "normalized_text_exact": " ".join(visible.split()) == " ".join(expected.get("text", "").split()),
        "confirmed_cell_text_exact": all(text in cells for text in expected.get("confirmed_cell_text", [])),
        "page_dimensions_rotation": [(p["width_points"], p["height_points"], p["rotation_degrees"]["value"]) for p in pages] == expected.get("pages", []),
        "rendered_dimensions": [[(r["width_pixels"], r["height_pixels"]) for r in p["renders"]["value"]] for p in pages] == expected.get("renders", []),
        "error_class_consistent": [error["classification"] for error in output["errors"]] == expected.get("error_classes", []),
    }


def evaluate_stage0(gold: Any, engine_run: Any) -> Any:
    """Delegate table/page/row/cell assignment to #439's unchanged evaluator."""
    from corridor.pdf_evaluation import evaluate

    return evaluate(gold, engine_run)


def geometry_contract(left_page: dict[str, Any], right_page: dict[str, Any]) -> dict[str, Any]:
    """Declare whether two normalized, displayed-page coordinate models compare.

    Comparability requires supported MediaBox, CropBox, and rotation, identical
    displayed dimensions and rotation, and finite boxes. Adapters normalize text
    geometry to top-left PDF points before this engine-neutral layer runs.
    """
    reasons = []
    for name in ("media_box", "crop_box", "rotation_degrees"):
        if left_page[name]["status"] != "supported" or right_page[name]["status"] != "supported":
            reasons.append(f"{name} unsupported")
    if (left_page["width_points"], left_page["height_points"]) != (right_page["width_points"], right_page["height_points"]):
        reasons.append("displayed page dimensions differ")
    if (left_page["rotation_degrees"].get("value") !=
            right_page["rotation_degrees"].get("value")):
        reasons.append("rotation differs")
    for page in (left_page, right_page):
        for name in ("media_box", "crop_box"):
            if page[name]["status"] == "supported" and not all(
                math.isfinite(page[name]["value"][edge]) for edge in ("x0", "y0", "x1", "y1")
            ):
                reasons.append(f"{name} has non-finite coordinates")
    return {"comparable": not reasons, "coordinate_space": "top-left PDF points",
            "reasons": sorted(set(reasons))}


def polygon_disagreement(left: list[tuple[float, float]],
                         right: list[tuple[float, float]]) -> dict[str, float]:
    """Return polygon IoU and maximum corresponding-coordinate disagreement.

    The fixtures and engine schema currently emit axis-aligned topology boxes;
    this dependency-free evaluator accepts their four-corner polygons and any
    other convex polygons. Corresponding-edge disagreement is defined only for
    equal vertex counts and is therefore reported as infinity otherwise.
    """
    intersection = _convex_clip(left, right)
    left_area, right_area = abs(_area(left)), abs(_area(right))
    intersection_area = abs(_area(intersection)) if len(intersection) >= 3 else 0.0
    union = left_area + right_area - intersection_area
    disagreement = (max(max(abs(a[0]-b[0]), abs(a[1]-b[1])) for a, b in zip(left, right))
                    if len(left) == len(right) and left else math.inf)
    return {"iou": intersection_area / union if union else 1.0,
            "max_coordinate_disagreement_points": disagreement}


def image_difference(left: bytes, right: bytes, *, channels: int = 3) -> dict[str, float]:
    """Compare equally shaped unpacked raster samples with MAE and global SSIM."""
    if len(left) != len(right) or not left or channels not in (1, 3, 4):
        raise ValueError("raster samples must be non-empty, equally shaped, unpacked pixels")
    a = list(left); b = list(right)
    mae = sum(abs(x-y) for x, y in zip(a, b)) / len(a)
    mean_a, mean_b = sum(a)/len(a), sum(b)/len(b)
    variance_a = sum((x-mean_a)**2 for x in a) / len(a)
    variance_b = sum((x-mean_b)**2 for x in b) / len(b)
    covariance = sum((x-mean_a)*(y-mean_b) for x, y in zip(a, b)) / len(a)
    c1, c2 = (0.01*255)**2, (0.03*255)**2
    ssim = ((2*mean_a*mean_b+c1)*(2*covariance+c2) /
            ((mean_a**2+mean_b**2+c1)*(variance_a+variance_b+c2)))
    return {"mean_absolute_pixel_difference": mae, "ssim": ssim,
            "different_sample_fraction": sum(x != y for x, y in zip(a, b))/len(a)}


def tesseract_tokens(image_path: str) -> list[str]:
    """Run Corridor's existing deterministic Tesseract language/PSM configuration."""
    completed = subprocess.run(
        ["tesseract", image_path, "stdout", "-l", "eng", "--oem", "3", "--psm", "6"],
        check=True, capture_output=True, text=True,
    )
    return completed.stdout.split()


def _area(points: list[tuple[float, float]]) -> float:
    return sum(x1*y2-x2*y1 for (x1,y1),(x2,y2) in zip(points, points[1:]+points[:1]))/2


def _convex_clip(subject: list[tuple[float, float]], clip: list[tuple[float, float]]) -> list[tuple[float, float]]:
    output = subject[:]
    orientation = 1 if _area(clip) >= 0 else -1
    for edge_start, edge_end in zip(clip, clip[1:]+clip[:1]):
        source, output = output, []
        for current, following in zip(source, source[1:]+source[:1]):
            current_inside = orientation*((edge_end[0]-edge_start[0])*(current[1]-edge_start[1])-(edge_end[1]-edge_start[1])*(current[0]-edge_start[0])) >= 0
            following_inside = orientation*((edge_end[0]-edge_start[0])*(following[1]-edge_start[1])-(edge_end[1]-edge_start[1])*(following[0]-edge_start[0])) >= 0
            if current_inside: output.append(current)
            if current_inside != following_inside:
                dx, dy = following[0]-current[0], following[1]-current[1]
                ex, ey = edge_end[0]-edge_start[0], edge_end[1]-edge_start[1]
                denominator = dx*ey-dy*ex
                if denominator:
                    t = ((edge_start[0]-current[0])*ey-(edge_start[1]-current[1])*ex)/denominator
                    output.append((current[0]+t*dx, current[1]+t*dy))
        if not output: break
    return output
