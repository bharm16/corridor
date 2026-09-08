"""Explicit maintenance commands and offline full-chain shadow receipts (#447).

The shadow command creates a guarded disposable database and uses only retained
answers. All other commands are deliberate maintenance operations on the chosen
database; selection is never a side effect of measuring or recording a gate.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from corridor.pipeline_contracts import (
    MeasuredEvidence, ObservationPlan, PipelineScope, QualificationPolicy, canonical_text,
)
from corridor.principals import HumanPrincipal


def _write_new(path: Path, value: dict):
    with path.open("x") as output:
        output.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def _retained_client(case, historical):
    from corridor.native_pipeline import RecordedPipelineClient
    return RecordedPipelineClient([{
        "system_sha256": historical["prompt_sha256"], "schema_sha256": historical["schema_sha256"],
        "user": page["listing"], "image_sha256s": [case.image_digests[page["number"]]],
        "answer": page["structure"],
    } for page in case.document["pages"]])


def _shadow(args) -> int:
    from corridor.m8_acceptance_database import provision_disposable_postgres
    from corridor.models import Document, PipelineConfiguration, Project
    from corridor.native_matrix_measurement import (
        DATASET_PATH, REPO_ROOT, compare_mapping_diagnostics, compare_required_rows,
        compare_retained_reading, load_case, verify_measured_configuration,
    )
    from corridor.native_pipeline import run_native_matrix_shadow
    from corridor.pipeline_qualification import pipeline_receipt, record_qualification, record_repeatability

    admin_url = os.environ.get(args.postgres_admin_url_env, "")
    if not admin_url:
        raise ValueError("the named local PostgreSQL administration variable is missing")
    if args.output.exists():
        raise ValueError("shadow output must be a new directory; prior receipts are immutable")
    dataset_bytes = DATASET_PATH.read_bytes()
    dataset = json.loads(dataset_bytes)
    historical = dataset["historical_configuration"]
    verify_measured_configuration(historical)
    cases = [load_case(item, results_root=args.results_root) for item in dataset["cases"]]
    args.output.mkdir(parents=True)
    scope = PipelineScope(
        deployment="offline-retained-matrix-qualification",
        source_sha256s=tuple(sorted(case.specification["source_sha256"] for case in cases)),
        corpus_version="native-matrix-retained-v1", corpus_sha256=sha256(dataset_bytes).hexdigest(),
    )
    outcomes, observation_ids, repeatability_ids = [], [], []
    actor = args.actor
    with provision_disposable_postgres(
        admin_url, repo_root=REPO_ROOT, error_cls=RuntimeError,
        database_prefix="corridor_pipeline_shadow_",
    ) as database:
        database_record = {"postgres_version": database.postgres_version,
                           "migration_head": database.migration_head, "disposable": True}
        with database.session_factory() as session:
            project = Project(slug="native-pipeline-retained", name="Historical retained-answer pipeline replay", is_synthetic=False)
            session.add(project)
            session.flush()
            for case in cases:
                document = Document(project_id=project.id, sha256=case.specification["source_sha256"],
                    filename=case.source.name, doc_type="matrix", pages=len(case.document["pages"]),
                    parse_status="parsed", numbering_scheme="project-unique")
                session.add(document)
                session.flush()
                pair = []
                for attempt in (1, 2):
                    client = _retained_client(case, historical)
                    plan = ObservationPlan(mode="retained_replay", origin_sha256=client.origin_sha256,
                        source_permission="public", description=f"Historical retained answers: {case.name}; no fresh provider observation")
                    result = run_native_matrix_shadow(
                        session, document, source_path=case.source, scope=scope, client=client, plan=plan,
                        output_dir=args.output / f"{case.name}-{attempt}", document_label=case.source.name,
                    )
                    pair.append(result)
                    _write_new(args.output / f"{case.name}-{attempt}.observation.json", pipeline_receipt(result.observation))
                repeated = record_repeatability(session, pair[0].observation.id, pair[1].observation.id, actor=actor)
                observation_ids.append(pair[0].observation.id)
                repeatability_ids.append(repeated.id)
                result_record = {"name": case.name, "source_sha256": document.sha256,
                                 "disposition": pipeline_receipt(pair[0].observation)["disposition"],
                                 "repeatability": pipeline_receipt(repeated),
                                 "observation_sha256s": [result.observation.receipt_sha256 for result in pair]}
                if pair[0].extraction is not None:
                    pages = pair[0].extraction.mapping.pages
                    result_record.update(
                        retained_required_rows=compare_required_rows(case.document, pages),
                        retained_full_reading=compare_retained_reading(case.document, pages),
                        retained_diagnostics=compare_mapping_diagnostics(case.document, pages),
                    )
                outcomes.append(result_record)
                print(f"{case.name}: {pipeline_receipt(pair[0].observation)['disposition']}; repeatability={pipeline_receipt(repeated)['passed']}", flush=True)
            # No predeclared current-policy or independent field gold is
            # manufactured from the cached answers. The concrete result is
            # deliberately incomplete, with every missing quantity retained.
            gate = record_qualification(session, observation_ids=observation_ids,
                repeatability_ids=repeatability_ids, actor=actor)
            gate_record = pipeline_receipt(gate)
            configuration = session.get_one(PipelineConfiguration, gate.configuration_sha256)
            _write_new(args.output / "configuration.json", json.loads(configuration.configuration_text))
            session.commit()
    receipt = {
        "schema_version": "corridor.pipeline-shadow-measurement.v1",
        "mode": "historical_retained_answer_full_chain_replay", "scope": scope.model_dump(mode="json"),
        "database": {**database_record, "dropped_after_measurement": True}, "documents": outcomes,
        "qualification": gate_record, "production_selection_executed": False,
        "new_provider_calls": 0, "new_provider_cost_usd": 0, "total_processing_cost_usd": None,
        "completed_documents": sum(item["disposition"] == "completed" for item in outcomes),
        "repeatability_pass": all(item["repeatability"]["passed"] for item in outcomes),
        "retained_required_rows_pass": all(item.get("retained_required_rows", {}).get("pass", False) for item in outcomes),
        "full_reading_parity": all(item.get("retained_full_reading", {}).get("pass", False) for item in outcomes),
        "historical_limits": dataset["limitations"],
        "quality_limit": "Required fields/refusals/diagnostics are retained-answer regression, not independent field gold or fresh model quality; spent 9540 stays spent.",
    }
    _write_new(args.output / "receipt.json", receipt)
    print(f"Retained full-chain measurement saved; qualification={gate_record['status']}: {args.output / 'receipt.json'}")
    # An incomplete qualification is the expected honest result. Process
    # success means the measurement completed, never that selection passed.
    return 0 if receipt["repeatability_pass"] and receipt["retained_required_rows_pass"] else 1


def _maintenance(args, session_factory) -> int:
    from corridor.pipeline_qualification import (
        pipeline_receipt, record_quality, record_qualification, record_repeatability,
        register_qualification_policy, select_qualified_pipeline,
    )
    if session_factory is None:
        raise ValueError("maintenance commands require the explicit make pipeline-qualification adapter")
    actor = HumanPrincipal(args.actor) if args.command in {"policy", "select"} else args.actor
    with session_factory() as session, session.begin():
        if args.command == "policy":
            policy = QualificationPolicy.model_validate_json(args.policy.read_text())
            row = register_qualification_policy(session, policy, actor=actor, contract_paths=args.contract)
            result = {"policy_sha256": row.policy_sha256, "scope_sha256": row.scope_sha256, "actor": row.actor}
        elif args.command == "repeatability":
            result = pipeline_receipt(record_repeatability(session, args.left, args.right, actor=actor))
        elif args.command == "quality":
            result = pipeline_receipt(record_quality(session, args.observation, reference_path=args.reference, actor=actor))
        elif args.command == "gate":
            evidence = []
            for path in args.evidence:
                item = json.loads(path.read_bytes())
                artifact_path = (path.parent / item["artifact_path"]).resolve()
                evidence.append((MeasuredEvidence.model_validate(item["measurement"]), artifact_path))
            result = pipeline_receipt(record_qualification(session,
                observation_ids=args.observation, repeatability_ids=args.repeatability,
                quality_ids=args.quality, evidence=evidence, actor=actor))
        else:
            result = pipeline_receipt(select_qualified_pipeline(session, args.qualification,
                actor=actor, reason=args.reason, expected_selection_id=args.expected_selection,
                enabled=not args.disable))
        print(canonical_text(result))
    return 0


def main(argv=None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = commands.add_parser("shadow", help="historical retained-answer full-chain replay; disposable database only")
    shadow.add_argument("--output", type=Path, required=True)
    shadow.add_argument("--postgres-admin-url-env", required=True)
    shadow.add_argument("--results-root", type=Path)
    shadow.add_argument("--actor", required=True, help="explicit measurement service/operator identity; not an approval")
    policy = commands.add_parser("policy", help="register metric contracts before observing a scope")
    policy.add_argument("--policy", type=Path, required=True)
    policy.add_argument("--contract", type=Path, action="append", required=True)
    repeated = commands.add_parser("repeatability")
    repeated.add_argument("--left", type=int, required=True)
    repeated.add_argument("--right", type=int, required=True)
    quality = commands.add_parser("quality")
    quality.add_argument("--observation", type=int, required=True)
    quality.add_argument("--reference", type=Path, required=True)
    gate = commands.add_parser("gate")
    gate.add_argument("--observation", type=int, action="append", required=True)
    gate.add_argument("--repeatability", type=int, action="append", default=[])
    gate.add_argument("--quality", type=int, action="append", default=[])
    gate.add_argument("--evidence", type=Path, action="append", default=[])
    selection = commands.add_parser("select", help="explicit maintainer selection; never implied by a passing gate")
    selection.add_argument("--qualification", type=int, required=True)
    predecessor = selection.add_mutually_exclusive_group(required=True)
    predecessor.add_argument("--expected-selection", type=int)
    predecessor.add_argument("--initial", action="store_true")
    selection.add_argument("--reason", required=True)
    selection.add_argument("--disable", action="store_true", help="append explicit rollback to incumbent routing")
    for command in (policy, repeated, quality, gate, selection):
        command.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    return _shadow(args) if args.command == "shadow" else _maintenance(args, session_factory)


if __name__ == "__main__":
    sys.exit(main())
