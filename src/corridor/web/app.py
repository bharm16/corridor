"""The adjudication queue and residual coordination work.

Ambiguous Candidates enter the Ledger only through a human keystroke here.
An exact mechanically admitted statement instead arrives as a read-only fact
whose remaining scope, owner, and Next Action decisions are made through the
same work surface. Splitting those residual decisions into a second app was
rejected because it would duplicate the Work List's one-current-question seam.
Everything here is shaped by throughput: one question at a time, hands on the
keyboard, evidence beside the claim.

`m` (merge) is deliberately absent. Merge ranking is M3, and accepting a
duplicate instead of merging corrupts the ledger — so the action is shown
as unavailable rather than faked.
"""

from __future__ import annotations

from hashlib import sha256
import json
import secrets
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from corridor import audit
from corridor import email_intake
from corridor.config import settings
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
from corridor.candidate_statement_facts import (
    CandidateStatementFacts,
    prepare_candidate_statement_facts,
)
from corridor.db import Session as SessionFactory
from corridor import access
from corridor.web import auth
from corridor.check_configuration import (
    SUPPORTED_THRESHOLDS,
    InvalidCheckConfiguration,
    configuration_history,
    effective_configuration,
    effective_thresholds,
    preview_configuration,
    save_configuration,
)
from corridor.exceptions import RULES, evaluate_project, format_exception_name
from corridor.export import to_xlsx
from corridor.report import build_report, render
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
    AuditLog,
    Candidate,
    CandidateDisposition,
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    InboundMessage,
    DependencyEventScopeDecision,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Milestone,
    MilestoneRegistration,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    CoordinationSummaryRequest,
)
from corridor.coordination_summary import (
    ConfigurationRequired,
    InvalidSummaryConfiguration,
    current_configuration as current_summary_configuration,
    declare_configuration as declare_summary_configuration,
    request_summary as request_coordination_summary,
)
from corridor.milestones import (
    MalformedMilestoneCsv,
    StaleMilestoneImport,
    confirm_import,
    preview_import,
)
from corridor.key_date_drafting import (
    StaleKeyDateDraft,
    load_key_date_draft,
    preview_drafted_key_dates,
    verify_draft_for_ordinary_import,
)
from corridor.dependency_events import (
    current_scope_decision_filter,
    current_statement_evidence_memberships,
)
from corridor.web.queue import (
    build_cohort_rail,
    build_evidence,
    build_supersession_review_view,
    build_view,
    change_strip,
    cohort_openable_dependency_ids,
    default_cohort_dependency_id,
    member_classification,
    next_incomplete_cohort_dependency_id,
    next_candidate,
    pending_counts,
)
from corridor.disputes import (
    DisputeMovedOn,
    NoSuchDispute,
    disputes_for,
    settle_dispute,
)
from corridor.event_admission import (
    StatementUnplaceable,
    attach_statement,
    waiting_statements,
)
from corridor.frontend_request_receipts import (
    FrontendRequestSubject,
    record_frontend_request,
)
from corridor.verbal import VerbalRefusal, record_verbal
from corridor.identity import document_numbering_schemes, party_canonical_names
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.source_intake import (
    ACCEPTED_DOC_TYPES,
    MAX_UPLOAD_BYTES,
    IntakeConflict,
    IntakeRefused,
    confirm_intake,
    list_confirmed_uploads,
    preview_intake,
    validate_and_stage,
)
from corridor.presentation import (
    documentation_review_label,
    field_label,
    input_reference_label,
    label,
    provenance_label,
    resolution_strategy_label,
    statement_type_label,
)
from corridor.report_release import (
    NoSuchReleasedReport,
    ReleaseRefusal,
    ReleasedArtifactIntegrityError,
    external_report_release_history,
    render_and_prepare_external_report,
    review_prepared_external_report,
    release_external_report,
    retrieve_prepared_external_report,
    retrieve_released_external_report,
)
from corridor.cohort import (
    CohortScopeViolation,
    cohort_candidate_ids,
    event_cohort_candidate_ids,
    require_cohort_member,
    require_event_cohort_member,
)
from corridor.models import CohortReceipt, EventCohortReceipt
from corridor.operative_support import resolve_operative_support
from corridor.work_decisions import (
    CoordinationSubject,
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
    ActiveRunDeclaration,
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    DependencyAdmissionOutcome,
    DocumentQuarantine,
    ExtractionRun,
    PolicyRun,
    RecordInclusionRequest,
)
from corridor.extraction_runs import declare_active_run
from corridor.extraction_runs import current_active_run_declaration, is_completed_run
from corridor.event_admission import read_event_admission_policy_status
from corridor.event_admission_acceptance import (
    lift_unknown_scope_admission,
    suspend_unknown_scope_admission,
)
from corridor.due_work import (
    HANDLER_PROJECT_PROCESSING,
    HANDLER_REVISION_RECONCILIATION,
)
from corridor.project_lock import lock_project
from corridor.statement_coordination import (
    AdmittedStatementCoordination,
    CLOSURE_TARGET_GAP,
    STATEMENT_NEXT_ACTION_CHOICES,
    StaleStatementCoordination,
    StatementCoordinationRefusal,
    StatementScopeCorrection,
    assign_admitted_statement_owner,
    coordinate_statement,
    correct_statement_facts,
    correct_statement_scope,
    keep_candidate_unresolved,
    keep_statement_unresolved,
    mark_statement_not_relevant,
    pending_candidate_authority_gap,
    pending_statement_authority_gap,
    read_admitted_statement_coordination,
    restore_statement_not_relevant,
    set_admitted_statement_next_action,
    undo_statement_coordination,
)
from corridor.statement_lifecycle import (
    current_candidate_disposition,
    current_lineage_statement,
)
from corridor.evidence_investigator_shadow import observe_shadow_review
from corridor.work_list import build_work_list
from corridor.web.statement_forms import (
    CANDIDATE_EVIDENCE_UNAVAILABLE,
    candidate_statement_evidence_view,
    optional_form_date,
    required_positive_form_id,
    statement_coordination_draft,
    statement_fact_correction_draft,
    statement_scope_from_form,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@pass_context
def _csrf_field(context) -> Markup:
    """Emit the hidden request-forgery field a signed-in POST form must carry.

    The token rides in a readable cookie set at sign-in; the form echoes it so
    the write path can match it against the session's stored hash (#331).
    """
    request = context.get("request")
    token = request.cookies.get(auth.CSRF_COOKIE, "") if request is not None else ""
    return Markup(
        f'<input type="hidden" name="{auth.CSRF_FIELD}" value="{escape(token)}">'
    )


TEMPLATES.env.globals.update(
    label=label,
    field_label=field_label,
    documentation_review_label=documentation_review_label,
    input_reference_label=input_reference_label,
    provenance_label=provenance_label,
    resolution_strategy_label=resolution_strategy_label,
    statement_type_label=statement_type_label,
    csrf_field=_csrf_field,
)
app = FastAPI(title="Corridor — coordination records")

_WORK_REASON_COPY = {
    "past_due": "The organization's commitment passed its stated date.",
    "critical_missing_internal_owner": (
        "No project person is assigned to this relocation, removal, or abandonment constraint."
    ),
    "critical_missing_next_action": (
        "This relocation, removal, or abandonment constraint has no Next Action."
    ),
    "committed_date_change": "The organization changed its promised timing.",
    "milestone_impact_unknown": "The effect on key dates is not yet known.",
    "disputed_date": "Sources disagree about a current date.",
    "required_by_advanced": "A schedule revision moved this constraint's Required By date.",
    "key_date_decision_affected": "A schedule revision moved a key date a recorded decision referenced.",
    "unknown_scope": "Applies to: not yet known.",
    "unplaced_statement": "Clarify the organization's statement and which constraints it applies to.",
    "missing_internal_owner": "Assign a project person for this Commitment.",
    "missing_next_action": "Set the Next Action for this Commitment.",
    "action_due": "The project Next Action is due now.",
    "action_due_date_unknown": "The Next Action needs a return date.",
    "external_closure_follow_up": "Confirm the project Next Action after the organization reported completion.",
}

def get_session():
    with SessionFactory() as session:
        yield session


def get_coordination_summary_client_factory():
    """Build an adapter only after the route found declared spend authority."""
    from corridor.llm import OpenAIClient

    def build(configuration):
        return OpenAIClient(
            model=configuration.model,
            timeout=configuration.timeout_seconds,
            max_output_tokens=configuration.max_output_tokens,
        )

    return build


async def get_human_principal(
    request: Request, session: Session = Depends(get_session)
) -> HumanPrincipal:
    """The signed-in person for this request (#331).

    Identity is the live session named by the HttpOnly cookie — never a form
    field, request header, role label, or deployment value, and with no
    fallback. An absent, expired, or revoked session refuses the request before
    any handler runs, so a revoked session or membership is felt at once. On a
    write, the session's request-forgery token must be echoed, so a cross-site
    POST — which carries neither the cookie nor the token — cannot act.
    """
    web_session = auth.load_session(request, session)
    if web_session is None:
        raise HTTPException(401, "sign in to continue")
    if request.method not in auth.SAFE_METHODS:
        submitted = await auth.extract_csrf(request)
        if not access.csrf_token_matches(web_session, submitted):
            raise HTTPException(403, "request could not be verified")
    return HumanPrincipal(web_session.principal_subject)


def _safe_return(redirect_to: str, fallback: str) -> str:
    """Only same-app paths: a form field must never become an open redirect."""
    candidate = (redirect_to or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    if candidate:
        raise HTTPException(400, "redirect_to must be a same-app path")
    return fallback


def _safe_cohort_return(
    return_to: str,
    *,
    project: Project,
    dependency_id: int,
    session: Session,
) -> str:
    """Only this Dependency's exact pinned rehearsal context may return."""

    candidate = _safe_return(return_to, "")
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
        raise HTTPException(400, "return_to must name this test coordination context")
    try:
        receipt_id = int(values["cohort_receipt_id"][0])
    except (TypeError, ValueError):
        raise HTTPException(400, "return_to must name this test coordination context")
    receipt = session.get(CohortReceipt, receipt_id)
    if receipt is None or receipt.project_id != project.id:
        raise HTTPException(400, "return_to must name this test coordination context")
    rail = build_cohort_rail(session, receipt, None)
    if dependency_id not in cohort_openable_dependency_ids(rail):
        raise HTTPException(400, "return_to must name this test coordination context")
    if "coordinate" in values:
        try:
            coordinate_id = int(values["coordinate"][0])
        except (TypeError, ValueError):
            raise HTTPException(400, "return_to must name this test coordination context")
        if values["coordinate"] != [str(coordinate_id)] or coordinate_id != dependency_id:
            raise HTTPException(400, "return_to must name this test coordination context")
    elif values.get("summary") != ["1"]:
        raise HTTPException(400, "return_to must name this test coordination context")
    return candidate


def _decision_location(
    slug: str,
    historical_document_id: int | None,
    cohort_receipt_id: int | None,
    *,
    event_cohort_receipt_id: int | None = None,
    coordinate_dependency_id: int | None = None,
) -> str:
    """Where a decision lands next: cohort lane or ordinary Work List.

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
    if historical_document_id is not None:
        return _queue_location(slug, historical_document_id)
    return f"/work/{slug}"


def _cohort_summary_location(lane_url: str) -> str:
    """Explicitly clear a deep-linked cohort item without leaving the lane."""

    return f"{lane_url}&summary=1"


def _next_cohort_coordinate_url(
    lane_url: str,
    rail,
    current_dependency_id: int | None,
) -> str:
    """Skip forward inside the incomplete admitted set, then fall back."""

    summary_url = _cohort_summary_location(lane_url)
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
    return sorted(siblings, key=lambda s: (s["doc_date"] or date.min))


def _project(
    session: Session,
    slug: str,
    principal: HumanPrincipal,
    *,
    designation: str | None = None,
) -> Project:
    """Resolve a project and, in the same step, gate access to it (#331).

    Every slug-addressed surface resolves its project here, so membership and
    designation are enforced by the resolver itself — a route cannot obtain a
    Project without passing the gate. A non-member sees the same 404 as a
    missing project, so project existence never leaks.
    """
    project = session.scalars(select(Project).where(Project.slug == slug)).first()
    if project is None:
        raise HTTPException(404, f"no project {slug!r}")
    _authorize(session, principal, project, designation=designation)
    return project


def _authorize(
    session: Session,
    principal: HumanPrincipal,
    project: Project,
    *,
    designation: str | None = None,
) -> access.MembershipAccess:
    """The one gate every project surface passes through (#331).

    Non-membership is answered exactly like a missing project, so an
    authenticated caller can neither enumerate projects nor confirm rows they
    may not see. A member who lacks the specific designation is refused with no
    side effects: membership never implies project coordination, Documentation
    Review, external release, or technical operations.
    """
    membership = access.resolve_membership(session, principal.subject, project.id)
    if membership is None:
        raise HTTPException(404, f"no project {project.slug!r}")
    if designation is not None and not membership.has(designation):
        raise HTTPException(403, "not authorized for this project action")
    return membership


def _project_for_document(session: Session, document_id: int) -> Project:
    """Resolve the owning project of a source document, or 404.

    Source images are addressed by document id alone, so the membership gate has
    to reach the project through the document rather than a slug in the path.
    """
    project_id = session.scalar(
        select(Document.project_id).where(Document.id == document_id)
    )
    if project_id is None:
        raise HTTPException(404, "no such page image")
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "no such page image")
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
        "Identify the right constraint, or choose Do not add.",
    ),
    "reference_resolves_to_many": (
        "Several records carry this number.",
        "Identify which one the organization was talking about.",
    ),
    "no_conflict_reference": (
        "This statement names no conflict.",
        "Read the quote and name the record it belongs to.",
    ),
    "party_mismatch": (
        "The speaker is not from the organization associated with this constraint.",
        "It may be an alias nobody has recorded, or the wrong record. "
        "Name the right one.",
    ),
    "party_unstated": (
        "This statement names no organization.",
        "Read the quote and name the record it belongs to.",
    ),
    "project_side_actor": (
        "The speaker is the project's own side.",
        "An internal action item, not an outside organization's commitment. "
        "It cannot attach as a statement.",
    ),
    "citations_unverified": (
        "The quote could not be found on its page.",
        "Check the page before placing it.",
    ),
    "event_type_outside_policy": (
        "This is not a Commitment or Change to promised timing.",
        "Only those statement types carry promised timing from the organization.",
    ),
    "no_date": (
        "This statement carries no date.",
        "Check the source wording and preserve the timing precision it supports.",
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
                        "field": name,
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
    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != project.id:
        raise HTTPException(404, "no such constraint in this project")
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
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The one pile: statements the machine could not place.

    Not a lane and not a queue — a short list a reviewer empties when
    they choose, because a dated promise from a meeting is exactly what
    this product exists to catch and losing it silently is worse.
    """
    project = _project(session, slug, principal)
    waiting = waiting_statements(session, project.id)
    for item in waiting:
        item["headline"], item["guidance"] = STATEMENT_REASONS.get(
            item["reason"],
            (
                "This statement needs clarification.",
                "Identify which constraints it applies to, or choose Do not add.",
            ),
        )
    return TEMPLATES.TemplateResponse(
        request,
        "statements.html",
        {"project": project, "waiting": waiting},
    )


@app.get("/statements/{slug}/{candidate_id}/coordinate", response_class=HTMLResponse)
def coordinate_statement_screen(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Show source Evidence and plain-language choices for one Unplaced Statement."""
    project = _project(session, slug, principal)
    candidate = _project_statement_candidate(session, project, candidate_id)
    if candidate.state == "pending" and observe_shadow_review(
        session, candidate.id, boundary="start", principal=principal
    ) is not None:
        pass
    response = _statement_coordination_screen(request, session, project, candidate)
    record_frontend_request(
        session,
        principal=principal,
        route_name="coordinate_statement_screen",
        route_template="/statements/{slug}/{candidate_id}/coordinate",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/coordinate")
async def save_coordinated_statement(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Delegate the ordinary screen's one Save to the atomic command."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        result = coordinate_statement(
            session,
            statement_coordination_draft(session, candidate, form),
            principal=principal,
        )
    except StaleStatementCoordination as exc:
        current = _project_statement_candidate(session, project, candidate_id)
        return _statement_coordination_screen(
            request,
            session,
            project,
            current,
            error=str(exc),
            status_code=409,
        )
    except StatementCoordinationRefusal as exc:
        current = _project_statement_candidate(session, project, candidate_id)
        return _statement_coordination_screen(
            request,
            session,
            project,
            current,
            error=str(exc),
            status_code=400,
        )
    observe_shadow_review(session, candidate.id, boundary="end", principal=principal)
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate_id}/coordinate",
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_coordinated_statement",
        route_template="/statements/{slug}/{candidate_id}/coordinate",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            commitment_lineage_id=result.event.commitment_lineage_id,
            statement_event_id=result.event.id,
        ),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/admitted/scope")
async def save_admitted_statement_scope(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Scope correction belongs only to the explicit Correct flow."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    coordination = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    if coordination is None:
        raise HTTPException(404, "no statement recorded by an exact rule in this project")
    response = _statement_coordination_screen(
        request,
        session,
        project,
        candidate,
        error="Use Correct to change which constraints an already recorded statement applies to.",
        status_code=400,
    )
    response.status_code = 400
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_admitted_statement_scope",
        route_template="/statements/{slug}/{candidate_id}/admitted/scope",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=(),
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/keep-unresolved")
def keep_unresolved_statement(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Acknowledge a recomputed structured gap and retain pending work."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    try:
        keep_statement_unresolved(
            session,
            project.id,
            candidate.id,
            principal=principal,
        )
    except (StatementCoordinationRefusal, StaleStatementCoordination) as exc:
        return _statement_coordination_screen(
            request,
            session,
            project,
            candidate,
            error=str(exc),
            status_code=409,
        )
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate",
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="keep_unresolved_statement",
        route_template="/statements/{slug}/{candidate_id}/keep-unresolved",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/admitted/owner")
async def save_admitted_statement_owner(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Assign an Internal Owner from the registered project roster."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    coordination = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    if coordination is None:
        raise HTTPException(404, "no statement recorded by an exact rule in this project")
    form = await request.form()
    try:
        roster_id = required_positive_form_id(form, "internal_owner_roster_entry_id")
        decision = assign_admitted_statement_owner(
            session,
            project.id,
            candidate.id,
            roster_id,
            principal=principal,
        )
    except (StatementCoordinationRefusal, ValueError) as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_admitted_statement_owner",
        route_template="/statements/{slug}/{candidate_id}/admitted/owner",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=(
                coordination.scope_dependencies[0].id
                if len(coordination.scope_dependencies) == 1
                else None
            ),
            commitment_lineage_id=coordination.lineage.id,
            work_decision_id=decision.id,
        ),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/admitted/next-action")
async def save_admitted_statement_next_action(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Append the structured project-language Next Action and return timing."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    coordination = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    if coordination is None:
        raise HTTPException(404, "no statement recorded by an exact rule in this project")
    form = await request.form()
    try:
        action = str(form.get("next_action") or "").strip()
        decision = set_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            action,
            due_date=optional_form_date(form, "action_due_date"),
            due_date_unknown_reason=(
                str(form.get("action_due_date_unknown_reason") or "").strip()
                or None
            ),
            principal=principal,
        )
    except (StatementCoordinationRefusal, ValueError) as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_admitted_statement_next_action",
        route_template="/statements/{slug}/{candidate_id}/admitted/next-action",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=(
                coordination.scope_dependencies[0].id
                if len(coordination.scope_dependencies) == 1
                else None
            ),
            commitment_lineage_id=coordination.lineage.id,
            work_decision_id=decision.id,
        ),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/coordination/{receipt_id}/undo")
def undo_coordinated_statement(
    request: Request,
    slug: str,
    candidate_id: int,
    receipt_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Undo exactly the Save identified by the rendered grouping receipt."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    receipt = session.get(StatementCoordinationReceipt, receipt_id)
    if receipt is None or receipt.candidate_id != candidate.id:
        raise HTTPException(404, "no such guided Save for this statement")
    try:
        undo_statement_coordination(session, receipt.id, principal=principal)
    except StatementCoordinationRefusal as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    session.commit()
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


@app.post("/statements/{slug}/{candidate_id}/not-relevant")
async def mark_waiting_statement_not_relevant(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Keep a reasoned non-statement disposition separate from generic rejection."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        mark_statement_not_relevant(
            session,
            candidate.id,
            reason=str(form.get("reason") or "").strip(),
            confirmed=str(form.get("confirmed") or "") == "yes",
            principal=principal,
        )
    except StatementCoordinationRefusal as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    observe_shadow_review(session, candidate.id, boundary="end", principal=principal)
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="mark_waiting_statement_not_relevant",
        route_template="/statements/{slug}/{candidate_id}/not-relevant",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/not-relevant/{disposition_id}/restore")
def restore_waiting_statement_not_relevant(
    request: Request,
    slug: str,
    candidate_id: int,
    disposition_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Restore a Candidate from its exact Not Relevant disposition."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    disposition = session.get(CandidateDisposition, disposition_id)
    if disposition is None or disposition.candidate_id != candidate.id:
        raise HTTPException(404, "no matching Do not add decision for this statement")
    try:
        restore_statement_not_relevant(session, disposition.id, principal=principal)
    except StatementCoordinationRefusal as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    session.commit()
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


@app.get("/statements/{slug}/{candidate_id}/correct", response_class=HTMLResponse)
def correct_statement_screen(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Show one supported scope or fact correction for the current statement."""
    project = _project(session, slug, principal)
    candidate = _project_statement_candidate(session, project, candidate_id)
    receipt = _active_statement_coordination_receipt(session, candidate.id)
    event: DependencyEvent | None
    scope_decision: DependencyEventScopeDecision | None
    if receipt is not None:
        event = current_lineage_statement(session, receipt.commitment_lineage_id)
        if event is None:
            raise HTTPException(409, "this statement is no longer current")
        scope_decision = session.scalar(
            select(DependencyEventScopeDecision)
            .where(
                DependencyEventScopeDecision.event_id == event.id,
                current_scope_decision_filter(),
            )
            .order_by(DependencyEventScopeDecision.id)
        )
    else:
        admitted = read_admitted_statement_coordination(
            session, project.id, candidate.id
        )
        if admitted is None:
            raise HTTPException(
                409, "this proposal has no current recorded statement to correct"
            )
        event = admitted.event
        scope_decision = admitted.scope
    if scope_decision is None:
        raise HTTPException(
            409,
            "this statement has no current record of which constraints it applies to",
        )
    affected_party = session.get(ExternalOrg, event.affected_external_org_id)
    response = TEMPLATES.TemplateResponse(
        request,
        "statement_correct.html",
        {
            "project": project,
            "candidate": candidate,
            "event": event,
            "affected_party_name": (
                affected_party.name
                if affected_party is not None
                else "Organization not identified"
            ),
            "scope_decision": scope_decision,
            "scope_dependency_ids": tuple(
                session.scalars(
                    select(DependencyEventScope.dependency_id).where(
                        DependencyEventScope.scope_decision_id == scope_decision.id
                    )
                ).all()
            ),
            "current_evidence": tuple(
                session.execute(
                    select(EvidenceLink, Document)
                    .join(Document, Document.id == EvidenceLink.document_id)
                    .join(
                        DependencyEventEvidence,
                        DependencyEventEvidence.evidence_link_id
                        == EvidenceLink.id,
                    )
                    .where(
                        DependencyEventEvidence.event_id == event.id,
                        Document.project_id == project.id,
                    )
                    .order_by(EvidenceLink.id)
                ).all()
            ),
            "parties": session.scalars(select(ExternalOrg).order_by(ExternalOrg.name)).all(),
            "documents": session.scalars(
                select(Document)
                .where(Document.project_id == project.id)
                .order_by(Document.filename)
            ).all(),
            "dependencies": session.execute(
                select(Dependency, ExternalOrg.name)
                .outerjoin(ExternalOrg, ExternalOrg.id == Dependency.external_org_id)
                .where(
                    Dependency.project_id == project.id,
                    Dependency.external_org_id == event.affected_external_org_id,
                    Dependency.dismissed_at.is_(None),
                )
                .order_by(Dependency.ref_code)
            ).all(),
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="correct_statement_screen",
        route_template="/statements/{slug}/{candidate_id}/correct",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/correct/scope")
async def correct_statement_scope_from_screen(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Delegate the scope form to the append-only scope correction command."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        correct_statement_scope(
            session,
            StatementScopeCorrection(
                candidate_id=candidate.id,
                event_id=required_positive_form_id(form, "expected_statement_event_id"),
                expected_scope_decision_id=required_positive_form_id(
                    form, "expected_scope_decision_id"
                ),
                scope=statement_scope_from_form(form),
            ),
            principal=principal,
        )
    except StaleStatementCoordination as exc:
        response = _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
        record_frontend_request(
            session,
            principal=principal,
            route_name="correct_statement_scope_from_screen",
            route_template="/statements/{slug}/{candidate_id}/correct/scope",
            method="POST",
            response=response,
            subject=FrontendRequestSubject(
                project_id=project.id, candidate_id=candidate.id
            ),
            request_fields=form,
        )
        session.commit()
        return response
    except (StatementCoordinationRefusal, ValueError) as exc:
        # The refusal receipt is the sole durable write for this request.  The
        # domain command validates before mutation, so committing it cannot
        # preserve a partial scope correction.
        response = _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
        record_frontend_request(
            session,
            principal=principal,
            route_name="correct_statement_scope_from_screen",
            route_template="/statements/{slug}/{candidate_id}/correct/scope",
            method="POST",
            response=response,
            subject=FrontendRequestSubject(
                project_id=project.id, candidate_id=candidate.id
            ),
            request_fields=form,
        )
        session.commit()
        return response
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="correct_statement_scope_from_screen",
        route_template="/statements/{slug}/{candidate_id}/correct/scope",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/statements/{slug}/{candidate_id}/correct/facts")
async def correct_statement_facts_from_screen(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Delegate a supported factual correction to the lineage successor command."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        successor = correct_statement_facts(
            session,
            statement_fact_correction_draft(form, candidate.id),
            principal=principal,
        )
    except StatementCoordinationRefusal as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    response = RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="correct_statement_facts_from_screen",
        route_template="/statements/{slug}/{candidate_id}/correct/facts",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            commitment_lineage_id=successor.commitment_lineage_id,
            predecessor_statement_event_id=successor.supersedes_event_id,
            successor_statement_event_id=successor.id,
        ),
        request_fields=form,
    )
    session.commit()
    return response


def _project_statement_candidate(
    session: Session, project: Project, candidate_id: int
) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.project_id != project.id or candidate.kind != "event":
        raise HTTPException(404, "no such proposed statement in this project")
    return candidate


def _active_statement_coordination_receipt(
    session: Session, candidate_id: int
) -> StatementCoordinationReceipt | None:
    return session.scalar(
        select(StatementCoordinationReceipt)
        .outerjoin(
            StatementCoordinationReversal,
            StatementCoordinationReversal.receipt_id
            == StatementCoordinationReceipt.id,
        )
        .where(
            StatementCoordinationReceipt.candidate_id == candidate_id,
            StatementCoordinationReversal.id.is_(None),
        )
        .order_by(StatementCoordinationReceipt.id.desc())
        .limit(1)
    )


def _admitted_statement_template_context(
    coordination: AdmittedStatementCoordination,
) -> dict:
    """Adapt residual statement coordination to the existing template names."""
    return {
        "event": coordination.event,
        "affected_party_name": coordination.affected_party_name,
        "lineage": coordination.lineage,
        "scope": coordination.scope,
        "outcome": coordination.outcome,
        "policy_run": coordination.policy_run,
        "evidence": coordination.evidence,
        "scope_dependencies": coordination.scope_dependencies,
        "roster": coordination.roster,
        "decision": coordination.next_decision,
        "authority_gap": coordination.authority_gap,
        "next_action_choices": coordination.next_action_choices,
    }


def _statement_coordination_screen(
    request: Request,
    session: Session,
    project: Project,
    candidate: Candidate,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """The browser reads facts; the command remains the one mutation seam."""
    admitted = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    if admitted is not None:
        return TEMPLATES.TemplateResponse(
            request,
            "statement_admitted_coordinate.html",
            {
                **_admitted_statement_template_context(admitted),
                "project": project,
                "candidate": candidate,
                "error": error,
            },
            status_code=status_code,
        )
    receipt = (
        _active_statement_coordination_receipt(session, candidate.id)
        if candidate.state == "accepted"
        else None
    )
    event = (
        current_lineage_statement(session, receipt.commitment_lineage_id)
        if receipt
        else None
    )
    line = (
        session.get(CommitmentLineage, receipt.commitment_lineage_id)
        if receipt
        else None
    )
    candidate_facts = prepare_candidate_statement_facts(session, candidate)
    candidate_evidence = candidate_statement_evidence_view(candidate_facts)
    candidate_evidence_available = candidate_facts.evidence_is_reviewable
    dependencies = session.execute(
        select(Dependency, ExternalOrg.name)
        .outerjoin(ExternalOrg, ExternalOrg.id == Dependency.external_org_id)
        .where(
            Dependency.project_id == project.id,
            Dependency.dismissed_at.is_(None),
        )
        .order_by(Dependency.ref_code)
    ).all()
    roster = session.scalars(
        select(ProjectRosterEntry)
        .where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.active.is_(True),
        )
        .order_by(ProjectRosterEntry.display_name)
    ).all()
    parties = list(
        session.scalars(select(ExternalOrg).order_by(ExternalOrg.name)).all()
    )
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project.id)
        .order_by(Milestone.code)
    ).all()
    fields = candidate_facts.fields
    candidate_party = candidate_facts.affected_party.wording
    candidate_stated_party = candidate_facts.stated_party.wording
    candidate_affected_party_id = (
        candidate_facts.affected_party.visible_external_org_id
    )
    candidate_stated_party_id = candidate_facts.stated_party.visible_external_org_id
    candidate_timing = _candidate_statement_timing_view(candidate_facts)
    disposition = current_candidate_disposition(session, candidate.id)
    not_relevant = (
        disposition
        if candidate.state == "rejected"
        and disposition is not None
        and disposition.disposition == "not_relevant"
        else None
    )
    history = _statement_coordination_history(session, candidate.id)
    pending_authority_gap = pending_statement_authority_gap(
        session,
        project.id,
        candidate.id,
    )
    unresolved_acknowledgment = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == audit.CANDIDATE,
            AuditLog.entity_id == candidate.id,
            AuditLog.action == audit.KEEP_STATEMENT_UNRESOLVED,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return TEMPLATES.TemplateResponse(
        request,
        "statement_coordinate.html",
        {
            "project": project,
            "candidate": candidate,
            "candidate_fields": fields,
            "candidate_party": candidate_party,
            "candidate_affected_party_id": candidate_affected_party_id,
            "candidate_stated_party": candidate_stated_party,
            "candidate_stated_party_id": candidate_stated_party_id,
            "candidate_description": str(fields.get("description") or ""),
            "candidate_event_date": str(fields.get("event_date") or ""),
            "candidate_timing": candidate_timing,
            "guided_save_available": (
                candidate_evidence_available and candidate_timing["available"]
            ),
            "next_action_choices": STATEMENT_NEXT_ACTION_CHOICES,
            "candidate_evidence": candidate_evidence,
            "candidate_evidence_available": candidate_evidence_available,
            "candidate_evidence_unavailable_message": (
                CANDIDATE_EVIDENCE_UNAVAILABLE
            ),
            "dependencies": [
                {
                    "id": dependency.id,
                    "ref_code": dependency.ref_code,
                    "source_ref": dependency.source_ref,
                    "title": dependency.title,
                    "location_desc": dependency.location_desc,
                    "station_from": dependency.station_from,
                    "station_to": dependency.station_to,
                    "external_org_id": dependency.external_org_id,
                    "external_org_name": external_org_name or "Organization not identified",
                }
                for dependency, external_org_name in dependencies
            ],
            "roster": roster,
            "parties": parties,
            "milestones": milestones,
            "receipt": receipt,
            "event": event,
            "line": line,
            "not_relevant": not_relevant,
            "history": history,
            "pending_authority_gap": pending_authority_gap,
            "pending_authority_gap_label": (
                _authority_gap_label(pending_authority_gap.code)
                if pending_authority_gap is not None
                else None
            ),
            "unresolved_acknowledgment": unresolved_acknowledgment,
            "error": error,
        },
        status_code=status_code,
    )


def _candidate_statement_timing_view(
    facts: CandidateStatementFacts,
) -> dict[str, str | bool]:
    """Expose supported Candidate timing read-only; never ask for transcription."""
    timing = facts.new_timing.visible_timing
    return {
        "available": facts.new_timing.is_visible,
        "text": timing.text if timing is not None else "",
        "precision": timing.precision if timing is not None else "",
        "start_date": (
            timing.start_date.isoformat()
            if timing is not None and timing.start_date is not None
            else ""
        ),
        "end_date": (
            timing.end_date.isoformat()
            if timing is not None and timing.end_date is not None
            else ""
        ),
    }


def _statement_coordination_history(session: Session, candidate_id: int) -> tuple[dict, ...]:
    """Render durable lifecycle acts in plain time order without hiding reversals."""
    receipts = session.scalars(
        select(StatementCoordinationReceipt)
        .where(StatementCoordinationReceipt.candidate_id == candidate_id)
        .order_by(StatementCoordinationReceipt.id)
    ).all()
    dispositions = session.scalars(
        select(CandidateDisposition)
        .where(CandidateDisposition.candidate_id == candidate_id)
        .order_by(CandidateDisposition.id)
    ).all()
    reversals = session.scalars(
        select(StatementCoordinationReversal)
        .where(StatementCoordinationReversal.candidate_id == candidate_id)
        .order_by(StatementCoordinationReversal.id)
    ).all()
    unresolved = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == audit.CANDIDATE,
            AuditLog.entity_id == candidate_id,
            AuditLog.action == audit.KEEP_STATEMENT_UNRESOLVED,
        )
        .order_by(AuditLog.id)
    ).all()
    rows = [
        {
            "created_at": receipt.created_at,
            "label": "Saved statement and Follow-up plan",
            "detail": f"grouping receipt {receipt.id}",
        }
        for receipt in receipts
    ]
    rows.extend(
        {
            "created_at": disposition.created_at,
            "label": (
                "Not added to project record"
                if disposition.disposition == "not_relevant"
                else "Recorded proposed statement"
            ),
            "detail": disposition.reason.replace("_", " ") if disposition.reason else "",
        }
        for disposition in dispositions
    )
    rows.extend(
        {
            "created_at": reversal.created_at,
            "label": (
                "Undid guided Save"
                if reversal.receipt_id is not None
                else "Restored extracted statement"
            ),
            "detail": (
                f"grouping receipt {reversal.receipt_id}"
                if reversal.receipt_id is not None
                else f"disposition {reversal.candidate_disposition_id}"
            ),
        }
        for reversal in reversals
    )
    rows.extend(
        {
            "created_at": entry.ts,
            "label": "Recorded unresolved authority gap",
            "detail": _authority_gap_label(
                str((entry.after_json or {}).get("authority_gap") or "")
            ),
        }
        for entry in unresolved
    )
    return tuple(sorted(rows, key=lambda row: (row["created_at"], row["label"])))


def _authority_gap_label(code: str) -> str:
    return {
        "closure_target_commitment_not_established": (
            "Commitment covered by the completion report is not established"
        ),
        "closure_target_relationship_not_established": (
            "One open commitment is recorded, but the completion report does not "
            "establish that it covers that commitment"
        ),
        "closure_target_commitment_ambiguous": (
            "Several open commitments match; the completion report does not "
            "identify which one"
        ),
        "closure_affected_party_not_established": (
            "The organization that reported completion is not established"
        ),
    }.get(code, code.replace("_", " "))


def _safe_next(candidate: str) -> str:
    """A same-app path, or ``/`` — never an off-site redirect target (#331)."""
    value = (candidate or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@app.get("/", response_class=HTMLResponse)
def root(request: Request, session: Session = Depends(get_session)):
    """Send a signed-in person to their projects, everyone else to sign-in."""
    web_session = auth.load_session(request, session)
    if web_session is None:
        return RedirectResponse("/sign-in", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "projects.html",
        {
            "projects": access.member_projects(session, web_session.principal_subject),
            "email": web_session.email_normalized,
        },
    )


@app.get("/sign-in", response_class=HTMLResponse)
def sign_in_form(request: Request, next: str = "", session: Session = Depends(get_session)):
    """The passwordless entry point; no identity is revealed here."""
    if auth.load_session(request, session) is not None:
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "sign_in.html",
        {"next": _safe_next(next), "sent": False, "throttled": False},
    )


@app.post("/sign-in/request", response_class=HTMLResponse)
def request_sign_in(
    request: Request,
    email: str = Form(...),
    next: str = Form(""),
    session: Session = Depends(get_session),
    sender: auth.EmailSender = Depends(auth.get_email_sender),
):
    """Issue a magic link, without ever revealing whether the address is enrolled.

    Every well-formed request gets the same "check your email" answer; only the
    abuse ceiling (independent of membership) can change it. A live link is
    reused rather than multiplied, and a link is created and mailed only for an
    enrolled identity — but the response never says which case occurred (#331).
    """
    normalized = access.normalize_email(email)
    scope = auth.client_scope(request)
    throttled = access.over_limit(
        session, access.ISSUE_IP, scope, access.MAX_ISSUE_PER_IP
    ) or (
        bool(normalized)
        and access.over_limit(
            session, access.ISSUE_EMAIL, normalized, access.MAX_ISSUE_PER_EMAIL
        )
    )
    if throttled:
        session.commit()
        response = TEMPLATES.TemplateResponse(
            request,
            "sign_in.html",
            {"next": _safe_next(next), "sent": False, "throttled": True},
            status_code=429,
        )
        return response
    access.record_attempt(session, access.ISSUE_IP, scope)
    if normalized:
        access.record_attempt(session, access.ISSUE_EMAIL, normalized)
        identity = access.identity_for_email(session, normalized)
        if identity is not None and not access.has_live_token(session, normalized):
            issued = access.issue_sign_in_token(
                session, normalized, redirect_path=_safe_next(next)
            )
            link = (
                str(request.base_url).rstrip("/")
                + "/sign-in/consume?token="
                + quote(issued.raw_token)
            )
            sender.send_sign_in_link(email=normalized, link=link)
    session.commit()
    return TEMPLATES.TemplateResponse(
        request,
        "sign_in.html",
        {"next": _safe_next(next), "sent": True, "throttled": False},
    )


@app.get("/sign-in/consume")
def consume_sign_in(
    request: Request,
    token: str = "",
    session: Session = Depends(get_session),
):
    """Spend a link once and open a session, or fail the same way for every cause.

    Expired, reused, tampered, and unknown tokens are indistinguishable: each
    lands on the same generic error, and none establishes a session.  The
    redirect target is re-constrained to the application (#331).
    """
    scope = auth.client_scope(request)
    if access.over_limit(session, access.CONSUME_IP, scope, access.MAX_CONSUME_PER_IP):
        session.commit()
        return TEMPLATES.TemplateResponse(
            request, "sign_in_invalid.html", {}, status_code=429
        )
    access.record_attempt(session, access.CONSUME_IP, scope)
    consumed = access.consume_sign_in_token(session, token)
    if consumed is None:
        session.commit()
        return TEMPLATES.TemplateResponse(
            request, "sign_in_invalid.html", {}, status_code=400
        )
    new_session = access.create_web_session(
        session,
        principal=consumed.principal,
        email_normalized=consumed.email_normalized,
    )
    response = RedirectResponse(_safe_next(consumed.redirect_path or "/"), status_code=303)
    auth.set_session_cookies(response, new_session)
    session.commit()
    return response


@app.post("/sign-out")
def sign_out(
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Revoke this session now; a cross-site POST cannot reach here (CSRF)."""
    raw = request.cookies.get(auth.SESSION_COOKIE) or ""
    access.revoke_web_session(session, raw)
    session.commit()
    response = RedirectResponse("/sign-in", status_code=303)
    auth.clear_session_cookies(response)
    return response


@app.get("/reports/{slug}", response_class=HTMLResponse)
def reports(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Ordinary project-person entry point for fixed PDF release."""
    project = _project(session, slug, principal)
    response = TEMPLATES.TemplateResponse(
        request,
        "report_release.html",
        {
            "project": project,
            "history": external_report_release_history(session, project.id),
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="reports",
        route_template="/reports/{slug}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.get("/reports/{slug}/prepared/{artifact_id}", response_class=HTMLResponse)
def review_report(
    request: Request,
    slug: str,
    artifact_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Review one retained PDF and only its frozen context."""
    project = _project(session, slug, principal)
    try:
        review = review_prepared_external_report(session, project.id, artifact_id)
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    response = TEMPLATES.TemplateResponse(
        request,
        "report_review.html",
        {"project": project, "review": review},
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="review_report",
        route_template="/reports/{slug}/prepared/{artifact_id}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, artifact_id=artifact_id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.get("/reports/{slug}/prepared/{artifact_id}/download")
def download_prepared_report(
    request: Request,
    slug: str,
    artifact_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Download exactly the retained bytes awaiting a release decision."""
    project = _project(session, slug, principal)
    try:
        artifact = retrieve_prepared_external_report(
            session, project.id, artifact_id
        )
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    response = _pdf_download(bytes(artifact.pdf_bytes), artifact.artifact_name)
    record_frontend_request(
        session,
        principal=principal,
        route_name="download_prepared_report",
        route_template="/reports/{slug}/prepared/{artifact_id}/download",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, artifact_id=artifact.id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.get("/reports/{slug}/prepared/{artifact_id}/preview")
def preview_prepared_report(
    request: Request,
    slug: str,
    artifact_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Display exactly the retained bytes awaiting a release decision."""
    project = _project(session, slug, principal)
    try:
        artifact = retrieve_prepared_external_report(
            session, project.id, artifact_id
        )
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    response = _pdf_response(
        bytes(artifact.pdf_bytes), artifact.artifact_name, disposition="inline"
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="preview_prepared_report",
        route_template="/reports/{slug}/prepared/{artifact_id}/preview",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, artifact_id=artifact.id
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post(
    "/reports/{slug}/prepared/{artifact_id}/release", response_class=HTMLResponse
)
def release_prepared_report(
    request: Request,
    slug: str,
    artifact_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Authorize the artifact bound to the review control, without rerendering."""
    project = _project(session, slug, principal, designation=access.EXTERNAL_RELEASE)
    try:
        with session.begin_nested():
            receipt = release_external_report(
                session,
                project_id=project.id,
                artifact_id=artifact_id,
                principal=principal,
            )
            history = external_report_release_history(session, project.id)
            released = next(item for item in history if item.release_id == receipt.id)
            response = TEMPLATES.TemplateResponse(
                request,
                "report_release.html",
                {"project": project, "history": history, "released": released},
                status_code=201,
            )
            record_frontend_request(
                session,
                principal=principal,
                route_name="release_prepared_report",
                route_template="/reports/{slug}/prepared/{artifact_id}/release",
                method="POST",
                response=response,
                subject=FrontendRequestSubject(
                    project_id=project.id,
                    artifact_id=artifact_id,
                    release_id=receipt.id,
                ),
                request_fields={},
            )
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return response


@app.get("/reports/{slug}/releases/{release_id}/download")
def download_released_report(
    slug: str,
    release_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Retrieve the immutable bytes named by one historical release."""
    project = _project(session, slug, principal)
    try:
        receipt = retrieve_released_external_report(
            session, project.id, release_id
        )
    except NoSuchReleasedReport as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReleasedArtifactIntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _pdf_download(bytes(receipt.pdf_bytes), receipt.artifact_name)


def _pdf_download(pdf_bytes: bytes, artifact_name: str) -> Response:
    """Return fixed bytes with an encoded, non-executable download name."""
    return _pdf_response(pdf_bytes, artifact_name, disposition="attachment")


def _pdf_response(
    pdf_bytes: bytes, artifact_name: str, *, disposition: Literal["inline", "attachment"]
) -> Response:
    """Return fixed bytes with an encoded, non-executable presentation name."""
    encoded_name = quote(artifact_name, safe="")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}"
        },
    )


@app.post("/reports/{slug}/render")
def render_report(
    request: Request,
    slug: str,
    ordinary: str | None = Form(None),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Render and retain one fixed PDF; this does not release it externally."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        with session.begin_nested():
            _rendered, report_run, artifact = render_and_prepare_external_report(
                session, project_id=project.id
            )
            if ordinary is not None:
                review = review_prepared_external_report(
                    session, project.id, artifact.id
                )
                response = TEMPLATES.TemplateResponse(
                    request,
                    "report_review.html",
                    {"project": project, "review": review},
                    status_code=201,
                )
            else:
                response = JSONResponse(
                    status_code=201,
                    content={
                        "artifact_id": artifact.id,
                        "artifact_name": artifact.artifact_name,
                        "pdf_sha256": artifact.pdf_sha256,
                    },
                )
            record_frontend_request(
                session,
                principal=principal,
                route_name="render_report",
                route_template="/reports/{slug}/render",
                method="POST",
                response=response,
                subject=FrontendRequestSubject(
                    project_id=project.id,
                    artifact_id=artifact.id,
                    report_run_id=report_run.id,
                ),
                request_fields={"ordinary": ordinary},
            )
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return response


@app.post("/reports/{slug}/release")
def release_report(
    slug: str,
    artifact_id: int = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Human-authorize one previously rendered PDF without regenerating it."""
    project = _project(session, slug, principal, designation=access.EXTERNAL_RELEASE)
    try:
        release = release_external_report(
            session,
            project_id=project.id,
            artifact_id=artifact_id,
            principal=principal,
        )
    except ReleaseRefusal as exc:
        raise HTTPException(409, str(exc)) from exc
    response = JSONResponse(
        status_code=201,
        content={
            "release_id": release.id,
            "artifact_name": release.artifact_name,
            "pdf_sha256": release.pdf_sha256,
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="release_report",
        route_template="/reports/{slug}/release",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            artifact_id=artifact_id,
            release_id=release.id,
        ),
        request_fields={"artifact_id": artifact_id},
    )
    session.commit()
    return response


def _key_dates_rows(session: Session, project: Project) -> list[dict]:
    """Current Key dates with their live Key Date Version, undated ones last."""
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project.id)
        .order_by(
            Milestone.need_date.is_(None),
            Milestone.need_date,
            Milestone.code,
        )
    ).all()
    rows = []
    for milestone in milestones:
        registration = (
            session.get(MilestoneRegistration, milestone.current_registration_id)
            if milestone.current_registration_id is not None
            else None
        )
        rows.append(
            {
                "code": milestone.code,
                "name": milestone.name,
                "need_date": milestone.need_date,
                "source_name": registration.source_name if registration else milestone.source,
                "recorded_by": registration.recorded_by if registration else None,
                "version_id": milestone.current_registration_id,
            }
        )
    return rows


def _key_dates_context(session: Session, project: Project, **overrides) -> dict:
    context = {
        "project": project,
        "rows": _key_dates_rows(session, project),
        "preview": None,
        "content": "",
        "source_name": "",
        "predecessors_json": "",
        "message": None,
        "error": None,
        "stale": None,
        "draft": None,
        "draft_receipt_id": "",
    }
    context.update(overrides)
    return context


@app.get("/key-dates/{slug}", response_class=HTMLResponse)
def key_dates(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read-only view of a project's registered Key dates and the import form."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    return TEMPLATES.TemplateResponse(
        request,
        "key_dates.html",
        _key_dates_context(
            session, project, message=request.query_params.get("imported")
        ),
    )


@app.post("/key-dates/{slug}/preview", response_class=HTMLResponse)
def key_dates_preview(
    request: Request,
    slug: str,
    source_name: str = Form(""),
    content: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Dry-run a hand-typed CSV: show its source, fingerprint, and row effects.

    Nothing is written; malformed input refuses without a partial import.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    resolved_source = source_name.strip() or "pasted-key-dates.csv"
    try:
        preview = preview_import(
            session,
            project_id=project.id,
            content=content.encode("utf-8"),
            source_name=resolved_source,
        )
    except MalformedMilestoneCsv as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "key_dates.html",
            _key_dates_context(
                session,
                project,
                content=content,
                source_name=source_name,
                error=str(exc),
            ),
            status_code=422,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "key_dates.html",
        _key_dates_context(
            session,
            project,
            preview=preview,
            content=content,
            source_name=preview.source_name,
            predecessors_json=json.dumps(preview.predecessors),
        ),
    )


@app.get("/key-dates/{slug}/drafts/{receipt_id}/preview", response_class=HTMLResponse)
def key_date_draft_preview(
    request: Request,
    slug: str,
    receipt_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Show a retained, non-authoritative source-bound draft in the normal preview.

    This route cannot invoke a runtime.  It only re-reads a receipt already
    bound to the authorized project source, then hands its validated rows to
    #337's existing preview.  That keeps model drafting separate from a
    person's ordinary import confirmation.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        draft = load_key_date_draft(
            session, receipt_id=receipt_id, project_id=project.id
        )
    except StaleKeyDateDraft as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "key_dates.html",
            _key_dates_context(session, project, error=str(exc)),
            status_code=409,
        )
    if not draft.rows:
        # The unresolved findings and quarantined sequencing are the product
        # here: they stay visible as questions for a person, with nothing to
        # import and no importer offered.
        return TEMPLATES.TemplateResponse(
            request,
            "key_dates.html",
            _key_dates_context(
                session,
                project,
                draft=draft,
                error=(
                    "this source-bound draft has no supported day-precise "
                    "Key date rows; its unresolved findings are shown below"
                ),
            ),
        )
    preview = preview_drafted_key_dates(session, draft)
    return TEMPLATES.TemplateResponse(
        request,
        "key_dates.html",
        _key_dates_context(
            session,
            project,
            preview=preview,
            content=draft.csv_content.decode("utf-8"),
            source_name=preview.source_name,
            predecessors_json=json.dumps(preview.predecessors),
            draft=draft,
            draft_receipt_id=str(draft.receipt_id),
        ),
    )


@app.post("/key-dates/{slug}/confirm")
def key_dates_confirm(
    request: Request,
    slug: str,
    source_name: str = Form(...),
    content: str = Form(...),
    expected_sha256: str = Form(...),
    expected_predecessors: str = Form(...),
    draft_receipt_id: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Commit the previewed CSV under the acting person's stable identity.

    Bound to the previewed bytes and the Key Date Versions the preview showed:
    changed content or a moved project state refuses and re-presents the current
    state rather than importing against a stale review.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        predecessors = json.loads(expected_predecessors) if expected_predecessors else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "expected_predecessors must be JSON") from exc
    if not isinstance(predecessors, dict):
        raise HTTPException(400, "expected_predecessors must be a JSON object")
    if draft_receipt_id:
        try:
            receipt_id = int(draft_receipt_id)
        except ValueError as exc:
            raise HTTPException(400, "draft_receipt_id must be an integer") from exc
        try:
            verify_draft_for_ordinary_import(
                session,
                receipt_id=receipt_id,
                project_id=project.id,
                content=content.encode("utf-8"),
                source_name=source_name,
            )
        except StaleKeyDateDraft as exc:
            raise HTTPException(409, str(exc)) from exc
    try:
        with session.begin_nested():
            result = confirm_import(
                session,
                project_id=project.id,
                content=content.encode("utf-8"),
                source_name=source_name,
                expected_sha256=expected_sha256,
                expected_predecessors=predecessors,
                principal=principal,
            )
    except MalformedMilestoneCsv as exc:
        raise HTTPException(422, str(exc)) from exc
    except StaleMilestoneImport as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "key_dates.html",
            _key_dates_context(
                session,
                project,
                preview=exc.preview,
                content=content,
                source_name=exc.preview.source_name,
                predecessors_json=json.dumps(exc.preview.predecessors),
                stale=str(exc),
            ),
            status_code=409,
        )
    session.commit()
    summary = f"{len(result.created)} added, {len(result.updated)} updated"
    return RedirectResponse(
        f"/key-dates/{project.slug}?imported={quote(summary)}", status_code=303
    )


# --- Internal working Coordination Report -----------------------------------
# A live, refreshable reading of the current Project Record for project
# coordinators, served without a terminal command. It reuses the same coherent
# project reading, Evaluation, report, facet and export behavior the external
# release path renders, but it seals nothing and advances nothing: viewing,
# refreshing, filtering, or downloading here reads only. It records no
# ReportRun, prepares no external artifact, and moves no report comparison
# baseline — the external release (one fixed PDF, ADR-0040) stays under
# /reports/{slug}, and the baseline it advances is the last released report
# (ADR-0053). Each surface builds one coherent bound reading through
# build_report, which freezes population, Evaluation, statement reading and
# provenance mode together and refuses a mismatched set.

_INTERNAL_REPORT_BANNER = (
    "Internal working view of the current project record. This is not an "
    "approved external release: viewing, refreshing, filtering, and downloading "
    "here record no report run and move no comparison baseline "
    "(ADR-0040, ADR-0053)."
)

_ALERT_POPULATION_PAGE = 50


def _provenance_mode_label(document_only: bool) -> str:
    return "Documents only" if document_only else "All supported sources"


def _xlsx_download(xlsx_bytes: bytes, filename: str) -> Response:
    """Return workbook bytes with an encoded, non-executable download name."""
    encoded_name = quote(filename, safe="")
    return Response(
        content=xlsx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"
        },
    )


def _internal_workbook_bytes(
    session: Session,
    project_id: int,
    *,
    evaluation,
    statement_publication,
) -> bytes:
    """Render the internal working workbook without retaining a file on disk."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = to_xlsx(
            session,
            project_id,
            Path(tmp) / "internal-working-constraint-log.xlsx",
            evaluation=evaluation,
            statement_publication=statement_publication,
            internal_working_copy=True,
        )
        return path.read_bytes()


def _internal_workbook_name(
    project: Project, evaluated_on: date, document_only: bool
) -> str:
    mode = "documents-only" if document_only else "all-sources"
    return (
        f"{project.slug}-internal-working-constraint-log-"
        f"{evaluated_on.isoformat()}-{mode}.xlsx"
    )


@app.get("/internal-report/{slug}", response_class=HTMLResponse)
def internal_report(
    request: Request,
    slug: str,
    document_only: bool = False,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The ordinary internal entry: current facts, no approval step."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    report = build_report(session, project.id, document_only=document_only)
    evaluation = report.evaluation
    facets = [
        {
            "rule": facet.rule,
            "name": format_exception_name(facet.rule),
            "count": facet.count,
            "top_quantity_days": (
                facet.exceptions[0].quantity_days if facet.has_quantities else None
            ),
            "critical_count": sum(1 for exception in facet.exceptions if exception.critical),
        }
        for facet in evaluation.facets()
    ]
    response = TEMPLATES.TemplateResponse(
        request,
        "internal_report.html",
        {
            "project": project,
            "document_only": document_only,
            "provenance_mode_label": _provenance_mode_label(document_only),
            "banner": _INTERNAL_REPORT_BANNER,
            "evaluated_on": evaluation.today,
            "ruleset_version": evaluation.ruleset_version,
            "thresholds": evaluation.thresholds,
            "covered_count": len(report.covered_records),
            "summary": report.summary,
            "coverage_note": report.coverage_note,
            "facets": facets,
            "summary_configuration": current_summary_configuration(session, project.id),
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="internal_report",
        route_template="/internal-report/{slug}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/coordination-summary/configuration")
async def declare_coordination_summary_configuration(
    slug: str,
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Declare bounded summary authority; this is technical operations, not a draft."""
    project = _project(session, slug, principal, designation=access.TECHNICAL_OPERATIONS)
    form = await request.form()
    try:
        declare_summary_configuration(
            session, project_id=project.id, principal=principal,
            model=str(form.get("model", "")), prompt_version=str(form.get("prompt_version", "")),
            source_scope=str(form.get("source_scope", "")),
            max_input_tokens=int(str(form.get("max_input_tokens", ""))),
            max_output_tokens=int(str(form.get("max_output_tokens", ""))),
            timeout_seconds=int(str(form.get("timeout_seconds", ""))),
            max_requests=int(str(form.get("max_requests", ""))),
            retry_policy=str(form.get("retry_policy", "")),
            retention_policy=str(form.get("retention_policy", "")),
            observation_context=str(form.get("observation_context", "")),
        )
    except (ValueError, InvalidSummaryConfiguration) as exc:
        raise HTTPException(400, f"Coordination Summary configuration refused: {exc}") from exc
    session.commit()
    return RedirectResponse(f"/internal-report/{project.slug}", status_code=303)


@app.post("/internal-report/{slug}/coordination-summary")
def request_internal_coordination_summary(
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    client_factory=Depends(get_coordination_summary_client_factory),
):
    """The single ordinary action that may spend the declared bounded budget."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        receipt = request_coordination_summary(
            session, project_id=project.id, principal=principal, client_factory=client_factory,
        )
    except ConfigurationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/internal-report/{project.slug}/coordination-summary/{receipt.public_id}",
        status_code=303,
    )


@app.get("/internal-report/{slug}/coordination-summary/{public_id}", response_class=HTMLResponse)
def read_internal_coordination_summary(
    request: Request,
    slug: str,
    public_id: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read one retained receipt; a GET never invokes a model."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    receipt = session.scalars(
        select(CoordinationSummaryRequest).where(
            CoordinationSummaryRequest.public_id == public_id,
            CoordinationSummaryRequest.project_id == project.id,
        )
    ).first()
    if receipt is None:
        raise HTTPException(404, "no Coordination Summary receipt for this project")
    response = TEMPLATES.TemplateResponse(
        request, "coordination_summary.html", {"project": project, "receipt": receipt},
    )
    record_frontend_request(
        session, principal=principal, route_name="coordination_summary_receipt",
        route_template="/internal-report/{slug}/coordination-summary/{public_id}",
        method="GET", response=response,
        subject=FrontendRequestSubject(project_id=project.id), request_fields=request.path_params,
    )
    session.commit()
    return response


@app.get("/internal-report/{slug}/full", response_class=HTMLResponse)
def internal_report_full(
    request: Request,
    slug: str,
    document_only: bool = False,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The full report markup, reused verbatim and marked internal."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    report = build_report(session, project.id, document_only=document_only)
    response = HTMLResponse(render(report, banner=_INTERNAL_REPORT_BANNER))
    record_frontend_request(
        session,
        principal=principal,
        route_name="internal_report_full",
        route_template="/internal-report/{slug}/full",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.get("/internal-report/{slug}/alerts/{rule}", response_class=HTMLResponse)
def internal_report_alerts(
    request: Request,
    slug: str,
    rule: str,
    document_only: bool = False,
    page: int = 1,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One alert's complete matching constraint population, bounded for reading."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    if rule not in RULES:
        raise HTTPException(404, "no such constraint alert")
    report = build_report(session, project.id, document_only=document_only)
    evaluation = report.evaluation
    # The complete matching population from the same reading the counts came
    # from, not the visible Work List page. browse filters on the computed
    # exceptions, so one row per matching constraint, and the total equals the
    # facet count.
    matching = browse(
        session,
        project.id,
        evaluation=evaluation,
        rule=rule,
        limit=100_000,
    )
    total = len(matching)
    pages = max(1, (total + _ALERT_POPULATION_PAGE - 1) // _ALERT_POPULATION_PAGE)
    page = min(max(page, 1), pages)
    start = (page - 1) * _ALERT_POPULATION_PAGE
    window = matching[start : start + _ALERT_POPULATION_PAGE]
    rows = []
    for row in window:
        found = next(
            (exception for exception in row.exceptions if exception.rule == rule), None
        )
        rows.append(
            {
                "dependency_id": row.dependency.id,
                "ref_code": row.dependency.ref_code,
                "title": row.dependency.title,
                "org_name": row.org_name,
                "is_ready": row.is_ready,
                "critical": found.critical if found is not None else False,
                "detail": found.detail if found is not None else "",
                "quantity_days": found.quantity_days if found is not None else None,
                "alert_label": found.label if found is not None else format_exception_name(rule),
            }
        )
    response = TEMPLATES.TemplateResponse(
        request,
        "internal_report_alerts.html",
        {
            "project": project,
            "document_only": document_only,
            "provenance_mode_label": _provenance_mode_label(document_only),
            "evaluated_on": evaluation.today,
            "ruleset_version": evaluation.ruleset_version,
            "rule": rule,
            "rule_name": format_exception_name(rule),
            "total": total,
            "rows": rows,
            "page": page,
            "pages": pages,
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="internal_report_alerts",
        route_template="/internal-report/{slug}/alerts/{rule}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.get("/internal-report/{slug}/workbook.xlsx")
def internal_report_workbook(
    request: Request,
    slug: str,
    document_only: bool = False,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The internal working workbook, at the same reading the view shows."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    report = build_report(session, project.id, document_only=document_only)
    xlsx_bytes = _internal_workbook_bytes(
        session,
        project.id,
        evaluation=report.evaluation,
        statement_publication=report.statement_publication,
    )
    response = _xlsx_download(
        xlsx_bytes,
        _internal_workbook_name(project, report.evaluation.today, document_only),
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="internal_report_workbook",
        route_template="/internal-report/{slug}/workbook.xlsx",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


def _operations_checks_form_values(
    effective, submitted: dict[str, str] | None
) -> dict[str, str]:
    """The value to show in each field: what was typed, else the effective one.

    On a refusal or a preview the operator sees exactly what they entered, so a
    rejected value is corrected rather than silently replaced. On a fresh view
    the fields carry the effective configuration, declared or default.
    """
    values: dict[str, str] = {}
    for spec in SUPPORTED_THRESHOLDS:
        if submitted is not None and submitted.get(spec.key) is not None:
            values[spec.key] = str(submitted[spec.key])
        else:
            values[spec.key] = str(getattr(effective.thresholds, spec.key))
    return values


def _render_operations_checks(
    request: Request,
    session: Session,
    project: Project,
    *,
    preview=None,
    error: str | None = None,
    submitted: dict[str, str] | None = None,
    saved_id: int | None = None,
    status_code: int = 200,
):
    """One coherent render of the check-configuration operations view."""
    effective = effective_configuration(session, project.id)
    form_values = _operations_checks_form_values(effective, submitted)
    supported = [
        {
            "key": spec.key,
            "label": spec.label,
            "unit": spec.unit,
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "rules": [format_exception_name(rule) for rule in spec.rules],
            "effective_value": getattr(effective.thresholds, spec.key),
            "form_value": form_values[spec.key],
        }
        for spec in SUPPORTED_THRESHOLDS
    ]
    history = [
        {
            "id": row.id,
            "stale_days": row.stale_days,
            "due_soon_days": row.due_soon_days,
            "action_due_soon_days": row.action_due_soon_days,
            "ruleset_version": row.ruleset_version,
            "declared_by": row.created_by,
            "declared_at": row.created_at,
            "is_effective": row.id == effective.configuration_id,
        }
        for row in configuration_history(session, project.id)
    ]
    preview_context = None
    if preview is not None:
        preview_context = {
            "evaluated_on": preview.evaluated_on,
            "ruleset_version": preview.ruleset_version,
            "proposed": {
                spec.key: getattr(preview.proposed_thresholds, spec.key)
                for spec in SUPPORTED_THRESHOLDS
            },
            "effective": {
                spec.key: getattr(preview.effective.thresholds, spec.key)
                for spec in SUPPORTED_THRESHOLDS
            },
            "affected_constraint_count": preview.affected_constraint_count,
            "facets": [
                {
                    "rule": facet.rule,
                    "name": format_exception_name(facet.rule),
                    "count": facet.count,
                }
                for facet in preview.facets
            ],
        }
    return TEMPLATES.TemplateResponse(
        request,
        "operations_checks.html",
        {
            "project": project,
            "ruleset_version": effective.ruleset_version,
            "effective": effective,
            "supported": supported,
            "history": history,
            "error": error,
            "preview": preview_context,
            "saved_id": saved_id,
        },
        status_code=status_code,
    )


def _submitted_check_thresholds(form) -> dict[str, str]:
    """Only the supported threshold fields, as submitted (validation follows).

    A field the form omits is left out, so the domain refuses it as incomplete
    rather than this adapter inventing a value. Only supported keys are read,
    so an extra field cannot smuggle a value past the supported catalog.
    """
    submitted: dict[str, str] = {}
    for spec in SUPPORTED_THRESHOLDS:
        value = form.get(spec.key)
        if value is not None:
            submitted[spec.key] = value
    return submitted


@app.get("/operations/{slug}/checks", response_class=HTMLResponse)
def operations_checks(
    request: Request,
    slug: str,
    saved: int | None = None,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Inspect the supported thresholds and this project's effective checks."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    response = _render_operations_checks(
        request, session, project, saved_id=saved if saved and saved > 0 else None
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="operations_checks",
        route_template="/operations/{slug}/checks",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/checks/preview", response_class=HTMLResponse)
async def operations_checks_preview(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Show the reading under a proposed configuration, writing nothing."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    form = await request.form()
    submitted = _submitted_check_thresholds(form)
    try:
        preview = preview_configuration(session, project.id, submitted)
        error = None
        status_code = 200
    except InvalidCheckConfiguration as exc:
        preview = None
        error = str(exc)
        status_code = 400
    response = _render_operations_checks(
        request,
        session,
        project,
        preview=preview,
        error=error,
        submitted=submitted,
        status_code=status_code,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="operations_checks_preview",
        route_template="/operations/{slug}/checks/preview",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/checks", response_class=HTMLResponse)
async def save_operations_checks(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Append a new retained configuration, or refuse an invalid proposal."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    form = await request.form()
    submitted = _submitted_check_thresholds(form)
    try:
        row = save_configuration(
            session, project.id, submitted, principal=principal
        )
    except InvalidCheckConfiguration as exc:
        # A refusal writes no configuration; only the access receipt is
        # recorded, so the invalid attempt is attributable without activating.
        response = _render_operations_checks(
            request,
            session,
            project,
            error=str(exc),
            submitted=submitted,
            status_code=400,
        )
        record_frontend_request(
            session,
            principal=principal,
            route_name="save_operations_checks",
            route_template="/operations/{slug}/checks",
            method="POST",
            response=response,
            subject=FrontendRequestSubject(project_id=project.id),
            request_fields=form,
        )
        session.commit()
        return response
    response = RedirectResponse(
        f"/operations/{project.slug}/checks?saved={row.id}", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_operations_checks",
        route_template="/operations/{slug}/checks",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, check_configuration_id=row.id
        ),
        request_fields=form,
    )
    session.commit()
    return response


# --- Processing operations (#344) -----------------------------------------


def _operations_state_fingerprint(value: dict) -> str:
    """Bind an operations form to the exact facts its screen rendered.

    This is a stale-state guard, not an authorization token: every mutation
    still obtains its scope and authority server-side.  A canonical digest
    makes a changed declaration chain, completed-run set, proof, or permitted
    policy action refuse rather than silently applying an obsolete choice.
    """
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run_offer_state(session: Session, document_id: int) -> str:
    current = session.get(ActiveExtractionRun, document_id)
    tail = current_active_run_declaration(session, document_id)
    completed_ids = [
        run.id
        for run in session.scalars(
            select(ExtractionRun)
            .where(ExtractionRun.document_id == document_id)
            .order_by(ExtractionRun.id)
        )
        if is_completed_run(run)
    ]
    return _operations_state_fingerprint(
        {
            "document_id": document_id,
            "active_run_id": None if current is None else current.extraction_run_id,
            "declaration_id": None if tail is None else tail.id,
            "completed_run_ids": completed_ids,
        }
    )


def _policy_offer_state(status) -> str:
    return _operations_state_fingerprint(
        {
            "status": status.status,
            "proof_status": status.proof_status,
            "latest_receipt_id": status.latest_receipt_id,
            "latest_action_id": status.latest_action_id,
            "allowed_operations": list(status.allowed_operations),
        }
    )


def _operations_processing_context(session: Session, project: Project) -> dict:
    """Read the bounded operations facts without inventing a second authority.

    The screen joins existing records only: Active Run declarations own the
    chosen reading, Event Admission owns effective policy status, and Due Work
    owns recovery state.  Keeping this composite read here prevents an operator
    UI from reimplementing any of their safety decisions.
    """
    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project.id)
        .order_by(Document.doc_date, Document.id)
    ).all()
    quarantines = {
        row.document_id: row
        for row in session.scalars(
            select(DocumentQuarantine)
            .join(Document, Document.id == DocumentQuarantine.document_id)
            .where(Document.project_id == project.id)
        )
    }
    declared = {
        row.document_id: row
        for row in session.scalars(
            select(ActiveExtractionRun)
            .join(Document, Document.id == ActiveExtractionRun.document_id)
            .where(Document.project_id == project.id)
        )
    }
    history_by_document: dict[int, list[ActiveRunDeclaration]] = {}
    for row in session.scalars(
        select(ActiveRunDeclaration)
        .join(Document, Document.id == ActiveRunDeclaration.document_id)
        .where(Document.project_id == project.id)
        .order_by(ActiveRunDeclaration.document_id, ActiveRunDeclaration.id)
    ):
        history_by_document.setdefault(row.document_id, []).append(row)
    runs_by_document: dict[int, list[ExtractionRun]] = {}
    for row in session.scalars(
        select(ExtractionRun)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project.id)
        .order_by(ExtractionRun.document_id, ExtractionRun.id)
    ):
        runs_by_document.setdefault(row.document_id, []).append(row)

    document_rows = []
    for document in documents:
        current = declared.get(document.id)
        document_rows.append(
            {
                "document": document,
                "active_run_id": current.extraction_run_id if current else None,
                "runs": runs_by_document.get(document.id, []),
                "history": history_by_document.get(document.id, []),
                "quarantine": quarantines.get(document.id),
                "offer_state": _run_offer_state(session, document.id),
            }
        )

    schedules = session.scalars(
        select(DueWorkSchedule)
        .where(
            DueWorkSchedule.project_id == project.id,
            DueWorkSchedule.handler_key.in_(
                (HANDLER_PROJECT_PROCESSING, HANDLER_REVISION_RECONCILIATION)
            ),
            DueWorkSchedule.disabled_at.is_(None),
        )
        .order_by(DueWorkSchedule.handler_key, DueWorkSchedule.id.desc())
    ).all()
    schedule_ids = [schedule.id for schedule in schedules]
    recovery_rows = []
    if schedule_ids:
        receipts_by_occurrence: dict[int, list[DueWorkReceipt]] = {}
        for receipt in session.scalars(
            select(DueWorkReceipt)
            .where(DueWorkReceipt.project_id == project.id)
            .order_by(DueWorkReceipt.occurrence_id, DueWorkReceipt.attempt_number)
        ):
            receipts_by_occurrence.setdefault(receipt.occurrence_id, []).append(receipt)
        for occurrence in session.scalars(
            select(DueWorkOccurrence)
            .where(DueWorkOccurrence.scheduled_job_id.in_(schedule_ids))
            .order_by(DueWorkOccurrence.id.desc())
            .limit(50)
        ):
            recovery_rows.append(
                {
                    "occurrence": occurrence,
                    "receipts": receipts_by_occurrence.get(occurrence.id, []),
                }
            )

    handoff = session.get(RecordInclusionRequest, project.id)
    event_policy = read_event_admission_policy_status(session, project.id)
    return {
        "documents": document_rows,
        "event_policy": event_policy,
        "policy_offer_state": _policy_offer_state(event_policy),
        "record_inclusion_pending": (
            handoff is not None and handoff.dirty_seq > handoff.reconciled_seq
        ),
        "schedules": schedules,
        "recovery_rows": recovery_rows,
    }


def _render_processing_operations(
    request: Request, session: Session, project: Project, *, notice: str | None = None
):
    return TEMPLATES.TemplateResponse(
        request,
        "operations.html",
        {"project": project, "notice": notice, **_operations_processing_context(session, project)},
    )


@app.get("/operations/{slug}", response_class=HTMLResponse)
def processing_operations(
    request: Request,
    slug: str,
    notice: str | None = None,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Show only the existing, project-scoped technical operations."""
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    response = _render_processing_operations(request, session, project, notice=notice)
    record_frontend_request(
        session,
        principal=principal,
        route_name="processing_operations",
        route_template="/operations/{slug}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/runs/{document_id}/declare")
async def declare_operations_active_run(
    request: Request,
    slug: str,
    document_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Append one permitted Current Production Run declaration.

    The document, run, completed state, declaration chain, and the two durable
    downstream handoffs are all revalidated by ``declare_active_run`` while the
    project is locked.  Form values never name the operator.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document")
    form = await request.form()
    run_id = _required_positive_http_id(form, "extraction_run_id")
    offered_state = str(form.get("state_fingerprint") or "")
    try:
        lock_project(session, project.id)
        if offered_state != _run_offer_state(session, document.id):
            raise ValueError("the production-run choices changed; refresh first")
        declaration = declare_active_run(
            session, document.id, run_id, principal=principal
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = RedirectResponse(
        f"/operations/{project.slug}?notice=declared-{declaration.id}", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="declare_operations_active_run",
        route_template="/operations/{slug}/runs/{document_id}/declare",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/unknown-scope/suspend")
async def suspend_operations_unknown_scope(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    form = await request.form()
    reason = str(form.get("reason") or "")
    try:
        lock_project(session, project.id)
        status = read_event_admission_policy_status(session, project.id)
        if "suspend" not in status.allowed_operations:
            raise ValueError("unknown-scope Event Admission is not active")
        if str(form.get("state_fingerprint") or "") != _policy_offer_state(status):
            raise ValueError("the policy status changed; refresh first")
        suspend_unknown_scope_admission(
            session,
            project_id=project.id,
            reason=reason,
            recorded_by=principal.subject,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = RedirectResponse(f"/operations/{project.slug}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="suspend_operations_unknown_scope",
        route_template="/operations/{slug}/unknown-scope/suspend",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/unknown-scope/lift")
async def lift_operations_unknown_scope(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    form = await request.form()
    try:
        lock_project(session, project.id)
        status = read_event_admission_policy_status(session, project.id)
        if "lift" not in status.allowed_operations:
            raise ValueError("unknown-scope Event Admission is not suspended")
        if str(form.get("state_fingerprint") or "") != _policy_offer_state(status):
            raise ValueError("the policy status changed; refresh first")
        lift_unknown_scope_admission(
            session, project_id=project.id, recorded_by=principal.subject
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response = RedirectResponse(f"/operations/{project.slug}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="lift_operations_unknown_scope",
        route_template="/operations/{slug}/unknown-scope/lift",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.get("/work/{slug}", response_class=HTMLResponse)
def coordinator_home(
    request: Request,
    slug: str,
    work_search: str = "",
    work_page: int = 1,
    statement_search: str = "",
    statement_page: int = 1,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The coordinator's short, project-language entry point."""
    project = _project(session, slug, principal)
    work_list = build_work_list(
        session,
        project.id,
        backlog_search=work_search,
        backlog_page=work_page,
        candidate_search=statement_search,
        candidate_page=statement_page,
    )

    def view(item):
        if item.kind == "candidate":
            if item.candidate_kind == "dependency":
                action_url = (
                    f"/queue/{project.slug}?lane=candidate&mode=review"
                    f"&candidate_id={item.candidate_id}"
                )
                action_label = "Review proposed constraint"
            else:
                action_url = (
                    f"/statements/{project.slug}/{item.candidate_id}/coordinate"
                )
                action_label = "Review proposed statement"
        elif item.kind == "dependency":
            action_url = f"/ledger/{project.slug}/{item.dependency_id}"
            action_label = "Open constraint"
        elif item.source_candidate_id is not None:
            action_url = (
                f"/statements/{project.slug}/{item.source_candidate_id}/coordinate"
            )
            action_label = "Open statement plan"
        else:
            action_url = f"/statements/{project.slug}"
            action_label = "Review statement"
        return {
            "item": item,
            "reasons": tuple(_WORK_REASON_COPY[code] for code in item.attention_reason_codes),
            "action_url": action_url,
            "action_label": action_label,
        }

    response = TEMPLATES.TemplateResponse(
        request,
        "work_list.html",
        {
            "project": project,
            "immediate": tuple(view(item) for item in work_list.immediate),
            "backlog": tuple(view(item) for item in work_list.backlog),
            "backlog_total": work_list.backlog_total,
            "backlog_page": work_list.backlog_page,
            "backlog_pages": work_list.backlog_pages,
            "backlog_search": work_list.backlog_search,
            "candidate_backlog": tuple(
                view(item) for item in work_list.candidate_backlog
            ),
            "candidate_backlog_total": work_list.candidate_backlog_total,
            "candidate_backlog_page": work_list.candidate_backlog_page,
            "candidate_backlog_pages": work_list.candidate_backlog_pages,
            "candidate_search": work_list.candidate_search,
            "event_candidate_total": work_list.event_candidate_total,
            "dependency_candidate_total": work_list.dependency_candidate_total,
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="coordinator_home",
        route_template="/work/{slug}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


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
    summary: int = 0,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug, principal)
    cohort_receipt = None
    if lane == "rehearsal":
        if cohort_receipt_id is None:
            raise HTTPException(400, "test coordination requires its input manifest")
        cohort_receipt = session.get(CohortReceipt, cohort_receipt_id)
        if cohort_receipt is None or cohort_receipt.project_id != project.id:
            raise HTTPException(404, "no such test input manifest in this project")
    event_cohort_receipt = None
    if lane == "events":
        if event_cohort_receipt_id is None:
            raise HTTPException(
                400, "statement test coordination requires its input manifest"
            )
        event_cohort_receipt = session.get(
            EventCohortReceipt, event_cohort_receipt_id
        )
        if (
            event_cohort_receipt is None
            or event_cohort_receipt.project_id != project.id
        ):
            raise HTTPException(
                404, "no such statement test input manifest in this project"
            )
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
    summary_url = _cohort_summary_location(lane_url)

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

    rail = (
        build_cohort_rail(
            session,
            cohort_receipt,
            candidate.id if candidate is not None else None,
        )
        if cohort_receipt is not None
        else None
    )

    coordinate_dependency = None
    openable_dependency_ids = frozenset(cohort_openable_dependency_ids(rail))
    if coordinate is not None and (
        cohort_receipt is not None or event_cohort_receipt is not None
    ):
        coordinate_dependency = session.get(Dependency, coordinate)
        if (
            coordinate_dependency is None
            or coordinate_dependency.project_id != project.id
            or (
                cohort_receipt is not None
                and coordinate_dependency.id not in openable_dependency_ids
            )
        ):
            coordinate_dependency = None
    if (
        candidate is None
        and coordinate_dependency is None
        and cohort_receipt is not None
        and coordinate is None
        and not summary
    ):
        default_coordinate_id = default_cohort_dependency_id(rail)
        if default_coordinate_id is not None:
            coordinate_dependency = session.get(Dependency, default_coordinate_id)
    next_coordinate_url = (
        lane_url
        if candidate is not None
        else _next_cohort_coordinate_url(
            lane_url,
            rail,
            coordinate_dependency.id if coordinate_dependency is not None else None,
        )
    )

    if candidate is None and coordinate_dependency is None:
        return TEMPLATES.TemplateResponse(
            request,
            "empty.html",
            {
                **lane_context,
                "rail": rail,
                "lane_url": lane_url,
                "summary_url": summary_url,
                "next_coordinate_url": next_coordinate_url,
            },
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
                "summary_url": summary_url,
                "next_coordinate_url": next_coordinate_url,
            },
        )

    if candidate.state == "pending" and observe_shadow_review(
        session, candidate.id, boundary="start", principal=principal
    ) is not None:
        pass

    reason = _review_reason(session, project, candidate)
    headline, guidance = REVIEW_REASONS.get(reason or "", (None, None))
    candidate_authority_gap = pending_candidate_authority_gap(
        session,
        project.id,
        candidate.id,
    )
    unresolved_acknowledgments = tuple(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == audit.CANDIDATE,
                AuditLog.entity_id == candidate.id,
                AuditLog.action == audit.KEEP_CANDIDATE_UNRESOLVED,
            )
            .order_by(AuditLog.id)
        ).all()
    )
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

    response = TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "project": project,
            "review_headline": headline,
            "review_guidance": guidance,
            "candidate_authority_gap": candidate_authority_gap,
            "unresolved_acknowledgments": unresolved_acknowledgments,
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
            "summary_url": summary_url,
            "next_coordinate_url": next_coordinate_url,
            **lane_context,
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="queue",
        route_template="/queue/{slug}",
        method="GET",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=(
                coordinate_dependency.id
                if coordinate_dependency is not None
                else None
            ),
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


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
    predecessor_document_id = _required_positive_http_id(
        form, "predecessor_document_id"
    )
    successor_candidate_id = _required_positive_http_id(
        form, "successor_candidate_id"
    )
    comparison_id = _required_positive_http_id(form, "comparison_id")
    finding_id = _required_positive_http_id(form, "finding_id")
    scope_fingerprint = _required_scope_fingerprint(form)

    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    org_id: int | None = None,
    resolution_strategy: str | None = None,
    ready: str | None = None,
    rule: str | None = None,
    owner: str | None = None,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug, principal)
    # This page is its own publication, so it takes its own evaluation —
    # stated here rather than defaulted inside `browse`, where a caller who
    # already held one could silently pay for a second. It reads under the
    # project's effective declared check thresholds like every other reader.
    rows = browse(
        session,
        project.id,
        evaluation=evaluate_project(
            session,
            project.id,
            thresholds=effective_thresholds(session, project.id),
        ),
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
                "org_id": org_id or "",
                "resolution_strategy": resolution_strategy or "",
                "ready": ready or "",
                "rule": rule or "",
                "owner": owner or "",
            },
            "rules": [
                (rule, format_exception_name(rule)) for rule in sorted(RULES)
            ],
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
    return_to: str = "",
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    project = _project(session, slug, principal)
    try:
        view = load_dependency(session, dependency_id)
    except LookupError:
        raise HTTPException(404, "no such constraint")
    if view.dependency.project_id != project.id:
        raise HTTPException(404, "no such constraint in this project")
    safe_return = (
        _safe_cohort_return(
            return_to,
            project=project,
            dependency_id=dependency_id,
            session=session,
        )
        if return_to
        else ""
    )
    owner_decision = current_internal_owner_decision(session, dependency_id)
    action_decision = current_next_action_decision(session, dependency_id)
    support = resolve_operative_support(session, (dependency_id,))[dependency_id]
    return TEMPLATES.TemplateResponse(
        request,
        "dependency.html",
        {
            "project": project,
            "view": view,
            "owner_decision": owner_decision,
            "action_decision": action_decision,
            "sufficient_evidence_ids": {
                item.evidence_link_id for item in support.readiness
            },
            "return_to": safe_return,
            "disputes": {
                d.field_name: d
                for d in disputes_for(
                    session, dependency_id, include_settled=True
                )
            },
            "dismiss_reasons": DISMISS_REASONS,
        },
    )


@app.post("/ledger/{slug}/{dependency_id}/verbal")
def record_dependency_verbal(
    slug: str,
    dependency_id: int,
    stated_party: str = Form(...),
    description: str = Form(...),
    conversation_date: date = Form(...),
    committed_date: date = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record one attributable phone statement from this Dependency's page."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = _project_dependency(session, project, dependency_id)
    try:
        record_verbal(
            session,
            dependency,
            stated_party=stated_party,
            description=description,
            conversation_date=conversation_date,
            committed_date=committed_date,
            principal=principal,
        )
    except VerbalRefusal as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/ledger/{slug}/{dependency_id}", status_code=303
    )


@app.post("/ledger/{slug}/{dependency_id}/settle")
def settle(
    slug: str,
    dependency_id: int,
    field_name: str = Form(...),
    value: str = Form(""),
    saw_claim_id: int | None = Form(None),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Say what the record concludes for one disputed field.

    The claims stay where they are; this records the judgment beside
    them (ADR-0031). A later revision disagreeing again reopens the
    Dispute without anyone reopening it.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None or dependency.project_id != project.id:
        raise HTTPException(404, "no such constraint in this project")
    try:
        settle_dispute(
            session,
            dependency_id,
            field_name,
            value=value.strip() or None,
            principal=principal,
            saw_claim_id=saw_claim_id,
        )
    except (NoSuchDispute, DisputeMovedOn) as exc:
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
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    try:
        decision = assign_internal_owner(
            session, dependency_id, owner, principal=principal
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    response = RedirectResponse(return_location, status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="assign_owner",
        route_template="/dependencies/{dependency_id}/owner",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
            work_decision_id=decision.id,
        ),
        request_fields={
            "slug": slug,
            "owner": owner,
            "redirect_to": redirect_to,
        },
    )
    session.commit()
    return response


@app.post("/dependencies/{dependency_id}/action")
def record_next_action(
    dependency_id: int,
    slug: str = Form(...),
    action: str = Form(...),
    due_date: str = Form(""),
    due_date_unknown_reason: str = Form(""),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One submit, one Work Decision: the action and its date together."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    parsed = None
    if due_date.strip():
        try:
            parsed = date.fromisoformat(due_date.strip())
        except ValueError:
            raise HTTPException(400, "an Action Due Date must be a date")
    try:
        decision = set_next_action(
            session,
            dependency_id,
            action,
            due_date=parsed,
            due_date_unknown_reason=due_date_unknown_reason.strip() or None,
            principal=principal,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    response = RedirectResponse(return_location, status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="record_next_action",
        route_template="/dependencies/{dependency_id}/action",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
            work_decision_id=decision.id,
        ),
        request_fields={
            "slug": slug,
            "action": action,
            "due_date": due_date,
            "due_date_unknown_reason": due_date_unknown_reason,
            "redirect_to": redirect_to,
        },
    )
    session.commit()
    return response


@app.post("/dependencies/{dependency_id}/action/{outcome}")
def close_next_action(
    dependency_id: int,
    outcome: Literal["complete", "cancel"],
    slug: str = Form(...),
    no_follow_up_reason: str = Form(""),
    cancellation_reason: str = Form(""),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Completion and cancellation are distinct decisions, never one button."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    try:
        if outcome == "complete":
            complete_next_action(
                session,
                dependency_id,
                no_follow_up_reason=no_follow_up_reason.strip() or None,
                principal=principal,
            )
        else:
            cancel_next_action(
                session,
                dependency_id,
                no_follow_up_reason=no_follow_up_reason.strip() or None,
                cancellation_reason=cancellation_reason.strip() or None,
                principal=principal,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.commit()
    return RedirectResponse(return_location, status_code=303)


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
    project = _project(session, slug, principal, designation=access.DOCUMENTATION_REVIEW)
    dependency = _project_dependency(session, project, dependency_id)
    _project_evidence(session, dependency, link_id)
    try:
        mark_satisfies(session, dependency_id, link_id, principal=principal)
    except NoSuchEvidence as exc:
        raise HTTPException(404, str(exc))
    except UnverifiedEvidence as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.get("/page-image/{document_id}/{page_no}")
def page_image(
    document_id: int,
    page_no: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Serve a source page image only to a member of its project (#331).

    The image is addressed by document id, so a guessed id must not leak another
    project's source. The owning project is resolved from the document and the
    membership gate applied before any file is read.
    """
    project = _project_for_document(session, document_id)
    _authorize(session, principal, project)
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document_id, DocPage.page_no == page_no
        )
    ).first()
    if page is None or not page.image_path or not Path(page.image_path).exists():
        raise HTTPException(404, "no rendered image for that page")
    return FileResponse(page.image_path, media_type="image/png")


@app.post("/candidates/{candidate_id}/keep-unresolved")
def keep_unresolved_candidate(
    candidate_id: int,
    slug: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Acknowledge the latest structured gap and retain the pending proposal."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_candidate(session, project, candidate_id)
    try:
        keep_candidate_unresolved(
            session,
            project.id,
            candidate.id,
            principal=principal,
        )
    except (StatementCoordinationRefusal, StaleStatementCoordination) as exc:
        raise HTTPException(409, str(exc)) from exc
    response = RedirectResponse(
        f"/queue/{project.slug}?lane=candidate&mode=review"
        f"&candidate_id={candidate.id}",
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="keep_unresolved_candidate",
        route_template="/candidates/{candidate_id}/keep-unresolved",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields={"slug": slug},
    )
    session.commit()
    return response


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
    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    aliases = party_canonical_names(session)
    try:
        check_sibling_request(
            candidate,
            merge_sibling_ids,
            in_event_lane=event_cohort_receipt_id is not None,
            schemes=schemes,
            aliases=aliases,
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
        check_sibling_set(candidate, siblings, schemes, aliases)
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
    response = RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="accept",
        route_template="/candidates/{candidate_id}/accept",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=dependency.id,
        ),
        request_fields={
            "slug": slug,
            "historical_document_id": historical_document_id,
            "cohort_receipt_id": cohort_receipt_id,
            "event_cohort_receipt_id": event_cohort_receipt_id,
            "merge_sibling_ids": merge_sibling_ids,
        },
    )
    session.commit()
    return response


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
    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    response = RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="edit_accept",
        route_template="/candidates/{candidate_id}/edit-accept",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=dependency.id,
        ),
        request_fields=form,
    )
    session.commit()
    return response


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
    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    except AlreadyDismissed as exc:
        raise HTTPException(409, str(exc))
    except InvalidCandidateScope as exc:
        raise HTTPException(409, str(exc))
    except InvalidCandidateProvenance as exc:
        raise HTTPException(400, str(exc))
    response = RedirectResponse(
        _decision_location(
            slug,
            historical_document_id,
            cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            coordinate_dependency_id=dependency.id,
        ),
        status_code=303,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="merge",
        route_template="/candidates/{candidate_id}/merge",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=candidate.id,
            dependency_id=dependency.id,
        ),
        request_fields={
            "slug": slug,
            "dependency_id": dependency_id,
            "historical_document_id": historical_document_id,
            "cohort_receipt_id": cohort_receipt_id,
            "event_cohort_receipt_id": event_cohort_receipt_id,
        },
    )
    session.commit()
    return response


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
    project = _project(session, slug, principal, designation=access.COORDINATION)
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
    response = RedirectResponse(
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
    record_frontend_request(
        session,
        principal=principal,
        route_name="reject",
        route_template="/candidates/{candidate_id}/reject",
        method="POST",
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, candidate_id=candidate.id
        ),
        request_fields={
            "slug": slug,
            "reason": reason,
            "historical_document_id": historical_document_id,
            "cohort_receipt_id": cohort_receipt_id,
            "event_cohort_receipt_id": event_cohort_receipt_id,
            "redirect_to": redirect_to,
        },
    )
    session.commit()
    return response


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


def _required_positive_http_id(form, name: str) -> int:
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
        raise HTTPException(404, "no such constraint")
    return dependency


def _project_evidence(
    session: Session, dependency: Dependency, link_id: int
) -> EvidenceLink:
    link = session.get(EvidenceLink, link_id)
    scoped_event = link is not None and current_statement_evidence_memberships(
        session, (dependency.id,)
    ).contains(dependency.id, link_id)
    if link is None or (link.dependency_id != dependency.id and not scoped_event):
        raise HTTPException(404, "no such cited passage")
    return link


# --- Source document intake (#349) -------------------------------------------
# The rare upload fallback (ADR-0058). A person hands Corridor one source file,
# sees read-only what registering it would do, and confirms it attributably. The
# bounded limits, preview, and durable handoff live in `corridor.source_intake`
# so the email front door (#372) reuses them; these routes only carry the HTTP.

_DOC_TYPE_CHOICES = tuple(sorted(ACCEPTED_DOC_TYPES))


@app.get("/projects/{slug}/sources/upload", response_class=HTMLResponse)
def source_upload_form(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Offer the upload fallback: no filesystem path, no command."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    return TEMPLATES.TemplateResponse(
        request,
        "source_upload.html",
        {
            "project": project,
            "doc_types": _DOC_TYPE_CHOICES,
            "max_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
            "error": None,
            "selected_doc_type": None,
        },
    )


@app.post("/projects/{slug}/sources/upload", response_class=HTMLResponse)
def source_upload_preview(
    request: Request,
    slug: str,
    doc_type: str = Form(...),
    upload: UploadFile = File(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Stage the bytes and show what confirming would create or change."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    # Read one byte past the limit so an unbounded upload is refused without
    # buffering all of it; the shared validator re-checks the true bound.
    body = upload.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        staged = validate_and_stage(body, upload.filename or "")
        preview = preview_intake(session, project, staged, doc_type)
    except IntakeRefused as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "source_upload.html",
            {
                "project": project,
                "doc_types": _DOC_TYPE_CHOICES,
                "max_mib": MAX_UPLOAD_BYTES // (1024 * 1024),
                "error": str(exc),
                "selected_doc_type": doc_type,
            },
            status_code=400,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "source_preview.html",
        {"project": project, "preview": preview},
    )


@app.post("/projects/{slug}/sources/confirm")
def source_confirm(
    slug: str,
    sha256: str = Form(...),
    filename: str = Form(...),
    doc_type: str = Form(...),
    binding_fingerprint: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Bind the previewed source to the acting person and register it."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        confirm_intake(
            session,
            project=project,
            sha256=sha256,
            filename=filename,
            doc_type=doc_type,
            binding_fingerprint=binding_fingerprint,
            principal=principal,
        )
    except IntakeRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    except IntakeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(f"/projects/{slug}/sources", status_code=303)


@app.get("/projects/{slug}/sources", response_class=HTMLResponse)
def source_uploads(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The confirmed uploads and their honest processing outcomes."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    return TEMPLATES.TemplateResponse(
        request,
        "source_uploads.html",
        {
            "project": project,
            "uploads": list_confirmed_uploads(session, project.id),
            "status_labels": _UPLOAD_STATUS_LABELS,
        },
    )


_UPLOAD_STATUS_LABELS = {
    "pending": "Pending — waiting for the processing pass",
    "processed": "Processed",
    "unreadable": "Unreadable — the reader could not use it",
    "parse_failed": "Failed to parse — the file could not be read",
    "processing_failed": "Processing failed — a later pass will retry",
    "held_unmodeled": "Held — its content is deliberately not read",
}


@app.post("/intake/inbound")
async def receive_inbound_mail(
    request: Request,
    inbound_token: str | None = Header(default=None, alias="X-Corridor-Inbound-Token"),
    session: Session = Depends(get_session),
):
    """Server-to-server receipt boundary for the one configured intake address.

    This is not an ordinary customer route: it refuses unless deployment supplied
    both server-owned configuration values and the sender authentication secret.
    It does not accept a project id, actor, or route from a client.
    """
    if (
        not settings.inbound_service_address
        or not settings.inbound_webhook_secret
        or inbound_token is None
        or not secrets.compare_digest(inbound_token, settings.inbound_webhook_secret)
    ):
        raise HTTPException(401, "inbound sender is not authorized")
    try:
        received = email_intake.receive_message(
            session,
            raw_bytes=await request.body(),
            service_address=settings.inbound_service_address,
        )
    except email_intake.InboundMailRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return {
        "message_id": received.message_id,
        "thread_id": received.thread_id,
        "project_id": received.project_id,
        "route_status": received.route_status,
        "created": received.created,
    }


@app.get("/projects/{slug}/inbound")
def inbound_mail_readback(
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read a member's project's retained inbound-message receipt metadata."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    messages = session.scalars(
        select(InboundMessage)
        .where(InboundMessage.project_id == project.id)
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
    ).all()
    return {
        "project": project.slug,
        "messages": [
            {
                "id": message.id,
                "thread_id": message.thread_id,
                "sender": message.sender,
                "subject": message.subject,
                "route_evidence": message.route_evidence_json,
                "document_id": message.document_id,
                "attachments": message.attachments_json,
                "received_at": message.received_at.isoformat(),
            }
            for message in messages
        ],
    }


@app.post("/projects/{slug}/inbound/{thread_id}/route")
def resolve_inbound_route(
    slug: str,
    thread_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Attribute the one pending routing choice to an authorized project member."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    try:
        email_intake.resolve_route_triage(
            session,
            thread_id=thread_id,
            project_id=project.id,
            principal=principal,
        )
    except email_intake.InboundRouteConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"thread_id": thread_id, "project": project.slug, "route_status": "routed"}
