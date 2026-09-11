"""What operations may do about a source the pass stopped taking, and its receipt (#842).

The 2026-09-10 customer-journey audit found a source that had failed to process
could become a permanent dead end ("Processing failures currently risk becoming
permanent dead ends"). Two of ``project_processing._eligible_documents``'s four
exclusions are permanent by design and correct: a document whose parse failed is
never re-parsed by the standing pass, because identical bytes make re-ingest a
no-op; and a document whose terminal reading is ``unreadable`` or ``no_matrix``
is never re-read, because an unhandled layout is a standing condition rather
than a transient one. Nothing offered a way out of either. The legacy operations
screens are outside the pilot boundary and stay there, so the coordinator's page
could name the blocked source and its owner (#840, #841) and there was no act
behind that owner at all.

**What this module adds is an act, not an authority.** It re-admits one source
to the ordinary pass under an attributable receipt; the pass then reads it
through every gate it always reads through. So a repair never means bytes were
sent anywhere, and a repair that "succeeded" means only that the ordinary
reading will be attempted again. That is the whole of the mechanical power here,
and it is deliberately small: the audit's requirement was that operations get a
*repeatable, attributable* procedure, and that hidden engineering rescue stop
being free.

**The receipt is the point.** It is one ``audit_log`` entry, written in the same
transaction as the re-parse or the re-admission it authorizes, and two readers
depend on it: ``project_processing`` takes a permanently-excluded source again
when a repair receipt covers the reading that excluded it, and
``source_register`` prints the repair standing on the row a coordinator is
looking at. An engineer who fixes a project by hand through this module leaves
the same visible line an operator does, which is what the audit asked for.

**Three things a repair may not do, and one it cannot.**

- It may not release held bytes. ``intake_hardening.assert_can_process_richly``
  is the existing gate over a quarantined document, and this calls it rather
  than restating it; a malware finding keeps its own owner and its own act, and
  nothing here deletes a ``document_quarantines`` row.
- It may not supply a customer authorization. A source whose retained Processing
  Failures name the provider boundary's ``authorization-absent`` or
  ``authorization-refused`` is not a mechanical failure: reading it again reads
  it the same way and is refused the same way, and the missing thing is #522's
  signed customer decision, which is a person's act outside this system.
- It may not change what a captured Source Fact says. A source that was read is
  refused, because "read it again" is not how a capture that is wrong about its
  source is put right; and structurally this module writes no Source Fact,
  Source Segment, Proposed Delta or record decision, and takes no argument that
  could name a value.

**The third procedure named in #842 is not here.** Operations may also perform
the source-grounded re-capture, and what that re-capture preserves, what it may
not overwrite, and what it returns through is #832's decision, drafted as
ADR-0100 and not accepted. Building it against an unaccepted decision would fix
the shape of a correction request before the maintainer had agreed one, so it
waits. The two procedures below need no decision that is not already in force.

**A corrected-mapping repair names the mapping it reads under.**
``CORRECTED_MAPPING`` says the source is being read again because the
customer's field mapping was corrected, and the receipt records the exact
registration in force -- identity and version -- so a later reader can tell
whether a changed reading followed a changed mapping. It does not adjudicate
whether that registration is *the* correction: ``field_mapping_manifest``'s own
rule is that nothing here concludes a form changed meaning, because that
conclusion is a person's and is recorded as the registration itself. Making
that registration is a separate act with a separate authority: #829's
``format_replacement`` offers and validates one, and PostgreSQL's
``register_baseline_format`` refuses a changed mapping revision unless the
person approving it holds the project-coordination designation (#597).
Operations supplies and re-reads; it does not approve what a customer's column
means. A project with no mapping registered at all has nothing to have
corrected, and the declaration is refused.

**A receipt names the reading it repaired, and is spent by the next one.** It
records the newest Extraction Run this source had when the repair was made, and
both readers compare against *that* rather than against a wall clock: the
standing pass takes the source again only while no newer run exists, so a
source that fails the same way a second time is excluded again instead of being
re-admitted for ever by one old receipt, and the register can say whether the
source has been read since without asking what time it is.

**No clock.** The comparisons above are between two recorded row identities,
and the one time comparison -- a mapping registration against the reading it
must post-date -- is between two of PostgreSQL's own instants. Two readings of
the same records reach the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import access, audit, refusals
from corridor.baseline_adoption import effective_baseline_formats
from corridor.intake_hardening import (
    HostileContentRefused,
    assert_can_process_richly,
)
from corridor.models import (
    AuditLog,
    Document,
    ExtractionRun,
    PageProcessingFailure,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.provider_authorization import (
    AUTHORIZATION_ABSENT,
    AUTHORIZATION_REFUSED,
)


# --- The procedures --------------------------------------------------------
#
# What the operator declares they are doing, recorded on the receipt so a
# reader can tell a bare re-read from one the customer's corrected form is the
# reason for. Neither name is a customer-facing label: both appear only on the
# runbook's own arguments and inside the receipt.

RETRY_PROCESSING = "retry_processing"
CORRECTED_MAPPING = "corrected_mapping"
PROCEDURES: tuple[str, ...] = (RETRY_PROCESSING, CORRECTED_MAPPING)

#: Why the standing pass had stopped taking this source, recorded on the
#: receipt beside the procedure. These are ``_eligible_documents``'s own two
#: permanent exclusions, spelled the way it spells them.
FAILED_PARSE = "failed_parse"
UNREADABLE_PERMANENT = "unreadable_permanent"

#: The extraction outcomes the standing pass treats as permanent. It lives here
#: rather than in ``project_processing`` because the repair and the exclusion
#: have to mean the same thing by construction: a repair offered for an outcome
#: the pass does not exclude would re-admit a source that was never held, and a
#: pass excluding an outcome no repair covers would be the dead end again.
PERMANENT_FAILURE_OUTCOMES: tuple[str, ...] = ("unreadable", "no_matrix")

#: The reasons a provider authorization check refuses, as a routed page's
#: retained Processing Failure records them.
_AUTHORIZATION_REASONS = frozenset({AUTHORIZATION_ABSENT, AUTHORIZATION_REFUSED})


class OperationsRepairRefused(refusals.Refusal, ValueError):
    """This repair is not one operations may perform, and nothing was written.

    ``reason`` is a stable machine code and ``str(exc)`` is the sentence a
    person reads. The codes are ``unknown_document``, ``not_designated``,
    ``held_in_quarantine``, ``authorization_missing``, ``already_read``,
    ``not_blocked``, ``unknown_procedure``, ``no_registered_mapping`` and
    ``parse_not_recoverable``.
    """

    refusal_kind = refusals.CONFLICT

    def __init__(
        self, reason: str, message: str, *, kind: str = refusals.CONFLICT
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.refusal_kind = kind


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """What one repair did, in the words its own receipt carries."""

    document_id: int
    procedure: str
    blocked_by: str
    outcome: str
    detail: str
    read_through_run_id: int | None
    under_mapping: str
    audit_id: int


@dataclass(frozen=True, slots=True)
class RepairReceipt:
    """The newest recorded repair of one source, as a reader gets it back."""

    document_id: int
    procedure: str
    blocked_by: str
    outcome: str
    detail: str
    read_through_run_id: int | None
    under_mapping: str
    performed_by: str
    performed_at: datetime | None


def repair_source_processing(
    session: Session,
    *,
    document_id: int,
    principal: HumanPrincipal,
    procedure: str = RETRY_PROCESSING,
    images_dir: str | None = None,
) -> RepairOutcome:
    """Re-admit one blocked source to the standing pass, attributably.

    Runs in the caller's transaction, so a rolled-back caller leaves no receipt
    and no claim that a repair happened. Every refusal below is raised before
    anything is written.
    """

    actor = require_human_principal(principal)
    if procedure not in PROCEDURES:
        raise OperationsRepairRefused(
            "unknown_procedure",
            f"{procedure!r} is not a repair operations performs. The two are "
            f"{RETRY_PROCESSING!r} and {CORRECTED_MAPPING!r}.",
            kind=refusals.MALFORMED_INPUT,
        )
    document = session.get(Document, document_id)
    if document is None:
        raise OperationsRepairRefused(
            "unknown_document",
            f"document {document_id} is not a registered source.",
            kind=refusals.NOT_OFFERED,
        )
    _require_technical_operations(session, actor, int(document.project_id))
    _refuse_held_bytes(session, int(document.id))
    _refuse_missing_customer_authorization(session, int(document.id))
    blocked_by = _blocked_by(session, document)
    under_mapping = (
        _registered_mapping(session, document)
        if procedure == CORRECTED_MAPPING
        else ""
    )
    read_through = session.scalar(
        select(func.max(ExtractionRun.id)).where(
            ExtractionRun.document_id == int(document.id)
        )
    )

    if blocked_by == FAILED_PARSE:
        # The bounded re-parse #350 already built, which is the only thing that
        # can move a failed parse: ordinary re-ingest of identical bytes is a
        # no-op. It writes its own RECOVER_DOCUMENT_PARSE receipt, so this act
        # leaves two entries -- what operations asked for, and what the parse
        # did -- rather than one that has to mean both. Imported here because
        # that module reaches the ingest engines and this one does not.
        from corridor.location_discovery import recover_document_parse

        recovery = recover_document_parse(
            session,
            document_id=int(document.id),
            principal=actor,
            images_dir=images_dir,
        )
        if recovery.outcome not in ("recovered", "still_failed"):
            # The re-parse refused rather than ran -- a sealed holdout is the
            # one way that happens from here. Recording a repair receipt for a
            # refusal would say operations acted when it did not, and would
            # re-admit a source nothing re-read.
            raise OperationsRepairRefused(
                "parse_not_recoverable",
                recovery.reason
                or f"the bounded re-parse answered {recovery.outcome!r}.",
                kind=refusals.NOT_OFFERED,
            )
        outcome, detail = recovery.outcome, (recovery.reason or "")
    else:
        outcome, detail = "readmitted", (
            "the next standing pass reads this source again; it was excluded "
            "as permanently unreadable until this repair"
        )

    entry = audit.record(
        session,
        principal=actor,
        action=audit.REPAIR_SOURCE_PROCESSING,
        entity_type=audit.DOCUMENT,
        entity_id=int(document.id),
        after={
            "procedure": procedure,
            "blocked_by": blocked_by,
            "outcome": outcome,
            "detail": detail,
            "sha256": document.sha256,
            # Which reading this repair is of. Recorded rather than compared
            # against a clock: the receipt and the run it repairs can share one
            # transaction's ``now()``, and "the newest run when I acted" is the
            # fact both readers actually want.
            "read_through_run_id": None if read_through is None else int(read_through),
            # Which mapping revision the re-read happens under, where the
            # operator declared one is the reason. Named rather than judged:
            # deciding that a form changed meaning is the registration, and it
            # was made by somebody holding a designation this act does not.
            "under_mapping": under_mapping,
        },
    )
    return RepairOutcome(
        document_id=int(document.id),
        procedure=procedure,
        blocked_by=blocked_by,
        outcome=outcome,
        detail=detail,
        read_through_run_id=None if read_through is None else int(read_through),
        under_mapping=under_mapping,
        audit_id=int(entry.id),
    )


def repair_receipts(
    session: Session, *, document_ids: Sequence[int]
) -> dict[int, RepairReceipt]:
    """The newest recorded repair of each of these sources, where there is one.

    One grouped statement rather than one per source: the register asks this
    for a whole page of deliveries at once.
    """

    ids = [int(value) for value in dict.fromkeys(document_ids)]
    if not ids:
        return {}
    newest = session.execute(
        select(AuditLog.entity_id, func.max(AuditLog.id))
        .where(
            AuditLog.action == audit.REPAIR_SOURCE_PROCESSING,
            AuditLog.entity_type == audit.DOCUMENT,
            AuditLog.entity_id.in_(ids),
        )
        .group_by(AuditLog.entity_id)
    ).all()
    if not newest:
        return {}
    entries = session.scalars(
        select(AuditLog).where(
            AuditLog.id.in_([int(entry_id) for _, entry_id in newest])
        )
    ).all()
    return {
        int(entry.entity_id): RepairReceipt(
            document_id=int(entry.entity_id),
            procedure=str((entry.after_json or {}).get("procedure") or ""),
            blocked_by=str((entry.after_json or {}).get("blocked_by") or ""),
            outcome=str((entry.after_json or {}).get("outcome") or ""),
            detail=str((entry.after_json or {}).get("detail") or ""),
            read_through_run_id=_run_id(entry),
            under_mapping=str((entry.after_json or {}).get("under_mapping") or ""),
            performed_by=str(entry.human_principal or entry.actor),
            performed_at=entry.ts,
        )
        for entry in entries
    }


def repaired_through(
    session: Session, *, document_ids: Sequence[int]
) -> dict[int, int]:
    """The newest reading each of these sources has been repaired through.

    Separate from ``repair_receipts`` because the standing pass needs only this
    one number, and because the answer is the highest run id any of a source's
    receipts covers rather than the newest receipt's: two repairs of the same
    source do not undo each other.
    """

    ids = [int(value) for value in dict.fromkeys(document_ids)]
    if not ids:
        return {}
    covered: dict[int, int] = {}
    for entry in session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.REPAIR_SOURCE_PROCESSING,
            AuditLog.entity_type == audit.DOCUMENT,
            AuditLog.entity_id.in_(ids),
        )
    ).all():
        run_id = _run_id(entry)
        if run_id is None:
            continue
        document_id = int(entry.entity_id)
        covered[document_id] = max(covered.get(document_id, 0), run_id)
    return covered


def _run_id(entry: AuditLog) -> int | None:
    """The reading one receipt was taken through, or nothing when it named none."""

    value = (entry.after_json or {}).get("read_through_run_id")
    return int(value) if isinstance(value, int) else None


# --- What the repair refuses ------------------------------------------------


def _require_technical_operations(
    session: Session, actor: HumanPrincipal, project_id: int
) -> None:
    """The technical-operations designation, read live from the roster.

    ``access.resolve_membership`` is the one reader every project surface uses,
    so a withdrawn designation is felt here at once and this is not a second
    opinion about who holds one.
    """

    membership = access.resolve_membership(session, actor.subject, project_id)
    if membership is None or not membership.has(access.TECHNICAL_OPERATIONS):
        raise OperationsRepairRefused(
            "not_designated",
            "repairing a source is a technical-operations act, and this "
            "principal does not hold that designation on this project.",
            kind=refusals.NOT_AUTHORIZED,
        )


def _refuse_held_bytes(session: Session, document_id: int) -> None:
    """A document held out of processing keeps its own owner and its own act.

    The gate is ``intake_hardening``'s, called rather than restated, and its
    sentence is the one a person reads. Nothing here removes the quarantine: a
    generic release control over every hold is exactly what #842 refuses, and a
    malware finding is not a mechanical failure to retry.
    """

    try:
        assert_can_process_richly(session, document_id)
    except HostileContentRefused as exc:
        raise OperationsRepairRefused(
            "held_in_quarantine",
            f"this source is held and is not processed: {exc.reason} A repair "
            "does not release held bytes, and nothing here lifts a hold.",
        ) from exc


def _refuse_missing_customer_authorization(
    session: Session, document_id: int
) -> None:
    """A reading refused for want of an authorization is not repaired by re-reading.

    The retained Processing Failure records the boundary's own reason, so this
    asks the record rather than re-deriving whether a project is authorized.
    """

    reason = session.scalar(
        select(PageProcessingFailure.error_type)
        .where(
            PageProcessingFailure.document_id == document_id,
            PageProcessingFailure.error_type.in_(sorted(_AUTHORIZATION_REASONS)),
        )
        .order_by(PageProcessingFailure.id.desc())
        .limit(1)
    )
    if reason is None:
        return
    raise OperationsRepairRefused(
        "authorization_missing",
        f"this source was not read because the processing it needs was "
        f"refused: {reason}. Reading it again reads it the same way and is "
        "refused the same way. What is missing is the customer's own signed "
        "authorization for that processing, which operations cannot supply "
        "here.",
        kind=refusals.NOT_AUTHORIZED,
    )


def _blocked_by(session: Session, document: Document) -> str:
    """Which permanent exclusion holds this source, or a refusal saying none does.

    The two are ``project_processing``'s own, asked in its order: the parse
    first, because a document that never parsed has no reading to be permanent
    about.
    """

    if document.parse_status == "failed":
        return FAILED_PARSE
    if document.parse_status != "parsed":
        raise OperationsRepairRefused(
            "not_blocked",
            f"this source's parse is {document.parse_status!r}, which the "
            "standing pass reaches on its own. There is nothing to re-admit.",
            kind=refusals.NOT_OFFERED,
        )
    outcomes = set(
        session.scalars(
            select(ExtractionRun.outcome).where(
                ExtractionRun.document_id == int(document.id)
            )
        ).all()
    )
    if "completed" in outcomes:
        raise OperationsRepairRefused(
            "already_read",
            "this source was read. Reading it again is not how a capture that "
            "is wrong about its source is put right, and nothing here changes "
            "what a captured fact says.",
            kind=refusals.NOT_OFFERED,
        )
    if outcomes & set(PERMANENT_FAILURE_OUTCOMES):
        return UNREADABLE_PERMANENT
    raise OperationsRepairRefused(
        "not_blocked",
        "the standing pass takes this source again on its own, so there is "
        "nothing to re-admit.",
        kind=refusals.NOT_OFFERED,
    )


def _registered_mapping(session: Session, document: Document) -> str:
    """The mapping revision in force, named, or a refusal when there is none.

    A project that has registered no field mapping has nothing to have
    corrected, so the declaration is refused rather than recorded against
    nothing. Where there is one, its identity and version go on the receipt:
    that is what makes "it was read again under the corrected mapping" a
    checkable statement rather than an operator's word for it.
    """

    registered = effective_baseline_formats(
        session, int(document.project_id)
    ).get("field_mapping")
    if registered is None:
        raise OperationsRepairRefused(
            "no_registered_mapping",
            "this project has registered no field mapping, so there is no "
            "corrected mapping for this source to be read under. Registering "
            "one is the project-coordination designation's act, not this one.",
            kind=refusals.NOT_OFFERED,
        )
    return f"{registered.format_identity} {registered.format_version}"


__all__ = [
    "CORRECTED_MAPPING",
    "FAILED_PARSE",
    "PERMANENT_FAILURE_OUTCOMES",
    "PROCEDURES",
    "RETRY_PROCESSING",
    "UNREADABLE_PERMANENT",
    "OperationsRepairRefused",
    "RepairOutcome",
    "RepairReceipt",
    "repair_receipts",
    "repair_source_processing",
    "repaired_through",
]
