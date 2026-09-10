"""Resolve an External Organization from retained row evidence, never by minting.

The old admission writer treated an unfamiliar string as a new organization.
That was locally convenient and globally wrong: a relabelled owner became a
second company without a person ever seeing the question.  ADR-0051 makes the
registry a controlled projection.  This module is the only place a source
party spelling may resolve to it, and the only writer of its identity receipts.

The public boundary deliberately returns an unresolved result instead of a
best match.  The dependency admission policy converts that result into a
truthful abstention; a coordinator can later confirm an existing organization
or create one from the displayed source wording.  The advanced automatic tiers
ship inactive until this policy's own recorded human decisions pass ADR-0050's
replay.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor import policy, replay_gate
from corridor.models import (
    Candidate,
    Dependency,
    DocPage,
    ExternalOrg,
    OrganizationIdentityActivation,
    OrganizationIdentityReceipt,
    RevisionComparisonFinding,
    RevisionComparisonRun,
    is_placeholder_party,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.record_inclusion import request_record_inclusion


POLICY_VERSION = "organization-identity-v1"
MACHINE_ACTOR = "corridor:organization-identity"
ACTIVATION_ACTOR = "corridor:organization-identity-activation"
_REGISTRY_LOCK_KEY = 345051
_LEGAL_SUFFIXES = frozenset({"co", "company", "inc", "incorporated", "llc", "ltd", "lp", "plc"})
_FACILITY_WORDS = frozenset({"gas", "electric", "power", "water", "sewer", "telecom", "telephone", "pipeline", "communications"})
_WORD = re.compile(r"[a-z0-9]+")
_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_ALIAS_LANGUAGE = re.compile(r"\b(?:d\s*/\s*b\s*/\s*a|formerly|a\s+subsidiary\s+of)\b", re.I)


class OrganizationIdentityRefusal(ValueError):
    """A requested identity decision is malformed, stale, or out of scope."""


@dataclass(frozen=True)
class IdentityResolution:
    """One deterministic resolution or visible residue for a source spelling."""

    external_org_id: int | None
    method: str | None
    stated_wording: str
    evidence: dict
    candidate_ids: tuple[int, ...]

    @property
    def resolved(self) -> bool:
        return self.external_org_id is not None


def normalize_organization(value: object) -> str:
    """Normalize only punctuation, case, whitespace, and legal suffixes.

    This is intentionally not a similarity score.  ``ABC Gas`` and ``ABC Gas
    Company`` become the same registered spelling; two different company words
    remain different until another retained evidence tier identifies one party.
    """

    words = _WORD.findall(str(value or "").casefold())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _name_stem(value: object) -> str:
    return " ".join(word for word in normalize_organization(value).split() if word not in _FACILITY_WORDS)


def _source_fields(candidate: Candidate) -> dict:
    payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
    fields = payload.get("fields")
    return fields if isinstance(fields, dict) else {}


def _stated_wording(candidate: Candidate) -> str:
    wording = str(_source_fields(candidate).get("external_org") or "").strip()
    if not wording or is_placeholder_party(wording):
        raise OrganizationIdentityRefusal("the source does not name an External Organization")
    return wording


def _registry_lock(session: Session) -> None:
    """Serialize registry mutation across projects without locking model I/O."""

    session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": _REGISTRY_LOCK_KEY})


def _registered_spelling_matches(session: Session, wording: str) -> list[ExternalOrg]:
    wanted = normalize_organization(wording)
    if not wanted:
        return []
    matches: list[ExternalOrg] = []
    for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)):
        if any(
            normalize_organization(spelling) == wanted
            for spelling in (org.name, *(org.aliases or ()))
            if spelling
        ):
            matches.append(org)
    return matches


def _facility_matches(session: Session, candidate: Candidate) -> tuple[list[ExternalOrg], dict]:
    fields = _source_fields(candidate)
    facility = str(fields.get("utility_type") or "").strip()
    stem = _name_stem(fields.get("external_org"))
    if not facility or not stem:
        return [], {"facility_class": facility or None, "name_stem": stem or None}
    candidates: dict[int, ExternalOrg] = {}
    # Facility ownership is registry knowledge.  A confirmation in one
    # project teaches the same registered organization for future matching in
    # every other project; it never rewrites a fact already recorded there.
    for receipt in session.scalars(select(OrganizationIdentityReceipt)):
        classes = {
            normalize_organization(value)
            for value in (receipt.facility_classes_json or [])
            if isinstance(value, str)
        }
        org = session.get(ExternalOrg, receipt.external_org_id)
        if org is None or normalize_organization(facility) not in classes:
            continue
        if _name_stem(org.name) == stem or any(_name_stem(alias) == stem for alias in (org.aliases or ())):
            candidates[org.id] = org
    return list(candidates.values()), {"facility_class": facility, "name_stem": stem}


def _contact_tokens(value: object) -> set[str]:
    raw = str(value or "").strip().casefold()
    if not raw:
        return set()
    result = {f"email:{match.group(0)}" for match in _EMAIL.finditer(raw)}
    digits = "".join(char for char in raw if char.isdigit())
    # A short number or a display-name collision is useful card context but
    # cannot prove organization identity.  Automatic contact continuity is
    # deliberately limited to an exact address or a complete North-American
    # phone number; the original source string remains on the pending card.
    if len(digits) == 10:
        result.add(f"phone:{digits}")
    return result


def _contact_matches(session: Session, candidate: Candidate) -> tuple[list[ExternalOrg], dict]:
    fields = _source_fields(candidate)
    source = str(fields.get("external_org_contact") or "").strip()
    wanted = _contact_tokens(source)
    if not wanted:
        return [], {"source_contact": source or None, "matched_tokens": []}
    matches: dict[int, set[str]] = {}
    for dependency in session.scalars(
        select(Dependency).where(
            Dependency.external_org_id.is_not(None),
            Dependency.external_contact.is_not(None),
        )
    ):
        overlap = wanted & _contact_tokens(dependency.external_contact)
        if overlap:
            matches.setdefault(dependency.external_org_id, set()).update(overlap)
    organizations = [session.get(ExternalOrg, oid) for oid in sorted(matches)]
    return (
        [organization for organization in organizations if organization is not None],
        {"source_contact": source, "matched_tokens": sorted(set().union(*matches.values())) if matches else []},
    )


def _revision_matches(session: Session, candidate: Candidate) -> tuple[list[ExternalOrg], dict]:
    found: dict[int, ExternalOrg] = {}
    paired_ids: list[int] = []
    findings = session.scalars(
        select(RevisionComparisonFinding)
        .join(RevisionComparisonRun, RevisionComparisonRun.id == RevisionComparisonFinding.revision_comparison_run_id)
        .where(RevisionComparisonRun.project_id == candidate.project_id)
    )
    for finding in findings:
        if candidate.id not in (finding.successor_candidate_ids or []):
            continue
        if finding.state not in {"unchanged", "changed"} or len(finding.predecessor_candidate_ids or []) != 1:
            continue
        predecessor_id = finding.predecessor_candidate_ids[0]
        predecessor = session.get(Candidate, predecessor_id)
        if predecessor is None:
            continue
        exact = _registered_spelling_matches(session, str(_source_fields(predecessor).get("external_org") or ""))
        if len(exact) == 1:
            found[exact[0].id] = exact[0]
            paired_ids.append(predecessor_id)
    return list(found.values()), {"predecessor_candidate_ids": sorted(paired_ids)}


def _verified_alias_matches(session: Session, candidate: Candidate) -> tuple[list[ExternalOrg], dict]:
    if not candidate.citations_verified:
        return [], {"citations_verified": False, "passages": []}
    wording = _stated_wording(candidate)
    payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    passages: list[dict] = []
    matches: dict[int, ExternalOrg] = {}
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        quote = str(citation.get("quote") or "").strip()
        document_id = citation.get("document_id")
        page_no = citation.get("page")
        if not quote or not isinstance(document_id, int) or not isinstance(page_no, int):
            continue
        page = session.scalar(select(DocPage).where(DocPage.document_id == document_id, DocPage.page_no == page_no))
        if page is None or quote not in (page.text or "") or not _ALIAS_LANGUAGE.search(quote):
            continue
        normalized_quote = normalize_organization(quote)
        if normalize_organization(wording) not in normalized_quote:
            continue
        for org in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)):
            if normalize_organization(org.name) in normalized_quote:
                matches[org.id] = org
                passages.append({"document_id": document_id, "page_no": page_no, "quote": quote})
    return list(matches.values()), {"citations_verified": True, "passages": passages}


def resolve_candidate_identity(
    session: Session,
    candidate: Candidate,
    *,
    permit_advanced: bool,
    permit_exact: bool = True,
) -> IdentityResolution:
    """Resolve one Candidate's party only when one retained answer survives."""

    wording = _stated_wording(candidate)
    exact = _registered_spelling_matches(session, wording)
    if permit_exact and len(exact) == 1:
        return IdentityResolution(
            exact[0].id,
            "automatic_name_alias",
            wording,
            {"normalized_spelling": normalize_organization(wording), "matched_spelling": exact[0].name},
            (exact[0].id,),
        )
    evidence: dict[str, object] = {
        "normalized_spelling": normalize_organization(wording),
        "name_alias_candidates": [org.id for org in exact],
    }
    if not permit_advanced:
        return IdentityResolution(None, None, wording, evidence, tuple(org.id for org in exact))
    tier_results = (
        ("automatic_facility_class", *_facility_matches(session, candidate)),
        ("automatic_contact", *_contact_matches(session, candidate)),
        ("automatic_revision_lineage", *_revision_matches(session, candidate)),
        ("automatic_stated_alias", *_verified_alias_matches(session, candidate)),
    )
    # Each retained evidence kind narrows the possible registry identities.  A
    # kind with no match says nothing; two non-empty kinds that disagree leave
    # no survivor and therefore preserve visible human residue.  Unioning the
    # lists would wrongly leave a facility-class tie unresolved even when an
    # exact contact proves which one of those candidates owns the row.
    survivor_ids: set[int] | None = (
        {organization.id for organization in exact} if permit_exact and exact else None
    )
    first_surviving_method: str | None = None
    for method, organizations, basis in tier_results:
        evidence[method] = basis
        ids = {organization.id for organization in organizations}
        if not ids:
            continue
        if survivor_ids is None:
            survivor_ids = ids
            first_surviving_method = method
        else:
            survivor_ids &= ids
    survivors = survivor_ids or set()
    if len(survivors) == 1:
        organization_id = next(iter(survivors))
        return IdentityResolution(
            organization_id,
            first_surviving_method,
            wording,
            evidence,
            (organization_id,),
        )
    return IdentityResolution(None, None, wording, evidence, tuple(sorted(survivors)))


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    return policy.pinned_sources(
        "corridor.organization_identity",
        "corridor.policy",
        "corridor.models",
        "corridor.migrations.c345a9f1d2e3",
    )


def policy_fingerprint() -> tuple[str, str]:
    return POLICY_VERSION, policy.canonical_sha256(
        {
            "policy_version": POLICY_VERSION,
            "normalizer": "case-punctuation-legal-suffix-v1",
            "advanced_tiers": ["facility-class", "contact-continuity", "revision-lineage", "verified-stated-alias"],
            "rules_digest": policy.digest_of_sources(_rule_source_bytes),
        }
    )


def _fingerprint() -> replay_gate.RuleFingerprint:
    version, sha256 = policy_fingerprint()
    return replay_gate.RuleFingerprint(version, sha256)


def replay_human_identity_decisions(
    session: Session, project_id: int
) -> replay_gate.ReplayOutcome:
    """Compare the advanced deterministic stack against recorded human choices.

    This family blocks on an abstention as well as on a contrary answer, and
    deliberately so: the advanced tiers claim to be able to reach every identity
    a person reached from retained evidence, so a case they cannot answer is an
    unexplained difference rather than a permitted silence (ADR-0050's
    "unexplained differences block").
    """

    receipts = session.scalars(
        select(OrganizationIdentityReceipt)
        .where(
            OrganizationIdentityReceipt.project_id == project_id,
            OrganizationIdentityReceipt.method == "human_confirmation",
        )
        .order_by(OrganizationIdentityReceipt.id)
    ).all()

    def recompute(candidate_id: int) -> object:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            return replay_gate.ABSTAINED
        # A confirmation adds its spelling as a future registry alias.  Letting
        # that new alias answer its own replay would prove only that the write
        # happened, not that facility/contact/revision/stated-alias evidence
        # could have made the same decision.  ADR-0050 requires the latter.
        replayed = resolve_candidate_identity(
            session, candidate, permit_advanced=True, permit_exact=False
        )
        if not replayed.resolved:
            return replay_gate.ABSTAINED
        return replayed.external_org_id

    return replay_gate.replay(
        family=replay_gate.FAMILY_ORGANIZATION_IDENTITY,
        human_decisions=[
            (receipt.candidate_id, receipt.external_org_id) for receipt in receipts
        ],
        recompute=recompute,
        abstention_blocks=True,
    )


def activation_status(session: Session, project_id: int) -> str:
    return replay_gate.activation_status(
        session,
        family=replay_gate.FAMILY_ORGANIZATION_IDENTITY,
        project_id=project_id,
        fingerprint=_fingerprint(),
    )


def attempt_activation(session: Session, project_id: int) -> OrganizationIdentityActivation | None:
    """Append an activation only after an ADR-0050 replay genuinely passes."""

    if activation_status(session, project_id) != replay_gate.INACTIVE:
        return None
    replay = replay_human_identity_decisions(session, project_id)
    if not replay.passed:
        return None
    return replay_gate.record_activation(
        session,
        family=replay_gate.FAMILY_ORGANIZATION_IDENTITY,
        project_id=project_id,
        fingerprint=_fingerprint(),
        replay_case_count=replay.case_count,
        reason="regression replay passed on recorded human identity decisions",
        recorded_by=ACTIVATION_ACTOR,
    )


def _append_alias(org: ExternalOrg, wording: str) -> None:
    if any(normalize_organization(value) == normalize_organization(wording) for value in (org.name, *(org.aliases or ()))):
        return
    org.aliases = [*(org.aliases or []), wording]


def _record_resolution(
    session: Session,
    candidate: Candidate,
    resolution: IdentityResolution,
    *,
    actor: str,
    facility_classes: tuple[str, ...] = (),
    add_alias: bool,
    policy_identity: bool,
) -> OrganizationIdentityReceipt:
    if not resolution.resolved or resolution.method is None:
        raise OrganizationIdentityRefusal("an unresolved spelling cannot be recorded as an identity")
    existing = session.scalar(
        select(OrganizationIdentityReceipt).where(
            OrganizationIdentityReceipt.candidate_id == candidate.id,
            OrganizationIdentityReceipt.method == resolution.method,
        )
    )
    if existing is not None:
        return existing
    _registry_lock(session)
    collision = _registered_spelling_matches(session, resolution.stated_wording)
    if collision and {org.id for org in collision} != {resolution.external_org_id}:
        raise OrganizationIdentityRefusal("the source spelling collides with several registered organizations")
    organization = session.get(ExternalOrg, resolution.external_org_id)
    if organization is None:
        raise OrganizationIdentityRefusal("the selected External Organization no longer exists")
    if add_alias:
        _append_alias(organization, resolution.stated_wording)
    version, sha256 = policy_fingerprint()
    receipt = OrganizationIdentityReceipt(
        project_id=candidate.project_id,
        candidate_id=candidate.id,
        external_org_id=organization.id,
        method=resolution.method,
        scope="registry",
        stated_wording=resolution.stated_wording,
        evidence_json=resolution.evidence,
        facility_classes_json=list(facility_classes),
        recorded_by=actor,
        policy_version=version if policy_identity else None,
        policy_sha256=sha256 if policy_identity else None,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def resolve_for_record_inclusion(session: Session, candidate: Candidate) -> IdentityResolution:
    """Resolve and retain a mechanical identity receipt, or leave visible residue."""

    exact = resolve_candidate_identity(session, candidate, permit_advanced=False)
    if exact.resolved:
        _record_resolution(session, candidate, exact, actor=MACHINE_ACTOR, add_alias=False, policy_identity=True)
        return exact
    attempt_activation(session, candidate.project_id)
    if activation_status(session, candidate.project_id) != "active":
        return exact
    advanced = resolve_candidate_identity(session, candidate, permit_advanced=True)
    if advanced.resolved:
        _record_resolution(session, candidate, advanced, actor=MACHINE_ACTOR, add_alias=True, policy_identity=True)
    return advanced


def _facility_classes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    cleaned = tuple(sorted({" ".join(value.split()) for value in values if isinstance(value, str) and value.strip()}))
    if len(cleaned) != len(tuple(values)):
        raise OrganizationIdentityRefusal("facility classes must be non-empty strings")
    return cleaned


def _request_affected_reconsideration(session: Session, wording: str) -> tuple[int, ...]:
    wanted = normalize_organization(wording)
    projects: set[int] = set()
    for candidate in session.scalars(select(Candidate).where(Candidate.state == "pending")):
        if normalize_organization(_source_fields(candidate).get("external_org")) == wanted:
            projects.add(candidate.project_id)
    for project_id in sorted(projects):
        request_record_inclusion(session, project_id, "organization_identity_committed")
    return tuple(sorted(projects))


def confirm_identity(
    session: Session,
    candidate: Candidate,
    *,
    external_org_id: int | None,
    principal: HumanPrincipal,
    facility_classes: tuple[str, ...] | list[str] = (),
) -> OrganizationIdentityReceipt:
    """A coordinator's attributable existing-or-new registry decision.

    The Candidate remains intact and pending.  The committed alias decision
    requests the durable load handoff; only that ordinary admission path may
    subsequently record a Project Record fact.
    """

    principal = require_human_principal(principal)
    if candidate.state != "pending":
        raise OrganizationIdentityRefusal("the source proposal is no longer pending")
    wording = _stated_wording(candidate)
    classes = _facility_classes(facility_classes)
    _registry_lock(session)
    if external_org_id is None:
        if _registered_spelling_matches(session, wording):
            raise OrganizationIdentityRefusal("the spelling now resolves to a registered organization; reload before deciding")
        organization = ExternalOrg(name=wording, org_type="utility", aliases=[])
        session.add(organization)
        session.flush([organization])
    else:
        organization = session.get(ExternalOrg, external_org_id)
        if organization is None:
            raise OrganizationIdentityRefusal("the selected External Organization no longer exists")
    resolution = IdentityResolution(
        organization.id,
        "human_confirmation",
        wording,
        {"source_candidate_id": candidate.id, "selected_external_org_id": organization.id},
        (organization.id,),
    )
    receipt = _record_resolution(
        session,
        candidate,
        resolution,
        actor=principal.subject,
        facility_classes=classes,
        add_alias=True,
        policy_identity=False,
    )
    _request_affected_reconsideration(session, wording)
    return receipt


def confirm_cited_stated_alias(
    session: Session,
    candidate: Candidate,
    *,
    external_org_id: int,
    document_id: int,
    page_no: int,
    quote: str,
    principal: HumanPrincipal,
) -> OrganizationIdentityReceipt:
    """Confirm a quoted alias relationship whose reading is not mechanical.

    Exact ``d/b/a`` passages naming both parties take the automatic tier.  A
    passage that needs a person to read its relationship can still teach the
    registry, but only through this source-bound, cited confirmation.  The
    quote is verified against the Candidate's own retained citation before any
    alias projection changes.
    """

    principal = require_human_principal(principal)
    if candidate.state != "pending":
        raise OrganizationIdentityRefusal("the source proposal is no longer pending")
    wording = _stated_wording(candidate)
    quote = quote.strip()
    payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    if not quote or not any(
        isinstance(citation, dict)
        and citation.get("document_id") == document_id
        and citation.get("page") == page_no
        and citation.get("quote") == quote
        for citation in citations
    ):
        raise OrganizationIdentityRefusal("the cited alias basis is not one of this Candidate's retained citations")
    page = session.scalar(
        select(DocPage).where(DocPage.document_id == document_id, DocPage.page_no == page_no)
    )
    if page is None or quote not in (page.text or "") or not _ALIAS_LANGUAGE.search(quote):
        raise OrganizationIdentityRefusal("the cited alias passage is not verified on its source page")
    organization = session.get(ExternalOrg, external_org_id)
    if organization is None:
        raise OrganizationIdentityRefusal("the selected External Organization no longer exists")
    resolution = IdentityResolution(
        organization.id,
        "human_cited_alias_confirmation",
        wording,
        {
            "document_id": document_id,
            "page_no": page_no,
            "quote": quote,
            "selected_external_org_id": organization.id,
        },
        (organization.id,),
    )
    receipt = _record_resolution(
        session,
        candidate,
        resolution,
        actor=principal.subject,
        add_alias=True,
        policy_identity=False,
    )
    _request_affected_reconsideration(session, wording)
    return receipt


def correct_registered_alias(
    session: Session,
    candidate: Candidate,
    *,
    external_org_id: int,
    principal: HumanPrincipal,
) -> OrganizationIdentityReceipt:
    """Move one previously confirmed *alias* after a new attributable decision.

    A canonical organization name is never repurposed here: that would be a
    merge/rename of an already-recorded organization, which #345 explicitly
    leaves out of scope.  The prior receipt remains immutable; this later
    receipt says exactly which alias moved and who corrected it.
    """

    principal = require_human_principal(principal)
    if candidate.state != "pending":
        raise OrganizationIdentityRefusal("the source proposal is no longer pending")
    wording = _stated_wording(candidate)
    _registry_lock(session)
    existing = _registered_spelling_matches(session, wording)
    if len(existing) != 1:
        raise OrganizationIdentityRefusal("the registered alias is absent or ambiguous; reload before correcting it")
    previous = existing[0]
    if normalize_organization(previous.name) == normalize_organization(wording):
        raise OrganizationIdentityRefusal("correcting a registered organization name is out of scope; only aliases can move")
    replacement = session.get(ExternalOrg, external_org_id)
    if replacement is None:
        raise OrganizationIdentityRefusal("the selected External Organization no longer exists")
    if replacement.id == previous.id:
        raise OrganizationIdentityRefusal("the alias already belongs to that External Organization")
    previous.aliases = [
        alias
        for alias in (previous.aliases or [])
        if normalize_organization(alias) != normalize_organization(wording)
    ]
    _append_alias(replacement, wording)
    receipt = OrganizationIdentityReceipt(
        project_id=candidate.project_id,
        candidate_id=candidate.id,
        external_org_id=replacement.id,
        method="alias_correction",
        scope="registry",
        stated_wording=wording,
        evidence_json={
            "source_candidate_id": candidate.id,
            "previous_external_org_id": previous.id,
            "replacement_external_org_id": replacement.id,
        },
        facility_classes_json=[],
        recorded_by=principal.subject,
        policy_version=None,
        policy_sha256=None,
    )
    session.add(receipt)
    session.flush([receipt])
    _request_affected_reconsideration(session, wording)
    return receipt
