"""The cached, retried AnalyzeDocument call.

Every raw response is kept as JSON under the cache directory, named by the
sha256 of the bytes sent, and the cache is read before every call: re-runs
and re-scoring cost nothing, and only a miss reaches Textract. Throttling is
retried with backoff. A page budget caps what one process may send, and an
offline client never calls at all: it names the misses so another transport
can fill the cache in the same shape.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

FEATURE_TYPES = ["TABLES"]
# AnalyzeDocument with Tables in us-east-2, first million pages a month;
# Layout is free with Tables. The Free Tier covers 100 pages a month.
DOLLARS_PER_PAGE = 0.015
COST_CAP_DOLLARS = 5.0
MAX_PAGES_DEFAULT = int(COST_CAP_DOLLARS / DOLLARS_PER_PAGE)
RETRYABLE = ("ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError")
MAX_ATTEMPTS = 8
BACKOFF_CAP_SECONDS = 20.0
DEFAULT_PROFILE = "corridor"
DEFAULT_REGION = "us-east-2"


class AnalyzeDocumentService(Protocol):
    """What the client needs of boto3's Textract client."""

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]: ...


class PendingAnalysis(LookupError):
    """The bytes have no cached response and the client is offline."""

    def __init__(self, sha256: str) -> None:
        super().__init__(sha256)
        self.sha256 = sha256


class BudgetExceeded(RuntimeError):
    """Sending one more page would pass the run's page budget."""


class AnalysisFailed(RuntimeError):
    """Textract refused the page, or kept throttling past every retry."""


@dataclass(frozen=True)
class Analysis:
    sha256: str
    response: dict[str, Any]
    cached: bool


def cache_path(cache: Path, sha256: str) -> Path:
    return cache / f"{sha256}.json"


def cache_entry(sha256: str, size: int, response: dict[str, Any], *, transport: str, attempts: int) -> dict[str, Any]:
    """The shape every cached response has, whatever transport fetched it."""
    return {
        "sha256": sha256,
        "bytes": size,
        "feature_types": list(FEATURE_TYPES),
        "requested_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "transport": transport,
        "attempts": attempts,
        "response": response,
    }


def write_entry(cache: Path, entry: dict[str, Any]) -> Path:
    """Write atomically, so an interrupted run never leaves a truncated entry."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache_path(cache, entry["sha256"])
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(entry) + "\n")
    os.replace(temporary, target)
    return target


def read_entry(cache: Path, sha256: str) -> dict[str, Any] | None:
    path = cache_path(cache, sha256)
    if not path.exists():
        return None
    try:
        entry: dict[str, Any] = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if "response" not in entry:
        return None
    return entry


def error_code(exc: BaseException) -> str:
    """botocore's error code, read without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def backoff_seconds(attempt: int) -> float:
    return min(BACKOFF_CAP_SECONDS, 0.5 * 2**attempt) * (0.5 + random.random())


class TextractClient:
    def __init__(
        self,
        cache: Path,
        service: AnalyzeDocumentService | None = None,
        *,
        max_pages: int = MAX_PAGES_DEFAULT,
        offline: bool = False,
        profile: str = DEFAULT_PROFILE,
        region: str = DEFAULT_REGION,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = cache
        self._service = service
        self.max_pages = max_pages
        self.offline = offline
        self.profile = profile
        self.region = region
        self.sleep = sleep
        self.sent = 0
        self.hits = 0
        self.pending: list[str] = []

    @property
    def dollars(self) -> float:
        return round(self.sent * DOLLARS_PER_PAGE, 3)

    def service(self) -> AnalyzeDocumentService:
        if self._service is None:
            import boto3

            session = boto3.Session(profile_name=self.profile, region_name=self.region)
            self._service = session.client("textract")
        return self._service

    def analyze(self, png: bytes) -> Analysis:
        """The cached response for these bytes, or one call to Textract."""
        sha256 = hashlib.sha256(png).hexdigest()
        entry = read_entry(self.cache, sha256)
        if entry is not None:
            self.hits += 1
            return Analysis(sha256, entry["response"], True)
        if self.offline:
            if sha256 not in self.pending:
                self.pending.append(sha256)
            raise PendingAnalysis(sha256)
        if self.sent >= self.max_pages:
            raise BudgetExceeded(f"{self.sent} pages sent, the budget of {self.max_pages}")
        response, attempts = self._call(png)
        self.sent += 1
        write_entry(self.cache, cache_entry(sha256, len(png), response, transport=f"boto3:{self.profile}:{self.region}", attempts=attempts))
        return Analysis(sha256, response, False)

    def _call(self, png: bytes) -> tuple[dict[str, Any], int]:
        service = self.service()
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = service.analyze_document(Document={"Bytes": png}, FeatureTypes=list(FEATURE_TYPES))
                return response, attempt + 1
            except Exception as exc:
                code = error_code(exc)
                if code not in RETRYABLE:
                    raise AnalysisFailed(f"{code or type(exc).__name__}: {exc}") from exc
                last = f"{code}: {exc}"
                self.sleep(backoff_seconds(attempt))
        raise AnalysisFailed(f"gave up after {MAX_ATTEMPTS} attempts: {last}")
