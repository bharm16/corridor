"""Run the complete native matrix challenger without selecting it (#447).

The separate reader, inventory, renderer and mapper adapters used to have no
single executable receipt. This coordinator records their actual chain and
source scope, runs the existing sealed source append, and leaves the incumbent
route and accepted record untouched. Every client is injected. Retained,
synthetic and future fresh observations have distinct origins and costs; a
replayed answer never becomes a fresh provider observation by being rerendered.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import time
from typing import Any
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor.extractor_lineage import DEPLOYED_NATIVE_MATRIX_REQUEST, deployed_native_matrix_config
from corridor.llm import RequestConfiguration
from corridor.models import Document, PipelineConfiguration, PipelineObservation, PipelineQualificationPolicy, Project
from corridor.native_matrix import NativeMatrixExtraction, extract_native_matrix, render_native_matrix_context
from corridor.native_matrix_bindings import NativeMatrixRefused
from corridor.native_provider_boundary import POSTURE, AuthorizedNativeMapper, CustomerAuthorization, ExperimentScope
from corridor.page_inventory import read_reader_page_inventories, route_reader_page
from corridor.pipeline_contracts import ObservationPlan, PipelineScope, canonical_text, content_digest
from corridor.render_profiles import crop_routed_region, load_render_profile_bundle, render_page_derivative
from corridor.retention import register_processing_artifact
from corridor.token_layers import read_native_pdf


ROOT = Path(__file__).resolve().parents[2]
CHAIN_SOURCES = (
    "src/corridor/native_pipeline.py", "src/corridor/pipeline_contracts.py",
    "src/corridor/pipeline.py", "src/corridor/native_matrix_runtime.py",
    "src/corridor/pipeline_comparison.py", "src/corridor/pipeline_qualification.py",
    "src/corridor/pipeline_selection_readback.py",
    "src/corridor/native_matrix.py", "src/corridor/native_matrix_bindings.py",
    "src/corridor/extractor_lineage.py", "src/corridor/extraction_runs.py",
    "src/corridor/extraction_run_queries.py", "src/corridor/token_layers.py",
    "src/corridor/reader_segments.py", "src/corridor/native_segment_projection.py",
    "src/corridor/page_inventory.py",
    "src/corridor/render_profiles.py", "workers/render/render_worker.py",
    "workers/render/raster.py", "workers/render/uv.lock", "uv.lock",
    "src/corridor/facts.py", "src/corridor/materializer.py", "src/corridor/fact_types.py",
    "src/corridor/source_append.py", "src/corridor/pdf_evaluation.py",
    "src/corridor/access.py",
    "src/corridor/migrations/baseline_versions/b2d5f8a1c4e7_source_append_commands.py",
    *sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "src/corridor/migrations/source_append_commands").glob("*.py")
    ),
)


def native_pipeline_configuration(client: object, plan: ObservationPlan) -> dict:
    """Actual code/dependency/request identities, independent of output values."""
    revision = os.environ.get("CORRIDOR_CODE_REVISION") or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("native pipeline requires the exact build commit identity")
    return {
        "schema": "native-pipeline-configuration-v1", "code_revision": revision,
        "source_sha256s": {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in CHAIN_SOURCES},
        "runtime": {"python": platform.python_version(), "system": platform.system(),
                    "machine": platform.machine(), "packages": {
                        name: version(name) for name in ("pypdfium2", "pypdf", "Pillow", "SQLAlchemy")}},
        "reader": {"engine": "tagged", "dpi": 36},
        "inventory": "page-inventory-router-reader-v1",
        "render_profiles": load_render_profile_bundle().model_dump(mode="json"),
        "model_context": {"operation": "render_native_matrix_context", "dpi": 110},
        "geometry_profile": "table_cv", "rasterizer": "pdfium",
        "extractor": asdict(deployed_native_matrix_config(client=client)),
        "eligibility": "explicit_selection_required", "qualification_policy_sha256": plan.qualification_policy_sha256,
        "provider": {"name": "openai-responses", "posture_sha256": plan.provider_posture_sha256},
        "textract": {"used": False, "reason": "OCR and structural assistance are excluded from this scope"},
    }


def register_pipeline_configuration(session: Session, configuration: dict) -> PipelineConfiguration:
    serialized = canonical_text(configuration)
    digest = sha256(serialized.encode()).hexdigest()
    session.execute(insert(PipelineConfiguration).values(
        configuration_sha256=digest, configuration_text=serialized,
    ).on_conflict_do_nothing())
    row = session.get_one(PipelineConfiguration, digest)
    if row.configuration_text != serialized:
        raise ValueError("registered pipeline configuration differs from its digest")
    return row


class RecordedPipelineClient:
    """Immutable exact-request replay, including newly rendered image bytes.

    `requests` contains system_sha256, schema_sha256, user, image_sha256s and
    answer for every call. Paths are deliberately not identity: a freshly
    rendered image is usable only when its bytes match the retained image.

    `configuration` is what the retained answers were produced under, stated
    as the value it is. This class used to carry a model name, an endpoint and
    a reasoning effort as class attributes so that receipt code scraping four
    duck-typed attributes would accept it — an offline adapter describing a
    live provider it cannot reach. It states the deployed configuration
    because that is the one these answers were recorded under; it still sends
    nothing anywhere.
    """

    def __init__(
        self,
        requests: list[dict],
        configuration: RequestConfiguration = DEPLOYED_NATIVE_MATRIX_REQUEST,
    ):
        self._configuration = configuration
        self._requests = canonical_text(requests)
        self.consumed = 0
        self.calls = self.prompt_tokens = self.completion_tokens = self.cached_tokens = 0

    def configuration(self) -> RequestConfiguration:
        return self._configuration

    @property
    def origin_sha256(self) -> str:
        return sha256(self._requests.encode()).hexdigest()

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        if logprobs:
            raise NativeMatrixRefused("pipeline replay retains no output logprobs")
        expected = json.loads(self._requests)
        if self.consumed >= len(expected):
            raise NativeMatrixRefused("pipeline replay requested an unrecorded answer")
        request = expected[self.consumed]
        actual = {
            "system_sha256": sha256(system.encode()).hexdigest(),
            "schema_sha256": content_digest(schema), "user": user,
            "image_sha256s": [sha256(Path(image).read_bytes()).hexdigest() for image in images],
        }
        if actual != {name: request[name] for name in actual}:
            raise NativeMatrixRefused("pipeline replay differs from its exact retained request")
        self.consumed += 1
        return request["answer"]

    def require_complete(self):
        if self.consumed != len(json.loads(self._requests)):
            raise NativeMatrixRefused("pipeline replay left retained answers unconsumed")


class _ObservedClient:
    """Capture real request/response identity around an explicitly supplied client."""

    def __init__(self, client, plan: ObservationPlan):
        self.client, self.plan = client, plan
        self.observations: list[dict[str, Any]] = []
        if plan.mode == "fresh_provider" and not callable(getattr(client, "last_call_receipt", None)):
            raise ValueError("fresh provider client must expose last_call_receipt with attempts, usage and response identity")

    def __getattr__(self, name):
        return getattr(self.client, name)

    def configuration(self) -> RequestConfiguration:
        """The observed client's own statement; this wrapper invents nothing."""
        return self.client.configuration()

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        request = {
            "system_sha256": sha256(system.encode()).hexdigest(), "schema_sha256": content_digest(schema),
            "user": user, "image_sha256s": [sha256(Path(image).read_bytes()).hexdigest() for image in images],
        }
        started = time.perf_counter_ns()
        observation: dict[str, Any] = {"request": request, "mode": self.plan.mode}
        self.observations.append(observation)
        try:
            answer = self.client.complete(
                system=system, user=user, schema=schema, images=images, logprobs=logprobs,
            )
            observation.update(answer=answer, response_sha256=content_digest(answer))
            return answer
        finally:
            observation["client_latency_ms"] = (time.perf_counter_ns() - started) / 1_000_000
            if self.plan.mode == "fresh_provider":
                receipt = self.client.last_call_receipt()
                if (not isinstance(receipt, dict) or receipt.get("request_sha256") != content_digest(request)
                        or receipt.get("response_sha256") != observation.get("response_sha256")
                        or not isinstance(receipt.get("transport_attempts"), int)
                        or receipt["transport_attempts"] < 1 or not receipt.get("response_id")
                        or not isinstance(receipt.get("usage"), dict)):
                    raise NativeMatrixRefused("fresh provider observation lacks exact request/response/attempt/usage provenance")
                observation["provider_receipt"] = receipt


def require_authorized_boundary(client: object, plan: ObservationPlan, scope: PipelineScope) -> AuthorizedNativeMapper:
    """A fresh observation runs only through a boundary that covers this exact run.

    The old flat refusal is not deleted: it is now conditional on the boundary
    failing to cover the run. Everything the boundary was opened with — its
    posture, its authorization record, its request configuration, its budget
    and its source allowlist — is in `origin_sha256`, so a plan cannot name one
    authorization and execute under another, and a scope cannot quietly widen
    past the digests the record actually authorizes.
    """
    if type(client) is not AuthorizedNativeMapper:
        raise ValueError("a fresh provider observation requires a verified outbound authorization boundary; a mode label is not one")
    reasons = []
    if plan.origin_sha256 != client.origin_sha256:
        reasons.append("plan origin differs from the boundary it was opened with")
    if plan.provider_posture_sha256 != client.posture.digest or client.posture.digest != POSTURE.digest:
        reasons.append("plan does not name the recorded provider posture bytes")
    if client.source_sha256s != set(scope.source_sha256s):
        reasons.append("boundary source allowlist differs from the declared scope")
    expected_permission = "customer" if isinstance(client.record, CustomerAuthorization) else ("synthetic" if scope.purpose == "synthetic_validation" else "public")
    if plan.source_permission != expected_permission:
        reasons.append("plan source permission differs from the authorization record kind")
    if isinstance(client.record, ExperimentScope) and plan.customer_authorization_sha256 is not None:
        reasons.append("an experiment scope cannot carry a customer authorization")
    if reasons:
        raise ValueError("fresh transport requires a verified outbound authorization boundary: " + "; ".join(reasons))
    return client


@dataclass(frozen=True)
class PipelineShadowResult:
    observation: PipelineObservation
    extraction: NativeMatrixExtraction | None


def _artifact(session, document, path: Path, kind: str, expected_sha256: str) -> dict:
    retention_kind = {
        "pipeline_geometry_render": "page_render", "pipeline_routed_crop": "page_render",
        "pipeline_model_context": "copied_prompt_context", "pipeline_model_observations": "unselected_model_response",
    }[kind]
    record = register_processing_artifact(
        session, project_id=document.project_id, kind=retention_kind, path=path,
        terminal_at=datetime.now(timezone.utc),
    )
    if record.content_sha256 != expected_sha256:
        raise NativeMatrixRefused("pipeline artifact changed between producing and registering its bytes")
    return {"artifact_id": record.id, "sha256": record.content_sha256, "kind": kind,
            "retention": "class_b"}


def _run_native_matrix_shadow(
    session: Session, document: Document, *, source_path: Path | str,
    scope: PipelineScope, client, plan: ObservationPlan, output_dir: Path | str,
    document_label: str, idempotency_key: str | None = None,
) -> PipelineShadowResult:
    """Execute the whole declared document or retain an explicit refusal.

    The fresh path requires an injected transport with exact accounting. This
    function never constructs a provider client. A scope exclusion is checked
    before rendering or calling a mapper; mixed/OCR inventory refuses the
    entire document rather than publishing only its native header.
    """
    from corridor.pipeline_comparison import canonical_native_output

    path = Path(source_path)
    if plan.mode == "fresh_provider":
        require_authorized_boundary(client, plan, scope)
    elif plan.source_permission == "customer":
        raise ValueError("customer material requires a verified outbound authorization boundary; digest strings are not approval")
    elif type(client) is not RecordedPipelineClient or plan.origin_sha256 != client.origin_sha256:
        raise ValueError("offline execution requires the sealed recorded client and its exact request/answer origin digest")
    if document.sha256 not in scope.source_sha256s or sha256(path.read_bytes()).hexdigest() != document.sha256:
        raise ValueError("shadow source is outside its exact declared scope or has changed bytes")
    project = session.get_one(Project, document.project_id)
    if scope.purpose == "synthetic_validation" and not project.is_synthetic:
        raise ValueError("synthetic qualification is confined to a synthetic project")
    if plan.mode == "synthetic" and (not project.is_synthetic or plan.source_permission != "synthetic"):
        raise ValueError("synthetic model observations require synthetic source scope")
    if plan.qualification_policy_sha256 is not None:
        policy = session.get(PipelineQualificationPolicy, plan.qualification_policy_sha256)
        if policy is None or policy.scope_sha256 != scope.identity:
            raise ValueError("pipeline metric policy must be registered for this scope before observation")
    observer = _ObservedClient(client, plan)
    configuration = native_pipeline_configuration(client, plan)
    configuration_sha256 = content_digest(configuration)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter_ns()
    started_at = datetime.now(timezone.utc).isoformat()
    cpu_started = time.process_time_ns()
    extraction = None
    artifacts = []
    chain: dict[str, Any] = {"source_sha256": document.sha256, "configuration_sha256": configuration_sha256}
    canonical: dict[str, Any] = {"schema_version": "corridor.native-pipeline-output.v1", "source_sha256": document.sha256,
                                 "pages": [{"page": number, "coverage": "not_processed"}
                                           for number in range(1, (document.pages or 0) + 1)], "rows": [], "facts": []}
    disposition = "completed"
    stage = "scope"
    outcome: dict[str, Any] = {"disposition": "completed"}
    raw_refusal = None
    try:
        if document.doc_type != "matrix":
            disposition = "excluded"
            raise NativeMatrixRefused("only native matrix semantics are in scope; native Minutes and other classes remain unsupported")
        stage = "inventory"
        inventories = read_reader_page_inventories(path)
        routes = {number: route_reader_page(inventory) for number, inventory in inventories.items()}
        chain["inventories"] = {str(n): value.model_dump(mode="json") for n, value in inventories.items()}
        chain["routes"] = {str(n): value.model_dump(mode="json") for n, value in routes.items()}
        canonical["pages"] = [{"page": number, "coverage": "inventory_only"} for number in sorted(inventories)]
        if not inventories or any(route.page_mode != "native" or route.structural_triggers for route in routes.values()):
            raise NativeMatrixRefused("document requires excluded OCR/mixed/structural assistance; no partial native scope was published")
        stage = "native_reading"
        reading = read_native_pdf(path, source_sha256=document.sha256)
        chain.update(reader_identity=reading.identity, reading_sha256=reading.reading_sha256)
        if set(inventories) != {page["number"] for page in reading.pages}:
            raise NativeMatrixRefused("reader and inventory page coverage differ")
        stage = "geometry_render_and_crop"
        rendered = []
        for number, inventory in inventories.items():
            derivative = render_page_derivative(
                pdf_path=path, page_number=number, profile_name="table_cv",
                output_dir=output / "geometry", rasterizer="pdfium",
            )
            manifest = derivative.model_dump(mode="json")
            manifest.pop("artifact_path")
            artifacts.append(_artifact(session, document, derivative.artifact_path, "pipeline_geometry_render", derivative.artifact_sha256))
            crops = []
            for region in routes[number].regions:
                cropped = crop_routed_region(
                    derivative=derivative, inventory=inventory, region=region, output_dir=output / "crops",
                )
                crop_record = cropped.model_dump(mode="json")
                crop_record["derivative"].pop("artifact_path")
                crops.append(crop_record)
                artifacts.append(_artifact(session, document, cropped.derivative.artifact_path, "pipeline_routed_crop", cropped.derivative.artifact_sha256))
            rendered.append({"page": number, "derivative": manifest, "crops": crops})
        chain["geometry"] = rendered
        stage = "measured_model_context"
        images = render_native_matrix_context(path, output / "model-context", sorted(inventories))
        chain["model_context"] = {str(n): {"dpi": 110, "sha256": sha256(image.read_bytes()).hexdigest(),
                                              "operation": "fresh_measured_pdfium_render"}
                                  for n, image in images.items()}
        artifacts.extend(_artifact(session, document, image, "pipeline_model_context", chain["model_context"][str(number)]["sha256"])
                         for number, image in images.items())
        # A failed canonical readback cannot leave a newly appended source run
        # behind without its observation. Existing exact-key retries remain.
        with session.begin_nested():
            stage = "mapping_and_source_append"
            extraction = extract_native_matrix(
                session, document, reading=reading, source_path=path, client=observer,
                images=images, document_label=document_label, idempotency_key=idempotency_key,
            )
            complete = getattr(client, "require_complete", None)
            if callable(complete):
                complete()
            stage = "canonical_source_readback"
            canonical = canonical_native_output(session, document, extraction, source_path=path)
            if native_pipeline_configuration(client, plan) != configuration or client.origin_sha256 != plan.origin_sha256:
                raise NativeMatrixRefused("pipeline implementation or retained inputs changed during execution")
    except NativeMatrixRefused as exc:
        disposition = "excluded" if disposition == "excluded" else "refused"
        raw_refusal = exc.record()
        outcome = {"disposition": disposition, "stage": stage, "reason": exc.reason, "kind": "native_matrix_refusal"}
        canonical.update(pages=[], rows=[], facts=[])
        extraction = None
    except Exception as exc:
        disposition = "failed"
        outcome = {"disposition": disposition, "stage": stage, "type": type(exc).__name__}
        canonical.update(pages=[], rows=[], facts=[])
        extraction = None
    raw_path = output / "model-observations.json"
    raw_bytes = canonical_text({"plan": plan.model_dump(mode="json"), "calls": observer.observations, "refusal": raw_refusal}).encode()
    raw_path.write_bytes(raw_bytes)
    artifacts.append(_artifact(session, document, raw_path, "pipeline_model_observations", sha256(raw_bytes).hexdigest()))
    # Elapsed time and request/answer identity are separate: rerunning the same
    # retained inputs should not compare a duration or a new provider ID.
    provider_costs = client.cost_receipt() if type(client) is AuthorizedNativeMapper else None
    if provider_costs is not None:
        chain["provider"] = provider_costs
    chain["model_observations"] = [{
        "request_sha256": content_digest(call["request"]), "response_sha256": call.get("response_sha256"),
        "mode": call["mode"], "origin_sha256": plan.origin_sha256,
    } for call in observer.observations]
    rows = canonical.get("rows", [])
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metrics = {
        "latency_ms": (time.perf_counter_ns() - started) / 1_000_000,
        "parent_cpu_ms": (time.process_time_ns() - cpu_started) / 1_000_000,
        "parent_process_lifetime_peak_rss_bytes": rss if platform.system() == "Darwin" else rss * 1024,
        "peak_process_tree_rss_bytes": None,
        "memory_limit": "Parent lifetime RSS excludes PDFium/render children; full-chain peak is unmeasured.",
        "client_calls": len(observer.observations),
        "outbound_attempts": sum(call.get("provider_receipt", {}).get("transport_attempts", 0) for call in observer.observations),
        "observed_new_provider_cost_usd": 0.0 if plan.mode != "fresh_provider" else None,
        "provider_calls": provider_costs["counts"]["calls"] if provider_costs else 0,
        "provider_retries": provider_costs["counts"]["retries"] if provider_costs else 0,
        "provider_failed_attempts": provider_costs["counts"]["failed_attempts"] if provider_costs else 0,
        "provider_refusals": provider_costs["counts"]["refusals"] if provider_costs else 0,
        "provider_tokens": provider_costs["tokens"] if provider_costs else
            {"input": 0, "output": 0, "cached_input": 0, "total": 0},
        "processing_cost_tokens": provider_costs["tokens"]["total"] if provider_costs else 0,
        "processing_cost_usd": None, "handling_minutes": None, "validated_customer_roi": None,
        "cost_limit": ("Measured processing cost is compute time plus the provider tokens above. "
                       "No list price for this model is recorded, so dollars, deployed total cost "
                       "and human handling burden remain unmeasured."
                       if plan.mode == "fresh_provider" else
                       "Replay has zero new provider calls; compute rates and total deployed processing cost remain unmeasured."),
        "rows": len(rows), "facts": len(canonical.get("facts", [])),
        "declared_pages": document.pages, "inventoried_pages": len(chain.get("inventories", {})),
        "mapped_pages": len(canonical["pages"]),
        "failure": int(disposition == "failed"), "refusal": int(disposition == "refused"),
        "proposed_rows": sum(row.get("disposition") == "extracted" for row in rows),
        "unconfirmed_readings": 0,
        "unconfirmed_reading_basis": "This native-only chain emits exact source-bound readings or explicit refusals; awaiting record acceptance is not uncertain transcription.",
        "field_outcomes": {
            status: sum(field["materialization"]["status"] == status for row in rows for field in row["fields"])
            for status in ("materialized", "refused", "not_extracted")
        },
    }
    record = {
        "schema": "pipeline-observation-v1", "observation_key": uuid4().hex,
        "started_at": started_at,
        "project_id": document.project_id, "configuration_sha256": configuration_sha256,
        "scope_sha256": scope.identity, "scope": scope.model_dump(mode="json"),
        "scope_text": canonical_text(scope.model_dump(mode="json")),
        "source_sha256": document.sha256, "plan": plan.model_dump(mode="json"),
        "disposition": disposition, "outcome": outcome, "chain": chain, "input_sha256": content_digest(chain),
        "output": canonical, "output_sha256": content_digest(canonical),
        "metrics": metrics, "artifacts": artifacts,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    register_pipeline_configuration(session, configuration)
    observation = PipelineObservation(
        project_id=document.project_id, document_id=document.id,
        extraction_run_id=extraction.run.id if extraction else None,
        configuration_sha256=configuration_sha256, scope_sha256=scope.identity,
        receipt_text=canonical_text(record), receipt_sha256=content_digest(record),
    )
    session.add(observation)
    session.flush()
    return PipelineShadowResult(observation, extraction)


def run_native_matrix_shadow(
    session: Session, document: Document, *, source_path: Path | str,
    scope: PipelineScope, client, plan: ObservationPlan, output_dir: Path | str,
    document_label: str, idempotency_key: str | None = None,
) -> PipelineShadowResult:
    """Append the source run and its full-chain observation as one transaction.

    Completed capture, classified processing artifacts and the permanent
    observation cannot separate if final receipt persistence fails. A model
    or source refusal remains an explicit observation with no partial run.
    """
    with session.begin_nested():
        return _run_native_matrix_shadow(
            session, document, source_path=source_path, scope=scope, client=client,
            plan=plan, output_dir=output_dir, document_label=document_label,
            idempotency_key=idempotency_key,
        )


def run_selected_native_matrix(
    session: Session, document: Document, *, deployment: str, client,
    plan: ObservationPlan, source_path: Path | str, output_dir: Path | str,
    document_label: str, idempotency_key: str | None = None,
) -> PipelineShadowResult:
    """Production routing, guarded by actual deployed configuration bytes.

    This does not make a captured Fact effective. The separate selection
    relation chooses this source capture chain; accepted changes retain their
    existing human or released-policy authority boundary.
    """
    from corridor.pipeline_selection_readback import PipelineQualificationRefused, selected_pipeline_configuration

    configuration = native_pipeline_configuration(client, plan)
    scope = selected_pipeline_configuration(
        session, document, deployment=deployment, configuration_sha256=content_digest(configuration),
    )
    required_mode = "synthetic" if scope.purpose == "synthetic_validation" else "fresh_provider"
    if plan.mode != required_mode:
        raise PipelineQualificationRefused("selected routing requires its qualified observation origin")
    return run_native_matrix_shadow(
        session, document, source_path=source_path, scope=scope, client=client,
        plan=plan, output_dir=output_dir, document_label=document_label,
        idempotency_key=idempotency_key,
    )
