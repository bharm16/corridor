"""One request that this project's next issue be prepared, and what came of it.

#529 prepares an immutable release candidate in three transactions and renders
*between* them, on a session factory, because the customer's own workbook is
produced by a renderer that reads and writes in one call. Running that inside
an HTTP request would hold a web worker for the length of a workbook render,
would be ambiguous to retry, and would put a long render in the same place as
the short act that asked for it. So the Issue section's one action records a
request and returns; this module is the record of that request and the
status derived from it, and ``release_preparation_worker`` is what runs it.

**Status is derived, and there is no close object.** Neither relation carries
a status column. ``preparation_standing`` answers "what is happening to this
project's next issue" from the append-only records alone: the newest request,
and the newest finished attempt at it. ADR-0085 refused a stored packet
lifecycle, #536 refused a stored reporting close, and #537 refused a stored
cross-project queue; a weekly-close row here would be the fourth attempt at the
same mistake, and it would be stale the moment a source arrived.

**An attempt is recorded when it finishes.** There is no ``running`` row to
claim and then complete, because completing one would need an UPDATE and every
release relation refuses one. The consequence is deliberate and worth stating:
a request whose worker died is indistinguishable from one still running, and
both read as *preparing*. That is the honest reading — Corridor does not know
which. **A coordinator cannot clear it**: the Issue section offers no act while
a request is in flight, deliberately, so that refreshing the page cannot queue a
second preparation of the same issue, and nothing else appends the attempt that
would end the wait. An abandoned request therefore holds the section until an
operator records one, and that gap is open work rather than a decided design.

**Three outcomes, kept apart, and no partial candidate.** #529 already
guarantees that: a refusal writes no candidate row and no artifact row, and a
blocked candidate is complete. This module records which of the three happened
and nothing else, and the database refuses a row that names a candidate and a
failure reason together.

**No clock.** Every instant — the request time, the attempt's start and finish,
the source cutoff — is supplied by the caller. Nothing here reads the wall
clock.

Terminology: nothing here coins a customer word. ``Release candidate`` stays
the internal technical name ADR-0086 put it in, and the action a coordinator
sees is the maintainer's own **Confirm coverage and prepare issue**.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import refusals
from corridor.models import (
    IssueCoverageDeclaration,
    ProjectRecordRevision,
    ReleasePreparationAttempt,
    ReleasePreparationRequest,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.analytics import EventFamily
from corridor.measurement_collection import emit_preparation_interaction


# What a project's next issue is doing, derived from the two append-only
# relations and from nothing else. These are internal identifiers; the words a
# screen prints live in the Issue section.
NOT_REQUESTED = "not_requested"
PREPARING = "preparing"
PREPARED = "prepared"
FAILED = "failed"

PREPARED_OUTCOME = "prepared"
REFUSED_OUTCOME = "refused"
FAILED_OUTCOME = "failed"


class PreparationRequestRefused(refusals.Refusal, ValueError):
    """This request would ask for an issue nobody confirmed the coverage of."""

    refusal_kind = refusals.CONFLICT


@dataclass(frozen=True, slots=True)
class PreparationStanding:
    """What one project's next issue is doing, and what proves it.

    ``state`` is one of the four above. It is not a portfolio state and never
    becomes one: a project being prepared asks nothing of a coordinator, so the
    portfolio's five primary states are untouched and a failure joins the
    current-issue technical blockers a project already shows.
    """

    project_id: int
    state: str
    request_id: int | None = None
    requested_at: datetime | None = None
    requested_by_principal: str | None = None
    attempt_id: int | None = None
    candidate_id: int | None = None
    refusal_code: str | None = None
    reason: str | None = None

    @property
    def in_flight(self) -> bool:
        """Whether a worker owes this project a candidate, or may still."""

        return self.state == PREPARING

    @property
    def failed(self) -> bool:
        return self.state == FAILED


# --- asking ------------------------------------------------------------------


def request_preparation(
    session: Session,
    *,
    project_id: int,
    declaration: IssueCoverageDeclaration,
    accepted_revision_id: int,
    requested_by: HumanPrincipal,
    requested_at: datetime,
    idempotency_key: str,
) -> ReleasePreparationRequest:
    """Append one request to prepare this project's next issue.

    Every input is revalidated against the confirmed declaration rather than
    trusted from the caller: the profile version and the cutoff must be the
    ones the coverage was confirmed under, and the accepted revision must be
    the project's current one. A request that named a different profile, a
    different cutoff, or a revision that had moved would ask a worker to
    prepare an issue under coverage nobody confirmed for it, which is the whole
    thing this ticket exists to stop.

    Idempotent by ``idempotency_key``: a resubmitted form, a retried POST, or a
    second click converges on the request already recorded.
    """

    actor = require_human_principal(requested_by)
    if requested_at.tzinfo is None:
        raise PreparationRequestRefused(
            "a preparation is requested at a declared, time-zone-aware "
            "instant; nothing here reads a clock"
        )
    key = (idempotency_key or "").strip()
    if not key:
        raise PreparationRequestRefused(
            "a preparation request needs an idempotency key"
        )
    if int(declaration.project_id) != int(project_id):
        raise PreparationRequestRefused(
            "the confirmed coverage declaration belongs to another project"
        )
    newest = int(
        session.scalar(
            select(func.max(ProjectRecordRevision.id)).where(
                ProjectRecordRevision.project_id == project_id
            )
        )
        or 0
    )
    if newest != int(accepted_revision_id):
        raise PreparationRequestRefused(
            f"the accepted record moved to revision {newest} after this issue "
            f"was reviewed at revision {accepted_revision_id}; read the week "
            "again and confirm the coverage that goes with it"
        )

    existing = session.scalars(
        select(ReleasePreparationRequest).where(
            ReleasePreparationRequest.project_id == project_id,
            ReleasePreparationRequest.idempotency_key == key[:160],
        )
    ).first()
    if existing is not None:
        emit_preparation_interaction(session, EventFamily.PREPARATION_REQUEST, existing,
                                     at=requested_at, principal_subject=actor.subject,
                                     request_id=existing.id, coverage_declaration_id=declaration.id,
                                     outcome="replayed")
        return existing

    row = ReleasePreparationRequest(
        project_id=int(project_id),
        accepted_revision_id=int(accepted_revision_id),
        issue_profile_id=int(declaration.issue_profile_id),
        issue_profile_identity=declaration.issue_profile_identity,
        issue_profile_version=int(declaration.issue_profile_version),
        coverage_declaration_id=int(declaration.id),
        source_cutoff=declaration.cutoff_at,
        requested_by_principal=actor.subject,
        requested_at=requested_at,
        idempotency_key=key[:160],
    )
    session.add(row)
    session.flush()
    emit_preparation_interaction(session, EventFamily.PREPARATION_REQUEST, row,
                                 at=requested_at, principal_subject=actor.subject,
                                 request_id=row.id, coverage_declaration_id=declaration.id,
                                 outcome="requested")
    return row


def preparation_idempotency_key(
    session: Session,
    *,
    project_id: int,
    declaration: IssueCoverageDeclaration,
) -> str:
    """The key one confirmation of one reading converges on, until it finishes.

    The digest of the confirmed declaration alone is not enough, and the
    difference is the whole of what a retry means. ``issue_readiness`` tells a
    coordinator whose preparation produced nothing that "asking for it again is
    what puts it right", and a key that named only the declaration made that
    sentence untrue: the retry would converge on the request that had already
    failed, nothing would be appended, no worker would pick anything up, and
    the project would be stuck at that cutoff for as long as its inputs did not
    happen to move. A transient failure — a lost database, template bytes a
    resolver could not retrieve — is exactly the case where the inputs do not
    move.

    So the key is the confirmed reading *and* the attempt this project has
    already finished. Within one preparation it does not change, so a
    resubmitted form, a retried POST and a second click all converge on the one
    request; once an attempt has finished, it does change, so asking again is a
    second request a worker can actually run.

    The boundary is the newest finished attempt for the **project**, not the
    newest for the request the standing happens to name: while a second request
    is in flight the standing carries no attempt at all, and a key derived from
    that would append a third request on every refresh.
    """

    latest = session.scalar(
        select(func.max(ReleasePreparationAttempt.id)).where(
            ReleasePreparationAttempt.project_id == int(project_id)
        )
    )
    return f"prepare:{declaration.declaration_digest}:after:{int(latest or 0)}"


# --- what a worker has left to do --------------------------------------------


def pending_request_ids(
    session: Session, *, project_id: int | None = None, limit: int = 50
) -> tuple[int, ...]:
    """Every request no attempt has finished, oldest first.

    Ordered by the append-only identifier rather than by ``requested_at``: the
    request instant is the caller's declaration, so ordering by it would let a
    later request be worked before an earlier one that declared an earlier
    time (#634).
    """

    finished = select(ReleasePreparationAttempt.request_id).where(
        ReleasePreparationAttempt.request_id == ReleasePreparationRequest.id
    )
    statement = (
        select(ReleasePreparationRequest.id)
        .where(~finished.exists())
        .order_by(ReleasePreparationRequest.id)
        .limit(limit)
    )
    if project_id is not None:
        statement = statement.where(
            ReleasePreparationRequest.project_id == project_id
        )
    return tuple(int(value) for value in session.scalars(statement).all())


# --- reading it back ---------------------------------------------------------


def preparation_standing(
    session: Session, *, project_id: int
) -> PreparationStanding:
    """What this project's next issue is doing, derived from the records."""

    return preparation_standings(session, project_ids=(project_id,))[project_id]


def preparation_standings(
    session: Session, *, project_ids: Sequence[int]
) -> dict[int, PreparationStanding]:
    """``preparation_standing`` for several projects, in two statements.

    The cross-project reading (#537, #636) shows many projects at once and may
    not ask this one project at a time, and it may not answer it by a second
    rule either — so the single-project reader above is this one over one
    project.
    """

    ids = tuple(dict.fromkeys(int(value) for value in project_ids))
    found = {
        project_id: PreparationStanding(project_id=project_id, state=NOT_REQUESTED)
        for project_id in ids
    }
    if not ids:
        return found
    newest = (
        select(
            ReleasePreparationRequest.project_id.label("project_id"),
            func.max(ReleasePreparationRequest.id).label("request_id"),
        )
        .where(ReleasePreparationRequest.project_id.in_(ids))
        .group_by(ReleasePreparationRequest.project_id)
        .subquery()
    )
    requests = {
        int(row.project_id): row
        for row in session.scalars(
            select(ReleasePreparationRequest).join(
                newest, ReleasePreparationRequest.id == newest.c.request_id
            )
        ).all()
    }
    if not requests:
        return found
    request_ids = tuple(int(row.id) for row in requests.values())
    latest_attempt = (
        select(
            ReleasePreparationAttempt.request_id.label("request_id"),
            func.max(ReleasePreparationAttempt.id).label("attempt_id"),
        )
        .where(ReleasePreparationAttempt.request_id.in_(request_ids))
        .group_by(ReleasePreparationAttempt.request_id)
        .subquery()
    )
    attempts = {
        int(row.request_id): row
        for row in session.scalars(
            select(ReleasePreparationAttempt).join(
                latest_attempt,
                ReleasePreparationAttempt.id == latest_attempt.c.attempt_id,
            )
        ).all()
    }
    for project_id, request in requests.items():
        attempt = attempts.get(int(request.id))
        if attempt is None:
            found[project_id] = PreparationStanding(
                project_id=project_id,
                state=PREPARING,
                request_id=int(request.id),
                requested_at=request.requested_at,
                requested_by_principal=request.requested_by_principal,
            )
            continue
        found[project_id] = PreparationStanding(
            project_id=project_id,
            state=PREPARED if attempt.outcome == PREPARED_OUTCOME else FAILED,
            request_id=int(request.id),
            requested_at=request.requested_at,
            requested_by_principal=request.requested_by_principal,
            attempt_id=int(attempt.id),
            candidate_id=(
                None if attempt.candidate_id is None else int(attempt.candidate_id)
            ),
            refusal_code=attempt.refusal_code,
            reason=attempt.reason,
        )
    return found
