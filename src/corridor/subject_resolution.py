"""Resolve source references only through exact registered subject aliases.

Names, identifiers, and email keys are evidence, not permission to guess.
This service enumerates the typed registry, records one exact-policy attempt
against an exact Source Segment, and preserves every conflicting or stale
candidate as visible work.  A Human Record Decision may register one alias;
model rankings remain a read-only aid and can never create that decision
(ADR-0070, issue #453).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.models import (
    Dependency,
    Document,
    ExternalOrg,
    Project,
    StatedByPerson,
    SourceSegment,
    SubjectCandidateSuggestion,
    SubjectResolutionAttempt,
    SubjectResolutionCandidate,
    SubjectResolutionDecision,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


RULE_IDENTITY = "exact-registered-alias-v1"
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SubjectTypeContract:
    """One controlled subject kind and its typed exact-reference columns."""

    target_field: str
    reference_kinds: frozenset[str]


SUBJECT_TYPE_CONTRACTS = {
    "external_org": SubjectTypeContract(
        target_field="external_org_id",
        reference_kinds=frozenset(
            {"organization_name", "email_sender", "email_domain"}
        ),
    ),
    "person": SubjectTypeContract(
        target_field="stated_by_person_id",
        reference_kinds=frozenset({"person_name", "person_email"}),
    ),
    "constraint": SubjectTypeContract(
        target_field="dependency_id",
        reference_kinds=frozenset({"source_identifier", "activity_identifier"}),
    ),
    "document": SubjectTypeContract(
        target_field="document_id",
        reference_kinds=frozenset({"document_identifier"}),
    ),
}
_REFERENCE_TYPES = {
    reference_kind: frozenset({subject_type})
    for subject_type, contract in SUBJECT_TYPE_CONTRACTS.items()
    for reference_kind in contract.reference_kinds
}
_SUBJECT_TYPES = frozenset(SUBJECT_TYPE_CONTRACTS)
_USAGES = frozenset({"identity", "statement_speaker", "affected_subject"})


class SubjectResolutionRefusal(ValueError):
    """A subject lookup or Human Record Decision violates its typed boundary."""


@dataclass(frozen=True, slots=True)
class RegisteredSubjectCandidate:
    """One stable read-only subject exposed to prose interpretation."""

    subject_type: str
    subject_id: int
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    """One active or stale exact candidate retained on an attempt."""

    subject_type: str
    subject_id: int
    display_name: str
    state: str
    match_source: str


@dataclass(frozen=True, slots=True)
class SubjectResolutionResult:
    """The immutable receipt returned from an exact subject lookup."""

    attempt_id: int
    state: str
    subject_type: str | None
    subject_id: int | None
    rule_identity: str
    attention_reason: str | None
    candidates: tuple[ResolutionCandidate, ...]


@dataclass(frozen=True, slots=True)
class SubjectResolutionWorkItem:
    """One unresolved source reference and every candidate a person must see."""

    attempt_id: int
    raw_reference: str
    reference_kind: str
    attention_reason: str
    candidates: tuple[ResolutionCandidate, ...]


def normalize_subject_reference(reference_kind: str, value: str) -> str:
    """Normalize presentation-only case and whitespace, never semantic content."""

    _require_reference_kind(reference_kind)
    if not isinstance(value, str) or not value.strip():
        raise SubjectResolutionRefusal("a source reference must be non-empty text")
    normalized = _WHITESPACE.sub(" ", value.strip()).casefold()
    if reference_kind == "email_domain" and normalized.startswith("@"):
        normalized = normalized[1:]
    return normalized


def registered_subject_candidates(
    session: Session, project_id: int
) -> tuple[RegisteredSubjectCandidate, ...]:
    """Enumerate the typed registry without resolving names or writing state."""

    if session.get(Project, project_id) is None:
        raise SubjectResolutionRefusal("the project does not exist")
    aliases = _decision_aliases(session, project_id)
    candidates: list[RegisteredSubjectCandidate] = []

    organizations = session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)).all()
    for organization in organizations:
        candidates.append(
            RegisteredSubjectCandidate(
                subject_type="external_org",
                subject_id=organization.id,
                display_name=organization.name,
                aliases=_unique_aliases(
                    organization.name,
                    (*(organization.aliases or ()), *aliases.get(("external_org", organization.id), ())),
                ),
            )
        )

    people = session.scalars(
        select(StatedByPerson)
        .where(StatedByPerson.project_id == project_id)
        .order_by(StatedByPerson.id)
    ).all()
    for person in people:
        candidates.append(
            RegisteredSubjectCandidate(
                subject_type="person",
                subject_id=person.id,
                display_name=person.display_name,
                aliases=_unique_aliases(
                    person.display_name,
                    (
                        *(person.aliases or ()),
                        *((person.email_normalized,) if person.email_normalized else ()),
                        *aliases.get(("person", person.id), ()),
                    ),
                ),
            )
        )

    dependencies = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_id)
        .order_by(Dependency.id)
    ).all()
    for dependency in dependencies:
        values = tuple(
            value for value in (dependency.source_ref,) if value and value.strip()
        )
        candidates.append(
            RegisteredSubjectCandidate(
                subject_type="constraint",
                subject_id=dependency.id,
                display_name=dependency.ref_code,
                aliases=_unique_aliases(
                    dependency.ref_code,
                    (*values, *aliases.get(("constraint", dependency.id), ())),
                ),
            )
        )

    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.id)
    ).all()
    for document in documents:
        values = tuple(
            value for value in (document.registry_id,) if value and value.strip()
        )
        candidates.append(
            RegisteredSubjectCandidate(
                subject_type="document",
                subject_id=document.id,
                display_name=document.filename,
                aliases=_unique_aliases(
                    document.filename,
                    (*values, *aliases.get(("document", document.id), ())),
                ),
            )
        )
    return tuple(candidates)


def resolve_subject_reference(
    session: Session,
    *,
    project_id: int,
    source_segment_id: int,
    reference_kind: str,
    raw_reference: str,
    expected_subject_type: str,
    usage: str,
) -> SubjectResolutionResult:
    """Apply the released exact-alias rule and retain its complete outcome."""

    _require_reference_kind(reference_kind)
    if expected_subject_type not in _SUBJECT_TYPES:
        raise SubjectResolutionRefusal("the expected subject type is unsupported")
    if usage not in _USAGES:
        raise SubjectResolutionRefusal("the subject-reference usage is unsupported")
    if expected_subject_type not in _REFERENCE_TYPES[reference_kind]:
        raise SubjectResolutionRefusal(
            "the reference kind cannot resolve the requested subject type"
        )
    project = session.get(Project, project_id)
    segment = session.get(SourceSegment, source_segment_id)
    if project is None or segment is None or segment.project_id != project_id:
        raise SubjectResolutionRefusal(
            "subject resolution requires a Source Segment from the same project"
        )
    normalized = normalize_subject_reference(reference_kind, raw_reference)
    if normalized not in _WHITESPACE.sub(" ", segment.exact_text).casefold():
        raise SubjectResolutionRefusal(
            "the source reference is absent from its declared Source Segment"
        )

    if (
        usage == "statement_speaker"
        and expected_subject_type == "external_org"
        and normalized
        in {
            normalize_subject_reference("organization_name", party)
            for party in (project.project_side_parties or ())
            if isinstance(party, str) and party.strip()
        }
    ):
        matches: tuple[ResolutionCandidate, ...] = ()
        state = "actor_boundary"
        attention_reason = "project_side_speaker"
        resolved = None
    else:
        matches = _exact_matches(
            session,
            project_id=project_id,
            reference_kind=reference_kind,
            normalized_reference=normalized,
        )
        active = tuple(candidate for candidate in matches if candidate.state == "active")
        stale = tuple(candidate for candidate in matches if candidate.state == "stale")
        if len(active) == 1 and not stale:
            state = "resolved"
            attention_reason = None
            resolved = active[0]
        elif stale:
            state = "stale"
            attention_reason = "stale_subject_reference"
            resolved = None
        elif len(active) > 1:
            state = "conflict"
            attention_reason = "conflicting_subject_references"
            resolved = None
        else:
            state = "unresolved"
            attention_reason = "unregistered_subject_reference"
            resolved = None

    digest = _attempt_digest(
        project_id=project_id,
        source_segment_id=source_segment_id,
        reference_kind=reference_kind,
        normalized_reference=normalized,
        expected_subject_type=expected_subject_type,
        usage=usage,
        state=state,
        matches=matches,
    )
    existing = session.scalar(
        select(SubjectResolutionAttempt).where(
            SubjectResolutionAttempt.content_sha256 == digest
        )
    )
    if existing is not None:
        return _result(session, existing)

    target = _attempt_target_kwargs(resolved)
    attempt = SubjectResolutionAttempt(
        project_id=project_id,
        source_document_id=segment.document_id,
        source_segment_id=source_segment_id,
        reference_kind=reference_kind,
        raw_reference=raw_reference.strip(),
        normalized_reference=normalized,
        expected_subject_type=expected_subject_type,
        usage=usage,
        state=state,
        attention_reason=attention_reason,
        rule_identity=RULE_IDENTITY,
        content_sha256=digest,
        **target,
    )
    session.add(attempt)
    session.flush([attempt])
    for candidate in matches:
        session.add(
            SubjectResolutionCandidate(
                attempt_id=attempt.id,
                subject_type=candidate.subject_type,
                subject_key=f"{candidate.subject_type}:{candidate.subject_id}",
                display_name=candidate.display_name,
                candidate_state=candidate.state,
                match_source=candidate.match_source,
                **_candidate_target_kwargs(candidate),
            )
        )
    session.flush()
    return _result(session, attempt)


def decide_subject_alias(
    session: Session,
    *,
    attempt_id: int,
    subject_type: str,
    subject_id: int,
    principal: HumanPrincipal,
) -> SubjectResolutionDecision:
    """Register one exact alias through an attributable Human Record Decision."""

    human = require_human_principal(principal)
    attempt = session.get(SubjectResolutionAttempt, attempt_id)
    if attempt is None:
        raise SubjectResolutionRefusal("the subject-resolution work item is absent")
    if attempt.state == "resolved":
        raise SubjectResolutionRefusal("an exact registered alias already resolves this reference")
    if attempt.state == "actor_boundary":
        raise SubjectResolutionRefusal(
            "a project-side speaker cannot be registered as an External Organization"
        )
    selected = next(
        (
            candidate
            for candidate in registered_subject_candidates(session, attempt.project_id)
            if candidate.subject_type == subject_type and candidate.subject_id == subject_id
        ),
        None,
    )
    if selected is None:
        raise SubjectResolutionRefusal("the selected subject is not in this project registry")
    if subject_type != attempt.expected_subject_type:
        raise SubjectResolutionRefusal(
            "the Human Record Decision must preserve the expected subject type"
        )
    if _candidate_state(session, attempt.project_id, selected) != "active":
        raise SubjectResolutionRefusal("a stale subject cannot receive a new alias")
    lock_project(session, attempt.project_id)
    existing = session.scalar(
        select(SubjectResolutionDecision).where(
            SubjectResolutionDecision.project_id == attempt.project_id,
            SubjectResolutionDecision.reference_kind == attempt.reference_kind,
            SubjectResolutionDecision.normalized_reference
            == attempt.normalized_reference,
        )
    )
    if existing is not None:
        if _decision_target(existing) != (subject_type, subject_id):
            raise SubjectResolutionRefusal(
                "the exact alias already has a different Human Record Decision"
            )
        return existing
    outcome = session.scalar(
        select(
            func.record_subject_alias_decision(
                attempt.project_id,
                attempt.id,
                subject_type,
                subject_id,
                human.subject,
                f"subject-alias:{attempt.content_sha256}",
            )
        )
    )
    session.expire_all()
    decision = session.get(SubjectResolutionDecision, int(outcome["decision_id"]))
    if decision is None:
        raise RuntimeError("subject alias decision command returned no decision")
    return decision


def unresolved_subject_work_items(
    session: Session, project_id: int
) -> tuple[SubjectResolutionWorkItem, ...]:
    """Read unresolved exact references not settled by a Human Record Decision."""

    attempts = session.scalars(
        select(SubjectResolutionAttempt)
        .where(
            SubjectResolutionAttempt.project_id == project_id,
            SubjectResolutionAttempt.state != "resolved",
        )
        .order_by(SubjectResolutionAttempt.id)
    ).all()
    decided_aliases = {
        (decision.reference_kind, decision.normalized_reference)
        for decision in session.scalars(
            select(SubjectResolutionDecision).where(
                SubjectResolutionDecision.project_id == project_id
            )
        ).all()
    }
    return tuple(
        SubjectResolutionWorkItem(
            attempt_id=attempt.id,
            raw_reference=attempt.raw_reference,
            reference_kind=attempt.reference_kind,
            attention_reason=attempt.attention_reason or "subject_resolution_required",
            candidates=_stored_candidates(session, attempt.id),
        )
        for attempt in attempts
        if (attempt.reference_kind, attempt.normalized_reference)
        not in decided_aliases
    )


def record_subject_candidate_ranking(
    session: Session,
    *,
    attempt_id: int,
    candidate_ids: tuple[int, ...],
    model: str,
    prompt_version: str,
) -> tuple[SubjectCandidateSuggestion, ...]:
    """Retain model ordering without changing or authorizing resolution."""

    attempt = session.get(SubjectResolutionAttempt, attempt_id)
    if attempt is None or attempt.state == "resolved":
        raise SubjectResolutionRefusal("only unresolved candidates may be ranked")
    if not model.strip() or not prompt_version.strip():
        raise SubjectResolutionRefusal("model rankings require model and prompt identity")
    candidates = session.scalars(
        select(SubjectResolutionCandidate)
        .where(SubjectResolutionCandidate.attempt_id == attempt_id)
        .order_by(SubjectResolutionCandidate.id)
    ).all()
    known = {candidate.id for candidate in candidates}
    if len(candidate_ids) != len(set(candidate_ids)) or any(
        candidate_id not in known for candidate_id in candidate_ids
    ):
        raise SubjectResolutionRefusal(
            "a model ranking may name each retained candidate at most once"
        )
    rows = tuple(
        SubjectCandidateSuggestion(
            attempt_id=attempt.id,
            candidate_id=candidate_id,
            rank=rank,
            model=model.strip(),
            prompt_version=prompt_version.strip(),
        )
        for rank, candidate_id in enumerate(candidate_ids, 1)
    )
    session.add_all(rows)
    session.flush()
    return rows


def _exact_matches(
    session: Session,
    *,
    project_id: int,
    reference_kind: str,
    normalized_reference: str,
) -> tuple[ResolutionCandidate, ...]:
    decisions = session.scalars(
        select(SubjectResolutionDecision).where(
            SubjectResolutionDecision.project_id == project_id,
            SubjectResolutionDecision.reference_kind == reference_kind,
            SubjectResolutionDecision.normalized_reference == normalized_reference,
        )
    ).all()
    if decisions:
        return tuple(_candidate_from_decision(session, decision) for decision in decisions)
    matches: dict[tuple[str, int], ResolutionCandidate] = {}
    for candidate in registered_subject_candidates(session, project_id):
        if candidate.subject_type not in _REFERENCE_TYPES[reference_kind]:
            continue
        values = _candidate_reference_values(
            session, project_id, candidate, reference_kind
        )
        if normalized_reference not in values:
            continue
        state = _candidate_state(session, project_id, candidate)
        matches[(candidate.subject_type, candidate.subject_id)] = ResolutionCandidate(
            subject_type=candidate.subject_type,
            subject_id=candidate.subject_id,
            display_name=candidate.display_name,
            state=state,
            match_source="registered_alias",
        )
    return tuple(matches[key] for key in sorted(matches))


def _candidate_reference_values(
    session: Session,
    project_id: int,
    candidate: RegisteredSubjectCandidate,
    reference_kind: str,
) -> frozenset[str]:
    values: tuple[str, ...]
    if candidate.subject_type == "external_org":
        organization = session.get(ExternalOrg, candidate.subject_id)
        values = (
            (organization.name, *(organization.aliases or ()))
            if reference_kind == "organization_name" and organization is not None
            else ()
        )
    elif candidate.subject_type == "person":
        person = session.get(StatedByPerson, candidate.subject_id)
        if (
            reference_kind == "person_name"
            and person is not None
            and person.project_id == project_id
        ):
            values = (person.display_name, *(person.aliases or ()))
        elif (
            reference_kind == "person_email"
            and person is not None
            and person.project_id == project_id
            and person.email_normalized
        ):
            values = (person.email_normalized,)
        else:
            values = ()
    elif candidate.subject_type == "constraint":
        dependency = session.get(Dependency, candidate.subject_id)
        values = (
            tuple(
                value
                for value in (dependency.ref_code, dependency.source_ref)
                if value and value.strip()
            )
            if dependency is not None
            else ()
        )
    else:
        document = session.get(Document, candidate.subject_id)
        values = (
            tuple(
                value
                for value in (document.registry_id, document.filename)
                if value and value.strip()
            )
            if document is not None
            else ()
        )
    return frozenset(
        normalize_subject_reference(reference_kind, value)
        for value in values
        if value and value.strip()
    )


def _candidate_state(
    session: Session, project_id: int, candidate: RegisteredSubjectCandidate
) -> str:
    if candidate.subject_type == "constraint":
        dependency = session.get(Dependency, candidate.subject_id)
        return "stale" if dependency is None or dependency.dismissed_at is not None else "active"
    if candidate.subject_type == "document":
        document = session.get(Document, candidate.subject_id)
        return "stale" if document is None or document.superseded_by is not None else "active"
    if candidate.subject_type == "person":
        person = session.get(StatedByPerson, candidate.subject_id)
        return (
            "active"
            if person is not None and person.project_id == project_id
            else "stale"
        )
    return "active" if session.get(ExternalOrg, candidate.subject_id) is not None else "stale"


def _candidate_from_decision(
    session: Session, decision: SubjectResolutionDecision
) -> ResolutionCandidate:
    subject_type, subject_id = _decision_target(decision)
    candidate = next(
        (
            item
            for item in registered_subject_candidates(session, decision.project_id)
            if item.subject_type == subject_type and item.subject_id == subject_id
        ),
        None,
    )
    if candidate is None:
        return ResolutionCandidate(
            subject_type=subject_type,
            subject_id=subject_id,
            display_name=f"{subject_type}:{subject_id}",
            state="stale",
            match_source="human_alias_decision",
        )
    return ResolutionCandidate(
        subject_type=subject_type,
        subject_id=subject_id,
        display_name=candidate.display_name,
        state=_candidate_state(session, decision.project_id, candidate),
        match_source="human_alias_decision",
    )


def _decision_aliases(
    session: Session, project_id: int
) -> dict[tuple[str, int], tuple[str, ...]]:
    grouped: dict[tuple[str, int], list[str]] = {}
    decisions = session.scalars(
        select(SubjectResolutionDecision)
        .where(SubjectResolutionDecision.project_id == project_id)
        .order_by(SubjectResolutionDecision.id)
    ).all()
    for decision in decisions:
        key = _decision_target(decision)
        grouped.setdefault(key, []).append(decision.raw_reference)
    return {key: tuple(values) for key, values in grouped.items()}


def _unique_aliases(display_name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    display = _WHITESPACE.sub(" ", display_name.strip()).casefold()
    seen: set[str] = set()
    aliases: list[str] = []
    for value in values:
        normalized = _WHITESPACE.sub(" ", value.strip()).casefold()
        if not normalized or normalized == display or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(value.strip())
    return tuple(aliases)


def _attempt_digest(**values: object) -> str:
    matches = values.pop("matches")
    payload = {
        **values,
        "rule_identity": RULE_IDENTITY,
        "matches": [
            {
                "subject_type": item.subject_type,
                "subject_id": item.subject_id,
                "state": item.state,
                "match_source": item.match_source,
            }
            for item in matches
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _attempt_target_kwargs(candidate: ResolutionCandidate | None) -> dict:
    values = {
        "resolved_external_org_id": None,
        "resolved_stated_by_person_id": None,
        "resolved_dependency_id": None,
        "resolved_document_id": None,
    }
    if candidate is not None:
        values[_target_column(candidate.subject_type, "resolved_")] = candidate.subject_id
    return values


def _candidate_target_kwargs(candidate: ResolutionCandidate) -> dict:
    values = {
        "external_org_id": None,
        "stated_by_person_id": None,
        "dependency_id": None,
        "document_id": None,
    }
    values[_target_column(candidate.subject_type)] = candidate.subject_id
    return values


def _target_column(subject_type: str, prefix: str = "") -> str:
    return f"{prefix}{SUBJECT_TYPE_CONTRACTS[subject_type].target_field}"


def _target_value(row: object, subject_type: str, prefix: str = "") -> int | None:
    return getattr(row, _target_column(subject_type, prefix))


def _decision_target(decision: SubjectResolutionDecision) -> tuple[str, int]:
    value = _target_value(decision, decision.subject_type)
    if value is None:
        raise SubjectResolutionRefusal("the stored subject decision has no typed target")
    return decision.subject_type, value


def _stored_candidates(
    session: Session, attempt_id: int
) -> tuple[ResolutionCandidate, ...]:
    rows = session.scalars(
        select(SubjectResolutionCandidate)
        .where(SubjectResolutionCandidate.attempt_id == attempt_id)
        # Insertion order, then sorted on the typed target below. Ordering on
        # `subject_key` sorted the string "constraint:<id>", so "…:100" came
        # before "…:99" and the candidate order inverted whenever the ids
        # straddled a digit-length boundary — which depends on how much else
        # had been written to the database, not on the record. A
        # coordinator-facing list orders by what it means (ADR-0035, ADR-0085).
        .order_by(SubjectResolutionCandidate.id)
    ).all()
    candidates = tuple(
        ResolutionCandidate(
            subject_type=row.subject_type,
            subject_id=_target_value(row, row.subject_type),
            display_name=row.display_name,
            state=row.candidate_state,
            match_source=row.match_source,
        )
        for row in rows
    )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (candidate.subject_type, candidate.subject_id),
        )
    )


def _result(
    session: Session, attempt: SubjectResolutionAttempt
) -> SubjectResolutionResult:
    target = next(
        (
            (subject_type, _target_value(attempt, subject_type, "resolved_"))
            for subject_type in SUBJECT_TYPE_CONTRACTS
            if _target_value(attempt, subject_type, "resolved_") is not None
        ),
        (None, None),
    )
    return SubjectResolutionResult(
        attempt_id=attempt.id,
        state=attempt.state,
        subject_type=target[0],
        subject_id=target[1],
        rule_identity=attempt.rule_identity,
        attention_reason=attempt.attention_reason,
        candidates=_stored_candidates(session, attempt.id),
    )


def _require_reference_kind(reference_kind: str) -> None:
    if reference_kind not in _REFERENCE_TYPES:
        raise SubjectResolutionRefusal("the subject-reference kind is unsupported")
