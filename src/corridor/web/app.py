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

from corridor.adjudicate import accept_candidate
from corridor.db import Session as SessionFactory
from corridor.models import AuditLog, Candidate, DocPage, Project
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
    accept_candidate(session, candidate, actor="reviewer")
    session.commit()
    return RedirectResponse(f"/queue/{slug}", status_code=303)


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
        session.add(
            AuditLog(
                actor="reviewer",
                action="edit_candidate",
                entity_type="candidate",
                entity_id=candidate.id,
                before_json={"fields": original},
                after_json={"fields": edited},
            )
        )
        payload["fields"] = edited
        candidate.payload_json = payload

    accept_candidate(session, candidate, actor="reviewer")
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
    session.add(
        AuditLog(
            actor="reviewer",
            action="reject_candidate",
            entity_type="candidate",
            entity_id=candidate.id,
            before_json=None,
            after_json={"reason": reason},
        )
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
