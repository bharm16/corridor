"""One immutable release candidate, prepared from one coherent reading (#529).

ADR-0086 makes one attributable authorization of one **set** of customer
artifacts the external issue unit, and ADR-0091 makes the membership of that set
per-project configuration with only the updated native UCM mandatory. #640
modelled the configuration, #641 resolved it into executable content and policy,
#495 renders the customer's workbook, #534 the change summary and weekly
Coordination Report, and #425 the chase list. This module is what turns all of
that into one candidate somebody can review, and #533 is what authorizes one.

**The configuration is read, never reinterpreted.** Everything about what this
project issues comes from ``issue_content.effective_issue_content`` unchanged:
which artifacts, which renderer identity and version produces each, which
coverage evaluators run, and which customer policies block. ``supported`` is the
gate — a configuration this release cannot execute produces a *refused*
preparation and never a candidate rendered by "probably the current renderer".
Two authorities over one configuration is the failure #641 exists to prevent, so
this module holds none of its own: ``ARTIFACT_INVOKERS`` says *how* to invoke a
renderer and is asserted at import to have exactly the keys #641's
``CONTENT_REGISTRY`` declares, so a renderer revision that is registered but not
invokable is an import-time error rather than a silent gap in a package.

**Identity is the digested declaration.** ``candidate_identity`` is the SHA-256
of the canonical bytes binding every input ADR-0086 shares across the set: the
project, the accepted Project Record revision, the previous authorized package
*or an explicit none*, the source cutoff, the coverage identity and digest, the
issue-profile row, identity, version and digest, the output-template and
field-mapping registrations and digests, the configured artifact types, every
renderer identity and version, the product and code revision, and the enabled
feature flags. The **content** digest binds all of that plus the ordered
artifact identities and their own SHA-256 digests, so "were these the same
inputs" and "is this the same issue" are two different questions with two
different answers. ``issue_profile.prepared_candidate_is_stale`` answers only
the *profile* part of the first one; it is one term in ``candidate_is_stale``
here and deliberately not the definition of the contract.

**No long render under a database lock.** Preparation is three phases and never
one transaction. ``bind_preparation`` takes the project row lock, resolves and
binds every input including the frozen accepted projection, the follow-up
reading and both structured issue readings, and returns; the caller commits and
the lock goes. ``render_candidate_artifacts`` then produces bytes and retains
them content-addressed through #487's store, holding **no** project lock — and
three of the four invokers take no session at all, which is why
``test_release_candidate`` can render them with ``session=None`` and prove no
transaction was open across them. Only the updated UCM needs a session while it
renders, because #495's seam reads and renders in one call; that session is the
render phase's own, holds no lock, and is closed before anything is written.
``attach_candidate`` then opens the second transaction, re-derives the whole
bound state and compares it against what was bound, and attaches the complete
set atomically. On any mismatch it records the refusal and attaches nothing.

**Three outcomes, kept apart.** A *failed* preparation leaves no candidate row
and no artifact row — the only trace is one ``ReleasePreparationRefusal`` with a
reason from a closed vocabulary. A *complete but blocked* candidate exists,
immutably, with every configured artifact rendered and retained and readiness
``blocked``; ``authorization_blockers`` refuses it for #533, and the only way to
clear it is a **newly prepared** candidate, because what changed is the bound
coverage or decision state that the old candidate's identity is made of. *Ready
with disclosed exceptions* is a candidate that exists, names its exceptions
honestly and is authorizable — ADR-0086's "honest adverse project conditions
never block release", carried literally.

**The predecessor is a typed reference.** ``latest_authorized_package`` reads
``release_packages`` and nothing else. It is never the newest report by
timestamp, never ``external_report_releases`` (which binds no accepted revision
— #635), never the newest render, never the newest candidate, and never the
adopted baseline: the baseline is the customer's own artifact adopted as a
starting record, not an issue Corridor authorized and returned. Before the first
authorized package the predecessor is explicitly absent, and the configured
artifacts render current accepted state without inventing a prior issue.
When the supervisor has frozen an external comparison window, that explicit
predecessor travels with it. Binding under the project lock refuses if another
authorization advanced the chain; selecting a new predecessor while retaining
the old floors would let the report and change summary contradict each other.

**No clock.** The source cutoff, the effective instant and the preparation
instant are all supplied by the caller. Nothing here reads the wall clock, so a
preparation of a past cutoff is the same preparation tomorrow.

Terminology: ``Release candidate``, ``ReleasePackage`` and ``Issue Profile``
stay internal technical names, in the class ADR-0086 and ADR-0091 put them in.
No customer-facing label is coined here, so `docs/agents/domain.md`'s
terminology-research procedure is not triggered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from corridor.analytics import (
    AnalyticsBinding,
    default_binding,
    emit_event,
    release_candidate_preparation_event,
)
from corridor.baseline_adoption import FormatIdentity, effective_baseline_formats
from corridor.follow_up_bundles import (
    ELAPSED,
    FollowUpReading,
    bundle_reading_payload,
    read_follow_up_bundles,
)
from corridor.issue_coverage import CoverageRefused, declared_lines, load_declaration
from corridor.issue_content import (
    ARTIFACT_WORDS,
    CHANGE_SUMMARY_IDENTITY,
    CHANGE_SUMMARY_VERSION,
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    CONTENT_CONTRACT_VERSION,
    CONTENT_REGISTRY,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
    ChangeFacts,
    EffectiveIssueContent,
    ResolvedBlockingPolicy,
    effective_issue_content,
)
from corridor.issue_profile import (
    UPDATED_UCM,
    IssueInventory,
    effective_issue_inventory,
    prepared_candidate_is_stale,
)
from corridor.issue_rendering import (
    BoundIssueReading,
    IssueArtifacts,
    MixedIssueInputs,
    PreviousApprovedIssue,
    SourceCoverage,
    TemplateBinding,
    bind_issue_reading,
    read_issue_artifacts,
    render_change_summary,
    render_weekly_report,
)
from corridor.models import (
    BLOCKED,
    PREPARATION_REFUSAL_REASONS,
    READY,
    READY_WITH_EXCEPTIONS,
    Document,
    ProjectRecordRevision,
    ProposedDelta,
    ReleaseCandidate,
    ReleaseCandidateArtifact,
    ReleasePackage,
    ReleasePreparationRefusal,
)
from corridor.native_follow_up_reading import AcceptedFollowUpPlan
from corridor.object_storage import ObjectStore, content_key, content_store
from corridor.presentation import field_label
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.report_preparation import AUTHORIZED_PACKAGE_COMPARISON
from corridor.review_packet_reading import read_open_deltas
from corridor.workbook_render import RenderProfile, render_project_record_workbook


# The shape of the digested declarations. A candidate written under a later
# schema binds different things, so the version travels inside the bytes.
INPUT_SCHEMA_VERSION = "release-candidate-inputs-v1"
CONTENT_SCHEMA_VERSION = "release-candidate-content-v1"

# The reason codes a refusal may carry, taken from the model so the closed
# vocabulary is spelled once and the database check and this module cannot
# disagree.
UNSUPPORTED_ISSUE_CONFIGURATION = "unsupported_issue_configuration"
RENDERER_FAILED = "renderer_failed"
ARTIFACT_MISSING = "artifact_missing"
STORAGE_FAILED = "storage_failed"
DIGEST_MISMATCH = "digest_mismatch"
INPUTS_CHANGED_WHILE_RENDERING = "inputs_changed_while_rendering"
MIXED_READING = "mixed_reading"
CANDIDATE_IDENTITY_CONFLICT = "candidate_identity_conflict"

# The file extension each artifact's retained bytes keep in the store.
ARTIFACT_SUFFIXES: Mapping[str, str] = {
    UPDATED_UCM: ".xlsx",
    "accepted_change_summary": ".txt",
    "weekly_coordination_report": ".txt",
    "chase_list": ".json",
}


class PreparationRefused(Exception):
    """Preparation cannot produce a candidate, and says which bounded reason.

    Carried rather than raised as a bare ``ValueError`` because the reason code
    is what the receipt records and what a caller counts; a message alone would
    make every refusal a string somebody has to classify by reading.
    """

    def __init__(self, code: str, sentence: str) -> None:
        if code not in PREPARATION_REFUSAL_REASONS:
            raise ValueError(f"{code!r} is not a recorded refusal reason")
        super().__init__(sentence)
        self.code = code
        self.sentence = sentence


# --- the declared coverage state -------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageDeclaration:
    """ADR-0086's one declared coverage state, with an identity and a digest.

    The identity and digest are bound into the candidate because "what was and
    was not reviewed for this issue" is one of the five shared inputs, and a
    coverage state that could be edited after the fact would let a package
    claim a review nobody performed.

    **It is no longer a value a caller composes** (#675). This is now read from
    one ``issue_coverage_declarations`` row — a coordinator's confirmation of
    the reading Corridor derived — and ``declaration_id`` names that row.
    Preparation therefore cannot be given a coverage state nobody confirmed,
    and the candidate carries the confirmation as a foreign key rather than as
    a digest of bytes whose provenance nothing records.
    """

    declaration_id: int
    identity: str
    lines: tuple[SourceCoverage, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "lines": [
                {
                    "source_name": line.source_name,
                    "requirement": line.requirement,
                    "state": line.state,
                    "detail": line.detail,
                }
                for line in self.lines
            ],
        }

    @property
    def declaration_json(self) -> str:
        return json.dumps(
            self.as_payload(), sort_keys=True, separators=(",", ":")
        )

    @property
    def content_sha256(self) -> str:
        return sha256(self.declaration_json.encode("utf-8")).hexdigest()

    @property
    def exceptions(self) -> tuple[SourceCoverage, ...]:
        return tuple(line for line in self.lines if line.is_exception)


def declared_coverage(
    session: Session, *, project_id: int, declaration_id: int
) -> CoverageDeclaration:
    """The confirmed coverage one preparation runs under, read by identity.

    The one door between #675's persisted declaration and this module's bound
    value. It reads the confirmed lines back out of the stored declaration
    bytes the row's digest covers, so what a candidate is prepared under is
    exactly what somebody put their name to.
    """

    row = load_declaration(
        session, project_id=project_id, declaration_id=declaration_id
    )
    return CoverageDeclaration(
        declaration_id=int(row.id),
        identity=row.coverage_identity,
        lines=declared_lines(row),
    )


# --- what one binding produced ---------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedDecision:
    """One unresolved difference an explicit customer policy waits on."""

    delta_id: int
    field: str | None
    change_type: str
    policy: str
    selector: str
    statement: str

    @property
    def sentence(self) -> str:
        subject = field_label(self.field) if self.field else "this change"
        return (
            f"{self.statement.rstrip('.')} — {subject} is proposed and not "
            f"decided, and this project's policy {self.policy} waits on "
            f"{self.selector}."
        )


@dataclass(frozen=True)
class BoundPreparation:
    """Every input one preparation is bound to, read once under one lock.

    Nothing here is re-read while rendering, and everything here is re-derived
    and compared before anything is attached.
    """

    project_id: int
    accepted_revision_id: int
    previous_package_id: int | None
    source_cutoff: datetime
    prepared_at: datetime
    coverage: CoverageDeclaration
    inventory: IssueInventory
    content: EffectiveIssueContent
    output_template: FormatIdentity
    output_template_format_id: int
    field_mapping: FormatIdentity
    field_mapping_format_id: int
    binding: AnalyticsBinding
    reading: BoundIssueReading
    issue_artifacts: IssueArtifacts
    follow_up: FollowUpReading
    unread_source_count: int
    unmet_coverage: tuple[str, ...]
    blocked_decisions: tuple[BlockedDecision, ...]
    disclosed_exceptions: tuple[str, ...]
    template_bytes_sha256: str
    # Which retained report-preparation reading this candidate measured (#690).
    # `preparation` above is the reading's *content*; these two are its
    # identity, so the next preparation can follow previous authorized package
    # -> its candidate -> that candidate's report-preparation receipt -> the
    # exact prior watermarks, instead of guessing at "the latest reading".
    # `None` where no supervisor bound one, spelled rather than omitted.
    report_receipt_id: int | None = None
    report_result_sha256: str | None = None

    @property
    def artifact_types(self) -> tuple[str, ...]:
        """Every configured artifact type, UCM first, then the configured rest.

        The order is the order the content digest binds, and the order the
        artifact rows record in ``position``.
        """

        return tuple(
            resolved.artifact_type
            for resolved in _ordered_artifacts(self.content)
        )

    @property
    def readiness(self) -> str:
        if self.unmet_coverage or self.blocked_decisions:
            return BLOCKED
        return READY_WITH_EXCEPTIONS if self.disclosed_exceptions else READY

    @property
    def blockers(self) -> tuple[str, ...]:
        """Every current blocker, named. Empty where nothing blocks."""

        return tuple(self.unmet_coverage) + tuple(
            decision.sentence for decision in self.blocked_decisions
        )

    def as_input_payload(self) -> dict[str, Any]:
        return {
            "schema_version": INPUT_SCHEMA_VERSION,
            "content_contract_version": CONTENT_CONTRACT_VERSION,
            "project_id": self.project_id,
            "accepted_revision_id": self.accepted_revision_id,
            # ADR-0086's explicit none, spelled rather than omitted: a missing
            # key and a null predecessor must not digest the same.
            "previous_authorized_package": (
                None
                if self.previous_package_id is None
                else {"package_id": self.previous_package_id}
            ),
            "source_cutoff": self.source_cutoff.isoformat(),
            "coverage": {
                # The confirmed declaration by identity (#675), beside what it
                # said. Two different confirmations of the same lines are two
                # different declarations, and a candidate says which one it was
                # prepared under.
                "declaration_id": self.coverage.declaration_id,
                "identity": self.coverage.identity,
                "content_sha256": self.coverage.content_sha256,
            },
            "issue_profile": {
                "profile_id": self.inventory.profile_id,
                "identity": self.inventory.profile_identity,
                "version": self.inventory.profile_version,
                "content_sha256": self.inventory.content_sha256,
            },
            "output_template": {
                "format_id": self.output_template_format_id,
                "identity": self.output_template.identity,
                "version": self.output_template.version,
                "content_sha256": self.output_template.content_sha256,
            },
            "field_mapping": {
                "format_id": self.field_mapping_format_id,
                "identity": self.field_mapping.identity,
                "version": self.field_mapping.version,
                "content_sha256": self.field_mapping.content_sha256,
            },
            # The exact retained reading, by identity and result digest. A
            # candidate that named only the counts could not prove which
            # receipt produced them, and the next issue's floor is read off
            # this chain.
            "report_preparation": (
                None
                if self.report_receipt_id is None
                else {
                    "receipt_id": self.report_receipt_id,
                    "result_sha256": self.report_result_sha256,
                }
            ),
            "configured_artifact_types": list(self.artifact_types),
            "renderers": [
                {
                    "artifact_type": resolved.artifact_type,
                    "renderer_identity": resolved.renderer.identity,
                    "renderer_version": resolved.renderer.version,
                }
                for resolved in _ordered_artifacts(self.content)
            ],
            "code_revision": self.binding.code_revision,
            "product_revision": self.binding.product_revision,
            "enabled_feature_flags": sorted(self.binding.enabled_feature_flags),
            # The derived state readiness stands on, bound into the identity
            # rather than merely revalidated against it. ADR-0086 requires
            # clearing a blocker to need a *newly prepared* candidate: if the
            # blocking coverage or decision state sat outside the identity, a
            # repeat preparation after the blocker cleared would converge on
            # the blocked row and hand a coordinator `blocked` as the answer
            # to having fixed it. ``reading_identity`` is #534's own
            # fingerprint of the frozen accepted projection and the four other
            # shared inputs, so the values the artifacts state are bound here
            # too and not only the identifiers they were read under.
            "derived_state": {
                "reading_identity": self.reading.reading_identity,
                "unread_source_count": self.unread_source_count,
                "unmet_coverage": list(self.unmet_coverage),
                "blocked_decisions": [
                    {
                        "delta_id": decision.delta_id,
                        "policy": decision.policy,
                        "selector": decision.selector,
                    }
                    for decision in self.blocked_decisions
                ],
                "disclosed_exceptions": list(self.disclosed_exceptions),
            },
        }

    @property
    def input_declaration(self) -> str:
        return json.dumps(
            self.as_input_payload(), sort_keys=True, separators=(",", ":")
        )

    @property
    def candidate_identity(self) -> str:
        return sha256(self.input_declaration.encode("utf-8")).hexdigest()

    def as_state_payload(self) -> dict[str, Any]:
        """The bound state the second transaction re-derives and compares.

        The identity already digests every input including the derived state,
        so this adds only what is bound to the preparation without being part
        of what the candidate *is*: the exact template bytes the artifacts were
        rendered from. One digest over all of it is why revalidation cannot
        forget a field — a new bound input that is not in here would have to be
        deliberately left out of the identity as well.
        """

        return {
            "identity": self.candidate_identity,
            "template_bytes_sha256": self.template_bytes_sha256,
        }

    @property
    def state_sha256(self) -> str:
        return sha256(
            json.dumps(
                self.as_state_payload(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class RenderedCandidateArtifact:
    """One artifact's exact bytes, its digest, and the renderer that made it."""

    artifact_type: str
    renderer_identity: str
    renderer_version: str
    content: bytes
    sha256: str
    storage_key: str

    @property
    def byte_count(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class RenderInputs:
    """What one invoker is given, and the only session any of them may see.

    ``session`` is ``None`` for every invoker but the updated UCM's, because
    #495's renderer reads and renders in one call. The other three are pure
    over the bound readings, which is what makes "no transaction is held open
    across rendering" a property a test can assert rather than a convention.
    """

    bound: BoundPreparation
    template_bytes: bytes
    session: Session | None = None


Invoker = Callable[[RenderInputs], bytes]


def _render_updated_ucm(inputs: RenderInputs) -> bytes:
    if inputs.session is None:
        raise PreparationRefused(
            RENDERER_FAILED,
            "the customer's own workbook is rendered through the registered "
            "template and mapping, which needs a reading session",
        )
    rendered = render_project_record_workbook(
        inputs.session,
        project_id=inputs.bound.project_id,
        revision_id=inputs.bound.accepted_revision_id,
        template_bytes=inputs.template_bytes,
        profile=RenderProfile(provenance_worksheet=True),
    )
    return rendered.content


def _render_change_summary(inputs: RenderInputs) -> bytes:
    return render_change_summary(
        inputs.bound.issue_artifacts.change_summary
    ).text.encode("utf-8")


def _render_weekly_report(inputs: RenderInputs) -> bytes:
    return render_weekly_report(
        inputs.bound.issue_artifacts.weekly_report
    ).text.encode("utf-8")


def _render_chase_list(inputs: RenderInputs) -> bytes:
    return json.dumps(
        bundle_reading_payload(inputs.bound.follow_up),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# How to invoke each registered renderer revision. The keys are #641's, not a
# second opinion about them: the assertion below refuses to import a release
# where a registered contract has no invoker or an invoker has no contract, so
# a package can never be produced by a renderer nobody declared the content of.
ARTIFACT_INVOKERS: Mapping[tuple[str, str, str], Invoker] = {
    (UPDATED_UCM, UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION): _render_updated_ucm,
    (
        "accepted_change_summary",
        CHANGE_SUMMARY_IDENTITY,
        CHANGE_SUMMARY_VERSION,
    ): _render_change_summary,
    (
        "weekly_coordination_report",
        WEEKLY_REPORT_IDENTITY,
        WEEKLY_REPORT_VERSION,
    ): _render_weekly_report,
    ("chase_list", CHASE_LIST_IDENTITY, CHASE_LIST_VERSION): _render_chase_list,
    ("chase_list", CHASE_LIST_IDENTITY, "v1"): _render_chase_list,
}

if set(ARTIFACT_INVOKERS) != {contract.key for contract in CONTENT_REGISTRY}:
    raise RuntimeError(
        "every registered renderer contract needs exactly one invoker and "
        "every invoker needs a registered contract; a package rendered by a "
        "renderer nobody declared the content of is a package nobody can audit"
    )


# --- the predecessor -------------------------------------------------------


def latest_authorized_package(
    session: Session, project_id: int
) -> ReleasePackage | None:
    """The previous authorized package for one project, or an explicit ``None``.

    It reads ``release_packages`` and nothing else. ADR-0086 forbids every
    substitute this could have been: the newest released report by timestamp,
    the newest ``ExternalReportRelease`` (which binds no accepted revision —
    #635), the newest render, the newest prepared candidate, and the adopted
    baseline, which is the customer's own starting artifact and never an issue
    Corridor authorized and returned. ``None`` before a project's first
    authorized package is the honest answer and the one ADR-0086 requires.

    **The chain head, not the newest timestamp** (#533, applying #634's
    principle). #529 wrote this as ``order by authorized_at desc`` while the
    relation was empty and had no chain to read. It now reads the package that
    no other package names as its predecessor, which is the append-only release
    identity #533's amendment requires. The difference is not theoretical: a
    release recorded at an earlier declared instant than its predecessor is
    still its successor, and ordering by the clock would hand the next
    candidate the wrong comparison baseline and quietly reopen a window the
    customer has already seen.
    """

    return latest_authorized_packages_by_project(session, (project_id,)).get(
        project_id
    )


def latest_authorized_packages_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, ReleasePackage | None]:
    """``latest_authorized_package`` for several projects, in one statement.

    The cross-project reading (#537) needs each project's comparison baseline
    to decide whether a prepared candidate still stands, and it may not ask for
    it one project at a time. It may not answer it by a second rule either, so
    the single-project reader above is this function over one project: the
    chain head is still the package no other package names as its predecessor,
    and the clock still gets no vote.
    """

    ids = tuple(dict.fromkeys(int(value) for value in project_ids))
    found: dict[int, ReleasePackage | None] = {project_id: None for project_id in ids}
    if not ids:
        return found
    later = aliased(ReleasePackage)
    for package in session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id.in_(ids),
            ~select(later.id)
            .where(later.previous_package_id == ReleasePackage.id)
            .exists(),
        )
    ).all():
        found[int(package.project_id)] = package
    return found


def current_candidates_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, ReleaseCandidate | None]:
    """The candidate each project is currently offering, in one statement.

    "Currently offering" is the newest one attached, by append-only identifier
    rather than by ``prepared_at``: the preparation instant is declared by its
    caller (#634), so a candidate prepared *as at* an earlier cutoff can be
    attached later, and ordering by the declared instant would offer the
    superseded one. An older candidate is not deleted and stays authorizable by
    identifier; it is simply not the one a portfolio row is about.

    ``current_release_candidate`` is the single-project sibling and delegates
    here, so the Issue section (#536) and the portfolio row (#636) cannot name
    different candidates for one project.
    """

    ids = tuple(dict.fromkeys(int(value) for value in project_ids))
    found: dict[int, ReleaseCandidate | None] = {
        project_id: None for project_id in ids
    }
    if not ids:
        return found
    newest = (
        select(
            ReleaseCandidate.project_id.label("project_id"),
            func.max(ReleaseCandidate.id).label("candidate_id"),
        )
        .where(ReleaseCandidate.project_id.in_(ids))
        .group_by(ReleaseCandidate.project_id)
        .subquery()
    )
    for candidate in session.scalars(
        select(ReleaseCandidate).join(
            newest, ReleaseCandidate.id == newest.c.candidate_id
        )
    ).all():
        found[int(candidate.project_id)] = candidate
    return found


# --- phase one: bind ------------------------------------------------------


def bind_preparation(
    session: Session,
    *,
    project_id: int,
    preparation: Mapping[str, Any],
    source_cutoff: datetime,
    prepared_at: datetime,
    coverage_declaration_id: int,
    templates: TemplateBinding,
    first_issue_behavior: str,
    template_bytes: bytes,
    binding: AnalyticsBinding | None = None,
    follow_up_plans: Sequence[AcceptedFollowUpPlan] = (),
    report_receipt_id: int | None = None,
    report_result_sha256: str | None = None,
) -> BoundPreparation:
    """Resolve and bind every input for one candidate, under the project lock.

    Short by construction: it reads, it binds, and it renders nothing. The
    caller commits immediately, and the project lock is gone before a single
    byte is produced.

    ``coverage_declaration_id`` replaced #529's ``coverage`` value deliberately
    (#675). The coverage state one issue is prepared under is now a confirmed
    ``issue_coverage_declarations`` row rather than a dataclass a caller
    composed, so preparation cannot be handed a coverage nobody attested to,
    and the candidate references that confirmation by foreign key.
    """

    if source_cutoff.tzinfo is None or prepared_at.tzinfo is None:
        raise PreparationRefused(
            MIXED_READING,
            "a candidate is bound to declared, time-zone-aware instants; "
            "nothing here reads a clock",
        )
    try:
        coverage = declared_coverage(
            session, project_id=project_id, declaration_id=coverage_declaration_id
        )
    except CoverageRefused as exc:
        raise PreparationRefused(MIXED_READING, str(exc)) from exc
    if not (coverage.identity or "").strip():
        raise PreparationRefused(
            MIXED_READING, "the declared coverage state needs an identity"
        )

    lock_project(session, project_id)
    bound_binding = binding or default_binding()

    inventory = effective_issue_inventory(session, project_id, source_cutoff)
    content = effective_issue_content(session, inventory)
    if not content.supported:
        raise PreparationRefused(
            UNSUPPORTED_ISSUE_CONFIGURATION,
            " ".join(problem.sentence for problem in content.problems),
        )
    assert inventory is not None  # `supported` is false without one

    formats = effective_baseline_formats(session, project_id)
    template_row = formats.get("output_template")
    mapping_row = formats.get("field_mapping")
    if template_row is None or mapping_row is None:
        raise PreparationRefused(
            MIXED_READING,
            "this project has no effective output-template and field-mapping "
            "registration, so the one mandatory artifact cannot be produced",
        )
    output_template = FormatIdentity(
        kind="output_template",
        identity=template_row.format_identity,
        version=template_row.format_version,
        content_sha256=template_row.content_sha256,
    )
    field_mapping = FormatIdentity(
        kind="field_mapping",
        identity=mapping_row.format_identity,
        version=mapping_row.format_version,
        content_sha256=mapping_row.content_sha256,
    )
    for declared, effective, what in (
        (inventory.output_template, output_template, "output template"),
        (inventory.field_mapping, field_mapping, "field mapping"),
    ):
        if (
            declared.identity,
            declared.version,
            declared.content_sha256,
        ) != (effective.identity, effective.version, effective.content_sha256):
            raise PreparationRefused(
                MIXED_READING,
                f"the issue profile names {what} {declared.identity} "
                f"{declared.version} and this project renders through "
                f"{effective.identity} {effective.version}; one issue is one "
                "approved template and mapping set, never two",
            )

    package = latest_authorized_package(session, project_id)
    previous_package_id = None if package is None else int(package.id)
    if preparation.get("comparison_baseline") == AUTHORIZED_PACKAGE_COMPARISON:
        frozen_previous = preparation.get("previous_authorized_package_id")
        if "previous_authorized_package_id" not in preparation or (
            frozen_previous is not None
            and (type(frozen_previous) is not int or frozen_previous <= 0)
        ):
            raise PreparationRefused(
                MIXED_READING,
                "an external comparison window requires an explicit previous "
                "authorized package identity or an explicit none for a first issue",
            )
        if frozen_previous != previous_package_id:
            raise PreparationRefused(
                MIXED_READING,
                "the previous authorized package changed after this request "
                "bound its comparison window; nothing is attached and a fresh "
                "preparation request is needed",
            )
    previous_issue = (
        None
        if package is None
        else PreviousApprovedIssue(
            issue_identity=package.package_identity,
            accepted_revision_id=int(package.accepted_revision_id),
            approved_at=package.authorized_at,
        )
    )

    try:
        reading = bind_issue_reading(
            session,
            project_id=project_id,
            preparation=preparation,
            source_cutoff=source_cutoff,
            coverage=coverage.lines,
            templates=templates,
            first_issue_behavior=first_issue_behavior,
            prepared_at=prepared_at,
            previous_issue=previous_issue,
            follow_up_plans=follow_up_plans,
        )
        issue_artifacts = read_issue_artifacts(session, reading)
    except MixedIssueInputs as exc:
        raise PreparationRefused(MIXED_READING, str(exc)) from exc

    chase = content.artifact("chase_list")
    follow_up = read_follow_up_bundles(
        session, project_id=project_id, as_of=source_cutoff,
        rule_version=chase.renderer.version if chase else CHASE_LIST_VERSION,
    )

    unread = int(
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.project_id == project_id,
                Document.parse_status != "parsed",
            )
        )
        or 0
    )
    unmet = tuple(
        requirement.statement
        for requirement in content.unmet_coverage(unread_source_count=unread)
    )

    blocked, exceptions = _open_difference_state(
        session,
        project_id=project_id,
        source_cutoff=source_cutoff,
        content=content,
    )
    exceptions = tuple(exceptions) + _coverage_exceptions(coverage) + _adverse(
        follow_up
    )

    return BoundPreparation(
        project_id=project_id,
        accepted_revision_id=reading.accepted_revision_id,
        previous_package_id=previous_package_id,
        source_cutoff=source_cutoff,
        prepared_at=prepared_at,
        coverage=coverage,
        inventory=inventory,
        content=content,
        output_template=output_template,
        output_template_format_id=int(template_row.id),
        field_mapping=field_mapping,
        field_mapping_format_id=int(mapping_row.id),
        binding=bound_binding,
        reading=reading,
        issue_artifacts=issue_artifacts,
        follow_up=follow_up,
        unread_source_count=unread,
        unmet_coverage=unmet,
        blocked_decisions=blocked,
        disclosed_exceptions=exceptions,
        template_bytes_sha256=sha256(template_bytes).hexdigest(),
        report_receipt_id=report_receipt_id,
        report_result_sha256=report_result_sha256,
    )


def _ordered_artifacts(content: EffectiveIssueContent):
    """The configured set in one fixed order: the mandatory UCM, then the rest.

    ADR-0091's mandatory member leads because it is the artifact the slice is
    defined by; the remainder follows by artifact type, which is the order
    #640's reading already returns them in. The order is what the content
    digest binds, so it may not depend on how a row happened to be inserted.
    """

    ucm = [
        resolved
        for resolved in content.artifacts
        if resolved.artifact_type == UPDATED_UCM
    ]
    rest = sorted(
        (
            resolved
            for resolved in content.artifacts
            if resolved.artifact_type != UPDATED_UCM
        ),
        key=lambda resolved: resolved.artifact_type,
    )
    return tuple(ucm + rest)


def _open_difference_state(
    session: Session,
    *,
    project_id: int,
    source_cutoff: datetime,
    content: EffectiveIssueContent,
) -> tuple[tuple[BlockedDecision, ...], tuple[str, ...]]:
    """Which open differences block this issue, and which are disclosed.

    The partition between them is #641's, not a second reading of the profile:
    a difference an executable customer policy selects and nobody has decided
    blocks; every other unresolved difference is an honest adverse condition
    that ADR-0086 requires to be rendered truthfully and never to block.
    """

    reading = read_open_deltas(session, project_id=project_id, as_of=source_cutoff)
    if not reading.open_delta_ids:
        return (), ()
    reasons = {
        standing.delta_id: standing.attention_reasons
        for standing in reading.standings
    }
    rows = session.scalars(
        select(ProposedDelta)
        .where(ProposedDelta.id.in_(tuple(reading.open_delta_ids)))
        .order_by(ProposedDelta.id)
    ).all()
    blocked: list[BlockedDecision] = []
    disclosed: list[str] = []
    for row in rows:
        change = ChangeFacts(
            field=row.target_field,
            change_type=row.change_type,
            attention_reasons=reasons.get(int(row.id), ()),
        )
        policies: tuple[ResolvedBlockingPolicy, ...] = (
            content.blocking_policies_for(change)
        )
        if policies:
            blocked.extend(
                BlockedDecision(
                    delta_id=int(row.id),
                    field=row.target_field,
                    change_type=row.change_type,
                    policy=policy.policy,
                    selector=policy.selector.token,
                    statement=policy.statement,
                )
                for policy in policies
            )
            continue
        stated = content.stating_artifacts(change)
        disclosed.append(
            f"{field_label(row.target_field) if row.target_field else 'A change'} "
            f"is proposed and not decided"
            + (
                ""
                if not stated
                else "; it would change what "
                + ", ".join(ARTIFACT_WORDS.get(name, name) for name in stated)
                + " states"
            )
            + "."
        )
    return tuple(blocked), tuple(disclosed)


def _coverage_exceptions(coverage: CoverageDeclaration) -> tuple[str, ...]:
    return tuple(
        f"{line.source_name} is {line.state} for this issue: {line.detail}."
        for line in coverage.exceptions
    )


def _adverse(follow_up: FollowUpReading) -> tuple[str, ...]:
    """Overdue accepted obligations, in #425's own words and bands.

    Derived from the chase list rather than re-derived here: a second alert
    rule beside #425's would be a second answer to the same question, and the
    two would disagree the first week one of them changed. Only #425's
    ``elapsed`` bands are exceptions — a date that has already passed is an
    adverse condition, and one that has not yet arrived is the project running
    normally, which is a distinction the bands already declare rather than one
    invented here.
    """

    overdue = {band.name for band in follow_up.bands if band.direction == ELAPSED}
    counted: dict[str, int] = {}
    for bundle in follow_up.bundles:
        if bundle.band in overdue:
            counted[bundle.band] = counted.get(bundle.band, 0) + 1
    words = {band.name: band.sentence for band in follow_up.bands}
    return tuple(
        f"{count} accepted follow-up item(s): {words[band]}"
        for band, count in sorted(counted.items())
    )


# --- phase two: render, holding nothing -----------------------------------


def render_candidate_artifacts(
    bound: BoundPreparation,
    *,
    template_bytes: bytes,
    session: Session | None = None,
    store: ObjectStore | None = None,
) -> tuple[RenderedCandidateArtifact, ...]:
    """Render and retain every configured artifact, holding no project lock.

    ``session`` is the render phase's own reading session and is used by
    exactly one invoker — the updated UCM's, because #495's renderer reads and
    renders in one call. It must never be the session that bound the inputs:
    that one is committed and its lock released before this is called.

    The bytes are retained before anything references them, which is #487's
    documented ordering: a crash between the two leaves an unreferenced object
    in the content-addressed store, never a candidate row without its bytes.
    """

    if sha256(template_bytes).hexdigest() != bound.template_bytes_sha256:
        raise PreparationRefused(
            DIGEST_MISMATCH,
            "these are not the template bytes the preparation was bound to",
        )
    backing = store if store is not None else content_store()
    rendered: list[RenderedCandidateArtifact] = []
    for resolved in _ordered_artifacts(bound.content):
        invoker = ARTIFACT_INVOKERS.get(
            (
                resolved.artifact_type,
                resolved.renderer.identity,
                resolved.renderer.version,
            )
        )
        if invoker is None:
            # Unreachable while `supported` gates this, and stated rather than
            # assumed: falling through to "probably the current renderer" is
            # the one thing this module may never do.
            raise PreparationRefused(
                UNSUPPORTED_ISSUE_CONFIGURATION,
                f"{resolved.artifact_type} is configured to be produced by "
                f"{resolved.renderer.identity} {resolved.renderer.version}, "
                "which this release cannot invoke",
            )
        inputs = RenderInputs(
            bound=bound,
            template_bytes=template_bytes,
            session=session if resolved.artifact_type == UPDATED_UCM else None,
        )
        try:
            body = invoker(inputs)
        except PreparationRefused:
            raise
        except Exception as exc:  # a renderer crashed; the set is incomplete
            raise PreparationRefused(
                RENDERER_FAILED,
                f"{ARTIFACT_WORDS.get(resolved.artifact_type, resolved.artifact_type)} "
                f"could not be produced by {resolved.renderer.identity} "
                f"{resolved.renderer.version}: {exc}",
            ) from exc
        if not body:
            raise PreparationRefused(
                ARTIFACT_MISSING,
                f"{ARTIFACT_WORDS.get(resolved.artifact_type, resolved.artifact_type)} "
                "rendered no bytes, so the configured set is incomplete",
            )
        digest = sha256(body).hexdigest()
        key = content_key(digest, ARTIFACT_SUFFIXES[resolved.artifact_type])
        try:
            backing.put(key, body, sha256=digest)
        except Exception as exc:
            raise PreparationRefused(
                STORAGE_FAILED,
                f"the exact bytes of "
                f"{ARTIFACT_WORDS.get(resolved.artifact_type, resolved.artifact_type)} "
                f"could not be retained: {exc}",
            ) from exc
        rendered.append(
            RenderedCandidateArtifact(
                artifact_type=resolved.artifact_type,
                renderer_identity=resolved.renderer.identity,
                renderer_version=resolved.renderer.version,
                content=body,
                sha256=digest,
                storage_key=key,
            )
        )
    produced = {one.artifact_type for one in rendered}
    missing = tuple(sorted(set(bound.artifact_types) - produced))
    if missing:
        raise PreparationRefused(
            ARTIFACT_MISSING,
            "the configured set is incomplete: "
            + ", ".join(ARTIFACT_WORDS.get(name, name) for name in missing),
        )
    return tuple(rendered)


def content_declaration(
    bound: BoundPreparation, rendered: Sequence[RenderedCandidateArtifact]
) -> str:
    """The candidate's content bytes: the input identity plus the artifact set."""

    return json.dumps(
        {
            "schema_version": CONTENT_SCHEMA_VERSION,
            "candidate_identity": bound.candidate_identity,
            "artifacts": [
                {
                    "position": position,
                    "artifact_type": one.artifact_type,
                    "renderer_identity": one.renderer_identity,
                    "renderer_version": one.renderer_version,
                    "content_sha256": one.sha256,
                }
                for position, one in enumerate(rendered, start=1)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# --- phase three: revalidate, then attach ---------------------------------


def attach_candidate(
    session: Session,
    bound: BoundPreparation,
    rendered: Sequence[RenderedCandidateArtifact],
    *,
    prepared_by: HumanPrincipal,
    preparation: Mapping[str, Any],
    templates: TemplateBinding,
    first_issue_behavior: str,
    template_bytes: bytes,
) -> ReleaseCandidate:
    """Re-derive every bound input, then attach the complete set atomically.

    The second transaction is not a shorter version of the first: it repeats
    the whole binding at the same declared cutoff and compares one digest over
    the result. A bound input that moved while the renderers ran therefore
    cannot be missed by a comparison somebody forgot to add, and the refusal it
    produces attaches no candidate at all.
    """

    actor = require_human_principal(prepared_by)
    lock_project(session, bound.project_id)

    rebound = bind_preparation(
        session,
        project_id=bound.project_id,
        preparation=preparation,
        source_cutoff=bound.source_cutoff,
        prepared_at=bound.prepared_at,
        coverage_declaration_id=bound.coverage.declaration_id,
        templates=templates,
        first_issue_behavior=first_issue_behavior,
        template_bytes=template_bytes,
        binding=bound.binding,
        follow_up_plans=bound.reading.follow_up_plans,
        # Re-bound from the candidate itself rather than re-resolved: this
        # transaction repeats the binding, it does not take a second opinion
        # about which retained reading the issue is being prepared from.
        report_receipt_id=bound.report_receipt_id,
        report_result_sha256=bound.report_result_sha256,
    )
    if rebound.state_sha256 != bound.state_sha256:
        raise PreparationRefused(
            INPUTS_CHANGED_WHILE_RENDERING,
            "an input this candidate was bound to changed while its artifacts "
            "were being rendered, so the set no longer describes one coherent "
            "reading; nothing is attached and a fresh preparation is needed",
        )
    newest = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == bound.project_id
        )
    )
    if int(newest or 0) != bound.accepted_revision_id:
        raise PreparationRefused(
            INPUTS_CHANGED_WHILE_RENDERING,
            f"the accepted record moved to revision {newest} while this "
            f"candidate's artifacts were being rendered from revision "
            f"{bound.accepted_revision_id}; nothing is attached",
        )

    declaration = content_declaration(bound, rendered)
    digest = sha256(declaration.encode("utf-8")).hexdigest()
    existing = session.scalars(
        select(ReleaseCandidate).where(
            ReleaseCandidate.project_id == bound.project_id,
            ReleaseCandidate.candidate_identity == bound.candidate_identity,
        )
    ).first()
    if existing is not None:
        if existing.content_sha256 != digest:
            raise PreparationRefused(
                CANDIDATE_IDENTITY_CONFLICT,
                "a candidate prepared from exactly these inputs already holds "
                "different artifact bytes; the same reading must produce the "
                "same issue, so nothing is overwritten and nothing is attached",
            )
        return existing

    ucm = next(one for one in rendered if one.artifact_type == UPDATED_UCM)
    candidate = ReleaseCandidate(
        project_id=bound.project_id,
        candidate_identity=bound.candidate_identity,
        input_declaration=bound.input_declaration,
        input_schema_version=INPUT_SCHEMA_VERSION,
        content_sha256=digest,
        content_declaration=declaration,
        accepted_revision_id=bound.accepted_revision_id,
        previous_package_id=bound.previous_package_id,
        source_cutoff=bound.source_cutoff,
        coverage_declaration_id=bound.coverage.declaration_id,
        coverage_identity=bound.coverage.identity,
        coverage_sha256=bound.coverage.content_sha256,
        issue_profile_id=bound.inventory.profile_id,
        issue_profile_identity=bound.inventory.profile_identity,
        issue_profile_version=bound.inventory.profile_version,
        issue_profile_sha256=bound.inventory.content_sha256,
        output_template_format_id=bound.output_template_format_id,
        field_mapping_format_id=bound.field_mapping_format_id,
        code_revision=bound.binding.code_revision,
        product_revision=bound.binding.product_revision,
        ucm_renderer_identity=ucm.renderer_identity,
        ucm_renderer_version=ucm.renderer_version,
        ucm_content_sha256=ucm.sha256,
        ucm_storage_key=ucm.storage_key,
        ucm_byte_count=ucm.byte_count,
        readiness=bound.readiness,
        prepared_by_principal=actor.subject,
        prepared_at=bound.prepared_at,
    )
    session.add(candidate)
    session.flush()
    for position, one in enumerate(rendered, start=1):
        if one.artifact_type == UPDATED_UCM:
            continue
        session.add(
            ReleaseCandidateArtifact(
                candidate_id=candidate.id,
                project_id=bound.project_id,
                artifact_type=one.artifact_type,
                renderer_identity=one.renderer_identity,
                renderer_version=one.renderer_version,
                content_sha256=one.sha256,
                storage_key=one.storage_key,
                byte_count=one.byte_count,
                position=position,
            )
        )
    session.flush()
    return candidate


def record_refused_preparation(
    session: Session,
    *,
    project_id: int,
    refusal: PreparationRefused,
    refused_by: HumanPrincipal,
    refused_at: datetime,
    candidate_identity: str | None = None,
) -> ReleasePreparationRefusal:
    """The one receipt a failed preparation leaves behind.

    It is written in its own transaction, after whatever the failure rolled
    back, so a refusal is recorded even though no part of the candidate is.
    """

    actor = require_human_principal(refused_by)
    if refused_at.tzinfo is None:
        raise ValueError("a refusal is recorded at a declared, aware instant")
    receipt = ReleasePreparationRefusal(
        project_id=project_id,
        candidate_identity=candidate_identity,
        reason_code=refusal.code,
        reason=refusal.sentence[:2000],
        refused_by_principal=actor.subject,
        refused_at=refused_at,
    )
    session.add(receipt)
    session.flush()
    return receipt


# --- the whole act, in three transactions ---------------------------------


@dataclass(frozen=True, slots=True)
class PreparationOutcome:
    """What one preparation produced: a candidate, or a refusal receipt.

    Exactly one of the two is identified. There is deliberately no third state
    and no partially populated one: a caller holding a ``candidate_id`` has the
    complete configured set, and a caller holding a ``refusal_id`` has no
    candidate row and no artifact row anywhere.

    Identifiers rather than ORM rows, because the three phases each commit and
    close their own session: handing back an object from a closed session would
    make every read of it a detached-instance accident waiting to happen.
    """

    project_id: int
    candidate_id: int | None = None
    candidate_identity: str | None = None
    content_sha256: str | None = None
    refusal_id: int | None = None
    refusal_code: str | None = None
    refusal_reason: str | None = None
    readiness: str | None = None
    blockers: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()

    @property
    def prepared(self) -> bool:
        return self.candidate_id is not None


def prepare_release_candidate(
    sessions: Callable[[], Session],
    *,
    project_id: int,
    prepared_by: HumanPrincipal,
    preparation: Mapping[str, Any],
    source_cutoff: datetime,
    prepared_at: datetime,
    coverage_declaration_id: int,
    templates: TemplateBinding,
    first_issue_behavior: str,
    template_bytes: bytes,
    binding: AnalyticsBinding | None = None,
    follow_up_plans: Sequence[AcceptedFollowUpPlan] = (),
    report_receipt_id: int | None = None,
    report_result_sha256: str | None = None,
    surface: str = "release_preparation",
    store: ObjectStore | None = None,
    on_rendered: Callable[[BoundPreparation], None] | None = None,
) -> PreparationOutcome:
    """Prepare one candidate: bind, render, revalidate, attach.

    ``sessions`` is a factory rather than a session because the three phases
    are three transactions on purpose. The first commits before a byte is
    rendered, so the project lock it took is released; the render phase's own
    session is closed before the third opens; and the third takes the lock
    again, re-derives everything, and either attaches the complete set or
    attaches nothing. No PostgreSQL transaction spans the rendering.

    ``on_rendered`` is a seam for proving exactly that: it runs after the
    render phase's session is closed and before the attach transaction opens.

    Every failure path ends in one refusal receipt with a bounded reason,
    written in its own transaction so that a rolled-back preparation still
    leaves the record of why.
    """

    bound: BoundPreparation | None = None
    try:
        with sessions() as binding_session:
            bound = bind_preparation(
                binding_session,
                project_id=project_id,
                preparation=preparation,
                source_cutoff=source_cutoff,
                prepared_at=prepared_at,
                coverage_declaration_id=coverage_declaration_id,
                templates=templates,
                first_issue_behavior=first_issue_behavior,
                template_bytes=template_bytes,
                binding=binding,
                follow_up_plans=follow_up_plans,
                report_receipt_id=report_receipt_id,
                report_result_sha256=report_result_sha256,
            )
            # Nothing was written; committing is how the project lock is let
            # go before the long work starts.
            binding_session.commit()

        with sessions() as render_session:
            rendered = render_candidate_artifacts(
                bound,
                template_bytes=template_bytes,
                session=render_session,
                store=store,
            )
            render_session.rollback()

        if on_rendered is not None:
            on_rendered(bound)

        with sessions() as attach_session:
            candidate = attach_candidate(
                attach_session,
                bound,
                rendered,
                prepared_by=prepared_by,
                preparation=preparation,
                templates=templates,
                first_issue_behavior=first_issue_behavior,
                template_bytes=template_bytes,
            )
            candidate_id = int(candidate.id)
            identity = candidate.candidate_identity
            content_digest = candidate.content_sha256
            attach_session.commit()
    except PreparationRefused as refusal:
        with sessions() as refusal_session:
            receipt = record_refused_preparation(
                refusal_session,
                project_id=project_id,
                refusal=refusal,
                refused_by=prepared_by,
                refused_at=prepared_at,
                candidate_identity=(
                    None if bound is None else bound.candidate_identity
                ),
            )
            refusal_id = int(receipt.id)
            refusal_session.commit()
        if bound is not None:
            emit_preparation(
                bound,
                principal_subject=prepared_by.subject,
                surface=surface,
                outcome="refused",
                refusal_code=refusal.code,
            )
        return PreparationOutcome(
            project_id=project_id,
            refusal_id=refusal_id,
            refusal_code=refusal.code,
            refusal_reason=refusal.sentence,
        )

    emit_preparation(
        bound,
        principal_subject=prepared_by.subject,
        surface=surface,
        outcome=bound.readiness,
        candidate_identity=identity,
        content_sha256=content_digest,
    )
    return PreparationOutcome(
        project_id=project_id,
        candidate_id=candidate_id,
        candidate_identity=identity,
        content_sha256=content_digest,
        readiness=bound.readiness,
        blockers=bound.blockers,
        exceptions=bound.disclosed_exceptions,
    )


# --- what #533 asks of a prepared candidate -------------------------------


def candidate_is_stale(
    session: Session, candidate: ReleaseCandidate, *, as_of: datetime
) -> tuple[str, ...]:
    """Every reason this candidate no longer describes the project, in order.

    ``prepared_candidate_is_stale`` answers the *profile* part of this and is
    one term here, never the whole contract: an accepted decision after
    preparation, a package authorized after preparation, and a replaced output
    template or field mapping each make a candidate stale without the profile
    moving at all.
    """

    package = latest_authorized_package(session, int(candidate.project_id))
    return candidate_staleness_reasons(
        candidate,
        inventory=effective_issue_inventory(
            session, int(candidate.project_id), as_of
        ),
        newest_revision_id=int(
            session.scalar(
                select(func.max(ProjectRecordRevision.id)).where(
                    ProjectRecordRevision.project_id == candidate.project_id
                )
            )
            or 0
        ),
        current_package_id=None if package is None else int(package.id),
        formats=effective_baseline_formats(session, int(candidate.project_id)),
    )


def candidate_staleness_reasons(
    candidate: ReleaseCandidate,
    *,
    inventory: IssueInventory | None,
    newest_revision_id: int,
    current_package_id: int | None,
    formats: Mapping[str, Any],
) -> tuple[str, ...]:
    """The staleness rule itself, over inputs the caller has already read.

    ``candidate_is_stale`` above is this function plus the four reads it
    needs, and it is the only reason this one is separate: a cross-project
    reading (#537, #636) has those four facts loaded for every project it
    shows and must not go back to the database once per candidate to re-ask
    them. Two authorities over one staleness rule is the failure #641 exists to
    prevent, so there is exactly one and this is it.

    The replaced output template or field mapping is the fourth term, and it
    was the one missing (#829). A coordinator may register a replacement
    between preparation and release, and the sealed artifacts are then not the
    ones the project produces; ``release_authorization`` refused that at the
    authorization itself while ``authorization_blockers`` — which is what the
    Issue section offers the act on — did not know about it, so the section
    offered an approval #533 would refuse. It is one term of one rule now,
    and ``release_authorization.candidate_format_differences`` is this term
    named on its own for the revalidation that raises on it.
    """

    reasons: list[str] = []
    if prepared_candidate_is_stale(
        inventory,
        profile_id=int(candidate.issue_profile_id),
        profile_version=int(candidate.issue_profile_version),
    ):
        reasons.append(
            "what this project is configured to externally issue changed after "
            "this candidate was prepared"
        )
    if newest_revision_id != int(candidate.accepted_revision_id):
        reasons.append(
            f"the accepted record moved to revision {newest_revision_id} after "
            f"this candidate was prepared from revision "
            f"{candidate.accepted_revision_id}"
        )
    if current_package_id != (
        None
        if candidate.previous_package_id is None
        else int(candidate.previous_package_id)
    ):
        reasons.append(
            "a package was authorized after this candidate was prepared, so "
            "its comparison baseline is no longer the current one"
        )
    reasons.extend(replaced_format_reasons(candidate, formats))
    return tuple(reasons)


def replaced_format_reasons(
    candidate: ReleaseCandidate, formats: Mapping[str, Any]
) -> tuple[str, ...]:
    """Whether the template and mapping a candidate was rendered through still hold.

    Stated once, here, because #533 raises on it and the Issue section offers
    an act on it, and those two answering differently is how a screen offers an
    approval the authorization refuses.
    ``release_authorization.candidate_format_differences`` is this function
    under the name revalidation already called it.
    """

    replaced: list[str] = []
    for kind, format_id, what in (
        ("output_template", candidate.output_template_format_id, "output template"),
        ("field_mapping", candidate.field_mapping_format_id, "field mapping"),
    ):
        registered = formats.get(kind)
        if registered is None or int(registered.id) != int(format_id):
            replaced.append(
                f"the {what} this project renders through was replaced after "
                "this candidate was prepared, so the sealed artifacts would "
                "not be the ones this project now produces. Nothing is "
                "released; prepare a fresh candidate."
            )
    return tuple(replaced)


def authorization_blockers(
    session: Session, candidate: ReleaseCandidate, *, as_of: datetime
) -> tuple[str, ...]:
    """Why #533 may not authorize this candidate. Empty means it may.

    A ``blocked`` candidate is complete — every configured artifact was
    rendered and retained — and still unauthorizable, and the only way past it
    is a **newly prepared** candidate: what blocks is the bound coverage or
    decision state, and that state is part of the candidate's own identity, so
    a candidate cannot be unblocked in place any more than it can be edited.
    """

    reasons: list[str] = []
    if candidate.readiness == BLOCKED:
        reasons.append(
            "this candidate is blocked: it was prepared against unmet coverage "
            "or an undecided difference an explicit customer policy waits on. "
            "Clearing it needs a newly prepared candidate, because the state "
            "that blocks it is bound into this one's identity."
        )
    reasons.extend(candidate_is_stale(session, candidate, as_of=as_of))
    return tuple(reasons)


def current_release_candidate(
    session: Session, project_id: int
) -> ReleaseCandidate | None:
    """The candidate this project's coordinator is being asked about, or none.

    The single-project sibling of ``current_candidates_by_project``, which it
    delegates to so the Issue section and the portfolio row cannot name
    different candidates for one project. That is not a stylistic preference:
    the two were written in parallel and ordered differently — this one by the
    declared ``prepared_at``, that one by the append-only identifier — and a
    caller-declared instant can attach a superseded candidate last (#634).
    The append-only order is the one that survives that, so it is the only one.

    It is deliberately not "the newest candidate that could be authorized".
    Skipping a blocked or stale candidate to reach an older authorizable one
    would present a superseded reading of the project as the current one, and
    the whole point of ADR-0086's blocked class is that clearing it needs a
    newly prepared candidate rather than an older one quietly standing in.
    """

    return current_candidates_by_project(session, (project_id,))[project_id]


def candidate_artifacts(
    session: Session, candidate: ReleaseCandidate
) -> tuple[ReleaseCandidateArtifact, ...]:
    """The configured remainder, in the order the content digest binds them."""

    return tuple(
        session.scalars(
            select(ReleaseCandidateArtifact)
            .where(ReleaseCandidateArtifact.candidate_id == candidate.id)
            .order_by(ReleaseCandidateArtifact.position)
        ).all()
    )


# --- the emission (#558) --------------------------------------------------


def emit_preparation(
    bound: BoundPreparation,
    *,
    principal_subject: str,
    surface: str,
    outcome: str,
    candidate_identity: str | None = None,
    content_sha256: str | None = None,
    refusal_code: str | None = None,
) -> None:
    """Record that one preparation happened, and what it produced.

    The binding travels with it, so the code and product revision, the
    packetizer rule version, the source and connector configuration, the
    template and mapping identities and the enabled feature flags are all
    bound to the measurement, exactly as #558 requires.
    """

    emit_event(
        release_candidate_preparation_event(
            bound.binding,
            occurred_at=bound.prepared_at,
            # Bounded shape only: the surface and outcome become the labels;
            # the project and the person stay in the payload (#491, #522).
            surface=surface,
            outcome=outcome,
            principal_subject=principal_subject,
            project_id=bound.project_id,
            source_cutoff=bound.source_cutoff.isoformat(),
            accepted_revision_id=bound.accepted_revision_id,
            previous_package_id=bound.previous_package_id,
            issue_profile_id=bound.inventory.profile_id,
            issue_profile_version=bound.inventory.profile_version,
            coverage_identity=bound.coverage.identity,
            configured_artifact_types=list(bound.artifact_types),
            candidate_identity=candidate_identity,
            content_sha256=content_sha256,
            readiness=bound.readiness,
            blocker_count=len(bound.blockers),
            exception_count=len(bound.disclosed_exceptions),
            refusal_code=refusal_code,
        )
    )
