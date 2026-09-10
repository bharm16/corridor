"""Extractor-time configuration receipts bind deployed source and usage."""

from __future__ import annotations

import pytest

from corridor.extractor_lineage import (
    deployed_extractor_config,
    injected_extractor_config,
    token_usage_delta,
    usage_snapshot,
)
from corridor.llm import Usage


def test_injected_configuration_seals_exact_sources_and_request_controls():
    config = injected_extractor_config(
        extractor="fixture",
        prompt_version="fixture_v1",
        model="fixture-model",
        schema_version="fixture-shape-v1",
        prompt_bytes=b"prompt bytes\n",
        schema={"type": "object"},
        postprocessor_bytes=b"postprocessor rules\n",
        request_controls={
            "reasoning_effort": "none",
            "image_detail": None,
            "store": False,
            "strict": True,
            "flex": False,
        },
        runtime={
            "python_implementation": "CPython",
            "python_version": "3.12.0",
            "dependency_lock_sha256": "9" * 64,
            "packages": {},
        },
    )

    assert config.prompt_sha256 == (
        "dab778ba95a65172a396d30b73a6b898dec9e616ae62cb79bf8c2fc57466469c"
    )
    assert config.schema_sha256 == (
        "a2c799262a3ce3c19ef5cdd983bf3d12b43ab3c426227091b909dcb7054738c0"
    )
    assert config.postprocessor_sha256 == (
        "73a4aa46d55dc1771eeff617a4d09bdd02a63520c91f1c09435a79d8d61d9817"
    )
    assert config.config_json == {
        "receipt_version": 1,
        "extractor": "fixture",
        "prompt_version": "fixture_v1",
        "model": "fixture-model",
        "schema_version": "fixture-shape-v1",
        "prompt_sha256": config.prompt_sha256,
        "schema_sha256": config.schema_sha256,
        "postprocessor_sha256": config.postprocessor_sha256,
        "request_controls": {
            "reasoning_effort": "none",
            "image_detail": None,
            "store": False,
            "strict": True,
            "flex": False,
        },
        "runtime": {
            "python_implementation": "CPython",
            "python_version": "3.12.0",
            "dependency_lock_sha256": "9" * 64,
            "packages": {},
        },
    }
    assert config.config_sha256 == (
        "df24409fefd6b48cd97a823aab7f354cb2c671e14f645d4898f2c3fa1ef97a21"
    )


def test_usage_receipt_reports_one_exact_batch_instead_of_allocating_tokens():
    class Client:
        usage = Usage(
            prompt_tokens=1_400,
            completion_tokens=260,
            reasoning_tokens=30,
            cached_tokens=600,
        )

    client = Client()
    before = usage_snapshot(client)
    client.usage.prompt_tokens += 600
    client.usage.completion_tokens += 140
    client.usage.reasoning_tokens += 20
    client.usage.cached_tokens += 200

    receipt = token_usage_delta(
        before,
        usage_snapshot(client),
        document_ids=[1435, 1438],
    )

    assert receipt == {
        "scope": "batch",
        "document_ids": [1435, 1438],
        "measurement": "exact",
        "prompt_tokens": 600,
        "completion_tokens": 140,
        "reasoning_tokens": 20,
        "cached_tokens": 200,
    }
    assert "allocated_prompt_tokens" not in receipt


def test_usage_receipt_refuses_a_counter_that_moves_backwards():
    with pytest.raises(ValueError, match="moved backwards"):
        token_usage_delta(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            {
                "prompt_tokens": 9,
                "completion_tokens": 5,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            document_ids=[1435],
        )

    with pytest.raises(ValueError, match="positive integer"):
        token_usage_delta(
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            document_ids=[1435, 0],
        )


@pytest.mark.parametrize(
    ("extractor", "prompt_version", "schema_version", "uses_images"),
    [
        ("minutes", "minutes_v5", "minutes_v5", False),
        ("agreement", "agreement_v3", "agreement_v3", False),
        ("sheet", "sheet_native_v2", "sheet_candidate_shape_v1", False),
    ],
)
def test_deployed_registry_seals_each_current_extractor(
    extractor, prompt_version, schema_version, uses_images
):
    class Client:
        model = "gpt-fixture"
        effort = "low"
        flex = True
        base_url = "https://provider.example/v1/"

    config = deployed_extractor_config(
        extractor,
        client=None if extractor == "sheet" else Client(),
    )

    assert config.prompt_version == prompt_version
    assert config.schema_version == schema_version
    assert config.model == (None if extractor == "sheet" else "gpt-fixture")
    assert all(
        len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        for digest in (
            config.prompt_sha256,
            config.schema_sha256,
            config.postprocessor_sha256,
            config.config_sha256,
        )
    )
    controls = config.config_json["request_controls"]
    runtime = config.config_json["runtime"]
    assert runtime["python_implementation"]
    assert runtime["python_version"]
    assert len(runtime["dependency_lock_sha256"]) == 64
    assert {"pypdfium2", "pypdf", "httpx", "openpyxl"} <= set(runtime["packages"])
    if extractor == "sheet":
        assert controls == {"provider": "native", "model_requests": 0}
    else:
        assert controls["reasoning_effort"] == "low"
        assert controls["store"] is False
        assert controls["strict"] is True
        assert controls["flex"] is True
        assert controls["provider_base_url"] == "https://provider.example/v1"
        assert "api_key" not in controls
        assert "cache_key" not in controls
        assert (controls["image_detail"] == "original") is uses_images


def test_deployed_registry_refuses_a_provider_url_that_contains_credentials():
    class Client:
        model = "gpt-fixture"
        effort = "none"
        flex = False
        base_url = "https://secret-token@provider.example/v1"

    with pytest.raises(ValueError, match="must not contain credentials"):
        deployed_extractor_config("agreement", client=Client())


def test_the_deployed_minutes_registry_seals_the_current_version_only():
    """The registry seals what is deployed; history lives in stored receipts.

    Retired versions had entries here purely so a test could ask for them.
    This module's contract is that a reader compares the receipt it stored
    and never reopens today's files, so an unreachable branch could only
    reconstruct a configuration that never ran.
    """

    class Client:
        model = "gpt-fixture"
        effort = "low"
        flex = False
        base_url = "https://provider.example/v1"

    current = deployed_extractor_config("minutes", client=Client())
    assert current.prompt_version == "minutes_v5"
    assert current.config_json["extractor"] == "minutes"

    with pytest.raises(ValueError):
        deployed_extractor_config("minutes_v4", client=Client())


def test_injected_configuration_refuses_unverifiable_runtime_identity():
    with pytest.raises(ValueError, match="runtime lock SHA-256"):
        injected_extractor_config(
            extractor="fixture",
            prompt_version="fixture-v1",
            model=None,
            schema_version="fixture-shape-v1",
            prompt_bytes=b"fixture prompt",
            schema={"type": "object"},
            postprocessor_bytes=b"fixture rules",
            request_controls={"provider": "fixture"},
            runtime={
                "python_implementation": "CPython",
                "python_version": "3.12.0",
                "dependency_lock_sha256": "not-a-digest",
                "packages": {},
            },
        )

    with pytest.raises(ValueError, match="python_implementation"):
        injected_extractor_config(
            extractor="fixture",
            prompt_version="fixture-v1",
            model=None,
            schema_version="fixture-shape-v1",
            prompt_bytes=b"fixture prompt",
            schema={"type": "object"},
            postprocessor_bytes=b"fixture rules",
            request_controls={"provider": "fixture"},
            runtime={},
        )
