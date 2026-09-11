"""One recorded Review Packet act, read back as its own receipt (#834).

#526 built the act and the row that preserves it, and #527 built the screen
that commits one.  Between them there was nothing a coordinator could open.
The saved result said how many changes were applied and named a Project Record
revision with no link, and `reverse_review_packet` — built, tested, and the
only way to take an accidental acceptance back — had no caller outside the
tests.  A coordinator who applied the wrong batch had to ask an engineer.

So this module is the reading half of that act, and deliberately only that.

**It restates none of the act's rules.**  Every outcome, every identity and
every refusal belongs to ``corridor.review_packets``; ``packet_children`` and
``packet_reversal`` are its own readers and are the ones used here.  Nothing
in this module decides whether an act may be undone, and nothing re-derives a
child's outcome: the child row records exactly what the coordinator chose, and
this reads it.

**A Defer-only act has no revision, and says so rather than showing a blank.**
ADR-0084 settled that deferral is Work List scheduling: it writes a dated
receipt, leaves the Proposed Delta open, and writes no Project Record
revision.  A receipt whose ``revision_id`` is null is therefore complete, not
missing a value, and the reading says which of the two it is instead of
printing an empty cell that reads as a failure (ADR-0085).

**A refusal is the command's own sentence, with the machine token taken off
the front.**  ``reverse_review_packet`` refuses a stale or superseded act in
PostgreSQL, where the state can be held still, and raises
``review_packet:<code> <sentence>``; ``delta_refusals`` declares the code and
records that this family carries no fixed sentence because every message names
the receipt or delta the coordinator needs.  Re-writing those sentences here
would put a second, quietly divergent vocabulary in front of the same rule —
the failure ``delta_refusals`` exists to prevent — so the words stay the
command's and only the token is removed.

Terminology: nothing here coins a customer word.  Apply, Keep current, Edit
and apply, Needs coordination and Defer are ADR-0085's four primary decisions
and its secondary one, spelled against #526's own constants so there is no
second token vocabulary; Undo is ADR-0035's.  What this module adds is the
past tense, because a receipt reports what was decided rather than offering a
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.delta_refusals import REFUSAL_TOKEN, REVIEW_PACKET_TOKEN
from corridor.delta_resolution import Refusal
from corridor.follow_up_plan_lifecycle import ClosureRefusal
from corridor.models import (
    DeltaReviewPacketChild,
    DeltaReviewPacketReceipt,
    ProposedDelta,
)
from corridor.presentation import field_label
from corridor.record_history import subject_names
from corridor.review_packets import (
    APPLY,
    DEFER,
    EDIT_AND_APPLY,
    KEEP_CURRENT,
    NEEDS_COORDINATION,
    packet_children,
    packet_reversal,
)


# What each of ADR-0085's decisions became, once it was recorded.  The keys are
# #526's own constants rather than the same five words spelled again, so a
# token this table does not know is impossible rather than silently unlabelled.
OUTCOME_RECORDED: dict[str, str] = {
    APPLY: "Applied this source's value",
    KEEP_CURRENT: "Kept current — the accepted value stands",
    EDIT_AND_APPLY: "Applied another source's value instead",
    NEEDS_COORDINATION: "Needs coordination — someone owes an answer",
    DEFER: "Deferred until a date",
}


@dataclass(frozen=True)
class PacketChildReading:
    """One child of the act, and the outcome its own row records."""

    ordinal: int
    delta_id: int
    subject_name: str
    field_name: str
    source: str
    outcome_words: str


@dataclass(frozen=True)
class PacketReceiptReading:
    """One recorded act: who decided it, what it recorded, and each child."""

    receipt_id: int
    decided_by: str
    decided_at: datetime
    revision_id: int | None
    children: tuple[PacketChildReading, ...]
    reversed_by: str | None = None
    reversed_at: datetime | None = None

    @property
    def undone(self) -> bool:
        """Whether a compensating act has already been recorded for this one."""

        return self.reversed_at is not None


def read_packet_receipt(
    session: Session, *, project_id: int, receipt_id: int
) -> PacketReceiptReading | None:
    """One project's own recorded act, or nothing where it holds no such act.

    Project scope is part of the lookup rather than a check after it, so a
    receipt belonging to another project is indistinguishable from one that
    does not exist.
    """

    receipt = session.scalar(
        select(DeltaReviewPacketReceipt).where(
            DeltaReviewPacketReceipt.id == receipt_id,
            DeltaReviewPacketReceipt.project_id == project_id,
        )
    )
    if receipt is None:
        return None
    children = packet_children(session, receipt.id)
    deltas = {
        row.id: row
        for row in session.scalars(
            select(ProposedDelta).where(
                ProposedDelta.project_id == project_id,
                ProposedDelta.id.in_([child.delta_id for child in children] or [0]),
            )
        )
    }
    names = subject_names(session, project_id=project_id)
    reversal = packet_reversal(session, receipt.id)
    return PacketReceiptReading(
        receipt_id=receipt.id,
        decided_by=receipt.decided_by_principal,
        decided_at=receipt.decided_at,
        revision_id=receipt.revision_id,
        children=tuple(
            _child_reading(child, deltas.get(int(child.delta_id)), names)
            for child in children
        ),
        reversed_by=reversal.reversed_by_principal if reversal is not None else None,
        reversed_at=reversal.reversed_at if reversal is not None else None,
    )


def _child_reading(
    child: DeltaReviewPacketChild,
    delta: ProposedDelta | None,
    names: dict[str, str],
) -> PacketChildReading:
    subject_key = delta.target_subject_identity if delta is not None else ""
    field = delta.target_field if delta is not None else None
    return PacketChildReading(
        ordinal=child.ordinal,
        delta_id=int(child.delta_id),
        subject_name=names.get(subject_key, subject_key),
        field_name=field_label(field) if field else "the whole record row",
        source=(
            f"{delta.source_family} {delta.source_revision}"
            if delta is not None
            else child.observed_source_revision
        ),
        outcome_words=OUTCOME_RECORDED[child.outcome],
    )


def refusal_words(refusal: Refusal | ClosureRefusal) -> str:
    """The command's own refusal sentence, without its machine token.

    The token is how the two halves of one rule agree on what refused
    (``delta_refusals``); it is not a sentence, and a coordinator reading
    ``review_packet:later_act_depends`` learns nothing the words after it do
    not already say.

    Both refusal shapes of this family reach here: the packet's, which is about
    a Proposed Delta, and #835's closure, which is about a Follow-up Plan.
    Only the sentence is read, and the sentence is the command's either way.

    Either family's token is stripped, because a refusal a screen prints can
    come from either: the Work List scheduling refusals of #903 are
    ``resolve_delta`` codes raised by the command under the lock it holds, so
    they reach a page with no readable-half sentence in front of them.
    """

    return REFUSAL_TOKEN.sub(
        "", REVIEW_PACKET_TOKEN.sub("", refusal.detail)
    ).strip()
