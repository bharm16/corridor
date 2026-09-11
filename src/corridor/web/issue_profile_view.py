"""The Issue configuration page's reading, and the act it carries (#828).

The customer-journey audit found ``register_issue_profile`` with a domain
implementation, a migration granting it to the web capability, and callers in
tests only: what a project externally issues — and therefore which questions
can block an issue — was "invisible operational magic"
(``docs/research/customer-journey-audit-2026-09-10.md``). This module is the
reading the page renders, the counterpart of ``corridor.web.issue_section`` for
the configuration rather than the week.

**It reads; it decides nothing.** ``issue_profile.effective_issue_inventory``
says what is configured, ``issue_content.effective_issue_content`` says whether
this release can execute it, ``issue_profile_approval.propose_issue_profile``
composes what an approval would register, and
``release_candidate.candidate_is_stale`` says what a prepared candidate no
longer matches. Every sentence about state on this page comes from one of
those. Re-deriving any of them here would put a second opinion about a
customer's configuration on the screen that changes it.

**It shows who may approve, and hides nothing on the strength of it.** The
audit's own finding is that a derived capability display is not a competing
security authority: reading the same roster designation ``register_issue_profile``
proves against, and saying so beside the control, prevents a predictable failed
click. The control is still rendered and the refusal still comes from the
command, so nothing here can drift into a gate.

**The proposal is a reading too.** No schema holds a prepared configuration, so
a proposal is composed from a selection that arrives with the request and is
bound by its digest and its predecessor. What makes it reviewable is that the
page prints the same bytes the approval registers.

Terminology: every artifact is named with ``issue_content.ARTIFACT_WORDS``,
which is ADR-0091's own prose table, and every other word on the page is
ADR-0086's or ADR-0091's. No customer-facing label is coined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from corridor.access import COORDINATION, MembershipAccess
from corridor.issue_content import (
    ARTIFACT_WORDS,
    ConfigurationProblem,
    effective_issue_content,
)
from corridor.issue_profile import (
    IssueInventory,
    RendererRevision,
    UPDATED_UCM,
    effective_issue_inventory,
    issue_profile_history,
)
from corridor.issue_profile_approval import (
    OFFERED_RENDERERS,
    ProposedIssueProfile,
    propose_issue_profile,
)
from corridor.models import CONFIGURED_ARTIFACT_TYPES
from corridor.release_candidate import candidate_is_stale, current_release_candidate


@dataclass(frozen=True, slots=True)
class ArtifactLine:
    """One artifact, as the configuration names it and a person reads it."""

    artifact_type: str
    words: str
    renderer: RendererRevision | None


@dataclass(frozen=True, slots=True)
class ArtifactChoice:
    """One configurable artifact, and whether this release can produce it."""

    artifact_type: str
    words: str
    configured: bool
    renderer: RendererRevision | None

    @property
    def offered(self) -> bool:
        """Whether a person may select it at all (#828, ADR-0091)."""

        return self.renderer is not None


@dataclass(frozen=True, slots=True)
class VersionLine:
    """One registered version of the configuration, and who approved it."""

    profile_version: int
    effective_from: datetime
    registered_by_principal: str
    registered_at: datetime
    content_sha256: str


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """What this project is configured to issue, as at the reading's instant."""

    profile_identity: str
    profile_version: int
    effective_from: datetime
    registered_by_principal: str
    registered_at: datetime
    content_sha256: str
    updated_ucm: ArtifactLine
    configured: tuple[ArtifactLine, ...]
    output_template: tuple[str, str]
    field_mapping: tuple[str, str]
    coverage_requirements: tuple[tuple[str, str], ...]
    decision_blocking_policies: tuple[tuple[str, str, str], ...]
    problems: tuple[ConfigurationProblem, ...]


@dataclass(frozen=True, slots=True)
class ProposalPanel:
    """The version an approval would produce, bound to what it would replace."""

    profile_version: int
    effective_from: datetime
    content_sha256: str
    supersedes_profile_id: int | None
    supersedes_version: int | None
    updated_ucm: ArtifactLine
    configured: tuple[ArtifactLine, ...]
    output_template: tuple[str, str]
    field_mapping: tuple[str, str]
    problems: tuple[ConfigurationProblem, ...]
    unchanged: bool

    @property
    def supported(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class PreparedCandidateLine:
    """The prepared issue a configuration change would leave behind, if any."""

    candidate_identity: str
    profile_version: int
    stale_reasons: tuple[str, ...]

    @property
    def stale(self) -> bool:
        return bool(self.stale_reasons)


@dataclass(frozen=True, slots=True)
class IssueConfigurationView:
    """Everything the Issue configuration page prints, read once."""

    effective: EffectiveConfiguration | None
    history: tuple[VersionLine, ...]
    choices: tuple[ArtifactChoice, ...]
    proposal: ProposalPanel | None
    candidate: PreparedCandidateLine | None
    may_approve: bool


def issue_configuration_view(
    session: Session,
    *,
    project_id: int,
    membership: MembershipAccess,
    as_of: datetime,
    proposed_artifact_types: tuple[str, ...] | None = None,
) -> IssueConfigurationView:
    """The configuration in force, its history, and the proposal if one was made.

    ``proposed_artifact_types`` is ``None`` when nobody has prepared anything —
    the page is then a reading with a selection form — and a tuple, possibly
    empty, when a selection arrived. Empty is a real proposal: a project may be
    configured to issue the mandatory updated UCM and nothing else.
    """

    inventory = effective_issue_inventory(session, project_id, as_of)
    effective = _effective(session, inventory)
    configured = {
        entry.artifact_type: entry.renderer
        for entry in (inventory.artifacts if inventory else ())
        if entry.artifact_type != UPDATED_UCM
    }
    proposal = None
    if proposed_artifact_types is not None:
        proposal = _panel(
            propose_issue_profile(
                session,
                project_id=project_id,
                artifact_types=proposed_artifact_types,
                effective_from=as_of,
            )
        )
    return IssueConfigurationView(
        effective=effective,
        history=tuple(
            VersionLine(
                profile_version=int(row.profile_version),
                effective_from=row.effective_from,
                registered_by_principal=row.registered_by_principal,
                registered_at=row.registered_at,
                content_sha256=row.content_sha256,
            )
            for row in reversed(issue_profile_history(session, project_id))
        ),
        choices=tuple(
            ArtifactChoice(
                artifact_type=artifact_type,
                words=ARTIFACT_WORDS.get(artifact_type, artifact_type),
                configured=artifact_type in configured,
                renderer=configured.get(artifact_type)
                or OFFERED_RENDERERS.get(artifact_type),
            )
            for artifact_type in CONFIGURED_ARTIFACT_TYPES
        ),
        proposal=proposal,
        candidate=_candidate(session, project_id, as_of),
        may_approve=membership.has(COORDINATION),
    )


def _effective(
    session: Session, inventory: IssueInventory | None
) -> EffectiveConfiguration | None:
    """The configuration in force, with the problems this release finds in it."""

    if inventory is None:
        return None
    content = effective_issue_content(session, inventory)
    return EffectiveConfiguration(
        profile_identity=inventory.profile_identity,
        profile_version=inventory.profile_version,
        effective_from=inventory.effective_from,
        registered_by_principal=inventory.registered_by_principal,
        registered_at=inventory.registered_at,
        content_sha256=inventory.content_sha256,
        updated_ucm=_line(UPDATED_UCM, inventory.renderer_for(UPDATED_UCM)),
        configured=tuple(
            _line(entry.artifact_type, entry.renderer)
            for entry in inventory.artifacts
            if entry.artifact_type != UPDATED_UCM
        ),
        output_template=(
            inventory.output_template.identity,
            inventory.output_template.version,
        ),
        field_mapping=(
            inventory.field_mapping.identity,
            inventory.field_mapping.version,
        ),
        coverage_requirements=tuple(
            (item.requirement, item.statement)
            for item in inventory.coverage_requirements
        ),
        decision_blocking_policies=tuple(
            (item.policy, item.required_decision, item.statement)
            for item in inventory.decision_blocking_policies
        ),
        problems=content.problems,
    )


def _panel(proposal: ProposedIssueProfile) -> ProposalPanel:
    return ProposalPanel(
        profile_version=proposal.profile_version,
        effective_from=proposal.effective_from,
        content_sha256=proposal.content_sha256,
        supersedes_profile_id=proposal.supersedes_profile_id,
        supersedes_version=proposal.supersedes_version,
        updated_ucm=_line(UPDATED_UCM, proposal.declaration.updated_ucm),
        configured=tuple(
            _line(entry.artifact_type, entry.renderer)
            for entry in proposal.declaration.configured_artifacts
        ),
        output_template=(
            proposal.declaration.output_template.identity,
            proposal.declaration.output_template.version,
        ),
        field_mapping=(
            proposal.declaration.field_mapping.identity,
            proposal.declaration.field_mapping.version,
        ),
        problems=proposal.problems,
        unchanged=proposal.unchanged,
    )


def _candidate(
    session: Session, project_id: int, as_of: datetime
) -> PreparedCandidateLine | None:
    """The prepared issue this configuration change would leave behind.

    ``candidate_is_stale`` is #529's own rule and the only one consulted: a
    configuration change makes a candidate stale by that comparison and by
    nothing this module adds.
    """

    candidate = current_release_candidate(session, project_id)
    if candidate is None:
        return None
    return PreparedCandidateLine(
        candidate_identity=candidate.candidate_identity,
        profile_version=int(candidate.issue_profile_version),
        stale_reasons=candidate_is_stale(session, candidate, as_of=as_of),
    )


def _line(artifact_type: str, renderer: RendererRevision | None) -> ArtifactLine:
    return ArtifactLine(
        artifact_type=artifact_type,
        words=ARTIFACT_WORDS.get(artifact_type, artifact_type),
        renderer=renderer,
    )
