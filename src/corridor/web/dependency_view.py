"""One Constraint's record screen, as its own domain readers already answer it.

The Constraint detail screen is the oldest and largest of the legacy screens,
and its handler had grown into a second opinion about the record. The clearest
case: it read every history assessment and derived the stale-agreement field set
itself, spelling the outcome as a literal —

    amendment_fields = {f for f, a in all_assessments.items()
                        if a.outcome == "contractual_amendment"}

— then subtracted that set from the Disputes and re-sliced the assessments
against the remainder. That rule now has a name at its owner,
``disputes.assessed_amendment_field_names``, beside the retained-resolution
sibling ``contractual_amendment_field_names`` that ``work_list`` reads, and
``disputes`` says there why the two answer different questions: one speaks as
soon as the chronology makes an executed agreement stale, the other only once a
resolution has been retained. Two rules about amendment work that read as one
was the actual hazard; naming both at the owner is what makes the difference
visible instead of accidental.

**Every rule here belongs to a domain reader.** ``ledger.load_dependency`` owns
the record; ``disputes`` owns which fields are contested and which carry a
stale executed agreement; ``dispute_timeline`` owns the chronology;
``documentation_checklist`` and ``condition_tracking`` own the documentation
review and its condition proposals; ``operative_support`` owns which evidence
is sufficient; ``work_decisions`` owns the current owner, Next Action, deferral
and Follow-up Plan receipt; ``support_update_routing`` owns the routed
consequence of ineligible replacement support. This module reads them once each
and holds nothing they do not say.

**One roster, one order.** The screen used to issue the same active-roster query
twice per request, ordered by ``(display_name, id)`` in one and by
``display_name`` alone in the other, and hand both to the same
``internal_owner_roster_entry_id`` control on different forms. Two orders for
one list of people is a difference nobody chose: with two members sharing a
display name the two forms offered them in different orders, so which person a
coordinator assigned depended on which form they used.
``active_project_roster`` is now the one reading, with the one total order.

**An optional explanation is an adjunct, never a precondition.** The
deterministic routed question and the retained before/after change context
stand on their own whether or not a bounded explanation exists, is stale, or
was refused (#360).

**No clock.** Nothing here reads the day; a screen that names a date past its
mark is given the day by its caller.

Terminology: nothing here coins a customer word. Constraint, Dispute,
Documentation Review, Follow-up Plan, Next Action and Assigned To come from the
adopted glossary through ``corridor.presentation`` and the readers below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.adjudicate import DISMISS_REASONS
from corridor.condition_tracking import propose_condition_clears
from corridor.dispute_timeline import build_dispute_timeline
from corridor.disputes import (
    assessed_amendment_field_names,
    disputes_for,
    history_assessments_for,
)
from corridor.documentation_checklist import read_checklist
from corridor.ledger import load_dependency
from corridor.models import ProjectRosterEntry
from corridor.operative_support import resolve_operative_support
from corridor.revision_change_explanation import (
    PROMPT_VERSION as REVISION_CHANGE_EXPLANATION_PROMPT_VERSION,
    current_configuration as current_revision_change_configuration,
    explanation_binding as revision_change_explanation_binding,
    latest_revision_change_explanation,
)
from corridor.support_update_routing import (
    changed_source_context,
    customer_consequences_by_dependency,
)
from corridor.work_decisions import (
    CANCELLATION_REASONS,
    DEFERRAL_REASONS,
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    NO_FOLLOW_UP_REASONS,
    UNKNOWN_DUE_DATE_REASONS,
    current_deferral_decision,
    current_follow_up_plan_receipt,
    current_internal_owner_decision,
    current_next_action_decision,
)


class NoSuchConstraint(LookupError):
    """This project holds no Constraint by that identifier."""


def active_project_roster(
    session: Session, project_id: int
) -> tuple[ProjectRosterEntry, ...]:
    """The active project-team members a Follow-up Plan may assign, in one order.

    ``display_name`` then ``id``: the name is what a coordinator reads and the
    identifier breaks the tie, so two people with one display name are always
    offered in the same order on every form that assigns work.
    """
    return tuple(
        session.scalars(
            select(ProjectRosterEntry)
            .where(
                ProjectRosterEntry.project_id == project_id,
                ProjectRosterEntry.active.is_(True),
            )
            .order_by(ProjectRosterEntry.display_name, ProjectRosterEntry.id)
        )
    )


@dataclass(frozen=True, slots=True)
class PlanForm:
    """The vocabulary the Follow-up Plan controls offer, and nothing derived.

    Every sequence is a declared reason set from ``work_decisions``, sorted for
    a stable rendering. A screen may not add a reason, and this is where a new
    one would have to appear to be offered.
    """

    roster: tuple[ProjectRosterEntry, ...]
    next_action_choices: Sequence[str]
    unknown_date_reasons: tuple[str, ...]
    no_follow_up_reasons: tuple[str, ...]
    cancellation_reasons: tuple[str, ...]
    deferral_reasons: tuple[str, ...]
    expected_internal_owner_decision_id: object = ""
    expected_next_action_decision_id: object = ""


def plan_form(session: Session, project_id: int, dependency_id: int) -> PlanForm:
    """The plan controls for one Constraint, bound to the decisions now current."""
    owner_decision = current_internal_owner_decision(session, dependency_id)
    action_decision = current_next_action_decision(session, dependency_id)
    return PlanForm(
        roster=active_project_roster(session, project_id),
        next_action_choices=FOLLOW_UP_NEXT_ACTION_CHOICES,
        unknown_date_reasons=tuple(sorted(UNKNOWN_DUE_DATE_REASONS)),
        no_follow_up_reasons=tuple(sorted(NO_FOLLOW_UP_REASONS)),
        cancellation_reasons=tuple(sorted(CANCELLATION_REASONS)),
        deferral_reasons=tuple(sorted(DEFERRAL_REASONS)),
        expected_internal_owner_decision_id=(
            owner_decision.id if owner_decision else ""
        ),
        expected_next_action_decision_id=(
            action_decision.id if action_decision else ""
        ),
    )


@dataclass(frozen=True, slots=True)
class DependencyView:
    """One Constraint: its record, its contested fields, and its plan controls."""

    project_id: int
    dependency_id: int
    view: Any
    owner_decision: Any
    action_decision: Any
    deferral_decision: Any
    plan: PlanForm
    plan_receipt: Any
    checklist: Any
    condition_proposals: Sequence[Any]
    sufficient_evidence_ids: frozenset[int]
    disputes: Mapping[str, Any]
    dispute_assessments: Mapping[str, Any]
    dispute_amendments: Mapping[str, Any]
    dispute_timelines: Mapping[str, Any]
    support_consequence: Any
    support_context: Any
    revision_change_binding: Any
    revision_change_explanation: Any
    revision_change_configuration: Any
    dismiss_reasons: Sequence[str] = DISMISS_REASONS
    revision_change_prompt_version: str = REVISION_CHANGE_EXPLANATION_PROMPT_VERSION

    @property
    def dependency(self) -> Any:
        """The Constraint record itself, for a caller that needs only it."""
        return self.view.dependency

    @property
    def settleable_field_names(self) -> tuple[str, ...]:
        """The contested fields that carry a settle control, in reading order.

        A field whose stale executed agreement is the current outcome is not
        here: that is coordination work with a why-line and both quotations,
        and pressing "settle" would record a decision the amendment has not
        made.
        """
        return tuple(self.disputes)


def dependency_view(
    session: Session, *, project_id: int, dependency_id: int
) -> DependencyView:
    """Read one Constraint of one project, or refuse to read someone else's."""

    try:
        view = load_dependency(session, dependency_id)
    except LookupError as error:
        raise NoSuchConstraint("no such constraint") from error
    if view.dependency.project_id != project_id:
        raise NoSuchConstraint("no such constraint in this project")

    support = resolve_operative_support(session, (dependency_id,))[dependency_id]
    checklist = read_checklist(
        session, dependency_id, legacy_ready=support.current_readiness != ()
    )

    all_assessments = {
        assessment.field_name: assessment
        for assessment in history_assessments_for(session, dependency_id)
    }
    # The one authority on which of these assessments is a stale executed
    # agreement. `disputes` also holds the retained-resolution sibling the Work
    # List reads, and says there why the two are different questions.
    amendment_fields = assessed_amendment_field_names(all_assessments.values())
    all_disputes = {
        dispute.field_name: dispute
        for dispute in disputes_for(session, dependency_id, include_settled=True)
    }
    disputes = {
        field_name: dispute
        for field_name, dispute in all_disputes.items()
        if field_name not in amendment_fields
    }
    # A stale executed agreement is coordination work, not a pick-one card: the
    # why-line and both quotations render without any settle control.
    amendments = {
        field_name: all_assessments[field_name]
        for field_name in amendment_fields
        if field_name in all_disputes and field_name in all_assessments
    }

    support_consequence = customer_consequences_by_dependency(session, project_id).get(
        dependency_id
    )
    support_context = (
        changed_source_context(session, support_consequence)
        if support_consequence is not None
        else None
    )
    # An optional, read-only explanation of the exact verified comparison the
    # question already selected (#360). The binding and any retained receipt are
    # an adjunct: the deterministic question and the change context stand on
    # their own whether or not one exists, is stale, or was refused.
    binding = (
        revision_change_explanation_binding(
            session, project_id=project_id, dependency_id=dependency_id
        )
        if support_context is not None
        else None
    )
    return DependencyView(
        project_id=project_id,
        dependency_id=dependency_id,
        view=view,
        owner_decision=current_internal_owner_decision(session, dependency_id),
        action_decision=current_next_action_decision(session, dependency_id),
        deferral_decision=current_deferral_decision(session, dependency_id),
        plan=plan_form(session, project_id, dependency_id),
        plan_receipt=current_follow_up_plan_receipt(session, dependency_id),
        checklist=checklist,
        condition_proposals=propose_condition_clears(
            session, dependency_id, checklist.conditions
        ),
        sufficient_evidence_ids=frozenset(
            item.evidence_link_id for item in support.readiness
        ),
        disputes=disputes,
        dispute_assessments={
            field_name: assessment
            for field_name, assessment in all_assessments.items()
            if field_name in disputes
        },
        dispute_amendments=amendments,
        dispute_timelines={
            field_name: build_dispute_timeline(session, dependency_id, field_name)
            for field_name in disputes
        },
        support_consequence=support_consequence,
        support_context=support_context,
        revision_change_binding=binding,
        revision_change_explanation=(
            latest_revision_change_explanation(
                session,
                project_id=project_id,
                dependency_id=dependency_id,
                comparison_id=binding.comparison_id,
                finding_id=binding.finding_id,
            )
            if binding is not None
            else None
        ),
        revision_change_configuration=current_revision_change_configuration(
            session, project_id
        ),
    )
