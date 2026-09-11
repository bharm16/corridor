"""Approving what one project externally issues, as one attributable act (#828).

#640 modelled the configured issue set and #641 resolved it into executable
content, and both landed with ``register_issue_profile`` reachable from tests
alone: the configuration that decides what a customer receives, and which
questions can block an issue, had no surface at all
(``docs/research/customer-journey-audit-2026-09-10.md``, "Issue configuration
exists only below the UI"). This module is the act that surface performs.

**It composes a proposal; it never invents one.** A proposed version carries
forward everything the version in force already declared — the coverage
requirements, the customer's decision-blocking policies, the updated UCM's own
renderer revision, and the renderer revision of every artifact that stays
configured — so a profile pinned to a released chase-list revision is not
silently upgraded by an approval about something else. What a caller chooses is
exactly what ADR-0091 made configurable: which optional artifacts participate.
The output template and field mapping are the project's own effective
registrations, because a profile naming a registration the project no longer
renders through describes an issue nobody would receive and a candidate nobody
can prepare.

**The running product decides what may be approved, and it decides it here.**
``issue_content.effective_issue_content`` is the one authority on whether a
configuration can be executed, and until now it was consulted only at
preparation: a project could be configured with a renderer revision this
release does not register and find out a week later, when a candidate refused
with ``unsupported_issue_configuration``. The same reading runs over the
*proposed* declaration before anything is registered, and its own sentences are
the refusal — including #670's, which names the canonical field a decision
selector must use and the label a customer reads that field under. Nothing here
composes a second diagnosis of a configuration.

``provenance_sidecar`` is the case that shape exists for. It is a configurable
artifact type with **no registered renderer contract at any version**
(``docs/pilot-success-criteria.md``), so selecting it is refused as
unconfigurable here rather than accepted and discovered at preparation.

**Approval binds the version it was shown.** The predecessor the page read and
the digest of the proposal it printed both travel back, and either having moved
refuses the approval whole rather than registering something other than what
somebody approved. An approval whose proposal digests to the configuration
already in force registers nothing: there is no new version to attribute,
because nothing changed. That is also what makes a resubmitted approval
converge — the second submission recomposes to what the first one registered.

**Staleness stays derived.** Nothing here reaches into a prepared release
candidate. ``release_candidate.candidate_staleness_reasons`` compares what a
candidate was prepared against with what is configured now, and a new version
makes that comparison differ; adding a push from this side would be a second
authority over one rule.

**It sits above ``issue_content`` because it has to.** ``issue_profile`` may not
import the module that resolves it — that is the layering #641 established — so
the registration command cannot ask what the product supports. This module
imports both, and is what the web surface calls.

**No clock.** The effective instant is the caller's declared one, as #640
requires of every profile version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from sqlalchemy.orm import Session

from corridor import refusals
from corridor.baseline_adoption import FormatIdentity, effective_baseline_formats
from corridor.issue_content import (
    ARTIFACT_WORDS,
    CHANGE_SUMMARY_IDENTITY,
    CHANGE_SUMMARY_VERSION,
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    UNREGISTERED_RENDERER,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
    ConfigurationProblem,
    effective_issue_content,
)
from corridor.issue_profile import (
    ArtifactEntry,
    CoverageRequirement,
    DecisionBlockingPolicy,
    IssueInventory,
    IssueProfileDeclaration,
    RendererRevision,
    UPDATED_UCM,
    effective_issue_inventory,
    issue_profile_history,
    register_issue_profile,
)
from corridor.models import CONFIGURED_ARTIFACT_TYPES, IssueProfile
from corridor.principals import HumanPrincipal


# The lineage identity a project's first approved configuration opens. A
# project has exactly one lineage — PostgreSQL refuses a second root — and
# every later version carries this one forward, so this is named once, here.
# It is an internal technical identity in the class of ``ReleasePackage`` and
# ``Review Packet``, not a word a customer reads.
FIRST_PROFILE_IDENTITY = "customer-issue"

#: The renderer revision this release produces each configurable artifact with.
#: Spelled from ``issue_content``'s own constants rather than picked out of
#: ``CONTENT_REGISTRY``, which also carries the compatibility revisions an
#: existing profile may still be pinned to. A newly selected artifact gets the
#: current revision; one that stays selected keeps the revision it already
#: names, so an approval about one artifact never upgrades another.
OFFERED_RENDERERS: Mapping[str, RendererRevision] = {
    "accepted_change_summary": RendererRevision(
        identity=CHANGE_SUMMARY_IDENTITY, version=CHANGE_SUMMARY_VERSION
    ),
    "weekly_coordination_report": RendererRevision(
        identity=WEEKLY_REPORT_IDENTITY, version=WEEKLY_REPORT_VERSION
    ),
    "chase_list": RendererRevision(
        identity=CHASE_LIST_IDENTITY, version=CHASE_LIST_VERSION
    ),
}

#: Artifact types the database admits as configurable that this release
#: produces with nothing. Derived rather than listed, so a renderer built later
#: leaves this set by being registered above and not by anyone remembering.
UNCONFIGURABLE_ARTIFACT_TYPES: tuple[str, ...] = tuple(
    artifact_type
    for artifact_type in CONFIGURED_ARTIFACT_TYPES
    if artifact_type not in OFFERED_RENDERERS
)


class IssueProfileApprovalRefused(refusals.Refusal, ValueError):
    """This approval is not the one that was shown, or cannot be executed.

    One family with a declared kind, so the HTTP adapter answers a superseded
    approval and an unexecutable one differently without either being restated
    as a status at the catch site.
    """

    def __init__(self, sentence: str, *, kind: str = refusals.CONFLICT) -> None:
        super().__init__(sentence)
        self.refusal_kind = kind


@dataclass(frozen=True, slots=True)
class ProposedIssueProfile:
    """One configuration prepared for approval, and everything that binds it.

    It is a value and not a row: no schema holds a proposal, so what makes an
    approval bind *this* one is that the predecessor and the digest travel back
    with it and are compared against a proposal recomposed from the database.
    """

    project_id: int
    profile_identity: str
    declaration: IssueProfileDeclaration
    supersedes_profile_id: int | None
    supersedes_version: int | None
    profile_version: int
    effective_from: datetime
    problems: tuple[ConfigurationProblem, ...]
    unchanged: bool

    @property
    def content_sha256(self) -> str:
        """The digest of the exact bytes an approval would register."""

        return self.declaration.content_sha256

    @property
    def supported(self) -> bool:
        """Whether this release can execute the configuration as proposed."""

        return not self.problems

    @property
    def artifact_types(self) -> tuple[str, ...]:
        return tuple(
            entry.artifact_type for entry in self.declaration.configured_artifacts
        )


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """What one approval did: a next version, or nothing because nothing moved."""

    project_id: int
    profile_id: int | None
    profile_version: int | None
    content_sha256: str
    registered: bool


def propose_issue_profile(
    session: Session,
    *,
    project_id: int,
    artifact_types: Sequence[str],
    effective_from: datetime,
) -> ProposedIssueProfile:
    """Compose the next configuration from the one in force and this selection.

    ``artifact_types`` is ADR-0091's configured set — the change summary, chase
    list, weekly Coordination Report and any sidecar. The updated UCM is not
    among them and is refused if named: it is the mandatory member, held on the
    profile itself, and a caller that could switch it off would be configuring
    a project that issues nothing.

    Every problem this finds is reported rather than raised, because the same
    composition renders the page a person reads before approving. Raising would
    be a configuration screen that cannot show why a configuration is refused.
    """

    history = issue_profile_history(session, project_id)
    current = history[-1] if history else None
    formats = effective_baseline_formats(session, project_id)
    template = formats.get("output_template")
    mapping = formats.get("field_mapping")
    if template is None or mapping is None:
        raise IssueProfileApprovalRefused(
            "this project has registered no output template and field mapping, "
            "so there is no form an issue could be rendered through. The "
            "customer's baseline is adopted first.",
            kind=refusals.NOT_OFFERED,
        )
    carried = _carried_forward(session, current)
    problems: list[ConfigurationProblem] = []
    entries: list[ArtifactEntry] = []
    for artifact_type in sorted(dict.fromkeys(artifact_types)):
        if artifact_type == UPDATED_UCM:
            raise IssueProfileApprovalRefused(
                "the updated UCM is every package's mandatory member and is "
                "not a configured artifact; it is declared once, on its own.",
                kind=refusals.MALFORMED_INPUT,
            )
        if artifact_type not in CONFIGURED_ARTIFACT_TYPES:
            raise IssueProfileApprovalRefused(
                f"{artifact_type!r} is not a configurable artifact type.",
                kind=refusals.MALFORMED_INPUT,
            )
        renderer = carried.artifacts.get(artifact_type) or OFFERED_RENDERERS.get(
            artifact_type
        )
        if renderer is None:
            problems.append(
                ConfigurationProblem(
                    UNREGISTERED_RENDERER,
                    "No renderer revision this release registers produces "
                    f"{ARTIFACT_WORDS.get(artifact_type, artifact_type)}, so "
                    "what it would contain is not known and it cannot be "
                    "configured. Building one is a new build rather than a "
                    "configuration change.",
                )
            )
            continue
        entries.append(ArtifactEntry(artifact_type=artifact_type, renderer=renderer))
    declaration = IssueProfileDeclaration(
        updated_ucm=carried.updated_ucm,
        output_template=FormatIdentity(
            kind="output_template",
            identity=template.format_identity,
            version=template.format_version,
            content_sha256=template.content_sha256,
        ),
        field_mapping=FormatIdentity(
            kind="field_mapping",
            identity=mapping.format_identity,
            version=mapping.format_version,
            content_sha256=mapping.content_sha256,
        ),
        configured_artifacts=tuple(entries),
        coverage_requirements=carried.coverage_requirements,
        decision_blocking_policies=carried.decision_blocking_policies,
    )
    version = 1 if current is None else int(current.profile_version) + 1
    problems.extend(
        effective_issue_content(
            session,
            _provisional_inventory(
                project_id=project_id,
                declaration=declaration,
                profile_identity=carried.profile_identity,
                profile_version=version,
                effective_from=effective_from,
            ),
        ).problems
    )
    return ProposedIssueProfile(
        project_id=int(project_id),
        profile_identity=carried.profile_identity,
        declaration=declaration,
        supersedes_profile_id=None if current is None else int(current.id),
        supersedes_version=None if current is None else int(current.profile_version),
        profile_version=version,
        effective_from=effective_from,
        problems=tuple(problems),
        unchanged=(
            current is not None
            and current.content_sha256 == declaration.content_sha256
        ),
    )


def approve_issue_profile(
    session: Session,
    *,
    project_id: int,
    artifact_types: Sequence[str],
    effective_from: datetime,
    supersedes_profile_id: int | None,
    supersedes_version: int | None,
    content_sha256: str,
    principal: HumanPrincipal,
) -> ApprovalOutcome:
    """Approve one prepared configuration, as the person performing the act.

    The order of the checks is the contract, and it is not the order they were
    written in. **Unchanged is decided first**, because a resubmitted approval
    is the case where the predecessor has legitimately moved: the version the
    first submission registered is now the head, and recomposing this selection
    against it digests to exactly what it registered. Checking staleness first
    would answer a replayed browser Post with "somebody else changed this",
    which is both untrue and unrecoverable from the page.

    Everything after it refuses whole and writes nothing: a predecessor that
    moved to some *other* configuration, a proposal whose bytes are not the
    bytes the page printed, and a configuration this release cannot execute.
    """

    proposal = propose_issue_profile(
        session,
        project_id=project_id,
        artifact_types=artifact_types,
        effective_from=effective_from,
    )
    submitted = (content_sha256 or "").strip()
    if proposal.unchanged and submitted == proposal.content_sha256:
        # Already in force, whether because this approval was submitted twice
        # or because it proposed no change. Either way there is no next version
        # to attribute to anybody.
        return ApprovalOutcome(
            project_id=int(project_id),
            profile_id=proposal.supersedes_profile_id,
            profile_version=proposal.supersedes_version,
            content_sha256=proposal.content_sha256,
            registered=False,
        )
    if (proposal.supersedes_profile_id, proposal.supersedes_version) != (
        supersedes_profile_id,
        supersedes_version,
    ):
        raise IssueProfileApprovalRefused(
            "what this project is configured to externally issue was changed "
            "by someone else after this page was read, so the version you "
            "approved is not the one it would replace. Read the configuration "
            "again.",
            kind=refusals.STALE,
        )
    if submitted != proposal.content_sha256:
        raise IssueProfileApprovalRefused(
            "this is not the configuration the page showed you: the proposal "
            "composed now digests to something else. Read the configuration "
            "again and approve what it prints.",
            kind=refusals.STALE,
        )
    if proposal.problems:
        raise IssueProfileApprovalRefused(
            " ".join(problem.sentence for problem in proposal.problems)
        )
    registered = register_issue_profile(
        session,
        project_id=project_id,
        profile_identity=proposal.profile_identity,
        declaration=proposal.declaration,
        effective_from=effective_from,
        principal=principal,
        # Derived from the approved bytes rather than generated, so the command
        # converges on the version already registered if this ever arrives
        # twice, instead of opening a second one.
        idempotency_key=f"issue-profile-approval:{proposal.content_sha256}",
    )
    return ApprovalOutcome(
        project_id=int(project_id),
        profile_id=int(registered.id),
        profile_version=int(registered.profile_version),
        content_sha256=registered.content_sha256,
        registered=True,
    )


@dataclass(frozen=True, slots=True)
class _CarriedForward:
    """What a next version keeps from the version it replaces, unexamined."""

    profile_identity: str
    updated_ucm: RendererRevision
    artifacts: Mapping[str, RendererRevision]
    coverage_requirements: tuple[CoverageRequirement, ...]
    decision_blocking_policies: tuple[DecisionBlockingPolicy, ...]


def _carried_forward(
    session: Session, current: IssueProfile | None
) -> _CarriedForward:
    """The parts of a configuration this page does not ask anyone to restate.

    Read back through ``effective_issue_inventory`` rather than re-parsed here,
    so a proposal is composed from the same reading every other consumer of the
    configuration uses. Its cutoff is the head version's own effective instant,
    which selects that version and no other: the chain's instants are strictly
    increasing, so the newest version effective at the newest effective instant
    is the newest version.

    A project with no configuration yet starts from the renderer revision this
    release registers for the mandatory member, which is the only one a first
    version could honestly name.
    """

    if current is None:
        return _CarriedForward(
            profile_identity=FIRST_PROFILE_IDENTITY,
            updated_ucm=RendererRevision(
                identity=UCM_RENDERER_IDENTITY, version=UCM_RENDERER_VERSION
            ),
            artifacts={},
            coverage_requirements=(),
            decision_blocking_policies=(),
        )
    inventory = effective_issue_inventory(
        session, int(current.project_id), current.effective_from
    )
    if inventory is None or inventory.profile_id != int(current.id):
        raise IssueProfileApprovalRefused(
            "this project's configuration cannot be read back as at its own "
            "effective instant, so there is nothing a next version could carry "
            "forward."
        )
    return _CarriedForward(
        profile_identity=inventory.profile_identity,
        updated_ucm=RendererRevision(
            identity=current.ucm_renderer_identity,
            version=current.ucm_renderer_version,
        ),
        artifacts={
            entry.artifact_type: entry.renderer
            for entry in inventory.artifacts
            if entry.artifact_type != UPDATED_UCM
        },
        coverage_requirements=inventory.coverage_requirements,
        decision_blocking_policies=inventory.decision_blocking_policies,
    )


def _provisional_inventory(
    *,
    project_id: int,
    declaration: IssueProfileDeclaration,
    profile_identity: str,
    profile_version: int,
    effective_from: datetime,
) -> IssueInventory:
    """The proposal shaped as the reading ``issue_content`` resolves.

    A proposal has no row, so it has no ``profile_id``; ``0`` says so rather
    than borrowing the predecessor's, and nothing downstream of the resolution
    reads it. The point of the shape is that the proposed configuration is
    proved by exactly the function that proves a registered one.
    """

    return IssueInventory(
        project_id=int(project_id),
        profile_id=0,
        profile_identity=profile_identity,
        profile_version=int(profile_version),
        content_sha256=declaration.content_sha256,
        effective_from=effective_from,
        registered_by_principal="",
        registered_at=effective_from,
        artifacts=(
            ArtifactEntry(
                artifact_type=UPDATED_UCM, renderer=declaration.updated_ucm
            ),
            *declaration.configured_artifacts,
        ),
        output_template=declaration.output_template,
        field_mapping=declaration.field_mapping,
        coverage_requirements=declaration.coverage_requirements,
        decision_blocking_policies=declaration.decision_blocking_policies,
    )
