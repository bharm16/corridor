"""The derived reading that places every open Proposed Delta in one item (#494).

ADR-0085 splits the adopted-project Work List in two.  ``review_packets`` owns
the one attributable **act** a coordinator commits (#526).  This module owns the
other half: the **reading** the act is committed over — the open and current
delta queries, the partition rule that decides which deltas are decided
together, the auditable consequence bands, and the deterministic exits.  It
ships no screen.  #559 supplies the shared UI primitives, #527 renders the
source-revision batch, and #528 the focused cross-source question; both read
what is here and neither re-derives it.

**The one guarantee.**  Every Proposed Delta of a project holds exactly one
standing, and every actionable one is offered by exactly one item — including a
one-delta fallback for anything that cannot safely join a batch.  Nothing
disappears from the accounting and nothing is offered twice, so two saves can
never race for the same delta and no reference elsewhere (the Record, source
history, a warning, an audit view) can grow a second decision control.  The
partition is a total function of the stored delta set, the declared cutoff, and
the rule version, so two readings of the same state rebuild the same items in
the same order.

**Why a delta leaves the actionable set by rule, never by judgement.**  Four
exits are deterministic and each is derived, never stored:

``resolved``     a semantic disposition settled it (#519);
``superseded``   a newer occurrence in the same lineage replaced it (#518);
``stale``        the accepted value it was compared against has since moved, so
                 the comparison no longer describes the record and #519 would
                 refuse the decision anyway; and
``deferred``     ADR-0084 Work List scheduling — the delta is still **open**,
                 still counted here, and simply hidden until its dated return or
                 a wake condition.

**A deferral that ended says which release ended it (#835).**  Until a
coordinator could see deferred work at all, "it came back" was the whole
account a returned delta gave of itself.  ``standing_sets`` decides the
release, so it also names it: the return instant the schedule recorded has
arrived, ADR-0084's wake condition fired, or a person ended the schedule
deliberately — a receipt whose return instant is at or before its own
``deferred_at``, which is what opening an item early writes.  The reason
travels on the actionable standing as ``DeferralReturn``; ``returns_at`` and
``wake_condition`` stay the *held* delta's schedule, so nothing has to read
one field and guess which of the two questions it answered.

**No clock takes part.**  ``as_of`` is a cutoff the caller declares.  A dated
deferral is judged against it because both sides are domain times a person
chose; nothing here compares a PostgreSQL-assigned ``created_at`` against a
logical time, which is the mistake #488 was rebuilt to remove.  "A newer source
version arrived" is answered from append-only ``proposed_deltas.id`` order, not
from timestamps.

**Why the reading is not ``live_delta_status``.**  ``delta_resolution``
deliberately answers whether an act on a delta is *lawful*, and that answer must
not change with the hour, so it calls a delta with any unreversed deferral
receipt ``deferred`` however old the return date is.  Whether the delta is
*visible* this week is this module's question, and it needs the cutoff.  A test
asserts the two never disagree about what has actually left: nothing this
reading offers is resolved or superseded, and nothing resolved or superseded is
ever offered.

**Consequence bands are ADR-0035's groups, not a score.**  ADR-0076 fixed the
bands as ADR-0035's ordering groups, ADR-0083 gave `apparent removal` its own
band, and ADR-0010 forbids a numeric severity anywhere.  A delta carries every
Attention Reason that applies and its band is the highest-consequence one, which
is ADR-0035's own rule for an item with several reasons.  ADR-0035's second
group — relocation work with no assigned person — is standing record state
rather than a proposed difference, so no delta joins it; it is not silently
folded into another band.  The three **visible** levels of ADR-0085 are
still not derived here: they are a projection of a difference onto the next
issue's declared content (ADR-0086, ADR-0091), which is per-project
configuration this module has no business reading.  ``consequence_levels``
performs that projection over ``issue_content``'s resolution of it, and
``packet_review`` attaches the result to the child it belongs to (#641).  The
bands below are unchanged by that: a level is the heading the reasons are
grouped under, never a replacement for one.

**What was tried before, and rejected.**  Keying every packet by source
revision destroys the cross-source case, because the documents that disagree
arrive separately; keying everything by coordination question destroys the
batch case, because one revision's thirty exact changes become thirty
questions.  ADR-0085 settled that the key is selected per packet, so the rule
here selects it and records which key it chose.  Storing the partition was
rejected by ADR-0085 as well: it would be a second authoritative record,
stale the moment a source arrived mid-review.

**What ``delta-partition-v2`` added (#528).**  Three things, each a split or a
join the earlier rule could not express:

*The shared commitment.*  ADR-0085's third key kind.  One attributable
External Party Statement that moves several Utility Conflicts is one decision,
not one per conflict, and the link is the spine's own ``delta_groups``
``statement_id`` rather than any resemblance between values.  A statement whose
Applies To is *not yet known* is not keyed this way at all: unknown scope is
current record state, never a confirmation question (ADR-0035, ADR-0039), and a
scope nobody settled cannot bound a decision.

*The materially different action.*  Two sources bearing on the same subject and
field do not always ask one question.  One that edits the value and one that
says the row is gone ask two, and answering either leaves the other open, so
they stop being one item.  Each still appears on the other's item as read-only
context, because "all the passages together" is the point of consolidating and
a split is not a reason to hide one.

*The identity question.*  Sources disagreeing about which utility a row **is**
are not offering two candidate values; they are asking a question that has to
be settled before any value recorded against that row means anything.  It stays
one coordination question and says so, so a screen names it as an identity
question rather than as a value question.

The rule version is now checked rather than merely recorded.  It used to be a
label a caller could set to anything while v1's body ran regardless, which
produced a receipt claiming a rule that never ran.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import refusals
from corridor.delta_resolution import (
    DEFERRED,
    ORGANIZATION_FIELDS,
    RESOLVED,
    SCHEDULE_KEY_DATE_FIELDS,
    STALE,
    TIMING_FIELDS,
    ChildDecisionRequest,
)
from corridor.models import (
    DeltaDeferral,
    DeltaDisposition,
    DeltaGroup,
    DeltaReviewPacketChild,
    DeltaReviewPacketReversal,
    DeltaSupersession,
    ExternalPartyStatement,
    FactDecision,
    ProjectRecordRevision,
    ProposedDelta,
)
from corridor.principals import HumanPrincipal
from corridor.review_packets import PacketChildRequest, ReviewPacketRequest


# The rule this reading partitions under.  It is recorded on every item and on
# every packet act built from one, so a later change to the rule is visible in
# the receipt rather than retroactive.
#
# ``v2`` adds #528's cross-source consolidation: the shared-commitment key, the
# split of a candidate question whose sources propose materially different
# actions, and the identity question that has to be settled before any value
# question about the same row can be.
PARTITION_RULE_VERSION = "delta-partition-v2"

# Only a rule this module actually implements may be recorded on an item.  The
# version was previously a label a caller could set to anything while v1's body
# ran regardless, which is a receipt saying a rule was applied that never was.
SUPPORTED_RULE_VERSIONS = frozenset({PARTITION_RULE_VERSION})

# Standings.  ``resolved``, ``deferred``, and ``stale`` keep #519's spelling so
# one word never means two things across the seam.
ACTIONABLE = "actionable"
SUPERSEDED = "superseded"
STANDINGS = (ACTIONABLE, DEFERRED, STALE, SUPERSEDED, RESOLVED)

# Why a scheduled Proposed Delta is back in immediate work (ADR-0084, #835).
# A deferral holds a delta out until *something* happens, and until #835 the
# something was never named: the delta simply reappeared.  These are the three
# ways ``standing_sets`` below actually releases one, so a screen states the
# reason the rule used rather than guessing at one.
RETURNED_DATE_REACHED = "return_date_reached"
RETURNED_WAKE_CONDITION = "wake_condition_met"
RETURNED_OPENED_EARLY = "opened_early"
RETURN_REASONS = (
    RETURNED_DATE_REACHED,
    RETURNED_WAKE_CONDITION,
    RETURNED_OPENED_EARLY,
)

# ADR-0085's packet keys, all three of which this rule version produces.
SOURCE_REVISION = "source_revision"
COORDINATION_QUESTION = "coordination_question"
SHARED_COMMITMENT = "shared_commitment"
PRODUCED_KEY_KINDS = (SOURCE_REVISION, COORDINATION_QUESTION, SHARED_COMMITMENT)

# Scope lives in its own satellite and a scope change is never an exact cell
# comparison, so it is decided on its own item.
SCOPE_FIELDS = frozenset({"applies_to"})
DATE_FIELDS = frozenset(TIMING_FIELDS | SCHEDULE_KEY_DATE_FIELDS)

# The fields that say *which* real utility a row is, rather than what is true
# of it.  Two sources disagreeing here is not a value question with two
# candidate answers: until the coordinator knows which utility the row is, no
# answer about its dates or its owner means anything.  ADR-0085 allows a
# coordination question to be keyed by the real question, and this is one.
IDENTITY_FIELDS = frozenset({"utility_id"})

# The statement scope modes that say a commitment's Applies To is settled *and*
# enumerable.  ``unknown`` is current Project Record state rather than a
# question to ask (ADR-0035, ADR-0039), so a statement whose scope is not
# settled is never presented as one bounded decision over several Utility
# Conflicts; its deltas fall through to the ordinary passes and keep their own
# items.
#
# ``all_active`` and ``carried_forward`` are deliberately excluded even though
# they are settled modes.  #528 requires the item to enumerate the *complete*
# scope, and those two name a population rather than a list — an item keyed by
# one of them would print "these are the conflicts inside it" over a set nobody
# wrote down.  A commitment at one of those modes keeps its ordinary items
# until that population can be enumerated from the record.
EXPLICIT_SCOPE_MODES = frozenset({"selected"})

# Consequence bands, in presentation order.  Each names the ADR-0035 group it
# projects, or ``None`` where ADR-0083 gave it a band of its own.
PAST_DUE_COMMITMENT_BAND = "past_due_commitment"
PROMISED_TIMING_CHANGE_BAND = "promised_timing_change"
SOURCE_CONTRADICTION_BAND = "source_contradiction"
APPARENT_REMOVAL_BAND = "apparent_removal"
UNPLACED_SUBJECT_BAND = "unplaced_subject"
RECORD_CLEANUP_BAND = "record_cleanup"


@dataclass(frozen=True, slots=True)
class ConsequenceBand:
    """One auditable band, and the sentence that explains why it is one."""

    name: str
    ordinal: int
    work_list_group: int | None
    reason: str


CONSEQUENCE_BANDS: tuple[ConsequenceBand, ...] = (
    ConsequenceBand(
        name=PAST_DUE_COMMITMENT_BAND,
        ordinal=1,
        work_list_group=1,
        reason=(
            "the promise or Key Date this delta concerns has already elapsed as "
            "of the reading's cutoff"
        ),
    ),
    ConsequenceBand(
        name=PROMISED_TIMING_CHANGE_BAND,
        ordinal=2,
        work_list_group=3,
        reason="the delta moves a Promised For, action date, or Required By",
    ),
    ConsequenceBand(
        name=SOURCE_CONTRADICTION_BAND,
        ordinal=3,
        work_list_group=3,
        reason=(
            "two retained sources give different answers for the same subject "
            "and field"
        ),
    ),
    ConsequenceBand(
        name=APPARENT_REMOVAL_BAND,
        ordinal=4,
        work_list_group=None,
        reason=(
            "a complete enumerative revision no longer contains the row, which "
            "fails differently from an edited value (ADR-0083)"
        ),
    ),
    ConsequenceBand(
        name=UNPLACED_SUBJECT_BAND,
        ordinal=5,
        work_list_group=4,
        reason="the source proposes a subject the accepted record does not hold",
    ),
    ConsequenceBand(
        name=RECORD_CLEANUP_BAND,
        ordinal=6,
        work_list_group=5,
        reason="an exact field change with no timing, identity, or removal risk",
    ),
)
BAND_ORDINALS: Mapping[str, int] = {
    band.name: band.ordinal for band in CONSEQUENCE_BANDS
}

# Why one delta could not join its revision's batch.  The vocabulary is #527's
# own list; a reason this rule version does not yet detect is absent rather
# than stubbed, so a reading never claims a check it did not make.
HELD_OUT_APPARENT_REMOVAL = "apparent_removal"
HELD_OUT_POSSIBLE_NEW_CONFLICT = "possible_new_conflict"
HELD_OUT_OWNER_MISMATCH = "owner_mismatch"
HELD_OUT_UNCERTAIN_SCOPE = "uncertain_scope"
# Two sources bear on the same subject and field but do not propose the same
# *kind* of change — one edits the value while the other says the row is gone.
# ADR-0085's packet is the smallest set that can be decided at once, and these
# cannot: answering one does not answer the other.
HELD_OUT_DIFFERENT_ACTION = "different_action"

# Why this item is keyed the way it is.  ADR-0085 requires the selected key to
# be shown, and #528 requires the grouping rule to be explainable from stored
# identities; a token plus its own sentence is that explanation, recorded on
# the item rather than reconstructed by a screen.
KEY_SOURCE_REVISION_BATCH = "source_revision_batch"
KEY_HELD_OUT_OF_BATCH = "held_out_of_batch"
KEY_CONTRADICTED_VALUE = "contradicted_value"
KEY_CONTRADICTED_IDENTITY = "contradicted_identity"
KEY_SHARED_COMMITMENT_SCOPE = "shared_commitment_scope"
KEY_REASONS: Mapping[str, str] = {
    KEY_SOURCE_REVISION_BATCH: (
        "one authoritative source revision proposed these changes together and "
        "they fail the same way, so they are decided together"
    ),
    KEY_HELD_OUT_OF_BATCH: (
        "this change cannot be decided with the rest of its source revision, so "
        "it keeps its own item"
    ),
    KEY_CONTRADICTED_VALUE: (
        "two retained sources answer the same field differently, so the "
        "disagreement is the decision"
    ),
    KEY_CONTRADICTED_IDENTITY: (
        "two retained sources disagree about which utility this row is, which "
        "has to be settled before any value recorded against it means anything"
    ),
    KEY_SHARED_COMMITMENT_SCOPE: (
        "one attributable commitment states its own Applies To scope, and these "
        "are the Utility Conflicts inside it"
    ),
}


class ReviewPacketReadingRefused(refusals.Refusal, ValueError):
    """A caller cannot build this reading, or cannot act on it as asked.

    One class covers both halves of ADR-0085's Work List, because the
    exactly-once rule is one rule: a delta an item does not offer is refused the
    same way whether the screen noticed it or the binding did.  ``packet_review``
    exports it under its own screen-facing name, and the review routes catch
    that name, so two classes meant a refusal from the binding reached a route
    that was only prepared for the screen's.

    A refusal about one child's own answer carries which child and which
    control, so the screen can link the message to the input that holds it
    without deciding anything a second time.  The rule lives in these modules;
    the screen only renders what they said.
    """

    refusal_kind = refusals.CONFLICT

    def __init__(
        self,
        message: str,
        *,
        delta_id: int | None = None,
        control: str | None = None,
    ) -> None:
        super().__init__(message)
        self.delta_id = delta_id
        self.control = control


# --- what the reading returns ---------------------------------------------


@dataclass(frozen=True, slots=True)
class DeferralReturn:
    """Why a scheduled Proposed Delta is back in immediate work (#835).

    The deferral receipt that was in force when the rule released the delta,
    plus which of ``RETURN_REASONS`` released it.  Every value is read off
    that one receipt, so a screen naming the date or the wake condition names
    what the coordinator actually recorded rather than a sentence composed
    from somewhere else.
    """

    reason: str
    scheduled_by: str
    scheduled_at: datetime
    returns_at: datetime | None
    wake_condition: str | None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DeltaStanding:
    """Where one Proposed Delta stands, and why."""

    delta_id: int
    standing: str
    band: str | None = None
    attention_reasons: tuple[str, ...] = ()
    item_key: str | None = None
    returns_at: datetime | None = None
    wake_condition: str | None = None
    superseded_by_delta_id: int | None = None
    baseline_revision: int | None = None
    current_accepted_revision_id: int | None = None
    # The attributable commitment this delta came from, where its statement
    # declared its own Applies To scope.  Recorded on the standing so a
    # presentation can name a commitment's whole scope — including the
    # conflicts that left it for their own item — without asking the partition
    # a second time.
    commitment_key: str | None = None
    # Set only on an actionable delta that had been scheduled and came back
    # (#835).  ``returns_at`` and ``wake_condition`` above stay the *held*
    # delta's schedule, so a reader asking "when does this return" and one
    # asking "why is this here" cannot be answered by the same field.
    returned: DeferralReturn | None = None


@dataclass(frozen=True, slots=True)
class ActionableItem:
    """One bounded set of deltas a coordinator can decide at once."""

    ordinal: int
    item_key: str
    grouping_key_kind: str
    grouping_key: str
    grouping_rule_version: str
    band: str
    attention_reasons: tuple[str, ...]
    delta_ids: tuple[int, ...]
    held_out_reason: str | None = None
    key_reason: str = KEY_SOURCE_REVISION_BATCH

    @property
    def key_sentence(self) -> str:
        """Why the rule keyed this item this way, in one sentence."""

        return KEY_REASONS[self.key_reason]

    @property
    def offer_identity(self) -> tuple[Any, ...]:
        """Everything the partition decided about this item, in one value.

        A presentation layer extends this item rather than wrapping it, so "is
        this the item the reading offered?" cannot be answered by comparing
        objects that also carry a headline and a customer's words.  It is
        answered here, over the partition's own fields alone.
        """

        return tuple(getattr(self, field.name) for field in fields(ActionableItem))

    def as_fields(self) -> dict[str, Any]:
        """This item's own fields, for a layer that adds presentation to them.

        Deliberately unnamed one by one: ``packet_review`` copies whatever the
        partition decided without spelling a grouping field itself, so the rule
        that only this module decides grouping survives the extension.
        """

        return {field.name: getattr(self, field.name) for field in fields(ActionableItem)}


@dataclass(frozen=True, slots=True)
class DeltaReading:
    """One project's open Proposed Deltas, partitioned exactly once."""

    project_id: int
    rule_version: str
    as_of: datetime
    accepted_revision_id: int | None
    items: tuple[ActionableItem, ...]
    standings: tuple[DeltaStanding, ...]

    @property
    def actionable_delta_ids(self) -> tuple[int, ...]:
        return tuple(
            standing.delta_id
            for standing in self.standings
            if standing.standing == ACTIONABLE
        )

    @property
    def deferred(self) -> tuple[DeltaStanding, ...]:
        return self._with_standing(DEFERRED)

    @property
    def stale(self) -> tuple[DeltaStanding, ...]:
        return self._with_standing(STALE)

    @property
    def superseded(self) -> tuple[DeltaStanding, ...]:
        return self._with_standing(SUPERSEDED)

    @property
    def resolved(self) -> tuple[DeltaStanding, ...]:
        return self._with_standing(RESOLVED)

    @property
    def open_delta_ids(self) -> tuple[int, ...]:
        """Open under ADR-0083: not resolved and not superseded."""

        return tuple(
            standing.delta_id
            for standing in self.standings
            if standing.standing in (ACTIONABLE, DEFERRED, STALE)
        )

    def item_for(self, delta_id: int) -> ActionableItem | None:
        """The one item that offers this delta, or ``None`` if none does."""

        for item in self.items:
            if delta_id in item.delta_ids:
                return item
        return None

    def _with_standing(self, standing: str) -> tuple[DeltaStanding, ...]:
        return tuple(row for row in self.standings if row.standing == standing)


# --- the queries ----------------------------------------------------------


def current_deltas(
    session: Session, *, project_id: int
) -> tuple[ProposedDelta, ...]:
    """Every delta no newer occurrence in its lineage superseded (#518)."""

    return tuple(
        session.scalars(
            select(ProposedDelta)
            .where(
                ProposedDelta.project_id == project_id,
                ~ProposedDelta.id.in_(
                    select(DeltaSupersession.prior_delta_id).where(
                        DeltaSupersession.project_id == project_id
                    )
                ),
            )
            .order_by(ProposedDelta.id)
        ).all()
    )


def open_deltas(session: Session, *, project_id: int) -> tuple[ProposedDelta, ...]:
    """Current and unresolved: ADR-0083's open state, deferrals included."""

    resolved = _resolved_ids(session, project_id)
    return tuple(
        delta
        for delta in current_deltas(session, project_id=project_id)
        if delta.id not in resolved
    )


def standing_accepted_revisions(
    session: Session, *, project_id: int
) -> dict[tuple[str, str], int]:
    """The revision each accepted subject and field currently stands on.

    The same rule ``delta_resolution.current_accepted_revision_id`` applies for
    one subject, read once for the whole project.
    """

    return standing_accepted_revisions_by_project(session, (project_id,)).get(
        project_id, {}
    )


# --- the same queries, asked once for many projects ------------------------
#
# A cross-project reading (#537) needs exactly the inputs above for every
# project it shows.  Asking for them one project at a time is one database
# round trip per project, which is the thing that ticket forbids, so each
# loader below is the batched form and the single-project one above delegates
# to it.  There is no second copy of any rule: the derivation that turns these
# rows into standings is ``standing_sets``, and both readers call it.


def _by_project(
    project_ids: Sequence[int],
) -> tuple[tuple[int, ...], bool]:
    """De-duplicated ids, and whether there is anything at all to ask about."""

    unique = tuple(dict.fromkeys(int(value) for value in project_ids))
    return unique, bool(unique)


def resolved_delta_ids_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, set[int]]:
    """Every delta a semantic disposition has already settled (#519)."""

    ids, any_ids = _by_project(project_ids)
    found: dict[int, set[int]] = {project_id: set() for project_id in ids}
    if not any_ids:
        return found
    for project_id, delta_id in session.execute(
        select(DeltaDisposition.project_id, DeltaDisposition.delta_id).where(
            DeltaDisposition.project_id.in_(ids)
        )
    ).all():
        found[int(project_id)].add(int(delta_id))
    return found


def superseding_delta_ids_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, dict[int, int | None]]:
    """Each replaced delta; None means its successor is a source reading (#455)."""

    ids, any_ids = _by_project(project_ids)
    found: dict[int, dict[int, int | None]] = {project_id: {} for project_id in ids}
    if not any_ids:
        return found
    for project_id, prior, superseding in session.execute(
        select(
            DeltaSupersession.project_id,
            DeltaSupersession.prior_delta_id,
            DeltaSupersession.superseding_delta_id,
        ).where(DeltaSupersession.project_id.in_(ids))
    ).all():
        found[int(project_id)][int(prior)] = int(superseding) if superseding is not None else None
    return found


def live_deferrals_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, dict[int, DeltaDeferral]]:
    """The newest unreversed scheduling receipt for each delta.

    A packet Undo compensates for its scheduling receipts, so a reversed
    deferral no longer holds anything out — the same rule
    ``delta_resolution.live_delta_status`` applies, with the newest receipt
    winning, because a later schedule is the one in force.
    """

    ids, any_ids = _by_project(project_ids)
    found: dict[int, dict[int, DeltaDeferral]] = {
        project_id: {} for project_id in ids
    }
    if not any_ids:
        return found
    reversed_receipts = (
        select(DeltaReviewPacketChild.deferral_id)
        .join(
            DeltaReviewPacketReversal,
            DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id,
        )
        .where(DeltaReviewPacketChild.deferral_id.is_not(None))
    )
    for row in session.scalars(
        select(DeltaDeferral)
        .where(
            DeltaDeferral.project_id.in_(ids),
            ~DeltaDeferral.id.in_(reversed_receipts),
        )
        .order_by(DeltaDeferral.id)
    ).all():
        found[int(row.project_id)][int(row.delta_id)] = row
    return found


def standing_accepted_revisions_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, dict[tuple[str, str], int]]:
    """``standing_accepted_revisions`` for several projects in one statement."""

    ids, any_ids = _by_project(project_ids)
    found: dict[int, dict[tuple[str, str], int]] = {
        project_id: {} for project_id in ids
    }
    if not any_ids:
        return found
    rows = session.execute(
        select(
            FactDecision.project_id,
            FactDecision.subject_key,
            FactDecision.fact_type,
            func.max(FactDecision.revision_id),
        )
        .where(
            FactDecision.project_id.in_(ids),
            FactDecision.superseded_by.is_(None),
        )
        .group_by(
            FactDecision.project_id, FactDecision.subject_key, FactDecision.fact_type
        )
    ).all()
    for project_id, subject, field, revision in rows:
        found[int(project_id)][(subject, field)] = int(revision)
    return found


def proposed_deltas_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, tuple[ProposedDelta, ...]]:
    """Every Proposed Delta of each project, in append-only identifier order."""

    ids, any_ids = _by_project(project_ids)
    collected: dict[int, list[ProposedDelta]] = {
        project_id: [] for project_id in ids
    }
    if not any_ids:
        return {project_id: () for project_id in ids}
    for delta in session.scalars(
        select(ProposedDelta)
        .where(ProposedDelta.project_id.in_(ids))
        .order_by(ProposedDelta.id)
    ).all():
        collected[int(delta.project_id)].append(delta)
    return {
        project_id: tuple(rows) for project_id, rows in collected.items()
    }


def accepted_revision_ids_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, int | None]:
    """The revision each project's record currently stands on, or ``None``.

    The same expression ``read_open_deltas`` uses for one project, so a
    cross-project reading names the same accepted revision the project's own
    week does.
    """

    ids, any_ids = _by_project(project_ids)
    found: dict[int, int | None] = {project_id: None for project_id in ids}
    if not any_ids:
        return found
    for project_id, revision_id in session.execute(
        select(
            ProjectRecordRevision.project_id,
            func.max(ProjectRecordRevision.id),
        )
        .where(ProjectRecordRevision.project_id.in_(ids))
        .group_by(ProjectRecordRevision.project_id)
    ).all():
        found[int(project_id)] = int(revision_id) if revision_id is not None else None
    return found


# --- the standing rule, shared by both readers -----------------------------


@dataclass(frozen=True, slots=True)
class DeltaStandingSets:
    """Which deltas are open, stale, held out by a deferral, and actionable.

    This is the whole of ADR-0083's open state and ADR-0084's scheduling, with
    nothing about items, bands, or presentation in it.  ``read_open_deltas``
    below builds its standings from this, and the cross-project portfolio
    reading (#537) builds its own from the same call over batched inputs, so
    the two cannot disagree about what is waiting.
    """

    open_ids: tuple[int, ...]
    stale_ids: frozenset[int]
    held: Mapping[int, DeltaDeferral]
    actionable: tuple[ProposedDelta, ...]
    #: Every delta that was scheduled and is back, and which release did it.
    returned: Mapping[int, DeferralReturn] = field(default_factory=dict)

    @property
    def actionable_ids(self) -> tuple[int, ...]:
        return tuple(delta.id for delta in self.actionable)


def standing_sets(
    deltas: Sequence[ProposedDelta],
    *,
    resolved: set[int],
    superseded_by: Mapping[int, int | None],
    schedules: Mapping[int, DeltaDeferral],
    standing: Mapping[tuple[str, str], int],
    as_of: datetime,
) -> DeltaStandingSets:
    """Place every delta of one project, from rows already read.

    Pure: it opens no session and reads no clock.  ``as_of`` is the cutoff its
    caller declared, and "a newer source version arrived" is answered from
    append-only identifier order exactly as ADR-0084 requires.
    """

    # Open under ADR-0083, before scheduling and staleness are considered.
    open_rows = [
        delta
        for delta in deltas
        if delta.id not in resolved and delta.id not in superseded_by
    ]
    stale_ids = frozenset(
        delta.id for delta in open_rows if is_stale(delta, standing)
    )
    live_rows = [delta for delta in open_rows if delta.id not in stale_ids]

    # ADR-0084's wake conditions.  A newer *independent* occurrence for the
    # same subject and field is read from append-only identifier order, and a
    # changed accepted value is already the staleness exit above.
    woken = _woken_by_a_newer_source(live_rows)

    held: dict[int, DeltaDeferral] = {}
    returned: dict[int, DeferralReturn] = {}
    for delta in live_rows:
        schedule = schedules.get(delta.id)
        if schedule is None:
            continue
        if delta.id in woken:
            returned[delta.id] = _returned(schedule, RETURNED_WAKE_CONDITION)
            continue
        if schedule.deferred_until is not None and schedule.deferred_until <= as_of:
            # A return instant at or before the moment the receipt itself was
            # recorded is not a date that arrived: it is the schedule being
            # ended deliberately, which is what "open it early" writes (#835).
            returned[delta.id] = _returned(
                schedule,
                RETURNED_OPENED_EARLY
                if schedule.deferred_until <= schedule.deferred_at
                else RETURNED_DATE_REACHED,
            )
            continue
        held[delta.id] = schedule

    return DeltaStandingSets(
        open_ids=tuple(delta.id for delta in open_rows),
        stale_ids=stale_ids,
        held=held,
        actionable=tuple(delta for delta in live_rows if delta.id not in held),
        returned=returned,
    )


def _returned(schedule: DeltaDeferral, reason: str) -> DeferralReturn:
    """One release, read off the scheduling receipt that was in force."""

    return DeferralReturn(
        reason=reason,
        scheduled_by=schedule.scheduled_by_principal,
        scheduled_at=schedule.deferred_at,
        returns_at=schedule.deferred_until,
        wake_condition=schedule.wake_condition,
        note=schedule.reason,
    )


def recorded_baseline_revision(recorded: str | None) -> int | None:
    """The revision identifier a delta recorded as the value it compared against."""

    if not recorded or not recorded.startswith("revision:"):
        return None
    tail = recorded.split(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def is_stale(
    delta: ProposedDelta, standing: Mapping[tuple[str, str], int]
) -> bool:
    """Whether the accepted value this delta was compared against has moved.

    A delta that never recorded which revision it compared against cannot be
    proved stale, so it is not called stale; the decision command's own
    stale-revision refusal still stands behind the reading.
    """

    baseline = recorded_baseline_revision(delta.accepted_baseline_revision)
    if baseline is None or delta.target_field is None:
        return False
    current = standing.get((delta.target_subject_identity, delta.target_field))
    return current is not None and current > baseline


# --- consequence -----------------------------------------------------------


def attention_reasons(
    delta: ProposedDelta, *, as_of: datetime, contradicted: bool
) -> tuple[str, ...]:
    """Every band this delta belongs to, highest consequence first (ADR-0035)."""

    reasons: list[str] = []
    field_name = delta.target_field or ""
    if field_name in DATE_FIELDS and _elapsed(delta, as_of):
        reasons.append(PAST_DUE_COMMITMENT_BAND)
    if field_name in DATE_FIELDS:
        reasons.append(PROMISED_TIMING_CHANGE_BAND)
    if contradicted:
        reasons.append(SOURCE_CONTRADICTION_BAND)
    if delta.change_type == "apparent_removal":
        reasons.append(APPARENT_REMOVAL_BAND)
    if delta.target_type == "proposed_subject":
        reasons.append(UNPLACED_SUBJECT_BAND)
    if not reasons:
        reasons.append(RECORD_CLEANUP_BAND)
    return tuple(sorted(reasons, key=lambda name: BAND_ORDINALS[name]))


def held_out_reason(delta: ProposedDelta) -> str | None:
    """Why this delta cannot join its source revision's batch, or ``None``.

    Batch eligibility is a property of the change's shape alone, so it is
    deterministic, versioned with ``PARTITION_RULE_VERSION``, and explainable
    from stored identities without any model judgement.
    """

    if delta.change_type == "apparent_removal":
        return HELD_OUT_APPARENT_REMOVAL
    if delta.target_type == "proposed_subject":
        return HELD_OUT_POSSIBLE_NEW_CONFLICT
    field_name = delta.target_field
    if field_name is None:
        return HELD_OUT_UNCERTAIN_SCOPE
    if field_name in ORGANIZATION_FIELDS:
        # An organization change has to say whether it corrects a wrong name or
        # records that ownership moved, and #519 refuses one that does not.
        return HELD_OUT_OWNER_MISMATCH
    if field_name in SCOPE_FIELDS:
        return HELD_OUT_UNCERTAIN_SCOPE
    return None


# --- the reading ----------------------------------------------------------


def read_open_deltas(
    session: Session,
    *,
    project_id: int,
    as_of: datetime,
    rule_version: str = PARTITION_RULE_VERSION,
) -> DeltaReading:
    """Partition one project's Proposed Deltas, exactly once each."""

    if as_of.tzinfo is None:
        raise ReviewPacketReadingRefused(
            "a reading is bound to an aware cutoff its caller declared"
        )
    if rule_version not in SUPPORTED_RULE_VERSIONS:
        raise ReviewPacketReadingRefused(
            f"{rule_version!r} is not a partition rule this reading implements; "
            f"one of {', '.join(sorted(SUPPORTED_RULE_VERSIONS))}"
        )

    deltas = tuple(
        session.scalars(
            select(ProposedDelta)
            .where(ProposedDelta.project_id == project_id)
            .order_by(ProposedDelta.id)
        ).all()
    )
    accepted_revision_id = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project_id
        )
    )
    if not deltas:
        return DeltaReading(
            project_id=project_id,
            rule_version=rule_version,
            as_of=as_of,
            accepted_revision_id=accepted_revision_id,
            items=(),
            standings=(),
        )

    resolved = _resolved_ids(session, project_id)
    superseded_by = _superseded_by(session, project_id)
    schedules = _live_deferrals(session, project_id)
    standing = standing_accepted_revisions(session, project_id=project_id)

    sets = standing_sets(
        deltas,
        resolved=resolved,
        superseded_by=superseded_by,
        schedules=schedules,
        standing=standing,
        as_of=as_of,
    )
    stale_ids = sets.stale_ids
    held = sets.held
    returned = sets.returned
    actionable = list(sets.actionable)
    contradicted = _contradicted_keys(actionable)
    commitments = _explicitly_scoped_commitments(session, project_id, actionable)

    items = _partition(
        actionable,
        as_of=as_of,
        contradicted=contradicted,
        commitments=commitments,
        rule_version=rule_version,
    )
    item_of = {
        delta_id: item.item_key for item in items for delta_id in item.delta_ids
    }
    reasons_of = {
        delta.id: attention_reasons(
            delta,
            as_of=as_of,
            contradicted=(delta.target_subject_identity, delta.target_field)
            in contradicted,
        )
        for delta in actionable
    }

    standings: list[DeltaStanding] = []
    for delta in deltas:
        if delta.id in resolved:
            standings.append(DeltaStanding(delta_id=delta.id, standing=RESOLVED))
        elif delta.id in superseded_by:
            standings.append(
                DeltaStanding(
                    delta_id=delta.id,
                    standing=SUPERSEDED,
                    superseded_by_delta_id=superseded_by[delta.id],
                )
            )
        elif delta.id in stale_ids:
            standings.append(
                DeltaStanding(
                    delta_id=delta.id,
                    standing=STALE,
                    baseline_revision=recorded_baseline_revision(
                        delta.accepted_baseline_revision
                    ),
                    current_accepted_revision_id=standing.get(
                        (delta.target_subject_identity, delta.target_field or "")
                    ),
                )
            )
        elif delta.id in held:
            schedule = held[delta.id]
            standings.append(
                DeltaStanding(
                    delta_id=delta.id,
                    standing=DEFERRED,
                    returns_at=schedule.deferred_until,
                    wake_condition=schedule.wake_condition,
                )
            )
        else:
            reasons = reasons_of[delta.id]
            standings.append(
                DeltaStanding(
                    delta_id=delta.id,
                    standing=ACTIONABLE,
                    band=reasons[0],
                    attention_reasons=reasons,
                    item_key=item_of[delta.id],
                    commitment_key=commitments.get(delta.id),
                    returned=returned.get(delta.id),
                )
            )

    return DeltaReading(
        project_id=project_id,
        rule_version=rule_version,
        as_of=as_of,
        accepted_revision_id=accepted_revision_id,
        items=items,
        standings=tuple(standings),
    )


# --- the invocation seam #527 and #528 use --------------------------------


def bind_child_decision(
    reading: DeltaReading, item: ActionableItem, request: ChildDecisionRequest
) -> ChildDecisionRequest:
    """Bind one single-delta decision to the item and revision it was read on.

    The returned request goes to #519's ``validate_child_decision`` or
    ``resolve_delta`` unchanged.  Nothing about accept, edit, reject, or defer
    is decided here; this only refuses a decision on a delta this item does not
    offer, which is what keeps "exactly one actionable item" true of the act as
    well as of the reading.
    """

    require_offered(reading, item, (request.delta_id,))
    if request.project_id != reading.project_id:
        raise ReviewPacketReadingRefused(
            "a decision belongs to the project its reading was taken for"
        )
    return replace(
        request, observed_accepted_revision_id=reading.accepted_revision_id
    )


def bind_packet_request(
    reading: DeltaReading,
    item: ActionableItem,
    *,
    principal: HumanPrincipal,
    idempotency_key: str,
    decided_at: datetime,
    children: Sequence[PacketChildRequest],
) -> ReviewPacketRequest:
    """Build the #526 packet act for one item's selected children.

    The coordinator's selection may be a subset — an unselected child simply
    stays open — but it may never reach outside the item, and the receipt
    records the key and rule version the partition actually chose.
    """

    if not children:
        raise ReviewPacketReadingRefused("a packet act names at least one child")
    require_offered(reading, item, tuple(child.delta_id for child in children))
    return ReviewPacketRequest(
        project_id=reading.project_id,
        grouping_rule_version=item.grouping_rule_version,
        grouping_key_kind=item.grouping_key_kind,
        grouping_key=item.grouping_key,
        principal=principal,
        idempotency_key=idempotency_key,
        decided_at=decided_at,
        children=tuple(children),
        observed_accepted_revision_id=reading.accepted_revision_id,
    )


# --- the partition rule ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Draft:
    """One item before its band, its reasons, and its ordinal are worked out."""

    item_key: str
    grouping_key_kind: str
    grouping_key: str
    key_reason: str
    children: list[ProposedDelta]
    held_out_reason: str | None = None


def _partition(
    actionable: Sequence[ProposedDelta],
    *,
    as_of: datetime,
    contradicted: frozenset[tuple[str, str | None]],
    commitments: Mapping[int, str],
    rule_version: str,
) -> tuple[ActionableItem, ...]:
    """Claim every actionable delta exactly once, in four declared passes.

    A pass claims deltas; it does not always produce one item per claim.  The
    first pass in particular splits a candidate question that cannot honestly
    be one, so some of what it claims leaves as its own focused item.
    """

    claimed: set[int] = set()
    drafts: list[_Draft] = []

    # 1. One cross-source coordination question per contradicted subject and
    #    field.  These are decided together or not at all, so they are claimed
    #    before any batch or commitment can take one of them — a commitment one
    #    of whose Utility Conflicts a later source contradicts is no longer one
    #    bounded decision, and the contradicted child leaves it.
    questions: dict[tuple[str, str | None], list[ProposedDelta]] = defaultdict(list)
    for delta in actionable:
        key = (delta.target_subject_identity, delta.target_field)
        if key in contradicted:
            questions[key].append(delta)
            claimed.add(delta.id)
    for (subject, field_name), children in sorted(
        questions.items(), key=lambda entry: min(row.id for row in entry[1])
    ):
        drafts.extend(_question_drafts(subject, field_name, children))

    # 2. Anything that cannot safely join a batch keeps its own focused item.
    for delta in actionable:
        if delta.id in claimed:
            continue
        reason = held_out_reason(delta)
        if reason is None:
            continue
        claimed.add(delta.id)
        drafts.append(_held_out_draft(delta, reason))

    # 3. One attributable commitment whose own Applies To scope is settled and
    #    which moves several Utility Conflicts stays one item, with the whole
    #    scope enumerated.  A commitment reaching one subject is an ordinary
    #    batch, and a commitment whose scope is not settled never gets here.
    shared: dict[str, list[ProposedDelta]] = defaultdict(list)
    for delta in actionable:
        if delta.id in claimed or delta.id not in commitments:
            continue
        shared[commitments[delta.id]].append(delta)
    for commitment, children in sorted(
        shared.items(), key=lambda entry: min(row.id for row in entry[1])
    ):
        if len({row.target_subject_identity for row in children}) < 2:
            continue
        for child in children:
            claimed.add(child.id)
        drafts.append(
            _Draft(
                item_key=f"{SHARED_COMMITMENT}:{commitment}",
                grouping_key_kind=SHARED_COMMITMENT,
                grouping_key=commitment,
                key_reason=KEY_SHARED_COMMITMENT_SCOPE,
                children=children,
            )
        )

    # 4. What remains batches by source revision and consequence band, so a
    #    burst of routine changes is one item and a timing change inside the
    #    same revision is not buried under it.
    batches: dict[tuple[str, str], list[ProposedDelta]] = defaultdict(list)
    for delta in actionable:
        if delta.id in claimed:
            continue
        claimed.add(delta.id)
        band = attention_reasons(delta, as_of=as_of, contradicted=False)[0]
        batches[(_lineage_key(delta), band)].append(delta)
    for (key, band), children in sorted(
        batches.items(), key=lambda entry: min(row.id for row in entry[1])
    ):
        drafts.append(
            _Draft(
                item_key=f"{SOURCE_REVISION}:{key}:{band}",
                grouping_key_kind=SOURCE_REVISION,
                grouping_key=key,
                key_reason=KEY_SOURCE_REVISION_BATCH,
                children=children,
            )
        )

    items: list[ActionableItem] = []
    for draft in drafts:
        children = draft.children
        reasons: list[str] = []
        for child in children:
            for name in attention_reasons(
                child,
                as_of=as_of,
                contradicted=(child.target_subject_identity, child.target_field)
                in contradicted,
            ):
                if name not in reasons:
                    reasons.append(name)
        reasons.sort(key=lambda name: BAND_ORDINALS[name])
        items.append(
            ActionableItem(
                ordinal=0,
                item_key=draft.item_key,
                grouping_key_kind=draft.grouping_key_kind,
                grouping_key=draft.grouping_key,
                grouping_rule_version=rule_version,
                band=reasons[0],
                attention_reasons=tuple(reasons),
                delta_ids=tuple(sorted(child.id for child in children)),
                held_out_reason=draft.held_out_reason,
                key_reason=draft.key_reason,
            )
        )

    items.sort(key=lambda item: (BAND_ORDINALS[item.band], item.delta_ids[0]))
    return tuple(
        replace(item, ordinal=ordinal)
        for ordinal, item in enumerate(items, start=1)
    )


def _question_drafts(
    subject: str, field_name: str | None, children: Sequence[ProposedDelta]
) -> list[_Draft]:
    """One contradicted subject and field, as the items it can honestly become.

    Sources that disagree about a value pose one question.  Sources that
    propose *materially different actions* — one edits the value while another
    says the row is gone — pose two, and answering either leaves the other
    unanswered, so ADR-0085's "smallest set decidable at once" splits them.
    """

    key = f"{subject}#{field_name or '*'}"
    # Disagreeing about which utility a row *is* is not a value question with
    # two candidate answers; it has to be settled before any value recorded
    # against the row means anything.
    reason = (
        KEY_CONTRADICTED_IDENTITY
        if field_name in IDENTITY_FIELDS
        else KEY_CONTRADICTED_VALUE
    )
    kinds = sorted({child.change_type for child in children})
    if len(kinds) == 1:
        return [
            _Draft(
                item_key=f"{COORDINATION_QUESTION}:{key}",
                grouping_key_kind=COORDINATION_QUESTION,
                grouping_key=key,
                key_reason=reason,
                children=list(children),
            )
        ]

    drafts: list[_Draft] = []
    for kind in kinds:
        matching = [child for child in children if child.change_type == kind]
        lineages = {
            (child.source_family, child.source_revision) for child in matching
        }
        if len(lineages) > 1:
            # Still a genuine disagreement, now between sources that at least
            # propose the same kind of change.
            drafts.append(
                _Draft(
                    item_key=f"{COORDINATION_QUESTION}:{key}#{kind}",
                    grouping_key_kind=COORDINATION_QUESTION,
                    grouping_key=f"{key}#{kind}",
                    key_reason=reason,
                    children=matching,
                )
            )
            continue
        for child in matching:
            drafts.append(_held_out_draft(child, HELD_OUT_DIFFERENT_ACTION))
    return drafts


def _held_out_draft(delta: ProposedDelta, reason: str) -> _Draft:
    """One delta that cannot be decided with anything else, as its own item."""

    key = _lineage_key(delta)
    return _Draft(
        item_key=f"{SOURCE_REVISION}:{key}:delta:{delta.id}",
        grouping_key_kind=SOURCE_REVISION,
        grouping_key=key,
        key_reason=KEY_HELD_OUT_OF_BATCH,
        children=[delta],
        held_out_reason=reason,
    )


def _explicitly_scoped_commitments(
    session: Session, project_id: int, actionable: Sequence[ProposedDelta]
) -> dict[int, str]:
    """Each actionable delta's commitment key, where its statement declared one.

    The link is the spine's own: ``delta_groups.statement_id`` names the one
    attributable External Party Statement a delta group came from.  The
    statement's ``scope_mode`` is read to tell a settled Applies To from
    ``not yet known``, which is record state and never a question to ask
    (ADR-0035, ADR-0039); an unsettled one simply produces no commitment key,
    so its deltas keep their own items instead of being decided as one.

    **What this does not read, and why.**  The declared Commitment Scope lives
    in ``dependency_event_scopes``, whose members are legacy ``dependencies``
    rows, while a Proposed Delta targets a spine subject identity.  No mapping
    between those two identity spaces exists, and inventing one here would be
    a guess about which Utility Conflict a scope link means.  So the scope this
    reading can enumerate is the commitment's *delta* scope — the conflicts its
    own Proposed Deltas reach — and a conflict inside the declared scope that
    produced no delta is not named.  That is why only ``selected`` qualifies:
    it is the mode whose deltas are the enumeration.
    """

    group_ids = {int(delta.group_id) for delta in actionable}
    if not group_ids:
        return {}
    rows = session.execute(
        select(DeltaGroup.id, ExternalPartyStatement.id)
        .join(
            ExternalPartyStatement,
            ExternalPartyStatement.id == DeltaGroup.statement_id,
        )
        .where(
            DeltaGroup.project_id == project_id,
            DeltaGroup.id.in_(sorted(group_ids)),
            ExternalPartyStatement.scope_mode.in_(sorted(EXPLICIT_SCOPE_MODES)),
        )
    ).all()
    statements = {int(group_id): int(statement_id) for group_id, statement_id in rows}
    return {
        delta.id: f"statement:{statements[int(delta.group_id)]}"
        for delta in actionable
        if int(delta.group_id) in statements
    }


def _contradicted_keys(
    actionable: Sequence[ProposedDelta],
) -> frozenset[tuple[str, str | None]]:
    """Subject-and-field keys two retained source versions answer differently."""

    lineages: dict[tuple[str, str | None], set[tuple[str, str]]] = defaultdict(set)
    for delta in actionable:
        lineages[(delta.target_subject_identity, delta.target_field)].add(
            (delta.source_family, delta.source_revision)
        )
    return frozenset(key for key, seen in lineages.items() if len(seen) > 1)


def _woken_by_a_newer_source(live: Sequence[ProposedDelta]) -> set[int]:
    """Deltas a newer independent occurrence returns to immediate work.

    ADR-0084's wake conditions include "a newer source version for the same
    subject and field".  Newer is read from ``proposed_deltas.id``, which is
    monotonic in append order whatever any clock said.
    """

    by_key: dict[tuple[str, str | None], list[ProposedDelta]] = defaultdict(list)
    for delta in live:
        by_key[(delta.target_subject_identity, delta.target_field)].append(delta)
    woken: set[int] = set()
    for rows in by_key.values():
        for delta in rows:
            lineage = (delta.source_family, delta.source_revision)
            if any(
                other.id > delta.id
                and (other.source_family, other.source_revision) != lineage
                for other in rows
            ):
                woken.add(delta.id)
    return woken


def _lineage_key(delta: ProposedDelta) -> str:
    return f"{delta.source_family}@{delta.source_revision}"


def _elapsed(delta: ProposedDelta, as_of: datetime) -> bool:
    """Whether either side of a date delta already lies behind the cutoff."""

    cutoff = as_of.date()
    return any(
        value is not None and value < cutoff
        for value in (_as_date(delta.accepted_value), _as_date(delta.proposed_value))
    )


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def require_offered(
    reading: DeltaReading, item: ActionableItem, delta_ids: Sequence[int]
) -> None:
    """Refuse a delta this item does not offer: ADR-0085's exactly-once rule.

    Public because the review screen refuses the same thing before it indexes
    its own children, and one rule with two implementations is how a delta ends
    up refused by one and not the other.  #526 re-checks it in SQL, which is the
    authority; these are the readable halves.
    """

    if item.offer_identity not in {other.offer_identity for other in reading.items}:
        raise ReviewPacketReadingRefused(
            "the item is not part of this reading; take the reading again"
        )
    outside = [
        delta_id for delta_id in delta_ids if delta_id not in item.delta_ids
    ]
    if outside:
        raise ReviewPacketReadingRefused(
            f"deltas {sorted(outside)} are not offered by this item; a delta is "
            "actionable in exactly one item"
        )


def _resolved_ids(session: Session, project_id: int) -> set[int]:
    return resolved_delta_ids_by_project(session, (project_id,)).get(project_id, set())


def _superseded_by(session: Session, project_id: int) -> dict[int, int | None]:
    return superseding_delta_ids_by_project(session, (project_id,)).get(project_id, {})


def _live_deferrals(session: Session, project_id: int) -> dict[int, DeltaDeferral]:
    return live_deferrals_by_project(session, (project_id,)).get(project_id, {})
