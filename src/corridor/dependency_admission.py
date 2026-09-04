"""Mechanical admission of the conflicts a project's matrices can anchor.

ADR-0027 admitted a conflict only where two revisions agreed exactly,
under an authorization a principal signed. ADR-0029 removed both gates,
because together they were the product's front door: a project with one
matrix could not be loaded at all, and a project with two showed nothing
until someone signed for rules they had not yet seen work. What the
machine surfaces is the point; the signature was buying a provenance
claim the verified citation already carries.

So a conflict admits when the revisions that state it can anchor it: the
declared Active Run of each holds exactly one candidate for the
identifier, the citation is verified, the row asserts something, and no
record already carries the reference. One matrix is enough. The newest
revision is admitted and the rest merge, whether or not they agree: an
agreement lands as corroboration, and a disagreement lands as a Dispute
carried on the row, both claims cited to their own pages (ADR-0031).

One existing record is not automatically human residue: re-extracting the
same registered Document can reproduce a Candidate that an earlier run already
associated to exactly one current Dependency.  Where every supported field and
every citation fact is identical, the fresh Candidate repeats no new claim.  It
is mechanically merged to that Dependency with a replay receipt, without
adding another Assertion, Evidence link, or Dependency.  Changed facts,
different Documents, and missing or ambiguous associations still abstain.

Only two disagreements still withhold a row, and neither is about what a
conflict says: several rows in one revision sharing an identity, and
revisions naming different External Parties — which asks whether these
are one conflict at all.

Abstention leaves the Candidate pending; the Ledger is never forced; no
model verdict appears anywhere in the path.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor import identity
from corridor import policy
from corridor.adjudicate import (
    AlreadyAdjudicated,
    CandidateAssertsNothing,
    InvalidCandidateProvenance,
    InvalidCandidateScope,
    MalformedCandidateShape,
    OrganizationIdentityUnresolved,
    admit_dependency_by_policy,
)
from corridor.extraction_runs import extractor_configuration
from corridor.models import (
    ActiveExtractionRun,
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DependencyAdmissionOutcome,
    ExtractionRun,
    PolicyRun,
    Document,
    EvidenceLink,
    Project,
    is_placeholder_party,
)
from corridor.project_lock import lock_project

DEPENDENCY_ADMISSION_POLICY_VERSION = "dependency-admission-v2"
FAMILY = "dependency-admission"
ABSTENTION_REASON_VERSION = "dependency-admission-abstentions-v5"
MACHINE_ACTOR = audit.DEPENDENCY_ADMISSION_ACTOR

OUTCOME_ADMITTED = "admitted"
OUTCOME_MERGED = "merged"
OUTCOME_ABSTAINED = "abstained"

_REPLAY_SOURCE_QUALITY_LISTS = (
    "unmapped_columns",
    "unverified_fields",
    "low_confidence_tokens",
)
_REPLAY_SOURCE_QUALITY_SCALARS = ("tier", "text_source")

# Two reasons are no longer emitted but stay named, because receipts
# written before their decisions still carry them and this set names what
# the column may hold across the family's history:
# `missing_from_agreement_document` (a revision that never mentions a row
# stopped withholding it, ADR-0029) and `revisions_disagree` (a
# disagreement became a Dispute on the row, ADR-0031).
ABSTENTION_REASONS = frozenset(
    {
        "citations_unverified",
        "no_utility_id",
        # The number is stated but the scheme needs more — a per-party
        # row with no stated party has no name (v2, ADR-0030).
        "no_row_identity",
        "missing_from_agreement_document",
        "multiple_rows_in_agreement_document",
        "revisions_disagree",
        # Whether these are one conflict, not what one conflict says
        # (v3, ADR-0031).
        "revisions_disagree_on_party",
        "already_admitted",
        "same_document_replay_unproven",
        # ADR-0051: the source named a party but retained evidence did not
        # determine one registered organization.  This is honest human residue,
        # not a failed write and never a reason to mint a registry row.
        "external_org_identity_unresolved",
        "asserts_nothing",
        "write_refused",
    }
)


@dataclass(frozen=True)
class DependencyAdmissionAbstention:
    candidate_id: int
    reason: str
    reason_version: str = ABSTENTION_REASON_VERSION


@dataclass(frozen=True)
class DependencyAdmissionResult:
    run_id: int
    admitted_count: int
    abstained_count: int
    abstentions: list[DependencyAdmissionAbstention] = field(
        default_factory=list
    )


@dataclass(frozen=True)
class _SameDocumentReplay:
    """One exact re-extraction that may reuse an established association."""

    candidate: Candidate
    dependency: Dependency
    eligibility: dict


@dataclass(frozen=True)
class _SameDocumentReplayIndex:
    """Tri-state replay findings: absent, eligible, or unsafe."""

    eligible: dict[int, _SameDocumentReplay]
    unsafe: dict[int, dict]


def declared_matrix_document_ids(
    session: Session, project_id: int
) -> list[int]:
    """Every current matrix revision the policy reads, oldest date first.

    Order is the record's own: the newest revision's row is the one
    admitted, and older revisions corroborate it. Nothing here elects a
    revision — corroboration only happens between rows that already
    state the same thing.

    A revision the registry has declared replaced is not read at all.
    Every other path into the Ledger already refuses its rows — that is
    what `actionable_candidate_query` means by scope — so including one
    here did not corroborate anything: the write refused, the savepoint
    rolled back, and the *current* revision's own row was lost with it.
    Measured on NHHIP, that was 211 conflicts admitted where 688 should
    have been. Supersession already answers which revision is current
    (ADR-0016); this reads the answer instead of re-deriving one.
    """
    return list(
        session.scalars(
            select(Document.id)
            .join(
                ActiveExtractionRun,
                ActiveExtractionRun.document_id == Document.id,
            )
            .where(
                Document.project_id == project_id,
                Document.doc_type == "matrix",
                Document.superseded_by.is_(None),
            )
            .order_by(Document.doc_date.asc().nulls_first(), Document.id.asc())
        ).all()
    )


def _validated_write_candidate_ids(
    session: Session,
    *,
    project_id: int,
    candidate_kind: str,
    write_candidate_ids: Sequence[int] | None,
) -> tuple[int, ...] | None:
    """Normalize an optional exact mutation boundary against current work."""
    if write_candidate_ids is None:
        return None
    values = tuple(write_candidate_ids)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in values
    ) or len(set(values)) != len(values):
        raise ValueError(
            f"write_candidate_ids must name unique positive {candidate_kind} Candidate ids"
        )

    from corridor.supersession import actionable_candidate_query

    observed = tuple(
        session.scalars(
            actionable_candidate_query(project_id).where(
                Candidate.kind == candidate_kind,
                Candidate.id.in_(values),
            )
        ).all()
    )
    observed_ids = {candidate.id for candidate in observed}
    if observed_ids != set(values):
        raise ValueError(
            "write_candidate_ids must name only current actionable pending "
            f"{candidate_kind} Candidates in project {project_id}"
        )
    return tuple(sorted(values))


def run_dependency_admission(
    session: Session,
    project_id: int,
    *,
    write_candidate_ids: Sequence[int] | None = None,
) -> DependencyAdmissionResult:
    """Admit what the project's declared matrix revisions can anchor.

    No authorization stands in front of this (ADR-0029). The run records
    the exact policy it evaluated — its version, the documents it read
    pinned by content hash, and the deployed bytes of these checks — so
    the receipt still answers "what ran, over what" without a signature
    having been collected first.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    lock_project(session, project_id)

    write_scope = _validated_write_candidate_ids(
        session,
        project_id=project_id,
        candidate_kind="dependency",
        write_candidate_ids=write_candidate_ids,
    )
    writable_ids = set(write_scope) if write_scope is not None else None

    document_ids = declared_matrix_document_ids(session, project_id)
    policy_json = _canonical_policy(
        session,
        project,
        document_ids,
        write_candidate_ids=write_scope,
    )
    policy_sha256 = policy.canonical_sha256(policy_json)
    schemes = identity.document_numbering_schemes(session, project_id)
    aliases = identity.party_canonical_names(session)
    prior_abstentions: dict[int, list[DependencyAdmissionOutcome]] = {}
    for outcome in session.scalars(
        select(DependencyAdmissionOutcome)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project_id,
            PolicyRun.policy_version == DEPENDENCY_ADMISSION_POLICY_VERSION,
            DependencyAdmissionOutcome.outcome == OUTCOME_ABSTAINED,
        )
        .order_by(DependencyAdmissionOutcome.id)
    ):
        prior_abstentions.setdefault(outcome.candidate_id, []).append(outcome)

    # Pending dependency candidates from each agreement document's
    # declared Active Run, grouped by document then by row identity under
    # the document's declared numbering scheme (ADR-0030).
    Identity = tuple[str, str]
    by_document: dict[int, dict[Identity, list[Candidate]]] = {}
    unnameable: dict[str, list[Candidate]] = {
        "no_utility_id": [],
        "no_row_identity": [],
    }
    for document_id in document_ids:
        active = session.get(ActiveExtractionRun, document_id)
        rows: dict[Identity, list[Candidate]] = {}
        candidates = session.scalars(
            select(Candidate)
            .where(
                Candidate.project_id == project_id,
                Candidate.kind == "dependency",
                Candidate.state == "pending",
                Candidate.extraction_run_id == active.extraction_run_id,
            )
            .order_by(Candidate.id)
        ).all()
        for candidate in candidates:
            key = identity.candidate_identity(candidate, schemes, aliases)
            if key is not None:
                rows.setdefault(key, []).append(candidate)
                continue
            fields = (candidate.payload_json or {}).get("fields", {})
            if fields.get("utility_id"):
                # The number is there; the scheme needs the party too.
                unnameable["no_row_identity"].append(candidate)
            else:
                unnameable["no_utility_id"].append(candidate)
        by_document[document_id] = rows

    identities = sorted({key for rows in by_document.values() for key in rows})
    replay_index = _same_document_replay_index(
        session,
        project_id=project_id,
        schemes=schemes,
        aliases=aliases,
        active_candidates=[
            candidate
            for rows in by_document.values()
            for candidates in rows.values()
            for candidate in candidates
        ],
    )

    admissible: list[tuple[Candidate, list[Candidate]]] = []
    replays: list[_SameDocumentReplay] = []
    abstentions: list[DependencyAdmissionAbstention] = []
    abstention_inputs: dict[int, dict] = {}

    def abstain(
        candidates: list[Candidate],
        reason: str,
        *,
        context_candidates: list[Candidate] | None = None,
        carriers: list[Dependency] | None = None,
        replay_inputs: dict[int, dict] | None = None,
    ) -> None:
        group_input = _abstention_group_input(
            context_candidates or candidates,
            carriers=carriers or [],
            aliases=aliases,
        )
        if replay_inputs:
            group_input["same_document_reextraction"] = [
                replay_inputs[candidate.id]
                for candidate in sorted(
                    context_candidates or candidates, key=lambda item: item.id
                )
                if candidate.id in replay_inputs
            ]
        group_sha256 = policy.canonical_sha256(group_input)
        for candidate in candidates:
            input_receipt = _abstention_input_receipt(
                candidate,
                policy_json=policy_json,
                policy_sha256=policy_sha256,
                group_input=group_input,
                group_sha256=group_sha256,
            )
            if (
                reason != "write_refused"
                and policy.has_matching_abstention(
                    prior_abstentions.get(candidate.id, []),
                    input_receipt=input_receipt,
                    verdict=reason,
                    reason_version=ABSTENTION_REASON_VERSION,
                )
            ):
                continue
            abstention_inputs[candidate.id] = input_receipt
            abstentions.append(
                DependencyAdmissionAbstention(
                    candidate_id=candidate.id, reason=reason
                )
            )

    for reason, candidates in unnameable.items():
        for candidate in candidates:
            if writable_ids is None or candidate.id in writable_ids:
                abstain([candidate], reason)

    for key in identities:
        # Only the revisions that state this conflict have anything to say
        # about it. A revision that never mentions the row does not
        # withhold it: one matrix is enough to put a conflict on the
        # record, and a later revision corroborates or disputes it
        # (ADR-0029).
        stating = [
            group for group in (by_document[d].get(key, []) for d in document_ids)
            if group
        ]
        participants = [c for group in stating for c in group]
        write_participants = [
            candidate
            for candidate in participants
            if writable_ids is None or candidate.id in writable_ids
        ]
        if not write_participants:
            continue

        if any(len(group) > 1 for group in stating):
            abstain(
                write_participants,
                "multiple_rows_in_agreement_document",
                context_candidates=participants,
            )
            continue
        candidates = [group[0] for group in stating]
        write_candidates = [
            candidate
            for candidate in candidates
            if writable_ids is None or candidate.id in writable_ids
        ]
        if any(not c.citations_verified for c in candidates):
            abstain(
                write_candidates,
                "citations_unverified",
                context_candidates=candidates,
            )
            continue
        # Revisions disagreeing about the *party* is not a field dispute:
        # it asks whether these are one conflict at all, and merging two
        # parties' rows into one record would answer it by accident. That
        # question is Adjudication's (ADR-0031).
        # Resolved through the party's registered aliases, like the
        # identity key above: `Zeta Cable Co` and `Zeta Cable Company` are
        # one company saying one thing, and reading them as two parties
        # would withhold a row over a spelling the registry already
        # reconciles. A blank or placeholder cell names nobody (ADR-0031
        # withholds only where revisions name different External
        # Parties), so `N/A` against `AT&T` is one named party and a
        # gap, never a disagreement.
        parties = {
            identity.canonical_party(org, aliases)
            for c in candidates
            for org in [
                (c.payload_json or {}).get("fields", {}).get("external_org")
            ]
            if str(org or "").strip() and not is_placeholder_party(org)
        }
        if len(parties) > 1:
            abstain(
                write_candidates,
                "revisions_disagree_on_party",
                context_candidates=candidates,
            )
            continue
        unsafe_replays = {
            candidate.id: replay_index.unsafe[candidate.id]
            for candidate in candidates
            if candidate.id in replay_index.unsafe
        }
        context_replays = [
            replay_index.eligible[candidate.id]
            for candidate in candidates
            if candidate.id in replay_index.eligible
        ]
        scoped_replays = [
            replay
            for replay in context_replays
            if writable_ids is None or replay.candidate.id in writable_ids
        ]
        replay_targets = {replay.dependency.id for replay in context_replays}
        if unsafe_replays or len(replay_targets) > 1:
            replay_inputs = dict(unsafe_replays)
            replay_inputs.update(
                {
                    replay.candidate.id: replay.eligibility
                    for replay in context_replays
                }
            )
            abstain(
                write_candidates,
                "same_document_replay_unproven",
                context_candidates=candidates,
                replay_inputs=replay_inputs,
            )
            continue
        replay_carriers: list[Dependency] = []
        if context_replays and len(replay_targets) == 1:
            # The predecessor association is stronger than the Dependency's
            # denormalized identity columns: it is an attributable link from
            # this exact same-Document claim to one current record.  Reading
            # it before the identity lookup prevents a later correction to a
            # row's projected identity from turning an exact re-extraction
            # into a duplicate Dependency.
            replays.extend(scoped_replays)
            replay_carriers = [context_replays[0].dependency]
            replayed_ids = {
                replay.candidate.id for replay in scoped_replays
            }
            write_candidates = [
                candidate
                for candidate in write_candidates
                if candidate.id not in replayed_ids
            ]
            if not write_candidates:
                continue
        # No "asserts nothing" branch here: a group only exists because
        # its rows carry an identity, and the identity's own fields are
        # claims — the branch that used to sit here could never fire. The
        # write boundary still enforces the rule (CandidateAssertsNothing
        # → write_refused), where it is real rather than vacuous.
        party, uid = key
        carriers = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project_id,
                Dependency.source_ref == uid,
            )
        ).all()
        if party:
            # Under a per-party scheme the number alone names nothing:
            # the record already carries this conflict only if a
            # Dependency with this number belongs to this party.
            carriers = [
                d
                for d in carriers
                if identity.party_matches(session, d, party)
            ]
        carrier_ids = {carrier.id for carrier in carriers}
        for dependency in replay_carriers:
            if dependency.id not in carrier_ids:
                carriers.append(dependency)
        if carriers:
            abstain(
                write_candidates,
                "already_admitted",
                context_candidates=candidates,
                carriers=carriers,
            )
            continue

        # The newest revision is the primary; the rest merge. Where they
        # state the same values that merge is corroboration, and where
        # they differ it is a Dispute: every revision's claim lands as an
        # Assertion citing its own page, and CONTRADICTION names the
        # fields they disagree about. Disagreement stopped withholding
        # the row (ADR-0031) — the newest revision is the record's
        # provisional reading, said out loud, until a human settles it.
        admissible.append((write_candidates[-1], write_candidates[:-1]))

    # Writes are attempted before the receipt exists, inside savepoints,
    # so a refused write abstains that conflict without sinking the batch
    # and the immutable run row is written once, with true counts.
    admitted: list[tuple[Candidate, list[Candidate], Dependency]] = []
    for primary, siblings in admissible:
        try:
            with session.begin_nested():
                dependency = admit_dependency_by_policy(
                    session, primary, siblings, machine_actor=MACHINE_ACTOR
                )
        except (
            AlreadyAdjudicated,
            CandidateAssertsNothing,
            InvalidCandidateProvenance,
            InvalidCandidateScope,
            MalformedCandidateShape,
            OrganizationIdentityUnresolved,
        ) as exc:
            reason = (
                "external_org_identity_unresolved"
                if isinstance(exc, OrganizationIdentityUnresolved)
                else "write_refused"
            )
            abstain([primary, *siblings], reason)
            continue
        admitted.append((primary, siblings, dependency))

    for replay in replays:
        _merge_same_document_reextraction(replay, session=session)

    run = PolicyRun(
        project_id=project_id,
        family=FAMILY,
        policy_approval_id=None,
        policy_version=DEPENDENCY_ADMISSION_POLICY_VERSION,
        policy_sha256=policy_sha256,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        applied_count=len(admitted),
        abstained_count=len(abstentions),
    )
    session.add(run)
    session.flush([run])

    for abstention in abstentions:
        eligibility = {
            "input": abstention_inputs[abstention.candidate_id],
            "verdict": abstention.reason,
            "reason_version": ABSTENTION_REASON_VERSION,
        }
        session.add(
            DependencyAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=abstention.candidate_id,
                outcome=OUTCOME_ABSTAINED,
                reason=abstention.reason,
                eligibility_json=eligibility,
                eligibility_sha256=policy.canonical_sha256(eligibility),
            )
        )
    for primary, siblings, dependency in admitted:
        session.add(
            DependencyAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=primary.id,
                outcome=OUTCOME_ADMITTED,
                dependency_id=dependency.id,
            )
        )
        for sibling in siblings:
            session.add(
                DependencyAdmissionOutcome(
                    policy_run_id=run.id,
                    candidate_id=sibling.id,
                    outcome=OUTCOME_MERGED,
                    dependency_id=dependency.id,
                )
            )
    for replay in replays:
        session.add(
            DependencyAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=replay.candidate.id,
                outcome=OUTCOME_MERGED,
                dependency_id=replay.dependency.id,
            )
        )

    session.flush()
    return DependencyAdmissionResult(
        run_id=run.id,
        admitted_count=len(admitted),
        abstained_count=len(abstentions),
        abstentions=abstentions,
    )


def _same_document_replay_index(
    session: Session,
    *,
    project_id: int,
    schemes: dict,
    aliases: dict,
    active_candidates: list[Candidate],
) -> _SameDocumentReplayIndex:
    """Prove exact same-source replays without reading Ledger conclusions.

    A replay is intentionally narrower than revision Carry-Forward.  It never
    compares two Documents and never treats a matching row identity as enough.
    The earlier and fresh Candidates must state exactly the same supported
    fields and the same citation facts, and every matching predecessor must
    point unambiguously at the same current Dependency.  Anything missing or
    contradictory leaves the fresh Candidate on the ordinary human path.
    """
    facts_by_active_id = {
        candidate.id: facts
        for candidate in active_candidates
        if (facts := _replay_facts(candidate)) is not None
    }
    if not facts_by_active_id:
        return _SameDocumentReplayIndex(eligible={}, unsafe={})

    document_ids = {candidate.source_document_id for candidate in active_candidates}
    predecessors = session.scalars(
        select(Candidate)
        .where(
            Candidate.project_id == project_id,
            Candidate.kind == "dependency",
            Candidate.source_document_id.in_(document_ids),
            Candidate.state.in_(("accepted", "merged")),
        )
        .order_by(Candidate.id)
    ).all()
    if not predecessors:
        return _SameDocumentReplayIndex(eligible={}, unsafe={})

    runs = {
        run.id: run
        for run in session.scalars(
            select(ExtractionRun).where(
                ExtractionRun.id.in_(
                    {
                        candidate.extraction_run_id
                        for candidate in [*active_candidates, *predecessors]
                        if candidate.extraction_run_id is not None
                    }
                )
            )
        )
    }
    # The sealed receipt is stored once by digest and referenced by the run
    # (#605). Resolve it here, once, so the receipt builders below stay pure
    # functions of the rows they were handed.
    configurations = {
        run_id: extractor_configuration(session, run)
        for run_id, run in runs.items()
    }
    active_snapshot_is_exact = {
        candidate.id: _candidate_matches_run_snapshot(candidate, runs)
        for candidate in active_candidates
    }

    predecessor_facts: dict[int, dict] = {}
    predecessor_snapshot_is_exact: dict[int, bool] = {}
    predecessors_by_key: dict[tuple[int, str], list[Candidate]] = {}
    predecessors_by_identity: dict[tuple[int, tuple[str, str]], list[Candidate]] = {}
    predecessors_by_citation: dict[tuple[int, int, str], list[Candidate]] = {}
    for predecessor in predecessors:
        facts = _replay_facts(predecessor)
        if facts is None:
            continue
        predecessor_facts[predecessor.id] = facts
        predecessor_snapshot_is_exact[predecessor.id] = (
            _candidate_matches_run_snapshot(predecessor, runs)
        )
        predecessors_by_key.setdefault(
            (predecessor.source_document_id, policy.canonical_sha256(facts)), []
        ).append(predecessor)
        predecessor_identity = identity.candidate_identity(
            predecessor, schemes, aliases
        )
        if predecessor_identity is not None:
            predecessors_by_identity.setdefault(
                (predecessor.source_document_id, predecessor_identity), []
            ).append(predecessor)
        for citation in facts["citations"]:
            predecessors_by_citation.setdefault(
                (
                    citation["document_id"],
                    citation["page"],
                    citation["quote"],
                ),
                [],
            ).append(predecessor)

    predecessor_ids = list(predecessor_facts)
    if not predecessor_ids:
        return _SameDocumentReplayIndex(eligible={}, unsafe={})
    associations: dict[int, set[int]] = {
        candidate_id: set() for candidate_id in predecessor_ids
    }
    association_sources: dict[int, list[dict]] = {
        candidate_id: [] for candidate_id in predecessor_ids
    }
    outcomes_by_pair: dict[tuple[int, int], list[str]] = {}
    for outcome in session.scalars(
        select(DependencyAdmissionOutcome).where(
            DependencyAdmissionOutcome.candidate_id.in_(predecessor_ids),
            DependencyAdmissionOutcome.outcome.in_((OUTCOME_ADMITTED, OUTCOME_MERGED)),
        )
    ):
        if outcome.dependency_id is not None:
            outcomes_by_pair.setdefault(
                (outcome.candidate_id, outcome.dependency_id), []
            ).append(outcome.outcome)
    durable_replay_pairs = {
        pair
        for pair, outcomes in outcomes_by_pair.items()
        if outcomes == [OUTCOME_MERGED]
    }

    project_dependencies = session.scalars(
        select(Dependency).where(Dependency.project_id == project_id)
    ).all()
    predecessor_id_set = set(predecessor_ids)
    predecessors_by_id = {candidate.id: candidate for candidate in predecessors}
    for dependency_id, records in audit.admission_records_for_dependencies(
        session, [dependency.id for dependency in project_dependencies]
    ).items():
        for record in records:
            if (
                record.attributable
                and record.candidate_id in predecessor_id_set
                and _admission_record_matches_candidate(
                    record,
                    predecessor=predecessors_by_id[record.candidate_id],
                    dependency_id=dependency_id,
                    facts=predecessor_facts[record.candidate_id],
                )
            ):
                associations[record.candidate_id].add(dependency_id)
                association_sources[record.candidate_id].append(
                    {
                        "kind": "admission",
                        "audit_log_id": record.audit_id,
                        "dependency_id": dependency_id,
                        "action": record.action,
                    }
                )

    for entry in session.scalars(
        select(AuditLog)
        .join(Dependency, Dependency.id == AuditLog.entity_id)
        .where(
            Dependency.project_id == project_id,
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.action == audit.REPLAY_DEPENDENCY_CANDIDATE,
            AuditLog.after_json["candidate_id"].astext.in_(
                [str(candidate_id) for candidate_id in predecessor_ids]
            ),
        )
        .order_by(AuditLog.id)
    ):
        after = entry.after_json if isinstance(entry.after_json, dict) else {}
        candidate_id = after.get("candidate_id")
        predecessor = (
            predecessors_by_id.get(candidate_id)
            if isinstance(candidate_id, int) and not isinstance(candidate_id, bool)
            else None
        )
        replay_receipt = after.get("same_document_reextraction")
        if (
            predecessor is None
            or entry.actor != MACHINE_ACTOR
            or entry.human_principal is not None
            or (candidate_id, entry.entity_id) not in durable_replay_pairs
            or predecessor.state != "merged"
            or predecessor.merged_into != entry.entity_id
            or not _replay_receipt_matches_candidate(
                replay_receipt,
                receipt_sha256=after.get("same_document_reextraction_sha256"),
                candidate=predecessor,
                dependency_id=entry.entity_id,
                facts=predecessor_facts[candidate_id],
            )
        ):
            continue
        associations[candidate_id].add(entry.entity_id)
        association_sources[candidate_id].append(
            {
                "kind": "same_document_replay",
                "audit_log_id": entry.id,
                "dependency_id": entry.entity_id,
                "action": entry.action,
            }
        )

    associated_dependency_ids = {
        dependency_id
        for candidate_associations in associations.values()
        for dependency_id in candidate_associations
    }
    current_dependencies = {
        dependency.id: dependency
        for dependency in project_dependencies
        if dependency.id in associated_dependency_ids
        and dependency.dismissed_at is None
    }
    legacy_project_record_support = _legacy_project_record_support_index(
        session, project_dependencies
    )
    eligible: dict[int, _SameDocumentReplay] = {}
    unsafe: dict[int, dict] = {}
    for candidate in active_candidates:
        facts = facts_by_active_id.get(candidate.id)
        if facts is None:
            continue
        exact_matching = predecessors_by_key.get(
            (candidate.source_document_id, policy.canonical_sha256(facts)), []
        )
        # Hashing locates a group; equality is still checked directly so the
        # authority claim never rests on collision resistance alone.
        exact_matching = [
            predecessor
            for predecessor in exact_matching
            if predecessor_facts[predecessor.id] == facts
        ]
        collision_by_id = {
            predecessor.id: predecessor for predecessor in exact_matching
        }
        candidate_identity = identity.candidate_identity(candidate, schemes, aliases)
        if candidate_identity is not None:
            for predecessor in predecessors_by_identity.get(
                (candidate.source_document_id, candidate_identity), []
            ):
                collision_by_id[predecessor.id] = predecessor
        for citation in facts["citations"]:
            for predecessor in predecessors_by_citation.get(
                (
                    citation["document_id"],
                    citation["page"],
                    citation["quote"],
                ),
                [],
            ):
                collision_by_id[predecessor.id] = predecessor
        matching = [collision_by_id[key] for key in sorted(collision_by_id)]
        if not matching:
            continue

        target_ids: set[int] = set()
        predecessor_receipts = []
        association_is_exact = (
            all(predecessor_facts[predecessor.id] == facts for predecessor in matching)
            and active_snapshot_is_exact.get(candidate.id) is True
            and any(
                predecessor.extraction_run_id is not None
                and predecessor.extraction_run_id != candidate.extraction_run_id
                for predecessor in matching
            )
        )
        for predecessor in matching:
            dependency_ids = sorted(associations[predecessor.id])
            snapshot_matches = (
                predecessor_snapshot_is_exact.get(predecessor.id) is True
            )
            legacy_project_record_proof = None
            if (
                len(dependency_ids) == 1
                and len(association_sources[predecessor.id]) == 1
            ):
                legacy_project_record_proof = _legacy_project_record_proof(
                    predecessor=predecessor,
                    dependency_id=dependency_ids[0],
                    facts=facts,
                    runs=runs,
                    configurations=configurations,
                    association_source=association_sources[predecessor.id][0],
                    support=legacy_project_record_support,
                )
            legacy_project_record_matches = legacy_project_record_proof is not None
            predecessor_receipts.append(
                {
                    "candidate_id": predecessor.id,
                    "extraction_run_id": predecessor.extraction_run_id,
                    "run_snapshot_matches_candidate": snapshot_matches,
                    "legacy_project_record_matches_claim": (
                        legacy_project_record_matches
                    ),
                    "legacy_project_record_proof": deepcopy(
                        legacy_project_record_proof
                    ),
                    "proof_basis": (
                        "immutable_run_snapshot"
                        if snapshot_matches
                        else (
                            "legacy_project_record"
                            if legacy_project_record_matches
                            else "unproven"
                        )
                    ),
                    "claim_facts_match": (
                        predecessor_facts[predecessor.id] == facts
                    ),
                    "associated_dependency_ids": dependency_ids,
                    "association_sources": deepcopy(
                        association_sources[predecessor.id]
                    ),
                }
            )
            if (
                len(dependency_ids) != 1
                or len(association_sources[predecessor.id]) != 1
                or not (snapshot_matches or legacy_project_record_matches)
            ):
                association_is_exact = False
                continue
            [dependency_id] = dependency_ids
            if dependency_id not in current_dependencies:
                association_is_exact = False
                continue
            target_ids.add(dependency_id)
        replay_input = {
            "receipt_version": "dependency-admission-same-document-replay-v1",
            "project_id": project_id,
            "source_document_id": candidate.source_document_id,
            "successor_candidate_id": candidate.id,
            "successor_extraction_run_id": candidate.extraction_run_id,
            "successor_configuration": _extraction_configuration(
                candidate, runs, configurations
            ),
            "successor_run_snapshot_matches_candidate": (
                active_snapshot_is_exact.get(candidate.id) is True
            ),
            "predecessor_candidates": predecessor_receipts,
            "predecessor_configurations": [
                {
                    "candidate_id": predecessor.id,
                    "configuration": _extraction_configuration(
                        predecessor, runs, configurations
                    ),
                }
                for predecessor in matching
            ],
            "associated_dependency_ids": sorted(target_ids),
            "supported_fields": deepcopy(facts["supported_fields"]),
            "citations": deepcopy(facts["citations"]),
            "source_quality": deepcopy(facts["source_quality"]),
            # Replay disposes only the duplicate Candidate. It never applies an
            # old field to a Dependency, so later human corrections remain the
            # current Project Record conclusion.
            "replay_effect": {
                "candidate_disposition_only": True,
                "dependency_projection_changed": False,
                "assertion_written": False,
                "evidence_link_written": False,
            },
        }
        if not association_is_exact or len(target_ids) != 1:
            unsafe[candidate.id] = {
                **replay_input,
                "verdict": "unsafe",
            }
            continue

        [dependency_id] = target_ids
        dependency = current_dependencies[dependency_id]
        eligibility = {
            **replay_input,
            "verdict": "eligible",
            "associated_dependency_id": dependency_id,
        }
        eligible[candidate.id] = _SameDocumentReplay(
            candidate=candidate,
            dependency=dependency,
            eligibility=eligibility,
        )
    return _SameDocumentReplayIndex(eligible=eligible, unsafe=unsafe)


def _admission_record_matches_candidate(
    record: audit.AdmissionRecord,
    *,
    predecessor: Candidate,
    dependency_id: int,
    facts: dict,
) -> bool:
    """Require one attributable Admission to state these immutable facts."""
    if record.fields != facts["supported_fields"]:
        return False
    if record.action == audit.ACCEPT_CANDIDATE:
        return predecessor.state == "accepted" and predecessor.merged_into is None
    if record.action == audit.MERGE_CANDIDATE:
        return (
            predecessor.state == "merged"
            and predecessor.merged_into == dependency_id
        )
    if record.action == audit.ADMIT_DEPENDENCY:
        if record.durable_outcome == OUTCOME_ADMITTED:
            return (
                predecessor.state == "accepted"
                and predecessor.merged_into is None
            )
        if record.durable_outcome == OUTCOME_MERGED:
            return (
                predecessor.state == "merged"
                and predecessor.merged_into == dependency_id
            )
        return False
    return False


def _replay_receipt_matches_candidate(
    receipt: object,
    *,
    receipt_sha256: object,
    candidate: Candidate,
    dependency_id: int,
    facts: dict,
) -> bool:
    """Validate a prior replay as one attributable, durable association."""
    return (
        isinstance(receipt, dict)
        and isinstance(receipt_sha256, str)
        and policy.canonical_sha256(receipt) == receipt_sha256
        and receipt.get("receipt_version")
        == "dependency-admission-same-document-replay-v1"
        and receipt.get("verdict") == "eligible"
        and receipt.get("project_id") == candidate.project_id
        and receipt.get("source_document_id") == candidate.source_document_id
        and receipt.get("successor_candidate_id") == candidate.id
        and receipt.get("successor_extraction_run_id")
        == candidate.extraction_run_id
        and receipt.get("associated_dependency_id") == dependency_id
        and receipt.get("associated_dependency_ids") == [dependency_id]
        and receipt.get("supported_fields") == facts["supported_fields"]
        and receipt.get("citations") == facts["citations"]
        and receipt.get("source_quality") == facts["source_quality"]
        and receipt.get("replay_effect")
        == {
            "candidate_disposition_only": True,
            "dependency_projection_changed": False,
            "assertion_written": False,
            "evidence_link_written": False,
        }
    )


def _replay_facts(candidate: Candidate) -> dict | None:
    """The complete claim surface an exact replay is allowed to compare."""
    return _replay_facts_from_values(
        payload=candidate.payload_json,
        source_document_id=candidate.source_document_id,
        citations_verified=candidate.citations_verified,
    )


def _replay_facts_from_values(
    *, payload: object, source_document_id: object, citations_verified: object
) -> dict | None:
    if citations_verified is not True:
        return None
    if not isinstance(payload, dict):
        return None
    fields = payload.get("fields")
    citations = payload.get("citations")
    if not isinstance(fields, dict) or not isinstance(citations, list) or not citations:
        return None
    if any(
        not isinstance(name, str)
        or (value is not None and not isinstance(value, str))
        for name, value in fields.items()
    ):
        return None

    source_quality = {}
    for name in _REPLAY_SOURCE_QUALITY_LISTS:
        value = payload.get(name)
        if name in payload and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            return None
        source_quality[name] = {
            "present": name in payload,
            "value": deepcopy(value),
        }
    for name in _REPLAY_SOURCE_QUALITY_SCALARS:
        value = payload.get(name)
        if name in payload and value is not None and not isinstance(value, str):
            return None
        source_quality[name] = {
            "present": name in payload,
            "value": value,
        }

    citation_facts = []
    for citation in citations:
        if not isinstance(citation, dict):
            return None
        if set(("document_id", "page", "quote", "verified", "whole_row")) - set(
            citation
        ):
            return None
        fact = {
            "document_id": citation["document_id"],
            "page": citation["page"],
            "quote": citation["quote"],
            "verified": citation["verified"],
            "whole_row": citation["whole_row"],
        }
        if (
            not isinstance(fact["document_id"], int)
            or isinstance(fact["document_id"], bool)
            or fact["document_id"] != source_document_id
            or not isinstance(fact["page"], int)
            or isinstance(fact["page"], bool)
            or fact["page"] <= 0
            or not isinstance(fact["quote"], str)
            or not fact["quote"].strip()
            or not isinstance(fact["verified"], bool)
            or fact["verified"] is not True
            or not isinstance(fact["whole_row"], bool)
        ):
            return None
        citation_facts.append(fact)

    citation_facts.sort(
        key=lambda citation: (
            citation["document_id"],
            citation["page"],
            citation["quote"],
            citation["verified"],
            citation["whole_row"],
        )
    )
    return {
        "supported_fields": deepcopy(fields),
        "citations": citation_facts,
        "source_quality": source_quality,
    }


def _candidate_matches_run_snapshot(
    candidate: Candidate, runs: dict[int, ExtractionRun]
) -> bool:
    """Bind mutable Candidate state back to one immutable extractor input."""
    run = runs.get(candidate.extraction_run_id)
    if (
        run is None
        or run.document_id != candidate.source_document_id
        or run.outcome != "completed"
        or run.page_errors != 0
        or not isinstance(run.candidate_inputs_json, list)
    ):
        return False
    snapshots = [
        item
        for item in run.candidate_inputs_json
        if isinstance(item, dict) and item.get("candidate_id") == candidate.id
    ]
    if len(snapshots) != 1:
        return False
    snapshot = snapshots[0]
    if (
        snapshot.get("project_id") != candidate.project_id
        or snapshot.get("kind") != candidate.kind
        or snapshot.get("source_document_id") != candidate.source_document_id
        or snapshot.get("prompt_version") != candidate.prompt_version
        or snapshot.get("model") != candidate.model
        or snapshot.get("state") != "pending"
    ):
        return False
    return _replay_facts(candidate) == _replay_facts_from_values(
        payload=snapshot.get("payload_json"),
        source_document_id=snapshot.get("source_document_id"),
        citations_verified=snapshot.get("citations_verified"),
    )


def _legacy_project_record_support_index(
    session: Session, dependencies: list[Dependency]
) -> dict:
    """Index exact admitted facts retained before run snapshots existed."""

    dependency_ids = [dependency.id for dependency in dependencies]
    if not dependency_ids:
        return {}
    support: dict = {}
    for evidence, assertion in session.execute(
        select(EvidenceLink, Assertion)
        .join(Assertion, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            EvidenceLink.dependency_id.in_(dependency_ids),
            Assertion.dependency_id == EvidenceLink.dependency_id,
        )
        .order_by(EvidenceLink.id, Assertion.id)
    ):
        assert evidence.dependency_id is not None
        citation = (
            evidence.document_id,
            evidence.page_no,
            evidence.quote,
            evidence.verified,
        )
        retained = (
            support.setdefault(evidence.dependency_id, {})
            .setdefault(citation, {})
            .setdefault(
                evidence.id,
                {
                    "evidence_link_id": evidence.id,
                    "document_id": evidence.document_id,
                    "page": evidence.page_no,
                    "quote": evidence.quote,
                    "verified": evidence.verified,
                    "assertions": [],
                },
            )
        )
        retained["assertions"].append(
            {
                "assertion_id": assertion.id,
                "field_name": assertion.field_name,
                "asserted_value": assertion.asserted_value,
                "evidence_link_id": evidence.id,
            }
        )
    return support


def _legacy_project_record_proof(
    *,
    predecessor: Candidate,
    dependency_id: int,
    facts: dict,
    runs: dict[int, ExtractionRun],
    configurations: dict[int, dict | None],
    association_source: dict,
    support: dict,
) -> dict | None:
    """Prove an unsnapshotted predecessor claim from its Project Record.

    Historical runs cannot satisfy a receipt field introduced later.  Their
    original attributable Admission, exact verified Evidence, and Assertions
    remain authoritative. Extractor-only annotations were not retained and are
    named honestly in the receipt rather than manufactured. This fallback accepts
    only that pre-migration shape; a present-but-disagreeing snapshot never falls
    through to it.
    """

    run = runs.get(predecessor.extraction_run_id)
    if (
        run is None
        or run.document_id != predecessor.source_document_id
        or run.outcome != "completed"
        or run.page_errors != 0
        or run.candidate_inputs_json is not None
        or _extraction_configuration(
            predecessor, runs, configurations
        ).get("lineage_status")
        != "historical_unsealed"
        or association_source.get("kind") != "admission"
    ):
        return None
    retained = support.get(dependency_id, {})
    evidence_receipts: list[dict] = []
    assertions_by_fact: dict[tuple[str, str | None], list[dict]] = {}
    for citation in facts["citations"]:
        exact_evidence = retained.get(
            (
                citation["document_id"],
                citation["page"],
                citation["quote"],
                citation["verified"],
            )
        )
        if not exact_evidence:
            return None
        for evidence_id in sorted(exact_evidence):
            evidence = exact_evidence[evidence_id]
            evidence_receipts.append(
                {
                    key: deepcopy(evidence[key])
                    for key in (
                        "evidence_link_id",
                        "document_id",
                        "page",
                        "quote",
                        "verified",
                    )
                }
            )
            for assertion in evidence["assertions"]:
                assertions_by_fact.setdefault(
                    (assertion["field_name"], assertion["asserted_value"]), []
                ).append(assertion)
    required = [
        (field_name, value)
        for field_name, value in facts["supported_fields"].items()
        if value is not None
    ]
    if any(fact not in assertions_by_fact for fact in required):
        return None
    assertion_receipts = [
        deepcopy(assertion)
        for fact in sorted(required)
        for assertion in sorted(
            assertions_by_fact[fact], key=lambda item: item["assertion_id"]
        )
    ]
    return {
        "proof_version": "legacy-project-record-replay-v1",
        "dependency_id": dependency_id,
        "admission_audit_log_id": association_source.get("audit_log_id"),
        "admission_action": association_source.get("action"),
        "evidence_links": evidence_receipts,
        "assertions": assertion_receipts,
        # These extractor annotations were not retained in the historical
        # Project Record. The receipt carries the fresh sealed values but does
        # not falsely claim a historical comparison for them.
        "historically_unretained_extractor_metadata": {
            "citation_whole_row": [
                citation["whole_row"] for citation in facts["citations"]
            ],
            "source_quality": deepcopy(facts["source_quality"]),
        },
        "claim_surface_proved": [
            "supported_fields",
            "citation_document",
            "citation_page",
            "citation_quote",
            "citation_verified",
        ],
    }


def _extraction_configuration(
    candidate: Candidate,
    runs: dict[int, ExtractionRun],
    configurations: dict[int, dict | None],
) -> dict:
    run = runs.get(candidate.extraction_run_id)
    configuration = (
        configurations.get(candidate.extraction_run_id) if run is not None else None
    )
    sealed_values = (
        run.prompt_sha256,
        run.schema_sha256,
        run.postprocessor_sha256,
        configuration,
        run.extractor_config_sha256,
        run.token_usage_json,
    ) if run is not None else ()
    if run is None:
        lineage_status = "missing"
    elif all(value is None for value in sealed_values):
        lineage_status = "historical_unsealed"
    elif all(value is not None for value in sealed_values):
        lineage_status = "sealed"
    else:
        lineage_status = "invalid_partial"
    return {
        "candidate_prompt_version": candidate.prompt_version,
        "candidate_model": candidate.model,
        "run_prompt_version": run.prompt_version if run is not None else None,
        "run_model": run.model if run is not None else None,
        "run_schema_version": run.schema_version if run is not None else None,
        "lineage_status": lineage_status,
        "prompt_sha256": run.prompt_sha256 if run is not None else None,
        "schema_sha256": run.schema_sha256 if run is not None else None,
        "postprocessor_sha256": (
            run.postprocessor_sha256 if run is not None else None
        ),
        "extractor_config_sha256": (
            run.extractor_config_sha256 if run is not None else None
        ),
        "extractor_config_json": deepcopy(configuration),
        "token_usage_json": (
            deepcopy(run.token_usage_json) if run is not None else None
        ),
    }


def _merge_same_document_reextraction(
    replay: _SameDocumentReplay, *, session: Session
) -> None:
    """Dispose one exact replay without adding claims or Evidence to the Ledger."""
    candidate = replay.candidate
    dependency = replay.dependency
    candidate.state = "merged"
    candidate.merged_into = dependency.id
    candidate.adjudicated_at = datetime.now(timezone.utc)
    eligibility_sha256 = policy.canonical_sha256(replay.eligibility)
    audit.record(
        session,
        actor=MACHINE_ACTOR,
        action=audit.REPLAY_DEPENDENCY_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "candidate_id": candidate.id,
            "ref_code": dependency.ref_code,
            "source_ref": dependency.source_ref,
            "role": "same_document_reextraction_replay",
            "fields": deepcopy(replay.eligibility["supported_fields"]),
            "same_document_reextraction": deepcopy(replay.eligibility),
            "same_document_reextraction_sha256": eligibility_sha256,
        },
    )


def _abstention_group_input(
    candidates: list[Candidate],
    *,
    carriers: list[Dependency],
    aliases: dict,
) -> dict:
    """The inspectable rows that made one grouped verdict true."""
    return {
        "candidates": [
            {
                "id": candidate.id,
                "source_document_id": candidate.source_document_id,
                "active_extraction_run_id": candidate.extraction_run_id,
                "payload_json": candidate.payload_json,
                "citations_verified": candidate.citations_verified,
                "state": candidate.state,
            }
            for candidate in sorted(candidates, key=lambda item: item.id)
        ],
        "carriers": [
            {
                "id": dependency.id,
                "source_ref": dependency.source_ref,
                "external_org_id": dependency.external_org_id,
            }
            for dependency in sorted(carriers, key=lambda item: item.id)
        ],
        "aliases": aliases,
    }


def _abstention_input_receipt(
    candidate: Candidate,
    *,
    policy_json: dict,
    policy_sha256: str,
    group_input: dict,
    group_sha256: str,
) -> dict:
    return {
        "receipt_version": "dependency-admission-abstention-input-v1",
        "project_id": candidate.project_id,
        "candidate_id": candidate.id,
        "source_document_id": candidate.source_document_id,
        "active_extraction_run_id": candidate.extraction_run_id,
        "candidate_payload_sha256": policy.canonical_sha256(candidate.payload_json),
        "citations_verified": candidate.citations_verified,
        "candidate_state": candidate.state,
        "policy": policy_json,
        "policy_sha256": policy_sha256,
        "group": group_input,
        "group_sha256": group_sha256,
    }


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes of the code that decides admission (ADR-0022)."""
    from pathlib import Path

    from corridor import adjudicate as adjudicate_module
    from corridor import extraction_runs as extraction_runs_module
    from corridor import models as models_module
    from corridor import principals as principals_module

    paths = (
        ("corridor.dependency_admission", Path(__file__)),
        ("corridor.adjudicate", Path(adjudicate_module.__file__)),
        ("corridor.audit", Path(audit.__file__)),
        ("corridor.extraction_runs", Path(extraction_runs_module.__file__)),
        ("corridor.identity", Path(identity.__file__)),
        ("corridor.policy", Path(policy.__file__)),
        ("corridor.models", Path(models_module.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        (
            "corridor.migrations.e9a4b7c2d158",
            Path(__file__).parent
            / "migrations/versions/e9a4b7c2d158_add_dependency_admission.py",
        ),
        (
            "corridor.migrations.e314a3d8c6f2",
            Path(__file__).parent
            / "migrations/versions/e314a3d8c6f2_dependency_abstention_inputs.py",
        ),
        (
            "corridor.migrations.e6f2a9c7d481",
            Path(__file__).parent
            / "migrations/versions/e6f2a9c7d481_add_revision_comparisons.py",
        ),
        (
            "corridor.migrations.b317c5d7e9f2",
            Path(__file__).parent
            / "migrations/versions/b317c5d7e9f2_seal_extractor_configuration.py",
        ),
    )
    return tuple((name, path.read_bytes()) for name, path in paths)




def _rules_digest() -> str:
    return policy.digest_of_sources(_rule_source_bytes)


def _canonical_policy(
    session: Session,
    project: Project,
    agreement_document_ids: list[int],
    *,
    write_candidate_ids: Sequence[int] | None = None,
) -> dict:
    """What the approval approves: the rules, the code, and the documents.

    The agreement documents are pinned by content hash. A named document
    that no longer exists, or whose bytes changed, makes the policy
    unverifiable — refusal, not a silent re-pin.
    """
    pinned = []
    for document_id in agreement_document_ids:
        document = session.get(Document, document_id)
        if document is None or document.project_id != project.id:
            raise ValueError(
                f"agreement document {document_id} is not part of this project"
            )
        active = session.get(ActiveExtractionRun, document_id)
        if active is None:
            raise ValueError(
                f"agreement document {document_id} has no declared Active Run"
            )
        # The run is pinned alongside the bytes: re-declaring a document's
        # Active Run is a legitimate human act that changes what would
        # admit, so it pauses the policy exactly as a swapped file does.
        # The declared numbering scheme rides with each document because
        # it decides what counts as one row (ADR-0030): re-declaring it
        # changes the digest the receipt records.
        pinned.append(
            {
                "document_id": document.id,
                "sha256": document.sha256,
                "active_extraction_run_id": active.extraction_run_id,
                "numbering_scheme": document.numbering_scheme,
            }
        )
    receipt = {
        "policy_version": DEPENDENCY_ADMISSION_POLICY_VERSION,
        "abstention_reason_version": ABSTENTION_REASON_VERSION,
        "abstention_reasons": sorted(ABSTENTION_REASONS),
        "agreement_documents": pinned,
        "checks": [
            "citations_verified",
            "exactly_one_row_per_agreement_document",
            "reference_not_already_admitted",
            "revisions_agree_on_the_party",
            "row_asserts_something",
            "row_identity_under_declared_numbering_scheme",
            "same_document_reextraction_requires_exact_fields_citations_and_one_current_dependency",
            "same_document_collision_abstains_before_new_dependency",
            "replay_candidate_matches_immutable_extraction_input",
            "replay_predecessor_association_is_attributable_and_durable",
        ],
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": _rules_digest(),
    }
    if write_candidate_ids is not None:
        receipt["write_candidate_ids"] = sorted(write_candidate_ids)
    return receipt
