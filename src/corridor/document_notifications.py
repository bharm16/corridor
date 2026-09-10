"""Document-change and lost-support interruptions, and their recoverable delivery.

Two of the four approved immediate-notification categories (ADR-0034 decision 39,
ADR-0037) remained after new assignments (#351): the loss of an affirmative
Documentation Review's applicable current support, and an authentic registered
source transition that affects a current Commitment or a
relocation/removal/abandonment Constraint.  A second delivery queue was rejected:
it would duplicate the leases, retries, clock, and crash recovery the one
supervised Due Work runtime (#332) already owns.  So this module reuses the
delivery *adapter seam* and the *runtime* from :mod:`corridor.notifications`, and
adds only what those two categories genuinely need that the assignment occurrence
cannot carry:

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
  handler calls.  It re-checks, before every send, that the underlying condition is
  still current, that the recipient is still a project member, and that a typed
  verified contact exists; it does its provider I/O holding no project mutation
  lock, and retains the provider result, bounded retry state, and an explicit
  uncertain outcome.  An ambiguous correspondence is delivered honestly as
  uncertain and never as a proved change.

This module deliberately imports no runtime and no web layer; the runtime depends
on it for the one handler key.
"""

from __future__ import annotations

from corridor import digests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from corridor import access, support_update_routing
from corridor.documentation_checklist import APPROVAL_INTERPRETATION, read_checklist
from corridor.models import (
    CommitmentLineage,
    Dependency,
    Document,
    DocumentNotification,
    DocumentNotificationAttempt,
    DocumentNotificationDispatch,
    DocumentationFieldConfirmation,
    DueWorkSchedule,
    EvidenceLink,
    ExternalPartyStatement,
    FollowUpPlanReceipt,
    PersonIdentity,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.notifications import (
    DeliveryAdapter,
    DeliveryOutcome,
    DeliveryRequest,
    LIMITATION_REVOKED_MEMBERSHIP,
    LIMITATION_UNRESOLVED_CONTACT,
)
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
        contact, limitation = _resolve_contact(session, recipient.principal_subject)
        idempotency_key = _sha256(
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
    return _sha256(
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


def _resolve_contact(
    session: Session, recipient_principal: str
) -> tuple[str | None, str | None]:
    """The verified contact for a principal, or a visible delivery limitation.

    Contact resolves only through the typed verified-contact record; an
    unresolved mapping is a limitation, never a guessed or name-inferred address.
    """

    identity = session.scalar(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == recipient_principal
        )
    )
    if identity is None:
        return None, LIMITATION_UNRESOLVED_CONTACT
    return identity.email_normalized, None


# --- Delivery sweep -------------------------------------------------------


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

    Each dispatch is processed in its own short transactions and the provider
    call holds no transaction at all, so provider I/O never spans a project
    mutation lock. Before every send the underlying condition, the recipient's
    membership, and the typed contact are re-checked; a resolved condition is a
    benign skip and an obsolete recipient a visible limitation. Bounded by
    ``budget``.
    """

    now = _aware_utc(clock.now())
    with session_factory() as reading:
        dispatch_ids = list(
            reading.scalars(
                select(DocumentNotificationDispatch.id)
                .where(
                    DocumentNotificationDispatch.project_id == project_id,
                    DocumentNotificationDispatch.channel == channel,
                    or_(
                        DocumentNotificationDispatch.delivery_state == "queued",
                        (
                            (DocumentNotificationDispatch.delivery_state == "retry_due")
                            & (DocumentNotificationDispatch.next_attempt_at <= now)
                        ),
                    ),
                )
                .order_by(DocumentNotificationDispatch.id)
                .limit(budget)
            ).all()
        )

    counts = {
        "considered": 0,
        "completed": 0,
        "retry_due": 0,
        "failed": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    for dispatch_id in dispatch_ids:
        counts["considered"] += 1
        contact: str | None = None
        summary: dict[str, Any] = {}
        idempotency_key = ""
        attempt_from = 0
        status = "current"
        with session_factory() as checking:
            with checking.begin():
                dispatch = checking.get(
                    DocumentNotificationDispatch, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.channel != channel:
                    continue
                if not _is_deliverable(dispatch, now=now):
                    continue
                notification = checking.get(
                    DocumentNotification, dispatch.notification_id
                )
                status, contact, limitation = _occurrence_currency(checking, notification)
                summary = _subject_summary(checking, notification)
                idempotency_key = dispatch.idempotency_key
                attempt_from = dispatch.attempt_count
                if status != "current":
                    _finalize_limitation(
                        checking,
                        dispatch,
                        limitation=limitation,
                        owner=owner,
                        now=_aware_utc(clock.now()),
                    )
                    # A resolved condition is a benign skip: the interruption is
                    # no longer current and needs no send. A revoked membership or
                    # an unresolved contact is a limitation the operator should see.
                    if status == "resolved":
                        counts["skipped"] += 1
                    else:
                        counts["failed"] += 1
                    continue

        if status != "current":
            continue

        try:
            outcome = adapter.deliver(
                DeliveryRequest(
                    channel=channel,
                    recipient_contact=contact,
                    idempotency_key=idempotency_key,
                    subject_summary=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one dispatch must not abort the pass
            outcome = DeliveryOutcome(
                status="failed",
                error_code="delivery_provider_error",
                retryable=True,
                provider_result={"delivered": False, "error": str(exc)[:200]},
            )

        with session_factory() as recording:
            with recording.begin():
                dispatch = recording.get(
                    DocumentNotificationDispatch, dispatch_id, with_for_update=True
                )
                if dispatch is None or dispatch.attempt_count != attempt_from:
                    continue  # another worker already finalized this attempt
                new_state = _record_outcome(
                    recording,
                    dispatch,
                    contact=contact,
                    outcome=outcome,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                    owner=owner,
                    now=_aware_utc(clock.now()),
                )
        counts[new_state] = counts.get(new_state, 0) + 1

    return summarize_delivery_pass(
        counts,
        project_id=project_id,
        configuration_version=configuration_version,
        delivery_enabled=_delivery_enabled(session_factory, project_id),
        observed_at=_aware_utc(clock.now()),
    )


def summarize_delivery_pass(
    counts: dict[str, int],
    *,
    project_id: int,
    configuration_version: str,
    delivery_enabled: bool,
    observed_at: datetime,
) -> dict[str, Any]:
    """One bounded, credential-free receipt body for a delivery pass."""

    attention = counts.get("failed", 0) > 0 or counts.get("uncertain", 0) > 0
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": "delivery_attention_required" if attention else "healthy",
        "delivery_enabled": bool(delivery_enabled),
        "considered": int(counts.get("considered", 0)),
        "completed": int(counts.get("completed", 0)),
        "retry_due": int(counts.get("retry_due", 0)),
        "failed": int(counts.get("failed", 0)),
        "uncertain": int(counts.get("uncertain", 0)),
        "skipped": int(counts.get("skipped", 0)),
    }


def _is_deliverable(dispatch: DocumentNotificationDispatch, *, now: datetime) -> bool:
    if dispatch.delivery_state == "queued":
        return True
    return (
        dispatch.delivery_state == "retry_due"
        and dispatch.next_attempt_at is not None
        and _aware_utc(dispatch.next_attempt_at) <= now
    )


def _occurrence_currency(
    session: Session, notification: DocumentNotification
) -> tuple[str, str | None, str | None]:
    """Re-check the interruption before dispatch; never present a stale one.

    Returns ``(status, contact, limitation)`` where status is ``current``,
    ``resolved`` (the loss or change is no longer current), ``revoked`` (the
    recipient is no longer a project member), or ``unresolved`` (no verified
    contact). The recipient re-check applies to a reviewer as much as an
    assignee, so a person who left the project is never mailed.
    """

    if not _condition_still_current(session, notification):
        return "resolved", None, LIMITATION_CONDITION_RESOLVED
    membership = access.resolve_membership(
        session, notification.recipient_principal_subject, notification.project_id
    )
    if membership is None:
        return "revoked", None, LIMITATION_REVOKED_MEMBERSHIP
    contact, limitation = _resolve_contact(
        session, notification.recipient_principal_subject
    )
    if contact is None:
        return "unresolved", None, limitation
    return "current", contact, None


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


def _record_outcome(
    session: Session,
    dispatch: DocumentNotificationDispatch,
    *,
    contact: str,
    outcome: DeliveryOutcome,
    max_attempts: int,
    backoff_seconds: int,
    owner: str,
    now: datetime,
) -> str:
    attempt_number = dispatch.attempt_count + 1
    dispatch.attempt_count = attempt_number
    dispatch.recipient_contact = contact
    dispatch.delivery_limitation = None
    dispatch.provider_message_id = outcome.provider_message_id
    dispatch.provider_result_json = outcome.provider_result

    if outcome.status == "completed":
        dispatch.delivery_state = "completed"
        dispatch.last_error_code = None
        dispatch.next_attempt_at = None
        attempt_outcome = "completed"
    else:
        error_code = outcome.error_code or (
            "acknowledgment_unavailable"
            if outcome.status == "uncertain"
            else "delivery_failed"
        )
        dispatch.last_error_code = error_code
        attempt_outcome = "uncertain" if outcome.status == "uncertain" else "failed"
        if outcome.retryable and attempt_number < max_attempts:
            dispatch.delivery_state = "retry_due"
            dispatch.next_attempt_at = now + timedelta(
                seconds=backoff_seconds * (2 ** (attempt_number - 1))
            )
        else:
            dispatch.delivery_state = (
                "uncertain" if outcome.status == "uncertain" else "failed"
            )
            dispatch.next_attempt_at = None
    session.flush([dispatch])
    _append_attempt(
        session,
        dispatch,
        attempt_number=attempt_number,
        outcome=attempt_outcome,
        recipient_contact=contact,
        limitation=None,
        provider_message_id=outcome.provider_message_id,
        provider_result=outcome.provider_result,
        error_code=dispatch.last_error_code,
        owner=owner,
        now=now,
    )
    return dispatch.delivery_state


def _finalize_limitation(
    session: Session,
    dispatch: DocumentNotificationDispatch,
    *,
    limitation: str | None,
    owner: str,
    now: datetime,
) -> None:
    """Terminally record why a re-checked interruption was not delivered.

    The terminal state is ``failed`` with an explanatory limitation for every
    case, including a benign resolved condition: nothing was sent, so the
    delivery is never marked completed. The pass count distinguishes a benign
    skip from a visible limitation.
    """

    attempt_number = dispatch.attempt_count + 1
    dispatch.attempt_count = attempt_number
    dispatch.delivery_state = "failed"
    dispatch.delivery_limitation = limitation
    dispatch.last_error_code = limitation
    dispatch.next_attempt_at = None
    session.flush([dispatch])
    _append_attempt(
        session,
        dispatch,
        attempt_number=attempt_number,
        outcome="skipped",
        recipient_contact=None,
        limitation=limitation,
        provider_message_id=None,
        provider_result=None,
        error_code=limitation,
        owner=owner,
        now=now,
    )


def _append_attempt(
    session: Session,
    dispatch: DocumentNotificationDispatch,
    *,
    attempt_number: int,
    outcome: str,
    recipient_contact: str | None,
    limitation: str | None,
    provider_message_id: str | None,
    provider_result: dict[str, Any] | None,
    error_code: str | None,
    owner: str,
    now: datetime,
) -> DocumentNotificationAttempt:
    attempt = DocumentNotificationAttempt(
        public_id=f"document-attempt:{_sha256({'dispatch': dispatch.id, 'n': attempt_number})[:24]}",
        dispatch_id=dispatch.id,
        project_id=dispatch.project_id,
        attempt_number=attempt_number,
        outcome=outcome,
        recipient_contact=recipient_contact,
        delivery_limitation=limitation,
        provider_message_id=provider_message_id,
        provider_result_json=provider_result,
        error_code=error_code,
        runtime_owner=owner,
        observed_at=now,
    )
    session.add(attempt)
    session.flush([attempt])
    return attempt


def _delivery_enabled(session_factory, project_id: int) -> bool:
    with session_factory() as reading:
        schedule = reading.scalar(
            select(DueWorkSchedule.id).where(
                DueWorkSchedule.project_id == project_id,
                DueWorkSchedule.handler_key == DOCUMENT_NOTIFICATION_HANDLER,
                DueWorkSchedule.disabled_at.is_(None),
            )
        )
    return schedule is not None


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

    rows = session.execute(
        select(DocumentNotification, DocumentNotificationDispatch)
        .join(
            DocumentNotificationDispatch,
            DocumentNotificationDispatch.notification_id == DocumentNotification.id,
        )
        .where(
            DocumentNotification.project_id == project_id,
            DocumentNotification.recipient_principal_subject == principal_subject,
        )
        .order_by(DocumentNotification.id.desc())
    ).all()
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

    rows = session.execute(
        select(DocumentNotification, DocumentNotificationDispatch)
        .join(
            DocumentNotificationDispatch,
            DocumentNotificationDispatch.notification_id == DocumentNotification.id,
        )
        .where(DocumentNotification.project_id == project_id)
        .order_by(DocumentNotification.id.desc())
    ).all()
    counts = {
        state: 0
        for state in ("queued", "completed", "retry_due", "failed", "uncertain")
    }
    deliveries = []
    for notification, dispatch in rows:
        counts[dispatch.delivery_state] = counts.get(dispatch.delivery_state, 0) + 1
        deliveries.append(
            {
                "notification_id": notification.id,
                "category": notification.category,
                "subject_label": _subject_label(session, notification),
                "recipient": notification.recipient_principal_subject,
                "recipient_role": notification.recipient_role,
                "delivery_state": dispatch.delivery_state,
                "delivery_limitation": dispatch.delivery_limitation,
                "attempt_count": dispatch.attempt_count,
                "last_error_code": dispatch.last_error_code,
            }
        )
    enabled = (
        session.scalar(
            select(DueWorkSchedule.id).where(
                DueWorkSchedule.project_id == project_id,
                DueWorkSchedule.handler_key == DOCUMENT_NOTIFICATION_HANDLER,
                DueWorkSchedule.disabled_at.is_(None),
            )
        )
        is not None
    )
    return {"delivery_enabled": enabled, "counts": counts, "deliveries": deliveries}


# --- Small shared helpers -------------------------------------------------


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DocumentNotificationRefusal(
            "a notification clock must supply an aware datetime"
        )
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()


_sha256 = digests.canonical_sha256
