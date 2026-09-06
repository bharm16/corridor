"""Place the pairs whose PDF prints the workbook exactly into a nested directory.

Input is the exactness result for a run. Files are APFS clones (`cp -c`) of the
true-pairs files, which are themselves clones of the originals, so the nested
set costs no disk and nothing above it changes. One folder per source folder;
a folder that holds several pairs keeps only its exact ones here.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from collections import Counter
from datetime import date
from pathlib import Path

from corridor_pdf_reader.bootstrap.corpus import TRUE_PAIRS


def authored_in_word(pdf: Path) -> bool:
    """A PDF whose Creator is Word was not printed from the workbook: the
    workbook copies the document's table, and its cell partition is not the
    page's."""
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=False).stdout
    creator = next((line.partition(":")[2].strip() for line in info.splitlines() if line.startswith("Creator:")), "")
    return creator.startswith(("Word", "Microsoft Word", "Microsoft® Word"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exactness", type=Path, required=True, help="exactness.json from a run")
    parser.add_argument("--name", default="exact", help="nested directory name under true-pairs")
    parser.add_argument("--source", type=Path, default=TRUE_PAIRS)
    args = parser.parse_args()
    target = args.source / args.name
    if target.exists():
        raise SystemExit(f"{target} exists; choose another name or remove it first")
    results = json.loads(args.exactness.read_text())
    by_pair = {(r["folder"], r["pdf"]): r for r in results}
    with open(args.source / "MANIFEST.csv", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    included, excluded = [], []
    for row in rows:
        result = by_pair.get((row["folder"], row["pdf"]))
        if row["duplicate_of"]:
            excluded.append({**row, "reason": "duplicate of " + row["duplicate_of"], "missing_examples": "", "unexplained_examples": ""})
            continue
        if result is None:
            excluded.append({**row, "reason": "not checked", "missing_examples": "", "unexplained_examples": ""})
            continue
        if result["exact"] and authored_in_word(args.source / row["folder"] / row["pdf"]):
            excluded.append({**row, "reason": "PDF authored in Word; the workbook copies its table and is not the print source", "missing_examples": "", "unexplained_examples": ""})
        elif result["exact"]:
            included.append((row, result))
        else:
            reasons = []
            if result["missing_from_pdf"]:
                reasons.append(f"{result['missing_from_pdf']} workbook cells not printed")
            if result["unexplained_in_pdf"]:
                reasons.append(f"{result['unexplained_in_pdf']} printed strings not in workbook")
            excluded.append(
                {
                    **row,
                    "reason": "; ".join(reasons),
                    "missing_examples": " | ".join(m[:60] for m in result["missing_examples"][:3]),
                    "unexplained_examples": " | ".join(u[:60] for u in result["unexplained_examples"][:3]),
                }
            )
    target.mkdir()
    for row, _ in included:
        folder = target / row["folder"]
        folder.mkdir(exist_ok=True)
        for name in (row["pdf"], row["spreadsheet"], "SOURCE.md"):
            src = args.source / row["folder"] / name
            dst = folder / name
            if src.exists() and not dst.exists():
                subprocess.run(["cp", "-c", str(src), str(dst)], check=True)
    extra = ["exact_check_run", "clipped_in_pdf", "uncached_formulas"]
    with open(target / "MANIFEST.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames + extra)
        writer.writeheader()
        for row, result in included:
            writer.writerow({**row, "exact_check_run": args.exactness.parent.name, "clipped_in_pdf": result["clipped_in_pdf"], "uncached_formulas": result["uncached_formulas"]})
    with open(target / "EXCLUDED.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames + ["reason", "missing_examples", "unexplained_examples"])
        writer.writeheader()
        writer.writerows(excluded)
    families: Counter[str] = Counter()
    for row, _ in included:
        families[row["folder"].split("_", 1)[0]] += 1
    pages = sum(int(row["pdf_pages"]) for row, _ in included)
    (target / "README.md").write_text(
        f"""# exact

Pairs from the parent directory whose PDF prints the workbook as it now stands:
every printed workbook cell appears in the PDF's text, and every string the PDF
prints is explained by a workbook cell. Checked on {date.today().isoformat()} from the
extracted text alone, with no table structure involved.

- {len(included)} pairs, {pages} pages, in {len({row['folder'] for row, _ in included})} folders; by agency:
  {', '.join(f'{k} {v}' for k, v in sorted(families.items()))}.
- `MANIFEST.csv` carries the parent's columns plus the check's run name, the
  count of cells the print clipped, and the count of formulas without cached
  values.
- `EXCLUDED.csv` lists every parent pair left out with the reason and up to
  three examples on each side. Most exclusions are workbooks edited after the
  print: retitled rows, rewritten comments, updated amounts or percentages.
- Files are APFS clones of the parent's files; the parent is unchanged.

Print artifacts that do not count against a pair, because the PDF cannot
avoid them: pagination; hashes for a number too wide for its column; the lines
of a wrapped cell that did not fit its row, or text cut at a cell edge, when a
printed run is a piece of the cell; overflow from a cell outside the print area;
a date printed under the machine's short-date format; results of formulas the
workbook stores without a cached value.

Rebuild: `make loop-exactness ARGS="--run results/RUN --reference results/REF --output results/RUN/exactness.json"`
then `.venv/bin/python -m bootstrap.nest_exact --exactness results/RUN/exactness.json --name NAME`.
"""
    )
    print(f"{len(included)} pairs cloned into {target}; {len(excluded)} excluded")


if __name__ == "__main__":
    main()
