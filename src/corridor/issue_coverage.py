"""The coverage reading Corridor derives, and the one a person confirms (#675).

ADR-0086 makes "one declared coverage state" a shared input of every artifact
in an issue, and #529 took it as an in-memory ``CoverageDeclaration`` its caller
supplied. Any caller could therefore prepare an issue under a coverage state
nobody had confirmed, and the Issue section (#536) had no way to produce one at
all — which is why a project whose candidate had gone stale saw a section that
said a fresh candidate was needed and offered nothing that could make one.

**The machine owns the facts and the person owns the declaration.**
``derive_coverage_reading`` answers what was and was not read for this issue
from the effective issue profile, the persisted Source Delivery ledger, the
confirmations that admitted those deliveries to processing, the processing
receipts on the documents they produced, and the declared cutoff. The
coordinator does not recreate any of it. What they may do is
confirm that exact reading, attach a bounded annotation to a line, or record
an intentional exclusion with a reason where the project's configuration
permits one. What they may never do is relabel a failed, quarantined,
unprocessed or post-cutoff source as read: ``confirm_coverage`` refuses every
one of those, because a coverage state a person could edit after the fact would
let a package claim a review nobody performed.

**The boundary is an append-only watermark, never a clock comparison.** #641
closed without ADR-0085's "source outside the current issue cutoff" limb and
recorded exactly why: a Proposed Delta has no trustworthy source-arrival
instant, and ``created_at`` is PostgreSQL-assigned. A *delivery* has one, and
it is a row in an append-only ledger, so this module freezes
``through_source_delivery_id`` — the highest ``source_deliveries`` row this
issue includes — beside the human-readable cutoff. Membership is then an
identity comparison against that watermark. The watermark is the last delivery
before the first one that arrived after the cutoff, so the included set is a
genuine prefix of the ledger and not a set that a concurrent insert could make
non-contiguous. ``None`` is the honest watermark of a project that has taken no
delivery at all, and is never read as "everything".

**Exclusion is bounded by the project's own configuration.** A source the
profile requires — a project that configured ADR-0086's required-coverage
evaluator requires every source — may not be excluded, because ADR-0086 makes
unmet required coverage a thing that *blocks* an issue, and the way past a
blocked issue is fixing the source, never declaring it away. A source the
profile does not require may be excluded with a reason. That rule is derived
from #640's configuration rather than invented here, so no new customer-facing
term is coined and `docs/agents/domain.md`'s terminology-research procedure is
not triggered.

**Reuse is exact.** ``reusable_declaration`` returns a confirmed declaration
only while the issue cutoff, the issue profile and its coverage requirements,
the exact Source Delivery boundary, and the derived lines and digest are all
unchanged. The reading deliberately does **not** bind the accepted Project
Record revision, so resolving a Proposed Delta does not force a coordinator to
confirm the same coverage again, while a newly received or newly failed source
changes the digest and does.

**Who may confirm is PostgreSQL's answer, not this module's** (#839). The
maintainer settled it on 2026-09-10: Project Coordination may confirm coverage
and request preparation, External Release may authorize, and read-only
membership confers neither. Until then any project member could confirm the
coverage an issue was prepared under, because nothing in the write path asked.
The rule now lives on the relation, as ``enforce_coordination_designation``:
the roster entry and its ``can_coordinate`` flag are re-read as the schema's
own owner when the row is appended, so a second caller cannot forget the check
and the principal string a caller passed proves nothing. What this module does
is name the act that was refused — a coverage confirmation, which leads
somewhere different from a refused release — so a screen can say what happened
instead of printing a database error.

**No clock.** ``cutoff`` and ``confirmed_at`` are supplied by the caller.
Nothing here reads the wall clock, so a reading of a past cutoff is the same
reading tomorrow.

Terminology: nothing here coins a customer word. The four line states are
``issue_rendering``'s own ``read``, ``failed``, ``excluded`` and ``late``, and
the section's one action label is the maintainer's own wording.
"""

from __future__ import annotations

from corridor import refusals
from corridor.analytics import EventFamily
from corridor.measurement_collection import emit_preparation_interaction

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor import access
from corridor.issue_content import COVERAGE_ALL_REQUIRED_SOURCES_READ
from corridor.issue_profile import IssueInventory
from corridor.issue_rendering import (
    COVERAGE_EXCLUDED,
    COVERAGE_FAILED,
    COVERAGE_LATE,
    COVERAGE_READ,
    OPTIONAL,
    REQUIRED,
    SourceCoverage,
)
from corridor.models import (
    Document,
    IssueCoverageDeclaration,
    SourceDelivery,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_delivery import (
    DISPOSITION_DUPLICATE,
    DISPOSITION_QUARANTINED,
    DISPOSITION_STORED,
    DISPOSITION_TERMINALLY_REFUSED,
    DISPOSITION_TRANSIENT_FAILURE,
    confirmed_delivery_ids,
)


# The shape of each digested declaration. A later schema binds different
# things, so the version travels inside the bytes rather than beside them.
READING_SCHEMA_VERSION = "issue-coverage-reading-v1"
DECLARATION_SCHEMA_VERSION = "issue-coverage-declaration-v1"

# How long one human addition may be. A bounded annotation is a sentence a
# coordinator writes beside a line; an unbounded one is a second narrative
# competing with the artifacts the issue actually contains.
ANNOTATION_LIMIT = 500
REASON_LIMIT = 500

# How far a delivery's own recorded refusal is carried into the line a person
# reads. The evidence stays whole on the ledger row; the line quotes it.
DETAIL_LIMIT = 240


class CoverageRefused(refusals.Refusal, ValueError):
    """A coordinator may not declare this coverage.

    The kind is per-instance, because two different rules refuse here and an
    adapter acts on the difference: a reading that moved, a line that may not
    be relabelled, and a cutoff in the future are all conflicts over the same
    reading, while "you hold no project-coordination designation" is not a
    conflict at all (#839). The route picks its status from the kind rather
    than from reading the sentence, which is the second gate #794 card 22
    removed from every catch site.
    """

    def __init__(self, sentence: str, *, kind: str = refusals.CONFLICT) -> None:
        super().__init__(sentence)
        self.refusal_kind = kind


@dataclass(frozen=True, slots=True)
class CoverageLine:
    """One source, and what Corridor's own records say became of it.

    ``source_key`` is the stable identity a human addition names, so an
    annotation or an exclusion is bound to the line it was written against and
    cannot slide onto another when the reading is taken again.
    """

    source_key: str
    source_name: str
    requirement: str
    state: str
    detail: str
    delivery_id: int | None = None
    document_id: int | None = None

    @property
    def is_exception(self) -> bool:
        return self.state != COVERAGE_READ

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "source_name": self.source_name,
            "requirement": self.requirement,
            "state": self.state,
            "detail": self.detail,
            "delivery_id": self.delivery_id,
            "document_id": self.document_id,
        }

    def as_source_coverage(self) -> SourceCoverage:
        """The same line in the shape every artifact already renders."""

        return SourceCoverage(
            source_name=self.source_name,
            requirement=self.requirement,
            state=self.state,
            detail=self.detail,
        )


@dataclass(frozen=True, slots=True)
class CoverageAnnotation:
    """One bounded note a coordinator attaches to a line they confirm."""

    source_key: str
    note: str


@dataclass(frozen=True, slots=True)
class CoverageExclusion:
    """One source a coordinator intentionally leaves out, and why."""

    source_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class DerivedCoverageReading:
    """What Corridor derived about one project's sources at one cutoff.

    Nothing on it came from a person. ``reading_digest`` is the SHA-256 of the
    canonical bytes below, and it is the digest the Issue section shows, the
    POST revalidates, and the declaration records — so a confirmation of one
    reading can never be recorded as a confirmation of another.
    """

    project_id: int
    cutoff: datetime
    through_source_delivery_id: int | None
    profile_id: int
    profile_identity: str
    profile_version: int
    profile_sha256: str
    requires_every_source_read: bool
    lines: tuple[CoverageLine, ...]

    @property
    def exceptions(self) -> tuple[CoverageLine, ...]:
        return tuple(line for line in self.lines if line.is_exception)

    @property
    def excludable_source_keys(self) -> frozenset[str]:
        """Which lines this project's configuration permits excluding.

        ADR-0086 makes unmet *required* coverage a blocker, and the way past a
        blocker is a source that gets read, never a declaration that says it
        did not matter. So a project that configured the required-coverage
        evaluator can exclude nothing, and one that did not can exclude any
        line. There is no third rule and no per-source switch: a per-source
        exclusion policy would be configuration nobody has modelled, and
        guessing one is how a screen starts asserting a customer rule.
        """

        if self.requires_every_source_read:
            return frozenset()
        return frozenset(line.source_key for line in self.lines)

    def line(self, source_key: str) -> CoverageLine | None:
        for one in self.lines:
            if one.source_key == source_key:
                return one
        return None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": READING_SCHEMA_VERSION,
            "project_id": self.project_id,
            "cutoff_at": self.cutoff.isoformat(),
            "through_source_delivery_id": self.through_source_delivery_id,
            "issue_profile": {
                "profile_id": self.profile_id,
                "identity": self.profile_identity,
                "version": self.profile_version,
                "content_sha256": self.profile_sha256,
            },
            "requires_every_source_read": self.requires_every_source_read,
            "lines": [line.as_payload() for line in self.lines],
        }

    @property
    def reading_json(self) -> str:
        return json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":")
        )

    @property
    def reading_digest(self) -> str:
        return sha256(self.reading_json.encode("utf-8")).hexdigest()


# --- what Corridor derives --------------------------------------------------


def derive_coverage_reading(
    session: Session,
    *,
    project_id: int,
    cutoff: datetime,
    inventory: IssueInventory | None,
) -> DerivedCoverageReading | None:
    """What was and was not read for one project's next issue, at one cutoff.

    ``None`` where no issue profile had taken effect by the cutoff — an
    explicit absence rather than an empty reading, because "this project has
    read nothing" and "this project is not yet configured to issue anything"
    are different facts and only one of them can be confirmed.
    """

    if cutoff.tzinfo is None:
        raise CoverageRefused(
            "a coverage reading is taken at a declared, time-zone-aware "
            "cutoff; nothing here reads a clock"
        )
    if inventory is None:
        return None

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

    confirmed = confirmed_delivery_ids(session, project_id)
    watermark = _watermark(deliveries, cutoff)
    requires = any(
        requirement.requirement == COVERAGE_ALL_REQUIRED_SOURCES_READ
        for requirement in inventory.coverage_requirements
    )
    requirement = REQUIRED if requires else OPTIONAL

    lines: list[CoverageLine] = []
    for delivery in deliveries:
        document = by_delivery.get(int(delivery.id))
        lines.append(
            _delivery_line(
                delivery,
                document=document,
                requirement=requirement,
                watermark=watermark,
                confirmed=int(delivery.id) in confirmed,
            )
        )
    for document in documents:
        if document.source_delivery_id is not None:
            continue
        lines.append(_document_line(document, requirement=requirement))

    return DerivedCoverageReading(
        project_id=int(project_id),
        cutoff=cutoff,
        through_source_delivery_id=watermark,
        profile_id=int(inventory.profile_id),
        profile_identity=inventory.profile_identity,
        profile_version=int(inventory.profile_version),
        profile_sha256=inventory.content_sha256,
        requires_every_source_read=requires,
        lines=tuple(lines),
    )


def _watermark(
    deliveries: Sequence[SourceDelivery], cutoff: datetime
) -> int | None:
    """The last delivery before the first one that arrived after the cutoff.

    A prefix of the append-only ledger rather than "every delivery whose
    ``received_at`` is at or before the cutoff": the two differ exactly when a
    delivery is recorded out of arrival order, and a boundary that admitted a
    later id while excluding an earlier one would not be a boundary a Proposed
    Delta could be compared against by identity.
    """

    boundary: int | None = None
    for delivery in deliveries:
        if delivery.received_at is not None and delivery.received_at > cutoff:
            return boundary
        boundary = int(delivery.id)
    return boundary


def _delivery_line(
    delivery: SourceDelivery,
    *,
    document: Document | None,
    requirement: str,
    watermark: int | None,
    confirmed: bool,
) -> CoverageLine:
    name = (
        document.filename
        if document is not None
        else str(delivery.external_identity)
    )
    key = f"delivery:{int(delivery.id)}"
    document_id = None if document is None else int(document.id)
    if watermark is None or int(delivery.id) > watermark:
        return CoverageLine(
            source_key=key,
            source_name=name,
            requirement=requirement,
            state=COVERAGE_LATE,
            detail="received after this issue's cutoff",
            delivery_id=int(delivery.id),
            document_id=document_id,
        )
    disposition = delivery.disposition
    if disposition in (DISPOSITION_STORED, DISPOSITION_DUPLICATE):
        if document is None:
            # Stored says Corridor holds the bytes, which is not the same as
            # somebody having admitted them to processing (#823). An upload
            # staged and abandoned reads as exactly what it is, rather than as
            # a source whose processing failed; the person who handed it over
            # is what says a confirmation was the next step at all.
            state = COVERAGE_FAILED
            if confirmed:
                detail = "confirmed, and no processing receipt records it being read"
            elif delivery.delivered_by_principal:
                detail = (
                    "received and stored, and nobody has confirmed it for "
                    "processing yet"
                )
            else:
                detail = "received, and no processing receipt records it being read"
        elif document.parse_status == "parsed":
            state, detail = COVERAGE_READ, "received and processed"
        else:
            state, detail = (
                COVERAGE_FAILED,
                f"received, and processing failed ({document.parse_status})",
            )
    else:
        state = COVERAGE_FAILED
        detail = f"{_DISPOSITION_WORDS[disposition]}: {delivery.refusal_reason or ''}"
    return CoverageLine(
        source_key=key,
        source_name=name,
        requirement=requirement,
        state=state,
        detail=detail[:DETAIL_LIMIT],
        delivery_id=int(delivery.id),
        document_id=document_id,
    )


# What each refused disposition means, in the plain words the ledger's own
# docstring uses. ``transient_failure`` is not "refused": the provider said
# nothing about the delivery, which is a different fact from refusing it.
_DISPOSITION_WORDS: Mapping[str, str] = {
    DISPOSITION_QUARANTINED: "held for review and not processed",
    DISPOSITION_TERMINALLY_REFUSED: "refused at intake and not processed",
    DISPOSITION_TRANSIENT_FAILURE: "delivery did not complete, so nothing was taken",
}


def _document_line(document: Document, *, requirement: str) -> CoverageLine:
    """One source registered through no transport, and what became of it.

    A document with no delivery is not an omission to hide: the corpus path
    registers real sources this way, and ``issue_readiness`` already counts an
    unreadable one. Leaving them out of the reading would let a project's
    coverage look complete while the file the record was built from failed.
    """

    if document.parse_status == "parsed":
        state, detail = COVERAGE_READ, "read in full"
    else:
        state, detail = (
            COVERAGE_FAILED,
            f"delivered and could not be read ({document.parse_status})",
        )
    return CoverageLine(
        source_key=f"document:{int(document.id)}",
        source_name=document.filename,
        requirement=requirement,
        state=state,
        detail=detail,
        document_id=int(document.id),
    )


# --- what a person may declare over it --------------------------------------


def declaration_payload(
    reading: DerivedCoverageReading,
    *,
    lines: Sequence[CoverageLine],
    annotations: Sequence[CoverageAnnotation],
    exclusions: Sequence[CoverageExclusion],
    principal_subject: str,
    confirmed_at: datetime,
) -> dict[str, Any]:
    """The exact bytes a confirmation is digested by, human half included."""

    return {
        "schema_version": DECLARATION_SCHEMA_VERSION,
        "derived_reading_digest": reading.reading_digest,
        "lines": [line.as_payload() for line in lines],
        "annotations": [
            {"source_key": one.source_key, "note": one.note}
            for one in annotations
        ],
        "exclusions": [
            {"source_key": one.source_key, "reason": one.reason}
            for one in exclusions
        ],
        "confirmed_by_principal": principal_subject,
        "confirmed_at": confirmed_at.isoformat(),
    }


def confirm_coverage(
    session: Session,
    *,
    project_id: int,
    reading: DerivedCoverageReading,
    confirmed_reading_digest: str,
    principal: HumanPrincipal,
    confirmed_at: datetime,
    idempotency_key: str,
    annotations: Sequence[CoverageAnnotation] = (),
    exclusions: Sequence[CoverageExclusion] = (),
) -> IssueCoverageDeclaration:
    """Append one immutable confirmation of exactly the reading that was shown.

    ``confirmed_reading_digest`` is the digest the coordinator saw. It is
    compared against the reading derived here and now, so a submission that
    was composed against an older reading — a source arrived, a document
    finished processing, the profile moved — is refused rather than recorded
    as a confirmation of something else. That is the whole reason the digest
    travels through the form at all.

    **Whether this person may confirm at all is asked of PostgreSQL** (#839),
    by appending the row and letting the relation's own guard answer. There is
    no roster read here that could disagree with it: a check composed in Python
    would be a second rule to keep in step, and the page a coordinator reads
    already prints the same roster's answer as a reading rather than as a gate.
    The append runs in a savepoint because a refusal aborts the transaction it
    is in, and the caller is usually mid-way through rendering the week the
    refusal belongs on.
    """

    actor = require_human_principal(principal)
    if confirmed_at.tzinfo is None:
        raise CoverageRefused(
            "a coverage declaration is confirmed at a declared, time-zone-aware "
            "instant; nothing here reads a clock"
        )
    if not (idempotency_key or "").strip():
        raise CoverageRefused("a coverage confirmation needs an idempotency key")
    if confirmed_reading_digest != reading.reading_digest:
        raise CoverageRefused(
            "the coverage reading changed after it was shown, so this "
            "confirmation would attest to a reading nobody saw. Read the "
            "current one and confirm that."
        )

    excluded = _proved_exclusions(reading, exclusions)
    annotated = _proved_annotations(reading, annotations)
    lines = tuple(
        line
        if line.source_key not in excluded
        else CoverageLine(
            source_key=line.source_key,
            source_name=line.source_name,
            requirement=line.requirement,
            state=COVERAGE_EXCLUDED,
            detail=excluded[line.source_key][:DETAIL_LIMIT],
            delivery_id=line.delivery_id,
            document_id=line.document_id,
        )
        for line in reading.lines
    )

    payload = declaration_payload(
        reading,
        lines=lines,
        annotations=annotated,
        exclusions=tuple(
            CoverageExclusion(key, reason) for key, reason in sorted(excluded.items())
        ),
        principal_subject=actor.subject,
        confirmed_at=confirmed_at,
    )
    declaration = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = sha256(declaration.encode("utf-8")).hexdigest()

    existing = session.scalars(
        select(IssueCoverageDeclaration).where(
            IssueCoverageDeclaration.project_id == project_id,
            IssueCoverageDeclaration.declaration_digest == digest,
        )
    ).first()
    if existing is not None:
        emit_preparation_interaction(session, EventFamily.COVERAGE_CONFIRMATION, existing,
                                     at=confirmed_at, principal_subject=actor.subject,
                                     coverage_declaration_id=existing.id,
                                     reading_sha256=reading.reading_digest,
                                     annotation_count=len(annotated), unchanged_declaration_reused=True)
        return existing

    row = IssueCoverageDeclaration(
        project_id=int(project_id),
        issue_profile_id=reading.profile_id,
        issue_profile_identity=reading.profile_identity,
        issue_profile_version=reading.profile_version,
        cutoff_at=reading.cutoff,
        through_source_delivery_id=reading.through_source_delivery_id,
        derived_reading=reading.reading_json,
        derived_reading_digest=reading.reading_digest,
        declaration=declaration,
        declaration_digest=digest,
        coverage_identity=coverage_identity(reading.cutoff, digest),
        confirmed_by_principal=actor.subject,
        confirmed_at=confirmed_at,
        idempotency_key=idempotency_key.strip()[:160],
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except DBAPIError as error:
        if not access.designation_refused(error):
            raise
        raise CoverageRefused(
            f"{actor.subject} holds no project-coordination designation for "
            f"project {int(project_id)}. Confirming the coverage an issue is "
            "prepared under is a project-coordination decision; reading this "
            "project and being designated to release it externally confer "
            "none of it. Nothing was confirmed.",
            kind=refusals.NOT_AUTHORIZED,
        ) from error
    emit_preparation_interaction(session, EventFamily.COVERAGE_CONFIRMATION, row,
                                 at=confirmed_at, principal_subject=actor.subject,
                                 coverage_declaration_id=row.id, reading_sha256=reading.reading_digest,
                                 annotation_count=len(annotated), unchanged_declaration_reused=False)
    return row


def coverage_identity(cutoff: datetime, declaration_digest: str) -> str:
    """The identity #529 binds into a candidate, derived from the declaration.

    An internal technical identifier in the same class as ``ReleasePackage``
    and ``Issue Profile``: it names the confirmed declaration, and it reaches
    no screen as a label.
    """

    return f"issue-coverage:{cutoff.date().isoformat()}:{declaration_digest[:16]}"


def _proved_exclusions(
    reading: DerivedCoverageReading, exclusions: Sequence[CoverageExclusion]
) -> dict[str, str]:
    """Every permitted exclusion, or a refusal naming the first that is not.

    This is where a coordinator's permitted act and a forbidden relabel are
    told apart. An exclusion states that a source Corridor's records describe
    honestly is intentionally left out of this issue, with a reason; it never
    changes what those records say. Marking a line ``read`` is not offered
    here at all, which is why "a coordinator relabels a failed source as read"
    is not a request this function can be given.
    """

    permitted = reading.excludable_source_keys
    proved: dict[str, str] = {}
    for exclusion in exclusions:
        line = reading.line(exclusion.source_key)
        if line is None:
            raise CoverageRefused(
                f"{exclusion.source_key} is not a source in this reading, so "
                "there is nothing here to exclude"
            )
        reason = (exclusion.reason or "").strip()
        if not reason:
            raise CoverageRefused(
                f"leaving {line.source_name} out of this issue is an "
                "intentional act and states its reason"
            )
        if len(reason) > REASON_LIMIT:
            raise CoverageRefused(
                f"the reason for leaving {line.source_name} out is longer than "
                f"{REASON_LIMIT} characters"
            )
        if exclusion.source_key not in permitted:
            raise CoverageRefused(
                f"{line.source_name} is a source this project requires for an "
                "issue, so it cannot be excluded. Required coverage that is "
                "unmet blocks the issue until the source is read; it is not "
                "something a declaration can settle."
            )
        if exclusion.source_key in proved:
            raise CoverageRefused(
                f"{line.source_name} is excluded twice, with two reasons"
            )
        proved[exclusion.source_key] = reason
    return proved


def _proved_annotations(
    reading: DerivedCoverageReading, annotations: Sequence[CoverageAnnotation]
) -> tuple[CoverageAnnotation, ...]:
    """Every bounded note, in a fixed order, or a refusal saying which is not."""

    proved: list[CoverageAnnotation] = []
    seen: set[str] = set()
    for annotation in annotations:
        if reading.line(annotation.source_key) is None:
            raise CoverageRefused(
                f"{annotation.source_key} is not a source in this reading, so "
                "there is nothing here to annotate"
            )
        note = (annotation.note or "").strip()
        if not note:
            continue
        if len(note) > ANNOTATION_LIMIT:
            raise CoverageRefused(
                f"a note beside one source is at most {ANNOTATION_LIMIT} "
                "characters; what a customer receives is the issue's own "
                "artifacts, not a second narrative here"
            )
        if annotation.source_key in seen:
            raise CoverageRefused(
                f"{annotation.source_key} carries two notes; one line takes one"
            )
        seen.add(annotation.source_key)
        proved.append(CoverageAnnotation(annotation.source_key, note))
    return tuple(sorted(proved, key=lambda one: one.source_key))


# --- reading one back -------------------------------------------------------


def declared_lines(
    declaration: IssueCoverageDeclaration,
) -> tuple[SourceCoverage, ...]:
    """The confirmed lines, out of the bytes the declaration digest covers.

    Read back from the stored declaration rather than re-derived, for the same
    reason the Issue section reads a candidate's bound inputs out of its own
    immutable declaration: re-deriving them would let a caller prepare an issue
    under a coverage state that differs from the one somebody confirmed.
    """

    payload = json.loads(declaration.declaration)
    return tuple(
        SourceCoverage(
            source_name=line.get("source_name") or "",
            requirement=line.get("requirement") or OPTIONAL,
            state=line.get("state") or COVERAGE_READ,
            detail=line.get("detail") or "",
        )
        for line in payload.get("lines") or ()
    )


def load_declaration(
    session: Session, *, project_id: int, declaration_id: int
) -> IssueCoverageDeclaration:
    """One confirmed declaration of one project, or a refusal naming why not."""

    row = session.get(IssueCoverageDeclaration, int(declaration_id))
    if row is None or int(row.project_id) != int(project_id):
        raise CoverageRefused(
            f"there is no confirmed coverage declaration {declaration_id} for "
            "this project, so there is nothing to prepare an issue under"
        )
    return row


def reusable_declaration(
    session: Session, *, project_id: int, reading: DerivedCoverageReading
) -> IssueCoverageDeclaration | None:
    """The confirmed declaration this reading may still be prepared under.

    All four of the ticket's conditions, asked separately rather than folded
    into the digest comparison alone. The digest already binds the cutoff, the
    profile version and the watermark, so the three explicit comparisons are
    redundant *today* — and that is the point: a later change to what the
    reading digests could silently widen reuse, and these three would still
    refuse it.
    """

    rows = session.scalars(
        select(IssueCoverageDeclaration)
        .where(
            IssueCoverageDeclaration.project_id == project_id,
            IssueCoverageDeclaration.cutoff_at == reading.cutoff,
            IssueCoverageDeclaration.issue_profile_id == reading.profile_id,
            IssueCoverageDeclaration.issue_profile_version
            == reading.profile_version,
            IssueCoverageDeclaration.derived_reading_digest
            == reading.reading_digest,
        )
        .order_by(IssueCoverageDeclaration.id.desc())
    ).all()
    for row in rows:
        boundary = row.through_source_delivery_id
        stated = None if boundary is None else int(boundary)
        if stated == reading.through_source_delivery_id:
            return row
    return None


# --- the boundary a Proposed Delta is compared against ----------------------


def delivery_ids_for_deltas(
    session: Session, *, project_id: int, delta_ids: Iterable[int]
) -> dict[int, int | None]:
    """The Source Delivery each Proposed Delta's Source Fact came in on.

    Reached through the delta's own group, which already names the document
    the source revision arrived as, and through that document's delivery link.
    ``None`` where the chain does not reach a delivery — a verbal statement has
    no document, and a document registered through the corpus path has no
    delivery — and a caller that gets ``None`` must not conclude anything about
    the coverage boundary from it. A reading never claims a check it did not
    make.
    """

    from corridor.models import DeltaGroup, ProposedDelta  # local: model cycle

    ids = tuple(dict.fromkeys(int(value) for value in delta_ids))
    found: dict[int, int | None] = {delta_id: None for delta_id in ids}
    if not ids:
        return found
    rows = session.execute(
        select(ProposedDelta.id, Document.source_delivery_id)
        .join(DeltaGroup, DeltaGroup.id == ProposedDelta.group_id)
        .join(Document, Document.id == DeltaGroup.document_id)
        .where(
            ProposedDelta.project_id == project_id,
            ProposedDelta.id.in_(ids),
        )
    ).all()
    for delta_id, delivery_id in rows:
        found[int(delta_id)] = None if delivery_id is None else int(delivery_id)
    return found


def deltas_outside_coverage_boundary(
    session: Session,
    *,
    project_id: int,
    delta_ids: Iterable[int],
    declaration: IssueCoverageDeclaration | None,
) -> frozenset[int]:
    """Which of these differences arrived after the confirmed boundary.

    ADR-0085's second "Can wait" limb, which #641 left absent because a
    Proposed Delta has no trustworthy arrival instant. It has an arrival
    *identity*: the delivery its Source Fact came in on. A delta whose delivery
    is newer than the declaration's frozen watermark is outside this issue by
    identity, and ``ProposedDelta.created_at`` is compared against nothing.

    Empty where no coverage has been confirmed, and empty for any delta whose
    delivery cannot be dereferenced. Both are the honest answer: without a
    confirmed boundary there is nothing to be outside of, and a delta that
    names no delivery has not been shown to be late.
    """

    if declaration is None:
        return frozenset()
    boundary = declaration.through_source_delivery_id
    if boundary is None:
        # A project that had taken no delivery when its coverage was confirmed:
        # every delivery is after the boundary, so every delta that came in on
        # one is outside it.
        return frozenset(
            delta_id
            for delta_id, delivery_id in delivery_ids_for_deltas(
                session, project_id=project_id, delta_ids=delta_ids
            ).items()
            if delivery_id is not None
        )
    return frozenset(
        delta_id
        for delta_id, delivery_id in delivery_ids_for_deltas(
            session, project_id=project_id, delta_ids=delta_ids
        ).items()
        if delivery_id is not None and delivery_id > int(boundary)
    )


def latest_declaration(
    session: Session, *, project_id: int, cutoff: datetime
) -> IssueCoverageDeclaration | None:
    """The newest coverage confirmed for this project at or before ``cutoff``.

    Ordered by the append-only identifier and not by ``confirmed_at``: the
    confirmation instant is declared by its caller, so ordering by it would let
    a superseded declaration be the current one (#634).
    """

    return session.scalars(
        select(IssueCoverageDeclaration)
        .where(
            IssueCoverageDeclaration.project_id == project_id,
            IssueCoverageDeclaration.cutoff_at <= cutoff,
        )
        .order_by(IssueCoverageDeclaration.id.desc())
        .limit(1)
    ).first()
