"""Draft source-bound Key date rows without creating schedule authority.

The existing Key dates import preview and confirmation already own the only
write path for schedule dates.  A model is useful only before that seam: it
can transcribe a small, explicitly selected portion of one registered source
into a *draft*.  This module binds the source bytes and pages, bounds the one
read-only runtime call, verifies each retained word against the source, and
keeps a redacted non-authoritative receipt.  It has no Milestone, Dependency,
or schedule-link writer.

The previous tempting design was to let a schedule reader feed the importer
directly.  That would turn an uncertain transcription into a changed Required
By date and silently discard scheduling relationships Corridor does not model.
Instead a person sees the ordinary preview and invokes the ordinary attributable
import (ADRs 0045, 0048, 0055, and 0057; ticket #363).
"""

from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.milestones import MilestoneImportPreview, preview_import
from corridor.models import (
    DocPage,
    Document,
    KeyDateDraftReceipt,
    KeyDateDraftRowReceipt,
)
from corridor.principals import HumanPrincipal, require_human_principal


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRECISIONS = frozenset({"day", "month", "year", "unknown"})


class StaleKeyDateDraft(ValueError):
    """The explicitly authorized source binding cannot be safely read."""


@dataclass(frozen=True)
class KeyDateDraftBudget:
    """Deliberately small, declared limits for exactly one source request."""

    max_rows: int = 50
    max_input_chars: int = 20_000
    max_output_tokens: int = 2_000
    max_elapsed_ms: int = 30_000
    max_spend_usd_micros: int = 250_000

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_rows,
                self.max_input_chars,
                self.max_output_tokens,
                self.max_elapsed_ms,
                self.max_spend_usd_micros,
            )
        ):
            raise ValueError("Key date drafting limits must be positive")

    def receipt_json(self) -> dict[str, int]:
        return {
            "max_rows": self.max_rows,
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "max_elapsed_ms": self.max_elapsed_ms,
            "max_spend_usd_micros": self.max_spend_usd_micros,
        }


@dataclass(frozen=True)
class SourceBoundKeyDateDraftRequest:
    """A person's exact authorization to read one document rendition."""

    project_id: int
    document_id: int
    source_sha256: str
    allowed_pages: tuple[int, ...]
    requested_by: HumanPrincipal

    def __post_init__(self) -> None:
        require_human_principal(self.requested_by)
        if self.project_id <= 0 or self.document_id <= 0:
            raise ValueError("a draft request needs one project and source document")
        if not _SHA256.fullmatch(self.source_sha256):
            raise ValueError("a draft request needs a SHA-256 source binding")
        if not self.allowed_pages or any(
            type(page) is not int or page <= 0 for page in self.allowed_pages
        ):
            raise ValueError("a draft request needs positive allowed pages")
        if len(set(self.allowed_pages)) != len(self.allowed_pages):
            raise ValueError("a draft request cannot repeat an allowed page")


@dataclass(frozen=True)
class AuthorizedSourcePage:
    """The only source text a drafting runtime may receive for this request."""

    page_no: int
    text: str


@dataclass(frozen=True)
class KeyDateDraftRow:
    """A proposed named event, never an import command or schedule decision."""

    code: str
    name: str
    scheduled_for: str | None
    precision: str
    page_no: int
    quote: str


@dataclass(frozen=True)
class QuarantinedSequencing:
    """A quoted relationship Corridor deliberately does not represent."""

    page_no: int
    quote: str


@dataclass(frozen=True)
class KeyDateDraftRuntimeOutput:
    """The strict, deliberately decision-free output shape for one runtime."""

    rows: tuple[KeyDateDraftRow, ...]
    usage: dict[str, int]
    sequencing: tuple[QuarantinedSequencing, ...] = ()


class KeyDateDraftRuntime(Protocol):
    """One injected, read-only model adapter; it cannot obtain a DB session."""

    identity: dict

    def draft(
        self,
        request: SourceBoundKeyDateDraftRequest,
        pages: tuple[AuthorizedSourcePage, ...],
        budget: KeyDateDraftBudget,
    ) -> KeyDateDraftRuntimeOutput: ...


@dataclass(frozen=True)
class DraftSourceLocator:
    document_id: int
    document_sha256: str
    document_name: str
    page_no: int
    quote: str


@dataclass(frozen=True)
class DraftedKeyDateRow:
    code: str
    name: str
    need_date: date
    source: DraftSourceLocator


@dataclass(frozen=True)
class UnresolvedKeyDateDraft:
    reason: str
    page_no: int | None
    quote: str | None


@dataclass(frozen=True)
class KeyDateDraft:
    receipt_id: int
    project_id: int
    status: str
    rows: tuple[DraftedKeyDateRow, ...]
    unresolved: tuple[UnresolvedKeyDateDraft, ...]
    quarantined_sequencing: tuple[QuarantinedSequencing, ...]
    source_name: str

    @property
    def csv_content(self) -> bytes:
        """The exact ordinary-import payload, generated only from safe rows."""
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("code", "name", "need_date"))
        for row in self.rows:
            writer.writerow((row.code, row.name, row.need_date.isoformat()))
        return output.getvalue().encode("utf-8")


def draft_key_dates(
    session: Session,
    request: SourceBoundKeyDateDraftRequest,
    *,
    runtime: KeyDateDraftRuntime,
    budget: KeyDateDraftBudget | None = None,
) -> KeyDateDraft:
    """Explicitly execute one bound read and retain no schedule-side effect."""
    budget = budget or KeyDateDraftBudget()
    document, pages = _bound_source_pages(session, request, budget)
    identity = _redacted_runtime_identity(runtime)
    started = time.monotonic()
    try:
        output = runtime.draft(request, pages, budget)
    except Exception as exc:  # noqa: BLE001 - an attempted model read needs a receipt
        receipt = _record_receipt(
            session,
            request,
            document,
            identity=identity,
            budget=budget,
            status="failed",
            reason="runtime_failure",
            detail=str(exc),
            usage={},
            unresolved=(),
            sequencing=(),
        )
        return _draft_from_receipt(session, receipt, document)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    rows, unresolved, sequencing, status, reason, detail, usage = _validate_output(
        output, pages, budget, observed_elapsed_ms=elapsed_ms
    )
    receipt = _record_receipt(
        session,
        request,
        document,
        identity=identity,
        budget=budget,
        status=status,
        reason=reason,
        detail=detail,
        usage=usage,
        unresolved=unresolved,
        sequencing=sequencing,
    )
    for ordinal, row in enumerate(rows, start=1):
        session.add(
            KeyDateDraftRowReceipt(
                receipt_id=receipt.id,
                ordinal=ordinal,
                code=row.code,
                name=row.name,
                need_date=row.need_date,
                precision="day",
                source_page=row.source.page_no,
                source_quote=row.source.quote,
            )
        )
    session.flush()
    return _draft_from_receipt(session, receipt, document)


def load_key_date_draft(
    session: Session, *, receipt_id: int, project_id: int
) -> KeyDateDraft:
    """Read a draft only inside its project and re-check the source binding."""
    receipt = session.scalar(
        select(KeyDateDraftReceipt).where(
            KeyDateDraftReceipt.id == receipt_id,
            KeyDateDraftReceipt.project_id == project_id,
        )
    )
    if receipt is None:
        raise StaleKeyDateDraft("no source-bound draft exists for this project")
    document = session.get(Document, receipt.source_document_id)
    if document is None or document.project_id != project_id:
        raise StaleKeyDateDraft("source document is no longer in this project")
    if document.sha256 != receipt.source_sha256:
        raise StaleKeyDateDraft("source bytes changed after the draft was made")
    return _draft_from_receipt(session, receipt, document)


def preview_drafted_key_dates(
    session: Session, draft: KeyDateDraft
) -> MilestoneImportPreview:
    """Pass only validated day-precise rows to the existing read-only preview."""
    if not draft.rows:
        raise StaleKeyDateDraft("this draft has no supported day-precise Key date rows")
    return preview_import(
        session,
        project_id=draft.project_id,
        content=draft.csv_content,
        source_name=draft.source_name,
    )


def verify_draft_for_ordinary_import(
    session: Session,
    *,
    receipt_id: int,
    project_id: int,
    content: bytes,
    source_name: str,
) -> None:
    """Keep a draft preview bound to its source before the normal import runs."""
    draft = load_key_date_draft(session, receipt_id=receipt_id, project_id=project_id)
    if draft.status != "drafted" or not draft.rows:
        raise StaleKeyDateDraft(
            "this source-bound draft is unresolved and cannot be imported"
        )
    if source_name != draft.source_name or content != draft.csv_content:
        raise StaleKeyDateDraft("draft rows changed after their source-bound preview")


def _bound_source_pages(
    session: Session,
    request: SourceBoundKeyDateDraftRequest,
    budget: KeyDateDraftBudget,
) -> tuple[Document, tuple[AuthorizedSourcePage, ...]]:
    document = session.get(Document, request.document_id)
    if document is None or document.project_id != request.project_id:
        raise StaleKeyDateDraft("source document is not in the requested project")
    if document.sha256 != request.source_sha256:
        raise StaleKeyDateDraft("source bytes no longer match the authorized request")
    found = session.scalars(
        select(DocPage)
        .where(
            DocPage.document_id == document.id,
            DocPage.page_no.in_(request.allowed_pages),
        )
        .order_by(DocPage.page_no)
    ).all()
    if {page.page_no for page in found} != set(request.allowed_pages):
        raise StaleKeyDateDraft("an authorized source page is unavailable")
    pages = tuple(AuthorizedSourcePage(page.page_no, page.text) for page in found)
    if sum(len(page.text) for page in pages) > budget.max_input_chars:
        raise StaleKeyDateDraft(
            "authorized source pages exceed the declared input limit"
        )
    return document, pages


def _redacted_runtime_identity(runtime: KeyDateDraftRuntime) -> dict:
    identity = getattr(runtime, "identity", None)
    if not isinstance(identity, dict):
        raise ValueError("a drafting runtime must declare its identity")
    required = ("adapter", "model", "prompt_version", "configuration")
    if any(not identity.get(key) for key in required) or not isinstance(
        identity["configuration"], dict
    ):
        raise ValueError("a drafting runtime identity is incomplete")
    # Never retain a prompt, response, or source-page body in the receipt.
    return {
        "adapter": str(identity["adapter"]),
        "model": str(identity["model"]),
        "prompt_version": str(identity["prompt_version"]),
        "configuration": {
            str(key): value
            for key, value in identity["configuration"].items()
            if isinstance(value, (str, int, float, bool)) or value is None
        },
    }


def _validate_output(
    output: object,
    pages: tuple[AuthorizedSourcePage, ...],
    budget: KeyDateDraftBudget,
    *,
    observed_elapsed_ms: int,
) -> tuple[
    tuple[DraftedKeyDateRow, ...],
    tuple[UnresolvedKeyDateDraft, ...],
    tuple[QuarantinedSequencing, ...],
    str,
    str | None,
    str | None,
    dict,
]:
    if not isinstance(output, KeyDateDraftRuntimeOutput):
        return (
            (),
            (),
            (),
            "abstained",
            "invalid_output",
            "runtime output is not the strict draft schema",
            {},
        )
    if (
        not isinstance(output.rows, tuple)
        or not isinstance(output.sequencing, tuple)
        or not isinstance(output.usage, dict)
    ):
        return (
            (),
            (),
            (),
            "abstained",
            "invalid_output",
            "runtime output collections are invalid",
            {},
        )
    usage = _valid_usage(output.usage, budget, observed_elapsed_ms)
    if usage is None:
        return (
            (),
            (),
            (),
            "abstained",
            "budget_exhaustion",
            "runtime usage exceeded or omitted a declared limit",
            {},
        )
    if len(output.rows) > budget.max_rows:
        return (
            (),
            (),
            (),
            "abstained",
            "budget_exhaustion",
            "runtime returned more rows than the declared limit",
            usage,
        )
    page_text = {page.page_no: page.text for page in pages}
    unresolved: list[UnresolvedKeyDateDraft] = []
    accepted: list[DraftedKeyDateRow] = []
    for row in output.rows:
        parsed, issue = _validate_row(row, page_text)
        if issue is not None:
            unresolved.append(issue)
        elif parsed is not None:
            accepted.append(parsed)
    sequencing, sequencing_issues = _validate_sequencing(output.sequencing, page_text)
    if output.sequencing:
        # A relationship can change the meaning of every involved row.  Keep
        # all of it visible and import none until a model that represents it
        # exists; dropping the edge while keeping its dates would be deceptive.
        accepted = []
        unresolved.extend(sequencing_issues)
    return tuple(accepted), tuple(unresolved), sequencing, "drafted", None, None, usage


def _valid_usage(
    usage: dict[str, int], budget: KeyDateDraftBudget, observed_elapsed_ms: int
) -> dict[str, int] | None:
    allowed = {"input_tokens", "output_tokens", "elapsed_ms", "spend_usd_micros"}
    if set(usage) != allowed or any(
        type(value) is not int or value < 0 for value in usage.values()
    ):
        return None
    if (
        usage["output_tokens"] > budget.max_output_tokens
        or usage["elapsed_ms"] > budget.max_elapsed_ms
        or observed_elapsed_ms > budget.max_elapsed_ms
        or usage["spend_usd_micros"] > budget.max_spend_usd_micros
    ):
        return None
    return {key: int(value) for key, value in usage.items()}


def _validate_row(
    row: object, page_text: dict[int, str]
) -> tuple[DraftedKeyDateRow | None, UnresolvedKeyDateDraft | None]:
    if not isinstance(row, KeyDateDraftRow):
        return None, UnresolvedKeyDateDraft(
            "runtime row is not a supported Key date shape", None, None
        )
    code, name, quote = row.code.strip(), row.name.strip(), row.quote.strip()
    if row.precision not in _PRECISIONS:
        return None, UnresolvedKeyDateDraft(
            "source date precision is unsupported", row.page_no, quote or None
        )
    if row.page_no not in page_text or not quote or quote not in page_text[row.page_no]:
        return None, UnresolvedKeyDateDraft(
            "citation is not on an authorized source page", row.page_no, quote or None
        )
    if not code or not name or code not in quote or name not in quote:
        return None, UnresolvedKeyDateDraft(
            "named Key date fields are not supported by the cited wording",
            row.page_no,
            quote,
        )
    if row.precision != "day":
        return None, UnresolvedKeyDateDraft(
            "source date is not day-precise", row.page_no, quote
        )
    if (
        not isinstance(row.scheduled_for, str)
        or not _DAY.fullmatch(row.scheduled_for)
        or row.scheduled_for not in quote
    ):
        return None, UnresolvedKeyDateDraft(
            "scheduled date is not a cited day-precise value", row.page_no, quote
        )
    try:
        need_date = date.fromisoformat(row.scheduled_for)
    except ValueError:
        return None, UnresolvedKeyDateDraft(
            "scheduled date is invalid", row.page_no, quote
        )
    return (
        DraftedKeyDateRow(
            code=code,
            name=name,
            need_date=need_date,
            source=DraftSourceLocator(0, "", "", row.page_no, quote),
        ),
        None,
    )


def _validate_sequencing(
    relationships: tuple[QuarantinedSequencing, ...], page_text: dict[int, str]
) -> tuple[tuple[QuarantinedSequencing, ...], tuple[UnresolvedKeyDateDraft, ...]]:
    retained: list[QuarantinedSequencing] = []
    issues: list[UnresolvedKeyDateDraft] = []
    for relationship in relationships:
        if not isinstance(relationship, QuarantinedSequencing):
            issues.append(
                UnresolvedKeyDateDraft(
                    "unsupported sequencing output is malformed", None, None
                )
            )
            continue
        quote = relationship.quote.strip()
        if (
            relationship.page_no not in page_text
            or not quote
            or quote not in page_text[relationship.page_no]
        ):
            issues.append(
                UnresolvedKeyDateDraft(
                    "sequencing citation is not on an authorized source page",
                    relationship.page_no,
                    quote or None,
                )
            )
            continue
        retained.append(QuarantinedSequencing(relationship.page_no, quote))
        issues.append(
            UnresolvedKeyDateDraft(
                "source contains unsupported sequencing; no rows were imported",
                relationship.page_no,
                quote,
            )
        )
    return tuple(retained), tuple(issues)


def _record_receipt(
    session: Session,
    request: SourceBoundKeyDateDraftRequest,
    document: Document,
    *,
    identity: dict,
    budget: KeyDateDraftBudget,
    status: str,
    reason: str | None,
    detail: str | None,
    usage: dict,
    unresolved: tuple[UnresolvedKeyDateDraft, ...],
    sequencing: tuple[QuarantinedSequencing, ...],
) -> KeyDateDraftReceipt:
    receipt = KeyDateDraftReceipt(
        project_id=request.project_id,
        source_document_id=document.id,
        source_sha256=document.sha256,
        allowed_pages_json=list(request.allowed_pages),
        requested_by=request.requested_by.subject,
        status=status,
        reason=reason,
        detail=detail,
        configuration_json=identity,
        budget_json=budget.receipt_json(),
        usage_json=usage,
        unresolved_json=[
            {"reason": item.reason, "page_no": item.page_no, "quote": item.quote}
            for item in unresolved
        ],
        sequencing_json=[
            {"page_no": item.page_no, "quote": item.quote} for item in sequencing
        ],
        non_authoritative=True,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _draft_from_receipt(
    session: Session, receipt: KeyDateDraftReceipt, document: Document
) -> KeyDateDraft:
    rows = session.scalars(
        select(KeyDateDraftRowReceipt)
        .where(KeyDateDraftRowReceipt.receipt_id == receipt.id)
        .order_by(KeyDateDraftRowReceipt.ordinal)
    ).all()
    return KeyDateDraft(
        receipt_id=receipt.id,
        project_id=receipt.project_id,
        status=receipt.status,
        rows=tuple(
            DraftedKeyDateRow(
                code=row.code,
                name=row.name,
                need_date=row.need_date,
                source=DraftSourceLocator(
                    document_id=document.id,
                    document_sha256=document.sha256,
                    document_name=document.filename,
                    page_no=row.source_page,
                    quote=row.source_quote,
                ),
            )
            for row in rows
        ),
        unresolved=tuple(
            UnresolvedKeyDateDraft(
                reason=str(item["reason"]),
                page_no=item.get("page_no"),
                quote=item.get("quote"),
            )
            for item in receipt.unresolved_json
        ),
        quarantined_sequencing=tuple(
            QuarantinedSequencing(int(item["page_no"]), str(item["quote"]))
            for item in receipt.sequencing_json
        ),
        source_name=document.filename,
    )
