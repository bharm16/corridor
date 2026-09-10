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

from dataclasses import dataclass

from corridor import refusals
from corridor import digests

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    CohortReceipt,
    Document,
    EventCohortReceipt,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.project_lock import lock_project

COHORT_RULE_VERSION = "newly-added-or-n-to-y-or-verification-blocked-v1"

# The event cohort's stated rule (docs/sh99-date-rehearsal.md): a conflict
# enters when at least one commitment, committed-date change, or closure event carrying a
# date references it and the reference matches a dependency Candidate's
# utility_id. Other event types and undated events stay in the default
# lanes; a reference matching nothing selects nothing.
EVENT_COHORT_RULE_VERSION = "dated-commitment-date-change-closure-refs-conflict-v2"

_EVENT_COHORT_EVENT_TYPES = frozenset(
    {"commitment", "committed_date_change", "closure"}
)

NEWLY_ADDED = "newly_added"
CONFLICT_FLAG_N_TO_Y = "conflict_flag_n_to_y"
VERIFICATION_BLOCKED = "verification_blocked"


@dataclass(frozen=True, slots=True)
class CohortClassification:
    """One reason a row is in the cohort, and the words a screen prints for it.

    The rule that assigns a classification is above; this is the same fact said
    in English once. It was said four times before: an ordered pair list in
    ``corridor.web.queue`` whose labels nothing read, a badge map in
    ``queue.html``, a second (differently capitalised) ordered pair list in the
    same template, and a bare ``newly_added`` literal further down it. Four
    spellings of three classifications is how a renamed classification keeps
    rendering under its old heading in one place and disappears from a rail in
    another.
    """

    key: str
    heading: str
    badge: str


# The order the rail groups its members in, which is also the order the rule
# above considers them in.
COHORT_CLASSIFICATIONS: tuple[CohortClassification, ...] = (
    CohortClassification(CONFLICT_FLAG_N_TO_Y, "Flipped N \N{RIGHTWARDS ARROW} Y", "flipped N \N{RIGHTWARDS ARROW} Y"),
    CohortClassification(VERIFICATION_BLOCKED, "Verification blocked", "verification blocked"),
    CohortClassification(NEWLY_ADDED, "Newly added", "newly added"),
)

CLASSIFICATION_ORDER: dict[str, int] = {
    classification.key: index
    for index, classification in enumerate(COHORT_CLASSIFICATIONS)
}

_CONFLICT_FLAG = "potential_conflict"


class CohortDerivationError(refusals.Refusal, ValueError):
    """The cohort could not be derived from exactly declared inputs."""

    refusal_kind = refusals.CONFLICT


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
    # Retained encoding: a stored cohort deduplication key, sealed with
    # non-ASCII escaped.
    canonical = digests.ascii_escaped_json(
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
        }
    )
    return digests.sha256_bytes(canonical)


def derive_event_cohort_receipt(session: Session, project_id: int) -> EventCohortReceipt:
    """Derive and persist the event cohort, or return the identical one.

    A sibling of derive_cohort_receipt for cohorts no Revision Comparison
    selects: the rule reads the project's Candidate stream as its declared
    Active Runs hold it. Refuses when any document holding this project's
    Candidates has no declared Active Run — the cohort stands only on
    declared work — and when an existing receipt under the same rule
    version disagrees with a fresh derivation.
    """
    lock_project(session, project_id)
    runs = _declared_project_runs(session, project_id)

    candidates = session.scalars(
        select(Candidate)
        .where(
            Candidate.project_id == project_id,
            Candidate.extraction_run_id.in_(runs.values()),
        )
        .order_by(Candidate.id)
    ).all()

    referenced_counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.kind != "event":
            continue
        fields = (candidate.payload_json or {}).get("fields", {})
        if fields.get("event_type") not in _EVENT_COHORT_EVENT_TYPES:
            continue
        if not (fields.get("event_date") or fields.get("committed_date")):
            continue
        ref = fields.get("conflict_ref")
        if ref:
            referenced_counts[str(ref)] = referenced_counts.get(str(ref), 0) + 1

    # Party attribution matters only for refs a dated event references: a
    # collision anywhere else cannot select a member, so it cannot block
    # the receipt. A collision on a referenced ref — real in this corpus:
    # SH 99's PL23 is re-attributed "Enterprise" → "UNK (former
    # Enterprise)" across matrix revisions, and NHHIP's 2/13/2026 matrix
    # carries two conflicts both labelled FOC14-69 — excludes that ref by
    # rule, exactly as the rehearsal cohort excludes uncertain
    # correspondences: the cohort never adjudicates ambiguity by accident,
    # and the excluded conflict stays in the default lanes.
    party_by_ref: dict[str, str] = {}
    ambiguous_refs: set[str] = set()
    for candidate in candidates:
        if candidate.kind != "dependency":
            continue
        fields = (candidate.payload_json or {}).get("fields", {})
        utility_id = fields.get("utility_id")
        if not utility_id or str(utility_id) not in referenced_counts:
            continue
        org = str(fields.get("external_org") or "")
        known = party_by_ref.setdefault(str(utility_id), org)
        if known != org:
            ambiguous_refs.add(str(utility_id))

    members = [
        {
            "conflict_ref": ref,
            "external_org": party_by_ref[ref],
            "dated_event_count": count,
        }
        for ref, count in sorted(referenced_counts.items())
        if ref in party_by_ref and ref not in ambiguous_refs
    ]
    input_run_ids = sorted(runs.values())
    digest = _event_cohort_digest(project_id, input_run_ids, members)

    existing = session.scalar(
        select(EventCohortReceipt).where(
            EventCohortReceipt.project_id == project_id,
            EventCohortReceipt.rule_version == EVENT_COHORT_RULE_VERSION,
        )
    )
    if existing is not None:
        # The receipt is checked, never trusted: its stored digest must
        # match its own stored content, and both must match the fresh
        # derivation. The first catches an edit outside the derivation;
        # the second catches a rule change hiding under an old version.
        stored = _event_cohort_digest(
            project_id, list(existing.input_run_ids), list(existing.members)
        )
        if stored != existing.content_sha256 or existing.content_sha256 != digest:
            raise CohortDerivationError(
                "an existing event-cohort receipt disagrees with a fresh "
                "derivation under the same rule version — either the rule "
                "changed without its version changing, or the receipt was "
                "edited outside the derivation"
            )
        return existing

    receipt = EventCohortReceipt(
        project_id=project_id,
        rule_version=EVENT_COHORT_RULE_VERSION,
        input_run_ids=input_run_ids,
        members=members,
        member_count=len(members),
        content_sha256=digest,
    )
    session.add(receipt)
    session.flush([receipt])
    return receipt


def _declared_project_runs(session: Session, project_id: int) -> dict[int, int]:
    """document_id → declared Active Run id for every document holding
    this project's Candidates; refuses when any is undeclared."""
    document_ids = session.scalars(
        select(Candidate.source_document_id)
        .where(Candidate.project_id == project_id)
        .distinct()
    ).all()
    runs: dict[int, int] = {}
    undeclared = 0
    for document_id in document_ids:
        active = session.get(ActiveExtractionRun, document_id)
        if active is None:
            undeclared += 1
        else:
            runs[document_id] = active.extraction_run_id
    if undeclared:
        raise CohortDerivationError(
            f"{undeclared} document(s) holding this project's Candidates "
            "have no declared Active Run — the cohort stands only on "
            "declared work"
        )
    return runs


def _event_cohort_digest(
    project_id: int, input_run_ids: list[int], members: list[dict]
) -> str:
    # Retained encoding: a stored cohort deduplication key, sealed with
    # non-ASCII escaped.
    canonical = digests.ascii_escaped_json(
        {
            "rule_version": EVENT_COHORT_RULE_VERSION,
            "project_id": project_id,
            "input_run_ids": input_run_ids,
            "members": members,
        }
    )
    return digests.sha256_bytes(canonical)


class CohortScopeViolation(refusals.Refusal, ValueError):
    """A mutation named the rehearsal cohort and a Candidate outside it."""

    refusal_kind = refusals.CONFLICT


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


def event_cohort_candidate_ids(
    session: Session, receipt: EventCohortReceipt
) -> frozenset[int]:
    """The live Candidate ids the receipt's members resolve to.

    Members are conflict refs; execution maps them to the pinned input
    runs' Candidates at read time — the member dependencies, and every
    event referencing a member ref. An adjudicated row simply stops
    resolving to pending work; the receipt does not change.
    """
    member_refs = {member["conflict_ref"] for member in receipt.members}
    candidates = session.scalars(
        select(Candidate).where(
            Candidate.extraction_run_id.in_(receipt.input_run_ids)
        )
    ).all()
    resolved = set()
    for candidate in candidates:
        fields = (candidate.payload_json or {}).get("fields", {})
        key = "utility_id" if candidate.kind == "dependency" else "conflict_ref"
        if str(fields.get(key)) in member_refs:
            resolved.add(candidate.id)
    return frozenset(resolved)


def require_event_cohort_member(
    session: Session,
    event_cohort_receipt_id: int,
    candidate_id: int,
    *,
    project_id: int | None = None,
) -> EventCohortReceipt:
    """Refuse a mutation on a Candidate outside the named event cohort.

    The same boundary require_cohort_member holds for the rehearsal lane
    (#175): the check lives at the moment of mutation, where a display
    filter cannot protect anything.
    """
    receipt = session.get(EventCohortReceipt, event_cohort_receipt_id)
    if receipt is None:
        raise CohortScopeViolation(
            f"event cohort receipt {event_cohort_receipt_id} does not exist"
        )
    if project_id is not None and receipt.project_id != project_id:
        raise CohortScopeViolation(
            f"event cohort receipt {receipt.id} belongs to another project"
        )
    if candidate_id not in event_cohort_candidate_ids(session, receipt):
        raise CohortScopeViolation(
            f"candidate {candidate_id} is not a member of event cohort "
            f"receipt {receipt.id} — the lane adjudicates only the pinned set"
        )
    return receipt


def require_cohort_member(
    session: Session,
    cohort_receipt_id: int,
    candidate_id: int,
    *,
    project_id: int | None = None,
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
    if project_id is not None and receipt.project_id != project_id:
        raise CohortScopeViolation(
            f"cohort receipt {receipt.id} belongs to another project"
        )
    if candidate_id not in cohort_candidate_ids(session, receipt):
        raise CohortScopeViolation(
            f"candidate {candidate_id} is not a member of cohort receipt "
            f"{receipt.id} — the rehearsal lane adjudicates only the pinned "
            "set"
        )
    return receipt
