"""The cache is read before every call; throttling is retried; the budget holds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corridor_pdf_reader.textract.client import (
    FEATURE_TYPES,
    AnalysisFailed,
    BudgetExceeded,
    PendingAnalysis,
    TextractClient,
    cache_entry,
    cache_path,
    read_entry,
    write_entry,
)


class ClientError(Exception):
    """botocore's shape: a `response` with an Error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeService:
    def __init__(self, failures: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures = list(failures or [])

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.calls.append({"bytes": Document["Bytes"], "features": FeatureTypes})
        if self.failures:
            raise ClientError(self.failures.pop(0))
        return {"DocumentMetadata": {"Pages": 1}, "Blocks": [{"BlockType": "PAGE", "Id": "p"}], "AnalyzeDocumentModelVersion": "1.0"}


def test_a_miss_calls_once_and_a_repeat_reads_the_cache(tmp_path: Path) -> None:
    service = FakeService()
    client = TextractClient(tmp_path / "cache", service, sleep=lambda s: None)
    first = client.analyze(b"png-bytes")
    assert not first.cached and first.response["Blocks"][0]["Id"] == "p"
    assert service.calls[0]["features"] == FEATURE_TYPES and service.calls[0]["bytes"] == b"png-bytes"
    entry = json.loads(cache_path(tmp_path / "cache", first.sha256).read_text())
    assert entry["sha256"] == first.sha256 and entry["bytes"] == 9 and entry["response"] == first.response and entry["attempts"] == 1
    second = client.analyze(b"png-bytes")
    assert second.cached and second.response == first.response
    assert len(service.calls) == 1 and client.sent == 1 and client.hits == 1 and client.dollars == 0.015
    client.analyze(b"other-bytes")
    assert len(service.calls) == 2 and client.sent == 2


def test_throttling_is_retried_with_backoff(tmp_path: Path) -> None:
    service = FakeService(["ThrottlingException", "ProvisionedThroughputExceededException"])
    naps: list[float] = []
    client = TextractClient(tmp_path, service, sleep=naps.append)
    analysis = client.analyze(b"x")
    assert len(service.calls) == 3 and len(naps) == 2 and naps[1] > 0
    assert read_entry(tmp_path, analysis.sha256)["attempts"] == 3  # type: ignore[index]


def test_other_errors_are_not_retried(tmp_path: Path) -> None:
    service = FakeService(["InvalidParameterException"])
    client = TextractClient(tmp_path, service, sleep=lambda s: None)
    with pytest.raises(AnalysisFailed, match="InvalidParameterException"):
        client.analyze(b"x")
    assert len(service.calls) == 1 and client.sent == 0 and not list(tmp_path.glob("*.json"))


def test_the_page_budget_stops_calls_but_not_cache_reads(tmp_path: Path) -> None:
    service = FakeService()
    client = TextractClient(tmp_path, service, max_pages=1, sleep=lambda s: None)
    client.analyze(b"one")
    with pytest.raises(BudgetExceeded):
        client.analyze(b"two")
    assert client.analyze(b"one").cached and len(service.calls) == 1


def test_offline_names_the_misses_and_never_calls(tmp_path: Path) -> None:
    service = FakeService()
    write_entry(tmp_path, cache_entry("f" * 64, 1, {"Blocks": []}, transport="test", attempts=1))
    client = TextractClient(tmp_path, service, offline=True)
    with pytest.raises(PendingAnalysis) as caught:
        client.analyze(b"never sent")
    assert client.pending == [caught.value.sha256] and not service.calls


def test_a_truncated_entry_is_a_miss(tmp_path: Path) -> None:
    service = FakeService()
    client = TextractClient(tmp_path, service, sleep=lambda s: None)
    sha = client.analyze(b"bytes").sha256
    cache_path(tmp_path, sha).write_text('{"sha256": "')
    assert not client.analyze(b"bytes").cached and len(service.calls) == 2
