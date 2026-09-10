"""The only authorized path from Corridor to a native-matrix model call (#447).

`native_pipeline.py` used to refuse every `fresh_provider` observation flatly,
because a mode label and a digest string are not authorization and no boundary
existed to be one. That refusal was right and is not deleted here; it is
replaced by a boundary that can actually be satisfied, in the shape
`corridor_pdf_reader/textract_adapter/boundary.py` already proved for Textract.

`open_native_provider_boundary` takes the authorization record, the request's
own boundary, the exact source digests the run intends to send, and the
campaign budget. It matches all of them against the recorded provider posture
and against each other, and refuses with zero outbound requests when anything
is absent or uncovered. Only then does it construct the mapper.

The transport is the seam. One `send` is one outbound request, counted as a
call, a retry or a failed attempt, so the receipt can separate what was
charged from what was merely attempted. The boundary owns the retry loop for
that reason: a client that retries internally reports one call for three
requests. Nothing here selects a configuration, writes an accepted record, or
makes a Fact effective; it returns structured answers and counts.

Customer material stays refused, now for a named reason rather than for the
absence of a mechanism: the posture records `customer_processing: blocked`
and `pdf_licensing: unresolved`, and both appear in the refusal.
"""

from __future__ import annotations

from corridor import digests
import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import time
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTURE_PATH = REPO_ROOT / "docs/operations/openai-responses-provider-posture.md"

NATIVE_MATRIX_PURPOSE = "native-matrix-structure-mapping"
MEASUREMENT_PURPOSE = "extraction-measurement"
EXPERIMENT_STAGE = "experiment"
CUSTOMER_STAGES: tuple[str, ...] = ("compatibility", "shadow", "authoritative")
RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return digests.canonical_json(value).decode()


_digest = digests.canonical_sha256


@dataclass(frozen=True)
class ProviderPosture:
    """What the provider is approved to do at all, bound to its document bytes.

    `customer_processing` and `pdf_licensing` are the maintainer's to change,
    and changing either means editing the document, which changes the digest,
    which invalidates every record that accepted the old one.
    """

    identity: str
    document: str
    digest: str
    provider: str
    api: str
    model: str
    reasoning_effort: str
    store: bool
    service_tier: str
    base_url: str
    image_detail: str
    image_dpi: int
    permitted_purposes: tuple[str, ...]
    permitted_source_classes: tuple[str, ...]
    retention: str
    training_opt_out: str
    zero_data_retention: str
    customer_processing: str
    pdf_licensing: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


POSTURE = ProviderPosture(
    identity="openai-responses-gpt-5.6-luna-structured-matrix-posture-1",
    document="docs/operations/openai-responses-provider-posture.md",
    digest="8520285d9ac80587358c2f70809e83e327c59920b4d8fef9a7b5dce7511f9cd7",
    provider="openai",
    api="responses",
    model="gpt-5.6-luna",
    reasoning_effort="none",
    store=False,
    service_tier="standard",
    base_url="https://api.openai.com/v1",
    image_detail="original",
    image_dpi=110,
    permitted_purposes=(NATIVE_MATRIX_PURPOSE, MEASUREMENT_PURPOSE),
    permitted_source_classes=("native_matrix",),
    retention="standard-abuse-monitoring-up-to-30-days",
    training_opt_out="default-not-trained",
    zero_data_retention="not-enabled",
    customer_processing="blocked",
    pdf_licensing="unresolved",
    status="approved",
)


@dataclass(frozen=True)
class Budget:
    """A campaign ceiling, checked before every request leaves the process."""

    max_calls: int
    max_pages: int
    max_total_tokens: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ExperimentScope:
    """The record for public or synthetic experiment material.

    It names the dataset, the exact source digests, the purpose and the
    ceiling, and it authorizes the `experiment` stage and nothing else, so no
    fictional customer agreement is ever written to cover reference material.
    """

    record_id: str
    dataset: str
    dataset_digest: str
    purpose: str
    scope: str
    source_classes: frozenset[str]
    source_sha256s: frozenset[str]
    max_calls: int
    max_pages: int
    max_total_tokens: int
    posture_identity: str
    posture_digest: str
    recorded_by: str
    recorded_on: str

    kind = "experiment-scope"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "record_id": self.record_id, "dataset": self.dataset,
                "dataset_digest": self.dataset_digest, "purpose": self.purpose, "scope": self.scope,
                "source_classes": sorted(self.source_classes), "source_sha256s": sorted(self.source_sha256s),
                "max_calls": self.max_calls, "max_pages": self.max_pages,
                "max_total_tokens": self.max_total_tokens, "posture_identity": self.posture_identity,
                "posture_digest": self.posture_digest, "recorded_by": self.recorded_by,
                "recorded_on": self.recorded_on}


@dataclass(frozen=True)
class CustomerAuthorization:
    """#522's signed instance, reduced to the fields this boundary matches."""

    record_id: str
    customer: str
    projects: frozenset[str]
    source_classes: frozenset[str]
    purposes: frozenset[str]
    stages: frozenset[str]
    source_sha256s: frozenset[str]
    max_calls: int
    max_pages: int
    max_total_tokens: int
    posture_identity: str
    posture_digest: str
    retention_disclosed: bool
    signed_by: str
    signed_on: str

    kind = "customer-authorization"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "record_id": self.record_id, "customer": self.customer,
                "projects": sorted(self.projects), "source_classes": sorted(self.source_classes),
                "purposes": sorted(self.purposes), "stages": sorted(self.stages),
                "source_sha256s": sorted(self.source_sha256s), "max_calls": self.max_calls,
                "max_pages": self.max_pages, "max_total_tokens": self.max_total_tokens,
                "posture_identity": self.posture_identity, "posture_digest": self.posture_digest,
                "retention_disclosed": self.retention_disclosed, "signed_by": self.signed_by,
                "signed_on": self.signed_on}


AuthorizationRecord = ExperimentScope | CustomerAuthorization


@dataclass(frozen=True)
class RequestBoundary:
    """What one campaign claims about itself, matched against the record.

    The model configuration is part of the boundary, not a client detail: a
    request that quietly raised reasoning effort or set `store` would be
    outside the posture whatever the record says about projects and purposes.
    """

    project: str
    source_class: str
    purpose: str
    stage: str
    posture_identity: str
    model: str
    reasoning_effort: str
    store: bool
    base_url: str
    image_detail: str
    image_dpi: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransportOutcome:
    """One HTTP attempt's result: the boundary owns retries, not the transport."""

    status: int
    body: dict[str, Any] | None = None
    retry_after: float | None = None
    error: str | None = None


class ResponsesTransport(Protocol):
    """One outbound request, no retry, no accounting. A double, or `live_transport`."""

    def send(self, payload: dict[str, Any]) -> TransportOutcome: ...


class NativeProviderRefused(RuntimeError):
    """A refusal, with its reason, every failing field, and what went out.

    `outbound_requests` is zero for an authorization refusal by construction:
    the check runs before a transport is ever handed a payload.
    """

    def __init__(self, reason: str, *, detail: str = "", mismatches: tuple[str, ...] = (),
                 outbound_requests: int = 0, request: RequestBoundary | None = None) -> None:
        super().__init__(f"{reason}: {detail or '; '.join(mismatches) or reason}")
        self.reason = reason
        self.detail = detail
        self.mismatches = tuple(mismatches)
        self.outbound_requests = outbound_requests
        self.request = request

    def record(self) -> dict[str, Any]:
        return {"kind": "native-provider-refusal", "reason": self.reason, "detail": self.detail,
                "mismatches": list(self.mismatches), "outbound_requests": self.outbound_requests,
                "request": None if self.request is None else self.request.as_dict(),
                "recorded_at": _now()}


def mismatches(
    record: object | None, request: RequestBoundary, *,
    source_sha256s: frozenset[str], budget: Budget, posture: ProviderPosture = POSTURE,
) -> tuple[str, ...]:
    """Every field the request is not covered on; empty is the only pass.

    Every failing field is reported, not the first, so one refusal names the
    whole gap instead of sending the maintainer back three times.
    """
    found: list[str] = []
    if request.posture_identity != posture.identity:
        found.append(f"posture-identity: request names {request.posture_identity!r}, the boundary implements {posture.identity!r}")
    for name, asked, allowed in (
        ("model", request.model, posture.model),
        ("reasoning-effort", request.reasoning_effort, posture.reasoning_effort),
        ("store", request.store, posture.store),
        ("base-url", request.base_url, posture.base_url),
        ("image-detail", request.image_detail, posture.image_detail),
        ("image-dpi", request.image_dpi, posture.image_dpi),
    ):
        if asked != allowed:
            found.append(f"{name}: request names {asked!r}, the posture approves {allowed!r}")
    if request.purpose not in posture.permitted_purposes:
        found.append(f"purpose: {request.purpose!r} is not a purpose the posture permits ({', '.join(posture.permitted_purposes)})")
    if request.source_class not in posture.permitted_source_classes:
        found.append(f"source-class: {request.source_class!r} is not a source class the posture permits ({', '.join(posture.permitted_source_classes)})")
    if posture.status != "approved":
        found.append(f"posture-status: the posture is {posture.status!r}; no request may be sent under it")
    if not source_sha256s:
        found.append("source-digests: a request must name the exact sources it intends to send")
    if record is None:
        found.append("authorization-absent: no experiment scope or customer authorization was given")
        return tuple(found)
    if not isinstance(record, (ExperimentScope, CustomerAuthorization)):
        found.append(f"record-kind: {type(record).__name__} is not an authorization record")
        return tuple(found)
    if record.posture_identity != posture.identity:
        found.append(f"posture-identity: record {record.record_id!r} accepts {record.posture_identity!r}, the boundary implements {posture.identity!r}")
    if record.posture_digest != posture.digest:
        found.append(f"posture-digest: record {record.record_id!r} accepts digest {record.posture_digest[:12]!r}, the posture document's digest is {posture.digest[:12]!r}")
    if request.source_class not in record.source_classes:
        found.append(f"source-class: {request.source_class!r} is not permitted by record {record.record_id!r} ({', '.join(sorted(record.source_classes))})")
    outside = sorted(source_sha256s - record.source_sha256s)
    if outside:
        found.append(f"source-digests: {len(outside)} source(s) are outside record {record.record_id!r}, beginning {outside[0][:12]!r}")
    for name, asked, ceiling in (
        ("budget-max-calls", budget.max_calls, record.max_calls),
        ("budget-max-pages", budget.max_pages, record.max_pages),
        ("budget-max-total-tokens", budget.max_total_tokens, record.max_total_tokens),
    ):
        if asked > ceiling or asked < 1:
            found.append(f"{name}: the campaign asks for {asked}, record {record.record_id!r} allows {ceiling}")
    if isinstance(record, CustomerAuthorization):
        # Status alone cannot open customer processing. Both fields are the
        # maintainer's to record in the posture document with evidence; the
        # boundary neither queries the provider nor infers them from a
        # signature, and refuses while either is unmet.
        for name, state, required in (("customer-processing", posture.customer_processing, "open"),
                                      ("pdf-licensing", posture.pdf_licensing, "resolved")):
            if state != required:
                found.append(f"posture-{name}: the posture records {state!r}; customer material requires {required!r} with evidence in the posture document")
        if not record.retention_disclosed:
            found.append(f"retention-disclosure: record {record.record_id!r} does not disclose the posture's abuse-monitoring retention")
        if request.project not in record.projects:
            found.append(f"project: {request.project!r} is not a project of record {record.record_id!r} ({', '.join(sorted(record.projects))})")
        if request.purpose not in record.purposes:
            found.append(f"purpose: {request.purpose!r} is not permitted by record {record.record_id!r} ({', '.join(sorted(record.purposes))})")
        if request.stage not in record.stages or request.stage not in CUSTOMER_STAGES:
            found.append(f"stage: {request.stage!r} is not authorized by record {record.record_id!r} ({', '.join(sorted(record.stages))})")
    else:
        if request.project != record.dataset:
            found.append(f"project: {request.project!r} is not the dataset of experiment scope {record.record_id!r} ({record.dataset!r})")
        if request.purpose != record.purpose:
            found.append(f"purpose: {request.purpose!r} is not the purpose of experiment scope {record.record_id!r} ({record.purpose!r})")
        if request.stage != EXPERIMENT_STAGE:
            found.append(f"stage: {request.stage!r} cannot be authorized by an experiment scope, which covers {EXPERIMENT_STAGE!r} only")
    return tuple(found)


@dataclass
class Usage:
    """Cumulative counters in the shape `extractor_lineage.usage_snapshot` reads."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


def open_native_provider_boundary(
    record: object | None, request: RequestBoundary, *, transport: ResponsesTransport,
    budget: Budget, source_sha256s: frozenset[str], campaign: str,
    posture: ProviderPosture = POSTURE, sleep=time.sleep,
) -> AuthorizedNativeMapper:
    """Match everything, then construct the mapper. A refusal sends nothing."""
    found = mismatches(record, request, source_sha256s=frozenset(source_sha256s), budget=budget, posture=posture)
    if found:
        reason = "authorization-absent" if record is None else "authorization-refused"
        raise NativeProviderRefused(reason, mismatches=found, outbound_requests=0, request=request)
    assert isinstance(record, (ExperimentScope, CustomerAuthorization))
    if not isinstance(campaign, str) or not campaign.strip():
        raise NativeProviderRefused("campaign-identity-missing", detail="a campaign needs an explicit identity", request=request)
    return AuthorizedNativeMapper(record, request, transport=transport, budget=budget,
                                  source_sha256s=frozenset(source_sha256s), campaign=campaign,
                                  posture=posture, sleep=sleep)


class AuthorizedNativeMapper:
    """The mapper after the check passed. Not shared between threads.

    It satisfies the structured-client shape the native matrix extractor and
    `deployed_native_matrix_config` require, so nothing downstream learns that
    a boundary is in front of the provider.
    """

    flex = False

    def __init__(self, record: AuthorizationRecord, request: RequestBoundary, *,
                 transport: ResponsesTransport, budget: Budget, source_sha256s: frozenset[str],
                 campaign: str, posture: ProviderPosture, sleep) -> None:
        self.record = record
        self.request = request
        self.posture = posture
        self.budget = budget
        self.source_sha256s = source_sha256s
        self.campaign = campaign
        self.opened_at = _now()
        self.model = request.model
        self.effort = request.reasoning_effort
        self.base_url = request.base_url
        self.image_detail = request.image_detail
        self.usage = Usage()
        self.calls = 0
        self.retries = 0
        self.failed_attempts = 0
        self.pages_requested = 0
        self.refusals: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self._transport = transport
        self._sleep = sleep

    @property
    def origin_sha256(self) -> str:
        """This observation origin: posture, record, request and budget together.

        An `ObservationPlan` names it, so a receipt cannot claim to have been
        observed under one authorization and actually run under another.
        """
        return _digest({"posture": self.posture.as_dict(), "record": self.record.as_dict(),
                        "request": self.request.as_dict(), "budget": self.budget.as_dict(),
                        "source_sha256s": sorted(self.source_sha256s)})

    # -- accounting ---------------------------------------------------------

    @property
    def prompt_tokens(self) -> int:
        return self.usage.prompt_tokens

    @property
    def completion_tokens(self) -> int:
        return self.usage.completion_tokens

    @property
    def cached_tokens(self) -> int:
        return self.usage.cached_tokens

    @property
    def outbound_requests(self) -> int:
        return self.calls + self.retries + self.failed_attempts

    @property
    def total_tokens(self) -> int:
        return self.usage.prompt_tokens + self.usage.completion_tokens

    def last_call_receipt(self) -> dict[str, Any]:
        """The exact identity of the call just made, for the observation record."""
        if not self.receipts:
            raise NativeProviderRefused("no-call-recorded", detail="no outbound call has completed on this boundary", request=self.request)
        return dict(self.receipts[-1])

    def cost_receipt(self) -> dict[str, Any]:
        """Calls, retries, failed attempts and tokens; dollars only if recorded.

        No price is invented. The posture document records no list price for
        this model, so the dollar figure stays null and says so, and the
        provider's own bill remains authoritative either way.
        """
        return {
            "kind": "native-provider-cost-receipt", "campaign": self.campaign,
            "opened_at": self.opened_at, "posture": self.posture.as_dict(),
            "authorization": {"kind": self.record.kind, "record_id": self.record.record_id},
            "request": self.request.as_dict(), "budget": self.budget.as_dict(),
            "counts": {"pages_requested": self.pages_requested, "calls": self.calls,
                       "retries": self.retries, "failed_attempts": self.failed_attempts,
                       "outbound_requests": self.outbound_requests, "refusals": len(self.refusals)},
            "tokens": {"input": self.usage.prompt_tokens, "output": self.usage.completion_tokens,
                       "cached_input": self.usage.cached_tokens, "total": self.total_tokens},
            "usd": None,
            "usd_note": "no list price for this model is recorded in the provider posture, so the dollar cost is not recorded; token counts above are the measured processing cost and the provider's bill is authoritative",
            "call_receipts": [dict(item) for item in self.receipts],
            "refusals": [dict(item) for item in self.refusals],
        }

    # -- the call -----------------------------------------------------------

    def complete(self, *, system: str, user: str, schema: dict[str, Any], images=()) -> dict[str, Any]:
        """One structured page mapping, budgeted and counted before it goes out."""
        paths = [Path(image) for image in images]
        # The four-field core is the request identity the observation record
        # already verifies. The authorization it went out under is recorded
        # beside it, not folded into it, so the two stay independently checkable.
        core = {
            "system_sha256": sha256(system.encode()).hexdigest(),
            "schema_sha256": _digest(schema), "user": user,
            "image_sha256s": [sha256(path.read_bytes()).hexdigest() for path in paths],
        }
        request_identity = {"core": core, "campaign": self.campaign,
                            "record_id": self.record.record_id, "boundary": self.request.as_dict()}
        self._require_budget(len(paths), request_identity)
        payload = {
            "model": self.model, "instructions": system,
            "input": [{"role": "user", "content": self._content(user, paths)}],
            "text": {"format": {"type": "json_schema", "name": "structure", "strict": True, "schema": schema}},
            "reasoning": {"effort": self.effort}, "store": self.request.store,
        }
        self.pages_requested += len(paths)
        attempts = 0
        last_error = ""
        started = time.perf_counter_ns()
        for attempt in range(MAX_ATTEMPTS):
            attempts += 1
            outcome = self._transport.send(payload)
            if outcome.status == 200 and outcome.body is not None:
                self.calls += 1
                return self._read(outcome.body, request_identity, attempts, started)
            if outcome.status in RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
                self.retries += 1
                last_error = f"{outcome.status}: {outcome.error or ''}"[:200]
                self._backoff(attempt, outcome.retry_after)
                continue
            self.failed_attempts += 1
            raise self._refuse("provider-call-failed", f"{outcome.status}: {outcome.error or ''}"[:300],
                               request_identity, attempts)
        raise self._refuse("provider-call-failed", f"failed after {attempts} attempts: {last_error}",
                           request_identity, attempts)

    def _require_budget(self, pages: int, request_identity: dict[str, Any]) -> None:
        exceeded = [
            name for name, value, ceiling in (
                ("calls", self.calls + 1, self.budget.max_calls),
                ("pages", self.pages_requested + pages, self.budget.max_pages),
                ("total_tokens", self.total_tokens, self.budget.max_total_tokens),
            ) if value > ceiling
        ]
        if exceeded:
            raise self._refuse("budget-exhausted", f"campaign budget reached: {', '.join(exceeded)}",
                               request_identity, 0)

    def _refuse(self, reason: str, detail: str, request_identity: dict[str, Any], attempts: int) -> NativeProviderRefused:
        refusal = NativeProviderRefused(reason, detail=detail, outbound_requests=attempts, request=self.request)
        record = refusal.record()
        record["request_sha256"] = _digest(request_identity["core"])
        record["boundary_sha256"] = _digest(request_identity)
        self.refusals.append(record)
        return refusal

    def _read(self, body: dict[str, Any], request_identity: dict[str, Any], attempts: int, started: int) -> dict[str, Any]:
        used = body.get("usage") or {}
        input_tokens = int(used.get("input_tokens", 0))
        output_tokens = int(used.get("output_tokens", 0))
        cached = int((used.get("input_tokens_details") or {}).get("cached_tokens", 0))
        reasoning = int((used.get("output_tokens_details") or {}).get("reasoning_tokens", 0))
        self.usage.prompt_tokens += input_tokens
        self.usage.completion_tokens += output_tokens
        self.usage.cached_tokens += cached
        self.usage.reasoning_tokens += reasoning
        if body.get("status") == "incomplete":
            raise self._refuse("provider-response-incomplete",
                               str((body.get("incomplete_details") or {}).get("reason", "unknown")),
                               request_identity, attempts)
        message = next((item for item in body.get("output") or [] if item.get("type") == "message"), None)
        parts = (message or {}).get("content") or []
        refusal = next((part for part in parts if part.get("type") == "refusal"), None)
        if refusal is not None:
            raise self._refuse("provider-refused-request", str(refusal.get("refusal")), request_identity, attempts)
        text_part = next((part for part in parts if part.get("type") == "output_text"), None)
        if text_part is None or not str(text_part.get("text") or "").strip():
            raise self._refuse("provider-response-unusable", "a 200 with no output text", request_identity, attempts)
        answer = json.loads(text_part["text"])
        receipt = {
            "campaign": self.campaign, "record_id": self.record.record_id,
            "request_sha256": _digest(request_identity["core"]),
            "boundary_sha256": _digest(request_identity), "response_sha256": _digest(answer),
            "raw_response_sha256": _digest(body), "response_id": body.get("id") or "",
            "model_reported": body.get("model") or self.model,
            "transport_attempts": attempts,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached},
            # No response is served from a local store: `store` is false and
            # this boundary keeps no cache. Provider-side prompt caching shows
            # up as cached input tokens, which is a discount, not a hit.
            "cached": False, "pages": len(request_identity["core"]["image_sha256s"]),
            "latency_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "recorded_at": _now(),
        }
        self.receipts.append(receipt)
        return answer

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            self._sleep(min(float(retry_after), 30.0))
            return
        self._sleep(min(2**attempt, 16) * (0.5 + random.random()))

    def _content(self, user: str, images: list[Path]) -> list[dict[str, Any]]:
        """Text first, images last, so the shared prefix stays cacheable."""
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
        for image in images:
            encoded = base64.b64encode(image.read_bytes()).decode()
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{encoded}",
                            "detail": self.image_detail})
        return content


@dataclass
class _HttpxTransport:
    """The one place Corridor opens a socket to the provider; never called by a test."""

    api_key: str
    base_url: str
    timeout: float = 300.0
    _client: Any = field(default=None, repr=False)

    def send(self, payload: dict[str, Any]) -> TransportOutcome:
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        try:
            response = self._client.post(
                f"{self.base_url.rstrip('/')}/responses",
                headers={"Authorization": f"Bearer {self.api_key}"}, json=payload,
            )
        except Exception as exc:
            return TransportOutcome(status=0, error=f"transport: {type(exc).__name__}: {exc}")
        if response.status_code != 200:
            retry_after = response.headers.get("retry-after")
            return TransportOutcome(status=response.status_code, error=response.text[:300],
                                    retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None)
        return TransportOutcome(status=200, body=response.json())

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def live_transport(api_key: str, base_url: str = POSTURE.base_url) -> _HttpxTransport:
    """Construct the real transport. A missing key is a refusal, not a silent no-op."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise NativeProviderRefused("provider-credential-absent", detail="OPENAI_API_KEY is not set")
    return _HttpxTransport(api_key=api_key, base_url=base_url)
