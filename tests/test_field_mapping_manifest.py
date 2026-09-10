"""The declared mapping revision a template is read through, against bytes alone.

``field_mapping_manifest`` exists for one failure no digest over a template can
catch: a form that stops carrying *Start Station* and *End Station* as two
values and starts carrying one combined range in the same two columns, or the
reverse.  Every heading and every drop-down is identical, so only the declared
composition separates them.  ``prove_manifest``, ``conformance_refusals``,
``composition_rule`` and the combined-range rule had no direct test at all;
they were reached only through the database-bound adoption tests.  These build
small workbooks and declarations, and read them.  No engine, no store, no model.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import Workbook

from corridor.baseline_workbook import read_baseline_workbook
from corridor.field_mapping_manifest import (
    ALL_ABSENT_IS_UNKNOWN,
    CALENDAR_DAY,
    CARRIES_RETIREMENT_WORDING,
    COMBINED_RANGE,
    DEFAULT_RANGE_DELIMITER,
    MANIFEST_SCHEMA_VERSION,
    ONE_VALUE_PER_COLUMN,
    RANGE_SEPARATORS,
    SOURCE_STATED_UNIT,
    ExternalReference,
    FieldMappingManifest,
    MappingDeclaration,
    MappingManifestRefused,
    MaterialMapping,
    composition_rule,
    conformance_refusals,
    declared_field_mapping,
    prove_manifest,
)


HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Size",
    "Material",
    "Start Station",
    "End Station",
    "Resolution Strategy Selected (from Resolution Alternatives)",
    "Promised For",
]

# One conforming row: two stations in two columns, the way the published forms
# head them (ADR-0005, ADR-0009).
SPLIT_ROW = [
    "UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel",
    "1149+00", "1150+00", "Relocate", "2026-03-01",
]

# The same form after the change this module exists to catch: one combined
# range under `Start Station`, and `End Station` blank.
COMBINED_ROW = [
    "UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel",
    "1149+00 - 1150+00", "", "Relocate", "2026-03-01",
]

STATION_RANGE = ("Start Station", "End Station")
STATION_FIELDS = ("station_from", "station_to")


def _reading(tmp_path: Path, row, name="ucm.xlsx"):
    path = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(list(HEADINGS))
    sheet.append(list(row))
    book.save(path)
    return read_baseline_workbook(path)


def _one_per_column(**overrides) -> MaterialMapping:
    values = {
        "source_columns": ("Start Station",),
        "target_fields": ("station_from",),
        "composition": ONE_VALUE_PER_COLUMN,
        "parser": "trim_cell_text_v1",
        "delimiter": DEFAULT_RANGE_DELIMITER,
        "precision": SOURCE_STATED_UNIT,
        "material": True,
        "example": ("1149+00",),
    }
    values.update(overrides)
    return MaterialMapping(**values)


def _combined(**overrides) -> MaterialMapping:
    values = {
        "source_columns": STATION_RANGE,
        "target_fields": STATION_FIELDS,
        "composition": COMBINED_RANGE,
        "parser": "trim_cell_text_v1",
        "delimiter": DEFAULT_RANGE_DELIMITER,
        "precision": SOURCE_STATED_UNIT,
        "material": True,
        "example": ("1149+00 - 1150+00", ""),
    }
    values.update(overrides)
    return MaterialMapping(**values)


def _manifest(*mappings, **overrides) -> FieldMappingManifest:
    values = {
        "identity": "customer-form",
        "version": "v1",
        "mappings": tuple(mappings),
    }
    values.update(overrides)
    return FieldMappingManifest(**values)


# --- The released composition rules ----------------------------------------


def test_only_a_released_composition_rule_may_be_named():
    assert composition_rule(ONE_VALUE_PER_COLUMN).identity == ONE_VALUE_PER_COLUMN
    assert composition_rule(COMBINED_RANGE).identity == COMBINED_RANGE
    with pytest.raises(MappingManifestRefused, match="released composition rule"):
        composition_rule("split_on_slash_v1")


def test_one_value_per_column_states_its_cardinality_and_round_trips():
    rule = composition_rule(ONE_VALUE_PER_COLUMN)
    mapping = _one_per_column(
        source_columns=STATION_RANGE, target_fields=STATION_FIELDS,
        example=("1149+00", "1150+00"),
    )
    assert mapping.cardinality == "2 → 2"
    values = rule.decompose(mapping, mapping.example)
    assert values == ("1149+00", "1150+00")
    assert rule.compose(mapping, values) == mapping.example
    assert rule.fields_at(mapping, 1) == ("station_to",)


def test_a_combined_range_carries_every_value_in_the_first_column():
    rule = composition_rule(COMBINED_RANGE)
    mapping = _combined()
    assert mapping.cardinality == "1 → 2"
    assert rule.fields_at(mapping, 0) == STATION_FIELDS
    assert rule.fields_at(mapping, 1) == ()
    values = rule.decompose(mapping, mapping.example)
    assert values == ("1149+00", "1150+00")
    assert rule.compose(mapping, values) == ("1149+00 - 1150+00", "")
    assert rule.compose(mapping, (None, None)) == ("", "")


def test_a_combined_range_needs_two_fields_and_a_declared_delimiter():
    with pytest.raises(MappingManifestRefused, match="at least two canonical fields"):
        prove_manifest(_manifest(_combined(
            source_columns=("Start Station",), target_fields=("station_from",),
            example=("1149+00",),
        )))
    with pytest.raises(MappingManifestRefused, match="needs the delimiter"):
        prove_manifest(_manifest(_combined(delimiter=None)))


def test_a_combined_range_refuses_a_trailing_column_that_still_carries_a_value():
    rule = composition_rule(COMBINED_RANGE)
    mapping = _combined()
    refusal = rule.refusal(mapping, ("1149+00 - 1150+00", "1150+00"))
    assert refusal is not None
    assert "'End Station' carries '1150+00'" in refusal


def test_a_combined_range_refuses_a_cell_that_does_not_carry_the_whole_range():
    rule = composition_rule(COMBINED_RANGE)
    mapping = _combined()
    assert "one value where the mapping declares 2 values" in rule.refusal(
        mapping, ("1149+00", "")
    )
    assert "does not split into the 2 values" in rule.refusal(
        mapping, ("1149+00 - 1150+00 - 1151+00", "")
    )
    assert rule.refusal(mapping, ("", "")) is None


def test_all_absent_is_unknown_refuses_a_partial_range():
    mapping = _combined(blank_behaviour=ALL_ABSENT_IS_UNKNOWN)
    with pytest.raises(MappingManifestRefused, match="either every one is present"):
        composition_rule(COMBINED_RANGE).compose(mapping, ("1149+00", None))


# --- The shape check that may only ever refuse ------------------------------


@pytest.mark.parametrize("separator", RANGE_SEPARATORS)
def test_a_declared_range_endpoint_refuses_a_cell_that_combines_two_values(separator):
    """`RANGE_SEPARATORS`' reason: spaced or worded, never a bare hyphen."""

    rule = composition_rule(ONE_VALUE_PER_COLUMN)
    mapping = _one_per_column()
    refusal = rule.refusal(mapping, (f"1149+00{separator}1150+00",))
    assert refusal is not None
    assert repr(separator) in refusal
    assert "one value per column" in refusal


def test_a_hyphenated_identifier_is_not_a_combined_range():
    """`SR-BL` is an identifier and a negative offset is a hyphen; neither refuses."""

    rule = composition_rule(ONE_VALUE_PER_COLUMN)
    assert rule.refusal(_one_per_column(), ("SR-BL",)) is None
    assert rule.refusal(_one_per_column(), ("-12.5",)) is None


def test_a_column_with_no_declared_range_semantics_is_never_shape_checked():
    rule = composition_rule(ONE_VALUE_PER_COLUMN)
    mapping = _one_per_column(
        source_columns=("Material",), target_fields=("material",),
        delimiter=None, example=("Steel - coated",),
    )
    assert rule.refusal(mapping, mapping.example) is None


# --- Proving a declaration --------------------------------------------------


def test_a_declaration_whose_example_round_trips_is_proved():
    prove_manifest(_manifest(_one_per_column()))
    prove_manifest(_manifest(_combined()))


def test_a_manifest_is_named_by_an_identity_a_version_and_its_schema():
    with pytest.raises(MappingManifestRefused, match="released mapping-manifest schema"):
        prove_manifest(_manifest(_one_per_column(), schema_version="field-mapping-v0"))
    with pytest.raises(MappingManifestRefused, match="identity and a version"):
        prove_manifest(_manifest(_one_per_column(), version="  "))
    with pytest.raises(MappingManifestRefused, match="at least one mapping"):
        prove_manifest(_manifest())
    assert _manifest(_one_per_column()).schema_version == MANIFEST_SCHEMA_VERSION
    assert _manifest(_one_per_column()).revision == "customer-form v1"


def test_one_canonical_field_and_one_heading_belong_to_one_mapping():
    duplicate_field = _one_per_column(
        source_columns=("Begin Station",), example=("1149+00",)
    )
    with pytest.raises(MappingManifestRefused, match="declared by two mappings"):
        prove_manifest(_manifest(_one_per_column(), duplicate_field))
    duplicate_heading = _one_per_column(
        target_fields=("station_to",), example=("1150+00",)
    )
    with pytest.raises(MappingManifestRefused, match="'Start Station' is declared by two"):
        prove_manifest(_manifest(_one_per_column(), duplicate_heading))


def test_a_declared_parser_must_be_the_transformation_the_record_performs():
    with pytest.raises(MappingManifestRefused, match="is captured by"):
        prove_manifest(_manifest(_one_per_column(parser="split_station_v1")))
    with pytest.raises(MappingManifestRefused, match="canonical Project Record field"):
        prove_manifest(_manifest(_one_per_column(target_fields=("crew_foreman",))))
    dated = _one_per_column(
        source_columns=("Promised For",), target_fields=("committed_date",),
        parser="iso_date_cell_v1", precision=CALENDAR_DAY, delimiter=None,
        example=("2026-03-01",),
    )
    prove_manifest(_manifest(dated))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"formatting": "reformat_dates_v1"}, "released formatting rule"),
        ({"blank_behaviour": "absent_is_zero_v1"}, "released blank behaviour"),
        ({"retirement": "carries_anything_v1"}, "released retirement handling"),
        ({"precision": "nearest_foot_v1"}, "unit or timing precision"),
    ],
)
def test_every_declared_rule_identity_must_be_released(overrides, message):
    with pytest.raises(MappingManifestRefused, match=message):
        prove_manifest(_manifest(_one_per_column(**overrides)))


def test_a_retirement_field_is_declared_by_identity_not_by_spelling():
    marked = _one_per_column(
        source_columns=("Resolution Strategy Selected (from Resolution Alternatives)",),
        target_fields=("resolution_strategy",), delimiter=None,
        retirement=CARRIES_RETIREMENT_WORDING, example=("Relocate",),
    )
    prove_manifest(_manifest(marked))


def test_an_external_reference_is_a_known_role_on_a_heading_no_field_claims():
    with pytest.raises(MappingManifestRefused, match="external-reference role"):
        prove_manifest(_manifest(
            _one_per_column(),
            external_references=(ExternalReference("UCM Record ID", "conflict_owner"),),
        ))
    with pytest.raises(MappingManifestRefused, match="cannot also"):
        prove_manifest(_manifest(
            _one_per_column(),
            external_references=(
                ExternalReference("Start Station", "external_system_id"),
            ),
        ))


def test_a_populated_example_proves_the_declaration_or_refuses_it():
    with pytest.raises(MappingManifestRefused, match="carries no populated values at all"):
        prove_manifest(_manifest(_one_per_column(example=())))
    with pytest.raises(MappingManifestRefused, match="proved by a populated"):
        prove_manifest(_manifest(_combined(example=("", "")), _one_per_column(
            source_columns=("Material",), target_fields=("material",),
            delimiter=None, example=("Steel",),
        )))
    with pytest.raises(MappingManifestRefused, match="has 1 cells"):
        prove_manifest(_manifest(_combined(example=("1149+00 - 1150+00",))))
    with pytest.raises(MappingManifestRefused, match="contradicts the declaration"):
        prove_manifest(_manifest(_one_per_column(example=("1149+00 - 1150+00",))))


def test_an_example_that_does_not_survive_the_round_trip_is_refused():
    """The combined value reads back, but writes back as something else."""

    with pytest.raises(MappingManifestRefused, match="does not survive a round trip"):
        prove_manifest(_manifest(_combined(example=("1149+00 -  1150+00", ""))))


# --- Conforming a template to the registered revision ----------------------


def test_a_conforming_template_produces_no_refusals(tmp_path):
    reading = _reading(tmp_path, SPLIT_ROW)
    manifest = declared_field_mapping(reading)
    assert conformance_refusals(manifest, reading) == ()


def test_a_template_that_combines_a_declared_pair_of_values_refuses(tmp_path):
    """The flip this module exists for: two values become one combined range."""

    registered = declared_field_mapping(_reading(tmp_path, SPLIT_ROW, "v1.xlsx"))
    successor = _reading(tmp_path, COMBINED_ROW, "v2.xlsx")

    [refusal] = conformance_refusals(registered, successor)
    assert "Utility Conflicts!3" in refusal
    assert "'Start Station' carries '1149+00 - 1150+00'" in refusal
    assert repr(DEFAULT_RANGE_DELIMITER) in refusal
    assert "project-coordination designation registers" in refusal


def test_a_template_that_splits_a_declared_combined_range_refuses(tmp_path):
    """And the reverse flip, which is the same failure the other way round."""

    combined_declaration = MappingDeclaration(mappings=(_combined(example=()),))
    registered = declared_field_mapping(
        _reading(tmp_path, COMBINED_ROW, "v2.xlsx"), combined_declaration
    )
    assert registered.mapping_for("station_to").composition == COMBINED_RANGE

    [refusal] = conformance_refusals(registered, _reading(tmp_path, SPLIT_ROW, "v1.xlsx"))
    assert "'End Station' carries '1150+00'" in refusal
    assert "the whole range in 'Start Station'" in refusal


def test_a_heading_carrying_a_field_the_revision_does_not_declare_refuses(tmp_path):
    reading = _reading(tmp_path, SPLIT_ROW)
    manifest = declared_field_mapping(reading)
    renamed = replace(
        manifest,
        mappings=tuple(
            replace(mapping, source_columns=("Begin Station",))
            if mapping.target_fields == ("station_from",)
            else mapping
            for mapping in manifest.mappings
        ),
    )

    refusals = conformance_refusals(renamed, reading)
    assert any(
        "'Start Station' carries 'station_from' in this template" in item
        for item in refusals
    )
    assert any(
        "declares 'Begin Station' carrying 'station_from'" in item
        for item in refusals
    )


def test_a_materiality_disagreement_refuses_before_any_composition_check(tmp_path):
    reading = _reading(tmp_path, SPLIT_ROW)
    manifest = declared_field_mapping(reading)
    demoted = replace(
        manifest,
        mappings=tuple(
            replace(mapping, material=False)
            if mapping.target_fields == ("station_from",)
            else mapping
            for mapping in manifest.mappings
        ),
    )

    refusals = conformance_refusals(demoted, reading)
    assert refusals == (
        "'station_from' is material in this template and not material in the "
        f"approved mapping revision {manifest.revision}",
    )


def test_a_declared_heading_the_workbook_heads_no_column_for_refuses(tmp_path):
    reading = _reading(tmp_path, SPLIT_ROW)
    declaration = MappingDeclaration(
        mappings=(_combined(source_columns=("Begin Station", "End Station"), example=()),)
    )
    with pytest.raises(MappingManifestRefused, match="heads no canonical column"):
        declared_field_mapping(reading, declaration)
