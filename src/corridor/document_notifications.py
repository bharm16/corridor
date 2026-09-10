"""Document-change and lost-support interruptions, and their recoverable delivery.

Two of the four approved immediate-notification categories (ADR-0034 decision 39,
ADR-0037) remained after new assignments (#351): the loss of an affirmative
Documentation Review's applicable current support, and an authentic registered
source transition that affects a current Commitment or a
relocation/removal/abandonment Constraint.  A second delivery queue was rejected:
it would duplicate the leases, retries, clock, and crash recovery the one
supervised Due Work runtime (#332) already owns.  Reusing the delivery *adapter
seam* and the *runtime* was the intent, but the state machine around them was
private to :mod:`corridor.notifications` and this module copied it — the queue,
the retry arithmetic, the attempt receipts, and the pass receipt, which then
drifted from their originals.  That machine is
:mod:`corridor.outgoing_dispatch` now and this module registers a category with
it, so what remains here is only what those two categories genuinely need that
the assignment occurrence cannot carry:

- **Discovery** — ``register_project_document_notifications`` reads *committed*
  state and registers one immutable occurrence per (event, recipient), bound to
  exact identities: the affirmative review and its reviewed citation for a loss,
  the proven Revision Comparison finding or the superseding statement event for a
  change.  It examines the complete authoritative affected population (the released
  supersession worklist and every affirmative review), not just the visible Work
  List, and converges on one row through the occurrence fingerprint so a persistent
  condition or a repeated pass never resends.  A loss preserves the earlier review
  *and its author*; one recipient (the original reviewer) may hold no roster entry,
  which the assignment-shaped occurrence cannot express.  Registration is a derived
  system act — Corridor stops showing the requirement as met and records visible
  work; it never reverses the human judgment, asserts physical work, or changes a
  source relationship.

- **Delivery** — ``deliver_project_document_notifications`` is what the runtime's
  handler calls.  The shared machine owns the pass; this module contributes the
  currency re-check that runs before every send — that the underlying condition is
  still current, that the recipient is still a project member, and that a typed
  verified contact exists — and the message content.  An ambiguous correspondence
  is delivered honestly as uncertain and never as a proved change.

This module deliberately imports no runtime and no web layer; the runtime depends
on it for the one handler key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor import digests, outgoing_dispatch, support_update_routing
from corridor.documentation_checklist import APPROVAL_INTERPRETATION, read_checklist
from corridor.due_work_contract import (
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    validate_scheduling,
)
from corridor.models import (
    CommitmentLineage,
    Dependency,
    Document,
    DocumentNotification,
    DocumentNotificationAttempt,
    DocumentNotificationDispatch,
    DocumentationFieldConfirmation,
    EvidenceLink,
    ExternalPartyStatement,
    FollowUpPlanReceipt,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.outgoing_dispatch import Currency, DeliveryAdapter
from corridor.statement_lifecycle import current_lineage_statement
from corridor.work_decisions import (
    CoordinationSubject,
    current_internal_owner_decision,
)


# The Due Work handler key for document-notification delivery. Defined here,
# next to the domain record it reconciles, and imported by the runtime — the
# runtime depends on this module, never the reverse.
DOCUMENT_NOTIFICATION_HANDLER = "document_notification"

# The two interruption categories this slice adds (#353). No fifth category is
# introduced: parse/fetch failures stay operations information (ADR-0034 #39).
CATEGORY_DOCUMENTATION_LOSS = "documentation_loss"
CATEGORY_DOCUMENT_CHANGE = "document_change"

CHANNEL_EMAIL = "email"

# One occurrence targets one recipient. A loss reaches the current assignee and
# the original reviewer; when they coincide, one occurrence carries the combined
# role so a coinciding recipient is deduplicated rather than mailed twice.
ROLE_CURRENT_ASSIGNEE = "current_assignee"
ROLE_ORIGINAL_REVIEWER = "original_reviewer"
ROLE_ASSIGNEE_AND_REVIEWER = "current_assignee_and_original_reviewer"

# The current supported reasons an affirmative review lost applicable support.
REASON_SUPERSEDING_REVISION = "superseding_revision"
REASON_CITATION_UNVERIFIED = "citation_no_longer_verified"
REASON_SUPPORT_REMOVED = "support_removed"
REASON_READING_UNSUPPORTED = "reading_no_longer_supported"
# The change class for a commitment whose plan a factual successor affected.
REASON_FACTUAL_SUCCESSOR = "factual_successor_statement"

# A benign delivery skip: the underlying loss or change resolved (a re-review, a
# carried-forward support, a coordinated plan) before the message was sent.
LIMITATION_CONDITION_RESOLVED = "condition_resolved"

# Constraints whose resolution involves relocation, removal, or abandonment are
# the "critical Dependency" of ADR-0034 #39; a document change to their support
# is a category-4 interruption.
_QUALIFYING_STRATEGIES = frozenset({"relocate", "remove", "abandon_in_place"})

_RESULT_SCHEMA_VERSION = "document-notification-result-v1"


class DocumentNotificationRefusal(ValueError):
    """A document-notification act was refused; nothing was written."""


# --- Discovery and registration -------------------------------------------


@dataclass(frozen=True)
class _Recipient:
    principal_subject: str
    role: str


@dataclass
class _Event:
    """One affected-subject event and the recipients it must reach."""

    category: str
    subject_kind: str
    dependency_id: int | None
    commitment_lineage_id: int | None
    reason_code: str
    change_uncertain: bool
    review_confirmation_id: int | None
    requirement_field: str | None
    reviewed_evidence_link_id: int | None
    original_reviewer_subject: str | None
    predecessor_document_id: int | None
    successor_document_id: int | None
    comparison_id: int | None
    finding_id: int | None
    statement_event_id: int | None
    source_context: dict[str, Any]
    recipients: tuple[_Recipient, ...]

    def transition_identity(self) -> dict[str, Any]:
        """The exact source/review/subject-transition identity for the fingerprint."""

        if self.category == CATEGORY_DOCUMENTATION_LOSS:
            return {
                "review_confirmation_id": self.review_confirmation_id,
                "reviewed_evidence_link_id": self.reviewed_evidence_link_id,
            }
        return {
            "comparison_id": self.comparison_id,
            "finding_id": self.finding_id,
            "statement_event_id": self.statement_event_id,
        }


def register_project_document_notifications(
    session: Session, *, project_id: int, registered_by: str
) -> tuple[DocumentNotification, ...]:
    """Register the project's current document-related interruptions (#353).

    Reads only committed state, so a rolled-back transition leaves nothing; a
    committed one is discovered and registered as recoverable delivery work. The
    occurrence fingerprint makes a repeated pass, a competing discoverer, and a
    persistent condition converge on one row rather than resend.
    """

    if not isinstance(registered_by, str) or not registered_by.strip():
        raise DocumentNotificationRefusal("a registrar identity is required")
    events = _discover_documentation_loss(session, project_id) + _discover_document_changes(
        session, project_id
    )
    registered: list[DocumentNotification] = []
    for event in events:
        for recipient in event.recipients:
            registered.append(
                _register_occurrence(
                    session,
                    project_id=project_id,
                    event=event,
                    recipient=recipient,
                    registered_by=registered_by.strip(),
                )
            )
    return tuple(registered)


def _discover_documentation_loss(
    session: Session, project_id: int
) -> list[_Event]:
    """Every affirmative Documentation Review that lost applicable support.

    Iterates the complete set of affirmative reviews, not the visible Work List.
    A review that still binds, one whose requirement another current act
    re-established, or a Constraint that is still Ready is not a loss — so an
    exact-unchanged support update never generates an interruption.
    """

    rows = session.execute(
        select(DocumentationFieldConfirmation, Dependency)
        .join(Dependency, DocumentationFieldConfirmation.dependency_id == Dependency.id)
        .where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
            DocumentationFieldConfirmation.conclusion == "approved",
        )
        .order_by(DocumentationFieldConfirmation.id)
    ).all()
    events: list[_Event] = []
    for confirmation, dependency in rows:
        checklist = read_checklist(session, dependency.id)
        if not checklist.uses_standard_checklist:
            # A preserved legacy history is not the ADR-0052 standard review;
            # historical markers without the required identity are left alone.
            continue
        try:
            field = checklist.field(APPROVAL_INTERPRETATION)
        except KeyError:
            continue
        if confirmation.id in field.confirmation_ids:
            continue  # the review still binds: no loss
        if field.complete or checklist.is_ready:
            continue  # re-reviewed or carried forward: the requirement is met
        reason, predecessor_id, superseding_id = _loss_reason(session, confirmation)
        context = _loss_context(session, dependency, confirmation, reason, superseding_id)
        events.append(
            _Event(
                category=CATEGORY_DOCUMENTATION_LOSS,
                subject_kind="constraint",
                dependency_id=dependency.id,
                commitment_lineage_id=None,
                reason_code=reason,
                change_uncertain=False,
                review_confirmation_id=confirmation.id,
                requirement_field=confirmation.field_name,
                reviewed_evidence_link_id=confirmation.evidence_link_id,
                original_reviewer_subject=confirmation.confirmed_by,
                predecessor_document_id=predecessor_id,
                successor_document_id=superseding_id,
                comparison_id=None,
                finding_id=None,
                statement_event_id=None,
                source_context=context,
                recipients=_loss_recipients(
                    session,
                    dependency=dependency,
                    reviewer_subject=confirmation.confirmed_by,
                    context=context,
                ),
            )
        )
    return events


def _loss_reason(
    session: Session, confirmation: DocumentationFieldConfirmation
) -> tuple[str, int | None, int | None]:
    """The current supported reason a review no longer has applicable support."""

    link = session.get(EvidenceLink, confirmation.evidence_link_id)
    if link is None:
        return REASON_SUPPORT_REMOVED, None, None
    document = session.get(Document, link.document_id)
    if document is None:
        return REASON_SUPPORT_REMOVED, None, None
    if document.superseded_by is not None:
        return REASON_SUPERSEDING_REVISION, document.id, document.superseded_by
    if link.verified is not True:
        return REASON_CITATION_UNVERIFIED, document.id, None
    return REASON_READING_UNSUPPORTED, document.id, None


def _loss_context(
    session: Session,
    dependency: Dependency,
    confirmation: DocumentationFieldConfirmation,
    reason: str,
    superseding_id: int | None,
) -> dict[str, Any]:
    link = session.get(EvidenceLink, confirmation.evidence_link_id)
    reviewed_document = (
        session.get(Document, link.document_id) if link is not None else None
    )
    superseding_document = (
        session.get(Document, superseding_id) if superseding_id is not None else None
    )
    return {
        "requirement_label": "Organization approval is confirmed",
        "reviewed_document": _document_ref(reviewed_document),
        "reviewed_passage": {
            "page_no": link.page_no if link is not None else None,
            "quote": link.quote if link is not None else None,
        },
        "newer_document": _document_ref(superseding_document),
        "original_reviewer": confirmation.confirmed_by,
        "reason": reason,
    }


def _loss_recipients(
    session: Session,
    *,
    dependency: Dependency,
    reviewer_subject: str,
    context: dict[str, Any],
) -> tuple[_Recipient, ...]:
    """The current assignee and the original reviewer, deduplicated.

    A current assignee that cannot be mapped to a typed roster identity is a
    visible gap recorded in the context, never a name-inferred recipient.
    """

    assignee_subject, mapping = _current_assignee_subject(
        session, CoordinationSubject.dependency(dependency.id)
    )
    context["current_assignee_mapping"] = mapping
    roles: dict[str, set[str]] = {}
    if assignee_subject is not None:
        roles.setdefault(assignee_subject, set()).add(ROLE_CURRENT_ASSIGNEE)
    if isinstance(reviewer_subject, str) and reviewer_subject.strip():
        roles.setdefault(reviewer_subject, set()).add(ROLE_ORIGINAL_REVIEWER)
    return tuple(
        _Recipient(principal_subject=subject, role=_combined_role(role_set))
        for subject, role_set in roles.items()
    )


def _combined_role(role_set: set[str]) -> str:
    if role_set == {ROLE_CURRENT_ASSIGNEE}:
        return ROLE_CURRENT_ASSIGNEE
    if role_set == {ROLE_ORIGINAL_REVIEWER}:
        return ROLE_ORIGINAL_REVIEWER
    return ROLE_ASSIGNEE_AND_REVIEWER


def _discover_document_changes(
    session: Session, project_id: int
) -> list[_Event]:
    """Every proven source transition affecting a qualifying current subject.

    Constraint side: the released supersession worklist's customer consequences
    on a relocation/removal/abandonment Constraint, each backed by an immutable
    Revision Comparison; a readiness change routes to loss-of-support instead, and
    a technical failure stays operations information. Commitment side: a current
    commitment whose plan a factual successor statement affected.
    """

    events: list[_Event] = []
    events.extend(_constraint_change_events(session, project_id))
    events.extend(_commitment_change_events(session, project_id))
    return events


def _constraint_change_events(
    session: Session, project_id: int
) -> list[_Event]:
    events: list[_Event] = []
    for consequence in support_update_routing.route_support_update_consequences(
        session, project_id
    ):
        if consequence.is_operations:
            continue  # a technical failure is operations info, never an interruption
        if consequence.destination == support_update_routing.DOCUMENTATION_REVIEW:
            continue  # a readiness loss is category A, handled by the review scan
        if consequence.comparison_id is None:
            continue  # no authentic proven transition to bind to
        dependency = session.get(Dependency, consequence.dependency_id)
        if dependency is None or dependency.resolution_strategy not in _QUALIFYING_STRATEGIES:
            continue
        assignee_subject, mapping = _current_assignee_subject(
            session, CoordinationSubject.dependency(dependency.id)
        )
        if assignee_subject is None:
            continue  # no one to interrupt; the change stays visible in the Work List
        uncertain = consequence.destination == support_update_routing.GUIDED_STATEMENT
        context = _change_context(
            session, consequence, mapping=mapping, uncertain=uncertain
        )
        events.append(
            _Event(
                category=CATEGORY_DOCUMENT_CHANGE,
                subject_kind="constraint",
                dependency_id=dependency.id,
                commitment_lineage_id=None,
                reason_code=consequence.reason,
                change_uncertain=uncertain,
                review_confirmation_id=None,
                requirement_field=None,
                reviewed_evidence_link_id=None,
                original_reviewer_subject=None,
                predecessor_document_id=consequence.predecessor_document_id,
                successor_document_id=consequence.successor_document_id,
                comparison_id=consequence.comparison_id,
                finding_id=consequence.finding_id,
                statement_event_id=None,
                source_context=context,
                recipients=(
                    _Recipient(
                        principal_subject=assignee_subject,
                        role=ROLE_CURRENT_ASSIGNEE,
                    ),
                ),
            )
        )
    return events


def _commitment_change_events(
    session: Session, project_id: int
) -> list[_Event]:
    events: list[_Event] = []
    lineages = session.scalars(
        select(CommitmentLineage)
        .where(
            CommitmentLineage.project_id == project_id,
            CommitmentLineage.plan_needs_review.is_(True),
        )
        .order_by(CommitmentLineage.id)
    ).all()
    for lineage in lineages:
        statement = current_lineage_statement(session, lineage.id)
        if statement is None or statement.supersedes_event_id is None:
            continue  # an affected plan needs an authentic superseding statement
        assignee_subject, mapping = _current_assignee_subject(
            session, CoordinationSubject.statement(lineage.id)
        )
        if assignee_subject is None:
            continue
        context = {
            "current_assignee_mapping": mapping,
            "superseding_statement_event_id": statement.id,
            "superseded_statement_event_id": statement.supersedes_event_id,
            "explanation": (
                "A newer statement changes the fact this commitment's plan "
                "responds to. Review whether the current plan still fits; the "
                "earlier plan and its receipts are preserved."
            ),
        }
        events.append(
            _Event(
                category=CATEGORY_DOCUMENT_CHANGE,
                subject_kind="statement",
                dependency_id=None,
                commitment_lineage_id=lineage.id,
                reason_code=REASON_FACTUAL_SUCCESSOR,
                change_uncertain=False,
                review_confirmation_id=None,
                requirement_field=None,
                reviewed_evidence_link_id=None,
                original_reviewer_subject=None,
                predecessor_document_id=None,
                successor_document_id=None,
                comparison_id=None,
                finding_id=None,
                statement_event_id=statement.id,
                source_context=context,
                recipients=(
                    _Recipient(
                        principal_subject=assignee_subject,
                        role=ROLE_CURRENT_ASSIGNEE,
                    ),
                ),
            )
        )
    return events


def _change_context(
    session: Session,
    consequence: support_update_routing.RoutedSupportConsequence,
    *,
    mapping: str,
    uncertain: bool,
) -> dict[str, Any]:
    predecessor = (
        session.get(Document, consequence.predecessor_document_id)
        if consequence.predecessor_document_id is not None
        else None
    )
    successor = (
        session.get(Document, consequence.successor_document_id)
        if consequence.successor_document_id is not None
        else None
    )
    context: dict[str, Any] = {
        "current_assignee_mapping": mapping,
        "explanation": consequence.explanation,
        "predecessor_document": _document_ref(predecessor),
        "newer_document": _document_ref(successor),
        "uncertain": uncertain,
    }
    source = support_update_routing.changed_source_context(session, consequence)
    if source is not None:
        context["changed_values"] = [
            {"field": change.field, "before": change.before, "after": change.after}
            for change in source.changed_values
        ]
        context["ambiguous"] = source.ambiguous
    return context


def _register_occurrence(
    session: Session,
    *,
    project_id: int,
    event: _Event,
    recipient: _Recipient,
    registered_by: str,
) -> DocumentNotification:
    occurrence_key = _occurrence_key(event, recipient.principal_subject)
    public_id = f"document-notification:{occurrence_key[:24]}"
    session.execute(
        insert(DocumentNotification)
        .values(
            public_id=public_id,
            project_id=project_id,
            category=event.category,
            subject_kind=event.subject_kind,
            dependency_id=event.dependency_id,
            commitment_lineage_id=event.commitment_lineage_id,
            recipient_principal_subject=recipient.principal_subject,
            recipient_role=recipient.role,
            review_confirmation_id=event.review_confirmation_id,
            requirement_field=event.requirement_field,
            reviewed_evidence_link_id=event.reviewed_evidence_link_id,
            original_reviewer_subject=event.original_reviewer_subject,
            predecessor_document_id=event.predecessor_document_id,
            successor_document_id=event.successor_document_id,
            comparison_id=event.comparison_id,
            finding_id=event.finding_id,
            statement_event_id=event.statement_event_id,
            reason_code=event.reason_code,
            change_uncertain=event.change_uncertain,
            source_context_json=event.source_context,
            occurrence_key=occurrence_key,
            registered_by=registered_by,
        )
        .on_conflict_do_nothing(index_elements=["occurrence_key"])
    )
    notification = session.scalar(
        select(DocumentNotification).where(
            DocumentNotification.occurrence_key == occurrence_key
        )
    )
    if notification is None:  # pragma: no cover - insert just guaranteed a row
        raise DocumentNotificationRefusal("the occurrence could not be registered")
    existing = session.scalar(
        select(DocumentNotificationDispatch).where(
            DocumentNotificationDispatch.notification_id == notification.id
        )
    )
    if existing is None:
        contact, limitation = outgoing_dispatch.resolve_contact(
            session, recipient.principal_subject
        )
        idempotency_key = digests.canonical_sha256(
            {"occurrence_key": occurrence_key, "channel": CHANNEL_EMAIL}
        )
        session.execute(
            insert(DocumentNotificationDispatch)
            .values(
                public_id=f"document-dispatch:{occurrence_key[:24]}",
                notification_id=notification.id,
                project_id=project_id,
                channel=CHANNEL_EMAIL,
                delivery_state="queued",
                recipient_contact=contact,
                delivery_limitation=limitation,
                attempt_count=0,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=["notification_id"])
        )
        session.flush()
    return notification


def _occurrence_key(event: _Event, recipient_principal: str) -> str:
    return digests.canonical_sha256(
        {
            "category": event.category,
            "subject_kind": event.subject_kind,
            "dependency_id": event.dependency_id,
            "commitment_lineage_id": event.commitment_lineage_id,
            "transition": event.transition_identity(),
            "recipient_principal": recipient_principal,
        }
    )


# --- Recipient resolution -------------------------------------------------


def _current_assignee_subject(
    session: Session, subject: CoordinationSubject
) -> tuple[str | None, str]:
    """The current assignee's principal through the typed roster binding only.

    Returns ``(principal_subject, mapping)`` where mapping is ``resolved`` when a
    grouping receipt binds the current Internal Owner decision to a roster
    identity, ``unassigned`` when there is no current owner, and ``unresolved``
    when an owner exists but no typed roster binding does — the last is a visible
    gap, never a name-inferred recipient.
    """

    decision = current_internal_owner_decision(session, subject)
    if decision is None:
        return None, "unassigned"
    if subject.dependency_id is not None:
        roster_entry_id = session.scalar(
            select(FollowUpPlanReceipt.internal_owner_roster_entry_id).where(
                FollowUpPlanReceipt.internal_owner_decision_id == decision.id
            )
        )
    else:
        roster_entry_id = session.scalar(
            select(StatementCoordinationReceipt.internal_owner_roster_entry_id).where(
                StatementCoordinationReceipt.internal_owner_decision_id == decision.id
            )
        )
    if roster_entry_id is None:
        return None, "unresolved"
    entry = session.get(ProjectRosterEntry, roster_entry_id)
    if entry is None:
        return None, "unresolved"
    return entry.principal_subject, "resolved"


# --- Delivery sweep: the category's contribution to the shared machine ----


def _document_category() -> outgoing_dispatch.DispatchCategory:
    """What the document-interruption family contributes to the shared machine."""
    return outgoing_dispatch.DispatchCategory(
        name="document",
        handler_key=DOCUMENT_NOTIFICATION_HANDLER,
        notification_model=DocumentNotification,
        dispatch_model=DocumentNotificationDispatch,
        attempt_model=DocumentNotificationAttempt,
        result_schema_version=_RESULT_SCHEMA_VERSION,
        currency=_occurrence_currency,
        subject_summary=_subject_summary,
    )


def deliver_project_document_notifications(
    session_factory,
    *,
    project_id: int,
    configuration_version: str,
    channel: str,
    adapter: DeliveryAdapter,
    clock,
    max_attempts: int,
    backoff_seconds: int,
    budget: int,
    owner: str,
) -> dict[str, Any]:
    """Deliver this project's due document notifications through the adapter.

    The shared dispatch machine owns the pass: each dispatch is processed in its
    own short transactions, the provider call holds no transaction at all, and the
    pass is bounded by ``budget``. What this category adds is
    ``_occurrence_currency`` — before every send the underlying loss or change,
    the recipient's membership, and the typed contact are re-checked, so a
    resolved condition is a benign skip and an obsolete recipient a visible
    limitation.
    """

    return outgoing_dispatch.deliver_pass(
        session_factory,
        _document_category(),
        project_id=project_id,
        configuration_version=configuration_version,
        channel=channel,
        adapter=adapter,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        budget=budget,
        owner=owner,
    )


def _occurrence_currency(
    session: Session, notification: DocumentNotification
) -> Currency:
    """Re-check the interruption before dispatch; never present a stale one.

    A loss or change that is no longer current is withheld as a benign skip: the
    interruption needs no send. A recipient who is no longer a project member, or
    who has no verified contact, is a limitation the operator should see — and the
    recipient re-check applies to a reviewer as much as an assignee, so a person
    who left the project is never mailed.
    """

    if not _condition_still_current(session, notification):
        return Currency.withheld(
            "resolved", LIMITATION_CONDITION_RESOLVED, benign=True
        )
    withheld = outgoing_dispatch.withheld_recipient(
        session,
        project_id=notification.project_id,
        principal_subject=notification.recipient_principal_subject,
    )
    if withheld is not None:
        return withheld
    contact, unreachable = outgoing_dispatch.contact_currency(
        session, notification.recipient_principal_subject
    )
    if unreachable is not None:
        return unreachable
    return Currency.current(contact)


def _condition_still_current(
    session: Session, notification: DocumentNotification
) -> bool:
    if notification.category == CATEGORY_DOCUMENTATION_LOSS:
        return _loss_still_current(session, notification)
    if notification.subject_kind == "statement":
        return _commitment_change_still_current(session, notification)
    return _constraint_change_still_current(session, notification)


def _loss_still_current(
    session: Session, notification: DocumentNotification
) -> bool:
    dependency = session.get(Dependency, notification.dependency_id)
    if dependency is None or dependency.dismissed_at is not None:
        return False
    checklist = read_checklist(session, dependency.id)
    if not checklist.uses_standard_checklist:
        return False
    try:
        field = checklist.field(APPROVAL_INTERPRETATION)
    except KeyError:
        return False
    if notification.review_confirmation_id in field.confirmation_ids:
        return False  # the review binds again
    return not (field.complete or checklist.is_ready)


def _constraint_change_still_current(
    session: Session, notification: DocumentNotification
) -> bool:
    for consequence in support_update_routing.route_support_update_consequences(
        session, notification.project_id
    ):
        if (
            not consequence.is_operations
            and consequence.dependency_id == notification.dependency_id
            and consequence.comparison_id == notification.comparison_id
            and consequence.finding_id == notification.finding_id
            and consequence.destination != support_update_routing.DOCUMENTATION_REVIEW
        ):
            return True
    return False


def _commitment_change_still_current(
    session: Session, notification: DocumentNotification
) -> bool:
    lineage = session.get(CommitmentLineage, notification.commitment_lineage_id)
    if lineage is None or not lineage.plan_needs_review:
        return False
    statement = current_lineage_statement(session, lineage.id)
    return statement is not None and statement.id == notification.statement_event_id


# --- Message content ------------------------------------------------------


def _subject_summary(
    session: Session, notification: DocumentNotification
) -> dict[str, Any]:
    """Plain-language interruption content with its exact linked source context.

    A loss preserves the earlier review and its author, asks for current project
    work, and never asserts that physical work is unfinished. A change links its
    exact source context and, when the correspondence is uncertain, says so
    rather than presenting a proved change to a particular project fact.
    """

    context = dict(notification.source_context_json or {})
    label = _subject_label(session, notification)
    if notification.category == CATEGORY_DOCUMENTATION_LOSS:
        body = (
            f"The documentation that met '{context.get('requirement_label', 'the requirement')}' "
            f"for {label} no longer has current applicable support. Please review the "
            "current documentation against the stated requirement. The earlier review "
            f"by {notification.original_reviewer_subject} is preserved; this does not "
            "reverse that judgment and does not mean any physical work is unfinished."
        )
        return {
            "category": notification.category,
            "subject_label": label,
            "recipient_role": notification.recipient_role,
            "requirement": context.get("requirement_label"),
            "reason": notification.reason_code,
            "earlier_review": {
                "author": notification.original_reviewer_subject,
                "reviewed_document": context.get("reviewed_document"),
                "reviewed_passage": context.get("reviewed_passage"),
            },
            "newer_document": context.get("newer_document"),
            "body": body,
            "uncertain": False,
        }
    if notification.change_uncertain:
        body = (
            f"A newer document may affect the support for {label}, but the "
            "correspondence is uncertain and is not treated as a proved change. "
            "Coordinate the correct interpretation; the alternatives are preserved."
        )
    else:
        body = context.get("explanation") or (
            f"A newer document changes source material that supports {label}. "
            "Review what changed against the current record."
        )
    return {
        "category": notification.category,
        "subject_label": label,
        "recipient_role": notification.recipient_role,
        "reason": notification.reason_code,
        "predecessor_document": context.get("predecessor_document"),
        "newer_document": context.get("newer_document"),
        "changed_values": context.get("changed_values", []),
        "body": body,
        "uncertain": bool(notification.change_uncertain),
    }


def _subject_label(session: Session, notification: DocumentNotification) -> str:
    if notification.subject_kind == "constraint":
        dependency = session.get(Dependency, notification.dependency_id)
        ref = dependency.ref_code if dependency is not None else "constraint"
        return f"Constraint {ref}"
    return f"Commitment {notification.commitment_lineage_id}"


def _document_ref(document: Document | None) -> dict[str, Any] | None:
    if document is None:
        return None
    return {"document_id": document.id, "filename": document.filename}


# --- Reads: recipient inbox and operations delivery view ------------------


def recipient_document_inbox(
    session: Session, *, project_id: int, principal_subject: str
) -> list[dict[str, Any]]:
    """One member's document notifications in this project, with standing.

    Scoped to the signed-in member and the one project, so no other project's
    sources, review history, or artifacts are exposed.
    """

    rows = outgoing_dispatch.paired_rows(
        session,
        DocumentNotification,
        DocumentNotificationDispatch,
        project_id=project_id,
        principal_subject=principal_subject,
    )
    return [
        {
            "notification_id": notification.id,
            "public_id": notification.public_id,
            "category": notification.category,
            "subject_kind": notification.subject_kind,
            "subject_label": _subject_label(session, notification),
            "recipient_role": notification.recipient_role,
            "uncertain": bool(notification.change_uncertain),
            "delivery_state": dispatch.delivery_state,
            "delivery_limitation": dispatch.delivery_limitation,
        }
        for notification, dispatch in rows
    ]


def operations_document_notifications_view(
    session: Session, *, project_id: int
) -> dict[str, Any]:
    """Every document-notification delivery in one project, for the operator.

    Distinguishes delivery states with subject context and any delivery
    limitation, and whether real delivery is enabled by a recorded gate-7
    configuration. Scoped to one project.
    """

    rows = outgoing_dispatch.paired_rows(
        session,
        DocumentNotification,
        DocumentNotificationDispatch,
        project_id=project_id,
    )
    enabled = outgoing_dispatch.enabled_schedule(
        session, project_id=project_id, handler_key=DOCUMENT_NOTIFICATION_HANDLER
    )
    return outgoing_dispatch.operations_projection(
        rows,
        delivery_enabled=enabled is not None,
        row=lambda notification, dispatch: {
            "notification_id": notification.id,
            "category": notification.category,
            "subject_label": _subject_label(session, notification),
            "recipient": notification.recipient_principal_subject,
            "recipient_role": notification.recipient_role,
            "delivery_state": dispatch.delivery_state,
            "delivery_limitation": dispatch.delivery_limitation,
            "attempt_count": dispatch.attempt_count,
            "last_error_code": dispatch.last_error_code,
        },
    )


# --- The Due Work declaration this delivery runs under ---------------------
#
# One channel, a positive request budget, and the two approved categories over
# the recorded affected population: what this delivery is declared to be lives
# with the delivery. The runtime keeps the lease and the retries (card 6).

# The upper ceiling on sends one bounded delivery pass may attempt. A gate-7
# notification schedule must declare a positive request budget within this.
_DOCUMENT_NOTIFICATION_BUDGET_CEILING = 10_000


@dataclass(frozen=True)
class DocumentNotificationDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables document-notification delivery.

    Delivery reads no model, so its ``model_token_budget`` must be a declared
    zero; instead it declares a positive ``notification_budget`` — the request
    budget bounding how many sends one bounded pass may attempt. The declaration
    records the project and channel scope, dispatch cadence and timezone, retry
    budget, missed-run handling, and retention. Missing or invalid configuration
    leaves delivery refused and disabled; a committed transition still registers
    its durable dispatch, but nothing is delivered until an authorized operator
    records this gate-7 scope. Project, source, and subject scope are fixed for
    this slice: the two approved categories over the recorded affected population,
    reaching the typed current assignee and the original reviewer through their
    verified-contact records only.
    """

    handler_key: ClassVar[str] = DOCUMENT_NOTIFICATION_HANDLER

    channel: str

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        starts_at: datetime,
        channel: str = "email",
        notification_budget: int = 500,
    ) -> "DocumentNotificationDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            channel=channel,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=60,
            claim_ttl_seconds=600,
            deadline_seconds=300,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=notification_budget,
        )


def _validated_declaration(
    declaration: DocumentNotificationDeclaration,
) -> ValidatedDeclaration:
    """Validate one document-notification declaration."""

    if declaration.channel != "email":
        raise DueWorkRefusal(
            "document-notification supports only the email channel in this slice"
        )
    starts_at = validate_scheduling(
        declaration,
        subject="document-notification",
        backoff_seconds=(1, 3600),
        notification_budget=(1, _DOCUMENT_NOTIFICATION_BUDGET_CEILING),
    )
    scope = {
        "project_id": declaration.project_id,
        "channel": declaration.channel,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=DOCUMENT_NOTIFICATION_HANDLER,
            scope=scope,
            input_identity={
                "kind": "project_document_notifications-v1",
                **scope,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
            extra={
                # The project/source/subject scope this delivery is authorized
                # for: the two approved categories over the recorded affected
                # population, reaching the typed current assignee and the
                # original reviewer only through their verified-contact records.
                "subject_scope": "documentation_loss_and_document_change-v1",
                "recipient_contact_source": "verified_person_identity-v1",
            },
        ),
        input_identity={"handler": DOCUMENT_NOTIFICATION_HANDLER, **scope},
    )


def _stored_declaration(
    stored: ResolvedSchedule,
) -> DocumentNotificationDeclaration:
    return DocumentNotificationDeclaration(
        **stored.scheduling_fields(),
        channel=stored.scope.get("channel", ""),
    )


def _run_due_work(context) -> dict[str, Any]:
    """Discover and deliver a project's due document notifications (#353).

    First, in its own committed transaction, it re-discovers the complete
    authoritative affected population from committed state and registers any new
    interruption occurrences idempotently — a rolled-back transition leaves
    nothing, and a persistent condition converges on the existing rows. Then it
    delivers the due dispatches, committing each outcome durably and holding no
    transaction across the provider call. It reads no model, and the channel's
    adapter is resolved from the shared notifications seam, which defaults to a
    non-sending adapter so completing the code enables no real delivery.
    """

    channel = context.schedule.scope.get("channel", "")
    with context.session_factory() as registering:
        with registering.begin():
            register_project_document_notifications(
                registering,
                project_id=context.schedule.project_id,
                registered_by=context.claim.runtime_owner,
            )

    return deliver_project_document_notifications(
        context.session_factory,
        project_id=context.schedule.project_id,
        configuration_version=context.schedule.configuration_version,
        channel=channel,
        adapter=outgoing_dispatch.resolve_delivery_adapter(channel),
        clock=context.clock,
        max_attempts=context.schedule.max_attempts,
        backoff_seconds=context.schedule.backoff_seconds,
        budget=context.schedule.notification_budget,
        owner=context.claim.runtime_owner,
    )


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=DOCUMENT_NOTIFICATION_HANDLER,
    scope_kind="one_project_document_notifications",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=_DOCUMENT_NOTIFICATION_BUDGET_CEILING,
    declaration_type=DocumentNotificationDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    run_effectful=_run_due_work,
)
