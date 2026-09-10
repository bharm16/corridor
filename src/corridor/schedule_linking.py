"""Link Constraints to the key dates that govern them, by the schedule's own data.

A Constraint's **Required By** date comes from the schedule activity that
governs its utility work. Deciding *which* activity is the schedule matcher's
job — the piece ``milestones.py`` deliberately leaves to this module (ADR-0057,
#371). Three things happen here, in the order a real schedule forces them:

1. **The governing set identifies itself.** Schedule activities carry codes and
   names ("UTIL-RELO-B — utility relocations complete"). Activities whose codes
   and names match utility conventions flag themselves as governing with no
   human step, and ``derive_governing_set`` records exactly which codes and
   names matched. A person picks only when the coding is too poor to read
   (``pick_governing_activities``), once, under their own subject.

2. **Conflicts link by location.** Each Constraint links to the governing
   activity whose station coverage contains it — an exact rule when exactly one
   activity covers it, a card of candidates when several do (the same
   matching pattern as ADR-0051 / ADR-0054, on station data). Values come
   verbatim from both sources; there are no similarity scores. The binding is
   written by ``milestones.link_dependency`` (unchanged); this module records
   the deciding values beside it.

3. **New dates flow through, per row, no gate.** When a schedule revision moves
   a linked activity's date, ``flow_through_revisions`` advances every linked
   Constraint's Required By basis to the new Key Date Version automatically,
   retaining the prior version in history. Nobody blesses reality; the impact
   surfaces as work-list attention, never as an approval question.

The boundary of ADR-0057 holds absolutely: nothing here touches Promised For or
any statement fact — only the project's Required By basis.

The automatic exact rule (step 2) is a *new automatic matching class*, so
ADR-0050 gates it: it may not auto-write until a regression replay of the
project's own recorded human link decisions passes with at least one real case
and no contradiction. It ships inactive. Until a person has linked by hand, a
single exact match waits as a one-candidate card rather than auto-linking —
exactly ADR-0050's "a brand-new rule with no history waits for a real case."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit, merge, policy, replay_gate
from corridor.milestones import link_dependency
from corridor.models import (
    AuditLog,
    Dependency,
    Milestone,
    ScheduleGoverningDerivation,
    ScheduleLinkActivation,
    ScheduleLinkReceipt,
)
from corridor.principals import HumanPrincipal, require_human_principal

POLICY_VERSION = "schedule-conflict-link-v1"
# The exact rule's machine identity, and the flow-through's — distinct because
# they are different acts: one decides a new link, one carries an existing link
# to a new date. Neither is ever a human subject (ADR-0045 provenance).
MACHINE_ACTOR = "corridor:schedule-link"
FLOW_THROUGH_ACTOR = "corridor:schedule-flow-through"
ACTIVATION_ACTOR = "corridor:schedule-link-activation"
DERIVATION_ACTOR = "corridor:schedule-governing-derivation"

# Utility-work conventions the ticket names (RELO*, UTIL*, and the plain words
# a scheduler writes). A code or name that matches flags its activity as
# governing; the matched rule string is recorded so the derivation is auditable.
_GOVERNING_CODE_PREFIXES = ("UTIL", "RELO")
_GOVERNING_NAME_KEYWORDS = ("utility relocation", "utility relocations", "reloc", "relocation")
_CODE_SPLIT = re.compile(r"[^A-Za-z0-9]+")

# One stationing token, `145+00` / `1149+00` / `135+58.68`. The same shape
# `merge.parse_station` reads; found here so an activity name carrying a range
# ("Relocate WL 145+00 to 152+00") yields both endpoints.
_STATION_TOKEN = re.compile(r"\d{1,5}\s*\+\s*\d{1,2}(?:\.\d+)?")


class ScheduleLinkRefusal(ValueError):
    """A proposed human link does not stand on a real, covering basis.

    Raised rather than recording an attributable no-op: a person's link must
    point at a governing activity that actually covers the Constraint's station
    (ADR-0035, ADR-0057). The refusal writes nothing.
    """


# --------------------------------------------------------------------------- #
# Governing-set derivation                                                    #
# --------------------------------------------------------------------------- #


def _governing_match(code: str, name: str) -> str | None:
    """The utility-convention rule this activity matches, or None.

    Verbatim, exact: a code stem equal to a known prefix, or a name containing a
    known keyword. No resemblance scoring — the same discipline as identity
    resolution (ADR-0051).
    """
    stems = [s for s in _CODE_SPLIT.split((code or "").upper()) if s]
    for prefix in _GOVERNING_CODE_PREFIXES:
        if prefix in stems or (code or "").upper().startswith(prefix):
            return f"code_prefix:{prefix}"
    folded = (name or "").casefold()
    for keyword in _GOVERNING_NAME_KEYWORDS:
        if keyword in folded:
            return f"name_keyword:{keyword}"
    return None


def _coded_matches(session: Session, project_id: int) -> list[dict]:
    """Every current Milestone that flags itself as governing, with its rule."""
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project_id)
        .order_by(Milestone.code)
    ).all()
    matches: list[dict] = []
    for milestone in milestones:
        rule = _governing_match(milestone.code, milestone.name)
        if rule is not None:
            matches.append(
                {
                    "milestone_id": milestone.id,
                    "code": milestone.code,
                    "name": milestone.name,
                    "matched_rule": rule,
                }
            )
    return matches


def derive_governing_set(
    session: Session,
    project_id: int,
    *,
    source_name: str,
    source_sha256: str | None = None,
) -> ScheduleGoverningDerivation | None:
    """Record which activities govern utility work, with no human step.

    ``coded`` when the schedule's own codes and names answer it; ``awaiting_pick``
    when they yield nothing usable and a person must pick once. Idempotent: a
    re-derivation whose matched set is identical to the newest one writes
    nothing (ADR-0057's no-redundant-history rule).
    """
    matches = _coded_matches(session, project_id)
    method = "coded" if matches else "awaiting_pick"
    newest = session.scalars(
        select(ScheduleGoverningDerivation)
        .where(
            ScheduleGoverningDerivation.project_id == project_id,
            ScheduleGoverningDerivation.method.in_(("coded", "awaiting_pick")),
        )
        .order_by(ScheduleGoverningDerivation.id.desc())
        .limit(1)
    ).first()
    if newest is not None and newest.method == method and newest.matches_json == matches:
        return None
    derivation = ScheduleGoverningDerivation(
        project_id=project_id,
        source_name=source_name,
        source_sha256=source_sha256,
        method=method,
        recorded_by=DERIVATION_ACTOR,
        matches_json=matches,
    )
    session.add(derivation)
    session.flush([derivation])
    return derivation


def pick_governing_activities(
    session: Session,
    project_id: int,
    milestone_ids: list[int],
    *,
    principal: HumanPrincipal,
    basis: str = "operator picked the governing utility activities",
) -> ScheduleGoverningDerivation:
    """Record a person's one-time pick of the governing activities.

    The fallback for a schedule whose coding is too poor to read (ADR-0057). The
    pick carries the person's subject and the activities they named; it is
    append-only and never asked again while it stands.
    """
    principal = require_human_principal(principal)
    if not milestone_ids:
        raise ScheduleLinkRefusal("a governing pick must name at least one activity")
    milestones = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project_id,
            Milestone.id.in_(milestone_ids),
        )
    ).all()
    found = {milestone.id for milestone in milestones}
    missing = [mid for mid in milestone_ids if mid not in found]
    if missing:
        raise ScheduleLinkRefusal(
            f"activities {missing} are not key dates in this project"
        )
    matches = [
        {"milestone_id": milestone.id, "code": milestone.code, "name": milestone.name}
        for milestone in sorted(milestones, key=lambda m: m.code)
    ]
    derivation = ScheduleGoverningDerivation(
        project_id=project_id,
        source_name=basis,
        source_sha256=None,
        method="human_pick",
        recorded_by=principal.subject,
        matches_json=matches,
    )
    session.add(derivation)
    session.flush([derivation])
    return derivation


def governing_milestone_ids(session: Session, project_id: int) -> set[int]:
    """The current governing set: coded self-identification plus recorded picks."""
    ids = {match["milestone_id"] for match in _coded_matches(session, project_id)}
    for derivation in session.scalars(
        select(ScheduleGoverningDerivation).where(
            ScheduleGoverningDerivation.project_id == project_id,
            ScheduleGoverningDerivation.method == "human_pick",
        )
    ):
        for match in derivation.matches_json or []:
            mid = match.get("milestone_id")
            if isinstance(mid, int):
                ids.add(mid)
    return ids


# --------------------------------------------------------------------------- #
# Location matching                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActivityCoverage:
    """One governing activity and the station range it covers, verbatim.

    ``low_text`` / ``high_text`` are the activity's own station strings; the feet
    are ``merge.parse_station`` of them. An activity whose code and name carry no
    station range has no coverage and never contains a Constraint.
    """

    milestone_id: int
    code: str
    name: str
    key_date_version_id: int | None
    low_ft: float
    high_ft: float
    low_text: str
    high_text: str


@dataclass(frozen=True)
class ConstraintMatch:
    """What the location rule says about one Constraint: exact, tie, or none."""

    dependency_id: int
    kind: str  # "exact" | "tie" | "none"
    candidates: tuple[ActivityCoverage, ...]
    station_from: str | None
    station_to: str | None


def _station_tokens(text: str) -> list[tuple[str, float]]:
    parsed: list[tuple[str, float]] = []
    for token in _STATION_TOKEN.finditer(text or ""):
        feet = merge.parse_station(token.group(0))
        if feet is not None:
            parsed.append((token.group(0), feet))
    return parsed


def activity_coverage(milestone: Milestone) -> ActivityCoverage | None:
    """The station range a governing activity covers, read from its own text."""
    tokens = _station_tokens(f"{milestone.code} {milestone.name}")
    if len(tokens) < 2:
        return None
    low = min(tokens, key=lambda t: t[1])
    high = max(tokens, key=lambda t: t[1])
    return ActivityCoverage(
        milestone_id=milestone.id,
        code=milestone.code,
        name=milestone.name,
        key_date_version_id=milestone.current_registration_id,
        low_ft=low[1],
        high_ft=high[1],
        low_text=low[0],
        high_text=high[0],
    )


def governing_coverages(session: Session, project_id: int) -> list[ActivityCoverage]:
    """Coverage for every governing activity that carries a station range."""
    ids = governing_milestone_ids(session, project_id)
    if not ids:
        return []
    milestones = session.scalars(
        select(Milestone).where(
            Milestone.project_id == project_id, Milestone.id.in_(ids)
        )
    ).all()
    coverages = [activity_coverage(milestone) for milestone in milestones]
    return [coverage for coverage in coverages if coverage is not None]


def _constraint_span(dependency: Dependency) -> tuple[float, float] | None:
    values = [
        feet
        for feet in (
            merge.parse_station(dependency.station_from),
            merge.parse_station(dependency.station_to),
        )
        if feet is not None
    ]
    if not values:
        return None
    return min(values), max(values)


def match_constraint(
    dependency: Dependency, coverages: list[ActivityCoverage]
) -> ConstraintMatch:
    """Which governing activities cover this Constraint's station.

    Full containment: the Constraint's station (a point, or its from→to span)
    lies inside the activity's covered range. Exactly one covering activity is an
    exact rule; several are a tie; none leaves the Constraint unlinked.
    """
    span = _constraint_span(dependency)
    if span is None:
        return ConstraintMatch(
            dependency.id, "none", (), dependency.station_from, dependency.station_to
        )
    covering = tuple(
        coverage
        for coverage in coverages
        if coverage.low_ft <= span[0] and span[1] <= coverage.high_ft
    )
    covering = tuple(sorted(covering, key=lambda c: (c.low_ft, c.high_ft, c.code)))
    kind = "exact" if len(covering) == 1 else "tie" if len(covering) > 1 else "none"
    return ConstraintMatch(
        dependency.id, kind, covering, dependency.station_from, dependency.station_to
    )


# --------------------------------------------------------------------------- #
# Policy fingerprint (ADR-0050 / ADR-0022)                                    #
# --------------------------------------------------------------------------- #


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes that decide a link — the fingerprint's ground truth."""
    from corridor import milestones as milestones_module
    from corridor import merge as merge_module
    from corridor import models as models_module
    from corridor import principals as principals_module

    here = Path(__file__).parent
    return (
        ("corridor.schedule_linking", Path(__file__).read_bytes()),
        ("corridor.milestones", Path(milestones_module.__file__).read_bytes()),
        ("corridor.merge", Path(merge_module.__file__).read_bytes()),
        ("corridor.audit", Path(audit.__file__).read_bytes()),
        ("corridor.policy", Path(policy.__file__).read_bytes()),
        ("corridor.principals", Path(principals_module.__file__).read_bytes()),
        ("corridor.models", Path(models_module.__file__).read_bytes()),
        (
            "corridor.migrations.d3f9a71c2b84",
            (
                here / "migrations/versions/d3f9a71c2b84_add_schedule_conflict_linking.py"
            ).read_bytes(),
        ),
    )


def canonical_policy() -> dict:
    """The exact rule a passing replay stands on.

    Config and deployed bytes both move this digest; either voids a prior
    activation until a new replay passes (ADR-0050).
    """
    return {
        "policy_version": POLICY_VERSION,
        "governing_code_prefixes": sorted(_GOVERNING_CODE_PREFIXES),
        "governing_name_keywords": sorted(_GOVERNING_NAME_KEYWORDS),
        "coverage_rule": "full-station-containment-v1",
        "station_parser": "merge.parse_station-v1",
        "matcher": "exactly-one-covering-activity-v1",
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": policy.digest_of_sources(_rule_source_bytes),
    }


def policy_fingerprint() -> tuple[str, str]:
    """The current (version, sha256) the auto-link rule runs under."""
    return POLICY_VERSION, policy.canonical_sha256(canonical_policy())


# --------------------------------------------------------------------------- #
# Regression replay + activation (ADR-0050, through the shared gate)          #
# --------------------------------------------------------------------------- #
#
# The comparison, the pass rule and the activation ledger live in
# ``corridor.replay_gate``. This family contributes only its own two answers:
# which link decisions a person actually made, and what the exact rule would do
# for one of them.


def _fingerprint() -> replay_gate.RuleFingerprint:
    version, sha256 = policy_fingerprint()
    return replay_gate.RuleFingerprint(version, sha256)


def replay_matches_human_decisions(
    session: Session, project_id: int
) -> replay_gate.ReplayOutcome:
    """Replay the exact rule against every recorded human link decision.

    The cases a person decided are the answer key. For each Constraint a person
    linked by hand, recompute the exact rule from the saved records — never
    re-reading a source file. A contradiction is the rule producing an exact
    link to a *different* activity than the person chose; the rule abstaining (a
    tie, no coverage now, or a Constraint that no longer exists) is not one.
    """
    receipts = session.scalars(
        select(ScheduleLinkReceipt)
        .where(
            ScheduleLinkReceipt.project_id == project_id,
            ScheduleLinkReceipt.basis == "human_choice",
        )
        .order_by(ScheduleLinkReceipt.id)
    ).all()
    latest_choice: dict[int, int] = {}
    for receipt in receipts:
        latest_choice[receipt.dependency_id] = receipt.milestone_id
    coverages = governing_coverages(session, project_id)

    def recompute(dependency_id: int) -> object:
        dependency = session.get(Dependency, dependency_id)
        if dependency is None:
            return replay_gate.ABSTAINED
        match = match_constraint(dependency, coverages)
        if match.kind != "exact":
            return replay_gate.ABSTAINED
        return match.candidates[0].milestone_id

    return replay_gate.replay(
        family=replay_gate.FAMILY_SCHEDULE_LINK,
        human_decisions=sorted(latest_choice.items()),
        recompute=recompute,
    )


def activation_status(session: Session, project_id: int) -> str:
    """`inactive` | `active` | `suspended` for the current rule fingerprint."""
    return replay_gate.activation_status(
        session,
        family=replay_gate.FAMILY_SCHEDULE_LINK,
        project_id=project_id,
        fingerprint=_fingerprint(),
    )


def is_auto_link_active(session: Session, project_id: int) -> bool:
    return activation_status(session, project_id) == replay_gate.ACTIVE


def attempt_activation(
    session: Session, project_id: int
) -> ScheduleLinkActivation | None:
    """Activate the auto-link rule iff its replay passes and no human suspended it.

    Runs automatically inside a linking pass. A deliberate human suspension beats
    every passing test, so an already-suspended project is left suspended until a
    person lifts it (ADR-0050).
    """
    if activation_status(session, project_id) != replay_gate.INACTIVE:
        return None
    replay = replay_matches_human_decisions(session, project_id)
    if not replay.passed:
        return None
    return replay_gate.record_activation(
        session,
        family=replay_gate.FAMILY_SCHEDULE_LINK,
        project_id=project_id,
        fingerprint=_fingerprint(),
        replay_case_count=replay.case_count,
        reason="regression replay passed on recorded human link decisions",
        recorded_by=ACTIVATION_ACTOR,
    )


def suspend_auto_link(
    session: Session,
    project_id: int,
    *,
    reason: str,
    principal: HumanPrincipal,
) -> ScheduleLinkActivation:
    """A person suspends the automatic rule; suspension beats any passing test."""
    principal = require_human_principal(principal)
    if not reason or not reason.strip():
        raise ScheduleLinkRefusal("a suspension must state a reason")
    return replay_gate.record_suspension(
        session,
        family=replay_gate.FAMILY_SCHEDULE_LINK,
        project_id=project_id,
        fingerprint=_fingerprint(),
        reason=reason,
        recorded_by=principal.subject,
    )


def lift_suspension(
    session: Session, project_id: int, *, principal: HumanPrincipal
) -> ScheduleLinkActivation:
    """A person lifts a suspension, only when a current replay still passes."""
    principal = require_human_principal(principal)
    replay = replay_matches_human_decisions(session, project_id)
    if not replay.passed:
        raise ScheduleLinkRefusal(
            "cannot lift the suspension: the regression replay does not pass"
        )
    return replay_gate.record_activation(
        session,
        family=replay_gate.FAMILY_SCHEDULE_LINK,
        project_id=project_id,
        fingerprint=_fingerprint(),
        replay_case_count=replay.case_count,
        reason="human lifted the suspension after a passing replay",
        recorded_by=principal.subject,
    )


# --------------------------------------------------------------------------- #
# Writing links + flowing dates through                                       #
# --------------------------------------------------------------------------- #


def _newest_link_audit(session: Session, dependency_id: int) -> AuditLog:
    entry = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == dependency_id,
            AuditLog.action == audit.LINK_MILESTONE,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).first()
    if entry is None:  # pragma: no cover - link_dependency always writes one
        raise RuntimeError("link_dependency did not record a LINK_MILESTONE entry")
    return entry


def _link_and_record(
    session: Session,
    dependency: Dependency,
    milestone: Milestone,
    *,
    basis: str,
    decided_by: str,
    deciding_values: dict,
    policy_version: str | None = None,
    policy_sha256: str | None = None,
) -> ScheduleLinkReceipt:
    """Bind the exact Key Date Version (unchanged write path) and retain the why."""
    link_dependency(session, dependency, milestone, actor=decided_by)
    audit_entry = _newest_link_audit(session, dependency.id)
    receipt = ScheduleLinkReceipt(
        project_id=dependency.project_id,
        dependency_id=dependency.id,
        milestone_id=milestone.id,
        milestone_registration_id=milestone.current_registration_id,
        audit_log_id=audit_entry.id,
        basis=basis,
        decided_by=decided_by,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        deciding_values_json=deciding_values,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def _coverage_values(match: ConstraintMatch, coverage: ActivityCoverage) -> dict:
    return {
        "constraint_station_from": match.station_from,
        "constraint_station_to": match.station_to,
        "activity_code": coverage.code,
        "activity_name": coverage.name,
        "activity_coverage_from": coverage.low_text,
        "activity_coverage_to": coverage.high_text,
        "key_date_version_id": coverage.key_date_version_id,
    }


def resolve_link(
    session: Session,
    dependency: Dependency,
    milestone: Milestone,
    *,
    principal: HumanPrincipal,
) -> ScheduleLinkReceipt:
    """Record a person's link of a Constraint to a governing activity.

    The tie-card click, and the confirmation of a single candidate while the
    automatic rule is still inactive. Every human link carries the person's
    subject and a cited basis — the verified station containment, verbatim from
    both sources. A link to an activity that does not actually cover the
    Constraint is refused, never recorded as an attributable no-op.
    """
    principal = require_human_principal(principal)
    coverages = governing_coverages(session, dependency.project_id)
    chosen = next(
        (coverage for coverage in coverages if coverage.milestone_id == milestone.id),
        None,
    )
    if chosen is None:
        raise ScheduleLinkRefusal(
            "the chosen activity is not a governing key date with station coverage"
        )
    span = _constraint_span(dependency)
    if span is None or not (chosen.low_ft <= span[0] and span[1] <= chosen.high_ft):
        raise ScheduleLinkRefusal(
            "the chosen activity does not cover this Constraint's station"
        )
    match = match_constraint(dependency, coverages)
    deciding = _coverage_values(match, chosen)
    deciding["candidates"] = [
        {
            "milestone_id": candidate.milestone_id,
            "activity_code": candidate.code,
            "activity_name": candidate.name,
            "activity_coverage_from": candidate.low_text,
            "activity_coverage_to": candidate.high_text,
        }
        for candidate in match.candidates
    ]
    return _link_and_record(
        session,
        dependency,
        milestone,
        basis="human_choice",
        decided_by=principal.subject,
        deciding_values=deciding,
    )


def flow_through_revisions(
    session: Session, project_id: int
) -> tuple[ScheduleLinkReceipt, ...]:
    """Advance every linked Constraint whose activity moved to the new date.

    No gate (ADR-0057). A Constraint bound to an earlier Key Date Version of its
    activity is re-linked to the current version, advancing its Required By basis
    and retaining the prior version in immutable history. A Constraint already on
    the current version is untouched, so a re-import that moved nothing writes
    nothing.
    """
    dependencies = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_id,
            Dependency.milestone_id.is_not(None),
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    flowed: list[ScheduleLinkReceipt] = []
    for dependency in dependencies:
        milestone = session.get(Milestone, dependency.milestone_id)
        if milestone is None or milestone.current_registration_id is None:
            continue
        if dependency.milestone_registration_id == milestone.current_registration_id:
            continue
        prior_registration_id = dependency.milestone_registration_id
        prior_need_date = dependency.need_date
        deciding = {
            "activity_code": milestone.code,
            "activity_name": milestone.name,
            "prior_key_date_version_id": prior_registration_id,
            "key_date_version_id": milestone.current_registration_id,
            "prior_need_date": prior_need_date.isoformat() if prior_need_date else None,
            "new_need_date": milestone.need_date.isoformat()
            if milestone.need_date
            else None,
        }
        flowed.append(
            _link_and_record(
                session,
                dependency,
                milestone,
                basis="flow_through",
                decided_by=FLOW_THROUGH_ACTOR,
                deciding_values=deciding,
            )
        )
    return tuple(flowed)


# --------------------------------------------------------------------------- #
# The linking pass                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LinkingResult:
    """What one linking pass did, and what it left for a person to decide."""

    governing_derivation: ScheduleGoverningDerivation | None
    activation: ScheduleLinkActivation | None
    auto_linked: tuple[ScheduleLinkReceipt, ...]
    flowed_through: tuple[ScheduleLinkReceipt, ...]
    ties: tuple[ConstraintMatch, ...]
    single_awaiting_confirmation: tuple[ConstraintMatch, ...]


def run_schedule_linking(
    session: Session,
    project_id: int,
    *,
    source_name: str,
    source_sha256: str | None = None,
) -> LinkingResult:
    """Derive the governing set, flow moved dates through, and link what is exact.

    The one entry point a schedule import calls. Flow-through runs first and
    ungated; the automatic exact rule runs only when its ADR-0050 replay has
    activated it, otherwise a single exact match waits as a one-candidate card. A
    tie never auto-links (ADR-0057).
    """
    derivation = derive_governing_set(
        session, project_id, source_name=source_name, source_sha256=source_sha256
    )
    flowed = flow_through_revisions(session, project_id)
    activation = attempt_activation(session, project_id)
    active = is_auto_link_active(session, project_id)
    coverages = governing_coverages(session, project_id)

    version, sha256 = policy_fingerprint()
    unlinked = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_id,
            Dependency.milestone_id.is_(None),
            Dependency.dismissed_at.is_(None),
        )
    ).all()
    auto_linked: list[ScheduleLinkReceipt] = []
    ties: list[ConstraintMatch] = []
    singles: list[ConstraintMatch] = []
    for dependency in unlinked:
        match = match_constraint(dependency, coverages)
        if match.kind == "tie":
            ties.append(match)
        elif match.kind == "exact":
            if not active:
                singles.append(match)
                continue
            coverage = match.candidates[0]
            milestone = session.get(Milestone, coverage.milestone_id)
            auto_linked.append(
                _link_and_record(
                    session,
                    dependency,
                    milestone,
                    basis="exact_station_containment",
                    decided_by=MACHINE_ACTOR,
                    deciding_values=_coverage_values(match, coverage),
                    policy_version=version,
                    policy_sha256=sha256,
                )
            )
    return LinkingResult(
        governing_derivation=derivation,
        activation=activation,
        auto_linked=tuple(auto_linked),
        flowed_through=flowed,
        ties=tuple(ties),
        single_awaiting_confirmation=tuple(singles),
    )


# --------------------------------------------------------------------------- #
# Impact surfacing (read model for the Work List)                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequiredByMove:
    """One Constraint's Required By basis moved by a schedule revision.

    Attention, never a question (ADR-0057): the old and new dates the coordinator
    should see, derived from the flow-through receipt that advanced the row.
    """

    dependency_id: int
    activity_code: str
    prior_need_date: date | None
    new_need_date: date | None
    key_date_version_id: int


def _parse_iso(value: object) -> date | None:
    if isinstance(value, str) and value:
        return date.fromisoformat(value)
    return None


def required_by_moves(
    session: Session, project_id: int
) -> dict[int, RequiredByMove]:
    """The newest schedule-driven Required By move per Constraint, for attention.

    Only a move that changed the date is surfaced (a re-link that carried the
    same date is not a slip). Read from the immutable flow-through receipts.
    """
    receipts = session.scalars(
        select(ScheduleLinkReceipt)
        .where(
            ScheduleLinkReceipt.project_id == project_id,
            ScheduleLinkReceipt.basis == "flow_through",
        )
        .order_by(ScheduleLinkReceipt.id)
    ).all()
    moves: dict[int, RequiredByMove] = {}
    for receipt in receipts:
        values = receipt.deciding_values_json or {}
        prior = _parse_iso(values.get("prior_need_date"))
        new = _parse_iso(values.get("new_need_date"))
        if prior == new:
            continue
        moves[receipt.dependency_id] = RequiredByMove(
            dependency_id=receipt.dependency_id,
            activity_code=str(values.get("activity_code") or ""),
            prior_need_date=prior,
            new_need_date=new,
            key_date_version_id=receipt.milestone_registration_id,
        )
    return moves


@dataclass(frozen=True)
class KeyDateMove:
    """One governing key date a schedule revision moved: old date and new."""

    milestone_id: int
    activity_code: str
    prior_need_date: date | None
    new_need_date: date | None


def moved_key_dates(
    session: Session, project_id: int
) -> dict[int, KeyDateMove]:
    """Each Milestone a schedule revision moved, as ``{milestone_id: (old, new)}``.

    A recorded decision (e.g. an Effect on Key Dates) that referenced one of
    these is the third impact ADR-0057 surfaces as attention.
    """
    receipts = session.scalars(
        select(ScheduleLinkReceipt)
        .where(
            ScheduleLinkReceipt.project_id == project_id,
            ScheduleLinkReceipt.basis == "flow_through",
        )
        .order_by(ScheduleLinkReceipt.id)
    ).all()
    moved: dict[int, KeyDateMove] = {}
    for receipt in receipts:
        values = receipt.deciding_values_json or {}
        prior = _parse_iso(values.get("prior_need_date"))
        new = _parse_iso(values.get("new_need_date"))
        if prior == new:
            continue
        moved[receipt.milestone_id] = KeyDateMove(
            milestone_id=receipt.milestone_id,
            activity_code=str(values.get("activity_code") or ""),
            prior_need_date=prior,
            new_need_date=new,
        )
    return moved
