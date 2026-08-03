"""Recall and precision against an independent enumeration.

The point of an eval is to compare the pipeline against something that did
not come out of the pipeline. Scoring extracted candidates against the
ledger they were accepted into measures the extractor against itself and
will happily report 100%.

So a gold set is a CSV somebody or something else produced, listing the
records a document should yield. `gold_from_page_text` builds one by
scanning the page text stream for utility-ID patterns — a different code
path from `find_tables()`, which is what makes it evidence rather than a
restatement.

What this cannot measure is what the source document itself leaves out. A
matrix is not ground truth for its own omissions, so every number here is
recall *against that enumeration* and is reported that way.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.db import Session as SessionFactory
from corridor.models import Candidate, Document, Project

REQUIRED_COLUMNS = ("source_ref",)

# Utility IDs across the observed layouts: FOC1-23, E92, W139, WW1**,
# FOC14-69.
#
# Anchored to the start of a line, which is what separates a row from a
# mention of one. The Notes column cites other conflicts freely — "appears
# to be part of FOC1-105", "should be combined with FOC1-106 (FOC1-51 is
# the copper cable)" — and counting those as records inflates the
# enumeration and reports recall the extractor never had a chance at. In
# the text stream a real row begins its own line; a citation of one does
# not.
# Longer prefixes first, so WW does not lose to W. The optional space and
# trailing asterisks are real: `FOC 14-36` and `WW1**` both occur.
_UTILITY_ID = re.compile(
    r"^[ \t]*((?:FOC|WW|UN|SS|E|W|G|T)[ ]?\d+(?:-\d+)?\*{0,2})[ \t]*$",
    re.MULTILINE,
)

# Stationing, offsets, sizes — what follows an id inside a Notes citation
# rather than at the head of a row.
_NUMERIC = re.compile(r"^[\d.,+'\"\-/ ]+$")


class MalformedGoldSet(Exception):
    """The gold set is unusable, and guessing at it would fake a number."""


@dataclass(frozen=True)
class GoldRecord:
    source_ref: str
    page: int | None = None


@dataclass
class EvalResult:
    project: str
    gold_total: int = 0
    extracted_total: int = 0
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    prompt_versions: dict[str, int] = field(default_factory=dict)
    models: dict[str, int] = field(default_factory=dict)
    coverage_note: str = ""

    @property
    def recall(self) -> float:
        return self.matched / self.gold_total if self.gold_total else 0.0

    @property
    def precision(self) -> float:
        return self.matched / self.extracted_total if self.extracted_total else 0.0


def load_gold(path: Path | str) -> list[GoldRecord]:
    path = Path(path)
    rows = list(csv.DictReader(path.read_text().splitlines()))
    if not rows:
        raise MalformedGoldSet(f"{path.name}: no rows")

    headers = {(h or "").strip().lower() for h in rows[0]}
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise MalformedGoldSet(
            f"{path.name}: missing column(s) {', '.join(missing)}; "
            f"found {', '.join(sorted(headers))}"
        )

    records = []
    for raw in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        ref = row.get("source_ref")
        if not ref:
            continue
        page = row.get("page")
        records.append(
            GoldRecord(source_ref=ref, page=int(page) if page and page.isdigit() else None)
        )
    if not records:
        raise MalformedGoldSet(f"{path.name}: no rows with a source_ref")
    return records


def gold_from_page_text(page_text: dict[int, str]) -> list[GoldRecord]:
    """An enumeration read off the text stream rather than the table.

    Deliberately a different code path from the one under test. It is
    coarser — it finds ids and nothing else — but an id the table parser
    never produced is a row it dropped, which is the number that matters.

    In a matrix row the id is followed by the utility owner, so a following
    line that is a number is the tell that this is a Notes citation whose
    wrap happened to leave the id alone on its line ("Nance Street, ties
    into / FOC1-6 / 1139+43"). Without that check the enumeration counts
    cross-references as rows and charges the extractor for missing them.
    """
    records = []
    for page_no in sorted(page_text):
        text = page_text[page_no] or ""
        for match in _UTILITY_ID.finditer(text):
            if not _followed_by_a_party(text, match.end()):
                continue
            records.append(GoldRecord(source_ref=match.group(1), page=page_no))
    return records


def gold_for_documents(session: Session, document_ids) -> list[GoldRecord]:
    """The enumeration over several documents, read one document at a time.

    Page numbers restart at 1 in every document, so a single page-keyed
    dictionary spanning five revisions of the same matrix keeps only the
    last text written for each page number and silently discards the rest.
    That reads as a precision collapse — every row of the overwritten
    documents becomes spurious — and it stayed invisible while only one
    Project A matrix had ever been extracted.
    """
    from corridor.models import DocPage

    records: list[GoldRecord] = []
    for document_id in sorted(document_ids):
        pages = session.execute(
            select(DocPage.page_no, DocPage.text).where(
                DocPage.document_id == document_id
            )
        ).all()
        records.extend(gold_from_page_text({p: t for p, t in pages}))
    return records


def _followed_by_a_party(text: str, start: int) -> bool:
    for line in text[start:].splitlines():
        line = line.strip()
        if not line:
            continue
        return not _NUMERIC.match(line)
    return False


def evaluate(
    session: Session, *, slug: str, gold: list[GoldRecord], kind: str = "dependency"
) -> EvalResult:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise MalformedGoldSet(f"no project {slug!r}")

    candidates = session.scalars(
        select(Candidate).where(
            Candidate.project_id == project.id, Candidate.kind == kind
        )
    ).all()

    extracted = Counter()
    versions: Counter = Counter()
    models: Counter = Counter()
    for candidate in candidates:
        ref = (candidate.payload_json.get("fields") or {}).get("utility_id")
        if ref:
            extracted[ref] += 1
        versions[candidate.prompt_version or "—"] += 1
        models[candidate.model or "deterministic"] += 1

    # Multiset, not set: ids repeat within a revision (47 reused in one
    # Project A matrix), and collapsing them would hide a dropped row
    # behind its twin.
    wanted = Counter(record.source_ref for record in gold)
    matched = sum((wanted & extracted).values())

    result = EvalResult(
        project=slug,
        gold_total=sum(wanted.values()),
        extracted_total=sum(extracted.values()),
        matched=matched,
        missing=sorted((wanted - extracted).elements()),
        spurious=sorted((extracted - wanted).elements()),
        prompt_versions=dict(versions),
        models=dict(models),
        coverage_note=(
            "Recall is measured against this enumeration only. The source "
            "document is not ground truth for its own omissions."
        ),
    )
    return result


def render(result: EvalResult) -> str:
    lines = [
        f"{result.project} — recall {result.recall:.1%}  "
        f"precision {result.precision:.1%}",
        f"  gold {result.gold_total}   extracted {result.extracted_total}   "
        f"matched {result.matched}",
    ]
    if result.missing:
        shown = ", ".join(result.missing[:12])
        more = "" if len(result.missing) <= 12 else f" (+{len(result.missing) - 12})"
        lines.append(f"  missing   {len(result.missing)}: {shown}{more}")
    if result.spurious:
        shown = ", ".join(result.spurious[:12])
        more = "" if len(result.spurious) <= 12 else f" (+{len(result.spurious) - 12})"
        lines.append(f"  spurious  {len(result.spurious)}: {shown}{more}")
    versions = ", ".join(f"{k}×{v}" for k, v in sorted(result.prompt_versions.items()))
    models = ", ".join(f"{k}×{v}" for k, v in sorted(result.models.items()))
    lines.append(f"  prompt_version: {versions or '—'}")
    lines.append(f"  model: {models or '—'}")
    lines.append(f"  note: {result.coverage_note}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """`eval <project-slug> [gold.csv]`.

    Without a CSV the enumeration is built from the stored page text of the
    project's matrices, which is independent of the table parser under test.
    """
    if not argv:
        print("usage: eval <project-slug> [gold.csv]", file=sys.stderr)
        return 2

    slug = argv[0]
    skipped: set[int] = set()
    with SessionFactory() as session:
        if len(argv) > 1:
            gold = load_gold(argv[1])
            source = argv[1]
        else:
            project = session.scalars(
                select(Project).where(Project.slug == slug)
            ).first()
            if project is None:
                print(f"no project {slug!r}", file=sys.stderr)
                return 1
            # Only documents something was actually extracted from. A
            # matrix that was ingested but never run through the extractor
            # would otherwise count every one of its rows as missed, and
            # report the backlog as a recall failure.
            extracted_docs = set(
                session.scalars(
                    select(Candidate.source_document_id).where(
                        Candidate.project_id == project.id
                    )
                ).all()
            )
            matrices = set(
                session.scalars(
                    select(Document.id).where(
                        Document.project_id == project.id,
                        Document.doc_type == "matrix",
                    )
                ).all()
            )
            skipped = matrices - extracted_docs
            if not extracted_docs:
                print(f"{slug}: nothing extracted yet", file=sys.stderr)
                return 1

            gold = gold_for_documents(session, extracted_docs)
            source = "page text (independent of the table parser)"
            if skipped:
                source += f"; {len(skipped)} ingested matrix/matrices not extracted"

        result = evaluate(session, slug=slug, gold=gold)

    print(render(result))
    if len(argv) == 1 and skipped:
        print(
            f"  {len(skipped)} ingested matrix/matrices contributed no candidates "
            "and are excluded from the enumeration, not counted as misses."
        )

    out = Path("out")
    out.mkdir(exist_ok=True)
    path = out / f"eval-{slug}.json"
    path.write_text(
        json.dumps(
            {
                "project": result.project,
                "gold_source": source,
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "recall": result.recall,
                "precision": result.precision,
                "gold_total": result.gold_total,
                "extracted_total": result.extracted_total,
                "matched": result.matched,
                "missing": result.missing,
                "spurious": result.spurious,
                "prompt_versions": result.prompt_versions,
                "models": result.models,
                "coverage_note": result.coverage_note,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\n{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
