"""Persist independent pipeline measurements and explicit scoped selection.

Completed challenger runs are deliberately ineligible for legacy declaration.
This boundary adds a separate maintenance act after a concrete measured gate;
it never invokes declaration, admission, reconciliation or accepted-value
commands. All decisions append. A project lock and expected predecessor make
selection concurrent-safe, and rollback is another attributable selection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.models import (
    Document, PipelineComparison, PipelineConfiguration, PipelineObservation,
    PipelineQualification, PipelineQualificationPolicy, PipelineSelection, Project,
)
from corridor.pipeline_comparison import compare_pipeline_outputs
from corridor.pipeline_contracts import MeasuredEvidence, PipelineScope, QualificationPolicy, canonical_text, content_digest
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.retention import register_processing_artifact


POLICY_VERSION = "native-matrix-qualification-v1"


class PipelineQualificationRefused(ValueError):
    """Missing, stale, mismatched or unauthorized qualification evidence."""


def _maintenance_capability(session: Session) -> None:
    # The existing local CLI uses the migration/maintenance credential. Web,
    # worker and source-append capabilities cannot acquire this grant through
    # a SECURITY DEFINER command. Identity alone is not authority.
    allowed = session.scalar(text(
        "select pg_has_role(current_user, relowner, 'USAGE') "
        "from pg_class where oid = 'public.pipeline_selections'::regclass"
    ))
    if not allowed:
        raise PipelineQualificationRefused("pipeline qualification/selection requires the database maintenance capability")


def _maintainer(session: Session, actor: HumanPrincipal) -> str:
    principal = require_human_principal(actor)
    _maintenance_capability(session)
    return principal.subject


def _measurement_actor(session: Session, actor: HumanPrincipal | str) -> str:
    _maintenance_capability(session)
    if isinstance(actor, HumanPrincipal):
        return require_human_principal(actor).subject
    if not isinstance(actor, str) or ":" not in actor or any(character.isspace() for character in actor):
        raise PipelineQualificationRefused("measurement actor must be an explicit namespaced service or human identity")
    return actor


def pipeline_receipt(row) -> dict:
    record = json.loads(row.receipt_text)
    if (sha256(row.receipt_text.encode()).hexdigest() != row.receipt_sha256
            or record["project_id"] != row.project_id
            or record["configuration_sha256"] != row.configuration_sha256
            or record["scope_sha256"] != row.scope_sha256):
        raise PipelineQualificationRefused("pipeline receipt bytes or scope binding differ")
    return record


def register_qualification_policy(
    session: Session, policy: QualificationPolicy, *, actor: HumanPrincipal,
    contract_paths: Iterable[Path],
) -> PipelineQualificationPolicy:
    """Freeze criteria and their original contract bytes before measuring them."""
    actor_subject = _maintainer(session, actor)
    artifacts = {sha256(Path(path).read_bytes()).hexdigest() for path in contract_paths}
    if not {item.contract_sha256 for item in policy.criteria} <= artifacts:
        raise PipelineQualificationRefused("policy criteria need their exact original metric-contract artifacts")
    existing = session.get(PipelineQualificationPolicy, policy.identity)
    if existing is not None:
        if existing.policy_text != canonical_text(policy.model_dump(mode="json")):
            raise PipelineQualificationRefused("registered metric policy bytes differ")
        return existing
    row = PipelineQualificationPolicy(
        policy_sha256=policy.identity, scope_sha256=policy.scope_sha256,
        policy_text=canonical_text(policy.model_dump(mode="json")), actor=actor_subject,
    )
    session.add(row)
    session.flush()
    return row


def _append(session, model, record: dict, **columns):
    serialized = canonical_text(record)
    row = model(
        project_id=record["project_id"], configuration_sha256=record["configuration_sha256"],
        scope_sha256=record["scope_sha256"], receipt_text=serialized,
        receipt_sha256=sha256(serialized.encode()).hexdigest(), **columns,
    )
    session.add(row)
    session.flush()
    return row


def _comparison_record(observation, actor: str, kind: str) -> dict:
    return {
        "schema": "pipeline-comparison-v1", "kind": kind,
        "project_id": observation.project_id, "configuration_sha256": observation.configuration_sha256,
        "scope_sha256": observation.scope_sha256, "actor": actor,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def record_repeatability(
    session: Session, left_id: int, right_id: int, *, actor: HumanPrincipal,
) -> PipelineComparison:
    actor_subject = _measurement_actor(session, actor)
    if left_id == right_id:
        raise PipelineQualificationRefused("repeatability needs two actual observations")
    left = session.get_one(PipelineObservation, left_id)
    right = session.get_one(PipelineObservation, right_id)
    a, b = pipeline_receipt(left), pipeline_receipt(right)
    if (left.project_id, left.configuration_sha256, left.scope_sha256, a["source_sha256"]) != (
        right.project_id, right.configuration_sha256, right.scope_sha256, b["source_sha256"],
    ):
        raise PipelineQualificationRefused("repeatability observations have different source/configuration/scope")
    comparison = compare_pipeline_outputs(a["output"], b["output"])
    same_inputs = a["input_sha256"] == b["input_sha256"]
    same_outcome = a["outcome"] == b["outcome"]
    passed = same_inputs and comparison["equal"] and same_outcome
    return _append(session, PipelineComparison, {
        **_comparison_record(left, actor_subject, "repeatability"),
        "left_observation_id": left.id, "right_observation_id": right.id,
        "left_observation_sha256": left.receipt_sha256, "right_observation_sha256": right.receipt_sha256,
        "source_sha256": a["source_sha256"], "same_inputs": same_inputs, "same_outcome": same_outcome,
        "left_outcome": a["outcome"], "right_outcome": b["outcome"], "passed": passed,
        "comparison": comparison,
        "limit": "Conditional on these exact inputs and observation origins; replay is not fresh model quality.",
    }, kind="repeatability")


def record_quality(
    session: Session, observation_id: int, *, reference_path: Path | str,
    actor: HumanPrincipal,
) -> PipelineComparison:
    """Compare against explicit reference bytes; record their actual origin.

    The source-only authoring boundary (#738) produces references. This
    consumer accepts its declared provenance, never relabels retained answers
    or incumbent proposals as independent gold, and retains the exact bytes.
    """
    actor_subject = _measurement_actor(session, actor)
    observation = session.get_one(PipelineObservation, observation_id)
    observed = pipeline_receipt(observation)
    path = Path(reference_path)
    reference_bytes = path.read_bytes()
    reference = json.loads(reference_bytes)
    if (reference.get("schema") != "pipeline-quality-reference-v1"
            or reference.get("source_sha256") != observed["source_sha256"]
            or reference.get("corpus_sha256") != observed["scope"]["corpus_sha256"]
            or reference.get("corpus_version") != observed["scope"]["corpus_version"]
            or reference.get("output", {}).get("source_sha256") != observed["source_sha256"]):
        raise PipelineQualificationRefused("quality reference differs from its exact source/corpus binding")
    origin = reference.get("origin", {})
    if origin.get("method") not in {
        "source_authored_native_reference", "source_authored_legacy_reference",
        "synthetic_source_authored", "independent_manual_annotation",
        "retained_answer_reference", "incumbent_observation",
    } or not origin.get("artifact_sha256") or not origin.get("description"):
        raise PipelineQualificationRefused("quality reference must disclose its source-only or non-independent origin")
    source_authored = origin["method"] in {"source_authored_native_reference", "source_authored_legacy_reference", "synthetic_source_authored", "independent_manual_annotation"}
    # Native/legacy machine references share their reader and only label a
    # source-ID/critical subset ceiling (#738). Source-only authorship does
    # not upgrade them into independent full-field/occurrence labels.
    independent = (origin["method"] == "independent_manual_annotation"
                   and set(origin.get("labeled_scope", [])) == {"fields", "physical_occurrences", "dispositions", "refusals", "diagnostics"}
                   and bool(origin.get("annotator")))
    synthetic_reference = (origin["method"] == "synthetic_source_authored"
                           and observed["scope"]["purpose"] == "synthetic_validation")
    comparison = compare_pipeline_outputs(reference["output"], observed["output"])
    artifact = register_processing_artifact(
        session, project_id=observation.project_id, kind="evaluation_working_data",
        path=path, terminal_at=datetime.now(timezone.utc),
    )
    return _append(session, PipelineComparison, {
        **_comparison_record(observation, actor_subject, "quality"),
        "observation_id": observation.id, "observation_sha256": observation.receipt_sha256,
        "source_sha256": observed["source_sha256"], "reference_sha256": sha256(reference_bytes).hexdigest(),
        "reference_origin": origin, "reference_artifact_id": artifact.id,
        "source_only_authoring": source_authored, "independent_reference": independent,
        "synthetic_reference": synthetic_reference, "comparison": comparison,
        "passed": comparison["passed"] and observed["disposition"] == "completed",
        "observation_mode": observed["plan"]["mode"],
    }, kind="quality")


def _bound_comparisons(session, ids, observations, kind):
    if len(set(ids)) != len(ids):
        raise PipelineQualificationRefused("comparison inputs repeat a receipt")
    rows = [session.get_one(PipelineComparison, identity) for identity in ids]
    for row in rows:
        if row.kind != kind:
            raise PipelineQualificationRefused("repeatability and quality receipt types are separate")
        record = pipeline_receipt(row)
        if kind == "quality":
            references = [(record["observation_id"], record["observation_sha256"])]
        else:
            references = [(record[f"{side}_observation_id"], record[f"{side}_observation_sha256"])
                          for side in ("left", "right")]
        if not any(identity in observations for identity, _ in references):
            raise PipelineQualificationRefused("comparison does not measure the nominated cohort")
        for identity, digest in references:
            observed = session.get_one(PipelineObservation, identity)
            if (observed.receipt_sha256 != digest or observed.project_id != row.project_id
                    or observed.configuration_sha256 != row.configuration_sha256
                    or observed.scope_sha256 != row.scope_sha256):
                raise PipelineQualificationRefused("comparison observation binding differs")
    return rows


def record_qualification(
    session: Session, *, observation_ids: Iterable[int], repeatability_ids: Iterable[int] = (),
    quality_ids: Iterable[int] = (), evidence: Iterable[tuple[MeasuredEvidence, Path]] = (),
    actor: HumanPrincipal,
) -> PipelineQualification:
    """Apply the fixed native scope policy to concrete, nonempty bound evidence."""
    actor_subject = _measurement_actor(session, actor)
    identities = tuple(observation_ids)
    if not identities or len(set(identities)) != len(identities):
        raise PipelineQualificationRefused("qualification needs distinct actual observations")
    observations = {identity: session.get_one(PipelineObservation, identity) for identity in identities}
    first = next(iter(observations.values()))
    records = [pipeline_receipt(row) for row in observations.values()]
    scope = PipelineScope.model_validate(records[0]["scope"])
    if scope.purpose == "synthetic_validation" and not session.get_one(Project, first.project_id).is_synthetic:
        raise PipelineQualificationRefused("synthetic gates cannot qualify a real project")
    if any((row.project_id, row.configuration_sha256, row.scope_sha256) != (
            first.project_id, first.configuration_sha256, first.scope_sha256) for row in observations.values()):
        raise PipelineQualificationRefused("qualification cohort mixes projects/configurations/scopes")
    if len({record["source_sha256"] for record in records}) != len(records):
        raise PipelineQualificationRefused("nominate exactly one quality observation per source; repeated attempts belong in repeatability")
    repeated = _bound_comparisons(session, tuple(repeatability_ids), observations, "repeatability")
    qualities = _bound_comparisons(session, tuple(quality_ids), observations, "quality")
    missing, failed = [], []
    if {record["source_sha256"] for record in records} != set(scope.source_sha256s):
        missing.append("declared_corpus_coverage")
    for identity, observed in observations.items():
        record = pipeline_receipt(observed)
        label = record["source_sha256"]
        if record["disposition"] != "completed":
            failed.append(f"{label}: {record['disposition']}")
        repeated_here = [pipeline_receipt(row) for row in repeated if identity in (
            pipeline_receipt(row)["left_observation_id"], pipeline_receipt(row)["right_observation_id"])]
        quality_here = [pipeline_receipt(row) for row in qualities if pipeline_receipt(row)["observation_id"] == identity]
        if not repeated_here:
            missing.append(f"{label}: repeatability")
        elif not all(row["passed"] for row in repeated_here):
            failed.append(f"{label}: repeatability")
        if not quality_here or not any(row["independent_reference"] or row["synthetic_reference"] for row in quality_here):
            missing.append(f"{label}: independent_source_quality")
        if quality_here and not all(row["passed"] for row in quality_here):
            failed.append(f"{label}: structured_quality")
        if scope.purpose == "prospective_production" and record["plan"]["mode"] != "fresh_provider":
            missing.append(f"{label}: fresh_model_observation")
        if scope.purpose == "synthetic_validation" and record["plan"]["mode"] != "synthetic":
            missing.append(f"{label}: synthetic_source_observation")
        if not record["metrics"]["rows"]:
            failed.append(f"{label}: empty_row_denominator")
        if record["metrics"].get("latency_ms") is None:
            missing.append(f"{label}: measured_latency")
    measured = {}
    bound_digests = sorted(row.receipt_sha256 for row in observations.values())
    for item, artifact_path in evidence:
        item = MeasuredEvidence.model_validate(item.model_dump())
        if (item.configuration_sha256 != first.configuration_sha256 or item.scope_sha256 != scope.identity
                or sorted(item.observation_sha256s) != bound_digests
                or sha256(Path(artifact_path).read_bytes()).hexdigest() != item.artifact_sha256):
            raise PipelineQualificationRefused("numeric measurement is not bound to these exact observation/configuration/scope/artifact bytes")
        artifact_record = json.loads(Path(artifact_path).read_bytes())
        if artifact_record.get("measurement") != item.model_dump(mode="json", exclude={"artifact_sha256"}):
            raise PipelineQualificationRefused("numeric value or denominator differs from its retained measurement artifact")
        if item.name in measured:
            raise PipelineQualificationRefused("numeric measurement repeats a metric")
        artifact = register_processing_artifact(
            session, project_id=first.project_id, kind="evaluation_working_data",
            path=artifact_path, terminal_at=datetime.now(timezone.utc),
        )
        measured[item.name] = {**item.model_dump(mode="json"), "artifact_id": artifact.id}
    configuration = json.loads(session.get_one(PipelineConfiguration, first.configuration_sha256).configuration_text)
    policy_sha = configuration.get("qualification_policy_sha256")
    policy_row = session.get(PipelineQualificationPolicy, policy_sha) if policy_sha else None
    policy = None
    if policy_row is None:
        missing.append("predeclared_metric_policy")
    else:
        policy = QualificationPolicy.model_validate_json(policy_row.policy_text)
        if policy.identity != policy_sha or policy.scope_sha256 != scope.identity:
            raise PipelineQualificationRefused("metric policy is not bound to the qualified scope/configuration")
        if any("started_at" not in record or datetime.fromisoformat(record["started_at"]) < policy_row.created_at for record in records):
            failed.append("metric_policy_not_predeclared")
        if scope.history == "compatibility_claimed" and not any(item.contract == "history" for item in policy.criteria):
            missing.append("historical_citation_contract")
        for criterion in policy.criteria:
            value = measured.get(criterion.name)
            if value is None or value["measurement"] != "measured":
                missing.append(criterion.name)
            elif value["contract_sha256"] != criterion.contract_sha256:
                raise PipelineQualificationRefused("measurement changed the predeclared metric contract")
            elif criterion.rule == "minimum_ratio" and (
                value["value"] > value["denominator"] or value["value"] / value["denominator"] < criterion.limit
            ):
                failed.append(criterion.name)
            elif criterion.rule == "maximum_value" and value["value"] > criterion.limit:
                failed.append(criterion.name)
    status = "failed" if failed else "incomplete" if missing else "passed"
    return _append(session, PipelineQualification, {
        "schema": "pipeline-qualification-v1", "policy": POLICY_VERSION,
        "metric_policy_sha256": policy_sha,
        "metric_policy": policy.model_dump(mode="json") if policy else None,
        "project_id": first.project_id, "configuration_sha256": first.configuration_sha256,
        "scope_sha256": scope.identity, "scope": scope.model_dump(mode="json"),
        "status": status, "missing": sorted(missing), "failed": sorted(failed),
        "observation_ids": sorted(observations), "observation_sha256s": bound_digests,
        "repeatability_ids": [row.id for row in repeated], "quality_ids": [row.id for row in qualities],
        "measurements": measured, "document_metrics": {record["source_sha256"]: record["metrics"] for record in records},
        "unmeasured": [name for name in ("handling_minutes", "validated_customer_roi") if name not in measured],
        "history": {"disposition": scope.history, "limit": "Original history stays intact; prospective scope does not qualify historical citation migration or engine retirement."},
        "actor": actor_subject, "recorded_at": datetime.now(timezone.utc).isoformat(),
    }, status=status)


def select_qualified_pipeline(
    session: Session, qualification_id: int, *, actor: HumanPrincipal, reason: str,
    expected_selection_id: int | None, enabled: bool = True,
) -> PipelineSelection:
    """Append a maintainer selection or disable/restore it without record writes."""
    actor_subject = _maintainer(session, actor)
    if not isinstance(reason, str) or not reason.strip() or type(enabled) is not bool:
        raise PipelineQualificationRefused("selection needs an explicit reason and enabled state")
    qualification = session.get_one(PipelineQualification, qualification_id)
    gate = pipeline_receipt(qualification)
    if qualification.status != "passed" or gate["status"] != "passed" or gate["missing"] or gate["failed"]:
        raise PipelineQualificationRefused("pipeline qualification is not complete and passing")
    scope = PipelineScope.model_validate(gate["scope"])
    session.execute(select(Project.id).where(Project.id == qualification.project_id).with_for_update())
    current = session.scalar(select(PipelineSelection).where(
        PipelineSelection.project_id == qualification.project_id, PipelineSelection.deployment == scope.deployment,
    ).order_by(PipelineSelection.id.desc()).limit(1))
    if (current.id if current else None) != expected_selection_id:
        raise PipelineQualificationRefused("pipeline selection predecessor changed")
    record = {
        "schema": "pipeline-selection-v1", "project_id": qualification.project_id,
        "configuration_sha256": qualification.configuration_sha256, "scope_sha256": scope.identity,
        "qualification_id": qualification.id, "qualification_sha256": qualification.receipt_sha256,
        "scope": scope.model_dump(mode="json"), "previous_selection_id": expected_selection_id,
        "actor": actor_subject, "reason": reason, "enabled": enabled,
        "selected_at": datetime.now(timezone.utc).isoformat(),
    }
    return _append(session, PipelineSelection, record, deployment=scope.deployment,
                   qualification_id=qualification.id, previous_selection_id=expected_selection_id,
                   actor=actor_subject, reason=reason, enabled=enabled)


def selected_pipeline_configuration(
    session: Session, document: Document, *, deployment: str,
    configuration_sha256: str,
) -> PipelineScope:
    """Require the selected source/deployment/configuration before future work."""
    selection = session.scalar(select(PipelineSelection).where(
        PipelineSelection.project_id == document.project_id, PipelineSelection.deployment == deployment,
    ).order_by(PipelineSelection.id.desc()).limit(1))
    if selection is None or not selection.enabled:
        raise PipelineQualificationRefused("no enabled qualified pipeline selection for this deployment")
    record = pipeline_receipt(selection)
    gate_row = session.get_one(PipelineQualification, selection.qualification_id)
    gate = pipeline_receipt(gate_row)
    scope = PipelineScope.model_validate(record["scope"])
    if (gate_row.status != "passed" or gate["status"] != "passed"
            or gate_row.receipt_sha256 != record["qualification_sha256"]
            or selection.configuration_sha256 != configuration_sha256
            or document.sha256 not in scope.source_sha256s or document.doc_type != "matrix"
            or (scope.purpose == "synthetic_validation" and not session.get_one(Project, document.project_id).is_synthetic)):
        raise PipelineQualificationRefused("selected pipeline does not qualify this exact source/configuration/class")
    configuration = session.get_one(PipelineConfiguration, configuration_sha256)
    if sha256(configuration.configuration_text.encode()).hexdigest() != configuration_sha256:
        raise PipelineQualificationRefused("selected configuration bytes differ")
    return scope
