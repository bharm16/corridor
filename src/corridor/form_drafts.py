"""What a coordinator had typed when their session expired, held until they return (#844).

A session can expire while a Review item is open.  The write path refuses that
submission before any handler runs, which is right -- an expired session is not
an authorization -- but until now the refusal also destroyed the work.  The
selection, the return date and the typed coordination answers were in a request
body nobody kept, so the person signed in again and retyped them.

What is held here is deliberately **not a record**.  A draft is permitted user
input and nothing else: never an accepted value, never a decision, never a
Proposed Delta.  It is scoped to one person, one customer, one project and one
form occurrence; it expires on a short bound; and it is taken *once*, by the
screen that re-renders the form, so nothing here is ever replayed as a write.
No domain reader knows this module exists, and nothing in it can make anything
effective.

What was considered and rejected:

- **Carrying the input in the sign-in redirect.**  A redirect target reaches
  the browser's history, the referrer of the next request and every log in
  between.  A coordination question naming a person does not belong there, so
  the redirect carries a same-app path and nothing else.
- **Replaying the refused submission after sign-in.**  A save composed against
  a reading taken before the session expired attests to a record that may have
  moved, and `packet_review` already refuses such a save.  Replaying it would
  be asking for that refusal on the person's behalf.  The draft is restored
  *into the form*, beside what moved under it, and the person submits again.
- **Keeping input for every form.**  Most write forms on the pilot pages carry
  no typed input at all: the two Issue forms send a candidate id, a profile
  version, an accepted revision and a coverage digest, all of them machine-
  derived state the page emitted and the route re-derives.  Restoring those
  would restore a stale attestation, which is exactly what #675 refuses.  So
  `KEPT_FORMS` is an allowlist of the forms a person actually fills, and every
  other form keeps its bare refusal.

**Where a draft lives, and the limit of that.**  Nothing already in the customer
database could hold this.  The object store is addressed by content hash alone,
with no person, project or tenant in the key and no expiry of its own -- an
object leaves only when a Class B row does, and anything else written there is
reported as an orphan by the storage reconciliation.  The one reusable expiry
shape is the Class B retention mixin, which is per project rather than per
person and brings the whole retention apparatus with it.  And there is no
generic key/value or scratch relation: this product has repeatedly refused to
call a relation a cache.

So the store below is held by the web process.  It needs no relation, which is
also what makes "no domain reader consumes a draft" true by construction, and
the customer boundary is carried explicitly in the scope rather than inherited
from the database a request happens to open.  The cost, stated rather than
hidden: a process restart loses every held draft, and a deployment serving one
customer from more than one process restores a draft only where the sign-in
lands on the process that kept it.  Losing a draft costs a retype and never a
wrong answer, which is why a best-effort holder is a fair trade today; a
durable one is a table of its own, with a person, a customer, a project, an
occurrence, the input and an expiry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# Long enough to notice the refusal, reach a mail client and follow a link that
# itself lives fifteen minutes; far shorter than the twelve-hour session,
# because a draft is unreviewed input rather than anything anyone relies on.
DRAFT_TTL = timedelta(minutes=30)

# What one process holds at once.  Nothing in this product sweeps per-person
# state on a schedule, so the bound is enforced where drafts are kept rather
# than by a job that may never run: keeping one discards what has expired, and
# then the oldest of whatever is still over the bound.
KEPT_AT_ONCE = 256


@dataclass(frozen=True, slots=True)
class KeptForm:
    """One form whose permitted input is worth holding across a sign-in.

    ``page`` is the route template of the GET that renders the form again, and
    ``occurrence`` names the hidden field saying which occurrence of it was
    open, so a draft typed into one Work List item is never offered on another.
    ``permitted`` is the allowlist: a field not named here is not restored, and
    the request-forgery token is not in any of them, because a restored form is
    issued a fresh one.
    """

    page: str
    occurrence: str
    occurrence_parameter: str
    permitted: frozenset[str]


REVIEW_ITEM_SAVE = "/review/{slug}"
REVIEW_ITEM_ANSWERS = "/review/{slug}/answers"

# The two Review Save forms, and no others.  `outcome` is absent from the batch
# form's allowlist on purpose: it is the submit button the coordinator pressed,
# and the whole point of this is that the decision is taken again deliberately
# rather than carried across a sign-in.  #846's `judgment` and
# `judgment_minutes` are permitted input and are restored; its three hidden
# occurrence fields are not, so the measurement that is finally recorded is
# bound to the packet the re-rendered page displays rather than to the one that
# was displayed before the session expired.
KEPT_FORMS: Mapping[str, KeptForm] = {
    REVIEW_ITEM_SAVE: KeptForm(
        page="/review/{slug}",
        occurrence="item_key",
        occurrence_parameter="item",
        permitted=frozenset(
            {"child", "defer_until", "judgment", "judgment_minutes"}
        ),
    ),
    REVIEW_ITEM_ANSWERS: KeptForm(
        page="/review/{slug}",
        occurrence="item_key",
        occurrence_parameter="item",
        permitted=frozenset(
            {
                "answer_delta",
                "answer_outcome",
                "answer_source",
                "answer_question",
                "answer_person",
                "answer_organization",
                "answer_return",
                "judgment",
                "judgment_minutes",
            }
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class DraftScope:
    """The four things a draft belongs to; all four must match to restore it.

    ``customer`` is here rather than implied because this store is not a
    customer database.  Two customers served by one process share the slug
    space, so without it a project slug alone could reach across the boundary
    every other read in the product is partitioned by.
    """

    customer: str
    principal_subject: str
    project_slug: str
    occurrence: str


@dataclass(frozen=True, slots=True)
class KeptInput:
    """One form's permitted input, as the browser sent it, with its scope."""

    scope: DraftScope
    form: str
    fields: tuple[tuple[str, str], ...]
    kept_at: datetime

    def values(self, name: str) -> tuple[str, ...]:
        """Every value sent under this name, in the order the form sent them."""

        return tuple(value for sent, value in self.fields if sent == name)

    def value(self, name: str, default: str = "") -> str:
        """The single value sent under this name, or ``default``."""

        found = self.values(name)
        return found[0] if found else default

    def expired(self, now: datetime) -> bool:
        return now - self.kept_at >= DRAFT_TTL


def permitted_input(
    form: str, submitted: Iterable[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Only the fields of this form a person typed or chose, in order.

    Everything else the browser sent is dropped here and never held: the
    forgery token, the occurrence the page displayed, and any field a later
    change adds to the form without adding it to the allowlist.  Repeated names
    keep their order, because the focused answers arrive as parallel lists and
    are meaningless out of it.
    """

    rule = KEPT_FORMS.get(form)
    if rule is None:
        return ()
    return tuple(
        (name, value)
        for name, value in submitted
        if name in rule.permitted and isinstance(value, str)
    )


class KeptDrafts:
    """Every draft this process holds, by scope, with its own declared clock.

    ``take`` is single use.  A draft answers the one page load that follows the
    sign-in and is then gone, so a stale draft cannot reappear days later on a
    reading that has moved several times since.
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._held: dict[DraftScope, KeptInput] = {}

    def keep(self, scope: DraftScope, form: str, fields: tuple[tuple[str, str], ...]) -> None:
        """Hold this input, discarding what has expired and the oldest over the bound."""

        if not fields:
            return
        moment = self._now()
        self._held = {
            held: draft
            for held, draft in self._held.items()
            if not draft.expired(moment)
        }
        self._held[scope] = KeptInput(
            scope=scope, form=form, fields=fields, kept_at=moment
        )
        while len(self._held) > KEPT_AT_ONCE:
            oldest = min(self._held, key=lambda held: self._held[held].kept_at)
            del self._held[oldest]

    def take(self, scope: DraftScope) -> KeptInput | None:
        """The live draft for exactly this scope, removed as it is handed over."""

        draft = self._held.pop(scope, None)
        if draft is None or draft.expired(self._now()):
            return None
        return draft

    def holding(self) -> int:
        """How many drafts this process is holding, for a health or test read."""

        return len(self._held)
