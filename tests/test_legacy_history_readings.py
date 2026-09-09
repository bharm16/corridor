"""Historical compatibility readers observe corrections and restoration time."""

from datetime import datetime, timezone

from corridor.legacy_history import (
    HistoryBatch, discrepancy_resolutions_as_of, statement_dispositions_as_of,
    statements_as_of, record_decisions_as_of,
)
from corridor.legacy_history_inventory import HISTORY_CLASSES


def _batch(**rows):
    classes = {entry.table: {"rows": rows.get(entry.table, [])} for entry in HISTORY_CLASSES}
    return HistoryBatch(1, 1, "migration:executor", "a" * 40,
        datetime(2026, 9, 1, tzinfo=timezone.utc), "d" * 64,
        {key: len(value["rows"]) for key, value in classes.items()}, classes, False)


def _at(month):
    return datetime(2026, month, 15, tzinfo=timezone.utc)


def test_do_not_add_and_restore_keep_the_original_decision_and_do_not_include():
    batch = _batch(candidate_dispositions=[{
        "id": 10, "candidate_id": 1, "disposition": "not_relevant", "recorded_by": "local:original",
        "created_at": "2026-01-01T00:00:00+00:00",
    }], statement_coordination_reversals=[{
        "id": 11, "receipt_id": None, "candidate_disposition_id": 10,
        "recorded_by": "local:restorer", "created_at": "2026-02-01T00:00:00+00:00",
    }])
    assert statement_dispositions_as_of(batch, at=_at(1))[0]["recorded_by"] == "local:original"
    assert statement_dispositions_as_of(batch, at=_at(2)) == ()


def test_correction_changes_statement_only_after_its_original_recorded_time():
    batch = _batch(dependency_events=[
        {"id": 1, "supersedes_event_id": None, "description": "March", "created_by": "local:recorder", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": 2, "supersedes_event_id": 1, "description": "April", "created_by": "local:corrector", "created_at": "2026-02-01T00:00:00+00:00"},
    ], dependency_event_timings=[{"id": 1, "event_id": 1, "precision": "month", "text": "March"}],
       recorded_verbal_origin_statements=[{"origin_id": 7, "statement_id": 1}])
    reading = statements_as_of(batch, at=_at(1))
    assert reading[0]["statement"]["description"] == "March"
    assert reading[0]["timings"][0]["precision"] == "month"
    assert reading[0]["verbal_origins"][0]["origin_id"] == 7
    assert statements_as_of(batch, at=_at(2))[0]["statement"]["created_by"] == "local:corrector"


def test_settled_null_is_preserved_and_newer_claim_reopens_the_discrepancy():
    batch = _batch(dispute_settlements=[{
        "id": 3, "dependency_id": 1, "field_name": "title", "settled_value": None,
        "settled_by": "local:settler", "covers_assertion_id": 4, "settled_at": "2026-01-02T00:00:00+00:00",
    }], assertions=[
        {"id": 4, "dependency_id": 1, "field_name": "title", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": 5, "dependency_id": 1, "field_name": "title", "created_at": "2026-02-01T00:00:00+00:00"},
    ])
    original = discrepancy_resolutions_as_of(batch, at=_at(1))[0]
    assert original["covered"] and original["settlement"]["settled_value"] is None
    assert not discrepancy_resolutions_as_of(batch, at=_at(2))[0]["covered"]


def test_native_revision_projection_preserves_authority_across_a_correction():
    batch = _batch(project_record_revisions=[
        {"id": 1, "human_principal": "local:first", "released_policy": None},
        {"id": 2, "human_principal": "local:second", "released_policy": None},
    ], facts=[{"id": 10, "text_value": "10+00"}, {"id": 11, "text_value": "20+00"}],
    fact_decisions=[
        {"id": 21, "fact_id": 10, "revision_id": 1, "superseded_by": 22, "subject_key": "row:1", "fact_type": "station_from", "disposition": "include"},
        {"id": 22, "fact_id": 11, "revision_id": 2, "superseded_by": None, "subject_key": "row:1", "fact_type": "station_from", "disposition": "include"},
    ], fact_sources=[{"id": 31, "fact_id": 10, "source_segment_id": 41}, {"id": 32, "fact_id": 11, "source_segment_id": 42}])
    old = record_decisions_as_of(batch, revision_id=1)[0]
    assert old["fact"]["text_value"] == "10+00"
    assert old["revision"]["human_principal"] == "local:first"
    assert old["sources"][0]["source_segment_id"] == 41
    assert record_decisions_as_of(batch, revision_id=2)[0]["fact"]["text_value"] == "20+00"


def test_support_history_preserves_policy_authority_and_refuses_missing_approval():
    from corridor.legacy_history import support_designations_at_capture

    values = dict(
        operative_support=[{"id": 1, "dependency_id": 9, "evidence_link_id": 4,
                            "designated_by": "corridor:automatic-carry-forward", "designated_at": "2026-02-01T00:00:00+00:00", "role": "publication", "field_name": None}],
        evidence_links=[{"id": 4, "document_id": 6, "quote": "Original supporting words."}],
        automatic_carry_forward_receipts=[{"audit_log_id": 7, "dependency_id": 9, "new_evidence_link_id": 4,
            "policy_approval_id": 8, "family": "automatic-carry-forward", "policy_version": "support-v1", "policy_sha256": "a" * 64,
            "after_json": {"moved_scopes": [{"role": "publication", "field_name": None, "to_evidence_link_id": 4}]}}],
        audit_log=[{"id": 7, "actor": "corridor:automatic-carry-forward", "action": "automatic_carry_forward",
                    "entity_type": "dependency", "entity_id": 9, "ts": "2026-02-01T00:00:00+00:00"}],
        policy_approvals=[{"id": 8, "family": "automatic-carry-forward", "policy_version": "support-v1",
                           "policy_sha256": "a" * 64, "approved_by": "local:approver"}],
    )
    reading = support_designations_at_capture(_batch(**values))[0]
    assert reading["authority"]["kind"] == "released_policy"
    assert reading["authority"]["original_actor"] == "corridor:automatic-carry-forward"
    assert reading["authority"]["approval"]["approved_by"] == "local:approver"
    missing = support_designations_at_capture(_batch(**{**values, "policy_approvals": []}))[0]
    assert missing["authority"]["kind"] == "unknown"
    assert missing["evidence"]["quote"] == "Original supporting words."
    wrong_role = {**values["operative_support"][0], "role": "readiness"}
    mismatched = support_designations_at_capture(_batch(**{**values, "operative_support": [wrong_role]}))[0]
    assert mismatched["authority"]["kind"] == "unknown"
