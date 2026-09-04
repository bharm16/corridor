"""The pilot chase list: contact-ready follow-up bundles from accepted authority.

A chase list is the one place where Corridor stops describing the record and
starts telling a coordinator to go and ask somebody something.  That is exactly
why it is the easiest surface in the product to get wrong, and #425 exists to
fix the wrong version before it ships.

**The rule the module is built around.**  An unresolved Proposed Delta alone
creates no chase item.  A source that disagrees with the accepted record is
incoming evidence, not a second accepted fact (ADR-0076), and turning it into
an external obligation would have Corridor telephone a utility about a value
nobody has accepted — on the strength of a spreadsheet cell that may be a typo.
So an incoming difference reaches this list only after a person has recorded a
**Follow-up Plan** through the coordination-needed outcome of #526: the exact
open question, the responsible person or organization, the return date, the
affected scope, and the evidence.  A dated internal **Defer** is not that.  It
is a person saying "not this week", it writes a scheduling receipt and never a
plan (``delta_review_packet_children`` enforces one identity per outcome), and
it stays out of external follow-up entirely.  ``_plan_items`` therefore reads
``project_workflow.outstanding_follow_up`` and never ``proposed_deltas``; the
open, deferred, rejected and stale sets are not inputs to this module at all,
which is the only construction under which "no plan, no chase item" cannot
quietly stop being true.

**The accepted position is composed here, from the frozen projection.**  A
bundle may *quote* what an incoming source says — that is often the whole point
of the ask, "your 15 December letter says X, our record says Y, which holds?" —
but a quotation is a separate, attributed field that can never be read as the
record.  Every accepted line is built from ``read_project_record_as_of_revision``
against the one accepted revision the reading is bound to, and
``_validate_bundle`` refuses a bundle whose accepted line does not match that
projection exactly.  An unaccepted value cannot reach a customer through this
module without that validator being deleted.

**"No response" is not an empty inbox.**  ADR-0090 retired the legacy ``STALE``
alert for precisely this reason: nobody sending a document is not evidence that
anyone failed to answer, and the rule fired just as hard on a record that was
correctly quiet.  A no-response item here requires a *retained outgoing
request* — the request itself, the moment it went, and the expected-response
boundary it declared — and Corridor retains none today, so
``read_retained_outgoing_requests`` truthfully returns nothing and the band is
empty in production.  The predicate is implemented, versioned and covered by
fixtures rather than left for later, because the temptation to approximate it
from silence is the whole failure mode.  ``no movement`` is likewise an exact
elapsed-time predicate and never a mood: it is the number of whole days between
a plan's own recorded date and the declared cutoff.

**Ordering is bands, never a score.**  ADR-0010 abolished the severity scalar
and ADR-0035 fixed ordering groups in its place; ``review_packet_reading``
already applies that to Proposed Deltas and this module applies it to follow-up.
Six bands, each with **one** declared quantity and a sentence saying why it is a
band; a bundle takes the highest-consequence band of its items; ties break on
declared identity strings.  No quantity is ever compared across bands and
nothing anywhere multiplies a band by a day count.  A bundle in a
higher-consequence band precedes every bundle below it whatever the numbers say,
and a test asserts that, because a "score" is exactly what re-appears when
somebody wants one list sorted "properly".

**Bundling follows the interaction a coordinator can perform**, not the shape of
the data: one recipient, one coherent ask, one compatible due window.  Several
Utility Conflicts needing the same answer from the same organization in the same
week are one telephone call and one bundle.  One External Organization owing
answers about different things, or by different dates, is several bundles, which
is why the ask kind and the due window are in the key.  Channel joins the key
only where it materially changes the interaction — that is, only where a contact
actually resolved to an address, since a bundle addressed to a role has no
channel to differ on.

**No bundle is suppressed for want of an address** (#425, amended 2026-09-02).
Where the accepted record carries an organization contact the bundle names the
resolved individual and channel; where it does not, the bundle names the
responsible role and says plainly that the contact is unresolved.  A coordinator
who knows the address is not helped by Corridor hiding the ask.

**No clock.**  ``as_of`` is the cutoff the caller declares, exactly as every
reading under it requires, so a screen and its test agree about what "overdue"
means.  This module writes nothing, needs no table, and adds no fact type: every
bundle is derived from records that already exist.  ``emit_follow_up_reading``
is the one thing that leaves it, and it leaves as a #558 measurement event
rather than a row: being shown a chase list is not an act, so there is no
receipt for having looked, and the event carries the reading's own digest so
what a coordinator saw can be rebuilt from the records.

Terminology: nothing here coins a customer word.  Promised For, Required By,
Utility Conflict, Follow-up Plan, External Organization, Source Discrepancy and
Attention Reason are the adopted glossary's, and ``bundle`` stays the internal
technical name it is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
    default_binding,
    emit_event,
)
from corridor.models import (
    BaselineSourceRow,
    DeltaFollowUpPlan,
    DeltaFollowUpPlanEvidence,
    Project,
    ProposedDelta,
)
from corridor.presentation import field_label
from corridor.project_workflow import FollowUpNeed, outstanding_follow_up
from corridor.record_projection import (
    CurrentRecordValue,
    read_project_record_as_of_revision,
)
from corridor.review_packet_reading import (
    SOURCE_CONTRADICTION_BAND,
    read_open_deltas,
)


# The released rule set.  Trigger, bundling and ordering are versioned as one
# unit because they are replayed as one: a reading rebuilt under a different
# ordering rule is a different reading even when the same asks come out of it.
FOLLOW_UP_RULE = "follow_up_bundles_from_accepted_authority"
FOLLOW_UP_RULE_VERSION = "v1"
FOLLOW_UP_RULE_SET = f"{FOLLOW_UP_RULE}_{FOLLOW_UP_RULE_VERSION}"
SUPPORTED_RULE_VERSIONS = frozenset({FOLLOW_UP_RULE_VERSION})

# How far ahead of the cutoff an accepted commitment date is worth an ask.
# Declared here, versioned with the rule set, and never read from a clock.
APPROACHING_COMMITMENT_HORIZON_DAYS = 21

# How a due window is computed.  Two asks share a window when their dates fall
# in the same ISO week: "both answers are needed the week of the 9th" is an
# interaction a coordinator can actually perform in one sitting, and it is
# reproducible from the dates alone.
DUE_WINDOW_RULE = "iso_week_of_due_date"
NO_DUE_WINDOW = "no_date"

# The accepted date fields an external ask can be raised about, and the one
# deliberately excluded.  ``action_due_date`` is the project's own internal
# Action Due Date; ADR-0090 retired the alerts that asserted every conflict
# owes an internal task, and an internal date is not something to chase an
# External Organization about.
COMMITMENT_FIELDS: tuple[str, ...] = ("committed_date", "need_date")
EXCLUDED_INTERNAL_DATE_FIELDS: tuple[str, ...] = ("action_due_date",)

# The accepted facts that say who the ask goes to.
RESPONSIBLE_ORGANIZATION_FIELD = "external_org"
ORGANIZATION_CONTACT_FIELD = "external_org_contact"

# Contact state (#562 amendment).  A bundle is never suppressed for want of an
# address; it says which of these two it is.
CONTACT_RESOLVED = "resolved"
CONTACT_UNRESOLVED = "unresolved_contact"
CHANNEL_EMAIL = "email"

# --- consequence bands ------------------------------------------------------

PAST_DUE_COMMITMENT_BAND = "past_due_commitment"
OVERDUE_ANSWER_BAND = "overdue_answer"
UNANSWERED_REQUEST_BAND = "unanswered_request"
APPROACHING_COMMITMENT_BAND = "approaching_commitment"
UNBOUNDED_ANSWER_BAND = "unbounded_answer"
AWAITING_ANSWER_BAND = "awaiting_answer"

ELAPSED = "elapsed"
REMAINING = "remaining"


@dataclass(frozen=True, slots=True)
class ConsequenceBand:
    """One auditable band, its single quantity, and why it is a band.

    ``quantity`` names the one number the band is ordered by. ``direction``
    says which end of that number is more consequential: an ``elapsed`` band
    puts the largest first, a ``remaining`` band the smallest. Nothing
    combines a band with a quantity, and no quantity is compared across bands.
    """

    name: str
    ordinal: int
    quantity: str
    direction: str
    sentence: str


CONSEQUENCE_BANDS: tuple[ConsequenceBand, ...] = (
    ConsequenceBand(
        name=PAST_DUE_COMMITMENT_BAND,
        ordinal=1,
        quantity="days_past_accepted_date",
        direction=ELAPSED,
        sentence=(
            "a date the accepted record holds has already passed, so the "
            "project is working from an obligation nobody has restated"
        ),
    ),
    ConsequenceBand(
        name=OVERDUE_ANSWER_BAND,
        ordinal=2,
        quantity="days_past_return_date",
        direction=ELAPSED,
        sentence=(
            "a person recorded the date this question would come back and "
            "that date has passed with the question still open"
        ),
    ),
    ConsequenceBand(
        name=UNANSWERED_REQUEST_BAND,
        ordinal=3,
        quantity="days_past_expected_response",
        direction=ELAPSED,
        sentence=(
            "a request Corridor retained passed the response boundary it "
            "declared, so there is a specific thing somebody has not answered"
        ),
    ),
    ConsequenceBand(
        name=APPROACHING_COMMITMENT_BAND,
        ordinal=4,
        quantity="days_until_accepted_date",
        direction=REMAINING,
        sentence=(
            "a date the accepted record holds falls inside the declared "
            "horizon, so confirming it now is still cheaper than missing it"
        ),
    ),
    ConsequenceBand(
        name=UNBOUNDED_ANSWER_BAND,
        ordinal=5,
        quantity="days_since_plan_recorded",
        direction=ELAPSED,
        sentence=(
            "the plan named no return date, so the only exact thing that can "
            "be said is how long it has been since it was recorded"
        ),
    ),
    ConsequenceBand(
        name=AWAITING_ANSWER_BAND,
        ordinal=6,
        quantity="days_until_return_date",
        direction=REMAINING,
        sentence=(
            "the answer is not late yet; the date the plan named has not "
            "arrived"
        ),
    ),
)

BANDS_BY_NAME: Mapping[str, ConsequenceBand] = {
    band.name: band for band in CONSEQUENCE_BANDS
}

# The bands that mean "there is something for the coordinator to do about this
# now", as against "somebody was asked and the date they named has not come".
# Every band above is in here except ``AWAITING_ANSWER_BAND``, whose own
# sentence is that the answer is not late yet: a plan inside its own return
# window is *waiting*, and telling a coordinator to chase it would be telling
# them to chase a person who is not late. ``UNBOUNDED_ANSWER_BAND`` is in,
# because a plan that named no return date has no date that will ever make it
# due — if it is not actionable now it is never actionable, and the ordering
# above already puts it above the awaiting band for exactly that reason.
ACTIONABLE_BANDS: frozenset[str] = frozenset(
    band.name for band in CONSEQUENCE_BANDS if band.name != AWAITING_ANSWER_BAND
)

BAND_ORDINALS: Mapping[str, int] = {
    band.name: band.ordinal for band in CONSEQUENCE_BANDS
}

# The exact predicate behind the phrase "no movement", stated once so a screen
# and a test cannot mean different things by it.
NO_MOVEMENT_RULE = "days_since_plan_recorded"
NO_MOVEMENT_SENTENCE = (
    "No movement is the exact number of whole days between the date the "
    "Follow-up Plan was recorded and the declared cutoff. It is never "
    "inferred from an absence of documents or mail."
)

# --- asks -------------------------------------------------------------------

ASK_CONFIRM_ACCEPTED_DATE = "confirm_accepted_date"
ASK_ANSWER_OPEN_QUESTION = "answer_open_question"
ASK_RESOLVE_SOURCE_DISCREPANCY = "resolve_source_discrepancy"
ASK_ANSWER_RETAINED_REQUEST = "answer_retained_request"

ASK_NAMES: Mapping[str, str] = {
    ASK_CONFIRM_ACCEPTED_DATE: "Confirm the date our record holds",
    ASK_ANSWER_OPEN_QUESTION: "Answer an open question",
    ASK_RESOLVE_SOURCE_DISCREPANCY: "Resolve a Source Discrepancy",
    ASK_ANSWER_RETAINED_REQUEST: "Answer a request we already sent",
}


class FollowUpBundleRefused(ValueError):
    """A bundle cannot be built, or cannot honestly be stated as asked."""


# --- what the reading returns ----------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One retained record a reader can go back to."""

    kind: str
    identity: str
    detail: str


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who the ask goes to, resolved as far as the record allows (#562)."""

    organization: str
    responsible_role: str
    contact_state: str
    contact_name: str | None = None
    channel: str | None = None

    @property
    def key(self) -> str:
        """The identity two items must share to be the same interaction."""

        if self.contact_state == CONTACT_RESOLVED:
            return f"contact:{self.channel or 'unknown'}:{self.contact_name}"
        return f"role:{self.organization}"

    def sentence(self) -> str:
        if self.contact_state == CONTACT_RESOLVED:
            channel = f" by {self.channel}" if self.channel else ""
            return f"{self.contact_name} at {self.organization}{channel}"
        return (
            f"{self.responsible_role} at {self.organization} — no contact is "
            "recorded for this organization in the accepted record"
        )


@dataclass(frozen=True, slots=True)
class AcceptedPositionLine:
    """One accepted value, as the frozen projection holds it.

    There is deliberately nowhere here to put a proposed value. The text comes
    from the projection and ``_validate_bundle`` checks it against the
    projection again before the bundle leaves this module.
    """

    subject_identity: str
    business_identity: str
    field: str
    field_name: str
    accepted_text: str
    decision_id: int
    fact_id: int
    revision_id: int


@dataclass(frozen=True, slots=True)
class QuotedWording:
    """Source wording quoted as the proposition being confirmed, never as fact.

    ADR-0084 forbids an unaccepted value being stated as the record's. A quote
    carries its attribution and its source reference so a reader can always
    tell who said it, and it is rendered beside the accepted position rather
    than in place of it.
    """

    text: str
    attribution: str
    reference: SourceReference


@dataclass(frozen=True, slots=True)
class RetainedOutgoingRequest:
    """A request Corridor retained, and the response boundary it declared.

    This is the only thing that can produce a no-response item. Corridor
    retains no outgoing correspondence today, so
    ``read_retained_outgoing_requests`` returns nothing and no production
    reading contains this band. The type exists because the predicate is the
    point: without a retained request and a declared boundary there is no
    no-response fact to state, only an empty inbox.
    """

    request_identity: str
    organization: str
    subject_identities: tuple[str, ...]
    question: str
    sent_on: date
    expected_response_by: date
    reference: SourceReference


@dataclass(frozen=True, slots=True)
class FollowUpItem:
    """One trigger's output, before bundling."""

    trigger: str
    band: str
    quantity: int
    ask: str
    recipient: Recipient
    due_date: date | None
    subject_identity: str
    question: str
    accepted_lines: tuple[AcceptedPositionLine, ...]
    quoted: tuple[QuotedWording, ...]
    references: tuple[SourceReference, ...]
    identity: str


@dataclass(frozen=True, slots=True)
class FollowUpBundle:
    """One interaction a coordinator can perform, contact-ready.

    "Contact-ready" is the amended ticket's list: a copy-ready subject, the
    exact ask, the current accepted position, the affected Utility Conflicts,
    the due or return date, the supporting source references, and the resolved
    recipient or the responsible role. It is deliberately not a drafted email:
    sending, delivery, receipt tracking and escalation are out of the pilot.
    """

    ordinal: int
    bundle_key: str
    ask: str
    ask_name: str
    recipient: Recipient
    band: str
    quantity_name: str
    quantity: int
    due_window: str
    due_date: date | None
    subject_line: str
    ask_sentence: str
    accepted_position: tuple[AcceptedPositionLine, ...]
    quoted_wording: tuple[QuotedWording, ...]
    open_questions: tuple[str, ...]
    affected_conflicts: tuple[str, ...]
    references: tuple[SourceReference, ...]
    item_identities: tuple[str, ...]
    rule_set: str = FOLLOW_UP_RULE_SET


@dataclass(frozen=True, slots=True)
class FollowUpReading:
    """One project's chase list at one declared cutoff."""

    project_id: int
    cutoff: datetime
    accepted_revision_id: int | None
    rule: str
    rule_version: str
    rule_set: str
    horizon_days: int
    due_window_rule: str
    no_movement_rule: str
    bands: tuple[ConsequenceBand, ...]
    bundles: tuple[FollowUpBundle, ...]
    retained_outgoing_requests: int
    reading_identity: str = ""

    @property
    def cutoff_date(self) -> date:
        return self.cutoff.date()


# --- the retained-request port ---------------------------------------------


def read_retained_outgoing_requests(
    session: Session, *, project_id: int, as_of: datetime
) -> tuple[RetainedOutgoingRequest, ...]:
    """Every outgoing request Corridor retained for this project: none, today.

    Corridor holds inbound deliveries (``source_deliveries``,
    ``inbound_messages``) and it holds what people decided, but it retains no
    outgoing correspondence and no declared expected-response boundary. There
    is therefore no source from which a no-response fact could honestly be
    read, and this returns empty rather than approximating one from silence —
    which is the exact substitution ADR-0090 retired ``STALE`` for making.

    It is a function rather than a constant so the no-response rule above has a
    real input to be exercised with, and so the seam that will one day retain a
    sent request has one obvious place to arrive.
    """

    return ()


# --- the reading ------------------------------------------------------------


def read_follow_up_bundles(
    session: Session,
    *,
    project_id: int,
    as_of: datetime,
    rule_version: str = FOLLOW_UP_RULE_VERSION,
    outgoing_requests: Sequence[RetainedOutgoingRequest] | None = None,
) -> FollowUpReading:
    """The pilot chase list for one project at one cutoff.

    ``as_of`` is declared by the caller and is the only time this reading
    knows. Two readings of the same records at the same cutoff produce the
    same bundles in the same order with the same ``reading_identity``.
    """

    if as_of.tzinfo is None:
        raise FollowUpBundleRefused(
            "a chase list is bound to an aware cutoff its caller declared"
        )
    if rule_version not in SUPPORTED_RULE_VERSIONS:
        raise FollowUpBundleRefused(
            f"{rule_version!r} is not a follow-up rule version this reading "
            f"implements; one of {', '.join(sorted(SUPPORTED_RULE_VERSIONS))}"
        )

    requests = tuple(
        outgoing_requests
        if outgoing_requests is not None
        else read_retained_outgoing_requests(
            session, project_id=project_id, as_of=as_of
        )
    )
    for request in requests:
        _validate_outgoing_request(request)

    reading = read_open_deltas(session, project_id=project_id, as_of=as_of)
    accepted_revision_id = reading.accepted_revision_id
    if accepted_revision_id is None:
        return _empty(project_id, as_of, None, len(requests))

    projection = _index_projection(
        read_project_record_as_of_revision(
            session, project_id, accepted_revision_id
        )
    )
    subjects = tuple(projection)
    identities = _business_identities(session, project_id, subjects)
    project_name = _project_name(session, project_id)
    today = as_of.date()

    needs = outstanding_follow_up(
        session, project_id=project_id, open_delta_ids=reading.open_delta_ids
    )
    discrepancies = frozenset(
        standing.delta_id
        for standing in reading.standings
        if SOURCE_CONTRADICTION_BAND in standing.attention_reasons
    )

    items: list[FollowUpItem] = []
    items.extend(
        _commitment_items(projection, identities, today=today)
    )
    items.extend(
        _plan_items(
            session,
            project_id=project_id,
            needs=needs,
            projection=projection,
            identities=identities,
            discrepancies=discrepancies,
            today=today,
        )
    )
    items.extend(
        _retained_request_items(
            requests, projection, identities, today=today
        )
    )

    bundles = _bundle(items, project_name=project_name, today=today)
    for bundle in bundles:
        _validate_bundle(bundle, projection)

    made = FollowUpReading(
        project_id=project_id,
        cutoff=as_of,
        accepted_revision_id=accepted_revision_id,
        rule=FOLLOW_UP_RULE,
        rule_version=rule_version,
        rule_set=FOLLOW_UP_RULE_SET,
        horizon_days=APPROACHING_COMMITMENT_HORIZON_DAYS,
        due_window_rule=DUE_WINDOW_RULE,
        no_movement_rule=NO_MOVEMENT_RULE,
        bands=CONSEQUENCE_BANDS,
        bundles=bundles,
        retained_outgoing_requests=len(requests),
    )
    return _with_identity(made)


def _empty(
    project_id: int,
    as_of: datetime,
    accepted_revision_id: int | None,
    requests: int,
) -> FollowUpReading:
    return _with_identity(
        FollowUpReading(
            project_id=project_id,
            cutoff=as_of,
            accepted_revision_id=accepted_revision_id,
            rule=FOLLOW_UP_RULE,
            rule_version=FOLLOW_UP_RULE_VERSION,
            rule_set=FOLLOW_UP_RULE_SET,
            horizon_days=APPROACHING_COMMITMENT_HORIZON_DAYS,
            due_window_rule=DUE_WINDOW_RULE,
            no_movement_rule=NO_MOVEMENT_RULE,
            bands=CONSEQUENCE_BANDS,
            bundles=(),
            retained_outgoing_requests=requests,
        )
    )


# --- triggers ---------------------------------------------------------------


def _commitment_items(
    projection: Mapping[str, Mapping[str, CurrentRecordValue]],
    identities: Mapping[str, str],
    *,
    today: date,
) -> tuple[FollowUpItem, ...]:
    """Asks raised by a date the accepted record already holds.

    The only inputs are accepted values. A subject with no accepted External
    Organization raises nothing: there is no external party to ask, and
    ADR-0090 retired the rules that asserted every Utility Conflict owes
    somebody a task.
    """

    made: list[FollowUpItem] = []
    for subject_identity in sorted(projection):
        values = projection[subject_identity]
        organization = values.get(RESPONSIBLE_ORGANIZATION_FIELD)
        if organization is None or not (organization.text_value or "").strip():
            continue
        recipient = _recipient(
            organization.text_value or "",
            values.get(ORGANIZATION_CONTACT_FIELD),
        )
        for field_name in COMMITMENT_FIELDS:
            value = values.get(field_name)
            if value is None or value.date_value is None:
                continue
            due = value.date_value
            elapsed = (today - due).days
            if elapsed > 0:
                band, quantity = PAST_DUE_COMMITMENT_BAND, elapsed
            elif -elapsed <= APPROACHING_COMMITMENT_HORIZON_DAYS:
                band, quantity = APPROACHING_COMMITMENT_BAND, -elapsed
            else:
                continue
            line = _accepted_line(
                subject_identity, identities, field_name, value
            )
            made.append(
                FollowUpItem(
                    trigger="accepted_date",
                    band=band,
                    quantity=quantity,
                    ask=ASK_CONFIRM_ACCEPTED_DATE,
                    recipient=recipient,
                    due_date=due,
                    subject_identity=subject_identity,
                    question=(
                        f"Does {field_label(field_name)} {due.isoformat()} "
                        f"still hold for {line.business_identity}?"
                    ),
                    accepted_lines=(line,),
                    quoted=(),
                    references=(),
                    identity=(
                        f"accepted_date:{subject_identity}:{field_name}:"
                        f"{value.decision_id}"
                    ),
                )
            )
    return tuple(made)


def plan_band(*, return_date: date | None, today: date) -> str:
    """Which consequence band one Follow-up Plan's own dates put it in.

    ``_plan_items`` is the only place this was written and it is still the only
    place it is decided; it is lifted out so a reading that already holds the
    plans — the cross-project portfolio (#537, #636) holds them for every
    project it shows — can ask which of them are actionable without rebuilding
    a whole chase list per project, and without a second opinion about where
    the line between "due" and "waiting" falls.
    """

    if return_date is None:
        return UNBOUNDED_ANSWER_BAND
    if return_date < today:
        return OVERDUE_ANSWER_BAND
    return AWAITING_ANSWER_BAND


def actionable_plan_count(needs: Sequence[FollowUpNeed], *, today: date) -> int:
    """How many live Follow-up Plans are due for coordinator action, not waiting.

    The bundling above turns items into interactions; this counts the plans
    that would reach an actionable band once they got there. It is deliberately
    plan-derived only: the commitment and retained-request triggers need the
    whole accepted projection of every project, which no bounded cross-project
    reading can afford, so a caller that needs those asks
    ``read_follow_up_bundles`` for the one project it is looking at.
    """

    return sum(
        1
        for need in needs
        if plan_band(return_date=need.return_date, today=today)
        in ACTIONABLE_BANDS
    )


def _plan_items(
    session: Session,
    *,
    project_id: int,
    needs: Sequence[FollowUpNeed],
    projection: Mapping[str, Mapping[str, CurrentRecordValue]],
    identities: Mapping[str, str],
    discrepancies: frozenset[int],
    today: date,
) -> tuple[FollowUpItem, ...]:
    """Asks a person authorized by recording a Follow-up Plan.

    ``outstanding_follow_up`` is the single authority for which plans are still
    live, and it is the *only* delta-derived input this module has. Nothing
    here reads ``proposed_deltas`` to decide whether an item exists — an open
    delta with no plan is invisible to this function by construction, and a
    dated Defer writes a scheduling receipt rather than a plan, so it is
    invisible for the same reason.
    """

    if not needs:
        return ()
    plans = {
        plan.id: plan
        for plan in session.scalars(
            select(DeltaFollowUpPlan).where(
                DeltaFollowUpPlan.project_id == project_id,
                DeltaFollowUpPlan.id.in_(tuple(need.plan_id for need in needs)),
            )
        )
    }
    deltas = {
        delta.id: delta
        for delta in session.scalars(
            select(ProposedDelta).where(
                ProposedDelta.project_id == project_id,
                ProposedDelta.id.in_(tuple(need.delta_id for need in needs)),
            )
        )
    }
    evidence = _plan_evidence(session, project_id, tuple(plans))

    made: list[FollowUpItem] = []
    for need in needs:
        plan = plans.get(need.plan_id)
        delta = deltas.get(need.delta_id)
        if plan is None or delta is None:
            continue
        organization = (plan.responsible_organization or "").strip()
        subject_identity = str(
            (plan.affected_scope or {}).get("subject_identity")
            or delta.target_subject_identity
        )
        values = projection.get(subject_identity, {})
        accepted_org = values.get(RESPONSIBLE_ORGANIZATION_FIELD)
        if not organization and accepted_org is not None:
            organization = (accepted_org.text_value or "").strip()
        recipient = _recipient(
            organization or (plan.responsible_principal or "").strip(),
            values.get(ORGANIZATION_CONTACT_FIELD),
            responsible_role=(plan.responsible_principal or "").strip() or None,
        )

        return_date = plan.return_date.date() if plan.return_date else None
        band = plan_band(return_date=return_date, today=today)
        if band == UNBOUNDED_ANSWER_BAND:
            quantity = (today - plan.recorded_at.date()).days
        elif band == OVERDUE_ANSWER_BAND:
            quantity = (today - return_date).days
        else:
            quantity = (return_date - today).days

        field_name = str((plan.affected_scope or {}).get("field") or "") or (
            delta.target_field or ""
        )
        accepted_lines = ()
        value = values.get(field_name)
        if value is not None:
            accepted_lines = (
                _accepted_line(subject_identity, identities, field_name, value),
            )

        quoted = _quoted_wording(session, delta)
        references = tuple(
            SourceReference(
                kind="support_assessment",
                identity=str(assessment_id),
                detail="evidence the Follow-up Plan cited",
            )
            for assessment_id in evidence.get(plan.id, ())
        )
        made.append(
            FollowUpItem(
                trigger="follow_up_plan",
                band=band,
                quantity=quantity,
                ask=(
                    ASK_RESOLVE_SOURCE_DISCREPANCY
                    if need.delta_id in discrepancies
                    else ASK_ANSWER_OPEN_QUESTION
                ),
                recipient=recipient,
                due_date=return_date,
                subject_identity=subject_identity,
                question=plan.open_question,
                accepted_lines=accepted_lines,
                quoted=quoted,
                references=references + tuple(
                    quote.reference for quote in quoted
                ),
                identity=f"follow_up_plan:{plan.id}",
            )
        )
    return tuple(made)


def _retained_request_items(
    requests: Sequence[RetainedOutgoingRequest],
    projection: Mapping[str, Mapping[str, CurrentRecordValue]],
    identities: Mapping[str, str],
    *,
    today: date,
) -> tuple[FollowUpItem, ...]:
    """No-response asks, and only from a retained request past its boundary.

    Both halves are required. A retained request whose expected-response
    boundary has not arrived is not a no-response; an absence of incoming mail
    with no retained request behind it is not one either, and never becomes
    one here.
    """

    made: list[FollowUpItem] = []
    for request in requests:
        elapsed = (today - request.expected_response_by).days
        if elapsed <= 0:
            continue
        subject_identity = (
            request.subject_identities[0] if request.subject_identities else ""
        )
        values = projection.get(subject_identity, {})
        organization = request.organization
        recipient = _recipient(
            organization, values.get(ORGANIZATION_CONTACT_FIELD)
        )
        lines = tuple(
            _accepted_line(identity, identities, field_name, value)
            for identity in request.subject_identities
            for field_name in COMMITMENT_FIELDS
            for value in (projection.get(identity, {}).get(field_name),)
            if value is not None and value.date_value is not None
        )
        made.append(
            FollowUpItem(
                trigger="retained_outgoing_request",
                band=UNANSWERED_REQUEST_BAND,
                quantity=elapsed,
                ask=ASK_ANSWER_RETAINED_REQUEST,
                recipient=recipient,
                due_date=request.expected_response_by,
                subject_identity=subject_identity,
                question=request.question,
                accepted_lines=lines,
                quoted=(),
                references=(request.reference,),
                identity=f"retained_outgoing_request:{request.request_identity}",
            )
        )
    return tuple(made)


# --- bundling ---------------------------------------------------------------


def _bundle(
    items: Sequence[FollowUpItem], *, project_name: str, today: date
) -> tuple[FollowUpBundle, ...]:
    """Group items into the interactions a coordinator can actually perform.

    The key is recipient, ask, and due window. Channel is inside the recipient
    key only where a contact actually resolved, which is the only case where it
    changes the interaction: a bundle addressed to a role has no channel to
    differ on.
    """

    grouped: dict[tuple[str, str, str], list[FollowUpItem]] = defaultdict(list)
    for item in items:
        grouped[
            (item.recipient.key, item.ask, _due_window(item.due_date))
        ].append(item)

    built: list[FollowUpBundle] = []
    for (recipient_key, ask, window), members in grouped.items():
        members = sorted(members, key=lambda item: item.identity)
        band = min(members, key=lambda item: BAND_ORDINALS[item.band]).band
        declared = BANDS_BY_NAME[band]
        in_band = [item for item in members if item.band == band]
        quantity = (
            max(item.quantity for item in in_band)
            if declared.direction == ELAPSED
            else min(item.quantity for item in in_band)
        )
        dates = [item.due_date for item in members if item.due_date is not None]
        due_date = min(dates) if dates else None
        lines = _unique_lines(members)
        conflicts = tuple(
            sorted({line.business_identity for line in lines})
        ) or tuple(
            sorted({item.subject_identity for item in members})
        )
        recipient = members[0].recipient
        built.append(
            FollowUpBundle(
                ordinal=0,
                bundle_key=f"{recipient_key}|{ask}|{window}",
                ask=ask,
                ask_name=ASK_NAMES[ask],
                recipient=recipient,
                band=band,
                quantity_name=declared.quantity,
                quantity=quantity,
                due_window=window,
                due_date=due_date,
                subject_line=_subject_line(
                    project_name, ask, conflicts, due_date
                ),
                ask_sentence=_ask_sentence(ask, members, due_date),
                accepted_position=lines,
                quoted_wording=_unique_quotes(members),
                open_questions=tuple(
                    dict.fromkeys(item.question for item in members)
                ),
                affected_conflicts=conflicts,
                references=_unique_references(members),
                item_identities=tuple(item.identity for item in members),
            )
        )

    built.sort(key=_order_key)
    return tuple(
        FollowUpBundle(**{**_as_kwargs(bundle), "ordinal": ordinal})
        for ordinal, bundle in enumerate(built, start=1)
    )


def _order_key(bundle: FollowUpBundle) -> tuple[Any, ...]:
    """Band first, then that band's own quantity, then declared identity.

    There is no score here on purpose. The band ordinal is compared before
    anything else, so a bundle in a higher-consequence band precedes every
    bundle below it whatever the day counts are; the quantity is only ever
    compared against another quantity of the same band; and the remaining
    components are declared strings, so two readings of the same state produce
    the same order.
    """

    declared = BANDS_BY_NAME[bundle.band]
    within = (
        -bundle.quantity if declared.direction == ELAPSED else bundle.quantity
    )
    return (
        declared.ordinal,
        within,
        bundle.due_date.isoformat() if bundle.due_date else "9999-12-31",
        bundle.recipient.key,
        bundle.ask,
        bundle.bundle_key,
    )


def _due_window(due: date | None) -> str:
    if due is None:
        return NO_DUE_WINDOW
    year, week, _ = due.isocalendar()
    return f"{year}-W{week:02d}"


# --- honesty guards ---------------------------------------------------------


def _validate_bundle(
    bundle: FollowUpBundle,
    projection: Mapping[str, Mapping[str, CurrentRecordValue]],
) -> None:
    """Refuse a bundle that would state something the record does not hold.

    Three things are checked, and each of them is a way a chase list lies.
    An accepted line must match the frozen projection exactly, so no proposed
    value can be presented as the record's position. A quotation must carry an
    attribution and a source reference, so no source wording can appear
    unattributed and be read as the record. And every bundle must name a
    recipient — the role at minimum, never nothing.
    """

    for line in bundle.accepted_position:
        value = projection.get(line.subject_identity, {}).get(line.field)
        if value is None:
            raise FollowUpBundleRefused(
                f"{line.subject_identity} has no accepted {line.field} in the "
                "revision this reading is bound to, so the bundle cannot "
                "state one as the accepted position"
            )
        if _projected_text(value) != line.accepted_text:
            raise FollowUpBundleRefused(
                f"the accepted position stated for {line.subject_identity} "
                f"{line.field} is not the accepted value; a bundle may quote "
                "an incoming value but never present it as the record's"
            )
    for quote in bundle.quoted_wording:
        if not quote.attribution.strip() or not quote.reference.identity.strip():
            raise FollowUpBundleRefused(
                "quoted source wording carries its attribution and the source "
                "it came from; an unattributed quotation reads as the record"
            )
    if not bundle.recipient.organization.strip():
        raise FollowUpBundleRefused(
            "a bundle names the resolved contact or the responsible role; it "
            "is never suppressed for want of an address, and never nameless"
        )


def _validate_outgoing_request(request: RetainedOutgoingRequest) -> None:
    """A no-response input is a retained request with a declared boundary."""

    if not request.request_identity.strip() or not request.reference.identity.strip():
        raise FollowUpBundleRefused(
            "a no-response item needs the retained outgoing request itself, "
            "not an absence of incoming mail"
        )
    if request.expected_response_by < request.sent_on:
        raise FollowUpBundleRefused(
            "an expected-response boundary falls on or after the day the "
            "request went out"
        )


# --- composition ------------------------------------------------------------


def _recipient(
    organization: str,
    contact: CurrentRecordValue | None,
    *,
    responsible_role: str | None = None,
) -> Recipient:
    """Resolve as far as the accepted record allows, and say which it was."""

    name = (contact.text_value or "").strip() if contact is not None else ""
    role = responsible_role or "the responsible contact"
    if not name:
        return Recipient(
            organization=organization,
            responsible_role=role,
            contact_state=CONTACT_UNRESOLVED,
        )
    return Recipient(
        organization=organization,
        responsible_role=role,
        contact_state=CONTACT_RESOLVED,
        contact_name=name,
        channel=CHANNEL_EMAIL if "@" in name else None,
    )


def _accepted_line(
    subject_identity: str,
    identities: Mapping[str, str],
    field_name: str,
    value: CurrentRecordValue,
) -> AcceptedPositionLine:
    return AcceptedPositionLine(
        subject_identity=subject_identity,
        business_identity=identities.get(subject_identity, subject_identity),
        field=field_name,
        field_name=field_label(field_name),
        accepted_text=_projected_text(value),
        decision_id=value.decision_id,
        fact_id=value.fact_id,
        revision_id=value.revision_id,
    )


def _projected_text(value: CurrentRecordValue) -> str:
    if value.date_value is not None:
        return value.date_value.isoformat()
    return (value.text_value or "").strip()


def _quoted_wording(
    session: Session, delta: ProposedDelta
) -> tuple[QuotedWording, ...]:
    """What the incoming source said, quoted and attributed, never asserted."""

    proposed = delta.proposed_value
    if proposed is None:
        return ()
    text = proposed if isinstance(proposed, str) else json.dumps(
        proposed, sort_keys=True
    )
    return (
        QuotedWording(
            text=text,
            attribution=(
                f"as stated in the {delta.source_family} source revision "
                f"{delta.source_revision}; this is what the source says and "
                "not a value the Project Record holds"
            ),
            reference=SourceReference(
                kind="proposed_delta",
                identity=str(delta.id),
                detail=f"{delta.source_family} {delta.source_revision}",
            ),
        ),
    )


def _subject_line(
    project_name: str,
    ask: str,
    conflicts: Sequence[str],
    due: date | None,
) -> str:
    """A subject a coordinator can paste, naming the project and the ask."""

    count = len(conflicts)
    noun = "Utility Conflict" if count == 1 else "Utility Conflicts"
    by = f" — needed by {due.isoformat()}" if due is not None else ""
    return f"{project_name}: {ASK_NAMES[ask]} ({count} {noun}){by}"


def _ask_sentence(
    ask: str, members: Sequence[FollowUpItem], due: date | None
) -> str:
    by = (
        f" We need your answer by {due.isoformat()}."
        if due is not None
        else " No return date has been recorded for this question."
    )
    if ask == ASK_CONFIRM_ACCEPTED_DATE:
        return (
            "Please confirm the dates our Project Record currently holds for "
            "the Utility Conflicts listed below, or tell us the date you now "
            f"hold instead.{by}"
        )
    if ask == ASK_RESOLVE_SOURCE_DISCREPANCY:
        return (
            "Two sources we hold say different things about the same value, "
            "so this is a Source Discrepancy. The accepted position and the "
            "quoted source wording are both shown below; please tell us which "
            f"one your organization holds to.{by}"
        )
    if ask == ASK_ANSWER_RETAINED_REQUEST:
        return (
            "We sent this request and the response date we asked for has "
            f"passed. The question is repeated below unchanged.{by}"
        )
    return (
        "A coordinator recorded a Follow-up Plan for the question below and "
        f"named your organization as the party who can answer it.{by}"
    )


def _unique_lines(
    members: Sequence[FollowUpItem],
) -> tuple[AcceptedPositionLine, ...]:
    seen: dict[tuple[str, str], AcceptedPositionLine] = {}
    for item in members:
        for line in item.accepted_lines:
            seen.setdefault((line.subject_identity, line.field), line)
    return tuple(seen[key] for key in sorted(seen))


def _unique_quotes(
    members: Sequence[FollowUpItem],
) -> tuple[QuotedWording, ...]:
    seen: dict[tuple[str, str], QuotedWording] = {}
    for item in members:
        for quote in item.quoted:
            seen.setdefault((quote.reference.identity, quote.text), quote)
    return tuple(seen[key] for key in sorted(seen))


def _unique_references(
    members: Sequence[FollowUpItem],
) -> tuple[SourceReference, ...]:
    seen: dict[tuple[str, str], SourceReference] = {}
    for item in members:
        for reference in item.references:
            seen.setdefault((reference.kind, reference.identity), reference)
    return tuple(seen[key] for key in sorted(seen))


# --- inputs -----------------------------------------------------------------


def _index_projection(
    values: Sequence[CurrentRecordValue],
) -> dict[str, dict[str, CurrentRecordValue]]:
    indexed: dict[str, dict[str, CurrentRecordValue]] = defaultdict(dict)
    for value in values:
        indexed[value.subject_key][value.fact_type] = value
    return dict(indexed)


def _business_identities(
    session: Session, project_id: int, subjects: Sequence[str]
) -> dict[str, str]:
    if not subjects:
        return {}
    rows = session.scalars(
        select(BaselineSourceRow).where(
            BaselineSourceRow.project_id == project_id,
            BaselineSourceRow.record_subject_key.in_(tuple(subjects)),
        )
    ).all()
    return {row.record_subject_key: row.business_identity for row in rows}


def _plan_evidence(
    session: Session, project_id: int, plan_ids: Sequence[int]
) -> dict[int, tuple[int, ...]]:
    if not plan_ids:
        return {}
    rows = session.execute(
        select(
            DeltaFollowUpPlanEvidence.plan_id,
            DeltaFollowUpPlanEvidence.support_assessment_id,
        )
        .where(
            DeltaFollowUpPlanEvidence.project_id == project_id,
            DeltaFollowUpPlanEvidence.plan_id.in_(tuple(plan_ids)),
        )
        .order_by(
            DeltaFollowUpPlanEvidence.plan_id,
            DeltaFollowUpPlanEvidence.ordinal,
        )
    ).all()
    found: dict[int, list[int]] = defaultdict(list)
    for plan_id, assessment_id in rows:
        found[plan_id].append(assessment_id)
    return {plan_id: tuple(ids) for plan_id, ids in found.items()}


def _project_name(session: Session, project_id: int) -> str:
    name = session.scalar(select(Project.name).where(Project.id == project_id))
    return name or f"Project {project_id}"


# --- the replayable payload -------------------------------------------------


def bundle_reading_payload(reading: FollowUpReading) -> dict[str, Any]:
    """The chase list as one JSON-safe mapping, for #534 and the release.

    A consumer takes this unchanged: the declared rule set, the cutoff, the
    accepted revision it was read against, every band with its own quantity
    and the sentence that justifies it, and the bundles in the order the rule
    produced. ``content_sha256`` is over everything but itself, so replaying a
    reading and comparing digests proves the rules are deterministic without
    comparing rendered bytes.
    """

    body: dict[str, Any] = {
        "payload_schema_version": "follow-up-bundles-v1",
        "rule": reading.rule,
        "rule_version": reading.rule_version,
        "rule_set": reading.rule_set,
        "project_id": reading.project_id,
        "cutoff": reading.cutoff.isoformat(),
        "accepted_revision_id": reading.accepted_revision_id,
        "horizon_days": reading.horizon_days,
        "due_window_rule": reading.due_window_rule,
        "no_movement_rule": reading.no_movement_rule,
        "no_movement_sentence": NO_MOVEMENT_SENTENCE,
        "retained_outgoing_requests": reading.retained_outgoing_requests,
        "bands": [
            {
                "name": band.name,
                "ordinal": band.ordinal,
                "quantity": band.quantity,
                "direction": band.direction,
                "sentence": band.sentence,
            }
            for band in reading.bands
        ],
        "bundles": [_bundle_payload(bundle) for bundle in reading.bundles],
    }
    body["content_sha256"] = sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _bundle_payload(bundle: FollowUpBundle) -> dict[str, Any]:
    return {
        "ordinal": bundle.ordinal,
        "bundle_key": bundle.bundle_key,
        "ask": bundle.ask,
        "ask_name": bundle.ask_name,
        "band": bundle.band,
        "quantity_name": bundle.quantity_name,
        "quantity": bundle.quantity,
        "due_window": bundle.due_window,
        "due_date": bundle.due_date.isoformat() if bundle.due_date else None,
        "subject_line": bundle.subject_line,
        "ask_sentence": bundle.ask_sentence,
        "recipient": {
            "organization": bundle.recipient.organization,
            "responsible_role": bundle.recipient.responsible_role,
            "contact_state": bundle.recipient.contact_state,
            "contact_name": bundle.recipient.contact_name,
            "channel": bundle.recipient.channel,
            "sentence": bundle.recipient.sentence(),
        },
        "accepted_position": [
            {
                "subject_identity": line.subject_identity,
                "business_identity": line.business_identity,
                "field": line.field,
                "field_name": line.field_name,
                "accepted_text": line.accepted_text,
                "decision_id": line.decision_id,
                "fact_id": line.fact_id,
                "revision_id": line.revision_id,
            }
            for line in bundle.accepted_position
        ],
        "quoted_wording": [
            {
                "text": quote.text,
                "attribution": quote.attribution,
                "reference": {
                    "kind": quote.reference.kind,
                    "identity": quote.reference.identity,
                    "detail": quote.reference.detail,
                },
            }
            for quote in bundle.quoted_wording
        ],
        "open_questions": list(bundle.open_questions),
        "affected_conflicts": list(bundle.affected_conflicts),
        "references": [
            {
                "kind": reference.kind,
                "identity": reference.identity,
                "detail": reference.detail,
            }
            for reference in bundle.references
        ],
        "item_identities": list(bundle.item_identities),
    }


def _as_kwargs(bundle: FollowUpBundle) -> dict[str, Any]:
    return {
        name: getattr(bundle, name)
        for name in FollowUpBundle.__dataclass_fields__
    }


def _with_identity(reading: FollowUpReading) -> FollowUpReading:
    payload = bundle_reading_payload(reading)
    return FollowUpReading(
        **{
            name: getattr(reading, name)
            for name in FollowUpReading.__dataclass_fields__
            if name != "reading_identity"
        },
        reading_identity=payload["content_sha256"],
    )


# --- the measurement event this reading owes the contract (#558) ------------
#
# One presentation event per reading, emitted where the portfolio's is (#537):
# beside the reading it describes, so a second surface cannot invent a second
# shape for the same fact. It carries the reading's own ``reading_identity``
# digest, the cutoff it was bound to, and a count per band, which is enough to
# rebuild exactly what a coordinator was shown from the records themselves.
#
# Two things are deliberately absent. There is no derivable database receipt,
# because the chase list stores nothing and being shown it is not an act. And
# the retained-outgoing-request count travels in the payload, so a reading that
# claims a no-response band while retaining no request is visible in the
# measurement record and not only in the screen's own guard.


def emit_follow_up_reading(
    reading: FollowUpReading,
    *,
    principal_subject: str,
    surface: str,
    binding: AnalyticsBinding | None = None,
) -> None:
    """Record that this chase list was presented, at its own declared cutoff."""

    counted: dict[str, int] = {band.name: 0 for band in reading.bands}
    for bundle in reading.bundles:
        counted[bundle.band] += 1
    emit_event(
        AnalyticsEvent(
            family=EventFamily.FOLLOW_UP_READING,
            binding=binding or default_binding(),
            occurred_at=reading.cutoff,
            payload={
                "principal_subject": principal_subject,
                "project_id": reading.project_id,
                "cutoff": reading.cutoff.isoformat(),
                "rule_set": reading.rule_set,
                "accepted_revision_id": reading.accepted_revision_id,
                "reading_identity": reading.reading_identity,
                "bundle_count": len(reading.bundles),
                "retained_outgoing_requests": reading.retained_outgoing_requests,
                "bundles_by_band": counted,
            },
            # Bounded shape only: the project and the person stay in the
            # payload, never in an infrastructure label (#491, #522).
            metric_labels={"surface": surface, "status": "presented"},
        )
    )
