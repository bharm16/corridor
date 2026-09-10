"""Two-stage content-addressed comparison artifacts for #499's offline engine."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

from corridor.db_roles import WORKER_CAPABILITY_LOGIN
from corridor.object_storage import content_key, content_store, store_bytes
from corridor.shadow_comparison import (
    ComparisonPolicy, FrozenRevision, Prediction, compare_revisions, freeze_predictions,
)


def revision(payload):
    return FrozenRevision(**{**payload, "seen_at": datetime.fromisoformat(payload["seen_at"])})


def prediction_freeze(payload):
    fields = ("identity", "fields", "material_fields", "sampling_seed", "minimum_material_cases")
    policy = ComparisonPolicy(**{key: payload["policy"][key] for key in fields})
    if any(key in payload["policy"] and payload["policy"][key] != value
           for key, value in policy.payload().items() if key not in fields):
        raise ValueError("stored policy changes the engine's matching/denominator rules")
    return freeze_predictions(revision(payload["baseline"]), policy,
        [Prediction(**{**item, "created_at": datetime.fromisoformat(item["created_at"])}) for item in payload["predictions"]],
        frozen_at=datetime.fromisoformat(payload["frozen_at"]))


def persist(payload):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = sha256(body).hexdigest()
    path = store_bytes(body, sha256=digest, suffix=".json")
    return {"sha256": digest, "path": str(path)}


def native_comparison(args):
    """Read an explicitly selected worker DB and retain exact native freeze bytes."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from corridor.shadow_measurement import compare_shadow_runs
    from corridor.shadow_receipts import read_shadow_run
    from corridor.shadow_reconciliation import retain_private_artifact

    database_url = os.environ.get(args.database_url_env)
    if not database_url:
        raise ValueError("the explicit worker database environment variable is not set")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql" or parsed.username != WORKER_CAPABILITY_LOGIN:
        raise ValueError("native comparison requires an explicit PostgreSQL corridor_worker URL")
    policy_payload = json.loads(args.policy.read_text())
    policy = ComparisonPolicy(**policy_payload)
    engine = create_engine(parsed)
    try:
        with Session(engine) as session:
            comparison = compare_shadow_runs(session, prediction_identity=args.prediction_run,
                reference_identity=args.reference_run, policy=policy, reference_dataset_id=args.reference_dataset)
            exports = {}
            for name, identity in (("prediction", args.prediction_run), ("reference", args.reference_run)):
                receipt = read_shadow_run(session, identity)
                exports[name] = {"canonicalization": "postgresql-jsonb-text-v1", "identity": identity,
                    "payload_text": receipt.payload_text, "output_sha256": receipt.output_sha256}
            session.rollback()
    finally:
        engine.dispose()
    # Native customer evidence stays in an explicitly selected private local
    # directory; this path does not inherit the configured object-store backend.
    frozen_receipts = {name: retain_private_artifact(args.output_dir, value) for name, value in exports.items()}
    return {"comparison": retain_private_artifact(args.output_dir, comparison),
            "native_freeze_receipts": frozen_receipts, "policy": retain_private_artifact(args.output_dir, policy_payload)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("input", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("--freeze-sha256", required=True)
    compare.add_argument("--successor", required=True, type=Path)
    compare.add_argument("--reference-dataset", required=True)
    native = sub.add_parser("native", help="compare two database-sealed native UCM captures")
    native.add_argument("--database-url-env", required=True, help="environment variable containing the explicit corridor_worker URL")
    native.add_argument("--prediction-run", required=True)
    native.add_argument("--reference-run", required=True)
    native.add_argument("--policy", type=Path, required=True)
    native.add_argument("--reference-dataset", required=True)
    native.add_argument("--output-dir", type=Path, required=True)
    review = sub.add_parser("review", help="retain one attributable human analytical review")
    review.add_argument("--comparison", type=Path, required=True)
    review.add_argument("--note", type=Path, required=True)
    review.add_argument("--previous-review", type=Path)
    review.add_argument("--output-dir", type=Path, required=True)
    reconcile = sub.add_parser("reconcile", help="reconcile complete append-only review chains")
    reconcile.add_argument("--comparison", type=Path, required=True)
    reconcile.add_argument("--reviews", type=Path, nargs="*", default=[])
    reconcile.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "freeze":
        frozen = prediction_freeze(json.loads(args.input.read_text()))
        result = persist(frozen.payload())
    elif args.command == "compare":
        payload = json.loads(content_store().get(content_key(args.freeze_sha256, ".json"), sha256=args.freeze_sha256))
        frozen = prediction_freeze(payload)
        if frozen.content_sha256 != args.freeze_sha256:
            raise ValueError("prediction freeze does not match its retained identity")
        result = persist(compare_revisions(frozen, revision(json.loads(args.successor.read_text())),
                                           reference_dataset_id=args.reference_dataset))
    elif args.command == "native":
        result = native_comparison(args)
    else:
        from corridor.shadow_reconciliation import record_finding_review, reconcile_findings, retain_private_artifact
        comparison = json.loads(args.comparison.read_text())
        original = retain_private_artifact(args.output_dir, comparison)
        if args.command == "review":
            note = json.loads(args.note.read_text())
            previous = json.loads(args.previous_review.read_text()) if args.previous_review else None
            receipt = record_finding_review(comparison, note, previous=previous)
            retained_note = retain_private_artifact(args.output_dir, note)
            retained_previous = retain_private_artifact(args.output_dir, previous) if previous else None
            result = {"review": retain_private_artifact(args.output_dir, receipt), "original_comparison": original,
                      "original_note": retained_note, "previous_review": retained_previous}
        else:
            reviews = [json.loads(path.read_text()) for path in args.reviews]
            reconciliation = reconcile_findings(comparison, reviews)
            retained_reviews = [retain_private_artifact(args.output_dir, receipt) for receipt in reviews]
            result = {"reconciliation": retain_private_artifact(args.output_dir, reconciliation),
                      "original_comparison": original, "review_receipts": retained_reviews}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    main()
