"""Compare a semantics-tier reading with a machine gold set of conflict rows.

Corridor's machine references list one row per conflict as `source_ref,page,
critical`. The tier's extracted rows carry a `utility_id` value read from a
cell and the page it came from; a gold row is found when a row on its page
carries its identifier. Nothing else in the gold is compared here.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reading", type=Path, required=True, nargs="+", help="document.json files written by replacement.semantics; several pool their rows")
    parser.add_argument("--gold", type=Path, required=True, help="machine reference CSV with source_ref and page")
    args = parser.parse_args()
    documents = [json.loads(path.read_text()) for path in args.reading]
    gold: Counter[tuple[int, str]] = Counter()
    with open(args.gold, newline="") as handle:
        for row in csv.DictReader(handle):
            gold[(int(row["page"]), row["source_ref"].strip())] += 1
    found: Counter[tuple[int, str]] = Counter()
    skipped: Counter[str] = Counter()
    for document, page in ((document, page) for document in documents for page in document["pages"]):
        reading = page["reading"]
        name = Path(document["source"]).name[:40]
        for row in reading["rows"]:
            if row["disposition"] == "extracted":
                found[(reading["number"], row["fields"]["utility_id"]["text"].strip())] += 1
            else:
                skipped[row["reason"]] += 1
        fields = sorted(set(reading["mapping"]["fields"].values())) if reading.get("mapping") else []
        marks = list(reading["mapping"]["marks"].values()) if reading.get("mapping") else []
        print(f"{name} page {reading['number']}: header_row={reading['header_row']} fields={fields} marks={marks} unmapped={reading['mapping']['unmapped'] if reading.get('mapping') else None} refused={reading['refused']}")
    matched = sum(min(n, found[key]) for key, n in gold.items())
    missing = sorted(key for key, n in gold.items() if found[key] < n)
    extra = sorted(key for key, n in found.items() if gold[key] < n)
    total_gold = sum(gold.values())
    total_found = sum(found.values())
    print(f"\ngold rows {total_gold}, extracted rows {total_found}, matched {matched}")
    print(f"recall {matched / total_gold:.1%}  precision {matched / total_found:.1%}" if total_gold and total_found else "no rows to compare")
    print("skipped rows:", dict(skipped))
    if missing:
        print("missing from the reading:", missing[:40])
    if extra:
        print("read but not in gold:", extra[:40])


if __name__ == "__main__":
    main()
