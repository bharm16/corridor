"""Native comparison consumes real sealed captures, not synthetic callback output."""

from copy import deepcopy

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.compatibility_intake import run_compatibility_intake
from corridor.field_mapping_manifest import DEMO_EXTERNAL_REFERENCES, MappingDeclaration
from corridor.models import Project
from corridor.push_intake import PushCredential, PushPayload, accept_delivery, bind_credential
from corridor.source_intake import validate_and_stage
from corridor.shadow_comparison import ComparisonPolicy
from corridor.shadow_measurement import NativeShadowComparisonRefused, compare_shadow_runs
from corridor.shadow_processing import run_shadow_ucm
from test_shadow_processing import shadow, authorization, NOW, DELETE
from later_revision_support import BASELINE_ROWS, CUSTOMER, PRINCIPAL, deliver, workbook_bytes


POLICY = ComparisonPolicy("synthetic-native-v1", ("utility_id", "size"), ("utility_id",), "fixture-seed", 30)


def capture(session, project_id, staged, envelope, approved, compatibility, *, complete=True):
    return run_shadow_ucm(session, project=session.get(Project, project_id), staged=staged,
        envelope=envelope, compatibility_receipt=compatibility, authorization=approved,
        customer=CUSTOMER, environment="synthetic-shadow", source_configuration="manual-ucm-v1",
        principal=PRINCIPAL, deletion_date=DELETE, now=NOW,
        is_complete_enumerative_source=complete, row_accounting_sealed=complete)


def delivery(database, project_id, tmp_path, rows, label):
    body = workbook_bytes(tmp_path/f"{label}.xlsx", rows)
    with database.session_factory.begin() as owner:
        project = owner.get(Project, project_id)
        slug = project.slug
        binding = bind_credential(owner, PushCredential("webhook", f"secret-{project.slug}"))
        received = accept_delivery(owner, binding, PushPayload(body=body, filename=f"{label}.xlsx", transport_delivery_id=label))
        staged, envelope = validate_and_stage(body, f"{label}.xlsx"), received.envelope
    approved = authorization(body, slug)
    compatibility = run_compatibility_intake(body, f"{label}.xlsx", authorization=approved,
        customer=CUSTOMER, project=slug, operator=PRINCIPAL.subject, environment="synthetic-compatibility",
        deletion_date=DELETE, external_references=MappingDeclaration(external_references=DEMO_EXTERNAL_REFERENCES).external_reference_headings)
    return staged, envelope, approved, compatibility.receipt


def test_native_adapter_compares_frozen_predictions_with_later_complete_source(shadow, tmp_path, monkeypatch, capsys):
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        first = capture(worker, project_id, staged, envelope, approved, compatibility)
    rows = deepcopy(BASELINE_ROWS)
    rows[0][3] = "18 in"
    rows[1][3] = "10 in"
    successor = delivery(database, project_id, tmp_path, rows, "native-reference")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        second = capture(worker, project_id, *successor)
    with Session(engines["corridor_worker"]) as worker:
        before = worker.scalar(text("select count(*) from project_record_revisions"))
        result = compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity=second["identity"],
                                     policy=POLICY, reference_dataset_id="synthetic-working-reference")
        assert result == compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity=second["identity"],
                                            policy=POLICY, reference_dataset_id="synthetic-working-reference")
        assert sorted(f["classification"] for f in result["findings"]) == ["customer_only", "matched"]
        matched = next(f for f in result["findings"] if f["classification"] == "matched")
        assert matched["predictions"][0]["delta_id"] == first["deltas"][0]["id"]
        assert matched["predictions"][0]["source_reference"].startswith(f"document:{first['document_id']}/fact:")
        assert result["native_receipts"]["reference_delivery_id"] > result["native_receipts"]["prediction_delivery_watermark"]
        assert result["reference_is_semantic_gold"] is False
        assert worker.scalar(text("select count(*) from project_record_revisions")) == before
        assert not worker.new and not worker.dirty and not worker.deleted
    import json
    import stat
    from pathlib import Path
    from corridor.shadow_comparison_cli import main
    policy_path = tmp_path/"comparison-policy.json"
    policy_path.write_text(json.dumps({key: POLICY.payload()[key] for key in
        ("identity", "fields", "material_fields", "sampling_seed", "minimum_material_cases")}))
    monkeypatch.setenv("SYNTHETIC_SHADOW_DATABASE_URL", engines["corridor_worker"].url.render_as_string(hide_password=False))
    capsys.readouterr()
    assert main(["native", "--database-url-env", "SYNTHETIC_SHADOW_DATABASE_URL", "--prediction-run", first["identity"],
        "--reference-run", second["identity"], "--policy", str(policy_path), "--reference-dataset", "synthetic-working-reference",
        "--output-dir", str(tmp_path/"native-measurement")]) == 0
    artifact = json.loads(capsys.readouterr().out)
    assert json.loads(Path(artifact["comparison"]["path"]).read_text()) == result
    for exported in artifact["native_freeze_receipts"].values():
        path = Path(exported["path"])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        frozen = json.loads(path.read_text())
        assert frozen["canonicalization"] == "postgresql-jsonb-text-v1"
        assert json.loads(frozen["payload_text"])["identity"] in {first["identity"], second["identity"]}


@pytest.mark.parametrize("already_delivered,complete", [(True, True), (False, False)])
def test_native_adapter_refuses_previsible_or_partial_reference(shadow, tmp_path, already_delivered, complete):
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    rows = deepcopy(BASELINE_ROWS)
    rows[0][3] = "20 in"
    later = delivery(database, project_id, tmp_path, rows, "reference") if already_delivered else None
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        first = capture(worker, project_id, staged, envelope, approved, compatibility)
    if later is None:
        later = delivery(database, project_id, tmp_path, rows, "reference")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        second = capture(worker, project_id, *later, complete=complete)
    with Session(engines["corridor_worker"]) as worker:
        with pytest.raises(NativeShadowComparisonRefused, match="already observed|complete enumerative"):
            compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity=second["identity"],
                                policy=POLICY, reference_dataset_id="synthetic-reference")


def test_native_new_subject_and_explicit_removal_expand_without_rekeying(shadow, tmp_path):
    database, engines, project_id, *_ = shadow
    rows = deepcopy(BASELINE_ROWS)
    removed = rows.pop(1)
    new = deepcopy(removed)
    new[0], new[12] = "UC-4", "UCM-1004"
    rows.append(new)
    predicted = delivery(database, project_id, tmp_path, rows, "add-remove-prediction")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        first = capture(worker, project_id, *predicted)
    # Different source bytes, same selected fields; body comment is outside
    # this predeclared utility_id/size comparison.
    rows[0][11] = "later working reference"
    successor = delivery(database, project_id, tmp_path, rows, "add-remove-reference")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        second = capture(worker, project_id, *successor)
    with Session(engines["corridor_worker"]) as worker:
        result = compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity=second["identity"],
                                    policy=POLICY, reference_dataset_id="synthetic-reference")
        assert len(result["findings"]) == 4
        assert all(f["classification"] == "matched" for f in result["findings"])
        new_delta = next(d for d in first["deltas"] if d["target_type"] == "proposed_subject")
        removal = next(d for d in first["deltas"] if d["change_type"] == "apparent_removal")
        assert {f["subject"] for f in result["findings"]} == {new_delta["target_subject_identity"], removal["target_subject_identity"]}
        assert {p["delta_id"] for f in result["findings"] for p in f["predictions"]} == {new_delta["id"], removal["id"]}
        assert sum(f["reference_present"] is False for f in result["findings"]) == 2


def test_native_comparison_refuses_unmapped_policy_field(shadow, tmp_path):
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        first = capture(worker, project_id, staged, envelope, approved, compatibility)
    policy = ComparisonPolicy("unsupported", ("closure_result",), ("closure_result",), "seed", 30)
    with Session(engines["corridor_worker"]) as worker:
        with pytest.raises(NativeShadowComparisonRefused, match="registered scalar mapping"):
            compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity="f"*64,
                                policy=policy, reference_dataset_id="synthetic-reference")


def test_reference_removal_retains_subject_inventory_when_selected_baseline_field_is_blank(shadow, tmp_path):
    database, engines, project_id, *_ = shadow
    rows = deepcopy(BASELINE_ROWS[:-1])  # UC-3 has no Action Due Date in the adopted workbook.
    predicted = delivery(database, project_id, tmp_path, rows, "blank-field-removal")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        first = capture(worker, project_id, *predicted)
    rows[0][11] = "later source observation"
    later = delivery(database, project_id, tmp_path, rows, "blank-field-reference")
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        second = capture(worker, project_id, *later)
    policy = ComparisonPolicy("dates-only", ("action_due_date",), ("action_due_date",), "seed", 30)
    with Session(engines["corridor_worker"]) as worker:
        result = compare_shadow_runs(worker, prediction_identity=first["identity"], reference_identity=second["identity"],
                                    policy=policy, reference_dataset_id="synthetic-reference")
        removed = next(d["target_subject_identity"] for d in first["deltas"] if d["change_type"] == "apparent_removal")
        assert result["baseline"]["values"][removed] == {}
        assert result["findings"] == []
