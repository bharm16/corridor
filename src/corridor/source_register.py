"""Every delivery this project has received, and what became of each one (#841).

The page the Work screen sent a coordinator to was honest about itself and
nothing else.  Its heading said **Uploaded documents** and its reader,
``source_intake.list_confirmed_uploads``, selected documents carrying a
``CONFIRM_SOURCE_INTAKE`` receipt — so a delivery the intake gate refused, one
whose bytes never reached the store, one a connector pulled, and one somebody
staged and walked away from were all absent, while the link that reached it
promised "every document delivered to this project".  The audit of 2026-09-10
recorded the consequence: an absence on that page reads as "Corridor never got
it", and four different records produce the same absence.

ADR-0089 is why this can be one reading now.  A delivery is persisted once
whatever transport carried it, with its disposition and its refusal evidence,
and #823 put the product upload into that family — so "did Corridor receive
this" is a row in ``source_deliveries`` rather than an inference from what
happened to be registered afterwards.  The register is that ledger, joined to
what the registered Document shows about processing, and its spine is the
delivery: one row per delivery, refused and failed ones included, each with the
reason the record actually holds.

**A registered source that dereferences no delivery still appears.**  The
corpus path registers documents that arrived through no transport at all, and
``Document.source_delivery_id`` is nullable exactly so a reading can say "this
source names no delivery" instead of guessing one (#675).  Leaving those rows
out would make the register look complete while the file the record was built
from was missing from it, which is the defect this module exists to remove.
``issue_coverage.derive_coverage_reading`` already reads its project the same
way, for the same reason, and this is deliberately the same shape.

**Processed never means nothing else needs attention.**  A completed extraction
can leave open Proposed Deltas nobody has decided, and a row that said only
"Processed" would report a finished job where a coordinator still owes a
decision.  So the state a row carries is the processing state *and* what is
still open: a processed row with open questions reads as needing attention and
says how many.

**What is blocked names one owner, and it is the Issue section's own.**  A file
Corridor could not read, could not store, or deliberately does not read is a
mechanical problem, and a coordinator cannot decide their way out of one — the
same rule ``project_workflow`` applies when it keeps source failures in issue
readiness rather than in review.  So the owners are that module's two, imported
rather than spelled again: a coordinator moves between the Issue section and
this page, and two screens disagreeing about who owns a mechanical failure is
worse than either answer alone (#840).  Neither is a designation claim — naming
one would assert an authority rule the write path does not enforce (#839).  A
row that is not blocked names nobody, because inventing an owner for work
nobody has to do is how a register becomes a queue.

**A blocked row also says what has been done about it (#842).**  Naming an
owner answers "who acts next" and leaves "and has anybody?" unanswered, which
is how a source sits blocked for a fortnight while everybody assumes somebody
is on it.  So a row whose processing failed, and any row a repair was recorded
against, carries the repair standing beside the owner: what operations did,
when, by whom, and whether the source has been read since.  It is read from the
receipt ``operations_repair`` writes, which is the same receipt the standing
pass reads to take the source again -- so an engineer who repaired a project by
hand appears here exactly as an operator does, which is what the 2026-09-10
audit asked for.

**A source no extractor reads says so, instead of waiting forever.**  Only some
declared kinds are ever handed to an extractor, and a ``plan`` PDF or an
undeclared source registered as ``other`` is never one of them — which used to
leave it at "waiting for the processing pass", a pass that was never going to
take it.  ``extraction_run_queries.extractable_document`` is the one predicate
that answers this, and it moved there from ``extract_project`` so that a reader
can ask the question without importing the engines that act on it.  This is not
a blocked row: nothing is lost and nobody has to do anything, and the register
says exactly that.

**"Waiting for the processing pass" is now a state a row really sits in
(#893).**  The confirmation used to render and parse the file inside the web
request, so a registered source was read or unreadable by the time anybody
could look at this page, and ``pending`` covered only the gap between a
completed read and the extraction that had not run yet.  The read is the
standing pass's now, so a source can be registered and genuinely unread for as
long as the cadence takes.  The words did not have to change, because they were
already the true ones; what had to be true is that the pass really takes it,
and ``project_processing`` selects exactly this state.  Nothing about the row
changes either: it names no owner and no next action, because nobody has to do
anything.  A read that fails leaves ``parse_failed`` here, which does name an
owner -- that is the difference between unread and unreadable, and this page is
where a coordinator sees it.

**No clock.**  Every ordering and every filter is read from the rows' own
recorded times and from the caller's declared bounds, so two readings of the
same records land on the same page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    DeltaGroup,
    DocumentQuarantine,
    Document,
    ExtractionRun,
    Fact,
    SourceDelivery,
    SourceDeliveryConfirmation,
)
from corridor.extraction_run_queries import extractable_document
from corridor.operations_repair import (
    CORRECTED_MAPPING,
    RepairReceipt,
    repair_receipts,
)
from corridor.project_workflow import COORDINATOR_OWNER, OPERATIONS_OWNER
from corridor.review_packet_reading import current_deltas, open_deltas
from corridor.source_delivery import (
    DISPOSITION_DUPLICATE,
    DISPOSITION_QUARANTINED,
    DISPOSITION_STORED,
    DISPOSITION_TERMINALLY_REFUSED,
    DISPOSITION_TRANSIENT_FAILURE,
    delivery_confirmations,
)


# How many deliveries one page of the register prints. The register is the one
# reading here that no search narrows down to a single row, so the page says
# when it has been cut and offers the deliveries received before the oldest one
# shown, exactly as the Record view's audit trail does (#830).
PAGE_LIMIT = 50

# The two owners a blocked row can name, which are the Issue section's two
# (#840), imported so that the two screens cannot drift apart. `Corridor
# Operations` is the bounded context `CONTEXT-MAP.md` names; `You, on this
# page` is the person reading, and it is true here because the one act this
# page offers -- uploading a document -- is where both coordinator-owned
# recoveries start.
OWNER_CORRIDOR_OPERATIONS = OPERATIONS_OWNER
OWNER_YOU = COORDINATOR_OWNER


@dataclass(frozen=True, slots=True)
class ProcessingOutput:
    """What reading one source actually produced, from its own receipts.

    Row accounting is the sealed reader's, so it is present exactly where the
    reader that seals it ran and absent — not zero — everywhere else. The three
    counts below are always answerable: a source that produced nothing produced
    nothing, which is a fact rather than a gap.
    """

    rows_detected: int | None
    rows_extracted: int | None
    rows_unaccounted: int | None
    facts: int
    proposed_changes: int
    open_questions: int


@dataclass(frozen=True, slots=True)
class RepairStanding:
    """What operations has done about one blocked source, and what came of it.

    ``procedure`` is empty when nothing has been recorded, which is a fact
    about this source rather than a gap: the sentence says so, and a screen
    never has to decide what an absent receipt means.
    """

    procedure: str
    performed_by: str
    performed_at: datetime | None
    read_again: bool
    sentence: str

    @property
    def recorded(self) -> bool:
        return bool(self.procedure)


@dataclass(frozen=True, slots=True)
class RegisterRow:
    """One delivery — or one registered source that names none — and its state."""

    key: str
    delivery_id: int | None
    document_id: int | None
    filename: str
    transport: str
    channel: str
    received_at: datetime | None
    content_sha256: str
    external_identity: str
    external_version: str
    doc_type: str
    source_family: str
    revision_relationship: str
    state: str
    tone: str
    state_words: str
    recorded_reason: str
    owner: str
    next_action: str
    confirmed_by: str
    confirmed_at: datetime | None
    output: ProcessingOutput | None
    repair: RepairStanding | None = None

    @property
    def is_blocked(self) -> bool:
        return bool(self.owner)


@dataclass(frozen=True, slots=True)
class RegisterFilters:
    """The bounds a reading was taken under, echoed back to the controls."""

    state: str = ""
    family: str = ""
    received_from: datetime | None = None
    received_to: datetime | None = None

    @property
    def any_applied(self) -> bool:
        return bool(
            self.state
            or self.family
            or self.received_from is not None
            or self.received_to is not None
        )


@dataclass(frozen=True, slots=True)
class SourceRegister:
    """One page of one project's register, and what was cut to make it."""

    rows: tuple[RegisterRow, ...]
    matching: int
    total: int
    filters: RegisterFilters
    states: tuple[tuple[str, str], ...]
    before: str = ""
    has_older: bool = False

    @property
    def oldest_key(self) -> str:
        """The cursor the next page starts after, empty when there is none."""

        return self.rows[-1].key if self.rows else ""


def read_source_register(
    session: Session,
    *,
    project_id: int,
    filters: RegisterFilters | None = None,
    before: str = "",
    limit: int = PAGE_LIMIT,
) -> SourceRegister:
    """Every delivery this project has received, newest first, one page of it.

    ``before`` is the key of the oldest row on the page the caller came from,
    so paging carries the filters rather than dropping the question the rest of
    the screen is answering.
    """

    applied = filters or RegisterFilters()
    rows = _rows(session, project_id)
    matching = tuple(row for row in rows if _matches(row, applied))
    start = _start_index(matching, before)
    page = matching[start : start + limit]
    return SourceRegister(
        rows=page,
        matching=len(matching),
        total=len(rows),
        filters=applied,
        states=tuple((state, STATE_WORDS[state]) for state in _states_present(rows)),
        before=before,
        has_older=start + len(page) < len(matching),
    )


def _rows(session: Session, project_id: int) -> tuple[RegisterRow, ...]:
    """One row per delivery, then one per source that dereferences none."""

    deliveries = tuple(
        session.scalars(
            select(SourceDelivery)
            .where(SourceDelivery.project_id == project_id)
            .order_by(SourceDelivery.id)
        ).all()
    )
    documents = tuple(
        session.scalars(
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.id)
        ).all()
    )
    by_delivery: dict[int, Document] = {}
    for document in documents:
        if document.source_delivery_id is not None:
            by_delivery.setdefault(int(document.source_delivery_id), document)

    confirmations = delivery_confirmations(session, project_id)
    context = _DocumentContext.read(session, project_id, documents)

    rows = [
        _delivery_row(
            delivery,
            document=by_delivery.get(int(delivery.id)),
            confirmation=confirmations.get(int(delivery.id)),
            context=context,
        )
        for delivery in deliveries
    ]
    rows.extend(
        _document_row(document, context=context)
        for document in documents
        if document.source_delivery_id is None
    )
    rows.sort(key=_ordering, reverse=True)
    return tuple(rows)


def _ordering(row: RegisterRow) -> tuple[float, int]:
    """Newest first, with a stable tie-break nothing but the record supplies."""

    at = row.received_at
    return (at.timestamp() if at is not None else 0.0, row.delivery_id or 0)


@dataclass(frozen=True, slots=True)
class _DocumentContext:
    """Everything the register knows about this project's registered sources.

    Read once per project rather than once per row: each member below is one
    grouped statement, so the number of statements does not grow with the
    number of deliveries.
    """

    quarantines: Mapping[int, str]
    runs: Mapping[int, ExtractionRun]
    facts: Mapping[int, int]
    proposed: Mapping[int, int]
    open_questions: Mapping[int, int]
    replaced_by: Mapping[int, str]
    replaces: Mapping[int, str]
    families: Mapping[int, str]
    repairs: Mapping[int, RepairReceipt]

    @classmethod
    def read(
        cls, session: Session, project_id: int, documents: Sequence[Document]
    ) -> "_DocumentContext":
        ids = [int(document.id) for document in documents]
        names = {int(document.id): document.filename for document in documents}
        if not ids:
            return cls({}, {}, {}, {}, {}, {}, {}, {}, {})

        quarantines = {
            int(document_id): str(reason)
            for document_id, reason in session.execute(
                select(
                    DocumentQuarantine.document_id, DocumentQuarantine.reason
                ).where(DocumentQuarantine.document_id.in_(ids))
            ).all()
        }
        newest = session.execute(
            select(
                ExtractionRun.document_id, func.max(ExtractionRun.id)
            )
            .where(ExtractionRun.document_id.in_(ids))
            .group_by(ExtractionRun.document_id)
        ).all()
        runs = {
            int(run.document_id): run
            for run in session.scalars(
                select(ExtractionRun).where(
                    ExtractionRun.id.in_([int(run_id) for _, run_id in newest])
                )
            ).all()
        }
        # A Source Fact dereferences a document, except where its origin is a
        # Recorded Verbal Statement and there is no document to name (ADR-0074);
        # those belong to no row of this register and are not counted here.
        facts = {
            int(document_id): int(count)
            for document_id, count in session.execute(
                select(Fact.document_id, func.count())
                .where(
                    Fact.project_id == project_id,
                    Fact.document_id.is_not(None),
                )
                .group_by(Fact.document_id)
            ).all()
        }
        # One statement answers both questions a delta group holds for this
        # register: which document a proposed change came from, and the source
        # family it was recorded under. A group bound to a Recorded Verbal
        # Statement rather than a document names no row here.
        groups: dict[int, int] = {}
        families: dict[int, str] = {}
        for group_id, document_id, family in session.execute(
            select(
                DeltaGroup.id, DeltaGroup.document_id, DeltaGroup.source_family
            ).where(
                DeltaGroup.project_id == project_id,
                DeltaGroup.document_id.is_not(None),
            )
        ).all():
            groups[int(group_id)] = int(document_id)
            families.setdefault(int(document_id), str(family))
        # The same partition the Review screen reads, asked for the whole
        # project: "proposed" is every occurrence no newer one superseded, and
        # "open" is the subset nobody has resolved (ADR-0083).
        proposed = _count_by_document(current_deltas(session, project_id=project_id), groups)
        unresolved = _count_by_document(
            open_deltas(session, project_id=project_id), groups
        )

        replaced_by: dict[int, str] = {}
        replaces: dict[int, str] = {}
        for document in documents:
            successor = document.superseded_by
            if successor is None:
                continue
            replaced_by[int(document.id)] = names.get(
                int(successor), f"document {int(successor)}"
            )
            replaces[int(successor)] = document.filename
        return cls(
            quarantines=quarantines,
            runs=runs,
            facts=facts,
            proposed=proposed,
            open_questions=unresolved,
            replaced_by=replaced_by,
            replaces=replaces,
            families=families,
            # The newest repair recorded against each of this project's
            # sources (#842). One grouped statement like every other member
            # here, and the only `audit_log` read this page makes.
            repairs=repair_receipts(session, document_ids=ids),
        )


def _count_by_document(deltas, groups: Mapping[int, int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for delta in deltas:
        document_id = groups.get(int(delta.group_id))
        if document_id is None:
            continue
        counts[document_id] = counts.get(document_id, 0) + 1
    return counts


# --- What each state is called, and who owns it when it is stuck ------------
#
# Nothing here is a new customer word. The six document states are the ones the
# register already printed; the delivery states are ADR-0089's five
# dispositions and the stored-but-unadmitted gap, in the plain words
# `issue_coverage` already renders them in, because a coverage line and a
# register row report the same record and must not disagree about it.

REFUSED_AT_INTAKE = "refused_at_intake"
DELIVERY_INCOMPLETE = "delivery_incomplete"
HELD_AT_INTAKE = "held_at_intake"
ALREADY_RECEIVED = "already_received"
AWAITING_CONFIRMATION = "awaiting_confirmation"
NOT_REGISTERED = "not_registered"
CONFIRMED_NOT_REGISTERED = "confirmed_not_registered"
# A registered source no extractor reads. Deliberately not called "excluded":
# `issue_coverage` already spends that word on a coordinator's declaration that
# one source is left out of one issue, which is a human act at a cutoff rather
# than a fact about the kind of document this is.
NOT_EXTRACTED = "not_extracted"

STATE_WORDS: Mapping[str, str] = {
    REFUSED_AT_INTAKE: "Refused at intake and not processed",
    DELIVERY_INCOMPLETE: "Delivery did not complete, so nothing was taken",
    HELD_AT_INTAKE: "Held for review and not processed",
    ALREADY_RECEIVED: "Already received; nothing new was written",
    AWAITING_CONFIRMATION: (
        "Received and stored, and nobody has confirmed it for processing yet"
    ),
    NOT_REGISTERED: "Received, and no processing receipt records it being read",
    CONFIRMED_NOT_REGISTERED: (
        "Confirmed, and no processing receipt records it being read"
    ),
    "pending": "Pending — waiting for the processing pass",
    "processed": "Processed",
    "unreadable": "Unreadable — the reader could not use it",
    "parse_failed": "Failed to parse — the file could not be read",
    "processing_failed": "Processing failed — a later pass will retry",
    "held_unmodeled": "Held — its content is deliberately not read",
    NOT_EXTRACTED: "Registered — no extractor reads this kind of document",
}

# The order the filter offers them in: what arrived, then what is waiting, then
# what is done, then what is stuck. It is a presentation order and nothing
# reads it as a lifecycle.
STATE_ORDER: tuple[str, ...] = (
    AWAITING_CONFIRMATION,
    "pending",
    "processed",
    ALREADY_RECEIVED,
    NOT_EXTRACTED,
    "held_unmodeled",
    HELD_AT_INTAKE,
    REFUSED_AT_INTAKE,
    DELIVERY_INCOMPLETE,
    NOT_REGISTERED,
    CONFIRMED_NOT_REGISTERED,
    "parse_failed",
    "unreadable",
    "processing_failed",
)

_TONES: Mapping[str, str] = {
    REFUSED_AT_INTAKE: "refused",
    DELIVERY_INCOMPLETE: "refused",
    HELD_AT_INTAKE: "attention",
    ALREADY_RECEIVED: "neutral",
    AWAITING_CONFIRMATION: "attention",
    NOT_REGISTERED: "attention",
    CONFIRMED_NOT_REGISTERED: "attention",
    "pending": "neutral",
    "processed": "settled",
    "unreadable": "refused",
    "parse_failed": "refused",
    "processing_failed": "attention",
    "held_unmodeled": "neutral",
    NOT_EXTRACTED: "neutral",
}

# What each owner actually does about each blocked row, in the Issue section's
# own voice: who acts, and then the sentence that says this page does not. A
# coordinator reading both screens should not have to work out that "a source
# that could not be read" means the same thing on each of them, so the two
# read-failure sentences are #840's, for the condition #840 names.
_OPERATIONS_READS_IT_AGAIN = (
    "Corridor Operations reads this source again, or asks for a copy it can "
    "read. Nothing on this page retries it."
)

_NEXT_ACTIONS: Mapping[str, tuple[str, str]] = {
    REFUSED_AT_INTAKE: (
        OWNER_YOU,
        "Upload a file the stated limits admit. Nothing was registered, so "
        "there is nothing to undo.",
    ),
    DELIVERY_INCOMPLETE: (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor Operations takes this delivery again. Nothing was refused "
        "about the file itself, and nothing on this page retries it.",
    ),
    HELD_AT_INTAKE: (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor Operations says what the intake policy did with these "
        "bytes. Nothing on this page releases them.",
    ),
    AWAITING_CONFIRMATION: (
        OWNER_YOU,
        "Upload it again to reach its preview, and confirm it there. The "
        "delivery is already recorded, so uploading it again registers "
        "nothing new; nothing is read until somebody confirms it.",
    ),
    NOT_REGISTERED: (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor Operations says why this delivery registered no source. "
        "Nothing on this page registers it.",
    ),
    CONFIRMED_NOT_REGISTERED: (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor Operations says why this delivery registered no source. "
        "Nothing on this page registers it.",
    ),
    "parse_failed": (OWNER_CORRIDOR_OPERATIONS, _OPERATIONS_READS_IT_AGAIN),
    "unreadable": (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor Operations reads this source again, or changes how it is "
        "read. Nothing on this page retries a run or changes an extraction "
        "policy.",
    ),
    "processing_failed": (
        OWNER_CORRIDOR_OPERATIONS,
        "A later pass retries this. Corridor Operations owns that retry, and "
        "nothing on this page changes it.",
    ),
    "held_unmodeled": (
        OWNER_CORRIDOR_OPERATIONS,
        "Corridor does not model what this document asserts, so it is "
        "registered and deliberately unread. That changes when Corridor "
        "models the relationship, not when the file is supplied again.",
    ),
}


# --- What has been done about a blocked source (#842) ----------------------
#
# The three states a repair procedure exists for: the two the standing pass
# excludes permanently, and the one it retries on its own. A row in any of
# them says what has been done, and so does any row a repair was recorded
# against, because a repair that worked must not disappear from the page the
# moment it works -- an unseen rescue is the cost the 2026-09-10 audit said
# was going unmeasured.
_REPAIRABLE_STATES = frozenset({"parse_failed", "unreadable", "processing_failed"})

_NO_REPAIR = "No repair has been recorded for this source."

_DEFAULT_PROCEDURE_WORDS = (
    "Corridor Operations put this source back to the processing pass."
)


def _procedure_words(receipt: RepairReceipt) -> str:
    """What was done, naming the mapping revision where one is the reason.

    The registration is named rather than described, because which revision a
    reading happened under is the fact a later reader needs and "corrected" is
    the person's own word for it, recorded when they registered it.
    """

    if receipt.procedure != CORRECTED_MAPPING:
        return _DEFAULT_PROCEDURE_WORDS
    return (
        "Corridor Operations put this source back to the processing pass to be "
        f"read under field mapping {receipt.under_mapping}."
    )


# What the recorded outcome adds, where the act did something the pass did not
# do later. A re-admission adds nothing: the next sentence says whether the
# pass has taken it.
_OUTCOME_WORDS: Mapping[str, str] = {
    "recovered": "Its file was read again, and this time it parsed.",
    "still_failed": "Its file was read again, and it did not parse.",
}

_READ_SINCE = "It has been read since; the state beside this is that reading."
_NOT_READ_SINCE = "Nothing has read it since."


def _repair(
    state: str, document_id: int | None, context: "_DocumentContext"
) -> RepairStanding | None:
    """The repair standing of one row, or nothing for a row that has no use for one."""

    receipt = (
        None if document_id is None else context.repairs.get(int(document_id))
    )
    if receipt is None:
        if state not in _REPAIRABLE_STATES:
            return None
        return RepairStanding(
            procedure="",
            performed_by="",
            performed_at=None,
            read_again=False,
            sentence=_NO_REPAIR,
        )
    # Whether anything has read this source since the repair, asked of the run
    # the receipt names rather than of the clock: a receipt and the reading it
    # repairs can share one transaction's `now()`, and the run identity cannot
    # tie.
    run = None if document_id is None else context.runs.get(int(document_id))
    read_again = run is not None and int(run.id) > (
        receipt.read_through_run_id or 0
    )
    parts = [
        _procedure_words(receipt),
        _OUTCOME_WORDS.get(receipt.outcome, ""),
        _READ_SINCE if read_again else _NOT_READ_SINCE,
    ]
    return RepairStanding(
        procedure=receipt.procedure,
        performed_by=receipt.performed_by,
        performed_at=receipt.performed_at,
        read_again=read_again,
        sentence=" ".join(part for part in parts if part),
    )


def _delivery_row(
    delivery: SourceDelivery,
    *,
    document: Document | None,
    confirmation: SourceDeliveryConfirmation | None,
    context: _DocumentContext,
) -> RegisterRow:
    """One ledger row, joined to whatever it registered."""

    state, reason = _delivery_state(delivery, document, context, confirmation)
    output = None if document is None else _output(int(document.id), context)
    return RegisterRow(
        key=f"delivery:{int(delivery.id)}",
        delivery_id=int(delivery.id),
        document_id=None if document is None else int(document.id),
        filename=(
            document.filename
            if document is not None
            else str(delivery.metadata_json.get("filename") or delivery.external_identity)
        ),
        transport=str(delivery.transport),
        channel=str(delivery.channel),
        received_at=delivery.received_at,
        content_sha256=str(delivery.content_sha256),
        external_identity=str(delivery.external_identity),
        external_version=_declared_revision(delivery),
        doc_type="" if document is None else str(document.doc_type),
        source_family=_family(document, context),
        revision_relationship=_revision_relationship(document, context),
        state=state,
        tone=_tone(state, output),
        state_words=_words(state, output),
        recorded_reason=reason,
        owner=_NEXT_ACTIONS.get(state, ("", ""))[0],
        next_action=_NEXT_ACTIONS.get(state, ("", ""))[1],
        confirmed_by="" if confirmation is None else confirmation.confirmed_by_principal,
        confirmed_at=None if confirmation is None else confirmation.confirmed_at,
        output=output,
        repair=_repair(
            state, None if document is None else int(document.id), context
        ),
    )


def _document_row(document: Document, *, context: _DocumentContext) -> RegisterRow:
    """One registered source that dereferences no delivery (#675).

    Its transport and channel are empty because it has none, which is the whole
    point of showing it: a corpus file is a source this project holds and never
    took delivery of, and the register says so rather than inventing a channel.
    """

    state = _document_state(document, context)
    output = _output(int(document.id), context)
    return RegisterRow(
        key=f"document:{int(document.id)}",
        delivery_id=None,
        document_id=int(document.id),
        filename=document.filename,
        transport="",
        channel="",
        received_at=document.created_at,
        content_sha256=str(document.sha256),
        external_identity="",
        external_version="",
        doc_type=str(document.doc_type),
        source_family=_family(document, context),
        revision_relationship=_revision_relationship(document, context),
        state=state,
        tone=_tone(state, output),
        state_words=_words(state, output),
        recorded_reason=context.quarantines.get(int(document.id), ""),
        owner=_NEXT_ACTIONS.get(state, ("", ""))[0],
        next_action=_NEXT_ACTIONS.get(state, ("", ""))[1],
        confirmed_by="",
        confirmed_at=None,
        output=output,
        repair=_repair(state, int(document.id), context),
    )


def _delivery_state(
    delivery: SourceDelivery,
    document: Document | None,
    context: _DocumentContext,
    confirmation: SourceDeliveryConfirmation | None,
) -> tuple[str, str]:
    """The state of one delivery, and the reason the record holds for it.

    The disposition is asked first, because a delivery whose bytes Corridor
    never took says nothing about processing: there is nothing to have
    processed. ``stored`` and ``duplicate`` are the two that mean Corridor
    holds the bytes, and only then does the registered source decide the state.
    """

    disposition = str(delivery.disposition)
    if disposition == DISPOSITION_TERMINALLY_REFUSED:
        return REFUSED_AT_INTAKE, str(delivery.refusal_reason or "")
    if disposition == DISPOSITION_TRANSIENT_FAILURE:
        return DELIVERY_INCOMPLETE, str(delivery.refusal_reason or "")
    if disposition == DISPOSITION_QUARANTINED:
        return HELD_AT_INTAKE, str(delivery.refusal_reason or "")
    if disposition == DISPOSITION_DUPLICATE and document is None:
        return ALREADY_RECEIVED, ""
    if document is not None:
        return _document_state(document, context), context.quarantines.get(
            int(document.id), ""
        )
    if confirmation is not None:
        return CONFIRMED_NOT_REGISTERED, ""
    if delivery.delivered_by_principal:
        # A person handed these bytes over and the confirmation is theirs to
        # give, so "nobody has confirmed it" is the state rather than a
        # failure. A machine delivery has no such step and reads differently.
        return AWAITING_CONFIRMATION, ""
    return NOT_REGISTERED, ""


def _document_state(document: Document, context: _DocumentContext) -> str:
    """The honest processing state of one registered source.

    Derived, never stored: a failed parse reads as failed, a quarantined
    document as held, an unreadable extraction as unreadable, and one the
    standing pass has not reached yet as pending. None is ever relabelled a
    success.
    """

    if document.parse_status == "failed":
        return "parse_failed"
    if document.parse_status != "parsed":
        return "pending"
    if int(document.id) in context.quarantines:
        return "held_unmodeled"
    run = context.runs.get(int(document.id))
    if run is None:
        # "Waiting for the processing pass" is only true of a document a pass
        # would take. Asked of the document rather than of the run, because a
        # source no extractor reads never produces a receipt to read this from.
        return "pending" if extractable_document(document) else NOT_EXTRACTED
    if run.outcome == "completed":
        return "processed"
    if run.outcome in ("unreadable", "no_matrix"):
        return "unreadable"
    return "processing_failed"


def _output(document_id: int, context: _DocumentContext) -> ProcessingOutput:
    """What reading this source produced, from the receipts that recorded it."""

    run = context.runs.get(document_id)
    accounting = (run.row_accounting_json or {}) if run is not None else {}
    unaccounted = accounting.get("unaccounted_rows")
    return ProcessingOutput(
        rows_detected=accounting.get("detected_row_count"),
        rows_extracted=accounting.get("extracted_row_count"),
        rows_unaccounted=None if unaccounted is None else len(unaccounted),
        facts=context.facts.get(document_id, 0),
        proposed_changes=context.proposed.get(document_id, 0),
        open_questions=context.open_questions.get(document_id, 0),
    )


def _tone(state: str, output: ProcessingOutput | None) -> str:
    """Processed is never the settled tone while a decision is still owed."""

    if state == "processed" and output is not None and output.open_questions:
        return "attention"
    return _TONES[state]


def _words(state: str, output: ProcessingOutput | None) -> str:
    """The state, and what a completed reading still leaves open."""

    if state != "processed" or output is None or not output.open_questions:
        return STATE_WORDS[state]
    count = output.open_questions
    if count == 1:
        return "Processed — 1 proposed change still needs a decision"
    return f"Processed — {count} proposed changes still need a decision"


def _declared_revision(delivery: SourceDelivery) -> str:
    """Which revision of the source the sender said this is, where they said.

    An upload that declares none is versioned by its own digest, so that the
    same file uploaded twice converges on one delivery rather than becoming
    two. That is an identity, not a revision anybody stated, and printing it
    beside a filename would show a coordinator a 64-character number as though
    it were the customer's own name for a revision.
    """

    version = str(delivery.external_version)
    return "" if version == str(delivery.content_sha256) else version


def _family(document: Document | None, context: _DocumentContext) -> str:
    """The source family a change from this document was recorded under.

    Read from the ``delta_groups`` row that recorded it rather than re-derived,
    so the register and the Record view name one lineage the same way. A source
    that has proposed nothing yet shows its registry id, and one that has
    neither shows nothing rather than a name invented for the column.
    """

    if document is None:
        return ""
    recorded = context.families.get(int(document.id))
    if recorded:
        return recorded
    return str(document.registry_id or "")


def _revision_relationship(
    document: Document | None, context: _DocumentContext
) -> str:
    """Replaces or Replaced by, in the glossary's own words, or nothing.

    Only a recorded Supersession produces one. A filename, an upload time, or
    a shared digest never does (ADR-0015).
    """

    if document is None:
        return ""
    successor = context.replaced_by.get(int(document.id))
    if successor:
        return f"Replaced by {successor}"
    predecessor = context.replaces.get(int(document.id))
    if predecessor:
        return f"Replaces {predecessor}"
    return ""


def _states_present(rows: Sequence[RegisterRow]) -> tuple[str, ...]:
    """Only the states this project's own register actually holds."""

    present = {row.state for row in rows}
    return tuple(state for state in STATE_ORDER if state in present)


def _matches(row: RegisterRow, filters: RegisterFilters) -> bool:
    if filters.state and row.state != filters.state:
        return False
    if filters.family:
        wanted = filters.family.casefold()
        searched = (row.source_family, row.filename, row.external_identity)
        if not any(wanted in (value or "").casefold() for value in searched):
            return False
    at = row.received_at
    if filters.received_from is not None and (at is None or at < filters.received_from):
        return False
    if filters.received_to is not None and (at is None or at > filters.received_to):
        return False
    return True


def _start_index(rows: Sequence[RegisterRow], before: str) -> int:
    """Where the page after ``before`` begins, or the top when it names none."""

    if not before:
        return 0
    for index, row in enumerate(rows):
        if row.key == before:
            return index + 1
    return 0
