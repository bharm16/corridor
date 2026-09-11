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

Two further boundaries live here (#531, ratifying #503), completed by #657 and
enforced by #680: one transaction holds one project-authorization scope, and
every relation the web capability can read carries a recorded answer to how
the partition covers it (``PARTITIONED_RELATIONS`` and the three lists beside
it).  #680 made the fourth list — the relations with no answer yet — a
deployment boundary rather than an inventory: ``corridor_web`` now holds no
privilege on any of them, and ``corridor.web_boundary`` says which routes the
live pilot serves on what remains.

- **Partition** — membership is also a *data* boundary, not only a rule the
  readers agree to keep.  ``open_project_partition`` asks PostgreSQL to prove
  the roster entry and then declare, for this transaction only, which projects
  the connection may see; row-level security on the project-scoped spine
  relations does the rest.  A reader that forgets its ``where`` clause reads
  nothing rather than another project's rows.  Both seals — the effective
  scope and #657's declaration — cover PostgreSQL's own top-level transaction
  id, so a capability that keeps a genuine setting-and-seal pair cannot replay
  it in a later transaction on the same pooled connection (#676).

  **The cost of that, accepted rather than avoided:** sealing calls
  ``pg_current_xact_id()``, so opening a project partition assigns a real
  transaction id even for a request that goes on to read only.  PostgreSQL
  documents that assignment, and it means every partitioned web request
  consumes an id and appears in ``pg_xact`` instead of running id-less.  The
  alternative was sealing over a clock reading, which two transactions can
  share and which is therefore not an identity at all; a seal that does not
  name its transaction is a seal that can be replayed.
- **Deprovisioning** — ``deprovision_principal`` is the whole offboarding act:
  every membership deactivated, every live session revoked, every pending
  sign-in link spent, in one transaction.  What it deliberately does *not*
  remove is the identity binding, because an accepted decision keeps the human
  principal who made it and a record that could no longer name that person
  would be a worse record, not a safer one.

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
from sqlalchemy.exc import DBAPIError
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

# The SQLSTATE the partition commands raise when a transaction is asked for a
# second, different scope. Class 25 is "invalid transaction state", which is
# what this is: the request is well formed and the principal may even be
# entitled to it, but not here and not now (#662).
_SCOPE_CONFLICT = "25000"

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


def retire_undelivered_sign_in_token(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> bool:
    """Retire a token whose link never reached anyone. True if one was retired.

    `has_live_token` coalesces duplicate requests, which is right while a link
    is in someone's inbox and wrong when delivery failed: every retry inside
    the 15-minute window would find the dead token, issue nothing, and leave an
    enrolled person unable to sign in until it expired. The raw token exists
    nowhere else once the request is over, so it cannot be resent.

    Marked consumed rather than deleted, using the same single-use guard, so
    the row still records that a token existed and was spent. Retiring one that
    was in fact delivered costs the recipient one more request; leaving a dead
    one in place costs them fifteen minutes.
    """

    moment = _now(now)
    retired = session.execute(
        update(SignInToken)
        .where(
            SignInToken.token_sha256 == _sha256_hex(raw_token),
            SignInToken.consumed_at.is_(None),
            SignInToken.expires_at > moment,
        )
        .values(consumed_at=moment)
        .returning(SignInToken.id)
    ).first()
    return retired is not None


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
    # The sign-in entry is written in the same transaction as the session
    # record, so an export can never claim a sign-in that was rolled back, and
    # a session can never exist without one (#531).
    audit.record(
        session,
        principal=principal,
        action=audit.SIGN_IN,
        entity_type=audit.PERSON_IDENTITY,
        entity_id=_identity_id(session, email_normalized),
        after={"email_normalized": email_normalized},
    )
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


def expired_web_session(
    session: Session, raw_session_id: str, *, now: datetime | None = None
) -> WebSession | None:
    """The session this cookie names, only when it expired and was not revoked.

    ``resolve_web_session`` answers one question — may this request act — and
    collapses expired, revoked and unknown into the same ``None``, which is
    right for authorization and wrong for the one caller that has to tell them
    apart.  A session that simply ran out of time belonged to a person who was
    working a moment ago; a revoked one belongs to someone who signed out or
    was offboarded, and #844 turns on never treating the second as the first.
    This still authorizes nothing: it returns a row that has already failed
    every check, so a caller can name the person who was here and no more.
    """
    if not raw_session_id:
        return None
    moment = _now(now)
    return session.scalars(
        select(WebSession).where(
            WebSession.session_sha256 == _sha256_hex(raw_session_id),
            WebSession.revoked_at.is_(None),
            WebSession.expires_at <= moment,
        )
    ).first()


def revoke_web_session(
    session: Session, raw_session_id: str, *, now: datetime | None = None
) -> bool:
    """Revoke on logout; a second logout or an unknown id changes nothing.

    A revocation that changed nothing records nothing: the audit entry follows
    the row count, so a replayed logout cannot manufacture a second event in
    the identity export.
    """
    if not raw_session_id:
        return False
    moment = _now(now)
    revoked = session.execute(
        update(WebSession)
        .where(
            WebSession.session_sha256 == _sha256_hex(raw_session_id),
            WebSession.revoked_at.is_(None),
        )
        .values(revoked_at=moment)
        .returning(WebSession.principal_subject, WebSession.email_normalized)
    ).first()
    if revoked is None:
        return False
    principal_subject, email_normalized = revoked
    audit.record(
        session,
        principal=HumanPrincipal(principal_subject),
        action=audit.SIGN_OUT,
        entity_type=audit.PERSON_IDENTITY,
        entity_id=_identity_id(session, email_normalized),
        after={"email_normalized": email_normalized},
    )
    return True


def csrf_token_matches(web_session: WebSession, submitted: str | None) -> bool:
    """Constant-time check that a write echoed this session's forgery token."""
    if not submitted:
        return False
    return secrets.compare_digest(web_session.csrf_sha256, _sha256_hex(submitted))


def _identity_id(session: Session, email_normalized: str) -> int:
    """The identity row an act names, or the unbound marker.

    Sign-in always has one, because a link can only be consumed for a bound
    email.  A session opened directly — by a test harness, or by a future
    non-email credential — has none, and says so rather than borrowing an
    unrelated row's id.
    """
    identity_id = session.scalar(
        select(PersonIdentity.id).where(
            PersonIdentity.email_normalized == email_normalized
        )
    )
    return int(identity_id) if identity_id is not None else audit.UNBOUND_IDENTITY


# --- Offboarding ----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Deprovisioning:
    """What one offboarding act actually took away."""

    principal_subject: str
    email_normalized: str | None
    projects_left: tuple[int, ...]
    sessions_revoked: int
    tokens_invalidated: int


def deprovision_principal(
    session: Session,
    *,
    principal: HumanPrincipal,
    operator: HumanPrincipal,
    now: datetime | None = None,
) -> Deprovisioning:
    """Take one person off the system, in one transaction (#531, #503).

    Three things end together, because ending them separately leaves a window
    in which the person is off the roster but still holding a live cookie, or
    off the roster with an unspent link in their inbox:

    - every active membership is deactivated *and* stripped of its
      designations, so a later re-enrollment has to grant each one again
      rather than reviving authority nobody re-decided;
    - every live session is revoked, which the request path checks on every
      request, so the next click is refused rather than the next hour's;
    - every unspent, unexpired sign-in link is marked consumed, so a link
      already sent cannot open a session after the fact.

    What survives on purpose is the identity binding and every historical act.
    The person's principal still names them on the decisions they made; ADR-0081
    is explicit that a migration executor is never the semantic author, and the
    same holds for an offboarding operator.  Deprovisioning removes *access*,
    never authorship.

    Idempotent: a second call finds nothing active and reports zeros, recording
    the act but inventing no second revocation.
    """
    require_human_principal(principal)
    require_human_principal(operator)
    moment = _now(now)

    entries = list(
        session.scalars(
            select(ProjectRosterEntry)
            .where(
                ProjectRosterEntry.principal_subject == principal.subject,
                ProjectRosterEntry.active.is_(True),
            )
            .order_by(ProjectRosterEntry.project_id)
        ).all()
    )
    projects_left = tuple(entry.project_id for entry in entries)
    for entry in entries:
        before = {
            "active": True,
            "designations": sorted(
                name
                for name, column in _DESIGNATION_COLUMNS.items()
                if getattr(entry, column)
            ),
        }
        entry.active = False
        for column in _DESIGNATION_COLUMNS.values():
            setattr(entry, column, False)
        session.flush()
        audit.record(
            session,
            principal=operator,
            action=audit.DEPROVISION_PROJECT_MEMBER,
            entity_type=audit.PROJECT,
            entity_id=entry.project_id,
            before=before,
            after={
                "principal_subject": principal.subject,
                "active": False,
                "designations": [],
            },
        )

    sessions_revoked = int(
        session.execute(
            update(WebSession)
            .where(
                WebSession.principal_subject == principal.subject,
                WebSession.revoked_at.is_(None),
            )
            .values(revoked_at=moment)
        ).rowcount
    )

    identity = session.scalars(
        select(PersonIdentity).where(
            PersonIdentity.principal_subject == principal.subject
        )
    ).first()
    email_normalized = identity.email_normalized if identity is not None else None
    tokens_invalidated = 0
    if email_normalized is not None:
        tokens_invalidated = int(
            session.execute(
                update(SignInToken)
                .where(
                    SignInToken.email_normalized == email_normalized,
                    SignInToken.consumed_at.is_(None),
                    SignInToken.expires_at > moment,
                )
                .values(consumed_at=moment)
            ).rowcount
        )

    audit.record(
        session,
        principal=operator,
        action=audit.DEPROVISION_PRINCIPAL,
        entity_type=audit.PERSON_IDENTITY,
        entity_id=(
            identity.id if identity is not None else audit.UNBOUND_IDENTITY
        ),
        after={
            "principal_subject": principal.subject,
            "email_normalized": email_normalized,
            "projects_left": list(projects_left),
            "sessions_revoked": sessions_revoked,
            "tokens_invalidated": tokens_invalidated,
            "identity_binding_retained": identity is not None,
        },
    )
    return Deprovisioning(
        principal_subject=principal.subject,
        email_normalized=email_normalized,
        projects_left=projects_left,
        sessions_revoked=sessions_revoked,
        tokens_invalidated=tokens_invalidated,
    )


# --- What the partition covers, relation by relation (#657) ---------------
#
# ADR-0083 makes the project a data-partition boundary inside the customer
# database.  A boundary that covers some relations and not others is not a
# boundary, so every relation ``corridor_web`` can read is classified here and
# ``tests/test_architecture.py`` refuses a new one that is not.
#
# The classification is deliberately not "every relation gets a policy".  Some
# relations are the partition's own inputs and partitioning them would make the
# partition unreachable.  Some are genuinely the whole customer database's.
# One is a view that is answered by the partitioned tables under it.  And some
# are project-scoped, directly selectable, and simply not covered yet — those
# are named with what is missing, because an honest list of holes is worth more
# than a list that reads as complete.
#
# ``NOT_YET_PARTITIONED_CEILING`` is a ratchet in the shape the migration
# policy already uses: it may fall and must never rise.  A new project-scoped
# relation therefore has to be partitioned, or has to displace one that is.
#
# **What "not yet partitioned" means after #680.**  It no longer means
# "readable across every project".  #657 left 130 such relations directly
# selectable by ``corridor_web``, which is what made the classification an
# inventory rather than a boundary.  #680 turned it into one: the live-pilot
# web capability now holds *no privilege at all* on any relation in this
# list, so the honest list of holes is also the list of things the human web
# role cannot open.  The rule is enforced against the database itself in
# ``tests/test_project_partition_and_offboarding.py`` — every name here is
# asserted unreadable by the real login — so the two halves cannot drift.
#
# Six relations moved out of this list.  Four because an enabled pilot route
# genuinely reads them: ``documents`` and ``source_deliveries`` (every project
# surface, and the source register the week shows) and
# ``external_report_artifacts`` and ``external_report_releases`` (the record
# history's issue trail).  #657 recorded that partitioning the first two would
# refuse a working ingress; #680 removed that objection rather than overruling
# it, by moving the transport-authenticated ingress onto the worker
# capability, which holds the unpartitioned policy.
#
# The other two — ``push_intake_credentials`` and
# ``connector_checkpoint_advances`` — are read by no route at all.  They are
# partitioned rather than revoked because #511 gives the *human* capability a
# designed authority over them: revoking a credential is a person's act, and
# taking that authority away would be this ticket deciding something #511
# decided.  Both carry a ``project_id``, so the partition is available and the
# grants stay.  ``connector_checkpoint_advance_deliveries`` does not carry
# one, no human surface has ever written it, and the connector poller writes
# it as ``corridor_worker``, so that one is revoked.
#
# **#824 moved six more, and corrected a reason while it did.**  Admitting the
# upload, confirmation and source-register routes gave five of them a reader
# at last: ``doc_pages``, ``token_layers``, ``page_render_derivatives`` and
# ``document_quarantines`` are what confirming one upload writes, and
# ``extraction_runs`` and ``document_quarantines`` are what the register reads
# to say honestly whether a document has been processed.  ``processing_artifacts``
# carries a ``project_id`` and takes the ordinary policy.
#
# The other five carry none, and the reason recorded against them said a
# policy therefore "has nothing to test".  That was wrong: a policy tests an
# expression, and theirs joins the parent it already has.  Each row belongs to
# one ``documents`` row, ``documents`` is partitioned, so the policy asks
# whether *that* document is in the caller's partition.  The remaining
# "carries no project column" entries below are still revoked, but the reason
# is that no enabled route reads them -- not that they could not be
# partitioned if one did.

PARTITION_CLASSIFICATIONS = (
    "partitioned",
    "authorization input",
    "protected through another relation",
    "customer-wide",
    "not yet partitioned",
    "not web-readable",
)

PARTITIONED_RELATIONS: frozenset[str] = frozenset(
    {
        "legacy_history_batches",
        "legacy_history_reversals",
        "legacy_history_evidence_migrations",
        "coordination_record_subjects",
        "coordination_subject_lineage",
        "coordination_history_activations",
        "coordination_record_decisions",
        "coordination_decision_lineage",
        "coordination_record_reversals",
        "coordination_reversal_lineage",
        "support_scope_lineage",
        "support_history_receipts",
        "pipeline_observations",
        "pipeline_comparisons",
        "pipeline_qualifications",
        "pipeline_acceptances",
        "pipeline_selections",
        "candidates",
        "capture_correction_requests",
        "connector_checkpoint_advances",
        "delta_decision_supports",
        "delta_deferrals",
        "delta_dispositions",
        "delta_follow_up_plan_closures",
        "delta_follow_up_plan_evidence",
        "delta_follow_up_plans",
        "delta_groups",
        "delta_record_decisions",
        "delta_review_packet_children",
        "delta_review_packet_receipts",
        "delta_review_packet_reversals",
        "delta_review_packet_supports",
        "delta_supersessions",
        "dependency_events",
        "doc_pages",
        "document_quarantines",
        "documents",
        "evidence_link_sources",
        "extracted_proposal_facts",
        "extracted_proposals",
        "external_report_artifacts",
        "external_report_releases",
        "extraction_runs",
        "fact_applies_to",
        "fact_closure_results",
        "fact_closure_sources",
        "fact_decisions",
        "fact_dispositions",
        "fact_sources",
        "fact_statement_timings",
        "facts",
        "issue_coverage_declarations",
        "outgoing_request_plans",
        "outgoing_request_responses",
        "outgoing_requests",
        "page_render_derivatives",
        "processing_artifacts",
        "project_baseline_adoptions",
        "project_baseline_format_manifests",
        "project_baseline_format_objects",
        "project_baseline_formats",
        "project_baseline_source_rows",
        "project_baseline_sources",
        "project_contact_imports",
        "project_contacts",
        "minutes_captures",
        "proposed_delta_impact_derivations",
        "project_issue_profile_artifacts",
        "project_issue_profiles",
        "project_record_revisions",
        "proposed_deltas",
        "push_intake_credentials",
        "record_inclusion_requests",
        "recorded_verbal_origin_backfill_receipts",
        "recorded_verbal_origin_fact_digests",
        "recorded_verbal_origin_statements",
        "recorded_verbal_origins",
        "release_candidate_artifacts",
        "release_candidates",
        "release_package_artifacts",
        "release_packages",
        "release_preparation_attempts",
        "release_preparation_publications",
        "release_preparation_readings",
        "release_preparation_refusals",
        "release_preparation_requests",
        "scanned_page_observations",
        "source_deliveries",
        "source_delivery_confirmations",
        "source_fact_append_receipts",
        "source_revision_declarations",
        "source_segments",
        "spend_authorizations",
        "support_assessment_sources",
        "support_assessments",
        "token_layers",
    }
)

AUTHORIZATION_INPUT_RELATIONS: dict[str, str] = {
    "customer_environment_binding": (
        "immutable deployment/customer identity only; routing checks it before "
        "sign-in, customer content or project membership is read (#656)"
    ),
    "person_identities": (
        "identity, not project data; the sign-in path reads it before any "
        "project is named"
    ),
    "project_roster_entries": (
        "a declared partition is derived from it: `resolve_membership` reads "
        "it before the declaration, and both proving commands re-prove "
        "against it as their own owner. Partitioning it would make the "
        "partition unreachable"
    ),
    "projects": (
        "the registry a slug is resolved against, read by the gate before any "
        "partition exists"
    ),
    "sign_in_attempts": (
        "identity, not project data; the backoff counter is keyed by email "
        "and address, never by project"
    ),
    "sign_in_tokens": (
        "identity, not project data; a link is minted and spent before any "
        "membership is resolved"
    ),
    "web_sessions": (
        "identity, not project data; every request resolves the cookie here "
        "before it knows which project it is for"
    ),
}

PROTECTED_RELATIONS: dict[str, str] = {
    "current_coordination_record": "security-invoker view over project-partitioned native coordination decisions and reversals",
    "current_project_processing_pass": (
        "a view over `due_work_occurrences` and `due_work_schedules`, both of "
        "which stay revoked below. It is protected the other way round from "
        "the two views beside it: they are security-invoker over partitioned "
        "tables, and this one carries `current_project_partition()` in its own "
        "body because the relations under it grant `corridor_web` nothing. A "
        "caller with no declared partition reads no rows, and the four columns "
        "it exposes carry no claim token, no owner and no declaration (#900)"
    ),
    "current_project_record": (
        "a view over `fact_decisions` and `facts`, both partitioned. #657 set "
        "`security_invoker` on it, so it reads as the caller and the "
        "partition on the tables under it is what answers the query. Without "
        "that the schema owner's row-level security bypass reached straight "
        "through it"
    ),
}

CUSTOMER_WIDE_RELATIONS: dict[str, str] = {
    "pipeline_configurations": (
        "immutable technical code, dependency, profile and prompt identities; "
        "actual source readings and values belong to partitioned observations"
    ),
    "pipeline_qualification_policies": (
        "immutable maintenance metric contracts and numeric rules, with scope "
        "digest only; source content and gate results are partitioned separately"
    ),
    "audit_log": (
        "the append-only attribution ledger of the whole customer database. "
        "Offboarding keeps authorship (ADR-0081) and the identity export "
        "reads it across projects, so it is not one project's rows"
    ),
    "external_orgs": (
        "the organization registry. #331 is explicit that enrollment writes "
        "no organization-registry row: being in it is not project data"
    ),
    "extractor_configurations": (
        "one sealed extractor receipt per digest, shared by every run in the "
        "database. #605 de-duplicated it precisely because it is not per- "
        "project and carries no project column"
    ),
}

NON_WEB_RELATIONS: dict[str, str] = {
    "shadow_projects": "isolated shadow binding; web privileges are revoked and its project is hidden by a restrictive registry policy",
    "shadow_runs": "non-authoritative frozen source predictions; only the isolated worker may read or append them",
}


NOT_YET_PARTITIONED_RELATIONS: dict[str, str] = {
    "active_extraction_runs": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `documents`, "
        "`extraction_runs`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "active_run_declarations": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `documents`, "
        "`extraction_runs`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "assertions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`evidence_links`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "assignment_notification_attempts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "assignment_notification_dispatches": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "assignment_notification_feedback": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "assignment_notifications": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "automatic_carry_forward_outcomes": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "automatic_carry_forward_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "candidate_dispositions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `candidates`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "cohort_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "commitment_lineages": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "condition_resolutions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependency_events`, "
        "`evidence_links`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "connector_checkpoint_advance_deliveries": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "coordination_summary_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "coordination_summary_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "dependencies": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "dependency_admission_outcomes": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `candidates`, "
        "`dependencies`, `policy_runs`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "dependency_dismissals": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "dependency_event_evidence": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependency_events`, "
        "`evidence_links`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "dependency_event_migration_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependency_events`, and "
        "#680 revoked every `corridor_web` privilege on it, so the live- "
        "pilot web capability cannot reach it by any id at all"
    ),
    "dependency_event_scope_decisions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependency_events`, and "
        "#680 revoked every `corridor_web` privilege on it, so the live- "
        "pilot web capability cannot reach it by any id at all"
    ),
    "dependency_event_scopes": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`dependency_event_scope_decisions`, `dependency_events`, and "
        "#680 revoked every `corridor_web` privilege on it, so the live- "
        "pilot web capability cannot reach it by any id at all"
    ),
    "dependency_event_timings": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependency_events`, and "
        "#680 revoked every `corridor_web` privilege on it, so the live- "
        "pilot web capability cannot reach it by any id at all"
    ),
    "dependency_evidence_sufficiencies": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`dependency_event_scopes`, `evidence_links`, and #680 revoked "
        "every `corridor_web` privilege on it, so the live-pilot web "
        "capability cannot reach it by any id at all"
    ),
    "discovered_references": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "dispute_history_resolutions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `assertions`, "
        "`dependencies`, and #680 revoked every `corridor_web` privilege "
        "on it, so the live-pilot web capability cannot reach it by any "
        "id at all"
    ),
    "dispute_settlements": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "document_notification_attempts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "document_notification_dispatches": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "document_notifications": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "document_rendition_derivations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "documentation_field_confirmations": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`evidence_links`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "due_action_notification_attempts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "due_action_notification_dispatches": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "due_action_notifications": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "due_work_occurrences": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `due_work_schedules`, and "
        "#680 revoked every `corridor_web` privilege on it, so the live- "
        "pilot web capability cannot reach it by any id at all"
    ),
    "due_work_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "due_work_schedules": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "event_admission_acceptance_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "event_admission_outcomes": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, "
        "`candidate_dispositions`, `candidates`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "event_cohort_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_candidate_review_starts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_capture_contracts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_capture_results": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_evaluation_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through no foreign key, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "evidence_investigation_packet_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`evidence_investigation_runs`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "evidence_investigation_review_observations": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`evidence_investigation_shadow_cases`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "evidence_investigation_runs": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_shadow_cases": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "evidence_investigation_shadow_executions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`evidence_investigation_runs`, "
        "`evidence_investigation_shadow_cases`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "evidence_investigation_shadow_outcomes": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`evidence_investigation_shadow_cases`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "evidence_investigation_step_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`evidence_investigation_runs`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "evidence_links": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`documents`, and #680 revoked every `corridor_web` privilege on "
        "it, so the live-pilot web capability cannot reach it by any id "
        "at all"
    ),
    "extraction_failure_diagnosis_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "extraction_failure_diagnosis_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "extraction_measurement_case_states": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "extraction_run_candidates": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `candidates`, "
        "`extraction_runs`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "follow_up_plan_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, "
        "`dependencies`, `project_roster_entries`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "follow_up_plan_reversals": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, "
        "`follow_up_plan_receipts`, `work_decisions`, and #680 revoked "
        "every `corridor_web` privilege on it, so the live-pilot web "
        "capability cannot reach it by any id at all"
    ),
    "inbound_messages": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "inbound_route_triage": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "inbound_thread_readings": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `candidates`, "
        "`inbound_messages`, `inbound_threads`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "inbound_threads": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "intake_project_identifiers": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "key_date_draft_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "key_date_draft_row_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `key_date_draft_receipts`, "
        "and #680 revoked every `corridor_web` privilege on it, so the "
        "live-pilot web capability cannot reach it by any id at all"
    ),
    "legacy_ledger_archives": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "milestone_registrations": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `milestones`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "milestones": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "operative_support": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`dependency_event_scopes`, `evidence_links`, and #680 revoked "
        "every `corridor_web` privilege on it, so the live-pilot web "
        "capability cannot reach it by any id at all"
    ),
    "organization_identity_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "page_processing_failures": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `documents`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "policy_activations": (
        "project-scoped, and no enabled live-pilot route reads it. One "
        "relation now carries every ADR-0050 activation ledger, keyed by "
        "policy family; #680 revoked every `corridor_web` privilege on the "
        "four it replaced rather than writing a policy for a reader that does "
        "not exist, and the consolidated relation inherits that boundary"
    ),
    "policy_approvals": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "policy_runs": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "production_run_explanation_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "production_run_explanation_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "project_check_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "reconfirmation_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, `candidates`, "
        "`dependencies`, and #680 revoked every `corridor_web` privilege "
        "on it, so the live-pilot web capability cannot reach it by any "
        "id at all"
    ),
    "report_runs": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "retention_holds": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "retention_manifest_items": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "retention_manifests": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through no foreign key, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "retention_references": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "retired_automatic_carry_forward_policy_activations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "retired_dependency_statuses": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, and #680 "
        "revoked every `corridor_web` privilege on it, so the live-pilot "
        "web capability cannot reach it by any id at all"
    ),
    "revision_change_explanation_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "revision_change_explanation_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "revision_comparison_findings": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `revision_comparison_runs`, "
        "and #680 revoked every `corridor_web` privilege on it, so the "
        "live-pilot web capability cannot reach it by any id at all"
    ),
    "revision_comparison_runs": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "revision_reconciliation_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "schedule_governing_derivations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "schedule_link_receipts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "scheduled_report_publications": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "source_fetch_attempts": (
        "read or written by the transport-authenticated ingress paths, "
        "which carry no person's membership and so declare no partition. "
        "#680 moved that ingress onto the worker capability and revoked "
        "every `corridor_web` privilege here, so the human web role no "
        "longer reaches it and the frozen global-address route (ADR-0059) "
        "keeps working as the machine capability it always was"
    ),
    "source_intake_draft_configurations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "source_intake_draft_requests": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "stated_by_people": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "statement_coordination_receipts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, "
        "`candidate_dispositions`, `candidates`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "statement_coordination_reversal_effects": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`statement_coordination_reversals`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "statement_coordination_reversals": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `audit_log`, "
        "`candidate_dispositions`, `candidates`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "statement_suggestion_eligibility_declarations": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "statement_suggestion_protection_ends": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`statement_suggestion_protections`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "statement_suggestion_protections": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "subject_candidate_suggestions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`subject_resolution_attempts`, `subject_resolution_candidates`, "
        "and #680 revoked every `corridor_web` privilege on it, so the "
        "live-pilot web capability cannot reach it by any id at all"
    ),
    "subject_resolution_attempts": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "subject_resolution_candidates": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `dependencies`, "
        "`documents`, `external_orgs`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "subject_resolution_decisions": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "unreadable_cell_reading_profiles": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "unreadable_cell_reading_runs": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "unreadable_cell_reading_steps": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through "
        "`unreadable_cell_reading_runs`, and #680 revoked every "
        "`corridor_web` privilege on it, so the live-pilot web capability "
        "cannot reach it by any id at all"
    ),
    "unreadable_cell_resolutions": (
        "project-scoped, and no enabled live-pilot route reads it. #680 "
        "revoked every `corridor_web` privilege on it rather than writing "
        "a policy for a reader that does not exist; a route that needs it "
        "again has to partition it first"
    ),
    "work_decision_milestone_impacts": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `milestones`, "
        "`work_decisions`, and #680 revoked every `corridor_web` "
        "privilege on it, so the live-pilot web capability cannot reach "
        "it by any id at all"
    ),
    "work_decisions": (
        "carries no project column, so a partition policy has nothing to "
        "test. Its project is reached through `commitment_lineages`, "
        "`dependencies`, and #680 revoked every `corridor_web` privilege "
        "on it, so the live-pilot web capability cannot reach it by any "
        "id at all"
    ),
}

NOT_YET_PARTITIONED_CEILING = 115


def classify_relation(name: str) -> tuple[str, str]:
    """How the partition answers this relation, and why, or ``("", "")``.

    An empty answer is the ratchet's whole point: a relation nobody classified
    is a relation nobody decided about, and that is what fails the build rather
    than what quietly reads across projects.
    """
    if name in PARTITIONED_RELATIONS:
        return ("partitioned", "carries the project-partition policy")
    for classification, relations in (
        ("authorization input", AUTHORIZATION_INPUT_RELATIONS),
        ("protected through another relation", PROTECTED_RELATIONS),
        ("customer-wide", CUSTOMER_WIDE_RELATIONS),
        ("not yet partitioned", NOT_YET_PARTITIONED_RELATIONS),
        ("not web-readable", NON_WEB_RELATIONS),
    ):
        if name in relations:
            return (classification, relations[name])
    return ("", "")


def unclassified_relations(names: Iterable[str]) -> tuple[str, ...]:
    """The relations nothing in this module has an answer for, in order."""
    return tuple(
        sorted(name for name in names if classify_relation(name)[0] == "")
    )


# --- The project data partition -------------------------------------------

class PartitionRefused(AccessError):
    """The database would not declare this partition."""


class PartitionScopeConflict(AccessError):
    """This transaction already declared a different project-authorization scope.

    A separate class from ``PartitionRefused`` on purpose.  A refusal is an
    answer about *this person and this project* — a caller may legitimately
    catch it and offer something narrower.  A conflict is an answer about the
    *transaction*: two scopes were asked for inside one unit of work, which is
    a defect in the caller rather than a fact about the roster, and swallowing
    it would hide exactly the confusion #662 asked to be refused.
    """


def _sqlstate(error: DBAPIError) -> str:
    """The PostgreSQL error code, or the empty string if the driver hid it."""
    return str(getattr(error.orig, "sqlstate", "") or "")


# PostgreSQL's `insufficient_privilege`.  Every command and guard that re-proves
# a roster designation inside the database raises it -- `open_project_partition`
# for membership, `authorize_release_package` for external release,
# `enforce_coordination_designation` for project coordination -- so a caller
# that means to render the refusal rather than crash on it has one code to
# recognise and does not classify a message by reading it.
INSUFFICIENT_PRIVILEGE = "42501"


def designation_refused(error: DBAPIError) -> bool:
    """Whether PostgreSQL refused this write for want of a designation.

    It answers only *which kind* of refusal arrived.  The sentence a person
    reads belongs to the act that was refused -- confirming coverage and
    authorizing a release are refused for different reasons and lead somewhere
    different -- so this module deliberately composes none.
    """

    return _sqlstate(error) == INSUFFICIENT_PRIVILEGE


def open_project_partition(
    session: Session, *, principal_subject: str, project_id: int
) -> int:
    """Declare, for this transaction, the one project this connection may read.

    The declaration is PostgreSQL's, not ours: the command re-proves the active
    roster entry as its own owner and seals the scope with a secret no runtime
    login can read, so an application that set the setting by hand would
    produce a scope that verifies as empty.  Under row-level security a reader
    that forgets ``project_id ==`` then reads nothing instead of another
    project's rows (#531, ADR-0083).

    A refusal is *recoverable* (#654).  The database proves membership by
    raising, which aborts the transaction the caller is in, so the command runs
    inside a savepoint and only that savepoint is given up before
    ``PartitionRefused`` leaves here.  Catching it therefore returns control
    with the surrounding session still usable — a caller that means to offer a
    narrower reading, fall back to a project list, or render a partial page can
    do so on the same transaction instead of meeting
    ``InFailedSqlTransaction`` on its next statement.  A full
    ``session.rollback()`` would buy the same recovery by discarding whatever
    legitimate work the caller had already done, which is why it is not used.

    Because the declaration is transaction-local (``set_config(..., true)``),
    rolling the savepoint back also takes back any scope the attempt had
    installed and restores whatever scope was declared before it.  So a failed
    attempt installs no unauthorized partition and does not cost the caller a
    valid one it already held.  The transaction id the seal covers (#676) is
    the *top-level* one, which a subtransaction neither owns nor gives back, so
    the restored scope's seal still verifies after the savepoint is discarded.
    """
    try:
        with session.begin_nested():
            scoped = session.scalar(
                select(func.open_project_partition(principal_subject, project_id))
            )
    except DBAPIError as error:
        if _sqlstate(error) == _SCOPE_CONFLICT:
            raise PartitionScopeConflict(
                "this transaction already declared a different "
                "project-authorization scope"
            ) from error
        raise PartitionRefused(
            f"no active membership of project {project_id}"
        ) from error
    return int(scoped)


def open_member_project_partition(
    session: Session, *, principal_subject: str
) -> tuple[int, ...]:
    """Declare the partition a cross-project reading may see: this person's projects.

    A principal with no active membership left declares the empty partition,
    which is what an offboarded person's still-open connection should be able
    to read: nothing.

    The scope this declares is identified by *this person's membership*, not by
    the ids that membership currently resolves to, so declaring it twice in one
    transaction is idempotent even if the roster moved in between.  Declaring
    it after a single project, or for a second principal, is a scope change and
    is refused (#662).  Like ``open_project_partition`` it runs in a savepoint,
    so a refused declaration leaves the caller holding what it already had.
    """
    try:
        with session.begin_nested():
            scoped = session.scalar(
                select(func.open_member_project_partition(principal_subject))
            )
    except DBAPIError as error:
        if _sqlstate(error) == _SCOPE_CONFLICT:
            raise PartitionScopeConflict(
                "this transaction already declared a different "
                "project-authorization scope"
            ) from error
        raise PartitionRefused(
            "the cross-project partition was refused"
        ) from error
    return tuple(int(value) for value in (scoped or ()))


def close_project_partition(session: Session) -> None:
    """Give up the declared partition; what remains is the empty one.

    Giving up the *reading* is not giving up the transaction's declared scope.
    A closed partition can be reopened as the same scope, and cannot be
    reopened as another project's or another person's, because closing is a
    caller saying it is finished — not a caller acquiring permission to start
    somewhere else (#662).
    """
    session.execute(select(func.close_project_partition()))


def current_partition_declaration(session: Session) -> str | None:
    """The scope this transaction declared, as the database sealed it.

    ``None`` means nothing has been declared yet, which is every transaction's
    starting state including on a pooled connection: the declaration is
    transaction-local, so it reverts when the transaction ends.
    """
    return session.scalar(select(func.current_partition_declaration()))


def current_project_partition(session: Session) -> tuple[int, ...] | None:
    """The projects this connection may currently read, as the database sees it.

    ``None`` means no verified partition is declared at all, which reads
    exactly like the empty one; both are told apart only for diagnosis.
    """
    scoped = session.scalar(select(func.current_project_partition()))
    if scoped is None:
        return None
    return tuple(int(value) for value in scoped)
