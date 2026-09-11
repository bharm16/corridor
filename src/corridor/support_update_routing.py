"""Route the unresolved consequence of ineligible replacement support.

Automatic Support Update (`automatic_carry_forward`) carries only exact, unique,
mechanically verified, unchanged replacement support and leaves every other row
unresolved with a stable Abstention reason (ADR-0034).  The generic customer
"reconfirm the replacement supporting document" ceremony that used to stand in
front of those rows is retired (ADR-0037): a coordinator must instead see the
*specific* project consequence of the change and reach the ordinary decision
authority that already owns it.

This module is the read seam for that routing.  It never writes, never carries
support, and never becomes a new decision authority.  It derives the current
consequences from the same live worklist the released policy reads
(`supersession_review.build_reviewer_worklist`), so a consequence a later human
act or a fresh comparison resolves simply stops being derived — exactly as the
Work List and Disputes are derived rather than stored.

Two things are kept strictly apart (ADR-0034):

* A **domain consequence** is a project question a coordinator can answer —
  changed, ambiguous, dropped, edited, or unsupported replacement support — and
  routes to the supported action that already exists: resolve a Source
  Discrepancy, review documentation against the requirement, verify a failed
  citation, coordinate the uncertain statement, or remove the incorrect entry.
* An **operations problem** is a technical failure — a missing or failed
  extraction, an undeclared Current Production Run, a missing, corrupt, or
  duplicate comparison, or broken admission lineage.  Its project consequence is
  plain ("a newer document needs attention"), but a customer is never asked to
  choose a run id, pick between comparisons, or repair lineage.

The changed-source context is generalized beyond the retired ceremony's display
by reading the immutable Revision Comparison back (`revision_comparison`): exact
retained before/after values, both document identities, the cited passages, and
the page images.  Ambiguous alternatives are preserved as several rows, never
reduced to one invented match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from corridor.models import Document
from corridor.revision_comparison import (
    RevisionComparisonError,
    read_revision_comparison,
)
from corridor.supersession_review import (
    SupersessionReview,
    build_reviewer_worklist,
)


# The supported destinations a domain consequence routes to (ADR-0037).  These
# are existing human decision authorities; routing points at them and never
# writes on their behalf.
SOURCE_DISCREPANCY = "source_discrepancy"
DOCUMENTATION_REVIEW = "documentation_review"
FAILED_CITATION = "failed_citation"
GUIDED_STATEMENT = "guided_statement"
CORRECTION_REMOVAL = "correction_removal"
# A technical failure that stays with Corridor operations, never a customer task.
OPERATIONS = "operations"

DESTINATIONS: frozenset[str] = frozenset(
    {
        SOURCE_DISCREPANCY,
        DOCUMENTATION_REVIEW,
        FAILED_CITATION,
        GUIDED_STATEMENT,
        CORRECTION_REMOVAL,
        OPERATIONS,
    }
)

# Work List Attention Reason code per customer destination.  One specific code
# per destination keeps the coordination question's reason project-specific
# (ADR-0035) while the grouped Work Item still appears once per Constraint.
WORK_LIST_REASONS: dict[str, str] = {
    SOURCE_DISCREPANCY: "support_changed_value",
    DOCUMENTATION_REVIEW: "support_documentation_review",
    FAILED_CITATION: "support_failed_citation",
    GUIDED_STATEMENT: "support_uncertain_match",
    CORRECTION_REMOVAL: "support_dropped_row",
}

# The full set of Work List Attention Reason codes this routing can add, in
# project-consequence order, for the Work List's own ordering table.
WORK_LIST_REASON_CODES: tuple[str, ...] = (
    "support_changed_value",
    "support_documentation_review",
    "support_failed_citation",
    "support_uncertain_match",
    "support_dropped_row",
)

# What a coordinator does next at each customer destination, in the same plain
# project language as ``RoutedSupportConsequence.explanation``.  These five
# sentences were minted inside ``dependency.html`` and existed nowhere else, so
# the screen that named a destination also decided what it asks for; they are
# moved here unchanged, beside the destinations they belong to.  An operations
# problem has no customer next step — a coordinator is never asked to repair
# processing — and the screen prints nothing for it, as it always has.
_DESTINATION_NEXT_STEPS: dict[str, str] = {
    SOURCE_DISCREPANCY: (
        "Resolve the source discrepancy below, or record Needs clarification "
        "to keep it open."
    ),
    DOCUMENTATION_REVIEW: (
        "Review the documentation against the stated requirement below."
    ),
    FAILED_CITATION: (
        "Check the citation on the supporting documents below before it is used."
    ),
    GUIDED_STATEMENT: "Coordinate the correct statement from the kept alternatives.",
    CORRECTION_REMOVAL: (
        "Remove this entry from the active log, or correct it, using the "
        "controls below."
    ),
    OPERATIONS: "",
}


def destination_next_step(destination: str) -> str:
    """The next step one routed destination asks for; empty for operations."""

    if destination not in DESTINATIONS:
        raise ValueError(f"unknown support-update destination {destination!r}")
    return _DESTINATION_NEXT_STEPS[destination]


# Citation-provenance failures are a failed-citation clarification, not a value
# disagreement.
_FAILED_CITATION_REASONS = frozenset(
    {
        "successor_provenance_unsafe",
        "predecessor_support_provenance_unsafe",
    }
)

# A partial or dropped replacement leaves an entry the newer revision no longer
# supports: remove the incorrect entry or correct it (ADR-0037, ADR-0035).
_CORRECTION_REASONS = frozenset(
    {
        "comparison_dropped",
        "partial_scope_transfer",
    }
)

# An uncertain match preserves its alternatives for guided coordination; it is
# never collapsed to one invented correspondence.
_GUIDED_STATEMENT_REASONS = frozenset(
    {
        "comparison_ambiguous",
        "comparison_unmatched",
    }
)

# A changed or edited value is a value disagreement between the recorded
# conclusion and the newer revision.  When the affected support is a readiness
# (documentation-sufficiency) scope it is a Documentation Review instead; that
# split is decided per review from its scope roles.
_VALUE_DISAGREEMENT_REASONS = frozenset(
    {
        "comparison_changed",
        "admission_fields_changed",
    }
)


@dataclass(frozen=True)
class ChangedValue:
    """One field the verified comparison retained a before and after for."""

    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class ComparedRow:
    """One side's exact retained snapshot from a verified comparison.

    Several rows on the successor side preserve an ambiguous alternative set;
    the reader shows every alternative rather than an invented single match.
    """

    candidate_id: int
    document_id: int
    document_name: str
    page_no: int | None
    quote: str | None
    fields: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class ChangedSourceContext:
    """The retained before/after a coordinator reads, from one comparison.

    This is display only.  It reproduces the immutable Revision Comparison
    receipt; it does not recompute the comparison or weaken its evidence.
    """

    comparison_id: int
    finding_id: int | None
    predecessor_document_id: int
    successor_document_id: int
    predecessor_document_name: str
    successor_document_name: str
    changed_values: tuple[ChangedValue, ...]
    predecessor_rows: tuple[ComparedRow, ...]
    successor_rows: tuple[ComparedRow, ...]
    ambiguous: bool


@dataclass(frozen=True)
class RoutedSupportConsequence:
    """One current unresolved consequence of ineligible replacement support.

    ``destination`` names the existing decision authority a coordinator uses;
    ``is_operations`` marks the technical failures that stay with Corridor
    operations.  ``explanation`` is plain project language, never a reason code.
    """

    dependency_id: int
    reason: str
    status: str
    destination: str
    is_operations: bool
    explanation: str
    comparison_id: int | None
    finding_id: int | None
    predecessor_document_id: int | None
    successor_document_id: int | None

    @property
    def is_customer(self) -> bool:
        return not self.is_operations

    @property
    def work_list_reason(self) -> str | None:
        """The Work List Attention Reason code, or None for an operations item."""

        if self.is_operations:
            return None
        return WORK_LIST_REASONS.get(self.destination)

    @property
    def next_step(self) -> str:
        """What a coordinator does next here, in this module's own words."""

        return destination_next_step(self.destination)


def route_support_update_consequences(
    session: Session, project_id: int
) -> tuple[RoutedSupportConsequence, ...]:
    """Every current routed consequence for a project, derived from live state.

    Read from the released policy's own worklist: each dependency-bound row the
    policy would abstain on becomes one routed consequence.  Pure current
    Candidate adjudication (a row with no supersession) is the ordinary
    proposal queue, not a support-update consequence, so it is excluded.

    Deriving the worklist is proportional to the project's supersession work, so
    a project with no superseded document short-circuits on one cheap existence
    query — the Work List build stays bounded when there is nothing to route.
    """

    has_supersession = session.scalar(
        select(
            exists().where(
                Document.project_id == project_id,
                Document.superseded_by.is_not(None),
            )
        )
    )
    if not has_supersession:
        return ()

    worklist = build_reviewer_worklist(session, project_id)
    consequences: list[RoutedSupportConsequence] = []
    for review in worklist.ordinary:
        if review.dependency_id is None:
            continue
        if review.status == "candidate_adjudication":
            continue
        consequences.append(classify_review(review))
    return tuple(consequences)


def customer_consequences_by_dependency(
    session: Session, project_id: int
) -> dict[int, RoutedSupportConsequence]:
    """The one customer-routed consequence per affected Constraint.

    A dependency with several abstaining rows keeps one Work Item; the
    highest-consequence destination controls it, so the coordination question
    appears once rather than one row per revision detail (ADR-0035).
    """

    chosen: dict[int, RoutedSupportConsequence] = {}
    for consequence in route_support_update_consequences(session, project_id):
        if consequence.is_operations:
            continue
        current = chosen.get(consequence.dependency_id)
        if current is None or _customer_priority(consequence) < _customer_priority(
            current
        ):
            chosen[consequence.dependency_id] = consequence
    return chosen


def operations_consequences(
    session: Session, project_id: int
) -> tuple[RoutedSupportConsequence, ...]:
    """The technical support-update failures for the operations screen only."""

    return tuple(
        consequence
        for consequence in route_support_update_consequences(session, project_id)
        if consequence.is_operations
    )


def changed_source_context(
    session: Session, consequence: RoutedSupportConsequence
) -> ChangedSourceContext | None:
    """Reproduce the retained before/after for one consequence, if it has one.

    Only a consequence backed by a readable comparison has context; a technical
    failure without a usable comparison has none, and the customer sees only the
    plain project consequence rather than a broken diff.
    """

    if consequence.comparison_id is None:
        return None
    try:
        readback = read_revision_comparison(session, consequence.comparison_id)
    except RevisionComparisonError:
        return None
    comparison = readback.comparison
    predecessor = session.get(Document, comparison.predecessor_document_id)
    successor = session.get(Document, comparison.successor_document_id)
    if predecessor is None or successor is None:
        return None

    finding = None
    if consequence.finding_id is not None:
        finding = next(
            (
                item
                for item in readback.findings
                if item.id == consequence.finding_id
            ),
            None,
        )

    predecessor_ids: tuple[int, ...] = ()
    successor_ids: tuple[int, ...] = ()
    changed_values: tuple[ChangedValue, ...] = ()
    ambiguous = False
    if finding is not None:
        predecessor_ids = tuple(finding.predecessor_candidate_ids)
        successor_ids = tuple(finding.successor_candidate_ids)
        ambiguous = finding.state == "ambiguous" or len(successor_ids) > 1
        changed_values = tuple(
            ChangedValue(
                field=str(change.get("field")),
                before=change.get("before"),
                after=change.get("after"),
            )
            for change in (finding.field_changes or ())
            if isinstance(change, dict)
        )

    return ChangedSourceContext(
        comparison_id=comparison.id,
        finding_id=consequence.finding_id,
        predecessor_document_id=predecessor.id,
        successor_document_id=successor.id,
        predecessor_document_name=predecessor.filename,
        successor_document_name=successor.filename,
        changed_values=changed_values,
        predecessor_rows=_rows(session, readback.predecessor_inputs, predecessor_ids),
        successor_rows=_rows(session, readback.successor_inputs, successor_ids),
        ambiguous=ambiguous,
    )


def classify_review(review: SupersessionReview) -> RoutedSupportConsequence:
    """Route one dependency-bound abstaining review to its consequence.

    Public so a caller holding a live worklist item (the queue read model) can
    ask whether one review is a customer question or an operations problem
    without re-deriving the whole worklist.
    """

    reason = review.reason or _status_reason(review.status)
    assert review.dependency_id is not None
    base = dict(
        dependency_id=review.dependency_id,
        reason=reason,
        status=review.status,
        comparison_id=review.comparison_id,
        finding_id=review.finding_id,
        predecessor_document_id=review.predecessor_document_id,
        successor_document_id=review.successor_document_id,
    )

    if reason in _FAILED_CITATION_REASONS:
        return RoutedSupportConsequence(
            destination=FAILED_CITATION,
            is_operations=False,
            explanation=(
                "The newer document's supporting passage could not be verified "
                "against its page. A person needs to check the citation before "
                "this supporting document is used."
            ),
            **base,
        )
    if reason in _GUIDED_STATEMENT_REASONS:
        return RoutedSupportConsequence(
            destination=GUIDED_STATEMENT,
            is_operations=False,
            explanation=(
                "The newer document has more than one row that could replace "
                "this supporting document, and the match is not certain. "
                "Coordinate the correct one — the alternatives are kept, and "
                "none is chosen for you."
            ),
            **base,
        )
    if reason in _CORRECTION_REASONS:
        return RoutedSupportConsequence(
            destination=CORRECTION_REMOVAL,
            is_operations=False,
            explanation=(
                "The newer document no longer contains the row this entry "
                "relied on. Remove the entry from the active log or correct it; "
                "it is not carried forward on its own."
            ),
            **base,
        )
    if reason in _VALUE_DISAGREEMENT_REASONS:
        if _has_readiness_scope(review):
            return RoutedSupportConsequence(
                destination=DOCUMENTATION_REVIEW,
                is_operations=False,
                explanation=(
                    "The newer document changes the supporting documentation "
                    "for this requirement. Review whether the current "
                    "documentation still meets the stated requirement."
                ),
                **base,
            )
        return RoutedSupportConsequence(
            destination=SOURCE_DISCREPANCY,
            is_operations=False,
            explanation=(
                "The newer document states a different value than the recorded "
                "conclusion. Resolve which one the record concludes — the "
                "earlier value is kept, and it is not changed on its own."
            ),
            **base,
        )

    # Everything else is a technical failure: a missing or failed extraction, an
    # undeclared run, a missing, corrupt, duplicate, or ambiguous comparison, or
    # broken admission lineage.  The customer sees only the plain consequence.
    return RoutedSupportConsequence(
        destination=OPERATIONS,
        is_operations=True,
        explanation=(
            "A newer document needs attention. Some supporting documents are no "
            "longer current while Corridor operations resolves the processing."
        ),
        **base,
    )


def _has_readiness_scope(review: SupersessionReview) -> bool:
    return any(scope.role == "readiness" for scope in review.superseded_scopes)


def _status_reason(status: str) -> str:
    """A stable reason for a review that carried none of its own."""

    if status in {"changed", "dropped", "ambiguous", "unmatched"}:
        return f"comparison_{status}"
    return status


# Customer destinations, ordered by project consequence so one Constraint keeps
# its sharpest question.  A value the record concludes and a documentation
# requirement outrank an uncertain match or a removal.
_CUSTOMER_DESTINATION_ORDER = {
    SOURCE_DISCREPANCY: 0,
    DOCUMENTATION_REVIEW: 0,
    FAILED_CITATION: 1,
    GUIDED_STATEMENT: 2,
    CORRECTION_REMOVAL: 3,
}


def _customer_priority(consequence: RoutedSupportConsequence) -> tuple[int, int]:
    return (
        _CUSTOMER_DESTINATION_ORDER.get(consequence.destination, 9),
        consequence.finding_id or 0,
    )


def _rows(
    session: Session,
    inputs: tuple[dict[str, Any], ...],
    candidate_ids: tuple[int, ...],
) -> tuple[ComparedRow, ...]:
    """Resolve exact retained snapshots for one side of a finding."""

    by_id = {
        candidate_id: snapshot
        for snapshot in inputs
        if (candidate_id := snapshot.get("candidate_id")) is not None
    }
    rows: list[ComparedRow] = []
    for candidate_id in candidate_ids:
        snapshot = by_id.get(candidate_id)
        if snapshot is None:
            continue
        payload = snapshot.get("payload_json") or {}
        fields = payload.get("fields") or {}
        citation = (payload.get("citations") or [{}])[0]
        source_document_id = snapshot.get("source_document_id")
        document_id = source_document_id if isinstance(source_document_id, int) else 0
        document_name = ""
        if document_id:
            document = session.get(Document, document_id)
            document_name = document.filename if document is not None else ""
        rows.append(
            ComparedRow(
                candidate_id=candidate_id,
                document_id=document_id,
                document_name=document_name,
                page_no=citation.get("page") if isinstance(citation, dict) else None,
                quote=citation.get("quote") if isinstance(citation, dict) else None,
                fields=tuple(
                    sorted(fields.items()) if isinstance(fields, dict) else ()
                ),
            )
        )
    return tuple(rows)
