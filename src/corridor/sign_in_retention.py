"""Delete a sign-in record once it can no longer authorize anyone (#907, ADR-0102).

``web_sessions``, ``sign_in_tokens`` and ``sign_in_attempts`` all evaluate
expiry inline in the ``WHERE`` clause against an injectable clock, which is
right for authorization: an expired row is never *read* as valid. But nothing
deleted them. Expired sessions, spent links, and every recorded attempt to
reach the product accumulated for the life of the customer database, with no
stated period after which they go. ADR-0102 states that period; this module is
the pass that enforces it.

**A period is when a row becomes eligible for deletion, not how long it lives.**
The two are separate terms. The period ends and the row becomes eligible; this
pass is what removes it, so whatever interval invokes the pass is an additional
lag on top of the period, and a hold, a failed run or a recovery delay
lengthens that lag further without changing the period. Under the daily
schedule ADR-0102 specifies, an eligible row goes within a further 24 hours.
Nothing here gives a row a maximum lifetime, and ``TOKEN_RETENTION`` in
particular must not be quoted as one.

**The three periods are product judgements, not figures a standard requires.**
Published practice says a period must exist and says to determine it by risk
assessment where no requirement governs (NIST SP 800-63B-4 §2.4.2); the audit
log retention figures that do exist -- CIS's ninety-day minimum, CNIL's usual
six-months-to-a-year discussion -- are about audit logs in general, and no
source read for #907 says when *these* rows may be deleted. Every requirement
found says *invalidate*, never *delete*. The research note records what each
source does and does not establish; a longer period is not automatically the
safer or the more compliant one.

**Why the Class B apparatus could not take these three.** ``retention`` is
per project: every candidate carries a ``project_id``, the hold is placed on a
project, the manifest lists a digest per row, and execution *nulls the content
columns* rather than deleting the row, because a Class B receipt keeps its
identity and its digests after its content is gone. None of that shape fits
here. A sign-in record is keyed to a person -- or, for an attempt, to a
normalized email or a client address that may belong to nobody -- so there is
no project to scope a sweep to and no completion-shaped column to anchor one
on. And there is no content to null: the *whole row* is the credential, so
what has to go is the row.

**What it does reuse, exactly.** The hold. ``retention.permit_unreferenced_deletion``
already answered the question this pass asks -- what does a project hold mean
for a deletion that cannot be attributed to a project -- and answered it
"any active hold anywhere refuses it", because the thing being deleted might
be the held project's. A sign-in record is in the same position: the person
whose session this was may be the person a litigation hold is about. So an
active ``RetentionHold`` anywhere stops this pass, and the pass reports that
rather than raising, the way ``retention_sweep`` reports a refusal.

**The hold is read inside each delete, not once before them all.** A read that
finds no hold, followed by a delete, leaves the window between the two: a hold
committed in it would be honoured by neither statement. So the predicate lives
in the ``DELETE`` itself, and at the READ COMMITTED isolation this application
runs under, each statement takes its own snapshot -- a hold committed at any
point before a given ``DELETE`` begins is seen by that ``DELETE``, which then
removes nothing. The separate read at the top of the pass is kept for what it
is good for, which is telling the receipt that a hold is why nothing happened.
What this does not do, because it cannot: a hold committed while a ``DELETE``
is already executing does not put back the rows that statement removed. Those
rows were past their stated period and no hold existed when they went.

**That predicate is not complete serialization, so the ordering is a protocol
rather than a race.** ``retention.take_hold_ordering_lock`` is one
environment-level boundary; ``place_hold`` takes it, and this pass takes it
before it reads hold state or deletes anything. Which side won is then a fact
the receipt can stand on: hold activation first and this pass refuses; this
pass first and it may finish before the hold is enforced, which the hold's own
acknowledgement is worded not to deny. The residual above is unchanged and is
still the guarantee for a hold that never passes the boundary -- an operator
inserting into ``retention_holds`` by hand -- and nothing anywhere recovers a
deleted row.

**Hold activation waits for a running batch, so batch length is part of the
contract.** Today this pass takes the boundary once and issues one unbounded
``DELETE`` per relation under it, so the batch a hold waits behind is the whole
pass over three small relations. That is short enough while they stay small,
which is the same fact ADR-0102's cadence rests on and the reason growth is
answered with a time index rather than a rarer schedule. If the pass ever stops
being short, the deletes must be bounded -- row-limited, each committing its
own transaction so the boundary is released between them -- before a hold could
reasonably be made to wait behind it.

**Nothing recorded is lost.** Who signed in and who signed out are
``audit_log`` entries written in the same transaction as the act, and
``identity_audit`` exports them; they are untouched here. What this pass
removes is the machinery underneath: a session hash that can no longer resolve,
a token hash that can no longer be consumed, and a throttle counter whose
window closed. The one asymmetry is ``sign_in_attempts``, which is the *only*
record of a caller who tried and did not succeed -- ``SIGN_IN`` is recorded at
session creation, so an attempt from an unknown email leaves no audit entry at
all. ADR-0102 keeps attempts longer for that reason and says why.

**The one live reader of a dead row**, which bounds how aggressive the session
period may be: ``access.expired_web_session`` deliberately reads a session that
expired and was not revoked, so #844 can hand back what a coordinator had typed.
That reader needs the row only while a held draft could still exist, and #844's
holder keeps one for thirty minutes, so the seven-day session period is 336
times the window it has to clear. A period shorter than that holder's TTL would
break #844.

**The receipt, and what makes an idle run observable.** A pass that deleted
something records one ``audit_log`` entry; a pass that deleted nothing records
none, because a domain audit event per idle sweep is a row saying nothing
happened. Every pass returns the same receipt either way -- policy version,
executing identity, the three cutoffs, the counts, the outcome and any refusal
-- and ``retention_cli`` prints it on every run, so the scheduled invocation's
own run record shows that the job ran and what it concluded even when it
removed nothing. None of it copies anything that was deleted: counts and
timestamps only, no address and no hash.

**What is deliberately not here.** The recurring trigger. This pass runs when
something invokes ``retention_cli``, and until a deployed schedule invokes it
once per customer environment, expiry depends on somebody remembering -- the
defect #488 named. Extending Due Work's per-project scope model to carry it was
considered and declined: Due Work is per project by schema and by invariant, and
this is one customer-environment-wide maintenance operation, so the answer is an
environment-scoped scheduled invocation of the command rather than nullable
project columns inside Due Work. ADR-0102 records exactly what that deployment
task needs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import (
    RetentionHold,
    SignInAttempt,
    SignInToken,
    WebSession,
)
from corridor.retention import take_hold_ordering_lock


# The three stated periods, measured from the moment the row stopped being able
# to authorize anything -- not from when it was created. Each is when a row
# becomes *eligible* for deletion and not a maximum lifetime for it: the row
# goes at the next pass, so the invocation interval is a further lag on top of
# the number below. ADR-0102 states the two terms separately and gives the
# reason for each period and for why they differ; changing one here without
# changing the ADR makes the stated retention and the enforced retention
# disagree.
SESSION_RETENTION = timedelta(days=7)
TOKEN_RETENTION = timedelta(hours=24)
ATTEMPT_RETENTION = timedelta(days=180)

# Which stated policy a receipt was produced under. The periods above *are* the
# policy, so this changes whenever one of them does: a receipt read a year later
# has to say which set of numbers produced its counts, and the cutoffs it
# records are only interpretable against them.
POLICY_VERSION = "adr-0102-2026-09-11"

RESULT_SCHEMA_VERSION = "sign-in-record-expiry-v1"

# The three relations this pass may touch, in the order the receipt reports
# them. It is a closed list on purpose: this module can no more be asked to
# expire a Project Record table than `retention` can.
SWEPT_RELATIONS: tuple[str, ...] = (
    "web_sessions",
    "sign_in_tokens",
    "sign_in_attempts",
)

# Any unlifted hold, on any project. Held as an EXISTS rather than run as a
# read of its own so it can be evaluated inside each delete; see the module
# docstring for why the window between a separate read and a delete matters.
_ACTIVE_HOLD = (
    select(RetentionHold.id).where(RetentionHold.lifted_at.is_(None)).exists()
)


class SignInRetentionRefused(ValueError):
    """The pass was given a clock it cannot measure a retention period from."""


def sweep_sign_in_records(session: Session, *, as_of: datetime) -> dict[str, Any]:
    """Delete every sign-in record past its stated period, or decline and say why.

    Returns the pass's receipt whether it deleted, declined, or found nothing
    due, so the caller that scheduled it can record that it ran either way.
    Only a pass that actually deleted something writes an ``audit_log`` entry.

    The clock is validated before the boundary is taken, so a caller that
    passes an unusable ``as_of`` is refused without making a hold wait.
    """

    moment = _aware_utc(as_of)
    cutoff = {
        "web_sessions": moment - SESSION_RETENTION,
        "sign_in_tokens": moment - TOKEN_RETENTION,
        "sign_in_attempts": moment - ATTEMPT_RETENTION,
    }
    receipt: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "executed_by": audit.SIGN_IN_RECORD_EXPIRY_ACTOR,
        "observed_at": moment.isoformat(),
        "outcome": "nothing_due",
        "health": "healthy",
        "refusal": "",
        "cutoff": {
            relation: cutoff[relation].isoformat() for relation in SWEPT_RELATIONS
        },
        "deleted": {relation: 0 for relation in SWEPT_RELATIONS},
        "audit_id": None,
    }

    # The boundary first, and the hold state only after it: a hold activation
    # that reached it first is committed by the time the boundary is granted
    # here, and one that did not now waits for this batch instead of
    # overlapping it.
    take_hold_ordering_lock(session)
    if _held(session):
        receipt["outcome"] = "refused"
        receipt["health"] = "retention_attention_required"
        receipt["refusal"] = "hold_active"
        return receipt

    deleted = receipt["deleted"]
    # A session stops being usable at whichever came first, its expiry or its
    # revocation: `resolve_web_session` requires both `revoked_at is null` and
    # a future `expires_at`. Offboarding revokes without checking expiry, so a
    # `revoked_at` later than `expires_at` exists and must not extend the row.
    deleted["web_sessions"] = _delete(
        session,
        WebSession,
        or_(
            WebSession.expires_at <= cutoff["web_sessions"],
            WebSession.revoked_at <= cutoff["web_sessions"],
        ),
    )
    # Same shape for a link: consumption requires `consumed_at is null` and a
    # future `expires_at`, so it dies at the earlier of the two.
    deleted["sign_in_tokens"] = _delete(
        session,
        SignInToken,
        or_(
            SignInToken.expires_at <= cutoff["sign_in_tokens"],
            SignInToken.consumed_at <= cutoff["sign_in_tokens"],
        ),
    )
    # An attempt has one time. It stops counting toward backoff when
    # `access.ATTEMPT_WINDOW` closes fifteen minutes later; everything after
    # that is the security record ADR-0102 keeps it for.
    deleted["sign_in_attempts"] = _delete(
        session,
        SignInAttempt,
        SignInAttempt.occurred_at <= cutoff["sign_in_attempts"],
    )

    if any(deleted.values()):
        receipt["outcome"] = "deleted"
        entry = audit.record(
            session,
            actor=audit.SIGN_IN_RECORD_EXPIRY_ACTOR,
            action=audit.EXPIRE_SIGN_IN_RECORDS,
            entity_type=audit.PERSON_IDENTITY,
            entity_id=audit.UNBOUND_IDENTITY,
            after={
                "schema_version": RESULT_SCHEMA_VERSION,
                "policy_version": POLICY_VERSION,
                "observed_at": receipt["observed_at"],
                "cutoff": receipt["cutoff"],
                "deleted": dict(deleted),
            },
        )
        receipt["audit_id"] = entry.id
    return receipt


def _delete(session: Session, model, condition) -> int:
    """Remove what is past its period, under the hold predicate, in one statement.

    The hold is read *inside* this statement. A read of the holds table
    followed by a delete would leave the window between them, in which a hold
    another transaction commits is honoured by neither; evaluated here, at READ
    COMMITTED, the statement's own snapshot includes any hold committed before
    it began and the delete then matches no row at all. That is the guarantee
    for a hold that never took the ordering boundary; one that did could not
    have committed while this was running at all.

    One statement, with no row limit: the whole relation is the batch. See the
    module docstring for why that is acceptable at this size and what would
    have to change if it stopped being.
    """

    result = session.execute(
        delete(model).where(condition).where(~_ACTIVE_HOLD)
    )
    return int(result.rowcount or 0)


def _held(session: Session) -> bool:
    """Any active project hold refuses a deletion no project can be named for.

    The rule and its reason are ``retention.permit_unreferenced_deletion``'s:
    a row that cannot be attributed to a project might be the held project's.
    This read is what lets the receipt say *why* nothing was deleted; the
    deletes carry the same predicate themselves.
    """

    return session.scalar(select(_ACTIVE_HOLD)) is True


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SignInRetentionRefused(
            "the sign-in record expiry needs an aware datetime to measure from"
        )
    return value.astimezone(timezone.utc)
