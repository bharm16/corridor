"""Deployment inputs for the selected native Matrix route (#766).

The challenger previously required a caller to assemble its authorization,
observation plan and output location. Production now loads those explicit
inputs from an operator-provided record; it never creates an authorization
or a pipeline selection. Tests inject the same runtime with a sealed replay.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path

from corridor.config import Settings
from corridor.native_provider_boundary import (
    Budget, CustomerAuthorization, ExperimentScope, NativeProviderRefused, RequestBoundary,
    live_transport, open_native_provider_boundary,
)
from corridor.pipeline_contracts import ObservationPlan, content_digest


@dataclass(frozen=True)
class NativeMatrixRuntime:
    deployment: str
    client: object
    plan: ObservationPlan
    output_dir: Path
    _transport: object | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


def configured_native_matrix_runtime(settings: Settings) -> NativeMatrixRuntime:
    """Open the existing outbound boundary from a declared runtime record.

    This constructs a transport without sending anything. Selection and the
    actual Document's project are checked by the route before execution.
    The processing pass shares the returned boundary across its documents so
    its budget does not reset per document. Direct callers own that same
    lifetime explicitly; the boundary's counters are not restart-persistent.
    """
    if not settings.native_matrix_runtime_file:
        raise ValueError("native matrix provider runtime is not configured (CORRIDOR_NATIVE_MATRIX_RUNTIME_FILE)")
    raw = json.loads(Path(settings.native_matrix_runtime_file).read_text())
    required = {"deployment", "authorization", "request", "budget", "source_sha256s", "campaign"}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - {"qualification_policy_sha256"}:
        raise ValueError("native matrix runtime record has unexpected or missing fields")
    if raw["deployment"] != settings.environment:
        raise ValueError("native matrix runtime deployment differs from CORRIDOR_ENVIRONMENT")
    authorization = dict(raw["authorization"])
    kind = authorization.pop("kind", None)
    if kind == ExperimentScope.kind:
        record_type = ExperimentScope
        sets = ("source_classes", "source_sha256s")
    elif kind == CustomerAuthorization.kind:
        record_type = CustomerAuthorization
        sets = ("projects", "source_classes", "purposes", "stages", "source_sha256s")
    else:
        raise ValueError("native matrix runtime requires an experiment scope or customer authorization")
    for name in sets:
        authorization[name] = frozenset(authorization[name])
    record = record_type(**authorization)
    request = RequestBoundary(**raw["request"])
    if settings.openai_base_url != request.base_url:
        raise NativeProviderRefused("provider-endpoint-mismatch",
            detail="the transport endpoint differs from the authorized request")
    transport = live_transport(settings.openai_api_key, request.base_url)
    try:
        client = open_native_provider_boundary(
            record, request, transport=transport,
            budget=Budget(**raw["budget"]), source_sha256s=frozenset(raw["source_sha256s"]),
            campaign=raw["campaign"],
        )
        plan = ObservationPlan(
            mode="fresh_provider", origin_sha256=client.origin_sha256,
            description="Production matrix extraction through the selected native configuration",
            provider_posture_sha256=client.posture.digest,
            customer_authorization_sha256=(content_digest(record.as_dict()) if isinstance(record, CustomerAuthorization) else None),
            source_permission="customer" if isinstance(record, CustomerAuthorization) else "public",
            qualification_policy_sha256=raw.get("qualification_policy_sha256"),
        )
        return NativeMatrixRuntime(
            deployment=raw["deployment"], client=client, plan=plan,
            output_dir=Path(settings.native_matrix_output_dir), _transport=transport,
        )
    except Exception:
        transport.close()
        raise
