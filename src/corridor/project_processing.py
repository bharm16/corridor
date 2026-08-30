"""One bounded, restart-safe pass that takes a project's landed documents
through extraction and Record Inclusion.

Extraction and Record Inclusion already exist as separate primitives
(``extract_project``, ``load_project``). What did not exist was one public entry
point that runs them together for a registered project under production timing,
survives a restart between the extraction commit and the load, and never
re-pays for work already on the record. Feature-owned schedulers were rejected
(#332): this pass is invoked by the shared Due Work runtime in production and by
the same operator recovery command, so it takes a ``session_factory`` and owns
its own transactions rather than assuming an ambient one.

The pass is three acts, each in its own transaction so a crash between them is
recoverable and idempotent:

1. Scope: load the project (refuse an unknown one before any model work) and
   select the eligible extractable documents. Held (quarantined), superseded
   (sealed), unparsed, and permanently unreadable documents are excluded here,
   before the model runs, and reported rather than silently retried (ADR-0034).
2. Extraction: drive ``extract_project`` per eligible document. Each document's
   proposals and its terminal Extraction Run commit together; a completed run
   is skipped without re-reading; a failed document does not stop its siblings.
   Every completed run dirties the durable Record Inclusion watermark in its own
   commit (see ``record_inclusion``).
3. Reconcile: run the watermark-gated Record Inclusion. It loads when the
   project is pending — including the case where every extraction was skipped
   but a prior crash left the watermark dirty — and is a no-op that appends no
   Policy Runs when the project is clean (ADR-0029, #342).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.admission import LoadResult, reconcile_record_inclusion
from corridor.extract_project import (
    Outcome,
    RouteSelector,
    extract_project,
    extractable_document,
)
from corridor.extraction_runs import completed_document_ids
from corridor.models import Document, DocumentQuarantine, ExtractionRun, Project


# An extraction run outcome that a later pass must not blindly re-read: an
# unhandled layout or a matrix the geometry could not find is a permanent
# condition, not a transient failure. A transient ``failed`` run stays eligible
# for a bounded retry; these do not (#342 retry-eligibility).
_PERMANENT_FAILURE_OUTCOMES = ("unreadable", "no_matrix")


class ProcessingScopeRefused(ValueError):
    """The pass was asked to process an unknown or out-of-scope project."""


@dataclass(frozen=True)
class ProcessingPassResult:
    """The honest per-document and per-pass result of one processing pass.

    ``outcomes`` are the per-document extraction outcomes exactly as
    ``extract_project`` reports them, so a caller can tell an extracted document
    from a skipped, failed, unreadable, or quarantined one without re-deriving
    it from row counts. ``excluded`` names the eligible-but-held-out documents by
    reason. ``reconciled`` and ``load`` describe the Record Inclusion pass.
    """

    project_id: int
    eligible_document_count: int
    outcomes: list[Outcome]
    excluded: dict[str, int]
    processing_failures: list[str]
    reconciled: bool
    load: LoadResult | None

    def _count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)

    @property
    def extracted(self) -> int:
        return self._count("extracted")

    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @property
    def failed(self) -> int:
        return self._count("failed")

    @property
    def unreadable(self) -> int:
        return self._count("unreadable")

    @property
    def quarantined(self) -> int:
        return self._count("quarantined")

    @property
    def held_out(self) -> int:
        return sum(self.excluded.values())

    @property
    def admitted(self) -> int:
        return self.load.admitted_count if self.load is not None else 0

    @property
    def waiting(self) -> int:
        return self.load.waiting_count if self.load is not None else 0

    @property
    def ambiguous_documents(self) -> list[str]:
        return list(self.load.ambiguous_documents) if self.load is not None else []


def process_project(
    session_factory,
    *,
    project_id: int,
    select_route: RouteSelector,
    clock,
) -> ProcessingPassResult:
    """Run one bounded processing pass for a registered project.

    ``select_route`` is the per-document reader selector — injected so tests and
    the production runtime supply their own model boundary — and ``clock`` is the
    controlled time source. The pass owns its transactions through
    ``session_factory``; it does not hold a project mutation lock across the
    model requests inside extraction.
    """

    with session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ProcessingScopeRefused(
                f"project {project_id} is not a registered project"
            )
        eligible, excluded = _eligible_documents(session, project_id)
        eligible_shas = [document.sha256 for document in eligible]

        outcomes: list[Outcome] = []
        processing_failures: list[str] = []
        for sha256 in eligible_shas:
            try:
                outcomes.extend(
                    extract_project(
                        session,
                        project,
                        select_route=select_route,
                        document_sha256=sha256,
                        commit=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one document must not sink its siblings
                # ``extract_project`` records and commits a failed Extraction Run
                # before re-raising an unexpected error, so the failure is
                # durable; the pass notes it and continues with clean siblings.
                session.rollback()
                processing_failures.append(f"{sha256}: {type(exc).__name__}: {exc}")

    # A relevant pass always reconciles unfinished Record Inclusion, even when
    # every extraction was skipped: a prior crash after an extraction commit but
    # before the load leaves the watermark dirty, and only this step finishes it.
    with session_factory() as reconciling:
        with reconciling.begin():
            reconcile = reconcile_record_inclusion(reconciling, project_id)

    return ProcessingPassResult(
        project_id=project_id,
        eligible_document_count=len(eligible_shas),
        outcomes=outcomes,
        excluded=excluded,
        processing_failures=processing_failures,
        reconciled=reconcile.did_load,
        load=reconcile.load,
    )


def _eligible_documents(
    session: Session, project_id: int
) -> tuple[list[Document], dict[str, int]]:
    """Select the extractable documents a production pass may read.

    Excludes, before any model work: superseded documents (a sealed input can
    never regain actionable proposals), quarantined documents (a held input is
    deliberately unread), documents whose parse did not succeed, and documents
    whose only terminal reading is a permanent unreadable/no-matrix outcome.
    Each exclusion is counted by reason for honest reporting.
    """

    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.doc_date, Document.id)
    ).all()
    quarantined = set(
        session.scalars(
            select(DocumentQuarantine.document_id)
            .join(Document, Document.id == DocumentQuarantine.document_id)
            .where(Document.project_id == project_id)
        ).all()
    )
    completed = completed_document_ids(session, project_id)
    permanently_failed = set(
        session.scalars(
            select(ExtractionRun.document_id)
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(
                Document.project_id == project_id,
                ExtractionRun.outcome.in_(_PERMANENT_FAILURE_OUTCOMES),
            )
        ).all()
    )

    eligible: list[Document] = []
    excluded = {
        "superseded": 0,
        "held_quarantined": 0,
        "failed_parse": 0,
        "unreadable_permanent": 0,
    }
    for document in documents:
        if not extractable_document(document):
            continue
        if document.superseded_by is not None:
            excluded["superseded"] += 1
        elif document.id in quarantined:
            excluded["held_quarantined"] += 1
        elif document.parse_status != "parsed":
            excluded["failed_parse"] += 1
        elif document.id in permanently_failed and document.id not in completed:
            excluded["unreadable_permanent"] += 1
        else:
            eligible.append(document)
    return eligible, excluded


def summarize_pass(
    result: ProcessingPassResult,
    *,
    configuration_version: str,
    observed_at: datetime,
) -> dict:
    """A bounded, counts-only receipt of one pass, for the Due Work handler.

    The rich per-document detail lives in ``ProcessingPassResult``; a durable
    receipt keeps only counts and a health verdict so it stays within the
    handler's byte contract regardless of project size.
    """

    processing_failures = (
        result.failed
        + result.unreadable
        + result.quarantined
        + len(result.processing_failures)
    )
    # Held-out documents (quarantined, superseded, unparsed) are a reported
    # steady state, not a failure of this pass, so they do not flip the verdict —
    # only this pass's own processing failures do. Their count rides on the
    # receipt for an operator who wants it.
    health = (
        "healthy"
        if processing_failures == 0
        else "processing_attention_required"
    )
    return {
        "schema_version": "project-processing-result-v1",
        "project_id": result.project_id,
        "configuration_version": configuration_version,
        "observed_at": observed_at.isoformat(),
        "health": health,
        "eligible_document_count": result.eligible_document_count,
        "extracted": result.extracted,
        "skipped": result.skipped,
        "failed": result.failed,
        "unreadable": result.unreadable,
        "quarantined": result.quarantined,
        "held_out": result.held_out,
        "processing_failures": processing_failures,
        "reconciled": result.reconciled,
        "admitted": result.admitted,
        "waiting": result.waiting,
        "ambiguous_documents": len(result.ambiguous_documents),
    }
