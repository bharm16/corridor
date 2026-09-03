"""The shared presentation vocabulary the coordinator work screens render with.

Why this exists: the packet-review and project-workflow screens (#527, #528,
#536, #537) each need the same handful of presentation decisions — how a state
is named, how a before-and-after pair is read in order, which one element the
keyboard and the screen reader land on after a Save or a refusal. Left to each
screen, those decisions drift, and the drift is not cosmetic. The Constraint
log marked a late date with colour and font weight alone and recorded that
choice in a comment beside the rule, so a coordinator reading a printout, a
projector, or a screen reader was told nothing at all.

What was tried before: nothing shared. Every template carried its own colour
classes (`late`, `ready`, `notready`, `contra`, `exc`, `tag new`) and its own
banner markup, and the same state was spelled differently on each screen. A
design system was considered and rejected: this is the minimum shared layer,
server-rendered Jinja in the existing style, with no framework, component
library, build step, or client-side rendering.

Words come from `corridor.presentation`, which holds the adopted customer
vocabulary. This module never coins a domain term; it composes adopted ones.
The accessibility properties the consuming screens must satisfy are recorded in
`docs/accessibility-acceptance-checklist.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from corridor.presentation import (
    documentation_review_label,
    exception_name,
    label,
)


# One tone per consequence a screen can report, and a mark for each. The mark
# is decorative reinforcement rendered `aria-hidden`; the words carry the
# meaning, so nothing here depends on the reader seeing a colour.
MARKS: Mapping[str, str] = {
    "settled": "\N{CHECK MARK}",
    "attention": "!",
    "refused": "\N{MULTIPLICATION SIGN}",
    "neutral": "\N{EN DASH}",
}

TONES: tuple[str, ...] = tuple(MARKS)

# The CSS classes that carry a state colour. They belong to the primitives
# template alone; `tests/test_architecture.py` refuses a shared template that
# reintroduces one of its own.
STATE_CLASSES = frozenset(
    [f"state-{tone}" for tone in TONES]
    + ["outcome-saved", "outcome-refused", "error-summary"]
)

# What a before-and-after pair prints where a side has no value. An em dash
# reads as nothing at all when spoken.
NOT_RECORDED = "not recorded"


@dataclass(frozen=True)
class StateLabel:
    """One state or consequence, named in words before it is drawn in colour."""

    tone: str
    text: str

    def __post_init__(self) -> None:
        if self.tone not in MARKS:
            raise ValueError(f"unknown tone: {self.tone!r}")
        if not self.text.strip():
            raise ValueError("a state label carries its own words, never colour alone")

    @property
    def mark(self) -> str:
        """The glyph printed beside the words; never the only signal."""
        return MARKS[self.tone]


@dataclass(frozen=True)
class FieldError:
    """One refused field, addressed by the id of the control that holds it."""

    field_id: str
    message: str


# --- The consequence labels the record screens share ------------------------
#
# Each reuses vocabulary already adopted for the same condition rather than
# naming it a second way: ADR-0048 keeps one word per meaning, and the
# Constraint alert column already prints these words for the same facts.

NEXT_ACTION_OVERDUE = StateLabel("attention", exception_name("ACTION_OVERDUE"))
PROMISED_FOR_AFTER_REQUIRED_BY = StateLabel(
    "attention", f"{label('promised_for')} is after {label('required_by')}"
)
SOURCES_DISAGREE = StateLabel("attention", "sources disagree")


def next_action_overdue(
    action_due_date: date | None, *, today: date
) -> StateLabel | None:
    """Name a project Next Action whose own due date has already passed.

    The caller supplies the day; nothing here reads a clock, so a screen and
    its test agree about what "past" means.
    """
    if action_due_date is None or action_due_date >= today:
        return None
    return NEXT_ACTION_OVERDUE


def promised_after_required(
    committed_date: date | None, required_by: date | None
) -> StateLabel | None:
    """Name a promise that falls after the date the Constraint is needed by."""
    if committed_date is None or required_by is None:
        return None
    if committed_date <= required_by:
        return None
    return PROMISED_FOR_AFTER_REQUIRED_BY


def documentation_review_state(sufficient: bool) -> StateLabel:
    """Carry the legacy documentation marker as words plus a tone."""
    return StateLabel(
        "settled" if sufficient else "neutral", documentation_review_label(sufficient)
    )


# --- Where the keyboard lands -----------------------------------------------

FOCUS_IDS: Mapping[str, str] = {
    "refusal": "work-refusal",
    "errors": "work-errors",
    "outcome": "work-outcome",
    "item": "work-item",
}


def focus_target(
    *,
    refused: bool = False,
    errors: Iterable[FieldError] = (),
    saved: bool = False,
) -> str:
    """Name the one element that takes focus in this response.

    A refusal outranks a field error, which outranks a completed Save, which
    outranks the plain reading. Exactly one element in a response carries
    `autofocus`, so a keyboard user is never returned to the top of the page
    and a screen reader announces the outcome rather than the document title.
    """
    if refused:
        return FOCUS_IDS["refusal"]
    if tuple(errors):
        return FOCUS_IDS["errors"]
    if saved:
        return FOCUS_IDS["outcome"]
    return FOCUS_IDS["item"]


def register(env) -> None:
    """Give every template the primitives' vocabulary under stable names."""
    env.globals.update(
        state_label=StateLabel,
        sources_disagree=SOURCES_DISAGREE,
        next_action_overdue=next_action_overdue,
        promised_after_required=promised_after_required,
        documentation_review_state=documentation_review_state,
        focus_ids=FOCUS_IDS,
        focus_target=focus_target,
        not_recorded=NOT_RECORDED,
    )
