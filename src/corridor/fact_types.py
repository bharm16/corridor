"""Released contracts for controlled typed source Facts.

This module owns the vocabulary shared by append validation, Record Inclusion,
and database-model checks.  It deliberately has no ORM dependency: a Fact type
is released policy, while rows and migrations are storage adapters for that
policy (ADRs 0067 and 0070).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FactTypeContract:
    """Validation, current-value, and inclusion rules for one Fact type."""

    value_class: str
    subject_kind: str
    transformation: str
    accepted_segment_kinds: frozenset[str]
    automatic_segment_kinds: frozenset[str]
    required_roles: frozenset[str]
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
STRUCTURED_CELL_FACT_TYPES = (*STRUCTURED_TEXT_FACT_TYPES, *STRUCTURED_DATE_FACT_TYPES)


FACT_TYPE_CONTRACTS = {
    **{
        name: FactTypeContract(
            value_class="external_org_wording" if name == "external_org" else "text",
            subject_kind="source_row",
            transformation="trim_cell_text_v1",
            accepted_segment_kinds=frozenset({"spreadsheet_cell"}),
            automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
            required_roles=frozenset({"value_source"}),
            current_value_rule="latest_effective_single_value",
            inclusion_rule="verified_mapping_cell_policy",
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
            current_value_rule="latest_effective_single_value",
            inclusion_rule="verified_mapping_cell_policy",
        )
        for name in STRUCTURED_DATE_FACT_TYPES
    },
    "applies_to": FactTypeContract(
        value_class="reference_set",
        subject_kind="source_row",
        transformation="structured_reference_set_v1",
        accepted_segment_kinds=frozenset({"spreadsheet_cell"}),
        automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
        required_roles=frozenset({"value_source"}),
        current_value_rule="effective_reference_set",
        inclusion_rule="verified_mapping_cell_policy",
    ),
    "closure_result": FactTypeContract(
        value_class="closure_result",
        subject_kind="source_row",
        transformation="typed_closure_result_v1",
        accepted_segment_kinds=frozenset({"spreadsheet_cell", "prose_span"}),
        automatic_segment_kinds=frozenset(),
        required_roles=frozenset({"value_source"}),
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="human_record_decision",
    ),
    "statement_wording": FactTypeContract(
        value_class="text",
        subject_kind="statement_candidate",
        transformation="exact_prose_span_v1",
        accepted_segment_kinds=frozenset({"prose_span"}),
        automatic_segment_kinds=frozenset(),
        required_roles=frozenset({"value_source", "attribution_source"}),
        current_value_rule="human_decision_effectiveness",
        inclusion_rule="human_record_decision",
    ),
}
