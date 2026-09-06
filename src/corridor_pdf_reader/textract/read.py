"""Read pairs through Textract and write a harness run.

For a list of pair keys and a lane this writes `reads/<key>.json` in the
reader's slim page shape and `read-receipts.json` in the shape
`bootstrap.read` writes, so `bootstrap.score` scores the run unchanged.

Lane A renders the original page and re-maps the document's own glyphs
into Textract's cell geometry; lane B reads the clean scan twin under
`scans-clean` and lane C the degraded twin under `scans-degraded`, where
Textract's words are the values. Lanes B and C keep the exact root's pair
keys, so one answer key scores all three and the same pages compare.

Every PNG sent is kept under `results/textract-png/<sha256>.png` and every
response under `results/textract-cache/<sha256>.json`; a second run costs
nothing. `--offline` never calls Textract: pages without a cached response
are written as pending, for another transport to fill the cache.

    textract/.venv/bin/python -m textract.read --lane A --output results/textract-A --keys 5e979146e9322953 ...
    textract/.venv/bin/python -m textract.read --lane B --pdf matrix.pdf --output results/textract-9424-B
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corridor_pdf_reader.bootstrap.corpus import TRUE_PAIRS, Pair, load_pairs  # noqa: E402
from corridor_pdf_reader.textract import render  # noqa: E402
from corridor_pdf_reader.textract.blocks import page_from_blocks  # noqa: E402
from corridor_pdf_reader.textract.client import (  # noqa: E402
    MAX_PAGES_DEFAULT,
    BudgetExceeded,
    PendingAnalysis,
    TextractClient,
)
from corridor_pdf_reader.textract.remap import remap_page  # noqa: E402

LANES = ("A", "B", "C")
LANE_DPI = {"A": 300, "B": 300, "C": 200}
TWIN_ROOTS = {"B": "scans-clean", "C": "scans-degraded"}
DEFAULT_CACHE = ROOT / "results" / "textract-cache"
DEFAULT_PNGS = ROOT / "results" / "textract-png"


def empty_page(number: int, geometry: render.Geometry) -> dict[str, Any]:
    return {"number": number, "size": [geometry.width, geometry.height], "rotation": geometry.rotation, "tables": [], "outside": [], "clipped": []}


def glyph_pages(pdf: Path, numbers: list[int]) -> dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """The reader's visible glyphs and hidden runs per page, from the text layer."""
    from corridor_pdf_reader.replacement.reader import read_pdf

    document = read_pdf(pdf, numbers, engine="tagged", dpi=36, retain_images=False)
    return {int(page["number"]): (page["characters"]["value"], page["clipped"]["value"]) for page in document["pages"]}


def read_document(
    source: Path,
    *,
    lane: str,
    client: TextractClient,
    dpi: int,
    pngs: Path,
    original: Path | None = None,
    pages: list[int] | None = None,
    margin: float = 0.0,
    rescue_runs: bool = False,
) -> dict[str, Any]:
    """Every requested page of `source` through Textract, in the slim shape.

    A page whose call fails is written empty with its error, so the run goes
    on and the miss is scored; a page with no cached response under an
    offline client is written empty as pending.
    """
    if lane not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    count = render.page_count(source)
    numbers = pages or list(range(1, count + 1))
    glyphs = glyph_pages(original or source, numbers) if lane == "A" else {}
    out: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    pending: list[str] = []
    for number in numbers:
        index = number - 1
        try:
            raster = render.render_page(source, index, dpi)
        except Exception as exc:
            page = empty_page(number, render.page_geometry(source, index))
            page["error"] = f"render: {type(exc).__name__}: {exc}"
            errors.append({"page": number, "error": page["error"]})
            out.append(page)
            continue
        sha = raster.sha256
        target = pngs / f"{sha}.png"
        if not target.exists():
            pngs.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raster.png)
        info: dict[str, Any] = {"png_sha256": sha, "dpi": dpi, "pixels": [raster.width_px, raster.height_px], "mode": raster.mode}
        try:
            analysis = client.analyze(raster.png)
        except PendingAnalysis:
            page = empty_page(number, raster.geometry)
            page.update(source=info, pending=sha)
            pending.append(sha)
            out.append(page)
            continue
        except (BudgetExceeded, Exception) as exc:
            page = empty_page(number, raster.geometry)
            page.update(source=info, error=f"textract: {type(exc).__name__}: {exc}")
            errors.append({"page": number, "error": page["error"]})
            out.append(page)
            continue
        geometry = raster.geometry
        page = page_from_blocks(analysis.response.get("Blocks") or [], number=number, size=(geometry.width, geometry.height), rotation=geometry.rotation)
        if lane == "A":
            chars, clipped = glyphs[number]
            page = remap_page(page, chars, clipped, margin, rescue_runs)
        info.update(cached=analysis.cached, model_version=analysis.response.get("AnalyzeDocumentModelVersion"))
        page["source"] = info
        out.append(page)
    return {
        "engine": f"textract-{lane}",
        "lane": lane,
        "source_sha256": render.sha256_of(source),
        "source_path": str(source),
        "page_count": count,
        "pages": out,
        "page_errors": errors,
        "pending": pending,
    }


def twin_path(pair: Pair, lane: str, twins: Path | None) -> Path:
    if lane == "A":
        return pair.pdf_path
    root = twins if twins is not None else TRUE_PAIRS / TWIN_ROOTS[lane]
    return root / pair.folder / pair.pdf


def read_pair(pair: Pair, *, lane: str, client: TextractClient, dpi: int, pngs: Path, twins: Path | None, reads: Path, margin: float = 0.0, rescue_runs: bool = False) -> dict[str, Any]:
    """One pair's read file and its receipt."""
    started = time.perf_counter()
    sent, hits = client.sent, client.hits
    try:
        source = twin_path(pair, lane, twins)
        if not source.exists():
            raise FileNotFoundError(f"no {lane} source at {source}")
        document = read_document(source, lane=lane, client=client, dpi=dpi, pngs=pngs, original=pair.pdf_path, margin=margin, rescue_runs=rescue_runs)
        slim = {
            "key": pair.key,
            "pdf": pair.pdf,
            "engine": document["engine"],
            "lane": lane,
            "source_sha256": document["source_sha256"],
            "source_path": document["source_path"],
            "twin_of_sha256": None if lane == "A" else pair.pdf_sha256,
            "pages": document["pages"],
        }
        (reads / f"{pair.key}.json").write_text(json.dumps(slim) + "\n")
        return {
            "key": pair.key,
            "pages": document["page_count"],
            "seconds": round(time.perf_counter() - started, 2),
            "sent": client.sent - sent,
            "cached": client.hits - hits,
            "pending": len(document["pending"]),
            "page_errors": document["page_errors"],
        }
    except Exception as exc:
        return {
            "key": pair.key,
            "seconds": round(time.perf_counter() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        }


def receipts_file(lane: str, dpi: int, client: TextractClient, receipts: list[dict[str, Any]], seconds: float, source_root: str, margin: float = 0.0, rescue_runs: bool = False) -> dict[str, Any]:
    return {
        "engine": f"textract-{lane}",
        "lane": lane,
        "dpi": dpi,
        "source_root": source_root,
        "cache": str(client.cache),
        "seconds": round(seconds, 1),
        "pages_sent": client.sent,
        "pages_cached": client.hits,
        "pages_pending": sum(int(r.get("pending", 0)) for r in receipts),
        "dollars": client.dollars,
        "remap_margin": margin,
        "remap_rescue_runs": rescue_runs,
        "receipts": receipts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--keys", nargs="*", help="pair keys to read; required unless --all")
    parser.add_argument("--all", action="store_true", help="every pair under TRUE_PAIRS_ROOT (the page budget still applies)")
    parser.add_argument("--twins", type=Path, help="twin root for lanes B and C; default scans-clean or scans-degraded beside TRUE_PAIRS_ROOT")
    parser.add_argument("--dpi", type=int, help="render resolution; default 300 for A and B, 200 for C")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--pngs", type=Path, default=DEFAULT_PNGS)
    parser.add_argument("--profile", default="corridor")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT, help="pages this run may send to Textract")
    parser.add_argument("--offline", action="store_true", help="never call Textract; uncached pages are written as pending")
    parser.add_argument("--margin", type=float, default=0.0, help="lane A: grow each cell polygon by this many points before assigning glyphs")
    parser.add_argument("--rescue-runs", action="store_true", help="lane A: a glyph no polygon holds joins the cell holding most of its text object")
    parser.add_argument("--pdf", type=Path, help="read one PDF outside the corpus into document.json")
    parser.add_argument("--pages", type=int, nargs="*", help="with --pdf: page numbers")
    args = parser.parse_args(argv)
    dpi = args.dpi or LANE_DPI[args.lane]
    client = TextractClient(args.cache, max_pages=args.max_pages, offline=args.offline, profile=args.profile, region=args.region)
    started = time.perf_counter()
    if args.pdf:
        args.output.mkdir(parents=True, exist_ok=False)
        document = read_document(args.pdf, lane=args.lane, client=client, dpi=dpi, pngs=args.pngs, original=args.pdf, pages=args.pages, margin=args.margin, rescue_runs=args.rescue_runs)
        for page in document["pages"]:
            sha = (page.get("source") or {}).get("png_sha256")
            if sha:
                (args.output / f"page-{page['number']}.png").write_bytes((args.pngs / f"{sha}.png").read_bytes())
        document["usage"] = {"pages_sent": client.sent, "pages_cached": client.hits, "dollars": client.dollars}
        (args.output / "document.json").write_text(json.dumps(document, indent=1) + "\n")
        print(f"{len(document['pages'])} pages, {client.sent} sent, {client.hits} cached, {len(document['pending'])} pending, {len(document['page_errors'])} failed, ${client.dollars} -> {args.output / 'document.json'}")
        return 0
    if not args.keys and not args.all:
        parser.error("--keys or --all is required")
    reads = args.output / "reads"
    reads.mkdir(parents=True, exist_ok=False)
    pairs = load_pairs()
    if args.keys:
        wanted = set(args.keys)
        pairs = [pair for pair in pairs if pair.key in wanted]
        missing = wanted - {pair.key for pair in pairs}
        if missing:
            parser.error(f"keys not in the corpus: {' '.join(sorted(missing))}")
    receipts = []
    for pair in pairs:
        receipt = read_pair(pair, lane=args.lane, client=client, dpi=dpi, pngs=args.pngs, twins=args.twins, reads=reads, margin=args.margin, rescue_runs=args.rescue_runs)
        receipts.append(receipt)
        if "error" in receipt:
            print("ERROR", pair.key, receipt["error"], flush=True)
        else:
            print(f"{pair.key} {pair.pdf[:40]:40} pages {receipt['pages']:3} sent {receipt['sent']:3} cached {receipt['cached']:3} pending {receipt['pending']:3} failed {len(receipt['page_errors'])}", flush=True)
    receipts.sort(key=lambda r: r["key"])
    source_root = str(TRUE_PAIRS if args.lane == "A" else (args.twins or TRUE_PAIRS / TWIN_ROOTS[args.lane]))
    summary = receipts_file(args.lane, dpi, client, receipts, time.perf_counter() - started, source_root, args.margin, args.rescue_runs)
    (args.output / "read-receipts.json").write_text(json.dumps(summary, indent=1) + "\n")
    failed = [r for r in receipts if "error" in r]
    print(f"{len(receipts)} documents, {sum(r.get('pages', 0) for r in receipts)} pages, {len(failed)} failed, {client.sent} sent, {client.hits} cached, {summary['pages_pending']} pending, ${client.dollars}, {time.perf_counter() - started:.0f}s -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
