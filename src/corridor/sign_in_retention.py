"""Delete a sign-in record once it can no longer authorize anyone (#907, ADR-0102).

``web_sessions``, ``sign_in_tokens`` and ``sign_in_attempts`` all evaluate
expiry inline in the ``WHERE`` clause against an injectable clock, which is
right for authorization: an expired row is never *read* as valid. But nothing
deleted them. Expired sessions, spent links, and every recorded attempt to
reach the product accumulated for the life of the customer database, with no
stated period after which they go. ADR-0102 states that period; this module is
the pass that enforces it.

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

**What it does reuse, exactly.** The hold. ``retention.permit_deletion_unreferenced``
already answered the question this pass asks -- what does a project hold mean
for a deletion that cannot be attributed to a project -- and answered it
"any active hold anywhere refuses it", because the thing being deleted might
be the held project's. A sign-in record is in the same position: the person
whose session this was may be the person a litigation hold is about. So an
active ``RetentionHold`` anywhere stops this pass, and the pass reports that
rather than raising, the way ``retention_sweep`` reports a refusal.

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
holder keeps one for thirty minutes, so any session period measured in days is
clear of it. A period shorter than that holder's TTL would break #844.

**What is deliberately not here.** Two things, both recorded in ADR-0102 rather
than half-built. This pass has no recurring trigger: Due Work is the product's
one supervised runtime and every relation it uses -- schedules, occurrences and
receipts -- carries a ``NOT NULL`` foreign key to ``projects``, so a
customer-environment-wide pass cannot be declared to it without a schema
change. And the receipt below is one ``audit_log`` entry, not a row in a
receipt family of its own; ``audit_log`` is already the append-only ledger of
the whole customer database rather than of one project (``access``'s own
classification), and already carries sign-in, sign-out and offboarding, so it
takes this act without a new relation. Both are stated in the ADR with the
exact change each would need.
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


# The three stated periods, measured from the moment the row stopped being able
# to authorize anything -- not from when it was created. ADR-0102 gives the
# reason for each and for why they differ; changing one here without changing
# the ADR makes the stated retention and the enforced retention disagree.
SESSION_RETENTION = timedelta(days=30)
TOKEN_RETENTION = timedelta(days=7)
ATTEMPT_RETENTION = timedelta(days=365)

RESULT_SCHEMA_VERSION = "sign-in-record-expiry-v1"

# The three relations this pass may touch, in the order the receipt reports
# them. It is a closed list on purpose: this module can no more be asked to
# expire a Project Record table than `retention` can.
SWEPT_RELATIONS: tuple[str, ...] = (
    "web_sessions",
    "sign_in_tokens",
    "sign_in_attempts",
)


class SignInRetentionRefused(ValueError):
    """The pass was given a clock it cannot measure a retention period from."""


def sweep_sign_in_records(session: Session, *, as_of: datetime) -> dict[str, Any]:
    """Delete every sign-in record past its stated period, or decline and say why.

    Returns the pass's receipt whether or not it deleted anything. A pass that
    deleted nothing writes no ``audit_log`` entry, for the reason
    ``retention_sweep`` discards an empty dry run: a row per idle pass is a row
    saying nothing happened. A refusal writes none either -- it deleted nothing,
    and the hold that caused it is already an attributable recorded act.
    """

    moment = _aware_utc(as_of)
    receipt: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "observed_at": moment.isoformat(),
        "health": "healthy",
        "refusal": "",
        "retained_for_days": {
            "web_sessions": SESSION_RETENTION.days,
            "sign_in_tokens": TOKEN_RETENTION.days,
            "sign_in_attempts": ATTEMPT_RETENTION.days,
        },
        "deleted": {relation: 0 for relation in SWEPT_RELATIONS},
        "audit_id": None,
    }

    if _held(session):
        receipt["health"] = "retention_attention_required"
        receipt["refusal"] = "hold_active"
        return receipt

    session_cutoff = moment - SESSION_RETENTION
    token_cutoff = moment - TOKEN_RETENTION
    attempt_cutoff = moment - ATTEMPT_RETENTION

    deleted = receipt["deleted"]
    # A session stops being usable at whichever came first, its expiry or its
    # revocation: `resolve_web_session` requires both `revoked_at is null` and
    # a future `expires_at`. Offboarding revokes without checking expiry, so a
    # `revoked_at` later than `expires_at` exists and must not extend the row.
    deleted["web_sessions"] = _delete(
        session,
        WebSession,
        or_(
            WebSession.expires_at <= session_cutoff,
            WebSession.revoked_at <= session_cutoff,
        ),
    )
    # Same shape for a link: consumption requires `consumed_at is null` and a
    # future `expires_at`, so it dies at the earlier of the two.
    deleted["sign_in_tokens"] = _delete(
        session,
        SignInToken,
        or_(
            SignInToken.expires_at <= token_cutoff,
            SignInToken.consumed_at <= token_cutoff,
        ),
    )
    # An attempt has one time. It stops counting toward backoff when
    # `access.ATTEMPT_WINDOW` closes fifteen minutes later; everything after
    # that is the security record ADR-0102 keeps it for.
    deleted["sign_in_attempts"] = _delete(
        session,
        SignInAttempt,
        SignInAttempt.occurred_at <= attempt_cutoff,
    )

    if any(deleted.values()):
        entry = audit.record(
            session,
            actor=audit.SIGN_IN_RECORD_EXPIRY_ACTOR,
            action=audit.EXPIRE_SIGN_IN_RECORDS,
            entity_type=audit.PERSON_IDENTITY,
            entity_id=audit.UNBOUND_IDENTITY,
            after={
                "schema_version": RESULT_SCHEMA_VERSION,
                "observed_at": receipt["observed_at"],
                "retained_for_days": receipt["retained_for_days"],
                "deleted": dict(deleted),
            },
        )
        receipt["audit_id"] = entry.id
    return receipt


def _delete(session: Session, model, condition) -> int:
    result = session.execute(delete(model).where(condition))
    return int(result.rowcount or 0)


def _held(session: Session) -> bool:
    """Any active project hold refuses a deletion no project can be named for.

    The rule and its reason are ``retention.permit_deletion_unreferenced``'s:
    a row that cannot be attributed to a project might be the held project's.
    """

    return (
        session.scalar(
            select(RetentionHold.id)
            .where(RetentionHold.lifted_at.is_(None))
            .limit(1)
        )
        is not None
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SignInRetentionRefused(
            "the sign-in record expiry needs an aware datetime to measure from"
        )
    return value.astimezone(timezone.utc)
