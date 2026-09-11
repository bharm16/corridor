"""The stated retention for sessions, sign-in links and attempts (#907, ADR-0102).

These prove the thing #907 said nothing did: that a sign-in record past its
stated period is actually *removed*, not merely refused by an inline expiry
check. They also pin the four boundaries the decision turns on -- an active
credential still authorizes after a pass, the draft-recovery window #844 reads
survives one, a credential the pass removed stays invalid rather than becoming
ambiguous, and a hold another transaction commits while the pass is running is
honoured by the delete itself.

Counts are asserted as "at least what this test created" rather than exactly,
because the three relations are customer-wide: another module's committed
scenario may have left one behind, and the sweep is deliberately unable to
scope itself to a project. What each test proves exactly is which of *its own*
rows survived.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor import access, audit, sign_in_retention
from corridor.config import settings
from corridor.form_drafts import DRAFT_TTL
from corridor.models import (
    AuditLog,
    Project,
    RetentionHold,
    SignInAttempt,
    SignInToken,
    WebSession,
)
from corridor.principals import HumanPrincipal
from corridor.retention import place_hold
from corridor.retention_cli import main as retention_main
from corridor.sign_in_retention import (
    ATTEMPT_RETENTION,
    POLICY_VERSION,
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


def _email() -> str:
    return f"{uuid4().hex[:8]}@example.gov"


def _session_row(db, *, expires_at, revoked_at=None) -> WebSession:
    row = WebSession(
        session_sha256=_hash(),
        csrf_sha256=_hash(),
        principal_subject="local:person",
        email_normalized=_email(),
        created_at=expires_at - access.SESSION_TTL,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(row)
    db.flush([row])
    return row


def _token_row(db, *, expires_at, consumed_at=None) -> SignInToken:
    row = SignInToken(
        email_normalized=_email(),
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
        scope_value=_email(),
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush([row])
    return row


def _enrolled(db, project_id: int) -> tuple[str, HumanPrincipal]:
    """One member, so a consumed link resolves to a real identity."""

    email = _email()
    principal = HumanPrincipal(f"local:{uuid4().hex[:8]}")
    access.enroll_member(
        db,
        project_id=project_id,
        email=email,
        principal=principal,
        display_name="Coordinator",
        designations=[access.COORDINATION],
        operator=OPERATOR,
    )
    return email, principal


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
    assert receipt["outcome"] == "deleted"
    assert receipt["health"] == "healthy"
    assert receipt["refusal"] == ""
    assert receipt["deleted"]["web_sessions"] >= 1
    assert receipt["deleted"]["sign_in_tokens"] >= 1
    assert receipt["deleted"]["sign_in_attempts"] >= 1


def test_a_record_inside_its_stated_period_survives(session):
    """A record that died recently is inside its period and is not touched."""

    recently_dead = _session_row(session, expires_at=NOW - timedelta(days=1))
    recently_spent = _token_row(
        session,
        expires_at=NOW - timedelta(hours=2),
        consumed_at=NOW - timedelta(hours=2, minutes=5),
    )
    recent_attempt = _attempt_row(session, occurred_at=NOW - timedelta(days=1))

    sweep_sign_in_records(session, as_of=NOW)

    assert _present(session, WebSession, recently_dead.id)
    assert _present(session, SignInToken, recently_spent.id)
    assert _present(session, SignInAttempt, recent_attempt.id)


def test_an_active_credential_still_authorizes_after_a_pass(session, project):
    """The first thing a retention pass must never do is end somebody's access.

    Presence in the table is not the claim; the claim is that the credential
    still *works*, so both are exercised through `access` rather than by
    selecting the row back.
    """

    # The wall clock, not this module's fixed NOW: both `access` writers take
    # `created_at` from the database default, and `ck_sign_in_token_expiry`
    # rejects a row whose injected expiry precedes it.
    now = datetime.now(timezone.utc)
    email, principal = _enrolled(session, project.id)
    opened = access.create_web_session(
        session, principal=principal, email_normalized=email, now=now
    )
    issued = access.issue_sign_in_token(session, email, redirect_path=None, now=now)
    fresh_attempt = _attempt_row(session, occurred_at=now - timedelta(minutes=1))

    receipt = sweep_sign_in_records(session, as_of=now)

    assert access.resolve_web_session(session, opened.raw_session_id, now=now) is not None
    assert access.has_live_token(session, email, now=now)
    consumed = access.consume_sign_in_token(session, issued.raw_token, now=now)
    assert consumed is not None and consumed.email_normalized == email
    assert _present(session, SignInAttempt, fresh_attempt.id)
    assert receipt["health"] == "healthy"


def test_the_draft_recovery_window_survives_a_pass(session):
    """#844 hands a coordinator back what they typed, and reads a dead row to do it.

    `access.expired_web_session` is the one live reader of an expired session,
    and `form_drafts.DRAFT_TTL` bounds how long it needs one. The session
    period has to clear that window by construction, so both the arithmetic and
    the behaviour at the far edge of the window are pinned here: this fails if
    a later change shortens the period toward the draft TTL.
    """

    assert SESSION_RETENTION > DRAFT_TTL
    assert SESSION_RETENTION == timedelta(days=7)
    assert SESSION_RETENTION / DRAFT_TTL == 336

    raw = uuid4().hex
    row = WebSession(
        session_sha256=sha256(raw.encode()).hexdigest(),
        csrf_sha256=_hash(),
        principal_subject="local:person",
        email_normalized=_email(),
        created_at=NOW - access.SESSION_TTL - DRAFT_TTL,
        expires_at=NOW - DRAFT_TTL,
    )
    session.add(row)
    session.flush([row])

    sweep_sign_in_records(session, as_of=NOW)

    assert access.expired_web_session(session, raw, now=NOW) is not None


def test_a_credential_the_pass_removed_stays_invalid(session):
    """A purged credential is refused, not resurrected and not an error.

    Every read of these relations matches on a hash, so a row that is gone and
    a hash that never existed give the same answer. The session is asserted
    before as well as after, because `expired_web_session` is the one reader
    that did return it: that is the difference the pass makes, and the answer
    it leaves behind has to be the unknown-credential answer.
    """

    raw_session_id = uuid4().hex
    dead = WebSession(
        session_sha256=sha256(raw_session_id.encode()).hexdigest(),
        csrf_sha256=_hash(),
        principal_subject="local:gone",
        email_normalized=_email(),
        created_at=NOW - SESSION_RETENTION - timedelta(days=3),
        expires_at=NOW - SESSION_RETENTION - timedelta(days=2),
    )
    raw_token = uuid4().hex
    unspent = SignInToken(
        email_normalized=_email(),
        token_sha256=sha256(raw_token.encode()).hexdigest(),
        created_at=NOW - TOKEN_RETENTION - timedelta(days=2, minutes=15),
        expires_at=NOW - TOKEN_RETENTION - timedelta(days=2),
    )
    session.add_all([dead, unspent])
    session.flush([dead, unspent])
    assert access.expired_web_session(session, raw_session_id, now=NOW) is not None

    sweep_sign_in_records(session, as_of=NOW)

    assert not _present(session, WebSession, dead.id)
    assert access.resolve_web_session(session, raw_session_id, now=NOW) is None
    assert access.expired_web_session(session, raw_session_id, now=NOW) is None
    assert access.consume_sign_in_token(session, raw_token, now=NOW) is None
    assert access.resolve_web_session(session, uuid4().hex, now=NOW) is None


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


def test_an_active_hold_refuses_the_pass_and_deletes_nothing(session, project):
    """A row no project can be named for might be the held project's."""

    dead_session = _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    old_attempt = _attempt_row(session, occurred_at=NOW - ATTEMPT_RETENTION - timedelta(days=1))
    place_hold(
        session, project_id=project.id, reason="open records request", principal=OPERATOR
    )

    receipt = sweep_sign_in_records(session, as_of=NOW)

    assert receipt["outcome"] == "refused"
    assert receipt["health"] == "retention_attention_required"
    assert receipt["refusal"] == "hold_active"
    assert receipt["deleted"] == {
        "web_sessions": 0,
        "sign_in_tokens": 0,
        "sign_in_attempts": 0,
    }
    assert _present(session, WebSession, dead_session.id)
    assert _present(session, SignInAttempt, old_attempt.id)


def test_the_delete_itself_refuses_while_a_hold_is_active(session, project, monkeypatch):
    """The guard is in the statement, so the check-then-delete window is closed.

    A hold committed after the pass read the holds table is the case that
    matters, and the pass's own reading is what this stands in for: told there
    is no hold, the deletes still remove nothing, because each one carries the
    hold predicate itself. Delete that predicate and this test fails while
    every other test in the module still passes.
    """

    dead_session = _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    old_attempt = _attempt_row(session, occurred_at=NOW - ATTEMPT_RETENTION - timedelta(days=1))
    place_hold(
        session, project_id=project.id, reason="open records request", principal=OPERATOR
    )
    monkeypatch.setattr(sign_in_retention, "_held", lambda _session: False)

    receipt = sweep_sign_in_records(session, as_of=NOW)

    assert receipt["outcome"] == "nothing_due"
    assert receipt["deleted"] == {
        "web_sessions": 0,
        "sign_in_tokens": 0,
        "sign_in_attempts": 0,
    }
    assert _present(session, WebSession, dead_session.id)
    assert _present(session, SignInAttempt, old_attempt.id)


def test_a_hold_committed_by_another_transaction_mid_pass_is_honoured(runtime_database):
    """The pass does not act on a reading of the holds table taken before it.

    Two committed transactions, because that is the only way a hold can appear
    while a pass is open. The sweeping transaction has already read the holds
    table as empty -- exactly the stale answer a check-then-delete would act on
    -- before the other transaction commits its hold.
    """

    factory = runtime_database.session_factory
    with factory() as setup:
        project = Project(
            slug=f"hold-race-{uuid4().hex[:8]}", name="Hold race", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        dead = _session_row(setup, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
        setup.commit()
        project_id, dead_id = project.id, dead.id

    with factory() as sweeping:
        assert (
            sweeping.scalar(
                select(RetentionHold.id).where(RetentionHold.lifted_at.is_(None))
            )
            is None
        )
        with factory() as holding:
            place_hold(
                holding,
                project_id=project_id,
                reason="open records request",
                principal=OPERATOR,
            )
            holding.commit()

        receipt = sweep_sign_in_records(sweeping, as_of=NOW)
        sweeping.commit()

    assert receipt["refusal"] == "hold_active"
    assert receipt["deleted"]["web_sessions"] == 0
    with factory() as verify:
        assert verify.get(WebSession, dead_id) is not None


def test_a_pass_that_deleted_something_writes_one_receipt(session):
    """The receipt says what went, names no person, and copies nothing deleted."""

    removed = _session_row(session, expires_at=NOW - SESSION_RETENTION - timedelta(days=1))
    removed_email, removed_hash = removed.email_normalized, removed.session_sha256
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
    assert entry.after_json["policy_version"] == POLICY_VERSION
    assert entry.after_json["deleted"] == receipt["deleted"]
    assert entry.after_json["cutoff"] == {
        "web_sessions": (NOW - SESSION_RETENTION).isoformat(),
        "sign_in_tokens": (NOW - TOKEN_RETENTION).isoformat(),
        "sign_in_attempts": (NOW - ATTEMPT_RETENTION).isoformat(),
    }
    written = repr(entry.after_json)
    assert removed_email not in written
    assert removed_hash not in written


def test_an_idle_pass_writes_no_audit_event_but_still_reports_a_run(session):
    """A domain audit event per idle sweep would be a daily row saying nothing.

    What has to survive instead is evidence that the job ran, so the receipt an
    idle pass returns is complete on its own: which policy produced it, who
    executed it, what it measured against, and that nothing was due.
    """

    _session_row(session, expires_at=NOW - timedelta(days=1))

    receipt = sweep_sign_in_records(
        session, as_of=datetime(2001, 1, 1, tzinfo=timezone.utc)
    )

    assert receipt["audit_id"] is None
    assert receipt["outcome"] == "nothing_due"
    assert receipt["health"] == "healthy"
    assert receipt["policy_version"] == POLICY_VERSION
    assert receipt["executed_by"] == audit.SIGN_IN_RECORD_EXPIRY_ACTOR
    assert receipt["deleted"] == {
        "web_sessions": 0,
        "sign_in_tokens": 0,
        "sign_in_attempts": 0,
    }
    assert set(receipt["cutoff"]) == {
        "web_sessions",
        "sign_in_tokens",
        "sign_in_attempts",
    }


def test_the_command_needs_no_argument_and_prints_its_run(
    session, capsys, monkeypatch
):
    """A scheduled invocation runs a fixed command line and cannot pass a clock.

    The printed payload is the run record ADR-0102 relies on, so this pins both
    halves: the command runs with no argument, and every run says what it did.

    The empty human principal is *set* here rather than read, because it is the
    condition being tested and not an ambient fact. A deployment running this on
    a schedule configures no ``CORRIDOR_HUMAN_PRINCIPAL`` -- no person performs
    this act, which is why its actor is ``SIGN_IN_RECORD_EXPIRY_ACTOR`` -- and
    before this change the command built a ``HumanPrincipal`` unconditionally
    and raised on the empty string, so the scheduled run would have crashed.
    Reading the ambient value instead would pass on a developer's machine and
    fail under CI, which configures one; it did exactly that.
    """

    @contextmanager
    def factory():
        yield session

    monkeypatch.setattr(settings, "human_principal", "")
    assert retention_main(["expire-sign-in-records"], session_factory=factory) == 0

    printed = capsys.readouterr().out
    assert '"outcome"' in printed
    assert POLICY_VERSION in printed
    assert audit.SIGN_IN_RECORD_EXPIRY_ACTOR in printed


def test_a_naive_clock_is_refused_rather_than_assumed_to_be_utc(session):
    with pytest.raises(SignInRetentionRefused):
        sweep_sign_in_records(session, as_of=datetime(2026, 9, 11, 12, 0))
