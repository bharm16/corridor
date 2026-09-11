"""Extraction Measurement over exact run receipts and declared references.

Candidate membership comes only from explicit completed Extraction Run ids;
prompt, document, Active Run, recency, and timestamps cannot select the
population. A reference CSV may be independently authored, or it may be a
machine reference whose author-time scope, bytes, method, provenance, shared
blind spots, and stable document hashes travel in a required manifest.

The machine path replaced founder row labelling. It is a semi-independent
ceiling, not semantic ground truth: a shared parser can miss the same region
as the extractor, and the source matrix cannot reveal its own omissions.
Every score is therefore reported against the named enumeration and never as
unqualified recall or Ledger correctness.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import digests
from corridor.experimental_command import (
    SingleValue,
    experiment_parser,
    print_receipt,
    run_command,
    run_experiment,
)
from corridor.experimental_database import (
    DatabaseGuard,
    require_experimental_database,
)
from corridor.extraction_run_queries import (
    completed_document_ids,
    completion_predicate,
    is_completed_run,
)
from corridor.models import Candidate, Document, ExtractionRun, Project
from corridor.receipts import identity
from corridor.measurement_cases import (
    CasePredictionError,
    HumanCaseMeasurement,
    load_case_predictions,
    score_measurement_cases,
)
from corridor.verify import unverified_fields
from corridor.reference_methods import (
    LEGACY_METHOD, NATIVE_METHOD, SCOPE_SCHEMA, SPENT_SOURCE_HASHES,
    reference_method, validate_native_authoring,
)

REQUIRED_COLUMNS = ("source_ref",)
# Public legacy aliases remain stable for historical callers and fixtures.
MACHINE_REFERENCE_SCOPE_SCHEMA = SCOPE_SCHEMA
MACHINE_REFERENCE_METHOD = LEGACY_METHOD.name
MACHINE_REFERENCE_METHOD_VERSION = LEGACY_METHOD.version
MACHINE_REFERENCE_LIMITATIONS = LEGACY_METHOD.limitations
SPENT_MEASUREMENT_ARTIFACTS = {
    "wsdot-9540": "out/eval-wsdot-9540-matrix_tiered_v3.json",
}

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


@dataclass(frozen=True)
class GoldSet:
    """An enumeration, and what it was able to look for.

    Whether an extracted id is *spurious* or merely *outside this
    enumeration's reach* is a property of the enumeration that produced
    it, not a global fact about id shapes. `gold_from_page_text` already
    decides which of two inverted row shapes applies to a document and
    then threw that decision away, so `evaluate` re-derived it from the
    union of both — for every gold set, however it was built.

    Two consequences, both removed by carrying it here. A hand-authored
    CSV reads the whole grid, so an id it does not contain is spurious by
    definition; under the union rule the M7 holdout's own ids
    (`PSEN-G-1001`) match no shape, and one genuinely spurious row would
    have suppressed the precision figure on a holdout that is spent once
    (ADR-0008). And on a prefixed layout the sequential shape never ran,
    yet `303` was still treated as a row the enumeration could have
    found — #90's defect in the opposite direction.

    `reach` of None means "reads everything": no id is beyond it.
    """

    records: tuple[GoldRecord, ...]
    reach: tuple[re.Pattern, ...] | None = None

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def can_adjudicate(self, ref: str | None) -> bool:
        """Could this enumeration have produced this id at all?

        Shape only — deliberately not "is it printed on its cited page".
        The obvious page test is whole-line equality against the stored
        text, and five live Project A rows defeat it: `OFOC14-1`,
        `OFOC14-2`, `OFOC25-1`, `OFOC27-1` and `OFOC27-2` share their
        line with the owner. A fixture where every id stands alone passes
        while the corpus does not.
        """
        cleaned = (ref or "").strip()
        if not cleaned:
            return False
        if self.reach is None:
            return True
        return any(pattern.fullmatch(cleaned) for pattern in self.reach)


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
    # Extracted rows this enumeration structurally could not adjudicate,
    # one entry per occurrence. On SH 99 that is 897 of 1,401: the layout
    # numbers its conflicts `C1`, `PL4`, `OH C45`, and no shape here can
    # look for them. They were reported as spurious extractions, which
    # printed `precision 36.0%` and read as an extractor that invented two
    # thirds of a document.
    unrecognized: list[str] = field(default_factory=list)
    reference_label: str = "reference"
    reference_limitations: list[str] = field(default_factory=list)

    @property
    def recognized_total(self) -> int:
        return self.extracted_total - len(self.unrecognized)

    @property
    def coverage(self) -> float:
        """Share of extracted rows the enumeration could adjudicate."""
        if not self.extracted_total:
            return 1.0
        return self.recognized_total / self.extracted_total

    @property
    def partial_coverage(self) -> bool:
        return bool(self.extracted_total) and self.recognized_total < self.extracted_total

    @property
    def precision_over_recognized(self) -> float | None:
        """Precision over the rows the enumeration could adjudicate.

        None when it could adjudicate none of them — #82's rule reached by
        the other road. A figure over an empty subset is not a score.
        """
        if not self.recognized_total:
            return None
        return self.matched / self.recognized_total

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


def load_gold(path: Path | str) -> GoldSet:
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
    # A hand-authored gold set is a whole-document enumeration: it read the
    # grid, so an id it does not contain is spurious rather than out of
    # reach. `reach=None` says exactly that.
    return GoldSet(tuple(records), None)


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


def gold_from_page_text(page_text: dict[int, str]) -> GoldSet:
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
            # The shape that won is this enumeration's reach. The one that
            # did not run could not have found anything, so an id of that
            # shape is outside the enumeration rather than spurious.
            return GoldSet(tuple(records), (pattern,))
    return GoldSet((), ())


def gold_for_documents(session: Session, document_ids) -> GoldSet:
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
    # Documents in one project may print different row shapes, so the
    # reach of the combined enumeration is the union of what each
    # document's scan could look for.
    reach: list[re.Pattern] = []
    for document_id in sorted(document_ids):
        pages = session.execute(
            select(DocPage.page_no, DocPage.text).where(
                DocPage.document_id == document_id
            )
        ).all()
        found = gold_from_page_text({p: t for p, t in pages})
        records.extend(found.records)
        for pattern in found.reach or ():
            if pattern not in reach:
                reach.append(pattern)
    return GoldSet(tuple(records), tuple(reach))


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
    return completed_document_ids(
        session, project_id, prompt_version=prompt_version
    )


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
    gold: GoldSet,
    kind: str = "dependency",
    prompt_version: str | None = None,
    document_ids: set[int] | None = None,
    extraction_run_ids: set[int] | None = None,
) -> EvalResult:
    """Score one extractor's output.

    When ``extraction_run_ids`` is present, it is the only lineage selector;
    prompt and document filters are ignored. The lower-level legacy path keeps
    those filters for direct scoring tests, but the public ``measure`` seam
    requires exact completed run ids and validates its assertions first.
    """
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise MalformedGoldSet(f"no project {slug!r}")

    query = select(Candidate).where(
        Candidate.project_id == project.id, Candidate.kind == kind
    )
    if extraction_run_ids is not None:
        query = query.where(
            Candidate.extraction_run_id.in_(extraction_run_ids or {0})
        )
    else:
        if prompt_version:
            query = query.where(Candidate.prompt_version == prompt_version)
        if document_ids is not None:
            query = query.where(Candidate.source_document_id.in_(document_ids or {0}))
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

    if not versions and extraction_run_ids is not None:
        selected_runs = session.scalars(
            select(ExtractionRun).where(
                ExtractionRun.id.in_(extraction_run_ids or {0})
            )
        ).all()
        versions.update({run.prompt_version: 0 for run in selected_runs})
        models.update({(run.model or "deterministic"): 0 for run in selected_runs})
    elif not versions and prompt_version is not None:
        versions.update(
            {
                version: 0
                for version in _completed_prompt_versions(
                    session,
                    project_id=project.id,
                    prompt_version=prompt_version,
                    document_ids=document_ids,
                )
            }
        )

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

    # An extracted row is adjudicable if *this* enumeration could have
    # produced its id — which the gold set answers, because only it knows
    # how it was built. Everything else is unrecognized rather than
    # spurious: the enumeration cannot say whether it is real, and
    # charging it to the extractor is the defect (#90).
    #
    # Counted per occurrence, not per distinct id. An extractor emitting
    # one unknown-shape id five times from a page printing it once should
    # show five unrecognized rows; a set would excuse exactly the
    # duplication this count exists to surface.
    surplus = extracted - wanted
    spurious, unrecognized = [], []
    for ref in surplus.elements():
        adjudicable = gold.can_adjudicate(ref) or ref in wanted
        (spurious if adjudicable else unrecognized).append(ref)

    result = EvalResult(
        project=slug,
        gold_total=sum(wanted.values()),
        extracted_total=sum(extracted.values()),
        matched=matched,
        missing=sorted((wanted - extracted).elements()),
        spurious=sorted(spurious),
        unrecognized=sorted(unrecognized),
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


def _completed_prompt_versions(
    session: Session,
    *,
    project_id: int,
    prompt_version: str,
    document_ids: set[int] | None,
) -> list[str]:
    """Completed prompt versions for the scoped document population.

    Used only when no candidate rows survive the scope, so the prompt
    provenance still names the extraction path that completed cleanly
    rather than collapsing to "unknown".
    """
    query = (
        select(ExtractionRun.prompt_version)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(
            Document.project_id == project_id,
            completion_predicate(),
            ExtractionRun.prompt_version == prompt_version,
        )
        .distinct()
    )
    if document_ids is not None:
        query = query.where(ExtractionRun.document_id.in_(document_ids or {0}))
    return list(session.scalars(query).all())


def render(result: EvalResult) -> str:
    if result.unmeasurable:
        return "\n".join(
            [
                f"{result.project} — NOT MEASURED: the reference is empty",
                f"  {result.extracted_total} rows were extracted, and none of "
                "them could be checked.",
                "  This document's rows could not be enumerated from its page "
                "text by any known",
                "  layout, so there is nothing to score against. It is not a "
                "recall of zero.",
                "  Supply a reference enumeration and exact run id(s): "
                f"make eval ARGS=\"{result.project} reference.csv "
                "--extraction-run=<id>\"",
            ]
        )

    # The headline never carries a bare precision under partial coverage.
    # `precision 36.0%` is what was misread as an extractor that invented
    # two thirds of SH 99, and a figure that needs a caveat printed three
    # lines below it will be quoted without one.
    if result.partial_coverage:
        lines = [f"{result.project} — recall {result.recall:.1%}"]
    else:
        lines = [
            f"{result.project} — recall {result.recall:.1%}  "
            f"precision {result.precision:.1%}"
        ]
    lines.append(
        f"  reference {result.gold_total}   extracted {result.extracted_total}   "
        f"matched {result.matched}"
    )
    lines.extend(_coverage_lines(result))
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
    versions = ", ".join(
        k if v == 0 else f"{k}×{v}" for k, v in sorted(result.prompt_versions.items())
    )
    models = ", ".join(f"{k}×{v}" for k, v in sorted(result.models.items()))
    lines.append(f"  prompt_version: {versions or '—'}")
    lines.append(f"  model: {models or '—'}")
    if result.reference_label != "reference":
        lines.append(f"  reference: {result.reference_label}")
    lines.extend(
        f"  limitation: {limitation}" for limitation in result.reference_limitations
    )
    lines.append(f"  note: {result.coverage_note}")
    return "\n".join(lines)


def _coverage_lines(result: EvalResult) -> list[str]:
    """What share of the document this enumeration could adjudicate.

    Named even at 100%, because "it read everything" is the claim worth
    making. Under partial coverage the precision figure is reported only
    over the rows it could read, and only ever with that scope attached —
    the whole-document number does not exist and must not be inferable.
    """
    if not result.partial_coverage:
        return [
            f"  coverage {result.coverage:.1%}  "
            f"(every one of {result.extracted_total} extracted rows is an id "
            "shape this enumeration can read)"
        ]

    shown = ", ".join(sorted(set(result.unrecognized))[:8])
    more = (
        ""
        if len(set(result.unrecognized)) <= 8
        else f" (+{len(set(result.unrecognized)) - 8} more shapes)"
    )
    lines = [
        f"  coverage {result.coverage:.1%}  "
        f"({result.recognized_total} of {result.extracted_total} extracted "
        "rows recognised)",
        "  precision  NOT MEASURED as a whole-document figure",
    ]
    if result.precision_over_recognized is None:
        lines.append(
            "    no extracted row carries an id shape this enumeration can "
            "read, so there is nothing to score."
        )
    else:
        lines.append(
            f"    over the {result.recognized_total} recognised rows: "
            f"{result.precision_over_recognized:.1%}"
        )
    lines.append(
        f"    {len(result.unrecognized)} rows use an id shape this "
        "enumeration cannot read; they are"
    )
    lines.append(f"    unrecognised, not spurious: {shown}{more}")
    return lines


def _critical_lines(result: EvalResult) -> list[str]:
    """M7's gate number, or an explicit statement that there isn't one.

    Never "critical recall 0.0%" for a set nobody labeled. The gate reads
    ≥95% here, and a zero that means "unlabeled" is indistinguishable from
    one that means "found none of them" at exactly the moment that
    distinction decides whether a holdout was passed or failed.
    """
    if not result.critical_labeled:
        return [
            "  critical recall  NOT MEASURED: this reference does not label "
            "criticality",
            "    Add a `critical` column to score the M7 gate's ≥95% bar.",
        ]
    if result.critical_gold_total == 0:
        return [
            "  critical recall  NOT MEASURED: the reference labels no row "
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


def artifact(
    result: EvalResult,
    *,
    reference_description: str,
    ran_at: datetime,
    extraction_runs: tuple[ExtractionRunScope, ...] = (),
    reference_scope: ReferenceScope | None = None,
    case_measurement: HumanCaseMeasurement | None = None,
) -> dict:
    """The machine-readable record of one measurement.

    The only thing a gate script consumes, and it lived inside `main`
    beside `mkdir` and `print`, so it could only be exercised by driving
    the whole command — which no test did. ADR-0008 makes this the
    highest-consequence untested code here: the artifact is how a
    measurement spent once is recorded.

    The null-vs-zero rule is the reason it is worth naming. `precision`
    and `critical_recall` are null rather than a number when the
    measurement could not be made, so a script cannot mistake "nobody
    labelled it" for "the extractor found none of them", or a
    subset figure for a whole-document one. The properties on
    `EvalResult` already encode that; stating it a second time here is
    how the JSON and the rendered text drift.
    """
    written = {
        "project": result.project,
        "reference_description": reference_description,
        "ran_at": ran_at.isoformat(),
        "extraction_run_ids": [run.id for run in extraction_runs],
        "extraction_runs": [run.as_dict() for run in extraction_runs],
        "reference_scope": (
            reference_scope.as_dict()
            if reference_scope is not None
            else {
                "kind": "unspecified_reference",
                "source": reference_description,
                "sha256": None,
                "documents": [],
                "limitations": [result.coverage_note],
            }
        ),
        "recall": result.recall,
        "reference_total": result.gold_total,
        "extracted_total": result.extracted_total,
        "matched": result.matched,
        "missing": result.missing,
        "spurious": result.spurious,
        "field_failures": result.field_failures,
        # Null under partial coverage. A whole-document precision does not
        # exist when the enumeration could not read the whole document,
        # and a gate script reading this key must not receive a subset
        # figure by accident — the same rule `critical_recall` follows for
        # an unlabelled gold set.
        "precision": None if result.partial_coverage else result.precision,
        "precision_over_recognized": result.precision_over_recognized,
        "coverage": result.coverage,
        "recognized_total": result.recognized_total,
        "unrecognized": result.unrecognized,
        # Null rather than 0.0 when unmeasurable, so a script reading this
        # artifact cannot mistake "nobody labeled it" for "the extractor
        # found none of them".
        "critical_recall": (
            None if result.critical_unmeasurable else result.critical_recall
        ),
        "critical_reference_total": result.critical_gold_total,
        "critical_matched": result.critical_matched,
        "critical_missing": result.critical_missing,
        "critical_labeled": result.critical_labeled,
        "prompt_versions": result.prompt_versions,
        "models": result.models,
        "coverage_note": result.coverage_note,
    }
    if case_measurement is not None:
        written["human_ruling_cases"] = case_measurement.as_dict()
    identity_material = dict(written)
    identity_reference = dict(identity_material["reference_scope"])
    identity_reference.pop("source")
    identity_reference.pop("manifest_source", None)
    identity_material["reference_scope"] = identity_reference
    written["artifact_identity"] = identity(
        identity_material, volatile=("ran_at", "reference_description")
    )
    return written


def exit_code(
    result: EvalResult,
    *,
    case_measurement: HumanCaseMeasurement | None = None,
) -> int:
    """A measurement that could not be made is not a pass.

    Exiting zero on an empty enumeration would let a broken measurement
    slide through the M7 gate run as a green run.
    """
    return 1 if (
        result.unmeasurable
        or (case_measurement is not None and case_measurement.mismatched > 0)
    ) else 0


class NothingToMeasure(Exception):
    """There is no measurement to take, so a score would be a fiction."""


def assert_measurement_not_spent(
    slug: str, *, document_sha256s: tuple[str, ...] = (),
) -> None:
    """Protect one-shot holdouts from accidental regeneration or rescoring."""
    historical = SPENT_MEASUREMENT_ARTIFACTS.get(slug)
    if historical is not None or SPENT_SOURCE_HASHES.intersection(document_sha256s):
        raise NothingToMeasure(
            f"{slug} is a spent holdout; preserve its historical measurement "
            f"at {historical or SPENT_MEASUREMENT_ARTIFACTS['wsdot-9540']} "
            "and do not regenerate or rescore it"
        )


@dataclass(frozen=True)
class ExtractionRunScope:
    """Exact immutable receipt metadata for one measured population."""

    id: int
    document_id: int
    document_sha256: str
    prompt_version: str
    model: str | None
    schema_version: str | None
    outcome: str
    candidate_count: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "document": {
                "id": self.document_id,
                "sha256": self.document_sha256,
            },
            "prompt_version": self.prompt_version,
            "model": self.model,
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True)
class DocumentScope:
    """One document identity within a measurement reference scope."""

    id: int
    sha256: str

    def as_dict(self) -> dict:
        return {"id": self.id, "sha256": self.sha256}


@dataclass(frozen=True)
class ReferenceScope:
    """The independent or semi-independent instrument and its limits."""

    kind: str
    source: str
    sha256: str
    documents: tuple[DocumentScope, ...]
    limitations: tuple[str, ...]
    manifest_source: str | None = None
    method: str | None = None
    method_version: str | None = None
    manifest_provenance: dict[str, str] | None = None
    native_authoring: dict | None = None

    @property
    def document_ids(self) -> tuple[int, ...]:
        return tuple(document.id for document in self.documents)

    @property
    def document_sha256s(self) -> tuple[str, ...]:
        return tuple(document.sha256 for document in self.documents)

    def as_dict(self) -> dict:
        scope = {
            "kind": self.kind,
            "source": self.source,
            "sha256": self.sha256,
            "manifest_source": self.manifest_source,
            "method": self.method,
            "method_version": self.method_version,
            "manifest_provenance": self.manifest_provenance,
            "documents": [document.as_dict() for document in self.documents],
            "document_sha256s": list(self.document_sha256s),
            "limitations": list(self.limitations),
        }
        if self.native_authoring is not None:
            scope["native_authoring"] = self.native_authoring
        return scope


def verified_machine_reference_scope(
    manifest_path: str | Path,
    *,
    reference_path: str | Path,
    reference_bytes: bytes,
    project_slug: str,
    documents: tuple[Document, ...],
) -> ReferenceScope:
    """Load and verify an author-time machine-reference scope manifest."""
    path = Path(manifest_path)
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NothingToMeasure(
            f"machine-reference scope manifest {path} is unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise NothingToMeasure(
            f"machine-reference scope manifest {path} must be a JSON object"
        )
    if payload.get("schema_version") != MACHINE_REFERENCE_SCOPE_SCHEMA:
        raise NothingToMeasure(
            "machine-reference scope manifest has unsupported schema_version"
        )
    if payload.get("project") != project_slug:
        raise NothingToMeasure(
            "machine-reference scope manifest project does not match "
            f"{project_slug!r}"
        )
    try:
        contract = reference_method(payload.get("method"), payload.get("method_version"))
    except ValueError as exc:
        raise NothingToMeasure(str(exc)) from exc

    actual_reference_sha256 = hashlib.sha256(reference_bytes).hexdigest()
    if payload.get("reference_sha256") != actual_reference_sha256:
        raise NothingToMeasure(
            "machine-reference scope manifest reference SHA-256 does not match "
            "the supplied reference bytes"
        )

    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise NothingToMeasure(
            "machine-reference scope manifest must name at least one document"
        )
    try:
        manifest_hashes = tuple(
            sorted(
                entry["sha256"]
                for entry in raw_documents
                if isinstance(entry, dict)
            )
        )
        manifest_filenames = tuple(
            entry.get("filename")
            for entry in raw_documents
            if isinstance(entry, dict)
        )
    except (KeyError, TypeError) as exc:
        raise NothingToMeasure(
            "machine-reference scope manifest has malformed document hashes"
        ) from exc
    valid_documents = (
        len(manifest_hashes) == len(raw_documents)
        and len(set(manifest_hashes)) == len(manifest_hashes)
        and all(
            isinstance(document_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", document_sha256) is not None
            for document_sha256 in manifest_hashes
        )
        and all(
            filename is None or (isinstance(filename, str) and bool(filename))
            for filename in manifest_filenames
        )
    )
    if not valid_documents:
        raise NothingToMeasure(
            "machine-reference scope manifest has malformed or duplicate "
            "document hashes"
        )
    runtime_documents = tuple(sorted(documents, key=lambda document: document.id))
    selected_hashes = tuple(sorted(document.sha256 for document in runtime_documents))
    if manifest_hashes != selected_hashes:
        raise NothingToMeasure(
            "machine-reference scope mismatch: the selected Extraction Runs "
            "do not exactly match the manifest document hash set; "
            f"selected {selected_hashes}, manifest {manifest_hashes}"
        )

    raw_limitations = payload.get("limitations")
    limitations = (
        tuple(raw_limitations) if isinstance(raw_limitations, list) else ()
    )
    if limitations != contract.limitations:
        raise NothingToMeasure(
            "machine-reference scope manifest limitations are missing or changed"
        )
    raw_provenance = payload.get("manifest_provenance")
    if not isinstance(raw_provenance, dict):
        raise NothingToMeasure(
            "machine-reference scope manifest provenance is missing or malformed"
        )
    provenance_kind = raw_provenance.get("kind")
    if provenance_kind == "author_time":
        valid_provenance = set(raw_provenance) == {"kind"}
    elif provenance_kind == "backfill":
        valid_provenance = (
            set(raw_provenance) == {"kind", "reference_commit", "note"}
            and isinstance(raw_provenance.get("reference_commit"), str)
            and re.fullmatch(
                r"[0-9a-f]{40}", raw_provenance["reference_commit"]
            )
            is not None
            and isinstance(raw_provenance.get("note"), str)
            and bool(raw_provenance["note"].strip())
        )
    else:
        valid_provenance = False
    if not valid_provenance:
        raise NothingToMeasure(
            "machine-reference scope manifest provenance is unsupported"
        )
    manifest_provenance = {
        str(key): str(value) for key, value in sorted(raw_provenance.items())
    }
    native_authoring = None
    if contract == NATIVE_METHOD:
        try:
            native_authoring = validate_native_authoring(payload.get("native_authoring"), manifest_hashes)
        except ValueError as exc:
            raise NothingToMeasure(str(exc)) from exc
    elif "native_authoring" in payload:
        raise NothingToMeasure("legacy reference cannot be relabeled with native authoring provenance")
    return ReferenceScope(
        kind="machine_reference",
        source=str(reference_path),
        sha256=actual_reference_sha256,
        documents=tuple(
            DocumentScope(id=document.id, sha256=document.sha256)
            for document in runtime_documents
        ),
        limitations=limitations,
        manifest_source=str(path),
        method=contract.name,
        method_version=contract.version,
        manifest_provenance=manifest_provenance,
        native_authoring=native_authoring,
    )


@dataclass(frozen=True)
class Measurement:
    """One scoring run, with the provenance of the enumeration it scored.

    `reference_description` is the human-readable description of the
    enumeration used; `reference_scope` is the structured, hashed authority.
    `skipped` names project matrices outside the caller's exact run population.
    """

    result: EvalResult
    reference_description: str
    skipped: frozenset[int] = frozenset()
    extraction_run_ids: tuple[int, ...] = ()
    extraction_runs: tuple[ExtractionRunScope, ...] = ()
    reference_scope: ReferenceScope | None = None
    case_measurement: HumanCaseMeasurement | None = None


def measure(
    session: Session,
    slug: str,
    *,
    gold_path: str | Path | None = None,
    reference_manifest_path: str | Path | None = None,
    prompt_version: str | None = None,
    document_ids: set[int] | None = None,
    extraction_run_ids: set[int] | None = None,
    case_predictions_path: str | Path | None = None,
) -> Measurement:
    """Score exactly the completed Extraction Runs the caller names.

    Run ids are the sole population selector. Prompt and document arguments
    survive only as assertions for command compatibility; Active Run state,
    timestamps and recency are deliberately absent. Candidate membership is
    checked against each receipt count before scoring by run lineage.

    Without a CSV, the reference is independently enumerated from the stored
    page text of the selected runs' documents. Supplying a machine-reference
    manifest verifies the author-time CSV hash, method, limitations and exact
    stable document hash set. A filename alone never grants machine-reference
    provenance.
    """
    assert_measurement_not_spent(slug)
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise NothingToMeasure(f"no project {slug!r}")
    if not extraction_run_ids:
        raise NothingToMeasure(
            f"{slug}: one or more explicit Extraction Run ids are required"
        )
    if reference_manifest_path is not None and gold_path is None:
        raise NothingToMeasure(
            "a machine-reference scope manifest requires a reference CSV"
        )

    matrices = set(
        session.scalars(
            select(Document.id).where(
                Document.project_id == project.id,
                Document.doc_type == "matrix",
            )
        ).all()
    )
    selected_runs = session.scalars(
        select(ExtractionRun)
        .where(ExtractionRun.id.in_(extraction_run_ids))
        .order_by(ExtractionRun.id)
    ).all()
    missing_run_ids = sorted(set(extraction_run_ids) - {run.id for run in selected_runs})
    if missing_run_ids:
        if len(missing_run_ids) == 1:
            raise NothingToMeasure(
                f"Extraction Run {missing_run_ids[0]} does not exist"
            )
        raise NothingToMeasure(
            "Extraction Runs "
            + ", ".join(str(run_id) for run_id in missing_run_ids)
            + " do not exist"
        )
    incomplete = [run.id for run in selected_runs if not is_completed_run(run)]
    if incomplete:
        raise NothingToMeasure(
            "Extraction Run(s) "
            + ", ".join(str(run_id) for run_id in incomplete)
            + " must be completed with zero page failures"
        )
    documents_by_id = {
        document.id: document
        for document in session.scalars(
            select(Document).where(
                Document.id.in_({run.document_id for run in selected_runs})
            )
        ).all()
    }
    wrong_project = [
        run.id
        for run in selected_runs
        if documents_by_id[run.document_id].project_id != project.id
    ]
    if wrong_project:
        raise NothingToMeasure(
            "Extraction Run(s) "
            + ", ".join(str(run_id) for run_id in wrong_project)
            + f" does not belong to project {slug!r}"
        )
    assert_measurement_not_spent(
        slug, document_sha256s=tuple(document.sha256 for document in documents_by_id.values()),
    )
    non_matrix = [
        run.id
        for run in selected_runs
        if documents_by_id[run.document_id].doc_type != "matrix"
    ]
    if non_matrix:
        raise NothingToMeasure(
            "Extraction Run(s) "
            + ", ".join(str(run_id) for run_id in non_matrix)
            + " must belong to a matrix document"
        )
    repeated_documents = sorted(
        document_id
        for document_id, count in Counter(
            run.document_id for run in selected_runs
        ).items()
        if count > 1
    )
    if repeated_documents:
        raise NothingToMeasure(
            "Extraction Measurement accepts at most one run per document; "
            "repeated document id(s): "
            + ", ".join(str(document_id) for document_id in repeated_documents)
        )
    linked_counts = dict(
        session.execute(
            select(Candidate.extraction_run_id, func.count(Candidate.id))
            .where(Candidate.extraction_run_id.in_(extraction_run_ids))
            .group_by(Candidate.extraction_run_id)
        ).all()
    )
    for run in selected_runs:
        linked_count = linked_counts.get(run.id, 0)
        if linked_count != run.candidate_count:
            raise NothingToMeasure(
                f"Extraction Run {run.id} records {run.candidate_count} "
                f"Candidates but owns {linked_count}"
            )
    if prompt_version is not None:
        mismatched_prompts = [
            run.id for run in selected_runs if run.prompt_version != prompt_version
        ]
        if mismatched_prompts:
            raise NothingToMeasure(
                f"prompt assertion {prompt_version!r} does not match Extraction "
                "Run(s) "
                + ", ".join(str(run_id) for run_id in mismatched_prompts)
            )
    selected_run_ids = tuple(run.id for run in selected_runs)
    extracted_docs = {run.document_id for run in selected_runs}
    run_scope = tuple(
        ExtractionRunScope(
            id=run.id,
            document_id=run.document_id,
            document_sha256=documents_by_id[run.document_id].sha256,
            prompt_version=run.prompt_version,
            model=run.model,
            schema_version=run.schema_version,
            outcome=run.outcome,
            candidate_count=run.candidate_count,
        )
        for run in selected_runs
    )
    if not extracted_docs:
        raise NothingToMeasure(
            f"{slug}: nothing extracted yet"
            + (f" at {prompt_version}" if prompt_version else "")
        )

    skipped = matrices - extracted_docs
    named = None
    if document_ids is not None:
        named = set(document_ids)
        if named != extracted_docs:
            raise NothingToMeasure(
                "document assertion does not match the exact Extraction Run "
                f"scope; asserted {sorted(named)}, selected {sorted(extracted_docs)}"
            )

    if gold_path is not None:
        gold = load_gold(gold_path)
        source = str(gold_path)
        reference_bytes = Path(gold_path).read_bytes()
        selected_documents = tuple(
            documents_by_id[document_id] for document_id in sorted(extracted_docs)
        )
        if reference_manifest_path is not None:
            reference_scope = verified_machine_reference_scope(
                reference_manifest_path,
                reference_path=gold_path,
                reference_bytes=reference_bytes,
                project_slug=slug,
                documents=selected_documents,
            )
            source += f"; machine-reference scope {reference_manifest_path}"
        else:
            reference_scope = ReferenceScope(
                kind="external_reference",
                source=str(gold_path),
                sha256=hashlib.sha256(reference_bytes).hexdigest(),
                documents=tuple(
                    DocumentScope(id=document.id, sha256=document.sha256)
                    for document in selected_documents
                ),
                limitations=(
                    "Recall is measured against this reference enumeration only; "
                    "the source document is not ground truth for its own omissions.",
                ),
            )
    else:
        gold = gold_for_documents(session, extracted_docs)
        source = "page text (independent of the table parser)"
        selected_document_scope = tuple(
            (document_id, documents_by_id[document_id].sha256)
            for document_id in sorted(extracted_docs)
        )
        reference_material = {
            "documents": [item[1] for item in selected_document_scope],
            "records": [
                {
                    "source_ref": record.source_ref,
                    "page": record.page,
                    "critical": record.critical,
                }
                for record in gold
            ],
        }
        reference_scope = ReferenceScope(
            kind="page_text_reference",
            source=source,
            sha256=digests.ascii_escaped_sha256(reference_material),
            documents=tuple(
                DocumentScope(id=document_id, sha256=document_sha256)
                for document_id, document_sha256 in selected_document_scope
            ),
            limitations=(
                "Recall is measured against the stored page-text enumeration only; "
                "the source document is not ground truth for its own omissions.",
            ),
        )

    if prompt_version:
        source += f"; scoped to {prompt_version}"
    if skipped:
        source += f"; {len(skipped)} project matrix/matrices outside exact run scope"
    source += f"; scored over {len(extracted_docs)} extracted matrix/matrices"
    if named is not None:
        source += f" named by the caller ({', '.join(str(i) for i in sorted(named))})"

    result = evaluate(
        session,
        slug=slug,
        gold=gold,
        prompt_version=prompt_version,
        document_ids=extracted_docs,
        extraction_run_ids=set(selected_run_ids),
    )
    result.reference_label = (
        f"machine reference — semi-independent ceiling ({reference_scope.method} v{reference_scope.method_version})"
        if reference_scope.kind == "machine_reference"
        else "reference"
    )
    result.reference_limitations = list(reference_scope.limitations)

    return Measurement(
        result=result,
        reference_description=source,
        skipped=frozenset(skipped),
        extraction_run_ids=selected_run_ids,
        extraction_runs=run_scope,
        reference_scope=reference_scope,
        case_measurement=score_measurement_cases(
            session,
            project_id=project.id,
            extraction_run_ids=set(selected_run_ids),
            predictions=(
                load_case_predictions(case_predictions_path)
                if case_predictions_path is not None
                else None
            ),
        ),
    )


def main(
    argv: list[str],
    *,
    session_factory=None,
    database_guard: DatabaseGuard = require_experimental_database,
    output_dir: Path | str | None = None,
    ran_at: datetime | None = None,
) -> int:
    """`eval <project-slug> [reference.csv] --extraction-run=N [...]`.

    Argument parsing, printing and the artifact file. The measurement
    itself is `measure`; the guarded database, refusal exit codes and the
    sealed receipt path are the `experimental_command` frame.

    `--prompt-version` and `--document` are optional assertions about the
    named runs; neither can select Candidates. `--reference-manifest`
    explicitly identifies a machine reference and binds it to its author-time
    bytes, method, limitations and stable document hash set.
    """
    parser = experiment_parser(
        prog="eval",
        usage=(
            "eval <project-slug> [reference.csv] "
            "--extraction-run=N [--extraction-run=N ...] "
            "--database-url=POSTGRESQL_URL "
            "[--reference-manifest=scope.json] "
            "[--case-predictions=outputs.json] "
            "[--prompt-version=X] [--document=N ...]"
        ),
        add_help=False,
        allow_abbrev=False,
        database_url_required=False,
    )
    parser.add_argument("slug")
    parser.add_argument("gold_path", nargs="?")
    parser.add_argument(
        "--extraction-run", action="append", default=[], dest="extraction_runs"
    )
    parser.add_argument("--prompt-version", action=SingleValue)
    parser.add_argument("--document", action="append", default=[], dest="documents")
    parser.add_argument("--reference-manifest", action=SingleValue)
    parser.add_argument("--case-predictions", action=SingleValue)

    def measurement(args) -> int:
        try:
            document_ids = {int(n) for n in args.documents} or None
            parsed_run_ids = [int(n) for n in args.extraction_runs]
            extraction_run_ids = set(parsed_run_ids)
        except ValueError:
            print(
                "--document and --extraction-run take integer ids",
                file=sys.stderr,
            )
            return 2
        if len(parsed_run_ids) != len(extraction_run_ids):
            print(
                "duplicate --extraction-run ids are not allowed",
                file=sys.stderr,
            )
            return 2
        if not extraction_run_ids or any(run_id <= 0 for run_id in extraction_run_ids):
            print(
                "one or more positive --extraction-run ids are required; prompt and "
                "document selectors cannot choose a measurement population",
                file=sys.stderr,
            )
            return 2
        if not args.database_url:
            print(
                "one explicit --database-url is required for Extraction Measurement",
                file=sys.stderr,
            )
            return 2

        def body(session: Session) -> int:
            measurement = measure(
                session,
                args.slug,
                gold_path=args.gold_path,
                reference_manifest_path=args.reference_manifest,
                prompt_version=args.prompt_version,
                document_ids=document_ids,
                extraction_run_ids=extraction_run_ids,
                case_predictions_path=args.case_predictions,
            )
            print(render(measurement.result))
            if measurement.case_measurement is not None:
                cases = measurement.case_measurement
                print(
                    "  human ruling cases  "
                    f"{cases.matched} matched, {cases.mismatched} mismatched, "
                    f"{cases.not_applicable} need another evaluator"
                )
            if args.gold_path is None and measurement.skipped:
                print(
                    f"  {len(measurement.skipped)} project matrix/matrices are outside the "
                    "exact run scope and are not counted as misses."
                )
            written = artifact(
                measurement.result,
                reference_description=measurement.reference_description,
                ran_at=ran_at or datetime.now(timezone.utc),
                extraction_runs=measurement.extraction_runs,
                reference_scope=measurement.reference_scope,
                case_measurement=measurement.case_measurement,
            )
            print_receipt(
                written,
                output_dir=output_dir,
                filename=(
                    f"extraction-measurement-{args.slug}-"
                    f"{written['artifact_identity'][:16]}.json"
                ),
                preserved="existing immutable artifact preserved",
            )
            return exit_code(
                measurement.result,
                case_measurement=measurement.case_measurement,
            )

        return run_experiment(
            args.database_url,
            body,
            session_factory=session_factory,
            database_guard=database_guard,
            refusals=(NothingToMeasure,),
        )

    return run_command(parser, argv, measurement)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
