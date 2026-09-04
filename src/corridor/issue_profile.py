"""What one project externally issues, and the reading every consumer uses (#640).

ADR-0091 amended ADR-0086 so that the externally issued set is **per-project
configuration** with only the updated native UCM mandatory, and recorded in the
same breath that the configuration "is not modelled". Nothing implemented it,
and the gap was load-bearing: #536 shipped ADR-0085's three visible consequence
levels deliberately underived, because a level is a projection onto *the next
issue's declared content* and there was no declared content to project onto,
and #529 still carried the fixed four-artifact list ADR-0091 had retired. This
module is that configuration and the one read seam over it.

**Two layers, and there is nowhere to put a third.** A project selects artifact
types and the renderer revision each is produced by; what a given renderer
revision contains — which accepted fields, alerts, follow-up content and
disclosures — is that renderer's own declaration, not a per-customer choice.
The effective inventory is the combination. There is no per-field column here,
because an inventory assembled field by field per customer is a report builder
rather than a configured issue set.

**The mandatory member is a column, not a row.** ADR-0091's updated UCM is the
one artifact with no configuration switch, so it lives on the profile itself
and ``project_issue_profile_artifacts`` holds only what ADR-0091 made a choice.
A profile with no UCM and a profile with two UCM entries are therefore states
PostgreSQL cannot represent, not states this module refuses.

**History is a chain, and the chain is the timing rule.** Every version names
its predecessor by that predecessor's whole identity, must be exactly one
version higher, and must take effect **strictly later**. Strictly, not
at-or-later: equality would leave one instant whose configured answer could
still be rewritten, and the rule this implements is that a caller must never be
able to retroactively change what an earlier reporting cutoff was configured to
issue. Later-effective versions are permitted because they stay monotonic; no
version of this ticket needs one, and none is manufactured.

**Who configures, and who does not.** A person holding the Project Coordination
designation registers or changes the profile. The external releaser
deliberately holds no authority here — that person authorizes a *prepared*
package (#533) — because release approval turning into package design is
exactly the conflation ADR-0086 kept apart when it made participation
configuration rather than a weekly editorial act.

**Staleness is derived, never pushed.** A profile change does not reach into a
prepared release candidate and edit it; #529 does not exist yet, and a hook
into a module nobody has written would be a guess. What exists instead is the
reading: a candidate records the profile row and version it was prepared
against, ``effective_issue_inventory`` says what is configured at a cutoff, and
``prepared_candidate_is_stale`` compares the two. The candidate keeps claiming
exactly what it claimed.

**No clock.** ``effective_from`` and ``cutoff`` are supplied by the caller.
This module never reads the wall clock, so a reading of a past cutoff is the
same reading tomorrow.

Terminology: ``Issue Profile`` is an internal technical name, in the same class
as ``ReleasePackage`` and ``Review Packet``. No customer-facing label is coined
here, so `docs/agents/domain.md`'s terminology-research procedure is not
triggered; the customer sees their own issue in project language.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.access import COORDINATION, resolve_membership
from corridor.baseline_adoption import FormatIdentity
from corridor.models import (
    CONFIGURED_ARTIFACT_TYPES,
    IssueProfile,
    IssueProfileArtifact,
)
from corridor.principals import HumanPrincipal, require_human_principal


# The declaration's own shape, apart from any one project's configuration. A
# declaration written under a later schema records different things, so the
# version travels inside the digested bytes.
DECLARATION_SCHEMA_VERSION = "issue-profile-v1"

# ADR-0091's mandatory member. It is not a configurable artifact type, and the
# database will not accept it as one.
UPDATED_UCM = "updated_ucm"


class IssueProfileRefused(ValueError):
    """A caller may not register this issue profile."""


@dataclass(frozen=True, slots=True)
class RendererRevision:
    """Which versioned renderer produces one artifact.

    The renderer revision is the second layer: it declares what the artifact
    contains. This names it; it does not restate it.
    """

    identity: str
    version: str

    def as_payload(self) -> dict[str, str]:
        return {"renderer_identity": self.identity, "renderer_version": self.version}


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """One artifact in a project's issued set, and the renderer producing it."""

    artifact_type: str
    renderer: RendererRevision


@dataclass(frozen=True, slots=True)
class CoverageRequirement:
    """One configured source-coverage requirement for an issue (ADR-0086)."""

    requirement: str
    statement: str

    def as_payload(self) -> dict[str, str]:
        return {"requirement": self.requirement, "statement": self.statement}


@dataclass(frozen=True, slots=True)
class DecisionBlockingPolicy:
    """One customer rule requiring a named decision before authorization.

    ADR-0086 lists "explicit customer policy" among the four things that may
    block a release. This is that policy, stated by the project rather than
    inferred, and it names the decision it waits on.
    """

    policy: str
    required_decision: str
    statement: str

    def as_payload(self) -> dict[str, str]:
        return {
            "policy": self.policy,
            "required_decision": self.required_decision,
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class IssueProfileDeclaration:
    """Everything one profile version configures, as the bytes it is digested by.

    ``updated_ucm`` is a field and not a member of ``configured_artifacts``
    because ADR-0091 gives it no configuration switch; the declaration has the
    same shape as the table it is stored in for that reason.
    """

    updated_ucm: RendererRevision
    output_template: FormatIdentity
    field_mapping: FormatIdentity
    configured_artifacts: tuple[ArtifactEntry, ...] = ()
    coverage_requirements: tuple[CoverageRequirement, ...] = ()
    decision_blocking_policies: tuple[DecisionBlockingPolicy, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": DECLARATION_SCHEMA_VERSION,
            "updated_ucm": self.updated_ucm.as_payload(),
            "output_template": _format_payload(self.output_template),
            "field_mapping": _format_payload(self.field_mapping),
            "configured_artifacts": [
                {"artifact_type": entry.artifact_type, **entry.renderer.as_payload()}
                for entry in self.configured_artifacts
            ],
            "coverage_requirements": [
                item.as_payload() for item in self.coverage_requirements
            ],
            "decision_blocking_policies": [
                item.as_payload() for item in self.decision_blocking_policies
            ],
        }

    @property
    def declaration_json(self) -> str:
        """The exact bytes stored and digested; key order is fixed, not incidental."""

        return json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":")
        )

    @property
    def content_sha256(self) -> str:
        return sha256(self.declaration_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IssueInventory:
    """What one project is configured to issue at one cutoff.

    This is the reading #529, #533, #536 and #537 share. It carries the profile
    row and version a candidate binds itself to, the digest that proves what
    that version declared, and every configured input a package needs.
    """

    profile_id: int
    profile_identity: str
    profile_version: int
    content_sha256: str
    effective_from: datetime
    registered_by_principal: str
    registered_at: datetime
    artifacts: tuple[ArtifactEntry, ...]
    output_template: FormatIdentity
    field_mapping: FormatIdentity
    coverage_requirements: tuple[CoverageRequirement, ...]
    decision_blocking_policies: tuple[DecisionBlockingPolicy, ...]

    @property
    def artifact_types(self) -> tuple[str, ...]:
        return tuple(entry.artifact_type for entry in self.artifacts)

    def issues(self, artifact_type: str) -> bool:
        """Whether this project externally issues one artifact type."""

        return artifact_type in self.artifact_types

    def renderer_for(self, artifact_type: str) -> RendererRevision | None:
        for entry in self.artifacts:
            if entry.artifact_type == artifact_type:
                return entry.renderer
        return None


def register_issue_profile(
    session: Session,
    *,
    project_id: int,
    profile_identity: str,
    declaration: IssueProfileDeclaration,
    effective_from: datetime,
    principal: HumanPrincipal,
    idempotency_key: str,
) -> IssueProfile:
    """Register one profile version, on its own attributable act.

    It writes no accepted value and never edits its predecessor: a changed
    profile is a new version effective from the business instant the caller
    supplies, and the version it replaces stays exactly as it was registered.

    The Project Coordination designation is required for every version,
    including the first. Configuring what a customer receives is a coordination
    decision; a person designated only to release externally authorizes a
    prepared package under #533 and does not decide what that package contains.
    """

    actor = require_human_principal(principal)
    identity = (profile_identity or "").strip()
    if not identity:
        raise IssueProfileRefused("an issue profile needs an identity")
    if not (idempotency_key or "").strip():
        raise IssueProfileRefused(
            "an issue profile registration needs an idempotency key"
        )
    if not isinstance(effective_from, datetime) or effective_from.tzinfo is None:
        raise IssueProfileRefused(
            "an issue profile takes effect from an explicitly supplied, "
            "time-zone-aware business instant; nothing here reads a clock"
        )
    _prove_declaration(declaration)
    membership = resolve_membership(session, actor.subject, project_id)
    if membership is None or not membership.has(COORDINATION):
        raise IssueProfileRefused(
            "configuring what this project externally issues is a "
            "project-coordination decision. A person designated only to "
            "release externally authorizes a prepared package and does not "
            "decide what it contains."
        )
    outcome = session.scalar(
        select(
            func.register_project_issue_profile(
                project_id,
                identity,
                declaration.declaration_json,
                effective_from,
                actor.subject,
                idempotency_key,
            )
        )
    )
    session.expire_all()
    return session.get_one(IssueProfile, int(outcome["profile_id"]))


def effective_issue_inventory(
    session: Session, project_id: int, cutoff: datetime
) -> IssueInventory | None:
    """What project ``project_id`` was configured to issue as at ``cutoff``.

    ``None`` where no profile had taken effect by then — an explicit absence,
    never an empty inventory, because "this project issues nothing" and "this
    project was not yet configured" are different facts and only one of them
    is a state a configured project can be in.

    The reading is derived, never stored per week: a later registration cannot
    change it for any cutoff before that registration's own effective instant,
    because a version may only take effect strictly after its predecessor.
    """

    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise IssueProfileRefused(
            "an effective inventory is read as at a declared, time-zone-aware "
            "cutoff; this reading never supplies one from a clock"
        )
    profile = session.scalar(
        select(IssueProfile)
        .where(
            IssueProfile.project_id == project_id,
            IssueProfile.effective_from <= cutoff,
        )
        .order_by(
            IssueProfile.effective_from.desc(), IssueProfile.profile_version.desc()
        )
        .limit(1)
    )
    if profile is None:
        return None
    configured = session.scalars(
        select(IssueProfileArtifact)
        .where(IssueProfileArtifact.profile_id == profile.id)
        .order_by(IssueProfileArtifact.artifact_type)
    ).all()
    declared = json.loads(profile.declaration)
    artifacts = (
        ArtifactEntry(
            artifact_type=UPDATED_UCM,
            renderer=RendererRevision(
                identity=profile.ucm_renderer_identity,
                version=profile.ucm_renderer_version,
            ),
        ),
        *(
            ArtifactEntry(
                artifact_type=row.artifact_type,
                renderer=RendererRevision(
                    identity=row.renderer_identity, version=row.renderer_version
                ),
            )
            for row in configured
        ),
    )
    return IssueInventory(
        profile_id=int(profile.id),
        profile_identity=profile.profile_identity,
        profile_version=int(profile.profile_version),
        content_sha256=profile.content_sha256,
        effective_from=profile.effective_from,
        registered_by_principal=profile.registered_by_principal,
        registered_at=profile.registered_at,
        artifacts=artifacts,
        output_template=_format_identity(declared, "output_template"),
        field_mapping=_format_identity(declared, "field_mapping"),
        coverage_requirements=tuple(
            CoverageRequirement(
                requirement=str(item["requirement"]),
                statement=str(item["statement"]),
            )
            for item in declared.get("coverage_requirements", ())
        ),
        decision_blocking_policies=tuple(
            DecisionBlockingPolicy(
                policy=str(item["policy"]),
                required_decision=str(item["required_decision"]),
                statement=str(item["statement"]),
            )
            for item in declared.get("decision_blocking_policies", ())
        ),
    )


def issue_profile_history(
    session: Session, project_id: int
) -> tuple[IssueProfile, ...]:
    """Every registered version of one project's profile, oldest first."""

    return tuple(
        session.scalars(
            select(IssueProfile)
            .where(IssueProfile.project_id == project_id)
            .order_by(IssueProfile.profile_version)
        ).all()
    )


def prepared_candidate_is_stale(
    inventory: IssueInventory | None, *, profile_id: int, profile_version: int
) -> bool:
    """Whether a candidate prepared against one profile version still stands.

    A profile change never edits a prepared candidate — the candidate keeps
    claiming exactly what it claimed — so the only honest thing to derive is
    that what it claimed is no longer what the project is configured to issue.
    A cutoff with no effective profile makes every candidate stale: nothing
    reads as configured, so nothing was prepared against what is in force.
    """

    if inventory is None:
        return True
    return (
        inventory.profile_id != profile_id
        or inventory.profile_version != profile_version
    )


def _prove_declaration(declaration: IssueProfileDeclaration) -> None:
    """Refuse a declaration before it is digested, not after it is stored."""

    if not isinstance(declaration, IssueProfileDeclaration):
        raise IssueProfileRefused(
            "an issue profile is registered from a declaration, never from a "
            "digest with nothing behind it"
        )
    _require_revision(declaration.updated_ucm, "the updated UCM renderer")
    for identity, kind in (
        (declaration.output_template, "output_template"),
        (declaration.field_mapping, "field_mapping"),
    ):
        if identity.kind != kind:
            raise IssueProfileRefused(
                f"the declared {kind} names a {identity.kind!r} registration"
            )
    seen: set[str] = set()
    for entry in declaration.configured_artifacts:
        if entry.artifact_type == UPDATED_UCM:
            raise IssueProfileRefused(
                "the updated UCM is every package's mandatory member and is "
                "not a configured artifact; it is declared once, on its own"
            )
        if entry.artifact_type not in CONFIGURED_ARTIFACT_TYPES:
            raise IssueProfileRefused(
                f"{entry.artifact_type!r} is not a configurable artifact type"
            )
        if entry.artifact_type in seen:
            raise IssueProfileRefused(
                f"{entry.artifact_type!r} is configured twice in one profile"
            )
        seen.add(entry.artifact_type)
        _require_revision(entry.renderer, f"the {entry.artifact_type} renderer")
    for requirement in declaration.coverage_requirements:
        _require_text(requirement.requirement, "a coverage requirement key")
        _require_text(requirement.statement, "a coverage requirement statement")
    for policy in declaration.decision_blocking_policies:
        _require_text(policy.policy, "a decision-blocking policy key")
        _require_text(
            policy.required_decision, "the decision a blocking policy waits on"
        )
        _require_text(policy.statement, "a decision-blocking policy statement")


def _require_revision(revision: RendererRevision, what: str) -> None:
    _require_text(revision.identity, f"{what} identity")
    _require_text(revision.version, f"{what} version")


def _require_text(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise IssueProfileRefused(f"{what} must be recorded")


def _format_payload(identity: FormatIdentity) -> dict[str, str]:
    return {
        "identity": identity.identity,
        "version": identity.version,
        "content_sha256": identity.content_sha256,
    }


def _format_identity(declared: Mapping[str, Any], kind: str) -> FormatIdentity:
    payload = declared[kind]
    return FormatIdentity(
        kind=kind,
        identity=str(payload["identity"]),
        version=str(payload["version"]),
        content_sha256=str(payload["content_sha256"]),
    )
