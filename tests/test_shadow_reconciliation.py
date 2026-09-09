"""Human comparison review preserves raw evidence, chains and unresolved rates."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import stat

import pytest

from corridor.shadow_comparison import ComparisonPolicy, FrozenRevision, Prediction, compare_revisions, freeze_predictions
from corridor.shadow_comparison_cli import main
from corridor.shadow_reconciliation import artifact_digest, record_finding_review, reconcile_findings, retain_private_artifact


AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def comparison():
    baseline = FrozenRevision("baseline", 1, "a"*64, AT, {key: {"size": "12"} for key in ("one", "two", "three", "four")})
    policy = ComparisonPolicy("declared", ("size",), ("size",), "seed", 30)
    predictions = [Prediction(index, key, "size", "18", AT, "ucm_revision", f"segment:{index}")
                   for index, key in enumerate(("one", "three", "four"), 1)]
    frozen = freeze_predictions(baseline, policy, predictions, frozen_at=AT)
    successor = FrozenRevision("later", 1, "b"*64, AT+timedelta(days=1),
                               {"one": {"size": "18"}, "two": {"size": "18"}, "three": {"size": "12"}, "four": {"size": "20"}})
    return compare_revisions(frozen, successor, reference_dataset_id="synthetic-working-reference")


def note(report, subject, classification, *, day=2, review_id=None):
    finding = next(f for f in report["findings"] if f["subject"] == subject)
    return {"review_id": review_id or f"review:{subject}:{day}", "comparison_sha256": artifact_digest(report),
        "finding_sha256": artifact_digest(finding), "subject": subject, "field": "size", "reviewer": "local:alice",
        "reviewed_at": (AT+timedelta(days=day)).isoformat(), "classification": classification,
        "reason": "Independent inspection of the retained source cells", "evidence_references": [f"review-notes:{subject}"],
        "cause": "source change absent from correct proposal" if classification == "customer_only" else None,
        "handling_outcome": "coordinator retained current value" if classification == "corridor_only" else None}


def test_human_causes_outcomes_and_resolution_keep_originals_and_unresolved_denominators():
    report = comparison()
    original = deepcopy(report)
    assert reconcile_findings(report, [])["reviewed_strata"]["size"]["precision"] is None
    first = record_finding_review(report, note(report, "two", "customer_only"))
    second = record_finding_review(report, note(report, "three", "corridor_only"))
    incomplete = reconcile_findings(report, [first, second])
    assert incomplete["unresolved_finding_count"] == 1
    assert incomplete["reviewed_strata"]["size"]["recall"] is None
    third = record_finding_review(report, note(report, "four", "customer_only"))
    complete = reconcile_findings(report, [first, second, third])
    assert complete["unresolved_finding_count"] == 0
    assert complete["reviewed_strata"]["size"]["precision"] == pytest.approx(1/3)
    assert complete["reviewed_strata"]["size"]["recall"] == pytest.approx(1/3)
    assert complete["working_reference_strata"]["size"]["precision"] is None
    assert complete["original_comparison"] == original == report
    assert first["cause"] and second["handling_outcome"]


def test_review_correction_requires_complete_unbranched_predecessor_chain():
    report = comparison()
    first = record_finding_review(report, note(report, "four", "customer_only"))
    corrected = record_finding_review(report, note(report, "four", "matched", day=3), previous=first)
    with pytest.raises(ValueError, match="missing its retained predecessor"):
        reconcile_findings(report, [corrected])
    result = reconcile_findings(report, [corrected, first, first])
    finding = next(f for f in result["findings"] if f["original_finding"]["subject"] == "four")
    assert finding["review_receipts"] == [first, corrected]
    assert finding["original_finding"]["classification"] == "ambiguous"
    assert finding["reviewed_classification"] == "matched"
    competing = record_finding_review(report, note(report, "four", "ambiguous", day=4), previous=first)
    with pytest.raises(ValueError, match="conflicting review corrections"):
        reconcile_findings(report, [first, corrected, competing])


@pytest.mark.parametrize("field,value,reason", [("comparison_sha256", "f"*64, "exact original"),
    ("cause", None, "cause"), ("evidence_references", [], "retained evidence"), ("reviewer", "reviewer", "namespaced")])
def test_review_refuses_missing_or_wrong_evidence(field, value, reason):
    report = comparison()
    proposed = note(report, "two", "customer_only")
    proposed[field] = value
    with pytest.raises(ValueError, match=reason):
        record_finding_review(report, proposed)


def test_review_cli_retains_private_immutable_originals_and_receipts(tmp_path, capsys):
    report = comparison()
    original = tmp_path/"comparison.json"
    original.write_text(json.dumps(report))
    proposed = tmp_path/"human-note.json"
    proposed.write_text(json.dumps(note(report, "two", "customer_only")))
    output = tmp_path/"private"
    assert main(["review", "--comparison", str(original), "--note", str(proposed), "--output-dir", str(output)]) == 0
    saved = json.loads(capsys.readouterr().out)
    review_path = saved["review"]["path"]
    assert main(["reconcile", "--comparison", str(original), "--reviews", review_path, "--output-dir", str(output)]) == 0
    joined = json.loads(capsys.readouterr().out)
    from pathlib import Path
    assert json.loads(Path(joined["reconciliation"]["path"]).read_text())["unresolved_finding_count"] == 2
    for path in output.glob("*.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    retained = retain_private_artifact(output, report)
    assert retained == saved["original_comparison"]
    before = Path(review_path).read_bytes()
    retain_private_artifact(output, report)
    assert Path(review_path).read_bytes() == before


def test_native_cli_requires_explicit_worker_url_before_connecting(tmp_path, monkeypatch):
    policy = tmp_path/"policy.json"
    policy.write_text('{}')
    monkeypatch.setenv("SYNTHETIC_SHADOW_DB", "postgresql+psycopg://owner:unused@localhost:5433/unused")
    with pytest.raises(ValueError, match="corridor_worker URL"):
        main(["native", "--database-url-env", "SYNTHETIC_SHADOW_DB", "--prediction-run", "a"*64,
              "--reference-run", "b"*64, "--policy", str(policy), "--reference-dataset", "fixture", "--output-dir", str(tmp_path/"out")])
    assert not (tmp_path/"out").exists()
