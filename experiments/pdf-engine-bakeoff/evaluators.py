"""Incumbent reflexivity and single-engine correctness evaluators."""

from __future__ import annotations

import copy
from typing import Any


def compare(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Compare deterministic layers; performance observations are excluded."""
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
