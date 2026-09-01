"""Released contracts for controlled typed source Facts.

This module owns the vocabulary shared by append validation, Record Inclusion,
and database-model checks.  It deliberately has no ORM dependency: a Fact type
is released policy, while rows and migrations are storage adapters for that
policy (ADRs 0067 and 0070).
"""

from __future__ import annotations

from dataclasses import dataclass


def inclusion_rule_admits_human_record_decision(inclusion_rule: str) -> bool:
    """Whether a Fact type may be settled by a Human Record Decision.

    A pure human type is always human-settled; a dual-use type
    (``..._else_human_record_decision``) is human-settled off its automatic
    source — a Recorded Verbal Statement's Applies To, not a spreadsheet cell.
    """

    return inclusion_rule == "human_record_decision" or inclusion_rule.endswith(
        "_else_human_record_decision"
    )


@dataclass(frozen=True)
class FactTypeContract:
    """Validation, current-value, and inclusion rules for one Fact type."""

    value_class: str
    subject_kind: str
    transformation: str
    accepted_segment_kinds: frozenset[str]
    automatic_segment_kinds: frozenset[str]
    required_roles: frozenset[str]
    validation_rule: str
    current_value_rule: str
    inclusion_rule: str


STRUCTURED_TEXT_FACT_TYPES = (
    "utility_id",
    "external_org",
    "external_org_contact",
    "utility_type",
    "utility_subtype",
    "utility_function",
    "operational_status",
    "size",
    "material",
    "oh_ug",
    "row_placement",
    "orientation",
    "baseline",
    "station_from",
    "station_to",
    "offset_from",
    "offset_to",
    "sue_level",
    "conflict_description",
    "resolution_strategy",
    "notes",
    "alignment",
    "location_start",
    "location_end",
    "offset_side",
    "potential_conflict",
    "data_source",
    "marked_resolution",
)
STRUCTURED_DATE_FACT_TYPES = ("committed_date", "action_due_date", "need_date")
STRUCTURED_SATELLITE_FACT_TYPES = ("applies_to", "closure_result")
SINGLE_VALUED_FACT_TYPES = (*STRUCTURED_TEXT_FACT_TYPES, *STRUCTURED_DATE_FACT_TYPES)
EFFECTIVE_SINGLE_VALUE_FACT_TYPES = (*SINGLE_VALUED_FACT_TYPES, "closure_result")
STRUCTURED_CELL_FACT_TYPES = (
    *STRUCTURED_TEXT_FACT_TYPES,
    *STRUCTURED_DATE_FACT_TYPES,
    "applies_to",
)


FACT_TYPE_CONTRACTS = {
    **{
        name: FactTypeContract(
            value_class="external_org_wording" if name == "external_org" else "text",
            subject_kind="source_row",
            transformation="trim_cell_text_v1",
            accepted_segment_kinds=frozenset({"spreadsheet_cell"}),
            automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
            required_roles=frozenset({"value_source"}),
            validation_rule=(
                "non_empty_replay_exact_optional_registered_alias"
                if name == "external_org"
                else "non_empty_replay_exact"
            ),
            current_value_rule="latest_effective_single_value",
            # A cell value is settled by the automatic policy; a Discrepancy
            # Resolution settles the same field by a Human Record Decision
            # over an already-observed fact of the matching type (#451).
            inclusion_rule="verified_mapping_cell_policy_else_human_record_decision",
        )
        for name in STRUCTURED_TEXT_FACT_TYPES
    },
    **{
        name: FactTypeContract(
            value_class="date",
            subject_kind="source_row",
            transformation="iso_date_cell_v1",
            accepted_segment_kinds=frozenset({"spreadsheet_cell"}),
            automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
            required_roles=frozenset({"value_source"}),
            validation_rule="iso_calendar_date_replay_exact",
            current_value_rule="latest_effective_single_value",
            inclusion_rule="verified_mapping_cell_policy_else_human_record_decision",
        )
        for name in STRUCTURED_DATE_FACT_TYPES
    },
    "applies_to": FactTypeContract(
        value_class="reference_set",
        subject_kind="source_row",
        transformation="structured_reference_set_v1",
        accepted_segment_kinds=frozenset(
            {"spreadsheet_cell", "recorded_verbal_statement", "prose_span"}
        ),
        automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
        required_roles=frozenset({"value_source"}),
        validation_rule="non_empty_scoped_reference_set",
        current_value_rule="effective_reference_set",
        # A spreadsheet Applies To is settled by the automatic cell policy; a
        # Recorded Verbal Statement's scope is a Human Record Decision (#451).
        inclusion_rule="verified_mapping_cell_policy_else_human_record_decision",
    ),
    "closure_result": FactTypeContract(
        value_class="closure_result",
        subject_kind="source_row",
        transformation="typed_closure_result_v1",
        accepted_segment_kinds=frozenset({"spreadsheet_cell", "prose_span"}),
        automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
        required_roles=frozenset({"value_source"}),
        validation_rule="typed_closure_with_governing_sources",
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="source_marked_cell_policy_else_human_record_decision",
    ),
    "statement_wording": FactTypeContract(
        value_class="text",
        subject_kind="statement_candidate",
        transformation="exact_prose_span_v1",
        accepted_segment_kinds=frozenset(
            {"prose_span", "recorded_verbal_statement"}
        ),
        automatic_segment_kinds=frozenset(),
        required_roles=frozenset({"value_source", "attribution_source"}),
        validation_rule="exact_attributed_prose_span",
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="human_record_decision",
    ),
    "statement_timing": FactTypeContract(
        value_class="statement_timing",
        subject_kind="statement_candidate",
        transformation="typed_statement_timing_v1",
        accepted_segment_kinds=frozenset(
            {"recorded_verbal_statement", "prose_span"}
        ),
        automatic_segment_kinds=frozenset(),
        required_roles=frozenset({"value_source"}),
        validation_rule="typed_statement_timing_set",
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="human_record_decision",
    ),
    # A relationship between one Project Record subject and one immutable
    # document revision (ADR-0074 stage 3): the identity is the registered
    # document row itself, so the fact needs no source segment to replay.
    "supporting_documentation_in_use": FactTypeContract(
        value_class="document_revision",
        subject_kind="record_subject",
        transformation="supporting_document_revision_v1",
        accepted_segment_kinds=frozenset(),
        automatic_segment_kinds=frozenset(),
        required_roles=frozenset(),
        validation_rule="registered_document_revision",
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="human_record_decision",
    ),
}
