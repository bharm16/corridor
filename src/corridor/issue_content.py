"""What a project's configured issue actually contains, and what blocks it (#641).

#640 modelled the per-project issue profile ADR-0091 called for: which artifact
types a project externally issues, the identity and version of the renderer that
produces each, the coverage requirements, and the customer's own
decision-blocking policies. It stores *what* a project issues. It deliberately
answers none of these:

- would this proposed difference change anything the configured issue states?
- does customer policy require this difference to be decided first?
- is the configured coverage requirement met?
- which renderer does a release candidate invoke, and is it one we support?

This module is the one place those are answered. #641 derives ADR-0085's three
visible consequence levels from it and #529 prepares a release candidate from
it; if each answered them separately the repository would grow two
interpretations of one configuration, which is the duplicated authority this
codebase has spent #492, #494 and #526 removing.

**Renderer contracts are registered, not inferred.** ``CONTENT_REGISTRY`` is
keyed by ``(artifact_type, renderer_identity, renderer_version)`` and is *pure*
— code, not a table. A renderer's content is a property of the renderer, not of
any customer's configuration, so a per-project row declaring it would be a
second place to get it wrong and a migration to keep in step with every release
of the renderer. An identity or version with no entry is an **invalid
issue-profile configuration**, reported as such. It is never resolved to "near
enough the current renderer": a package prepared from a guess about what a
renderer publishes is a package nobody can audit, and a consequence level
derived from one is ADR-0010's invented severity wearing a heading.

**A profile statement is never parsed for behaviour.** The declaration carries
three different kinds of string and this module keeps them apart:

``policy`` / ``requirement``  a versioned machine evaluator identity;
``required_decision``         a typed machine selector;
``statement``                 human-readable explanation, and nothing else.

``DecisionBlockingPolicy(policy="resolve_before_issue:v1",
required_decision="field:committed_date", statement="Promised For changes must
be decided before this issue.")`` is executable. The same policy with
``required_decision="Utility owner sign-off recorded"`` is not, and matching
that prose against a Proposed Delta — exactly, fuzzily, or by any string
distance at all — would let a sentence a coordinator typed decide which changes
block a customer's issue. So an unrecognised evaluator or selector produces a
configuration problem that Issue readiness prints, and the levels are not
derived at all. It does **not** classify every packet "Must handle before this
issue": a blanket block asserts a customer rule nobody configured, and a
coordinator who is told everything is urgent is told nothing.

**A canonical field is the field a Proposed Delta targets**, which is the
vocabulary ``delta_generation`` compares and the record stores. It is not the
label a screen prints: ``promised_for`` is ``presentation.label``'s wording for
``committed_date``, so ``field:promised_for`` selects nothing and says so, and
``field:committed_date`` is the executable form. Accepting the label as an alias
would be prose parsing with a lookup table in front of it.

**The label appears in the diagnostic and nowhere else (#670).** A coordinator
who hand-authored ``field:promised_for`` is told which canonical field to
configure and which label it is shown under, because "not a decision selector"
alone leaves them guessing at a vocabulary they cannot see. That naming is
*display text*. ``parse_selector`` never consults it, no alternate selector is
written back into the declaration or its digest, nothing retries with the
suggestion, and no callable here returns the canonical field a label names —
``_selector_guidance`` returns a finished sentence, so the only thing that can
escape this module is prose. An alias table that is merely one step further
from the evaluator is still an alias table, and it would drift from field
identity exactly as a direct alias would.

Composing a selector is therefore the caller's one supported path:
``decision_selector`` proves the parts against the same ``parse_selector`` the
evaluator uses, so a configuration screen stores its ``token`` and displays
``presentation.field_label`` beside it. The label is what a person reads; the
token is what is stored, digested and matched, and the two never swap places.

**No clock.** Everything here is derived from an ``IssueInventory`` the caller
already read as at a declared cutoff (#640). This module never reads the wall
clock and never opens a second reading of the profile.

Terminology: no customer-facing label is coined. ``Issue Profile``, ``Review
Packet`` and ``ReleasePackage`` remain internal technical names, and the three
visible levels are ADR-0085's own accepted words, kept in
``consequence_levels``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.baseline_adoption import effective_baseline_formats_by_project
from corridor.delta_generation import COMPARABLE_FACT_TYPES
from corridor.field_mapping_manifest import manifest_from_declaration
from corridor.models import BaselineFormatManifest
from corridor.presentation import field_label
from corridor.issue_profile import (
    IssueInventory,
    RendererRevision,
    UPDATED_UCM,
)
from corridor.review_packet_reading import CONSEQUENCE_BANDS


# The shape of the answers below. A consumer records it beside what it did, so
# a later change to what a contract declares is visible in the receipt rather
# than applied retroactively to one already written.
CONTENT_CONTRACT_VERSION = "issue-content-v1"

# --- the machine vocabulary the profile's own strings are read in ----------
#
# Each identity carries its own version because the rule it names is what
# changes: a customer whose policy was evaluated under v1 did not agree to v2.

# ADR-0086's "required coverage" gate, as one evaluator.
COVERAGE_ALL_REQUIRED_SOURCES_READ = "all_required_sources_read:v1"
SUPPORTED_COVERAGE_EVALUATORS = frozenset({COVERAGE_ALL_REQUIRED_SOURCES_READ})

# ADR-0086's "explicit customer policy" gate, as one evaluator.
RESOLVE_BEFORE_ISSUE = "resolve_before_issue:v1"
SUPPORTED_DECISION_POLICIES = frozenset({RESOLVE_BEFORE_ISSUE})

# The typed selectors a decision policy may name, and nothing else.
SELECT_FIELD = "field"
SELECT_DIFFERENCE = "difference"
SELECT_REASON = "reason"
SELECT_ALL_ISSUE_AFFECTING = "all_issue_affecting"
SELECTOR_KINDS = (
    SELECT_FIELD,
    SELECT_DIFFERENCE,
    SELECT_REASON,
    SELECT_ALL_ISSUE_AFFECTING,
)

# The three vocabularies a selector's value is proved against. Each is read
# back from the module that owns it rather than respelled, so a field, a
# difference kind or an Attention Reason that does not exist cannot be
# configured and then quietly select nothing.
CANONICAL_FIELDS = frozenset(COMPARABLE_FACT_TYPES)
DIFFERENCE_KINDS = frozenset({"add", "modify", "apparent_removal"})
ATTENTION_REASONS = frozenset(band.name for band in CONSEQUENCE_BANDS)

# What a screen already prints for each canonical field, read back from the one
# module that owns product wording rather than respelled here. It is consulted
# only to write a diagnostic sentence (#670); the evaluator never sees it.
_PRESENTATION_LABELS: Mapping[str, str] = {
    field: field_label(field) for field in sorted(CANONICAL_FIELDS)
}


# --- what one renderer publishes -------------------------------------------
#
# The identities and versions below are the ones the producing modules record
# on their own output. They are spelled here rather than imported because
# ``follow_up_bundles`` imports ``project_workflow``, which imports
# ``packet_review``, which imports this module: importing the constants would
# close that cycle. ``test_issue_content`` asserts every one of them still
# equals the producing module's own constant, so there is exactly one authority
# and a rename cannot pass unnoticed.
UCM_RENDERER_IDENTITY = "workbook_render"
UCM_RENDERER_VERSION = "workbook_render_v1"
CHANGE_SUMMARY_IDENTITY = "change_summary_from_accepted_revisions"
CHANGE_SUMMARY_VERSION = "v1"
WEEKLY_REPORT_IDENTITY = "weekly_coordination_report_from_accepted_revision"
WEEKLY_REPORT_VERSION = "v1"
CHASE_LIST_IDENTITY = "follow_up_bundles_from_accepted_authority"
CHASE_LIST_VERSION = "v1"

# ``None`` in ``accepted_record_fields`` means "the fields the project's own
# registered field-mapping manifest targets". Only the customer's native
# workbook is shaped that way, because only it renders through their form.
FROM_FIELD_MAPPING = None


@dataclass(frozen=True, slots=True)
class RendererContract:
    """What one registered renderer revision publishes, declared once.

    Every field answers a question a consumer would otherwise guess. A
    contract that does not publish something says so with an empty set rather
    than by omitting the field, because "declares nothing here" and "nobody
    thought about it" must not look the same.
    """

    artifact_type: str
    renderer_identity: str
    renderer_version: str
    # The accepted Project Record fields this artifact states. ``None`` means
    # the registered field-mapping manifest decides, per project.
    accepted_record_fields: frozenset[str] | None
    # Whether the artifact carries accepted-change content, and which kinds of
    # difference reach it once accepted.
    publishes_accepted_changes: bool
    change_families: frozenset[str]
    # Whether the artifact discloses an open question or pending coordination
    # to the customer, so an *undecided* difference is already visible in it.
    discloses_open_questions: bool
    # The template-controlled regions that narrow the artifact further, and the
    # released check rules it runs, both as the producing module declares them.
    template_sections: tuple[str, ...]
    constraint_alert_rules: tuple[str, ...]
    # Whether accepted Follow-up Plan content is published, and whether
    # provenance or redline content is.
    publishes_follow_up_plans: bool
    publishes_provenance: bool
    statement: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.artifact_type, self.renderer_identity, self.renderer_version)


CONTENT_REGISTRY: tuple[RendererContract, ...] = (
    RendererContract(
        artifact_type=UPDATED_UCM,
        renderer_identity=UCM_RENDERER_IDENTITY,
        renderer_version=UCM_RENDERER_VERSION,
        accepted_record_fields=FROM_FIELD_MAPPING,
        publishes_accepted_changes=False,
        change_families=frozenset({"add", "modify", "apparent_removal"}),
        discloses_open_questions=False,
        template_sections=(),
        constraint_alert_rules=(),
        publishes_follow_up_plans=False,
        publishes_provenance=True,
        statement=(
            "the customer's own workbook, carrying the accepted value of every "
            "field their registered field mapping targets"
        ),
    ),
    RendererContract(
        artifact_type="accepted_change_summary",
        renderer_identity=CHANGE_SUMMARY_IDENTITY,
        renderer_version=CHANGE_SUMMARY_VERSION,
        accepted_record_fields=frozenset(COMPARABLE_FACT_TYPES),
        publishes_accepted_changes=True,
        change_families=frozenset({"add", "modify", "apparent_removal"}),
        discloses_open_questions=False,
        template_sections=(),
        constraint_alert_rules=(),
        publishes_follow_up_plans=False,
        publishes_provenance=True,
        statement=(
            "what the accepted record changed since the previous approved "
            "issue, for every comparable field"
        ),
    ),
    RendererContract(
        artifact_type="weekly_coordination_report",
        renderer_identity=WEEKLY_REPORT_IDENTITY,
        renderer_version=WEEKLY_REPORT_VERSION,
        accepted_record_fields=frozenset(
            {
                "committed_date",
                "action_due_date",
                "need_date",
                "external_org",
                "external_org_contact",
            }
        ),
        publishes_accepted_changes=True,
        change_families=frozenset({"add", "modify", "apparent_removal"}),
        # Its pending-coordination region states the questions still open at
        # the cutoff, so a difference nobody has decided is already in front of
        # the customer.
        discloses_open_questions=True,
        template_sections=(
            "constraint_alerts",
            "commitments",
            "key_dates",
            "follow_up_plans",
            "pending_coordination",
        ),
        constraint_alert_rules=(
            "OVERDUE",
            "DUE_SOON",
            "MISSING_DATE",
            "MISSING_EVIDENCE",
            "SUPERSEDED_CITATION",
        ),
        publishes_follow_up_plans=True,
        publishes_provenance=True,
        statement=(
            "the weekly report's declared regions: commitments, key dates, "
            "Constraint Alerts, accepted Follow-up Plans, and the coordination "
            "still pending at this cutoff"
        ),
    ),
    RendererContract(
        artifact_type="chase_list",
        renderer_identity=CHASE_LIST_IDENTITY,
        renderer_version=CHASE_LIST_VERSION,
        # ADR-0091 and #425: the chase list is derived from accepted authority.
        # An unresolved Proposed Delta is not an external ask (ADR-0084) and
        # never enters it, which is why no record field reaches it directly.
        accepted_record_fields=frozenset(),
        publishes_accepted_changes=False,
        change_families=frozenset(),
        discloses_open_questions=False,
        template_sections=(),
        constraint_alert_rules=(),
        publishes_follow_up_plans=True,
        publishes_provenance=False,
        statement=(
            "accepted Follow-up Plans and the bundles derived from them; an "
            "unresolved proposed change alone never enters it"
        ),
    ),
)

# What each configured artifact is called in a sentence a coordinator reads.
# The words are ADR-0091's own table, not a new label: the ADR already names
# each member of the configured set in prose, and reusing that wording keeps
# `docs/agents/domain.md`'s terminology procedure untriggered.
ARTIFACT_WORDS: Mapping[str, str] = {
    UPDATED_UCM: "the customer's updated UCM workbook",
    "accepted_change_summary": "the accepted-change summary",
    "weekly_coordination_report": "the weekly Coordination Report",
    "chase_list": "the chase list",
    "provenance_sidecar": "the provenance sidecar",
}


_BY_KEY: Mapping[tuple[str, str, str], RendererContract] = {
    contract.key: contract for contract in CONTENT_REGISTRY
}


def registered_contract(
    artifact_type: str, renderer: RendererRevision
) -> RendererContract | None:
    """The contract for one configured artifact, or ``None`` where none exists.

    ``None`` is the whole answer for an unknown identity *and* for a known
    identity at an unknown version. A renderer version is not a decoration on
    an identity: it is what decides which fields and sections the artifact
    carries, so falling back to another version of the same identity would
    publish one thing and claim another.
    """

    return _BY_KEY.get((artifact_type, renderer.identity, renderer.version))


# --- configuration problems ------------------------------------------------

NO_ISSUE_PROFILE = "no_issue_profile"
UNREGISTERED_RENDERER = "unregistered_renderer"
UNRESOLVED_FIELD_MAPPING = "unresolved_field_mapping"
UNSUPPORTED_COVERAGE_EVALUATOR = "unsupported_coverage_evaluator"
UNSUPPORTED_DECISION_POLICY = "unsupported_decision_policy"
UNSUPPORTED_DECISION_SELECTOR = "unsupported_decision_selector"


@dataclass(frozen=True, slots=True)
class ConfigurationProblem:
    """One reason this project's issue profile cannot be executed as written."""

    code: str
    sentence: str


# --- the typed selector ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionSelector:
    """Which proposed differences one customer policy waits on.

    It is a token and a value proved against a closed vocabulary, never a
    phrase compared against a delta. ``selects`` therefore answers from stored
    identities alone and gives the same answer every time it is asked.
    """

    kind: str
    value: str | None = None

    @property
    def token(self) -> str:
        return self.kind if self.value is None else f"{self.kind}:{self.value}"

    def selects(self, change: "ChangeFacts", *, issue_affecting: bool) -> bool:
        if self.kind == SELECT_ALL_ISSUE_AFFECTING:
            return issue_affecting
        if self.kind == SELECT_FIELD:
            return change.field == self.value
        if self.kind == SELECT_DIFFERENCE:
            return change.change_type == self.value
        if self.kind == SELECT_REASON:
            return self.value in change.attention_reasons
        return False


@dataclass(frozen=True, slots=True)
class ChangeFacts:
    """The stored identities a content or policy question is answered from.

    Deliberately not a ``ProposedDelta``: #529 asks these questions about a
    candidate's content and #641 about a Review child, and neither should have
    to hand this module an ORM row to get an answer.
    """

    field: str | None
    change_type: str
    attention_reasons: tuple[str, ...] = ()


def parse_selector(required_decision: str) -> DecisionSelector | None:
    """The typed selector one ``required_decision`` names, or ``None``.

    ``None`` for anything that is not one — including well-formed prose. There
    is deliberately no free-text branch: a sentence is a ``statement``, and a
    statement explains a policy to a person rather than executing it.
    """

    text = (required_decision or "").strip()
    if text == SELECT_ALL_ISSUE_AFFECTING:
        return DecisionSelector(SELECT_ALL_ISSUE_AFFECTING)
    kind, separator, value = text.partition(":")
    if not separator or not value:
        return None
    permitted = {
        SELECT_FIELD: CANONICAL_FIELDS,
        SELECT_DIFFERENCE: DIFFERENCE_KINDS,
        SELECT_REASON: ATTENTION_REASONS,
    }.get(kind)
    if permitted is None or value not in permitted:
        return None
    return DecisionSelector(kind, value)


class UnsupportedSelector(ValueError):
    """A caller composed a selector this release cannot execute."""


def decision_selector(kind: str, value: str | None = None) -> DecisionSelector:
    """The selector a configuration caller means, proved before it is stored.

    A caller composing a ``DecisionBlockingPolicy`` writes
    ``decision_selector(SELECT_FIELD, "committed_date").token`` into
    ``required_decision`` rather than spelling the token by hand, so a profile
    cannot be registered against a string that was never in the vocabulary and
    then fail closed a week later at the moment it was supposed to block.

    It is deliberately ``parse_selector`` run over the composed token rather
    than a second vocabulary check. The helper and the evaluator therefore
    cannot disagree about what is executable: anything this returns is
    something ``parse_selector`` already accepts, and widening one would widen
    both rather than leaving a token a configuration screen offers and the
    evaluator refuses.
    """

    composed = kind if value is None else f"{kind}:{value}"
    selector = parse_selector(composed)
    if selector is None:
        raise UnsupportedSelector(
            f"{composed!r} is not a decision selector this release can match "
            "against a proposed change. "
            f"{_selector_guidance(composed)}"
            "A selector is field:<canonical field>, difference:<kind>, "
            "reason:<attention reason>, or all_issue_affecting."
        )
    return selector


def _selector_guidance(required_decision: str) -> str:
    """Display-only help for a ``field:`` selector naming no canonical field.

    Returns a finished sentence, or ``""`` where there is nothing exact to say.
    It returns *text*: never a selector, never a field, and nothing it produces
    is parsed, stored or retried. That is the whole reason it is shaped this
    way — a function handing a caller back the canonical field a label names
    would be the alias table this module refuses, one step removed.

    The match is exact equality on a normalized spelling of both sides, not a
    resemblance. There is no edit distance, no prefix or substring test and no
    token overlap, so a near miss such as ``field:promissed_for`` is told only
    that it names no canonical field.
    """

    kind, separator, value = (required_decision or "").strip().partition(":")
    if kind != SELECT_FIELD or not separator or not value:
        return ""
    candidates = _fields_shown_as(value, _PRESENTATION_LABELS)
    if len(candidates) == 1:
        field = candidates[0]
        return (
            f"There is no canonical field {value!r}; use "
            f"{SELECT_FIELD}:{field}, shown as {_PRESENTATION_LABELS[field]}. "
        )
    if candidates:
        listed = ", ".join(f"{SELECT_FIELD}:{field}" for field in candidates)
        return (
            f"There is no canonical field {value!r}, and more than one canonical "
            f"field is shown by that name, so none is named here; the supported "
            f"selectors are {listed}. "
        )
    return ""


def _fields_shown_as(text: str, labels: Mapping[str, str]) -> tuple[str, ...]:
    """Every canonical field in ``labels`` a coordinator would read as ``text``.

    Sorted, and returned whole. An ambiguous label yields more than one member
    and the caller must not pick between them: a diagnostic that guessed would
    send a coordinator to configure a field they did not mean, which is worse
    than the vocabulary list they already had.
    """

    wanted = _shown_key(text)
    return tuple(
        sorted(
            field for field, shown in labels.items() if _shown_key(shown) == wanted
        )
    )


def _shown_key(text: str) -> str:
    """One spelling for comparing a written value against a printed label.

    Case, underscores and runs of whitespace are the three ways the same label
    is written down; nothing else is folded away.
    """

    return " ".join(text.replace("_", " ").split()).casefold()


# --- what the reading returns ----------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """One configured artifact, its renderer, and the content it publishes."""

    artifact_type: str
    renderer: RendererRevision
    contract: RendererContract
    accepted_record_fields: frozenset[str]

    def states(self, change: ChangeFacts) -> bool:
        """Whether accepting or editing this difference changes what it states."""

        if change.change_type not in self.contract.change_families:
            return False
        if change.field is None:
            return False
        return change.field in self.accepted_record_fields

    def discloses(self, change: ChangeFacts) -> bool:
        """Whether it already discloses this difference while it stays open."""

        return self.contract.discloses_open_questions


@dataclass(frozen=True, slots=True)
class ResolvedCoverageRequirement:
    """One configured coverage requirement, bound to the evaluator that runs it."""

    evaluator: str
    statement: str


@dataclass(frozen=True, slots=True)
class ResolvedBlockingPolicy:
    """One executable customer rule: an evaluator, a selector, and its words."""

    policy: str
    selector: DecisionSelector
    statement: str


@dataclass(frozen=True, slots=True)
class EffectiveIssueContent:
    """Everything a consumer needs to know about one project's configured issue.

    This is the reading #641 derives consequence levels from and #529 prepares
    a release candidate from, unchanged. It carries the profile identity a
    candidate binds itself to, the renderers it must invoke with the contracts
    that say what they publish, the coverage evaluators to run, the executable
    blocking policies, and every reason the configuration cannot be executed.

    ``supported`` is the one gate. A consumer that finds it false states the
    problems and does no derivation: not a partial one, not a conservative one.
    """

    profile_id: int | None
    profile_identity: str | None
    profile_version: int | None
    content_sha256: str | None
    contract_version: str
    artifacts: tuple[ResolvedArtifact, ...]
    coverage_requirements: tuple[ResolvedCoverageRequirement, ...]
    blocking_policies: tuple[ResolvedBlockingPolicy, ...]
    problems: tuple[ConfigurationProblem, ...]

    @property
    def supported(self) -> bool:
        """Whether every configured renderer, evaluator and selector is known."""

        return not self.problems

    @property
    def artifact_types(self) -> tuple[str, ...]:
        return tuple(artifact.artifact_type for artifact in self.artifacts)

    def artifact(self, artifact_type: str) -> ResolvedArtifact | None:
        for resolved in self.artifacts:
            if resolved.artifact_type == artifact_type:
                return resolved
        return None

    def stating_artifacts(self, change: ChangeFacts) -> tuple[str, ...]:
        """Which configured artifacts this difference would change once accepted."""

        return tuple(
            resolved.artifact_type
            for resolved in self.artifacts
            if resolved.states(change)
        )

    def disclosing_artifacts(self, change: ChangeFacts) -> tuple[str, ...]:
        """Which configured artifacts disclose it while it is still open."""

        return tuple(
            resolved.artifact_type
            for resolved in self.artifacts
            if resolved.discloses(change)
        )

    def affects_issue(self, change: ChangeFacts) -> bool:
        return bool(
            self.stating_artifacts(change) or self.disclosing_artifacts(change)
        )

    def blocking_policies_for(
        self, change: ChangeFacts
    ) -> tuple[ResolvedBlockingPolicy, ...]:
        """Every executable customer policy that waits on this difference."""

        affecting = self.affects_issue(change)
        return tuple(
            policy
            for policy in self.blocking_policies
            if policy.selector.selects(change, issue_affecting=affecting)
        )

    def unmet_coverage(
        self, *, unread_source_count: int
    ) -> tuple[ResolvedCoverageRequirement, ...]:
        """Which configured coverage requirements this project does not meet.

        The only released evaluator asks whether every delivered source was
        read, which is the same fact ``project_workflow.issue_readiness``
        already prints one line per unread document for. It is answered here so
        that #529 can refuse a candidate on ADR-0086's coverage gate without
        re-deciding what coverage means, and the Review surfaces deliberately
        print no second sentence saying what the unread-source lines already
        say.
        """

        if unread_source_count <= 0:
            return ()
        return tuple(
            requirement
            for requirement in self.coverage_requirements
            if requirement.evaluator == COVERAGE_ALL_REQUIRED_SOURCES_READ
        )


def effective_issue_content(
    session: Session, inventory: IssueInventory | None
) -> EffectiveIssueContent:
    """Resolve one project's configured issue into executable content and policy.

    ``inventory`` is #640's reading, already taken as at the caller's declared
    cutoff, so this function adds no cutoff of its own and cannot disagree with
    the one the caller used.

    ``None`` — no profile had taken effect by that cutoff — is a configuration
    problem and not an empty issue. ADR-0091 makes the updated UCM mandatory,
    so "this project issues nothing" is not a state a configured project can be
    in, and treating an unconfigured project as one issuing nothing would
    quietly classify every proposed change "Can wait".
    """

    if inventory is None:
        return _unconfigured()
    return effective_issue_contents(session, {inventory.project_id: inventory})[
        inventory.project_id
    ]


def effective_issue_contents(
    session: Session, inventories: Mapping[int, IssueInventory | None]
) -> dict[int, EffectiveIssueContent]:
    """``effective_issue_content`` for several projects, in two statements.

    The cross-project reading (#537) may not ask this once per project, and it
    may not answer it by a second rule either, so the single-project reader
    above is this function over one project. Only the registered field mapping
    needs the database at all — every other answer is in the registry — so the
    whole batch costs the same two statements one project would.
    """

    mapped = _mapped_fields_by_project(session, inventories)
    return {
        project_id: (
            _unconfigured()
            if inventory is None
            else _resolve(inventory, mapped.get(project_id))
        )
        for project_id, inventory in inventories.items()
    }


def _unconfigured() -> EffectiveIssueContent:
    return EffectiveIssueContent(
        profile_id=None,
        profile_identity=None,
        profile_version=None,
        content_sha256=None,
        contract_version=CONTENT_CONTRACT_VERSION,
        artifacts=(),
        coverage_requirements=(),
        blocking_policies=(),
        problems=(
            ConfigurationProblem(
                NO_ISSUE_PROFILE,
                "No issue profile had taken effect at this cutoff, so what this "
                "project externally issues is not configured.",
            ),
        ),
    )


def _resolve(
    inventory: IssueInventory, mapped_fields: frozenset[str] | None
) -> EffectiveIssueContent:
    """Turn one stored inventory into contracts, evaluators, and problems."""

    problems: list[ConfigurationProblem] = []
    artifacts: list[ResolvedArtifact] = []
    for entry in inventory.artifacts:
        contract = registered_contract(entry.artifact_type, entry.renderer)
        if contract is None:
            problems.append(
                ConfigurationProblem(
                    UNREGISTERED_RENDERER,
                    f"The {entry.artifact_type} is configured to be produced by "
                    f"{entry.renderer.identity} {entry.renderer.version}, which "
                    "is not a renderer revision this release supports, so what "
                    "that artifact would contain is not known.",
                )
            )
            continue
        if contract.accepted_record_fields is FROM_FIELD_MAPPING:
            if mapped_fields is None:
                problems.append(
                    ConfigurationProblem(
                        UNRESOLVED_FIELD_MAPPING,
                        f"The {entry.artifact_type} states the fields its "
                        "registered field mapping targets, and the mapping "
                        "revision this profile names cannot be read back for "
                        "this project, so which fields it carries is not known."
                    )
                )
                continue
            fields = mapped_fields
        else:
            fields = contract.accepted_record_fields
        artifacts.append(
            ResolvedArtifact(
                artifact_type=entry.artifact_type,
                renderer=entry.renderer,
                contract=contract,
                accepted_record_fields=fields,
            )
        )

    coverage: list[ResolvedCoverageRequirement] = []
    for requirement in inventory.coverage_requirements:
        if requirement.requirement not in SUPPORTED_COVERAGE_EVALUATORS:
            problems.append(
                ConfigurationProblem(
                    UNSUPPORTED_COVERAGE_EVALUATOR,
                    f"The coverage requirement {requirement.requirement!r} names "
                    "no coverage evaluator this release runs, so whether it is "
                    "met cannot be decided. Its recorded explanation is wording "
                    "for a person and is never read as a rule.",
                )
            )
            continue
        coverage.append(
            ResolvedCoverageRequirement(
                evaluator=requirement.requirement, statement=requirement.statement
            )
        )

    policies: list[ResolvedBlockingPolicy] = []
    for policy in inventory.decision_blocking_policies:
        if policy.policy not in SUPPORTED_DECISION_POLICIES:
            problems.append(
                ConfigurationProblem(
                    UNSUPPORTED_DECISION_POLICY,
                    f"The decision-blocking policy {policy.policy!r} names no "
                    "policy evaluator this release runs, so nothing can be "
                    "blocked on it. Its recorded explanation is wording for a "
                    "person and is never read as a rule.",
                )
            )
            continue
        selector = parse_selector(policy.required_decision)
        if selector is None:
            problems.append(
                ConfigurationProblem(
                    UNSUPPORTED_DECISION_SELECTOR,
                    f"The policy {policy.policy!r} waits on "
                    f"{policy.required_decision!r}, which is not a decision "
                    "selector this release can match against a proposed change. "
                    f"{_selector_guidance(policy.required_decision)}"
                    "A selector is field:<canonical field>, difference:<kind>, "
                    "reason:<attention reason>, or all_issue_affecting; a "
                    "sentence explains a policy to a person and is never "
                    "matched against a proposed change.",
                )
            )
            continue
        policies.append(
            ResolvedBlockingPolicy(
                policy=policy.policy, selector=selector, statement=policy.statement
            )
        )

    return EffectiveIssueContent(
        profile_id=inventory.profile_id,
        profile_identity=inventory.profile_identity,
        profile_version=inventory.profile_version,
        content_sha256=inventory.content_sha256,
        contract_version=CONTENT_CONTRACT_VERSION,
        artifacts=tuple(artifacts),
        coverage_requirements=tuple(coverage),
        blocking_policies=tuple(policies),
        problems=tuple(problems),
    )


def _mapped_fields_by_project(
    session: Session, inventories: Mapping[int, IssueInventory | None]
) -> dict[int, frozenset[str] | None]:
    """The canonical fields each project's registered mapping revision targets.

    ``None`` where the registration cannot be read back to a declaration
    (#610), and ``None`` too where the profile names one mapping revision while
    the project renders through another: a profile describing an issue nobody
    would actually receive is a configuration problem, not a fact to average
    out.
    """

    wanted = {
        project_id: inventory
        for project_id, inventory in inventories.items()
        if inventory is not None
    }
    if not wanted:
        return {}
    formats = effective_baseline_formats_by_project(session, tuple(wanted))
    registrations = {
        project_id: formats.get(project_id, {}).get("field_mapping")
        for project_id in wanted
    }
    format_ids = tuple(
        int(row.id) for row in registrations.values() if row is not None
    )
    stored = (
        {
            int(row.format_id): row
            for row in session.scalars(
                select(BaselineFormatManifest).where(
                    BaselineFormatManifest.format_id.in_(format_ids)
                )
            ).all()
        }
        if format_ids
        else {}
    )
    resolved: dict[int, frozenset[str] | None] = {}
    for project_id, inventory in wanted.items():
        registration = registrations.get(project_id)
        declaration = (
            stored.get(int(registration.id)) if registration is not None else None
        )
        if declaration is None:
            resolved[project_id] = None
            continue
        manifest = manifest_from_declaration(declaration.declaration)
        if (manifest.identity, manifest.version) != (
            inventory.field_mapping.identity,
            inventory.field_mapping.version,
        ):
            resolved[project_id] = None
            continue
        resolved[project_id] = frozenset(
            field for mapping in manifest.mappings for field in mapping.target_fields
        )
    return resolved
