"""The rehearsal cohort: a deterministic rule, an immutable receipt.

The first pass of the NHHIP workflow rehearsal is deterministic, not
judgmental (docs/nhhip-workflow-rehearsal.md): every named External Party
row of the successor's Active Run that is newly added, changed N→Y on the
document's conflict flag, or verification-blocked. Correspondences the
matcher could not decide, and rows changed only in other fields, are
excluded by rule and stay in the default lanes.

Membership is derived from the pinned Comparison and its successor inputs
snapshot — never from live Candidate rows, which Adjudication mutates — so
the same inputs always yield the same members and the same digest. The
receipt is what the queue's rehearsal lane reads, and it is a mutation
boundary, not a view (#175).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    CohortReceipt,
    Document,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.project_lock import lock_project

COHORT_RULE_VERSION = "newly-added-or-n-to-y-or-verification-blocked-v1"

NEWLY_ADDED = "newly_added"
CONFLICT_FLAG_N_TO_Y = "conflict_flag_n_to_y"
VERIFICATION_BLOCKED = "verification_blocked"

_CONFLICT_FLAG = "potential_conflict"


class CohortDerivationError(ValueError):
    """The cohort could not be derived from exactly declared inputs."""


def derive_cohort_receipt(
    session: Session, comparison_id: int, *, external_org: str
) -> CohortReceipt:
    """Derive and persist the cohort, or return the identical existing one.

    Refuses when the Comparison is missing or unsealed, when either run is
    not the declared Active Run for its document, or when an existing
    receipt for the same (comparison, rule, party) disagrees with the fresh
    derivation — an impossibility under one rule version, so disagreement
    means the rule changed without its version changing.
    """
    if not isinstance(external_org, str) or not external_org.strip():
        raise CohortDerivationError("external_org must name one External Party")
    external_org = external_org.strip()

    comparison = session.get(RevisionComparisonRun, comparison_id)
    if comparison is None:
        raise CohortDerivationError(f"comparison {comparison_id} does not exist")
    if comparison.sealed_at is None:
        raise CohortDerivationError("the Revision Comparison is not sealed")
    lock_project(session, comparison.project_id)

    for document_id, run_id, side in (
        (
            comparison.predecessor_document_id,
            comparison.predecessor_extraction_run_id,
            "predecessor",
        ),
        (
            comparison.successor_document_id,
            comparison.successor_extraction_run_id,
            "successor",
        ),
    ):
        active = session.get(ActiveExtractionRun, document_id)
        if active is None or active.extraction_run_id != run_id:
            raise CohortDerivationError(
                f"the {side} run is not the declared Active Run for its "
                "document — the cohort stands only on declared work"
            )

    members = _members(session, comparison, external_org)
    digest = _digest(comparison, external_org, members)

    existing = session.scalar(
        select(CohortReceipt).where(
            CohortReceipt.revision_comparison_run_id == comparison.id,
            CohortReceipt.rule_version == COHORT_RULE_VERSION,
            CohortReceipt.external_org == external_org,
        )
    )
    if existing is not None:
        if existing.content_sha256 != digest:
            raise CohortDerivationError(
                "an existing receipt disagrees with a fresh derivation under "
                "the same rule version — the rule changed without its "
                "version changing"
            )
        return existing

    receipt = CohortReceipt(
        project_id=comparison.project_id,
        revision_comparison_run_id=comparison.id,
        predecessor_extraction_run_id=comparison.predecessor_extraction_run_id,
        successor_extraction_run_id=comparison.successor_extraction_run_id,
        external_org=external_org,
        rule_version=COHORT_RULE_VERSION,
        matcher_version=comparison.matcher_version,
        members=members,
        member_count=len(members),
        content_sha256=digest,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def _members(
    session: Session, comparison: RevisionComparisonRun, external_org: str
) -> list[dict]:
    successor_registry_id = session.scalar(
        select(Document.registry_id).where(
            Document.id == comparison.successor_document_id
        )
    )
    snapshots = {
        snapshot["candidate_id"]: snapshot
        for snapshot in comparison.successor_inputs_json
    }

    def fields_of(candidate_id: int) -> dict:
        return (snapshots.get(candidate_id) or {}).get("payload_json", {}).get(
            "fields", {}
        )

    def is_party_row(candidate_id: int) -> bool:
        return fields_of(candidate_id).get("external_org") == external_org

    classified: dict[str, str] = {}

    findings = session.scalars(
        select(RevisionComparisonFinding)
        .where(
            RevisionComparisonFinding.revision_comparison_run_id == comparison.id
        )
        .order_by(RevisionComparisonFinding.ordinal)
    ).all()
    for finding in findings:
        if finding.state == "added":
            [candidate_id] = finding.successor_candidate_ids
            if is_party_row(candidate_id):
                utility_id = fields_of(candidate_id).get("utility_id")
                if utility_id:
                    classified.setdefault(str(utility_id), NEWLY_ADDED)
        elif finding.state == "changed":
            [candidate_id] = finding.successor_candidate_ids
            if not is_party_row(candidate_id):
                continue
            flips = any(
                change.get("field") == _CONFLICT_FLAG
                and str(change.get("before") or "").strip().upper() == "N"
                and str(change.get("after") or "").strip().upper() == "Y"
                for change in finding.field_changes
            )
            if flips:
                utility_id = fields_of(candidate_id).get("utility_id")
                if utility_id:
                    classified.setdefault(str(utility_id), CONFLICT_FLAG_N_TO_Y)
        # 'unchanged' is not rehearsal work; 'dropped' has no successor row
        # to adjudicate; 'ambiguous' and 'unmatched' are excluded by rule —
        # uncertainty stays in the default lanes.

    for snapshot in comparison.successor_inputs_json:
        if snapshot.get("citations_verified"):
            continue
        fields = snapshot.get("payload_json", {}).get("fields", {})
        if fields.get("external_org") != external_org:
            continue
        utility_id = fields.get("utility_id")
        if utility_id:
            # Verification-blocked outranks the comparison classifications:
            # whatever else moved, the row cannot be adjudicated until its
            # citation is resolved, and that is what the lane must say.
            classified[str(utility_id)] = VERIFICATION_BLOCKED

    return [
        {
            "document_registry_id": successor_registry_id,
            "utility_id": utility_id,
            "classification": classification,
        }
        for utility_id, classification in sorted(classified.items())
    ]


def _digest(
    comparison: RevisionComparisonRun, external_org: str, members: list[dict]
) -> str:
    canonical = json.dumps(
        {
            "rule_version": COHORT_RULE_VERSION,
            "comparison_id": comparison.id,
            "comparison_sha256": comparison.content_sha256,
            "predecessor_extraction_run_id": (
                comparison.predecessor_extraction_run_id
            ),
            "successor_extraction_run_id": (
                comparison.successor_extraction_run_id
            ),
            "external_org": external_org,
            "members": members,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class CohortScopeViolation(ValueError):
    """A mutation named the rehearsal cohort and a Candidate outside it."""


def cohort_candidate_ids(
    session: Session, receipt: CohortReceipt
) -> frozenset[int]:
    """The live Candidate ids the receipt's members resolve to.

    Members are registry identities; execution maps them to the successor
    Active Run's Candidates at read time. A member whose row has since been
    adjudicated simply stops resolving to pending work — the receipt does
    not change.
    """
    member_utility_ids = {member["utility_id"] for member in receipt.members}
    candidates = session.scalars(
        select(Candidate).where(
            Candidate.extraction_run_id == receipt.successor_extraction_run_id
        )
    ).all()
    return frozenset(
        candidate.id
        for candidate in candidates
        if str(
            (candidate.payload_json or {}).get("fields", {}).get("utility_id")
        )
        in member_utility_ids
    )


def require_cohort_member(
    session: Session, cohort_receipt_id: int, candidate_id: int
) -> CohortReceipt:
    """Refuse a mutation on a Candidate outside the named cohort.

    The cohort is a mutation boundary, not a view (#175): a display filter
    lets a mistyped URL or a stale tab admit an out-of-scope row; this
    check, at the moment of mutation, cannot.
    """
    receipt = session.get(CohortReceipt, cohort_receipt_id)
    if receipt is None:
        raise CohortScopeViolation(
            f"cohort receipt {cohort_receipt_id} does not exist"
        )
    if candidate_id not in cohort_candidate_ids(session, receipt):
        raise CohortScopeViolation(
            f"candidate {candidate_id} is not a member of cohort receipt "
            f"{receipt.id} — the rehearsal lane adjudicates only the pinned "
            "set"
        )
    return receipt
