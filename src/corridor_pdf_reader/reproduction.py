"""The 333-pair reproduction of loop-020: an explicit experiment, never CI (#729, ADR-0008).

One documented command (`make pdf-reader-reproduce`) does what the standalone
loop did by hand, in the declared environment rather than a laptop's: check
every corpus file against its declared digest, build the answer keys with
`bootstrap.reference` and compare them file for file with the retained
loop-reference-v6 digests, read every pair with `bootstrap.read --engine
tagged` at its measured defaults, score the development set and then the
already-spent holdout with `bootstrap.score`, tally, and write a receipt
that carries the configuration identity: the commit, the package digest,
every dependency version, the corpus manifest digest, the split digest, the
key digests, the command lines, the wall times and the machine.

The holdout is scored because loop-020 scored it, and only for that reason:
this is a reproduction, no tuning follows from it, and `--retain` appends the
access to `bootstrap/LOOP-LOG.md` so it does not sit outside the ledger.

If a number differs from loop-020, the receipt records the difference
exactly and says so; nothing here adjusts anything to close it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from corridor_pdf_reader import provenance
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE

PACKAGE_ROOT = provenance.PACKAGE_ROOT
RECEIPTS = PACKAGE_ROOT / "receipts"
LOOP_LOG = PACKAGE_ROOT / "bootstrap" / "LOOP-LOG.md"
MEASURED_JOBS = 4

# loop-020 as bootstrap/LOOP-LOG.md, HANDOFF.md and receipts/loop-020 record it.
LOOP_020 = {
    "development": {"pairs": [262, 263], "pages": [1589, 1589]},
    "holdout": {"pairs": [70, 70], "pages": [409, 409]},
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def corpus_preflight(root: Path) -> dict[str, Any]:
    """Every selected file of the corpus hashes to the manifest's declaration."""
    rows = list(csv.DictReader(open(root / "MANIFEST.csv", newline="")))
    mismatches: list[dict[str, str]] = []
    missing: list[str] = []
    checked = 0
    total = 0
    for row in rows:
        if row["duplicate_of"]:
            continue
        for column, name in (("pdf_sha256", row["pdf"]), ("book_sha256", row["spreadsheet"])):
            path = root / row["folder"] / name
            if not path.is_file():
                missing.append(str(path))
                continue
            checked += 1
            total += path.stat().st_size
            actual = sha256_file(path)
            if actual != row[column]:
                mismatches.append({"path": str(path), "declared": row[column], "actual": actual})
    return {
        "root": str(root),
        "manifest_sha256": sha256_file(root / "MANIFEST.csv"),
        "excluded_sha256": sha256_file(root / "EXCLUDED.csv") if (root / "EXCLUDED.csv").is_file() else None,
        "pairs": sum(1 for row in rows if not row["duplicate_of"]),
        "files_checked": checked,
        "bytes": total,
        "mismatches": mismatches,
        "missing": missing,
    }


def run_step(name: str, command: list[str], log: Path, env: dict[str, str]) -> dict[str, Any]:
    """Run one harness command as its own process, keeping its output and wall time."""
    started = time.perf_counter()
    with open(log, "w", encoding="utf-8") as handle:
        completed = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env, check=False)
    seconds = round(time.perf_counter() - started, 1)
    if completed.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {completed.returncode}; see {log}")
    return {"step": name, "command": command, "seconds": seconds, "log": log.name}


def digest_directory(root: Path) -> dict[str, Any]:
    entries = [
        {"path": str(path.relative_to(root)), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return {"files": len(entries), "bytes": sum(entry["bytes"] for entry in entries), "entries": entries}


def compare_keys(built: Path, retained_manifest: Path, keys: list[str]) -> dict[str, Any]:
    """The freshly built keys against the retained loop-reference-v6, file for file."""
    retained = {
        entry["path"]: entry["sha256"]
        for entry in json.loads(retained_manifest.read_text(encoding="utf-8"))["entries"]
    }
    identical: list[str] = []
    equal_content: list[str] = []
    different: list[str] = []
    absent: list[str] = []
    for key in keys:
        name = f"{key}.json"
        path = built / name
        if not path.is_file():
            absent.append(key)
            continue
        if name not in retained:
            absent.append(key)
            continue
        if sha256_file(path) == retained[name]:
            identical.append(key)
        else:
            different.append(key)
    return {
        "retained_manifest": str(retained_manifest.relative_to(PACKAGE_ROOT)),
        "keys": len(keys),
        "identical_bytes": len(identical),
        "equal_content_only": equal_content,
        "different": different,
        "absent": absent,
    }


def score_counts(scores: Path, keys: list[str]) -> dict[str, Any]:
    """Pairs, pages and cells over one set of pair keys, from the per-pair score files."""
    pairs = pages = pairs_pass = pages_pass = exact = reference = 0
    failing: list[str] = []
    for key in keys:
        path = scores / f"{key}.json"
        if not path.is_file():
            continue
        score = json.loads(path.read_text(encoding="utf-8"))
        pairs += 1
        pairs_pass += int(score["pass"])
        pages += len(score["pages"])
        pages_pass += score["pages_pass"]
        exact += score["exact_cells"]
        reference += score["reference_cells"]
        if not score["pass"]:
            failing.append(key)
    return {
        "pairs": [pairs_pass, pairs],
        "pages": [pages_pass, pages],
        "cells_exact": [exact, reference],
        "failing_pairs": failing,
    }


def environment() -> dict[str, Any]:
    import pypdfium2

    versions = {
        name: importlib.metadata.version(name)
        for name in ("pypdfium2", "pypdf", "pdf-oxide", "pillow", "openpyxl", "xlrd")
    }
    node = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    ssf = json.loads(
        (PACKAGE_ROOT / "paired_trial" / "node_modules" / "ssf" / "package.json").read_text(encoding="utf-8")
    )["version"]
    return {
        "python": sys.version.split()[0],
        "prefix": sys.prefix,
        "packages": versions,
        "pdfium_build": str(pypdfium2.PDFIUM_INFO),
        "pypdfium2_build": str(pypdfium2.PYPDFIUM_INFO),
        "node": node,
        "ssf": ssf,
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "hostname": platform.node(),
        },
    }


def compare_with_loop_020(development: dict[str, Any], holdout: dict[str, Any]) -> dict[str, Any]:
    differences = []
    for split, counts in (("development", development), ("holdout", holdout)):
        for measure in ("pairs", "pages"):
            expected = LOOP_020[split][measure]
            if counts[measure] != expected:
                differences.append(
                    f"{split} {measure}: reproduced {counts[measure][0]} of {counts[measure][1]}, "
                    f"loop-020 recorded {expected[0]} of {expected[1]}"
                )
    return {"expected": LOOP_020, "matches": not differences, "differences": differences}


def loop_log_entry(receipt: dict[str, Any]) -> str:
    development, holdout = receipt["results"]["development"], receipt["results"]["holdout"]
    verdict = "reproduces loop-020 exactly" if receipt["loop_020"]["matches"] else (
        "differs from loop-020: " + "; ".join(receipt["loop_020"]["differences"])
    )
    return (
        f"\n## Holdout access {receipt['date']}: reproduction inside Corridor (#729), no tuning\n\n"
        f"Purpose: reproduce loop-020 from the declared Corridor environment after importing the\n"
        f"reader unchanged; the holdout was scored because loop-020 scored it, and nothing was\n"
        f"changed before or after (ADR-0008). Configuration: commit `{receipt['source']['commit'][:7]}`,\n"
        f"package digest `{receipt['source']['package_digest'][:16]}`, engine `{receipt['reader']['engine']}`,\n"
        f"dpi {receipt['reader']['dpi']}, jobs {receipt['reader']['jobs']}, pypdfium2 "
        f"{receipt['environment']['packages']['pypdfium2']} (PDFium {receipt['environment']['pdfium_build']}), "
        f"pypdf {receipt['environment']['packages']['pypdf']}, corpus manifest "
        f"`{receipt['corpus']['manifest_sha256'][:16]}`, split `{receipt['split']['sha256'][:16]}`, keys "
        f"{receipt['keys']['identical_bytes']} of {receipt['keys']['keys']} byte-identical to loop-reference-v6.\n\n"
        f"| Set | Pairs | Pages | Cells exact |\n|---|---:|---:|---:|\n"
        f"| development | {development['pairs'][0]} / {development['pairs'][1]} | "
        f"{development['pages'][0]} / {development['pages'][1]} | "
        f"{development['cells_exact'][0]:,} / {development['cells_exact'][1]:,} |\n"
        f"| holdout | {holdout['pairs'][0]} / {holdout['pairs'][1]} | "
        f"{holdout['pages'][0]} / {holdout['pages'][1]} | "
        f"{holdout['cells_exact'][0]:,} / {holdout['cells_exact'][1]:,} |\n\n"
        f"Result: {verdict}. Receipt: `receipts/{receipt['run']}/receipt.json` in the Corridor package;\n"
        f"handed to #731 for the Extraction Measurement ledger.\n"
    )


def retain(output: Path, receipt: dict[str, Any]) -> Path:
    """Copy the receipt set into the package and append the holdout access to the loop log."""
    target = RECEIPTS / receipt["run"]
    if target.exists():
        raise RuntimeError(f"{target} already holds a receipt; choose another run name")
    target.mkdir(parents=True)
    for name in (
        "receipt.json",
        "dev-SUMMARY.md",
        "dev-summary.json",
        "all-SUMMARY.md",
        "all-summary.json",
        "tally.txt",
        "read-receipts.json",
        "keys.manifest.json",
        "reads.manifest.json",
        "scores.manifest.json",
    ):
        shutil.copyfile(output / name, target / name)
    with open(LOOP_LOG, "a", encoding="utf-8") as handle:
        handle.write(loop_log_entry(receipt))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True, help="a new directory outside git")
    parser.add_argument("--jobs", type=int, default=MEASURED_JOBS, help="the measured default is 4")
    parser.add_argument(
        "--retain",
        action="store_true",
        help="copy the receipt set into the package's receipts/ and append the holdout access to bootstrap/LOOP-LOG.md",
    )
    args = parser.parse_args(argv)

    from corridor_pdf_reader.bootstrap.corpus import PAIRS_FILE, TRUE_PAIRS, load_pairs, load_split

    if not (TRUE_PAIRS / "MANIFEST.csv").is_file():
        print(f"TRUE_PAIRS_ROOT={TRUE_PAIRS} holds no MANIFEST.csv", file=sys.stderr)
        return 2
    if not (PACKAGE_ROOT / "paired_trial" / "node_modules" / "ssf").is_dir():
        print("ssf is not installed; run `make pdf-reader-node` first", file=sys.stderr)
        return 2
    try:
        importlib.metadata.version("xlrd")
    except importlib.metadata.PackageNotFoundError:
        print("xlrd is not installed; run through `make pdf-reader-reproduce` (the pdf-reader-experiment group)", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=False)
    (PACKAGE_ROOT / "tmp").mkdir(exist_ok=True)

    started = time.perf_counter()
    date = dt.datetime.now(dt.UTC).date().isoformat()
    run_name = args.output.name
    env = dict(os.environ)
    python = sys.executable
    steps: list[dict[str, Any]] = []

    problems = provenance.verify()
    if problems:
        print("the package does not match commit c39363e:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    corpus = corpus_preflight(TRUE_PAIRS)
    if corpus["mismatches"] or corpus["missing"]:
        print("corpus preflight failed:", json.dumps(corpus, indent=1), file=sys.stderr)
        return 1
    pairs = load_pairs()
    split = load_split()
    keys = [pair.key for pair in pairs]
    development = [key for key in keys if not split[key]["holdout"]]
    holdout = [key for key in keys if split[key]["holdout"]]

    reference = args.output / "reference"
    steps.append(
        run_step(
            "keys",
            [python, "-m", "corridor_pdf_reader.bootstrap.reference", "--output", str(reference), "--jobs", str(args.jobs)],
            args.output / "keys.log",
            env,
        )
    )
    key_receipts = json.loads((reference / "receipts.json").read_text(encoding="utf-8"))
    keys_comparison = compare_keys(reference, RECEIPTS / "loop-reference-v6.manifest.json", keys)
    keys_comparison["build_failures"] = [receipt for receipt in key_receipts if "error" in receipt]
    keys_comparison["build_errors"] = [receipt["errors"] for receipt in key_receipts if receipt.get("errors")]
    (args.output / "keys.manifest.json").write_text(json.dumps(digest_directory(reference), indent=1) + "\n")

    loop = args.output / "loop"
    steps.append(
        run_step(
            "read",
            [python, "-m", "corridor_pdf_reader.bootstrap.read", "--output", str(loop), "--engine", MEASURED_ENGINE, "--jobs", str(args.jobs)],
            args.output / "read.log",
            env,
        )
    )
    read_receipts = json.loads((loop / "read-receipts.json").read_text(encoding="utf-8"))
    shutil.copyfile(loop / "read-receipts.json", args.output / "read-receipts.json")
    (args.output / "reads.manifest.json").write_text(json.dumps(digest_directory(loop / "reads"), indent=1) + "\n")

    steps.append(
        run_step(
            "score-development",
            [python, "-m", "corridor_pdf_reader.bootstrap.score", "--run", str(loop), "--reference", str(reference)],
            args.output / "score-development.log",
            env,
        )
    )
    shutil.copyfile(loop / "SUMMARY.md", args.output / "dev-SUMMARY.md")
    shutil.copyfile(loop / "summary.json", args.output / "dev-summary.json")
    steps.append(
        run_step(
            "score-with-holdout",
            [python, "-m", "corridor_pdf_reader.bootstrap.score", "--run", str(loop), "--reference", str(reference), "--include-holdout"],
            args.output / "score-with-holdout.log",
            env,
        )
    )
    shutil.copyfile(loop / "SUMMARY.md", args.output / "all-SUMMARY.md")
    shutil.copyfile(loop / "summary.json", args.output / "all-summary.json")
    (args.output / "scores.manifest.json").write_text(json.dumps(digest_directory(loop / "scores"), indent=1) + "\n")
    with open(args.output / "tally.txt", "w", encoding="utf-8") as handle:
        tally = subprocess.run(
            [python, "-m", "corridor_pdf_reader.bootstrap.tally", str(loop), "--limit", "30"],
            stdout=handle, stderr=subprocess.STDOUT, env=env, check=False,
        )
    steps.append({"step": "tally", "command": ["python", "-m", "corridor_pdf_reader.bootstrap.tally", str(loop)], "exit_code": tally.returncode, "log": "tally.txt"})

    development_counts = score_counts(loop / "scores", development)
    holdout_counts = score_counts(loop / "scores", holdout)
    dev_summary = json.loads((args.output / "dev-summary.json").read_text(encoding="utf-8"))
    all_summary = json.loads((args.output / "all-summary.json").read_text(encoding="utf-8"))
    receipt: dict[str, Any] = {
        "run": run_name,
        "date": date,
        "what": "Reproduction of loop-020 inside Corridor from the declared environment; an explicit experiment outside CI (#729, ADR-0008). No tuning.",
        "source": {
            "commit": provenance.SOURCE_COMMIT,
            "repository": provenance.SOURCE_REPOSITORY,
            "branch": provenance.SOURCE_BRANCH,
            "package_digest": provenance.package_digest(),
            "manifest_verified": True,
        },
        "reader": {"engine": MEASURED_ENGINE, "dpi": MEASURED_DPI, "jobs": args.jobs, "driver": "bootstrap.read", "retain_images": False},
        "environment": environment(),
        "corpus": corpus,
        "split": {
            "file": str(PAIRS_FILE.relative_to(PACKAGE_ROOT)) if PAIRS_FILE.is_relative_to(PACKAGE_ROOT) else str(PAIRS_FILE),
            "sha256": sha256_file(PAIRS_FILE),
            "seed": json.loads(PAIRS_FILE.read_text(encoding="utf-8"))["seed"],
            "development_pairs": len(development),
            "holdout_pairs": len(holdout),
        },
        "keys": keys_comparison,
        "reads": {
            "engine": read_receipts["engine"],
            "seconds": read_receipts["seconds"],
            "documents": len(read_receipts["receipts"]),
            "pages": sum(item.get("pages", 0) for item in read_receipts["receipts"]),
            "errors": [item for item in read_receipts["receipts"] if "error" in item],
        },
        "steps": steps,
        "results": {
            "development": development_counts,
            "holdout": holdout_counts,
            "development_summary": {key: dev_summary[key] for key in ("pairs", "pairs_pass", "pages", "pages_pass", "reference_cells", "exact_cells", "holdout_included")},
            "all_summary": {key: all_summary[key] for key in ("pairs", "pairs_pass", "pages", "pages_pass", "reference_cells", "exact_cells", "holdout_included")},
        },
        "loop_020": compare_with_loop_020(development_counts, holdout_counts),
        "wall_seconds": round(time.perf_counter() - started, 1),
        "output": str(args.output.resolve()),
    }
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({"development": development_counts, "holdout": holdout_counts, "loop_020": receipt["loop_020"], "keys": {k: v for k, v in keys_comparison.items() if k != "equal_content_only"}, "wall_seconds": receipt["wall_seconds"]}, indent=1))
    if args.retain:
        target = retain(args.output, receipt)
        print(f"retained in {target}; holdout access appended to {LOOP_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
