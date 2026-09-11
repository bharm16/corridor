"""The one stage-aware answer to "may this be done with this document?" (#919).

A hold used to be one free-text row on ``document_quarantines`` that said
nothing machine-readable about what it forbade, and the single gate over it
refused *every* kind of rich processing for *any* row. Meanwhile every hold a
production path wrote was an extraction restriction on a document that had
already been read. Applying the gate would have stopped reading uploads on a
rule no writer ever stated; not applying it left a genuine restriction
unenforced. Both were guesses, so #919 asked the maintainer to decide, and this
module is that decision.

**Two processing boundaries, and nothing in between.**

``DOCUMENT_READING`` prohibited
    No ordinary rich parsing, rendering, OCR or downstream semantic
    extraction. The delivery and any permitted safety or operations evidence
    are retained; the bytes are not opened for ordinary work.

``SEMANTIC_EXTRACTION`` prohibited
    Authorized, bounded document reading and Source Segment creation may
    proceed. Semantic interpretation and downstream proposal generation remain
    blocked. A schedule workbook can therefore be read into cells while the
    scheduling relationships it asserts stay unmodeled -- which is a statement
    about what Corridor may do with it, not a claim that the schedule has
    become semantically supported.

**The effective permission is an intersection, never the newest row.** Several
independent restrictions may stand on one document at once, each its own row.
Reading is permitted only when no open hold prohibits reading; extraction only
when no open hold prohibits either. So an unsupported-semantics hold cleared
beside a security restriction leaves reading prohibited, and imposing a new
restriction can never relax an existing one -- there is no row to overwrite.
PostgreSQL holds the same rule: a recorded hold is append-only and its one
permitted change is the attributable release below.

**This is not the only requirement an operation must meet.** #827's onboarding
permission, the customer-data and provider gates, and the processing-scope
gates are independent of this module and are all still asked. An operation
needs a valid permission *and* no applicable prohibiting hold; neither
mechanism replaces the other. In particular, permission to read a document
locally does not authorize an outbound OCR call or a model call -- ``ingest``
describes that separate outbound authorization boundary, and nothing here
touches it.

**Who may impose what.** The authority is recorded on the row and checked when
it is written, because "which pipeline decided this" is exactly what the
predecessor could not say:

``INTAKE_SECURITY``
    The intake and security pipeline, from its own declared checks. It may
    prohibit document reading.
``PROCESSING_RULE``
    The processing pipeline, where a known rule establishes that the source
    cannot be interpreted under the current contract. It may prohibit semantic
    extraction.
``TECHNICAL_OPERATIONS``
    A named operator holding the technical-operations designation. It may
    impose either boundary, and it is the only authority that may classify an
    unclassified historical hold or release one. That is not discretion to
    overrule a malware finding, a missing customer authorization or unsupported
    source semantics: a release must cite the evidence or configuration change
    that actually removes the restriction's cause, and the cause is not removed
    by wanting the document read.
``MIGRATION``
    Recorded by the transition that classified the rows that already existed.
    It is refused at runtime: a hold this migration wrote is lifted by an
    attributable act, never by another migration.

The coordinator has no authority here. Supplying a missing declaration or a
correction request can satisfy a *specific recorded precondition*, at which
point the rule that imposed the hold no longer applies and an operator records
the release citing that input -- which is a different thing from a general
permission to release a quarantine.

**Unclassified is restrictive, and says so.** A hold whose origin and scope the
transition could not establish from retained rows prohibits document reading
under ``UNCLASSIFIED_HISTORICAL_HOLD``. That is a conservative default while
nobody has classified it, and it is deliberately not a finding that the file is
dangerous; every surface that prints it says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import access, audit
from corridor.models import Document, DocumentQuarantine
from corridor.principals import HumanPrincipal, require_human_principal


# --- The two processing boundaries -----------------------------------------

DOCUMENT_READING = "document_reading"
SEMANTIC_EXTRACTION = "semantic_extraction"
PROHIBITED_STAGES = (DOCUMENT_READING, SEMANTIC_EXTRACTION)

# What each boundary is called where a person reads it. The maintainer's own
# words; nothing here mints a customer label of its own.
STAGE_WORDS = {
    DOCUMENT_READING: "document reading is not permitted",
    SEMANTIC_EXTRACTION: "semantic extraction is not permitted",
}


# --- Who may impose one ----------------------------------------------------

INTAKE_SECURITY = "intake_security"
PROCESSING_RULE = "processing_rule"
TECHNICAL_OPERATIONS = "technical_operations"
MIGRATION = "migration"

# The stages each authority may impose. `MIGRATION` is absent on purpose: it is
# a value the classification transition recorded, not one a caller may claim.
IMPOSABLE_STAGES = {
    INTAKE_SECURITY: frozenset({DOCUMENT_READING}),
    PROCESSING_RULE: frozenset({SEMANTIC_EXTRACTION}),
    TECHNICAL_OPERATIONS: frozenset(PROHIBITED_STAGES),
}


# --- The reason codes this repository writes -------------------------------
#
# Machine-readable, and never derived by reading the explanatory sentence. A
# new one is a new line here, so the set of reasons a hold can carry is
# readable in one place rather than spread across its writers.

UNMODELED_SEQUENCING_SEMANTICS = "unmodeled_sequencing_semantics"
UNDECLARED_SOURCE_REVISION = "undeclared_source_revision"
EXTRACTION_REFUSED_BY_RULE = "extraction_refused_by_rule"
INTAKE_SECURITY_FINDING = "intake_security_finding"
UNCLASSIFIED_HISTORICAL_HOLD = "unclassified_historical_hold"


class ProcessingHoldInForce(ValueError):
    """A recorded hold prohibits the stage this operation needed.

    Its own class rather than ``HostileContentRefused``: most holds are not
    security findings, and a refusal that says hostile content was refused is
    the exact mislabel #919 was opened about. ``holds`` carries the rows that
    answered, so a caller reports what the record says instead of composing a
    sentence of its own.
    """

    def __init__(self, stage: str, holds: tuple[DocumentQuarantine, ...]) -> None:
        self.stage = stage
        self.holds = holds
        super().__init__(hold_sentence(stage, holds))


class UnknownDocument(ValueError):
    """The gate was asked about a document this database does not hold."""


class ProcessingHoldRefused(ValueError):
    """The caller may not impose, classify or release this restriction."""


@dataclass(frozen=True, slots=True)
class ProcessingPermission:
    """What may be done with one document, from every open hold at once.

    The two answers are an intersection and not a lookup of the newest row: a
    document is readable only when nothing prohibits reading, and extractable
    only when nothing prohibits either stage. A reading prohibition therefore
    implies an extraction prohibition without a second row having to say so.
    """

    document_id: int
    reading_holds: tuple[DocumentQuarantine, ...]
    extraction_holds: tuple[DocumentQuarantine, ...]

    @property
    def may_read_document(self) -> bool:
        return not self.reading_holds

    @property
    def may_extract_semantics(self) -> bool:
        return not self.reading_holds and not self.extraction_holds

    @property
    def holds(self) -> tuple[DocumentQuarantine, ...]:
        return self.reading_holds + self.extraction_holds

    @property
    def recorded_reason(self) -> str:
        """Every open restriction's own recorded words, as the record wrote them."""

        return " ".join(hold.reason.strip() for hold in self.holds if hold.reason)


def hold_line(hold: DocumentQuarantine) -> str:
    """One standing restriction as one sentence, for a person to read.

    The stage comes from this module's own words and the rest is the recorded
    reason exactly as its writer wrote it. A screen never composes this itself,
    because a sentence minted beside the state it describes is how the register
    ended up asserting a cause that was already untrue of half the holds it
    printed (#919).
    """

    return f"{STAGE_WORDS[hold.prohibited_stage]}. {hold.reason.strip()}"


def hold_sentence(stage: str, holds: tuple[DocumentQuarantine, ...]) -> str:
    """Why the stage is refused, in the recorded reasons rather than a new claim."""

    reasons = "; ".join(
        f"{hold.reason_code}: {hold.reason.strip()}" for hold in holds
    )
    return f"{STAGE_WORDS[stage]} for document {holds[0].document_id} ({reasons})"


# --- Reading the record ----------------------------------------------------


def open_holds(
    session: Session, document_id: int
) -> tuple[DocumentQuarantine, ...]:
    """Every restriction standing on this document, oldest first."""

    return tuple(
        session.scalars(
            select(DocumentQuarantine)
            .where(
                DocumentQuarantine.document_id == document_id,
                DocumentQuarantine.released_at.is_(None),
            )
            .order_by(DocumentQuarantine.id)
        ).all()
    )


def open_holds_for_project(
    session: Session, project_id: int
) -> dict[int, tuple[DocumentQuarantine, ...]]:
    """Every open restriction in one project, grouped by document.

    One statement, because the readers that need this -- the source register,
    the operations view, the processing pass's own scope act -- ask it for a
    whole project and must not grow a query per row.
    """

    grouped: dict[int, list[DocumentQuarantine]] = {}
    for hold in session.scalars(
        select(DocumentQuarantine)
        .join(Document, Document.id == DocumentQuarantine.document_id)
        .where(
            Document.project_id == project_id,
            DocumentQuarantine.released_at.is_(None),
        )
        .order_by(DocumentQuarantine.document_id, DocumentQuarantine.id)
    ).all():
        grouped.setdefault(int(hold.document_id), []).append(hold)
    return {
        document_id: tuple(holds) for document_id, holds in grouped.items()
    }


def permission(session: Session, document_id: int) -> ProcessingPermission:
    """The effective permission for one document, from its open holds."""

    return permission_from(document_id, open_holds(session, document_id))


def permission_from(
    document_id: int, holds: tuple[DocumentQuarantine, ...]
) -> ProcessingPermission:
    """The same intersection, over holds a caller has already read.

    A reader that has taken one project's holds in a single statement answers
    from those rows rather than asking the database again per document. It is
    the same rule either way, which is the point of it living here.
    """

    return ProcessingPermission(
        document_id=int(document_id),
        reading_holds=tuple(
            hold for hold in holds if hold.prohibited_stage == DOCUMENT_READING
        ),
        extraction_holds=tuple(
            hold for hold in holds if hold.prohibited_stage == SEMANTIC_EXTRACTION
        ),
    )


# --- The gate every entry point asks ---------------------------------------


def assert_may_read_document(session: Session, document_id: int) -> None:
    """Refuse before a rich parser opens these bytes.

    Asked by the standing read act, by direct parsing ingest, by the bounded
    re-parse an operator runs, and by the onboarding compatibility read. A
    document this database does not hold is refused too: the gate answers about
    a row, and no row is not an answer.
    """

    if session.get(Document, document_id) is None:
        raise UnknownDocument(f"document {document_id} does not exist")
    holds = open_holds(session, document_id)
    reading = tuple(
        hold for hold in holds if hold.prohibited_stage == DOCUMENT_READING
    )
    if reading:
        raise ProcessingHoldInForce(DOCUMENT_READING, reading)


def assert_may_extract_semantics(session: Session, document_id: int) -> None:
    """Refuse before a semantic reader or a proposal writer runs.

    Both boundaries answer here: a document nobody may read is a document
    nobody may extract from, so the reading holds are reported first and the
    extraction holds only when reading itself is permitted.
    """

    if session.get(Document, document_id) is None:
        raise UnknownDocument(f"document {document_id} does not exist")
    standing = permission(session, document_id)
    if standing.reading_holds:
        raise ProcessingHoldInForce(DOCUMENT_READING, standing.reading_holds)
    if standing.extraction_holds:
        raise ProcessingHoldInForce(SEMANTIC_EXTRACTION, standing.extraction_holds)


# --- Writing the record ----------------------------------------------------


def impose_hold(
    session: Session,
    *,
    document_id: int,
    prohibited_stage: str,
    reason_code: str,
    reason: str,
    authority: str,
    imposed_by: str,
    evidence: str,
) -> DocumentQuarantine:
    """Record one restriction, without touching any other.

    Idempotent per ``(document_id, reason_code)``: the same rule reaching the
    same document twice returns the standing row rather than writing a second,
    and the database's partial unique index says the same thing. A restriction
    already recorded is never rewritten -- a rule whose sentence has changed
    records a release and a new hold, so the reason a source was held stays
    legible.
    """

    if prohibited_stage not in PROHIBITED_STAGES:
        raise ValueError(f"unknown processing stage {prohibited_stage!r}")
    permitted = IMPOSABLE_STAGES.get(authority)
    if permitted is None:
        raise ValueError(f"{authority!r} is not an authority that imposes a hold")
    if prohibited_stage not in permitted:
        raise ValueError(
            f"{authority} may not prohibit {prohibited_stage}: it may prohibit "
            f"{', '.join(sorted(permitted))}"
        )
    for text in (reason_code, reason, imposed_by, evidence):
        if not str(text).strip():
            raise ValueError(
                "a hold records its reason code, its words, the rule or actor "
                "that imposed it, and the evidence behind it"
            )

    standing = session.scalars(
        select(DocumentQuarantine).where(
            DocumentQuarantine.document_id == document_id,
            DocumentQuarantine.reason_code == reason_code,
            DocumentQuarantine.released_at.is_(None),
        )
    ).first()
    if standing is not None:
        return standing

    hold = DocumentQuarantine(
        document_id=document_id,
        prohibited_stage=prohibited_stage,
        reason_code=reason_code,
        reason=reason,
        imposed_by_authority=authority,
        imposed_by=imposed_by,
        evidence=evidence,
    )
    session.add(hold)
    session.flush()
    return hold


def release_hold(
    session: Session,
    *,
    document_id: int,
    reason_code: str,
    principal: HumanPrincipal,
    evidence: str,
    at: datetime,
) -> DocumentQuarantine:
    """Remove one restriction, citing what removed its cause.

    Only the named restriction. Every other hold on the document stands
    untouched, which is the whole mechanism behind "a mapping repair must not
    accidentally clear a safety restriction": there is no statement here that
    could reach a second row.

    The evidence is required and is not a formality: it is the configuration
    change, the supplied declaration, or the re-scan that actually removes the
    cause. Nothing in this module decides that it does -- an operator records
    what they relied on, attributably, and a later reader can check it.
    """

    actor = require_human_principal(principal)
    if not str(evidence).strip():
        raise ValueError(
            "releasing a restriction cites the evidence or configuration "
            "change that removes its cause"
        )
    hold = session.scalars(
        select(DocumentQuarantine).where(
            DocumentQuarantine.document_id == document_id,
            DocumentQuarantine.reason_code == reason_code,
            DocumentQuarantine.released_at.is_(None),
        )
    ).first()
    if hold is None:
        raise ValueError(
            f"no open {reason_code!r} restriction stands on document {document_id}"
        )
    document = session.get(Document, document_id)
    if document is None:
        raise UnknownDocument(f"document {document_id} does not exist")
    _require_technical_operations(session, actor, int(document.project_id))

    hold.released_at = at
    hold.released_by_authority = TECHNICAL_OPERATIONS
    hold.released_by = actor.subject
    hold.release_evidence = evidence
    session.flush()
    audit.record(
        session,
        principal=actor,
        action=audit.RELEASE_PROCESSING_HOLD,
        entity_type=audit.DOCUMENT,
        entity_id=int(document_id),
        after={
            "hold_id": int(hold.id),
            "reason_code": reason_code,
            "prohibited_stage": hold.prohibited_stage,
            "release_evidence": evidence,
        },
    )
    return hold


def classify_hold(
    session: Session,
    *,
    document_id: int,
    prohibited_stage: str | None,
    reason_code: str,
    reason: str,
    principal: HumanPrincipal,
    evidence: str,
    at: datetime,
) -> DocumentQuarantine | None:
    """Say what an unclassified historical hold actually prohibited.

    The transition that typed this relation could not establish the origin of
    every row it found, so those rows prohibit document reading until somebody
    says otherwise. This is that act, and it is two attributable records rather
    than an edit: the unclassified hold is released citing the evidence, and the
    restriction the operator established -- if any -- is imposed as its own row
    with its own reason. Nothing is overwritten, so what the record said before
    the classification stays readable afterwards.

    ``prohibited_stage`` of ``None`` means the operator established that no
    restriction applies at all, and the returned value is ``None``.
    """

    actor = require_human_principal(principal)
    release_hold(
        session,
        document_id=document_id,
        reason_code=UNCLASSIFIED_HISTORICAL_HOLD,
        principal=actor,
        evidence=evidence,
        at=at,
    )
    if prohibited_stage is None:
        return None
    return impose_hold(
        session,
        document_id=document_id,
        prohibited_stage=prohibited_stage,
        reason_code=reason_code,
        reason=reason,
        authority=TECHNICAL_OPERATIONS,
        imposed_by=actor.subject,
        evidence=evidence,
    )


def _require_technical_operations(
    session: Session, actor: HumanPrincipal, project_id: int
) -> None:
    """The designation, read live from the roster.

    ``access.resolve_membership`` is the one reader every project surface uses,
    so a withdrawn designation is felt here at once and this is not a second
    opinion about who holds one. The same rule ``operations_repair`` applies to
    the repair procedure applies to the hold it is forbidden to lift.
    """

    membership = access.resolve_membership(session, actor.subject, project_id)
    if membership is None or not membership.has(access.TECHNICAL_OPERATIONS):
        raise ProcessingHoldRefused(
            "classifying or releasing a processing restriction is a "
            "technical-operations act, and this principal does not hold that "
            "designation on this project."
        )
