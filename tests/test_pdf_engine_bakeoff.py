"""Issue #721 incumbent adapter, containment, and mutation evidence."""

from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "experiments/pdf-engine-bakeoff"
sys.path.insert(0, str(EXPERIMENT))

from contract import ContractError, repeatability_digest, validate_result  # noqa: E402
from evaluators import compare, evaluate_expected, validate_capability_claims  # noqa: E402
from fixtures import generate  # noqa: E402
from harness import run_subprocess  # noqa: E402
from registry import REAL_ENGINES  # noqa: E402


def _strict(receipt):
    return {key: value for key, value in receipt.items() if key != "operation_observation"}


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pdf-bakeoff")
    definitions = generate(directory); source = directory / "geometry-and-tables.pdf"
    request = {"engine": "pymupdf", "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "clip": definitions[source.name]["clip"]}
    return run_subprocess(request, repetition=1), run_subprocess(request, repetition=2)


def test_registry_contains_exactly_one_real_incumbent():
    assert REAL_ENGINES == ("pymupdf",)
    assert "pdf_oxide" not in sys.modules
    assert "pypdfium2" not in sys.modules


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
