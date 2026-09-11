"""One bounded, restart-safe pass that takes a project's landed documents
through reading, extraction and Record Inclusion.

Reading, extraction and Record Inclusion already exist as separate primitives
(``parse_registered_document``, ``extract_project``, ``load_project``). What did
not exist was one public entry
point that runs them together for a registered project under production timing,
survives a restart between the extraction commit and the load, and never
re-pays for work already on the record. Feature-owned schedulers were rejected
(#332): this pass is invoked by the shared Due Work runtime in production and by
the same operator recovery command, so it takes a ``session_factory`` and owns
its own transactions rather than assuming an ambient one.

The pass is four acts, each in its own transaction so a crash between them is
recoverable and idempotent:

1. Read: render and parse every registered document whose bytes nothing has
   read yet, one transaction each. A registration is now allowed to commit
   without a read — ``source_intake.confirm_intake`` does exactly that, so a
   person confirming an upload is not held open while forty pages render
   (#893) — and this is what makes that safe rather than a permanent pending
   state. Selection is the document's own ``pending`` status, so a crash
   before or during a read leaves the document selected for the next pass and
   a read that committed is never repeated, and the row is claimed for the
   duration of its own read so two overlapping workers cannot both take it.
   It covers every registered document and not only the extractable ones,
   because a source no extractor reads still has pages and segments a citation
   is replayed against, and a kind this act skipped would wait for a reader
   that never came.
2. Scope: load the project (refuse an unknown one before any model work) and
   select the eligible extractable documents. Held (quarantined), superseded
   (sealed), unread, unreadable, and permanently unreadable documents are
   excluded here,
   before the model runs, and reported rather than silently retried (ADR-0034).
3. Extraction: drive ``extract_project`` per eligible document. Each document's
   proposals and its terminal Extraction Run commit together; a completed run
   is skipped without re-reading; a failed document does not stop its siblings.
   Every completed run dirties the durable Record Inclusion watermark in its own
   commit (see ``record_inclusion``).
4. Reconcile: run the watermark-gated Record Inclusion. It loads when the
   project is pending — including the case where every extraction was skipped
   but a prior crash left the watermark dirty — and is a no-op that appends no
   Policy Runs when the project is clean (ADR-0029, #342).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import operations_repair
from corridor.admission import LoadResult, reconcile_record_inclusion
from corridor.config import settings
from corridor.extract_project import (
    Outcome,
    RouteSelector,
    extract_project,
    extractable_document,
)
from corridor.extraction_runs import completed_document_ids
from corridor.ingest import parse_registered_document
from corridor.models import Document, DocumentQuarantine, ExtractionRun, Project


# An extraction run outcome that a later pass must not blindly re-read: an
# unhandled layout or a matrix the geometry could not find is a permanent
# condition, not a transient failure. A transient ``failed`` run stays eligible
# for a bounded retry; these do not (#342 retry-eligibility).
#
# "Permanent" meant "for ever" until #842, and the customer-journey audit found
# that to be a dead end: nothing could ever take the source again, whatever
# operations did about the condition that stopped it. It now means "until a
# technical operator records a repair against it", which is one attributable
# receipt and not a flag anybody can set -- see ``corridor.operations_repair``.
_PERMANENT_FAILURE_OUTCOMES = operations_repair.PERMANENT_FAILURE_OUTCOMES


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
    ``parsed_document_count`` is how many documents this pass read for the
    first time and *committed*, which is the one number that says a confirmed
    upload was picked up rather than left waiting (#893).
    """

    project_id: int
    eligible_document_count: int
    outcomes: list[Outcome]
    excluded: dict[str, int]
    processing_failures: list[str]
    reconciled: bool
    load: LoadResult | None
    parsed_document_count: int = 0

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

    # Read what nobody has read, before the scope is taken, so a document
    # confirmed since the last pass becomes eligible in this one rather than
    # waiting a whole cadence for a second (#893). An unknown project selects
    # no documents, so this spends nothing before the refusal below.
    parsed_document_count, parse_failures = _parse_landed_documents(
        session_factory, project_id
    )

    with session_factory() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ProcessingScopeRefused(
                f"project {project_id} is not a registered project"
            )
        eligible, excluded = _eligible_documents(session, project_id)
        eligible_shas = [document.sha256 for document in eligible]

        outcomes: list[Outcome] = []
        processing_failures: list[str] = list(parse_failures)
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
        parsed_document_count=parsed_document_count,
    )


def _parse_landed_documents(
    session_factory, project_id: int
) -> tuple[int, list[str]]:
    """Read every registered document of this project nothing has read yet.

    One transaction per document, because that is what makes a crash cheap and
    a retry honest: a read that commits carries its pages, its segments and the
    status flip together, and a read that does not commit leaves the document
    exactly as it was — ``pending``, and selected again by the next pass. The
    status is what is selected on, so a committed read is never repeated and a
    lost one is never dropped.

    A document the reader cannot use ends ``failed`` rather than ``pending``,
    which takes it out of this selection and hands it to the bounded,
    attributable re-parse (``location_discovery.recover_document_parse``)
    rather than to a pass that would fail on it every hour for ever. That
    failure is this pass's own, so it is reported as a processing failure and
    not as a held-out steady state.

    **Why the row is claimed and not merely re-checked.** Production scheduling
    does not keep two workers out of this loop. ``due_work.claim_due_work``
    locks its candidate occurrences ``for update skip locked`` and holds the
    project-processing schedule to one live claim, but an occurrence whose
    lease has expired is deliberately claimable again -- that is how a killed
    worker's work is recovered -- and nothing stops the first worker, which may
    still be alive and mid-pass, from carrying on. ``process_project`` never
    re-checks its claim token, so the two overlap here. Re-reading the status
    is not enough on its own: both workers can see ``pending`` before either
    commits, and both would then render the same file and write the same pages.
    The ``for update skip locked`` below is the per-document exclusion that
    answers it. The second worker's ``select`` returns nothing, so it leaves
    the document to the worker that holds it rather than blocking behind a
    render; the row stays ``pending`` and the next pass takes it if that worker
    never commits.
    """

    images_dir = settings.corpus_images
    with session_factory() as scoping:
        pending = scoping.scalars(
            select(Document.id)
            .where(
                Document.project_id == project_id,
                Document.parse_status == "pending",
            )
            .order_by(Document.id)
        ).all()

    parsed = 0
    failures: list[str] = []
    for document_id in pending:
        with session_factory() as reading:
            try:
                with reading.begin():
                    document = reading.scalars(
                        select(Document)
                        .where(Document.id == document_id)
                        .with_for_update(skip_locked=True)
                    ).first()
                    if document is None or document.parse_status != "pending":
                        # Either another worker is holding this row's read open
                        # right now, or one committed between the selection and
                        # here. Its commit is the one that counts.
                        continue
                    sha256 = document.sha256
                    read = parse_registered_document(
                        reading, document=document, images_dir=images_dir
                    )
            except Exception as exc:  # noqa: BLE001 — one document must not sink its siblings
                # The transaction is already rolled back by the failing
                # ``begin`` block, so the document is still ``pending`` and
                # the next pass takes it again.
                failures.append(f"document {document_id}: {type(exc).__name__}: {exc}")
                continue
        # Counted here rather than inside the block above, because the number
        # this returns is a claim about committed state: a read that succeeded
        # and then failed to commit rolled its pages back with it, and a
        # receipt that had already counted it would report a parse this
        # database does not hold. The commit is behind us on this line, and a
        # commit that raised took the ``except`` above instead.
        if read:
            parsed += 1
        else:
            failures.append(f"{sha256}: parse failed")
    return parsed, failures


def _eligible_documents(
    session: Session, project_id: int
) -> tuple[list[Document], dict[str, int]]:
    """Select the extractable documents a production pass may read.

    Excludes, before any model work: superseded documents (a sealed input can
    never regain actionable proposals), quarantined documents (a held input is
    deliberately unread), documents whose parse failed, documents nothing has
    read yet, and documents whose only terminal reading is a permanent
    unreadable/no-matrix outcome. Each exclusion is counted by reason for
    honest reporting.

    ``failed_parse`` and ``awaiting_parse`` are counted apart because they are
    owed to different people (#893). A failed parse waits for the bounded,
    attributable re-parse; an unread document waits for nothing at all — the
    read act at the top of this pass takes it, and one still counted here was
    confirmed after that act ran or could not be read this time round. Counting
    the second as the first would report a source that needs somebody as a
    source that needs nobody.

    The last of those four is the one a repair lifts (#842). A repair receipt
    names the newest reading this source had when it was made, and the source
    is taken again while no newer permanent reading exists -- so one receipt
    re-admits the source once and a second identical failure excludes it again,
    rather than a single old repair making the source eligible for ever. The
    repair itself is refused unless the actor holds the technical-operations
    designation, so this reads a receipt rather than deciding an authority. The
    other three exclusions stand: a quarantine is not released by a repair, a
    sealed input is never re-read, and a failed parse is put right by the
    bounded re-parse rather than by this selection.
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
    permanently_failed = {
        int(document_id): int(run_id)
        for document_id, run_id in session.execute(
            select(ExtractionRun.document_id, func.max(ExtractionRun.id))
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(
                Document.project_id == project_id,
                ExtractionRun.outcome.in_(_PERMANENT_FAILURE_OUTCOMES),
            )
            .group_by(ExtractionRun.document_id)
        ).all()
    }
    repaired = operations_repair.repaired_through(
        session, document_ids=sorted(permanently_failed)
    )

    eligible: list[Document] = []
    excluded = {
        "superseded": 0,
        "held_quarantined": 0,
        "failed_parse": 0,
        "awaiting_parse": 0,
        "unreadable_permanent": 0,
    }
    for document in documents:
        if not extractable_document(document):
            continue
        if document.superseded_by is not None:
            excluded["superseded"] += 1
        elif document.id in quarantined:
            excluded["held_quarantined"] += 1
        elif document.parse_status == "failed":
            excluded["failed_parse"] += 1
        elif document.parse_status != "parsed":
            excluded["awaiting_parse"] += 1
        elif (
            document.id in permanently_failed
            and document.id not in completed
            and repaired.get(int(document.id), 0)
            < permanently_failed[int(document.id)]
        ):
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
    # Held-out documents (quarantined, superseded, already-failed) are a reported
    # steady state, not a failure of this pass, so they do not flip the verdict —
    # only this pass's own processing failures do. Their count rides on the
    # receipt for an operator who wants it. A document *this* pass could not
    # read is on the other side of that line: it is in
    # ``result.processing_failures`` above, so it does flip the verdict (#893).
    health = (
        "healthy"
        if processing_failures == 0
        else "processing_attention_required"
    )
    return {
        "schema_version": "project-processing-result-v2",
        "project_id": result.project_id,
        "configuration_version": configuration_version,
        "observed_at": observed_at.isoformat(),
        "health": health,
        "eligible_document_count": result.eligible_document_count,
        "parsed": result.parsed_document_count,
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
