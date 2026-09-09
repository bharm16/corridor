"""Explicit retained-history classes for ADR-0081 stage 2.

Older retirement archived only the active Dependency graph. This inventory names
history and its ownership joins before any row is copied. Names are physical
storage identities, not new Project Record terminology. A compatibility copy
preserves a historical assertion; it never turns its words into a Source Fact.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryClass:
    table: str
    project_predicate: str
    treatment: str
    expiry: str


# These predicates deliberately follow subject ownership, never an unrelated
# shared organization's identity. Alias h always denotes the table being read.
_DEPENDENCY = "h.dependency_id in (select id from dependencies where project_id = p_project_id)"
_EVENT = "h.event_id in (select id from dependency_events where project_id = p_project_id)"
_CANDIDATE = "h.candidate_id in (select id from candidates where project_id = p_project_id)"
_PROJECT = "h.project_id = p_project_id"
_WORK = "h.work_decision_id in (select id from work_decisions where dependency_id in (select id from dependencies where project_id=p_project_id) or commitment_lineage_id in (select id from commitment_lineages where project_id=p_project_id))"
_NATIVE_EXPIRY = "Keep original lineage for the lifetime of the referencing record and released artifacts."
_COMPAT_EXPIRY = "Remove active compatibility routing only after every consuming surface proves native field and record coverage, selected as-of readings, and rollback-window closure; retain original history under the customer retention and backup policy."
_QUOTE_EXPIRY = "Retain original quote permanently as historical text when no exact source locator can be proven; never manufacture a Source Segment. Customer-wide disposition still applies."


def _classes(tables, predicate, treatment="compatibility", expiry=_COMPAT_EXPIRY):
    return tuple(HistoryClass(table, predicate, treatment, expiry) for table in tables)


HISTORY_CLASSES = (
    *_classes(("dependencies", "dependency_events", "commitment_lineages", "candidates"), _PROJECT),
    *_classes(("assertions", "dependency_dismissals", "dispute_settlements", "dispute_history_resolutions", "operative_support", "dependency_evidence_sufficiencies", "documentation_field_confirmations", "condition_resolutions", "retired_dependency_statuses", "follow_up_plan_receipts", "reconfirmation_receipts"), _DEPENDENCY),
    *_classes(("evidence_links",), _DEPENDENCY, "retained_quote", _QUOTE_EXPIRY),
    *_classes(("dependency_event_scope_decisions", "dependency_event_scopes", "dependency_event_timings", "dependency_event_evidence", "dependency_event_migration_receipts"), _EVENT),
    *_classes(("candidate_dispositions", "statement_coordination_receipts", "statement_coordination_reversals"), _CANDIDATE),
    *_classes(("work_decisions",), f"({_DEPENDENCY}) or h.commitment_lineage_id in (select id from commitment_lineages where project_id=p_project_id)"),
    *_classes(("work_decision_milestone_impacts",), _WORK),
    *_classes(("statement_coordination_reversal_effects",), "h.reversal_id in (select id from statement_coordination_reversals where candidate_id in (select id from candidates where project_id=p_project_id))"),
    *_classes(("follow_up_plan_reversals",), "h.receipt_id in (select id from follow_up_plan_receipts where dependency_id in (select id from dependencies where project_id=p_project_id))"),
    *_classes(("dependency_admission_outcomes", "event_admission_outcomes"), _CANDIDATE),
    *_classes(("automatic_carry_forward_receipts", "automatic_carry_forward_outcomes", "schedule_link_receipts", "milestones", "schedule_governing_derivations", "organization_identity_receipts"), _PROJECT),
    *_classes(("milestone_registrations",), "h.milestone_id in (select id from milestones where project_id=p_project_id)"),
    *_classes(("documents", "source_segments", "facts", "fact_decisions", "project_record_revisions", "recorded_verbal_origins", "recorded_verbal_origin_statements", "evidence_link_sources", "extracted_proposals", "support_assessments"), _PROJECT, "native_lineage", _NATIVE_EXPIRY),
    *_classes(("fact_sources", "fact_statement_timings"), "h.fact_id in (select id from facts where project_id=p_project_id)", "native_lineage", _NATIVE_EXPIRY),
    *_classes(("fact_applies_to", "fact_closure_results"), _PROJECT, "native_lineage", _NATIVE_EXPIRY),
    *_classes(("fact_closure_sources",), "h.fact_id in (select id from facts where project_id=p_project_id)", "native_lineage", _NATIVE_EXPIRY),
    *_classes(("support_assessment_sources",), "h.support_assessment_id in (select id from support_assessments where project_id=p_project_id)", "native_lineage", _NATIVE_EXPIRY),
    *_classes(("audit_log",), "(h.entity_type='project' and h.entity_id=p_project_id) or (h.entity_type='dependency' and h.entity_id in (select id from dependencies where project_id=p_project_id)) or (h.entity_type='candidate' and h.entity_id in (select id from candidates where project_id=p_project_id)) or (h.entity_type='commitment_lineage' and h.entity_id in (select id from commitment_lineages where project_id=p_project_id)) or (h.entity_type='document' and h.entity_id in (select id from documents where project_id=p_project_id)) or (h.entity_type='milestone' and h.entity_id in (select id from milestones where project_id=p_project_id))"),
)

# The three known quote carriers: retain without fabricating a locator. Rows
# that already carry EvidenceLinkSource retain that genuine citation too.
QUOTE_WRITERS = (
    "adjudicate._evidence_link: EvidenceLink.quote",
    "operative_support.transfer_operative_scopes_under_lock: EvidenceLink.quote",
    "operative_support.resolve_operative_support: EvidenceLink.quote",
)
