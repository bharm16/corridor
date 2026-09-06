"""Retained responses through the normalizer, twice each, with a receipt (#732, ADR-0094).

Exact replay means replaying the retained response, never assuming a fresh
call returns the same output. This module replays a retained entry (the
imported client's cache shape, or one of the four fixture files) through
`identity.normalize` under every page frame the retained lane reads recorded
for its raster, as many passes as asked, and records the raw-response digest,
the normalized-reading digest of each pass, whether the passes agree, the
provider-reported model version and the block, table and cell counts.

Two uses. CI replays the four committed fixtures and compares the result with
`receipts/textract/fixture-replay.json`, so a change to the parser or the
normalizer that alters a reading is seen. `make textract-replay` is the
explicit experiment outside CI over the 116-entry experiment cache, which is
103 MB and stays in the standalone worktree, read only; its receipt is
retained beside the fixture one.

A frame is needed because Textract's geometry is ratios of the image and the
reader's page is in points: the retained reads carry each raster's page size
and rotation, and two rasters carry two frames each (an original stores its
rotation, its scan twin stores the rotated size), so those are replayed under
both.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corridor_pdf_reader import provenance
from corridor_pdf_reader.textract_adapter.identity import (
    TEXTRACT_WORDS,
    NormalizationConfiguration,
    adapter_identity,
    model_version,
    normalize,
    raw_response_digest,
    reading_counts,
    reading_digest,
)

PACKAGE_ROOT = provenance.PACKAGE_ROOT
FIXTURES = PACKAGE_ROOT / "textract" / "tests" / "fixtures"
RECEIPTS = PACKAGE_ROOT / "receipts" / "textract"
FIXTURE_RECEIPT = RECEIPTS / "fixture-replay.json"
DEFAULT_PASSES = 2


@dataclass(frozen=True)
class Frame:
    size: tuple[float, float]
    rotation: int
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {"size": [self.size[0], self.size[1]], "rotation": self.rotation, "from": self.source}


def frames_from_reads(roots: list[Path]) -> dict[str, list[Frame]]:
    """Every raster's page frames, from `reads/*.json` and `document.json` under the roots."""
    frames: dict[str, list[Frame]] = {}
    for root in roots:
        files = sorted(root.rglob("reads/*.json")) + sorted(root.rglob("document.json"))
        for path in files:
            document = json.loads(path.read_text(encoding="utf-8"))
            for page in document.get("pages", []):
                sha = (page.get("source") or {}).get("png_sha256")
                if not sha:
                    continue
                frame = Frame((float(page["size"][0]), float(page["size"][1])), int(page["rotation"]), f"{path.relative_to(root.parent)} page {page['number']}")
                known = frames.setdefault(sha, [])
                if all((f.size, f.rotation) != (frame.size, frame.rotation) for f in known):
                    known.append(frame)
    return frames


def fixture_frame(entry: dict[str, Any], name: str) -> Frame:
    width, height = entry["displayed_size"]
    return Frame((float(width), float(height)), 0, f"{name}: displayed_size")


def replay_entry(
    entry: dict[str, Any],
    name: str,
    frames: list[Frame],
    *,
    passes: int = DEFAULT_PASSES,
    normalization: NormalizationConfiguration = NormalizationConfiguration(),
) -> dict[str, Any]:
    response = entry["response"]
    readings = []
    for frame in frames:
        digests = []
        counts: dict[str, int] = {}
        for _ in range(passes):
            page = normalize(response, number=1, size=frame.size, rotation=frame.rotation, normalization=normalization)
            digests.append(reading_digest(page))
            counts = reading_counts(page)
        readings.append(
            {
                "frame": frame.as_dict(),
                "normalization": normalization.as_dict(TEXTRACT_WORDS),
                "digests": digests,
                "identical": len(set(digests)) == 1,
                **counts,
            }
        )
    return {
        "name": name,
        "raster_sha256": entry.get("sha256") or entry.get("png_sha256"),
        "raster_bytes": entry.get("bytes"),
        "transport": entry.get("transport"),
        "requested_at": entry.get("requested_at"),
        "attempts": entry.get("attempts"),
        "feature_types": entry.get("feature_types"),
        "model_version": model_version(response),
        "raw_response_digest": raw_response_digest(response),
        "blocks": len(response.get("Blocks") or []),
        "readings": readings,
    }


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    readings = [reading for entry in entries for reading in entry["readings"]]
    return {
        "entries": len(entries),
        "readings": len(readings),
        "identical_readings": sum(1 for r in readings if r["identical"]),
        "all_identical": all(r["identical"] for r in readings) and bool(readings),
        "entries_without_a_frame": sum(1 for entry in entries if not entry["readings"]),
        "distinct_raw_response_digests": len({entry["raw_response_digest"] for entry in entries}),
        "model_versions": dict(sorted(Counter(str(entry["model_version"]) for entry in entries).items())),
        "blocks": sum(entry["blocks"] for entry in entries),
        "tables": sum(r["tables"] for r in readings),
        "cells": sum(r["cells"] for r in readings),
        "outside": sum(r["outside"] for r in readings),
    }


def receipt(entries: list[dict[str, Any]], *, source: dict[str, Any], passes: int) -> dict[str, Any]:
    return {
        "kind": "textract-retained-response-replay",
        "replayed_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "adapter": adapter_identity(),
        "package_digest": provenance.package_digest(),
        "passes": passes,
        "source": source,
        "summary": summarize(entries),
        "entries": entries,
    }


def replay_fixtures(passes: int = DEFAULT_PASSES) -> list[dict[str, Any]]:
    """The four retained fixture responses, each under its declared displayed size."""
    entries = []
    for path in sorted(FIXTURES.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        entries.append(replay_entry(entry, path.stem, [fixture_frame(entry, path.stem)], passes=passes))
    return entries


def replay_cache(cache: Path, frames: dict[str, list[Frame]], passes: int = DEFAULT_PASSES) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(cache.glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if "response" not in entry:
            continue
        sha = entry.get("sha256") or path.stem
        entries.append(replay_entry(entry, path.stem, frames.get(sha, []), passes=passes))
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, help="directory of retained entries in the imported client's shape (read only)")
    parser.add_argument("--reads", type=Path, action="append", default=[], help="a results root whose reads/*.json and document.json supply each raster's page frame; repeatable")
    parser.add_argument("--fixtures", action="store_true", help="replay the four committed fixture responses instead of a cache")
    parser.add_argument("--passes", type=int, default=DEFAULT_PASSES)
    parser.add_argument("--output", type=Path, required=True, help="receipt to write")
    parser.add_argument("--retain", action="store_true", help="also copy the receipt into the package's receipts/textract/")
    args = parser.parse_args(argv)
    if args.fixtures == (args.cache is not None):
        parser.error("give --fixtures or --cache, not both")
    if args.fixtures:
        entries = replay_fixtures(args.passes)
        source: dict[str, Any] = {"fixtures": str(FIXTURES.relative_to(PACKAGE_ROOT)), "files": len(entries)}
        retained_name = FIXTURE_RECEIPT.name
    else:
        frames = frames_from_reads(args.reads)
        entries = replay_cache(args.cache, frames, args.passes)
        source = {
            "cache": str(args.cache),
            "entries": len(entries),
            "bytes": sum(path.stat().st_size for path in args.cache.glob("*.json")),
            "frames_from": [str(root) for root in args.reads],
            "rasters_with_frames": len(frames),
        }
        retained_name = args.output.name
    document = receipt(entries, source=source, passes=args.passes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    if args.retain:
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, RECEIPTS / retained_name)
    summary = document["summary"]
    print(
        f"{summary['entries']} entries, {summary['readings']} readings, {summary['identical_readings']} identical, "
        f"{summary['entries_without_a_frame']} without a frame, model versions {summary['model_versions']}, "
        f"{summary['tables']} tables, {summary['cells']} cells -> {args.output}"
    )
    return 0 if summary["all_identical"] and not summary["entries_without_a_frame"] else 1


if __name__ == "__main__":
    sys.exit(main())
