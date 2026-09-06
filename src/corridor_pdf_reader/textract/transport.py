"""Move rasters and responses between this machine and S3 by presigned URL.

When Textract is reached through the Claude AWS connector rather than a
local profile, the call runs in the connector's sandbox, which sees only
AWS. The PNGs go up to S3 by presigned PUT, the sandbox calls
AnalyzeDocument on each S3 object and writes the cache entry (the shape
`textract.client.cache_entry` makes) back to S3, and the entries come down
by presigned GET into the same cache the client reads. The bytes Textract
reads are the uploaded PNG bytes, so the cache key is unchanged.

    python -m textract.transport pending --pngs results/textract-png --cache results/textract-cache
    python -m textract.transport put --urls urls.json --pngs results/textract-png
    python -m textract.transport get --urls urls.json --cache results/textract-cache

`urls.json` maps a sha256 to a presigned URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corridor_pdf_reader.textract.client import read_entry, write_entry  # noqa: E402


def pending(pngs: Path, cache: Path) -> list[dict[str, Any]]:
    """PNGs with no cache entry, oldest name first."""
    items = []
    for path in sorted(pngs.glob("*.png")):
        sha = path.stem
        if read_entry(cache, sha) is None:
            items.append({"sha256": sha, "bytes": path.stat().st_size})
    return items


def put(urls: dict[str, str], pngs: Path) -> list[dict[str, Any]]:
    receipts = []
    for sha, url in urls.items():
        data = (pngs / f"{sha}.png").read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"{sha}.png does not hash to its name")
        request = urllib.request.Request(url, data=data, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                receipts.append({"sha256": sha, "status": response.status, "bytes": len(data)})
        except urllib.error.HTTPError as exc:
            receipts.append({"sha256": sha, "status": exc.code, "error": exc.read()[:200].decode("utf-8", "replace")})
    return receipts


def get(urls: dict[str, str], cache: Path) -> list[dict[str, Any]]:
    """Download entries and keep only the ones that are what they claim."""
    receipts = []
    for sha, url in urls.items():
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            receipts.append({"sha256": sha, "status": exc.code, "error": exc.read()[:200].decode("utf-8", "replace")})
            continue
        try:
            entry = json.loads(body)
        except json.JSONDecodeError as exc:
            receipts.append({"sha256": sha, "error": f"not JSON: {exc}"})
            continue
        if entry.get("sha256") != sha or "response" not in entry or "Blocks" not in entry["response"]:
            receipts.append({"sha256": sha, "error": "entry is not a response for these bytes"})
            continue
        write_entry(cache, entry)
        receipts.append({"sha256": sha, "blocks": len(entry["response"]["Blocks"]), "attempts": entry.get("attempts")})
    return receipts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["pending", "put", "get"])
    parser.add_argument("--pngs", type=Path, default=ROOT / "results" / "textract-png")
    parser.add_argument("--cache", type=Path, default=ROOT / "results" / "textract-cache")
    parser.add_argument("--urls", type=Path, help="JSON object: sha256 -> presigned URL")
    args = parser.parse_args(argv)
    if args.action == "pending":
        items = pending(args.pngs, args.cache)
        print(json.dumps(items))
        print(f"{len(items)} pending, {sum(i['bytes'] for i in items)} bytes", file=sys.stderr)
        return 0
    if not args.urls:
        parser.error("--urls is required")
    urls = json.loads(args.urls.read_text())
    receipts = put(urls, args.pngs) if args.action == "put" else get(urls, args.cache)
    for receipt in receipts:
        print(json.dumps(receipt))
    failed = [r for r in receipts if "error" in r]
    print(f"{len(receipts) - len(failed)} ok, {len(failed)} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
