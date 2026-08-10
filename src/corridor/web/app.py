"""The adjudication queue.

The only path from a Candidate into the Ledger is a human keystroke, and
this is that keystroke. Everything here is shaped by throughput: one
candidate at a time, hands on the keyboard, evidence beside the claim.

`m` (merge) is deliberately absent. Merge ranking is M3, and accepting a
duplicate instead of merging corrupts the ledger — so the action is shown
as unavailable rather than faked.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from corridor.adjudicate import (
    AlreadyAdjudicated,
    AlreadyDismissed,
    InvalidDismissReason,
    DISMISS_REASONS,
    dismiss_dependency,
    CandidateAssertsNothing,
    InvalidCandidateProvenance,
    InvalidCandidateScope,
    InvalidRejectReason,
    REJECT_REASONS,
    UnadjudicableKind,
    accept_candidate,
    edit_candidate,
    merge_candidate,
    reject_candidate,
)
from corridor.automatic_carry_forward import automatic_carry_forward_status
from corridor.db import Session as SessionFactory
from corridor.config import settings
from corridor.exceptions import RULES, evaluate_project
from corridor.lane import (
    LaneRefusal,
    SiblingsNeedTheEventLane,
    check_sibling_request,
    check_sibling_set,
    conflict_key,
    same_conflict,
)
from corridor.ledger import (
    NoSuchEvidence,
    UnverifiedEvidence,
    browse,
    load_dependency,
    mark_satisfies,
)
from corridor.models import (
    RESOLUTION_STRATEGIES,
    DEP_STATUSES,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.web.queue import (
    build_cohort_rail,
    build_evidence,
    build_supersession_review_view,
    build_view,
    change_strip,
    member_classification,
    next_candidate,
    pending_counts,
)
from corridor.disputes import NoSuchDispute, disputes_for, settle_dispute
from corridor.event_admission import (
    StatementUnplaceable,
    attach_statement,
    waiting_statements,
)
from corridor.identity import document_numbering_schemes
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.cohort import (
    CohortScopeViolation,
    cohort_candidate_ids,
    event_cohort_candidate_ids,
    require_cohort_member,
    require_event_cohort_member,
)
from corridor.models import CohortReceipt, EventCohortReceipt
from corridor.work_decisions import (
    assign_internal_owner,
    cancel_next_action,
    complete_next_action,
    current_internal_owner_decision,
    current_next_action_decision,
    set_next_action,
)
from corridor.supersession_review import (
    ReconfirmationUnavailable,
    build_reviewer_worklist,
    normalize_scope_fingerprint,
    ordinary_candidate_for_update,
    reconfirm_operative_support,
)
from corridor.models import (
    ActiveExtractionRun,
    DependencyAdmissionOutcome,
    PolicyRun,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Corridor — adjudication")


def get_session():
    with SessionFactory() as session:
        yield session


def get_human_principal() -> HumanPrincipal:
    """Resolve deployment identity, never identity supplied by the request."""
    try:
        return HumanPrincipal(settings.human_principal)
    except InvalidHumanPrincipal as exc:
        raise HTTPException(
            503,
            "Admission is unavailable until CORRIDOR_HUMAN_PRINCIPAL names "
            "a stable human subject",
        ) from exc


def _safe_return(redirect_to: str, fallback: str) -> str:
    """Only same-app paths: a form field must never become an open redirect."""
    candidate = (redirect_to or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    if candidate:
        raise HTTPException(400, "redirect_to must be a same-app path")
    return fallback


def _decision_location(
    slug: str,
    historical_document_id: int | None,
    cohort_receipt_id: int | None,
    *,
    event_cohort_receipt_id: int | None = None,
    coordinate_dependency_id: int | None = None,
) -> str:
    """Where a decision lands next: the same lane it was made in.

    In the rehearsal lane an accept flows into the coordination strip for
    the record it just admitted — one pass, not two."""
    if cohort_receipt_id is not None:
        url = f"/queue/{slug}?lane=rehearsal&cohort_receipt_id={cohort_receipt_id}"
        if coordinate_dependency_id is not None:
            url += f"&coordinate={coordinate_dependency_id}"
        return url
    if event_cohort_receipt_id is not None:
        url = (
            f"/queue/{slug}?lane=events"
            f"&event_cohort_receipt_id={event_cohort_receipt_id}"
        )
        if coordinate_dependency_id is not None:
            url += f"&coordinate={coordinate_dependency_id}"
        return url
    return _queue_location(slug, historical_document_id)


def _require_cohort_scope(
    session: Session,
    cohort_receipt_id: int | None,
    candidate_id: int,
    *,
    project: Project | None = None,
) -> None:
    """Server-side, at the mutation: the cohort is a boundary, not a view.

    The project is passed because a receipt belonging to another project
    was refused on the read path and accepted on every write path.
    """
    if cohort_receipt_id is None:
        return
    try:
        require_cohort_member(
            session,
            cohort_receipt_id,
            candidate_id,
            project_id=project.id if project else None,
        )
    except CohortScopeViolation as exc:
        raise HTTPException(409, str(exc))


def _require_event_cohort_scope(
    session: Session,
    event_cohort_receipt_id: int | None,
    candidate_id: int,
    *,
    project: Project | None = None,
) -> None:
    """The event lane's boundary, held at the same place: the mutation."""
    if event_cohort_receipt_id is None:
        return
    try:
        require_event_cohort_member(
            session,
            event_cohort_receipt_id,
            candidate_id,
            project_id=project.id if project else None,
        )
    except CohortScopeViolation as exc:
        raise HTTPException(409, str(exc))


def _event_lane_dependency_ids(
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


def _sibling_revisions(
    session: Session,
    allowed_ids: frozenset[int],
    candidate: Candidate,
) -> list[dict]:
    """Other pending revisions of the same conflict, offered as merges.

    The registry holds no supersession chain for these documents, so no
    revision is machine-current; the reviewer's gesture is the explicit
    choice (#199)."""
    schemes = document_numbering_schemes(session, candidate.project_id)
    if conflict_key(candidate, schemes) is None:
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
        if not same_conflict(candidate, other, schemes):
            continue
        document = session.get(Document, other.source_document_id)
        siblings.append(
            {
                "id": other.id,
                "filename": document.filename if document else "?",
                "doc_date": document.doc_date if document else None,
            }
        )
    return sorted(siblings, key=lambda s: (s["doc_date"] or date.min))


def _project(session: Session, slug: str) -> Project:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise HTTPException(404, f"no project {slug!r}")
    return project


def _review_reason(
    session: Session, project: Project, candidate: Candidate
) -> str | None:
    """Why this row is in front of a human, in the machine's own words.

    Under the admission policies a row reaches Adjudication only because
    the machine refused to admit it, and the refusal is recorded with a
    stated reason. Leading with that reason is the difference between
    "judge this row" and "judge this row, and here is what to look at".
    """
    return session.scalar(
        select(DependencyAdmissionOutcome.reason)
        .join(
            PolicyRun,
            PolicyRun.id == DependencyAdmissionOutcome.policy_run_id,
        )
        .where(
            PolicyRun.project_id == project.id,
            DependencyAdmissionOutcome.candidate_id == candidate.id,
            DependencyAdmissionOutcome.outcome == "abstained",
        )
        .order_by(DependencyAdmissionOutcome.id.desc())
        .limit(1)
    )


# What each unplaced statement means to the person now holding it. The
# machine's vocabulary names the check; the reviewer needs the question.
STATEMENT_REASONS = {
    "reference_resolves_to_no_dependency": (
        "This statement names a conflict the record does not have.",
        "The number may be misread, or its conflict may not be loaded yet. "
        "Name the right record, or toss it.",
    ),
    "reference_resolves_to_many": (
        "Several records carry this number.",
        "Name which one the party was talking about.",
    ),
    "no_conflict_reference": (
        "This statement names no conflict.",
        "Read the quote and name the record it belongs to.",
    ),
    "party_mismatch": (
        "The speaker is not the party that owns this conflict.",
        "It may be an alias nobody has recorded, or the wrong record. "
        "Name the right one.",
    ),
    "party_unstated": (
        "This statement names no party.",
        "Read the quote and name the record it belongs to.",
    ),
    "project_side_actor": (
        "The speaker is the project's own side.",
        "An internal action item, never an External Party's commitment. "
        "It cannot attach as a statement.",
    ),
    "citations_unverified": (
        "The quote could not be found on its page.",
        "Check the page before placing it.",
    ),
    "event_type_outside_policy": (
        "This is not a commitment, slip, or closure.",
        "Only those three carry a date anyone is held to.",
    ),
    "no_date": (
        "This statement carries no date.",
        "Without one there is nothing to hold anyone to.",
    ),
    "unparseable_date": (
        "The stated date could not be read.",
        "Check the page for what it actually says.",
    ),
}


# What each abstention means to the person now holding the row. The
# machine's vocabulary is precise and the reviewer's question is
# different: not "which check failed" but "what am I deciding".
REVIEW_REASONS = {
    "revisions_disagree": (
        "The revisions disagree about this conflict.",
        "Both pages are shown. Accept the revision that is right, or edit "
        "the values before accepting.",
    ),
    "missing_from_agreement_document": (
        "Only one revision has this conflict.",
        "It was added or dropped between revisions. Accept it if the "
        "record should carry it.",
    ),
    "multiple_rows_in_agreement_document": (
        "One revision lists this conflict twice.",
        "Two rows share an identifier. Accept the one that is right and "
        "reject the other.",
    ),
    "citations_unverified": (
        "The quote could not be found on the cited page.",
        "Check the page before accepting anything from this row.",
    ),
    "already_admitted": (
        "A record already carries this identifier.",
        "Merge into the existing record rather than admitting a second one.",
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
    "no_row_identity": (
        "This row's number needs a party to name it.",
        "This document numbers each party's conflicts separately, and the "
        "row states no party — check the page and fill in what it shows.",
    ),
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
                ActiveExtractionRun.extraction_run_id
                == Candidate.extraction_run_id,
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
                        "field": name.replace("_", " "),
                        "mine": mine.get(name) or "—",
                        "theirs": theirs.get(name) or "—",
                        "other_document": document.filename if document else "?",
                    }
                )
    return siblings, differences


@app.post("/projects/{slug}/statements/{candidate_id}/attach")
def attach_waiting_statement(
    slug: str,
    candidate_id: int,
    dependency_ref: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Put an unplaced statement on the record a human names."""
    project = _project(session, slug)
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.project_id != project.id:
        raise HTTPException(404, "no such statement in this project")
    dependency = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project.id,
            Dependency.ref_code == dependency_ref.strip(),
        )
    ).first()
    if dependency is None:
        raise HTTPException(
            400, f"no record in this project called {dependency_ref!r}"
        )
    try:
        attach_statement(session, candidate, dependency, principal=principal)
    except StatementUnplaceable as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return RedirectResponse(f"/statements/{slug}", status_code=303)


@app.post("/ledger/{slug}/{dependency_id}/dismiss")
def dismiss(
    slug: str,
    dependency_id: int,
    reason: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Take a junk record off the working list, with a reason."""
    project = _project(session, slug)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != project.id:
        raise HTTPException(404, "no such dependency in this project")
    try:
        dismiss_dependency(session, dependency, reason, principal=principal)
    except InvalidDismissReason as exc:
        raise HTTPException(400, str(exc))
    except AlreadyDismissed as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return RedirectResponse(f"/ledger/{slug}", status_code=303)


@app.get("/statements/{slug}", response_class=HTMLResponse)
def statements(
    request: Request,
    slug: str,
    session: Session = Depends(get_session),
):
    """The one pile: statements the machine could not place.

    Not a lane and not a queue — a short list a reviewer empties when
    they choose, because a dated promise from a meeting is exactly what
    this product exists to catch and losing it silently is worse.
    """
    project = _project(session, slug)
    waiting = waiting_statements(session, project.id)
    for item in waiting:
        item["headline"], item["guidance"] = STATEMENT_REASONS.get(
            item["reason"],
            (
                "This statement could not be placed.",
                "Name the record it belongs to, or toss it.",
            ),
        )
    return TEMPLATES.TemplateResponse(
        request,
        "statements.html",
        {"project": project, "waiting": waiting},
    )


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/queue/nhhip-3c2", status_code=302)


@app.get("/queue/{slug}", response_class=HTMLResponse)
def queue(
    request: Request,
    slug: str,
    lane: Literal["candidate", "reconfirmation", "rehearsal", "events"] = "candidate",
    mode: Literal["auto", "review"] = "auto",
    historical_document_id: int | None = None,
    cohort_receipt_id: int | None = None,
    event_cohort_receipt_id: int | None = None,
    candidate_id: int | None = None,
    coordinate: int | None = None,
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    cohort_receipt = None
    if lane == "rehearsal":
        if cohort_receipt_id is None:
            raise HTTPException(400, "the rehearsal lane names its cohort receipt")
        cohort_receipt = session.get(CohortReceipt, cohort_receipt_id)
        if cohort_receipt is None or cohort_receipt.project_id != project.id:
            raise HTTPException(404, "no such cohort receipt in this project")
    event_cohort_receipt = None
    if lane == "events":
        if event_cohort_receipt_id is None:
            raise HTTPException(400, "the event lane names its cohort receipt")
        event_cohort_receipt = session.get(
            EventCohortReceipt, event_cohort_receipt_id
        )
        if (
            event_cohort_receipt is None
            or event_cohort_receipt.project_id != project.id
        ):
            raise HTTPException(404, "no such event cohort receipt in this project")
    worklist = build_reviewer_worklist(session, project.id)
    ordinary_candidate_ids = frozenset(
        candidate_id
        for review in worklist.ordinary
        for candidate_id in review.successor_candidate_ids
    )
    allowed_candidate_ids = (
        None if historical_document_id is not None else ordinary_candidate_ids
    )
    if cohort_receipt is not None:
        # The lane presents exactly the receipt's members — the pinned set
        # intersected with what is ordinarily actionable, never widened.
        allowed_candidate_ids = (
            ordinary_candidate_ids
            & cohort_candidate_ids(session, cohort_receipt)
        )
    if event_cohort_receipt is not None:
        allowed_candidate_ids = (
            ordinary_candidate_ids
            & _event_lane_dependency_ids(session, event_cohort_receipt)
        )
    ordinary_reviews = [
        build_supersession_review_view(session, review)
        for review in worklist.ordinary
        if not review.successor_candidate_ids
    ]
    total, _verified = pending_counts(
        session,
        project.id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )
    lane_context = {
        "project": project,
        "remaining": total,
        # A count, not the pile itself: the queue points at it and never
        # becomes a second place to work statements.
        "waiting_statements": len(waiting_statements(session, project.id)),
        "lane": lane,
        "cohort_receipt": cohort_receipt,
        "candidate_count": total + len(ordinary_reviews),
        "reconfirmation_count": len(worklist.reconfirmation),
        "ordinary_reviews": ordinary_reviews,
        "automatic_carry_forward": automatic_carry_forward_status(
            session,
            project.id,
            worklist=worklist,
        ),
    }
    if lane == "reconfirmation" and not worklist.reconfirmation:
        return TEMPLATES.TemplateResponse(request, "empty.html", lane_context)
    if lane == "reconfirmation":
        return TEMPLATES.TemplateResponse(
            request,
            "reconfirmation.html",
            {
                **lane_context,
                "view": build_supersession_review_view(
                    session, worklist.reconfirmation[0]
                ),
            },
        )

    candidate = None
    if candidate_id is not None:
        # Direct selection from the rail: still resolved through the same
        # actionable scope the default pick uses — the rail is navigation,
        # never a widening.
        candidate = next_candidate(
            session,
            project.id,
            historical_document_id=historical_document_id,
            allowed_candidate_ids=(
                (allowed_candidate_ids or frozenset()) & {candidate_id}
                if allowed_candidate_ids is not None
                else frozenset({candidate_id})
            ),
        )
    if candidate is None:
        candidate = next_candidate(
            session,
            project.id,
            historical_document_id=historical_document_id,
            allowed_candidate_ids=allowed_candidate_ids,
        )

    coordinate_dependency = None
    if coordinate is not None and (
        cohort_receipt is not None or event_cohort_receipt is not None
    ):
        coordinate_dependency = session.get(Dependency, coordinate)
        if (
            coordinate_dependency is None
            or coordinate_dependency.project_id != project.id
        ):
            coordinate_dependency = None

    lane_url = f"/queue/{slug}?lane=candidate"
    if cohort_receipt is not None:
        lane_url = (
            f"/queue/{slug}?lane=rehearsal&cohort_receipt_id={cohort_receipt.id}"
        )
    if event_cohort_receipt is not None:
        lane_url = (
            f"/queue/{slug}?lane=events"
            f"&event_cohort_receipt_id={event_cohort_receipt.id}"
        )

    if candidate is None and coordinate_dependency is None:
        return TEMPLATES.TemplateResponse(
            request,
            "empty.html",
            lane_context,
        )

    rail = (
        build_cohort_rail(
            session,
            cohort_receipt,
            candidate.id if candidate is not None else None,
        )
        if cohort_receipt is not None
        else None
    )
    if candidate is None:
        # Every member decided, but a coordination strip is still open for
        # the last admitted record.
        return TEMPLATES.TemplateResponse(
            request,
            "empty.html",
            {
                **lane_context,
                "rail": rail,
                "coordinate_dependency": coordinate_dependency,
                "lane_url": lane_url,
            },
        )

    reason = _review_reason(session, project, candidate)
    headline, guidance = REVIEW_REASONS.get(reason or "", (None, None))
    siblings, differences = (
        _revision_panels(session, project, candidate)
        if reason == "revisions_disagree"
        else ([], [])
    )
    evidence_panels = [
        build_evidence(
            session,
            other,
            label="the other revision",
        )
        for other in siblings
    ]

    return TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project,
            "review_headline": headline,
            "review_guidance": guidance,
            "differences": differences,
            "evidence_panels": evidence_panels,
            "view": build_view(
                session,
                candidate,
                historical_document_id=historical_document_id,
                allowed_candidate_ids=allowed_candidate_ids,
            ),
            "event_cohort_receipt": event_cohort_receipt,
            "siblings": (
                _sibling_revisions(
                    session, allowed_candidate_ids or frozenset(), candidate
                )
                if event_cohort_receipt is not None
                else []
            ),
            "rail": rail,
            "classification": (
                member_classification(cohort_receipt, candidate)
                if cohort_receipt is not None
                else None
            ),
            "changes": (
                change_strip(session, cohort_receipt, candidate)
                if cohort_receipt is not None
                else []
            ),
            "coordinate_dependency": coordinate_dependency,
            "lane_url": lane_url,
            **lane_context,
        },
    )


@app.post("/supersession-review/{dependency_id}/reconfirm")
async def reconfirm_support(
    request: Request,
    dependency_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Carry one exact reviewer decision into the fail-closed domain seam."""

    form = await request.form()
    slug = form.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise HTTPException(400, "slug must be a non-empty string")
    slug = slug.strip()
    predecessor_document_id = _required_positive_form_id(
        form, "predecessor_document_id"
    )
    successor_candidate_id = _required_positive_form_id(
        form, "successor_candidate_id"
    )
    comparison_id = _required_positive_form_id(form, "comparison_id")
    finding_id = _required_positive_form_id(form, "finding_id")
    scope_fingerprint = _required_scope_fingerprint(form)

    project = _project(session, slug)
    _project_dependency(session, project, dependency_id)
    try:
        reconfirm_operative_support(
            session,
            project_id=project.id,
            dependency_id=dependency_id,
            predecessor_document_id=predecessor_document_id,
            successor_candidate_id=successor_candidate_id,
            comparison_id=comparison_id,
            finding_id=finding_id,
            scope_fingerprint=scope_fingerprint,
            principal=principal,
        )
    except ReconfirmationUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/queue/{slug}?lane=reconfirmation", status_code=303
    )


@app.get("/ledger/{slug}", response_class=HTMLResponse)
def ledger(
    request: Request,
    slug: str,
    status: str | None = None,
    org_id: int | None = None,
    resolution_strategy: str | None = None,
    ready: str | None = None,
    rule: str | None = None,
    owner: str | None = None,
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    # This page is its own publication, so it takes its own evaluation —
    # stated here rather than defaulted inside `browse`, where a caller who
    # already held one could silently pay for a second.
    rows = browse(
        session,
        project.id,
        evaluation=evaluate_project(session, project.id),
        status=status or None,
        org_id=org_id,
        resolution_strategy=resolution_strategy or None,
        ready={"yes": True, "no": False}.get(ready or ""),
        rule=rule or None,
        owner=owner or None,
    )
    orgs = session.scalars(select(ExternalOrg).order_by(ExternalOrg.name)).all()
    # Only the people this project has actually assigned work to. A list of
    # every principal who ever touched anything would offer names that
    # match nothing here.
    owners = session.scalars(
        select(Dependency.internal_owner)
        .where(
            Dependency.project_id == project.id,
            Dependency.dismissed_at.is_(None),
            Dependency.internal_owner.is_not(None),
        )
        .distinct()
        .order_by(Dependency.internal_owner)
    ).all()
    return TEMPLATES.TemplateResponse(
        request,
        "ledger.html",
        {
            "project": project,
            "rows": rows,
            "orgs": orgs,
            "filters": {
                "status": status or "",
                "org_id": org_id or "",
                "resolution_strategy": resolution_strategy or "",
                "ready": ready or "",
                "rule": rule or "",
                "owner": owner or "",
            },
            "rules": sorted(RULES),
            "statuses": DEP_STATUSES,
            "strategies": RESOLUTION_STRATEGIES,
            "owners": owners,
            "today": date.today(),
        },
    )


@app.get("/ledger/{slug}/{dependency_id}", response_class=HTMLResponse)
def dependency_detail(
    request: Request,
    slug: str,
    dependency_id: int,
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    try:
        view = load_dependency(session, dependency_id)
    except LookupError:
        raise HTTPException(404, "no such dependency")
    if view.dependency.project_id != project.id:
        raise HTTPException(404, "no such dependency in this project")
    owner_decision = current_internal_owner_decision(session, dependency_id)
    action_decision = current_next_action_decision(session, dependency_id)
    return TEMPLATES.TemplateResponse(
        request,
        "dependency.html",
        {
            "project": project,
            "view": view,
            "owner_decision": owner_decision,
            "action_decision": action_decision,
            "disputes": {
                d.field_name: d for d in disputes_for(session, dependency_id)
            },
            "dismiss_reasons": DISMISS_REASONS,
        },
    )


@app.post("/ledger/{slug}/{dependency_id}/settle")
def settle(
    slug: str,
    dependency_id: int,
    field_name: str = Form(...),
    value: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Say what the record concludes for one disputed field.

    The claims stay where they are; this records the judgment beside
    them (ADR-0031). A later revision disagreeing again reopens the
    Dispute without anyone reopening it.
    """
    project = _project(session, slug)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != project.id:
        raise HTTPException(404, "no such dependency in this project")
    try:
        settle_dispute(
            session,
            dependency_id,
            field_name,
            value=value.strip() or None,
            principal=principal,
        )
    except NoSuchDispute as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        f"/ledger/{slug}/{dependency_id}", status_code=303
    )


@app.post("/dependencies/{dependency_id}/owner")
def assign_owner(
    dependency_id: int,
    slug: str = Form(...),
    owner: str = Form(...),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record a Work Decision assigning the Internal Owner (ADR-0025).

    One decision per submit. The rules belong to the Work Decision seam;
    this route carries the HTTP.
    """
    project = _project(session, slug)
    _project_dependency(session, project, dependency_id)
    try:
        assign_internal_owner(session, dependency_id, owner, principal=principal)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        _safe_return(redirect_to, f"/ledger/{slug}/{dependency_id}"),
        status_code=303,
    )


@app.post("/dependencies/{dependency_id}/action")
def record_next_action(
    dependency_id: int,
    slug: str = Form(...),
    action: str = Form(...),
    due_date: str = Form(""),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One submit, one Work Decision: the action and its date together."""
    project = _project(session, slug)
    _project_dependency(session, project, dependency_id)
    parsed = None
    if due_date.strip():
        try:
            parsed = date.fromisoformat(due_date.strip())
        except ValueError:
            raise HTTPException(400, "an Action Due Date must be a date")
    try:
        set_next_action(
            session, dependency_id, action, due_date=parsed, principal=principal
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        _safe_return(redirect_to, f"/ledger/{slug}/{dependency_id}"),
        status_code=303,
    )


@app.post("/dependencies/{dependency_id}/action/{outcome}")
def close_next_action(
    dependency_id: int,
    outcome: Literal["complete", "cancel"],
    slug: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Completion and cancellation are distinct decisions, never one button."""
    project = _project(session, slug)
    _project_dependency(session, project, dependency_id)
    close = complete_next_action if outcome == "complete" else cancel_next_action
    try:
        close(session, dependency_id, principal=principal)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.post("/dependencies/{dependency_id}/evidence/{link_id}/satisfies")
def mark_evidence_satisfies(
    dependency_id: int,
    link_id: int,
    slug: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Mark evidence as meeting the dependency's `evidence_required` bar.

    This is the only way a Dependency becomes ready (ADR-0002), so it is a
    deliberate act on a named piece of evidence rather than a status change.
    The rules belong to the Ledger; this route carries the HTTP.
    """
    project = _project(session, slug)
    dependency = _project_dependency(session, project, dependency_id)
    _project_evidence(session, dependency, link_id)
    try:
        mark_satisfies(session, dependency_id, link_id, principal=principal)
    except NoSuchEvidence as exc:
        raise HTTPException(404, str(exc))
    except UnverifiedEvidence as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.get("/page-image/{document_id}/{page_no}")
def page_image(document_id: int, page_no: int, session: Session = Depends(get_session)):
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document_id, DocPage.page_no == page_no
        )
    ).first()
    if page is None or not page.image_path or not Path(page.image_path).exists():
        raise HTTPException(404, "no rendered image for that page")
    return FileResponse(page.image_path, media_type="image/png")


@app.post("/candidates/{candidate_id}/accept")
def accept(
    candidate_id: int,
    slug: str = Form(...),
    historical_document_id: int | None = Form(None),
    cohort_receipt_id: int | None = Form(None),
    event_cohort_receipt_id: int | None = Form(None),
    merge_sibling_ids: list[int] = Form([]),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    _require_cohort_scope(
        session, cohort_receipt_id, candidate_id, project=project
    )
    _require_event_cohort_scope(
        session, event_cohort_receipt_id, candidate_id, project=project
    )
    candidate = _project_pending_candidate(
        session,
        project,
        candidate_id,
        historical_document_id=historical_document_id,
    )

    # Resolving is the route's job — project and receipt scope are what it
    # knows. Whether the resolved set is one gesture over one conflict is
    # the lane's, and is refused in full before anything is written (#199).
    schemes = document_numbering_schemes(session, project.id)
    try:
        check_sibling_request(
            candidate,
            merge_sibling_ids,
            in_event_lane=event_cohort_receipt_id is not None,
            schemes=schemes,
        )
    except SiblingsNeedTheEventLane as exc:
        raise HTTPException(400, str(exc))
    except LaneRefusal as exc:
        raise HTTPException(409, str(exc))

    siblings = []
    for sibling_id in merge_sibling_ids:
        _require_event_cohort_scope(
            session, event_cohort_receipt_id, sibling_id, project=project
        )
        siblings.append(
            _project_pending_candidate(
                session, project, sibling_id, historical_document_id=None
            )
        )
    try:
        check_sibling_set(candidate, siblings, schemes)
    except LaneRefusal as exc:
        raise HTTPException(409, str(exc))

    dependency = _accept(
        session,
        candidate,
        principal,
        historical_document_id=historical_document_id,
    )
    for sibling in siblings:
        try:
            merge_candidate(session, sibling, dependency, principal=principal)
        except (AlreadyAdjudicated, InvalidCandidateScope) as exc:
            raise HTTPException(409, str(exc))
        except InvalidCandidateProvenance as exc:
            raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )


def _accept(
    session: Session,
    candidate: Candidate,
    principal: HumanPrincipal,
    *,
    historical_document_id: int | None = None,
) -> "Dependency":
    """The queue disables this button; a form post can still reach it."""
    try:
        return accept_candidate(
            session,
            candidate,
            principal=principal,
            historical_document_id=historical_document_id,
        )
    except (AlreadyAdjudicated, InvalidCandidateScope) as exc:
        raise HTTPException(409, str(exc))
    except InvalidCandidateProvenance as exc:
        raise HTTPException(400, str(exc))
    except UnadjudicableKind as exc:
        raise HTTPException(400, str(exc))
    except CandidateAssertsNothing as exc:
        raise HTTPException(400, str(exc))


@app.post("/candidates/{candidate_id}/edit-accept")
async def edit_accept(
    request: Request,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Edit-then-accept.

    The edited values are written into the payload before acceptance, so the
    assertions recorded are what the reviewer concluded — and the original
    extraction stays in the audit log rather than being overwritten
    silently.
    """
    form = await request.form()
    slug = form.get("slug")
    historical_document_id = _parse_historical_document_id(
        form.get("historical_document_id")
    )
    raw_cohort_receipt_id = form.get("cohort_receipt_id")
    cohort_receipt_id = None
    if raw_cohort_receipt_id is not None and str(raw_cohort_receipt_id).strip():
        # A malformed scope refuses; silently dropping it would let a
        # mangled form post mutate outside the boundary it claimed.
        try:
            cohort_receipt_id = int(str(raw_cohort_receipt_id).strip())
        except ValueError:
            raise HTTPException(400, "cohort_receipt_id must be an integer")
    raw_event_receipt_id = form.get("event_cohort_receipt_id")
    event_cohort_receipt_id = None
    if raw_event_receipt_id is not None and str(raw_event_receipt_id).strip():
        try:
            event_cohort_receipt_id = int(str(raw_event_receipt_id).strip())
        except ValueError:
            raise HTTPException(400, "event_cohort_receipt_id must be an integer")
    project = _project(session, slug)
    _require_cohort_scope(
        session, cohort_receipt_id, candidate_id, project=project
    )
    _require_event_cohort_scope(
        session, event_cohort_receipt_id, candidate_id, project=project
    )
    candidate = _project_pending_candidate(
        session,
        project,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    edited = {
        key[6:]: value.strip()
        for key, value in form.items()
        if key.startswith("field_") and value.strip()
    }
    try:
        edit_candidate(
            session,
            candidate,
            edited,
            principal=principal,
            historical_document_id=historical_document_id,
        )
    except (AlreadyAdjudicated, InvalidCandidateScope) as exc:
        raise HTTPException(409, str(exc))

    dependency = _accept(
        session,
        candidate,
        principal,
        historical_document_id=historical_document_id,
    )
    session.commit()
    return RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )


@app.post("/candidates/{candidate_id}/merge")
def merge(
    candidate_id: int,
    slug: str = Form(...),
    dependency_id: int = Form(...),
    historical_document_id: int | None = Form(None),
    cohort_receipt_id: int | None = Form(None),
    event_cohort_receipt_id: int | None = Form(None),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    _require_cohort_scope(
        session, cohort_receipt_id, candidate_id, project=project
    )
    _require_event_cohort_scope(
        session, event_cohort_receipt_id, candidate_id, project=project
    )
    candidate = _project_pending_candidate(
        session,
        project,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    dependency = _project_dependency(session, project, dependency_id)

    try:
        merge_candidate(
            session,
            candidate,
            dependency,
            principal=principal,
            historical_document_id=historical_document_id,
        )
    except InvalidCandidateScope as exc:
        raise HTTPException(409, str(exc))
    except InvalidCandidateProvenance as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )


@app.post("/candidates/{candidate_id}/reject")
def reject(
    candidate_id: int,
    slug: str = Form(...),
    reason: str = Form(...),
    historical_document_id: int | None = Form(None),
    cohort_receipt_id: int | None = Form(None),
    event_cohort_receipt_id: int | None = Form(None),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    # The receipt id arrived with every reject form and was ignored — the
    # boundary holds at every mutation or it is not a boundary.
    _require_cohort_scope(
        session, cohort_receipt_id, candidate_id, project=project
    )
    _require_event_cohort_scope(
        session, event_cohort_receipt_id, candidate_id, project=project
    )
    candidate = _project_pending_candidate(
        session,
        project,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    try:
        reject_candidate(
            session,
            candidate,
            reason,
            principal=principal,
            historical_document_id=historical_document_id,
        )
    except (AlreadyAdjudicated, InvalidCandidateScope) as exc:
        raise HTTPException(409, str(exc))
    except InvalidRejectReason as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(
        _safe_return(
            redirect_to,
            _decision_location(
                slug,
                historical_document_id,
                cohort_receipt_id,
                event_cohort_receipt_id=event_cohort_receipt_id,
            ),
        ),
        status_code=303,
    )


def _candidate(session: Session, candidate_id: int) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "no such candidate")
    return candidate


def _project_candidate(
    session: Session, project: Project, candidate_id: int
) -> Candidate:
    candidate = _candidate(session, candidate_id)
    if candidate.project_id != project.id:
        raise HTTPException(404, "no such candidate")
    return candidate


def _project_pending_candidate(
    session: Session,
    project: Project,
    candidate_id: int,
    *,
    historical_document_id: int | None = None,
) -> Candidate:
    candidate = _project_candidate(session, project, candidate_id)
    if candidate.state != "pending":
        # Two tabs, or a double submit. Adjudicating twice would create a
        # second Dependency from one source row.
        raise HTTPException(409, f"already {candidate.state}")
    scoped = ordinary_candidate_for_update(
        session,
        project.id,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    if scoped is None:
        raise HTTPException(409, "candidate is outside the actionable queue scope")
    return scoped


def _parse_historical_document_id(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        document_id = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "historical_document_id must be an integer") from exc
    if document_id <= 0:
        raise HTTPException(422, "historical_document_id must be positive")
    return document_id


def _required_positive_form_id(form, name: str) -> int:
    value = form.get(name)
    try:
        identifier = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} must be a positive integer") from exc
    if identifier <= 0:
        raise HTTPException(400, f"{name} must be a positive integer")
    return identifier


def _required_scope_fingerprint(form):
    raw = form.get("scope_fingerprint")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, "scope_fingerprint must be present")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "scope_fingerprint must be valid JSON") from exc
    try:
        return normalize_scope_fingerprint(decoded)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _queue_location(slug: str, historical_document_id: int | None) -> str:
    location = f"/queue/{slug}"
    if historical_document_id is not None:
        return f"{location}?historical_document_id={historical_document_id}"
    return location


def _project_dependency(
    session: Session, project: Project, dependency_id: int
) -> Dependency:
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != project.id:
        raise HTTPException(404, "no such dependency")
    return dependency


def _project_evidence(
    session: Session, dependency: Dependency, link_id: int
) -> EvidenceLink:
    link = session.get(EvidenceLink, link_id)
    if link is None or link.dependency_id != dependency.id:
        raise HTTPException(404, "no such evidence")
    return link
