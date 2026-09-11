"""The source bindings a project is authorized to take delivery on (#886).

``activation_runtime.require_source_delivery`` used to compare a delivery's
channel and configuration against the **single** ``source_channel`` /
``source_configuration`` pair on the activation configuration.  A deployment
whose front door is the per-project alias therefore refused a product upload
(#823) even though the delivery was valid and the database accepted it --
which is precisely the configuration a measured pilot needs, because #845
keeps at least one genuine project-connected channel and ADR-0058 keeps manual
upload as the explicit rare fallback.

**Manual upload is authorized, not excepted.**  The obvious repair --
``channel == product_upload or matches_activation`` -- was rejected.  Handing
Corridor a file is a delivery like any other; whether *this* project accepts
one is exactly as much an authorization question as whether it accepts a
connector's, and a rule that answers it by naming a channel in code cannot be
withdrawn, versioned, or attributed to anybody.  So the pair is replaced by a
recorded, versioned **set** of authorized source bindings, and the upload is a
member of it or it is not delivered.

**The shape is #827's, deliberately.**  The limited onboarding authorization
built one hour earlier is a per-project, immutable, versioned record written
only by a restricted operations actor, read through one ``SECURITY DEFINER``
standing function so a Python reader and the database can never disagree,
superseded by recording a higher version, and readable as history afterwards.
This module is that same arrangement for source bindings rather than a second
authorization format beside it: ``record_source_authorization`` mirrors
``record_onboarding_grant``, ``source_binding_standing`` mirrors
``onboarding_grant_standing``, the two ``governing_authorization_*`` columns
are the same two columns, and the refusal codes a version conflict raises are
the same two names.

**What this module is not.**  It is not the authorization.  The customer
authorization lives in the control plane (ADR-0083), and what crosses is
identifiers, versions and digests.  It is not the activation either: the
record says what is authorized *now*, and whether the deployment was activated
against that version is the activation receipt's question, which
``corridor.activation_runtime`` asks using the three values a standing
carries.

**Three endings, only one of which erases anything -- and it erases nothing.**
Narrowing the set withdraws one binding, recording an empty set withdraws them
all, and either way the predecessor row, its bindings, and every delivery
already recorded under them stay exactly as they were.  A withdrawal stops new
processing; it does not reach back into what a previous version admitted.
That is why there is no ``revoked`` column and no event relation: the
attributable act is the new version, carrying who issued it, who recorded it,
and when.

**No digest is computed here.**  ``binding_set_sha256`` is derived by the
command from the rows it writes, in the one canonical form stated in
``corridor.migrations.source_append_commands.source_authorization``, and this
module only ever reads it back.  A second encoding in Python is exactly what
``corridor.digests`` exists to prevent, and a digest computed beside the rows
rather than from them is the defect #457 closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.models import (
    AUTHENTICATION_MODES,
    ProjectSourceAuthorization,
    ProjectSourceAuthorizationBinding,
)

#: The three ways a delivery's transport can have authenticated it (#823).
CONNECTOR_CONFIGURATION = "connector_configuration"
MACHINE_CREDENTIAL = "machine_credential"
HUMAN_PRINCIPAL = "human_principal"

_REFUSAL_TOKEN = re.compile(r"\bsource_authorization:([a-z_]+)")


class SourceAuthorizationRefused(ValueError):
    """This authorized source set cannot be recorded as described.

    Deliberately not a ``refusals.Refusal``: recording the set is a restricted
    operations act performed against the customer database, not a request some
    web adapter has to turn into a status code for a person.  A delivery that
    is *refused* under a recorded set raises ``RouteRefused`` from the
    activation gate, which is where every other source refusal already lands.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


def refusal_from_database(exc: BaseException) -> SourceAuthorizationRefused | None:
    """The declared refusal the command raised, or ``None``.

    The token the command raises names its code, so the sentence table lives
    in plpgsql beside the rule and nothing here has to stay in step with it.
    """

    match = _REFUSAL_TOKEN.search(str(exc))
    if match is None:
        return None
    return SourceAuthorizationRefused(match.group(1), str(exc))


@dataclass(frozen=True, slots=True)
class AuthorizedSourceBinding:
    """One way a source may be delivered to this project, as authorized.

    The five facts the decision names: the channel, the configuration identity
    and version that carried the delivery, the source classes this binding
    permits, and the authentication mode the transport must have used.  Two
    bindings may share a channel -- two folders on one connector, two aliases
    into one project -- so the identity is the whole triple, not the channel.
    """

    channel: str
    configuration_identity: str
    permitted_source_classes: tuple[str, ...]
    authentication_mode: str
    configuration_version: str = ""

    def __post_init__(self) -> None:
        if self.authentication_mode not in AUTHENTICATION_MODES:
            raise SourceAuthorizationRefused(
                "malformed_binding",
                f"{self.authentication_mode!r} is not an authentication mode",
            )
        if not self.permitted_source_classes:
            raise SourceAuthorizationRefused(
                "malformed_binding",
                "an authorized source binding names the source classes it permits",
            )

    def as_recorded(self) -> dict[str, Any]:
        """What the command is handed. It canonicalizes and digests the set."""

        return {
            "channel": self.channel,
            "configuration_identity": self.configuration_identity,
            "configuration_version": self.configuration_version,
            "permitted_source_classes": list(self.permitted_source_classes),
            "authentication_mode": self.authentication_mode,
        }


@dataclass(frozen=True)
class RecordedSourceAuthorization:
    """One recorded version, whether this call wrote it or replayed it."""

    authorization_id: int
    created: bool
    binding_set_sha256: str


@dataclass(frozen=True)
class SourceBindingStanding:
    """What the database says about one delivery's own selection, right now."""

    permitted: bool
    reason: str = ""
    authorization_id: int | None = None
    authorization_identity: str = ""
    authorization_version: int = 0
    binding_set_sha256: str = ""
    governing_authorization_identity: str = ""
    governing_authorization_version: str = ""
    binding_id: int | None = None
    authentication_mode: str = ""
    permitted_source_classes: tuple[str, ...] = ()

    @property
    def recorded(self) -> bool:
        """Whether this project records an authorized source set at all."""

        return self.authorization_version > 0

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> "SourceBindingStanding":
        def identifier(key: str) -> int | None:
            value = payload.get(key)
            return int(value) if value is not None else None

        return cls(
            permitted=bool(payload.get("permitted")),
            reason=str(payload.get("reason") or ""),
            authorization_id=identifier("authorization_id"),
            authorization_identity=str(payload.get("authorization_identity") or ""),
            authorization_version=int(payload.get("authorization_version") or 0),
            binding_set_sha256=str(payload.get("binding_set_sha256") or ""),
            governing_authorization_identity=str(
                payload.get("governing_authorization_identity") or ""
            ),
            governing_authorization_version=str(
                payload.get("governing_authorization_version") or ""
            ),
            binding_id=identifier("binding_id"),
            authentication_mode=str(payload.get("authentication_mode") or ""),
            permitted_source_classes=tuple(
                payload.get("permitted_source_classes") or ()
            ),
        )


def authentication_mode_of(binding) -> str:
    """How the transport that carried this delivery authenticated it (#823).

    Read off the delivery binding rather than recorded beside it: a pulled
    delivery authenticates neither way because the connector configuration is
    the whole binding, and a pushed one names exactly one of the two things
    that can authenticate a push, which ``DeliveryBinding`` already enforces.
    """

    if getattr(binding, "transport", "") == "pull":
        return CONNECTOR_CONFIGURATION
    if getattr(binding, "credential_id", None) is not None:
        return MACHINE_CREDENTIAL
    return HUMAN_PRINCIPAL


def record_source_authorization(
    session: Session,
    *,
    project_id: int,
    authorization_identity: str,
    authorization_version: int,
    customer: str,
    environment: str,
    governing_authorization_identity: str,
    governing_authorization_version: str,
    bindings: Sequence[AuthorizedSourceBinding],
    issued_at: datetime,
    issued_by_actor: str,
    recorded_by_actor: str,
) -> RecordedSourceAuthorization:
    """Record one version of the authorized set. Operations capability only.

    The command canonicalizes the bindings, derives the version's digest from
    them, and refuses a version that goes backwards or one already bound to
    different terms.  Recording the same version with the same terms returns
    the version already recorded.
    """

    payload = _command(
        session,
        func.record_project_source_authorization(
            project_id,
            authorization_identity,
            authorization_version,
            customer,
            environment,
            governing_authorization_identity,
            governing_authorization_version,
            _jsonb([binding.as_recorded() for binding in bindings]),
            _aware(issued_at),
            issued_by_actor,
            recorded_by_actor,
        ),
    )
    return RecordedSourceAuthorization(
        authorization_id=int(payload["authorization_id"]),
        created=bool(payload.get("created")),
        binding_set_sha256=str(payload["binding_set_sha256"]),
    )


def source_binding_standing(
    session: Session,
    *,
    project_id: int,
    customer: str,
    channel: str,
    configuration_identity: str,
    configuration_version: str,
    authentication_mode: str,
) -> SourceBindingStanding:
    """Whether this exact selection may be delivered to this project now.

    Reads the database's own derivation, so nothing here can be more generous
    than the record.  It answers about the version *in force*; the caller
    compares the version it names against what the deployment was activated
    for.
    """

    payload = session.scalar(
        select(
            func.source_binding_standing(
                project_id,
                customer,
                channel,
                configuration_identity,
                configuration_version,
                authentication_mode,
            )
        )
    )
    return SourceBindingStanding.of(payload or {})


def authorization_in_force(
    session: Session, project_id: int
) -> ProjectSourceAuthorization | None:
    """The newest version recorded for this project, or ``None``."""

    return session.scalars(
        select(ProjectSourceAuthorization)
        .where(ProjectSourceAuthorization.project_id == project_id)
        .order_by(
            ProjectSourceAuthorization.authorization_version.desc(),
            ProjectSourceAuthorization.id.desc(),
        )
        .limit(1)
    ).first()


def recorded_authorizations(
    session: Session, project_id: int
) -> tuple[ProjectSourceAuthorization, ...]:
    """Every version recorded for this project, oldest first.

    What makes a withdrawal readable rather than erasing: the terms a delivery
    was taken under are still here after the version that replaced them.
    """

    return tuple(
        session.scalars(
            select(ProjectSourceAuthorization)
            .where(ProjectSourceAuthorization.project_id == project_id)
            .order_by(ProjectSourceAuthorization.authorization_version)
        ).all()
    )


def authorized_bindings(
    session: Session, authorization: ProjectSourceAuthorization
) -> tuple[AuthorizedSourceBinding, ...]:
    """The bindings one recorded version carries, in the digest's own order."""

    rows = session.scalars(
        select(ProjectSourceAuthorizationBinding)
        .where(ProjectSourceAuthorizationBinding.authorization_id == authorization.id)
        .order_by(
            ProjectSourceAuthorizationBinding.channel,
            ProjectSourceAuthorizationBinding.configuration_identity,
            ProjectSourceAuthorizationBinding.configuration_version,
        )
    ).all()
    return tuple(
        AuthorizedSourceBinding(
            channel=row.channel,
            configuration_identity=row.configuration_identity,
            configuration_version=row.configuration_version,
            permitted_source_classes=tuple(row.permitted_source_classes),
            authentication_mode=row.authentication_mode,
        )
        for row in rows
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SourceAuthorizationRefused(
            "naive_instant", "a source authorization instant carries its time zone"
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


def _jsonb(value: object):
    return cast(bindparam(None, json.dumps(value)), JSONB)
