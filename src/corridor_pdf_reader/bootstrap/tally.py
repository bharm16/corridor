"""Tally a scored run: pass rates by producer family, failing classes by
pages, and the worst pairs.

Reads the score files a `bootstrap.score` run wrote and the split the run
was scored under (`TRUE_PAIRS_ROOT` and `TRUE_PAIRS_SPLIT` select them, as
for every loop target). Artifacts the gate ignores are left out of the class
tally, so the counts are what still fails.

    make loop-score ARGS="--run results/loop-020 --reference results/loop-reference-v6"
    .venv/bin/python -m bootstrap.tally results/loop-020 --limit 25
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter, defaultdict
from typing import Any

from corridor_pdf_reader.bootstrap.corpus import load_split

SKIPPED_CLASSES = ("unverifiable", "prose_row", "columns_folded", "merged_rows")
SKIPPED_MISMATCHES = ("clipped", "overflow", "rounded", "epoch")


def failing(error: dict[str, Any]) -> bool:
    if error["class"] in SKIPPED_CLASSES:
        return False
    return not (error["class"] == "value_mismatch" and error["sub"].startswith(SKIPPED_MISMATCHES))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="run directory holding scores/")
    parser.add_argument("--limit", type=int, default=30, help="classes and pairs to list")
    args = parser.parse_args()
    split = load_split()
    families: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    classes: Counter[str] = Counter()
    examples: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    pairs: list[tuple[Any, ...]] = []
    for path in glob.glob(f"{args.run}/scores/*.json"):
        with open(path) as handle:
            score = json.load(handle)
        record: dict[str, Any] = dict(split[score["key"]])
        family, pdf = str(record["family"]), str(record["pdf"])
        tally = families[family]
        tally[0] += 1
        tally[1] += int(score["pass"])
        tally[2] += len(score["pages"])
        tally[3] += score["pages_pass"]
        for page in score["pages"]:
            seen: set[str] = set()
            for error in page["errors"]:
                if not failing(error):
                    continue
                name = error["class"] + "/" + error["sub"]
                if name not in seen:
                    classes[name] += 1
                    seen.add(name)
                if len(examples[name]) < 3:
                    examples[name].append((pdf[:26], page["number"], (error.get("ref") or "")[:26], (error.get("got") or "")[:30]))
        if score["uncovered"] and all(page["pass"] for page in score["pages"]):
            classes["uncovered-only"] += 1
            first = score["uncovered"][0]
            if len(examples["uncovered-only"]) < 4:
                examples["uncovered-only"].append((pdf[:26], first["r"], first["ref"][:26], first["sub"]))
        if not score["pass"]:
            failed_pages = [page["number"] for page in score["pages"] if not page["pass"]]
            counts = Counter(
                error["class"] + "/" + error["sub"]
                for page in score["pages"]
                if not page["pass"]
                for error in page["errors"]
                if failing(error)
            )
            pairs.append((len(failed_pages), family[:10], pdf[:46], score["key"], failed_pages[:5], dict(counts.most_common(2)), len(score["uncovered"])))
    for family, tally in sorted(families.items()):
        print(f"{family:16} pairs {tally[1]}/{tally[0]}  pages {tally[3]}/{tally[2]}")
    print("---- failing classes (pages)")
    for name, count in classes.most_common(args.limit):
        print(f"{count:5} {name:40} {examples[name][:2]}")
    print("---- failing pairs", len(pairs))
    for row in sorted(pairs, key=lambda item: (-item[0], item[2]))[: args.limit]:
        print(*row)


if __name__ == "__main__":
    main()
