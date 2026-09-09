"""Capture and replay original legacy history without changing record authority.

Stage 2 needs old authors, times, decision types and sources, including rows
superseded before migration. The compatibility reader returns exact stored rows
and typed decision chains from a verified immutable batch. It never substitutes
the executor for an original actor or reconstructs an unknown time by guessing.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor.legacy_history_inventory import HISTORY_CLASSES


@dataclass(frozen=True)
class HistoryInventory:
    project_id: int
    content_sha256: str
    counts: dict[str, int]
    classes: dict[str, Any]


@dataclass(frozen=True)
class HistoryBatch:
    id: int
    project_id: int
    executor: str
    code_revision: str
    captured_at: datetime
    content_sha256: str
    counts: dict[str, int]
    classes: dict[str, Any]
    reversed: bool


class HistoryRefused(ValueError):
    """History or the requested reading cannot be proven from retained rows."""


def inventory_history(session: Session, project_id: int) -> HistoryInventory:
    row = session.execute(text("""
      select content, encode(sha256(convert_to(content::text,'UTF8')),'hex') as digest
      from (select legacy_history_content(:project) as content) inventory
    """), {"project": project_id}).one()
    classes = row.content["classes"]
    return HistoryInventory(project_id, row.digest,
                            {key: len(value["rows"]) for key, value in classes.items()}, classes)


def capture_history(session: Session, inventory: HistoryInventory, *, run_key: str,
                    executor: str, code_revision: str) -> HistoryBatch:
    """Seal reviewed rows; caller owns a repeatable-read transaction and commit."""
    batch_id = session.scalar(text("select capture_legacy_history(:project,:key,:executor,:code,:digest)"),
        {"project": inventory.project_id, "key": run_key, "executor": executor,
         "code": code_revision, "digest": inventory.content_sha256})
    return read_history(session, inventory.project_id, batch_id)


def read_history(session: Session, project_id: int, batch_id: int) -> HistoryBatch:
    """Verify the complete retained history before exposing any class."""
    row = session.execute(text("""
      select b.*, encode(sha256(convert_to(b.payload::text,'UTF8')),'hex') as actual_digest,
       exists(select 1 from legacy_history_reversals r where r.batch_id=b.id) as reversed
      from legacy_history_batches b where b.id=:batch and b.project_id=:project
    """), {"project": project_id, "batch": batch_id}).mappings().one_or_none()
    if row is None:
        raise HistoryRefused("history batch does not belong to this project")
    classes = row["payload"]["classes"]
    if (row["actual_digest"] != row["content_sha256"]
        or row["payload"].get("project_id") != project_id
        or row["payload"].get("version") != "legacy-history-v1"
        or set(classes) != {entry.table for entry in HISTORY_CLASSES}
        or row["counts"] != {key: len(value["rows"]) for key, value in classes.items()}):
        raise HistoryRefused("history content, class coverage or counts do not match the receipt")
    return HistoryBatch(row["id"], project_id, row["executor"], row["code_revision"],
                        row["captured_at"], row["content_sha256"], row["counts"], classes, row["reversed"])


def reverse_history(session: Session, batch: HistoryBatch, *, actor: str, reason: str) -> HistoryBatch:
    """Withdraw this batch from routing without erasing a single historical row."""
    session.execute(text("select reverse_legacy_history(:project,:batch,:actor,:reason)"),
                    {"project": batch.project_id, "batch": batch.id, "actor": actor, "reason": reason})
    return read_history(session, batch.project_id, batch.id)


def history_rows(batch: HistoryBatch, table: str) -> tuple[dict[str, Any], ...]:
    """Read original row identities, actors, event times and source links exactly."""
    if table not in batch.classes:
        raise HistoryRefused("history class is outside the declared inventory")
    return tuple(deepcopy(batch.classes[table]["rows"]))


def coordination_decisions_as_of(batch: HistoryBatch, *, at: datetime) -> tuple[dict[str, Any], ...]:
    """Project original coordination chains at an observed instant.

    The original recorded_at axis governs effectiveness; the migration time and
    the executor have no semantic role. This projects typed Work Decisions,
    including compensation; it makes no claim about mutable Dependency fields.
    """
    if at.tzinfo is None or at > batch.captured_at:
        raise HistoryRefused("as-of time must be timezone-aware and no later than capture")
    eligible = {row["id"]: row for row in history_rows(batch, "work_decisions")
                if datetime.fromisoformat(row["recorded_at"]) <= at}
    superseded = {row["predecessor_decision_id"] for row in eligible.values()}
    receipts = {row["id"]: row for row in history_rows(batch, "statement_coordination_receipts")}
    undone = set()
    for reversal in _observed(batch, "statement_coordination_reversals", at):
        receipt = receipts.get(reversal.get("receipt_id"))
        if receipt:
            undone.update(receipt.get(field) for field in (
                "internal_owner_decision_id", "next_action_decision_id", "milestone_impact_decision_id"))
    return tuple(row for key, row in sorted(eligible.items()) if key not in superseded | undone)


def _observed(batch: HistoryBatch, table: str, at: datetime, time_field="created_at"):
    if at.tzinfo is None or at > batch.captured_at:
        raise HistoryRefused("as-of time must be timezone-aware and no later than capture")
    return tuple(row for row in history_rows(batch, table)
                 if datetime.fromisoformat(row[time_field]) <= at)


def statement_dispositions_as_of(batch: HistoryBatch, *, at: datetime) -> tuple[dict[str, Any], ...]:
    """Do Not Add and its explicit restoration retain their original authors."""
    receipts = {row["id"]: row for row in history_rows(batch, "statement_coordination_receipts")}
    undone = set()
    for reversal in _observed(batch, "statement_coordination_reversals", at):
        undone.add(reversal.get("candidate_disposition_id"))
        receipt = receipts.get(reversal.get("receipt_id"))
        if receipt:
            undone.add(receipt.get("candidate_disposition_id"))
    return tuple(row for row in _observed(batch, "candidate_dispositions", at) if row["id"] not in undone)


def statements_as_of(batch: HistoryBatch, *, at: datetime) -> tuple[dict[str, Any], ...]:
    """Read corrected statement, precision, scope and cited/verbal origin lineage."""
    events = _observed(batch, "dependency_events", at)
    superseded = {row.get("supersedes_event_id") for row in events}
    receipts = {row["id"]: row for row in history_rows(batch, "statement_coordination_receipts")}
    undone = {receipts[row["receipt_id"]].get("dependency_event_id")
              for row in _observed(batch, "statement_coordination_reversals", at)
              if row.get("receipt_id") in receipts}
    scopes = _observed(batch, "dependency_event_scope_decisions", at)
    superseded_scope = {row.get("supersedes_scope_decision_id") for row in scopes}
    result = []
    for event in events:
        if event["id"] in superseded | undone:
            continue
        current_scopes = tuple(row for row in scopes if row["event_id"] == event["id"] and row["id"] not in superseded_scope)
        if len(current_scopes) > 1:
            raise HistoryRefused("historical statement has ambiguous scope decisions")
        scope_ids = {row["id"] for row in current_scopes}
        result.append({
            "statement": event,
            "timings": tuple(row for row in history_rows(batch, "dependency_event_timings") if row["event_id"] == event["id"]),
            "scope_decisions": current_scopes,
            "scope_members": tuple(row for row in history_rows(batch, "dependency_event_scopes") if row["scope_decision_id"] in scope_ids),
            "evidence": tuple(row for row in history_rows(batch, "dependency_event_evidence") if row["event_id"] == event["id"]),
            "verbal_origins": tuple(row for row in history_rows(batch, "recorded_verbal_origin_statements") if row["statement_id"] == event["id"]),
        })
    return tuple(result)


def discrepancy_resolutions_as_of(batch: HistoryBatch, *, at: datetime) -> tuple[dict[str, Any], ...]:
    """A later assertion reopens the exact field beyond a settlement's coverage."""
    newest_claim = {}
    for claim in _observed(batch, "assertions", at):
        key = (claim["dependency_id"], claim["field_name"])
        newest_claim[key] = max(newest_claim.get(key, 0), claim["id"])
    latest = {}
    settlements = sorted(_observed(batch, "dispute_settlements", at, "settled_at"), key=lambda row: (row["settled_at"], row["id"]))
    for settlement in settlements:
        latest[(settlement["dependency_id"], settlement["field_name"])] = settlement
    return tuple({"settlement": row, "covered": newest_claim.get(key, 0) <= row["covers_assertion_id"]}
                 for key, row in sorted(latest.items()))


def record_decisions_as_of(batch: HistoryBatch, *, revision_id: int) -> tuple[dict[str, Any], ...]:
    """Rebuild captured native decisions across corrections and suppressions.

    Original revision identities, original recorded_at and human/policy
    authority are retained. Source identity lives on the Fact's own sources.
    No Candidate or current Dependency value participates in this reading.
    """
    revisions = {row["id"]: row for row in history_rows(batch, "project_record_revisions")}
    if revision_id not in revisions:
        raise HistoryRefused("revision does not belong to the retained project history")
    facts = {row["id"]: row for row in history_rows(batch, "facts")}
    eligible = {row["id"]: row for row in history_rows(batch, "fact_decisions") if row["revision_id"] <= revision_id}
    effective = [row for row in eligible.values() if row["superseded_by"] not in eligible]
    suppressed = {row["subject_key"] for row in effective if row["fact_type"] == "statement_wording" and row["disposition"] == "do_not_add"}
    result = []
    for decision in sorted(effective, key=lambda row: row["id"]):
        if decision["disposition"] != "include" or decision["subject_key"] in suppressed:
            continue
        fact = facts.get(decision["fact_id"])
        revision = revisions.get(decision["revision_id"])
        if fact is None or revision is None:
            raise HistoryRefused("retained decision lost its Fact or revision")
        result.append({"decision": decision, "fact": fact, "revision": revision,
                       "sources": tuple(row for row in history_rows(batch, "fact_sources") if row["fact_id"] == fact["id"])})
    return tuple(result)


def support_designations_at_capture(batch: HistoryBatch) -> tuple[dict[str, Any], ...]:
    """Return retained current designations with exact original support roles.

    Earlier operative rows were mutable. They cannot be projected to arbitrary
    historical instants without a retained audit/transfer receipt; this API
    intentionally names its supported instant instead of inventing history.
    """
    evidence = {row["id"]: row for row in history_rows(batch, "evidence_links")}
    result = []
    for support in history_rows(batch, "operative_support"):
        link = evidence.get(support["evidence_link_id"])
        if link is None:
            raise HistoryRefused("support designation lost its original Evidence Link")
        result.append({"designation": support, "evidence": link})
    return tuple(result)


def backfill_evidence_sources(session: Session, batch: HistoryBatch) -> tuple[dict[str, Any], ...]:
    """Migrate only unique exact located passages; preserve all original quotes."""
    session.execute(text("select backfill_legacy_evidence_sources(:project,:batch)"),
                    {"project": batch.project_id, "batch": batch.id})
    return tuple(dict(row) for row in session.execute(text("""
        select legacy_evidence_link_id,evidence_link_source_id,source_segment_id,
               original_quote_sha256,outcome,reason
        from legacy_history_evidence_migrations where batch_id=:batch and project_id=:project
        order by legacy_evidence_link_id
    """), {"project": batch.project_id, "batch": batch.id}).mappings())
