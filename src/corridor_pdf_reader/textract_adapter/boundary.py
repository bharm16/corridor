"""The only path from Corridor to a Textract call (#732, ADR-0094).

Nothing else in Corridor may import the imported client or transport;
`tests/test_textract_adapter.py` fails if something does. `open_boundary`
takes the authorization record and the request's boundary, matches them on
every field, and refuses with a Processing Failure carrying the reason and
zero outbound requests when the record is absent or does not cover the
request. Only after that does it construct the imported client, and the
client is handed a scope directory named by the cache identity, a counting
proxy in place of the boto3 service, and the region the request named.

The proxy is the transport seam. Every outbound request passes through it
and is counted as a call (a response came back), a retry (a retryable error
the client will try again) or a failed attempt (anything else), so the cost
receipt can list calls, retries and hits separately and a Processing Failure
can say exactly how many requests went out before it. A cache hit never
reaches the proxy and is never a charge.

Every `analyze_page` binds the rendition and page it was asked about to the
response it used, whether that response was fetched now or read from the
scope: one raster shared by two renditions is two bindings and one charge,
and a response one Extraction Run fetched is a hit, not a charge, in the
next. Reusing a response across *different* boundaries is a governance
decision this module does not make: a different boundary is a different
scope, and the page is sent again under its own authorization.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from corridor_pdf_reader.textract.client import (
    DOLLARS_PER_PAGE,
    MAX_PAGES_DEFAULT,
    RETRYABLE,
    AnalysisFailed,
    BudgetExceeded,
    TextractClient,
    error_code,
    read_entry,
    write_entry,
)
from corridor_pdf_reader.textract.render import Raster
from corridor_pdf_reader.textract_adapter.identity import (
    CacheScope,
    NativeGlyphs,
    NormalizationConfiguration,
    RequestConfiguration,
    cache_scope,
    model_version,
    normalization_record,
    normalize,
    raw_response_digest,
    reading_counts,
    reading_digest,
    request_id,
)
from corridor_pdf_reader.textract_adapter.records import (
    NATIVE_GEOMETRY_PURPOSE,
    PROVIDER_POSTURE,
    AuthorizationRecord,
    CustomerAuthorization,
    ExperimentScope,
    ProviderPosture,
    RequestBoundary,
    mismatches,
)

SCOPE_FILE = "scope.json"


class AnalyzeDocumentService(Protocol):
    """What the boundary needs of a Textract service: boto3's client, or a double."""

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]: ...


def live_service(region: str, profile: str | None = None) -> AnalyzeDocumentService:
    """boto3's Textract client. The one place Corridor names the service; never called by a test."""
    import boto3

    service: AnalyzeDocumentService = boto3.Session(profile_name=profile, region_name=region).client("textract")
    return service


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class TextractProcessingFailure(RuntimeError):
    """A Processing Failure: the attempt did not complete the contract, and its record is preserved.

    `reason` is a short code (`authorization-refused`, `resource-budget-exhausted`,
    `provider-call-failed`, `retained-entry-invalid`); `outbound_requests` is
    how many requests left this process before the failure, which is zero for
    a refusal by construction.
    """

    def __init__(
        self,
        reason: str,
        *,
        detail: str = "",
        mismatches: tuple[str, ...] = (),
        outbound_requests: int = 0,
        request: RequestBoundary | None = None,
    ) -> None:
        summary = detail or "; ".join(mismatches) or reason
        super().__init__(f"{reason}: {summary}")
        self.reason = reason
        self.detail = detail
        self.mismatches = mismatches
        self.outbound_requests = outbound_requests
        self.request = request

    def record(self) -> dict[str, Any]:
        return {
            "kind": "processing-failure",
            "reason": self.reason,
            "detail": self.detail,
            "mismatches": list(self.mismatches),
            "outbound_requests": self.outbound_requests,
            "request": None if self.request is None else self.request.as_dict(),
            "recorded_at": _now(),
        }


class _CountingService:
    """Every outbound request passes here and is counted, whatever becomes of it."""

    def __init__(self, service: AnalyzeDocumentService) -> None:
        self._service = service
        self.attempts = 0
        self.calls = 0
        self.retries = 0
        self.failed_attempts = 0

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.attempts += 1
        try:
            response = self._service.analyze_document(Document=Document, FeatureTypes=FeatureTypes)
        except Exception as exc:
            if error_code(exc) in RETRYABLE:
                self.retries += 1
            else:
                self.failed_attempts += 1
            raise
        self.calls += 1
        return response

    def counts(self) -> tuple[int, int, int, int]:
        return (self.attempts, self.calls, self.retries, self.failed_attempts)


@dataclass(frozen=True)
class ProvenanceBinding:
    """One rendition page bound to the response it was read from."""

    extraction_run: str
    record_id: str
    rendition_sha256: str
    page_number: int
    raster_sha256: str
    scope_digest: str
    frame: dict[str, Any]
    normalization: dict[str, Any]
    raw_response_digest: str
    normalized_reading_digest: str
    model_version: str | None
    request_identity: dict[str, Any]
    cached: bool
    counts: dict[str, int]
    recorded_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "extraction_run": self.extraction_run,
            "record_id": self.record_id,
            "rendition_sha256": self.rendition_sha256,
            "page_number": self.page_number,
            "raster_sha256": self.raster_sha256,
            "scope_digest": self.scope_digest,
            "frame": self.frame,
            "normalization": self.normalization,
            "raw_response_digest": self.raw_response_digest,
            "normalized_reading_digest": self.normalized_reading_digest,
            "model_version": self.model_version,
            "request_identity": self.request_identity,
            "cached": self.cached,
            "counts": self.counts,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class PageReading:
    page: dict[str, Any]
    binding: ProvenanceBinding


class CostReceipt:
    """Per Extraction Run: calls, retries and hits separately, every binding, every failure."""

    def __init__(self, extraction_run: str, scope: CacheScope, record: AuthorizationRecord, request: RequestBoundary) -> None:
        self.extraction_run = extraction_run
        self.scope = scope
        self.record = record
        self.request = request
        self.opened_at = _now()
        self.pages_requested = 0
        self.hits = 0
        self.calls = 0
        self.retries = 0
        self.failed_attempts = 0
        self.bindings: list[ProvenanceBinding] = []
        self.failures: list[dict[str, Any]] = []

    @property
    def outbound_requests(self) -> int:
        return self.calls + self.retries + self.failed_attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "textract-cost-receipt",
            "extraction_run": self.extraction_run,
            "opened_at": self.opened_at,
            "authorization": {"kind": self.record.kind, "record_id": self.record.record_id},
            "request": self.request.as_dict(),
            "scope_digest": self.scope.digest,
            "counts": {
                "pages_requested": self.pages_requested,
                "hits": self.hits,
                "calls": self.calls,
                "retries": self.retries,
                "failed_attempts": self.failed_attempts,
                "outbound_requests": self.outbound_requests,
                "failures": len(self.failures),
            },
            "charged_pages": self.calls,
            "list_price_per_page_usd": DOLLARS_PER_PAGE,
            "list_charge_usd": round(self.calls * DOLLARS_PER_PAGE, 3),
            "charge_note": "list price for successful calls; retries and hits are not charged; the provider's bill is authoritative",
            "provider_model_versions": sorted({b.model_version for b in self.bindings if b.model_version is not None}),
            "bindings": [binding.as_dict() for binding in self.bindings],
            "failures": list(self.failures),
        }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomic, like the imported client's entries: never a truncated identity file."""
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def open_boundary(
    record: object | None,
    request: RequestBoundary,
    *,
    extraction_run: str,
    cache_root: Path,
    service: AnalyzeDocumentService,
    configuration: RequestConfiguration = RequestConfiguration(),
    normalization: NormalizationConfiguration = NormalizationConfiguration(),
    max_pages: int = MAX_PAGES_DEFAULT,
    credential_label: str = "unspecified",
    sleep: Callable[[float], None] = time.sleep,
    posture: ProviderPosture = PROVIDER_POSTURE,
) -> AuthorizedTextract:
    """Match the record against the request on every field; refuse, or construct the client.

    A refusal is raised before anything touches the disk or the service:
    no scope directory, no client, zero outbound requests.
    """
    found = mismatches(record, request, posture)
    if found:
        reason = "authorization-absent" if record is None else "authorization-refused"
        raise TextractProcessingFailure(reason, mismatches=found, outbound_requests=0, request=request)
    assert isinstance(record, (CustomerAuthorization, ExperimentScope))
    return AuthorizedTextract(
        record,
        request,
        extraction_run=extraction_run,
        cache_root=cache_root,
        service=service,
        configuration=configuration,
        normalization=normalization,
        max_pages=max_pages,
        credential_label=credential_label,
        sleep=sleep,
    )


class AuthorizedTextract:
    """The adapter after the check passed. Not shared between threads."""

    def __init__(
        self,
        record: AuthorizationRecord,
        request: RequestBoundary,
        *,
        extraction_run: str,
        cache_root: Path,
        service: AnalyzeDocumentService,
        configuration: RequestConfiguration,
        normalization: NormalizationConfiguration,
        max_pages: int,
        credential_label: str,
        sleep: Callable[[float], None],
    ) -> None:
        self.record = record
        self.request = request
        self.configuration = configuration
        self.normalization = normalization
        self.scope = cache_scope(record, request, configuration)
        self.directory = cache_root / self.scope.digest
        self.directory.mkdir(parents=True, exist_ok=True)
        scope_file = self.directory / SCOPE_FILE
        if scope_file.exists():
            recorded = json.loads(scope_file.read_text(encoding="utf-8"))
            if recorded != self.scope.as_dict():
                raise TextractProcessingFailure(
                    "cache-scope-mismatch",
                    detail=f"{scope_file} records a different identity for digest {self.scope.digest[:12]}",
                    request=request,
                )
        else:
            _write_json(scope_file, self.scope.as_dict())
        self._proxy = _CountingService(service)
        self._client = TextractClient(
            self.directory,
            self._proxy,
            max_pages=max_pages,
            offline=False,
            profile=credential_label,
            region=request.region,
            sleep=sleep,
        )
        self.receipt = CostReceipt(extraction_run, self.scope, record, request)

    # -- retained entries ---------------------------------------------------

    def identity_path(self, raster_sha256: str) -> Path:
        return self.directory / f"{raster_sha256}.identity.json"

    def _identity(self, raster_sha256: str, entry: dict[str, Any], *, retained: bool) -> dict[str, Any]:
        path = self.identity_path(raster_sha256)
        if path.exists():
            loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return loaded
        response = entry["response"]
        identity: dict[str, Any] = {
            "scope": self.scope.as_dict(),
            "raster_sha256": raster_sha256,
            "raster_bytes": entry.get("bytes"),
            "model_version": model_version(response),
            "raw_response_digest": raw_response_digest(response),
            "request_identity": {
                "request_id": request_id(response),
                "requested_at": entry.get("requested_at"),
                "transport": entry.get("transport"),
                "attempts": entry.get("attempts"),
                "record_id": self.record.record_id,
                "retained": retained,
            },
            "bindings": [],
        }
        _write_json(path, identity)
        return identity

    def retain(self, entry: dict[str, Any]) -> None:
        """Record a response obtained under this same boundary by another transport.

        The entry has the imported client's shape (`sha256`, `bytes`,
        `response` with `Blocks`, `transport`, `attempts`, `requested_at`).
        Whether a response obtained elsewhere may be reused here is the
        caller's governance decision; this only records it under the scope.
        """
        sha = entry.get("sha256")
        response = entry.get("response")
        if not isinstance(sha, str) or len(sha) != 64 or not isinstance(response, dict) or "Blocks" not in response:
            raise TextractProcessingFailure("retained-entry-invalid", detail="an entry needs a 64-hex sha256 and a response with Blocks", request=self.request)
        write_entry(self.directory, entry)
        self._identity(sha, entry, retained=True)

    # -- the read -----------------------------------------------------------

    def analyze_page(
        self,
        raster: Raster,
        *,
        rendition_sha256: str,
        page_number: int,
        native_glyphs: NativeGlyphs | None = None,
    ) -> PageReading:
        """The reader's page for this raster, from the scope or from one counted call.

        With `native_glyphs`, Textract supplies geometry only and the reading's
        text is the document's own glyphs (the measured lane A). Supplying that
        input selects native materialization, rather than attaching incidental
        evidence, so a customer request must name the native geometry purpose.
        That purpose also requires usable glyphs and cannot become an OCR
        reading. Experiments may compare both modes. The raw response remains
        provider evidence.
        The consuming route selects values per region on mixed native/image
        pages; this boundary does not classify the page from a native header.
        """
        self.receipt.pages_requested += 1
        purpose_refusal = None
        if (
            isinstance(self.record, CustomerAuthorization)
            and native_glyphs is not None
            and self.request.purpose != NATIVE_GEOMETRY_PURPOSE
        ):
            purpose_refusal = (
                f"purpose: native glyphs select {NATIVE_GEOMETRY_PURPOSE!r}; "
                f"the customer request names {self.request.purpose!r}"
            )
        elif self.request.purpose == NATIVE_GEOMETRY_PURPOSE and (
            native_glyphs is None
            or not any(isinstance(char.get("text"), str) and char["text"].strip() for char in native_glyphs.characters)
        ):
            purpose_refusal = (
                f"purpose: {NATIVE_GEOMETRY_PURPOSE!r} requires usable native glyphs; "
                "Textract words cannot substitute for them"
            )
        if purpose_refusal is not None:
            failure = TextractProcessingFailure(
                "authorization-refused",
                mismatches=(purpose_refusal,),
                outbound_requests=0,
                request=self.request,
            )
            self.receipt.failures.append(failure.record())
            raise failure
        if raster.dpi != self.configuration.dpi or raster.mode != self.configuration.mode:
            failure = TextractProcessingFailure(
                "raster-configuration-mismatch",
                detail=(
                    f"the raster is {raster.dpi} dpi, mode {raster.mode}; this boundary's configuration is "
                    f"{self.configuration.dpi} dpi, mode {self.configuration.mode}, and the bytes sent must be what the identity says"
                ),
                outbound_requests=0,
                request=self.request,
            )
            self.receipt.failures.append(failure.record())
            raise failure
        before = self._proxy.counts()
        try:
            analysis = self._client.analyze(raster.png)
        except BudgetExceeded as exc:
            failure = TextractProcessingFailure("resource-budget-exhausted", detail=str(exc), outbound_requests=0, request=self.request)
            self._account(before)
            self.receipt.failures.append(failure.record())
            raise failure from exc
        except AnalysisFailed as exc:
            attempts = self._proxy.attempts - before[0]
            failure = TextractProcessingFailure("provider-call-failed", detail=str(exc), outbound_requests=attempts, request=self.request)
            self._account(before)
            self.receipt.failures.append(failure.record())
            raise failure from exc
        self._account(before)
        if analysis.cached:
            self.receipt.hits += 1
        entry = read_entry(self.directory, analysis.sha256)
        assert entry is not None
        identity = self._identity(analysis.sha256, entry, retained=False)
        geometry = raster.geometry
        page = normalize(
            analysis.response,
            number=page_number,
            size=(geometry.width, geometry.height),
            rotation=geometry.rotation,
            glyphs=native_glyphs,
            normalization=self.normalization,
        )
        binding = ProvenanceBinding(
            extraction_run=self.receipt.extraction_run,
            record_id=self.record.record_id,
            rendition_sha256=rendition_sha256,
            page_number=page_number,
            raster_sha256=analysis.sha256,
            scope_digest=self.scope.digest,
            frame={"size": [geometry.width, geometry.height], "rotation": geometry.rotation, "dpi": raster.dpi, "pixels": [raster.width_px, raster.height_px], "mode": raster.mode},
            normalization=normalization_record(self.normalization, native_glyphs),
            raw_response_digest=identity["raw_response_digest"],
            normalized_reading_digest=reading_digest(page),
            model_version=identity["model_version"],
            request_identity=identity["request_identity"],
            cached=analysis.cached,
            counts=reading_counts(page),
            recorded_at=_now(),
        )
        identity["bindings"].append(binding.as_dict())
        _write_json(self.identity_path(analysis.sha256), identity)
        self.receipt.bindings.append(binding)
        return PageReading(page, binding)

    def _account(self, before: tuple[int, int, int, int]) -> None:
        _, calls, retries, failed = self._proxy.counts()
        self.receipt.calls += calls - before[1]
        self.receipt.retries += retries - before[2]
        self.receipt.failed_attempts += failed - before[3]
