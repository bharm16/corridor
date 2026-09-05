"""Issue #720's isolated schema and manifests are strict and self-contained."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "experiments/pdf-engine-bakeoff"
spec = importlib.util.spec_from_file_location("bakeoff_contract", EXPERIMENT / "contract.py")
contract = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(contract)


def supported(value):
    return {"status": "supported", "value": value}


def result():
    box = {"x0": 0, "y0": 0, "x1": 612, "y1": 792}
    page = {
        "page_number": 1, "width_points": 612, "height_points": 792,
        "media_box": supported(box), "crop_box": supported(box),
        "rotation_degrees": supported(0), "user_unit": supported(1),
        "characters": {"status": "unsupported", "reason": "adapter not implemented"},
        "words": supported([]), "lines": supported([]),
        "tables": {"status": "unsupported", "reason": "engine has no table API"},
        "vector_paths": {"status": "unsupported", "reason": "adapter not implemented"},
        "renders": supported([]),
    }
    deterministic = {
        "source_sha256": "a" * 64, "engine": "fixture", "engine_version": "1",
        "bundled_engine_version": "1", "adapter": "fixture", "adapter_version": "1",
        "status": "success", "metadata": supported({}), "pages": [page],
        "warnings": [], "errors": [],
        "unsupported_capabilities": ["characters", "tables", "vector_paths"],
    }
    receipt = {
        "schema_version": "corridor.pdf-engine-result.v1",
        "deterministic_output": deterministic,
        "repeatability_sha256": "0" * 64,
        "run_observation": {
            "host": {"hostname": "host", "os": "Linux", "architecture": "x86_64",
                     "cpu": "fixture", "cpu_quota": "1", "memory_limit_bytes": 1024,
                     "python_version": "3.12.13", "package_versions": {"fixture": "1"}},
            "cgroup": {}, "run_order": 1, "mode": "process-cold",
            "thread_mode": "single", "repetition": 1,
            "started_at": "2026-09-05T00:00:00Z", "process_start_ms": 1,
            "import_ms": 1, "operation_latency_ms": 1, "peak_rss_bytes": 1,
        },
    }
    receipt["repeatability_sha256"] = contract.repeatability_digest(deterministic)
    return receipt


def test_result_schema_rejects_undeclared_fields_at_every_layer():
    value = result()
    value["deterministic_output"]["pages"][0]["vendor_score"] = 0.99
    with pytest.raises(contract.ContractError, match="undeclared fields"):
        contract.validate_result(value)


def test_capability_cannot_claim_success_without_value_or_unsupported_reason():
    for ambiguous in ({"status": "supported"}, {"status": "unsupported"}, []):
        value = result()
        value["deterministic_output"]["pages"][0]["tables"] = ambiguous
        with pytest.raises(contract.ContractError, match="unambiguous schema alternative"):
            contract.validate_result(value)


def test_run_observation_does_not_change_deterministic_repeatability_input():
    value = result()
    deterministic_before = copy.deepcopy(value["deterministic_output"])
    value["run_observation"]["operation_latency_ms"] = 999
    contract.validate_result(value)
    assert value["deterministic_output"] == deterministic_before


def test_document_success_cannot_hide_a_failed_capability_or_partial_failure():
    value = result()
    value["deterministic_output"]["pages"][0]["tables"] = {
        "status": "failed", "error_class": "parse", "message": "bad table"
    }
    value["repeatability_sha256"] = contract.repeatability_digest(
        value["deterministic_output"]
    )
    with pytest.raises(contract.ContractError, match="success contains a failed outcome"):
        contract.validate_result(value)

    value = result()
    value["deterministic_output"]["status"] = "failed"
    value["deterministic_output"]["errors"] = [
        {"classification": "malformed", "message": "invalid xref"}
    ]
    value["repeatability_sha256"] = contract.repeatability_digest(
        value["deterministic_output"]
    )
    with pytest.raises(contract.ContractError, match="no partial pages"):
        contract.validate_result(value)


def test_manifests_pin_wheels_and_reuse_the_stage_zero_gold_digest():
    candidates = json.loads((EXPERIMENT / "candidate-manifest.v1.json").read_text())
    assert {item["version"] for item in candidates["candidates"]} == {"1.28.0", "0.3.77", "5.13.0"}
    assert all(item["wheel"]["platform_tag"].endswith("x86_64") for item in candidates["candidates"])
    assert all(len(item["wheel"]["sha256"]) == 64 for item in candidates["candidates"])
    corpus = json.loads((EXPERIMENT / "corpus-manifest.v1.json").read_text())
    assert corpus["stage0_contract"] == "src/corridor/pdf_evaluation.py"
    import hashlib
    dataset = ROOT / corpus["gold_dataset"]["path"]
    assert hashlib.sha256(dataset.read_bytes()).hexdigest() == corpus["gold_dataset"]["sha256"]
    assert "holdout" in {item["split"] for item in corpus["documents"]}
    assert "No holdout access" in corpus["holdout_policy"]
