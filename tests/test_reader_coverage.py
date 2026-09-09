"""Missing records or copied fields must defeat otherwise identical output."""

from dataclasses import replace

import pytest

from corridor.reader_coverage import (
    CONTRACTS, SemanticRecord, SurfaceReading, compare_all_surfaces, compare_surface,
)


def test_equal_rendered_workbooks_do_not_mask_legacy_fields_or_missing_population():
    contract = next(item for item in CONTRACTS if item.name == "workbook_export")
    record = SemanticRecord("workbook", "DEP-1", {
        "population": ["DEP-1"], "accepted_values": {"station_from": "12+00"},
        "coordination": None, "statements": [], "source_support": [],
    }, {field: "revision:7" for field in contract.fields})
    before = SurfaceReading(contract.name, (record,), contract.record_kinds, "same-cells")
    assert compare_surface(contract, before, before).passed
    copied = replace(record, field_origins={**record.field_origins, "coordination": "legacy:work_decisions:12"})
    assert not compare_surface(contract, before, replace(before, records=(copied,))).passed
    assert not compare_surface(contract, before, replace(before, records=())).passed
    assert not compare_surface(contract, before, replace(before, observed_record_kinds=frozenset({"workbook"}))).passed
    assert not compare_surface(contract, before, replace(before, records=(record, record))).passed


def test_changed_field_and_uncompared_unchanged_artifact_fail():
    contract = CONTRACTS[5]
    record = SemanticRecord("release", "release-1", {field: None for field in contract.fields},
                            {field: "revision:9" for field in contract.fields})
    before = SurfaceReading(contract.name, (record,), contract.record_kinds, "page-text-1")
    changed = replace(record, fields={**record.fields, "approval": "another principal"})
    assert not compare_surface(contract, before, replace(before, records=(changed,))).passed
    assert not compare_surface(contract, before, replace(before, output_identity=None)).passed
    assert not compare_surface(contract, before, replace(before, records=(replace(record, fields={}),))).passed


def test_all_seven_surfaces_are_required_even_when_no_records_exist():
    # The reference population is explicitly empty for each declared class.
    readings = tuple(SurfaceReading(item.name, (), item.record_kinds, "empty") for item in CONTRACTS)
    assert all(item.passed for item in compare_all_surfaces(readings, readings))
    assert not all(item.passed for item in compare_all_surfaces(readings, readings[:-1]))
    assert not all(item.passed for item in compare_all_surfaces(readings, (*readings, readings[0])))


@pytest.mark.parametrize("field", ["accepted_values", "authority"])
def test_support_assessment_is_not_accepted_value_authority(field):
    contract = CONTRACTS[2]
    record = SemanticRecord("current", "subject-1", {name: None for name in contract.fields},
                            {name: "revision:9" for name in contract.fields})
    reading = SurfaceReading(contract.name, (record,), contract.record_kinds)
    supported = replace(record, field_origins={**record.field_origins,
                        "source_support": "native_assessment:support_assessments:17"})
    assert compare_surface(contract, reading, replace(reading, records=(supported,))).passed
    forged = replace(supported, field_origins={**supported.field_origins,
                     field: "native_assessment:support_assessments:17"})
    assert not compare_surface(contract, reading, replace(reading, records=(forged,))).passed


@pytest.mark.parametrize("kind", ["source", "decision", "correction", "reversal", "support"])
def test_assessment_history_origin_applies_only_to_support_rows(kind):
    contract = CONTRACTS[6]
    record = SemanticRecord(kind, "17", {name: None for name in contract.fields},
                            {name: "native_assessment:support_assessments:17" for name in contract.fields})
    reading = SurfaceReading(contract.name, (record,), contract.record_kinds)
    assert compare_surface(contract, reading, reading).passed == (kind == "support")


@pytest.mark.parametrize("identity", ["", "0", "-1", "17:other", "unknown", "١٧"])
def test_assessment_history_requires_a_concrete_database_identity(identity):
    contract = CONTRACTS[6]
    record = SemanticRecord("support", "17", {name: None for name in contract.fields},
                            {name: f"native_assessment:support_assessments:{identity}"
                             for name in contract.fields})
    reading = SurfaceReading(contract.name, (record,), contract.record_kinds)
    assert not compare_surface(contract, reading, reading).passed
