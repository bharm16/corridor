"""Generate reproducible pilot economics and written checkpoint artifacts.

The CLI consumes private frozen measurement and evidence files. It never reads
customer accounts or changes pilot activation, the Project Record, or the
success-contract document; the written checkpoint records that human decision.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile

from corridor.pilot_measurement_cli import write_private_json
from corridor.pilot_report import derive_report
from corridor.pilot_checkpoint import build_checkpoint, render_checkpoint


def _write_private_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="derive #424 economics from #532 analytical records")
    report.add_argument("--measurement", type=Path, required=True)
    report.add_argument("--contract", type=Path, required=True)
    report.add_argument("--evidence", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    checkpoint = sub.add_parser("checkpoint", help="derive #498 checkpoint JSON and Markdown")
    checkpoint.add_argument("--report", type=Path, required=True)
    checkpoint.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "report":
        inputs = {"measurement": json.loads(args.measurement.read_text()),
                  "declaration": json.loads(args.contract.read_text()),
                  "evidence": json.loads(args.evidence.read_text())}
        result = derive_report(**inputs)
        write_private_json(args.output.with_suffix(".inputs.json"), inputs)
    else:
        result = build_checkpoint(json.loads(args.report.read_text()))
        _write_private_text(args.output.with_suffix(".md"), render_checkpoint(result))
    write_private_json(args.output, result)
    print(f"Wrote {args.command} to {args.output}; input SHA-256 {result['input_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
