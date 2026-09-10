"""Each event family's constructor writes exactly what its emitter wrote by hand.

The expected dicts below are the hand-built payloads and metric labels copied
from the emitters before they moved onto the constructors; key order is part of
the pin because the logging emitter serializes the payload as it stands.
"""

from datetime import datetime, timezone

import pytest

from corridor.analytics import (
    EventFamily,
    InvalidEventError,
    child_decision_event,
    coverage_confirmation_event,
    coverage_reading_event,
    default_binding,
    delta_supersession_event,
    evidence_opening_event,
    follow_up_plan_creation_event,
    follow_up_reading_event,
    packet_opening_event,
    packet_save_event,
    packet_surfacing_event,
    portfolio_reading_event,
    preparation_attempt_finished_event,
    preparation_attempt_started_event,
    preparation_request_event,
    project_opening_event,
    project_selection_event,
    proposed_delta_creation_event,
    release_authorization_event,
    release_candidate_preparation_event,
)

BINDING = default_binding(code_revision="git:fixture")
AT = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)


def pin(event, family, payload, labels):
    assert event.family is family
    assert event.binding == BINDING
    assert event.occurred_at == AT
    assert event.payload == payload and list(event.payload) == list(payload)
    assert event.metric_labels == labels and list(event.metric_labels) == list(labels)


@pytest.mark.parametrize("reason,label", [(None, "none"), ("stale_revision", "stale_revision")])
def test_child_decision_matches_delta_resolution_emitter(reason, label):
    event = child_decision_event(
        BINDING, occurred_at=AT, project_id=7, delta_id=31, action="apply", effect_kind="field_change",
        outcome="saved", refusal_reason=reason, revision_id=12, packet_owned=True, support_assessment_count=2,
    )
    pin(event, EventFamily.CHILD_DECISION, {
        "project_id": 7,
        "delta_id": 31,
        "action": "apply",
        "effect_kind": "field_change",
        "outcome": "saved",
        "refusal_reason": reason,
        "revision_id": 12,
        "packet_owned": True,
        "support_assessment_count": 2,
    }, {"action": "apply", "outcome": "saved", "refusal_reason": label})


@pytest.mark.parametrize("revision_id,wrote", [(12, "true"), (None, "false")])
def test_packet_save_matches_review_packets_emitter(revision_id, wrote):
    counts = {"apply": 1, "keep_current": 0, "edit_and_apply": 0, "needs_coordination": 1, "defer": 0}
    event = packet_save_event(
        BINDING, occurred_at=AT, project_id=7, receipt_id=4, revision_id=revision_id,
        grouping_rule_version="delta-partition-v2", grouping_key_kind="source_revision",
        grouping_key="2026-09", observed_accepted_revision_id=11, child_count=2,
        outcome_counts=counts, outcome="saved", refusal_reason=None,
    )
    pin(event, EventFamily.PACKET_SAVE, {
        "project_id": 7,
        "receipt_id": 4,
        "revision_id": revision_id,
        "grouping_rule_version": "delta-partition-v2",
        "grouping_key_kind": "source_revision",
        "grouping_key": "2026-09",
        "observed_accepted_revision_id": 11,
        "child_count": 2,
        "outcome_counts": counts,
        "outcome": "saved",
        "refusal_reason": None,
        "wrote_revision": revision_id is not None,
    }, {
        "grouping_key_kind": "source_revision",
        "outcome": "saved",
        "refusal_reason": "none",
        "wrote_revision": wrote,
    })


def test_follow_up_plan_creation_matches_review_packets_emitter():
    event = follow_up_plan_creation_event(
        BINDING, occurred_at=AT, project_id=7, delta_id=31, follow_up_plan_id=9, revision_id=12,
        grouping_rule_version="delta-partition-v2", has_return_date=True, responsible_kind="principal",
        evidence_count=3,
    )
    pin(event, EventFamily.FOLLOW_UP_PLAN_CREATION, {
        "project_id": 7,
        "delta_id": 31,
        "follow_up_plan_id": 9,
        "revision_id": 12,
        "grouping_rule_version": "delta-partition-v2",
        "has_return_date": True,
        "responsible_kind": "principal",
        "evidence_count": 3,
    }, {"responsible_kind": "principal", "has_return_date": "true"})


def test_proposed_delta_creation_matches_proposed_deltas_emitter():
    event = proposed_delta_creation_event(
        BINDING, occurred_at=AT, project_id=7, source_family="matrix", source_revision="2026-09",
        document_id=5, delta_id=31, outcome="created", complete_enumerative_source=True,
        row_accounting_sealed=False,
    )
    pin(event, EventFamily.PROPOSED_DELTA_CREATION, {
        "project_id": 7,
        "source_family": "matrix",
        "source_revision": "2026-09",
        "document_id": 5,
        "delta_id": 31, "delta_count": 1, "delta_ids": [31],
        "outcome": "created",
        "complete_enumerative_source": True,
        "row_accounting_sealed": False,
    }, {})


ITEM = {
    "project_id": 7,
    "item_key": "packet-a",
    "grouping_key_kind": "source_revision",
    "grouping_key": "2026-09",
    "grouping_rule_version": "delta-partition-v2",
    "band": "changes_to_review",
    "attention_reasons": ["stale_support"],
    "held_out_reason": None,
    "child_count": 2,
    "ready_count": 1,
    "held_out_count": 1,
    "unchanged_count": 0,
    "customer_artifacts": ["updated_ucm"],
    "artifact_rule_version": "artifact-v1",
    "observed_accepted_revision_id": 11,
    "cutoff": AT.isoformat(),
    "issue_profile_id": 3,
    "issue_profile_identity": "fixture-issue",
    "issue_profile_version": 1,
    "issue_profile_sha256": "a" * 64,
    "issue_profile_problems": [],
    "consequence_rule_version": "consequence-v1",
    "consequence_level": "must_handle_before_issue",
    "child_consequences": [{"delta_id": 31, "level": "MUST_HANDLE", "reasons": ["material"]}],
}
ITEM_LABELS = {
    "grouping_key_kind": "source_revision",
    "band": "changes_to_review",
    "held_out_reason": "none",
    "consequence_level": "must_handle_before_issue",
}


@pytest.mark.parametrize("construct,family", [
    (packet_surfacing_event, EventFamily.PACKET_SURFACING),
    (packet_opening_event, EventFamily.PACKET_OPENING),
])
def test_packet_item_families_match_packet_review_emitters(construct, family):
    event = construct(BINDING, occurred_at=AT, principal_subject="local:coordinator", **ITEM)
    pin(event, family, {**ITEM, "principal_subject": "local:coordinator"}, ITEM_LABELS)
    absent = construct(BINDING, occurred_at=AT, principal_subject=None,
                       **{**ITEM, "held_out_reason": "awaiting_support", "consequence_level": None})
    assert absent.metric_labels == {**ITEM_LABELS, "held_out_reason": "awaiting_support",
                                    "consequence_level": "none"}
    with pytest.raises(InvalidEventError, match="declared item keys"):
        construct(BINDING, occurred_at=AT, principal_subject=None, **{**ITEM, "unexpected": 1})
    with pytest.raises(InvalidEventError, match="declared item keys"):
        construct(BINDING, occurred_at=AT, principal_subject=None,
                  **{k: v for k, v in ITEM.items() if k != "band"})


def test_release_candidate_preparation_matches_release_candidate_emitter():
    event = release_candidate_preparation_event(
        BINDING, occurred_at=AT, surface="worker", outcome="ready", principal_subject="local:coordinator",
        project_id=7, source_cutoff=AT.isoformat(), accepted_revision_id=11, previous_package_id=2,
        issue_profile_id=3, issue_profile_version=1, coverage_identity="coverage:abc",
        configured_artifact_types=["updated_ucm", "weekly_report"], candidate_identity="candidate:1",
        content_sha256="b" * 64, readiness="ready", blocker_count=0, exception_count=1, refusal_code=None,
    )
    pin(event, EventFamily.RELEASE_CANDIDATE_PREPARATION, {
        "principal_subject": "local:coordinator",
        "project_id": 7,
        "source_cutoff": AT.isoformat(),
        "accepted_revision_id": 11,
        "previous_package_id": 2,
        "issue_profile_id": 3,
        "issue_profile_version": 1,
        "coverage_identity": "coverage:abc",
        "configured_artifact_types": ["updated_ucm", "weekly_report"],
        "candidate_identity": "candidate:1",
        "content_sha256": "b" * 64,
        "readiness": "ready",
        "blocker_count": 0,
        "exception_count": 1,
        "refusal_code": None,
    }, {"surface": "worker", "status": "ready"})


def test_release_authorization_matches_release_authorization_emitter():
    event = release_authorization_event(
        BINDING, occurred_at=AT, surface="issue_screen", status="authorized",
        principal_subject="local:coordinator", project_id=7, candidate_id=5, candidate_identity="candidate:1",
        accepted_revision_id=11, issue_profile_version=1, source_cutoff=AT.isoformat(),
        package_identity="package:1", issue_number=4, refusal_code=None,
    )
    pin(event, EventFamily.RELEASE_AUTHORIZATION, {
        "principal_subject": "local:coordinator",
        "project_id": 7,
        "candidate_id": 5,
        "candidate_identity": "candidate:1",
        "accepted_revision_id": 11,
        "issue_profile_version": 1,
        "source_cutoff": AT.isoformat(),
        "package_identity": "package:1",
        "issue_number": 4,
        "refusal_code": None,
    }, {"surface": "issue_screen", "status": "authorized"})


def test_portfolio_reading_matches_project_portfolio_emitter():
    projects = [{
        "project_id": 7, "state": "changes_to_review", "landing": "/work/p", "changes_to_review": 2,
        "follow_up_waiting": 0, "follow_up_overdue": 0, "follow_up_due": 0, "readiness_problems": 0,
        "preparing": False, "measurement_context": {"issue_profile_identity": "fixture-issue"},
    }]
    event = portfolio_reading_event(
        BINDING, occurred_at=AT, principal_subject="local:coordinator", cutoff=AT.isoformat(), projects=projects,
    )
    pin(event, EventFamily.PORTFOLIO_READING, {
        "principal_subject": "local:coordinator",
        "cutoff": AT.isoformat(),
        "project_count": 1,
        "projects": projects,
    }, {"surface": "portfolio", "status": "presented"})


@pytest.mark.parametrize("context", [None, {"issue_profile_identity": "fixture-issue", "template_identity": "t:1"}])
def test_project_selection_matches_project_portfolio_emitter(context):
    event = project_selection_event(
        BINDING, occurred_at=AT, principal_subject="local:coordinator", project_id=7,
        state="changes_to_review", landing="/work/p", measurement_context=context,
    )
    pin(event, EventFamily.PROJECT_SELECTION, {
        "principal_subject": "local:coordinator",
        "project_id": 7,
        "state": "changes_to_review",
        "landing": "/work/p",
        **(context or {}),
    }, {"surface": "portfolio", "state": "changes_to_review"})


def test_follow_up_reading_matches_follow_up_bundles_emitter():
    event = follow_up_reading_event(
        BINDING, occurred_at=AT, surface="chase_list", principal_subject="local:coordinator", project_id=7,
        cutoff=AT.isoformat(), rule_set="follow-up-v1", accepted_revision_id=11, reading_identity="reading:abc",
        bundle_count=2, retained_outgoing_requests=1, bundles_by_band={"overdue": 1, "waiting": 1},
    )
    pin(event, EventFamily.FOLLOW_UP_READING, {
        "principal_subject": "local:coordinator",
        "project_id": 7,
        "cutoff": AT.isoformat(),
        "rule_set": "follow-up-v1",
        "accepted_revision_id": 11,
        "reading_identity": "reading:abc",
        "bundle_count": 2,
        "retained_outgoing_requests": 1,
        "bundles_by_band": {"overdue": 1, "waiting": 1},
    }, {"surface": "chase_list", "status": "presented"})


def test_presentation_families_match_measurement_collection_emitter():
    opening = project_opening_event(BINDING, occurred_at=AT, project_id=7, principal_subject="local:coordinator")
    pin(opening, EventFamily.PROJECT_OPENING,
        {"project_id": 7, "principal_subject": "local:coordinator"}, {"surface": "project_opening"})

    coverage = coverage_reading_event(
        BINDING, occurred_at=AT, project_id=7, principal_subject="local:coordinator", reading_sha256="c" * 64,
        issue_profile_identity="fixture-issue", issue_profile_version=1, issue_profile_sha256="a" * 64,
        coverage_declaration_id=8, through_source_delivery_id=3,
    )
    pin(coverage, EventFamily.COVERAGE_READING, {
        "project_id": 7, "principal_subject": "local:coordinator",
        "reading_sha256": "c" * 64,
        "issue_profile_identity": "fixture-issue",
        "issue_profile_version": 1,
        "issue_profile_sha256": "a" * 64,
        "coverage_declaration_id": 8,
        "through_source_delivery_id": 3,
    }, {"surface": "coverage_reading"})

    evidence = evidence_opening_event(
        BINDING, occurred_at=AT, project_id=7, principal_subject="local:coordinator",
        item_key="packet-a", delta_id=31, source_row_id=44, link_role="source",
    )
    pin(evidence, EventFamily.EVIDENCE_OPENING, {
        "project_id": 7, "principal_subject": "local:coordinator",
        "item_key": "packet-a", "delta_id": 31, "source_row_id": 44, "link_role": "source",
    }, {"surface": "evidence_opening"})


def test_preparation_interaction_families_match_measurement_collection_emitter():
    request = preparation_request_event(
        BINDING, occurred_at=AT, project_id=7, receipt_id=6, issue_profile_identity="fixture-issue",
        issue_profile_version=1, issue_profile_sha256="a" * 64, principal_subject="local:coordinator",
        request_id=6, coverage_declaration_id=8, outcome="requested",
    )
    pin(request, EventFamily.PREPARATION_REQUEST, {
        "project_id": 7, "receipt_id": 6,
        "issue_profile_identity": "fixture-issue",
        "issue_profile_version": 1,
        "issue_profile_sha256": "a" * 64,
        "principal_subject": "local:coordinator",
        "request_id": 6, "coverage_declaration_id": 8, "outcome": "requested",
    }, {})

    confirmation = coverage_confirmation_event(
        BINDING, occurred_at=AT, project_id=7, receipt_id=8, issue_profile_identity="fixture-issue",
        issue_profile_version=1, issue_profile_sha256="a" * 64, principal_subject="local:coordinator",
        coverage_declaration_id=8, reading_sha256="c" * 64, annotation_count=2, unchanged_declaration_reused=False,
    )
    pin(confirmation, EventFamily.COVERAGE_CONFIRMATION, {
        "project_id": 7, "receipt_id": 8,
        "issue_profile_identity": "fixture-issue",
        "issue_profile_version": 1,
        "issue_profile_sha256": "a" * 64,
        "principal_subject": "local:coordinator",
        "coverage_declaration_id": 8, "reading_sha256": "c" * 64,
        "annotation_count": 2, "unchanged_declaration_reused": False,
    }, {})


def test_preparation_attempt_shapes_match_release_preparation_worker_emitter():
    profile = {"issue_profile_identity": "fixture-issue", "issue_profile_version": 1, "issue_profile_sha256": "a" * 64}
    started = preparation_attempt_started_event(
        BINDING, occurred_at=AT, project_id=7, request_id=6, coverage_declaration_id=8,
        principal_subject="local:coordinator", started_at=AT.isoformat(), **profile,
    )
    pin(started, EventFamily.PREPARATION_ATTEMPT, {
        "project_id": 7, "request_id": 6,
        "coverage_declaration_id": 8,
        "principal_subject": "local:coordinator", "outcome": "started",
        "started_at": AT.isoformat(), **profile,
    }, {})

    finished = preparation_attempt_finished_event(
        BINDING, occurred_at=AT, project_id=7, request_id=6, attempt_id=2, outcome="failed", candidate_id=None,
        refusal_code="renderer_failed", started_at=AT.isoformat(), coverage_declaration_id=8,
        principal_subject="local:coordinator", **profile,
    )
    pin(finished, EventFamily.PREPARATION_ATTEMPT, {
        "project_id": 7, "request_id": 6,
        "attempt_id": 2, "outcome": "failed",
        "candidate_id": None, "refusal_code": "renderer_failed",
        "started_at": AT.isoformat(),
        "coverage_declaration_id": 8,
        "principal_subject": "local:coordinator", **profile,
    }, {})


def test_delta_supersession_matches_email_spine_emitter():
    event = delta_supersession_event(
        BINDING, project_id=7, prior_delta_id=31, superseding_delta_id=32, source_reading_id=5,
        source_revision="d" * 64, comparison_rule_version="email-v3",
    )
    assert event.family is EventFamily.DELTA_SUPERSESSION
    assert event.occurred_at.tzinfo is timezone.utc
    expected = {
        "project_id": 7,
        "prior_delta_id": 31,
        "superseding_delta_id": 32,
        "source_reading_id": 5, "source_revision": "d" * 64,
        "comparison_rule_version": "email-v3",
    }
    assert event.payload == expected and list(event.payload) == list(expected)
    assert event.metric_labels == {}
