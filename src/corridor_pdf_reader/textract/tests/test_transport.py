"""Entries fetched by URL land in the cache only when they are what they claim."""

from __future__ import annotations

import json
from pathlib import Path

from corridor_pdf_reader.textract.client import cache_entry, read_entry, write_entry
from corridor_pdf_reader.textract.transport import get, pending


def test_pending_lists_pngs_without_an_entry(tmp_path: Path) -> None:
    pngs, cache = tmp_path / "png", tmp_path / "cache"
    pngs.mkdir()
    (pngs / ("a" * 64 + ".png")).write_bytes(b"x")
    (pngs / ("b" * 64 + ".png")).write_bytes(b"yy")
    write_entry(cache, cache_entry("a" * 64, 1, {"Blocks": []}, transport="test", attempts=1))
    assert pending(pngs, cache) == [{"sha256": "b" * 64, "bytes": 2}]


def test_get_keeps_matching_entries_and_refuses_others(tmp_path: Path) -> None:
    good = cache_entry("c" * 64, 3, {"Blocks": [{"BlockType": "PAGE", "Id": "p"}]}, transport="connector", attempts=2)
    (tmp_path / "good.json").write_text(json.dumps(good))
    (tmp_path / "wrong.json").write_text(json.dumps({**good, "sha256": "d" * 64}))
    (tmp_path / "broken.json").write_text("{")
    cache = tmp_path / "cache"
    receipts = get({"c" * 64: (tmp_path / "good.json").as_uri(), "e" * 64: (tmp_path / "wrong.json").as_uri(), "f" * 64: (tmp_path / "broken.json").as_uri()}, cache)
    assert receipts[0] == {"sha256": "c" * 64, "blocks": 1, "attempts": 2}
    assert "error" in receipts[1] and "error" in receipts[2]
    assert read_entry(cache, "c" * 64) == good and read_entry(cache, "e" * 64) is None
