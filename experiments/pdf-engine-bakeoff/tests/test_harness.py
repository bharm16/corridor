"""Issue #721 incumbent adapter, containment, and mutation evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

EXPERIMENT = Path(__file__).parents[1]
sys.path.insert(0, str(EXPERIMENT))

from contract import ContractError, repeatability_digest, validate_result  # noqa: E402
from evaluators import (compare, evaluate_expected, geometry_contract,
                        image_difference, polygon_disagreement,
                        validate_capability_claims)  # noqa: E402
from fixtures import generate  # noqa: E402
from harness import run, run_subprocess  # noqa: E402
from registry import REAL_ENGINES  # noqa: E402


def _strict(receipt):
    return {key: value for key, value in receipt.items() if key != "operation_observation"}


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pdf-bakeoff")
    definitions = generate(directory); source = directory / "geometry-and-tables.pdf"
    request = {"engine": "pymupdf", "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "clip": definitions[source.name]["clip"]}
    return run_subprocess(request, repetition=1), run_subprocess(request, repetition=2)


def test_registry_contains_all_three_isolated_engines():
    assert REAL_ENGINES == ("pymupdf", "pdf_oxide", "pdfium")
    assert "pdf_oxide" not in sys.modules
    assert "pypdfium2" not in sys.modules


def test_smoke_run_resolves_generated_sources_and_produces_pages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = Path("relative-output")
    assert run(output) == 0
    receipts = [
        json.loads(path.read_text())
        for path in (output / "receipts").glob("*.json")
    ]
    assert receipts
    assert all(receipt["deterministic_output"]["pages"] for receipt in receipts)
    assert all(receipt["deterministic_output"]["status"] == "success" for receipt in receipts)


def test_all_failed_run_exits_nonzero_and_refuses_repeatability(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        return {"deterministic_output": {"status": "failed", "pages": [], "errors": [
            {"classification": "adapter_failure", "message": "broken"}
        ]}}

    monkeypatch.setattr("harness.run_subprocess", failed)
    output = tmp_path / "failed"
    assert run(output) == 1
    report = (output / "report.md").read_text()
    assert "Receipts: **0 succeeded, 24 failed**" in report
    assert "Failure: one or more receipts failed." in report
    assert "Failure: every receipt failed." in report
    assert "REFUSED — no successful pages" in report
    assert "| yes |" not in report


@pytest.mark.parametrize("engine", REAL_ENGINES)
def test_each_engine_runs_generated_fixture_and_is_repeatable(tmp_path, engine):
    generate(tmp_path); source = tmp_path / "borderless.pdf"
    request = {"engine": engine, "source": str(source),
               "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    left, right = run_subprocess(request), run_subprocess(request)
    assert left["deterministic_output"]["status"] == "success"
    assert compare(left, right)["equal"]
    assert validate_capability_claims(left) == []


def test_independent_runs_are_reflexive_and_observations_are_separate(pair):
    left, right = pair
    validate_result(_strict(left)); validate_result(_strict(right))
    comparison = compare(left, right)
    assert comparison["equal"]
    assert all(comparison["layers"].values())
    assert left["repeatability_sha256"] == right["repeatability_sha256"]
    assert "operation_observation" in left
    assert "operation_observation" not in _strict(left)


def test_mutations_detect_coordinate_source_digest_and_identity(pair):
    left, right = pair
    coordinate = copy.deepcopy(right)
    coordinate["deterministic_output"]["pages"][0]["words"]["value"][0]["box"]["x0"] += 1
    coordinate["repeatability_sha256"] = repeatability_digest(coordinate["deterministic_output"])
    assert not compare(left, coordinate)["equal"]
    digest = copy.deepcopy(right); digest["deterministic_output"]["source_sha256"] = "f" * 64
    digest["repeatability_sha256"] = repeatability_digest(digest["deterministic_output"])
    assert not compare(left, digest)["equal"]
    identity = copy.deepcopy(right); identity["deterministic_output"].pop("engine_version")
    identity["repeatability_sha256"] = repeatability_digest(identity["deterministic_output"])
    with pytest.raises(ContractError, match="missing fields"):
        validate_result(_strict(identity))


def test_mutation_unsupported_cannot_be_reported_as_success(pair):
    receipt = copy.deepcopy(pair[0])
    receipt["deterministic_output"]["unsupported_capabilities"] = ["tables"]
    assert validate_capability_claims(receipt) == ["tables: reported both supported and unsupported"] * len(receipt["deterministic_output"]["pages"])


@pytest.mark.parametrize("engine", REAL_ENGINES)
def test_unsupported_as_successful_mutation_is_detected_for_every_engine(tmp_path, engine):
    generate(tmp_path); source = tmp_path / "borderless.pdf"
    receipt = run_subprocess({"engine": engine, "source": str(source),
                              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    capability = "tables" if engine == "pdfium" else "characters"
    output = receipt["deterministic_output"]
    output["unsupported_capabilities"] = sorted(set(output["unsupported_capabilities"] + [capability]))
    output["pages"][0][capability] = {"status": "supported", "value": []}
    assert f"{capability}: reported both supported and unsupported" in validate_capability_claims(receipt)


def test_cross_engine_geometry_and_pixel_evaluators():
    box = lambda x0, y0, x1, y1: [(x0,y0),(x1,y0),(x1,y1),(x0,y1)]
    result = polygon_disagreement(box(0, 0, 10, 10), box(1, 0, 11, 10))
    assert result["iou"] == pytest.approx(9 / 11)
    assert result["max_coordinate_disagreement_points"] == 1
    assert image_difference(bytes([0, 10, 20]), bytes([0, 10, 20])) == {
        "mean_absolute_pixel_difference": 0, "ssim": 1,
        "different_sample_fraction": 0,
    }


def test_geometry_contract_refuses_incommensurable_pages(pair):
    left = pair[0]["deterministic_output"]["pages"][0]
    right = copy.deepcopy(left)
    assert geometry_contract(left, right)["comparable"]
    right["rotation_degrees"]["value"] = 90
    refusal = geometry_contract(left, right)
    assert not refusal["comparable"] and "rotation differs" in refusal["reasons"]


def test_single_engine_exact_text_dimensions_renders_and_error_class(pair):
    receipt = pair[0]; pages = receipt["deterministic_output"]["pages"]
    text = "\n".join(line["text"] for page in pages for line in page["lines"]["value"])
    expected = {
        "text": text,
        "confirmed_cell_text": ["confirmed cell"],
        "pages": [(page["width_points"], page["height_points"], page["rotation_degrees"]["value"]) for page in pages],
        "renders": [[(render["width_pixels"], render["height_pixels"]) for render in page["renders"]["value"]] for page in pages],
        "error_classes": [],
    }
    assert all(evaluate_expected(receipt, expected).values())


def test_native_abort_is_retained_without_terminating_parent(tmp_path):
    result = run_subprocess({}, worker=EXPERIMENT / "crash_stub.py", timeout=5)
    assert result["containment_status"] == "crash"
    assert result["error_class"] == "native_crash"
    assert result["returncode"] != 0
    assert tmp_path.exists()  # the pytest process survived the abort


def test_missing_isolated_interpreter_is_not_an_engine_crash(monkeypatch, tmp_path):
    missing = tmp_path / "missing-python"
    monkeypatch.setattr("harness.ISOLATED_PYTHON", missing)
    with pytest.raises(RuntimeError) as raised:
        run_subprocess({})
    assert str(missing) in str(raised.value)
    assert "uv sync --project experiments/pdf-engine-bakeoff --frozen --no-dev" in str(raised.value)
    assert "native_crash" not in str(raised.value)


def test_crash_envelopes_compare_by_containment_and_error_class():
    left = {"containment_status": "crash", "error_class": "native_crash"}
    right = {"containment_status": "crash", "error_class": "native_crash"}
    comparison = compare(left, right)
    assert comparison == {
        "equal": True,
        "layers": {"containment_status": True, "error_class": True},
    }


def test_malformed_and_encrypted_error_classes_are_deterministic(tmp_path):
    definitions = generate(tmp_path)
    malformed = tmp_path / "malformed-truncated.pdf"
    request = {"engine": "pymupdf", "source": str(malformed), "source_sha256": hashlib.sha256(malformed.read_bytes()).hexdigest()}
    left, right = run_subprocess(request), run_subprocess(request)
    assert compare(left, right)["layers"]["error_class"]
    assert left["deterministic_output"]["status"] == "failed"
    encrypted = tmp_path / "encrypted.pdf"
    encrypted_request = {"engine": "pymupdf", "source": str(encrypted), "source_sha256": hashlib.sha256(encrypted.read_bytes()).hexdigest()}
    assert run_subprocess(encrypted_request)["deterministic_output"]["errors"][0]["classification"] == "encrypted"
    encrypted_request["password"] = definitions[encrypted.name]["password"]
    assert run_subprocess(encrypted_request)["deterministic_output"]["status"] == "success"
