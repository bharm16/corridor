"""Derive activation evidence from retained provider observations (#514/#535).

A synthetic success flag or an archive filename cannot prove AWS disposition.
This read-only adapter requires an executed provider-bound plan, its exact final
whole-environment receipt, and completed recovery phases for one immutable
rehearsal specification. It produces a gate payload; it never activates a route.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import re

from corridor.disposition_contracts import DispositionRefused, json_digest as digest


_REQUIRED_PHASES = ("restore", "state_verification", "cleanup", "backup_expiration")


def disposition_gate_payload(control_plane, *, plan_id, operation_id, rehearsal_operation_id,
                             configuration_identity, inventory_digest):
    plan = control_plane.disposition_plan(plan_id)
    if plan is None or plan.status != "executed" or plan.provider_resources_sha256 != inventory_digest:
        raise DispositionRefused("disposition evidence requires an executed plan for the configured inventory")
    if not plan.provider_resources or digest(plan.provider_resources) != inventory_digest or not plan.provider_resources.get("whole_environment"):
        raise DispositionRefused("disposition evidence requires the persisted whole-environment provider inventory")
    expected = f"aws:{plan.environment_id}/whole-environment-absent/{inventory_digest}/{plan.manifest_sha256}"
    finals = [row for row in control_plane.destruction_receipts(plan.environment_id)
              if row.operation_id == operation_id and row.component == "environment" and row.outcome == "completed"]
    if len(finals) != 1 or finals[0].evidence_ref != expected:
        raise DispositionRefused("final disposition receipt does not bind this exact AWS plan and inventory")
    rehearsal = control_plane.disposition_rehearsal_receipts(plan.environment_id, rehearsal_operation_id)
    selected = []
    for phase in _REQUIRED_PHASES:
        rows = [row for row in rehearsal if row["phase"] == phase]
        if not rows:
            raise DispositionRefused("rehearsal evidence is missing a required provider phase")
        latest = max(row["observed_at"] for row in rows)
        observations = [row for row in rows if row["observed_at"] == latest]
        if any(row["outcome"] != "completed" for row in observations):
            raise DispositionRefused("latest rehearsal phase observation is not completed")
        selected.extend(observations)
    specifications = {row["evidence"].get("spec_sha256") for row in selected}
    if len(specifications) != 1 or not re.fullmatch("[0-9a-f]{64}", next(iter(specifications)) or ""):
        raise DispositionRefused("rehearsal phases do not share one pinned specification")
    if not configuration_identity:
        raise DispositionRefused("activation evidence requires the exact configuration identity")
    def normalize(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise DispositionRefused("provider observations need explicit timezones")
            return value.isoformat()
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value
    normalized = sorted((normalize(row) for row in selected), key=lambda row: (row["phase"], row["observed_at"], row["receipt_id"]))
    # Regenerating an artifact must not freshen an old provider proof. The
    # earliest required observation supplies the conservative freshness anchor.
    observed_at = min([finals[0].observed_at, *(row["observed_at"] for row in selected)])
    return {"gate": "disposition", "outcome": "passed", "configuration": configuration_identity,
            "observed_at": normalize(observed_at), "inventory_digest": inventory_digest,
            "external_receipt_reference": f"control-plane:destruction/{finals[0].receipt_id}",
            "rehearsal_receipt_sha256": digest(normalized),
            "final_receipt_sha256": digest(normalize(asdict(finals[0])))}
