"""The stated retention for sessions, sign-in links and attempts (#907, ADR-0102).

These prove the thing #907 said nothing did: that a sign-in record past its
stated period is actually *removed*, not merely refused by an inline expiry
check. They also pin the three boundaries the sweep must not cross -- a record
that can still authorize somebody, a project hold, and the expired session
#844's draft hand-back still reads.

Counts are asserted as "at least what this test created" rather than exactly,
because the three relations are customer-wide: another module's committed
scenario may have left one behind, and the sweep is deliberately unable to
scope itself to a project. What each test proves exactly is which of *its own*
rows survived.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor import access, audit
from corridor.models import AuditLog, SignInAttempt, SignInToken, WebSession
from corridor.principals import HumanPrincipal
from corridor.retention import place_hold
from corridor.sign_in_retention import (
    ATTEMPT_RETENTION,
    RESULT_SCHEMA_VERSION,
    SESSION_RETENTION,
    SignInRetentionRefused,
    TOKEN_RETENTION,
    sweep_sign_in_records,
)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
OPERATOR = HumanPrincipal("local:retention-operator")


def _hash() -> str:
    return uuid4().hex + uuid4().hex


def _session_row(db, *, expires_at, revoked_at=None) -> WebSession:
    row = WebSession(
        session_sha256=_hash(),
        csrf_sha256=_hash(),
        principal_subject="local:person",
        email_normalized=f"{uuid4().hex[:8]}@example.gov",
        created_at=expires_at - access.SESSION_TTL,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(row)
    db.flush([row])
    return row


def _token_row(db, *, expires_at, consumed_at=None) -> SignInToken:
    row = SignInToken(
        email_normalized=f"{uuid4().hex[:8]}@example.gov",
        token_sha256=_hash(),
        created_at=expires_at - access.SIGN_IN_TOKEN_TTL,
        expires_at=expires_at,
        consumed_at=consumed_at,
    )
    db.add(row)
    db.flush([row])
    return row


def _attempt_row(db, *, occurred_at) -> SignInAttempt:
    row = SignInAttempt(
        scope_kind=access.ISSUE_EMAIL,
        scope_value=f"{uuid4().hex[:8]}@example.gov",
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush([row])
    return row


def _present(db, model, row_id) -> bool:
    return db.scalar(select(model.id).where(model.id == row_id)) is not None


def test_a_record_past_its_stated_period_is_actually_removed(session):
    """The whole point of #907: expiry was evaluated, nothing was deleted."""

    dead_session = _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    dead_token = _token_row(
        session,
        expires_at=NOW - TOKEN_RETENTION - timedelta(days=1),
        consumed_at=NOW - TOKEN_RETENTION - timedelta(days=1, minutes=5),
    )
    old_attempt = _attempt_row(session, occurred_at=NOW - ATTEMPT_RETENTION - timedelta(days=1))

    receipt = sweep_sign_in_records(session, as_of=NOW)

    assert not _present(session, WebSession, dead_session.id)
    assert not _present(session, SignInToken, dead_token.id)
    assert not _present(session, SignInAttempt, old_attempt.id)
    assert receipt["health"] == "healthy"
    assert receipt["refusal"] == ""
    assert receipt["deleted"]["web_sessions"] >= 1
    assert receipt["deleted"]["sign_in_tokens"] >= 1
    assert receipt["deleted"]["sign_in_attempts"] >= 1


def test_a_record_inside_its_stated_period_survives(session):
    """A live session, an unspent link and a recent attempt are not touched."""

    live_session = _session_row(session, expires_at=NOW + access.SESSION_TTL)
    recently_dead = _session_row(session, expires_at=NOW - timedelta(days=1))
    live_token = _token_row(session, expires_at=NOW + access.SIGN_IN_TOKEN_TTL)
    recent_attempt = _attempt_row(session, occurred_at=NOW - timedelta(days=1))

    sweep_sign_in_records(session, as_of=NOW)

    assert _present(session, WebSession, live_session.id)
    assert _present(session, WebSession, recently_dead.id)
    assert _present(session, SignInToken, live_token.id)
    assert _present(session, SignInAttempt, recent_attempt.id)


def test_the_period_runs_from_revocation_not_from_a_later_expiry(session):
    """Offboarding revokes without checking expiry, so both columns are read.

    `deprovision_principal` revokes every session whose `revoked_at` is null,
    including one whose `expires_at` is still far away. That row stopped being
    usable when it was revoked, and its retention runs from then.
    """

    revoked_long_ago = _session_row(
        session,
        expires_at=NOW + timedelta(days=365),
        revoked_at=NOW - SESSION_RETENTION - timedelta(days=1),
    )
    revoked_yesterday = _session_row(
        session,
        expires_at=NOW + timedelta(days=365),
        revoked_at=NOW - timedelta(days=1),
    )

    sweep_sign_in_records(session, as_of=NOW)

    assert not _present(session, WebSession, revoked_long_ago.id)
    assert _present(session, WebSession, revoked_yesterday.id)


def test_the_expired_session_844_reads_outlives_a_sweep(session):
    """A session that expired minutes ago is still readable after a pass.

    `access.expired_web_session` is the one live reader of a dead row, and
    `form_drafts.DRAFT_TTL` bounds how long it needs one. A session retention
    measured in days is clear of it; this test fails if that ever stops being
    true.
    """

    raw = uuid4().hex
    row = WebSession(
        session_sha256=sha256(raw.encode()).hexdigest(),
        csrf_sha256=_hash(),
        principal_subject="local:person",
        email_normalized=f"{uuid4().hex[:8]}@example.gov",
        created_at=NOW - access.SESSION_TTL - timedelta(minutes=10),
        expires_at=NOW - timedelta(minutes=10),
    )
    session.add(row)
    session.flush([row])

    sweep_sign_in_records(session, as_of=NOW)

    assert access.expired_web_session(session, raw, now=NOW) is not None


def test_an_active_hold_refuses_the_pass_and_deletes_nothing(session, project):
    """A row no project can be named for might be the held project's."""

    dead_session = _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    old_attempt = _attempt_row(session, occurred_at=NOW - ATTEMPT_RETENTION - timedelta(days=1))
    place_hold(
        session, project_id=project.id, reason="open records request", principal=OPERATOR
    )

    receipt = sweep_sign_in_records(session, as_of=NOW)

    assert receipt["health"] == "retention_attention_required"
    assert receipt["refusal"] == "hold_active"
    assert receipt["deleted"] == {
        "web_sessions": 0,
        "sign_in_tokens": 0,
        "sign_in_attempts": 0,
    }
    assert _present(session, WebSession, dead_session.id)
    assert _present(session, SignInAttempt, old_attempt.id)


def test_a_pass_that_deleted_something_writes_one_receipt(session):
    """The receipt says what went, and names no person, because none is named."""

    _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    before = session.scalar(
        select(AuditLog.id)
        .where(AuditLog.action == audit.EXPIRE_SIGN_IN_RECORDS)
        .order_by(AuditLog.id.desc())
        .limit(1)
    )

    receipt = sweep_sign_in_records(session, as_of=NOW)

    entry = session.get(AuditLog, receipt["audit_id"])
    assert entry is not None
    assert (before is None) or entry.id > before
    assert entry.actor == audit.SIGN_IN_RECORD_EXPIRY_ACTOR
    assert entry.human_principal is None
    assert entry.entity_type == audit.PERSON_IDENTITY
    assert entry.entity_id == audit.UNBOUND_IDENTITY
    assert entry.after_json["schema_version"] == RESULT_SCHEMA_VERSION
    assert entry.after_json["deleted"] == receipt["deleted"]
    assert entry.after_json["retained_for_days"] == {
        "web_sessions": SESSION_RETENTION.days,
        "sign_in_tokens": TOKEN_RETENTION.days,
        "sign_in_attempts": ATTEMPT_RETENTION.days,
    }


def test_a_pass_with_nothing_due_writes_no_receipt(session):
    """An idle pass per week would be a weekly row saying nothing happened."""

    _session_row(session, expires_at=NOW + access.SESSION_TTL)

    receipt = sweep_sign_in_records(session, as_of=datetime(2001, 1, 1, tzinfo=timezone.utc))

    assert receipt["audit_id"] is None
    assert receipt["deleted"] == {
        "web_sessions": 0,
        "sign_in_tokens": 0,
        "sign_in_attempts": 0,
    }


def test_a_naive_clock_is_refused_rather_than_assumed_to_be_utc(session):
    with pytest.raises(SignInRetentionRefused):
        sweep_sign_in_records(session, as_of=datetime(2026, 9, 11, 12, 0))
