"""One command scores a named configuration against the registered dataset and writes a receipt (#731).

`make pdf-pairs-measure` is the Extraction Measurement of the paired-rendition
method: it reads every selected pair's PDF with the named configuration through
`bootstrap.read` (a process pool, one PDFium per process), scores each read
against the answer key of the registered Reference Dataset with
`bootstrap.score`, tallies, and writes a receipt that keeps pair, page and
cell measures apart, keeps the development and holdout sets apart, names the
configuration identity and the registration digest, compares every pair with
the registered baseline, and carries the limits paragraph. Nothing here
adjusts anything to a result; a difference is recorded as a difference.

The holdout is spent (ADR-0008). The command refuses to touch it without
`--include-holdout`, an actor and a reason, and appends every access it makes,
with its result, to `gold/pdf-pairs/v1/holdout-access.jsonl`, whether the run
finished or failed.

A named configuration is a reader engine at the measured defaults. The frozen
reader (`tagged`, 36 dpi) is the baseline; `drawn-grid` is the reader's
drawn-grid engine without Excel's structure tree, kept so the gate is proven
to fail on a configuration that reads less. Adding a configuration here is
how an integration ticket measures its change; retaining the receipt is how
the result enters the registry.
"""

from __future__ import annotations

import argparse
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
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corridor_pdf_reader import provenance, registry
from corridor_pdf_reader.bootstrap.corpus import load_pairs
from corridor_pdf_reader.bootstrap.score import markdown, score_pair, summarize
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE
from corridor_pdf_reader.reproduction import compare_keys, corpus_preflight, digest_directory

PACKAGE_ROOT = provenance.PACKAGE_ROOT
MEASURED_JOBS = 4
DEFAULT_CORPUS = Path(
    os.environ.get(
        "TRUE_PAIRS_ROOT",
        "/Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs/exact",
    )
)
RETAINED_FILES = (
    "receipt.json",
    "RECEIPT.md",
    "SUMMARY.md",
    "summary.json",
    "tally.txt",
    "read-receipts.json",
    "reads.manifest.json",
)


@dataclass(frozen=True)
class Configuration:
    name: str
    engine: str
    description: str
    dpi: int = MEASURED_DPI

    def identity(self, jobs: int) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "engine": self.engine,
            "dpi": self.dpi,
            "jobs": jobs,
            "driver": "bootstrap.read",
            "retain_images": False,
            "reader": {
                "commit": provenance.SOURCE_COMMIT,
                "repository": provenance.SOURCE_REPOSITORY,
                "package_digest": provenance.package_digest(),
                "matches_commit": provenance.verify() == [],
            },
        }


CONFIGURATIONS: dict[str, Configuration] = {
    "frozen-reader": Configuration(
        "frozen-reader",
        MEASURED_ENGINE,
        "the imported reader as loop-020 measured it: PDFium glyphs joined to Excel's structure tree "
        "through pypdf, the deterministic reconstructor on what is left, engine `tagged` at 36 dpi",
    ),
    "drawn-grid": Configuration(
        "drawn-grid",
        "pdfium",
        "the reader's drawn-grid engine (`pdfium`) without Excel's structure tree: not the measured "
        "configuration and not a candidate; it reads less than the frozen reader so the gate is proven to fail",
    ),
}


def sha256_file(path: Path) -> str:
    return registry.sha256_file(path)


def load_split_records(corpus_root: Path, split_file: Path) -> dict[str, dict[str, Any]]:
    """The sealed split restricted to the pairs the corpus root holds, keyed by pair key.

    What `bootstrap.corpus.load_split` does, with the two paths explicit
    rather than read from the environment at import time.
    """
    records = json.loads(split_file.read_text(encoding="utf-8"))["pairs"]
    present = {pair.key for pair in load_pairs(corpus_root)}
    return {record["key"]: record for record in records if record["key"] in present}


def environment() -> dict[str, Any]:
    """Dependency versions and the machine, for the configuration identity."""
    import pypdfium2

    versions: dict[str, str | None] = {}
    for name in ("pypdfium2", "pypdf", "pdf-oxide", "pillow", "openpyxl", "xlrd"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    node: str | None
    try:
        node = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        node = None
    ssf_manifest = PACKAGE_ROOT / "paired_trial" / "node_modules" / "ssf" / "package.json"
    ssf = json.loads(ssf_manifest.read_text(encoding="utf-8"))["version"] if ssf_manifest.is_file() else None
    return {
        "python": sys.version.split()[0],
        "packages": versions,
        "pdfium_build": str(pypdfium2.PDFIUM_INFO),
        "pypdfium2_build": str(pypdfium2.PYPDFIUM_INFO),
        "node": node,
        "ssf": ssf,
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "hostname": platform.node(),
        },
    }


def run_step(name: str, command: list[str], log: Path, env: dict[str, str]) -> dict[str, Any]:
    """One harness command in its own process, its output and wall time kept."""
    started = time.perf_counter()
    with open(log, "w", encoding="utf-8") as handle:
        completed = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env, check=False)
    seconds = round(time.perf_counter() - started, 1)
    if completed.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {completed.returncode}; see {log}")
    return {"step": name, "command": [str(part) for part in command], "seconds": seconds, "log": log.name}


def score_selected(
    run: Path,
    reference: Path,
    keys: list[str],
    records: dict[str, dict[str, Any]],
    *,
    holdout_included: bool,
) -> dict[str, Any]:
    """Score the selected reads in this process, writing what `bootstrap.score` writes.

    Per-pair files under `scores/`, `summary.json` and `SUMMARY.md` over the
    selection; the scorer itself (`score_pair`, `summarize`, `markdown`) is
    the imported one, unchanged.
    """
    scores = run / "scores"
    if scores.exists():
        for stale in scores.glob("*.json"):
            stale.unlink()
    scores.mkdir(exist_ok=True)
    receipts = json.loads((run / "read-receipts.json").read_text(encoding="utf-8"))["receipts"]
    read_errors = {item["key"]: item["error"] for item in receipts if "error" in item and item["key"] in keys}
    results = []
    for key in sorted(keys):
        read_path = run / "reads" / f"{key}.json"
        reference_path = reference / f"{key}.json"
        if not read_path.exists() or not reference_path.exists():
            continue
        result = score_pair(
            json.loads(reference_path.read_text(encoding="utf-8")),
            json.loads(read_path.read_text(encoding="utf-8")),
        )
        (scores / f"{key}.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
        results.append(result)
    summary = summarize(results, records, read_errors)
    summary["holdout_included"] = holdout_included
    (run / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    (run / "SUMMARY.md").write_text(markdown(summary, run.name, holdout_included), encoding="utf-8")
    return summary


def measures(scores: Path, keys: list[str], records: dict[str, dict[str, Any]], read_errors: dict[str, str]) -> dict[str, Any]:
    """Pair, page and cell measures over one set of keys, each kept as its own pair of numbers."""
    results = []
    unread: list[str] = []
    for key in sorted(keys):
        path = scores / f"{key}.json"
        if not path.is_file():
            unread.append(key)
            continue
        results.append(json.loads(path.read_text(encoding="utf-8")))
    summary = summarize(results, records, {key: read_errors[key] for key in read_errors if key in keys})
    failing = sorted(result["key"] for result in results if not result["pass"])
    failing_pages = sum(len(result["pages"]) - result["pages_pass"] for result in results)
    return {
        "keys_selected": len(keys),
        "pairs": [summary["pairs_pass"], summary["pairs"]],
        "pages": [summary["pages_pass"], summary["pages"]],
        "cells_exact": [summary["exact_cells"], summary["reference_cells"]],
        "failing_pairs": failing,
        "failing_pages": failing_pages,
        "unscored_keys": unread,
        "read_errors": summary["read_errors"],
        "by_class": summary["by_class"],
        "by_subclass": summary["by_subclass"],
        "pairs_with_class": summary["pairs_with_class"],
        "pages_with_class": summary["pages_with_class"],
        "by_family": summary["by_family"],
        "worst_pairs": summary["worst_pairs"][:5],
    }


def gate(result: dict[str, Any]) -> dict[str, Any]:
    """The gate's verdict on one set: every pair passes, or the pairs that do not."""
    all_pass = (
        result["pairs"][1] > 0
        and result["pairs"][0] == result["pairs"][1]
        and not result["unscored_keys"]
        and not result["read_errors"]
    )
    return {
        "all_pairs_pass": all_pass,
        "failing_pairs": result["failing_pairs"],
        "failing_pages": result["failing_pages"],
        "unscored_keys": result["unscored_keys"],
        "read_errors": sorted(result["read_errors"]),
    }


def compare_with_baseline(scores: Path, baseline_scores: Path, keys: list[str]) -> dict[str, Any]:
    """Pair by pair against the registered baseline's retained scores.

    A regression is a pair the baseline passed and this run fails, a page the
    baseline passed and this run fails, or a reference cell the baseline read
    exactly and this run did not; each is counted on its own.
    """
    regressed: list[str] = []
    improved: list[str] = []
    absent: list[str] = []
    compared = 0
    pages_before = pages_after = cells_before = cells_after = 0
    for key in sorted(keys):
        current = scores / f"{key}.json"
        previous = baseline_scores / f"{key}.json"
        if not current.is_file() or not previous.is_file():
            absent.append(key)
            continue
        now = json.loads(current.read_text(encoding="utf-8"))
        then = json.loads(previous.read_text(encoding="utf-8"))
        compared += 1
        if then["pass"] and not now["pass"]:
            regressed.append(key)
        elif now["pass"] and not then["pass"]:
            improved.append(key)
        pages_before += then["pages_pass"]
        pages_after += now["pages_pass"]
        cells_before += then["exact_cells"]
        cells_after += now["exact_cells"]
    return {
        "pairs_compared": compared,
        "pairs_regressed": regressed,
        "pairs_improved": improved,
        "pairs_not_in_baseline": absent,
        "pages_pass": {"baseline": pages_before, "this_run": pages_after},
        "cells_exact": {"baseline": cells_before, "this_run": cells_after},
        "regressed": bool(regressed) or pages_after < pages_before or cells_after < cells_before,
    }


def holdout_entry(
    *,
    run: str,
    receipt_path: str,
    actor: str,
    reason: str,
    configuration: dict[str, Any],
    dataset: dict[str, Any],
    holdout_keys: list[str],
    holdout_pages: int,
    all_holdout: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "accessed_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "run": run,
        "actor": actor,
        "reason": reason,
        "purpose": f"measurement of configuration `{configuration['name']}` through make pdf-pairs-measure",
        "dataset": {"dataset_version": dataset["dataset_version"], "registration_sha256": dataset["sha256"]},
        "configuration": {
            "name": configuration["name"],
            "engine": configuration["engine"],
            "dpi": configuration["dpi"],
            "commit": configuration["reader"]["commit"],
            "package_digest": configuration["reader"]["package_digest"],
            "pypdfium2": configuration["environment"]["packages"]["pypdfium2"],
            "pdfium": configuration["environment"]["pdfium_build"],
            "pypdf": configuration["environment"]["packages"]["pypdf"],
        },
        "holdout": {
            "pairs": len(holdout_keys),
            "pages": holdout_pages,
            "all": all_holdout,
            "keys_sha256": hashlib.sha256("\n".join(sorted(holdout_keys)).encode()).hexdigest(),
        },
        "result": result,
        "receipt": receipt_path,
    }


def receipt_markdown(receipt: dict[str, Any]) -> str:
    configuration = receipt["configuration"]
    lines = [
        f"# Paired-rendition measurement: {receipt['run']}",
        "",
        f"Configuration `{configuration['name']}`: {configuration['description']}. Engine `{configuration['engine']}`, "
        f"{configuration['dpi']} dpi, {configuration['jobs']} jobs; reader commit `{configuration['reader']['commit'][:7]}`, "
        f"package digest `{configuration['reader']['package_digest'][:16]}`; pypdfium2 "
        f"{configuration['environment']['packages']['pypdfium2']} (PDFium {configuration['environment']['pdfium_build']}), "
        f"pypdf {configuration['environment']['packages']['pypdf']}.",
        "",
        f"Reference Dataset `{receipt['dataset']['dataset_version']}`, registration `{receipt['dataset']['sha256'][:16]}`, "
        f"corpus manifest `{receipt['dataset']['corpus_manifest_sha256'][:16]}`, split `{receipt['dataset']['split_sha256'][:16]}`; "
        f"{receipt['selection']['development_keys']} development and {receipt['selection']['holdout_keys']} holdout pairs selected"
        f"{' (a subset)' if receipt['selection']['subset'] else ''}; answer keys {receipt['reference']['identical_bytes']} of "
        f"{receipt['reference']['keys']} byte-identical to loop-reference-v6.",
        "",
        "| Set | Pairs | Pages | Cells exact | Failing pairs |",
        "|---|---:|---:|---:|---|",
    ]
    for name in ("development", "holdout"):
        result = receipt["results"].get(name)
        if result is None:
            lines.append(f"| {name} | not scored | | | |")
            continue
        lines.append(
            f"| {name} | {result['pairs'][0]} / {result['pairs'][1]} | {result['pages'][0]} / {result['pages'][1]} | "
            f"{result['cells_exact'][0]:,} / {result['cells_exact'][1]:,} | {', '.join(result['failing_pairs']) or 'none'} |"
        )
    lines += ["", "## Gate", ""]
    for name, verdict in receipt["gate"].items():
        lines.append(
            f"- {name}: {'every pair passes' if verdict['all_pairs_pass'] else str(len(verdict['failing_pairs'])) + ' pair(s) fail'}"
            f", {verdict['failing_pages']} page(s) fail"
            + (f", read errors on {len(verdict['read_errors'])}" if verdict["read_errors"] else "")
        )
    comparison = receipt["baseline_comparison"]
    lines += ["", "## Against the registered baseline", ""]
    if comparison is None:
        lines.append("- no registered baseline to compare with: this run is the baseline once retained")
    else:
        for name, delta in comparison["by_set"].items():
            lines.append(
                f"- {name}: {delta['pairs_compared']} pairs compared with `{comparison['against']}`; "
                f"{len(delta['pairs_regressed'])} regressed ({', '.join(delta['pairs_regressed']) or 'none'}), "
                f"{len(delta['pairs_improved'])} improved; pages passing {delta['pages_pass']['baseline']} -> "
                f"{delta['pages_pass']['this_run']}; cells exact {delta['cells_exact']['baseline']:,} -> "
                f"{delta['cells_exact']['this_run']:,}"
            )
        lines.append(f"- regression against the baseline: **{'yes' if comparison['regressed'] else 'no'}**")
    lines += ["", "## Holdout", ""]
    if receipt["holdout"]["included"]:
        lines.append(
            f"- included; actor `{receipt['holdout']['actor']}`, reason: {receipt['holdout']['reason']}; "
            f"the access is appended to `{receipt['holdout']['ledger']}`"
        )
    else:
        lines.append("- not included; the holdout was neither read nor scored")
    lines += ["", "## Limits", "", receipt["limits"], ""]
    return "\n".join(lines)


def retain(output: Path, receipt: dict[str, Any], role: str, registry_root: Path) -> Path:
    """Copy the receipt set beside the registry and index it by path and digest."""
    target = registry_root / "receipts" / receipt["run"]
    if target.exists():
        raise RuntimeError(f"{target} already holds a receipt; choose another run name")
    target.mkdir(parents=True)
    names = [name for name in RETAINED_FILES if (output / name).is_file()]
    for name in names:
        shutil.copyfile(output / name, target / name)
    shutil.copytree(output / "loop" / "scores", target / "scores")
    names.extend(f"scores/{path.name}" for path in sorted((target / "scores").glob("*.json")))
    entry = {
        "run": receipt["run"],
        "role": role,
        "date": receipt["date"],
        "path": registry.relative(target),
        "what": receipt["what"],
        "configuration": {
            "name": receipt["configuration"]["name"],
            "engine": receipt["configuration"]["engine"],
            "dpi": receipt["configuration"]["dpi"],
            "jobs": receipt["configuration"]["jobs"],
            "commit": receipt["configuration"]["reader"]["commit"],
            "package_digest": receipt["configuration"]["reader"]["package_digest"],
            "pypdfium2": receipt["configuration"]["environment"]["packages"]["pypdfium2"],
            "pdfium": receipt["configuration"]["environment"]["pdfium_build"],
            "pypdf": receipt["configuration"]["environment"]["packages"]["pypdf"],
        },
        "dataset": receipt["dataset"],
        "selection": receipt["selection"],
        "measures": {
            name: (
                None
                if result is None
                else {"pairs": result["pairs"], "pages": result["pages"], "cells_exact": result["cells_exact"], "failing_pairs": result["failing_pairs"]}
            )
            for name, result in receipt["results"].items()
        },
        "gate": receipt["gate"],
        "baseline_comparison": (
            None
            if receipt["baseline_comparison"] is None
            else {"against": receipt["baseline_comparison"]["against"], "regressed": receipt["baseline_comparison"]["regressed"]}
        ),
        "holdout_access": receipt["holdout"].get("ledger_entry"),
        "files": registry.digest_files(target, names),
    }
    registry.register_receipt(entry, registry_root / registry.RECEIPT_INDEX.name)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--configuration", required=True, choices=sorted(CONFIGURATIONS), help="the named configuration to score")
    parser.add_argument("--output", type=Path, required=True, help="a new directory outside git; its name names the run")
    parser.add_argument("--keys", nargs="*", help="restrict the measurement to these pair keys")
    parser.add_argument("--reference", type=Path, help="answer keys already built by bootstrap.reference; built into the output otherwise")
    parser.add_argument("--jobs", type=int, default=MEASURED_JOBS, help="reader and key-builder processes; the measured default is 4")
    parser.add_argument("--include-holdout", action="store_true", help="read and score the spent holdout (ADR-0008); needs an actor and a reason")
    parser.add_argument("--holdout-actor")
    parser.add_argument("--holdout-reason")
    parser.add_argument("--retain", choices=["baseline", "failure-proof", "measurement"], help="copy the receipt set beside the registry and index it under this role")
    parser.add_argument("--registry", type=Path, default=registry.REGISTRY_ROOT, help="the registry directory (gold/pdf-pairs/v1)")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="TRUE_PAIRS_ROOT: the exact set")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configuration = CONFIGURATIONS[args.configuration]
    if args.include_holdout and not (args.holdout_actor and args.holdout_reason):
        print("a holdout run requires --include-holdout, --holdout-actor and --holdout-reason (ADR-0008)", file=sys.stderr)
        return 2
    registration_path = args.registry / registry.REGISTRATION.name
    ledger_path = args.registry / registry.HOLDOUT_LEDGER.name
    if not registration_path.is_file():
        print(f"{registration_path} does not exist; the Reference Dataset is not registered", file=sys.stderr)
        return 2
    registration = registry.load_registration(registration_path)
    dataset = registry.registration_identity(registration_path)
    if not (args.corpus / "MANIFEST.csv").is_file():
        print(f"{args.corpus} holds no MANIFEST.csv; set TRUE_PAIRS_ROOT or --corpus to the exact set", file=sys.stderr)
        return 2
    split_file = registry.REPO_ROOT / registration["split"]["file"]
    if sha256_file(args.corpus / "MANIFEST.csv") != registration["corpus"]["manifest_sha256"]:
        print(f"{args.corpus}/MANIFEST.csv is not the registered corpus manifest", file=sys.stderr)
        return 1
    if sha256_file(split_file) != registration["split"]["sha256"]:
        print(f"{split_file} is not the registered split", file=sys.stderr)
        return 1
    problems = provenance.verify()
    if problems and configuration.name == "frozen-reader":
        print("the package does not match commit c39363e, so `frozen-reader` is not the frozen reader:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    if args.reference is None:
        if not (PACKAGE_ROOT / "paired_trial" / "node_modules" / "ssf").is_dir():
            print("ssf is not installed; run `make pdf-reader-node` first, or pass --reference", file=sys.stderr)
            return 2
        try:
            importlib.metadata.version("xlrd")
        except importlib.metadata.PackageNotFoundError:
            print("xlrd is not installed; run through `make pdf-pairs-measure` (the pdf-reader-experiment group)", file=sys.stderr)
            return 2

    records = load_split_records(args.corpus, split_file)
    registered = {pair["key"]: pair for pair in registration["pairs"]}
    if set(records) != set(registered):
        print("the corpus root and the registration disagree on which pairs exist", file=sys.stderr)
        return 1
    development = sorted(key for key, record in records.items() if not record["holdout"])
    holdout = sorted(key for key, record in records.items() if record["holdout"])
    if args.keys:
        unknown = sorted(set(args.keys) - set(records))
        if unknown:
            print(f"keys not in the registered dataset: {unknown}", file=sys.stderr)
            return 2
        withheld = sorted(set(args.keys) & set(holdout))
        if withheld and not args.include_holdout:
            print(f"holdout keys need --include-holdout with an actor and a reason: {withheld}", file=sys.stderr)
            return 2
        development = [key for key in development if key in set(args.keys)]
        holdout = [key for key in holdout if key in set(args.keys)]
    if not args.include_holdout:
        holdout = []
    selected = development + holdout
    if not selected:
        print("no pair selected", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=False)
    (PACKAGE_ROOT / "tmp").mkdir(exist_ok=True)
    started = time.perf_counter()
    date = dt.datetime.now(dt.UTC).date().isoformat()
    run_name = args.output.name
    env = dict(os.environ, TRUE_PAIRS_ROOT=str(args.corpus), TRUE_PAIRS_SPLIT=str(split_file))
    python = sys.executable
    steps: list[dict[str, Any]] = []
    identity = configuration.identity(args.jobs)
    identity["environment"] = environment()

    corpus = corpus_preflight(args.corpus)
    if corpus["mismatches"] or corpus["missing"]:
        print("corpus preflight failed:", json.dumps(corpus, indent=1), file=sys.stderr)
        return 1

    reference = args.reference
    if reference is None:
        reference = args.output / "reference"
        steps.append(
            run_step(
                "keys",
                [python, "-m", "corridor_pdf_reader.bootstrap.reference", "--output", str(reference), "--jobs", str(args.jobs), "--keys", *selected],
                args.output / "keys.log",
                env,
            )
        )
    keys_comparison = compare_keys(reference, registry.REPO_ROOT / registration["reference"]["manifest"], selected)
    keys_comparison["source"] = "built" if args.reference is None else str(args.reference)
    if keys_comparison["different"] or keys_comparison["absent"]:
        print("the answer keys are not the registered ones:", json.dumps(keys_comparison, indent=1), file=sys.stderr)
        return 1

    holdout_record: dict[str, Any] = {"included": bool(holdout)}
    if holdout:
        holdout_record.update(actor=args.holdout_actor, reason=args.holdout_reason, ledger=registry.relative(ledger_path))
    holdout_pages = sum(int(records[key]["pages"]) for key in holdout)
    # The ledger cites the receipt where it will live: beside the registry when retained.
    receipt_path = registry.relative(
        (args.registry / "receipts" / run_name / "receipt.json") if args.retain else (args.output / "receipt.json")
    )

    def record_access(result: dict[str, Any]) -> None:
        if not holdout:
            return
        entry = registry.append_holdout_access(
            holdout_entry(
                run=run_name,
                receipt_path=receipt_path,
                actor=args.holdout_actor,
                reason=args.holdout_reason,
                configuration=identity,
                dataset=dataset,
                holdout_keys=holdout,
                holdout_pages=holdout_pages,
                all_holdout=len(holdout) == sum(1 for record in records.values() if record["holdout"]),
                result=result,
            ),
            ledger_path,
        )
        holdout_record["ledger_entry"] = entry["accessed_at"]

    loop = args.output / "loop"
    try:
        steps.append(
            run_step(
                "read",
                [python, "-m", "corridor_pdf_reader.bootstrap.read", "--output", str(loop), "--engine", configuration.engine, "--jobs", str(args.jobs), "--keys", *selected],
                args.output / "read.log",
                env,
            )
        )
        read_receipts = json.loads((loop / "read-receipts.json").read_text(encoding="utf-8"))
        shutil.copyfile(loop / "read-receipts.json", args.output / "read-receipts.json")
        (args.output / "reads.manifest.json").write_text(json.dumps(digest_directory(loop / "reads"), indent=1) + "\n", encoding="utf-8")
        scoring_started = time.perf_counter()
        summary = score_selected(loop, reference, selected, records, holdout_included=bool(holdout))
        steps.append({"step": "score", "command": ["bootstrap.score.score_pair", "in process, over the selection"], "seconds": round(time.perf_counter() - scoring_started, 1)})
        shutil.copyfile(loop / "SUMMARY.md", args.output / "SUMMARY.md")
        shutil.copyfile(loop / "summary.json", args.output / "summary.json")
        with open(args.output / "tally.txt", "w", encoding="utf-8") as handle:
            tally = subprocess.run(
                [python, "-m", "corridor_pdf_reader.bootstrap.tally", str(loop), "--limit", "30"],
                stdout=handle, stderr=subprocess.STDOUT, env=env, check=False,
            )
        steps.append({"step": "tally", "command": [python, "-m", "corridor_pdf_reader.bootstrap.tally", str(loop)], "exit_code": tally.returncode, "log": "tally.txt"})
        read_errors = {item["key"]: item["error"] for item in read_receipts["receipts"] if "error" in item}
        results: dict[str, Any] = {
            "development": measures(loop / "scores", development, records, read_errors) if development else None,
            "holdout": measures(loop / "scores", holdout, records, read_errors) if holdout else None,
        }
    except BaseException as exc:
        record_access({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]})
        raise

    verdicts = {name: gate(result) for name, result in results.items() if result is not None}
    baseline = registry.baseline_receipt(args.registry / registry.RECEIPT_INDEX.name)
    comparison: dict[str, Any] | None = None
    if baseline is not None and baseline["run"] != run_name:
        baseline_scores = registry.REPO_ROOT / baseline["path"] / "scores"
        by_set = {
            name: compare_with_baseline(loop / "scores", baseline_scores, development if name == "development" else holdout)
            for name, result in results.items()
            if result is not None
        }
        comparison = {
            "against": baseline["run"],
            "baseline_path": baseline["path"],
            "baseline_configuration": baseline["configuration"],
            "by_set": by_set,
            "regressed": any(delta["regressed"] for delta in by_set.values()),
        }

    receipt: dict[str, Any] = {
        "schema_version": registry.RECEIPT_SCHEMA,
        "run": run_name,
        "date": date,
        "what": f"Extraction Measurement of configuration `{configuration.name}` against the registered paired-rendition Reference Dataset; an explicit experiment outside CI.",
        "configuration": identity,
        "dataset": dataset,
        "selection": {
            "development_keys": len(development),
            "holdout_keys": len(holdout),
            "subset": bool(args.keys),
            "keys": selected if args.keys else "every registered pair of each scored set",
            "keys_sha256": hashlib.sha256("\n".join(selected).encode()).hexdigest(),
        },
        "corpus": corpus,
        "reference": keys_comparison,
        "reads": {
            "engine": read_receipts["engine"],
            "seconds": read_receipts["seconds"],
            "documents": len(read_receipts["receipts"]),
            "pages": sum(item.get("pages", 0) for item in read_receipts["receipts"]),
            "errors": [item for item in read_receipts["receipts"] if "error" in item],
        },
        "steps": steps,
        "results": results,
        "summary_over_selection": {key: summary[key] for key in ("pairs", "pairs_pass", "pages", "pages_pass", "reference_cells", "exact_cells", "holdout_included")},
        "gate": verdicts,
        "baseline_comparison": comparison,
        "holdout": holdout_record,
        "limits": registry.LIMITS,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "output": str(args.output.resolve()),
    }
    if holdout:
        assert results["holdout"] is not None
        record_access(
            {
                "status": "scored",
                "pairs": results["holdout"]["pairs"],
                "pages": results["holdout"]["pages"],
                "cells_exact": results["holdout"]["cells_exact"],
                "failing_pairs": results["holdout"]["failing_pairs"],
                "regressed_against_baseline": None if comparison is None else comparison["by_set"]["holdout"]["regressed"],
            }
        )
    receipt["wall_seconds"] = round(time.perf_counter() - started, 1)
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.output / "RECEIPT.md").write_text(receipt_markdown(receipt), encoding="utf-8")
    print(json.dumps({"results": {name: None if result is None else {k: result[k] for k in ("pairs", "pages", "cells_exact", "failing_pairs")} for name, result in results.items()}, "gate": verdicts, "baseline_comparison": None if comparison is None else {"against": comparison["against"], "regressed": comparison["regressed"]}, "wall_seconds": receipt["wall_seconds"]}, indent=1))
    if args.retain:
        target = retain(args.output, receipt, args.retain, args.registry)
        print(f"retained in {target} and indexed in {args.registry / registry.RECEIPT_INDEX.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
