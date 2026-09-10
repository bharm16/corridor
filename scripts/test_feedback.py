"""Validate required-CI timing receipts and assess the feedback targets.

The strict diagnostic CLI verdict: `aggregate` builds one report from a
directory of shard receipts and `assess` judges it against the checked-in
policy and a history directory. The definitions live in `scripts/test_gate`
(ADR-0097 makes elapsed-time breaches advisory in the merge gate; this command
still returns them as failures for a local diagnosis). No database or
third-party package is needed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test_gate.evidence import EvidenceError, read_json
from scripts.test_gate.feedback import aggregate_receipts, assess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--receipts", type=Path, required=True)
    aggregate.add_argument("--expected", type=Path, required=True)
    aggregate.add_argument("--gate-seconds", type=float, required=True)
    aggregate.add_argument("--queue-seconds", type=float)
    aggregate.add_argument("--setup-seconds", type=float)
    aggregate.add_argument("--output", type=Path, required=True)
    assessment = commands.add_parser("assess")
    assessment.add_argument("--current", type=Path, required=True)
    assessment.add_argument("--history", type=Path, required=True)
    assessment.add_argument("--policy", type=Path, required=True)
    assessment.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        # A failed retry must not leave an older successful report available
        # to an always-running artifact upload step.
        if arguments.output is not None:
            arguments.output.unlink(missing_ok=True)
        if arguments.command == "aggregate":
            if not arguments.receipts.is_dir():
                raise EvidenceError("receipt directory is absent")
            result = aggregate_receipts(
                [read_json(path) for path in sorted(arguments.receipts.rglob("*.json"))],
                read_json(arguments.expected), arguments.gate_seconds,
                {name: getattr(arguments, name) for name in ("queue_seconds", "setup_seconds")
                 if getattr(arguments, name) is not None},
            )
        else:
            if not arguments.history.is_dir():
                raise EvidenceError("history directory is absent; create it explicitly for bootstrap")
            result = assess(
                read_json(arguments.current),
                [read_json(path) for path in sorted(arguments.history.rglob("*.json"))],
                read_json(arguments.policy),
            )
        rendered = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if arguments.output is not None:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if result.get("passed", True) else 1
    except (EvidenceError, OSError, ValueError, TypeError, KeyError) as error:
        print(f"invalid timing evidence: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
