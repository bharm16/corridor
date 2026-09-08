"""Every PDF prose Source Segment on the registered corpus, re-verified
against the reader-backed page text (#733).

Why this exists. ADR-0094 replaces the native engine, and a prose Source
Segment's locator is a pair of character offsets into one page's text
(ADR-0068). Offsets into a string only mean anything under the reading that
produced the string, so replacing the reader puts every historical prose
locator on the table at once. This command is the evidence for that: it takes
each segment the production segmentation appends for the registered corpus,
re-verifies it against the page text the replacement adapter would write, and
records, per segment, exactly one of three outcomes.

- **verified unchanged** — the stored offsets still recover the stored text
  from the new page text, byte for byte.
- **verified through the compatibility path** — the offsets no longer land,
  but the stored text is recovered byte for byte from the new page text at a
  position a stated rule determines, and its digest still matches. Two rules,
  in order: the stored text occurs exactly once in the new page text; or it
  occurs several times and the historical reading of the same registered bytes
  holds the same number of occurrences, so the segment's occurrence ordinal
  carries over and names one of them. Both recover the segment's exact stored
  bytes and nothing else. Neither normalizes, folds whitespace, matches
  approximately, or picks the nearest offset, which is why this is a
  verification rather than an excuse: what comes back is the segment's own
  text, at a position the rule determines rather than the reader's luck.
- **incompatible** — anything else: the text is absent from the new page text,
  or present at several positions the ordinal rule cannot separate, or the
  page itself is gone. An explained mismatch is recorded as incompatible. It
  never becomes a pass because the explanation is good.

What was tried, and rejected. Matching the segment after whitespace or
Unicode normalization would convert most of the incompatible remainder into
a pass, and would be exactly the lie ADR-0068 forbids: the segment's stored
text is its exact text, and a span that only matches once folded has not been
recovered. Binding an ambiguous locator to the occurrence nearest its old
offset was rejected for the same reason — the old and new readings do not
share a coordinate system, so "nearest" is a guess wearing a number.

The historical reading is the incumbent engine's, because that is what the
offsets were written against; there is no other way to replay a historical
locator. That import is why this module is on `ENGINE_ALLOWLIST`. It is a
one-time migration measurement, not a runtime path: nothing in the product
imports this module, and a rebinding, once performed and recorded, does not
need the old engine again.

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
# The prose locator is appended for exactly this document class today
# (`source_segments.append_ingested_source_segments`), so these are the
# segments the record actually holds. Every other PDF class is measured
# beside them, under the same segmentation, so that table-bearing and drawing
# pages are checked too rather than assumed to behave like minutes.
REGISTERED_DOC_TYPE = "minutes"
OUTCOMES = (
    "verified_unchanged",
    "verified_unique_occurrence",
    "verified_preserved_occurrence_ordinal",
    "incompatible_absent",
    "incompatible_ambiguous",
    "incompatible_page_missing",
    "historical_locator_already_invalid",
)
VERIFIED = OUTCOMES[:1]
COMPATIBLE = OUTCOMES[1:3]
INCOMPATIBLE = OUTCOMES[3:]


@dataclass
class Counts:
    pages: int = 0
    pages_projection_matches_reader_text: int = 0
    documents: int = 0
    outcomes: dict[str, int] = field(
        default_factory=lambda: {outcome: 0 for outcome in OUTCOMES}
    )

    def add(self, other: "Counts") -> None:
        self.pages += other.pages
        self.pages_projection_matches_reader_text += (
            other.pages_projection_matches_reader_text
        )
        self.documents += other.documents
        for outcome, value in other.outcomes.items():
            self.outcomes[outcome] += value

    def as_json(self) -> dict:
        segments = sum(self.outcomes.values())
        return {
            "documents": self.documents,
            "pages": self.pages,
            "pages_projection_matches_reader_text": (
                self.pages_projection_matches_reader_text
            ),
            "segments": segments,
            "verified_unchanged": sum(self.outcomes[k] for k in VERIFIED),
            "verified_through_the_compatibility_path": sum(
                self.outcomes[k] for k in COMPATIBLE
            ),
            "incompatible": sum(self.outcomes[k] for k in INCOMPATIBLE),
            "by_outcome": dict(self.outcomes),
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
) -> str:
    """One segment's outcome, under the rules this module's docstring states."""

    if historical is None or historical[
        segment.start_offset : segment.end_offset
    ] != segment.exact_text:
        # The control: the segment must dereference under the reading that
        # wrote it, or the comparison has nothing to say about the new one.
        return "historical_locator_already_invalid"
    if replacement is None:
        return "incompatible_page_missing"
    if (
        segment.end_offset <= len(replacement)
        and replacement[segment.start_offset : segment.end_offset]
        == segment.exact_text
    ):
        return "verified_unchanged"
    new_positions = occurrences(replacement, segment.exact_text)
    if not new_positions:
        return "incompatible_absent"
    if len(new_positions) == 1:
        return "verified_unique_occurrence"
    old_positions = occurrences(historical, segment.exact_text)
    if (
        len(old_positions) == len(new_positions)
        and segment.start_offset in old_positions
    ):
        return "verified_preserved_occurrence_ordinal"
    return "incompatible_ambiguous"


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
    with pymupdf.open(path) as document:
        historical = {
            index + 1: document[index].get_text() for index in range(document.page_count)
        }
    for segment in pdf_prose_segments(path):
        outcome = classify(
            segment, historical.get(segment.page_no), replacement.get(segment.page_no)
        )
        counts.outcomes[outcome] += 1
        if outcome in INCOMPATIBLE and len(samples) < 40:
            samples.append(
                {
                    "sha256": digest,
                    "page_no": segment.page_no,
                    "outcome": outcome,
                    "exact_text": segment.exact_text[:160],
                    "content_sha256": segment.content_sha256,
                    "occurrences_in_replacement": len(
                        occurrences(
                            replacement.get(segment.page_no, ""), segment.exact_text
                        )
                    ),
                }
            )
    return counts, {"pages": counts.pages, "outcomes": dict(counts.outcomes)}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-mb",
        type=float,
        default=30.0,
        help="skip registered PDFs larger than this, and record how many",
    )
    arguments = parser.parse_args()

    executor = PdfiumExecutor()
    started = time.perf_counter()
    totals = {"registered": Counts(), "extended": Counts()}
    by_doc_type: dict[str, Counts] = {}
    samples: list[dict] = []
    documents: list[dict] = []
    skipped: list[dict] = []
    failures: list[dict] = []

    for entry in registered_pdfs():
        lane = (
            "registered" if entry["doc_type"] == REGISTERED_DOC_TYPE else "extended"
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
            f"{summary['pages']:>4} pages  {sum(summary['outcomes'].values()):>5} segments",
            file=sys.stderr,
        )

    combined = Counts()
    combined.add(totals["registered"])
    combined.add(totals["extended"])
    receipt = {
        "ticket": "#733",
        "experiment": (
            "prose-locator regression: every PDF prose Source Segment the "
            "production segmentation appends for the registered corpus, "
            "re-verified against the page text the reader-backed native "
            "adapter would write"
        ),
        "what_was_compared": (
            "For each registered corpus PDF, read from the content store by "
            "SHA-256 through corridor.storage.staged_file: the prose segments "
            "corridor.source_segments.pdf_prose_segments derives from the "
            "registered bytes - the same call ingest appends from - against "
            "the page text corridor.token_layers.page_text_projection rebuilds "
            "from the reader-backed native token layer. A segment verifies only "
            "when its exact stored text is recovered byte for byte; see this "
            "module's docstring for the compatibility rules and for what was "
            "rejected."
        ),
        "lanes": {
            "registered": (
                "doc_type 'minutes': the PDFs whose prose segments the record "
                "actually holds today"
            ),
            "extended": (
                "every other registered PDF class - matrix, agreement, plan, "
                "other - under the same segmentation, so table and drawing "
                "pages are measured rather than assumed"
            ),
        },
        "an_explained_mismatch_is_not_a_pass": (
            "Segments counted incompatible are counted incompatible. The "
            "sampled reasons below characterize them; they do not reclassify "
            "them."
        ),
        "configuration": configuration_identity(executor),
        "max_mb": arguments.max_mb,
        "wall_seconds": None,
        "summary": {
            "registered": totals["registered"].as_json(),
            "extended": totals["extended"].as_json(),
            "all_registered_pdfs": combined.as_json(),
            "by_doc_type": {
                doc_type: counts.as_json()
                for doc_type, counts in sorted(by_doc_type.items())
            },
            "documents_skipped": len(skipped),
            "documents_failed": len(failures),
        },
        "skipped": skipped,
        "failures": failures,
        "incompatible_samples": samples,
        "documents": documents,
    }
    receipt["wall_seconds"] = round(time.perf_counter() - started, 1)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(receipt, indent=1) + "\n")

    for lane in ("registered", "extended", "all_registered_pdfs"):
        numbers = receipt["summary"][lane]
        print(
            f"{lane}: {numbers['segments']} segments — "
            f"{numbers['verified_unchanged']} unchanged, "
            f"{numbers['verified_through_the_compatibility_path']} compatible, "
            f"{numbers['incompatible']} incompatible"
        )
    print(f"receipt: {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
