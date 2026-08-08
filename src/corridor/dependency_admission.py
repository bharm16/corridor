"""Policy-authorized admission of dependencies when revisions agree exactly.

ADR-0027 finishes what ADR-0026 started: a record enters the Ledger by a
human decision or by a named deterministic policy a human authorized —
and stopping that rule at events produced a product whose cold start was
bulk human data entry. The eligibility proof here is exact agreement:
the policy names its agreement documents, pinned by content hash, and a
conflict admits mechanically only when each named document's declared
Active Run holds exactly one candidate for its identifier, their fields
are byte-identical, the citations are verified, and no record already
carries the reference. Two revisions independently asserting the
identical row is stronger evidence than one reviewer glancing at a card.

Where revisions disagree is exactly where human judgment pays, so the
disagreement — with the missing, the duplicated, and the ambiguous — is
all Adjudication ever sees. Abstention leaves the Candidate pending; the
Ledger is never forced; no model verdict appears anywhere in the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
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
    DependencyAdmissionPolicyApproval,
    DependencyAdmissionRun,
    Document,
    Project,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project

DEPENDENCY_ADMISSION_POLICY_VERSION = "dependency-admission-v1"
ABSTENTION_REASON_VERSION = "dependency-admission-abstentions-v1"
MACHINE_ACTOR = audit.DEPENDENCY_ADMISSION_ACTOR

OUTCOME_ADMITTED = "admitted"
OUTCOME_MERGED = "merged"
OUTCOME_ABSTAINED = "abstained"

ABSTENTION_REASONS = frozenset(
    {
        "citations_unverified",
        "no_utility_id",
        "missing_from_agreement_document",
        "multiple_rows_in_agreement_document",
        "revisions_disagree",
        "already_admitted",
        "asserts_nothing",
        "write_refused",
    }
)


class DependencyAdmissionNotAuthorized(RuntimeError):
    """No current approval covers this policy for this project."""


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


def authorize_dependency_admission(
    session: Session,
    project_id: int,
    *,
    principal: HumanPrincipal,
    agreement_document_ids: list[int],
) -> DependencyAdmissionPolicyApproval:
    """Append one human authorization naming the agreement documents.

    The named documents must belong to the project and hold declared
    Active Runs — the policy stands only on declared work — and the
    approval pins each by its content hash, so a swapped file pauses the
    policy exactly as an edited check does.
    """
    principal = require_human_principal(principal)
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    if len(agreement_document_ids) < 2:
        raise ValueError(
            "agreement needs at least two documents — one document alone "
            "cannot corroborate itself"
        )
    lock_project(session, project_id)

    for document_id in agreement_document_ids:
        document = session.get(Document, document_id)
        if document is None or document.project_id != project_id:
            raise ValueError(
                f"document {document_id} is not part of this project"
            )
        if document.doc_type != "matrix":
            raise ValueError(
                f"document {document_id} ({document.filename}) is a "
                f"{document.doc_type} — the exact-agreement policy stands "
                "on matrix revisions; other document types need their own "
                "eligibility rule"
            )
        if session.get(ActiveExtractionRun, document_id) is None:
            raise ValueError(
                f"document {document_id} ({document.filename}) has no "
                "declared Active Run — the policy stands only on declared "
                "work"
            )

    return policy.record_approval(
        session,
        DependencyAdmissionPolicyApproval,
        project_id=project_id,
        policy_version=DEPENDENCY_ADMISSION_POLICY_VERSION,
        policy_json=_canonical_policy(session, project, agreement_document_ids),
        principal=principal,
        action=audit.AUTHORIZE_DEPENDENCY_ADMISSION,
        also_recorded={"agreement_document_ids": list(agreement_document_ids)},
    )


def current_dependency_admission_approval(
    session: Session, project_id: int
) -> DependencyAdmissionPolicyApproval | None:
    """The newest approval, and only if it still describes what would run.

    The named documents are re-read from the approval itself, so a
    re-declared Active Run or a swapped file pauses the policy: the
    recompute raises, and a policy that can no longer be described is not
    a current one.
    """

    def recompute(project: Project, approval) -> dict:
        stored = approval.policy_json.get("agreement_documents", [])
        return _canonical_policy(
            session, project, [int(d["document_id"]) for d in stored]
        )

    return policy.current_approval(
        session,
        DependencyAdmissionPolicyApproval,
        project_id=project_id,
        policy_version=DEPENDENCY_ADMISSION_POLICY_VERSION,
        recompute=recompute,
    )


def run_dependency_admission(
    session: Session, project_id: int
) -> DependencyAdmissionResult:
    """Evaluate the authorized policy over the agreement documents' rows."""
    approval = current_dependency_admission_approval(session, project_id)
    if approval is None:
        raise DependencyAdmissionNotAuthorized(
            "dependency admission requires a current authorization of "
            f"{DEPENDENCY_ADMISSION_POLICY_VERSION} — authorization covers "
            "the rules, and a changed policy needs a new one"
        )
    lock_project(session, project_id)

    document_ids = [
        int(d["document_id"])
        for d in approval.policy_json["agreement_documents"]
    ]

    # Pending dependency candidates from each agreement document's
    # declared Active Run, grouped by document then by utility_id.
    by_document: dict[int, dict[str, list[Candidate]]] = {}
    for document_id in document_ids:
        active = session.get(ActiveExtractionRun, document_id)
        rows: dict[str, list[Candidate]] = {}
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
            fields = (candidate.payload_json or {}).get("fields", {})
            uid = fields.get("utility_id")
            if uid:
                rows.setdefault(str(uid), []).append(candidate)
            else:
                rows.setdefault("", []).append(candidate)
        by_document[document_id] = rows

    all_uids = sorted(
        {uid for rows in by_document.values() for uid in rows if uid}
    )

    admissible: list[tuple[Candidate, list[Candidate]]] = []
    abstentions: list[DependencyAdmissionAbstention] = []

    def abstain(candidates: list[Candidate], reason: str) -> None:
        for candidate in candidates:
            abstentions.append(
                DependencyAdmissionAbstention(
                    candidate_id=candidate.id, reason=reason
                )
            )

    for rows in by_document.values():
        if rows.get(""):
            abstain(rows[""], "no_utility_id")

    for uid in all_uids:
        per_doc = [by_document[d].get(uid, []) for d in document_ids]
        participants = [c for group in per_doc for c in group]

        if any(len(group) > 1 for group in per_doc):
            abstain(participants, "multiple_rows_in_agreement_document")
            continue
        if any(not group for group in per_doc):
            abstain(participants, "missing_from_agreement_document")
            continue
        candidates = [group[0] for group in per_doc]
        if any(not c.citations_verified for c in candidates):
            abstain(candidates, "citations_unverified")
            continue
        contents = {
            _fields_digest((c.payload_json or {}).get("fields", {}))
            for c in candidates
        }
        if len(contents) > 1:
            abstain(candidates, "revisions_disagree")
            continue
        fields = (candidates[0].payload_json or {}).get("fields", {})
        if not any(v for v in fields.values() if v):
            abstain(candidates, "asserts_nothing")
            continue
        existing = session.scalars(
            select(Dependency).where(
                Dependency.project_id == project_id,
                Dependency.source_ref == uid,
            )
        ).first()
        if existing is not None:
            abstain(candidates, "already_admitted")
            continue

        # The primary is the last-named agreement document's row; the
        # rest merge as corroboration.
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

    run = DependencyAdmissionRun(
        project_id=project_id,
        policy_approval_id=approval.id,
        policy_version=approval.policy_version,
        policy_sha256=approval.policy_sha256,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        admitted_count=len(admitted),
        abstained_count=len(abstentions),
    )
    session.add(run)
    session.flush([run])

    for abstention in abstentions:
        session.add(
            DependencyAdmissionOutcome(
                dependency_admission_run_id=run.id,
                candidate_id=abstention.candidate_id,
                outcome=OUTCOME_ABSTAINED,
                reason=abstention.reason,
            )
        )
    for primary, siblings, dependency in admitted:
        session.add(
            DependencyAdmissionOutcome(
                dependency_admission_run_id=run.id,
                candidate_id=primary.id,
                outcome=OUTCOME_ADMITTED,
                dependency_id=dependency.id,
            )
        )
        for sibling in siblings:
            session.add(
                DependencyAdmissionOutcome(
                    dependency_admission_run_id=run.id,
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


def _fields_digest(fields: dict) -> str:
    return policy.canonical_sha256(fields)


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
        pinned.append(
            {
                "document_id": document.id,
                "sha256": document.sha256,
                "active_extraction_run_id": active.extraction_run_id,
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
            "fields_byte_identical_across_documents",
            "reference_not_already_admitted",
            "row_asserts_something",
        ],
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": _rules_digest(),
    }


