"""What a provisioned but unadopted project shows, and the act it carries (#827).

The customer-journey audit found the opposite of a screen: opening a
provisioned project that has not adopted a baseline answered 404 under the
enforced boundary, so a coordinator's first act in the product was being told
their project did not exist
(``docs/research/customer-journey-audit-2026-09-10.md``, "Uploading a document
is not adopting a baseline"). This module is the reading that page renders.

**It reads; it decides nothing.** ``onboarding_authorization`` says whether
onboarding is permitted and why not, ``baseline_adoption`` says what the
prepared reading contains and which questions a coordinator is entitled to
answer, and ``operating_mode`` says whether the project has adopted. Every
sentence about state on this page comes from one of those.

**Three audiences, and only one of them is on this page.** ADR-0099 requires a
withdrawal to be visible, and requires the project coordinator's view of it to
carry why onboarding is paused and which operations remain available *without*
the global control-plane registry or the customer's legal evidence. So
``PausedPanel`` carries a notice and a list of operations and nothing else:
the requester, the executing actor, the governing authorization and its
evidence digest are operations' record, reached through the operations CLI, and
are deliberately unreachable from here.

**Operations and the coordinator read the same reading, in two panels.** The
split is #509's: importer mechanics -- worksheets, formulas, hidden content,
unknown columns, diagnostics, the round trip -- are operations' and never a
project decision, and the material questions are the coordinator's. This module
reuses ``format_replacement``'s ``ValidationFinding`` and the same
``OperationsPanel`` shape #829 established rather than inventing a second
diagnostics shape for the same reading.

Terminology: nothing here coins a customer-facing label. "Adopt the baseline"
is ADR-0076's own name for the act, "onboarding" is the ADR-0099 technical
word used in prose rather than as a labelled object, and every question and
answer is printed in the sentence its own kind writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.access import COORDINATION, TECHNICAL_OPERATIONS, MembershipAccess
from corridor.baseline_adoption import (
    ANSWER_EFFECTS,
    BLOCKING_QUESTION_KINDS,
    PERMITTED_ANSWERS,
    RetainedPreview,
    latest_baseline_reading,
)
from corridor.format_replacement import ValidationFinding
from corridor.models import SourceDelivery, SourceDeliveryConfirmation
from corridor.onboarding_authorization import (
    ADOPT_BASELINE,
    INSPECT_COMPATIBILITY,
    ONBOARDING_OPERATIONS,
    OnboardingStanding,
    completed_act,
    onboarding_standing,
    permitted_operations_now,
    withdrawal_record,
)
from corridor.operating_mode import baseline_adoption, is_adopted_baseline
from corridor.source_delivery import delivery_confirmations
from corridor.web.format_replacement_view import OperationsPanel
from corridor.web.ui_primitives import recorded_moment


#: What each permitted operation is called where a coordinator reads it. These
#: are sentences about what Corridor may do next, not labelled objects, so no
#: new customer-facing name is introduced (``docs/agents/domain.md``).
OPERATION_WORDS = {
    "reach_project": "open this project and read its onboarding state",
    "receive_source": "supply the baseline workbook",
    "inspect_compatibility": "check that workbook against the record",
    "prepare_mapping": "prepare the column mapping",
    "review_baseline_questions": "review the questions the workbook raises",
    "adopt_baseline": "adopt the baseline",
    "approve_issue_profile": "approve what this project issues",
}


@dataclass(frozen=True, slots=True)
class QuestionLine:
    """One material question, with the answers it has and what each one does."""

    kind: str
    subject: str
    detail: str
    source_rows: tuple[str, ...]
    blocking: bool
    answers: tuple[tuple[str, str], ...]
    needs_choice: bool


@dataclass(frozen=True, slots=True)
class ReadingPanel:
    """The prepared reading a coordinator is being asked to adopt."""

    preview_id: int
    filename: str
    source_identity: str
    content_sha256: str
    binding_fingerprint: str
    mapping_identity: str
    mapping_version: str
    prepared_at: datetime
    adopted_rows: int
    excluded_rows: int
    adoptable: bool
    operations_resolved: bool
    questions: tuple[QuestionLine, ...]

    @property
    def blocking_questions(self) -> tuple[QuestionLine, ...]:
        return tuple(item for item in self.questions if item.blocking)


@dataclass(frozen=True, slots=True)
class SuppliedSource:
    """One file already handed over on this project, and not yet read (#934).

    The page needs it because supplying the workbook and reading it are two
    acts: a coordinator who has just uploaded one lands back here, and until
    #934 the page still asked them to supply it. The delivery id, the digest
    and the name are what ``/projects/{slug}/baseline/prepare`` takes, so the
    control the page prints carries exactly what the route reads and nothing
    composed for it.

    The delivery id is on the control because the reading is an act on *this
    delivery*, not on whichever row happens to share its digest (#933). Two
    deliveries of one workbook -- a connector pull and a person's upload an
    hour later -- carry the same bytes and are different acts under different
    authority, so a route given only a digest cannot say which one it read.

    Which is what the rest of these fields are for. A coordinator being asked
    to choose between two deliveries has to be able to tell them apart, and
    every fact that tells them apart is one the delivery ledger already
    retains: when it arrived, the channel and configuration that carried it,
    who is recorded as having submitted it, and whether anybody has confirmed
    it for processing yet. ``submitted_by`` and ``confirmation`` are sentences
    rather than values because the truthful answer is sometimes that nothing
    is recorded -- a connector pull has no submitting person at all -- and a
    name read out of a mailbox or a filename would be this page inventing one.
    """

    delivery_id: int
    content_sha256: str
    filename: str
    delivered_at: datetime
    channel: str
    configuration_identity: str
    configuration_version: str
    delivery_identity: str
    submitted_by: str
    confirmation: str


@dataclass(frozen=True, slots=True)
class SuppliedGroup:
    """The deliveries that carried one set of bytes: together, never merged.

    ``_supplied`` used to order stored deliveries newest first and drop every
    later row sharing a ``content_sha256``, so a connector pull and a person's
    upload of the same workbook reached the page as one control and the
    earlier act left no trace on it at all. One rule was answering three
    different questions, and only the last of them is this reading's:

    * whether to store the same bytes twice -- ``source_intake`` answers it,
      with a content-addressed store, and still does;
    * whether a retried submission is a second delivery --
      ``source_delivery`` answers it, with ``uq_source_deliveries_observation``
      over the derived delivery identity, and still does;
    * whether two independently recorded deliveries of identical bytes are one
      act. They are not, and this reading no longer says they are.

    Identical contents are still worth saying, because they are why one
    filename can be on the page twice. So the deliveries are grouped under
    that fact rather than collapsed into it, and every one of them stays
    named, inspectable and selectable.
    """

    content_sha256: str
    deliveries: tuple[SuppliedSource, ...]

    @property
    def identical_contents(self) -> bool:
        return len(self.deliveries) > 1


@dataclass(frozen=True, slots=True)
class PausedPanel:
    """Why onboarding is paused, and what is still available. Nothing wider.

    ``notice`` is ``onboarding_authorization``'s own sentence, so this page
    cannot say "withdrawn" while the customer-side grant is still usable.
    """

    notice: str
    remaining: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdoptedPanel:
    """What the project shows once the baseline is its accepted record."""

    adopted_at: datetime
    adopted_by_principal: str
    baseline_source_sha256: str
    importer: str
    revision_id: int | None
    next_step: str


@dataclass(frozen=True, slots=True)
class OnboardingView:
    """One provisioned project's onboarding state, as its coordinator reads it."""

    project_slug: str
    project_name: str
    adopted: AdoptedPanel | None
    reading: ReadingPanel | None
    operations: OperationsPanel | None
    findings: tuple[ValidationFinding, ...]
    paused: PausedPanel | None
    supplied: tuple[SuppliedGroup, ...]
    may_adopt: bool
    may_prepare: bool
    standing: OnboardingStanding
    next_action: str

    @property
    def awaiting_source(self) -> bool:
        return self.adopted is None and self.reading is None


def onboarding_view(
    session: Session,
    *,
    project_id: int,
    project_slug: str,
    project_name: str,
    membership: MembershipAccess,
    as_of: datetime,
) -> OnboardingView:
    """Read this project's onboarding state and the next act it offers."""

    adopted = None
    if is_adopted_baseline(session, project_id):
        receipt = baseline_adoption(session, project_id)
        adopted = AdoptedPanel(
            adopted_at=receipt.adopted_at,
            adopted_by_principal=receipt.adopted_by_principal,
            baseline_source_sha256=receipt.baseline_source_sha256,
            importer=f"{receipt.importer_identity} {receipt.importer_version}",
            revision_id=receipt.revision_id,
            next_step=(
                "Review and approve what this project issues."
                if completed_act(
                    session, project_id=project_id, operation="approve_issue_profile"
                )
                is None
                else "Submit the next revision of the customer's workbook."
            ),
        )

    retained = latest_baseline_reading(session, project_id=project_id)
    standing = onboarding_standing(
        session, project_id=project_id, operation=ADOPT_BASELINE, at=as_of
    )
    preparing = onboarding_standing(
        session, project_id=project_id, operation=INSPECT_COMPATIBILITY, at=as_of
    )

    withdrawal = withdrawal_record(session, project_id)
    paused = None
    if withdrawal is not None:
        paused = PausedPanel(
            notice=withdrawal.coordinator_notice,
            remaining=tuple(
                OPERATION_WORDS[operation]
                for operation in permitted_operations_now(
                    session, project_id=project_id, at=as_of
                )
            ),
        )

    findings: list[ValidationFinding] = []
    if retained is not None and not retained.operations_resolved:
        findings.append(
            ValidationFinding(
                code="operations_unresolved",
                sentence=(
                    "Corridor operations is still resolving how this workbook is "
                    "shaped. Nothing here is yours to decide until they have."
                ),
            )
        )
    if retained is not None and adopted is None and not retained.adoptable:
        findings.append(
            ValidationFinding(
                code="reading_not_adoptable",
                sentence=(
                    "This reading was prepared for checking rather than as a "
                    "baseline this project can adopt."
                ),
            )
        )
    if not standing.permitted and adopted is None and paused is None:
        findings.append(
            ValidationFinding(code=standing.reason, sentence=_standing_sentence(standing))
        )
    if (
        not preparing.permitted
        and adopted is None
        and paused is None
        and preparing.reason != standing.reason
    ):
        # A project whose compatibility permission has lapsed while its
        # adoption permission still stands is exactly the case that produced a
        # button and then a refusal, so the page says why instead (#934). The
        # two standings usually fail for the same recorded reason, and saying
        # it twice would read as two problems.
        findings.append(
            ValidationFinding(
                code=preparing.reason, sentence=_standing_sentence(preparing)
            )
        )

    supplied = (
        ()
        if adopted is not None or retained is not None
        else _supplied(session, project_id)
    )

    return OnboardingView(
        project_slug=project_slug,
        project_name=project_name,
        adopted=adopted,
        reading=None if retained is None else _reading(retained),
        operations=None,
        findings=tuple(findings),
        paused=paused,
        supplied=supplied,
        may_adopt=membership.has(COORDINATION),
        may_prepare=_may_prepare(membership, preparing),
        standing=standing if adopted is None else preparing,
        next_action=_next_action(
            adopted, retained, standing, paused, supplied, preparing
        ),
    )


def _may_prepare(membership: MembershipAccess, preparing: OnboardingStanding) -> bool:
    """Whether this person may ask for the reading, right now (#934).

    Two conditions, and until #934 only the first was read. **Who**: requesting
    an existing, supported preview is Project Coordination's or Technical
    Operations', and neither is implied by membership. **Whether Corridor may**:
    the compatibility permission ADR-0099 grants is per-project, versioned and
    expiring, and ``prepare_baseline_reading`` proves it in the database before
    it opens anything -- so a designated person whose project's permission has
    lapsed or been withdrawn was offered an inviting button and then met a
    refusal this page already had the answer to. A control nobody can use is
    worse than no control: it puts the refusal after the click instead of
    before it.
    """

    designated = membership.has(TECHNICAL_OPERATIONS) or membership.has(COORDINATION)
    return designated and preparing.permitted


def _supplied(session: Session, project_id: int) -> tuple[SuppliedGroup, ...]:
    """Every delivery this project has stored, newest first, grouped by bytes.

    Only the deliveries that were actually stored: a refused upload and a
    transport failure are both rows in the same ledger, and neither is a
    workbook anybody can read. The page offers the file rather than checking
    the content store for it, because the bytes can be swept between the page
    and the click and the route already answers that in its own words.

    No delivery is dropped here; ``SuppliedGroup`` says why. What replaces the
    de-duplication is the ledger's own account of each delivery, with the
    confirmations read once for the project rather than once per row, through
    the same reader the source register uses.
    """

    rows = session.scalars(
        select(SourceDelivery)
        .where(
            SourceDelivery.project_id == project_id,
            SourceDelivery.disposition == "stored",
        )
        .order_by(SourceDelivery.received_at.desc(), SourceDelivery.id.desc())
    ).all()
    confirmations = delivery_confirmations(session, project_id)
    grouped: dict[str, list[SuppliedSource]] = {}
    for row in rows:
        grouped.setdefault(row.content_sha256, []).append(
            SuppliedSource(
                delivery_id=int(row.id),
                content_sha256=row.content_sha256,
                filename=str(row.metadata_json.get("filename") or row.external_identity),
                delivered_at=row.received_at,
                channel=row.channel,
                configuration_identity=row.configuration_identity,
                configuration_version=row.configuration_version,
                delivery_identity=row.delivery_identity,
                submitted_by=_submitted_by(row),
                confirmation=_confirmation(confirmations.get(int(row.id))),
            )
        )
    return tuple(
        SuppliedGroup(content_sha256=digest, deliveries=tuple(deliveries))
        for digest, deliveries in grouped.items()
    )


def _submitted_by(delivery: SourceDelivery) -> str:
    """Who is recorded as having submitted this delivery, or that nobody is.

    Never derived. ``ck_source_delivery_authentication`` admits exactly one of
    three cases: a person's authenticated session carried the bytes, a machine
    credential did, or a connector configuration is the whole binding and no
    submitter exists to record. Only the first names anybody. Reading a sender
    out of a mailbox address or a filename would put a name on this page the
    record does not hold, which is the one thing a page asking a coordinator to
    choose between two deliveries must not do.
    """

    if delivery.delivered_by_principal:
        return f"Submitted by {delivery.delivered_by_principal}"
    if delivery.transport == "push":
        return (
            "No submitting person is recorded; a machine credential "
            "authenticated this delivery"
        )
    return (
        "No submitting person is recorded; this delivery arrived on a "
        "connector configuration"
    )


def _confirmation(confirmation: SourceDeliveryConfirmation | None) -> str:
    """Whether anybody has confirmed this delivery for processing, and who.

    Taking delivery and confirming it are two acts by two parties (#823), and
    a delivery nobody has confirmed is the ordinary state of an upload whose
    preview was left on screen. So the absence is said rather than left to be
    inferred from a line that is not there.

    In the source register's own words, and through its rendering of the
    instant, because these two screens report the same relation and must not
    describe it differently. What is deliberately not borrowed is
    ``source_register``'s *state*: those words are a joint reading of the
    disposition, the registered source and the confirmation, and this row has
    read only the last of the three.
    """

    if confirmation is None:
        return "Nobody has confirmed it for processing yet"
    return (
        f"Confirmed by {confirmation.confirmed_by_principal} "
        f"on {recorded_moment(confirmation.confirmed_at)}"
    )


def _reading(retained: RetainedPreview) -> ReadingPanel:
    excluded = sum(1 for row in retained.rows if row.get("excluded"))
    return ReadingPanel(
        preview_id=retained.preview_id,
        filename=retained.filename,
        source_identity=retained.source_identity,
        content_sha256=retained.content_sha256,
        binding_fingerprint=retained.binding_fingerprint,
        mapping_identity=retained.field_mapping.identity,
        mapping_version=retained.field_mapping.version,
        prepared_at=retained.prepared_at,
        adopted_rows=len(retained.rows) - excluded,
        excluded_rows=excluded,
        adoptable=retained.adoptable,
        operations_resolved=retained.operations_resolved,
        questions=tuple(
            QuestionLine(
                kind=item.kind,
                subject=item.subject,
                detail=item.detail,
                source_rows=item.source_rows,
                blocking=item.kind in BLOCKING_QUESTION_KINDS,
                answers=tuple(
                    (answer, ANSWER_EFFECTS[answer])
                    for answer in PERMITTED_ANSWERS[item.kind]
                ),
                needs_choice="confirm_basis" in PERMITTED_ANSWERS[item.kind],
            )
            for item in retained.questions
        ),
    )


def _standing_sentence(standing: OnboardingStanding) -> str:
    from corridor.onboarding_authorization import OnboardingRefused

    return OnboardingRefused(standing.reason).customer_sentence


def _next_action(
    adopted: AdoptedPanel | None,
    retained: RetainedPreview | None,
    standing: OnboardingStanding,
    paused: PausedPanel | None,
    supplied: tuple[SuppliedGroup, ...] = (),
    preparing: OnboardingStanding | None = None,
) -> str:
    """One sentence naming what happens next, never a list of possibilities."""

    if adopted is not None:
        return adopted.next_step
    if paused is not None:
        return paused.notice
    if not standing.permitted:
        return _standing_sentence(standing)
    if retained is None:
        if supplied:
            if preparing is not None and not preparing.permitted:
                # The act this page would otherwise name is one Corridor may
                # not perform on this project right now, so the sentence is
                # why, not an invitation (#934).
                return _standing_sentence(preparing)
            if sum(len(group.deliveries) for group in supplied) > 1:
                # "This workbook" named the one delivery the page used to
                # show. It became false the moment the page stopped collapsing
                # deliveries by digest and started listing all of them (#933),
                # because the reader now has to pick one before anything can
                # be prepared. Same promise, said about the delivery they
                # choose rather than about a workbook the page cannot name.
                return (
                    "Choose a delivery and prepare a preview of the values it "
                    "would establish. Nothing is adopted until you approve it."
                )
            return (
                "Prepare a preview of the values this workbook would "
                "establish. Nothing is adopted until you approve it."
            )
        return "Supply the customer's UCM workbook as this project's baseline."
    if not retained.operations_resolved:
        return "Corridor operations is resolving how this workbook is shaped."
    if not retained.adoptable:
        return "Prepare a reading this project can adopt."
    return "Answer the questions below, then adopt the baseline."
