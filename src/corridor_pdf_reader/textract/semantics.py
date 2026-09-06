"""The semantics tier over a Textract read: the same OpenAI tier, by cell ID.

`replacement.semantics` is unchanged. This runner feeds it the page dicts a
lane wrote for one document (`textract.read --pdf`) and a smaller copy of
the PNG each page was read from, and writes `document.json` in the shape
the tier's own CLI writes, so `bootstrap.semantics_eval` compares it with
Corridor's machine gold unchanged. The key comes from `OPENAI_API_KEY` or
`--env-file` and is never printed.

    textract/.venv/bin/python -m textract.semantics --reading results/textract-9424-B/document.json \\
        --output results/semantics-9424-textract-B --env-file /path/to/.env
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corridor_pdf_reader.replacement.semantics import (  # noqa: E402
    PROMPT_VERSION,
    listing,
    read_document,
    reading_dict,
)
from corridor_pdf_reader.textract import render  # noqa: E402
from corridor_pdf_reader.textract.read import DEFAULT_PNGS  # noqa: E402


def model_images(pages: list[dict[str, Any]], pngs: Path, output: Path, dpi: int) -> dict[int, Path]:
    """The raster each page was read from, scaled to `dpi` for the model."""
    images: dict[int, Path] = {}
    for page in pages:
        source = page.get("source") or {}
        sha = source.get("png_sha256")
        if not sha or dpi <= 0:
            continue
        png = (pngs / f"{sha}.png").read_bytes()
        factor = dpi / float(source.get("dpi") or dpi)
        target = output / f"page-{page['number']}.png"
        target.write_bytes(render.downscale_png(png, factor) if factor < 1 else png)
        images[int(page["number"])] = target
    return images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reading", type=Path, required=True, help="document.json written by textract.read --pdf")
    parser.add_argument("--output", type=Path, required=True, help="new directory for document.json and the page images shown")
    parser.add_argument("--pngs", type=Path, default=DEFAULT_PNGS)
    parser.add_argument("--dpi", type=int, default=110, help="resolution of the page image shown to the model; 0 sends no image")
    parser.add_argument("--model", default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args(argv)

    from corridor_pdf_reader.replacement.llm import DEFAULT_MODEL, OpenAIClient, api_key_from

    if args.output.exists():
        raise SystemExit(f"{args.output} exists; choose a new directory")
    args.output.mkdir(parents=True)
    read = json.loads(args.reading.read_text())
    pages = read["pages"]
    name = Path(read["source_path"]).name
    images = model_images(pages, args.pngs, args.output, args.dpi)
    client = OpenAIClient(model=args.model or DEFAULT_MODEL, api_key=api_key_from(args.env_file))
    try:
        results = read_document(client, pages, name, images)
    finally:
        client.close()
    usage = {"calls": client.calls, "prompt_tokens": client.prompt_tokens, "completion_tokens": client.completion_tokens, "cached_tokens": client.cached_tokens}
    document = {
        "source": read["source_path"],
        "reading": str(args.reading),
        "engine": read["engine"],
        "model": client.model,
        "prompt_version": PROMPT_VERSION,
        "image_dpi": args.dpi,
        "usage": usage,
        "pages": [{"number": reading.number, "structure": structure, "reading": reading_dict(reading), "listing": listing(page, name)} for page, (structure, reading) in zip(pages, results, strict=True)],
    }
    (args.output / "document.json").write_text(json.dumps(document, indent=1))
    for _, reading in results:
        extracted = sum(1 for row in reading.rows if row.disposition == "extracted")
        fields = sorted(set(reading.mapping.fields.values())) if reading.mapping else []
        print(f"page {reading.number}: matrix={reading.is_utility_matrix} table={reading.matrix_table} header_row={reading.header_row} rows={extracted}/{len(reading.rows)} fields={len(fields)} unmapped={len(reading.mapping.unmapped) if reading.mapping else 0} refused={len(reading.refused)}")
    print(f"{usage['calls']} calls, {usage['prompt_tokens']} prompt tokens ({usage['cached_tokens']} cached), {usage['completion_tokens']} completion tokens -> {args.output / 'document.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
