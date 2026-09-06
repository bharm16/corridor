"""Read every pair's PDF with the candidate reader. No workbook enters this step.

Output per pair is the reader's structured cells plus the text it left outside
every table, which is all the scorer needs; characters and renders stay out.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from multiprocessing import Pool
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from corridor_pdf_reader.bootstrap.corpus import Pair, load_pairs  # noqa: E402
from corridor_pdf_reader.replacement.pages import slim_page  # noqa: E402
from corridor_pdf_reader.replacement.reader import read_pdf  # noqa: E402


def read_one(task: tuple[Pair, Path, str]) -> dict[str, Any]:
    pair, output, engine = task
    started = time.perf_counter()
    try:
        import pypdfium2 as pdfium

        count = len(pdfium.PdfDocument(pair.pdf_path))
        document = read_pdf(pair.pdf_path, list(range(1, count + 1)), engine=engine, dpi=36, retain_images=False)
        slim = {
            "key": pair.key,
            "pdf": pair.pdf,
            "engine": engine,
            "source_sha256": document["source_sha256"],
            "pages": [slim_page(page) for page in document["pages"]],
        }
        (output / f"{pair.key}.json").write_text(json.dumps(slim) + "\n")
        return {"key": pair.key, "pages": count, "seconds": round(time.perf_counter() - started, 2)}
    except Exception as exc:
        return {
            "key": pair.key,
            "seconds": round(time.perf_counter() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--engine", default="pdfium", choices=["pdfium", "pdf_oxide", "oxide_pdfium", "tagged"])
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--keys", nargs="*", help="restrict to these pair keys")
    args = parser.parse_args()
    reads = args.output / "reads"
    reads.mkdir(parents=True, exist_ok=False)
    pairs = load_pairs()
    if args.keys:
        pairs = [pair for pair in pairs if pair.key in set(args.keys)]
    receipts = []
    started = time.perf_counter()
    with Pool(args.jobs) as pool:
        for receipt in pool.imap_unordered(read_one, [(pair, reads, args.engine) for pair in pairs]):
            receipts.append(receipt)
            if "error" in receipt:
                print("ERROR", receipt["key"], receipt["error"], flush=True)
    receipts.sort(key=lambda r: r["key"])
    (args.output / "read-receipts.json").write_text(
        json.dumps({"engine": args.engine, "seconds": round(time.perf_counter() - started, 1), "receipts": receipts}, indent=1) + "\n"
    )
    failed = [r for r in receipts if "error" in r]
    pages = sum(r.get("pages", 0) for r in receipts)
    print(f"{len(receipts)} documents, {pages} pages, {len(failed)} failed, {time.perf_counter() - started:.0f}s -> {args.output}")


if __name__ == "__main__":
    main()
