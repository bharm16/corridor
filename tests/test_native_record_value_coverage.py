"""The current/as-of collector must independently verify accepted values."""

from dataclasses import replace

import pytest

from test_native_reader_coverage import adopted as adopted, collect, surfaces


@pytest.mark.parametrize("fault", ["omitted", "duplicated", "text", "subject", "fact_subject", "project",
                                    "dependency_scope", "subject_scope", "closure_successor", "closure_sources"])
def test_native_value_population_and_payload_are_checked_against_raw_authority(
    session, adopted, monkeypatch, fault,
):
    from corridor import record_projection

    project, adoption = adopted
    reader = record_projection.read_native_record_values

    def tampered(*args, **kwargs):
        values = reader(*args, **kwargs)
        if fault == "omitted":
            return values[1:]
        if fault == "duplicated":
            return (*values, values[0])
        index = next(i for i, value in enumerate(values) if value.fact_type == "station_from")
        row = values[index]
        changes = {"text": {"text_value": "invented accepted station"},
                   "subject": {"subject_key": "another record"},
                   "fact_subject": {"fact_subject_key": "another original source"},
                   "project": {"project_id": project.id + 1},
                   "dependency_scope": {"applies_to_dependency_ids": (999,)},
                   "subject_scope": {"applies_to_subject_keys": ("invented target",)},
                   "closure_successor": {"closure_successor_dependency_id": 999},
                   "closure_sources": {"closure_governing_source_segment_ids": (999,)}}[fault]
        return (*values[:index], replace(row, **changes), *values[index + 1:])

    monkeypatch.setattr(record_projection, "read_native_record_values", tampered)
    observed = surfaces(collect(session, project, adoption))["current_and_as_of_record"]
    assert observed.blockers
    assert not observed.reading.observed_record_kinds
    if fault in {"dependency_scope", "subject_scope", "closure_successor", "closure_sources"}:
        expected_reason = "structured source metadata differs"
    else:
        expected_reason = "population differs" if fault in {"omitted", "duplicated"} else "value or source identity differs"
    assert any(expected_reason in reason for reason in observed.blockers)
