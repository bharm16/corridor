"""Measure reconstructed incumbent prose against the reader's text (#733, #736).

Why this exists. ADR-0094 replaces the native engine, and a prose Source
Segment's locator is a pair of character offsets into one page's text
(ADR-0068). Offsets into a string only mean anything under the reading that
produced the string, so replacing the reader puts every historical prose
locator at risk. This command reconstructs the incumbent segmentation from
the corpus's original bytes on each run. It does not query persisted Source
Segments, accepted references, or customer citations. Its denominator is a
corpus-generated population, not an inventory of existing customer records.

Exact-text recovery and physical source-location equivalence are separate
outcomes. The text classification records matching offsets, one occurrence,
equal occurrence counts, multiple occurrences, or absent text. Even identical
strings at identical offsets can describe different physical labels when a
reader reverses their order. Equal counts and occurrence ordinals do not prove
which label survived; neither does a single replacement occurrence when the
historical reading contained several.

Every source-location outcome in this experiment is **unknown**. The experiment
does not collect independently discriminating coordinates bound to each exact
historical and replacement span in a common source frame. A word box or a page
number without that mapping cannot close this gap. No text outcome authorizes
rebinding, and this command never returns a replacement locator. #447 owns
historical compatibility qualification; #741 owns retained-citation proof.

What was tried, and rejected. Whitespace or Unicode normalization can conceal
missing exact text: a span that only matches once folded has not been
recovered. Binding an ambiguous locator to the occurrence nearest its old
offset was rejected for the same reason — the old and new readings do not
share a coordinate system, so "nearest" is a guess wearing a number.

The reconstructed historical reading is the incumbent engine's. That import
is why this module is on `ENGINE_ALLOWLIST`: nothing in the product imports
this measurement, and #741 owns its removal. Replaying persisted locators
under their recorded reading identity is a separate obligation, not something
this corpus reconstruction proves. The original #733 receipt remains frozen;
new receipts clarify its interpretation without changing its bytes.

The command reads the content store only through `corridor.storage`, and
writes nothing into it.

    make prose-locator-regression ARGS="--output artifacts/.../receipt.json"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import pymupdf

from corridor.source_segments import ProseSegment, pdf_prose_segments
from corridor.storage import staged_file
from corridor.token_layers import (
    READER_ENGINE,
    READER_NATIVE_ADAPTER_VERSION,
    page_text_projection,
    read_native_token_layers,
)
from corridor_pdf_reader import provenance
from corridor_pdf_reader.execution import (
    MEASURED_DPI,
    MEASURED_ENGINE,
    PdfiumExecutor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "corpus"
# Minutes are the incumbent prose-ingest class, but this script reconstructs
# them from corpus bytes. It does not establish that any generated segment
# was persisted, cited, or accepted. Other PDF classes use the same algorithm
# to include table-bearing and drawing pages in the text experiment.
PROSE_DOC_TYPE = "minutes"
TEXT_OUTCOMES = (
    "exact_text_at_recorded_offsets",
    "exact_text_unique_candidate",
    "exact_text_equal_count_ordinal_candidate",
    "exact_text_multiple_candidates",
    "exact_text_absent",
    "replacement_page_missing",
    "historical_locator_invalid",
)
TEXT_RECOVERED = TEXT_OUTCOMES[:4]
TEXT_NOT_RECOVERED = TEXT_OUTCOMES[4:6]
FROZEN_RECEIPT = (
    REPO_ROOT / "artifacts/pdf-reader-native-layer/733-prose-locator-regression.json"
)


# Traits characterize exact-text failures; they do not excuse a mismatch or
# imply that every mismatch containing a trait was caused by that trait.
TEXT_FAILURE_TRAITS = ("contains_line_break", "contains_soft_hyphen")


@dataclass(frozen=True)
class Comparison:
    """Text evidence only; an unresolved location cannot emit a new locator."""

    text_outcome: str
    candidate_start_offsets: tuple[int, ...] = ()
    source_location_outcome: str = field(default="unknown", init=False)
    source_location_reason: str = field(
        default="independent_occurrence_coordinates_not_collected", init=False
    )
    rebound_start_offset: None = field(default=None, init=False)


@dataclass
class Counts:
    pages: int = 0
    pages_projection_matches_reader_text: int = 0
    documents: int = 0
    text_outcomes: dict[str, int] = field(
        default_factory=lambda: {outcome: 0 for outcome in TEXT_OUTCOMES}
    )
    text_failure_traits: dict[str, int] = field(
        default_factory=lambda: {trait: 0 for trait in TEXT_FAILURE_TRAITS}
    )

    def add(self, other: "Counts") -> None:
        self.pages += other.pages
        self.pages_projection_matches_reader_text += (
            other.pages_projection_matches_reader_text
        )
        self.documents += other.documents
        for outcome, value in other.text_outcomes.items():
            self.text_outcomes[outcome] += value
        for trait, value in other.text_failure_traits.items():
            self.text_failure_traits[trait] += value

    def as_json(self) -> dict:
        segments = sum(self.text_outcomes.values())
        return {
            "documents": self.documents,
            "pages": self.pages,
            "pages_projection_matches_reader_text": (
                self.pages_projection_matches_reader_text
            ),
            "segments": segments,
            "exact_text_recovered": sum(self.text_outcomes[k] for k in TEXT_RECOVERED),
            "exact_text_not_recovered": sum(
                self.text_outcomes[k] for k in TEXT_NOT_RECOVERED
            ),
            "historical_locator_invalid": self.text_outcomes["historical_locator_invalid"],
            "by_text_outcome": dict(self.text_outcomes),
            "by_source_location_outcome": {"unknown": segments},
            "replacement_locators_proved": 0,
            "text_failure_traits": dict(self.text_failure_traits),
        }


def registered_pdfs() -> list[dict]:
    """Every PDF the corpus manifests register, once each, by digest."""

    documents: dict[str, dict] = {}
    for manifest in sorted(CORPUS.glob("*.lock.json")):
        for url, source in json.loads(manifest.read_text()).get("sources", {}).items():
            if not str(source.get("local_path", "")).endswith(".pdf"):
                continue
            documents.setdefault(
                source["sha256"],
                {
                    "sha256": source["sha256"],
                    "doc_type": source["doc_type"],
                    "bytes": source["bytes"],
                    "manifest": manifest.name,
                    "url": url,
                },
            )
    return [documents[digest] for digest in sorted(documents)]


def occurrences(text: str, needle: str) -> list[int]:
    if not needle:
        return []
    found: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return found
        found.append(index)
        start = index + 1


def classify(
    segment: ProseSegment, historical: str | None, replacement: str | None
) -> Comparison:
    """Find exact text candidates without asserting a physical occurrence."""

    if (
        historical is None
        or not 0 <= segment.start_offset < segment.end_offset <= len(historical)
        or historical[segment.start_offset : segment.end_offset] != segment.exact_text
        or sha256(segment.exact_text.encode("utf-8")).hexdigest() != segment.content_sha256
    ):
        # The control: the segment must dereference under the reading that
        # wrote it, or the comparison has nothing to say about the new one.
        return Comparison("historical_locator_invalid")
    if replacement is None:
        return Comparison("replacement_page_missing")
    new_positions = tuple(occurrences(replacement, segment.exact_text))
    if not new_positions:
        return Comparison("exact_text_absent")
    if (
        segment.end_offset <= len(replacement)
        and replacement[segment.start_offset : segment.end_offset]
        == segment.exact_text
    ):
        return Comparison("exact_text_at_recorded_offsets", new_positions)
    if len(new_positions) == 1:
        return Comparison("exact_text_unique_candidate", new_positions)
    old_positions = occurrences(historical, segment.exact_text)
    if (
        len(old_positions) == len(new_positions)
        and segment.start_offset in old_positions
    ):
        return Comparison("exact_text_equal_count_ordinal_candidate", new_positions)
    return Comparison("exact_text_multiple_candidates", new_positions)


def historical_page_texts(path: Path) -> dict[int, str]:
    """Reconstruct the incumbent strings, without implying persisted citations."""

    with pymupdf.open(path) as document:
        return {
            index + 1: document[index].get_text() for index in range(document.page_count)
        }


def measure_document(
    path: Path, executor: PdfiumExecutor, samples: list[dict], digest: str
) -> tuple[Counts, dict]:
    counts = Counts(documents=1)
    layers = read_native_token_layers(path, source_sha256=digest, executor=executor)
    replacement = {layer.page_no: page_text_projection(layer) for layer in layers}
    counts.pages = len(layers)
    counts.pages_projection_matches_reader_text = sum(
        1 for layer in layers if layer.quality["projection_matches_reader_text"]
    )
    historical = historical_page_texts(path)
    for segment in pdf_prose_segments(path):
        comparison = classify(
            segment, historical.get(segment.page_no), replacement.get(segment.page_no)
        )
        outcome = comparison.text_outcome
        counts.text_outcomes[outcome] += 1
        if outcome in TEXT_NOT_RECOVERED:
            if "\n" in segment.exact_text:
                counts.text_failure_traits["contains_line_break"] += 1
            if "\u00ad" in segment.exact_text:
                counts.text_failure_traits["contains_soft_hyphen"] += 1
        if outcome not in TEXT_RECOVERED and len(samples) < 40:
            samples.append(
                {
                    "sha256": digest,
                    "page_no": segment.page_no,
                    "text_outcome": outcome,
                    "source_location_outcome": comparison.source_location_outcome,
                    "exact_text": segment.exact_text[:160],
                    "content_sha256": segment.content_sha256,
                    "occurrences_in_replacement": len(
                        occurrences(
                            replacement.get(segment.page_no, ""), segment.exact_text
                        )
                    ),
                }
            )
    return counts, counts.as_json()


def configuration_identity(executor: PdfiumExecutor) -> dict:
    return {
        "native_layer": {
            "engine": READER_ENGINE,
            "adapter_version": READER_NATIVE_ADAPTER_VERSION,
            "reader_engine": MEASURED_ENGINE,
            "dpi": MEASURED_DPI,
            "source_commit": provenance.SOURCE_COMMIT,
            "package_digest": provenance.package_digest(),
        },
        "historical_reading": {
            "engine": "pymupdf",
            "engine_version": getattr(pymupdf, "__version__", "unknown"),
        },
        "execution_contract": executor.contract(),
        "corpus_manifests": {
            manifest.name: sha256(manifest.read_bytes()).hexdigest()
            for manifest in sorted(CORPUS.glob("*.lock.json"))
        },
        "implementation_files": {
            relative: sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
            for relative in (
                "scripts/prose_locator_regression.py",
                "src/corridor/source_segments.py",
                "src/corridor/token_layers.py",
            )
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "corridor_commit": subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-mb",
        type=float,
        default=30.0,
        help="skip registered PDFs larger than this, and record how many",
    )
    arguments = parser.parse_args(argv)
    if arguments.output.exists():
        parser.error("receipt already exists; choose a new output path to preserve history")

    executor = PdfiumExecutor()
    started = time.perf_counter()
    configuration = configuration_identity(executor)
    totals = {"minutes_population": Counts(), "extended_population": Counts()}
    by_doc_type: dict[str, Counts] = {}
    samples: list[dict] = []
    documents: list[dict] = []
    skipped: list[dict] = []
    failures: list[dict] = []

    for entry in registered_pdfs():
        lane = (
            "minutes_population"
            if entry["doc_type"] == PROSE_DOC_TYPE
            else "extended_population"
        )
        if entry["bytes"] > arguments.max_mb * 1_000_000:
            skipped.append({**entry, "reason": "larger than --max-mb"})
            continue
        path = staged_file(entry["sha256"])
        if path is None:
            skipped.append({**entry, "reason": "not in the content store"})
            continue
        try:
            counts, summary = measure_document(
                path, executor, samples, entry["sha256"]
            )
        except Exception as error:  # a reader failure is a result, not a crash
            failures.append(
                {**entry, "error_type": type(error).__name__, "error": str(error)}
            )
            print(f"  ! {entry['sha256'][:12]} {type(error).__name__}: {error}", file=sys.stderr)
            continue
        totals[lane].add(counts)
        by_doc_type.setdefault(entry["doc_type"], Counts()).add(counts)
        documents.append({**entry, "lane": lane, **summary})
        print(
            f"  {entry['sha256'][:12]} {entry['doc_type']:<9} "
            f"{summary['pages']:>4} pages  {summary['segments']:>5} generated segments",
            file=sys.stderr,
        )

    combined = Counts()
    combined.add(totals["minutes_population"])
    combined.add(totals["extended_population"])
    receipt = {
        "schema_version": "corridor.prose-locator-regression.v2",
        "ticket": "#736",
        "clarifies_frozen_receipt": {
            "path": str(FROZEN_RECEIPT.relative_to(REPO_ROOT)),
            "sha256": sha256(FROZEN_RECEIPT.read_bytes()).hexdigest(),
        },
        "experiment": (
            "Exact-text comparison of the incumbent prose segmentation "
            "reconstructed from registered corpus PDFs with the new native "
            "page text. Physical source-location equivalence is unknown."
        ),
        "what_was_compared": (
            "For each registered corpus PDF, read from the content store by "
            "SHA-256 through corridor.storage.staged_file: the prose segments "
            "corridor.source_segments.pdf_prose_segments reconstructs from "
            "the original bytes using the incumbent algorithm against "
            "the page text corridor.token_layers.page_text_projection rebuilds "
            "from the reader-backed native token layer. The script never "
            "queries stored Source Segments, accepted references or customer "
            "citations; no count is a count of broken customer citations."
        ),
        "source_location_evidence": (
            "Not collected: the comparison has no independently discriminating "
            "coordinates bound to each historical and replacement occurrence "
            "in a common source frame. Same offsets, unique text, equal counts "
            "and occurrence ordinal are text candidates only. Every location "
            "is unknown; no replacement locator is proved or emitted."
        ),
        "qualification": (
            "No text category is a historical-compatibility pass. #447 owns "
            "qualification; #741 owns retained-citation preservation with the "
            "old engines absent. Existing readings and citations are not changed."
        ),
        "lanes": {
            "minutes_population": (
                "doc_type 'minutes': the incumbent prose-ingest class, "
                "reconstructed without checking database persistence or citations"
            ),
            "extended_population": (
                "every other registered PDF class - matrix, agreement, plan, "
                "other - under the same segmentation, so table and drawing "
                "pages are measured rather than assumed"
            ),
        },
        "an_explained_mismatch_is_not_a_pass": (
            "Missing exact text stays missing. No whitespace, soft-hyphen or "
            "Unicode normalization turns a mismatch into a recovery. Traits "
            "characterize failures and do not establish their cause."
        ),
        "configuration": configuration,
        "max_mb": arguments.max_mb,
        "wall_seconds": None,
        "summary": {
            "minutes_population": totals["minutes_population"].as_json(),
            "extended_population": totals["extended_population"].as_json(),
            "all_pdf_population": combined.as_json(),
            "by_doc_type": {
                doc_type: counts.as_json()
                for doc_type, counts in sorted(by_doc_type.items())
            },
            "documents_skipped": len(skipped),
            "documents_failed": len(failures),
        },
        "skipped": skipped,
        "failures": failures,
        "text_failure_samples": samples,
        "documents": documents,
    }
    receipt["wall_seconds"] = round(time.perf_counter() - started, 1)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects history if another run wins the race
    # after the early argument check.
    with arguments.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, indent=1) + "\n")

    for lane in ("minutes_population", "extended_population", "all_pdf_population"):
        numbers = receipt["summary"][lane]
        print(
            f"{lane}: {numbers['segments']} segments — "
            f"{numbers['exact_text_recovered']} exact text recovered, "
            f"{numbers['exact_text_not_recovered']} exact text not recovered, "
            f"{numbers['historical_locator_invalid']} invalid historical controls; "
            f"{numbers['by_source_location_outcome']['unknown']} locations unknown"
        )
    print(f"receipt: {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
