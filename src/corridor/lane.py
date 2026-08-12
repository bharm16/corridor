"""What a review lane offers, and what one gesture may cover.

The rehearsal and event lanes grew inside the HTTP handlers: three
consecutive features added 296 lines to `web/app.py` and none to any
module here. The cost showed up as one rule written twice in one file —
`_sibling_revisions` decided which revisions to *offer* and the accept
handler decided which to *accept*, each with a private copy of "same
`utility_id` means same conflict" — and as rules whose only test surface
was an HTTP status code.

This module owns the lane's judgments. It does not resolve Candidates
(project and receipt scope are the caller's, and the caller is the only
one that knows which reviewer is asking) and it does not map anything to
a status code. It answers two questions: what makes two Candidates
revisions of one conflict, and whether a proposed one-gesture merge set
is coherent.
"""

from __future__ import annotations

from collections.abc import Mapping

from corridor.identity import candidate_identity
from corridor.models import Candidate


class LaneRefusal(Exception):
    """The lane refuses the gesture. Nothing has been written."""


class SiblingsNeedTheEventLane(LaneRefusal):
    """Sibling merges are the event lane's affordance, not a general one."""


class MalformedSiblingList(LaneRefusal):
    """A gesture cannot name a Candidate twice, or name the one accepted."""


class NotTheSameConflict(LaneRefusal):
    """One gesture covers one conflict."""


class ConflictUnidentified(LaneRefusal):
    """Without an identifier there is no 'same conflict' to reason about."""


def conflict_key(
    candidate: Candidate,
    schemes: Mapping[int, str],
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """What makes two Candidates revisions of one conflict.

    The registry holds no supersession chain for these documents, so no
    revision is machine-current and the reviewer's gesture is the explicit
    choice (#199). The identity is what the revisions agree on when they
    disagree about everything else — derived under each document's
    declared numbering scheme (ADR-0030), because on a per-party form the
    number alone would call nine different conflicts one.

    One definition, because the lane both offers merges and refuses them:
    a display that offers a sibling the mutation then rejects is a worse
    failure than either rule alone.
    """
    return candidate_identity(candidate, schemes, aliases)


def same_conflict(
    candidate: Candidate,
    other: Candidate,
    schemes: Mapping[int, str],
    aliases: Mapping[str, str] | None = None,
) -> bool:
    key = conflict_key(candidate, schemes, aliases)
    return key is not None and conflict_key(other, schemes, aliases) == key


def check_sibling_request(
    candidate: Candidate,
    sibling_ids: list[int],
    *,
    in_event_lane: bool,
    schemes: Mapping[int, str],
    aliases: Mapping[str, str] | None = None,
) -> None:
    """Refuse an incoherent request before anything is even looked up.

    Everything answerable from the request alone is answered here, so a
    malformed gesture is refused without the caller resolving rows it was
    never going to be allowed to touch.
    """
    if not sibling_ids:
        return

    if not in_event_lane:
        raise SiblingsNeedTheEventLane(
            "sibling merges belong to the event lane"
        )

    if candidate.id in sibling_ids or len(set(sibling_ids)) != len(sibling_ids):
        raise MalformedSiblingList(
            "a sibling list may not repeat a candidate or name the one "
            "being accepted"
        )

    if conflict_key(candidate, schemes, aliases) is None:
        raise ConflictUnidentified(
            f"candidate {candidate.id} states no identity under its "
            "document's numbering scheme — there is no conflict for a "
            "sibling to be a revision of"
        )


def check_sibling_set(
    candidate: Candidate,
    siblings: list[Candidate],
    schemes: Mapping[int, str],
    aliases: Mapping[str, str] | None = None,
) -> None:
    """Refuse the resolved set unless it is one gesture over one conflict.

    Checked in full before anything is written, so a refused sibling
    refuses the whole gesture and leaves every row pending (#199) rather
    than admitting the primary and stranding the rest.
    """
    for sibling in siblings:
        if not same_conflict(candidate, sibling, schemes, aliases):
            raise NotTheSameConflict(
                f"candidate {sibling.id} is not a revision of the same "
                "conflict — one gesture covers one conflict"
            )
