"""Extracting conflicts from a spreadsheet source (ADR-0005).

The structured original is the Document of Record, and this is what reading
one costs: a name lookup and a loop. There is no model here, no prompt, no
confidence and no tier chosen per page, because none of those answer a
question this file poses. A worksheet states its columns by name and its
values one per cell; ADR-0006's whole apparatus exists to recover exactly
that from a printout of it.

What stays the same is everything after the read. A row still becomes a
Candidate and nothing else, the Ledger is still reached only by a human
keystroke in `corridor.adjudicate`, and a column with no canonical field is
still reported rather than filed under a guessed heading.

Two properties are stronger here than on the page path, and both follow
from the source rather than from effort:

- **Every value is on the page by construction.** The page text was
  generated from the same cells the values came from, so a citation
  verifies exactly. The 0.9 threshold exists for print damage — separators
  lost between spans, a cell clipped at its boundary — and none of it can
  happen to a value read out of a cell.
- **The mapping is not a judgement.** A heading either is a template column
  or is not.

Every detected data row is also sealed on the Extraction Run as extracted,
blank, or skipped with a reason. Candidate count alone was rejected because
it cannot reveal a row dropped before proposal creation (#366).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.candidates import propose
from corridor.geometry import dedupe_hint
from corridor.models import Candidate, DocPage, Document
from corridor.row_accounting import RowAccounting
from corridor.sheets import (
    NoConflictSheet,
    conflict_sheet,
    native_evidence_table,
    read_workbook,
    row_text,
)
from corridor.storage import stored_file
from corridor.vocabulary import MIN_ROW_FIELDS, REQUIRED, is_retired_row
from corridor.verify import quote_appears_on, threshold_for, unverified_fields

# What produced this reading. The column is named `prompt_version` because
# every other extractor has one; a native read has no prompt, and what it
# has instead is a reader whose behaviour can change — which is the same
# reason the column exists (ADR-0003). A version bump here means the same
# thing it means there: rows read before and after are different readings
# of one document and must not be pooled.
PROMPT_VERSION = "sheet_native_v2"
SCHEMA_VERSION = "sheet_candidate_shape_v1"

# Not a tier in ADR-0006's sense — those name how the model was used, and
# this used no model. Recorded beside them so a run can say how each row
# was read without a reader having to infer it from the absence of a model.
TIER_NATIVE = "native"


def extract_document(session: Session, document: Document) -> list[Candidate]:
    """Every conflict row of one workbook.

    Raises `NoMatrixFound` — via `NoConflictSheet` — when no sheet is a
    conflict matrix. Returns an empty list for a form with no conflicts,
    which is a different answer and the one the published template gives.
    """
    path = stored_file(document)
    if path is None:
        raise NoConflictSheet(
            f"no stored file for {document.filename}; there is nothing to "
            "read. Re-ingest before treating this as an empty workbook."
        )

    sheets = read_workbook(path)
    try:
        chosen = conflict_sheet(sheets)
    except NoConflictSheet:
        return _extract_evidence_table(
            session,
            document,
            native_evidence_table(sheets),
        )
    page_no = chosen.page_no
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document.id, DocPage.page_no == page_no
        )
    ).first()
    page_text = (page.text if page else "") or ""
    # Read off the stored page rather than asserted here, so the strictness
    # follows the record. A page this reader wrote is `cells`; if one ever
    # is not, the citation is checked against what it actually is.
    threshold = threshold_for(page.text_source if page else None)

    headings = chosen.headings
    mapping = chosen.mapping
    unmapped = [
        heading.strip()
        for position, heading in enumerate(headings)
        if heading.strip() and position not in mapping
    ]

    candidates = []
    accounting = RowAccounting(
        reader_version=PROMPT_VERSION,
        reader_path="spreadsheet_cells",
    )
    for row_number, raw in enumerate(chosen.rows, start=1):
        row_id = f"sheet:{page_no}:row:{row_number}"
        accounting.detect(row_id, page=page_no, row_number=row_number)
        if not any(str(value or "").strip() for value in raw):
            accounting.account(
                row_id,
                disposition="blank",
                reason="blank_source_row",
            )
            continue
        fields = {
            field: raw[position].strip()
            for position, field in sorted(mapping.items())
            if position < len(raw) and raw[position].strip()
        }
        if is_retired_row(fields):
            # The form's retired numbering, excluded by the same stated
            # rule as the page path (ADR-0012).
            accounting.account(
                row_id,
                disposition="skipped",
                reason="retired_row",
            )
            continue
        if len(fields) < MIN_ROW_FIELDS:
            accounting.account(
                row_id,
                disposition="skipped",
                reason="insufficient_mapped_fields",
            )
            continue
        if not all(fields.get(name) for name in REQUIRED):
            accounting.account(
                row_id,
                disposition="skipped",
                reason="missing_required_fields",
            )
            continue

        candidate = _candidate(
            document,
            page_no,
            row_number,
            chosen.sheet.name,
            fields,
            raw,
            page_text,
            unmapped,
            threshold,
        )
        candidates.append(candidate)
        accounting.account(
            row_id,
            disposition="extracted",
            reason="candidate_recorded",
        )

    document.extraction_tiers = {TIER_NATIVE: 1}
    # The structured original states its own headers; there is no printed
    # header to read two ways.
    document.header_disagreements = 0
    accounted = accounting.finish(candidates)
    session.add_all(candidates)
    session.flush()
    return accounted


def _extract_evidence_table(session: Session, document: Document, chosen):
    """Exact rows from one recognized SUE table, never Constraint proposals."""

    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == chosen.page_no,
        )
    ).first()
    page_text = (page.text if page else "") or ""
    threshold = threshold_for(page.text_source if page else None)
    candidates = []
    accounting = RowAccounting(
        reader_version=PROMPT_VERSION,
        reader_path="spreadsheet_cells",
    )
    for row_number, raw in enumerate(chosen.rows, start=1):
        row_id = f"sheet:{chosen.page_no}:row:{row_number}"
        accounting.detect(
            row_id,
            page=chosen.page_no,
            row_number=row_number,
        )
        if not any(str(value or "").strip() for value in raw):
            accounting.account(
                row_id,
                disposition="blank",
                reason="blank_source_row",
            )
            continue
        fields = {
            field: raw[index].strip()
            for index, field in sorted(chosen.mapping.items())
            if index < len(raw) and raw[index].strip()
        }
        if not all(fields.get(name) for name in chosen.required_fields):
            accounting.account(
                row_id,
                disposition="skipped",
                reason="missing_required_fields",
            )
            continue
        unmapped_values = {
            heading: raw[index].strip()
            for index, heading in chosen.unmapped_columns
            if index < len(raw) and raw[index].strip()
        }
        fields = {
            **fields,
            **{
                f"unmapped:{heading}": value
                for heading, value in unmapped_values.items()
            },
        }
        quote = row_text(raw)
        candidate = propose(
            document,
            kind="evidence",
            fields=fields,
            page_no=chosen.page_no,
            quote=quote,
            quote_verified=quote_appears_on(quote, page_text, threshold),
            whole_row=True,
            confidence=None,
            prompt_version=PROMPT_VERSION,
            tier=f"{TIER_NATIVE}:{chosen.kind}",
            dedupe=dedupe_hint(
                {
                    "external_org": fields.get("external_org", ""),
                    "utility_type": chosen.kind,
                    "station_from": fields.get("probe_number")
                    or fields.get("test_hole_number", ""),
                    "station_to": "",
                }
            ),
            text_source="cells",
            unverified=sorted(unverified_fields(fields, page_text)),
            unmapped=chosen.unmapped_headings,
        )
        candidate.payload_json["citations"][0]["table_row"] = row_number
        candidate.payload_json["citations"][0]["sheet_name"] = chosen.sheet.name
        candidates.append(candidate)
        accounting.account(
            row_id,
            disposition="extracted",
            reason="candidate_recorded",
        )
    document.extraction_tiers = {TIER_NATIVE: 1}
    document.header_disagreements = 0
    accounted = accounting.finish(candidates)
    session.add_all(candidates)
    session.flush()
    return accounted


def _candidate(
    document: Document,
    page_no: int,
    row_number: int,
    sheet_name: str,
    fields: dict[str, str],
    raw,
    page_text: str,
    unmapped: list[str],
    threshold: float,
) -> Candidate:
    # Rendered by the same function that wrote the page text, so the quote
    # is the line a reviewer reads rather than a reconstruction of one.
    quote = row_text(raw)

    candidate = propose(
        document,
        kind="dependency",
        fields=fields,
        page_no=page_no,
        quote=quote,
        # Exactly, not at 0.9 — this text was generated from the same cells
        # the values came from, so there is no print damage to make room for.
        quote_verified=quote_appears_on(quote, page_text, threshold),
        whole_row=True,
        # No number, deliberately. Confidence on the page path is the
        # model's judgement about what the columns mean; here there is no
        # judgement to be more or less sure of, and a hardcoded 1.0 would be
        # this reader asserting certainty it was never asked for.
        confidence=None,
        prompt_version=PROMPT_VERSION,
        tier=TIER_NATIVE,
        dedupe=dedupe_hint(fields),
        text_source="cells",
        unverified=sorted(unverified_fields(fields, page_text)),
        unmapped=unmapped,
    )
    candidate.payload_json["citations"][0]["table_row"] = row_number
    candidate.payload_json["citations"][0]["sheet_name"] = sheet_name
    return candidate
