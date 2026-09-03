"""Current product words at presentation boundaries, without changing records.

The accepted terminology differs from retained model, rule, and provenance
identifiers (ADR-0048). Replacing words in finished text would alter source
quotations and names. These pure adapters accept only known system labels or
enumerated kinds; source-authored text never passes through a replacement loop.
"""

from __future__ import annotations

import re
from typing import Protocol


_LABELS = {
    "constraint": "Constraint",
    "constraints": "Constraints",
    "constraint_plural": "Constraints",
    "dependency": "Constraint",
    "dependencies": "Constraints",
    "constraint_log": "Constraint log",
    "ledger": "Constraint log",
    "organization": "Organization",
    "external_party": "Organization",
    "key_date": "Key date",
    "key_dates": "Key dates",
    "milestone": "Key date",
    "key_date_version": "Key date version",
    "milestone_readiness": "Constraints by key date",
    "effect_on_key_dates": "Effect on key dates",
    "milestone_impact": "Effect on key dates",
    "required_by": "Required by",
    "need_date": "Required by",
    "promised_for": "Promised for",
    "committed_date": "Promised for",
    "assigned_to": "Assigned to",
    "internal_owner": "Assigned to",
    "next_action": "Next action",
    "action_due_date": "Action due date",
    "follow_up_plan": "Follow-up plan",
    "work_plan": "Follow-up plan",
    "coordination_decision": "Coordination decision",
    "source_discrepancy": "Source discrepancy",
    "discrepancy_resolution": "Discrepancy resolution",
    "record_conclusion": "Record conclusion",
    "supporting_documents": "Supporting documents",
    "evidence": "Supporting documents",
    "cited_passage": "Cited passage",
    "evidence_quote": "Cited passage",
    "required_documents": "Documents required for this condition",
    "readiness_requirement": "Documents required for this condition",
    "documentation_review": "Documentation review",
    "applies_to": "Applies to",
    "statement_history": "Statement history",
    "statement_type": "Statement type",
    "source_passage_check": "Source passage check",
    "do_not_add": "Do not add",
    "remove_incorrect_entry": "Remove incorrect entry",
    "approved_to_share": "Approved to share",
    "work_item": "Coordination item",
    "attention_reason": "Why this needs attention",
    "constraint_alerts": "Constraint alerts",
    "exceptions": "Constraint alerts",
    "constraint_check": "Constraint check",
    "report": "Coordination report",
    "coordination_summary": "Coordination summary — AI draft",
    "assertion": "Source field value",
    "derivation": "Calculated result",
    "verbal": "Recorded verbal statement",
    "provenance": "Source traceability",
    "critical_items": "Relocation / removal / abandonment",
    "resolution_strategy": "Resolution method",
    "organization_commitments": "Organization commitments",
}


def label(key: str) -> str:
    """Return a known system label; reject accidental source-text inputs."""
    return _LABELS[key]


def statement_type_label(event_type: str) -> str:
    """Explain an event kind without renaming the retained event identity."""
    return {
        "commitment": "Commitment",
        "committed_date_change": "Change to promised timing",
        "closure": "Completion reported",
    }.get(event_type, event_type)


def source_passage_check_label(status: str) -> str:
    """Name a Source Passage Check state without claiming what it supports.

    The stored identifiers stay the machine words ADR-0082 fixed (``valid``,
    ``invalid``, ``not_checked``); these are the customer words for the same
    three states.  Each says only where the cited passage was looked for and
    whether it was there, never that the source supports the value beside it —
    that is a Support Assessment, and it is displayed separately.

    Passed, Failed, and Not run were the first wording and were rejected
    (#600): each reads as a verdict on the value, or on work that passed an
    inspection, which the check never is.  "Not run" also misdescribed the
    mechanism, because the status is computed by replaying the locator when it
    is read; ``not_checked`` means there was no cited location to replay.
    """
    return {
        "valid": "Found at cited location",
        "invalid": "Not found at cited location",
        "not_checked": "No cited location recorded",
    }[status]


def documentation_review_label(sufficient: bool) -> str:
    """Describe the legacy marker; never infer a specific construction outcome."""
    return "Documents marked sufficient" if sufficient else "Not confirmed"


def resolution_strategy_label(strategy: str) -> str:
    """Explain a known method enum without rewriting a source field value."""
    return {
        "relocate": "Relocate",
        "remove": "Remove",
        "abandon_in_place": "Abandon in place",
        "adjust_vertical": "Vertical adjustment",
        "protect_in_place": "Protect in place",
        "change_design": "Change design",
        "policy_exception": "Exception to policy",
    }.get(strategy, strategy)


def provenance_label(kind: str) -> str:
    """Name a known provenance kind while keeping its stored identifier intact."""
    return {
        "assertion": label("assertion"),
        "derivation": label("derivation"),
        "decision": label("coordination_decision"),
        "work_decision": label("coordination_decision"),
        "work decision": label("coordination_decision"),
        "workdecision": label("coordination_decision"),
        "verbal": label("verbal"),
        "cited": "Cited statement",
        "evidence": label("supporting_documents"),
        "exception": "Constraint alert",
        "exception_bucket": "Constraint alert group",
    }.get(kind.casefold(), kind)


def input_reference_label(ref: str) -> str:
    """Present an exact generated reference, preserving arbitrary source names."""
    milestone = re.fullmatch(r"Milestone Registration (MR[0-9]+)", ref)
    if milestone is not None:
        return f"{label('key_date_version')} {milestone.group(1)}"
    return ref


def field_label(field_name: str) -> str:
    """Label a known system field key, never a field's source-authored value."""
    return {
        "external_org": label("organization"),
        "external_org_id": label("organization"),
        "internal_owner": label("assigned_to"),
        "evidence_required": label("required_documents"),
        "need_date": label("required_by"),
        "resolution_strategy": label("resolution_strategy"),
        "committed_date": label("promised_for"),
        "station_from": "From station",
        "station_to": "To station",
        "utility_id": "Source conflict ID",
        "utility_type": "Utility type",
        "external_contact": "Organization contact",
        "next_action": label("next_action"),
        "action_due_date": label("action_due_date"),
        "milestone": label("key_date"),
        "milestone_id": label("key_date"),
        "milestone_impact": label("effect_on_key_dates"),
        "title": "Title",
        "notes": "Notes",
        "location_desc": "Location",
    }.get(field_name, field_name)


def exception_name(rule: str) -> str:
    """Name a known check without changing its retained rule code or meaning."""
    return {
        # The rule counts supporting documents whose cited passage was found
        # in its source, so the alert names that check rather than calling the
        # documents themselves verified (ADR-0082).
        "MISSING_EVIDENCE": "No supporting document passed the source passage check",
        "MISSING_DATE": "No exact promised date for this check",
        "MISSING_OWNER": "No person assigned",
        "OVERDUE": "Promised timing passed",
        "DUE_SOON": "Required by date is near",
        "STALE": "No recent supporting documents",
        "CONTRADICTION": "Sources disagree",
        "ORPHAN": "No key date linked",
        "SUPERSEDED_CITATION": "Supporting document replaced",
        "MISSING_ACTION": "No next action",
        "ACTION_DUE_SOON": "Next action is due soon",
        "ACTION_OVERDUE": "Next action is overdue",
    }.get(rule, rule)


# The accepted record runs its own check set (``accepted_record_checks_v2``,
# ADR-0090), and two of its rules do not mean what the released legacy
# ruleset's rules of the same name mean.  Only a rule whose predicate actually
# changed appears here; every other rule keeps the label above, verbatim, so
# one finding reads the same way wherever it is shown.
#
# ``MISSING_EVIDENCE`` is the one that changed.  The legacy rule fires when no
# supporting document on a Constraint had its cited passage found in its
# source, and its label says exactly that.  ADR-0082 decided that a locatable
# passage is not support, so the accepted record's rule instead fires when no
# Supporting Documentation is in use for the accepted proposition — read from
# the Support Assessment relation and never from the Source Passage Check.
# Carrying the old label onto the new predicate would tell a customer that a
# quotation could not be located, which the check no longer looks at.  The
# replacement wording is researched in
# docs/research/missing-evidence-alert-label-2026-09-03.md; it deliberately
# avoids the Source Passage Check state labels (#600) so the two can never be
# read as the same finding.
_ACCEPTED_RECORD_LABELS = {
    "MISSING_EVIDENCE": "No supporting document in use for this value",
}


def accepted_record_exception_name(rule: str) -> str:
    """Name a check of the accepted record's own set (ADR-0090).

    The legacy ruleset keeps computing all twelve rules over a legacy
    project until ADR-0081 stage 6 retires those tables, and its labels stay
    exactly as they are: a label that stopped describing the predicate under
    it would make the legacy report lie.  The two sets are allowed to differ
    and the report declares the set it ran, so this is the one place the
    difference is expressed.
    """
    return _ACCEPTED_RECORD_LABELS.get(rule) or exception_name(rule)


class _ExceptionFact(Protocol):
    rule: str
    quantity_days: int | None


def exception_label(exception: _ExceptionFact) -> str:
    """Keep each alert's own day quantity beside its current customer label."""
    quantity = (
        f" {exception.quantity_days}d" if exception.quantity_days is not None else ""
    )
    return f"{exception_name(exception.rule)}{quantity}"
