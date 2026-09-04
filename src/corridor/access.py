"""Individual sign-in identity and project-scoped access (#331).

This module replaces the single deployment principal at the customer boundary
with three separated concerns, each a distinct record so none can silently
stand in for another:

- **Identity** — a verified email resolves to one stable ``HumanPrincipal``
  (``person_identities``).  That principal, unchanged, reaches the existing
  human-decision writers; sign-in never invents a parallel identity.
- **Membership** — an ``active`` ``ProjectRosterEntry`` makes the person a
  member of one project.  Membership is the read boundary and nothing more:
  it is not authority to write.
- **Designation** — each of the four write authorities (project coordination,
  Documentation Review, external release, technical operations) is an explicit
  flag on the membership.  None is implied by another or by membership, so a
  valid principal never confers all of them (ADR-0034 decisions 28 and 34,
  ADR-0035).

The magic link is a CSPRNG token stored only as a SHA-256 hash and consumed by
one atomic, single-use ``UPDATE``.  Sessions carry an opaque id (stored hashed)
with explicit expiry and revocation checked on every request, so a revoked
session or membership takes effect immediately.  The HTTP adapter owns cookies
and request-forgery tokens; this module owns the records and the rules.

Everything here accepts a caller-supplied ``Session``; it opens no engine and
sends no email.  ``now`` is injectable so expiry, reuse, and backoff are
testable without real clocks or real mail.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import (
    PersonIdentity,
    Project,
    ProjectRosterEntry,
    SignInAttempt,
    SignInToken,
    WebSession,
)
from corridor.principals import HumanPrincipal, require_human_principal


# --- Designations ---------------------------------------------------------

COORDINATION = "project_coordination"
DOCUMENTATION_REVIEW = "documentation_review"
EXTERNAL_RELEASE = "external_release"
TECHNICAL_OPERATIONS = "technical_operations"

# The four are kept distinct on purpose; this order is only for stable receipts.
DESIGNATIONS: tuple[str, ...] = (
    COORDINATION,
    DOCUMENTATION_REVIEW,
    EXTERNAL_RELEASE,
    TECHNICAL_OPERATIONS,
)

_DESIGNATION_COLUMNS = {
    COORDINATION: "can_coordinate",
    DOCUMENTATION_REVIEW: "can_review_documentation",
    EXTERNAL_RELEASE: "can_release_externally",
    TECHNICAL_OPERATIONS: "is_technical_operator",
}


# --- Backoff / lifetime policy -------------------------------------------

SIGN_IN_TOKEN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(hours=12)
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_ISSUE_PER_EMAIL = 5
MAX_ISSUE_PER_IP = 20
MAX_CONSUME_PER_IP = 30

ISSUE_EMAIL = "issue_email"
ISSUE_IP = "issue_ip"
CONSUME_IP = "consume_ip"


class AccessError(ValueError):
    """An enrollment or identity invariant was violated."""


@dataclass(frozen=True, slots=True)
class MembershipAccess:
    """One person's resolved standing in one project."""

    principal: HumanPrincipal
    project_id: int
    display_name: str
    designations: frozenset[str]

    def has(self, designation: str) -> bool:
        return designation in self.designations


@dataclass(frozen=True, slots=True)
class ConsumedSignInToken:
    """The identity a single-use link established, once and atomically."""

    email_normalized: str
    principal: HumanPrincipal
    redirect_path: str | None


@dataclass(frozen=True, slots=True)
class IssuedSignInToken:
    """A freshly minted link secret; the raw token leaves only in the email."""

    raw_token: str
    email_normalized: str
    redirect_path: str | None


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _sha256_hex(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_email(raw: str) -> str:
    """Case-fold and trim so one person is one identity, not one per spelling."""
    return (raw or "").strip().casefold()


# --- Membership and enrollment -------------------------------------------

def resolve_membership(
    session: Session, principal_subject: str, project_id: int
) -> MembershipAccess | None:
    """Live standing for this principal in this project, or ``None``.

    Read straight from the roster on every call so a deactivated membership or a
    withdrawn designation is felt at once, never masked by a cached session.
    """
    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project_id,
            ProjectRosterEntry.principal_subject == principal_subject,
            ProjectRosterEntry.active.is_(True),
        )
    ).first()
    if entry is None:
        return None
    designations = frozenset(
        name
        for name, column in _DESIGNATION_COLUMNS.items()
        if getattr(entry, column)
    )
    return MembershipAccess(
        principal=HumanPrincipal(entry.principal_subject),
        project_id=project_id,
        display_name=entry.display_name,
        designations=designations,
    )


def member_projects(session: Session, principal_subject: str) -> list[Project]:
    """Projects this principal may currently read, for a signed-in landing."""
    return list(
        session.scalars(
            select(Project)
            .join(
                ProjectRosterEntry,
                ProjectRosterEntry.project_id == Project.id,
            )
            .where(
                ProjectRosterEntry.principal_subject == principal_subject,
                ProjectRosterEntry.active.is_(True),
            )
            .order_by(Project.name, Project.id)
        ).all()
    )


def coordinated_projects(
    session: Session, principal_subject: str
) -> list[Project]:
    """Projects this principal may currently *coordinate*, not merely read.

    Membership is the read boundary and coordination is a separate explicit
    designation, so a cross-project coordination reading (#537) is scoped by
    the designation rather than by membership: a person enrolled to read a
    project is not shown that project's coordination work.
    """
    return list(
        session.scalars(
            select(Project)
            .join(
                ProjectRosterEntry,
                ProjectRosterEntry.project_id == Project.id,
            )
            .where(
                ProjectRosterEntry.principal_subject == principal_subject,
                ProjectRosterEntry.active.is_(True),
                ProjectRosterEntry.can_coordinate.is_(True),
            )
            .order_by(Project.name, Project.id)
        ).all()
    )


def enroll_member(
    session: Session,
    *,
    project_id: int,
    email: str,
    principal: HumanPrincipal,
    display_name: str,
    designations: Iterable[str] = (),
    operator: HumanPrincipal,
    active: bool = True,
    now: datetime | None = None,
) -> ProjectRosterEntry:
    """Managed, attributable, project-scoped enrollment or re-designation.

    Binds an email to its stable principal (once, never rebinding a different
    one) and records the project membership with exactly the designations named.
    The act is attributed to the operator and scoped to the one project; it
    writes no organization-registry row, so being enrolled — or being a
    selectable assignee — never becomes global mutation authority (#331).
    """
    require_human_principal(principal)
    require_human_principal(operator)
    display_name = (display_name or "").strip()
    if not display_name:
        raise AccessError("a member needs a display name")
    normalized = normalize_email(email)
    if not normalized:
        raise AccessError("a member needs an email address")
    requested = frozenset(designations)
    unknown = requested - set(DESIGNATIONS)
    if unknown:
        raise AccessError(f"unknown designation(s): {sorted(unknown)}")

    _bind_identity(session, normalized, principal)

    entry = session.scalars(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project_id,
            ProjectRosterEntry.principal_subject == principal.subject,
        )
    ).first()
    if entry is None:
        entry = ProjectRosterEntry(
            project_id=project_id,
            principal_subject=principal.subject,
            display_name=display_name,
        )
        session.add(entry)
    else:
        entry.display_name = display_name
    entry.active = active
    for name, column in _DESIGNATION_COLUMNS.items():
        setattr(entry, column, name in requested)
    session.flush()

    audit.record(
        session,
        principal=operator,
        action=audit.ENROLL_PROJECT_MEMBER,
        entity_type=audit.PROJECT,
        entity_id=project_id,
        after={
            "principal_subject": principal.subject,
            "email_normalized": normalized,
            "display_name": display_name,
            "active": active,
            "designations": sorted(requested),
        },
    )
    return entry


def _bind_identity(
    session: Session, email_normalized: str, principal: HumanPrincipal
) -> PersonIdentity:
    """One email maps to one principal, forever; refuse any conflicting rebind."""
    by_email = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.email_normalized == email_normalized
        )
    ).first()
    by_principal = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == principal.subject
        )
    ).first()
    if by_email is not None and by_email.principal_subject != principal.subject:
        raise AccessError(
            "this email is already bound to a different principal"
        )
    if by_principal is not None and by_principal.email_normalized != email_normalized:
        raise AccessError(
            "this principal is already bound to a different email"
        )
    if by_email is not None:
        return by_email
    identity = PersonIdentity(
        email_normalized=email_normalized,
        principal_subject=principal.subject,
    )
    session.add(identity)
    session.flush()
    return identity


def identity_for_email(session: Session, email: str) -> PersonIdentity | None:
    return session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.email_normalized == normalize_email(email)
        )
    ).first()


# --- Abuse backoff --------------------------------------------------------

def count_recent_attempts(
    session: Session,
    scope_kind: str,
    scope_value: str,
    *,
    now: datetime | None = None,
    window: timedelta = ATTEMPT_WINDOW,
) -> int:
    since = _now(now) - window
    return int(
        session.scalar(
            select(func.count())
            .select_from(SignInAttempt)
            .where(
                SignInAttempt.scope_kind == scope_kind,
                SignInAttempt.scope_value == scope_value,
                SignInAttempt.occurred_at >= since,
            )
        )
        or 0
    )


def record_attempt(
    session: Session, scope_kind: str, scope_value: str
) -> None:
    session.add(
        SignInAttempt(scope_kind=scope_kind, scope_value=scope_value)
    )
    session.flush()


def over_limit(
    session: Session,
    scope_kind: str,
    scope_value: str,
    limit: int,
    *,
    now: datetime | None = None,
) -> bool:
    return count_recent_attempts(session, scope_kind, scope_value, now=now) >= limit


# --- Magic-link tokens ----------------------------------------------------

def has_live_token(
    session: Session, email_normalized: str, *, now: datetime | None = None
) -> bool:
    """A still-usable link already exists, so a duplicate request coalesces."""
    moment = _now(now)
    return (
        session.scalars(
            select(SignInToken.id).where(
                SignInToken.email_normalized == email_normalized,
                SignInToken.consumed_at.is_(None),
                SignInToken.expires_at > moment,
            )
        ).first()
        is not None
    )


def issue_sign_in_token(
    session: Session,
    email_normalized: str,
    *,
    redirect_path: str | None,
    now: datetime | None = None,
    ttl: timedelta = SIGN_IN_TOKEN_TTL,
) -> IssuedSignInToken:
    """Mint one CSPRNG link secret; persist only its hash and expiry."""
    moment = _now(now)
    raw_token = secrets.token_urlsafe(32)
    session.add(
        SignInToken(
            email_normalized=email_normalized,
            token_sha256=_sha256_hex(raw_token),
            redirect_path=redirect_path,
            expires_at=moment + ttl,
        )
    )
    session.flush()
    return IssuedSignInToken(
        raw_token=raw_token,
        email_normalized=email_normalized,
        redirect_path=redirect_path,
    )


def consume_sign_in_token(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> ConsumedSignInToken | None:
    """Spend a link exactly once, atomically; return the identity it proves.

    The single ``UPDATE ... WHERE consumed_at IS NULL AND expires_at > now``
    RETURNING is the whole guard: expired, reused, tampered, and concurrent
    consumers all fall through to ``None`` because at most one statement can flip
    the row.  A malformed or unknown token hashes to a value no row carries.
    """
    moment = _now(now)
    token_sha256 = _sha256_hex(raw_token or "")
    row = session.execute(
        update(SignInToken)
        .where(
            SignInToken.token_sha256 == token_sha256,
            SignInToken.consumed_at.is_(None),
            SignInToken.expires_at > moment,
        )
        .values(consumed_at=moment)
        .returning(SignInToken.email_normalized, SignInToken.redirect_path)
    ).first()
    if row is None:
        return None
    email_normalized, redirect_path = row
    identity = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.email_normalized == email_normalized
        )
    ).first()
    if identity is None:
        # The identity was removed after the link was issued; establish nothing.
        return None
    return ConsumedSignInToken(
        email_normalized=email_normalized,
        principal=HumanPrincipal(identity.principal_subject),
        redirect_path=redirect_path,
    )


# --- Sessions -------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NewWebSession:
    """The two secrets a fresh session hands to the browser, and nothing else."""

    raw_session_id: str
    raw_csrf_token: str
    expires_at: datetime


def create_web_session(
    session: Session,
    *,
    principal: HumanPrincipal,
    email_normalized: str,
    now: datetime | None = None,
    ttl: timedelta = SESSION_TTL,
) -> NewWebSession:
    """Open a session, storing only hashes of its id and request-forgery token."""
    require_human_principal(principal)
    moment = _now(now)
    raw_session_id = secrets.token_urlsafe(32)
    raw_csrf_token = secrets.token_urlsafe(32)
    expires_at = moment + ttl
    session.add(
        WebSession(
            session_sha256=_sha256_hex(raw_session_id),
            csrf_sha256=_sha256_hex(raw_csrf_token),
            principal_subject=principal.subject,
            email_normalized=email_normalized,
            expires_at=expires_at,
        )
    )
    session.flush()
    return NewWebSession(
        raw_session_id=raw_session_id,
        raw_csrf_token=raw_csrf_token,
        expires_at=expires_at,
    )


def resolve_web_session(
    session: Session, raw_session_id: str, *, now: datetime | None = None
) -> WebSession | None:
    """The live session for this cookie, or ``None`` if expired/revoked/unknown."""
    if not raw_session_id:
        return None
    moment = _now(now)
    return session.scalars(
        select(WebSession).where(
            WebSession.session_sha256 == _sha256_hex(raw_session_id),
            WebSession.revoked_at.is_(None),
            WebSession.expires_at > moment,
        )
    ).first()


def revoke_web_session(
    session: Session, raw_session_id: str, *, now: datetime | None = None
) -> bool:
    """Revoke on logout; a second logout or an unknown id changes nothing."""
    if not raw_session_id:
        return False
    moment = _now(now)
    updated = session.execute(
        update(WebSession)
        .where(
            WebSession.session_sha256 == _sha256_hex(raw_session_id),
            WebSession.revoked_at.is_(None),
        )
        .values(revoked_at=moment)
    )
    return bool(updated.rowcount)


def csrf_token_matches(web_session: WebSession, submitted: str | None) -> bool:
    """Constant-time check that a write echoed this session's forgery token."""
    if not submitted:
        return False
    return secrets.compare_digest(web_session.csrf_sha256, _sha256_hex(submitted))
