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
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.web.queue import (
    build_supersession_review_view,
    build_view,
    next_candidate,
    pending_counts,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
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


def _project(session: Session, slug: str) -> Project:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise HTTPException(404, f"no project {slug!r}")
    return project


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/queue/nhhip-3c2", status_code=302)


@app.get("/queue/{slug}", response_class=HTMLResponse)
def queue(
    request: Request,
    slug: str,
    lane: Literal["candidate", "reconfirmation"] = "candidate",
    historical_document_id: int | None = None,
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    worklist = build_reviewer_worklist(session, project.id)
    ordinary_candidate_ids = frozenset(
        candidate_id
        for review in worklist.ordinary
        for candidate_id in review.successor_candidate_ids
    )
    allowed_candidate_ids = (
        None if historical_document_id is not None else ordinary_candidate_ids
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
        "lane": lane,
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

    candidate = next_candidate(
        session,
        project.id,
        historical_document_id=historical_document_id,
        allowed_candidate_ids=allowed_candidate_ids,
    )

    if candidate is None:
        return TEMPLATES.TemplateResponse(
            request,
            "empty.html",
            lane_context,
        )

    return TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project,
            "view": build_view(
                session,
                candidate,
                historical_document_id=historical_document_id,
                allowed_candidate_ids=allowed_candidate_ids,
            ),
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
    )
    orgs = session.scalars(select(ExternalOrg).order_by(ExternalOrg.name)).all()
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
            },
            "rules": sorted(RULES),
            "statuses": DEP_STATUSES,
            "strategies": RESOLUTION_STRATEGIES,
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
        },
    )


@app.post("/dependencies/{dependency_id}/owner")
def assign_owner(
    dependency_id: int,
    slug: str = Form(...),
    owner: str = Form(...),
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
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.post("/dependencies/{dependency_id}/action")
def record_next_action(
    dependency_id: int,
    slug: str = Form(...),
    action: str = Form(...),
    due_date: str = Form(""),
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
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


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
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
    candidate = _project_pending_candidate(
        session,
        project,
        candidate_id,
        historical_document_id=historical_document_id,
    )
    _accept(
        session,
        candidate,
        principal,
        historical_document_id=historical_document_id,
    )
    session.commit()
    return RedirectResponse(
        _queue_location(slug, historical_document_id), status_code=303
    )


def _accept(
    session: Session,
    candidate: Candidate,
    principal: HumanPrincipal,
    *,
    historical_document_id: int | None = None,
) -> None:
    """The queue disables this button; a form post can still reach it."""
    try:
        accept_candidate(
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
    project = _project(session, slug)
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

    _accept(
        session,
        candidate,
        principal,
        historical_document_id=historical_document_id,
    )
    session.commit()
    return RedirectResponse(
        _queue_location(slug, historical_document_id), status_code=303
    )


@app.post("/candidates/{candidate_id}/merge")
def merge(
    candidate_id: int,
    slug: str = Form(...),
    dependency_id: int = Form(...),
    historical_document_id: int | None = Form(None),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
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
        _queue_location(slug, historical_document_id), status_code=303
    )


@app.post("/candidates/{candidate_id}/reject")
def reject(
    candidate_id: int,
    slug: str = Form(...),
    reason: str = Form(...),
    historical_document_id: int | None = Form(None),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug)
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
        _queue_location(slug, historical_document_id), status_code=303
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
