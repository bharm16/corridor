"""Track an approval condition as a field in its own words (ADR-0060, #373).

A clean approval letter earns one confirm (ADR-0056).  A hedged one records as
conditional by itself and keeps the Constraint not ready — the fail-closed
direction costs no click (ADR-0060).  #347 already classifies the letter and
holds the quoted hedge; what was missing was tracking the hedge as a living
entry: surfacing it in the company's own words, linking it to what it names,
and accepting evidence against it.  That is this module.

The design follows ADR-0052's rule that a machine field is a predicate, never a
stored checkmark.  A condition entry is therefore *derived* at read time from
its conditional letter: quoting it, blocking Ready with it, and resolving its
tier are all read-time computations, so recording is automatic, idempotent by
construction (one cited conditional sentence is one condition — re-reading the
same letter can never make a second entry), and utterly incapable of producing
a false Ready, because it only ever *adds* a blocker.  Only the acts that move
an open condition toward Ready are stored, append-only and attributable:

- a person clearing it against a cited later passage or a recorded verbal;
- the exact-and-mechanical automatic clear, which — because it moves toward
  Ready — expands automatic Record Inclusion and is therefore ADR-0050
  replay-gated, inert until this project's own recorded human clears vouch for
  it (the ship-inactive posture of #370/#371);
- a person dismissing a misdetection with a reason.

Tier-1 linking reuses ADR-0054's identifying-language discipline: verbatim
inputs, an exact rule only when exactly one target survives, no scores.  The
candidate set is only ever this project's own active Constraints, so a
cross-project link is refused by construction.  A linked condition needs no
clearing act of its own: it clears when its target clears, because the target's
own gate already proved that fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import refusals
from corridor import audit
from corridor import replay_gate
from corridor.dependency_events import (
    COMMITTED_EVENT_TYPES,
    closed_party_commitment_lineages,
    current_scope_decision_filter,
)
from corridor.models import (
    ConditionResolution,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventScopeDecision,
    Document,
    DocumentationFieldConfirmation,
    EvidenceLink,
    ExternalOrg,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.statement_lifecycle import current_statement_event_filter
from corridor.verify import normalize


MACHINE_ACTOR = "corridor:condition-clearing"
CLEARING_POLICY_VERSION = "condition-clearing-v1-exact-mechanical"

# The ADR-0050 policy family this module replays under.  Like identifying
# language, it keeps no activation ledger of its own: the replay is read at the
# moment of the clear, so there is nothing to append.
CONDITION_CLEAR_FAMILY = "condition_clear"

# The exact-and-mechanical clear only fires when a later passage both names the
# condition's own words and states a completion.  These are the completion
# tokens; the guard is deliberately conservative because the safe direction is
# to leave a condition open (someone chases it) rather than clear it wrongly.
_COMPLETION_TOKENS = (
    "passed",
    "complete",
    "completed",
    "received",
    "executed",
    "satisfied",
    "fulfilled",
    "approved",
    "cleared",
    "finished",
    "recorded",
    "in place",
)
# Hedge and filler words carry no identifying content, so they never count as
# the salient words the mechanical clear must find echoed in a later passage.
_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by for from has have is it its of on once
    only or our pending provided that the their them then they this to until upon
    us we when which will with subject after conditioned conditional
    """.split()
)
# The words that open a hedge.  A condition's salient content is what comes
# *after* the hedge (the thing being waited on), never the approval verb before
# it, so "approved pending final inspection" is tracked by "final inspection".
_HEDGE_OPENERS = frozenset(
    "pending subject conditional conditioned after until once contingent provided".split()
)
_APPROVAL_WORDS = frozenset("approved approval approve approves".split())


class ConditionResolutionRefusal(refusals.Refusal, ValueError):
    """The condition or its cited basis cannot lawfully receive this act."""

    refusal_kind = refusals.CONFLICT


# --------------------------------------------------------------------------- #
# Read-time derivation: the condition entry, its tier, and whether it is open #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConditionLink:
    """One conditional letter passage — the tracking key of a condition."""

    evidence_link_id: int
    condition_text: str
    document_id: int
    document_filename: str
    page_no: int


@dataclass(frozen=True)
class FieldCandidate:
    """A required field a condition may name, with its match terms and state."""

    name: str
    terms: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class ConditionTarget:
    kind: str  # 'field' | 'row' | 'generic'
    field_name: str | None = None
    dependency_id: int | None = None
    ref_code: str | None = None
    matched_language: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConditionEntry:
    """A condition in the company's own words, its link, and its state."""

    dependency_id: int
    evidence_link_id: int
    condition_text: str
    document_id: int
    document_filename: str
    page_no: int
    target: ConditionTarget
    state: str  # 'open' | 'cleared' | 'dismissed'
    resolution_kind: str | None = None
    resolution_reason: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    basis_evidence_link_id: int | None = None
    basis_event_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def blocks_ready(self) -> bool:
        return self.state == "open"


def _phrase(value: str | None) -> str:
    """ADR-0054's normalization, reused so linking speaks the same language.

    Punctuation is folded to spaces so a word at a sentence end ("inspection.")
    matches the same word mid-sentence ("inspection passed"); the verbatim,
    no-scores discipline is unchanged.
    """
    lowered = normalize((value or "").replace("-", " ")).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", lowered).split())


def resolve_condition_target(
    condition_text: str,
    *,
    field_candidates: tuple[FieldCandidate, ...],
    others: tuple[tuple[Dependency, tuple[str, ...]], ...],
) -> ConditionTarget:
    """Link the condition's words to one target, or leave it generic.

    Every candidate is matched by verbatim presence of its identifying
    language in the condition's own words; the exact rule links only when
    exactly one target — across the Constraint's own required fields and this
    project's other active Constraints — survives.  Zero or several survivors
    stay generic, which is the safe default, not a failure.
    """
    normalized = _phrase(condition_text)
    field_hits: list[tuple[FieldCandidate, str]] = []
    for candidate in field_candidates:
        for term in candidate.terms:
            normalized_term = _phrase(term)
            if normalized_term and normalized_term in normalized:
                field_hits.append((candidate, term))
                break
    row_hits: list[tuple[Dependency, tuple[str, ...]]] = []
    for dependency, org_terms in others:
        identifying = (
            dependency.ref_code,
            dependency.source_ref,
            dependency.title,
            dependency.location_desc,
            *org_terms,
        )
        matched = tuple(
            sorted(
                {
                    _phrase(value)
                    for value in identifying
                    if value
                    and len(_phrase(value)) >= 3
                    and _phrase(value) in normalized
                }
            )
        )
        if matched:
            row_hits.append((dependency, matched))
    if len(field_hits) + len(row_hits) != 1:
        return ConditionTarget("generic")
    if field_hits:
        candidate, term = field_hits[0]
        return ConditionTarget(
            "field", field_name=candidate.name, matched_language=(term,)
        )
    dependency, matched = row_hits[0]
    return ConditionTarget(
        "row",
        dependency_id=dependency.id,
        ref_code=dependency.ref_code,
        matched_language=matched,
    )


def derive_condition_entries(
    session: Session,
    dependency: Dependency,
    conditional_links: tuple[ConditionLink, ...],
    field_candidates: tuple[FieldCandidate, ...],
) -> tuple[ConditionEntry, ...]:
    """Derive every condition entry for a Constraint at read time."""
    if not conditional_links:
        return ()
    others = _link_candidates(session, dependency)
    resolutions = _binding_resolutions(session, dependency.id)
    overrides = _immaterial_override_link_ids(session, dependency.id)
    targets = {
        link.evidence_link_id: resolve_condition_target(
            link.condition_text, field_candidates=field_candidates, others=others
        )
        for link in conditional_links
    }
    completed = (
        completed_dependency_ids(session, dependency.project_id)
        if any(target.kind == "row" for target in targets.values())
        else frozenset()
    )
    field_complete = {candidate.name: candidate.complete for candidate in field_candidates}
    entries: list[ConditionEntry] = []
    for link in conditional_links:
        target = targets[link.evidence_link_id]
        state, kind, resolution = _entry_state(
            link.evidence_link_id,
            target,
            overrides=overrides,
            resolutions=resolutions,
            field_complete=field_complete,
            completed=completed,
        )
        entries.append(
            ConditionEntry(
                dependency_id=dependency.id,
                evidence_link_id=link.evidence_link_id,
                condition_text=link.condition_text,
                document_id=link.document_id,
                document_filename=link.document_filename,
                page_no=link.page_no,
                target=target,
                state=state,
                resolution_kind=kind,
                resolution_reason=resolution.reason if resolution else None,
                resolved_by=resolution.resolved_by if resolution else None,
                resolved_at=resolution.resolved_at if resolution else None,
                basis_evidence_link_id=(
                    resolution.basis_evidence_link_id if resolution else None
                ),
                basis_event_id=resolution.basis_event_id if resolution else None,
            )
        )
    return tuple(entries)


def _entry_state(
    evidence_link_id: int,
    target: ConditionTarget,
    *,
    overrides: frozenset[int],
    resolutions: dict[int, ConditionResolution],
    field_complete: dict[str, bool],
    completed: frozenset[int],
) -> tuple[str, str | None, ConditionResolution | None]:
    """Resolve one condition's state; the fail-closed default is open."""
    if evidence_link_id in overrides:
        return "cleared", "override", None
    resolution = resolutions.get(evidence_link_id)
    if resolution is not None and resolution.kind == "dismissed":
        return "dismissed", "dismissed", resolution
    if resolution is not None and resolution.kind == "cleared":
        return "cleared", "cleared", resolution
    if target.kind == "field" and field_complete.get(target.field_name or ""):
        return "cleared", "linked_field", None
    if target.kind == "row" and target.dependency_id in completed:
        return "cleared", "linked_row", None
    return "open", None, None


# --------------------------------------------------------------------------- #
# Derivation helpers — recorded facts only                                     #
# --------------------------------------------------------------------------- #


def _link_candidates(
    session: Session, dependency: Dependency
) -> tuple[tuple[Dependency, tuple[str, ...]], ...]:
    """This project's other active Constraints and their party's names.

    The candidate set never leaves the project, so a cross-project link cannot
    form even when another project holds an identically named Constraint.
    """
    rows = session.execute(
        select(Dependency, ExternalOrg)
        .join(ExternalOrg, ExternalOrg.id == Dependency.external_org_id, isouter=True)
        .where(
            Dependency.project_id == dependency.project_id,
            Dependency.id != dependency.id,
            Dependency.dismissed_at.is_(None),
        )
        .order_by(Dependency.id)
    ).all()
    candidates: list[tuple[Dependency, tuple[str, ...]]] = []
    for other, org in rows:
        org_terms: tuple[str, ...] = ()
        if org is not None:
            org_terms = (org.name, *(org.aliases or ()))
        candidates.append((other, org_terms))
    return tuple(candidates)


def _binding_resolutions(
    session: Session, dependency_id: int
) -> dict[int, ConditionResolution]:
    """The newest stored resolution per condition (append-only history)."""
    resolutions: dict[int, ConditionResolution] = {}
    for resolution in session.scalars(
        select(ConditionResolution)
        .where(ConditionResolution.dependency_id == dependency_id)
        .order_by(ConditionResolution.id)
    ):
        resolutions[resolution.evidence_link_id] = resolution
    return resolutions


def _immaterial_override_link_ids(
    session: Session, dependency_id: int
) -> frozenset[int]:
    """Conditional letters a person counted as full approval (ADR-0060)."""
    return frozenset(
        session.scalars(
            select(DocumentationFieldConfirmation.evidence_link_id).where(
                DocumentationFieldConfirmation.dependency_id == dependency_id,
                DocumentationFieldConfirmation.condition_immaterial.is_(True),
            )
        ).all()
    )


def completed_dependency_ids(session: Session, project_id: int) -> frozenset[int]:
    """Constraints whose party-level commitment has Completion Reported.

    Reuses the single closure rule (``closed_party_commitment_lineages``) so a
    cross-row condition's "after they complete their work" clears on exactly
    the completion the rest of the system already recognizes (ADR-0054).
    """
    closed = closed_party_commitment_lineages(session, project_id)
    if not closed:
        return frozenset()
    rows = session.execute(
        select(
            DependencyEvent.commitment_lineage_id, DependencyEventScope.dependency_id
        )
        .join(DependencyEventScope, DependencyEventScope.event_id == DependencyEvent.id)
        .join(
            DependencyEventScopeDecision,
            DependencyEventScopeDecision.id == DependencyEventScope.scope_decision_id,
        )
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.commitment_lineage_id.in_(closed),
            current_statement_event_filter(DependencyEvent.id),
            current_scope_decision_filter(),
        )
    ).all()
    return frozenset(
        dependency_id
        for lineage_id, dependency_id in rows
        if lineage_id in closed
    )


def _current_verified_links(
    session: Session, dependency_id: int
) -> tuple[ConditionLink, ...]:
    """Current verified passages on a Constraint, as candidate condition keys."""
    return tuple(
        ConditionLink(
            evidence_link_id=link.id,
            condition_text=link.quote,
            document_id=document.id,
            document_filename=document.filename,
            page_no=link.page_no,
        )
        for link, document in session.execute(
            select(EvidenceLink, Document)
            .join(Document, EvidenceLink.document_id == Document.id)
            .join(Dependency, EvidenceLink.dependency_id == Dependency.id)
            .where(
                EvidenceLink.dependency_id == dependency_id,
                EvidenceLink.verified.is_(True),
                Document.superseded_by.is_(None),
                Document.project_id == Dependency.project_id,
            )
            .order_by(EvidenceLink.id)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Human acts — clearing and dismissal, both append-only and attributable      #
# --------------------------------------------------------------------------- #


def clear_condition(
    session: Session,
    dependency: Dependency,
    evidence_link_id: int,
    *,
    principal: HumanPrincipal,
    entries: tuple[ConditionEntry, ...],
    basis_evidence_link_id: int | None = None,
    basis_event_id: int | None = None,
    reason: str | None = None,
) -> ConditionResolution:
    """Record that a cited later passage or a recorded verbal met a condition.

    The caller passes the Constraint's derived condition entries (the checklist
    is the single place that derives them); the condition must be one that is
    open, and the basis must be a verified passage or a recorded statement in
    the same project.  The act carries the person and the exact words it stood
    on — the judgment path ADR-0060 keeps for a generic condition ("committed
    by cited confirm where judgment is needed").
    """
    principal = require_human_principal(principal)
    entry = _validate_open(session, dependency, entries, evidence_link_id)
    resolution = append_condition_clear(
        session,
        dependency=dependency,
        entry=entry,
        basis_evidence_link_id=basis_evidence_link_id,
        basis_event_id=basis_event_id,
        reason=reason,
        resolved_by=principal.subject,
        receipt_json=None,
    )
    audit.record(
        session,
        principal=principal,
        action=audit.CLEAR_CONDITION,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "evidence_link_id": evidence_link_id,
            "basis_evidence_link_id": basis_evidence_link_id,
            "basis_event_id": basis_event_id,
            "condition_text": entry.condition_text,
        },
    )
    session.flush()
    return resolution


def dismiss_condition(
    session: Session,
    dependency: Dependency,
    evidence_link_id: int,
    *,
    principal: HumanPrincipal,
    entries: tuple[ConditionEntry, ...],
    reason: str,
) -> ConditionResolution:
    """Record that a person judged a condition a misdetection, with a reason.

    Dismissal only ever removes a spurious blocker; the Constraint's Ready is
    then governed by its real fields.  No automatic path can do this — a
    misdetection is retired only by a named person stating why (ADR-0060).
    """
    principal = require_human_principal(principal)
    cleaned = (reason or "").strip()
    if not cleaned:
        raise ConditionResolutionRefusal("a dismissal must record a reason")
    entry = _validate_open(session, dependency, entries, evidence_link_id)
    resolution = ConditionResolution(
        dependency_id=dependency.id,
        evidence_link_id=evidence_link_id,
        kind="dismissed",
        condition_text=entry.condition_text,
        reason=cleaned,
        resolved_by=principal.subject,
    )
    session.add(resolution)
    audit.record(
        session,
        principal=principal,
        action=audit.DISMISS_CONDITION,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "evidence_link_id": evidence_link_id,
            "reason": cleaned,
            "condition_text": entry.condition_text,
        },
    )
    session.flush()
    return resolution


def _validate_open(
    session: Session,
    dependency: Dependency,
    entries: tuple[ConditionEntry, ...],
    evidence_link_id: int,
) -> ConditionEntry:
    """Find one open condition among the caller's derived entries, under lock.

    The entries came from the checklist read; taking the project lock and
    re-checking the stored resolutions here closes the window between that read
    and the write, so a condition another writer just resolved is refused
    rather than resolved twice.
    """
    session.flush()
    lock_project(session, dependency.project_id)
    session.expire_all()
    entry = next(
        (item for item in entries if item.evidence_link_id == evidence_link_id), None
    )
    if entry is None:
        raise ConditionResolutionRefusal(
            "no condition cites that passage on this constraint"
        )
    if not entry.is_open:
        raise ConditionResolutionRefusal("this condition is already resolved")
    if evidence_link_id in _binding_resolutions(session, dependency.id):
        raise ConditionResolutionRefusal("this condition is already resolved")
    return entry


def append_condition_clear(
    session: Session,
    *,
    dependency: Dependency,
    entry: ConditionEntry,
    basis_evidence_link_id: int | None,
    basis_event_id: int | None,
    reason: str | None,
    resolved_by: str,
    receipt_json: dict | None,
) -> ConditionResolution:
    """Validate a clear's cited basis and append it, or refuse.

    Shared by the human cited/verbal clear and the gated mechanical clear; the
    ``resolved_by`` is the only thing that differs (a person, or the machine
    actor with a reproducible receipt).
    """
    if basis_evidence_link_id is None and basis_event_id is None:
        raise ConditionResolutionRefusal("a clear must cite a passage or a verbal")
    if basis_evidence_link_id is not None:
        basis = session.get(EvidenceLink, basis_evidence_link_id)
        document = (
            session.get(Document, basis.document_id) if basis is not None else None
        )
        if basis is None or document is None or document.project_id != dependency.project_id:
            raise ConditionResolutionRefusal(
                "the cited basis is not a passage in this project"
            )
        if not basis.verified:
            raise ConditionResolutionRefusal("the cited basis passage must be verified")
        if basis_evidence_link_id == entry.evidence_link_id:
            raise ConditionResolutionRefusal(
                "a condition cannot clear itself; cite a later passage"
            )
    if basis_event_id is not None:
        event = session.get(DependencyEvent, basis_event_id)
        if event is None or event.project_id != dependency.project_id:
            raise ConditionResolutionRefusal(
                "the cited verbal is not a recorded statement in this project"
            )
    resolution = ConditionResolution(
        dependency_id=dependency.id,
        evidence_link_id=entry.evidence_link_id,
        kind="cleared",
        condition_text=entry.condition_text,
        basis_evidence_link_id=basis_evidence_link_id,
        basis_event_id=basis_event_id,
        reason=(reason or "").strip() or None,
        receipt_json=receipt_json,
        resolved_by=resolved_by,
    )
    session.add(resolution)
    return resolution


# --------------------------------------------------------------------------- #
# The exact-and-mechanical automatic clear — ADR-0050 replay-gated             #
# --------------------------------------------------------------------------- #


def _salient_words(condition_text: str) -> frozenset[str]:
    """The condition's own subject words — what a later passage must echo.

    Taken from after the hedge opener, so the approval verb before it never
    becomes a word a completion passage would have to repeat.
    """
    tokens = _phrase(condition_text).split()
    start = 0
    for index, token in enumerate(tokens):
        if token in _HEDGE_OPENERS:
            start = index + 1
            break
    return frozenset(
        token
        for token in tokens[start:]
        if len(token) >= 3
        and token not in _STOPWORDS
        and token not in _APPROVAL_WORDS
    )


def mechanical_match(condition_text: str, evidence_quote: str) -> bool:
    """Whether a later passage exactly and mechanically meets a condition.

    Conservative on purpose: the passage must echo *every* salient word of the
    condition and state a completion.  "final inspection passed" clears
    "pending final inspection"; "final inspection is scheduled" does not.
    """
    salient = _salient_words(condition_text)
    if not salient:
        return False
    quote = _phrase(evidence_quote)
    if not all(word in quote for word in salient):
        return False
    return any(token in quote for token in _COMPLETION_TOKENS)


@dataclass(frozen=True)
class ConditionClearRun:
    cleared_count: int
    replay: replay_gate.ReplayOutcome


def replay_matches_human_condition_clears(
    session: Session, project_id: int
) -> replay_gate.ReplayOutcome:
    """Compare the mechanical clear to this project's human condition clears.

    A person's cited clear is the answer key.  A contradiction is the rule
    clearing the same condition with a *different* sole passage than the
    person cited; the rule abstaining — no sole passage — on a case a person
    decided by judgment is not a contradiction.  Machine clears are never the
    answer key.  The comparison, the pass rule and the zero-case refusal are
    ``corridor.replay_gate``'s (ADR-0050); this family contributes only the
    answer key and the recomputation, and keeps no activation ledger.
    """
    human_clears = session.scalars(
        select(ConditionResolution)
        .join(Dependency, Dependency.id == ConditionResolution.dependency_id)
        .where(
            Dependency.project_id == project_id,
            ConditionResolution.kind == "cleared",
            ConditionResolution.basis_evidence_link_id.is_not(None),
            ConditionResolution.resolved_by.not_like("corridor:%"),
        )
        .order_by(ConditionResolution.id)
    ).all()
    # One condition is one case, whichever clear recorded it first; the case is
    # keyed by the clear's own id so a contradiction names the human act.
    cases: dict[tuple[int, int], ConditionResolution] = {}
    for clear in human_clears:
        cases.setdefault((clear.dependency_id, clear.evidence_link_id), clear)
    by_id = {clear.id: clear for clear in cases.values()}

    def recompute(clear_id: int) -> object:
        clear = by_id[clear_id]
        matches = mechanical_matches(
            session, clear.dependency_id, clear.evidence_link_id, clear.condition_text
        )
        if len(matches) != 1:
            return replay_gate.ABSTAINED
        return matches[0]

    return replay_gate.replay(
        family=CONDITION_CLEAR_FAMILY,
        human_decisions=[
            (clear_id, by_id[clear_id].basis_evidence_link_id)
            for clear_id in sorted(by_id)
        ],
        recompute=recompute,
    )


def mechanical_matches(
    session: Session,
    dependency_id: int,
    condition_link_id: int,
    condition_text: str,
) -> list[int]:
    """Current verified passages on the Constraint the rule would accept."""
    return [
        link.evidence_link_id
        for link in _current_verified_links(session, dependency_id)
        if link.evidence_link_id != condition_link_id
        and mechanical_match(condition_text, link.condition_text)
    ]


# --------------------------------------------------------------------------- #
# Surfacing — proposals shown to a person for a generic open condition         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConditionClearProposal:
    evidence_link_id: int
    condition_text: str
    basis_evidence_link_id: int
    basis_quote: str
    basis_document_filename: str
    basis_page_no: int


def propose_condition_clears(
    session: Session, dependency_id: int, entries: tuple[ConditionEntry, ...]
) -> tuple[ConditionClearProposal, ...]:
    """Later passages whose language matches an open generic condition.

    Shown with both quotes so a person can confirm the clear where judgment is
    needed; the exact-and-mechanical subset is what the gated auto-clear takes.
    """
    proposals: list[ConditionClearProposal] = []
    links = {link.evidence_link_id: link for link in _current_verified_links(session, dependency_id)}
    for entry in entries:
        if not entry.is_open or entry.target.kind != "generic":
            continue
        salient = _salient_words(entry.condition_text)
        if not salient:
            continue
        for link in links.values():
            if link.evidence_link_id == entry.evidence_link_id:
                continue
            quote = _phrase(link.condition_text)
            if all(word in quote for word in salient):
                proposals.append(
                    ConditionClearProposal(
                        evidence_link_id=entry.evidence_link_id,
                        condition_text=entry.condition_text,
                        basis_evidence_link_id=link.evidence_link_id,
                        basis_quote=link.condition_text,
                        basis_document_filename=link.document_filename,
                        basis_page_no=link.page_no,
                    )
                )
    return tuple(proposals)
