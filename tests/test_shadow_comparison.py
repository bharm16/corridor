"""Frozen changes are compared with a working reference, never declared semantic gold."""

from datetime import datetime, timezone, timedelta

import pytest


def test_comparison_keeps_matches_misses_extra_findings_and_ambiguity_separate():
    from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, freeze_predictions, compare_revisions
    before = datetime(2026, 9, 1, tzinfo=timezone.utc)
    baseline = FrozenRevision("baseline", 1, "a" * 64, before,
        {"UC1": {"need_date": "2026-10-01"}, "UC2": {"need_date": "2026-10-01"},
         "UC3": {"need_date": "2026-10-01"}, "UC4": {"need_date": "2026-10-01"}})
    policy = ComparisonPolicy("comparison-v1", ("need_date",), ("need_date",), "material-seed", 30)
    frozen = freeze_predictions(baseline, policy, [
        Prediction(1, "UC1", "need_date", "2026-11-01", before + timedelta(days=1), "minutes", "source:one"),
        Prediction(2, "UC2", "need_date", "2026-11-01", before + timedelta(days=1), "minutes", "source:two"),
        Prediction(3, "UC4", "need_date", "2026-11-01", before + timedelta(days=1), "minutes", "source:three"),
        Prediction(4, "UC4", "need_date", "2026-12-01", before + timedelta(days=1), "email", "source:four"),
    ], frozen_at=before + timedelta(days=2))
    successor = FrozenRevision("successor", 1, "b" * 64, before + timedelta(days=4),
        {"UC1": {"need_date": "2026-11-01"}, "UC2": {"need_date": "2026-10-01"},
         "UC3": {"need_date": "2026-12-01"}, "UC4": {"need_date": "2026-11-01"}})
    report = compare_revisions(frozen, successor, reference_dataset_id="customer-matrix-working-reference")
    assert [(r["subject"], r["classification"]) for r in report["findings"]] == [
        ("UC1", "matched"), ("UC2", "corridor_only"), ("UC3", "customer_only"), ("UC4", "ambiguous")]
    assert report["findings"][0]["days_earlier"] == 3
    assert report["findings"][2]["cause"] == "unreviewed"
    assert report["strata"]["need_date"]["reference_change_denominator"] == 3
    assert report["strata"]["need_date"]["recall"] is None
    assert report["reference_is_semantic_gold"] is False
    with pytest.raises(ValueError, match="before"):
        compare_revisions(frozen, FrozenRevision("early", 1, "c" * 64, before, {}), reference_dataset_id="working")


def test_material_sample_is_all_up_to_twenty_otherwise_twenty_without_replacement():
    from corridor.shadow_comparison import select_source_sample
    arrivals = [{"id": str(i), "partner": "p", "week": "2026-W36", "source_class": "minutes"} for i in range(25)]
    assert len(select_source_sample(arrivals[:20], seed="declared")["selected_ids"]) == 20
    sample = select_source_sample(arrivals, seed="declared")
    assert len(set(sample["selected_ids"])) == 20
    assert select_source_sample(list(reversed(arrivals)), seed="declared") == sample
    excluded = select_source_sample([{"id": "oral", "partner": "p", "week": "2026-W36", "source_class": "recorded_verbal"}], seed="declared")
    assert excluded["selected_ids"] == [] and excluded["excluded_ids"] == ["oral"]


def test_material_false_write_fails_its_class_even_with_insufficient_sample():
    from corridor.shadow_comparison import ComparisonPolicy, assess_material_sample
    policy = ComparisonPolicy("v1", ("need_date",), ("need_date",), "declared", 30)
    arrivals = [{"id": "one", "partner": "partner", "week": "2026-W36", "source_class": "minutes"}]
    inspections = [{"arrival_id": "one", "reviewer": "local:independent", "resolvers": [],
        "material_cases": [{"id": "case-one", "miss": True, "cause": "date omitted",
            "confirmed_material_automatic_false_write": True, "policy_class": "schedule-date"}]}]
    result = assess_material_sample(arrivals, policy=policy, inspections=inspections)["partners"]["partner"]
    assert result["status"] == "insufficient_evidence"
    assert result["material_miss_rate"] is None
    assert result["material_case_denominator"] == 1
    assert result["failed_policy_classes"] == ["schedule-date"]
    inspections[0]["resolvers"] = ["local:first", "local:coordinator"]
    inspections[0]["reviewer"] = "local:coordinator"
    with pytest.raises(ValueError, match="independent"):
        assess_material_sample(arrivals, policy=policy, inspections=inspections)


def test_freeze_round_trip_preserves_excluded_sources_and_defends_against_input_mutation():
    from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, freeze_predictions
    from corridor.shadow_comparison_cli import prediction_freeze
    moment = datetime(2026, 9, 1, tzinfo=timezone.utc)
    values = {"UC1": {"need_date": "2026-10-01"}}
    baseline = FrozenRevision("baseline", 1, "a" * 64, moment, values)
    policy = ComparisonPolicy("v1", ("need_date",), ("need_date",), "seed", 30)
    frozen = freeze_predictions(baseline, policy, [Prediction(1, "UC1", "need_date", "2026-11-01",
        moment, "recorded_verbal", "origin:one")], frozen_at=moment)
    original = frozen.content_sha256
    values["UC1"]["need_date"] = "changed after freeze"
    assert frozen.content_sha256 == original
    assert prediction_freeze(frozen.payload()).content_sha256 == original
    assert frozen.excluded_delta_ids == (1,)


def test_null_and_missing_fields_are_distinct_reference_changes():
    from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, freeze_predictions, compare_revisions
    time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    baseline = FrozenRevision("baseline", 1, "a" * 64, time, {"UC1": {"need_date": None}})
    policy = ComparisonPolicy("v1", ("need_date",), ("need_date",), "seed", 30)
    frozen = freeze_predictions(baseline, policy, [
        Prediction(1, "UC1", "need_date", None, time, "ucm_revision", "source:one", present=False),
        Prediction(2, "UC2", "need_date", None, time, "ucm_revision", "source:two"),
    ], frozen_at=time)
    successor = FrozenRevision("next", 1, "b" * 64, time + timedelta(days=1), {"UC2": {"need_date": None}})
    report = compare_revisions(frozen, successor, reference_dataset_id="working")
    assert [r["classification"] for r in report["findings"]] == ["matched", "matched"]
    assert [(r["baseline_present"], r["reference_present"]) for r in report["findings"]] == [(True, False), (False, True)]


def test_cli_persists_freeze_then_loads_its_exact_digest_for_comparison(isolated_content_store, tmp_path, monkeypatch, capsys):
    import json
    from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, freeze_predictions
    from corridor.shadow_comparison_cli import main
    from corridor.object_storage import content_store, content_key
    time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    baseline = FrozenRevision("baseline", 1, "a" * 64, time, {"UC1": {"need_date": "October"}})
    frozen = freeze_predictions(baseline, ComparisonPolicy("v1", ("need_date",), ("need_date",), "seed", 30),
        [Prediction(1, "UC1", "need_date", "November", time, "minutes", "segment:1")], frozen_at=time)
    input_path = tmp_path / "predictions.json"
    input_path.write_text(json.dumps(frozen.payload()))
    monkeypatch.setattr("sys.argv", ["shadow-comparison", "freeze", str(input_path)])
    main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["sha256"] == frozen.content_sha256
    successor = tmp_path / "successor.json"
    successor.write_text(json.dumps(FrozenRevision("next", 1, "b" * 64, time + timedelta(days=1),
        {"UC1": {"need_date": "November"}}).payload()))
    monkeypatch.setattr("sys.argv", ["shadow-comparison", "compare", "--freeze-sha256", receipt["sha256"],
        "--successor", str(successor), "--reference-dataset", "working-matrix"])
    main()
    result = json.loads(capsys.readouterr().out)
    report = json.loads(content_store().get(content_key(result["sha256"], ".json"), sha256=result["sha256"]))
    assert report["prediction_freeze_sha256"] == receipt["sha256"]
    assert report["findings"][0]["classification"] == "matched"
