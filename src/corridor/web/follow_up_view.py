"""The chase list as the Follow-up section of the project workflow reads it.

#425 built the contact-ready follow-up bundles and merged with no screen and no
route: `web/app.py` belonged to a concurrent lane, so the derivation shipped
deterministic, digested, and invisible. This module is the missing half, and it
is deliberately the smallest half it can be.

**It consumes the derivation; it does not restate it.** Every trigger, every
bundling rule and the whole ordering live in ``corridor.follow_up_bundles``,
put there so a second surface could not quietly disagree with the first. So
nothing here filters a bundle, sorts one, re-groups one, or decides that a
bundle is or is not worth showing. ``chase_view`` walks
``FollowUpReading.bundles`` once, in the order the reading produced, and
refuses outright if what it is about to render is not that sequence:
``_refuse_reordered`` compares each view's position against the ordinal the
reader itself assigned. A sort, a filter, or a "just move the overdue ones to
the top" added later raises rather than shipping a screen that says something
the reading does not.

**"No response" is not quiet.** ADR-0090 retired the legacy ``STALE`` alert
because nobody sending a document is not evidence that anybody failed to
answer. #425 kept that line by making the no-response band require a *retained
outgoing request* and the response boundary that request declared, and #837
gave a coordinator the way to record one, so the band is reachable in
production now and its discipline matters more rather than less. A screen is
exactly where that discipline is most likely to be lost — a long-quiet bundle
looks like a chase, and "waiting on a reply" is an easy caption to write. So
the no-response state here is produced from one place only, the
``unanswered_request`` band, and ``_refuse_unbacked_no_response`` refuses a
reading whose band is populated while it retained no request at all. No other
band, elapsed time, or absence can reach that wording.

**A recorded reply is not a settled question** (#837, the accepted #652
contract). The correspondence this section shows is a separate fact from the
record question a Follow-up Plan carries: recording that City Water replied
stops the literal no-response condition and resolves nothing. So the reply is
printed as what it is, and the control that would settle the question is a link
back to the coordination question on the surface that owns it, not a button
here. ``CorrespondenceView`` composes what was recorded; it decides nothing
about plans, and #835 owns plan update, cancellation, return-date change and
early resume, so no control here competes with those.

**The same words also compose the Record history page's correspondence.**
A follow-up bundle is built from the plans a week is *still* asking about, so
once every plan one message advanced has closed, that message has no bundle
left to hang on and the week renders it nowhere — the record survived in
``read_correspondence`` and in the database and reached nobody (#837, #910).
``correspondence_history`` is the composition the read-only Record history page
renders instead, and it lives here rather than in a second module because it
prints the same facts about the same records: a second set of sentences for
"they acknowledged" or "this is what it was linked to" is how one surface comes
to say something the other does not. What differs is what surrounds them — the
history rendering carries no bundle, no coverage sentence, no band and no
control, and it names each Follow-up Plan the message advanced together with
the closure that ended it, because on that page the closure is the point.

**A request covering three of a bundle's five plans says three.** One
communication advances as many Follow-up Plans as the coordinator addressed in
it, so the section names the covered ones and names the ones left uncovered
rather than letting a bundle imply that one email answered all of it. That
sentence is the whole reason the request-to-plans relation exists.

**No bundle is suppressed for want of an address** (#425 as amended
2026-09-02). Where a contact resolved, the bundle names the individual and the
channel; where none did, it names the responsible role and says plainly that no
contact is recorded. Both cases are stated in words through the shared state
primitive, so a reader in greyscale, in print, or listening is told the same
thing.

**The brief is text a coordinator can copy, and nothing more.** #425 excludes
generating an email, sending one, tracking delivery, and escalation, and the
amendment allows exactly a structured copy-ready brief. ``copy_ready_brief``
therefore composes plain text out of fields the bundle already carries — its
subject line, its ask sentence, the accepted position, the attributed
quotations, the affected Utility Conflicts, the date, and the evidence — and
adds no address line, no salutation, no send control, and no template that
could be mistaken for a message Corridor intends to deliver.

**No clock.** Overdue is the bundle's own due date against the cutoff the
reading was bound to, both of which arrive from the reading. Nothing in this
module reads the day.

Terminology: nothing here coins a customer word. Utility Conflict, Follow-up
Plan, Promised For, Required By and Source Discrepancy come from the adopted
glossary through the reading and ``corridor.presentation``; "not recorded" is
the phrase the shared primitives already print for an absent value.
"""

from __future__ import annotations

from dataclasses import dataclass

from collections.abc import Mapping, Sequence
from typing import Any

from corridor.follow_up_bundles import (
    BANDS_BY_NAME,
    CONTACT_RESOLVED,
    UNANSWERED_REQUEST_BAND,
    ConsequenceBand,
    FollowUpBundle,
    FollowUpReading,
    plan_ids_of,
)
from corridor.follow_up_plan_lifecycle import CANCELLATION_REASON_WORDS
from corridor.outgoing_requests import RecordedRequest
from corridor.presentation import label
from corridor.record_history import NamedFollowUpPlan, RetainedCorrespondence
from corridor.web.ui_primitives import NOT_RECORDED, StateLabel


class FollowUpViewRefused(ValueError):
    """The screen would state something the reading does not support."""


# The words each state prints. They are words first: colour and the mark beside
# them are redundant reinforcement (#559).
CONTACT_RECORDED = StateLabel("settled", "Contact recorded")
CONTACT_NOT_RECORDED = StateLabel("attention", f"Contact {NOT_RECORDED}")
OVERDUE = StateLabel("attention", "Past the date this bundle names")
NO_RESPONSE = StateLabel(
    "attention", "No response to a request we retained"
)
# What one recorded observation says came back. The three are the maintainer's
# own words (#652 decision of 2026-09-04, carried into #837): "whether the
# response was complete, partial, or merely acknowledged". Nothing here coins a
# customer term, and each of these sentences is a confirmation question for the
# maintainer rather than a choice this module made.
REPLY_ACKNOWLEDGED = StateLabel("settled", "They acknowledged, without answering yet")
REPLY_PARTIAL = StateLabel("settled", "They answered part of the ask")
REPLY_SUBSTANTIVE = StateLabel("settled", "They answered the ask")
REPLIES_BY_COMPLETENESS = {
    "acknowledgement": REPLY_ACKNOWLEDGED,
    "partial": REPLY_PARTIAL,
    "substantive": REPLY_SUBSTANTIVE,
}
# A record superseded by a correction. It is still shown, because an
# append-only correction that hid what it corrected would be a rewrite.
CORRECTED = StateLabel("attention", "Corrected by a later record")
AWAITING_REPLY = StateLabel("attention", "Nothing recorded back yet")
# How one Follow-up Plan a retained message named has ended, on the history
# page. The tone is neutral in both closed cases on purpose: a question that
# was cancelled or corrected is finished, and printing it in the tone the week
# uses for work still owed would turn a closed ask back into a chase. The words
# are the ones the closure act itself prints (#835), not new ones.
PLAN_CANCELLED = StateLabel("neutral", "No longer an outside ask")
PLAN_SUPERSEDED = StateLabel("neutral", "Replaced by a corrected Follow-up Plan")
PLAN_NOT_CLOSED = StateLabel(
    "attention", "No closure is recorded for this Follow-up Plan"
)
PLAN_STATES_BY_CLOSURE = {
    "cancelled": PLAN_CANCELLED,
    "superseded": PLAN_SUPERSEDED,
}

# What recording a reply did not do. The accepted #652 contract says it in as
# many words, and the history page is where a reader is most likely to read a
# recorded reply as an answer, because by then the follow-up is often over.
SETTLES_NOTHING = (
    "A recorded reply stops the no-response finding. It does not settle the "
    "question, resolve the proposed change, or change an accepted value."
)

# The one heading a bundle's anchor is built from, so the section, the test and
# a link into it all spell the same id.
ANCHOR_PREFIX = "follow-up-bundle"


# --- the sentences both surfaces print --------------------------------------
#
# The week and the Record history page say the same things about the same
# retained records, so each of these is written once. A property below returns
# one of them; nothing composes a second version of it.


def sender_sentence(request: RecordedRequest) -> str:
    """Who sent it and who says so, kept apart even when they are one person."""

    if request.recorded_by_the_sender:
        return f"Sent by {request.sent_by_principal}, who recorded it."
    return (
        f"Sent by {request.sent_by_principal}; recorded here by "
        f"{request.recorded_by_principal}."
    )


def correction_sentence(request: RecordedRequest) -> str | None:
    """What this request corrected, and why, when it is a correction."""

    if request.corrects_request_id is None:
        return None
    return (
        f"Corrects the request recorded as {request.corrects_request_id}: "
        f"{request.correction_reason}"
    )


def request_state(request: RecordedRequest) -> StateLabel:
    """What this retained request amounts to right now, in one label.

    A correction wins over a reply: a record a later one corrected is not the
    record of what came back, whatever came back against it.
    """

    if not request.stands:
        return CORRECTED
    standing = request.standing_responses
    if standing:
        return REPLIES_BY_COMPLETENESS[standing[-1].completeness]
    return AWAITING_REPLY


@dataclass(frozen=True, slots=True)
class ReplyView:
    """One recorded observation that a reply arrived, ready to print."""

    response: Any
    state: StateLabel
    evidence_sentence: str

    @property
    def correction_sentence(self) -> str | None:
        """What this corrected, and why, when it is a correction."""

        if self.response.corrects_response_id is None:
            return None
        return (
            f"Corrects the reply recorded as {self.response.corrects_response_id}: "
            f"{self.response.correction_reason}"
        )


@dataclass(frozen=True, slots=True)
class RequestView:
    """One retained request as one bundle reads it, and what came back.

    ``covered`` and ``uncovered`` are this bundle's own Follow-up Plans split
    by whether the request named them. A request that advanced three of five is
    the case the relation exists for, and this is where the page stops it from
    reading as five.
    """

    request: RecordedRequest
    covered: tuple[int, ...]
    uncovered: tuple[int, ...]
    #: Plans this message named that are no longer an outside ask at all --
    #: settled, reversed, superseded or cancelled (#835). They are printed
    #: because the message did cover them: a follow-up that shrank after the
    #: fact must not make the record of what was asked shrink with it.
    retired: tuple[int, ...]
    state: StateLabel
    replies: tuple[ReplyView, ...]

    @property
    def coverage_sentence(self) -> str:
        """Which of this bundle's plans the one communication actually covered."""

        total = len(self.covered) + len(self.uncovered)
        covered = (
            f"Covers {len(self.covered)} of the {total} Follow-up Plans in this "
            "follow-up"
        )
        if self.uncovered:
            sentence = (
                f"{covered}. Not covered by this request: "
                + ", ".join(str(plan_id) for plan_id in self.uncovered)
                + "."
            )
        else:
            sentence = f"{covered}: all of them."
        if not self.retired:
            return sentence
        count = len(self.retired)
        return (
            f"{sentence} It also named {count} Follow-up "
            f"Plan{'' if count == 1 else 's'} since retired: "
            + ", ".join(str(plan_id) for plan_id in self.retired)
            + "."
        )

    @property
    def sender_sentence(self) -> str:
        """Who sent it and who says so, kept apart even when they are one person."""

        return sender_sentence(self.request)

    @property
    def correction_sentence(self) -> str | None:
        """What this corrected, and why, when it is a correction."""

        return correction_sentence(self.request)


@dataclass(frozen=True, slots=True)
class PlanChoice:
    """One Follow-up Plan a coordinator may say a message covered.

    The shape ``ui.child_selection`` prints: an id, the sentence beside the
    checkbox, and whether it starts ticked. Every plan on the bundle starts
    ticked, because the common case is one message covering the whole
    follow-up; unticking is how a coordinator records that this one did not.
    """

    id: int
    text: str
    selected: bool = True
    held_out: str = ""


@dataclass(frozen=True, slots=True)
class CorrespondenceView:
    """What has been sent and heard back on one bundle's Follow-up Plans.

    Composition only. Nothing here decides that a plan is resolved, because a
    reply does not resolve one: the accepted #652 contract keeps the record
    question and the correspondence apart, and #835 owns the plan lifecycle.
    """

    plan_ids: tuple[int, ...]
    requests: tuple[RequestView, ...]
    plan_choices: tuple[PlanChoice, ...]

    @property
    def summary(self) -> str:
        """What this bundle's correspondence amounts to, said once."""

        if not self.plan_ids:
            return (
                "This follow-up carries no recorded Follow-up Plan, so there "
                "is nothing here to record a request against."
            )
        standing = [view for view in self.requests if view.request.stands]
        if not standing:
            return (
                "Nothing has been recorded as sent on this follow-up yet. "
                "Corridor sends nothing; send from wherever you send, then "
                "record it here."
            )
        answered = sum(1 for view in standing if view.request.answered)
        return (
            f"{len(standing)} request{'' if len(standing) == 1 else 's'} "
            f"recorded as sent, {answered} with something recorded back. "
            "A recorded reply stops the no-response finding; it does not "
            "settle the question."
        )


@dataclass(frozen=True, slots=True)
class BundleView:
    """One bundle, with the states it prints and the text it can be copied as.

    Every field is composed from the bundle beside it. There is no second
    source of truth here and nothing that could hold a different opinion about
    which bundles exist, what order they are in, or which band one is in.
    """

    bundle: FollowUpBundle
    band: ConsequenceBand
    anchor: str
    contact: StateLabel
    overdue: StateLabel | None
    no_response: StateLabel | None
    brief: str
    correspondence: CorrespondenceView

    @property
    def consequence_sentence(self) -> str:
        """Why this band is a band, in the band's own justifying sentence.

        ADR-0010 abolished the severity scalar and #425 replaced it with six
        declared bands. This prints the band's sentence and the one quantity it
        declares; it never prints a score, and there is none to print.
        """

        return self.band.sentence

    @property
    def quantity_sentence(self) -> str:
        """The band's single declared quantity, named and valued."""

        return f"{self.band.quantity.replace('_', ' ')}: {self.bundle.quantity}"


@dataclass(frozen=True, slots=True)
class ChaseView:
    """One project's chase list, ready to render and provably the reading's."""

    reading: FollowUpReading
    bundles: tuple[BundleView, ...]

    @property
    def bundle_keys(self) -> tuple[str, ...]:
        """The identities this view renders, in the order it renders them."""

        return tuple(view.bundle.bundle_key for view in self.bundles)

    @property
    def summary(self) -> str:
        """What this section is, said once above the bundles."""

        count = len(self.bundles)
        if not count:
            return (
                "Nothing the accepted record holds needs an outside answer at "
                "this cutoff."
            )
        return (
            f"{count} contact-ready follow-up{'' if count == 1 else 's'}, each "
            "one interaction: one recipient, one ask, one compatible date. "
            "Corridor sends nothing; the brief is here to be copied."
        )


def chase_view(
    reading: FollowUpReading,
    correspondence: Sequence[RecordedRequest] = (),
    plans: Mapping[int, str] | None = None,
) -> ChaseView:
    """Render-ready views of every bundle, in the reading's own order.

    ``correspondence`` is every retained request the project holds at this
    cutoff, read by ``outgoing_requests.read_correspondence``. It is handed in
    whole rather than queried per bundle so one reading answers the whole
    section, and each bundle keeps only the requests that name one of its own
    Follow-up Plans.

    ``plans`` says what each Follow-up Plan is about, in the week's own words,
    so the send form can name the plans a coordinator is ticking rather than
    their numbers. A plan the caller did not describe is still offered, by
    number, rather than being silently dropped from a form that decides what
    one message covered.
    """

    _refuse_unbacked_no_response(reading)
    views = tuple(
        _view(bundle, reading, correspondence, plans)
        for bundle in reading.bundles
    )
    _refuse_reordered(views, reading)
    return ChaseView(reading=reading, bundles=views)


def _view(
    bundle: FollowUpBundle,
    reading: FollowUpReading,
    correspondence: Sequence[RecordedRequest] = (),
    plans: Mapping[int, str] | None = None,
) -> BundleView:
    return BundleView(
        bundle=bundle,
        band=BANDS_BY_NAME[bundle.band],
        anchor=f"{ANCHOR_PREFIX}-{bundle.ordinal}",
        contact=(
            CONTACT_RECORDED
            if bundle.recipient.contact_state == CONTACT_RESOLVED
            else CONTACT_NOT_RECORDED
        ),
        overdue=(
            OVERDUE
            if bundle.due_date is not None
            and bundle.due_date < reading.cutoff_date
            else None
        ),
        # The only place this state can come from. It is the band, not the
        # silence, and the band cannot be populated without a retained
        # outgoing request behind it.
        no_response=(
            NO_RESPONSE if bundle.band == UNANSWERED_REQUEST_BAND else None
        ),
        brief=copy_ready_brief(bundle),
        correspondence=_correspondence(bundle, correspondence, plans),
    )


# --- the correspondence one bundle carries ----------------------------------


def _correspondence(
    bundle: FollowUpBundle,
    recorded: Sequence[RecordedRequest],
    plans: Mapping[int, str] | None = None,
) -> CorrespondenceView:
    """The requests that named one of this bundle's plans, and their replies.

    A request reaches a bundle only through the request-to-plans relation. It
    is never matched on the organization, the ask, or the week, because those
    are how a bundle is built and would attach one email to every follow-up
    that happened to look like it.
    """

    plan_ids = plan_ids_of(bundle)
    mine = frozenset(plan_ids)
    # A plan still on somebody's week is live whichever follow-up it sits in;
    # `plans` is the week's own outstanding set, so a plan a request named that
    # is absent from it was retired rather than merely bundled elsewhere.
    live = frozenset(plans or {})
    return CorrespondenceView(
        plan_ids=plan_ids,
        requests=tuple(
            _request_view(request, plan_ids, live)
            for request in recorded
            if mine & frozenset(request.covered_plan_ids)
        ),
        plan_choices=tuple(
            PlanChoice(
                id=plan_id,
                text=(plans or {}).get(plan_id, f"Follow-up Plan {plan_id}"),
            )
            for plan_id in plan_ids
        ),
    )


def _request_view(
    request: RecordedRequest,
    plan_ids: tuple[int, ...],
    live: frozenset[int] = frozenset(),
) -> RequestView:
    named = frozenset(request.covered_plan_ids)
    replies = tuple(_reply_view(reply) for reply in request.responses)
    return RequestView(
        request=request,
        covered=tuple(plan_id for plan_id in plan_ids if plan_id in named),
        uncovered=tuple(
            plan_id for plan_id in plan_ids if plan_id not in named
        ),
        retired=tuple(
            plan_id
            for plan_id in request.covered_plan_ids
            if plan_id not in plan_ids and plan_id not in live
        ),
        state=request_state(request),
        replies=replies,
    )


def _reply_view(reply: Any) -> ReplyView:
    return ReplyView(
        response=reply,
        state=(
            CORRECTED
            if not reply.stands
            else REPLIES_BY_COMPLETENESS[reply.completeness]
        ),
        evidence_sentence=_evidence_sentence(reply),
    )


def _evidence_sentence(reply: Any) -> str:
    """What this observation is linked to, in the words for its own kind.

    Four kinds, four sentences, and the exact reference on every one of them.
    A recorded reply with nothing behind it is the assumption ADR-0090 retired
    the legacy STALE alert for, so there is no fifth branch that prints the
    reply alone.
    """

    if reply.evidence_kind == "document":
        return (
            f"The document that came in (document {reply.document_id}) — "
            f"{reply.source_reference}"
        )
    if reply.evidence_kind == "source_delivery":
        return (
            f"The delivery it arrived in (source delivery "
            f"{reply.source_delivery_id}) — {reply.source_reference}"
        )
    if reply.evidence_kind == "source_segment":
        return (
            f"{label('cited_passage')} {reply.source_segment_id} — "
            f"{reply.source_reference}"
        )
    return (
        f"{reply.observed_by_principal} recorded what they were told: "
        f"{reply.observation} — {reply.source_reference}"
    )


# --- the correspondence the Record history page renders ----------------------
#
# Everything below composes the same retained records the section above does,
# for the read-only surface that still has them when no bundle does. It builds
# no bundle, no band and no control: the Record history page decides nothing,
# and a closed ask rendered with a chase's vocabulary would be a chase.


@dataclass(frozen=True, slots=True)
class NamedPlanView:
    """One Follow-up Plan a retained message named, and how that ask ended."""

    plan: NamedFollowUpPlan
    state: StateLabel

    @property
    def closure_sentence(self) -> str | None:
        """Who closed this ask, when, and by what act. ``None`` while open.

        It reports the closure relation's own row. Whether the plan would be
        on somebody's week today is ``outstanding_follow_up``'s to say, and
        this page does not ask it: an absent closure is printed as an absent
        closure, not as a live ask.
        """

        plan = self.plan
        if plan.closure_kind is None:
            return None
        closed = f"Closed on {plan.closed_at.date()} by {plan.closed_by_principal}"
        if plan.closure_kind == "superseded":
            return (
                f"{closed}, replaced by Follow-up Plan {plan.successor_plan_id}. "
                "The question was corrected, not abandoned."
            )
        # The relation constrains the reason to this vocabulary, so an
        # unknown one is a defect to raise rather than a token to print at a
        # coordinator (#835, ``models.delta.CANCELLATION_REASONS``).
        reason = CANCELLATION_REASON_WORDS[plan.cancellation_reason]
        return f"{closed}: {reason.lower()}."


@dataclass(frozen=True, slots=True)
class HistoryRequestView:
    """One retained request as the Record history page reads it.

    The four facts an acknowledgement leaves independently true (#652 decision
    of 2026-09-04) are four separate readings here, so none of them can be
    inferred from another: ``state`` and ``replies`` say what came back,
    ``plans`` say what became of each question it asked about,
    ``answer_sentence`` says whether the answer itself is still outstanding,
    and ``settles_nothing`` says what recording any of it did not do.
    """

    request: RecordedRequest
    plans: tuple[NamedPlanView, ...]
    state: StateLabel
    replies: tuple[ReplyView, ...]
    covered_subjects: tuple[str, ...]

    @property
    def sender_sentence(self) -> str:
        return sender_sentence(self.request)

    @property
    def correction_sentence(self) -> str | None:
        return correction_sentence(self.request)

    @property
    def settles_nothing(self) -> str:
        return SETTLES_NOTHING

    @property
    def answer_sentence(self) -> str:
        """Whether the substance of the answer is still outstanding.

        Separate from ``state`` because they are separate facts. An
        acknowledgement stops "they have not replied" being true and leaves
        the answer exactly as outstanding as it was, and a page that printed
        only the reply would let a reader take the one for the other.
        """

        standing = self.request.standing_responses
        if not standing:
            return "Nothing has been recorded back against this request."
        answered = [
            one for one in standing if one.completeness == "substantive"
        ]
        if answered:
            return f"The ask was answered on {answered[-1].received_on}."
        if any(one.completeness == "partial" for one in standing):
            return (
                "Part of the ask is recorded as answered; the rest of the "
                "answer is still outstanding."
            )
        return (
            "An acknowledgement is recorded and nothing more; the answer "
            "itself is still outstanding."
        )


def correspondence_history(
    recorded: Sequence[RetainedCorrespondence],
) -> tuple[HistoryRequestView, ...]:
    """Every retained request this project holds, ready for the history page.

    Nothing is filtered. A request whose plans have all closed is exactly the
    one this composition exists for, and one corrected by a later record is
    kept beside its correction for the reason ``read_correspondence`` keeps
    both: hiding the original would be rewriting history rather than appending
    to it.
    """

    return tuple(
        HistoryRequestView(
            request=entry.request,
            plans=tuple(_named_plan_view(plan) for plan in entry.plans),
            state=request_state(entry.request),
            replies=tuple(_reply_view(reply) for reply in entry.request.responses),
            covered_subjects=entry.covered_subjects,
        )
        for entry in recorded
    )


def _named_plan_view(plan: NamedFollowUpPlan) -> NamedPlanView:
    return NamedPlanView(
        plan=plan,
        state=(
            PLAN_NOT_CLOSED
            if plan.closure_kind is None
            else PLAN_STATES_BY_CLOSURE[plan.closure_kind]
        ),
    )


# --- the guards -------------------------------------------------------------


def _refuse_unbacked_no_response(reading: FollowUpReading) -> None:
    """Refuse a no-response band with no retained request behind it.

    The reading already refuses to *derive* one, and this refuses to *render*
    one, because the two failures are different: the first is a rule quietly
    changing, the second is a screen deciding on its own that a long silence
    reads well as a chase. Corridor retains no outgoing correspondence, so in
    production this is unreachable and the band is empty.
    """

    if reading.retained_outgoing_requests:
        return
    for bundle in reading.bundles:
        if bundle.band == UNANSWERED_REQUEST_BAND:
            raise FollowUpViewRefused(
                "a no-response bundle needs the retained outgoing request and "
                "the response boundary it declared; this reading retained "
                "none, and an empty inbox is not a fact about anybody"
            )


def _refuse_reordered(
    views: tuple[BundleView, ...], reading: FollowUpReading
) -> None:
    """Refuse to render anything but the reading's own bundles, in its order.

    #425 put trigger, bundling and ordering in one derivation exactly so a
    second surface could not disagree with it. This is that promise made
    mechanical: the ordinal the reading assigned must equal the position this
    view is about to render the bundle at, and every bundle must still be here.
    """

    if len(views) != len(reading.bundles):
        raise FollowUpViewRefused(
            "the screen renders every bundle the reading produced; a bundle is "
            "never suppressed, not even for want of an address"
        )
    for position, view in enumerate(views, start=1):
        if view.bundle.ordinal != position:
            raise FollowUpViewRefused(
                f"bundle {view.bundle.bundle_key!r} is ordinal "
                f"{view.bundle.ordinal} in the reading and would render at "
                f"{position}; ordering belongs to the reading's bands alone"
            )


# --- the copy-ready brief ---------------------------------------------------


def copy_ready_brief(bundle: FollowUpBundle) -> str:
    """The bundle as plain text a coordinator can select and copy.

    Composition only: every line comes from a field the bundle already carries.
    Nothing here is addressed, sent, tracked, or escalated — #425 places all of
    that outside the pilot, and the amendment allows exactly this structured
    brief.
    """

    lines: list[str] = [
        f"Subject: {bundle.subject_line}",
        "",
        f"To: {bundle.recipient.sentence()}",
        "",
        bundle.ask_sentence,
    ]
    if bundle.accepted_position:
        lines += ["", "What our record currently holds:"]
        lines += [
            f"  - {line.business_identity} — {line.field_name}: "
            f"{line.accepted_text or NOT_RECORDED}"
            for line in bundle.accepted_position
        ]
    if bundle.quoted_wording:
        lines += ["", "Quoted from a source, not held by our record:"]
        lines += [
            f'  - "{quote.text}" ({quote.attribution})'
            for quote in bundle.quoted_wording
        ]
    if bundle.open_questions:
        lines += ["", "The question:"]
        lines += [f"  - {question}" for question in bundle.open_questions]
    lines += [
        "",
        f"Utility Conflicts: {', '.join(bundle.affected_conflicts)}",
        (
            f"Date: {bundle.due_date.isoformat()}"
            if bundle.due_date is not None
            else f"Date: {NOT_RECORDED}"
        ),
    ]
    if bundle.references:
        lines += ["", "What this is based on:"]
        lines += [
            f"  - {reference.kind} {reference.identity} — {reference.detail}"
            for reference in bundle.references
        ]
    return "\n".join(lines)
