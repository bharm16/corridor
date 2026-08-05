"""The adjudication queue.

The only path from a Candidate into the Ledger is a human keystroke, and
this is that keystroke. Everything here is shaped by throughput: one
candidate at a time, hands on the keyboard, evidence beside the claim.

`m` (merge) is deliberately absent. Merge ranking is M3, and accepting a
duplicate instead of merging corrupts the ledger — so the action is shown
as unavailable rather than faked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from corridor import audit
from corridor.adjudicate import (
    UnadjudicableKind,
    accept_candidate,
    merge_candidate,
)
from corridor.db import Session as SessionFactory
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
    ExternalOrg,
    Project,
)
from corridor.web.queue import build_view, next_candidate, pending_counts

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
REJECT_REASONS = ("duplicate", "wrong", "irrelevant", "bad-citation")

app = FastAPI(title="Corridor — adjudication")


def get_session():
    with SessionFactory() as session:
        yield session


def _project(session: Session, slug: str) -> Project:
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise HTTPException(404, f"no project {slug!r}")
    return project


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/queue/nhhip-3c2", status_code=302)


@app.get("/queue/{slug}", response_class=HTMLResponse)
def queue(request: Request, slug: str, session: Session = Depends(get_session)):
    project = _project(session, slug)
    candidate = next_candidate(session, project.id)

    if candidate is None:
        total, verified = pending_counts(session, project.id)
        return TEMPLATES.TemplateResponse(
            request, "empty.html", {"project": project, "remaining": total}
        )

    return TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {"project": project, "view": build_view(session, candidate)},
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
    return TEMPLATES.TemplateResponse(
        request, "dependency.html", {"project": project, "view": view}
    )


@app.post("/dependencies/{dependency_id}/evidence/{link_id}/satisfies")
def mark_evidence_satisfies(
    dependency_id: int,
    link_id: int,
    slug: str = Form(...),
    session: Session = Depends(get_session),
):
    """Mark evidence as meeting the dependency's `evidence_required` bar.

    This is the only way a Dependency becomes ready (ADR-0002), so it is a
    deliberate act on a named piece of evidence rather than a status change.
    The rules belong to the Ledger; this route carries the HTTP.
    """
    try:
        mark_satisfies(session, dependency_id, link_id, actor="reviewer")
    except NoSuchEvidence as exc:
        raise HTTPException(404, str(exc))
    except UnverifiedEvidence as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.get("/page-image/{document_id}/{page_no}")
def page_image(
    document_id: int, page_no: int, session: Session = Depends(get_session)
):
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
    session: Session = Depends(get_session),
):
    candidate = _pending(session, candidate_id)
    _accept(session, candidate)
    session.commit()
    return RedirectResponse(f"/queue/{slug}", status_code=303)


def _accept(session: Session, candidate) -> None:
    """The queue disables this button; a form post can still reach it."""
    try:
        accept_candidate(session, candidate, actor="reviewer")
    except UnadjudicableKind as exc:
        raise HTTPException(400, str(exc))


@app.post("/candidates/{candidate_id}/edit-accept")
async def edit_accept(
    request: Request,
    candidate_id: int,
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
    candidate = _pending(session, candidate_id)

    payload = dict(candidate.payload_json or {})
    original = dict(payload.get("fields") or {})
    edited = {
        key[6:]: value.strip()
        for key, value in form.items()
        if key.startswith("field_") and value.strip()
    }

    if edited != original:
        audit.record(
            session,
            actor="reviewer",
            action=audit.EDIT_CANDIDATE,
            entity_type=audit.CANDIDATE,
            entity_id=candidate.id,
            before={"fields": original},
            after={"fields": edited},
        )
        payload["fields"] = edited
        candidate.payload_json = payload

    _accept(session, candidate)
    session.commit()
    return RedirectResponse(f"/queue/{slug}", status_code=303)


@app.post("/candidates/{candidate_id}/merge")
def merge(
    candidate_id: int,
    slug: str = Form(...),
    dependency_id: int = Form(...),
    session: Session = Depends(get_session),
):
    candidate = _pending(session, candidate_id)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != candidate.project_id:
        # Blocking is a hard filter, and so is this: merging across projects
        # would join two ledgers that were never the same thing.
        raise HTTPException(400, "no such dependency in this project")

    merge_candidate(session, candidate, dependency, actor="reviewer")
    session.commit()
    return RedirectResponse(f"/queue/{slug}", status_code=303)


@app.post("/candidates/{candidate_id}/reject")
def reject(
    candidate_id: int,
    slug: str = Form(...),
    reason: str = Form(...),
    session: Session = Depends(get_session),
):
    if reason not in REJECT_REASONS:
        raise HTTPException(400, f"reason must be one of {REJECT_REASONS}")

    candidate = _pending(session, candidate_id)
    candidate.state = "rejected"
    candidate.adjudicated_at = datetime.now(timezone.utc)
    audit.record(
        session,
        actor="reviewer",
        action=audit.REJECT_CANDIDATE,
        entity_type=audit.CANDIDATE,
        entity_id=candidate.id,
        after={"reason": reason},
    )
    session.commit()
    return RedirectResponse(f"/queue/{slug}", status_code=303)


def _pending(session: Session, candidate_id: int) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "no such candidate")
    if candidate.state != "pending":
        # Two tabs, or a double submit. Adjudicating twice would create a
        # second Dependency from one source row.
        raise HTTPException(409, f"already {candidate.state}")
    return candidate
