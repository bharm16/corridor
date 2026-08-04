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

A gold set may also label which of its rows are **critical**, which is the
one number M7's gate turns on (≥95%). It is scored from those labels alone
— the extractor classifies nothing — so it is independent of the
Ledger-side criticality work. `gold_from_page_text` cannot know, and says
so rather than reporting zero.
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
from corridor.verify import unverified_fields

REQUIRED_COLUMNS = ("source_ref",)

# The optional label M7's gate is scored on. Spelled out rather than
# "anything non-empty is true", so a typo raises instead of silently
# reading as not-critical: an unrecognised label swallowed as `False`
# shrinks the denominator, and recall then reads highest exactly when a row
# went missing from the measurement.
CRITICAL_TRUE = frozenset({"1", "true", "t", "yes", "y", "critical"})
CRITICAL_FALSE = frozenset({"", "0", "false", "f", "no", "n", "normal"})

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

# FDOT numbers its conflicts 1, 2, 3 rather than prefixing them, and a bare
# integer alone on a line is otherwise indistinguishable from an offset, a
# sheet number or a quantity. What identifies it is what follows: FDOT
# prints the row's stationing next.
#
# This shape is a **fallback**, tried only on a document where the prefixed
# shape found nothing, and that is not fastidiousness. TxDOT prints an
# offset after every station, so `303` followed by `1153+17` matches this
# rule perfectly — measured at 668, 531 and 666 phantom rows on three
# Project A revisions. Right answer on one layout, ruinous on the other,
# which is why it can never run alongside the first.
_SEQUENTIAL_ID = re.compile(r"^[ \t]*(\d{1,3})[ \t]*$", re.MULTILINE)
_STATION = re.compile(r"^[ \t]*\d{1,5}\+\d{2}(?:\.\d+)?\b")


class MalformedGoldSet(Exception):
    """The gold set is unusable, and guessing at it would fake a number."""


@dataclass(frozen=True)
class GoldRecord:
    source_ref: str
    page: int | None = None
    # `None` means this gold set does not label criticality at all, which
    # is a different fact from "labelled, and this row is not critical".
    # Carried on the record rather than passed beside the list so a caller
    # cannot forget it and turn a real score into NOT MEASURED.
    critical: bool | None = None


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
    # Rows carrying a value that is not text on the cited page. Reported
    # beside recall because the two measure different things and the
    # ADR-0006 gate turned entirely on the second: two paths matched to
    # within 0.3 points on recall while differing fifty-fold here.
    field_failures: int = 0
    # No enumeration could be built, so nothing here is a score. 0% recall
    # says the extractor found nothing; an empty gold set says we could not
    # check. Reporting the second as the first read as total failure on a
    # document that had extracted 66 correct rows.
    unmeasurable: bool = False
    coverage_note: str = ""
    # M7's gate is "≥95% recall on labeled critical dependencies". Scored
    # from the gold labels alone — the extractor classifies nothing — which
    # is what keeps this independent of the Ledger-side work (#86).
    critical_gold_total: int = 0
    critical_matched: int = 0
    critical_missing: list[str] = field(default_factory=list)
    # Whether the gold set labels criticality at all. False makes critical
    # recall unmeasurable for the same reason an empty enumeration does.
    critical_labeled: bool = False

    @property
    def recall(self) -> float:
        return self.matched / self.gold_total if self.gold_total else 0.0

    @property
    def precision(self) -> float:
        return self.matched / self.extracted_total if self.extracted_total else 0.0

    @property
    def critical_recall(self) -> float:
        return (
            self.critical_matched / self.critical_gold_total
            if self.critical_gold_total
            else 0.0
        )

    @property
    def critical_unmeasurable(self) -> bool:
        """No labeled critical set, so there is nothing to score.

        Distinct from a recall of zero, which says the extractor missed
        every critical row. Reporting the first as the second is #82's
        mistake one metric further in, and on the holdout ADR-0008 spends
        once it cannot be taken back.
        """
        return not self.critical_labeled or self.critical_gold_total == 0


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

    labels_criticality = "critical" in headers

    records = []
    for raw in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        ref = row.get("source_ref")
        if not ref:
            continue
        page = row.get("page")
        records.append(
            GoldRecord(
                source_ref=ref,
                page=int(page) if page and page.isdigit() else None,
                critical=_critical(path, row) if labels_criticality else None,
            )
        )
    if not records:
        raise MalformedGoldSet(f"{path.name}: no rows with a source_ref")
    return records


def _critical(path: Path, row: dict[str, str]) -> bool:
    value = (row.get("critical") or "").lower()
    if value in CRITICAL_TRUE:
        return True
    if value in CRITICAL_FALSE:
        return False
    raise MalformedGoldSet(
        f"{path.name}: row {row.get('source_ref')!r} has critical={value!r}, "
        f"which is neither {'/'.join(sorted(CRITICAL_TRUE))} nor "
        f"{'/'.join(sorted(c for c in CRITICAL_FALSE if c))}. Reading it as "
        "not-critical would drop the row from the denominator the M7 gate "
        "is scored on."
    )


def gold_from_page_text(page_text: dict[int, str]) -> list[GoldRecord]:
    """An enumeration read off the text stream rather than the table.

    Two row shapes are known, and they are tried in order because their
    discriminators are inverted: a TxDOT row's id is followed by the
    owner, an FDOT row's by its stationing. Applying both at once would
    let each layout's rule fire on the other's data. First shape to find
    anything wins, per document.

    Deliberately a different code path from the one under test. It is
    coarser — it finds ids and nothing else — but an id the table parser
    never produced is a row it dropped, which is the number that matters.

    In a matrix row the id is followed by the utility owner, so a following
    line that is a number is the tell that this is a Notes citation whose
    wrap happened to leave the id alone on its line ("Nance Street, ties
    into / FOC1-6 / 1139+43"). Without that check the enumeration counts
    cross-references as rows and charges the extractor for missing them.
    """
    for pattern, follows in (
        (_UTILITY_ID, _followed_by_a_party),
        (_SEQUENTIAL_ID, _followed_by_stationing),
    ):
        records = []
        for page_no in sorted(page_text):
            text = page_text[page_no] or ""
            for match in pattern.finditer(text):
                if not follows(text, match.end()):
                    continue
                records.append(GoldRecord(source_ref=match.group(1), page=page_no))
        if records:
            return records
    return []


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


def _followed_by_stationing(text: str, start: int) -> bool:
    return bool(_STATION.match(_next_line(text, start) or ""))


def _next_line(text: str, start: int) -> str | None:
    for line in text[start:].splitlines():
        if line.strip():
            return line
    return None


def _followed_by_a_party(text: str, start: int) -> bool:
    for line in text[start:].splitlines():
        line = line.strip()
        if not line:
            continue
        return not _NUMERIC.match(line)
    return False


def extracted_documents(
    session: Session, project_id: int, *, prompt_version: str | None = None
) -> set[int]:
    """Documents this extractor actually read.

    Only these belong in the enumeration. A matrix that was ingested but
    never run through the extractor would otherwise count every one of its
    rows as missed, and report the backlog as a recall failure.
    """
    query = select(Candidate.source_document_id).where(
        Candidate.project_id == project_id
    )
    if prompt_version:
        query = query.where(Candidate.prompt_version == prompt_version)
    return set(session.scalars(query.distinct()).all())


def _page_text(session: Session, document_ids) -> dict[tuple[int, int], str]:
    from corridor.models import DocPage

    if not document_ids:
        return {}
    rows = session.execute(
        select(DocPage.document_id, DocPage.page_no, DocPage.text).where(
            DocPage.document_id.in_(document_ids)
        )
    ).all()
    return {(d, p): (t or "") for d, p, t in rows}


def evaluate(
    session: Session,
    *,
    slug: str,
    gold: list[GoldRecord],
    kind: str = "dependency",
    prompt_version: str | None = None,
) -> EvalResult:
    """Score one extractor's output.

    `prompt_version` scopes the run to a single extraction path. Two paths'
    Candidates coexist on a project while a migration is undecided, and
    pooled they are meaningless: every row appears twice, so recall reads
    100% and precision reads 50% no matter how either extractor did.
    """
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise MalformedGoldSet(f"no project {slug!r}")

    query = select(Candidate).where(
        Candidate.project_id == project.id, Candidate.kind == kind
    )
    if prompt_version:
        query = query.where(Candidate.prompt_version == prompt_version)
    candidates = session.scalars(query).all()

    extracted = Counter()
    versions: Counter = Counter()
    models: Counter = Counter()
    page_text = _page_text(session, {c.source_document_id for c in candidates})
    field_failures = 0
    for candidate in candidates:
        ref = (candidate.payload_json.get("fields") or {}).get("utility_id")
        if ref:
            extracted[ref] += 1
        versions[candidate.prompt_version or "—"] += 1
        models[candidate.model or "deterministic"] += 1
        text = page_text.get(
            (candidate.source_document_id, (candidate.source_pages or [0])[0]), ""
        )
        if unverified_fields(candidate.payload_json.get("fields") or {}, text):
            field_failures += 1

    # Multiset, not set: ids repeat within a revision (47 reused in one
    # Project A matrix), and collapsing them would hide a dropped row
    # behind its twin.
    wanted = Counter(record.source_ref for record in gold)
    matched = sum((wanted & extracted).values())

    # Scored the same way, over the labeled subset. An id that appears in
    # the gold set twice — once critical, once not — credits the critical
    # copy from a single extraction, because `source_ref` is the only join
    # key either metric has. Overall recall still records the miss.
    critical_wanted = Counter(r.source_ref for r in gold if r.critical)
    critical_matched = sum((critical_wanted & extracted).values())

    result = EvalResult(
        project=slug,
        gold_total=sum(wanted.values()),
        extracted_total=sum(extracted.values()),
        matched=matched,
        missing=sorted((wanted - extracted).elements()),
        spurious=sorted((extracted - wanted).elements()),
        prompt_versions=dict(versions),
        models=dict(models),
        field_failures=field_failures,
        unmeasurable=not gold,
        critical_gold_total=sum(critical_wanted.values()),
        critical_matched=critical_matched,
        critical_missing=sorted((critical_wanted - extracted).elements()),
        critical_labeled=any(r.critical is not None for r in gold),
        coverage_note=(
            "Recall is measured against this enumeration only. The source "
            "document is not ground truth for its own omissions."
        ),
    )
    return result


def render(result: EvalResult) -> str:
    if result.unmeasurable:
        return "\n".join(
            [
                f"{result.project} — NOT MEASURED: the gold set is empty",
                f"  {result.extracted_total} rows were extracted, and none of "
                "them could be checked.",
                "  This document's rows could not be enumerated from its page "
                "text by any known",
                "  layout, so there is nothing to score against. It is not a "
                "recall of zero.",
                "  Supply a hand-authored enumeration: "
                f"make eval ARGS=\"{result.project} gold.csv\"",
            ]
        )

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
    share = (
        100 * result.field_failures / result.extracted_total
        if result.extracted_total
        else 0.0
    )
    lines.append(
        f"  field-token failures  {result.field_failures} "
        f"({share:.2f}% of extracted rows carry a value not on their page)"
    )
    lines.extend(_critical_lines(result))
    versions = ", ".join(f"{k}×{v}" for k, v in sorted(result.prompt_versions.items()))
    models = ", ".join(f"{k}×{v}" for k, v in sorted(result.models.items()))
    lines.append(f"  prompt_version: {versions or '—'}")
    lines.append(f"  model: {models or '—'}")
    lines.append(f"  note: {result.coverage_note}")
    return "\n".join(lines)


def _critical_lines(result: EvalResult) -> list[str]:
    """M7's gate number, or an explicit statement that there isn't one.

    Never "critical recall 0.0%" for a set nobody labeled. The gate reads
    ≥95% here, and a zero that means "unlabeled" is indistinguishable from
    one that means "found none of them" at exactly the moment that
    distinction decides whether a holdout was passed or failed.
    """
    if not result.critical_labeled:
        return [
            "  critical recall  NOT MEASURED: this gold set does not label "
            "criticality",
            "    Add a `critical` column to score the M7 gate's ≥95% bar.",
        ]
    if result.critical_gold_total == 0:
        return [
            "  critical recall  NOT MEASURED: the gold set labels no row "
            "critical",
            "    It is not a recall of zero — there was nothing to find.",
        ]

    lines = [
        f"  critical recall {result.critical_recall:.1%}  "
        f"({result.critical_matched}/{result.critical_gold_total} labeled "
        "critical rows found)"
    ]
    if result.critical_missing:
        shown = ", ".join(result.critical_missing[:12])
        more = (
            ""
            if len(result.critical_missing) <= 12
            else f" (+{len(result.critical_missing) - 12})"
        )
        lines.append(f"    missing critical  {shown}{more}")
    return lines


def main(argv: list[str]) -> int:
    """`eval <project-slug> [gold.csv] [--prompt-version=X]`.

    Without a CSV the enumeration is built from the stored page text of the
    project's matrices, which is independent of the table parser under test.

    `--prompt-version` scopes both the enumeration and the scoring to one
    extraction path, which is what makes two paths on the same project
    comparable rather than pooled.
    """
    flags = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    prompt_version = next(
        (f.split("=", 1)[1] for f in flags if f.startswith("--prompt-version=")), None
    )
    if not args or any(not f.startswith("--prompt-version=") for f in flags):
        print(
            "usage: eval <project-slug> [gold.csv] [--prompt-version=X]",
            file=sys.stderr,
        )
        return 2

    slug = args[0]
    skipped: set[int] = set()
    with SessionFactory() as session:
        if len(args) > 1:
            gold = load_gold(args[1])
            source = args[1]
        else:
            project = session.scalars(
                select(Project).where(Project.slug == slug)
            ).first()
            if project is None:
                print(f"no project {slug!r}", file=sys.stderr)
                return 1
            extracted_docs = extracted_documents(
                session, project.id, prompt_version=prompt_version
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
                print(
                    f"{slug}: nothing extracted yet"
                    + (f" at {prompt_version}" if prompt_version else ""),
                    file=sys.stderr,
                )
                return 1

            gold = gold_for_documents(session, extracted_docs)
            source = "page text (independent of the table parser)"
            if prompt_version:
                source += f"; scoped to {prompt_version}"
            if skipped:
                source += f"; {len(skipped)} ingested matrix/matrices not extracted"

        result = evaluate(
            session, slug=slug, gold=gold, prompt_version=prompt_version
        )

    print(render(result))
    if len(args) == 1 and skipped:
        print(
            f"  {len(skipped)} ingested matrix/matrices contributed no candidates "
            "and are excluded from the enumeration, not counted as misses."
        )

    out = Path("out")
    out.mkdir(exist_ok=True)
    # One file per extraction path, so measuring the new one does not
    # overwrite the baseline it is being compared against.
    stem = f"eval-{slug}" + (f"-{prompt_version}" if prompt_version else "")
    path = out / f"{stem}.json"
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
                "field_failures": result.field_failures,
                # Null rather than 0.0 when unmeasurable, so a script
                # reading this artifact cannot mistake "nobody labeled it"
                # for "the extractor found none of them".
                "critical_recall": (
                    None if result.critical_unmeasurable else result.critical_recall
                ),
                "critical_gold_total": result.critical_gold_total,
                "critical_matched": result.critical_matched,
                "critical_missing": result.critical_missing,
                "critical_labeled": result.critical_labeled,
                "prompt_versions": result.prompt_versions,
                "models": result.models,
                "coverage_note": result.coverage_note,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\n{path}")
    # A measurement that could not be made is not a pass. Exiting zero here
    # would let a broken enumeration slide through a script as a green run.
    return 1 if result.unmeasurable else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
