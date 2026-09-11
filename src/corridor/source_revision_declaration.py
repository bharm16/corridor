"""What confirmation establishes about a delivery that its bytes cannot (#825).

``later_revision.capture_later_revision`` reads one delivered workbook under
the mapping revision a project registered, captures what it says as Source
Facts, and appends the typed differences from the accepted record as Proposed
Deltas.  Two of the facts that decide what it may propose are not in the file
and never can be.  ``is_complete_enumerative_source`` says whether the workbook
enumerates the customer's whole population; ``row_accounting_sealed`` says
whether the reading accounted for all of it.  ADR-0076 makes an apparent
removal lawful only from a complete enumerative source revision, so those two
answers decide whether a row's absence is allowed to become a proposal against
the accepted record -- and until this module existed the only way to answer
them was to be the developer writing the call.

This module is where they are asked and retained instead, together with the
three other facts the same delivery cannot state: which registered source
family it belongs to, which Document Revision it is, and whether it replaces,
adds to, or is another Document Rendition of a revision already delivered
(ADR-0069: a fact cites one rendition, and agreement across renditions is a
decision, never a merge).

**A default comes from registration or from the transport, and from nothing
else.**  That restriction is the whole design, not a caution:

- The **source family** is read back from the field mapping the project
  registered (``baseline_adoption.effective_baseline_formats``).  A project
  with no registered mapping has no family for a file to belong to, and that is
  a held source rather than a guess.
- The **mapping** answer is *proved*, not defaulted: the staged workbook is
  read through the registered manifest and
  ``field_mapping_manifest.conformance_refusals`` either returns nothing or
  says exactly which columns left the mapping.  A file that fails it is held.
- The **revision identity** defaults only from the transport's own external
  version, and only when that version is something the transport actually
  supplied.  A product upload whose uploader declared no revision carries the
  content digest in that column (``source_intake._upload_observation`` says so
  in as many words), and a digest is Corridor's own fallback rather than the
  source system's fact about itself, so it is never offered as an answer.
- **Completeness and the relationship are never defaulted at all.**  No
  registration and no transport metadata says whether a workbook is the
  customer's whole matrix or a filtered export of it, and none says whether a
  file replaces what came before.  The previous delivery having been complete
  says nothing about this one -- a coordinator who exported "my rows" instead
  of "all rows" produces a file that looks exactly like the complete one --
  and neither does a filename, which is why no rule here reads either.

``answer_sources_json`` on the retained row records which of those three
origins each answer had, so the receipt can show that completeness was asked
rather than inferred even after the defaulting rules change.

**What this module does not do.**  It writes no accepted value, registers no
Document, and decides nothing about a row.  It records one person's declaration
about one delivery and reads it back for the dispatch; every refusal that
matters to the accepted record still belongs to ``later_revision`` and to the
source-append commands, which re-prove the registered mapping for themselves
rather than trusting what a screen resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.baseline_adoption import (
    adopted_baseline_source,
    effective_baseline_formats,
    effective_field_mapping_manifest,
)
from corridor.baseline_workbook import (
    BaselineWorkbookUnsupported,
    read_baseline_workbook,
)
from corridor.field_mapping_manifest import FieldMappingManifest, conformance_refusals
from corridor.ingest import SPREADSHEET_SUFFIXES
from corridor.models import Document, Project, SourceDelivery, SourceRevisionDeclaration
from corridor.operating_mode import is_adopted_baseline
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_intake import StagedSource
from corridor.storage import stored_file


# The document kind a UCM workbook is registered under, the same one adoption
# and `later_revision` register it as.
REVISION_DOC_TYPE = "matrix"

# Whether the delivery lists every current row of its source or only some of
# them. Closed, because ADR-0076 admits an apparent removal from exactly one of
# them and a third state would be a third answer to a two-answer question.
COMPLETE_ENUMERATION = "complete_enumeration"
PARTIAL_EXPORT = "partial_export"
COMPLETENESS_CHOICES = (COMPLETE_ENUMERATION, PARTIAL_EXPORT)

# How the delivery stands to the revisions already delivered. `replaces` and
# `supplements` are both later revisions of the source; `additional_rendition`
# is the same Document Revision in another file, which ADR-0069 says is a
# second rendition and not a second revision.
REPLACES = "replaces"
SUPPLEMENTS = "supplements"
ADDITIONAL_RENDITION = "additional_rendition"
RELATIONSHIP_CHOICES = (REPLACES, SUPPLEMENTS, ADDITIONAL_RENDITION)

# Where one recorded answer came from.
FROM_REGISTRATION = "registration"
FROM_SOURCE_METADATA = "source_metadata"
FROM_DECLARATION = "declared"

# The fields confirmation establishes, in the order they are asked.
SOURCE_FAMILY = "source_family"
REVISION_IDENTITY = "revision_identity"
COMPLETENESS = "completeness"
REVISION_RELATIONSHIP = "revision_relationship"
RELATED_REVISION_IDENTITY = "related_revision_identity"
USES_REGISTERED_MAPPING = "uses_registered_mapping"

# What the dispatch does with a delivered spreadsheet on an adopted project.
CAPTURE_LATER_REVISION = "capture_later_revision"
RETAIN_RENDITION = "retain_rendition"
HOLD = "hold"

# Who acts next on a held source. Corridor Operations is the bounded context
# that resolves a workbook's mechanics and prepares a mapping revision
# (CONTEXT-MAP.md); the coordinator is the person who delivered the file.
CORRIDOR_OPERATIONS = "Corridor operations"


class SourceRevisionDeclarationRefused(ValueError):
    """The declaration a caller supplied is not one this delivery can carry."""


class SourceRevisionHeld(RuntimeError):
    """This delivery is held, and nothing partial may be written from it.

    Its own class rather than an ``ExtractionFailed``, because the two mean
    opposite things to anybody reading the outcome afterwards: a failed
    extraction is an attempt a later pass may simply repeat, and a held source
    will read exactly the same way on every pass until the party this names
    acts. ``held`` carries the three facts so a reader is not left parsing the
    sentence back apart.
    """

    def __init__(self, held: "HeldSource") -> None:
        super().__init__(held.sentence)
        self.held = held


@dataclass(frozen=True)
class DeclarationChoice:
    """One answer a coordinator may give, and the words it is offered in."""

    value: str
    label: str


@dataclass(frozen=True)
class DeclarationQuestion:
    """One fact confirmation must establish, and what is known about it.

    ``answer`` and ``answer_label`` are filled where registration or the
    transport already supplies the answer; such a question is shown as a known
    fact with ``source`` saying where it came from, and is not asked again.
    A question with no ``answer`` is asked, and ``choices`` is empty where the
    answer is the customer's own wording rather than one of a closed set.
    """

    field: str
    question: str
    note: str = ""
    choices: tuple[DeclarationChoice, ...] = ()
    answer: str = ""
    answer_label: str = ""
    source: str = ""
    detail: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.answer)


@dataclass(frozen=True)
class HeldSource:
    """A delivery nothing may be read from yet, and who acts next.

    Three separate facts, because a held source that says only "held" sends a
    coordinator to ask an engineer what happened. ``reason`` is what Corridor
    found, ``responsible_party`` is who acts, and ``next_action`` is what would
    change it.
    """

    reason: str
    responsible_party: str
    next_action: str

    @property
    def sentence(self) -> str:
        """The three facts as one durable sentence, composed here.

        A recorded hold's ``reason`` is one column, so the three have to reach
        a reader through it. The module that owns the words composes them; a
        processing pass or a template joining them would be minting a customer
        sentence away from the state it describes.
        """

        return f"{self.reason} {self.responsible_party} acts next. {self.next_action}"

    def as_payload(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "responsible_party": self.responsible_party,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class RevisionIntakeReading:
    """What confirming this staged delivery must establish, read-only.

    ``applies`` is false where the delivery is not a later revision of a
    registered source at all -- a legacy project, or a file that is not a
    workbook -- and the ordinary confirmation is the whole of it.
    """

    applies: bool
    held: HeldSource | None = None
    questions: tuple[DeclarationQuestion, ...] = ()
    source_family: str = ""
    manifest: FieldMappingManifest | None = None

    @property
    def unanswered(self) -> tuple[DeclarationQuestion, ...]:
        return tuple(item for item in self.questions if not item.answered)

    @property
    def known(self) -> tuple[DeclarationQuestion, ...]:
        return tuple(item for item in self.questions if item.answered)


@dataclass(frozen=True)
class RevisionDeclaration:
    """One coordinator's retained declaration, as a capture reads it.

    The reader is executed by a worker's service identity; this is the separate
    fact that says what a person declared about the delivery and who that
    person was. It is never manufactured by the worker: a capture with no
    retained declaration behind it has no ``RevisionDeclaration`` to pass.
    """

    declared_by: HumanPrincipal
    source_family: str = ""
    revision_identity: str = ""
    revision_relationship: str = ""
    related_revision_identity: str = ""
    is_complete_enumerative_source: bool = False
    row_accounting_sealed: bool = False
    uses_registered_mapping: bool = True
    declaration_id: int | None = None
    answer_sources: dict[str, str] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "declaration_id": self.declaration_id,
            "declared_by_principal": self.declared_by.subject,
            "source_family": self.source_family,
            "revision_identity": self.revision_identity,
            "revision_relationship": self.revision_relationship,
            "related_revision_identity": self.related_revision_identity,
            "is_complete_enumerative_source": self.is_complete_enumerative_source,
            "row_accounting_sealed": self.row_accounting_sealed,
            "uses_registered_mapping": self.uses_registered_mapping,
            "answer_sources": dict(self.answer_sources or {}),
        }


@dataclass(frozen=True)
class RevisionRouting:
    """What the ordinary processing pass does with one delivered workbook."""

    disposition: str
    declaration: RevisionDeclaration | None = None
    held: HeldSource | None = None


# --- The reading confirmation draws --------------------------------------


def revision_intake_reading(
    session: Session, project: Project, staged: StagedSource, doc_type: str
) -> RevisionIntakeReading:
    """What a later revision of this project's registered source must declare.

    Writes nothing. Resolves every answer registration or the transport can
    supply, proves the workbook against the registered mapping revision, and
    leaves everything else to be asked.
    """

    if (
        doc_type != REVISION_DOC_TYPE
        or staged.suffix.lower() not in SPREADSHEET_SUFFIXES
        or not is_adopted_baseline(session, project.id)
    ):
        return RevisionIntakeReading(applies=False)

    registration = effective_baseline_formats(session, project.id).get("field_mapping")
    if registration is None:
        return RevisionIntakeReading(
            applies=True,
            held=HeldSource(
                reason=(
                    "This project has no registered field mapping, so Corridor "
                    "cannot say which registered source this file is a revision "
                    "of."
                ),
                responsible_party=CORRIDOR_OPERATIONS,
                next_action=(
                    "Corridor operations prepares the field mapping revision "
                    "and a person holding this project's coordination "
                    "designation registers it. Nothing is read from this file "
                    "until then."
                ),
            ),
        )

    manifest = effective_field_mapping_manifest(session, project.id)
    if manifest is None:
        return RevisionIntakeReading(
            applies=True,
            held=HeldSource(
                reason=(
                    "This project's registered field mapping "
                    f"({registration.format_identity} "
                    f"{registration.format_version}) stores no declaration, so "
                    "the mapping revision it names cannot be resolved."
                ),
                responsible_party=CORRIDOR_OPERATIONS,
                next_action=(
                    "Corridor operations registers the mapping revision again "
                    "with the declaration it digests to. Nothing is read from "
                    "this file until then."
                ),
            ),
        )

    try:
        reading = read_baseline_workbook(
            staged.stored_path,
            external_references=manifest.external_reference_headings,
        )
    except BaselineWorkbookUnsupported as exc:
        return RevisionIntakeReading(
            applies=True,
            held=_operations_hold(
                "Corridor could not read this workbook: " f"{exc}",
            ),
            manifest=manifest,
        )
    if not reading.resolved:
        return RevisionIntakeReading(
            applies=True,
            held=_operations_hold(
                "Corridor operations has not resolved this workbook's "
                "mechanics, so its columns cannot be read."
            ),
            manifest=manifest,
        )

    refusals = conformance_refusals(manifest, reading)
    if refusals:
        return RevisionIntakeReading(
            applies=True,
            held=HeldSource(
                reason=(
                    "This file is not the field mapping revision registered "
                    f"for this project ({manifest.revision}): "
                    + "; ".join(refusals)
                ),
                responsible_party=CORRIDOR_OPERATIONS,
                next_action=(
                    "Supply this revision in the registered form, or have "
                    "Corridor operations register the successor mapping "
                    "revision. Nothing is read from this file until then."
                ),
            ),
            manifest=manifest,
        )

    return RevisionIntakeReading(
        applies=True,
        questions=_questions(session, project, staged, manifest),
        source_family=source_family_of(manifest),
        manifest=manifest,
    )


def _operations_hold(reason: str) -> HeldSource:
    return HeldSource(
        reason=reason,
        responsible_party=CORRIDOR_OPERATIONS,
        next_action=(
            "Corridor operations settles the workbook's mechanics. Nothing is "
            "read from this file until then."
        ),
    )


def _questions(
    session: Session,
    project: Project,
    staged: StagedSource,
    manifest: FieldMappingManifest,
) -> tuple[DeclarationQuestion, ...]:
    """Every fact confirmation establishes, answered where it honestly can be."""

    family = source_family_of(manifest)
    supplied = _supplied_revision_identity(session, project, staged)
    return (
        DeclarationQuestion(
            field=SOURCE_FAMILY,
            question="Which registered source is this a revision of?",
            answer=family,
            answer_label=f"{manifest.identity} {manifest.version}",
            source=FROM_REGISTRATION,
            detail="The field mapping revision registered for this project.",
        ),
        DeclarationQuestion(
            field=USES_REGISTERED_MAPPING,
            question="Does this file use the registered field mapping?",
            answer="yes",
            answer_label="Yes",
            source=FROM_REGISTRATION,
            detail=(
                "Its columns were checked against the registered mapping "
                f"revision {manifest.revision} and match it."
            ),
        ),
        DeclarationQuestion(
            field=REVISION_IDENTITY,
            question="Which revision of that source is this file?",
            note=(
                "The customer's own name for this revision, as the source "
                "system or the document states it."
            ),
            answer=supplied,
            answer_label=supplied,
            source=FROM_SOURCE_METADATA if supplied else "",
            detail=(
                "The external version the delivery arrived under."
                if supplied
                else ""
            ),
        ),
        DeclarationQuestion(
            field=COMPLETENESS,
            question=(
                "Does this file list every current row of that source, or "
                "only some of them?"
            ),
            note=(
                "A row missing from a file that lists only some rows is never "
                "read as a removal. Nothing about the file says which this is, "
                "and an earlier delivery having listed every row says nothing "
                "about this one."
            ),
            choices=(
                DeclarationChoice(COMPLETE_ENUMERATION, "Every current row"),
                DeclarationChoice(PARTIAL_EXPORT, "Only some of the rows"),
            ),
        ),
        DeclarationQuestion(
            field=REVISION_RELATIONSHIP,
            question="How does this file stand to what has already arrived?",
            choices=(
                DeclarationChoice(REPLACES, "It replaces an earlier revision"),
                DeclarationChoice(
                    SUPPLEMENTS, "It adds to a revision already delivered"
                ),
                DeclarationChoice(
                    ADDITIONAL_RENDITION,
                    "It is another file of a revision already delivered",
                ),
            ),
        ),
        DeclarationQuestion(
            field=RELATED_REVISION_IDENTITY,
            question="Which revision is it another file of?",
            note=(
                "Answered only where the file is another file of a revision "
                "already delivered."
            ),
        ),
    )


def _supplied_revision_identity(
    session: Session, project: Project, staged: StagedSource
) -> str:
    """The transport's own external version for these bytes, where it has one.

    A product upload whose uploader declared no revision carries the content
    digest in ``external_version`` -- that is Corridor's own fallback so a
    re-upload converges on one delivery, not the source system's fact about
    itself -- so a version equal to the digest is no answer and is not offered
    as one. Nothing here reads the filename.
    """

    row = session.scalars(
        select(SourceDelivery)
        .where(
            SourceDelivery.project_id == project.id,
            SourceDelivery.content_sha256 == staged.sha256,
        )
        .order_by(SourceDelivery.id.desc())
    ).first()
    if row is None:
        return ""
    version = (row.external_version or "").strip()
    return "" if version == row.content_sha256 else version


def source_family_of(manifest: FieldMappingManifest) -> str:
    """The lineage a revision and its successors share.

    Named by the mapping revision's *identity*, never its version: a successor
    mapping revision of the same family still describes the same customer form,
    and a delta from this revision has to be able to supersede one from the
    last (#518). ``later_revision`` records exactly this string on the Proposed
    Delta group, so the family a coordinator declares and the family the group
    carries are one value rather than two that agree by habit.
    """

    return f"ucm_workbook:{manifest.identity}"[:64]


# --- The declaration itself -----------------------------------------------


def declare_source_revision(
    session: Session,
    *,
    project: Project,
    delivery: SourceDelivery,
    reading: RevisionIntakeReading,
    principal: HumanPrincipal,
    revision_identity: str,
    completeness: str,
    revision_relationship: str,
    related_revision_identity: str = "",
) -> SourceRevisionDeclaration:
    """Record what this person declared about this delivery, once.

    Runs in the caller's transaction. The answers registration and the
    transport supply are taken from ``reading`` -- re-derived server side by the
    caller rather than echoed back through a form -- and only the answers a
    person actually gave arrive as arguments. A replay returns the declaration
    already recorded; correcting one is a separate act against a new delivery,
    because what was declared, and when, is never edited.
    """

    actor = require_human_principal(principal)
    if not reading.applies or reading.held is not None:
        raise SourceRevisionDeclarationRefused(
            "a held delivery carries no revision declaration; nothing is read "
            "from it until the named party acts"
        )
    if delivery.project_id != project.id:
        raise SourceRevisionDeclarationRefused(
            "this delivery belongs to another project"
        )
    if completeness not in COMPLETENESS_CHOICES:
        raise SourceRevisionDeclarationRefused(
            f"completeness must be one of {list(COMPLETENESS_CHOICES)}"
        )
    if revision_relationship not in RELATIONSHIP_CHOICES:
        raise SourceRevisionDeclarationRefused(
            f"the relationship must be one of {list(RELATIONSHIP_CHOICES)}"
        )
    related = related_revision_identity.strip()
    if revision_relationship == ADDITIONAL_RENDITION and not related:
        raise SourceRevisionDeclarationRefused(
            "another file of a revision already delivered names the revision "
            "it is another file of"
        )

    answers = {question.field: question for question in reading.questions}
    identity = (answers[REVISION_IDENTITY].answer or revision_identity).strip()
    if not identity:
        raise SourceRevisionDeclarationRefused(
            "a later revision is declared under the customer's own name for it"
        )
    sources = {
        SOURCE_FAMILY: FROM_REGISTRATION,
        USES_REGISTERED_MAPPING: FROM_REGISTRATION,
        REVISION_IDENTITY: (
            FROM_SOURCE_METADATA
            if answers[REVISION_IDENTITY].answered
            else FROM_DECLARATION
        ),
        COMPLETENESS: FROM_DECLARATION,
        REVISION_RELATIONSHIP: FROM_DECLARATION,
    }
    if related:
        sources[RELATED_REVISION_IDENTITY] = FROM_DECLARATION

    existing = declaration_for_delivery(session, project.id, int(delivery.id))
    if existing is not None:
        return existing
    row = SourceRevisionDeclaration(
        delivery_id=int(delivery.id),
        project_id=project.id,
        source_family=reading.source_family,
        revision_identity=identity,
        completeness=completeness,
        revision_relationship=revision_relationship,
        related_revision_identity=related or None,
        uses_registered_mapping=True,
        answer_sources_json=sources,
        declared_by_principal=actor.subject,
    )
    session.add(row)
    session.flush()
    return row


def declaration_for_delivery(
    session: Session, project_id: int, delivery_id: int
) -> SourceRevisionDeclaration | None:
    """The declaration recorded for one delivery of one project, if any."""

    return session.scalars(
        select(SourceRevisionDeclaration).where(
            SourceRevisionDeclaration.project_id == project_id,
            SourceRevisionDeclaration.delivery_id == delivery_id,
        )
    ).first()


def capture_declaration(row: SourceRevisionDeclaration) -> RevisionDeclaration:
    """The retained row, as ``later_revision`` reads it.

    One declared answer settles both of the reader's flags, and deliberately
    so. ``row_accounting.finish`` already refuses a reading with an unaccounted
    row, so the seal a capture can offer is exactly the claim that the file
    lists every current row of its source; declaring "every current row" is
    therefore declaring both, and declaring "only some of the rows" withholds
    both. Keeping them as two independent arguments would let a caller declare
    a complete enumeration whose rows it had not accounted for, which is the
    combination no answer on the screen can produce.
    """

    complete = row.completeness == COMPLETE_ENUMERATION
    return RevisionDeclaration(
        declared_by=HumanPrincipal(row.declared_by_principal),
        source_family=row.source_family,
        revision_identity=row.revision_identity,
        revision_relationship=row.revision_relationship,
        related_revision_identity=row.related_revision_identity or "",
        is_complete_enumerative_source=complete,
        row_accounting_sealed=complete,
        uses_registered_mapping=bool(row.uses_registered_mapping),
        declaration_id=int(row.id),
        answer_sources=dict(row.answer_sources_json or {}),
    )


# --- What the ordinary processing pass does with one --------------------------


def route_delivered_revision(
    session: Session, document: Document
) -> RevisionRouting | None:
    """Route one delivered spreadsheet on an adopted project, or decline to.

    ``None`` means this is not a delivered revision of a registered source and
    the ordinary spreadsheet reader keeps it: a legacy project, a document that
    arrived through no transport, or the adopted baseline's own bytes, which
    are the revision every later one is compared against rather than one of
    them.

    Otherwise the delivery is one of three things. A declared later revision is
    captured with its declarations. A declared *additional rendition* is
    retained and not compared: ADR-0069 says a fact cites one rendition, so its
    exact Source Segments and locators are kept by the registration that
    already appended them, and reading it a second time would produce a second
    logical revision of one revision and the same actionable changes twice.
    Anything else is held with the party who acts next named -- including a
    delivery nobody declared, because a workbook nobody has said anything about
    is exactly the unknown file that must not be read by guesswork.
    """

    delivery_id = getattr(document, "source_delivery_id", None)
    if delivery_id is None:
        return None
    project_id = int(document.project_id)
    if not is_adopted_baseline(session, project_id):
        return None
    baseline = adopted_baseline_source(session, project_id)
    if baseline is not None and baseline.content_sha256 == document.sha256:
        return None

    declaration = declaration_for_delivery(session, project_id, int(delivery_id))
    if declaration is None:
        delivery = session.get(SourceDelivery, int(delivery_id))
        delivered_by = (
            (delivery.delivered_by_principal or "").strip()
            if delivery is not None
            else ""
        )
        return RevisionRouting(
            disposition=HOLD,
            held=HeldSource(
                reason=(
                    "Nobody has declared which revision of this project's "
                    "registered source this file is, so Corridor cannot read "
                    "it as one."
                ),
                responsible_party=(
                    delivered_by
                    or "a person holding this project's coordination designation"
                ),
                next_action=(
                    "Confirm this file through the product and answer which "
                    "revision it is and whether it lists every current row. "
                    "Nothing is read from it until then."
                ),
            ),
        )
    if declaration.revision_relationship == ADDITIONAL_RENDITION:
        return RevisionRouting(
            disposition=RETAIN_RENDITION,
            declaration=capture_declaration(declaration),
        )
    return RevisionRouting(
        disposition=CAPTURE_LATER_REVISION,
        declaration=capture_declaration(declaration),
    )


def staged_delivered_source(document: Document) -> StagedSource | None:
    """The registered bytes of one document, as a capture takes them.

    ``later_revision`` reads the content-addressed store rather than a
    Document, because the same bytes are one object however many times they
    were delivered. This is the one place the pass converts the one into the
    other, so no caller assembles a ``StagedSource`` of its own.
    """

    path = getattr(document, "_stored_path", None) or stored_file(document)
    if path is None:
        return None
    resolved = Path(path)
    if not resolved.exists():
        return None
    return StagedSource(
        sha256=document.sha256,
        size_bytes=resolved.stat().st_size,
        suffix=resolved.suffix.lower(),
        filename=document.filename,
        stored_path=resolved,
    )
