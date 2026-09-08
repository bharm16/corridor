"""Reproduce a governed #532 report from declared periods and retained inputs.

JSON is used rather than an analytics service: the event log and immutable
domain receipts already hold the evidence, and a private snapshot plus digest
makes a result reproducible without creating a second customer data store.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from corridor.analytics import AnalyticsEvent
from corridor.pilot_measurement import MeasurementPeriod, derive_measurement


INPUT_VERSION = "pilot-measurement-input-v1"


def read_event_log(path: Path) -> list[AnalyticsEvent]:
    """Consume raw #558 JSONL or the application's structured logging envelope."""
    events = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if "analytics_event" in value:
            value = value["analytics_event"]
        elif "family" not in value:
            # An operational log is not a product interaction.
            continue
        events.append(AnalyticsEvent.from_dict(value))
    return events


def write_private_json(path: Path, value: object) -> None:
    """Atomically write a customer-data artifact with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, default=str)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="declared periods and optional retained events")
    parser.add_argument("--events", type=Path, help="existing governed product event JSONL/log export")
    parser.add_argument("--database-receipts", action="store_true", help="read the scoped customer DB; no writes")
    parser.add_argument("--output", type=Path, required=True, help="private report output; customer data outside the repo")
    args = parser.parse_args(argv)
    source = json.loads(args.input.read_text())
    if source.get("schema_version") != INPUT_VERSION:
        raise ValueError("unsupported measurement input schema")
    periods = [MeasurementPeriod.from_dict(p) for p in source["periods"]]
    events = [AnalyticsEvent.from_dict(e) for e in source.get("events", [])]
    if args.events:
        events.extend(read_event_log(args.events))
    if args.database_receipts:
        from corridor.db import WorkerSession
        from corridor.pilot_measurement_receipts import read_domain_receipts

        with WorkerSession() as session:
            events.extend(read_domain_receipts(session, periods, events=events))
            session.rollback()
        periods = [replace(p, domain_receipts_complete=True) for p in periods]
    result = derive_measurement(periods, events)
    # Retain the exact inputs beside the result; rerunning this snapshot does
    # not connect to a database or depend on later changes in the log stream.
    snapshot = {"schema_version": INPUT_VERSION, "periods": [p.as_dict() for p in periods],
                "events": [e.as_dict() for e in events]}
    write_private_json(args.output.with_suffix(".inputs.json"), snapshot)
    write_private_json(args.output, result)
    print(f"Wrote {len(result['periods'])} declared periods to {args.output}; "
          f"input digest {result['input_sha256']}. Customer outcomes remain insufficient evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
