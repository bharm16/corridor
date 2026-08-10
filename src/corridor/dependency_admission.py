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

Only two disagreements still withhold a row, and neither is about what a
conflict says: several rows in one revision sharing an identity, and
revisions naming different External Parties — which asks whether these
are one conflict at all.

Abstention leaves the Candidate pending; the Ledger is never forced; no
model verdict appears anywhere in the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    admit_dependency_by_policy,
)
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    DependencyAdmissionOutcome,
    PolicyRun,
    Document,
    Project,
)
from corridor.project_lock import lock_project

DEPENDENCY_ADMISSION_POLICY_VERSION = "dependency-admission-v1"
FAMILY = "dependency-admission"
ABSTENTION_REASON_VERSION = "dependency-admission-abstentions-v3"
MACHINE_ACTOR = audit.DEPENDENCY_ADMISSION_ACTOR

OUTCOME_ADMITTED = "admitted"
OUTCOME_MERGED = "merged"
OUTCOME_ABSTAINED = "abstained"

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


def run_dependency_admission(
    session: Session, project_id: int
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

    document_ids = declared_matrix_document_ids(session, project_id)
    policy_json = _canonical_policy(session, project, document_ids)
    schemes = identity.document_numbering_schemes(session, project_id)
    aliases = identity.party_canonical_names(session)

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

    admissible: list[tuple[Candidate, list[Candidate]]] = []
    abstentions: list[DependencyAdmissionAbstention] = []

    def abstain(candidates: list[Candidate], reason: str) -> None:
        for candidate in candidates:
            abstentions.append(
                DependencyAdmissionAbstention(
                    candidate_id=candidate.id, reason=reason
                )
            )

    for reason, candidates in unnameable.items():
        if candidates:
            abstain(candidates, reason)

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

        if any(len(group) > 1 for group in stating):
            abstain(participants, "multiple_rows_in_agreement_document")
            continue
        candidates = [group[0] for group in stating]
        if any(not c.citations_verified for c in candidates):
            abstain(candidates, "citations_unverified")
            continue
        # Revisions disagreeing about the *party* is not a field dispute:
        # it asks whether these are one conflict at all, and merging two
        # parties' rows into one record would answer it by accident. That
        # question is Adjudication's (ADR-0031).
        # Resolved through the party's registered aliases, like the
        # identity key above: `Zeta Cable Co` and `Zeta Cable Company` are
        # one company saying one thing, and reading them as two parties
        # would withhold a row over a spelling the registry already
        # reconciles.
        parties = {
            identity.canonical_party(
                (c.payload_json or {}).get("fields", {}).get("external_org") or "",
                aliases,
            )
            for c in candidates
        }
        if len(parties) > 1:
            abstain(candidates, "revisions_disagree_on_party")
            continue
        fields = (candidates[0].payload_json or {}).get("fields", {})
        if not any(v for v in fields.values() if v):
            abstain(candidates, "asserts_nothing")
            continue
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
        if carriers:
            abstain(candidates, "already_admitted")
            continue

        # The newest revision is the primary; the rest merge. Where they
        # state the same values that merge is corroboration, and where
        # they differ it is a Dispute: every revision's claim lands as an
        # Assertion citing its own page, and CONTRADICTION names the
        # fields they disagree about. Disagreement stopped withholding
        # the row (ADR-0031) — the newest revision is the record's
        # provisional reading, said out loud, until a human settles it.
        admissible.append((candidates[-1], candidates[:-1]))

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
        ):
            abstain([primary, *siblings], "write_refused")
            continue
        admitted.append((primary, siblings, dependency))

    run = PolicyRun(
        project_id=project_id,
        family=FAMILY,
        policy_approval_id=None,
        policy_version=DEPENDENCY_ADMISSION_POLICY_VERSION,
        policy_sha256=policy.canonical_sha256(policy_json),
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        applied_count=len(admitted),
        abstained_count=len(abstentions),
    )
    session.add(run)
    session.flush([run])

    for abstention in abstentions:
        session.add(
            DependencyAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=abstention.candidate_id,
                outcome=OUTCOME_ABSTAINED,
                reason=abstention.reason,
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

    session.flush()
    return DependencyAdmissionResult(
        run_id=run.id,
        admitted_count=len(admitted),
        abstained_count=len(abstentions),
        abstentions=abstentions,
    )


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes of the code that decides admission (ADR-0022)."""
    from pathlib import Path

    from corridor import adjudicate as adjudicate_module
    from corridor import models as models_module
    from corridor import principals as principals_module

    paths = (
        ("corridor.dependency_admission", Path(__file__)),
        ("corridor.adjudicate", Path(adjudicate_module.__file__)),
        ("corridor.audit", Path(audit.__file__)),
        ("corridor.identity", Path(identity.__file__)),
        ("corridor.policy", Path(policy.__file__)),
        ("corridor.models", Path(models_module.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        (
            "corridor.migrations.e9a4b7c2d158",
            Path(__file__).parent
            / "migrations/versions/e9a4b7c2d158_add_dependency_admission.py",
        ),
    )
    return tuple((name, path.read_bytes()) for name, path in paths)




def _rules_digest() -> str:
    return policy.digest_of_sources(_rule_source_bytes)


def _canonical_policy(
    session: Session, project: Project, agreement_document_ids: list[int]
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
    return {
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
        ],
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": _rules_digest(),
    }


