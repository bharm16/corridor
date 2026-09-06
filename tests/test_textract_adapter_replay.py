"""Retained responses replay to the retained digests (#732, ADR-0094).

CI replays the four committed fixture responses through the normalizer twice
and compares every digest with `receipts/textract/fixture-replay.json`, so a
change to the parser or the normalizer that alters a reading is seen before
it is called the same reading. The 116-entry experiment cache is not
committed (103 MB); its replay is `make textract-replay`, and what CI can
check is that the retained receipt of that experiment is internally
consistent and says what the ticket asks: every entry replayed identically,
under every frame the lane reads recorded for it.
"""

from __future__ import annotations

import json
from pathlib import Path

from corridor_pdf_reader.textract_adapter import replay
from corridor_pdf_reader.textract_adapter.identity import ADAPTER_VERSION

FIXTURE_NAMES = [
    "holmberg-5007-page2-clean",
    "status-mobility-fy2018-page2-clean",
    "status-mobility-fy2018-page2-degraded",
    "synthetic-grid-merged",
]
EXPERIMENT_RECEIPT = replay.RECEIPTS / "experiment-cache-replay-2026-09-06.json"


def test_the_four_retained_fixtures_replay_to_the_retained_digests():
    retained = json.loads(replay.FIXTURE_RECEIPT.read_text(encoding="utf-8"))

    entries = replay.replay_fixtures(passes=2)

    assert [entry["name"] for entry in entries] == FIXTURE_NAMES
    assert entries == retained["entries"]
    summary = replay.summarize(entries)
    assert summary == retained["summary"]
    assert summary["all_identical"] and summary["entries"] == 4 and summary["readings"] == 4
    assert summary["model_versions"] == {"1.0": 4}
    assert summary["distinct_raw_response_digests"] == 4
    for entry in entries:
        reading = entry["readings"][0]
        assert reading["digests"][0] == reading["digests"][1] and reading["identical"]
        assert reading["tables"] >= 1 and reading["cells"] >= 1
        assert reading["normalization"] == {"text_source": "textract-words", "parser": "textract-analyze-document-tables-v1", "remap_margin": 0.0, "rescue_runs": False}
    assert retained["adapter"]["adapter_version"] == ADAPTER_VERSION and retained["passes"] == 2


def test_a_different_frame_changes_the_reading_digest_but_never_the_raw_one():
    entry = json.loads((replay.FIXTURES / "holmberg-5007-page2-clean.json").read_text(encoding="utf-8"))
    declared = replay.fixture_frame(entry, "holmberg")
    doubled = replay.Frame((declared.size[0] * 2, declared.size[1]), 0, "test: doubled width")

    one = replay.replay_entry(entry, "holmberg", [declared, doubled])

    assert one["raw_response_digest"] == replay.replay_entry(entry, "holmberg", [doubled])["raw_response_digest"]
    first, second = one["readings"]
    assert first["digests"] != second["digests"] and first["identical"] and second["identical"]
    assert first["frame"]["from"] == "holmberg: displayed_size" and second["frame"]["from"] == "test: doubled width"
    assert first["cells"] == second["cells"] == 16


def test_frames_come_from_the_retained_reads_with_their_provenance(tmp_path: Path):
    root = tmp_path / "results"
    (root / "lane-B" / "reads").mkdir(parents=True)
    (root / "lane-A").mkdir()
    (root / "lane-B" / "reads" / "k1.json").write_text(json.dumps({"pages": [
        {"number": 1, "size": [1224.0, 792.0], "rotation": 0, "source": {"png_sha256": "a" * 64}},
        {"number": 2, "size": [612.0, 792.0], "rotation": 0, "source": {"png_sha256": "b" * 64}},
        {"number": 3, "size": [612.0, 792.0], "rotation": 0, "tables": []},
    ]}))
    (root / "lane-A" / "document.json").write_text(json.dumps({"pages": [
        {"number": 1, "size": [792.0, 1224.0], "rotation": 90, "source": {"png_sha256": "a" * 64}},
        {"number": 2, "size": [612.0, 792.0], "rotation": 0, "source": {"png_sha256": "b" * 64}},
    ]}))

    frames = replay.frames_from_reads([root])

    assert [(f.size, f.rotation, f.source) for f in frames["a" * 64]] == [
        ((1224.0, 792.0), 0, "results/lane-B/reads/k1.json page 1"),
        ((792.0, 1224.0), 90, "results/lane-A/document.json page 1"),
    ]
    assert len(frames["b" * 64]) == 1, "the same frame twice is one frame"
    assert set(frames) == {"a" * 64, "b" * 64}


def test_the_experiment_cache_replay_receipt_is_retained_and_consistent():
    receipt = json.loads(EXPERIMENT_RECEIPT.read_text(encoding="utf-8"))
    summary = receipt["summary"]

    assert receipt["kind"] == "textract-retained-response-replay" and receipt["passes"] == 2
    assert summary == replay.summarize(receipt["entries"])
    assert summary["entries"] == 116 and summary["entries_without_a_frame"] == 0
    assert summary["readings"] == 118, "two rasters are shared by an original and its twin, and carry both frames"
    assert summary["identical_readings"] == 118 and summary["all_identical"]
    assert summary["distinct_raw_response_digests"] == 116
    assert summary["model_versions"] == {"1.0": 116}
    assert receipt["source"]["entries"] == 116 and receipt["source"]["rasters_with_frames"] >= 116
    assert receipt["adapter"]["adapter_version"] == ADAPTER_VERSION
    shas = [entry["raster_sha256"] for entry in receipt["entries"]]
    assert len(set(shas)) == 116 and all(len(sha) == 64 for sha in shas)
    assert all(entry["feature_types"] == ["TABLES"] for entry in receipt["entries"])
    assert all(reading["digests"][0] == reading["digests"][1] for entry in receipt["entries"] for reading in entry["readings"])
