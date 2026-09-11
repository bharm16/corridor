"""The limited onboarding authorization, as the customer environment holds it (#827).

ADR-0099 decides that everything which must happen before authoritative
activation runs under a **limited onboarding authorization**, that the control
plane is authoritative for it, and that a restricted operations or deployment
actor issues it from the recorded customer-data authorization. The control
plane is a different database (ADR-0083), so this module is not the
authorization: it is the **grant** the customer environment holds, the standing
that grant has right now, and the refusal a caller gets when that standing
cannot be established.

**Why the enforcement is in PostgreSQL and this module is thin.** The decision
is explicit that "the database adoption command must verify a server-trusted,
project-bound authorization, not accept a browser-supplied Boolean saying
onboarding is permitted". So `onboarding_grant_standing` is a SQL function,
`commit_onboarding_act` calls it inside the committing transaction, and the
functions here read the same one. A Python reader and the command can therefore
never disagree about whether onboarding writes are permitted, which is the same
arrangement `operating_mode` uses for the adopted-baseline mode.

**Three different endings, and they are not one sentence.** ADR-0099's
"no continuing onboarding authority after it expires, is withdrawn, or is
consumed" reads as one rule and is three:

- A **consumed adoption permission** ends permission for new adoption writes
  and nothing else. Every other permitted operation this grant names is still
  permitted on its own terms, and the completed preview, answers and receipt
  stay readable under the ordinary read authorization.
- **Expiry or withdrawal of the relevant permission** ends new protected
  onboarding writes under that permission. It does not reach back into what was
  already committed, and it does not decide anything about a different
  permission the same grant carries.
- **Authorized retrieval of an existing record** is neither. Reading the
  completed onboarding status, or returning the receipt an exact retry names,
  is retrieval of a prior result under current read access, not the exercise of
  authority that has ended.

**No offline grace.** ``REVALIDATION_WINDOW`` is the stated maximum
stale-validity window ADR-0099 requires #827 to name before customer
deployment. A positive validity result is good for that window and then has to
be established again; a grant whose validity cannot be established refuses.
Nothing here claims instantaneous cross-database revocation, and
``docs/operations/onboarding-authorization.md`` says so in the words operations
has to answer with.

**Custody, as grants rather than as a comment.** ``record_onboarding_grant``
and ``record_onboarding_grant_event`` are executable by the operations
capability alone; ``corridor_web`` holds no execute on either and no write on
any of the four relations. A project coordinator therefore cannot
self-authorize, and cannot extend, annotate or revive the permission they act
under, whatever their designation says about what they may do inside the
customer database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor import refusals
from corridor.models import OnboardingAct, OnboardingGrant, OnboardingGrantEvent


#: The closed set of operations a limited onboarding authorization may permit.
#: ADR-0099's "what it allows" list, one name per activity, plus the bounded
#: setup permission its consequences require for the step after adoption
#: (#828), so consuming the adoption permission cannot strand a coordinator
#: between "baseline adopted" and "project ready for activation".
REACH_PROJECT = "reach_project"
RECEIVE_SOURCE = "receive_source"
INSPECT_COMPATIBILITY = "inspect_compatibility"
PREPARE_MAPPING = "prepare_mapping"
REVIEW_BASELINE_QUESTIONS = "review_baseline_questions"
ADOPT_BASELINE = "adopt_baseline"
APPROVE_ISSUE_PROFILE = "approve_issue_profile"

ONBOARDING_OPERATIONS: tuple[str, ...] = (
    REACH_PROJECT,
    RECEIVE_SOURCE,
    INSPECT_COMPATIBILITY,
    PREPARE_MAPPING,
    REVIEW_BASELINE_QUESTIONS,
    ADOPT_BASELINE,
    APPROVE_ISSUE_PROFILE,
)

#: The operations that are themselves committing acts, so each leaves a
#: retained proof row and each happens at most once. Every other permitted
#: operation is preparatory and consumes nothing.
ONBOARDING_ACT_OPERATIONS: tuple[str, ...] = (ADOPT_BASELINE, APPROVE_ISSUE_PROFILE)

#: What can be recorded about a grant after it is issued.
ONBOARDING_EVENT_KINDS: tuple[str, ...] = (
    "revalidated",
    "withdrawal_requested",
    "withdrawal_enforced",
    "withdrawal_enforcement_failed",
    "governing_authorization_superseded",
)

#: The maximum stale-validity window, stated rather than left to whatever
#: propagation happens to get built. A protected onboarding act commits only
#: while a positive validity result is at most this old; past it the act
#: refuses until validity is established again. Fifteen minutes is short
#: enough that a withdrawal recorded in the control plane stops customer-side
#: onboarding writes inside one support interaction, and long enough that a
#: coordinator answering baseline questions is not revalidating every click.
#: It is an operation-and-verification window, not a claim that a withdrawal
#: reaches this database instantly.
REVALIDATION_WINDOW = timedelta(minutes=15)

#: What a coordinator may be told about a paused onboarding, and what they may
#: not: the sentence names this project's own state and never the
#: cross-customer registry or the customer's legal evidence.
WITHDRAWAL_REQUESTED_NOTICE = (
    "Withdrawal requested. Confirmation that processing has stopped is pending."
)
WITHDRAWAL_ENFORCED_NOTICE = "Onboarding writes are disabled for this project as of {at}."

_REFUSAL_TOKEN = re.compile(
    r"\bonboarding_(?:act|grant|preview):([a-z_]+)"
)

#: The refusal codes the commands raise, and the kind each answers as. A code
#: this table does not name is reported as a conflict rather than as something
#: the caller is told to fix.
_REFUSAL_KINDS: Mapping[str, str] = {
    "no_onboarding_authorization": refusals.NOT_AUTHORIZED,
    "onboarding_authorization_withdrawn": refusals.NOT_AUTHORIZED,
    "onboarding_authorization_expired": refusals.NOT_AUTHORIZED,
    "onboarding_authorization_not_yet_in_force": refusals.NOT_AUTHORIZED,
    "onboarding_authorization_revalidation_required": refusals.NOT_AUTHORIZED,
    "governing_authorization_superseded": refusals.NOT_AUTHORIZED,
    "conflicting_reuse": refusals.MALFORMED_INPUT,
    "missing_request_key": refusals.MALFORMED_INPUT,
    "already_performed": refusals.CONFLICT,
    "already_adopted": refusals.CONFLICT,
    "preview_not_retained": refusals.STALE,
    "preview_not_adoptable": refusals.NOT_OFFERED,
    "operations_unresolved": refusals.NOT_OFFERED,
    "blocking_question_unresolved": refusals.NOT_OFFERED,
    "version_bound_to_other_terms": refusals.CONFLICT,
    "version_went_backwards": refusals.CONFLICT,
    "unknown_grant": refusals.MALFORMED_INPUT,
}

#: The sentence a person reads for each refusal. The commands' own messages
#: name the machine code and a technical reason; these are what a screen says.
_REFUSAL_SENTENCES: Mapping[str, str] = {
    "no_onboarding_authorization": (
        "Corridor operations has not recorded an onboarding authorization for "
        "this project, so onboarding work cannot start here yet."
    ),
    "onboarding_authorization_withdrawn": (
        "Onboarding for this project is paused. Corridor operations can say "
        "what happens next."
    ),
    "onboarding_authorization_expired": (
        "The onboarding authorization for this project has run out. Corridor "
        "operations can reissue it."
    ),
    "onboarding_authorization_not_yet_in_force": (
        "The onboarding authorization for this project does not start yet."
    ),
    "onboarding_authorization_revalidation_required": (
        "Corridor could not confirm just now that onboarding for this project "
        "is still permitted, so it stopped rather than continue on an old "
        "answer. Try again shortly."
    ),
    "governing_authorization_superseded": (
        "The customer authorization this onboarding permission was issued "
        "under has been replaced, so Corridor operations has to confirm the "
        "permission again before further onboarding work."
    ),
    "conflicting_reuse": (
        "This submission reuses an earlier request's identifier with different "
        "content. Reload the page and submit it again."
    ),
    "missing_request_key": "This form was not the one the page rendered.",
    "already_performed": (
        "This project has already completed that step. Changing it is a later "
        "supported act, not another one of these."
    ),
    "already_adopted": (
        "This project has already adopted a baseline. Replacing it is a later "
        "record change, not another initial adoption."
    ),
    "preview_not_retained": (
        "This adoption no longer matches the reading you were shown. Prepare "
        "the baseline reading again before adopting it."
    ),
    "preview_not_adoptable": (
        "That reading was prepared for checking, not as a baseline this "
        "project can adopt."
    ),
    "operations_unresolved": (
        "Corridor operations has not resolved this workbook yet."
    ),
    "blocking_question_unresolved": (
        "A question that has to be decided before adoption is still open."
    ),
    "version_bound_to_other_terms": (
        "That authorization version is already recorded with different terms."
    ),
    "version_went_backwards": (
        "A later version of that authorization is already recorded."
    ),
    "unknown_grant": "This project holds no such onboarding authorization.",
}


class OnboardingRefused(refusals.Refusal, ValueError):
    """This onboarding act is not permitted, and nothing was written.

    Its own type because the answer differs by audience: operations needs the
    code, and the person on the screen needs a sentence that names neither the
    cross-customer registry nor the customer's legal evidence.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(_REFUSAL_SENTENCES.get(code, detail or code))

    @property
    def refusal_kind(self) -> str:  # type: ignore[override]
        return _REFUSAL_KINDS.get(self.code, refusals.CONFLICT)


def refusal_from_database(exc: BaseException) -> OnboardingRefused | None:
    """The declared refusal one of these commands raised, or ``None``.

    The token the command raises names its code, so one reader serves all
    five commands and no sentence table has to stay in step with plpgsql.
    """

    match = _REFUSAL_TOKEN.search(str(exc))
    if match is None:
        return None
    return OnboardingRefused(match.group(1), str(exc))


@dataclass(frozen=True)
class OnboardingStanding:
    """What the database says about one permitted operation, right now."""

    permitted: bool
    reason: str
    grant_id: int | None = None
    authorization_id: str = ""
    grant_version: int = 0
    operation: str = ""
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    validated_at: datetime | None = None
    governing_authorization_identity: str = ""
    governing_authorization_version: str = ""
    evidence_identity: str = ""
    evidence_sha256: str = ""
    source_scope: str = ""

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> "OnboardingStanding":
        def when(key: str) -> datetime | None:
            value = payload.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            permitted=bool(payload.get("permitted")),
            reason=str(payload.get("reason") or ""),
            grant_id=(
                int(payload["grant_id"]) if payload.get("grant_id") is not None else None
            ),
            authorization_id=str(payload.get("authorization_id") or ""),
            grant_version=int(payload.get("grant_version") or 0),
            operation=str(payload.get("operation") or ""),
            issued_at=when("issued_at"),
            expires_at=when("expires_at"),
            validated_at=when("validated_at"),
            governing_authorization_identity=str(
                payload.get("governing_authorization_identity") or ""
            ),
            governing_authorization_version=str(
                payload.get("governing_authorization_version") or ""
            ),
            evidence_identity=str(payload.get("evidence_identity") or ""),
            evidence_sha256=str(payload.get("evidence_sha256") or ""),
            source_scope=str(payload.get("source_scope") or ""),
        )


def onboarding_standing(
    session: Session, *, project_id: int, operation: str, at: datetime
) -> OnboardingStanding:
    """Whether this project may perform that onboarding operation right now.

    Reads the database's own derivation, so nothing here can be more generous
    than the command that will refuse.
    """

    if operation not in ONBOARDING_OPERATIONS:
        raise OnboardingRefused("unknown_operation", f"unknown operation {operation!r}")
    payload = session.scalar(
        select(
            func.onboarding_grant_standing(
                project_id,
                operation,
                _aware(at),
                int(REVALIDATION_WINDOW.total_seconds()),
            )
        )
    )
    return OnboardingStanding.of(payload or {})


def require_onboarding_permission(
    session: Session, *, project_id: int, operation: str, at: datetime
) -> OnboardingStanding:
    """The standing, or the refusal it names. Fails closed on every reason."""

    standing = onboarding_standing(
        session, project_id=project_id, operation=operation, at=at
    )
    if not standing.permitted:
        raise OnboardingRefused(standing.reason or "no_onboarding_authorization")
    return standing


def record_onboarding_grant(
    session: Session,
    *,
    project_id: int,
    authorization_id: str,
    grant_version: int,
    customer: str,
    environment: str,
    permitted_operations: Sequence[str],
    source_scope: str,
    governing_authorization_identity: str,
    governing_authorization_version: str,
    evidence_identity: str,
    evidence_sha256: str,
    issued_at: datetime,
    expires_at: datetime,
    issued_by_actor: str,
    recorded_by_actor: str,
) -> int:
    """Record here what the control plane issued. Operations capability only."""

    unknown = sorted(set(permitted_operations) - set(ONBOARDING_OPERATIONS))
    if unknown:
        raise OnboardingRefused(
            "unknown_operation", f"unknown permitted operations: {', '.join(unknown)}"
        )
    outcome = _command(
        session,
        func.record_onboarding_grant(
            project_id,
            authorization_id,
            grant_version,
            customer,
            environment,
            list(permitted_operations),
            source_scope,
            governing_authorization_identity,
            governing_authorization_version,
            evidence_identity,
            evidence_sha256,
            _aware(issued_at),
            _aware(expires_at),
            issued_by_actor,
            recorded_by_actor,
        ),
    )
    return int(outcome["grant_id"])


def record_onboarding_event(
    session: Session,
    *,
    project_id: int,
    grant_id: int,
    kind: str,
    executed_by_actor: str,
    executed_at: datetime,
    requested_by: str | None = None,
    requested_at: datetime | None = None,
    reason: str | None = None,
    detail: str | None = None,
) -> int:
    """Record what happened to a grant. Operations capability only."""

    if kind not in ONBOARDING_EVENT_KINDS:
        raise OnboardingRefused("unknown_event", f"unknown event kind {kind!r}")
    outcome = _command(
        session,
        func.record_onboarding_grant_event(
            project_id,
            grant_id,
            kind,
            requested_by,
            _aware(requested_at) if requested_at else None,
            executed_by_actor,
            _aware(executed_at),
            reason,
            detail,
        ),
    )
    return int(outcome["event_id"])


def held_grant(session: Session, project_id: int) -> OnboardingGrant | None:
    """The newest grant recorded for this project, whatever its standing."""

    return session.scalars(
        select(OnboardingGrant)
        .where(OnboardingGrant.project_id == project_id)
        .order_by(OnboardingGrant.grant_version.desc(), OnboardingGrant.id.desc())
        .limit(1)
    ).first()


def grant_events(session: Session, project_id: int) -> tuple[OnboardingGrantEvent, ...]:
    """Everything recorded about this project's grants, oldest first."""

    return tuple(
        session.scalars(
            select(OnboardingGrantEvent)
            .where(OnboardingGrantEvent.project_id == project_id)
            .order_by(OnboardingGrantEvent.id)
        ).all()
    )


def completed_act(
    session: Session, *, project_id: int, operation: str
) -> OnboardingAct | None:
    """The retained proof of one completed onboarding act, if it happened."""

    return session.scalars(
        select(OnboardingAct)
        .where(
            OnboardingAct.project_id == project_id,
            OnboardingAct.operation == operation,
        )
        .limit(1)
    ).first()


@dataclass(frozen=True)
class WithdrawalRecord:
    """One withdrawal, told three ways because three audiences need different halves.

    ADR-0099 insists the effective state and the enforcement state are
    different facts and that a customer's request must not be described as
    fully enforced while the customer database can still exercise the grant.
    So ``requested_at`` and ``enforced_at`` are separate, and
    ``coordinator_notice`` says "pending" until the second one exists.
    """

    requested: bool
    requested_by: str
    requested_at: datetime | None
    request_reason: str
    executed_by_actor: str
    enforced_at: datetime | None
    outstanding_failure: str
    governing_authorization_identity: str
    governing_authorization_version: str

    @property
    def enforced(self) -> bool:
        return self.enforced_at is not None

    @property
    def coordinator_notice(self) -> str:
        """What the project coordinator reads. No registry, no legal evidence."""

        if self.enforced_at is not None:
            return WITHDRAWAL_ENFORCED_NOTICE.format(
                at=self.enforced_at.astimezone(timezone.utc).isoformat()
            )
        return WITHDRAWAL_REQUESTED_NOTICE

    @property
    def customer_acknowledgement(self) -> str:
        """The accurate acknowledgement the verified requester is owed.

        An attributable operations communication is enough for the pilot, so
        this is the sentence operations sends rather than a new portal.
        """

        if self.enforced_at is None:
            return (
                "Your withdrawal request was received"
                + (
                    f" on {self.requested_at.astimezone(timezone.utc).isoformat()}"
                    if self.requested_at
                    else ""
                )
                + ". Corridor has stopped starting new onboarding work under "
                "this permission. Confirmation that processing has stopped in "
                "the customer environment is pending."
            )
        return (
            "Your withdrawal request was received"
            + (
                f" on {self.requested_at.astimezone(timezone.utc).isoformat()}"
                if self.requested_at
                else ""
            )
            + ". Onboarding writes in the customer environment are disabled as "
            f"of {self.enforced_at.astimezone(timezone.utc).isoformat()}."
        )

    def operations_record(self) -> dict[str, Any]:
        """The full record operations answers with."""

        return {
            "requested": self.requested,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "reason": self.request_reason,
            "executed_by": self.executed_by_actor,
            "enforced_at": self.enforced_at,
            "enforcement_state": "enforced" if self.enforced else "pending",
            "outstanding_failure": self.outstanding_failure,
            "governing_authorization_identity": self.governing_authorization_identity,
            "governing_authorization_version": self.governing_authorization_version,
        }


def withdrawal_record(session: Session, project_id: int) -> WithdrawalRecord | None:
    """This project's withdrawal, or ``None`` where none was requested."""

    grant = held_grant(session, project_id)
    if grant is None:
        return None
    events = [
        event for event in grant_events(session, project_id) if event.grant_id == grant.id
    ]
    requested = next(
        (event for event in reversed(events) if event.kind == "withdrawal_requested"),
        None,
    )
    enforced = next(
        (event for event in reversed(events) if event.kind == "withdrawal_enforced"),
        None,
    )
    failed = next(
        (
            event
            for event in reversed(events)
            if event.kind == "withdrawal_enforcement_failed"
        ),
        None,
    )
    if requested is None and enforced is None:
        return None
    source = requested or enforced
    return WithdrawalRecord(
        requested=requested is not None,
        requested_by=(requested.requested_by or "") if requested else "",
        requested_at=requested.requested_at if requested else None,
        request_reason=(requested.reason or "") if requested else "",
        executed_by_actor=(enforced or source).executed_by_actor,
        enforced_at=enforced.executed_at if enforced is not None else None,
        outstanding_failure=(
            failed.detail or ""
            if failed is not None
            and (enforced is None or failed.id > enforced.id)
            else ""
        ),
        governing_authorization_identity=grant.governing_authorization_identity,
        governing_authorization_version=grant.governing_authorization_version,
    )


def permitted_operations_now(
    session: Session, *, project_id: int, at: datetime
) -> tuple[str, ...]:
    """Which onboarding operations this project may perform at this instant.

    What the coordinator's screen prints beside a paused onboarding: which
    operations remain available, answered by the same derivation that would
    refuse them, rather than by reading the grant's own list and hoping.
    """

    return tuple(
        operation
        for operation in ONBOARDING_OPERATIONS
        if onboarding_standing(
            session, project_id=project_id, operation=operation, at=at
        ).permitted
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OnboardingRefused(
            "naive_instant", "an onboarding instant must carry its time zone"
        )
    return value.astimezone(timezone.utc)


def _command(session: Session, expression) -> Mapping[str, Any]:
    try:
        return session.scalar(select(expression))
    except DBAPIError as exc:
        refusal = refusal_from_database(exc)
        if refusal is None:
            raise
        raise refusal from exc


def retain_preview(
    session: Session,
    *,
    project_id: int,
    source_sha256: str,
    filename: str,
    source_identity: str,
    mapping_identity: str,
    mapping_version: str,
    binding_fingerprint: str,
    payload: Mapping[str, Any],
    operations_resolved: bool,
    blocking_question_count: int,
    prepared_by_actor: str,
    prepared_at: datetime,
) -> Mapping[str, Any]:
    """Retain one prepared reading, under the compatibility permission.

    The command proves that permission itself and decides adoptability from
    ``project_operating_mode``, so the caller cannot claim either. Retaining
    the same fingerprint twice returns the row already retained.
    """

    return _command(
        session,
        func.retain_onboarding_preview(
            project_id,
            source_sha256,
            filename,
            source_identity,
            mapping_identity,
            mapping_version,
            binding_fingerprint,
            _jsonb(dict(payload)),
            operations_resolved,
            blocking_question_count,
            prepared_by_actor,
            _aware(prepared_at),
            int(REVALIDATION_WINDOW.total_seconds()),
        ),
    )


@dataclass(frozen=True)
class ActCheck:
    """What committing this act would do, before any of its work is done."""

    replay: bool
    act_id: int | None = None
    result: Mapping[str, Any] | None = None
    committed_at: datetime | None = None
    standing: OnboardingStanding | None = None


@dataclass(frozen=True)
class ActOutcome:
    """The committed act, or the one already committed under the same key."""

    act_id: int
    created: bool
    result: Mapping[str, Any]


def canonical_material_digest(payload: Mapping[str, Any]) -> str:
    """The canonical binding of one submission's material content.

    ADR-0099's five database identities are payload and authority checks. They
    do not identify *this* submission, so a second submission under the same
    key with different content would otherwise read as a retry. This digest is
    what makes the difference sayable: the material a person submitted, in a
    canonical form, and nothing incidental. Fresh cookies and a fresh
    request-forgery token are not adoption content and are not in it.
    """

    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def check_onboarding_act(
    session: Session,
    *,
    project_id: int,
    operation: str,
    request_key: str,
    material_sha256: str,
    at: datetime,
    preview_fingerprint: str = "",
    answers: Sequence[Mapping[str, Any]] = (),
) -> ActCheck:
    """Ask what committing would do, before doing the work it needs.

    A courtesy, never the authority: ``commit_onboarding_act`` runs every one
    of these checks again inside the committing transaction.
    """

    payload = _command(
        session,
        func.check_onboarding_act(
            project_id,
            operation,
            request_key,
            material_sha256,
            _aware(at),
            preview_fingerprint,
            _jsonb([dict(item) for item in answers]),
            int(REVALIDATION_WINDOW.total_seconds()),
        ),
    )
    if payload.get("replay"):
        committed = payload.get("committed_at")
        return ActCheck(
            replay=True,
            act_id=int(payload["act_id"]),
            result=payload.get("result") or {},
            committed_at=datetime.fromisoformat(committed) if committed else None,
        )
    return ActCheck(
        replay=False, standing=OnboardingStanding.of(payload.get("standing") or {})
    )


def commit_onboarding_act(
    session: Session,
    *,
    project_id: int,
    operation: str,
    request_key: str,
    material_sha256: str,
    principal: str,
    at: datetime,
    result: Mapping[str, Any],
    preview_fingerprint: str = "",
    answers: Sequence[Mapping[str, Any]] = (),
) -> ActOutcome:
    """Consume the permission for this act, atomically, with its retained proof.

    Called in the same transaction as the writes it attributes, and last, so
    the retained ``result`` is the receipt that was actually written. A
    concurrent identical submission loses the relation's unique and converges
    on the committed one rather than adopting twice.
    """

    payload = _command(
        session,
        func.commit_onboarding_act(
            project_id,
            operation,
            request_key,
            material_sha256,
            principal,
            _aware(at),
            preview_fingerprint,
            _jsonb([dict(item) for item in answers]),
            int(REVALIDATION_WINDOW.total_seconds()),
            _jsonb(dict(result)),
        ),
    )
    return ActOutcome(
        act_id=int(payload["act_id"]),
        created=bool(payload.get("created")),
        result=payload.get("result") or {},
    )


def _jsonb(value: object):
    return cast(bindparam(None, json.dumps(value, default=str)), JSONB)
