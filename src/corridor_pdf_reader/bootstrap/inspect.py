"""Print one page as the scorer saw it: reader rows beside aligned sheet rows.

    .venv/bin/python -m bootstrap.inspect --run results/loop-002 \\
        --reference results/loop-reference-v3 --pdf 19132-71-VE-June-2023.pdf --page 12
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from corridor_pdf_reader.bootstrap.corpus import load_split
from corridor_pdf_reader.bootstrap.score import Sheet, align_columns, align_rows, compact, table_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--width", type=int, default=18, help="characters per cell")
    args = parser.parse_args()
    split = load_split()
    key = next(k for k, record in split.items() if record["pdf"] == args.pdf)
    read = json.loads((args.run / "reads" / f"{key}.json").read_text())
    reference = json.loads((args.reference / f"{key}.json").read_text())
    sheets = [Sheet(data) for data in reference["sheets"]]
    page = read["pages"][args.page - 1]
    width = args.width
    print(f"{args.pdf} page {args.page}: {len(page['tables'])} tables, {len(page['outside'])} outside strings")
    for index, table in enumerate(page["tables"]):
        rows = table_rows(table)
        keys = [compact(cell["text"]) for cell in table["cells"] if compact(cell["text"])]
        sheet = max(sheets, key=lambda s: sum(1 for k in keys if k in s.key_count))
        row_map = align_rows(rows, sheet)
        col_map = align_columns(rows, row_map, sheet)
        print(f"\n== table {index} method={table.get('method')} sheet={sheet.name!r} rows={len(rows)} columns mapped={col_map}")
        for i in sorted(rows)[: args.rows]:
            r = row_map.get(i)
            reader = {cell["column"]: cell["text"].replace("\n", " ")[:width] for cell in rows[i] if cell["text"].strip()}
            line = " | ".join(f"{j}:{text}" for j, text in sorted(reader.items()))
            print(f"  reader {i:>3} -> sheet {r!s:>5}: {line}")
            if r is not None:
                ref = {c: sheet.display[r, c].replace("\n", " ")[:width] for c in sorted(sheet.rows[r])}
                print(f"         sheet {r:>5}        : " + " | ".join(f"{c}:{text}" for c, text in ref.items()))
    score_path = args.run / "scores" / f"{key}.json"
    if score_path.exists():
        score = json.loads(score_path.read_text())
        errors = score["pages"][args.page - 1]["errors"]
        print(f"\n== {len(errors)} scorer errors on this page")
        for err in errors[:30]:
            print("  ", json.dumps({k: v for k, v in err.items() if k not in ("page",)}, ensure_ascii=False)[:160])
    if page["outside"]:
        print("\n== outside strings")
        for item in page["outside"][:15]:
            print("  ", repr(item["text"][:70]), [round(v) for v in item["box"]])


if __name__ == "__main__":
    main()
