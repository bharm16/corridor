"""Deterministic controlled-lane executor for the M8 acceptance gate.

This module owns synthetic scenario setup, exact comparison execution, policy
exercise, and controlled evidence export.  It never calls a model and never
operates on the captured real-project lane.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor import automatic_carry_forward as automatic_carry_forward_module
from corridor.adjudicate import accept_candidate, edit_candidate
from corridor.automatic_carry_forward import (
    authorize_automatic_carry_forward,
    run_automatic_carry_forward,
)
from corridor.exceptions import evaluate as evaluate_exceptions
from corridor.extract_matrix import ExtractionFailed
from corridor.extract_project import extract_project
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.m8_acceptance_contract import (
    AcceptanceError,
    AssertionResult,
    CLAIM_BOUNDARY,
    ControlledContradiction,
)
from corridor.models import (
    ActiveAutomaticCarryForwardPolicy,
    ActiveExtractionRun,
    Assertion,
    AuditLog,
    AutomaticCarryForwardOutcome,
    AutomaticCarryForwardPolicyApproval,
    AutomaticCarryForwardReceipt,
    AutomaticCarryForwardRun,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionRun,
    OperativeSupport,
    Project,
    RevisionComparisonRun,
)
from corridor.operative_support import resolve_operative_support
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import (
    create_revision_comparison,
    read_revision_comparison,
)
from corridor.supersession import (
    SupersessionDeclaration,
    actionable_candidate_query,
    register_supersessions,
)
from corridor.supersession_review import build_reviewer_worklist


_SIMULATED_PRINCIPAL = HumanPrincipal("local:m8-acceptance-fixture")
_CONTROLLED_PROMPT_VERSION = "m8-controlled-transformations-v1"
_CONTROLLED_MODEL = "deterministic-controlled-fixture-v1"


def _assert_partition(readback) -> None:
    predecessor_ids = [
        candidate_id
        for finding in readback.findings
        for candidate_id in finding.predecessor_candidate_ids
    ]
    successor_ids = [
        candidate_id
        for finding in readback.findings
        for candidate_id in finding.successor_candidate_ids
    ]
    expected_predecessors = [
        item["candidate_id"] for item in readback.predecessor_inputs
    ]
    expected_successors = [item["candidate_id"] for item in readback.successor_inputs]
    if Counter(predecessor_ids) != Counter(expected_predecessors):
        raise AcceptanceError("comparison does not partition predecessor inputs")
    if Counter(successor_ids) != Counter(expected_successors):
        raise AcceptanceError("comparison does not partition successor inputs")


def _ledger_counts(session: Session, project_id: int) -> dict[str, int]:
    dependency_ids = select(Dependency.id).where(Dependency.project_id == project_id)
    return {
        "dependencies": session.scalar(
            select(func.count()).select_from(Dependency).where(
                Dependency.project_id == project_id
            )
        ),
        "assertions": session.scalar(
            select(func.count()).select_from(Assertion).where(
                Assertion.dependency_id.in_(dependency_ids)
            )
        ),
        "evidence_links": session.scalar(
            select(func.count()).select_from(EvidenceLink).where(
                EvidenceLink.dependency_id.in_(dependency_ids)
            )
        ),
        "operative_support": session.scalar(
            select(func.count()).select_from(OperativeSupport).where(
                OperativeSupport.dependency_id.in_(dependency_ids)
            )
        ),
        "carry_forward_receipts": session.scalar(
            select(func.count())
            .select_from(AutomaticCarryForwardReceipt)
            .where(AutomaticCarryForwardReceipt.project_id == project_id)
        ),
    }


def _automation_record_snapshot(
    session: Session,
    project_id: int,
    successor_candidate_ids: tuple[int, ...],
) -> dict[str, dict[int, dict[str, Any]]]:
    """Freeze the Ledger-facing rows at the exact carry execution boundary."""

    dependencies = tuple(
        session.scalars(
            select(Dependency)
            .where(Dependency.project_id == project_id)
            .order_by(Dependency.id)
        ).all()
    )
    dependency_ids = tuple(item.id for item in dependencies)
    assertions = tuple(
        session.scalars(
            select(Assertion)
            .where(Assertion.dependency_id.in_(dependency_ids))
            .order_by(Assertion.id)
        ).all()
    )
    evidence_links = tuple(
        session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id.in_(dependency_ids))
            .order_by(EvidenceLink.id)
        ).all()
    )
    operative_support = tuple(
        session.scalars(
            select(OperativeSupport)
            .where(OperativeSupport.dependency_id.in_(dependency_ids))
            .order_by(OperativeSupport.id)
        ).all()
    )
    receipts = tuple(
        session.scalars(
            select(AutomaticCarryForwardReceipt)
            .where(AutomaticCarryForwardReceipt.project_id == project_id)
            .order_by(AutomaticCarryForwardReceipt.audit_log_id)
        ).all()
    )
    carry_runs = tuple(
        session.scalars(
            select(AutomaticCarryForwardRun)
            .where(AutomaticCarryForwardRun.project_id == project_id)
            .order_by(AutomaticCarryForwardRun.id)
        ).all()
    )
    carry_outcomes = tuple(
        session.scalars(
            select(AutomaticCarryForwardOutcome)
            .where(AutomaticCarryForwardOutcome.project_id == project_id)
            .order_by(AutomaticCarryForwardOutcome.id)
        ).all()
    )
    project_candidate_ids = select(Candidate.id).where(
        Candidate.project_id == project_id
    )
    audit_entries = tuple(
        session.scalars(
            select(AuditLog)
            .where(
                or_(
                    (
                        (AuditLog.entity_type == audit.DEPENDENCY)
                        & AuditLog.entity_id.in_(dependency_ids)
                    ),
                    (
                        (AuditLog.entity_type == audit.CANDIDATE)
                        & AuditLog.entity_id.in_(project_candidate_ids)
                    ),
                    (
                        (AuditLog.entity_type == audit.PROJECT)
                        & (AuditLog.entity_id == project_id)
                    ),
                )
            )
            .order_by(AuditLog.id)
        ).all()
    )
    successor_candidates = tuple(
        session.scalars(
            select(Candidate)
            .where(Candidate.id.in_(successor_candidate_ids))
            .order_by(Candidate.id)
        ).all()
    )
    return {
        "dependencies": {
            item.id: _dependency_fields_snapshot(item) for item in dependencies
        },
        "assertions": {
            item.id: {
                "dependency_id": item.dependency_id,
                "field_name": item.field_name,
                "asserted_value": item.asserted_value,
                "evidence_link_id": item.evidence_link_id,
            }
            for item in assertions
        },
        "evidence_links": {
            item.id: {
                "dependency_id": item.dependency_id,
                "document_id": item.document_id,
                "page_no": item.page_no,
                "quote": item.quote,
                "verified": item.verified,
                "satisfies_requirement": item.satisfies_requirement,
            }
            for item in evidence_links
        },
        "operative_support": {
            item.id: {
                "dependency_id": item.dependency_id,
                "evidence_link_id": item.evidence_link_id,
                "role": item.role,
                "field_name": item.field_name,
                "designated_by": item.designated_by,
            }
            for item in operative_support
        },
        "automatic_carry_forward_receipts": {
            item.audit_log_id: {
                "dependency_id": item.dependency_id,
                "policy_approval_id": item.policy_approval_id,
                "comparison_id": item.comparison_id,
                "finding_id": item.finding_id,
                "predecessor_candidate_id": item.predecessor_candidate_id,
                "successor_candidate_id": item.successor_candidate_id,
                "new_evidence_link_id": item.new_evidence_link_id,
                "origin_admission_audit_id": item.origin_admission_audit_id,
                "predecessor_support_transfer_audit_id": (
                    item.predecessor_support_transfer_audit_id
                ),
                "before_json": deepcopy(item.before_json),
                "after_json": deepcopy(item.after_json),
            }
            for item in receipts
        },
        "automatic_carry_forward_runs": {
            item.id: {
                "policy_approval_id": item.policy_approval_id,
                "policy_version": item.policy_version,
                "policy_sha256": item.policy_sha256,
                "abstention_reason_version": item.abstention_reason_version,
                "carried_count": item.carried_count,
                "abstained_count": item.abstained_count,
            }
            for item in carry_runs
        },
        "automatic_carry_forward_outcomes": {
            item.id: {
                "run_id": item.run_id,
                "policy_approval_id": item.policy_approval_id,
                "dependency_id": item.dependency_id,
                "outcome": item.outcome,
                "reason": item.reason,
                "reason_version": item.reason_version,
                "receipt_audit_log_id": item.receipt_audit_log_id,
                "comparison_id": item.comparison_id,
                "finding_id": item.finding_id,
                "predecessor_candidate_id": item.predecessor_candidate_id,
                "successor_candidate_id": item.successor_candidate_id,
            }
            for item in carry_outcomes
        },
        "audit": {
            item.id: {
                "action": item.action,
                "actor": item.actor,
                "human_principal": item.human_principal,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "before_json": deepcopy(item.before_json),
                "after_json": deepcopy(item.after_json),
            }
            for item in audit_entries
        },
        "successor_candidates": {
            item.id: {
                "state": item.state,
                "merged_into": item.merged_into,
                "payload_json": deepcopy(item.payload_json),
            }
            for item in successor_candidates
        },
    }


def _automation_write_boundary(
    before: dict[str, dict[int, dict[str, Any]]],
    after: dict[str, dict[int, dict[str, Any]]],
) -> dict[str, Any]:
    """Export a mechanically checkable row delta, not a narrative claim."""

    deltas: dict[str, dict[str, list[int]]] = {}
    for category in sorted(before):
        before_rows = before[category]
        after_rows = after[category]
        deltas[category] = {
            "created_ids": sorted(after_rows.keys() - before_rows.keys()),
            "updated_ids": sorted(
                row_id
                for row_id in before_rows.keys() & after_rows.keys()
                if before_rows[row_id] != after_rows[row_id]
            ),
            "deleted_ids": sorted(before_rows.keys() - after_rows.keys()),
        }
    observed_mutations = sorted(
        category
        for category, delta in deltas.items()
        if any(delta.values())
    )
    created_audit_ids = deltas["audit"]["created_ids"]
    created_audits = [after["audit"][row_id] for row_id in created_audit_ids]
    admissions = [
        entry
        for entry in created_audits
        if entry["action"] in (audit.ACCEPT_CANDIDATE, audit.MERGE_CANDIDATE)
    ]
    human_admissions = [
        entry for entry in admissions if entry["human_principal"] is not None
    ]
    machine_admissions = [
        entry
        for entry in admissions
        if entry["actor"] == audit.AUTOMATIC_CARRY_FORWARD_ACTOR
    ]
    other_admissions = [
        entry
        for entry in admissions
        if entry not in human_admissions and entry not in machine_admissions
    ]
    allowed_mutations = sorted(
        {
            "audit",
            "automatic_carry_forward_outcomes",
            "automatic_carry_forward_receipts",
            "automatic_carry_forward_runs",
            "evidence_links",
            "operative_support",
        }
    )
    return {
        "boundary": (
            "after_policy_authorization_before_first_carry_through_"
            "idempotent_second_carry"
        ),
        "new_dependency_rows": len(deltas["dependencies"]["created_ids"]),
        "new_assertion_rows": len(deltas["assertions"]["created_ids"]),
        "admission_audit_actions_created": {
            "human": len(human_admissions),
            "machine": len(machine_admissions),
            "other": len(other_admissions),
            "total": len(admissions),
        },
        "successor_candidate_states": {
            str(row_id): row["state"]
            for row_id, row in sorted(after["successor_candidates"].items())
        },
        "allowed_mutation_categories": allowed_mutations,
        "observed_mutation_categories": observed_mutations,
        "audit_entries_created": created_audits,
        "record_deltas": deltas,
    }


def _canonical_automation_write_boundary(boundary: dict[str, Any]) -> dict[str, Any]:
    """Remove database identities while retaining every asserted write shape."""

    return {
        "boundary": boundary["boundary"],
        "new_dependency_rows": boundary["new_dependency_rows"],
        "new_assertion_rows": boundary["new_assertion_rows"],
        "admission_audit_actions_created": deepcopy(
            boundary["admission_audit_actions_created"]
        ),
        "successor_candidate_state_counts": dict(
            sorted(Counter(boundary["successor_candidate_states"].values()).items())
        ),
        "allowed_mutation_categories": list(
            boundary["allowed_mutation_categories"]
        ),
        "observed_mutation_categories": list(
            boundary["observed_mutation_categories"]
        ),
        "audit_entries_created": [
            {
                key: entry[key]
                for key in ("action", "actor", "human_principal", "entity_type")
            }
            for entry in boundary["audit_entries_created"]
        ],
        "record_delta_counts": {
            category: {
                key.removesuffix("_ids"): len(value)
                for key, value in delta.items()
            }
            for category, delta in boundary["record_deltas"].items()
        },
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _raw_comparison_export(
    readback,
    *,
    predecessor_registry_id: str,
    successor_registry_id: str,
) -> dict[str, Any]:
    comparison = readback.comparison
    return {
        "comparison_id": comparison.id,
        "predecessor_registry_id": predecessor_registry_id,
        "successor_registry_id": successor_registry_id,
        "predecessor_extraction_run_id": comparison.predecessor_extraction_run_id,
        "successor_extraction_run_id": comparison.successor_extraction_run_id,
        "matcher_version": comparison.matcher_version,
        "matcher_config": deepcopy(comparison.matcher_config),
        "content_sha256": comparison.content_sha256,
        "finding_count": comparison.finding_count,
        "finding_counts": dict(
            sorted(Counter(item.state for item in readback.findings).items())
        ),
        "predecessor_inputs": deepcopy(list(readback.predecessor_inputs)),
        "successor_inputs": deepcopy(list(readback.successor_inputs)),
        "findings": [
            {
                "finding_id": item.id,
                "ordinal": item.ordinal,
                "state": item.state,
                "predecessor_candidate_ids": list(item.predecessor_candidate_ids),
                "successor_candidate_ids": list(item.successor_candidate_ids),
                "match_score": item.match_score,
                "field_changes": deepcopy(item.field_changes),
                "matcher_detail": deepcopy(item.matcher_detail),
            }
            for item in readback.findings
        ],
    }


@dataclass
class _ControlledCase:
    definition: dict[str, Any]
    predecessor: Document
    predecessor_candidate: Candidate
    predecessor_run: ExtractionRun
    dependency: Dependency
    old_evidence: EvidenceLink
    successor: Document | None = None
    successor_candidates: tuple[Candidate, ...] = ()
    successor_run: ExtractionRun | None = None
    comparison: RevisionComparisonRun | None = None
    finding: Any = None
    review_before_policy: Any = None


def _run_controlled_lane(
    session: Session,
    *,
    seed: dict[str, Any],
    transformations: dict[str, Any],
    transformations_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project = Project(
        slug=f"m8-controlled-{uuid4().hex}",
        name="M8 controlled carry-forward conformance",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    lifecycle: list[dict[str, Any]] = []
    cases_export: list[dict[str, Any]] = []
    scope_fail_closed = {
        "default_candidate_hidden": False,
        "historical_override_visible": False,
    }
    superseded_citation_observed = False
    failed_attempt: dict[str, Any] | None = None
    before_policy = {
        "eligible_route": None,
        "automatic_writes": 0,
    }
    passes = {
        "first": {"carried": 0},
        "second": {"carried": 0},
        "receipt_count": 0,
    }

    def contradiction(
        *,
        name: str,
        observed: Any,
        expected: Any,
        detail: str,
    ) -> None:
        raw, canonical = _controlled_partial_exports(
            session,
            project=project,
            cases=cases,
            lifecycle=lifecycle,
            scope_fail_closed=scope_fail_closed,
            superseded_citation_observed=superseded_citation_observed,
            failed_attempt=failed_attempt,
            before_policy=before_policy,
            passes=passes,
            cases_export=cases_export,
            failure={
                "name": name,
                "observed": deepcopy(observed),
                "expected": deepcopy(expected),
                "detail": detail,
            },
        )
        raise ControlledContradiction(
            AssertionResult(
                name=name,
                passed=False,
                observed=deepcopy(observed),
                expected=deepcopy(expected),
                detail=detail,
            ),
            controlled_raw=raw,
            controlled_canonical=canonical,
        )

    case_definitions = list(transformations["cases"])
    index_text = "\n".join(
        ["M8 controlled supersession index"]
        + [
            f"M8-{item['case_id']}-A replaced by M8-{item['case_id']}-B on 8/6/2026"
            for item in case_definitions
        ]
        + ["M8-scope-sentinel-A replaced by M8-scope-sentinel-B on 8/6/2026"]
    )
    index = _controlled_document(
        session,
        project,
        registry_id="M8-CONTROLLED-INDEX",
        filename="m8-controlled-index.txt",
        page_text=index_text,
        doc_type="other",
        doc_date=date(2026, 8, 6),
    )

    cases: list[_ControlledCase] = []
    for ordinal, definition in enumerate(case_definitions, start=1):
        case_id = definition["case_id"]
        predecessor_fields = _controlled_case_fields(
            seed["fields"], definition, ordinal
        )
        predecessor_quote = _controlled_quote(predecessor_fields)
        page_text = predecessor_quote
        if definition.get("human_edit"):
            page_text += "\n" + str(definition["human_edit"]["value"])
        predecessor = _controlled_document(
            session,
            project,
            registry_id=f"M8-{case_id}-A",
            filename=f"m8-{case_id}-a.txt",
            page_text=page_text,
            doc_type="matrix",
            doc_date=date(2026, 1, ordinal),
        )
        predecessor_candidate = _controlled_candidate(
            project,
            predecessor,
            predecessor_fields,
        )
        predecessor_candidates = [predecessor_candidate]
        if definition["successor"]["kind"] == "fan_in_exact":
            predecessor_candidates.append(
                _controlled_candidate(project, predecessor, predecessor_fields)
            )
        predecessor_run = record_extraction_run(
            session,
            predecessor,
            prompt_version=_CONTROLLED_PROMPT_VERSION,
            candidate_count=len(predecessor_candidates),
            page_errors=0,
            candidates=tuple(predecessor_candidates),
            model=_CONTROLLED_MODEL,
            schema_version=_CONTROLLED_PROMPT_VERSION,
        )
        declare_active_run(
            session,
            predecessor.id,
            predecessor_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
        if definition.get("human_edit"):
            edited = dict(predecessor_fields)
            edit = definition["human_edit"]
            edited[edit["field"]] = edit["value"]
            edit_candidate(
                session,
                predecessor_candidate,
                edited,
                principal=_SIMULATED_PRINCIPAL,
            )
        dependency = accept_candidate(
            session,
            predecessor_candidate,
            principal=_SIMULATED_PRINCIPAL,
        )
        old_evidence = session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.dependency_id == dependency.id)
            .order_by(EvidenceLink.id)
        ).one()
        if definition["support_precondition"] != "publication_only":
            mark_satisfies(
                session,
                dependency.id,
                old_evidence.id,
                principal=_SIMULATED_PRINCIPAL,
            )
        cases.append(
            _ControlledCase(
                definition=definition,
                predecessor=predecessor,
                predecessor_candidate=predecessor_candidate,
                predecessor_run=predecessor_run,
                dependency=dependency,
                old_evidence=old_evidence,
            )
        )

    sentinel_fields = {
        "utility_id": "M8-SCOPE-SENTINEL",
        "external_org": "Controlled Scope Sentinel",
        "utility_type": "Telecom",
        "station_from": "9900+00",
        "baseline": "IH-45",
    }
    sentinel_predecessor = _controlled_document(
        session,
        project,
        registry_id="M8-scope-sentinel-A",
        filename="m8-scope-sentinel-a.txt",
        page_text=_controlled_quote(sentinel_fields),
        doc_type="matrix",
        doc_date=date(2026, 1, 31),
    )
    sentinel_candidate = _controlled_candidate(
        project, sentinel_predecessor, sentinel_fields
    )
    sentinel_run = record_extraction_run(
        session,
        sentinel_predecessor,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(sentinel_candidate,),
        model=_CONTROLLED_MODEL,
        schema_version=_CONTROLLED_PROMPT_VERSION,
    )
    declare_active_run(
            session,
            sentinel_predecessor.id,
            sentinel_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
    _controlled_document(
        session,
        project,
        registry_id="M8-scope-sentinel-B",
        filename="m8-scope-sentinel-b.txt",
        page_text="scope sentinel successor",
        doc_type="other",
        doc_date=date(2026, 8, 6),
    )
    register_supersessions(
        session,
        (
            SupersessionDeclaration(
                predecessor_registry_id="M8-scope-sentinel-A",
                successor_registry_id="M8-scope-sentinel-B",
                replacement_date=date(2026, 8, 6),
                source_registry_id=index.registry_id,
                source_page=1,
            ),
        ),
        project_id=project.id,
    )
    default_actionable_ids = set(
        session.scalars(actionable_candidate_query(project.id)).all()
    )
    historical_actionable_ids = set(
        session.scalars(
            actionable_candidate_query(
                project.id,
                historical_document_id=sentinel_predecessor.id,
            )
        ).all()
    )
    scope_fail_closed = {
        "default_candidate_hidden": (
            sentinel_candidate not in default_actionable_ids
        ),
        "historical_override_visible": (
            sentinel_candidate in historical_actionable_ids
        ),
    }

    readiness_case = next(
        item for item in cases if item.definition["case_id"] == "readiness-exact"
    )
    readiness_successor = _create_controlled_successor(
        session, project, readiness_case, ordinal=1
    )
    _register_controlled_edge(session, project, index, readiness_case)
    lifecycle = [
        _lifecycle_observation(
            session,
            project.id,
            readiness_case.dependency.id,
            expected="awaiting_extraction",
        )
    ]
    exceptions_after_registration = {
        item.rule
        for item in evaluate_exceptions(session, project.id)
        if item.dependency_id == readiness_case.dependency.id
    }
    superseded_citation_observed = (
        "SUPERSEDED_CITATION" in exceptions_after_registration
    )

    def injected_failure(_session: Session, document: Document) -> list[Candidate]:
        if document.id != readiness_successor.id:
            raise AcceptanceError("failure injection reached the wrong document")
        partial_fields = deepcopy(
            readiness_case.predecessor_run.candidate_inputs_json[0]["payload_json"][
                "fields"
            ]
        )
        partial_candidate = _controlled_candidate(
            project,
            document,
            partial_fields,
        )
        _session.add(partial_candidate)
        _session.flush([partial_candidate])
        raise ExtractionFailed("controlled injected extraction failure")

    failed_outcomes = extract_project(
        session,
        project,
        extract=injected_failure,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        redo=False,
        commit=False,
    )
    failed_outcome = next(
        item for item in failed_outcomes if item.document_id == readiness_successor.id
    )
    if failed_outcome.status != "failed" or failed_outcome.extraction_run_id is None:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={
                "failed_outcome_status": failed_outcome.status,
                "extraction_run_id": failed_outcome.extraction_run_id,
            },
            expected={
                "failed_outcome_status": "failed",
                "extraction_run_id": "present",
            },
            detail="injected controlled failure was not recorded",
        )
    session.commit()
    session.expire_all()
    failed_run = session.get(ExtractionRun, failed_outcome.extraction_run_id)
    if failed_run is None:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={"failed_run": None},
            expected={"failed_run": "durable"},
            detail="failed Extraction Run was not durable before retry",
        )
    partial_candidate_count_after_failure = int(
        session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.source_document_id == readiness_successor.id)
        )
        or 0
    )
    active_at_failure = (
        session.get(ActiveExtractionRun, readiness_successor.id) is not None
    )
    lifecycle.append(
        _lifecycle_observation(
            session,
            project.id,
            readiness_case.dependency.id,
            expected="extraction_failed",
        )
    )
    comparison_count_after_failure = session.scalar(
        select(func.count())
        .select_from(RevisionComparisonRun)
        .where(RevisionComparisonRun.project_id == project.id)
    )

    try:
        _extract_controlled_successor(session, project, readiness_case)
    except AcceptanceError as exc:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={"phase": "extract_readiness_successor"},
            expected="controlled readiness successor extraction succeeds",
            detail=str(exc),
        )
    lifecycle.append(
        _lifecycle_observation(
            session,
            project.id,
            readiness_case.dependency.id,
            expected="awaiting_active_run",
        )
    )
    assert readiness_case.successor_run is not None
    assert readiness_case.successor is not None
    declare_active_run(
            session,
            readiness_case.successor.id,
            readiness_case.successor_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
    lifecycle.append(
        _lifecycle_observation(
            session,
            project.id,
            readiness_case.dependency.id,
            expected="awaiting_comparison",
        )
    )
    try:
        _compare_controlled_case(session, readiness_case)
    except AcceptanceError as exc:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={"phase": "compare_readiness_case"},
            expected="controlled readiness comparison remains self-consistent",
            detail=str(exc),
        )
    try:
        review = _review_for_dependency(
            session, project.id, readiness_case.dependency.id
        )
    except AcceptanceError as exc:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={"phase": "review_readiness_case"},
            expected="one controlled readiness review",
            detail=str(exc),
        )
    lifecycle.append(
        {
            "review_status": "actionable",
            "observed_status": review.status,
            "route": review.route,
            "reason": review.reason,
        }
    )

    for ordinal, controlled_case in enumerate(cases[1:], start=2):
        _create_controlled_successor(
            session, project, controlled_case, ordinal=ordinal
        )
        _register_controlled_edge(session, project, index, controlled_case)
        try:
            _extract_controlled_successor(session, project, controlled_case)
        except AcceptanceError as exc:
            contradiction(
                name="controlled_lane_claims_remain_self_consistent",
                observed={
                    "phase": "extract_controlled_successor",
                    "case_id": controlled_case.definition["case_id"],
                },
                expected="controlled successor extraction succeeds",
                detail=str(exc),
            )
        assert controlled_case.successor is not None
        assert controlled_case.successor_run is not None
        declare_active_run(
            session,
            controlled_case.successor.id,
            controlled_case.successor_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
        try:
            _compare_controlled_case(session, controlled_case)
        except AcceptanceError as exc:
            contradiction(
                name="controlled_lane_claims_remain_self_consistent",
                observed={
                    "phase": "compare_controlled_case",
                    "case_id": controlled_case.definition["case_id"],
                },
                expected="controlled comparison remains self-consistent",
                detail=str(exc),
            )

    for controlled_case in cases:
        try:
            controlled_case.review_before_policy = _review_for_dependency(
                session, project.id, controlled_case.dependency.id
            )
        except AcceptanceError as exc:
            contradiction(
                name="controlled_lane_claims_remain_self_consistent",
                observed={
                    "phase": "review_before_policy",
                    "case_id": controlled_case.definition["case_id"],
                },
                expected="one controlled review before policy",
                detail=str(exc),
            )
    ledger_before_policy = _ledger_counts(session, project.id)
    before_policy_receipts = _carry_receipt_count(session, project.id)
    runtime = _acceptance_carry_runtime()
    before_policy_result = run_automatic_carry_forward(
        session, project.id, _runtime=runtime
    )
    before_policy = {
        "eligible_route": (
            "human_reconfirmation"
            if readiness_case.review_before_policy.route == "reconfirmation"
            else readiness_case.review_before_policy.route
        ),
        "automatic_writes": (
            _carry_receipt_count(session, project.id) - before_policy_receipts
        ),
    }
    if before_policy_result.carried or before_policy_result.abstentions:
        contradiction(
            name="controlled_lane_claims_remain_self_consistent",
            observed={
                "carried": len(before_policy_result.carried),
                "abstentions": len(before_policy_result.abstentions),
            },
            expected={"carried": 0, "abstentions": 0},
            detail="policy-disabled carry-forward produced an outcome",
        )

    before_fingerprints = {
        item.dependency.id: _ledger_mutation_fingerprint(
            session,
            item.dependency.id,
            tuple(candidate.id for candidate in item.successor_candidates),
        )
        for item in cases
    }
    before_dependency_fields = {
        item.dependency.id: _dependency_fields_snapshot(item.dependency)
        for item in cases
    }
    approval = authorize_automatic_carry_forward(
        session,
        project.id,
        principal=_SIMULATED_PRINCIPAL,
        _runtime=runtime,
    )
    successor_candidate_ids = tuple(
        candidate.id
        for controlled_case in cases
        for candidate in controlled_case.successor_candidates
    )
    automation_before = _automation_record_snapshot(
        session,
        project.id,
        successor_candidate_ids,
    )
    first = run_automatic_carry_forward(session, project.id, _runtime=runtime)
    after_first_fingerprints = {
        item.dependency.id: _ledger_mutation_fingerprint(
            session,
            item.dependency.id,
            tuple(candidate.id for candidate in item.successor_candidates),
        )
        for item in cases
    }
    second = run_automatic_carry_forward(session, project.id, _runtime=runtime)
    automation_after = _automation_record_snapshot(
        session,
        project.id,
        successor_candidate_ids,
    )
    automation_boundary = _automation_write_boundary(
        automation_before,
        automation_after,
    )
    after_second_fingerprints = {
        item.dependency.id: _ledger_mutation_fingerprint(
            session,
            item.dependency.id,
            tuple(candidate.id for candidate in item.successor_candidates),
        )
        for item in cases
    }
    after_dependency_fields = {
        item.dependency.id: _dependency_fields_snapshot(item.dependency)
        for item in cases
    }

    receipts = tuple(first.carried)
    receipt_by_dependency = {item.dependency_id: item for item in receipts}
    abstention_by_dependency = {
        item.dependency_id: item for item in first.abstentions
    }
    cases_export: list[dict[str, Any]] = []
    for controlled_case in cases:
        case_id = controlled_case.definition["case_id"]
        dependency_id = controlled_case.dependency.id
        receipt = receipt_by_dependency.get(dependency_id)
        abstention = abstention_by_dependency.get(dependency_id)
        production_reason = abstention.reason if abstention is not None else None
        abstention_reason = production_reason
        support = resolve_operative_support(session, (dependency_id,))[dependency_id]
        inherited_scopes = (
            sorted(
                {
                    item["role"]
                    for item in (receipt.after_json.get("moved_scopes") or [])
                }
            )
            if receipt is not None
            else []
        )
        successor_states = {
            candidate.state for candidate in controlled_case.successor_candidates
        }
        successor_state = (
            next(iter(successor_states)) if len(successor_states) == 1 else None
        )
        expected_state = _expected_correspondence_state(
            controlled_case.definition["expected_correspondence"]
        )
        actual_state = controlled_case.finding.state
        unresolved_after = any(
            item.dependency_id == dependency_id
            for item in build_reviewer_worklist(session, project.id).reviews
        )
        outcome = "carried" if receipt is not None else "abstained"
        cases_export.append(
            {
                "case_id": case_id,
                "expected_correspondence": expected_state,
                "observed_correspondence": actual_state,
                "outcome": outcome,
                "abstention_reason": abstention_reason,
                "production_abstention_reason": production_reason,
                "review_reason_before_policy": (
                    controlled_case.review_before_policy.reason
                ),
                "inherited_scopes": inherited_scopes,
                "ready_after": support.is_ready,
                "dependency_fields_changed": (
                    before_dependency_fields[dependency_id]
                    != after_dependency_fields[dependency_id]
                ),
                "ledger_unchanged": (
                    before_fingerprints[dependency_id]
                    == after_first_fingerprints[dependency_id]
                    == after_second_fingerprints[dependency_id]
                ),
                "successor_candidate_state": successor_state,
                "successor_candidate_ids": [
                    item.id for item in controlled_case.successor_candidates
                ],
                "predecessor_candidate_id": (
                    controlled_case.predecessor_candidate.id
                ),
                "finding_predecessor_candidate_ids": list(
                    controlled_case.finding.predecessor_candidate_ids
                ),
                "finding_successor_candidate_ids": list(
                    controlled_case.finding.successor_candidate_ids
                ),
                "finding_predecessor_candidate_count": len(
                    controlled_case.finding.predecessor_candidate_ids
                ),
                "finding_successor_candidate_count": len(
                    controlled_case.finding.successor_candidate_ids
                ),
                "dependency_id": dependency_id,
                "comparison_id": controlled_case.comparison.id,
                "finding_id": controlled_case.finding.id,
                "unresolved_after": unresolved_after,
            }
        )

    authorization_count = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity_type == audit.PROJECT,
            AuditLog.entity_id == project.id,
            AuditLog.action == audit.AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
        )
    )
    drift_outcome = _exercise_policy_drift(session)
    ledger_after = _ledger_counts(session, project.id)
    controlled_documents, controlled_runs, controlled_active_runs = (
        _controlled_lineage_exports(session, project.id)
    )
    raw_comparisons = [
        _raw_comparison_export(
            read_revision_comparison(session, item.comparison.id),
            predecessor_registry_id=item.predecessor.registry_id,
            successor_registry_id=item.successor.registry_id,
        )
        for item in cases
    ]
    raw = {
        "project_id": project.id,
        "claim_boundary": CLAIM_BOUNDARY,
        "test_precondition": {
            "principal": _SIMULATED_PRINCIPAL.subject,
            "simulated_human_setup": True,
            "human_review_performed": False,
        },
        "seed": deepcopy(seed),
        "transformations": {
            "schema_version": transformations["schema_version"],
            "sha256": transformations_sha256,
            "case_count": len(cases),
        },
        "lifecycle": lifecycle,
        "status": "passed",
        "scope_fail_closed": scope_fail_closed,
        "superseded_citation_observed": superseded_citation_observed,
        "failed_attempt": {
            "run_id": failed_run.id,
            "outcome": failed_run.outcome,
            "candidate_count": failed_run.candidate_count,
            "page_errors": failed_run.page_errors,
            "partial_candidate_count_after_failure": (
                partial_candidate_count_after_failure
            ),
            "active_at_failure": active_at_failure,
            "committed_before_retry": True,
            "comparison_count_after_failure": comparison_count_after_failure,
            "error_detail": failed_run.error_detail,
        },
        "before_policy": before_policy,
        "policy": {
            "approval_id": approval.id,
            "policy_version": approval.policy_version,
            "policy_sha256": approval.policy_sha256,
            "policy_json": deepcopy(approval.policy_json),
            "authorization_count": authorization_count,
            "drift_outcome": drift_outcome,
        },
        "passes": {
            "first": {"carried": len(first.carried)},
            "second": {"carried": len(second.carried)},
            "receipt_count": _carry_receipt_count(session, project.id),
        },
        "documents": controlled_documents,
        "runs": controlled_runs,
        "active_run_declarations": controlled_active_runs,
        "comparisons": raw_comparisons,
        "receipts": [
            {
                "audit_log_id": item.audit_log_id,
                "dependency_id": item.dependency_id,
                "policy_approval_id": item.policy_approval_id,
                "comparison_id": item.comparison_id,
                "finding_id": item.finding_id,
                "predecessor_candidate_id": item.predecessor_candidate_id,
                "successor_candidate_id": item.successor_candidate_id,
                "new_evidence_link_id": item.new_evidence_link_id,
                "before_json": deepcopy(item.before_json),
                "after_json": deepcopy(item.after_json),
            }
            for item in receipts
        ],
        "abstentions": [asdict(item) for item in first.abstentions],
        "cases": cases_export,
        "ledger_counts_before_policy": ledger_before_policy,
        "ledger_counts_after": ledger_after,
        "automation_write_boundary": automation_boundary,
    }
    canonical = {
        "claim_boundary": CLAIM_BOUNDARY,
        "test_precondition": raw["test_precondition"],
        "seed_selection_sha256": seed["selection_sha256"],
        "transformations": raw["transformations"],
        "lifecycle": [
            {
                key: item[key]
                for key in ("review_status", "observed_status", "route", "reason")
                if key in item
            }
            for item in lifecycle
        ],
        "scope_fail_closed": raw["scope_fail_closed"],
        "superseded_citation_observed": raw["superseded_citation_observed"],
        "failed_attempt": {
            key: raw["failed_attempt"][key]
            for key in (
                "outcome",
                "candidate_count",
                "page_errors",
                "partial_candidate_count_after_failure",
                "active_at_failure",
                "committed_before_retry",
                "comparison_count_after_failure",
                "error_detail",
            )
        },
        "before_policy": before_policy,
        "policy": {
            "policy_version": approval.policy_version,
            "policy_sha256": approval.policy_sha256,
            "authorization_count": authorization_count,
            "drift_outcome": drift_outcome,
        },
        "passes": raw["passes"],
        "automation_write_boundary": _canonical_automation_write_boundary(
            automation_boundary
        ),
        "lineage_counts": {
            "documents": len(controlled_documents),
            "runs": len(controlled_runs),
            "active_run_declarations": len(controlled_active_runs),
            "comparisons": len(raw_comparisons),
        },
        "cases": [
            {
                key: item[key]
                for key in (
                    "case_id",
                    "expected_correspondence",
                    "observed_correspondence",
                    "outcome",
                    "abstention_reason",
                    "production_abstention_reason",
                    "review_reason_before_policy",
                    "inherited_scopes",
                    "ready_after",
                    "dependency_fields_changed",
                    "ledger_unchanged",
                    "successor_candidate_state",
                    "finding_predecessor_candidate_count",
                    "finding_successor_candidate_count",
                    "unresolved_after",
                )
            }
            for item in cases_export
        ],
    }
    return raw, canonical


def _controlled_lineage_exports(
    session: Session,
    project_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents = tuple(
        session.scalars(
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.registry_id, Document.id)
        ).all()
    )
    document_by_id = {item.id: item for item in documents}
    runs = tuple(
        session.scalars(
            select(ExtractionRun)
            .where(ExtractionRun.document_id.in_(document_by_id))
            .order_by(ExtractionRun.id)
        ).all()
    )
    active = tuple(
        session.scalars(
            select(ActiveExtractionRun)
            .where(ActiveExtractionRun.document_id.in_(document_by_id))
            .order_by(ActiveExtractionRun.document_id)
        ).all()
    )
    return (
        [
            {
                "document_id": item.id,
                "registry_id": item.registry_id,
                "filename": item.filename,
                "sha256": item.sha256,
                "doc_type": item.doc_type,
                "pages": item.pages,
                "superseded_by": item.superseded_by,
            }
            for item in documents
        ],
        [
            {
                "run_id": item.id,
                "document_id": item.document_id,
                "registry_id": document_by_id[item.document_id].registry_id,
                "prompt_version": item.prompt_version,
                "model": item.model,
                "schema_version": item.schema_version,
                "outcome": item.outcome,
                "candidate_count": item.candidate_count,
                "page_errors": item.page_errors,
                "error_detail": item.error_detail,
                "candidate_inputs": deepcopy(item.candidate_inputs_json),
            }
            for item in runs
        ],
        [
            {
                "document_id": item.document_id,
                "registry_id": document_by_id[item.document_id].registry_id,
                "extraction_run_id": item.extraction_run_id,
            }
            for item in active
        ],
    )


def _controlled_document(
    session: Session,
    project: Project,
    *,
    registry_id: str,
    filename: str,
    page_text: str,
    doc_type: str,
    doc_date: date,
) -> Document:
    identity = _canonical_json(
        {
            "registry_id": registry_id,
            "filename": filename,
            "page_text": page_text,
        }
    )
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=_sha256(identity),
        filename=filename,
        doc_type=doc_type,
        doc_date=doc_date,
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=None,
            text_source="text_layer",
        )
    )
    session.flush()
    return document


def _controlled_case_fields(
    seed_fields: dict[str, Any],
    definition: dict[str, Any],
    ordinal: int,
) -> dict[str, str]:
    fields = {
        str(key): value.strip()
        for key, value in seed_fields.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }
    fields.update(
        {
            "utility_id": f"M8-{ordinal:03d}",
            "external_org": f"Controlled Utility {ordinal:03d}",
            "utility_type": "Telecom",
            "station_from": f"{ordinal * 100}+00",
            "baseline": "IH-45",
            "location": f"controlled case {definition['case_id']}",
        }
    )
    if (definition.get("predecessor") or {}).get("remove_identity") is True:
        for key in (
            "utility_id",
            "station_from",
            "station_to",
            "baseline",
            "location",
            "location_desc",
            "location_start",
            "location_end",
            "alignment",
        ):
            fields.pop(key, None)
    return fields


def _controlled_successor_fields(
    predecessor_fields: dict[str, Any],
    definition: dict[str, Any],
) -> dict[str, Any]:
    fields = deepcopy(predecessor_fields)
    successor = definition["successor"]
    if successor["kind"] == "changed":
        fields.update(successor.get("field_updates") or {})
    elif successor["kind"] == "normalized_only":
        station = fields.get("station_from")
        if isinstance(station, str):
            if successor.get("station_format") != "spaced":
                raise AcceptanceError("unsupported normalized station transformation")
            fields["station_from"] = station.replace("+", " + ")
    return fields


def _controlled_quote(fields: dict[str, Any]) -> str:
    return " | ".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if value is not None
    )


def _controlled_candidate(
    project: Project,
    document: Document,
    fields: dict[str, Any],
    *,
    citation_shape: str = "verified_single",
) -> Candidate:
    quote = _controlled_quote(fields)
    verified = citation_shape != "unverified_single"
    citation = {
        "document_id": document.id,
        "page": 1,
        "quote": quote,
        "verified": verified,
        "whole_row": True,
    }
    citation_count = 2 if citation_shape == "multiple" else 1
    payload = {
        "kind": "dependency",
        "fields": deepcopy(fields),
        "citations": [deepcopy(citation) for _ in range(citation_count)],
        "unverified_fields": [] if verified else sorted(fields),
        "unmapped_columns": [],
        "low_confidence_tokens": [],
        "tier": "controlled",
        "dedupe_hint": fields.get("utility_id"),
        "text_source": "text_layer",
    }
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json=payload,
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        model=_CONTROLLED_MODEL,
        citations_verified=verified,
    )


def _create_controlled_successor(
    session: Session,
    project: Project,
    controlled_case: _ControlledCase,
    *,
    ordinal: int,
) -> Document:
    definition = controlled_case.definition
    predecessor_snapshot = controlled_case.predecessor_run.candidate_inputs_json[0]
    predecessor_fields = deepcopy(
        predecessor_snapshot["payload_json"]["fields"]
    )
    successor_fields = _controlled_successor_fields(predecessor_fields, definition)
    successor = definition["successor"]
    rows = successor.get("rows")
    if rows is None:
        rows = (
            2
            if successor["kind"] in ("ambiguous", "fan_out_exact")
            else 1
        )
    quotes = [_controlled_quote(successor_fields) for _ in range(rows)]
    page_text = "\n".join(quotes) if quotes else "controlled zero-row successor"
    controlled_case.successor = _controlled_document(
        session,
        project,
        registry_id=f"M8-{definition['case_id']}-B",
        filename=f"m8-{definition['case_id']}-b.txt",
        page_text=page_text,
        doc_type="matrix",
        doc_date=date(2026, 8, ordinal),
    )
    return controlled_case.successor


def _register_controlled_edge(
    session: Session,
    project: Project,
    index: Document,
    controlled_case: _ControlledCase,
) -> None:
    assert controlled_case.successor is not None
    attempts = session.scalar(
        select(func.count())
        .select_from(ExtractionRun)
        .where(ExtractionRun.document_id == controlled_case.successor.id)
    )
    if attempts:
        raise AcceptanceError("controlled successor extraction preceded registration")
    register_supersessions(
        session,
        (
            SupersessionDeclaration(
                predecessor_registry_id=controlled_case.predecessor.registry_id,
                successor_registry_id=controlled_case.successor.registry_id,
                replacement_date=date(2026, 8, 6),
                source_registry_id=index.registry_id,
                source_page=1,
            ),
        ),
        project_id=project.id,
    )


def _extract_controlled_successor(
    session: Session,
    project: Project,
    controlled_case: _ControlledCase,
) -> None:
    assert controlled_case.successor is not None
    definition = controlled_case.definition
    predecessor_fields = deepcopy(
        controlled_case.predecessor_run.candidate_inputs_json[0]["payload_json"][
            "fields"
        ]
    )
    successor_fields = _controlled_successor_fields(predecessor_fields, definition)
    successor = definition["successor"]
    rows = successor.get("rows")
    if rows is None:
        rows = (
            2
            if successor["kind"] in ("ambiguous", "fan_out_exact")
            else 1
        )
    citation_shape = successor.get("citation_shape", "verified_single")

    def extract(_session: Session, document: Document) -> list[Candidate]:
        if document.id != controlled_case.successor.id:
            raise AcceptanceError("controlled extraction reached the wrong document")
        return [
            _controlled_candidate(
                project,
                document,
                successor_fields,
                citation_shape=citation_shape,
            )
            for _ in range(rows)
        ]

    outcomes = extract_project(
        session,
        project,
        extract=extract,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        redo=False,
        commit=False,
    )
    outcome = next(
        item
        for item in outcomes
        if item.document_id == controlled_case.successor.id
    )
    if outcome.status != "extracted" or outcome.extraction_run_id is None:
        raise AcceptanceError(
            f"controlled extraction failed for {definition['case_id']}"
        )
    run = session.get(ExtractionRun, outcome.extraction_run_id)
    if run is None or run.candidate_count != rows:
        raise AcceptanceError("controlled completed run lost exact Candidate inputs")
    controlled_case.successor_run = run
    controlled_case.successor_candidates = tuple(
        session.scalars(
            select(Candidate)
            .where(Candidate.extraction_run_id == run.id)
            .order_by(Candidate.id)
        ).all()
    )


def _compare_controlled_case(
    session: Session,
    controlled_case: _ControlledCase,
) -> None:
    assert controlled_case.successor_run is not None
    comparison = create_revision_comparison(
        session,
        controlled_case.predecessor_run.id,
        controlled_case.successor_run.id,
    )
    readback = read_revision_comparison(session, comparison.id)
    _assert_partition(readback)
    matches = tuple(
        item
        for item in readback.findings
        if controlled_case.predecessor_candidate.id
        in item.predecessor_candidate_ids
    )
    if len(matches) != 1:
        raise AcceptanceError(
            f"controlled {controlled_case.definition['case_id']} has no exact finding"
        )
    controlled_case.comparison = comparison
    controlled_case.finding = matches[0]


def _expected_correspondence_state(value: str) -> str:
    mapping = {
        "exact_unchanged": "unchanged",
        "normalized_only_unchanged": "unchanged",
        "changed": "changed",
        "dropped": "dropped",
        "ambiguous": "ambiguous",
        "unmatched": "unmatched",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise AcceptanceError(f"unsupported expected correspondence {value!r}") from exc


def _review_for_dependency(
    session: Session,
    project_id: int,
    dependency_id: int,
):
    matches = tuple(
        item
        for item in build_reviewer_worklist(session, project_id).reviews
        if item.dependency_id == dependency_id
    )
    if len(matches) != 1:
        raise AcceptanceError(
            f"dependency {dependency_id} has {len(matches)} controlled reviews"
        )
    return matches[0]


def _lifecycle_observation(
    session: Session,
    project_id: int,
    dependency_id: int,
    *,
    expected: str,
) -> dict[str, Any]:
    review = _review_for_dependency(session, project_id, dependency_id)
    return {
        "review_status": expected,
        "observed_status": review.status,
        "route": review.route,
        "reason": review.reason,
    }


def _carry_receipt_count(session: Session, project_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(AutomaticCarryForwardReceipt)
            .where(AutomaticCarryForwardReceipt.project_id == project_id)
        )
        or 0
    )


def _dependency_fields_snapshot(dependency: Dependency) -> dict[str, Any]:
    return {
        "ref_code": dependency.ref_code,
        "source_ref": dependency.source_ref,
        "dep_type": dependency.dep_type,
        "title": dependency.title,
        "location_desc": dependency.location_desc,
        "station_from": dependency.station_from,
        "station_to": dependency.station_to,
        "external_org_id": dependency.external_org_id,
        "milestone_id": dependency.milestone_id,
        "status": dependency.status,
        "resolution_strategy": dependency.resolution_strategy,
        "committed_date": (
            dependency.committed_date.isoformat()
            if dependency.committed_date is not None
            else None
        ),
        "need_date": (
            dependency.need_date.isoformat()
            if dependency.need_date is not None
            else None
        ),
        "evidence_required": dependency.evidence_required,
        "notes": dependency.notes,
    }


def _ledger_mutation_fingerprint(
    session: Session,
    dependency_id: int,
    successor_candidate_ids: tuple[int, ...],
) -> str:
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise AcceptanceError("controlled dependency disappeared")
    candidates = [
        session.get(Candidate, candidate_id)
        for candidate_id in successor_candidate_ids
    ]
    state = {
        "dependency": _dependency_fields_snapshot(dependency),
        "successor_candidates": [
            {
                "id": item.id,
                "state": item.state,
                "merged_into": item.merged_into,
                "payload_json": deepcopy(item.payload_json),
            }
            for item in candidates
            if item is not None
        ],
        "assertions": [
            {
                "id": item.id,
                "field_name": item.field_name,
                "asserted_value": item.asserted_value,
                "evidence_link_id": item.evidence_link_id,
            }
            for item in session.scalars(
                select(Assertion)
                .where(Assertion.dependency_id == dependency_id)
                .order_by(Assertion.id)
            ).all()
        ],
        "evidence": [
            {
                "id": item.id,
                "document_id": item.document_id,
                "page_no": item.page_no,
                "quote": item.quote,
                "verified": item.verified,
                "satisfies_requirement": item.satisfies_requirement,
            }
            for item in session.scalars(
                select(EvidenceLink)
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        ],
        "operative_support": [
            {
                "id": item.id,
                "evidence_link_id": item.evidence_link_id,
                "role": item.role,
                "field_name": item.field_name,
                "designated_by": item.designated_by,
            }
            for item in session.scalars(
                select(OperativeSupport)
                .where(OperativeSupport.dependency_id == dependency_id)
                .order_by(OperativeSupport.id)
            ).all()
        ],
        "audit": [
            {
                "id": item.id,
                "actor": item.actor,
                "human_principal": item.human_principal,
                "action": item.action,
                "before_json": deepcopy(item.before_json),
                "after_json": deepcopy(item.after_json),
            }
            for item in session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == audit.DEPENDENCY,
                    AuditLog.entity_id == dependency_id,
                )
                .order_by(AuditLog.id)
            ).all()
        ],
    }
    return _json_sha256(state)


def _exercise_policy_drift(session: Session) -> dict[str, Any]:
    """Observe pause, replacement authorization, recovery, and idempotence."""

    project = Project(
        slug=f"m8-policy-drift-{uuid4().hex}",
        name="M8 controlled policy drift",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    fields = {
        "utility_id": "M8-DRIFT-001",
        "external_org": "Controlled Drift Utility",
        "utility_type": "Telecom",
        "station_from": "100+00",
        "baseline": "IH-45",
    }
    predecessor = _controlled_document(
        session,
        project,
        registry_id="M8-DRIFT-A",
        filename="m8-drift-a.txt",
        page_text=_controlled_quote(fields),
        doc_type="matrix",
        doc_date=date(2026, 1, 1),
    )
    successor = _controlled_document(
        session,
        project,
        registry_id="M8-DRIFT-B",
        filename="m8-drift-b.txt",
        page_text=_controlled_quote(fields),
        doc_type="matrix",
        doc_date=date(2026, 8, 1),
    )
    index = _controlled_document(
        session,
        project,
        registry_id="M8-DRIFT-INDEX",
        filename="m8-drift-index.txt",
        page_text="M8-DRIFT-A replaced by M8-DRIFT-B on 8/1/2026",
        doc_type="other",
        doc_date=date(2026, 8, 1),
    )
    predecessor_candidate = _controlled_candidate(
        project, predecessor, fields
    )
    predecessor_run = record_extraction_run(
        session,
        predecessor,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(predecessor_candidate,),
        model=_CONTROLLED_MODEL,
        schema_version=_CONTROLLED_PROMPT_VERSION,
    )
    declare_active_run(
            session,
            predecessor.id,
            predecessor_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
    dependency = accept_candidate(
        session,
        predecessor_candidate,
        principal=_SIMULATED_PRINCIPAL,
    )
    evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    mark_satisfies(
        session,
        dependency.id,
        evidence.id,
        principal=_SIMULATED_PRINCIPAL,
    )
    register_supersessions(
        session,
        (
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date(2026, 8, 1),
                source_registry_id=index.registry_id,
                source_page=1,
            ),
        ),
        project_id=project.id,
    )
    successor_candidate = _controlled_candidate(project, successor, fields)
    successor_run = record_extraction_run(
        session,
        successor,
        prompt_version=_CONTROLLED_PROMPT_VERSION,
        candidate_count=1,
        page_errors=0,
        candidates=(successor_candidate,),
        model=_CONTROLLED_MODEL,
        schema_version=_CONTROLLED_PROMPT_VERSION,
    )
    declare_active_run(
            session,
            successor.id,
            successor_run.id,
            principal=_SIMULATED_PRINCIPAL,
        )
    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
    )
    initial_runtime = (
        automatic_carry_forward_module.AutomaticCarryForwardRuntime.deployed()
    )
    initial_approval = authorize_automatic_carry_forward(
        session,
        project.id,
        principal=_SIMULATED_PRINCIPAL,
        _runtime=initial_runtime,
    )
    initial_policy_json = deepcopy(initial_approval.policy_json)
    initial_policy_sha256 = initial_approval.policy_sha256
    initial_rules_digest = initial_policy_json["rules_digest"]
    drifted_runtime = automatic_carry_forward_module.AutomaticCarryForwardRuntime(
        safety_sources=tuple(
            (
                module_name,
                (
                    b"m8-acceptance-policy-drift-v1"
                    if module_name == "corridor.automatic_carry_forward"
                    else source_bytes
                ),
            )
            for module_name, source_bytes in initial_runtime.safety_sources
        ),
        matcher_version=initial_runtime.matcher_version,
        matcher_config=deepcopy(initial_runtime.matcher_config),
    )
    drifted_rules_digest = drifted_runtime.rules_digest()
    if drifted_rules_digest == initial_rules_digest:
        raise AcceptanceError("controlled rules digest did not produce drift")
    before_pause = _ledger_mutation_fingerprint(
        session,
        dependency.id,
        (successor_candidate.id,),
    )

    replacement_approval = None
    paused = None
    resumed = None
    idempotent = None
    active_approval_id_during_pause = None
    active_approval_id_after_replacement = None
    after_pause = None
    paused = run_automatic_carry_forward(
        session, project.id, _runtime=drifted_runtime
    )
    after_pause = _ledger_mutation_fingerprint(
        session,
        dependency.id,
        (successor_candidate.id,),
    )
    active_during_pause = session.get(
        ActiveAutomaticCarryForwardPolicy,
        project.id,
    )
    active_approval_id_during_pause = (
        active_during_pause.policy_approval_id
        if active_during_pause is not None
        else None
    )
    replacement_approval = authorize_automatic_carry_forward(
        session,
        project.id,
        principal=_SIMULATED_PRINCIPAL,
        _runtime=drifted_runtime,
    )
    active_after_replacement = session.get(
        ActiveAutomaticCarryForwardPolicy,
        project.id,
        populate_existing=True,
    )
    active_approval_id_after_replacement = (
        active_after_replacement.policy_approval_id
        if active_after_replacement is not None
        else None
    )
    resumed = run_automatic_carry_forward(
        session, project.id, _runtime=drifted_runtime
    )
    idempotent = run_automatic_carry_forward(
        session, project.id, _runtime=drifted_runtime
    )

    assert paused is not None
    assert replacement_approval is not None
    assert resumed is not None
    assert idempotent is not None
    preserved = session.get(
        AutomaticCarryForwardPolicyApproval,
        initial_approval.id,
        populate_existing=True,
    )
    authorization_count = int(
        session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_type == audit.PROJECT,
                AuditLog.entity_id == project.id,
                AuditLog.action == audit.AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
            )
        )
        or 0
    )
    comparison_count = int(
        session.scalar(
            select(func.count())
            .select_from(RevisionComparisonRun)
            .where(RevisionComparisonRun.project_id == project.id)
        )
        or 0
    )
    return {
        "outcome": "paused_then_reauthorized_and_resumed",
        "initial_rules_digest": initial_rules_digest,
        "drifted_rules_digest": drifted_rules_digest,
        "initial_policy_sha256": initial_policy_sha256,
        "replacement_policy_sha256": replacement_approval.policy_sha256,
        "approval_replaced": replacement_approval.id != initial_approval.id,
        "initial_approval_preserved": bool(
            preserved is not None
            and preserved.policy_json == initial_policy_json
            and preserved.policy_sha256 == initial_policy_sha256
        ),
        "pause": {
            "carried": len(paused.carried),
            "reasons": sorted({item.reason for item in paused.abstentions}),
            "ledger_unchanged": before_pause == after_pause,
            "active_remained_initial": (
                active_approval_id_during_pause == initial_approval.id
            ),
        },
        "replacement": {
            "rules_digest": replacement_approval.policy_json.get("rules_digest"),
            "active_pointer_replaced": (
                active_approval_id_after_replacement == replacement_approval.id
            ),
            "authorization_count": authorization_count,
        },
        "resume": {
            "carried": len(resumed.carried),
            "receipt_bound_to_replacement": all(
                item.policy_approval_id == replacement_approval.id
                for item in resumed.carried
            ),
            "idempotent_second_carried": len(idempotent.carried),
            "comparison_count": comparison_count,
            "comparison_preserved": all(
                item.comparison_id == comparison.id for item in resumed.carried
            ),
        },
    }


def _acceptance_carry_runtime():
    return automatic_carry_forward_module.AutomaticCarryForwardRuntime.deployed()


def _skipped_controlled_lane(*, reason: str) -> dict[str, Any]:
    return {
        "claim_boundary": CLAIM_BOUNDARY,
        "status": "not_run",
        "reason": reason,
        "cases": [],
        "before_policy": {
            "eligible_route": None,
            "automatic_writes": 0,
        },
        "passes": {
            "first": {"carried": 0},
            "second": {"carried": 0},
            "receipt_count": 0,
        },
        "policy": {
            "policy_sha256": None,
            "authorization_count": 0,
            "drift_outcome": None,
        },
    }


def _controlled_partial_exports(
    session: Session,
    *,
    project: Project,
    cases: Sequence[_ControlledCase],
    lifecycle: Sequence[dict[str, Any]],
    scope_fail_closed: dict[str, bool],
    superseded_citation_observed: bool,
    failed_attempt: dict[str, Any] | None,
    before_policy: dict[str, Any],
    passes: dict[str, Any],
    cases_export: Sequence[dict[str, Any]],
    failure: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    documents, runs, active_run_declarations = _controlled_lineage_exports(
        session, project.id
    )
    raw = {
        "project_id": project.id,
        "claim_boundary": CLAIM_BOUNDARY,
        "status": "failed",
        "failure": deepcopy(failure),
        "lifecycle": list(lifecycle),
        "scope_fail_closed": deepcopy(scope_fail_closed),
        "superseded_citation_observed": superseded_citation_observed,
        "failed_attempt": deepcopy(failed_attempt),
        "before_policy": deepcopy(before_policy),
        "passes": deepcopy(passes),
        "policy": {
            "policy_sha256": None,
            "authorization_count": int(
                session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.entity_type == audit.PROJECT,
                        AuditLog.entity_id == project.id,
                        AuditLog.action
                        == audit.AUTHORIZE_AUTOMATIC_CARRY_FORWARD,
                    )
                )
                or 0
            ),
            "drift_outcome": None,
        },
        "cases": list(cases_export)
        or [
            _partial_controlled_case_export(controlled_case)
            for controlled_case in cases
        ],
        "documents": documents,
        "runs": runs,
        "active_run_declarations": active_run_declarations,
        "comparisons": [],
        "ledger_counts": _ledger_counts(session, project.id),
    }
    canonical = {
        "claim_boundary": CLAIM_BOUNDARY,
        "status": "failed",
        "failure": deepcopy(failure),
        "cases": [
            {
                "case_id": item["case_id"],
                "expected_correspondence": item.get("expected_correspondence"),
                "observed_correspondence": item.get("observed_correspondence"),
                "outcome": item.get("outcome"),
                "abstention_reason": item.get("abstention_reason"),
            }
            for item in raw["cases"]
        ],
        "documents": documents,
        "runs": runs,
        "active_run_declarations": active_run_declarations,
        "ledger_counts": deepcopy(raw["ledger_counts"]),
    }
    return raw, canonical


def _partial_controlled_case_export(controlled_case: _ControlledCase) -> dict[str, Any]:
    observed = (
        controlled_case.finding.state
        if controlled_case.finding is not None
        else None
    )
    expected = _expected_correspondence_state(
        controlled_case.definition["expected_correspondence"]
    )
    return {
        "case_id": controlled_case.definition["case_id"],
        "expected_correspondence": expected,
        "observed_correspondence": observed,
        "outcome": None,
        "abstention_reason": None,
        "inherited_scopes": [],
        "ready_after": False,
        "dependency_fields_changed": False,
        "ledger_unchanged": True,
        "successor_candidate_state": None,
        "successor_candidate_ids": [
            item.id for item in controlled_case.successor_candidates
        ],
        "dependency_id": controlled_case.dependency.id,
        "comparison_id": (
            controlled_case.comparison.id
            if controlled_case.comparison is not None
            else None
        ),
        "finding_id": (
            controlled_case.finding.id if controlled_case.finding is not None else None
        ),
        "unresolved_after": True,
    }
