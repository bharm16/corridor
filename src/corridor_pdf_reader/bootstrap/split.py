"""Seal a stratified holdout once. Nothing in the loop may tune on it.

Strata are agency and PDF producer family, because those are the axes along
which page construction differs. The split is deterministic under the repo's
existing seed so it can be regenerated and checked, never quietly changed.
"""

from __future__ import annotations

import json
import random
import subprocess
from collections import defaultdict

from corridor_pdf_reader.bootstrap.corpus import PAIRS_FILE, Pair, load_pairs

HOLDOUT_FRACTION = 0.2
SEED = 720

FAMILIES = (
    ("Excel", "Excel"),
    ("Print To PDF", "PrintToPDF"),
    ("Ghostscript", "Ghostscript"),
    ("Distiller", "Distiller"),
    ("Bluebeam", "Bluebeam"),
    ("Word", "Word"),
    ("Adobe PDF Library", "AdobePDFLibrary"),
    ("Reporting Services", "ReportingServices"),
    ("LibreOffice", "LibreOffice"),
)


def pdf_info(pair: Pair) -> tuple[str, bool]:
    """Producer family and tagged flag from Poppler's pdfinfo (a witness tool)."""
    output = subprocess.run(
        ["pdfinfo", str(pair.pdf_path)], capture_output=True, text=True, check=True
    ).stdout
    info = dict(
        (line.split(":", 1)[0].strip(), line.split(":", 1)[1].strip())
        for line in output.splitlines()
        if ":" in line
    )
    producer = info.get("Producer", "") or info.get("Creator", "")
    family = next((name for needle, name in FAMILIES if needle in producer), "Other")
    return family, info.get("Tagged", "") == "yes"


def main() -> None:
    pairs = load_pairs()
    strata: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    details: dict[str, tuple[str, bool]] = {}
    for pair in pairs:
        family, tagged = pdf_info(pair)
        details[pair.key] = (family, tagged)
        strata[(pair.agency, family)].append(pair)
    rng = random.Random(SEED)
    holdout: set[str] = set()
    for stratum in sorted(strata):
        members = sorted(strata[stratum], key=lambda pair: pair.key)
        rng.shuffle(members)
        count = round(len(members) * HOLDOUT_FRACTION)
        holdout.update(pair.key for pair in members[:count])
    records = []
    for pair in sorted(pairs, key=lambda pair: pair.key):
        family, tagged = details[pair.key]
        records.append(
            {
                "key": pair.key,
                "folder": pair.folder,
                "pdf": pair.pdf,
                "spreadsheet": pair.spreadsheet,
                "agency": pair.agency,
                "family": family,
                "tagged": tagged,
                "tier": pair.tier,
                "reading": pair.reading,
                "pages": pair.pages,
                "visible_cells": pair.visible_cells,
                "holdout": pair.key in holdout,
            }
        )
    PAIRS_FILE.write_text(
        json.dumps(
            {
                "seed": SEED,
                "holdout_fraction": HOLDOUT_FRACTION,
                "strata": "agency x producer family",
                "pairs": records,
            },
            indent=1,
        )
        + "\n"
    )
    print(
        f"{len(records)} pairs, {len(holdout)} sealed holdout, "
        f"{len(strata)} strata -> {PAIRS_FILE}"
    )


if __name__ == "__main__":
    main()
