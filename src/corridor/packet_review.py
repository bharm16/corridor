"""What a coordinator actually reads before deciding one source revision (#527).

ADR-0085 splits the adopted-project Work List into a **reading** and an **act**.
``review_packet_reading`` owns the reading's skeleton — which deltas are decided
together, in which band, with which exit — and ``review_packets`` owns the one
atomic act.  Neither of them knows a single customer-facing fact: not the
accepted value beside the incoming one, not the sheet and cell the incoming
value came from, not the row identifier the customer's own utility-management
system prints, and not which artifact the customer receives would change.  This
module supplies exactly that layer, so the screen is a template over a reading
rather than a pile of queries in a route.

**It re-derives nothing.**  The partition, the consequence bands, the hold-out
reasons, and the exactly-once guarantee are read from ``read_open_deltas`` and
passed through untouched; ``bind_packet_request`` builds the act.  If a
grouping question is ever asked here, the answer is wrong by construction,
because two modules deciding which deltas belong together is how one delta gets
offered twice.

**The screen emits only the two events nothing else can.**  #519 already emits
one ``child_decision`` per committed or refused child and #526 one
``packet_save`` per act, both bound to the packetizer rule version the act was
built by.  Emitting a third copy here would double-count exactly the measure the
contract exists for, so this module adds only ``packet_surfacing`` and
``packet_opening`` — the two facts that live on the screen and nowhere else.

**Why a child can be listed and still not be applicable.**  #519 refuses an
accept whose incoming Source Fact carries no effective ``value_support``
Support Assessment (#530), and it is right to: a passed Source Passage Check
says a cell exists, never that it supports the value.  A screen that hid that
would offer forty changes and have the whole packet refused on the fortieth.
So support is read here, per child, and the item reports how many of its
children are *ready* — meaning Apply would not be refused for missing support.
A child that is not ready is still offered by this item, still selectable, and
still decidable as Keep current or Defer; nothing is hidden, and nothing that
would refuse is presented as ready.

**Held out is a sibling, not a disappearance.**  Everything the partition took
out of a source revision's batch — an apparent removal, an unplaced subject, an
organization change, an unresolved scope, a contradicted field — is still an
actionable item of its own.  The batch names those siblings read-only, with the
reason and the item that owns them, so the coordinator can see that the
revision's accounting is complete without ever finding a second control for the
same delta (ADR-0085's exactly-once rule).

**A held-out item is focused, not a one-row batch (#659).**  It was held out
because it does *not* fail the way the batch fails, so answering it with the
batch's three outcomes was always the wrong offer.  Its real answer is very
often neither "apply" nor "keep" nor a date, but "somebody owes me an answer
before this can be accepted" — an apparent removal nobody has confirmed, a
Utility Conflict the record does not hold yet, an organization change that has
not said whether it corrects a name or records a transfer, a scope nobody has
settled.  Each of those is a coordination question with a single source and no
contradiction anywhere, and until #659 the only route to Needs coordination was
the cross-source item, which is contradicted by construction: every Follow-up
Plan the product could create was therefore a Source Discrepancy.  ``focused``
now covers the held-out item, so the per-child controls answer it and #526
records the plan.  ``BATCH_OUTCOMES`` was deliberately *not* widened — the
failure mode it excludes is forty unrelated routine changes planned away in one
act, and that is still excluded.

**Which customer artifacts would change** is answered from what the project has
actually registered, never guessed.  ``ARTIFACT_IMPACT_RULE_VERSION`` names the
rule: a field the customer's own workbook carries changes that workbook when an
output template is registered (#495, ``workbook_render``), and every accepted
change reaches the weekly change summary and report, which counts exactly the
deltas that resolved (#534, ``report_preparation``).  A project that has
registered no output template is simply not promised a workbook it has not
configured.

**ADR-0085's three visible consequence levels are derived here, once (#641).**
A level is a projection of one proposed difference onto what the project is
configured to externally issue — which artifacts, produced by which registered
renderer revision, stating which accepted fields, under which explicit customer
policy.  ``issue_profile`` records that configuration (#640) and
``issue_content`` resolves it into executable contracts and typed policy
selectors; ``consequence_levels`` performs the projection.  This module reads
the project's configuration **once per reading**, at the same cutoff the
partition was bound to, and hangs the resulting level on the child it belongs
to.  Every Review surface then prints that object: the review screen and the
project's own week (#536) cannot disagree about a packet, because neither of
them derives anything.  A packet's own level is the highest of its children's
and is a property, not a stored field — ADR-0085 permits a packet to headline
and forbids splitting one because the levels differ.

Where the configuration cannot be executed — a renderer revision this release
does not register, an evaluator it does not run, a ``required_decision`` that
is a sentence rather than a typed selector — **no level is derived at all** and
the reason is stated in Issue readiness.  Not a blanket "Must handle before
this issue", which would assert a customer rule nobody configured, and not a
blanket "Can wait", which would promise that a change reaches nothing while
nobody knows what the artifacts contain.

**No clock.**  ``as_of`` and ``decided_at`` are declared by the caller, exactly
as the reading below them requires, so a screen and its test agree about what
"already elapsed" and "decided at" mean.

Terminology: nothing here coins a customer word.  Utility Conflict, Utility
Conflict Matrix, Constraint, Proposed Delta, Defer, and Attention Reason are
the adopted glossary's, and ``Review Packet`` stays the internal technical name
it is — the screen names the source revision, never the packet type.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
    default_binding,
    emit_event,
)
from corridor.baseline_adoption import effective_baseline_formats
from corridor.baseline_workbook import BASELINE_FACT_FIELDS
from corridor.delta_generation import COMPARABLE_FACT_TYPES
from corridor.delta_resolution import CapturedSupport, RecordEffect
from corridor.models import (
    BaselineSourceRow,
    DeltaGroup,
    Document,
    Fact,
    FactSource,
    ProposedDelta,
    SourceSegment,
    SupportAssessment,
)
from corridor.consequence_levels import (
    CONSEQUENCE_RULE_VERSION,
    LEVEL_HEADINGS,
    ConsequenceLevel,
    consequence_level,
    headline_level,
)
from corridor.issue_content import ChangeFacts, EffectiveIssueContent, effective_issue_content
from corridor.issue_coverage import (
    deltas_outside_coverage_boundary,
    latest_declaration,
)
from corridor.issue_profile import effective_issue_inventory
from corridor.presentation import field_label
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import (
    CONSEQUENCE_BANDS,
    COORDINATION_QUESTION,
    DEFERRED,
    RESOLVED,
    STALE,
    SUPERSEDED,
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_DIFFERENT_ACTION,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    HELD_OUT_UNCERTAIN_SCOPE,
    IDENTITY_FIELDS,
    KEY_CONTRADICTED_IDENTITY,
    KEY_REASONS,
    PARTITION_RULE_VERSION,
    SHARED_COMMITMENT,
    SOURCE_REVISION,
    ActionableItem,
    DeltaReading,
    bind_packet_request,
    read_open_deltas,
    standing_accepted_revisions,
)
from corridor.review_packets import (
    APPLY,
    DEFER,
    EDIT_AND_APPLY,
    KEEP_CURRENT,
    NEEDS_COORDINATION,
    CoordinationRequest,
    DeferralRequest,
    PacketChildRequest,
    ReviewPacketRequest,
)


# The rule that decides which customer artifacts a change reaches.  Recorded
# beside every answer so a later change to the rule is visible rather than
# retroactive, exactly as the partition records its own version.
ARTIFACT_IMPACT_RULE_VERSION = "customer-artifact-impact-v1"

# The two artifacts an adopted project actually produces today.  Both names are
# the ones the repository already uses for them; neither is a new term.
CUSTOMER_WORKBOOK = "Utility Conflict Matrix workbook"
WEEKLY_REPORT = "Change summary and weekly report"

# The Support Assessment that makes an incoming value applicable (#530).  A
# passed Source Passage Check is not support and is never read as one.
VALUE_SUPPORT_ROLE = "value_support"
SUPPORTING_ASSESSMENTS = ("supported", "partially_supported")

# What each partition hold-out reason means, in the coordinator's own terms.
# #494 owns the tokens; the words for them are this screen's, and each explains
# how the change fails rather than naming a new kind of thing.
HELD_OUT_WORDS = {
    HELD_OUT_APPARENT_REMOVAL: (
        "the revised source no longer contains this row, which fails "
        "differently from an edited value"
    ),
    HELD_OUT_POSSIBLE_NEW_CONFLICT: (
        "the source proposes a Utility Conflict the accepted record does not "
        "hold yet"
    ),
    HELD_OUT_OWNER_MISMATCH: (
        "an organization change has to say whether it corrects a wrong name or "
        "records that ownership moved"
    ),
    HELD_OUT_UNCERTAIN_SCOPE: (
        "which Constraints this applies to is not settled by an exact cell "
        "comparison"
    ),
    HELD_OUT_DIFFERENT_ACTION: (
        "another source proposes a different kind of change to the same value, "
        "so answering this one would not answer that one"
    ),
}
CONTRADICTED_ELSEWHERE = (
    "two retained sources answer this differently, so it is decided as its own "
    "question"
)

# The sentence each consequence band already carries in #494, read back rather
# than restated, so the screen and the audit agree word for word.
BAND_SENTENCES = {band.name: band.reason for band in CONSEQUENCE_BANDS}

# Why a listed child cannot be applied right now.  These are not partition
# hold-out reasons: the child stays in this item and stays selectable for Keep
# current and Defer.
NOT_READY_NO_INCOMING_FACT = (
    "the incoming value has no captured Source Fact to make effective"
)
NOT_READY_NO_SUPPORT = (
    "no effective Support Assessment records that the source supports this value"
)

# The batch outcomes the source-revision screen offers.  Edit and apply and
# Needs coordination are per-child answers rather than batch ones, so they
# belong to the focused item below (#528, #659) and never to a batch: planning
# forty unrelated routine changes away in one act is exactly the failure this
# tuple exists to exclude, and #659 did not widen it.
BATCH_OUTCOMES = (APPLY, KEEP_CURRENT, DEFER)

# The focused item's outcomes: ADR-0085's four primary decisions in project
# language, plus the secondary dated Defer.  A child a coordinator has not
# answered is simply left open, which is why "leave open" is the absence of a
# decision rather than a sixth one.
LEAVE_OPEN = "leave_open"
FOCUSED_OUTCOMES = (
    APPLY,
    KEEP_CURRENT,
    EDIT_AND_APPLY,
    NEEDS_COORDINATION,
    DEFER,
)

# Why one focused answer could not be built, in the same voice as the rest of
# the screen's refusals.  Each is a shape a person can correct without losing
# what they had already chosen.
NEEDS_QUESTION = (
    "Needs coordination records the exact question that must be answered "
    "before this value can be accepted."
)
NEEDS_OWNER = "Needs coordination records who owes the answer."
NEEDS_RETURN_DATE = "Defer records the date this comes back."
NEEDS_CHOSEN_SOURCE = (
    "Edit and apply records which captured source value becomes the accepted "
    "one; it can never be free text for an external fact (ADR-0084)."
)
RIVAL_ANSWERS = (
    "Only one of these sources can become the accepted value for a field. "
    "Choose one to apply and answer the others."
)
NOT_A_BATCH = (
    "this change is answered on its own, not as one outcome over a batch: a "
    "cross-source question is answered against every source that raised it, "
    "and a change held out of its source revision is answered by itself"
)

# Only a link a browser can follow read-only is rendered as a link.
_LINKABLE_SCHEMES = frozenset({"http", "https"})

# What a source row says when the incoming value carries no cited location.
NOT_CITED = "no cited location recorded"


class ReviewScreenRefused(ValueError):
    """The caller cannot build or act on this coordinator reading.

    A refusal about one child's own answer carries which child and which
    control, so the screen can link the message to the input that holds it
    without deciding anything a second time.  The rule lives here; the screen
    only renders what it said.
    """

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


# The per-child controls a focused answer can be refused against.
CONTROL_OUTCOME = "outcome"
CONTROL_SOURCE = "source"
CONTROL_QUESTION = "question"
CONTROL_RESPONSIBLE = "responsible"
CONTROL_RETURN = "return"


# --- what one child shows --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalRecordLink:
    """One identifier or link the customer's own row already printed.

    ``href`` is present only for an ``http``/``https`` location, so a stored
    value that is not a followable web address is shown as text rather than
    rendered as a link to nowhere.  Nothing here is a map; a map waits for
    partner data.
    """

    role: str
    text: str
    href: str | None = None
    source_row_id: int | None = None


@dataclass(frozen=True, slots=True)
class IncomingCapture:
    """The captured Source Fact behind one incoming value, and where it was read."""

    fact: Fact
    segment: SourceSegment | None
    document: Document | None


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Exactly where the incoming value was read, in words."""

    document_filename: str
    sheet_name: str | None = None
    cell_range: str | None = None
    page_no: int | None = None
    exact_text: str | None = None

    @property
    def location(self) -> str:
        """The locator as a coordinator reads it aloud."""

        if self.sheet_name and self.cell_range:
            return f"sheet {self.sheet_name}, cell {self.cell_range}"
        if self.page_no is not None:
            return f"page {self.page_no}"
        return "no cited location recorded"


@dataclass(frozen=True, slots=True)
class AcceptedPosition:
    """What the accepted record says today about one subject and field.

    The focused item shows this beside every source that disagrees with it, so
    the coordinator is answering "which of these is right" against the record
    rather than against the newest arrival.
    """

    subject_identity: str
    subject_name: str
    field: str | None
    field_name: str
    value: str | None
    revision_id: int | None

    @property
    def text(self) -> str:
        return self.value if self.value is not None else "not recorded"


@dataclass(frozen=True, slots=True)
class SourceAnswer:
    """What one retained source says, kept whole beside the others.

    ADR-0085 forbids collapsing two retained sources into one incoming value,
    so a focused item carries one of these per child rather than a merged row.
    """

    delta_id: int
    subject_name: str
    field_name: str
    source: str
    document_filename: str
    location: str
    quote: str | None
    value: str | None
    external_links: tuple[ExternalRecordLink, ...]
    not_ready_reason: str | None

    @property
    def ready(self) -> bool:
        return self.not_ready_reason is None


@dataclass(frozen=True, slots=True)
class CommitmentScopeRow:
    """One Utility Conflict inside a commitment's Applies To scope.

    A conflict a later contradiction or a hold-out took to its own item stays
    listed here, naming that item, so the commitment's accounting is complete
    on the screen that decides it.
    """

    subject_identity: str
    subject_name: str
    fields: tuple[str, ...]
    held_out_reason: str | None = None
    held_out_item_key: str | None = None

    @property
    def decided_here(self) -> bool:
        return self.held_out_reason is None


@dataclass(frozen=True, slots=True)
class ChildReading:
    """One Proposed Delta, as the coordinator sees it on the item."""

    delta_id: int
    subject_identity: str
    subject_name: str
    field: str | None
    field_name: str
    change_type: str
    accepted_value: str | None
    accepted_revision_id: int | None
    incoming_value: str | None
    source_family: str
    source_revision: str
    band: str
    attention_reasons: tuple[str, ...]
    customer_artifacts: tuple[str, ...]
    source: SourceReference | None = None
    external_links: tuple[ExternalRecordLink, ...] = ()
    incoming_fact_id: int | None = None
    support_assessment_ids: tuple[int, ...] = ()
    not_ready_reason: str | None = None
    # Whether this child is in the coordinator's current selection. A child
    # that Apply would refuse starts unselected, so the batch's primary action
    # never names work it cannot do.
    selected: bool = False
    # Set only on a child this item lists read-only because another item owns
    # the decision.
    held_out_reason: str | None = None
    held_out_item_key: str | None = None
    # ADR-0085's visible level for this one change, with its own reasons.
    # ``None`` where the project's issue profile cannot be executed as
    # configured (#641): an absent heading, never a default one.
    consequence: ConsequenceLevel | None = None

    @property
    def consequence_heading(self) -> str | None:
        """The accepted ADR-0085 heading this change is shown under."""

        return self.consequence.heading if self.consequence is not None else None

    @property
    def consequence_reasons(self) -> tuple[str, ...]:
        """Why this change is at that level, in its own words (#641)."""

        return self.consequence.reasons if self.consequence is not None else ()

    @property
    def ready(self) -> bool:
        """Whether Apply would not be refused for missing fact or support."""

        return self.not_ready_reason is None

    @property
    def id(self) -> int:
        """The identity the shared selection primitive addresses a child by."""

        return self.delta_id

    @property
    def text(self) -> str:
        """One line naming the change, for the selection list."""

        return (
            f"{self.subject_name} — {self.field_name}: "
            f"{self.accepted_value or 'not recorded'} → "
            f"{self.incoming_value or 'not recorded'}"
        )

    @property
    def held_out(self) -> str | None:
        """The shared selection primitive's reason for disabling a child."""

        return self.held_out_reason


# --- what one item shows ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemReading:
    """One bounded Work List item, in project language."""

    actionable: ActionableItem
    headline: str
    source_family: str
    source_revision: str
    children: tuple[ChildReading, ...]
    held_out_children: tuple[ChildReading, ...]
    unchanged_count: int
    customer_artifacts: tuple[str, ...]
    artifact_rule_version: str = ARTIFACT_IMPACT_RULE_VERSION
    consequence_rule_version: str = CONSEQUENCE_RULE_VERSION

    @property
    def consequence(self) -> str | None:
        """The highest visible level among this item's own children (#641).

        A packet may headline its children's highest consequence and is never
        split because they differ (ADR-0085), so this is derived from the
        children rather than stored beside them.  Every surface reads it from
        here; none derives a level of its own, which is how the review screen
        and the project's week cannot disagree about one packet.
        """

        return headline_level(child.consequence for child in self.children)

    @property
    def consequence_heading(self) -> str | None:
        """That level as ADR-0085's own accepted heading, or nothing."""

        level = self.consequence
        return LEVEL_HEADINGS[level] if level is not None else None

    @property
    def ordinal(self) -> int:
        return self.actionable.ordinal

    @property
    def item_key(self) -> str:
        return self.actionable.item_key

    @property
    def grouping_key_kind(self) -> str:
        return self.actionable.grouping_key_kind

    @property
    def grouping_key(self) -> str:
        return self.actionable.grouping_key

    @property
    def grouping_rule_version(self) -> str:
        return self.actionable.grouping_rule_version

    @property
    def band(self) -> str:
        return self.actionable.band

    @property
    def attention_reasons(self) -> tuple[str, ...]:
        return self.actionable.attention_reasons

    @property
    def held_out_reason(self) -> str | None:
        return self.actionable.held_out_reason

    @property
    def child_count(self) -> int:
        return len(self.children)

    @property
    def ready_count(self) -> int:
        return sum(1 for child in self.children if child.ready)

    @property
    def held_out_count(self) -> int:
        return len(self.held_out_children)

    @property
    def batched(self) -> bool:
        """Whether one outcome answers every selected child at once (#527).

        Only a source revision's own changes are alike enough for that: they
        arrived together, they fail the same way, and Apply means the same
        thing for each.  A delta the partition *held out* of that batch is by
        construction the one that does not fail the same way, so it is not a
        one-row batch — it is a focused item, below.
        """

        return (
            self.grouping_key_kind == SOURCE_REVISION
            and self.held_out_reason is None
        )

    @property
    def focused(self) -> bool:
        """Whether each child carries its own answer (#528, #659).

        A cross-source coordination question and a shared commitment are both
        answered child by child — one source's value may become the accepted
        one while another's is kept out, and that is one act, not two.

        A **held-out single-source item** is focused for a different reason and
        reaches the same controls.  It carries exactly one change, and the
        partition held it out precisely because the change raises a question an
        exact cell comparison cannot settle: the row is apparently gone, the
        source proposes a Utility Conflict the record does not hold, an
        organization changed without saying whether that corrects a name or
        records a transfer, or the scope is not settled.  Each of those is a
        coordination question with no contradiction anywhere, and before #659
        the only answer the screen offered for one was Apply, Keep current, or
        Defer — so the coordinator's real answer, "somebody owes me an answer
        before this can be accepted", had to be spelled as a date.  That is why
        every Follow-up Plan the product could create was a Source Discrepancy.

        Answering it per child is not a widening of the batch: it is one delta,
        and ``BATCH_OUTCOMES`` still excludes Needs coordination so that forty
        unrelated routine changes can never be planned away in one act.
        """

        return (
            self.grouping_key_kind in (COORDINATION_QUESTION, SHARED_COMMITMENT)
            or self.held_out_reason is not None
        )

    @property
    def decidable(self) -> bool:
        """Whether this item carries decision controls at all.

        Every item does now; ADR-0085's exactly-once rule is kept by opening
        exactly one item at a time rather than by leaving a kind of item
        undecidable.
        """

        return self.batched or self.focused

    @property
    def commitment(self) -> bool:
        """Whether this item is one attributable commitment's own scope."""

        return self.grouping_key_kind == SHARED_COMMITMENT

    @property
    def identity_question(self) -> bool:
        """Whether the sources disagree about which utility this row is."""

        return self.key_reason == KEY_CONTRADICTED_IDENTITY

    @property
    def key_reason(self) -> str:
        return self.actionable.key_reason

    @property
    def key_words(self) -> str:
        """Why the rule keyed this item this way, in one sentence."""

        return KEY_REASONS[self.key_reason]

    @property
    def accepted_positions(self) -> tuple[AcceptedPosition, ...]:
        """What the record says today, once per subject and field this touches.

        A cross-source question has one: the accepted value the disagreeing
        sources are both arguing about.  A shared commitment has one per
        Utility Conflict inside its scope.
        """

        seen: dict[tuple[str, str | None], AcceptedPosition] = {}
        for child in self.children:
            key = (child.subject_identity, child.field)
            if key in seen:
                continue
            seen[key] = AcceptedPosition(
                subject_identity=child.subject_identity,
                subject_name=child.subject_name,
                field=child.field,
                field_name=child.field_name,
                value=child.accepted_value,
                revision_id=child.accepted_revision_id,
            )
        return tuple(seen.values())

    @property
    def source_answers(self) -> tuple[SourceAnswer, ...]:
        """Each source's own answer, side by side and never merged into one."""

        return tuple(
            SourceAnswer(
                delta_id=child.delta_id,
                subject_name=child.subject_name,
                field_name=child.field_name,
                source=f"{child.source_family} {child.source_revision}",
                document_filename=(
                    child.source.document_filename
                    if child.source is not None
                    else NOT_CITED
                ),
                location=(
                    child.source.location if child.source is not None else NOT_CITED
                ),
                quote=child.source.exact_text if child.source is not None else None,
                value=child.incoming_value,
                external_links=child.external_links,
                not_ready_reason=child.not_ready_reason,
            )
            for child in self.children
        )

    @property
    def scope_subjects(self) -> tuple[CommitmentScopeRow, ...]:
        """The Utility Conflicts this item's own decision reaches, enumerated.

        For a shared commitment this is the Applies To scope the coordinator is
        answering over: every conflict the statement moved that is still
        offered here, and every one that left for its own item.
        """

        fields: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        left: dict[str, ChildReading] = {}
        for child in (*self.children, *self.held_out_children):
            names.setdefault(child.subject_identity, child.subject_name)
            seen = fields.setdefault(child.subject_identity, [])
            if child.field_name not in seen:
                seen.append(child.field_name)
            if child.held_out_reason is not None:
                left.setdefault(child.subject_identity, child)
        return tuple(
            CommitmentScopeRow(
                subject_identity=identity,
                subject_name=names[identity],
                fields=tuple(fields[identity]),
                held_out_reason=(
                    left[identity].held_out_reason if identity in left else None
                ),
                held_out_item_key=(
                    left[identity].held_out_item_key if identity in left else None
                ),
            )
            for identity in fields
        )

    def alternatives_for(self, delta_id: int) -> tuple[ChildReading, ...]:
        """The other captured source values this child's field could take.

        ADR-0084 forbids settling an external fact with typed words, so an
        Edit and apply here is never free text: it is one of the *other*
        sources on this very item, chosen explicitly and applied with that
        source's own captured Source Fact as its basis.
        """

        this = next(
            (child for child in self.children if child.delta_id == delta_id), None
        )
        if this is None:
            return ()
        return tuple(
            child
            for child in self.children
            if child.delta_id != delta_id
            and child.subject_identity == this.subject_identity
            and child.field == this.field
            and child.incoming_fact_id is not None
        )

    @property
    def selected_count(self) -> int:
        return sum(1 for child in self.children if child.selected)

    @property
    def selection_children(self) -> tuple[ChildReading, ...]:
        """Every delta of this source revision: this item's, then its siblings'.

        The siblings are listed disabled with the reason they were held out, so
        the revision's accounting is complete on one screen and no held-out
        delta is ever submitted by this item's action.
        """

        return self.children + self.held_out_children

    @property
    def attention_sentences(self) -> tuple[str, ...]:
        """Why this item is in front of the coordinator, in #494's own words."""

        return tuple(
            BAND_SENTENCES[name]
            for name in self.attention_reasons
            if name in BAND_SENTENCES
        )

    @property
    def held_out_words(self) -> str | None:
        """This item's own hold-out reason as a sentence, where it has one."""

        if self.held_out_reason is None:
            return None
        return HELD_OUT_WORDS.get(self.held_out_reason, self.held_out_reason)

    @property
    def before_after_rows(self) -> tuple[dict[str, str], ...]:
        """The accepted and incoming values, in the shared primitive's shape."""

        return tuple(
            {
                "field": f"{child.subject_name} — {child.field_name}",
                "before": child.accepted_value,
                "after": child.incoming_value,
                "source": (
                    f"{child.source.document_filename}, {child.source.location}"
                    if child.source is not None
                    else ""
                ),
            }
            for child in self.children
        )

    @property
    def subject_names(self) -> tuple[str, ...]:
        """Every Utility Conflict this item touches, once each, in order."""

        seen: list[str] = []
        for child in self.children:
            if child.subject_name not in seen:
                seen.append(child.subject_name)
        return tuple(seen)

    @property
    def field_names(self) -> tuple[str, ...]:
        """Every affected field, once each, in order."""

        seen: list[str] = []
        for child in self.children:
            if child.field_name not in seen:
                seen.append(child.field_name)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class ReviewReading:
    """One project's actionable items, assembled for one coordinator."""

    project_id: int
    as_of: datetime
    rule_version: str
    accepted_revision_id: int | None
    items: tuple[ItemReading, ...]
    reading: DeltaReading
    # The one resolution of what this project is configured to issue at this
    # cutoff, and of the customer policy over it (#641).  Every level below
    # was derived from this object; a surface that wants to explain a level, or
    # to say why there is none, reads its problems rather than asking again.
    issue_content: EffectiveIssueContent | None = None

    def standing_sentence(self, delta_id: int) -> str:
        """Why this proposed change is no longer offered, in the reading's terms.

        A coordinator whose Save was refused needs the *reason* their reading
        went out of date, not the identifier of a row they never saw. Every
        answer comes from #494's own deterministic exits.
        """

        for standing in self.reading.standings:
            if standing.delta_id != delta_id:
                continue
            if standing.standing == RESOLVED:
                return "it was already decided since this reading was taken"
            if standing.standing == SUPERSEDED:
                return (
                    "a newer source version replaced it "
                    f"(proposed change {standing.superseded_by_delta_id})"
                )
            if standing.standing == STALE:
                return (
                    "the accepted value it was compared against moved from "
                    f"revision {standing.baseline_revision} to revision "
                    f"{standing.current_accepted_revision_id}"
                )
            if standing.standing == DEFERRED:
                return (
                    "it was deferred and returns on "
                    f"{standing.returns_at.date() if standing.returns_at else 'a wake condition'}"
                )
            return "it is offered on a different item under this reading"
        return "it is no longer one of this project's open changes"

    def item(self, item_key: str) -> ItemReading | None:
        for item in self.items:
            if item.item_key == item_key:
                return item
        return None

    @property
    def actionable_delta_ids(self) -> tuple[int, ...]:
        return self.reading.actionable_delta_ids


# --- the reading -----------------------------------------------------------


def select_children(
    item: ItemReading, delta_ids: Sequence[int] | None
) -> ItemReading:
    """Mark exactly the children a coordinator chose, preserving their order.

    ``None`` keeps the reading's own default — every child Apply would not
    refuse. A refused Save passes the submitted set back, so a resubmission
    starts from what the person had chosen rather than from the default.
    """

    if delta_ids is None:
        return item
    chosen = set(delta_ids)
    return replace(
        item,
        children=tuple(
            replace(child, selected=child.delta_id in chosen)
            for child in item.children
        ),
    )


def read_review_items(
    session: Session,
    *,
    project_id: int,
    as_of: datetime,
    rule_version: str = PARTITION_RULE_VERSION,
) -> ReviewReading:
    """Assemble every actionable item of one project for presentation."""

    reading = read_open_deltas(
        session, project_id=project_id, as_of=as_of, rule_version=rule_version
    )
    deltas = _deltas_by_id(session, project_id, reading.actionable_delta_ids)
    standing = standing_accepted_revisions(session, project_id=project_id)
    documents = _lineage_documents(session, project_id)
    incoming = _incoming_facts(session, project_id, deltas.values(), documents)
    support = _value_support(session, project_id, incoming.values())
    rows = _baseline_rows(session, project_id)
    artifacts = _configured_artifacts(session, project_id)
    # What this project is configured to externally issue at this very cutoff,
    # resolved once for the whole reading (#640, #641).  Two surfaces cannot
    # disagree about a packet's level because neither derives one: both read
    # the levels this single resolution produced.
    issue_content = effective_issue_content(
        session, effective_issue_inventory(session, project_id, as_of)
    )
    # Which differences arrived after the coverage boundary this project's
    # issue was confirmed under (#675). Empty where no coverage has been
    # confirmed and for every delta whose Source Fact dereferences no
    # delivery, which is the honest answer in both cases.
    outside_boundary = deltas_outside_coverage_boundary(
        session,
        project_id=project_id,
        delta_ids=reading.actionable_delta_ids,
        declaration=latest_declaration(session, project_id=project_id, cutoff=as_of),
    )
    reasons = {
        standing_row.delta_id: standing_row.attention_reasons
        for standing_row in reading.standings
    }
    bands = {
        standing_row.delta_id: standing_row.band
        for standing_row in reading.standings
    }
    unchanged = _unchanged_counts(session, project_id, standing, documents)

    by_lineage: dict[str, list[ActionableItem]] = defaultdict(list)
    for item in reading.items:
        by_lineage[item.grouping_key].append(item)
    commitment_of = {
        standing.delta_id: standing.commitment_key
        for standing in reading.standings
        if standing.commitment_key is not None
    }
    item_of = {
        delta_id: item for item in reading.items for delta_id in item.delta_ids
    }

    # Each actionable delta is read once, whichever item names it.
    readings = {
        delta_id: _child(
            deltas[delta_id],
            standing=standing,
            incoming=incoming,
            support=support,
            rows=rows,
            artifacts=artifacts,
            band=bands[delta_id],
            attention_reasons=reasons.get(delta_id, ()),
            issue_content=issue_content,
            outside_boundary=outside_boundary,
        )
        for delta_id in reading.actionable_delta_ids
    }

    items: list[ItemReading] = []
    for item in reading.items:
        children = tuple(readings[delta_id] for delta_id in item.delta_ids)
        # Only the source revision's batch names its held-out siblings. A
        # focused one-delta item already says, in its own words, which batch it
        # was held out of; listing the whole revision beside it would put five
        # hundred read-only rows under one change.
        siblings: tuple[ChildReading, ...] = ()
        if item.held_out_reason is None and item.grouping_key_kind == SOURCE_REVISION:
            siblings = tuple(
                _held_out_sibling(readings[delta_id], other)
                for other in by_lineage[item.grouping_key]
                if other.item_key != item.item_key
                for delta_id in other.delta_ids
            )
        elif item.grouping_key_kind == COORDINATION_QUESTION:
            # Everything else bearing on this very subject and field, listed
            # read-only: a source split out for proposing a materially
            # different action still belongs in front of the person answering
            # the question, because "every passage together" is the whole
            # point of consolidating them.
            asked = {
                (child.subject_identity, child.field) for child in children
            }
            siblings = tuple(
                _held_out_sibling(readings[delta_id], item_of[delta_id])
                for delta_id in reading.actionable_delta_ids
                if item_of[delta_id].item_key != item.item_key
                and (
                    readings[delta_id].subject_identity,
                    readings[delta_id].field,
                )
                in asked
            )
        elif item.grouping_key_kind == SHARED_COMMITMENT:
            # A commitment names every Utility Conflict its own statement
            # moved, including the ones a later contradiction or a hold-out
            # took to their own items, so its Applies To scope is complete on
            # the screen that decides it.
            siblings = tuple(
                _held_out_sibling(readings[delta_id], item_of[delta_id])
                for delta_id, commitment in sorted(commitment_of.items())
                if commitment == item.grouping_key
                and item_of[delta_id].item_key != item.item_key
            )
        family, _, revision = item.grouping_key.partition("@")
        items.append(
            ItemReading(
                actionable=item,
                headline=_headline(item, children),
                source_family=family,
                source_revision=revision,
                children=children,
                held_out_children=siblings,
                unchanged_count=unchanged.get(item.grouping_key, 0),
                customer_artifacts=_union(
                    child.customer_artifacts for child in children
                ),
            )
        )

    return ReviewReading(
        project_id=project_id,
        as_of=as_of,
        rule_version=reading.rule_version,
        accepted_revision_id=reading.accepted_revision_id,
        items=tuple(items),
        reading=reading,
        issue_content=issue_content,
    )


def _headline(item: ActionableItem, children: Sequence[ChildReading]) -> str:
    """Name the item's own subject, never its packet type (ADR-0085)."""

    family, _, revision = item.grouping_key.partition("@")
    if item.grouping_key_kind == SHARED_COMMITMENT:
        names = []
        for child in children:
            if child.subject_name not in names:
                names.append(child.subject_name)
        listed = ", ".join(names[:3])
        if len(names) > 3:
            listed = f"{listed} and {len(names) - 3} more"
        return f"One commitment covering {listed}"
    if item.grouping_key_kind == COORDINATION_QUESTION:
        subject = children[0].subject_name if children else item.grouping_key
        field = children[0].field_name if children else "a recorded value"
        if item.key_reason == KEY_CONTRADICTED_IDENTITY:
            return f"{subject} — sources disagree about which utility this is"
        return f"{subject} — sources disagree about {field}"
    if item.held_out_reason is not None and children:
        child = children[0]
        return (
            f"{child.subject_name} — {child.field_name} in "
            f"{family} {revision}"
        )
    return f"{family} {revision}"


# --- one child -------------------------------------------------------------


def _child(
    delta: ProposedDelta,
    *,
    standing: Mapping[tuple[str, str], int],
    incoming: Mapping[int, IncomingCapture],
    support: Mapping[int, tuple[int, ...]],
    rows: Mapping[str, BaselineSourceRow],
    artifacts: Mapping[str, str | None],
    band: str,
    attention_reasons: tuple[str, ...],
    issue_content: EffectiveIssueContent,
    outside_boundary: frozenset[int],
) -> ChildReading:
    capture = incoming.get(delta.id)
    support_ids = support.get(capture.fact.id, ()) if capture is not None else ()
    row = rows.get(delta.target_subject_identity)
    if capture is None:
        not_ready: str | None = NOT_READY_NO_INCOMING_FACT
    elif not support_ids:
        not_ready = NOT_READY_NO_SUPPORT
    else:
        not_ready = None
    return ChildReading(
        delta_id=delta.id,
        subject_identity=delta.target_subject_identity,
        subject_name=_subject_name(delta.target_subject_identity, row),
        field=delta.target_field,
        field_name=(
            field_label(delta.target_field)
            if delta.target_field
            else "the whole row"
        ),
        change_type=delta.change_type,
        accepted_value=_value_text(delta.accepted_value),
        accepted_revision_id=standing.get(
            (delta.target_subject_identity, delta.target_field or "")
        ),
        incoming_value=_value_text(delta.proposed_value),
        source_family=delta.source_family,
        source_revision=delta.source_revision,
        band=band,
        attention_reasons=attention_reasons,
        customer_artifacts=_artifacts_for(delta.target_field, artifacts),
        source=_source_reference(capture),
        external_links=_external_links(row),
        incoming_fact_id=capture.fact.id if capture is not None else None,
        support_assessment_ids=support_ids,
        not_ready_reason=not_ready,
        selected=not_ready is None,
        # Every child listed here is actionable, so the decision a customer
        # policy waits on is by construction unsettled; #529 asks the same
        # question about differences that have been decided and passes its own
        # answer (#641).
        consequence=consequence_level(
            issue_content,
            ChangeFacts(
                field=delta.target_field,
                change_type=delta.change_type,
                attention_reasons=attention_reasons,
            ),
            decision_settled=False,
            # ADR-0085's cutoff limb, answered by `issue_coverage` against the
            # confirmed Source Delivery watermark and never by comparing
            # `ProposedDelta.created_at` to anything (#675).
            source_outside_coverage_boundary=delta.id in outside_boundary,
        ),
    )


def _held_out_sibling(child: ChildReading, owner: ActionableItem) -> ChildReading:
    """The same child, marked as another item's decision and never this one's."""

    reason = (
        HELD_OUT_WORDS.get(owner.held_out_reason, owner.held_out_reason)
        if owner.held_out_reason is not None
        else CONTRADICTED_ELSEWHERE
    )
    return replace(
        child,
        selected=False,
        held_out_reason=reason,
        held_out_item_key=owner.item_key,
    )


def _subject_name(subject_identity: str, row: BaselineSourceRow | None) -> str:
    """The Utility Conflict in the customer's own words where they exist."""

    if row is None:
        return subject_identity
    if row.business_identity:
        return f"{row.business_identity} ({row.sheet_name} row {row.row_number})"
    return f"{row.sheet_name} row {row.row_number}"


def _value_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _source_reference(capture: IncomingCapture | None) -> SourceReference | None:
    if capture is None or capture.document is None:
        return None
    segment = capture.segment
    return SourceReference(
        document_filename=capture.document.filename,
        sheet_name=segment.sheet_name if segment is not None else None,
        cell_range=segment.cell_range if segment is not None else None,
        page_no=segment.page_no if segment is not None else None,
        exact_text=segment.exact_text if segment is not None else None,
    )


def _external_links(row: BaselineSourceRow | None) -> tuple[ExternalRecordLink, ...]:
    """The utility-management-system id and document link the row printed."""

    if row is None:
        return ()
    links: list[ExternalRecordLink] = []
    if row.external_system_id:
        links.append(
            ExternalRecordLink(
                role="external_system_id",
                text=row.external_system_id,
                href=_linkable(row.external_system_id),
                source_row_id=int(row.id),
            )
        )
    if row.source_url:
        links.append(
            ExternalRecordLink(
                role="source_url",
                text=row.source_url,
                href=_linkable(row.source_url),
                source_row_id=int(row.id),
            )
        )
    return tuple(links)


def _linkable(value: str) -> str | None:
    """A followable web address, or ``None`` for anything else."""

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() in _LINKABLE_SCHEMES and parsed.netloc:
        return value.strip()
    return None


# --- customer artifacts ----------------------------------------------------


def _configured_artifacts(session: Session, project_id: int) -> dict[str, str | None]:
    """What this project has actually registered to produce."""

    formats = effective_baseline_formats(session, project_id)
    template = formats.get("output_template")
    return {
        "output_template": (
            f"{template.format_identity} {template.format_version}"
            if template is not None
            else None
        )
    }


def _artifacts_for(
    field: str | None, artifacts: Mapping[str, str | None]
) -> tuple[str, ...]:
    """Which customer artifacts one field's change would reach."""

    names: list[str] = []
    template = artifacts.get("output_template")
    if template is not None and field is not None and field in BASELINE_FACT_FIELDS:
        names.append(f"{CUSTOMER_WORKBOOK} ({template})")
    names.append(WEEKLY_REPORT)
    return tuple(names)


def _union(groups: Any) -> tuple[str, ...]:
    """Every artifact any child would reach, once each, in the rule's order."""

    seen: list[str] = []
    for group in groups:
        for name in group:
            if name not in seen:
                seen.append(name)
    return tuple(seen)


# --- the queries behind the reading ---------------------------------------


def _deltas_by_id(
    session: Session, project_id: int, delta_ids: Sequence[int]
) -> dict[int, ProposedDelta]:
    if not delta_ids:
        return {}
    return {
        delta.id: delta
        for delta in session.scalars(
            select(ProposedDelta).where(
                ProposedDelta.project_id == project_id,
                ProposedDelta.id.in_(list(delta_ids)),
            )
        ).all()
    }


def _lineage_documents(
    session: Session, project_id: int
) -> dict[str, tuple[int, ...]]:
    """The documents each source lineage was captured from."""

    documents: dict[str, list[int]] = defaultdict(list)
    for family, revision, document_id in session.execute(
        select(
            DeltaGroup.source_family,
            DeltaGroup.source_revision,
            DeltaGroup.document_id,
        ).where(DeltaGroup.project_id == project_id)
    ).all():
        if document_id is not None:
            documents[f"{family}@{revision}"].append(int(document_id))
    return {key: tuple(sorted(value)) for key, value in documents.items()}


def _incoming_facts(
    session: Session,
    project_id: int,
    deltas: Sequence[ProposedDelta],
    documents: Mapping[str, tuple[int, ...]],
) -> dict[int, IncomingCapture]:
    """The captured Source Fact each delta's incoming value came from.

    The newest Fact for the subject and field inside the delta's own source
    lineage: the delta was derived from that capture, and #519 will only make
    that exact Fact effective.
    """

    wanted = {
        delta.id: (
            documents.get(f"{delta.source_family}@{delta.source_revision}", ()),
            delta.target_subject_identity,
            delta.target_field,
        )
        for delta in deltas
        if delta.target_field is not None
    }
    document_ids = sorted(
        {value for scope, _, _ in wanted.values() for value in scope}
    )
    if not document_ids:
        return {}
    by_key: dict[tuple[int, str, str], IncomingCapture] = {}
    for fact, segment, document in session.execute(
        select(Fact, SourceSegment, Document)
        .outerjoin(
            FactSource,
            (FactSource.fact_id == Fact.id) & (FactSource.role == "value_source"),
        )
        .outerjoin(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
        .outerjoin(Document, Document.id == Fact.document_id)
        .where(
            Fact.project_id == project_id,
            Fact.document_id.in_(document_ids),
        )
        .order_by(Fact.id, FactSource.ordinal)
    ).all():
        # A later Fact for the same subject and field replaces an earlier one;
        # its first value-source segment is the locator shown beside it.
        key = (int(fact.document_id), fact.subject_key, fact.fact_type)
        if key in by_key and by_key[key].fact.id == fact.id:
            continue
        by_key[key] = IncomingCapture(fact=fact, segment=segment, document=document)

    found: dict[int, IncomingCapture] = {}
    for delta_id, (scope, subject, field_name) in wanted.items():
        for document_id in reversed(scope):
            capture = by_key.get((document_id, subject, field_name or ""))
            if capture is not None:
                found[delta_id] = capture
                break
    return found


def _value_support(
    session: Session, project_id: int, captures: Sequence[IncomingCapture]
) -> dict[int, tuple[int, ...]]:
    """The effective value-support assessments of each incoming Source Fact."""

    fact_ids = sorted({int(capture.fact.id) for capture in captures})
    if not fact_ids:
        return {}
    support: dict[int, list[int]] = defaultdict(list)
    for assessment_id, fact_id in session.execute(
        select(SupportAssessment.id, SupportAssessment.fact_id).where(
            SupportAssessment.project_id == project_id,
            SupportAssessment.proposition_kind == "source_fact",
            SupportAssessment.fact_id.in_(fact_ids),
            SupportAssessment.superseded_by.is_(None),
            SupportAssessment.evidence_role == VALUE_SUPPORT_ROLE,
            SupportAssessment.assessment.in_(SUPPORTING_ASSESSMENTS),
        )
    ).all():
        support[int(fact_id)].append(int(assessment_id))
    return {key: tuple(sorted(value)) for key, value in support.items()}


def _baseline_rows(
    session: Session, project_id: int
) -> dict[str, BaselineSourceRow]:
    """Each adopted subject's own source row, for its printed identifiers."""

    return {
        row.record_subject_key: row
        for row in session.scalars(
            select(BaselineSourceRow).where(
                BaselineSourceRow.project_id == project_id,
                BaselineSourceRow.record_subject_key.is_not(None),
            )
        ).all()
    }


def _unchanged_counts(
    session: Session,
    project_id: int,
    standing: Mapping[tuple[str, str], int],
    documents: Mapping[str, tuple[int, ...]],
) -> dict[str, int]:
    """How many values each source revision carried that the record agreed with.

    A captured value counts as unchanged when the accepted record already holds
    that subject and field and this lineage proposed no difference for it —
    which is the same comparison ``delta_generation`` made when it declined to
    append a delta, read back rather than stored.
    """

    changed: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for family, revision, subject, field_name in session.execute(
        select(
            ProposedDelta.source_family,
            ProposedDelta.source_revision,
            ProposedDelta.target_subject_identity,
            ProposedDelta.target_field,
        ).where(ProposedDelta.project_id == project_id)
    ).all():
        changed[f"{family}@{revision}"].add((subject, field_name or ""))

    counts: dict[str, int] = {}
    for lineage_key, document_ids in documents.items():
        if not document_ids:
            counts[lineage_key] = 0
            continue
        counts[lineage_key] = sum(
            1
            for subject, fact_type in session.execute(
                select(Fact.subject_key, Fact.fact_type).where(
                    Fact.project_id == project_id,
                    Fact.document_id.in_(list(document_ids)),
                    Fact.fact_type.in_(sorted(COMPARABLE_FACT_TYPES)),
                )
            ).all()
            if (subject, fact_type) in standing
            and (subject, fact_type) not in changed[lineage_key]
        )
    return counts


# --- the act this reading is committed over -------------------------------


def packet_request(
    reading: ReviewReading,
    item: ItemReading,
    *,
    outcome: str,
    principal: HumanPrincipal,
    decided_at: datetime,
    delta_ids: Sequence[int],
    deferred_until: datetime | None = None,
    deferral_reason: str | None = None,
) -> ReviewPacketRequest:
    """Build the #526 act for the children the coordinator actually selected.

    Every child is named explicitly with the source version it was read
    against; there is no wildcard and no implicit selection, and
    ``bind_packet_request`` refuses anything this item does not offer.
    """

    if outcome not in BATCH_OUTCOMES:
        raise ReviewScreenRefused(
            f"{outcome!r} is not one of this screen's batch decisions"
        )
    if not item.batched:
        raise ReviewScreenRefused(NOT_A_BATCH)
    offered = {child.delta_id: child for child in item.children}
    outside = [value for value in delta_ids if value not in offered]
    if outside:
        raise ReviewScreenRefused(
            f"deltas {sorted(outside)} are not offered by this item; a delta is "
            "actionable in exactly one item"
        )
    selected = [offered[value] for value in delta_ids]
    if not selected:
        raise ReviewScreenRefused("select at least one change before saving")
    if outcome == DEFER and deferred_until is None:
        raise ReviewScreenRefused("a Defer records the date the item returns")

    children = tuple(
        PacketChildRequest(
            delta_id=child.delta_id,
            outcome=outcome,
            observed_source_revision=child.source_revision,
            record_effects=(
                _apply_effects(child) if outcome == APPLY else ()
            ),
            support_assessment_ids=(
                child.support_assessment_ids if outcome == APPLY else ()
            ),
            deferral=(
                DeferralRequest(
                    deferred_until=deferred_until, reason=deferral_reason
                )
                if outcome == DEFER
                else None
            ),
        )
        for child in selected
    )
    return bind_packet_request(
        reading.reading,
        item.actionable,
        principal=principal,
        idempotency_key=idempotency_key(
            item,
            outcome=outcome,
            principal=principal,
            delta_ids=[child.delta_id for child in selected],
            observed_accepted_revision_id=reading.accepted_revision_id,
            deferred_until=deferred_until,
        ),
        decided_at=decided_at,
        children=children,
    )


# --- the focused act: one question, one answer per source (#528) -----------


@dataclass(frozen=True, slots=True)
class FocusedAnswer:
    """One coordinator answer to one child of a focused item.

    A child with no answer is simply left open, so the absence of a
    ``FocusedAnswer`` is the "decide this later" case and there is no sixth
    outcome meaning nothing.
    """

    delta_id: int
    outcome: str
    question: str | None = None
    responsible_principal: str | None = None
    responsible_organization: str | None = None
    return_date: datetime | None = None
    # Edit and apply only: which *other* source on this item carries the
    # captured value that becomes the accepted one.
    apply_fact_from_delta_id: int | None = None
    reason: str | None = None


def focused_request(
    reading: ReviewReading,
    item: ItemReading,
    *,
    principal: HumanPrincipal,
    decided_at: datetime,
    answers: Sequence[FocusedAnswer],
) -> ReviewPacketRequest:
    """Build the one #526 act that answers a cross-source item child by child.

    A coordination question is not a batch: one source's value may become the
    accepted one while the other's is kept out, and both halves of that are the
    same decision.  So the act carries a per-child outcome and commits as one
    Project Record revision, exactly as ADR-0085 requires and exactly as #526
    already supports.

    Everything refused here is refused *before* anything is read for writing,
    and every refusal names a shape the coordinator can correct without losing
    the answers they already gave.
    """

    if not item.focused:
        raise ReviewScreenRefused(
            "a source revision's changes are decided as one batch, not child "
            "by child"
        )
    offered = {child.delta_id: child for child in item.children}
    answered = [answer for answer in answers if answer.outcome != LEAVE_OPEN]
    if not answered:
        raise ReviewScreenRefused("answer at least one of these before saving")
    seen: set[int] = set()
    for answer in answered:
        if answer.outcome not in FOCUSED_OUTCOMES:
            raise ReviewScreenRefused(
                f"{answer.outcome!r} is not one of this screen's decisions",
                delta_id=answer.delta_id,
                control=CONTROL_OUTCOME,
            )
        if answer.delta_id not in offered:
            raise ReviewScreenRefused(
                f"proposed change {answer.delta_id} is not offered by this item; "
                "a change is actionable in exactly one item"
            )
        if answer.delta_id in seen:
            raise ReviewScreenRefused(
                f"proposed change {answer.delta_id} was answered twice"
            )
        seen.add(answer.delta_id)

    _require_one_answer_per_field(item, answered, offered)
    children = tuple(
        _focused_child(item, answer, offered[answer.delta_id])
        for answer in answered
    )
    return bind_packet_request(
        reading.reading,
        item.actionable,
        principal=principal,
        idempotency_key=focused_idempotency_key(
            item,
            principal=principal,
            answers=answered,
            observed_accepted_revision_id=reading.accepted_revision_id,
        ),
        decided_at=decided_at,
        children=children,
    )


def _require_one_answer_per_field(
    item: ItemReading,
    answered: Sequence[FocusedAnswer],
    offered: Mapping[int, ChildReading],
) -> None:
    """Two sources cannot both become the accepted value for one field.

    #526 refuses this too, and must, because both screens build acts through
    it.  Refusing here as well means the coordinator is told before anything is
    read for writing, and their other answers survive untouched.
    """

    claimed: set[tuple[str, str | None]] = set()
    for answer in answered:
        if answer.outcome not in (APPLY, EDIT_AND_APPLY):
            continue
        child = offered[answer.delta_id]
        key = (child.subject_identity, child.field)
        if key in claimed:
            raise ReviewScreenRefused(
                RIVAL_ANSWERS, delta_id=answer.delta_id, control=CONTROL_OUTCOME
            )
        claimed.add(key)


def _focused_child(
    item: ItemReading, answer: FocusedAnswer, child: ChildReading
) -> PacketChildRequest:
    """One answered child, as the exact request #526 validates and commits."""

    if answer.outcome == APPLY:
        if child.not_ready_reason is not None:
            raise ReviewScreenRefused(
                f"{child.subject_name} — {child.field_name}: "
                f"{child.not_ready_reason}",
                delta_id=child.delta_id,
                control=CONTROL_OUTCOME,
            )
        return PacketChildRequest(
            delta_id=child.delta_id,
            outcome=APPLY,
            observed_source_revision=child.source_revision,
            record_effects=_apply_effects(child),
            support_assessment_ids=child.support_assessment_ids,
            contradiction=item.grouping_key_kind == COORDINATION_QUESTION,
        )

    if answer.outcome == EDIT_AND_APPLY:
        chosen = next(
            (
                other
                for other in item.alternatives_for(child.delta_id)
                if other.delta_id == answer.apply_fact_from_delta_id
            ),
            None,
        )
        if chosen is None or chosen.incoming_fact_id is None:
            raise ReviewScreenRefused(
                NEEDS_CHOSEN_SOURCE,
                delta_id=child.delta_id,
                control=CONTROL_SOURCE,
            )
        if chosen.not_ready_reason is not None:
            raise ReviewScreenRefused(
                f"{chosen.subject_name} — {chosen.field_name}: "
                f"{chosen.not_ready_reason}",
                delta_id=child.delta_id,
                control=CONTROL_SOURCE,
            )
        return PacketChildRequest(
            delta_id=child.delta_id,
            outcome=EDIT_AND_APPLY,
            observed_source_revision=child.source_revision,
            edit_basis=CapturedSupport(fact_id=chosen.incoming_fact_id),
            support_assessment_ids=chosen.support_assessment_ids,
            effective_value=chosen.incoming_value,
            rationale=answer.reason,
            contradiction=item.grouping_key_kind == COORDINATION_QUESTION,
        )

    if answer.outcome == NEEDS_COORDINATION:
        question = (answer.question or "").strip()
        if not question:
            raise ReviewScreenRefused(
                NEEDS_QUESTION, delta_id=child.delta_id, control=CONTROL_QUESTION
            )
        responsible = (answer.responsible_principal or "").strip() or (
            answer.responsible_organization or ""
        ).strip()
        if not responsible:
            raise ReviewScreenRefused(
                NEEDS_OWNER, delta_id=child.delta_id, control=CONTROL_RESPONSIBLE
            )
        return PacketChildRequest(
            delta_id=child.delta_id,
            outcome=NEEDS_COORDINATION,
            observed_source_revision=child.source_revision,
            coordination=CoordinationRequest(
                question=question,
                responsible_principal=(answer.responsible_principal or "").strip()
                or None,
                responsible_organization=(
                    answer.responsible_organization or ""
                ).strip()
                or None,
                return_date=answer.return_date,
                affected_scope={
                    "subject_identity": child.subject_identity,
                    "field": child.field,
                },
                evidence_support_assessment_ids=child.support_assessment_ids,
            ),
        )

    if answer.outcome == DEFER:
        if answer.return_date is None:
            raise ReviewScreenRefused(
                NEEDS_RETURN_DATE, delta_id=child.delta_id, control=CONTROL_RETURN
            )
        return PacketChildRequest(
            delta_id=child.delta_id,
            outcome=DEFER,
            observed_source_revision=child.source_revision,
            deferral=DeferralRequest(
                deferred_until=answer.return_date, reason=answer.reason
            ),
        )

    return PacketChildRequest(
        delta_id=child.delta_id,
        outcome=KEEP_CURRENT,
        observed_source_revision=child.source_revision,
        contradiction=item.grouping_key_kind == COORDINATION_QUESTION,
    )


def focused_idempotency_key(
    item: ItemReading,
    *,
    principal: HumanPrincipal,
    answers: Sequence[FocusedAnswer],
    observed_accepted_revision_id: int | None,
) -> str:
    """One key per distinct focused act, so a resubmitted form replays.

    Every child's own outcome is in the key, because two acts over the same
    item that answer its children differently are two different acts (#457).
    """

    material = "\x00".join(
        [
            item.item_key,
            principal.subject,
            str(observed_accepted_revision_id or 0),
            *(
                "|".join(
                    [
                        str(answer.delta_id),
                        answer.outcome,
                        str(answer.apply_fact_from_delta_id or ""),
                        answer.return_date.isoformat()
                        if answer.return_date is not None
                        else "",
                        (answer.question or "").strip(),
                    ]
                )
                for answer in sorted(answers, key=lambda row: row.delta_id)
            ),
        ]
    )
    return f"focused-packet:{sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _apply_effects(child: ChildReading) -> tuple[RecordEffect, ...]:
    """The one Source Fact an Apply makes effective, or none to be refused by #519."""

    if child.incoming_fact_id is None:
        return ()
    return (RecordEffect(fact_id=child.incoming_fact_id),)


def idempotency_key(
    item: ItemReading,
    *,
    outcome: str,
    principal: HumanPrincipal,
    delta_ids: Sequence[int],
    observed_accepted_revision_id: int | None,
    deferred_until: datetime | None,
) -> str:
    """One key per distinct act, so a resubmitted form replays rather than doubles.

    Everything that makes the act different is in it — the item, the outcome,
    the exact children, the revision they were read against, the person, and a
    deferral's own return date — and nothing that merely makes the *request*
    different is, so a double-click writes one packet (#457).
    """

    material = "\x00".join(
        [
            item.item_key,
            outcome,
            principal.subject,
            ",".join(str(value) for value in sorted(delta_ids)),
            str(observed_accepted_revision_id or 0),
            deferred_until.isoformat() if deferred_until is not None else "",
        ]
    )
    return f"review-packet:{sha256(material.encode('utf-8')).hexdigest()[:32]}"


# --- the versioned event contract (#558) ----------------------------------


def screen_binding(
    item: ItemReading, binding: AnalyticsBinding | None = None
) -> AnalyticsBinding:
    """Bind every screen event to the packetizer rule the item was built by."""

    base = binding or default_binding()
    if base.packetizer_rules_version == item.grouping_rule_version:
        return base
    return replace(base, packetizer_rules_version=item.grouping_rule_version)


def emit_packet_surfacing(
    reading: ReviewReading,
    item: ItemReading,
    *,
    binding: AnalyticsBinding | None = None,
    principal_subject: str | None = None,
) -> None:
    """One event per item the Work List actually put in front of a person."""

    emit_event(
        AnalyticsEvent(
            family=EventFamily.PACKET_SURFACING,
            binding=screen_binding(item, binding),
            occurred_at=reading.as_of,
            payload={**_item_payload(reading, item), "principal_subject": principal_subject},
            metric_labels=_item_labels(item),
        )
    )


def emit_packet_opening(
    reading: ReviewReading,
    item: ItemReading,
    *,
    binding: AnalyticsBinding | None = None,
    principal_subject: str | None = None,
) -> None:
    """The coordinator opened this item; surfacing alone is not opening."""

    emit_event(
        AnalyticsEvent(
            family=EventFamily.PACKET_OPENING,
            binding=screen_binding(item, binding),
            occurred_at=reading.as_of,
            payload={**_item_payload(reading, item), "principal_subject": principal_subject},
            metric_labels=_item_labels(item),
        )
    )


def _item_payload(reading: ReviewReading, item: ItemReading) -> dict[str, Any]:
    """What the coordinator was shown, including the consequence they read (#641).

    The issue profile the levels were derived from travels with them — its row,
    its identity, its version and the digest of the declaration that version
    made — because a level is only interpretable against the configuration it
    projected onto, and that configuration changes by attributable act. So does
    the rule that performed the projection, and the cutoff the whole reading
    was bound to.

    These are **added to the existing family**, not published as a second one.
    ``packet_surfacing`` already means "this item was put in front of a
    person"; emitting another family because the reading learned more about the
    same event would double-count the one measure the contract exists for,
    which is the reason this module emits only two events in the first place.
    """

    content = reading.issue_content
    return {
        "project_id": reading.project_id,
        "item_key": item.item_key,
        "grouping_key_kind": item.grouping_key_kind,
        "grouping_key": item.grouping_key,
        "grouping_rule_version": item.grouping_rule_version,
        "band": item.band,
        "attention_reasons": list(item.attention_reasons),
        "held_out_reason": item.held_out_reason,
        "child_count": item.child_count,
        "ready_count": item.ready_count,
        "held_out_count": item.held_out_count,
        "unchanged_count": item.unchanged_count,
        "customer_artifacts": list(item.customer_artifacts),
        "artifact_rule_version": item.artifact_rule_version,
        "observed_accepted_revision_id": reading.accepted_revision_id,
        "cutoff": reading.as_of.isoformat(),
        "issue_profile_id": content.profile_id if content is not None else None,
        "issue_profile_identity": (
            content.profile_identity if content is not None else None
        ),
        "issue_profile_version": (
            content.profile_version if content is not None else None
        ),
        "issue_profile_sha256": (
            content.content_sha256 if content is not None else None
        ),
        # Why a level is absent, where one is. A null consequence with nothing
        # beside it would be unreadable in the series.
        "issue_profile_problems": (
            [problem.code for problem in content.problems]
            if content is not None
            else []
        ),
        "consequence_rule_version": item.consequence_rule_version,
        "consequence_level": item.consequence,
        "child_consequences": [
            {
                "delta_id": child.delta_id,
                "level": child.consequence.name
                if child.consequence is not None
                else None,
                "reasons": list(child.consequence_reasons),
            }
            for child in item.children
        ],
    }


def _item_labels(item: ItemReading) -> dict[str, str]:
    return {
        "grouping_key_kind": item.grouping_key_kind,
        "band": item.band,
        "held_out_reason": item.held_out_reason or "none",
        "consequence_level": item.consequence or "none",
    }
