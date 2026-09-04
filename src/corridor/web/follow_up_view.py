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
outgoing request* and the response boundary that request declared, and Corridor
retains no outgoing correspondence, so ``read_retained_outgoing_requests``
truthfully returns nothing and the band is empty in production. A screen is
exactly where that discipline is most likely to be lost — a long-quiet bundle
looks like a chase, and "waiting on a reply" is an easy caption to write. So
the no-response state here is produced from one place only, the
``unanswered_request`` band, and ``_refuse_unbacked_no_response`` refuses a
reading whose band is populated while it retained no request at all. No other
band, elapsed time, or absence can reach that wording.

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

from corridor.follow_up_bundles import (
    BANDS_BY_NAME,
    CONTACT_RESOLVED,
    UNANSWERED_REQUEST_BAND,
    ConsequenceBand,
    FollowUpBundle,
    FollowUpReading,
)
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

# The one heading a bundle's anchor is built from, so the section, the test and
# a link into it all spell the same id.
ANCHOR_PREFIX = "follow-up-bundle"


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


def chase_view(reading: FollowUpReading) -> ChaseView:
    """Render-ready views of every bundle, in the reading's own order."""

    _refuse_unbacked_no_response(reading)
    views = tuple(_view(bundle, reading) for bundle in reading.bundles)
    _refuse_reordered(views, reading)
    return ChaseView(reading=reading, bundles=views)


def _view(bundle: FollowUpBundle, reading: FollowUpReading) -> BundleView:
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
