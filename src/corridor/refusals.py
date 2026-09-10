"""One shape every refusal wears, so an adapter maps it once (#794 card 22).

Fourteen refusal families grew independently.  Most subclass ``ValueError``,
two subclassed ``Exception``, and one subclasses its own sibling.  Nothing
joined them, so the HTTP adapter re-derived the answer at every catch site:
``web/app.py`` held 99 ``try`` blocks and 176 ``raise HTTPException``, and 23
of those sites caught a bare ``ValueError``.  A bare catch is what made five
of them forward ``str(exc)`` to the browser under a hand-picked status, so an
incidental parse error from *inside* a domain call became a 409 whose body was
an internal message.

What was tried first was hand-picking the status at each site, which is what
the 176 raises are.  It does not survive: two sites for the same family
disagree by accident and nothing notices, and a family with no declared class
gets whatever the nearest ``except ValueError`` happens to say.

So a refusal declares its own kind, once, next to its own docstring.  A
refusal is not a kind of value error; it is a bounded, attributable "no" with
a sentence a person reads.  ``Refusal`` is the class that says so.  It is
mixed in ahead of the existing ``ValueError`` lineage so every caller that
already catches ``ValueError`` keeps working, and no family changes what it
raises, when, or what it says.

The kinds are only the distinctions an adapter acts on:

``STALE``
    What the screen showed is no longer current.  ADR-0039 is why this is its
    own kind rather than a conflict: a stale Save is refused *whole* and the
    newer state is presented, so a screen that can re-render itself needs to
    tell "reload and look again" apart from "that request was wrong".
``NOT_OFFERED``
    The control was never offered in this state, so there is nothing to redo.
``CONFLICT``
    The act contradicts what the record already holds.
``MALFORMED_INPUT``
    The request is not one this screen produces.
``NOT_AUTHORIZED``
    The caller holds no designation for this act.

A refusal that belongs to one control on one row carries which control and
which Proposed Delta hold it, so a screen can bind the sentence to the input
without deciding anything a second time.  ``packet_review.ReviewScreenRefused``
already carried both; the attributes are declared here so every family may.

The kind is deliberately *not* an HTTP status.  The kind-to-status table is
one dict in the adapter (``corridor.web.app.REFUSAL_STATUS``), because a
status is what one protocol says about a refusal rather than what the refusal
is.  A second adapter would write its own table against the same five kinds.
"""

STALE = "stale"
NOT_OFFERED = "not_offered"
CONFLICT = "conflict"
MALFORMED_INPUT = "malformed_input"
NOT_AUTHORIZED = "not_authorized"

REFUSAL_KINDS = frozenset(
    {STALE, NOT_OFFERED, CONFLICT, MALFORMED_INPUT, NOT_AUTHORIZED}
)


class Refusal(Exception):
    """A bounded refusal: nothing was written, and this is what to say.

    Mixed in ahead of an existing exception base rather than replacing it, so
    ``class SomeRefusal(Refusal, ValueError)`` keeps answering ``isinstance``
    for both.  Subclasses override ``refusal_kind``; ``CONFLICT`` is the
    default because a refusal an author never classified is at worst reported
    as one, never as a malformed request the caller is told to fix.
    """

    refusal_kind: str = CONFLICT

    # Where a refusal belongs to one control on one row, which ones.
    delta_id: int | None = None
    control: str | None = None

    @property
    def customer_sentence(self) -> str:
        """The words a person reads. Each family writes its own message."""

        return str(self)


class StaleOffer(Refusal):
    """What this screen offered is no longer current; nothing was written.

    Raised by an adapter that compared its own rendered offer against the live
    state before calling a domain command.  The comparison is the screen's, so
    the refusal is the screen's too — the domain command never saw the request.
    """

    refusal_kind = STALE


class NotOffered(Refusal):
    """This control was not offered in the state the record is actually in."""

    refusal_kind = NOT_OFFERED


class MalformedSave(Refusal):
    """This submission is not one the screen produces; nothing was written.

    One of the two classes an adapter stands in front of a domain command with
    while that command still refuses by raising a bare ``ValueError``.  The
    adapter answers with its own customer sentence rather than forwarding an
    internal message to a browser, because the set of messages a bare
    ``ValueError`` can carry is unbounded: an incidental parse error inside the
    command reads to a browser exactly like a refusal.  The command declaring
    its own family is the better fix, and retires the stand-in.
    """

    refusal_kind = MALFORMED_INPUT


class ConflictingSave(Refusal):
    """This act contradicts the record; nothing was written.

    The conflict-kind half of ``MalformedSave``, for the same reason and with
    the same retirement.
    """

    refusal_kind = CONFLICT
