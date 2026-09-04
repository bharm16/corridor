"""ADR-0085's three visible consequence levels, derived at last (#641).

ADR-0085 declares three headings for the adopted-project Work List: **Must
handle before this issue**, **Affects this issue**, and **Can wait**. #536
shipped the Work List without them on purpose and said why in its own
docstring: a level is a projection of a proposed difference onto *the next
issue's declared content*, ADR-0091 made that content per-project
configuration, and the configuration was not modelled. #640 modelled it and
``issue_content`` resolves it, so the projection has something to project onto
and this module performs it.

**The words are ADR-0085's own.** ``LEVEL_HEADINGS`` holds the three accepted
labels exactly as that decision wrote them. ADR-0085 settled both the wording
and the distinction between these visible headings and the internal Attention
Reasons underneath them, and recorded that the terminology-research procedure
in `docs/agents/domain.md` is not triggered; only an *alternative* wording
would trigger it. Nothing here invents adjacent phrasing, and nothing here
replaces a band: every Attention Reason a delta carries is still recorded,
still queryable, and still printed where #527 and #528 already print it.

**A level is not a severity.** It is derived from three stored facts and
nothing else: whether an executable customer policy selects the difference,
whether the difference has already been settled, and which configured
artifacts state or disclose it. There is no score, no ranking within a level
beyond the consequence bands' own deterministic order, and no judgement
(ADR-0010).

**An unsupported configuration produces no level at all.** Where
``EffectiveIssueContent.supported`` is false — an unregistered renderer
revision, an evaluator this release does not run, a ``required_decision`` that
is prose rather than a selector — ``consequence_level`` answers ``None`` and
the caller states the configuration problem in Issue readiness instead. That is
the deliberate alternative to two tempting mistakes: calling everything "Must
handle before this issue", which asserts a customer rule nobody configured and
tells a coordinator that everything is urgent; and calling everything "Can
wait", which quietly promises that a change reaches no artifact when nobody
knows what the artifacts contain.

**What is deliberately not derived.** ADR-0085's "Can wait" has a second limb:
a difference whose *source is outside the current issue cutoff*. A Proposed
Delta records no domain instant for when its source arrived — ``created_at`` is
PostgreSQL-assigned, and #488 was rebuilt precisely to stop that column being
compared against a logical time — and the reading a level is derived over is
already bounded by the caller's declared cutoff. So the limb is absent rather
than stubbed, in the same way ``review_packet_reading`` leaves a hold-out
reason it cannot yet detect out of its vocabulary: a reading never claims a
check it did not make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from corridor.issue_content import (
    ARTIFACT_WORDS,
    ChangeFacts,
    EffectiveIssueContent,
)


# The rule that turns configured content into a visible level. Recorded beside
# every level a screen prints and every event that reports one, so a later
# change to the derivation is visible in the receipt rather than retroactive —
# the same discipline `delta-partition-v2` and `customer-artifact-impact-v1`
# already keep.
CONSEQUENCE_RULE_VERSION = "issue-consequence-v1"

MUST_HANDLE = "must_handle_before_issue"
AFFECTS_ISSUE = "affects_issue"
CAN_WAIT = "can_wait"

# In presentation order, highest consequence first.
LEVELS: tuple[str, ...] = (MUST_HANDLE, AFFECTS_ISSUE, CAN_WAIT)
LEVEL_ORDINALS: Mapping[str, int] = {name: index for index, name in enumerate(LEVELS)}

# ADR-0085's accepted labels, spelled once. A screen prints these and never a
# paraphrase of them.
LEVEL_HEADINGS: Mapping[str, str] = {
    MUST_HANDLE: "Must handle before this issue",
    AFFECTS_ISSUE: "Affects this issue",
    CAN_WAIT: "Can wait",
}

CAN_WAIT_SENTENCE = (
    "it changes nothing this project is configured to issue for this cutoff"
)


@dataclass(frozen=True, slots=True)
class ConsequenceLevel:
    """One visible level, and the reasons it is that level rather than another.

    Each proposed change keeps its own level and its own reasons. A packet may
    headline the highest level among its children, but a child never inherits
    a sibling's heading and no packet is split because its children differ —
    ADR-0085's partition is a property of what can be decided together, never
    of how the consequences read.
    """

    name: str
    reasons: tuple[str, ...]

    @property
    def heading(self) -> str:
        return LEVEL_HEADINGS[self.name]

    @property
    def ordinal(self) -> int:
        return LEVEL_ORDINALS[self.name]


def consequence_level(
    content: EffectiveIssueContent,
    change: ChangeFacts,
    *,
    decision_settled: bool = False,
) -> ConsequenceLevel | None:
    """Which of ADR-0085's three levels one proposed difference is at.

    ``decision_settled`` is whether the decision the customer's policy waits on
    has already been made. A Review child is by construction actionable, so it
    is always unsettled there; the parameter exists because #529 asks the same
    question about differences that *have* been decided when it decides whether
    a prepared candidate is blocked, and because ADR-0084 makes a dated Defer
    scheduling rather than a decision — a deferred difference a policy waits on
    still blocks the issue.

    ``None`` where the issue profile cannot be executed as configured.
    """

    if not content.supported:
        return None
    blocking = content.blocking_policies_for(change)
    if blocking and not decision_settled:
        return ConsequenceLevel(
            MUST_HANDLE,
            tuple(
                f"{policy.statement.rstrip('.')} — this project's own policy, "
                f"waiting on {policy.selector.token}, and it is not settled yet"
                for policy in blocking
            ),
        )
    stating = content.stating_artifacts(change)
    disclosing = content.disclosing_artifacts(change)
    if stating or disclosing:
        reasons = [
            f"accepting or editing it changes what "
            f"{ARTIFACT_WORDS.get(name, name)} states"
            for name in stating
        ]
        reasons.extend(
            f"{ARTIFACT_WORDS.get(name, name)} discloses this open question "
            "for this cutoff"
            for name in disclosing
        )
        return ConsequenceLevel(AFFECTS_ISSUE, tuple(reasons))
    return ConsequenceLevel(CAN_WAIT, (CAN_WAIT_SENTENCE,))


def headline_level(levels: Iterable[ConsequenceLevel | None]) -> str | None:
    """The highest level among a packet's children, or ``None``.

    ADR-0085 permits a packet to headline its children's highest consequence
    and forbids splitting one because the levels differ, so this is a reading
    over the children rather than a fourth thing a packet carries. ``None``
    where the configuration is unsupported or the packet has no children — an
    absent heading, never a default one.
    """

    ordinals = [level.ordinal for level in levels if level is not None]
    if not ordinals:
        return None
    return LEVELS[min(ordinals)]


def group_by_level(
    pairs: Sequence[tuple[object, ConsequenceLevel | None]],
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """The same items under ADR-0085's three headings, in presentation order.

    A level with nothing under it is omitted rather than printed empty, and the
    order is the declared one, so two readings of the same state group the same
    way.
    """

    collected: dict[str, list[object]] = {name: [] for name in LEVELS}
    for item, level in pairs:
        if level is None:
            continue
        collected[level.name].append(item)
    return tuple(
        (name, tuple(collected[name])) for name in LEVELS if collected[name]
    )
