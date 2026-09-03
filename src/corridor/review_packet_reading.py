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
deliberately not derived here: they need the next issue's declared content
inventory (ADR-0086), and they are the heading a screen prints.

**What was tried before, and rejected.**  Keying every packet by source
revision destroys the cross-source case, because the documents that disagree
arrive separately; keying everything by coordination question destroys the
batch case, because one revision's thirty exact changes become thirty
questions.  ADR-0085 settled that the key is selected per packet, so the rule
here selects it and records which key it chose.  Storing the partition was
rejected by ADR-0085 as well: it would be a second authoritative record,
stale the moment a source arrived mid-review.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    DeltaReviewPacketChild,
    DeltaReviewPacketReversal,
    DeltaSupersession,
    FactDecision,
    ProjectRecordRevision,
    ProposedDelta,
)
from corridor.principals import HumanPrincipal
from corridor.review_packets import PacketChildRequest, ReviewPacketRequest


# The rule this reading partitions under.  It is recorded on every item and on
# every packet act built from one, so a later change to the rule is visible in
# the receipt rather than retroactive.
PARTITION_RULE_VERSION = "delta-partition-v1"

# Standings.  ``resolved``, ``deferred``, and ``stale`` keep #519's spelling so
# one word never means two things across the seam.
ACTIONABLE = "actionable"
SUPERSEDED = "superseded"
STANDINGS = (ACTIONABLE, DEFERRED, STALE, SUPERSEDED, RESOLVED)

# ADR-0085's packet keys.  ``shared_commitment`` is a released key kind that
# this rule version does not yet produce; #528 extends the rule to it, and
# until then a commitment's deltas reach a coordination question or their own
# focused item, never nothing.
SOURCE_REVISION = "source_revision"
COORDINATION_QUESTION = "coordination_question"
PRODUCED_KEY_KINDS = (SOURCE_REVISION, COORDINATION_QUESTION)

# Scope lives in its own satellite and a scope change is never an exact cell
# comparison, so it is decided on its own item.
SCOPE_FIELDS = frozenset({"applies_to"})
DATE_FIELDS = frozenset(TIMING_FIELDS | SCHEDULE_KEY_DATE_FIELDS)

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


class ReviewPacketReadingRefused(ValueError):
    """A caller cannot build this reading, or cannot act on it as asked."""


# --- what the reading returns ---------------------------------------------


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

    rows = session.execute(
        select(
            FactDecision.subject_key,
            FactDecision.fact_type,
            func.max(FactDecision.revision_id),
        )
        .where(
            FactDecision.project_id == project_id,
            FactDecision.superseded_by.is_(None),
        )
        .group_by(FactDecision.subject_key, FactDecision.fact_type)
    ).all()
    return {(subject, field): int(revision) for subject, field, revision in rows}


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

    # Open under ADR-0083, before scheduling and staleness are considered.
    open_rows = [
        delta
        for delta in deltas
        if delta.id not in resolved and delta.id not in superseded_by
    ]
    stale_ids = {delta.id for delta in open_rows if is_stale(delta, standing)}
    live_rows = [delta for delta in open_rows if delta.id not in stale_ids]

    # ADR-0084's wake conditions.  A newer *independent* occurrence for the
    # same subject and field is read from append-only identifier order, and a
    # changed accepted value is already the staleness exit above.
    woken = _woken_by_a_newer_source(live_rows)

    held: dict[int, DeltaDeferral] = {}
    for delta in live_rows:
        schedule = schedules.get(delta.id)
        if schedule is None or delta.id in woken:
            continue
        if schedule.deferred_until is not None and schedule.deferred_until <= as_of:
            continue
        held[delta.id] = schedule

    actionable = [delta for delta in live_rows if delta.id not in held]
    contradicted = _contradicted_keys(actionable)

    items = _partition(
        actionable,
        as_of=as_of,
        contradicted=contradicted,
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

    _require_offered(reading, item, (request.delta_id,))
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
    _require_offered(reading, item, tuple(child.delta_id for child in children))
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


def _partition(
    actionable: Sequence[ProposedDelta],
    *,
    as_of: datetime,
    contradicted: frozenset[tuple[str, str | None]],
    rule_version: str,
) -> tuple[ActionableItem, ...]:
    """Claim every actionable delta exactly once, in three declared passes."""

    claimed: set[int] = set()
    drafts: list[tuple[str, str, str, str | None, list[ProposedDelta]]] = []

    # 1. One cross-source coordination question per contradicted subject and
    #    field.  These are decided together or not at all, so they are claimed
    #    before any batch can take one of them.
    questions: dict[tuple[str, str | None], list[ProposedDelta]] = defaultdict(list)
    for delta in actionable:
        key = (delta.target_subject_identity, delta.target_field)
        if key in contradicted:
            questions[key].append(delta)
            claimed.add(delta.id)
    for (subject, field_name), children in sorted(
        questions.items(), key=lambda entry: min(row.id for row in entry[1])
    ):
        key = f"{subject}#{field_name or '*'}"
        drafts.append(
            (f"{COORDINATION_QUESTION}:{key}", COORDINATION_QUESTION, key, None, children)
        )

    # 2. Anything that cannot safely join a batch keeps its own focused item.
    for delta in actionable:
        if delta.id in claimed:
            continue
        reason = held_out_reason(delta)
        if reason is None:
            continue
        claimed.add(delta.id)
        key = _lineage_key(delta)
        drafts.append(
            (
                f"{SOURCE_REVISION}:{key}:delta:{delta.id}",
                SOURCE_REVISION,
                key,
                reason,
                [delta],
            )
        )

    # 3. What remains batches by source revision and consequence band, so a
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
            (f"{SOURCE_REVISION}:{key}:{band}", SOURCE_REVISION, key, None, children)
        )

    items: list[ActionableItem] = []
    for item_key, kind, key, reason, children in drafts:
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
                item_key=item_key,
                grouping_key_kind=kind,
                grouping_key=key,
                grouping_rule_version=rule_version,
                band=reasons[0],
                attention_reasons=tuple(reasons),
                delta_ids=tuple(sorted(child.id for child in children)),
                held_out_reason=reason,
            )
        )

    items.sort(key=lambda item: (BAND_ORDINALS[item.band], item.delta_ids[0]))
    return tuple(
        replace(item, ordinal=ordinal)
        for ordinal, item in enumerate(items, start=1)
    )


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


def _require_offered(
    reading: DeltaReading, item: ActionableItem, delta_ids: Sequence[int]
) -> None:
    if item not in reading.items:
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
    return set(
        session.scalars(
            select(DeltaDisposition.delta_id).where(
                DeltaDisposition.project_id == project_id
            )
        ).all()
    )


def _superseded_by(session: Session, project_id: int) -> dict[int, int]:
    return {
        int(prior): int(superseding)
        for prior, superseding in session.execute(
            select(
                DeltaSupersession.prior_delta_id,
                DeltaSupersession.superseding_delta_id,
            ).where(DeltaSupersession.project_id == project_id)
        ).all()
    }


def _live_deferrals(session: Session, project_id: int) -> dict[int, DeltaDeferral]:
    """The newest unreversed scheduling receipt for each delta.

    A packet Undo compensates for its scheduling receipts, so a reversed
    deferral no longer holds anything out — the same rule
    ``delta_resolution.live_delta_status`` applies, read for the whole project
    at once and with the newest receipt winning, because a later schedule is
    the one in force.
    """

    reversed_receipts = (
        select(DeltaReviewPacketChild.deferral_id)
        .join(
            DeltaReviewPacketReversal,
            DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id,
        )
        .where(DeltaReviewPacketChild.deferral_id.is_not(None))
    )
    newest: dict[int, DeltaDeferral] = {}
    for row in session.scalars(
        select(DeltaDeferral)
        .where(
            DeltaDeferral.project_id == project_id,
            ~DeltaDeferral.id.in_(reversed_receipts),
        )
        .order_by(DeltaDeferral.id)
    ).all():
        newest[int(row.delta_id)] = row
    return newest
