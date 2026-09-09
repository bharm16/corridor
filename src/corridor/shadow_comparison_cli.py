"""Two-stage content-addressed comparison artifacts for #499's offline engine."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("input", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("--freeze-sha256", required=True)
    compare.add_argument("--successor", required=True, type=Path)
    compare.add_argument("--reference-dataset", required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        frozen = prediction_freeze(json.loads(args.input.read_text()))
        result = persist(frozen.payload())
    else:
        payload = json.loads(content_store().get(content_key(args.freeze_sha256, ".json"), sha256=args.freeze_sha256))
        frozen = prediction_freeze(payload)
        if frozen.content_sha256 != args.freeze_sha256:
            raise ValueError("prediction freeze does not match its retained identity")
        result = persist(compare_revisions(frozen, revision(json.loads(args.successor.read_text())),
                                           reference_dataset_id=args.reference_dataset))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
