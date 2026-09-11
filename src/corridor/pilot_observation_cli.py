"""Import observations of work performed outside Corridor into the collector (#846).

The pilot measures things Corridor cannot watch happen: the operations minutes
spent setting a connector up, the pre-adoption baseline the coordinator logged
on their own, the artifact somebody repaired in a spreadsheet after it was
downloaded. Those are real observations of real work and they belong in the
same #532 stream as the ones the product records, so they are read from a
governed declaration file rather than typed into a screen that was never the
place they happened.

What this command refuses is the point of it existing separately. It cannot
supply a presentation, a decision, a capture or a release record -- those are
Corridor's own account of what Corridor did, and a denominator assembled from
a file is not a measurement. It cannot supply a packet or child usefulness
judgment either: the contract takes those at the moment of triage, in the
product, and an imported one is a retrospective relabelling. The whole file is
read and validated before anything is emitted, so a rejected import leaves the
stream exactly as it was.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corridor.pilot_observations import IMPORT_VERSION, import_observations
from corridor.receipts import write_private_snapshot


_CONTRACT = f"""\
The file declares {IMPORT_VERSION}, an optional default `binding`, and one
`observations` entry per observation: its `family`, a zoned `occurred_at`, its
`payload`, and optionally its own `binding`.

  make pilot-observations ARGS="--input /governed/external-observations.json \\
      --output /governed/external-observations.jsonl"

The output is the #558 JSONL `make pilot-measurement ARGS="--events ..."`
reads. Presentation, decision and release families are refused, and so are
packet and child usefulness judgments, which the product collects at triage.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="declared external observations; customer data outside the repository",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="private #558 JSONL of the collected observations",
    )
    args = parser.parse_args(argv)
    events = import_observations(json.loads(args.input.read_text()))
    write_private_snapshot(
        args.output,
        "".join(
            json.dumps(event.as_dict(), sort_keys=True, default=str) + "\n"
            for event in events
        ),
    )
    print(
        f"Collected {len(events)} external observations to {args.output}. "
        "They observe work; they establish no decision, authority or denominator."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
