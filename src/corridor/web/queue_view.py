"""The adjudication queue's reading: which one row, and why it is in front.

The queue handler was 309 lines of route body — the largest single reading in
`web/app.py` — and it mixed four unrelated jobs: validating the lane's pinned
input manifest, deciding which Extracted Proposals are actionable, choosing the
one row to present, and composing everything the screen prints about it. This
module is that reading, in the shape ``corridor.web.follow_up_view`` and
``corridor.web.issue_section`` already use for the adopted screens: one public
entry, frozen dataclasses, and no decision of its own.

**Membership is the receipt's fact and the lane never widens it.** A pinned
cohort presents exactly its own members intersected with what is ordinarily
actionable. Selecting a row from the rail resolves through the same actionable
scope the default pick uses, so the rail is navigation and never a widening, and
the mutation boundary stays server-side at accept/edit/merge.

**Why the row is here comes from the machine's own recorded refusal.** Under the
admission policies a row reaches Adjudication only because the machine refused
to admit it, with a stated reason; ``REVIEW_REASONS`` turns that reason into the
coordinator's question. Nothing here decides that a row needs review.

**The classification vocabulary is read, not restated.**
``cohort.COHORT_CLASSIFICATIONS`` is the one ordered sequence of (classification,
heading, badge words). It used to be spelled four times: an ordered pair list
here whose labels nothing read, a badge map in ``queue.html``, a second
differently-capitalised ordered pair list in the same template, and a bare
``newly_added`` literal further down it.

**No clock.** Nothing here reads the day. ``_sibling_revisions`` orders by the
document dates it was given.

Terminology: nothing here coins a customer word. Utility Conflict, Extracted
Proposal, Constraint and Follow-up Plan come from the adopted glossary through
``corridor.presentation`` and the readers below; "test input manifest" is the
wording the rehearsal lane already prints for a cohort receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.cohort import (
    COHORT_CLASSIFICATIONS,
    NEWLY_ADDED,
    CohortClassification,
    cohort_candidate_ids,
    event_cohort_candidate_ids,
)
from corridor.identity import document_numbering_schemes, party_canonical_names
from corridor.lane import conflict_key, same_conflict
from corridor.models import (
    ActiveExtractionRun,
    AuditLog,
    Candidate,
    CohortReceipt,
    DependencyAdmissionOutcome,
    Dependency,
    Document,
    EventCohortReceipt,
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.organization_identity import (
    OrganizationIdentityRefusal,
    resolve_candidate_identity,
)
from corridor.statement_coordination import pending_candidate_authority_gap
from corridor.supersession_review import build_reviewer_worklist
from corridor.support_update_routing import (
    classify_review,
    customer_consequences_by_dependency,
)
from corridor.event_admission import waiting_statements
from corridor.web.dependency_view import PlanForm, plan_form
from corridor.web.queue import (
    build_cohort_rail,
    build_evidence,
    build_supersession_review_view,
    build_view,
    change_strip,
    cohort_openable_dependency_ids,
    default_cohort_dependency_id,
    member_classification,
    next_candidate,
    next_incomplete_cohort_dependency_id,
    pending_counts,
)

CLASSIFICATIONS_BY_KEY: Mapping[str, CohortClassification] = {
    classification.key: classification for classification in COHORT_CLASSIFICATIONS
}


class LaneManifestRequired(ValueError):
    """A pinned lane was asked for without naming its input manifest."""


class NoSuchLaneManifest(LookupError):
    """This project holds no input manifest by that identifier."""


class ReturnContextRefused(ValueError):
    """A return link does not name this exact pinned coordination context."""


# What each abstention means to the person now holding the row. The machine's
# vocabulary is precise and the reviewer's question is different: not "which
# check failed" but "what am I deciding".
REVIEW_REASONS: Mapping[str, tuple[str, str]] = {
    "revisions_disagree": (
        "The revisions disagree about this conflict.",
        "Both pages are shown. Add the revision that is right, or edit "
        "the values before adding the record.",
    ),
    "missing_from_agreement_document": (
        "Only one revision has this conflict.",
        "It was added or dropped between revisions. Add it if the "
        "record should carry it.",
    ),
    "multiple_rows_in_agreement_document": (
        "One revision lists this conflict twice.",
        "Two rows share an identifier. Add the one that is right and "
        "choose Do not add for the other.",
    ),
    "citations_unverified": (
        "The quote could not be found on the cited page.",
        "Check the page before adding anything from this row.",
    ),
    "already_admitted": (
        "A record already carries this identifier.",
        "Merge into the existing record to avoid adding a duplicate.",
    ),
    "same_document_replay_unproven": (
        "This source row was previously handled, but safe replay is not proven.",
        "Its extracted facts or current Constraint association no longer prove "
        "an exact replay. Keep it pending and review the cited passages.",
    ),
    "external_org_identity_unresolved": (
        "External Organization identity not established.",
        "The source wording does not yet determine one registered organization. "
        "Confirm the organization from its source context; Corridor will not create one silently.",
    ),
    "asserts_nothing": (
        "This row states nothing.",
        "An identifier with no values is bookkeeping, not a conflict.",
    ),
    "write_refused": (
        "The record could not be written from this row.",
        "Something in the row's own shape stopped it. Read the fields "
        "before deciding.",
    ),
    "no_utility_id": (
        "This row has no identifier.",
        "Nothing can name it in the record as it stands.",
    ),
    "revisions_disagree_on_party": (
        "The revisions name different organizations for this conflict.",
        "That asks whether these are one conflict at all, which is not "
        "something the machine may answer. Read both pages and add the "
        "one that is right.",
    ),
    "no_row_identity": (
        "This row's number needs an organization to identify it.",
        "This document numbers each organization's conflicts separately, and the "
        "row states no organization — check the page and fill in what it shows.",
    ),
}

# The lane that no longer exists. Exact unchanged support moves through the
# managed automatic path with no customer confirmation, and the specific
# consequence of every other case is a Work List question, so the retired
# ceremony sends the coordinator there.
RECONFIRMATION_LANE = "reconfirmation"


@dataclass(frozen=True, slots=True)
class QueueView:
    """One queue request's reading: the lane, its counts, and the one open row.

    ``template`` and ``redirect_to`` are part of the reading because what to
    render is decided by what is left to do: a lane with no actionable row and
    no open coordination strip is a different page, not a different branch of
    one. The handler renders what this names and adds nothing.
    """

    project: Project
    lane: str
    template: str
    lane_url: str
    summary_url: str
    next_coordinate_url: str
    remaining: int
    waiting_statements: int
    candidate_count: int
    support_update_count: int
    ordinary_reviews: Sequence[Any]
    cohort_receipt: CohortReceipt | None = None
    event_cohort_receipt: EventCohortReceipt | None = None
    rail: Sequence[Any] | None = None
    coordinate_dependency: Dependency | None = None
    plan: PlanForm | None = None
    candidate: Candidate | None = None
    view: Any = None
    review_headline: str | None = None
    review_guidance: str | None = None
    identity_card: Mapping[str, Any] | None = None
    candidate_authority_gap: Any = None
    unresolved_acknowledgments: Sequence[AuditLog] = ()
    differences: Sequence[Mapping[str, Any]] = ()
    evidence_panels: Sequence[Any] = ()
    siblings: Sequence[Any] = ()
    classification: CohortClassification | None = None
    changes: Sequence[Any] = ()
    redirect_to: str | None = None

    @property
    def classifications(self) -> tuple[CohortClassification, ...]:
        """The rail's groups, in the order the cohort rule considers them."""
        return COHORT_CLASSIFICATIONS

    @property
    def row_is_new(self) -> bool:
        """Whether the open row exists only in the successor revision.

        The classification key is compared here against the constant the cohort
        rule assigns, not spelled as a literal in a template — the template used
        to carry ``classification == "newly_added"`` as a bare string.
        """
        return self.classification is not None and self.classification.key == NEWLY_ADDED

    def rail_members(self, classification: CohortClassification) -> tuple[Any, ...]:
        """The rail entries of one classification, in the rail's own order."""
        return tuple(
            entry
            for entry in (self.rail or ())
            if entry.classification == classification.key
        )


def queue_view(
    session: Session,
    *,
    project: Project,
    lane: str = "candidate",
    historical_document_id: int | None = None,
    cohort_receipt_id: int | None = None,
    event_cohort_receipt_id: int | None = None,
    candidate_id: int | None = None,
    coordinate: int | None = None,
    summary: bool = False,
) -> QueueView:
    """Read one queue request: the lane's scope, its counts, and the open row."""

    cohort_receipt = (
        _cohort_receipt(session, project, cohort_receipt_id)
        if lane == "rehearsal"
        else None
    )
    event_cohort_receipt = (
        _event_cohort_receipt(session, project, event_cohort_receipt_id)
        if lane == "events"
        else None
    )

    worklist = build_reviewer_worklist(session, project.id)
    ordinary_candidate_ids = frozenset(
        successor_id
        for review in worklist.ordinary
        for successor_id in review.successor_candidate_ids
    )
    allowed_candidate_ids = (
        None if historical_document_id is not None else ordinary_candidate_ids
    )
    if cohort_receipt is not None:
        # The lane presents exactly the receipt's members — the pinned set
        # intersected with what is ordinarily actionable, never widened.
        allowed_candidate_ids = ordinary_candidate_ids & cohort_candidate_ids(
            session, cohort_receipt
        )
    if event_cohort_receipt is not None:
        allowed_candidate_ids = ordinary_candidate_ids & event_lane_dependency_ids(
            session, event_cohort_receipt
        )

    # Only the technical failures a customer cannot act on are shown here as a
    # plain "supporting documents unavailable" note; a changed, dropped,
    # ambiguous, or edited row is a routed coordination question that appears
    # once on the Work List, never twice (ADR-0034, ADR-0037).
    ordinary_reviews = [
        build_supersession_review_view(session, review)
        for review in worklist.ordinary
        if not review.successor_candidate_ids
        and review.dependency_id is not None
        and classify_review(review).is_operations
    ]
    remaining, _verified = pending_counts(
        session,
        project.id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )
    lane_url = _lane_url(project.slug, cohort_receipt, event_cohort_receipt)
    counts = {
        "project": project,
        "lane": lane,
        "remaining": remaining,
        # A count, not the pile itself: the queue points at it and never
        # becomes a second place to work statements.
        "waiting_statements": len(waiting_statements(session, project.id)),
        "cohort_receipt": cohort_receipt,
        "event_cohort_receipt": event_cohort_receipt,
        "candidate_count": remaining + len(ordinary_reviews),
        # The retired generic reconfirmation lane is gone; the count that used
        # to invite a customer to reconfirm now points at the specific routed
        # coordination questions on the Work List.
        "support_update_count": len(
            customer_consequences_by_dependency(session, project.id)
        ),
        "ordinary_reviews": ordinary_reviews,
        "lane_url": lane_url,
        "summary_url": cohort_summary_location(lane_url),
    }
    if lane == RECONFIRMATION_LANE:
        return QueueView(
            **counts,
            template="",
            next_coordinate_url=lane_url,
            redirect_to=f"/work/{project.slug}",
        )

    candidate = _open_candidate(
        session,
        project,
        candidate_id=candidate_id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )
    rail = (
        build_cohort_rail(
            session, cohort_receipt, candidate.id if candidate is not None else None
        )
        if cohort_receipt is not None
        else None
    )
    coordinate_dependency = _coordinate_dependency(
        session,
        project,
        rail,
        coordinate=coordinate,
        pinned=cohort_receipt is not None or event_cohort_receipt is not None,
        require_openable=cohort_receipt is not None,
        default_allowed=(
            candidate is None and cohort_receipt is not None and not summary
        ),
    )
    next_coordinate_url = (
        lane_url
        if candidate is not None
        else next_cohort_coordinate_url(
            lane_url,
            rail,
            coordinate_dependency.id if coordinate_dependency is not None else None,
        )
    )
    plan = (
        plan_form(session, coordinate_dependency.project_id, coordinate_dependency.id)
        if coordinate_dependency is not None
        else None
    )

    if candidate is None:
        # Nothing actionable is left in this lane. The page is the same one
        # whether or not a coordination strip is still open for the last
        # admitted record; the strip is part of the reading, not a second page.
        return QueueView(
            **counts,
            template="empty.html",
            rail=rail,
            coordinate_dependency=coordinate_dependency,
            plan=plan,
            next_coordinate_url=next_coordinate_url,
        )

    reason = _review_reason(session, project, candidate)
    headline, guidance = REVIEW_REASONS.get(reason or "", (None, None))
    siblings, differences = (
        _revision_panels(session, project, candidate)
        if reason == "revisions_disagree"
        else ([], [])
    )
    return QueueView(
        **counts,
        template="queue.html",
        rail=rail,
        coordinate_dependency=coordinate_dependency,
        plan=plan,
        next_coordinate_url=next_coordinate_url,
        candidate=candidate,
        view=build_view(
            session,
            candidate,
            historical_document_id=historical_document_id,
            allowed_candidate_ids=allowed_candidate_ids,
        ),
        review_headline=headline,
        review_guidance=guidance,
        identity_card=(
            _identity_card(session, candidate)
            if reason == "external_org_identity_unresolved"
            else None
        ),
        candidate_authority_gap=pending_candidate_authority_gap(
            session, project.id, candidate.id
        ),
        unresolved_acknowledgments=tuple(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == audit.CANDIDATE,
                    AuditLog.entity_id == candidate.id,
                    AuditLog.action == audit.KEEP_CANDIDATE_UNRESOLVED,
                )
                .order_by(AuditLog.id)
            )
        ),
        differences=differences,
        evidence_panels=[
            build_evidence(session, other, label="the other revision")
            for other in siblings
        ],
        siblings=(
            _sibling_revisions(
                session, allowed_candidate_ids or frozenset(), candidate
            )
            if event_cohort_receipt is not None
            else []
        ),
        classification=(
            CLASSIFICATIONS_BY_KEY.get(
                member_classification(cohort_receipt, candidate) or ""
            )
            if cohort_receipt is not None
            else None
        ),
        changes=(
            change_strip(session, cohort_receipt, candidate)
            if cohort_receipt is not None
            else []
        ),
    )


# --- the pinned lanes -------------------------------------------------------


def _cohort_receipt(
    session: Session, project: Project, receipt_id: int | None
) -> CohortReceipt:
    if receipt_id is None:
        raise LaneManifestRequired("test coordination requires its input manifest")
    receipt = session.get(CohortReceipt, receipt_id)
    if receipt is None or receipt.project_id != project.id:
        raise NoSuchLaneManifest("no such test input manifest in this project")
    return receipt


def _event_cohort_receipt(
    session: Session, project: Project, receipt_id: int | None
) -> EventCohortReceipt:
    if receipt_id is None:
        raise LaneManifestRequired(
            "statement test coordination requires its input manifest"
        )
    receipt = session.get(EventCohortReceipt, receipt_id)
    if receipt is None or receipt.project_id != project.id:
        raise NoSuchLaneManifest(
            "no such statement test input manifest in this project"
        )
    return receipt


def event_lane_dependency_ids(
    session: Session, receipt: EventCohortReceipt
) -> frozenset[int]:
    """The admission phase serves only dependency Candidates; events wait
    for the ADR-0026 policy machinery."""
    ids = event_cohort_candidate_ids(session, receipt)
    if not ids:
        return frozenset()
    return frozenset(
        session.scalars(
            select(Candidate.id).where(
                Candidate.id.in_(ids), Candidate.kind == "dependency"
            )
        )
    )


def _lane_url(
    slug: str,
    cohort_receipt: CohortReceipt | None,
    event_cohort_receipt: EventCohortReceipt | None,
) -> str:
    if cohort_receipt is not None:
        return f"/queue/{slug}?lane=rehearsal&cohort_receipt_id={cohort_receipt.id}"
    if event_cohort_receipt is not None:
        return (
            f"/queue/{slug}?lane=events"
            f"&event_cohort_receipt_id={event_cohort_receipt.id}"
        )
    return f"/queue/{slug}?lane=candidate"


def cohort_summary_location(lane_url: str) -> str:
    """Explicitly clear a deep-linked cohort item without leaving the lane."""

    return f"{lane_url}&summary=1"


def next_cohort_coordinate_url(
    lane_url: str, rail, current_dependency_id: int | None
) -> str:
    """Skip forward inside the incomplete admitted set, then fall back."""

    summary_url = cohort_summary_location(lane_url)
    if current_dependency_id is None:
        return summary_url
    next_dependency_id = next_incomplete_cohort_dependency_id(
        rail, current_dependency_id
    )
    return (
        f"{lane_url}&coordinate={next_dependency_id}"
        if next_dependency_id is not None
        else summary_url
    )


def safe_cohort_return(
    return_to: str, *, project: Project, dependency_id: int, session: Session
) -> str:
    """Only this Dependency's exact pinned rehearsal context may return.

    A same-app path is not enough: the link decides which pinned lane a
    coordinator lands back in, so it must name this project's own receipt, this
    exact Dependency, and exactly one of the item or the summary.
    """

    candidate = (return_to or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        raise ReturnContextRefused("return_to must be a same-app path")
    parsed = urlsplit(candidate)
    values = parse_qs(parsed.query, keep_blank_values=True)
    allowed_keys = {"lane", "cohort_receipt_id", "coordinate", "summary"}
    if (
        parsed.path != f"/queue/{project.slug}"
        or parsed.fragment
        or set(values) - allowed_keys
        or values.get("lane") != ["rehearsal"]
        or len(values.get("cohort_receipt_id", ())) != 1
        or (("coordinate" in values) == ("summary" in values))
    ):
        raise ReturnContextRefused(
            "return_to must name this test coordination context"
        )
    try:
        receipt_id = int(values["cohort_receipt_id"][0])
    except (TypeError, ValueError):
        raise ReturnContextRefused(
            "return_to must name this test coordination context"
        )
    receipt = session.get(CohortReceipt, receipt_id)
    if receipt is None or receipt.project_id != project.id:
        raise ReturnContextRefused(
            "return_to must name this test coordination context"
        )
    rail = build_cohort_rail(session, receipt, None)
    if dependency_id not in cohort_openable_dependency_ids(rail):
        raise ReturnContextRefused(
            "return_to must name this test coordination context"
        )
    if "coordinate" in values:
        try:
            coordinate_id = int(values["coordinate"][0])
        except (TypeError, ValueError):
            raise ReturnContextRefused(
                "return_to must name this test coordination context"
            )
        if values["coordinate"] != [str(coordinate_id)] or coordinate_id != dependency_id:
            raise ReturnContextRefused(
                "return_to must name this test coordination context"
            )
    elif values.get("summary") != ["1"]:
        raise ReturnContextRefused(
            "return_to must name this test coordination context"
        )
    return candidate


# --- the one open row -------------------------------------------------------


def _open_candidate(
    session: Session,
    project: Project,
    *,
    candidate_id: int | None,
    historical_document_id: int | None,
    allowed_candidate_ids: frozenset[int] | None,
) -> Candidate | None:
    """The row the screen opens: the one asked for, else the lane's next.

    Direct selection from the rail is still resolved through the same
    actionable scope the default pick uses — the rail is navigation, never a
    widening.
    """
    if candidate_id is not None:
        chosen = next_candidate(
            session,
            project.id,
            historical_document_id=historical_document_id,
            allowed_candidate_ids=(
                (allowed_candidate_ids or frozenset()) & {candidate_id}
                if allowed_candidate_ids is not None
                else frozenset({candidate_id})
            ),
        )
        if chosen is not None:
            return chosen
    return next_candidate(
        session,
        project.id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )


def _coordinate_dependency(
    session: Session,
    project: Project,
    rail,
    *,
    coordinate: int | None,
    pinned: bool,
    require_openable: bool,
    default_allowed: bool,
) -> Dependency | None:
    """The Constraint whose coordination strip is open, inside this lane only."""
    openable = frozenset(cohort_openable_dependency_ids(rail))
    if coordinate is not None and pinned:
        dependency = session.get(Dependency, coordinate)
        if (
            dependency is not None
            and dependency.project_id == project.id
            and not (require_openable and dependency.id not in openable)
        ):
            return dependency
        return None
    if default_allowed and coordinate is None:
        default_id = default_cohort_dependency_id(rail)
        if default_id is not None:
            return session.get(Dependency, default_id)
    return None


def _review_reason(
    session: Session, project: Project, candidate: Candidate
) -> str | None:
    """Why this row is in front of a human, in the machine's own words.

    Under the admission policies a row reaches Adjudication only because the
    machine refused to admit it, and the refusal is recorded with a stated
    reason. Leading with that reason is the difference between "judge this row"
    and "judge this row, and here is what to look at".
    """
    return session.scalar(
        select(DependencyAdmissionOutcome.reason)
        .join(PolicyRun, PolicyRun.id == DependencyAdmissionOutcome.policy_run_id)
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.candidate_id == candidate.id,
            DependencyAdmissionOutcome.outcome == "abstained",
        )
        .order_by(DependencyAdmissionOutcome.id.desc())
        .limit(1)
    )


def _identity_card(session: Session, candidate: Candidate) -> dict | None:
    """The one card behind an unresolved External Organization spelling.

    ADR-0051: after every deterministic evidence kind came up empty or
    ambiguous, the remaining question lives in a person's head. The card shows
    the unresolved source wording, the evidence the stack already considered,
    and the explicit existing-or-new choices — nothing preselected, nothing
    scored.
    """

    try:
        residue = resolve_candidate_identity(session, candidate, permit_advanced=True)
    except OrganizationIdentityRefusal:
        return None
    return {
        "stated_wording": residue.stated_wording,
        "evidence": residue.evidence,
        "surviving_ids": residue.candidate_ids,
        "organizations": session.scalars(
            select(ExternalOrg).order_by(ExternalOrg.name)
        ).all(),
    }


def _revision_panels(
    session: Session, project: Project, candidate: Candidate
) -> tuple[list, list[dict]]:
    """The other pending revisions of this conflict, and the fields that
    differ — the whole of a disagreement judgment, side by side."""
    uid = (candidate.payload_json or {}).get("fields", {}).get("utility_id")
    if not uid:
        return [], []
    siblings = [
        other
        for other in session.scalars(
            select(Candidate)
            .join(
                ActiveExtractionRun,
                ActiveExtractionRun.extraction_run_id == Candidate.extraction_run_id,
            )
            .where(
                Candidate.project_id == project.id,
                Candidate.kind == "dependency",
                Candidate.state == "pending",
                Candidate.id != candidate.id,
            )
            .order_by(Candidate.id)
        ).all()
        if str((other.payload_json or {}).get("fields", {}).get("utility_id"))
        == str(uid)
    ]
    if not siblings:
        return [], []

    mine = (candidate.payload_json or {}).get("fields", {})
    differences = []
    for other in siblings:
        theirs = (other.payload_json or {}).get("fields", {})
        document = session.get(Document, other.source_document_id)
        for name in sorted(set(mine) | set(theirs)):
            if (mine.get(name) or "") != (theirs.get(name) or ""):
                differences.append(
                    {
                        "field": name,
                        "mine": mine.get(name) or "—",
                        "theirs": theirs.get(name) or "—",
                        "other_document": document.filename if document else "?",
                    }
                )
    return siblings, differences


def _sibling_revisions(
    session: Session, allowed_ids: frozenset[int], candidate: Candidate
) -> list[dict]:
    """Other pending revisions of the same conflict, offered as merges.

    The registry holds no supersession chain for these documents, so no
    revision is machine-current; the reviewer's gesture is the explicit
    choice (#199)."""
    schemes = document_numbering_schemes(session, candidate.project_id)
    aliases = party_canonical_names(session)
    if conflict_key(candidate, schemes, aliases) is None:
        return []
    siblings = []
    others = session.scalars(
        select(Candidate).where(
            Candidate.id.in_(allowed_ids),
            Candidate.id != candidate.id,
            Candidate.state == "pending",
        )
    ).all()
    for other in others:
        # The same predicate the accept path refuses on, so the lane never
        # offers a merge the mutation would then reject.
        if not same_conflict(candidate, other, schemes, aliases):
            continue
        document = session.get(Document, other.source_document_id)
        siblings.append(
            {
                "id": other.id,
                "filename": document.filename if document else "?",
                "doc_date": document.doc_date if document else None,
            }
        )
    return sorted(siblings, key=lambda sibling: (sibling["doc_date"] or date.min))
