"""Work List coverage corroborates every public Follow-up Plan value."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from corridor.native_reader_coverage import collect_native_reader_coverage
from test_native_accepted_readers import _follow_up_plan
from test_native_reader_coverage import adopted as _adopted_fixture, surfaces

# Reuse the collector's adopted-project fixture; the session fixture is shared.
adopted = _adopted_fixture


def _work_list(session, project, revision_id):
    result = collect_native_reader_coverage(session, project.id, as_of_revision_id=revision_id,
        evaluated_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    return surfaces(result)["work_list"]


def test_complete_public_follow_up_plan_has_verified_native_work_coverage(session, adopted):
    project, adoption = adopted
    saved, delta, _, _ = _follow_up_plan(session, project, adoption)
    work = _work_list(session, project, saved.revision_id)
    assert "constraint_work" in work.reading.observed_record_kinds, work.blockers
    assert "proposed_delta" in work.reading.observed_record_kinds, work.blockers
    record = next(row for row in work.reading.records if row.kind == "constraint_work")
    assert record.fields["identity"]["delta_id"] == delta.id
    assert record.fields["assignment"] == {"person": "local:utility-coordinator", "organization": "City Water"}


@pytest.mark.parametrize("field", [
    "responsible_principal", "responsible_organization", "return_date", "recorded_at", "target_subject_identity",
    "open_question", "recorded_by", "revision_id", "delta_id", "support_assessment_ids", "source_segment_ids",
])
def test_modified_public_follow_up_plan_cannot_claim_native_work_coverage(session, adopted, monkeypatch, field):
    import corridor.packet_review as packet_review
    project, adoption = adopted
    saved, _, _, _ = _follow_up_plan(session, project, adoption)
    original_reader = packet_review.read_review_items
    at = datetime.now(timezone.utc) + timedelta(seconds=1)
    original = original_reader(session, project_id=project.id, as_of=at)
    assert len(original.follow_up_plans) == 1
    original_plan = original.follow_up_plans[0]
    value = getattr(original_plan, field)
    if field in {"return_date", "recorded_at"}:
        replacement = value + timedelta(days=1)
    elif field in {"revision_id", "delta_id"}:
        replacement = value + 10000
    elif field in {"support_assessment_ids", "source_segment_ids"}:
        replacement = ()
    else:
        replacement = "altered-public-value"

    def altered_reader(*args, **kwargs):
        reading = original_reader(*args, **kwargs)
        return replace(reading, items=tuple(replace(item, children=tuple(
            replace(child, follow_up_plans=tuple(replace(plan, **{field: replacement})
                for plan in child.follow_up_plans)) for child in item.children)) for item in reading.items))

    monkeypatch.setattr(packet_review, "read_review_items", altered_reader)
    work = _work_list(session, project, saved.revision_id)
    assert any("differs from its complete native act/target/evidence" in blocker for blocker in work.blockers)
    assert "constraint_work" not in work.reading.observed_record_kinds
    assert "proposed_delta" not in work.reading.observed_record_kinds
    assert not any(row.kind == "constraint_work" for row in work.reading.records)
    # Raw reader output remains diagnostic; forged values never become records
    # carrying a native Follow-up Plan origin on the comparison surface.
    assert all(not row.fields["assignment"] and not row.fields["next_action"]
        for row in work.reading.records if row.kind == "proposed_delta")
