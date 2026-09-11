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

import logging
from dataclasses import replace
import json
import secrets
from datetime import date, datetime, time, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Callable, Literal
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
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

from corridor import refusals
from corridor import email_intake
from corridor import notifications
from corridor import push_intake
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
    OrganizationIdentityUnresolved,
    REJECT_REASONS,
    UnadjudicableKind,
    accept_candidate,
    edit_candidate,
    merge_candidate,
    reject_candidate,
)
from corridor.db import WebSession, WorkerSession
from corridor.web.customer_routing import (
    customer_session, set_customer_cookie, clear_customer_cookie,
    needs_customer_sign_in, clear_invalid_customer_cookies,
)
from corridor.object_storage import ObjectStore, StorageError, content_store
from corridor.operational_health import ComponentHealth, runtime_report, serving_report
from corridor import telemetry
from corridor.telemetry import (
    ROLE_WEB,
    RequestCorrelationMiddleware,
    configure_logging,
)
from corridor import access
from corridor import web_boundary
from corridor.web.artifact_downloads import (
    CANDIDATE_DOWNLOAD,
    ISSUE_DOWNLOAD,
    bundle_bytes,
    download_name,
    download_response,
)
from corridor.web import auth
from corridor.web import ui_primitives
from corridor.check_configuration import (
    SUPPORTED_THRESHOLDS,
    InvalidCheckConfiguration,
    configuration_history,
    effective_configuration,
    effective_thresholds,
    preview_configuration,
    save_configuration,
)
from corridor.constraint_reading import available
from corridor.exceptions import RULES, evaluate_project, format_exception_name
from corridor.export import to_xlsx
from corridor.report import build_report, render
from corridor.lane import (
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
from corridor.condition_tracking import (
    clear_condition,
    dismiss_condition,
    propose_condition_clears,
)
from corridor.documentation_checklist import (
    confirm_interpretation,
    read_checklist,
    record_documentation_clarification,
)
from corridor.models import (
    BaselineSourceRow,
    ProposedDelta,
    RESOLUTION_STRATEGIES,
    AssignmentNotification,
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
    FollowUpPlanReceipt,
    Milestone,
    MilestoneRegistration,
    Project,
    StatementCoordinationReceipt,
    CoordinationSummaryRequest,
    ProductionRunExplanationConfiguration,
    ProductionRunExplanationRequest,
    ExtractionFailureDiagnosisRequest,
    RevisionChangeExplanationRequest,
    ReleaseCandidate,
    ReleasePackage,
    SourceIntakeDraftRequest,
)
from corridor.coordination_summary import (
    ConfigurationRequired,
    InvalidSummaryConfiguration,
    current_configuration as current_summary_configuration,
    declare_configuration as declare_summary_configuration,
    request_summary as request_coordination_summary,
)
from corridor.production_run_explanation import (
    PROMPT_VERSION as RUN_EXPLANATION_PROMPT_VERSION,
    ConfigurationRequired as RunExplanationConfigurationRequired,
    ExplanationRequestRefused,
    InvalidExplanationConfiguration,
    competing_production_runs,
    competing_runs_state_token,
    current_configuration as current_run_explanation_configuration,
    declare_configuration as declare_run_explanation_configuration,
    request_run_explanation,
)
from corridor.extraction_failure_diagnosis import (
    PROMPT_VERSION as FAILURE_DIAGNOSIS_PROMPT_VERSION,
    ConfigurationRequired as FailureDiagnosisConfigurationRequired,
    FailureDiagnosisRefused,
    InvalidDiagnosisConfiguration,
    current_configuration as current_failure_diagnosis_configuration,
    declare_configuration as declare_failure_diagnosis_configuration,
    failed_extraction_runs,
    failure_diagnosis_state_token,
    request_failure_diagnosis,
)
from corridor.revision_change_explanation import (
    PROMPT_VERSION as REVISION_CHANGE_EXPLANATION_PROMPT_VERSION,
    ConfigurationRequired as RevisionChangeConfigurationRequired,
    ExplanationRequestRefused as RevisionChangeExplanationRequestRefused,
    InvalidExplanationConfiguration as RevisionChangeInvalidConfiguration,
    current_configuration as current_revision_change_configuration,
    declare_configuration as declare_revision_change_configuration,
    explanation_binding as revision_change_explanation_binding,
    latest_revision_change_explanation,
    request_revision_change_explanation,
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
from corridor.web.follow_up_view import chase_view
from corridor.web.issue_section import artifact_words, issue_view
from corridor.analytics import EventFamily
from corridor.measurement_collection import binding_for_session, emit_presentation
from corridor.web.dependency_view import (
    NoSuchConstraint,
    active_project_roster,
    dependency_view,
)
from corridor.web.operations_view import operations_view, policy_offer_state
from corridor.web.queue_view import (
    LaneManifestRequired,
    NoSuchLaneManifest,
    REVIEW_REASONS,
    ReturnContextRefused,
    queue_view,
    safe_cohort_return,
)
from corridor.disputes import (
    disputes_for,
    history_assessments_for,
    record_dispute_clarification,
    settle_dispute,
)
from corridor.dispute_timeline import build_dispute_timeline
from corridor.event_admission import waiting_statements
from corridor.frontend_request_receipts import (
    FrontendRequestSubject,
    record_frontend_request,
)
from corridor.verbal import (
    correct_verbal_scope,
    record_verbal_change,
    record_verbal_statement,
)
from corridor.external_statements import StatementScope, StatementTiming
from corridor.identity import document_numbering_schemes, party_canonical_names
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.source_intake import (
    ACCEPTED_DOC_TYPES,
    MAX_UPLOAD_BYTES,
    IntakeRefused,
    UploadNotTaken,
    confirm_intake,
    list_confirmed_uploads,
    preview_intake,
    receive_upload,
)
from corridor.source_intake_draft import (
    PROMPT_VERSION as INTAKE_DRAFT_PROMPT_VERSION,
    ConfigurationRequired as IntakeDraftConfigurationRequired,
    IntakeDraftRefused,
    InvalidDraftConfiguration,
    StagedDraftSource,
    current_configuration as current_intake_draft_configuration,
    declare_configuration as declare_intake_draft_configuration,
    intake_draft_state_token,
    request_intake_draft,
)
from corridor.render_profiles import render_path_for_page
from corridor.source_passage_view import (
    SourcePassageNotFound,
    log_source_bytes_retrieval_failure,
    read_source_passage,
)
from corridor.storage import staged_file, stored_file
from corridor.locator_validation import (
    evidence_link_locator_validation_status as locator_validation_status,
    evidence_link_verified,
)
from corridor.presentation import (
    attention_reason_sentence,
    documentation_review_label,
    documentation_state_label,
    field_label,
    input_reference_label,
    label,
    provenance_label,
    resolution_strategy_label,
    source_passage_check_label,
    statement_type_label,
)
from corridor.report_release import (
    NoSuchReleasedReport,
    external_report_release_history,
    render_and_prepare_external_report,
    review_prepared_external_report,
    release_external_report,
    retrieve_prepared_external_report,
    retrieve_released_external_report,
)
from corridor.report_publication import project_publication_history
from corridor.cohort import (
    cohort_candidate_ids,
    event_cohort_candidate_ids,
    require_cohort_member,
    require_event_cohort_member,
)
from corridor.models import CohortReceipt, EventCohortReceipt
from corridor.operative_support import resolve_operative_support
from corridor.organization_identity import (
    OrganizationIdentityRefusal,
    confirm_cited_stated_alias,
    confirm_identity,
    resolve_candidate_identity,
)
from corridor.work_decisions import (
    FOLLOW_UP_NEXT_ACTION_CHOICES,
    CoordinationSubject,
    ExpectedNextAction,
    FollowUpPlanDraft,
    FollowUpPlanPredecessors,
    StaleFollowUpPlan,
    StaleNextAction,
    cancel_next_action,
    complete_next_action,
    current_deferral_decision,
    current_follow_up_plan_receipt,
    current_internal_owner_decision,
    current_next_action_decision,
    defer_work,
    save_follow_up_plan,
    undo_follow_up_plan,
)
from corridor.supersession_review import (
    build_reviewer_worklist,
    ordinary_candidate_for_update,
)
from corridor.support_update_routing import (
    changed_source_context,
    classify_review,
    customer_consequences_by_dependency,
    operations_consequences,
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
    ReleasePreparationRequest,
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
    CLOSURE_TARGET_GAP,
    StaleStatementCoordination,
    StatementCoordinationRefusal,
    StatementScopeCorrection,
    assign_admitted_statement_owner,
    cancel_admitted_statement_next_action,
    complete_admitted_statement_next_action,
    coordinate_statement,
    defer_admitted_statement,
    correct_statement_facts,
    correct_statement_scope,
    keep_candidate_unresolved,
    keep_statement_unresolved,
    mark_statement_not_relevant,
    pending_candidate_authority_gap,
    read_admitted_statement_coordination,
    restore_statement_not_relevant,
    set_admitted_statement_next_action,
    undo_statement_coordination,
)
from corridor.statement_lifecycle import (
    current_lineage_statement,
)
from corridor.evidence_investigator_shadow import observe_shadow_review
from corridor.consequence_levels import MUST_HANDLE
from corridor.packet_review import (
    LEAVE_OPEN,
    FocusedAnswer,
    ReviewScreenRefused,
    emit_packet_opening,
    emit_packet_surfacing,
    focused_request,
    packet_request,
    read_review_items,
    screen_binding,
    select_children,
)
from corridor.pilot_observations import (
    JUDGMENT_CHOICES,
    TriageOccurrence,
    collect_triage_observations,
    collection_is_pinned,
    judgment_from_form,
    minutes_from_form,
)
from corridor.operating_mode import is_adopted_baseline
from corridor.project_portfolio import (
    PORTFOLIO,
    derive_standings,
    emit_portfolio_reading,
    emit_project_selection,
    read_portfolio,
)
from corridor.follow_up_bundles import (
    emit_follow_up_reading,
    read_follow_up_bundles,
)
from corridor.issue_coverage import CoverageRefused, confirm_coverage, derive_coverage_reading
from corridor.issue_profile import effective_issue_inventory
from corridor.project_workflow import read_project_workflow
from corridor.release_preparation import (
    PreparationRequestRefused,
    preparation_idempotency_key,
    request_preparation,
)
from corridor.release_authorization import (
    NOT_DESIGNATED,
    Authorization,
    AuthorizationRefused,
    authorize_release_package,
    retrieve_candidate_artifact,
    retrieve_released_artifact,
    retrieve_released_package,
)
from corridor.record_history import (
    UnknownRevision,
    read_record_history,
    readable_terms,
)
from corridor.review_packet_reading import SHARED_COMMITMENT
from corridor.review_packets import (
    APPLY,
    DEFER,
    EDIT_AND_APPLY,
    KEEP_CURRENT,
    NEEDS_COORDINATION,
    REVERSED,
    resolve_review_packet,
    reverse_review_packet,
)
from corridor.work_list import build_work_list
from corridor.web.packet_receipt import read_packet_receipt, refusal_words
from corridor.web.statement_forms import (
    optional_form_date,
    required_positive_form_id,
    statement_coordination_draft,
    statement_fact_correction_draft,
    statement_scope_from_form,
)
from corridor.web.statement_view import (
    active_statement_coordination_receipt,
    read_statement_coordination,
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
    # The words one configured artifact goes by, so the Issue section and the
    # Record view's package history name the same file the same way (#830).
    artifact_words=artifact_words,
    documentation_review_label=documentation_review_label,
    documentation_state_label=documentation_state_label,
    input_reference_label=input_reference_label,
    provenance_label=provenance_label,
    resolution_strategy_label=resolution_strategy_label,
    statement_type_label=statement_type_label,
    # The Source Passage Check, as its own three steps: the mechanical status
    # of one locator, whether that status is the passed one, then the customer
    # word for the state (ADR-0082). A screen never reads the retired
    # `verified` column itself, and never re-derives the passed predicate as
    # `== 'valid'`: `locator_validation` is the one place that spells it.
    locator_validation_status=locator_validation_status,
    evidence_link_verified=evidence_link_verified,
    source_passage_check_label=source_passage_check_label,
    # Whether a Constraint reading's field is a value at all, so a screen can
    # print the reading's stated reason instead of an em dash that reads as an
    # emptiness the population never claimed (``constraint_reading``).
    available=available,
    csrf_field=_csrf_field,
)
# The shared accessibility primitives (#559): state and consequence words, the
# before-and-after reading, and the one focus target per response.
ui_primitives.register(TEMPLATES.env)
app = FastAPI(title="Corridor — coordination records")
# Configured where the process is defined rather than in an entry point, so
# every way this application is served — uvicorn, a test client, a smoke
# test — produces the same structured stream (#491A).
configure_logging(role=ROLE_WEB)
app.add_middleware(RequestCorrelationMiddleware)


# --- What one refusal kind means over HTTP, in one place -------------------
#
# `corridor.refusals` gives every family one `refusal_kind`; this is the only
# place that turns a kind into a status. It replaces the hand-picked status at
# each catch site: 99 `try` blocks and 176 `raise HTTPException` had drifted
# far enough apart that two sites for the same family disagreed by accident,
# and five of them forwarded `str(exc)` from a bare `except ValueError`, so an
# incidental parse error inside a domain call reached a browser as a 409
# carrying an internal message.
#
# A handler now catches a refusal only when it has something extra to do with
# it -- re-render the screen with the newer state (ADR-0039), bind the sentence
# to the control that holds it, or keep the coordinator's typing. A refusal it
# would only have restated as a status is left to travel here.
REFUSAL_STATUS: dict[str, int] = {
    # Three kinds are a conflict over the same resource, and all answer 409:
    # what the screen showed has moved on, the control was never offered in
    # this state, or the act contradicts the record. They stay distinct kinds
    # because a screen that re-renders itself acts on the difference.
    refusals.STALE: 409,
    refusals.NOT_OFFERED: 409,
    refusals.CONFLICT: 409,
    refusals.MALFORMED_INPUT: 400,
    refusals.NOT_AUTHORIZED: 403,
}


def refusal_status(refusal: refusals.Refusal) -> int:
    """The one status this refusal's kind answers."""

    return REFUSAL_STATUS[refusal.refusal_kind]


def refusal_response(refusal: refusals.Refusal) -> JSONResponse:
    """Answer a refusal with the sentence it wrote and the status its kind says.

    The same body shape `HTTPException` produced, so a caller reading
    ``{"detail": ...}`` is unaffected by which side answered.
    """

    return JSONResponse(
        {"detail": refusal.customer_sentence}, status_code=refusal_status(refusal)
    )


@app.exception_handler(refusals.Refusal)
async def _answer_a_refusal(request: Request, exc: refusals.Refusal) -> JSONResponse:
    """Every declared refusal that reaches the adapter, mapped once."""

    return refusal_response(exc)


def get_session(request: Request):
    with customer_session(request, WebSession) as session:
        yield session


# --- #680 The live-pilot web capability boundary ---------------------------
#
# `corridor.web_boundary` says which routes the live pilot serves and which
# relations each of them reaches; the migration revokes every remaining
# unpartitioned relation from `corridor_web`. Three things here keep the two
# halves honest, and they are together so a reader meets them at once.
#
# `get_machine_session` is the operations capability. Two routes carry no
# person and therefore no partition — the transport-authenticated mail
# receipt and the platform probe — and reading customer rows as the *human*
# web role with no membership behind them is exactly what the boundary is
# against. They take the worker login instead, which holds the unpartitioned
# policy, and that is what made it safe to partition `documents` and
# `source_deliveries` at all. It opens no new credential: this process
# already builds the worker engine at import.
#
# `refuse_routes_the_boundary_cannot_serve` is the route half (#680, #694).
# It reads the route's own template out of the matched scope rather than a
# second copy of the paths, so a renamed route cannot drift away from the list
# quietly, and it is a router-level dependency rather than middleware because
# that is the last thing FastAPI resolves before a handler's own dependencies
# and therefore before any statement one of them would issue.
#
# `get_web_capability` is what tells the two flag-off deployments apart, and
# it is why #694 is not a second boolean. A legacy development clone reads as
# `corridor_legacy_dev` and lost nothing, so it may not be refused; a
# live-pilot deployment that forgot the flag reads as `corridor_web` and holds
# nothing on 124 relations, so it must be. Both facts are the login the
# request's own reads run as, taken off the session's bind — no query, and no
# second thing to keep in step with the revoke. A bind this reader cannot
# inspect is neither of those deployments, and #822 stopped it passing for the
# forgiving one: `web_boundary.legacy_capabilities` names who may be excused.
#
# `_refuse_legacy_project_under_the_boundary` is the one place a route this
# boundary *enables* still has to refuse: `/work/{slug}` renders ADR-0035's
# item-per-record Work List for an unadopted project, and every relation that
# list reads is revoked.
#
# `_deployment_serves` is the same question asked forwards, for a page rather
# than for a request (#824). An enabled page that prints a control for a route
# this deployment does not serve is a button that answers 404, and the person
# clicking it has done nothing wrong. So a screen offering an optional route's
# control asks here first, and the answer is the identical `route_refusal` the
# gate would give that request -- not a second reading of the flag.


def get_machine_session(request: Request):
    """A session held by the operations capability, not the human web role."""

    with customer_session(request, WorkerSession, capability="worker") as session:
        yield session


def get_web_capability(session: Session = Depends(get_session)) -> str:
    """The database login this request's human reads run as.

    Read off the bind, so it costs no statement and cannot disagree with the
    capability the revoke was aimed at. An unrecognizable bind answers with no
    capability at all, and no capability is one `web_boundary` cannot name:
    since #822 that is an inconsistent configuration and refuses, rather than
    passing for the legacy development clone that refuses nothing.
    """

    try:
        bind = session.get_bind()
        return getattr(bind, "engine", bind).url.username or ""
    except Exception:
        return ""


def get_live_pilot_boundary_state(
    capability: str = Depends(get_web_capability),
) -> web_boundary.BoundaryState:
    """Whether this deployment enforces the boundary, lost nothing, or disagrees."""

    return web_boundary.boundary_state(
        declared=settings.live_pilot_web_boundary, web_capability=capability
    )


def refuse_routes_the_boundary_cannot_serve(
    request: Request,
    state: web_boundary.BoundaryState = Depends(get_live_pilot_boundary_state),
) -> None:
    """Answer a route this deployment cannot serve, before its handler runs."""

    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template is None:
        return
    request.state.live_pilot_boundary = state
    refusal = web_boundary.route_refusal(state, request.method.upper(), template)
    if refusal is not None:
        raise HTTPException(refusal.status_code, refusal.detail)


@app.get("/livez")
def livez() -> Response:
    """Answer that this process is running, and read nothing to say so.

    Declared *above* the router-level boundary dependency on purpose. That
    dependency resolves `get_web_capability`, which resolves `get_session`, so
    a route declared after it opens a database connection before its handler
    runs -- and `add_api_route` snapshots the router's dependencies at
    decoration time, so position is what exempts this one.

    A liveness probe that touches the database turns one slow dependency into a
    restart loop: the probe times out, the orchestrator kills a process that
    was fine, and the replacement meets the same slow database. /readyz is
    where the dependencies are allowed to matter.
    """

    return JSONResponse({"status": "ok"}, status_code=200)


# Registered on the router before the first route is declared, because
# `add_api_route` snapshots the router's dependencies at decoration time.
app.router.dependencies.append(Depends(refuse_routes_the_boundary_cannot_serve))


def _refuse_legacy_project_under_the_boundary(
    request: Request, project: Project
) -> None:
    """A pilot deployment serves adopted projects; the legacy list has no data."""

    state = getattr(
        request.state, "live_pilot_boundary", web_boundary.BoundaryState.NOT_DECLARED
    )
    if state is web_boundary.BoundaryState.ENFORCED:
        raise HTTPException(
            404, f"no project {project.slug!r}"
        )
    if state is web_boundary.BoundaryState.INCONSISTENT:
        raise HTTPException(503, web_boundary.BOUNDARY_DISABLED_REASON)


def _deployment_serves(request: Request, method: str, template: str) -> bool:
    """Whether this deployment would answer that route, so a page may offer it.

    The question a screen has to ask before printing an optional control. It is
    answered by `route_refusal` on the state the gate already decided for this
    request, so a page cannot offer a button the very next request is refused.
    """

    state = getattr(
        request.state, "live_pilot_boundary", web_boundary.BoundaryState.NOT_DECLARED
    )
    return web_boundary.route_refusal(state, method, template) is None


def get_content_store() -> ObjectStore:
    """The deployment's object store, as a seam a health probe can substitute."""

    return content_store()


@app.get("/readyz")
def readyz(
    session: Session = Depends(get_session),
    store: ObjectStore = Depends(get_content_store),
) -> Response:
    """Report whether this web process can serve, for the load balancer.

    Deliberately excludes the worker heartbeat: see
    `corridor.operational_health.serving_report`. Unauthenticated on the same
    terms as /health -- component names, bounded reason codes, nothing else.
    """

    report = serving_report(session, store=store, role=ROLE_WEB)
    return JSONResponse(
        report.as_dict(), status_code=200 if report.healthy else 503
    )


@app.get("/health")
def health(
    session: Session = Depends(get_machine_session),
    store: ObjectStore = Depends(get_content_store),
    boundary: web_boundary.BoundaryState = Depends(get_live_pilot_boundary_state),
) -> Response:
    """Report application, database, object-storage, and worker-heartbeat state.

    Unauthenticated on purpose: a platform probe has no principal. It therefore
    answers with component names, bounded reason codes, and counts only — no
    project, no principal, and no driver message (#491A).

    The live-pilot boundary is a fifth component (#694). A deployment whose
    web capability has lost 124 relations while the route half is undeclared
    is not serving; readiness has to say so, because every other reading it
    takes would be green while most of the product answered `permission
    denied`. `/health` is itself an enabled route reaching no relation, so it
    stays served in every state and can report the state that refuses others.
    """

    report = runtime_report(
        session,
        now=datetime.now(timezone.utc),
        store=store,
        role=ROLE_WEB,
    )
    report = replace(
        report,
        checks=report.checks
        + (
            ComponentHealth(
                web_boundary.HEALTH_COMPONENT,
                boundary is not web_boundary.BoundaryState.INCONSISTENT,
                web_boundary.health_detail(boundary),
            ),
        ),
    )
    return JSONResponse(
        report.as_dict(), status_code=200 if report.healthy else 503
    )


def get_coordination_summary_client_factory():
    """Build an adapter only after the route found declared spend authority."""
    from corridor.llm import OpenAIClient

    return OpenAIClient.from_spend_authorization


def get_run_explanation_client_factory():
    """Build an explanation adapter only after declared spend authority exists."""
    from corridor.llm import OpenAIClient

    return OpenAIClient.from_spend_authorization


def get_failure_diagnosis_client_factory():
    """Build a diagnosis adapter only after declared spend authority exists."""
    from corridor.llm import OpenAIClient

    return OpenAIClient.from_spend_authorization


def get_revision_change_explanation_client_factory():
    """Build a revision-change adapter only after declared spend authority exists."""
    from corridor.llm import OpenAIClient

    return OpenAIClient.from_spend_authorization


def get_intake_draft_client_factory():
    """Build an intake-draft adapter only after declared spend authority exists."""
    from corridor.llm import OpenAIClient

    return OpenAIClient.from_spend_authorization


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
    require_cohort_member(
        session,
        cohort_receipt_id,
        candidate_id,
        project_id=project.id if project else None,
    )


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
    require_event_cohort_member(
        session,
        event_cohort_receipt_id,
        candidate_id,
        project_id=project.id if project else None,
    )


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
    # The same authorization, declared to PostgreSQL for this transaction, so
    # that the reading below is partitioned by the database rather than by
    # every reader remembering its `project_id ==` (#531, ADR-0083).
    access.open_project_partition(
        session, principal_subject=principal.subject, project_id=project.id
    )
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


@app.post("/projects/{slug}/statements/{candidate_id}/attach")
def attach_waiting_statement(
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Retire raw attach-by-reference without translating it into a decision."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    # Deliberately ignore every submitted value.  The old endpoint named a
    # Constraint directly, bypassing explicit scope, source, and stale-state
    # checks.  The guided screen starts no mutation by itself.
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


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
        request=request,
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
        request=request,
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
        request=request,
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
        request=request,
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
        request=request,
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
        request=request,
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


@app.post("/statements/{slug}/{candidate_id}/admitted/complete")
async def complete_admitted_statement(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Complete an accepted commitment's Next Action without closing the fact.

    Completing internal work never establishes Completion Reported by an
    External Organization; the statement, Applies To, and reports are
    unchanged (ADR-0035/0038).
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        complete_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=required_positive_form_id(
                form, "expected_next_action_decision_id"
            ),
            principal=principal,
            successor_action=str(form.get("successor_action") or "").strip() or None,
            successor_due_date=optional_form_date(form, "successor_due_date"),
            successor_due_date_unknown_reason=(
                str(form.get("successor_due_date_unknown_reason") or "").strip() or None
            ),
            no_follow_up_reason=(
                str(form.get("no_follow_up_reason") or "").strip() or None
            ),
            note=str(form.get("note") or "").strip() or None,
        )
    except StaleStatementCoordination as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    except (StatementCoordinationRefusal, ValueError) as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    session.commit()
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


@app.post("/statements/{slug}/{candidate_id}/admitted/cancel")
async def cancel_admitted_statement(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Cancel an accepted commitment's Next Action with a structured reason."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        cancel_admitted_statement_next_action(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=required_positive_form_id(
                form, "expected_next_action_decision_id"
            ),
            principal=principal,
            cancellation_reason=(
                str(form.get("cancellation_reason") or "").strip() or None
            ),
            successor_action=str(form.get("successor_action") or "").strip() or None,
            successor_due_date=optional_form_date(form, "successor_due_date"),
            successor_due_date_unknown_reason=(
                str(form.get("successor_due_date_unknown_reason") or "").strip() or None
            ),
            no_follow_up_reason=(
                str(form.get("no_follow_up_reason") or "").strip() or None
            ),
            note=str(form.get("note") or "").strip() or None,
        )
    except StaleStatementCoordination as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    except (StatementCoordinationRefusal, ValueError) as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    session.commit()
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


@app.post("/statements/{slug}/{candidate_id}/admitted/defer")
async def defer_admitted_statement_route(
    request: Request,
    slug: str,
    candidate_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Defer an accepted commitment's immediate work to a stated return date."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_statement_candidate(session, project, candidate_id)
    form = await request.form()
    try:
        return_date = optional_form_date(form, "return_date")
        if return_date is None:
            raise StatementCoordinationRefusal(
                "a deferral needs a stated return date"
            )
        defer_admitted_statement(
            session,
            project.id,
            candidate.id,
            expected_next_action_decision_id=required_positive_form_id(
                form, "expected_next_action_decision_id"
            ),
            reason=str(form.get("deferral_reason") or "").strip(),
            return_date=return_date,
            principal=principal,
        )
    except StaleStatementCoordination as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=409
        )
    except (StatementCoordinationRefusal, ValueError) as exc:
        return _statement_coordination_screen(
            request, session, project, candidate, error=str(exc), status_code=400
        )
    session.commit()
    return RedirectResponse(
        f"/statements/{project.slug}/{candidate.id}/coordinate", status_code=303
    )


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
        request=request,
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
    receipt = active_statement_coordination_receipt(session, candidate.id)
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
        request=request,
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
            request=request,
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
            request=request,
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
        request=request,
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
        request=request,
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


def _statement_coordination_screen(
    request: Request,
    session: Session,
    project: Project,
    candidate: Candidate,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """The browser reads facts; the command remains the one mutation seam.

    The reading names its own screen, so the only thing added here is the
    refusal sentence a write path just raised, which is this route's own and
    not a fact about the record.
    """
    reading = read_statement_coordination(
        session, project=project, candidate=candidate
    )
    return TEMPLATES.TemplateResponse(
        request,
        reading.template,
        {"project": project, "reading": reading, "error": error},
        status_code=status_code,
    )


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
    # A cross-project surface still reads inside a partition: this person's
    # active projects, and no others (#531).
    access.open_member_project_partition(
        session, principal_subject=web_session.principal_subject
    )
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
    if not needs_customer_sign_in(request) and auth.load_session(request, session) is not None:
        return RedirectResponse("/", status_code=303)
    response = TEMPLATES.TemplateResponse(
        request,
        "sign_in.html",
        {"next": _safe_next(next), "sent": False, "throttled": False},
    )
    return clear_invalid_customer_cookies(request, response)


def _deliver_sign_in_link(
    sender: auth.EmailSender, email: str, link: str, raw_token: str,
    request: Request,
) -> None:
    """Send the link, and let no outcome reach the caller.

    Runs after the response has been returned. A delivery failure is a real
    operational event and is logged as one, with the recipient and the bounded
    reason -- never the link, which is a live credential.

    On failure the token is retired. It reached nobody, and `has_live_token`
    would otherwise make every retry inside its 15-minute lifetime coalesce
    onto the dead one: the caller keeps seeing the same success page while no
    link is ever issued or sent. Its own session, because the request's
    transaction committed before this ran.
    """

    try:
        sender.send_sign_in_link(email=email, link=link)
        return
    except auth.EmailDeliveryUnavailable as error:
        reason = str(error)

    retired = False
    try:
        # The originating request keeps the trusted customer route. A failed
        # delivery must retire its token in that same database (#656).
        with customer_session(request, WebSession) as session:
            retired = access.retire_undelivered_sign_in_token(session, raw_token)
            session.commit()
    except Exception:  # noqa: BLE001 - the response is already sent
        retired = False

    telemetry.log_event(
        logging.getLogger("corridor.auth"),
        "sign_in_link_delivery_failed",
        reason=reason,
        token_retired=retired,
    )


@app.post("/sign-in/request", response_class=HTMLResponse)
def request_sign_in(
    request: Request,
    background: BackgroundTasks,
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
        return clear_invalid_customer_cookies(request, response)
    access.record_attempt(session, access.ISSUE_IP, scope)
    if normalized:
        access.record_attempt(session, access.ISSUE_EMAIL, normalized)
        identity = access.identity_for_email(session, normalized)
        if identity is not None and not access.has_live_token(session, normalized):
            issued = access.issue_sign_in_token(
                session, normalized, redirect_path=_safe_next(next)
            )
            # Never request.base_url: that is the caller's own Host header,
            # so a forged host would send the real user a live token pointing
            # at the attacker. The origin comes from configuration, and only a
            # local clone is allowed to fall back to the request.
            origin = auth.PUBLIC_ORIGIN or str(request.base_url).rstrip("/")
            link = origin + "/sign-in/consume?token=" + quote(issued.raw_token)
            # Delivered after the response, not during it. A synchronous send
            # happens only for an enrolled address, so its latency -- and a
            # provider failure, which would otherwise surface as a 500 -- would
            # tell an unauthenticated caller which addresses are enrolled. That
            # is exactly what this endpoint's identical answer exists to
            # prevent, and it would undo it through the side door.
            background.add_task(
                _deliver_sign_in_link, sender, normalized, link, issued.raw_token, request
            )
    session.commit()
    response = TEMPLATES.TemplateResponse(
        request,
        "sign_in.html",
        {"next": _safe_next(next), "sent": True, "throttled": False},
    )
    return clear_invalid_customer_cookies(request, response)


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
        response = TEMPLATES.TemplateResponse(
            request, "sign_in_invalid.html", {}, status_code=429
        )
        return clear_invalid_customer_cookies(request, response)
    access.record_attempt(session, access.CONSUME_IP, scope)
    consumed = access.consume_sign_in_token(session, token)
    if consumed is None:
        session.commit()
        response = TEMPLATES.TemplateResponse(
            request, "sign_in_invalid.html", {}, status_code=400
        )
        return clear_invalid_customer_cookies(request, response)
    new_session = access.create_web_session(
        session,
        principal=consumed.principal,
        email_normalized=consumed.email_normalized,
    )
    response = RedirectResponse(_safe_next(consumed.redirect_path or "/"), status_code=303)
    auth.set_session_cookies(response, new_session)
    set_customer_cookie(response, new_session.raw_session_id)
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
    clear_customer_cookie(response)
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
            "publications": project_publication_history(session, project.id),
        },
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="reports",
        request=request,
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
    review = review_prepared_external_report(session, project.id, artifact_id)
    response = TEMPLATES.TemplateResponse(
        request,
        "report_review.html",
        {"project": project, "review": review},
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="review_report",
        request=request,
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
    artifact = retrieve_prepared_external_report(
        session, project.id, artifact_id
    )
    response = _pdf_download(bytes(artifact.pdf_bytes), artifact.artifact_name)
    record_frontend_request(
        session,
        principal=principal,
        route_name="download_prepared_report",
        request=request,
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
    artifact = retrieve_prepared_external_report(
        session, project.id, artifact_id
    )
    response = _pdf_response(
        bytes(artifact.pdf_bytes), artifact.artifact_name, disposition="inline"
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="preview_prepared_report",
        request=request,
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
            request=request,
            response=response,
            subject=FrontendRequestSubject(
                project_id=project.id,
                artifact_id=artifact_id,
                release_id=receipt.id,
            ),
            request_fields={},
        )
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
            request=request,
            response=response,
            subject=FrontendRequestSubject(
                project_id=project.id,
                artifact_id=artifact.id,
                report_run_id=report_run.id,
            ),
            request_fields={"ordinary": ordinary},
        )
    session.commit()
    return response


@app.post("/reports/{slug}/release")
def release_report(
    request: Request,
    slug: str,
    artifact_id: int = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Human-authorize one previously rendered PDF without regenerating it."""
    project = _project(session, slug, principal, designation=access.EXTERNAL_RELEASE)
    release = release_external_report(
        session,
        project_id=project.id,
        artifact_id=artifact_id,
        principal=principal,
    )
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
        request=request,
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
    # One element per response takes focus: a refusal, then a completed
    # import, then the reading itself (#559).
    context["focus"] = ui_primitives.focus_target(
        refused=bool(context["error"] or context["stale"]),
        saved=bool(context["message"]),
    )
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
        request=request,
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
        session, principal=principal, route_name="read_internal_coordination_summary",
        request=request, response=response,
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
        request=request,
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
                # Read the Constraint reading, never the population's own
                # subject: an adopted project's subject is an
                # ``AcceptedConstraint`` with no ``title`` column at all, and
                # reading one off it answered 500 on every alert facet of every
                # adopted project. The reading is the one shape both
                # populations have, and it composes the accepted record's title
                # at its owner.
                "dependency_id": row.reading.id,
                "ref_code": row.reading.ref_code,
                "title": row.reading.title,
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
        request=request,
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
        request=request,
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
        request=request,
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
        request=request,
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
            request=request,
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
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id, check_configuration_id=row.id
        ),
        request_fields=form,
    )
    session.commit()
    return response


# --- Processing operations (#344) -----------------------------------------


def _render_processing_operations(
    request: Request, session: Session, project: Project, *, notice: str | None = None
):
    """Render the operator's reading; every rule in it belongs to its owner."""
    return TEMPLATES.TemplateResponse(
        request,
        "operations.html",
        {
            "project": project,
            "notice": notice,
            "reading": operations_view(session, project_id=project.id),
        },
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
        request=request,
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
    lock_project(session, project.id)
    # The screen's own offer, checked against the live state before the command
    # is called. Only this comparison is the adapter's to refuse; the command's
    # own rules refuse with their own declared families, and anything else it
    # raises is a defect rather than something to tell an operator to retry.
    if offered_state != competing_runs_state_token(session, document.id):
        raise refusals.StaleOffer(
            "the production-run choices changed; refresh first"
        )
    declaration = declare_active_run(
        session, document.id, run_id, principal=principal
    )
    response = RedirectResponse(
        f"/operations/{project.slug}?notice=declared-{declaration.id}", status_code=303
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name="declare_operations_active_run",
        request=request,
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.post("/operations/{slug}/run-explanation/configuration")
async def declare_run_explanation_configuration_route(
    slug: str,
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Declare bounded run-explanation authority; this is technical operations.

    A missing or incomplete declaration is exactly why an explanation refuses
    before any model call — there is no environment or default fallback.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    form = await request.form()
    try:
        declare_run_explanation_configuration(
            session,
            project_id=project.id,
            principal=principal,
            model=str(form.get("model", "")),
            prompt_version=str(form.get("prompt_version", "")),
            max_input_tokens=int(str(form.get("max_input_tokens", ""))),
            max_output_tokens=int(str(form.get("max_output_tokens", ""))),
            timeout_seconds=int(str(form.get("timeout_seconds", ""))),
            max_requests=int(str(form.get("max_requests", ""))),
            retry_policy=str(form.get("retry_policy", "")),
            retention_policy=str(form.get("retention_policy", "")),
            observation_context=str(form.get("observation_context", "")),
        )
    except (ValueError, InvalidExplanationConfiguration) as exc:
        raise HTTPException(
            400, f"run-explanation configuration refused: {exc}"
        ) from exc
    session.commit()
    return RedirectResponse(f"/operations/{project.slug}", status_code=303)


@app.post("/operations/{slug}/runs/{document_id}/explain")
async def explain_operations_competing_runs(
    request: Request,
    slug: str,
    document_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    client_factory=Depends(get_run_explanation_client_factory),
):
    """Run one explicit, read-only explanation of competing production runs.

    The declaration controls are untouched: this only reads immutable snapshots
    and stores a non-authoritative receipt. It never declares or preselects a
    run.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document")
    form = await request.form()
    raw_ids = str(form.get("competing_run_ids") or "").strip()
    try:
        expected_run_ids = tuple(
            int(part) for part in raw_ids.split(",") if part.strip()
        )
    except ValueError as exc:
        raise HTTPException(400, "competing_run_ids must be integers") from exc
    state_token = str(form.get("state_token") or "")
    try:
        receipt = request_run_explanation(
            session,
            project_id=project.id,
            document_id=document.id,
            principal=principal,
            client_factory=client_factory,
            expected_run_ids=expected_run_ids,
            state_token=state_token,
        )
    except ExplanationRequestRefused as exc:
        raise HTTPException(
            404 if exc.reason == "cross_project" else 409, exc.detail
        ) from exc
    except RunExplanationConfigurationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/operations/{project.slug}/runs/{document.id}/explanation/{receipt.public_id}",
        status_code=303,
    )


@app.get(
    "/operations/{slug}/runs/{document_id}/explanation/{public_id}",
    response_class=HTMLResponse,
)
def read_run_explanation(
    request: Request,
    slug: str,
    document_id: int,
    public_id: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read one retained non-authoritative explanation; a GET never spends.

    The deterministic run details and the human declaration controls stay live
    and no run is preselected — whether the explanation completed, refused, or
    abstained, the operator still declares.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document")
    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.public_id == public_id,
            ProductionRunExplanationRequest.project_id == project.id,
            ProductionRunExplanationRequest.document_id == document.id,
        )
    ).first()
    if receipt is None:
        raise HTTPException(404, "no run explanation for this document")
    current = session.get(ActiveExtractionRun, document.id)
    return TEMPLATES.TemplateResponse(
        request,
        "production_run_explanation.html",
        {
            "project": project,
            "document": document,
            "receipt": receipt,
            "runs": (receipt.comparison_json or {}).get("runs", []),
            "competing_runs": competing_production_runs(session, document.id),
            "current_active_run_id": current.extraction_run_id if current else None,
            "offer_state": competing_runs_state_token(session, document.id),
        },
    )


@app.post("/operations/{slug}/failure-diagnosis/configuration")
async def declare_failure_diagnosis_configuration_route(
    slug: str,
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Declare bounded failure-diagnosis authority; this is technical operations.

    A missing or incomplete declaration is exactly why a diagnosis refuses
    before any model call — there is no environment or default fallback.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    form = await request.form()
    try:
        declare_failure_diagnosis_configuration(
            session,
            project_id=project.id,
            principal=principal,
            model=str(form.get("model", "")),
            prompt_version=str(form.get("prompt_version", "")),
            max_input_tokens=int(str(form.get("max_input_tokens", ""))),
            max_output_tokens=int(str(form.get("max_output_tokens", ""))),
            timeout_seconds=int(str(form.get("timeout_seconds", ""))),
            max_requests=int(str(form.get("max_requests", ""))),
            retry_policy=str(form.get("retry_policy", "")),
            retention_policy=str(form.get("retention_policy", "")),
            observation_context=str(form.get("observation_context", "")),
        )
    except (ValueError, InvalidDiagnosisConfiguration) as exc:
        raise HTTPException(
            400, f"failure-diagnosis configuration refused: {exc}"
        ) from exc
    session.commit()
    return RedirectResponse(f"/operations/{project.slug}", status_code=303)


@app.post("/operations/{slug}/runs/{document_id}/diagnose")
async def diagnose_operations_failed_run(
    request: Request,
    slug: str,
    document_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    client_factory=Depends(get_failure_diagnosis_client_factory),
):
    """Run one explicit, read-only diagnosis of one failed extraction attempt.

    The failure receipt, run outcome, and quarantine are untouched: this only
    reads the immutable failure detail and the permitted source pages and stores
    a non-authoritative diagnosis. It never retries or relabels the failure.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document")
    form = await request.form()
    expected_run_id = _required_positive_http_id(form, "extraction_run_id")
    state_token = str(form.get("state_token") or "")
    try:
        receipt = request_failure_diagnosis(
            session,
            project_id=project.id,
            document_id=document.id,
            principal=principal,
            client_factory=client_factory,
            expected_run_id=expected_run_id,
            state_token=state_token,
        )
    except FailureDiagnosisRefused as exc:
        raise HTTPException(
            404 if exc.reason in ("cross_project", "no_such_run") else 409,
            exc.detail,
        ) from exc
    except FailureDiagnosisConfigurationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/operations/{project.slug}/runs/{document.id}/diagnosis/{receipt.public_id}",
        status_code=303,
    )


@app.get(
    "/operations/{slug}/runs/{document_id}/diagnosis/{public_id}",
    response_class=HTMLResponse,
)
def read_failure_diagnosis(
    request: Request,
    slug: str,
    document_id: int,
    public_id: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read one retained non-authoritative diagnosis; a GET never spends.

    The deterministic failure detail and the safe operator recovery paths stay
    available whether the diagnosis completed, refused, or could not help — the
    failure is never relabelled and the record stays with the operator.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document")
    receipt = session.scalars(
        select(ExtractionFailureDiagnosisRequest).where(
            ExtractionFailureDiagnosisRequest.public_id == public_id,
            ExtractionFailureDiagnosisRequest.project_id == project.id,
            ExtractionFailureDiagnosisRequest.document_id == document.id,
        )
    ).first()
    if receipt is None:
        raise HTTPException(404, "no failure diagnosis for this document")
    return TEMPLATES.TemplateResponse(
        request,
        "extraction_failure_diagnosis.html",
        {
            "project": project,
            "document": document,
            "receipt": receipt,
            "failure": (receipt.source_context_json or {}).get("failure", {}),
            "pages": (receipt.source_context_json or {}).get("pages", []),
            "quarantine": session.get(DocumentQuarantine, document.id),
        },
    )


@app.post("/operations/{slug}/revision-change-explanation/configuration")
async def declare_revision_change_explanation_configuration_route(
    slug: str,
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Declare bounded revision-change-explanation authority; technical operations.

    A missing or incomplete declaration is exactly why an explanation refuses
    before any model call — there is no environment or default fallback.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    form = await request.form()
    try:
        declare_revision_change_configuration(
            session,
            project_id=project.id,
            principal=principal,
            model=str(form.get("model", "")),
            prompt_version=str(form.get("prompt_version", "")),
            max_input_tokens=int(str(form.get("max_input_tokens", ""))),
            max_output_tokens=int(str(form.get("max_output_tokens", ""))),
            timeout_seconds=int(str(form.get("timeout_seconds", ""))),
            max_requests=int(str(form.get("max_requests", ""))),
            retry_policy=str(form.get("retry_policy", "")),
            retention_policy=str(form.get("retention_policy", "")),
            observation_context=str(form.get("observation_context", "")),
        )
    except (ValueError, RevisionChangeInvalidConfiguration) as exc:
        raise HTTPException(
            400, f"revision-change-explanation configuration refused: {exc}"
        ) from exc
    session.commit()
    return RedirectResponse(f"/operations/{project.slug}", status_code=303)


@app.post("/ledger/{slug}/{dependency_id}/explain-revision-change")
async def explain_revision_change(
    request: Request,
    slug: str,
    dependency_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    client_factory=Depends(get_revision_change_explanation_client_factory),
):
    """Run one explicit, read-only explanation of a verified newer-document change.

    The deterministic question and change context are untouched: this only reads
    the exact integrity-verified comparison and stores a non-authoritative
    receipt. It never selects a comparison, updates support, or settles anything.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    form = await request.form()
    try:
        expected_comparison_id = int(str(form.get("comparison_id", "")))
        expected_finding_id = int(str(form.get("finding_id", "")))
    except ValueError as exc:
        raise HTTPException(
            400, "comparison_id and finding_id must be integers"
        ) from exc
    state_token = str(form.get("state_token") or "")
    try:
        request_revision_change_explanation(
            session,
            project_id=project.id,
            dependency_id=dependency_id,
            principal=principal,
            client_factory=client_factory,
            expected_comparison_id=expected_comparison_id,
            expected_finding_id=expected_finding_id,
            state_token=state_token,
        )
    except RevisionChangeExplanationRequestRefused as exc:
        raise HTTPException(
            404 if exc.reason == "wrong_project" else 409, exc.detail
        ) from exc
    except RevisionChangeConfigurationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/ledger/{project.slug}/{dependency_id}", status_code=303
    )


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
    lock_project(session, project.id)
    status = read_event_admission_policy_status(session, project.id)
    if "suspend" not in status.allowed_operations:
        raise refusals.NotOffered("unknown-scope Event Admission is not active")
    if str(form.get("state_fingerprint") or "") != policy_offer_state(status):
        raise refusals.StaleOffer("the policy status changed; refresh first")
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason=reason,
        recorded_by=principal.subject,
    )
    response = RedirectResponse(f"/operations/{project.slug}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="suspend_operations_unknown_scope",
        request=request,
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
    lock_project(session, project.id)
    status = read_event_admission_policy_status(session, project.id)
    if "lift" not in status.allowed_operations:
        raise refusals.NotOffered("unknown-scope Event Admission is not suspended")
    if str(form.get("state_fingerprint") or "") != policy_offer_state(status):
        raise refusals.StaleOffer("the policy status changed; refresh first")
    lift_unknown_scope_admission(
        session, project_id=project.id, recorded_by=principal.subject
    )
    response = RedirectResponse(f"/operations/{project.slug}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="lift_operations_unknown_scope",
        request=request,
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=form,
    )
    session.commit()
    return response


@app.get("/assignments/{slug}/inbox", response_class=HTMLResponse)
def assignment_inbox(
    request: Request,
    slug: str,
    notice: str | None = None,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One member's own new-assignment notifications and delivery standing (#351).

    Membership alone gates this read; a member sees only their own assignments in
    this one project, never another person's or another project's records.
    """
    project = _project(session, slug, principal)
    items = notifications.recipient_inbox(
        session, project_id=project.id, principal_subject=principal.subject
    )
    response = TEMPLATES.TemplateResponse(
        request,
        "assignment_inbox.html",
        {"project": project, "items": items, "notice": notice},
    )
    session.commit()
    return response


@app.post("/assignments/{slug}/notifications/{notification_id}/flag-incorrect")
async def flag_incorrect_assignment_route(
    request: Request,
    slug: str,
    notification_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The assigned person flags an incorrect assignment; it changes nothing (#351).

    The assignment and its Work Decision history stand until an authorized person
    changes them. The notification must belong to this project, and the domain
    guard additionally requires the acting person to be the assigned recipient.
    """
    project = _project(session, slug, principal)
    notification = session.get(AssignmentNotification, notification_id)
    if notification is None or notification.project_id != project.id:
        raise HTTPException(404, "no such assignment notification")
    form = await request.form()
    note = str(form.get("note") or "").strip() or None
    try:
        notifications.flag_incorrect_assignment(
            session,
            notification_id=notification_id,
            principal=principal,
            note=note,
        )
    except notifications.NotificationAccessRefusal as exc:
        raise HTTPException(403, str(exc)) from exc
    except notifications.NotificationRefusal as exc:
        raise HTTPException(404, str(exc)) from exc
    response = RedirectResponse(
        f"/assignments/{project.slug}/inbox?notice=flagged", status_code=303
    )
    session.commit()
    return response


@app.get("/operations/{slug}/deliveries", response_class=HTMLResponse)
def assignment_delivery_operations(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """The operator's project-scoped view of assignment-notification delivery (#351).

    Distinguishes queued, completed, retry-due, failed, and uncertain deliveries
    with subject context and whether real delivery is enabled by a recorded
    gate-7 configuration. Gated by the technical-operations designation.
    """
    project = _project(
        session, slug, principal, designation=access.TECHNICAL_OPERATIONS
    )
    view = notifications.operations_delivery_view(session, project_id=project.id)
    response = TEMPLATES.TemplateResponse(
        request,
        "assignment_deliveries.html",
        {"project": project, "view": view},
    )
    session.commit()
    return response


def get_review_clock():
    """The instant one review request is read and decided at.

    A seam rather than a call to ``datetime.now`` inside the handlers: the
    reading's cutoff, a deferral's return date, and the decision time all come
    from here, so a test states the moment instead of racing the wall clock
    (ADR-0084 and #488's rule that no logical time is taken from a clock a
    caller cannot supply).
    """

    def now() -> datetime:
        return datetime.now(timezone.utc)

    return now


# --- The adopted project's ordered week (#536) -----------------------------
#
# Review, accepted follow-up, and the customer issue, in the order they happen,
# with the landing section derived rather than stored. The page carries no
# decision control of its own: `read_project_workflow` re-derives nothing about
# which deltas are decided together, and every act stays on the one surface
# that already owns it, so no Proposed Delta grows a second control
# (ADR-0085's exactly-once rule).


def _project_workflow_response(
    request: Request,
    project: Project,
    principal: HumanPrincipal,
    session: Session,
    *,
    now: datetime,
    refusal: AuthorizationRefused | None = None,
    approved: Authorization | None = None,
    prepare_refusal: str | None = None,
    requested: ReleasePreparationRequest | None = None,
    route_name: str = "coordinator_home",
    request_fields: Any = None,
    status_code: int = 200,
) -> Response:
    """Render one adopted project's week and record the read.

    The same rendering serves the plain reading and the response to the one act
    the page carries, so a coordinator who approves an issue — or is refused —
    reads the outcome on the surface they were already on, with the week
    re-derived around it rather than a redirect to a page that would answer the
    same questions again.
    """

    workflow = read_project_workflow(session, project_id=project.id, as_of=now)
    landing = workflow.section(workflow.landing)
    # The chase list is read here and rendered inside the Follow-up section;
    # every trigger, bundling and ordering rule stays in the one derivation
    # (#425), and `chase_view` refuses to render any sequence but the
    # reading's own (#658).
    chase = chase_view(
        read_follow_up_bundles(session, project_id=project.id, as_of=now)
    )
    # The Issue section (#529, #533). `issue_view` reads #529's own
    # `authorization_blockers` and refuses to offer an approval it named a
    # reason against; nothing here derives readiness a second time.
    issue = issue_view(session, project_id=project.id, as_of=now)
    response = TEMPLATES.TemplateResponse(
        request,
        "project_workflow.html",
        {
            "project": project,
            "workflow": workflow,
            "chase": chase,
            "issue": issue,
            "landing": landing,
            "refusal": refusal,
            "approved": approved,
            # The outcome of #675's own act, kept apart from #533's: a
            # preparation that was refused sent nothing and approved nothing,
            # and announcing it under the approval's heading would tell a
            # coordinator something untrue about what just happened.
            "prepare_refusal": prepare_refusal,
            "requested": requested,
            # Exactly one element carries `autofocus`: a refusal first, then a
            # completed approval, and otherwise the section the coordinator's
            # work actually starts in.
            "focus": (
                ui_primitives.focus_target(
                    refused=refusal is not None or prepare_refusal is not None,
                    saved=approved is not None or requested is not None,
                )
                if refusal is not None
                or approved is not None
                or prepare_refusal is not None
                or requested is not None
                else workflow.landing
            ),
            "cutoff": workflow.cutoff.date().isoformat(),
            "today": now.date(),
        },
        status_code=status_code,
    )
    record_frontend_request(
        session,
        principal=principal,
        route_name=route_name,
        request=request,
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=(
            request.query_params if request_fields is None else request_fields
        ),
    )
    # The presentation itself, under the #558 contract, at the cutoff the
    # reading was bound to rather than at a clock. The Product Proving receipt
    # above proves which handler served the request; this records what the
    # chase list said when it did, by its own content digest.
    analytics_binding = binding_for_session(session)
    emit_follow_up_reading(
        chase.reading,
        principal_subject=principal.subject,
        surface="project_workflow",
        binding=analytics_binding,
    )
    # The week already puts these packets in front of a person. Waiting until
    # /review was opened would lose every unopened interruption from #532.
    for item in workflow.undecided:
        emit_packet_surfacing(workflow.review, item, principal_subject=principal.subject, binding=analytics_binding)
    if request.method == "GET":
        emit_presentation(EventFamily.PROJECT_OPENING, project_id=project.id,
                          principal_subject=principal.subject, at=now, binding=analytics_binding)
    if issue.coverage is not None:
        coverage = issue.coverage
        emit_presentation(
            EventFamily.COVERAGE_READING, project_id=project.id,
            principal_subject=principal.subject, at=now, binding=analytics_binding,
            reading_sha256=coverage.reading_digest,
            issue_profile_identity=coverage.profile_identity,
            issue_profile_version=coverage.profile_version,
            issue_profile_sha256=coverage.profile_sha256,
            coverage_declaration_id=(issue.candidate.coverage_declaration_id if issue.candidate else None),
            through_source_delivery_id=coverage.through_source_delivery_id,
        )
    session.commit()
    return response


@app.post("/work/{slug}/issue/authorize", response_class=HTMLResponse)
def authorize_project_issue(
    request: Request,
    slug: str,
    candidate_id: int = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Approve one prepared issue for sharing, without leaving the week (#533).

    **No designation check here.** Only a principal holding the
    external-release designation may authorize, and #533 proves that inside
    PostgreSQL as the release command's own owner. Passing
    ``designation=access.EXTERNAL_RELEASE`` to `_project` would be a second
    gate over the same roster that a later change could let drift from the one
    the command reads, and it would answer a member without the designation
    with a bare 403 rather than the sentence the database gives. `_project`
    still runs, because it is the project partition every slug-addressed
    surface opens and the read boundary a non-member is answered by (#531).

    **No second readiness derivation either.** A blocked or stale candidate is
    refused by ``authorize_release_package`` through #529's own
    ``authorization_blockers``, and the section does not offer the control in
    the first place because ``issue_view`` read the same answer. Both refusals
    are the one authority; neither is a rule spelled again here.

    The release instant is the declared one from `get_review_clock`, the same
    instant the week around it is read at.
    """

    project = _project(session, slug, principal)
    if not is_adopted_baseline(session, project.id):
        # This act belongs to the adopted project's ordered week and to no
        # other surface. A legacy project has no such week and no prepared
        # candidate, and is answered the way a route that does not exist for
        # it is answered rather than with a refusal about a candidate.
        raise HTTPException(404, f"no project {slug!r}")
    now = clock()
    try:
        approved = authorize_release_package(
            session,
            project_id=project.id,
            candidate_id=candidate_id,
            releaser=principal,
            authorized_at=now,
            surface="project_workflow",
            binding=binding_for_session(session),
        )
    except AuthorizationRefused as refusal:
        # Every refusal left the candidate as it was and wrote no part of a
        # release; the designation refusal gave up only its own savepoint, so
        # this session can still render the week around the refusal.
        return _project_workflow_response(
            request,
            project,
            principal,
            session,
            now=now,
            refusal=refusal,
            route_name="authorize_project_issue",
            request_fields={"candidate_id": candidate_id},
            status_code=403 if refusal.code == NOT_DESIGNATED else 409,
        )
    return _project_workflow_response(
        request,
        project,
        principal,
        session,
        now=now,
        approved=approved,
        route_name="authorize_project_issue",
        request_fields={"candidate_id": candidate_id},
        status_code=201,
    )


@app.post("/work/{slug}/issue/prepare", response_class=HTMLResponse)
def prepare_project_issue(
    request: Request,
    slug: str,
    issue_profile_id: int = Form(...),
    issue_profile_version: int = Form(...),
    accepted_revision_id: int = Form(...),
    cutoff: str = Form(...),
    derived_reading_digest: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Confirm the coverage shown and ask for this issue to be prepared (#675).

    **It revalidates everything the coordinator was looking at.** The declared
    cutoff, the issue profile row and version, the accepted revision, and the
    digest of the derived coverage reading all travel through the form and are
    all re-derived here before anything is written. A submission composed
    against a reading that has since moved — a source arrived, a document
    finished processing, the profile took a new version, the record advanced —
    is refused and nothing is appended. That is the point of carrying the
    digest at all: without it the confirmation would attest to whatever the
    database happened to say when the POST landed.

    **It writes two separately identified records and returns.** The coverage
    declaration is the person's attestation over the machine's reading; the
    preparation request is what a worker executes through #529's three phases.
    Both are idempotent by a key derived from the confirmed declaration, so a
    resubmitted form converges rather than queueing a second preparation.

    **It renders nothing.** #529 renders outside its own short transactions and
    needs a session factory; the customer's workbook renderer holds a session
    while it reads and produces the file. Doing that here would hold a web
    worker for the length of a workbook render and would be ambiguous to retry.

    **No designation check here either.** Preparing a candidate is not
    releasing one: #533's external-release designation gates the approval and
    PostgreSQL proves it there. `_project` still runs, because it is the
    project partition every slug-addressed surface opens.
    """

    project = _project(session, slug, principal)
    if not is_adopted_baseline(session, project.id):
        raise HTTPException(404, f"no project {slug!r}")
    now = clock()
    fields = {
        "issue_profile_id": issue_profile_id,
        "issue_profile_version": issue_profile_version,
        "accepted_revision_id": accepted_revision_id,
        "cutoff": cutoff,
        "derived_reading_digest": derived_reading_digest,
    }
    try:
        # One savepoint around both appends, so a refusal gives up its own work
        # and nothing else: the same shape the authorization route above uses,
        # and what lets this session still render the week around the refusal.
        with session.begin_nested():
            declared = _declared_cutoff(cutoff, now)
            inventory = effective_issue_inventory(session, project.id, declared)
            reading = derive_coverage_reading(
                session,
                project_id=project.id,
                cutoff=declared,
                inventory=inventory,
            )
            if reading is None:
                raise CoverageRefused(
                    "this project is not configured to issue anything as at that "
                    "cutoff, so there is no coverage to confirm"
                )
            if (
                reading.profile_id != int(issue_profile_id)
                or reading.profile_version != int(issue_profile_version)
            ):
                raise CoverageRefused(
                    "what this project is configured to issue changed after this "
                    "coverage was shown, so the reading you confirmed is not the "
                    "one in force. Read the week again."
                )
            declaration = confirm_coverage(
                session,
                project_id=project.id,
                reading=reading,
                confirmed_reading_digest=derived_reading_digest,
                principal=principal,
                confirmed_at=now,
                # Derived from what was confirmed rather than generated, so a
                # resubmitted form converges on the declaration already recorded
                # instead of appending a second identical one.
                idempotency_key=f"coverage:{reading.reading_digest}",
            )
            requested = request_preparation(
                session,
                project_id=project.id,
                declaration=declaration,
                accepted_revision_id=accepted_revision_id,
                requested_by=principal,
                requested_at=now,
                # Derived rather than generated, and derived from the finished
                # attempt as well as the confirmed reading: a resubmitted form
                # converges on the request already recorded, and a retry after
                # a preparation that produced nothing is a second request a
                # worker can run (`preparation_idempotency_key`).
                idempotency_key=preparation_idempotency_key(
                    session,
                    project_id=project.id,
                    declaration=declaration,
                ),
            )
    except (CoverageRefused, PreparationRequestRefused) as refusal:
        return _project_workflow_response(
            request,
            project,
            principal,
            session,
            now=now,
            prepare_refusal=str(refusal),
            route_name="prepare_project_issue",
            request_fields=fields,
            status_code=409,
        )
    return _project_workflow_response(
        request,
        project,
        principal,
        session,
        now=now,
        requested=requested,
        route_name="prepare_project_issue",
        request_fields=fields,
        status_code=202,
    )


def _declared_cutoff(supplied: str, now: datetime) -> datetime:
    """The cutoff the coordinator was shown, proved rather than trusted.

    It comes back through the form because a confirmation is *of a reading at a
    cutoff*, and the instant the POST lands is not that cutoff. It is proved
    time-zone-aware and not in the future, so a caller cannot confirm coverage
    of sources that have not arrived; everything else about it is the
    coordinator's own declared instant, exactly as every other seam from #640
    onward requires.
    """

    try:
        declared = datetime.fromisoformat(supplied)
    except ValueError as exc:
        raise CoverageRefused(
            "the cutoff this coverage was read at is not a readable instant"
        ) from exc
    if declared.tzinfo is None:
        raise CoverageRefused(
            "the cutoff this coverage was read at carries no time zone"
        )
    if declared > now:
        raise CoverageRefused(
            "coverage cannot be confirmed for a cutoff that has not arrived"
        )
    return declared


# --- The exact bytes of one prepared or approved issue (#830) --------------
#
# A coordinator could read an artifact's type, renderer and digest and had no
# way to the file. These three routes are that way, and they are reads: they
# write nothing, retain nothing, and change nothing about the candidate or the
# receipt they serve from. The legacy single-report downloads above stay where
# they are and stay out of the pilot; ADR-0086's package is the release model,
# and a second one is exactly what the audit asked not to be built.
#
# **Every identifier is resolved on the server.** The path carries a project
# slug, a candidate row id or an issue number, and an artifact type. There is
# no parameter for a storage key or a digest, and the lookups below filter on
# `project.id` as well as the identifier, so a member of one project naming
# another project's candidate id or issue number is answered by the same 404
# that a missing row gives — `_project` having already answered the slug that
# way for a non-member (#331).
#
# **No designation check.** Reading back what was prepared or approved for
# this project is the project's plain read boundary, the same one `/record`
# uses. The external-release designation gates the *approval*, PostgreSQL
# proves it there, and a second gate here would deny a coordinator the file
# they are being asked to check before approving it.
#
# **A digest mismatch raises.** `release_authorization`'s retrieval functions
# read through the content-addressed store, which verifies on the way out.
# Nothing below catches that: serving bytes that no longer hash to what the
# receipt recorded is the one outcome a customer deliverable may not have, so
# the request fails and the file is not served.


@app.get("/work/{slug}/issue/candidates/{candidate_id}/artifacts/{artifact_type}")
def download_candidate_artifact(
    slug: str,
    candidate_id: int,
    artifact_type: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Download exactly the bytes one prepared candidate retained.

    Every candidate this project holds is reachable, including one the project
    has moved past: an un-offerable candidate "stays listed here as it was
    prepared, so what was proposed to the customer and refused is not lost",
    and a listing whose files could not be opened would keep only half of it.
    Inspecting a candidate is not approving one — whether the approval is
    offered is `issue_view`'s answer, taken from #529's own blockers, and this
    route offers nothing.
    """

    project = _project(session, slug, principal)
    candidate = session.scalars(
        select(ReleaseCandidate).where(
            ReleaseCandidate.id == candidate_id,
            ReleaseCandidate.project_id == project.id,
        )
    ).first()
    if candidate is None:
        raise HTTPException(404, "no such prepared issue for this project")
    try:
        data = retrieve_candidate_artifact(session, candidate, artifact_type)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return download_response(
        data,
        filename=download_name(
            project_slug=project.slug,
            kind=CANDIDATE_DOWNLOAD,
            number=int(candidate.id),
            artifact_type=artifact_type,
        ),
    )


@app.get("/work/{slug}/issue/packages/{issue_number}/artifacts/{artifact_type}")
def download_issue_artifact(
    slug: str,
    issue_number: int,
    artifact_type: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Download exactly the bytes one approved issue sealed.

    The issue number is the package's position in this project's release
    chain, which is what a person means by "the third issue", and it is the
    identifier the Issue section and the Record view both print. An earlier
    issue answers with the bytes it was approved with however far the accepted
    record has moved since: the object is content-addressed and the receipt is
    immutable, so there is nothing here to re-derive.
    """

    project = _project(session, slug, principal)
    package = _issue_package(session, project, issue_number)
    try:
        data = retrieve_released_artifact(session, package, artifact_type)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return download_response(
        data,
        filename=download_name(
            project_slug=project.slug,
            kind=ISSUE_DOWNLOAD,
            number=int(package.sequence_number),
            artifact_type=artifact_type,
        ),
    )


@app.get("/work/{slug}/issue/packages/{issue_number}/bundle")
def download_issue_bundle(
    slug: str,
    issue_number: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Download one approved issue as the single set it was approved as.

    ADR-0086 makes the *set* the external issue unit, so the set has to be
    obtainable as one thing rather than as a list of files a person collects
    by hand and hopes they finished. `retrieve_released_package` reads and
    verifies every member before any of them is returned, and the archive is
    written only from that complete verified set: a member whose retained
    bytes no longer hash to what the receipt recorded raises, and no partial
    bundle is served.
    """

    project = _project(session, slug, principal)
    package = _issue_package(session, project, issue_number)
    members = retrieve_released_package(session, package)
    return download_response(
        bundle_bytes(members),
        filename=download_name(
            project_slug=project.slug,
            kind=ISSUE_DOWNLOAD,
            number=int(package.sequence_number),
        ),
    )


def _issue_package(
    session: Session, project: Project, issue_number: int
) -> ReleasePackage:
    """One approved package of this project, by its number in the chain.

    The caller has already passed the project through `_project`, and the
    number is resolved within the project it returned, so an issue number that
    exists in some other project is not this project's issue number and is
    answered as missing.
    """

    package = session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id == project.id,
            ReleasePackage.sequence_number == issue_number,
        )
    ).first()
    if package is None:
        raise HTTPException(404, "no such approved issue for this project")
    return package


# --- The coordinator's cross-project week (#537) ---------------------------
#
# One reading over every adopted project the signed-in person may coordinate,
# derived from the same records each project's own week is derived from (#536).
# There is no portfolio-work table, no completion flag, no owner, no cadence,
# and no deferral of its own, so this route writes nothing at all: a project
# row changes because a decision, plan, source, or template changed.
#
# The page's own measurement events go through the #558 contract rather than a
# frontend-request receipt, because a receipt is bound to one project and this
# reading is bound to none.


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio(
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Every adopted project this coordinator holds, each shown exactly once."""

    web_session = auth.load_session(request, session)
    # The cross-project reading (#537) is partitioned by the same rule: every
    # project this coordinator is actively on, and nothing else (#531).
    access.open_member_project_partition(
        session, principal_subject=principal.subject
    )
    reading = read_portfolio(
        session, principal_subject=principal.subject, as_of=clock()
    )
    waiting = (reading.waiting or (None,))[0]
    response = TEMPLATES.TemplateResponse(
        request,
        "portfolio.html",
        {
            "portfolio": reading,
            # Where the skip link goes, and nothing else: the first project
            # still holding work. Reading this page moves no focus at all, so
            # no element carries `autofocus` and the link is offered only when
            # there is somewhere worth skipping to.
            "skip_to": waiting.slug if waiting is not None else "",
            "cutoff": reading.cutoff.date().isoformat(),
            "email": web_session.email_normalized if web_session else "",
        },
    )
    # The presentation itself, with the state derived for each project shown.
    # Nothing further is emitted for a project that was shown and left alone,
    # which is what makes the absence of a `project_selection` naming it
    # evidence that it cost no click.
    emit_portfolio_reading(reading, binding=binding_for_session(session))
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
    clock=Depends(get_review_clock),
    came_from: str = Query("", alias="from"),
):
    """The coordinator's short, project-language entry point.

    An adopted-baseline project opens on its own ordered week instead (#536).
    ADR-0085 amends only the adopted-project presentation, so a legacy project
    keeps ADR-0035's item-per-record Work List below, unchanged.
    """
    project = _project(session, slug, principal)
    if is_adopted_baseline(session, project.id):
        now = clock()
        if came_from == PORTFOLIO:
            # A project opened *from* the portfolio is the one act that page
            # can produce (#537, under the #558 contract). It is recorded here
            # rather than on the portfolio because being listed is not being
            # opened, and only this request proves the click happened.
            standing = derive_standings(
                session, projects=(project,), as_of=now
            )[0]
            emit_project_selection(
                standing, principal_subject=principal.subject, at=now, binding=binding_for_session(session)
            )
        return _project_workflow_response(
            request, project, principal, session, now=now
        )
    _refuse_legacy_project_under_the_boundary(request, project)
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
            # One sentence per Attention Reason, from the one place they are
            # minted; an unknown code raises rather than rendering a blank row.
            "reasons": tuple(
                attention_reason_sentence(code)
                for code in item.attention_reason_codes
            ),
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
        request=request,
        response=response,
        subject=FrontendRequestSubject(project_id=project.id),
        request_fields=request.query_params,
    )
    session.commit()
    return response


# --- The read-only record and history investigation view (#642) ------------
#
# "What does this Utility Conflict say today, what did it say in July, and who
# changed it" had no home: #536's ordered week is a work surface and the source
# register lists deliveries. This is the reading half, and it is *only* a
# reading.
#
# Two properties hold it in place. It exposes one method, GET, so there is no
# route here that can accept, resolve, correct, refuse, or authorize anything —
# every one of those acts keeps the single home it already has, and this page
# links to that home rather than growing a second door (ADR-0085). And it
# writes nothing at all, not even a frontend-request receipt: looking at the
# record is not an act, and a receipt for having looked would be the one row
# this surface could be accused of adding.
#
# Authorization is the project's plain read boundary. `_project` without a
# designation is exactly that: whoever may read the project may read its
# history, and a non-member gets the same 404 as a missing project. It is
# deliberately not `access.COORDINATION`, which #537 uses to decide whose
# *work* a project is; reading history is not coordinating.


@app.get("/record/{slug}", response_class=HTMLResponse)
def record_history_screen(
    request: Request,
    slug: str,
    conflict: str = "",
    source: str = "",
    field: str = "",
    revision: str = "",
    audit_before: int | None = None,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One adopted project's accepted record, its sources, and its history.

    ``audit_before`` pages the audit trail backwards. It is a reading
    parameter and not a search term: it selects which page of the one capped
    section is shown, changes the URL and nothing else, and every other
    section answers the same whatever it says.
    """

    project = _project(session, slug, principal)
    terms = readable_terms(
        conflict=conflict, source=source, field=field, revision=revision
    )
    refused_revision: int | None = None
    try:
        history = read_record_history(
            session, project_id=project.id, terms=terms, audit_before=audit_before
        )
    except UnknownRevision:
        # A revision this project does not hold is answered, not guessed at:
        # the record is shown as it stands now and the page says why.
        refused_revision = terms.revision
        terms = replace(terms, revision=None)
        history = read_record_history(
            session, project_id=project.id, terms=terms, audit_before=audit_before
        )
    return TEMPLATES.TemplateResponse(
        request,
        "record_history.html",
        {
            "project": project,
            "history": history,
            "refused_revision": refused_revision,
            # The search this page is answering, as a query string, so the
            # one capped section can offer its older entries without dropping
            # the question the rest of the page is asking (#830). The path is
            # written in the template beside it rather than composed here: a
            # link whose whole target is an expression is one
            # `tests/test_manifest_page_links.py` cannot read.
            "audit_query": urlencode(
                {
                    "conflict": history.terms.conflict,
                    "source": history.terms.source,
                    "field": history.terms.field,
                    "revision": history.terms.revision or "",
                }
            ),
            # Exactly one region carries `autofocus`: the answer once one has
            # been asked for, and the question itself before that.
            "focus": "values" if history.terms.any_term else "search",
        },
    )



# --- The source-revision review screen (#527) -----------------------------
#
# One authoritative source revision is one bounded Work List item. Every
# actionable item of the project is listed with its own project-language
# subject and its own accounting; exactly one is opened at a time, and only the
# opened one carries decision controls, which is how ADR-0085's "actionable in
# exactly one packet" survives contact with a screen. Saving goes through
# #526's atomic packet command with the coordinator's explicit child set;
# nothing here writes.


def _review_context(
    session: Session,
    project: Project,
    *,
    now: datetime,
    opened_key: str,
    selected: list[int] | None = None,
    answers: dict | None = None,
    saved: dict | None = None,
    refusal: dict | None = None,
    errors: tuple = (),
    judgment: dict | None = None,
) -> dict:
    reading = read_review_items(session, project_id=project.id, as_of=now)
    binding = binding_for_session(session)
    opened = reading.item(opened_key) if opened_key else None
    landing = opened or (reading.items[0] if reading.items else None)
    focus = ui_primitives.focus_target(
        refused=refusal is not None, errors=errors, saved=saved is not None
    )
    views = []
    for item in reading.items:
        is_open = opened is not None and item.item_key == opened.item_key
        shown = select_children(item, selected) if is_open else item
        views.append(
            {
                "item": shown,
                "opened": is_open,
                "id": (
                    ui_primitives.FOCUS_IDS["item"]
                    if landing is not None and item.item_key == landing.item_key
                    else f"item-{item.ordinal}"
                ),
                "guidance": _review_guidance(item),
                "counts": _review_counts(shown),
                "answers": (answers or {}) if is_open else {},
                # The pilot's interrupting-packet sample is exactly the items
                # Corridor placed at "Must handle before this issue"; the level
                # is read from the item rather than derived a second time here.
                "interrupting": item.consequence == MUST_HANDLE,
                "open_url": (
                    f"/review/{project.slug}?item={quote(item.item_key, safe='')}"
                ),
            }
        )
    return {
        "project": project,
        "items": tuple(views),
        "reading": reading,
        "binding": binding,
        "opened": opened,
        "focus": focus,
        "errors": tuple(errors),
        "saved": saved,
        "refusal": refusal,
        "defer_until": "",
        "focused_outcomes": _FOCUSED_OUTCOME_LABELS,
        "leave_open": LEAVE_OPEN,
        # #846: the measurement controls belong to the pinned pilot
        # configuration, and to nothing else. Every other deployment renders a
        # review screen with no measurement on it at all.
        "judgment_offered": collection_is_pinned(binding),
        "judgment_choices": JUDGMENT_CHOICES,
        "judgment_answer": judgment or {},
    }


def _review_guidance(item) -> str:
    """Why this item is in front of the coordinator, in one sentence."""

    sentences = list(item.attention_sentences)
    if item.held_out_words:
        sentences.append(
            f"it is held out of its source revision's batch because "
            f"{item.held_out_words}"
        )
    else:
        # A focused item is not held out of anything; what it needs said is why
        # the rule keyed it the way it did (#528, ADR-0085).
        sentences.append(item.key_words)
    if not sentences:
        return ""
    joined = "; ".join(sentences)
    return f"{joined[0].upper()}{joined[1:]}."


# ADR-0085's four primary decisions and its secondary dated Defer, in the
# coordinator's own words. The tokens are #526's own constants rather than
# literals spelled again here, so there is no second vocabulary to drift from
# the command's.
_FOCUSED_OUTCOME_LABELS: tuple[tuple[str, str], ...] = (
    (LEAVE_OPEN, "Leave open — decide this later"),
    (APPLY, "Apply this source's value"),
    (KEEP_CURRENT, "Keep current — the accepted value stands"),
    (EDIT_AND_APPLY, "Apply another source's value instead"),
    (NEEDS_COORDINATION, "Needs coordination — someone owes an answer"),
    (DEFER, "Defer until a date"),
)


def _review_counts(item) -> tuple[tuple[str, object], ...]:
    """The item's own accounting, as terms and values a screen reader reads."""

    if item.grouping_key_kind == SHARED_COMMITMENT:
        return (
            ("Utility Conflicts in this commitment", len(item.scope_subjects)),
            ("Changes to answer here", item.child_count),
            ("Changes ready to apply", item.ready_count),
            ("Answered on their own item", item.held_out_count),
            ("Fields affected", ", ".join(item.field_names) or "none"),
        )
    if item.focused:
        position = item.accepted_positions[0] if item.accepted_positions else None
        return (
            ("Utility Conflict", position.subject_name if position else "not recorded"),
            ("Field in question", position.field_name if position else "not recorded"),
            (
                "What the record says today",
                position.text if position else "not recorded",
            ),
            # A focused item is no longer always several sources disagreeing:
            # since #659 a single source held out of its batch is focused too,
            # and "sources that answer it differently" reads as 1 for a change
            # nothing disagrees with. The count is the same number either way;
            # only the sentence naming it changes.
            (
                "Sources answering it"
                if item.child_count == 1
                else "Sources that answer it differently",
                item.child_count,
            ),
            (
                "Whether its value could be applied now"
                if item.child_count == 1
                else "Sources whose value could be applied now",
                item.ready_count,
            ),
        )
    return (
        ("Source revision", f"{item.source_family} {item.source_revision}"),
        ("Values this revision left unchanged", item.unchanged_count),
        ("Changes ready for decision", item.ready_count),
        ("Selected now", item.selected_count),
        ("Held out of this batch", item.held_out_count),
        ("Utility Conflicts affected", len(item.subject_names)),
        ("Fields affected", ", ".join(item.field_names) or "none"),
    )


@app.get("/review/{slug}", response_class=HTMLResponse)
def review_source_changes(
    request: Request,
    slug: str,
    item: str = "",
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """List every actionable item, and open the one the coordinator asked for."""

    project = _project(session, slug, principal)
    now = clock()
    context = _review_context(session, project, now=now, opened_key=item)
    return _render_review_response(request, context, principal=principal, record_opening=True)


@app.get("/review/{slug}/source")
def open_review_source(
    slug: str, item: str, delta_id: int, source_row_id: int, role: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session), clock=Depends(get_review_clock),
):
    """Open the exact source link the authorized packet reading already offered.

    No URL comes from the query: resolve the retained child and its immutable
    baseline source row inside the project read boundary. This still works
    after the child is resolved or superseded. Inline source presentation is
    not counted as a click, and the event carries no external URL or source text.
    """
    project = _project(session, slug, principal)
    now = clock()
    child = session.get(ProposedDelta, delta_id)
    source = session.get(BaselineSourceRow, source_row_id)
    if (child is None or source is None or child.project_id != project.id or source.project_id != project.id
            or child.target_subject_identity != source.record_subject_key
            or role not in {"external_system_id", "source_url"}):
        raise HTTPException(404, "no such source link in this reading")
    url = (getattr(source, role) or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise HTTPException(404, "no followable source link") from None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(404, "no followable source link")
    emit_presentation(EventFamily.EVIDENCE_OPENING, project_id=project.id,
                      principal_subject=principal.subject, at=now, binding=binding_for_session(session),
                      item_key=item, delta_id=delta_id, source_row_id=source_row_id, link_role=role)
    return RedirectResponse(url, status_code=303)


# --- The pilot's contemporaneous triage observation (#846) -----------------
#
# The contract asks the coordinator to mark each interrupting packet as
# genuinely required *at the moment of triage* and to log the minutes spent
# rebuilding context outside Corridor for it then. Both Save screens carry the
# question, because all four decisions -- Apply, Keep current, Needs
# coordination and Defer -- are that moment.
#
# Two things about the shape. The occurrence comes back from the page rather
# than from a fresh reading: the judgment is of the packet that was displayed,
# at the consequence level and rule version it was displayed at, and re-deriving
# those after the Save would record a judgment of whatever the packet became.
# And the collection happens after the decision has committed, through a seam
# that raises nothing, because measurement observes a project decision and
# never decides one.


def _triage_form(
    judgment: str, minutes: str, cutoff: str, consequence: str, rule_version: str
) -> dict[str, str]:
    """What the rendered page sent back: the answers, and the occurrence shown."""

    return {
        "judgment": judgment,
        "minutes": minutes,
        "cutoff": cutoff,
        "consequence": consequence,
        "rule_version": rule_version,
    }


def _observe_triage(project: Project, item, *, principal, binding, form) -> None:
    """Collect this triage act's observations; a failure changes nothing saved."""

    collect_triage_observations(
        project_id=project.id,
        actor=principal.subject,
        occurrence=TriageOccurrence(
            item_key=item.item_key,
            cutoff=form["cutoff"],
            consequence_level=form["consequence"],
            consequence_rule_version=form["rule_version"],
        ),
        binding=screen_binding(item, binding),
        judgment=judgment_from_form(form["judgment"]),
        minutes=minutes_from_form(form["minutes"]),
    )


# --- What both review Save screens say when they refuse --------------------
#
# `save_source_changes` and `save_focused_answers` built nine
# `{"heading", "detail", "rows"}` dicts between them, and three of the
# sentences were written twice: an item that left the reading, a declared
# `ReviewScreenRefused`, and a change that moved under the reading while the
# coordinator was answering. Two copies of a sentence is two places to edit and
# one of them gets missed, so the words live here once and each handler chooses
# which refusal it is presenting.

REVIEW_REFUSAL_HEADING = "Nothing was saved"


def _review_refusal(
    detail: str,
    rows: Iterable[dict[str, str]] = (),
    *,
    heading: str = REVIEW_REFUSAL_HEADING,
) -> dict:
    """One refusal the review screen renders: a heading, a sentence, its rows."""

    return {"heading": heading, "detail": detail, "rows": tuple(rows)}


def _item_left_the_reading() -> dict:
    """The opened item is gone: a newer source or an earlier decision closed it."""

    return _review_refusal(
        "A newer source or an earlier decision changed what is open, "
        "so nothing was saved. Open the item again from the list below.",
        heading="This item is no longer part of the reading",
    )


def _review_screen_refused(exc: ReviewScreenRefused) -> dict:
    """The request builder refused. It wrote the sentence, so it is quoted whole."""

    return _review_refusal(exc.customer_sentence)


def _a_change_moved_under_the_reading(result, *, chosen: str) -> dict:
    """The atomic packet refused: one change moved, so the whole Save refused.

    `chosen` is how the calling screen names what the coordinator did -- a
    change is *selected* on the source-revision screen and *answered* on the
    focused one -- and is the only word the two sentences differ by.
    """

    return _review_refusal(
        f"One of the changes you {chosen} moved under this reading, so the "
        "whole save was refused and the project record is unchanged.",
        (
            {
                "subject": (
                    f"{refused.subject_identity or 'a proposed change'}"
                    f" — {refused.field or 'the whole row'}"
                ),
                "detail": refused.detail,
            }
            for refused in result.refusals
        ),
    )


@app.post("/review/{slug}", response_class=HTMLResponse)
def save_source_changes(
    request: Request,
    slug: str,
    item_key: str = Form(...),
    outcome: str = Form(...),
    child: list[str] = Form(default=[]),
    defer_until: str = Form(""),
    judgment: str = Form(""),
    judgment_minutes: str = Form(""),
    judgment_cutoff: str = Form(""),
    judgment_consequence: str = Form(""),
    judgment_rule_version: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Decide the selected children of one item, completely or not at all (#526)."""

    project = _project(session, slug, principal, designation=access.COORDINATION)
    now = clock()
    judgment_form = _triage_form(
        judgment, judgment_minutes, judgment_cutoff, judgment_consequence,
        judgment_rule_version,
    )
    selected = [int(value) for value in child if value.strip().isdigit()]
    reading = read_review_items(session, project_id=project.id, as_of=now)
    item = reading.item(item_key)
    if item is None:
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key="",
            selected=selected,
            refusal=_item_left_the_reading(),
            status_code=409,
        )

    return_date = _optional_form_date(defer_until)
    errors = []
    if outcome == "defer" and return_date is None:
        errors.append(
            ui_primitives.FieldError(
                field_id=f"{ui_primitives.FOCUS_IDS['item']}-defer-until",
                message="Give the date this item should come back before deferring it.",
            )
        )
    if not selected:
        errors.append(
            ui_primitives.FieldError(
                field_id=f"child-{item.children[0].delta_id}" if item.children else "child",
                message="Select at least one change before saving.",
            )
        )
    if errors:
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            selected=selected,
            errors=tuple(errors),
            judgment=judgment_form,
            status_code=400,
        )

    offered = {child.delta_id for child in item.children}
    missing = [value for value in selected if value not in offered]
    if missing:
        # The reading moved under the coordinator between opening and saving.
        # Nothing is written, the selection survives, and each change that left
        # says why it left (#494's deterministic exits).
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            selected=selected,
            refusal=_review_refusal(
                "Part of what you selected is no longer offered on this item, "
                "so the whole save was refused and the project record is "
                "unchanged.",
                (
                    {
                        "subject": f"Proposed change {value}",
                        "detail": reading.standing_sentence(value),
                    }
                    for value in missing
                ),
            ),
            judgment=judgment_form,
            status_code=409,
        )

    try:
        act = packet_request(
            reading,
            select_children(item, selected),
            outcome=outcome,
            principal=principal,
            decided_at=now,
            delta_ids=selected,
            deferred_until=(
                datetime.combine(return_date, time(0, 0), tzinfo=timezone.utc)
                if return_date is not None
                else None
            ),
            deferral_reason=None,
        )
    except ReviewScreenRefused as exc:
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            selected=selected,
            refusal=_review_screen_refused(exc),
            judgment=judgment_form,
            status_code=409,
        )

    binding = binding_for_session(session)
    result = resolve_review_packet(session, act, binding=binding)
    if result.status != "saved":
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            selected=[row.delta_id for row in result.preserved_selections],
            refusal=_a_change_moved_under_the_reading(result, chosen="selected"),
            judgment=judgment_form,
            status_code=409,
        )

    session.commit()
    _observe_triage(
        project, item, principal=principal, binding=binding, form=judgment_form
    )
    return _review_render(
        request,
        session,
        project,
        principal=principal,
        now=now,
        opened_key="",
        saved={
            "heading": _review_saved_heading(outcome, len(act.children)),
            "detail": (
                "Project record revision "
                f"{result.revision_id} records each decision separately."
                if result.revision_id is not None
                else (
                    "This is Work List scheduling: the proposed changes stay open "
                    "and the accepted record is unchanged."
                )
            ),
            # The act, not only the revision: a packet of dated Defers records
            # no revision at all (ADR-0084), and the receipt is what every save
            # has (#834).
            "receipt_id": result.receipt_id,
        },
        status_code=200,
    )


# --- The focused cross-source screen (#528) --------------------------------
#
# A coordination question and a shared commitment are answered child by child:
# one source's value may become the accepted one while another's is kept out,
# and both halves are the same act. The form carries one answer per child in
# parallel lists, positionally aligned with the hidden child identity, so every
# control has a static name and nothing about the shape depends on script.
# Everything it can refuse is refused by `focused_request` before a single row
# is read for writing, and each refusal names the control that holds it.


@app.post("/review/{slug}/answers", response_class=HTMLResponse)
def save_focused_answers(
    request: Request,
    slug: str,
    item_key: str = Form(...),
    answer_delta: list[str] = Form(default=[]),
    answer_outcome: list[str] = Form(default=[]),
    answer_source: list[str] = Form(default=[]),
    answer_question: list[str] = Form(default=[]),
    answer_person: list[str] = Form(default=[]),
    answer_organization: list[str] = Form(default=[]),
    answer_return: list[str] = Form(default=[]),
    judgment: str = Form(""),
    judgment_minutes: str = Form(""),
    judgment_cutoff: str = Form(""),
    judgment_consequence: str = Form(""),
    judgment_rule_version: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Answer one cross-source item child by child, completely or not at all."""

    project = _project(session, slug, principal, designation=access.COORDINATION)
    now = clock()
    judgment_form = _triage_form(
        judgment, judgment_minutes, judgment_cutoff, judgment_consequence,
        judgment_rule_version,
    )
    lists = (
        answer_outcome,
        answer_source,
        answer_question,
        answer_person,
        answer_organization,
        answer_return,
    )
    if any(len(column) != len(answer_delta) for column in lists):
        # Every child renders every control, so the columns arrive the same
        # length or the submission is not one this screen produced.
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            refusal=_review_refusal(
                "The answers did not arrive as this screen sends them, so "
                "nothing was written. Open the item again and answer it."
            ),
            judgment=judgment_form,
            status_code=400,
        )

    answers = []
    typed: dict[int, dict] = {}
    for index, raw in enumerate(answer_delta):
        if not raw.strip().isdigit():
            continue
        delta_id = int(raw)
        chosen = answer_source[index].strip()
        typed[delta_id] = {
            "outcome": answer_outcome[index],
            "source": chosen,
            "question": answer_question[index],
            "person": answer_person[index],
            "organization": answer_organization[index],
            "return_date": answer_return[index],
        }
        return_date = _optional_form_date(answer_return[index])
        answers.append(
            FocusedAnswer(
                delta_id=delta_id,
                outcome=answer_outcome[index],
                question=answer_question[index],
                responsible_principal=answer_person[index],
                responsible_organization=answer_organization[index],
                return_date=(
                    datetime.combine(return_date, time(0, 0), tzinfo=timezone.utc)
                    if return_date is not None
                    else None
                ),
                apply_fact_from_delta_id=int(chosen) if chosen.isdigit() else None,
            )
        )

    reading = read_review_items(session, project_id=project.id, as_of=now)
    item = reading.item(item_key)
    if item is None:
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key="",
            answers=typed,
            refusal=_item_left_the_reading(),
            status_code=409,
        )

    try:
        act = focused_request(
            reading,
            item,
            principal=principal,
            decided_at=now,
            answers=answers,
        )
    except ReviewScreenRefused as exc:
        if exc.delta_id is not None and exc.control is not None:
            return _review_render(
                request,
                session,
                project,
                principal=principal,
                now=now,
                opened_key=item_key,
                answers=typed,
                errors=(
                    ui_primitives.FieldError(
                        field_id=f"answer-{exc.control}-{exc.delta_id}",
                        message=str(exc),
                    ),
                ),
                judgment=judgment_form,
                status_code=400,
            )
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            answers=typed,
            refusal=_review_screen_refused(exc),
            judgment=judgment_form,
            status_code=409,
        )

    binding = binding_for_session(session)
    result = resolve_review_packet(session, act, binding=binding)
    if result.status != "saved":
        return _review_render(
            request,
            session,
            project,
            principal=principal,
            now=now,
            opened_key=item_key,
            answers=typed,
            refusal=_a_change_moved_under_the_reading(result, chosen="answered"),
            judgment=judgment_form,
            status_code=409,
        )

    session.commit()
    _observe_triage(
        project, item, principal=principal, binding=binding, form=judgment_form
    )
    answered = len(act.children)
    return _review_render(
        request,
        session,
        project,
        principal=principal,
        now=now,
        opened_key="",
        saved={
            "heading": (
                f"Answered {answered} "
                f"{'change' if answered == 1 else 'changes'}"
            ),
            "detail": (
                "Project record revision "
                f"{result.revision_id} records each decision separately."
                if result.revision_id is not None
                else (
                    "This is Work List scheduling: the proposed changes stay open "
                    "and the accepted record is unchanged."
                )
            ),
            # The act, not only the revision: a packet of dated Defers records
            # no revision at all (ADR-0084), and the receipt is what every save
            # has (#834).
            "receipt_id": result.receipt_id,
        },
        status_code=200,
    )


def _review_saved_heading(outcome: str, count: int) -> str:
    noun = "change" if count == 1 else "changes"
    return {
        "apply": f"Applied {count} {noun}",
        "keep_current": f"Kept the current value for {count} {noun}",
        "defer": f"Deferred {count} {noun}",
    }.get(outcome, f"Saved {count} {noun}")


def _review_render(
    request: Request,
    session: Session,
    project: Project,
    *,
    principal: HumanPrincipal,
    now: datetime,
    opened_key: str,
    selected: list[int] | None = None,
    answers: dict | None = None,
    saved: dict | None = None,
    refusal: dict | None = None,
    errors: tuple = (),
    judgment: dict | None = None,
    status_code: int = 200,
):
    """Re-read after a write, so what the coordinator sees is what now stands."""

    context = _review_context(
        session,
        project,
        now=now,
        opened_key=opened_key,
        selected=selected,
        answers=answers,
        saved=saved,
        refusal=refusal,
        errors=errors,
        judgment=judgment,
    )
    return _render_review_response(request, context, principal=principal, status_code=status_code)


def _render_review_response(request, context, *, principal, status_code=200, record_opening=False):
    """Record every actual presentation, including Save and refusal responses."""
    response = TEMPLATES.TemplateResponse(request, "review.html", context, status_code=status_code)
    binding = context["binding"]
    for view in context["items"]:
        emit_packet_surfacing(context["reading"], view["item"],
                              principal_subject=principal.subject, binding=binding)
    if record_opening and context["opened"] is not None:
        emit_packet_opening(context["reading"], context["opened"],
                           principal_subject=principal.subject, binding=binding)
    return response


# --- One recorded decision, and Undo (#834) --------------------------------
#
# The saved result used to name a count and a Project Record revision with no
# link, and a packet of dated Defers writes no revision at all (ADR-0084), so
# there was nothing to link to at all for that act. The receipt is the one
# thing every act has, so the saved result links here and every decided
# child's outcome is read from the act itself rather than searched for in
# record history.
#
# Reading the act is the project's plain read boundary, exactly as the review
# reading is. Undo is a Project Record act, so it carries `access.COORDINATION`
# like every other write on this screen, and it decides nothing itself:
# `reverse_review_packet` owns whether an act may be compensated, and a refusal
# is rendered in that command's own words.


def _packet_receipt_facts(receipt) -> tuple[tuple[str, object], ...]:
    """The act's own terms and values, as a screen reader reads them."""

    return (
        ("Decided by", receipt.decided_by),
        ("Decided on", receipt.decided_at),
        ("Changes decided", len(receipt.children)),
        (
            "Project Record revision",
            receipt.revision_id if receipt.revision_id is not None else "none written",
        ),
    )


def _packet_receipt_response(
    request: Request,
    project: Project,
    receipt,
    *,
    undone: dict | None = None,
    refusal: dict | None = None,
    status_code: int = 200,
):
    return TEMPLATES.TemplateResponse(
        request,
        "packet_receipt.html",
        {
            "project": project,
            "receipt": receipt,
            "receipt_facts": _packet_receipt_facts(receipt),
            "undone": undone,
            "refusal": refusal,
            # Reading one recorded act performs no act, so nothing on it takes
            # focus: moving focus on load carries a screen-reader user past the
            # heading that says what they are looking at
            # (`docs/accessibility-acceptance-checklist.md`, item 4). A
            # response to Undo has an outcome to announce and announces it.
            "focus": (
                ui_primitives.focus_target(
                    refused=refusal is not None, saved=undone is not None
                )
                if undone is not None or refusal is not None
                else ""
            ),
        },
        status_code=status_code,
    )


def _packet_receipt(session: Session, project: Project, receipt_id: int):
    receipt = read_packet_receipt(
        session, project_id=project.id, receipt_id=receipt_id
    )
    if receipt is None:
        raise HTTPException(404, "no such recorded decision for this project")
    return receipt


@app.get("/review/{slug}/packet/{receipt_id}", response_class=HTMLResponse)
def packet_receipt_screen(
    request: Request,
    slug: str,
    receipt_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One recorded decision and what each change it decided became."""

    project = _project(session, slug, principal)
    return _packet_receipt_response(
        request, project, _packet_receipt(session, project, receipt_id)
    )


@app.post("/review/{slug}/packet/{receipt_id}/undo", response_class=HTMLResponse)
def undo_packet_decision(
    request: Request,
    slug: str,
    receipt_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    clock=Depends(get_review_clock),
):
    """Compensate for exactly the decision this receipt records (#526, ADR-0035)."""

    project = _project(session, slug, principal, designation=access.COORDINATION)
    _packet_receipt(session, project, receipt_id)
    result = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=receipt_id,
        principal=principal,
        reversed_at=clock(),
        # The act's own identity is the key: one receipt can be compensated at
        # most once, so a resubmitted form returns the compensation that was
        # already recorded rather than refusing a second one.
        idempotency_key=f"undo:packet:{receipt_id}",
    )
    if result.status != REVERSED:
        return _packet_receipt_response(
            request,
            project,
            _packet_receipt(session, project, receipt_id),
            refusal={
                "heading": "This decision was not undone",
                "detail": refusal_words(result.refusal),
            },
            status_code=409,
        )
    session.commit()
    return _packet_receipt_response(
        request,
        project,
        _packet_receipt(session, project, receipt_id),
        undone={
            "heading": "This decision was undone",
            "detail": (
                "Project record revision "
                f"{result.revision_id} records the compensation; the original "
                "decision and everything it recorded stay in history."
                if result.revision_id is not None
                else (
                    "The deferred changes are back in immediate work; nothing "
                    "was deleted and the accepted record is unchanged."
                )
            ),
        },
    )


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
    """Present the one Extracted Proposal this lane opens, and why it is open."""

    project = _project(session, slug, principal)
    try:
        reading = queue_view(
            session,
            project=project,
            lane=lane,
            historical_document_id=historical_document_id,
            cohort_receipt_id=cohort_receipt_id,
            event_cohort_receipt_id=event_cohort_receipt_id,
            candidate_id=candidate_id,
            coordinate=coordinate,
            summary=bool(summary),
        )
    except LaneManifestRequired as refusal:
        raise HTTPException(400, str(refusal))
    except NoSuchLaneManifest as refusal:
        raise HTTPException(404, str(refusal))
    if reading.redirect_to is not None:
        return RedirectResponse(reading.redirect_to, status_code=303)
    if reading.candidate is not None and reading.candidate.state == "pending":
        observe_shadow_review(
            session, reading.candidate.id, boundary="start", principal=principal
        )
    response = TEMPLATES.TemplateResponse(
        request, reading.template, {"project": project, "reading": reading}
    )
    if reading.candidate is None:
        return response
    record_frontend_request(
        session,
        principal=principal,
        route_name="queue",
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            candidate_id=reading.candidate.id,
            dependency_id=(
                reading.coordinate_dependency.id
                if reading.coordinate_dependency is not None
                else None
            ),
        ),
        request_fields=request.query_params,
    )
    session.commit()
    return response


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
    now: Callable[[], datetime] = Depends(get_review_clock),
):
    """The Constraint Log, read at the declared review instant like its siblings.

    The clock is the same ``get_review_clock`` seam eight other routes already
    take, rather than ``date.today()`` inside the body: the alert this page
    prints beside a Next Action past its date is then a fact a test can state
    instead of one that depends on the day the suite runs.
    """

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
            "today": now().date(),
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
    return _dependency_detail_response(
        request, session, project, dependency_id, return_to
    )


def _dependency_detail_response(
    request: Request,
    session: Session,
    project: Project,
    dependency_id: int,
    return_to: str,
    *,
    plan_error: str | None = None,
    status_code: int = 200,
):
    """Render one Constraint's record, and record nothing else about it."""

    try:
        reading = dependency_view(
            session, project_id=project.id, dependency_id=dependency_id
        )
    except NoSuchConstraint as refusal:
        raise HTTPException(404, str(refusal))
    try:
        safe_return = (
            safe_cohort_return(
                return_to,
                project=project,
                dependency_id=dependency_id,
                session=session,
            )
            if return_to
            else ""
        )
    except ReturnContextRefused as refusal:
        raise HTTPException(400, str(refusal))
    return TEMPLATES.TemplateResponse(
        request,
        "dependency.html",
        {
            "project": project,
            "reading": reading,
            "return_to": safe_return,
            "plan_error": plan_error,
        },
        status_code=status_code,
    )


def _verbal_timing_from_form(
    precision: str,
    committed_date: date | None,
    committed_month: str,
    timing_text: str,
) -> StatementTiming:
    """Build the stated timing exactly at the precision the recorder chose.

    The form never invents an exact day for a month or approximate promise:
    each precision keeps only the wording and calendar bounds it actually
    supports (ADR-0036).
    """
    words = timing_text.strip()
    if precision == "day":
        if committed_date is None:
            raise HTTPException(400, "an exact-day promise needs the day the party gave")
        return StatementTiming.day(words or committed_date.isoformat(), committed_date)
    if precision == "month":
        try:
            month_start = date.fromisoformat(f"{committed_month.strip()}-01")
        except ValueError as exc:
            raise HTTPException(400, "a month promise needs a YYYY-MM month") from exc
        return StatementTiming.month(
            words or month_start.strftime("%B %Y"),
            month_start.year,
            month_start.month,
        )
    if precision == "approximate":
        if not words:
            raise HTTPException(
                400, "an approximate promise needs the words the party used"
            )
        return StatementTiming.approximate(words)
    raise HTTPException(400, f"unknown timing precision {precision!r}")


def _verbal_scope_from_form(
    scope_mode: str,
    dependency: Dependency,
    scope_dependency_ids: list[int],
) -> StatementScope:
    """Build the explicit scope decision the recorder chose, never inferred."""
    if scope_mode == "this":
        return StatementScope.selected((dependency.id,))
    if scope_mode == "selected":
        if not scope_dependency_ids:
            raise HTTPException(400, "choose at least one Constraint, or a wider scope")
        return StatementScope.selected(tuple(scope_dependency_ids))
    if scope_mode == "all_active":
        return StatementScope.all_active()
    if scope_mode == "unknown":
        return StatementScope.unknown()
    raise HTTPException(400, f"unknown scope choice {scope_mode!r}")


@app.post("/ledger/{slug}/{dependency_id}/verbal")
def record_dependency_verbal(
    slug: str,
    dependency_id: int,
    stated_party: str = Form(...),
    description: str = Form(...),
    conversation_date: date = Form(...),
    timing_precision: str = Form("day"),
    committed_date: date | None = Form(None),
    committed_month: str = Form(""),
    timing_text: str = Form(""),
    scope_mode: str = Form("this"),
    scope_dependency_ids: list[int] = Form(default_factory=list),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record one attributable phone statement at its stated precision/scope."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = _project_dependency(session, project, dependency_id)
    if dependency.external_org_id is None:
        raise HTTPException(400, "this record has no resolved External Party")
    new_timing = _verbal_timing_from_form(
        timing_precision, committed_date, committed_month, timing_text
    )
    scope = _verbal_scope_from_form(scope_mode, dependency, scope_dependency_ids)
    record_verbal_statement(
        session,
        project_id=project.id,
        external_org_id=dependency.external_org_id,
        stated_party=stated_party,
        description=description,
        conversation_date=conversation_date,
        new_timing=new_timing,
        scope=scope,
        principal=principal,
    )
    session.commit()
    return RedirectResponse(
        f"/ledger/{slug}/{dependency_id}", status_code=303
    )


@app.post("/ledger/{slug}/{dependency_id}/verbal-change")
def record_dependency_verbal_change(
    slug: str,
    dependency_id: int,
    commitment_lineage_id: int = Form(...),
    stated_party: str = Form(...),
    description: str = Form(...),
    conversation_date: date = Form(...),
    timing_precision: str = Form("day"),
    committed_date: date | None = Form(None),
    committed_month: str = Form(""),
    timing_text: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record a stated change to what one existing commitment promised."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    lineage = session.get(CommitmentLineage, commitment_lineage_id)
    if lineage is None or lineage.project_id != project.id:
        raise HTTPException(404, "no such commitment in this project")
    new_timing = _verbal_timing_from_form(
        timing_precision, committed_date, committed_month, timing_text
    )
    record_verbal_change(
        session,
        commitment_lineage_id=commitment_lineage_id,
        stated_party=stated_party,
        description=description,
        conversation_date=conversation_date,
        new_timing=new_timing,
        principal=principal,
    )
    session.commit()
    return RedirectResponse(
        f"/ledger/{slug}/{dependency_id}", status_code=303
    )


@app.post("/ledger/{slug}/{dependency_id}/verbal-scope")
def correct_dependency_verbal_scope(
    slug: str,
    dependency_id: int,
    statement_event_id: int = Form(...),
    expected_scope_decision_id: int = Form(...),
    scope_mode: str = Form(...),
    scope_dependency_ids: list[int] = Form(default_factory=list),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Correct which Constraints a recorded verbal applies to, preserving it."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = _project_dependency(session, project, dependency_id)
    event = session.get(DependencyEvent, statement_event_id)
    if event is None or event.project_id != project.id:
        raise HTTPException(404, "no such statement in this project")
    scope = _verbal_scope_from_form(scope_mode, dependency, scope_dependency_ids)
    correct_verbal_scope(
        session,
        event_id=statement_event_id,
        scope=scope,
        expected_scope_decision_id=expected_scope_decision_id,
        principal=principal,
    )
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
    settle_dispute(
        session,
        dependency_id,
        field_name,
        value=value.strip() or None,
        principal=principal,
        saw_claim_id=saw_claim_id,
    )
    session.commit()
    return RedirectResponse(
        f"/ledger/{slug}/{dependency_id}", status_code=303
    )


@app.post("/ledger/{slug}/{dependency_id}/clarify")
def clarify_dispute(
    request: Request,
    slug: str,
    dependency_id: int,
    field_name: str = Form(...),
    internal_owner_roster_entry_id: int = Form(...),
    next_action: str = Form(...),
    due_date: date | None = Form(None),
    due_date_unknown_reason: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Keep a source discrepancy open and record the coordinated follow-up."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    clarification = record_dispute_clarification(
        session,
        dependency_id,
        field_name,
        roster_entry_id=internal_owner_roster_entry_id,
        next_action=next_action,
        due_date=due_date,
        due_date_unknown_reason=due_date_unknown_reason.strip() or None,
        principal=principal,
    )
    response = RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="clarify_dispute",
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
            work_decision_id=clarification.next_action_decision_id,
        ),
        request_fields={
            "field_name": field_name,
            "internal_owner_roster_entry_id": str(internal_owner_roster_entry_id),
            "next_action": next_action,
            "due_date": due_date.isoformat() if due_date else "",
            "due_date_unknown_reason": due_date_unknown_reason,
        },
    )
    session.commit()
    return response


@app.post("/dependencies/{dependency_id}/plan")
def save_dependency_follow_up_plan(
    request: Request,
    dependency_id: int,
    slug: str = Form(...),
    internal_owner_roster_entry_id: str = Form(""),
    next_action: str = Form(...),
    action_due_date: str = Form(""),
    action_due_date_unknown_reason: str = Form(""),
    expected_internal_owner_decision_id: str = Form(""),
    expected_next_action_decision_id: str = Form(""),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """One roster-backed Save for the whole Follow-up Plan (#333, ADR-0035).

    The grouped rules — active-roster identity, structured Next Action, a
    date or its structured unknown reason, atomicity, and the stale check —
    belong to the Work Decision seam; this route carries the HTTP.  A stale
    concurrent Save re-renders the newer state with nothing applied.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    parsed_due_date = None
    if action_due_date.strip():
        try:
            parsed_due_date = date.fromisoformat(action_due_date.strip())
        except ValueError:
            raise HTTPException(400, "an Action Due Date must be a date")
    draft = FollowUpPlanDraft(
        dependency_id=dependency_id,
        internal_owner_roster_entry_id=_optional_form_id(
            internal_owner_roster_entry_id
        ),
        next_action=next_action,
        action_due_date=parsed_due_date,
        action_due_date_unknown_reason=(
            action_due_date_unknown_reason.strip() or None
        ),
        expected=FollowUpPlanPredecessors(
            internal_owner_decision_id=_optional_form_id(
                expected_internal_owner_decision_id
            ),
            next_action_decision_id=_optional_form_id(
                expected_next_action_decision_id
            ),
        ),
    )
    try:
        result = save_follow_up_plan(session, draft, principal=principal)
    except StaleFollowUpPlan as exc:
        return _dependency_detail_response(
            request,
            session,
            project,
            dependency_id,
            redirect_to,
            plan_error=str(exc),
            status_code=409,
        )
    response = RedirectResponse(return_location, status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="save_dependency_follow_up_plan",
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
            work_decision_id=(
                result.internal_owner_decision.id
                if result.internal_owner_decision is not None
                else result.next_action_decision.id
            ),
        ),
        request_fields={
            "slug": slug,
            "internal_owner_roster_entry_id": internal_owner_roster_entry_id,
            "next_action": next_action,
            "action_due_date": action_due_date,
            "action_due_date_unknown_reason": action_due_date_unknown_reason,
            "redirect_to": redirect_to,
        },
    )
    session.commit()
    return response


@app.post("/dependencies/{dependency_id}/plan/undo")
def undo_dependency_follow_up_plan(
    dependency_id: int,
    slug: str = Form(...),
    receipt_id: int = Form(...),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Append the grouped compensation for one immediately undoable plan Save."""
    project = _project(session, slug, principal, designation=access.COORDINATION)
    dependency = _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    receipt = session.get(FollowUpPlanReceipt, receipt_id)
    if receipt is None or receipt.dependency_id != dependency.id:
        raise HTTPException(404, "no such grouped plan Save for this constraint")
    undo_follow_up_plan(session, receipt_id, principal=principal)
    session.commit()
    return RedirectResponse(return_location, status_code=303)


def _optional_form_id(value: str) -> int | None:
    """A form's hidden identity: an integer, or None when the field is empty."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise HTTPException(400, "a form identity must be a whole number")


def _optional_form_date(value: str) -> date | None:
    """A form's date field: an ISO date, or None when the field is empty."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, "a date field must be a valid date")


EXPECTED_NEXT_ACTION_FIELD = "expected_next_action_decision_id"


async def _stated_next_action(request: Request) -> ExpectedNextAction:
    """Which Next Action this submission says its screen was showing.

    A ``Form`` parameter cannot answer this. FastAPI reads an empty form value
    as an absent one and substitutes the parameter's default, so a screen
    reporting that it saw no Next Action and a body that never carried the
    field at all arrive as the same ``None`` -- and ``None`` used to mean "do
    not check", which closed or deferred whichever action happened to be
    current. So the submission is read as it was sent.

    Every screen that closes or defers renders the hidden field, empty when
    the Constraint had no Next Action to show. A body without it did not come
    from one and cannot say what its coordinator was looking at, which is a
    malformed submission. An empty one came from a screen and says what it
    saw: nothing. That answer is compared like any other.
    """
    submitted = (await request.form()).get(EXPECTED_NEXT_ACTION_FIELD)
    if submitted is None:
        raise HTTPException(
            400, f"{EXPECTED_NEXT_ACTION_FIELD} must be submitted, even when empty"
        )
    return _optional_form_id(str(submitted))


@app.post("/dependencies/{dependency_id}/action/{outcome}")
def close_next_action(
    request: Request,
    dependency_id: int,
    outcome: Literal["complete", "cancel"],
    slug: str = Form(...),
    no_follow_up_reason: str = Form(""),
    cancellation_reason: str = Form(""),
    successor_action: str = Form(""),
    successor_due_date: str = Form(""),
    successor_due_date_unknown_reason: str = Form(""),
    note: str = Form(""),
    expected_next_action_decision_id: ExpectedNextAction = Depends(_stated_next_action),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Completion and cancellation are distinct decisions, never one button.

    On an open Constraint each requires exactly the permitted successor Next
    Action or a structured no-follow-up reason.  The closure binds to the exact
    action the coordinator saw, so a stale, repeated, or cross-project submit
    refuses without closing a different action (#334, ADR-0035/0038).
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    parsed_successor_due_date = _optional_form_date(successor_due_date)
    common = dict(
        successor_action=successor_action.strip() or None,
        successor_due_date=parsed_successor_due_date,
        successor_due_date_unknown_reason=(
            successor_due_date_unknown_reason.strip() or None
        ),
        no_follow_up_reason=no_follow_up_reason.strip() or None,
        note=note.strip() or None,
        expected_next_action_decision_id=expected_next_action_decision_id,
        permitted_successor_actions=FOLLOW_UP_NEXT_ACTION_CHOICES,
    )
    try:
        if outcome == "complete":
            complete_next_action(
                session, dependency_id, principal=principal, **common
            )
        else:
            cancel_next_action(
                session,
                dependency_id,
                principal=principal,
                cancellation_reason=cancellation_reason.strip() or None,
                **common,
            )
    except StaleNextAction as exc:
        return _dependency_detail_response(
            request, session, project, dependency_id, redirect_to,
            plan_error=str(exc), status_code=409,
        )
    session.commit()
    return RedirectResponse(return_location, status_code=303)


@app.post("/dependencies/{dependency_id}/defer")
def defer_dependency_action(
    request: Request,
    dependency_id: int,
    slug: str = Form(...),
    deferral_reason: str = Form(""),
    return_date: str = Form(""),
    expected_next_action_decision_id: ExpectedNextAction = Depends(_stated_next_action),
    redirect_to: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Defer immediate work with a structured reason and a stated return date.

    A deferral is a separate attributable decision, never a synonym for an
    unknown Action Due Date: without a return date the work stays immediate
    (#334, ADR-0035).
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    return_location = _safe_return(
        redirect_to, f"/ledger/{slug}/{dependency_id}"
    )
    parsed_return_date = _optional_form_date(return_date)
    if parsed_return_date is None:
        raise HTTPException(400, "a deferral needs a return date")
    try:
        defer_work(
            session,
            dependency_id,
            reason=deferral_reason.strip(),
            return_date=parsed_return_date,
            principal=principal,
            expected_next_action_decision_id=expected_next_action_decision_id,
        )
    except StaleNextAction as exc:
        return _dependency_detail_response(
            request, session, project, dependency_id, redirect_to,
            plan_error=str(exc), status_code=409,
        )
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
    if read_checklist(session, dependency_id).uses_standard_checklist:
        raise HTTPException(
            409,
            "this constraint uses the standard documentation checklist; "
            "a legacy sufficiency mark cannot bypass it",
        )
    try:
        mark_satisfies(session, dependency_id, link_id, principal=principal)
    except NoSuchEvidence as exc:
        raise HTTPException(404, str(exc))
    except UnverifiedEvidence as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.post("/dependencies/{dependency_id}/documentation/confirm-approval")
def confirm_documentation_approval(
    request: Request,
    dependency_id: int,
    slug: str = Form(...),
    evidence_link_id: int = Form(...),
    condition_immaterial: bool = Form(False),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Append a Documentation Reviewer's cited approval confirmation.

    The route intentionally accepts no conclusion.  The checklist recomputes
    the displayed classification from the cited current source under the
    project lock, and only a Documentation Reviewer may make that thin human
    confirmation (#347, ADR-0052/0056).  A conditional letter needs no click
    to stay not ready; ``condition_immaterial`` is the one optional override
    that records a quoted hedge as approval (ADR-0060).
    """

    project = _project(session, slug, principal, designation=access.DOCUMENTATION_REVIEW)
    _project_dependency(session, project, dependency_id)
    confirmation = confirm_interpretation(
        session,
        dependency_id,
        evidence_link_id,
        principal=principal,
        condition_immaterial=condition_immaterial,
    )
    response = RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="confirm_documentation_approval",
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
        ),
        request_fields={
            "slug": slug,
            "evidence_link_id": evidence_link_id,
            "condition_immaterial": condition_immaterial,
            "documentation_confirmation_id": confirmation.id,
        },
    )
    session.commit()
    return response


@app.post("/dependencies/{dependency_id}/documentation/clarify")
def clarify_documentation_review(
    request: Request,
    dependency_id: int,
    slug: str = Form(...),
    internal_owner_roster_entry_id: int = Form(...),
    next_action: str = Form(...),
    due_date: date | None = Form(None),
    due_date_unknown_reason: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Keep an open Documentation Review open and record the coordinated follow-up.

    The same Needs clarification a Source Discrepancy supports: an Internal Owner
    and Next Action, without forcing a conclusion to clear the work (ADR-0037).
    """

    project = _project(session, slug, principal, designation=access.COORDINATION)
    _project_dependency(session, project, dependency_id)
    clarification = record_documentation_clarification(
        session,
        dependency_id,
        roster_entry_id=internal_owner_roster_entry_id,
        next_action=next_action,
        due_date=due_date,
        due_date_unknown_reason=due_date_unknown_reason.strip() or None,
        principal=principal,
    )
    response = RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="clarify_documentation_review",
        request=request,
        response=response,
        subject=FrontendRequestSubject(
            project_id=project.id,
            dependency_id=dependency_id,
            work_decision_id=clarification.next_action_decision_id,
        ),
        request_fields={
            "internal_owner_roster_entry_id": str(internal_owner_roster_entry_id),
            "next_action": next_action,
            "due_date": due_date.isoformat() if due_date else "",
            "due_date_unknown_reason": due_date_unknown_reason,
        },
    )
    session.commit()
    return response
@app.post("/dependencies/{dependency_id}/conditions/clear")
def clear_documentation_condition(
    dependency_id: int,
    slug: str = Form(...),
    evidence_link_id: int = Form(...),
    basis_evidence_link_id: int | None = Form(None),
    basis_event_id: int | None = Form(None),
    reason: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record that a cited later passage or a recorded verbal met a condition.

    ADR-0060's judgment path for a generic condition: a Documentation Reviewer
    confirms the clear against a source they cite, both quotes on the screen.
    The rule that the basis must be a verified passage or a recorded statement
    in this project belongs to the Ledger; this route only carries the HTTP.
    """
    project = _project(session, slug, principal, designation=access.DOCUMENTATION_REVIEW)
    dependency = _project_dependency(session, project, dependency_id)
    entries = read_checklist(session, dependency_id).conditions
    clear_condition(
        session,
        dependency,
        evidence_link_id,
        principal=principal,
        entries=entries,
        basis_evidence_link_id=basis_evidence_link_id,
        basis_event_id=basis_event_id,
        reason=reason or None,
    )
    session.commit()
    return RedirectResponse(f"/ledger/{slug}/{dependency_id}", status_code=303)


@app.post("/dependencies/{dependency_id}/conditions/dismiss")
def dismiss_documentation_condition(
    dependency_id: int,
    slug: str = Form(...),
    evidence_link_id: int = Form(...),
    reason: str = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Retire a misdetected condition attributably, with a reason.

    Erring safe (ADR-0060): a person states why the condition was not real.
    Dismissal only removes a spurious blocker; it never fills the approval
    field, so no misdetection can produce Ready.
    """
    project = _project(session, slug, principal, designation=access.DOCUMENTATION_REVIEW)
    dependency = _project_dependency(session, project, dependency_id)
    entries = read_checklist(session, dependency_id).conditions
    dismiss_condition(
        session,
        dependency,
        evidence_link_id,
        principal=principal,
        entries=entries,
        reason=reason,
    )
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
    if page is None or not page.image_path:
        raise HTTPException(404, "no rendered image for that page")
    path = render_path_for_page(
        session,
        document_id=document_id,
        page_number=page_no,
        purpose="review",
        legacy_image_path=page.image_path,
    )
    if path is None or not Path(path).exists():
        raise HTTPException(404, "no rendered image for that page")
    return FileResponse(path, media_type="image/png")


@app.post("/candidates/{candidate_id}/keep-unresolved")
def keep_unresolved_candidate(
    request: Request,
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
        request=request,
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
    request: Request,
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
    check_sibling_set(candidate, siblings, schemes, aliases)

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
        request=request,
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
    except OrganizationIdentityUnresolved as exc:
        raise HTTPException(409, str(exc))


@app.post("/candidates/{candidate_id}/confirm-organization")
def confirm_organization(
    request: Request,
    candidate_id: int,
    slug: str = Form(...),
    external_org_id: str = Form(""),
    create_organization: bool = Form(False),
    facility_classes: list[str] = Form([]),
    alias_citation_document_id: str = Form(""),
    alias_citation_page_no: str = Form(""),
    alias_citation_quote: str = Form(""),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Record the one human registry decision behind a pending source spelling.

    This is intentionally a narrow command rather than a general organization
    editor: it can only decide the cited pending Candidate in the caller's own
    project.  A blank organization id means the person explicitly creates the
    source-named organization; it never means a default selection.  The normal
    durable handoff later re-runs Record Inclusion and is the only path that
    may create a Project Record fact.
    """

    project = _project(session, slug, principal, designation=access.COORDINATION)
    candidate = _project_pending_candidate(session, project, candidate_id)
    raw_org_id = external_org_id.strip()
    if raw_org_id:
        try:
            selected_org_id = int(raw_org_id)
        except ValueError as exc:
            raise HTTPException(400, "external_org_id must be a positive identity") from exc
        if selected_org_id <= 0:
            raise HTTPException(400, "external_org_id must be a positive identity")
    else:
        selected_org_id = None
    if selected_org_id is None and not create_organization:
        raise HTTPException(400, "choose an existing organization or explicitly create one")
    if selected_org_id is not None and create_organization:
        raise HTTPException(400, "choose either an existing organization or create one")
    cited_alias_values = (
        alias_citation_document_id.strip(),
        alias_citation_page_no.strip(),
        alias_citation_quote.strip(),
    )
    if any(cited_alias_values) and not all(cited_alias_values):
        raise HTTPException(400, "a cited alias confirmation needs document, page, and quote")
    try:
        if all(cited_alias_values):
            if selected_org_id is None:
                raise HTTPException(400, "a cited alias confirmation chooses an existing organization")
            try:
                citation_document_id = int(cited_alias_values[0])
                citation_page_no = int(cited_alias_values[1])
            except ValueError as exc:
                raise HTTPException(400, "cited alias document and page must be positive identities") from exc
            if citation_document_id <= 0 or citation_page_no <= 0:
                raise HTTPException(400, "cited alias document and page must be positive identities")
            receipt = confirm_cited_stated_alias(
                session,
                candidate,
                external_org_id=selected_org_id,
                document_id=citation_document_id,
                page_no=citation_page_no,
                quote=cited_alias_values[2],
                principal=principal,
            )
        else:
            receipt = confirm_identity(
                session,
                candidate,
                external_org_id=selected_org_id,
                facility_classes=tuple(facility_classes),
                principal=principal,
            )
    except OrganizationIdentityRefusal as exc:
        raise HTTPException(409, str(exc))
    response = RedirectResponse(f"/queue/{project.slug}", status_code=303)
    record_frontend_request(
        session,
        principal=principal,
        route_name="confirm_organization",
        request=request,
        response=response,
        subject=FrontendRequestSubject(project_id=project.id, candidate_id=candidate.id),
        request_fields={
            "slug": slug,
            "external_org_id": selected_org_id,
            "create_organization": create_organization,
            "facility_classes": facility_classes,
            "alias_citation_document_id": cited_alias_values[0] or None,
            "alias_citation_page_no": cited_alias_values[1] or None,
            "organization_identity_receipt_id": receipt.id,
        },
    )
    session.commit()
    return response


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
        request=request,
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
    request: Request,
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
        request=request,
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
    request: Request,
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
        request=request,
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
        # Taking delivery is what this route does, and every outcome of it is a
        # ledger row (#823): the person's authenticated session is what admitted
        # the bytes, so the delivery names them rather than a machine credential.
        received = receive_upload(
            session,
            project=project,
            body=body,
            filename=upload.filename or "",
            principal=principal,
            customer=settings.customer_id,
        )
        preview = preview_intake(session, project, received.staged, doc_type)
    except (IntakeRefused, UploadNotTaken) as exc:
        # The refusal or the failed attempt is durable before the response that
        # refuses the request; rolling it back with the response is exactly the
        # loss ADR-0089 removed. The status keeps the two apart for a client as
        # the ledger does for a reader: a refusal is about the file, and a
        # storage or scanner failure is about Corridor on one attempt.
        session.commit()
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
            status_code=503 if isinstance(exc, UploadNotTaken) else 400,
        )
    # The delivery outlives an abandoned preview: the person may close the tab,
    # and what arrived is recorded either way.
    session.commit()
    offers_draft = _deployment_serves(
        request, "POST", "/projects/{slug}/sources/draft"
    )
    return TEMPLATES.TemplateResponse(
        request,
        "source_preview.html",
        {
            "project": project,
            "preview": preview,
            "source_delivery_id": received.delivery_id,
            # The optional, explicitly requested draft of source-bound
            # suggestions (#362). Offered only once bounded spend authority is
            # declared; requesting it is a separate, attributable act.
            #
            # And only where this deployment serves the route behind the button
            # (#824). The live pilot admits the deterministic path and leaves
            # the model-assisted draft out of it, so on an enforcing deployment
            # the draft is not offered at all -- which is also what keeps this
            # page off `source_intake_draft_configurations`, a relation that
            # boundary revokes.
            "draft_configured": offers_draft
            and current_intake_draft_configuration(session, project.id) is not None,
            "draft_state_token": (
                intake_draft_state_token(session, project.id, received.staged.sha256)
                if offers_draft
                else ""
            ),
        },
    )


@app.post("/projects/{slug}/sources/draft-configuration")
async def declare_intake_draft_configuration_route(
    slug: str,
    request: Request,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Declare bounded intake-draft authority; a missing one refuses a draft.

    There is no environment or default fallback: drafting source-bound
    suggestions spends model budget, so an attributable coordination declaration
    names the model, prompt, and every bound before any draft request is allowed.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    form = await request.form()
    try:
        declare_intake_draft_configuration(
            session,
            project_id=project.id,
            principal=principal,
            model=str(form.get("model", "")),
            prompt_version=str(form.get("prompt_version", "")),
            max_input_tokens=int(str(form.get("max_input_tokens", ""))),
            max_output_tokens=int(str(form.get("max_output_tokens", ""))),
            timeout_seconds=int(str(form.get("timeout_seconds", ""))),
            max_requests=int(str(form.get("max_requests", ""))),
            retry_policy=str(form.get("retry_policy", "")),
            retention_policy=str(form.get("retention_policy", "")),
            observation_context=str(form.get("observation_context", "")),
        )
    except (ValueError, InvalidDraftConfiguration) as exc:
        raise HTTPException(
            400, f"intake-draft configuration refused: {exc}"
        ) from exc
    session.commit()
    return RedirectResponse(
        f"/projects/{project.slug}/sources/upload", status_code=303
    )


@app.post("/projects/{slug}/sources/draft")
async def source_intake_draft_request(
    request: Request,
    slug: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
    client_factory=Depends(get_intake_draft_client_factory),
):
    """Run one explicit, read-only draft of source-bound intake suggestions.

    It reads the exact staged bytes' permitted pages and the project's registered
    identities, then stores a non-authoritative receipt. It registers no
    document and declares no Supersession — confirmation stays the ordinary
    /sources/confirm act, which reconstructs its binding independently.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    form = await request.form()
    sha256 = str(form.get("sha256") or "")
    filename = str(form.get("filename") or "")
    doc_type = str(form.get("doc_type") or "")
    state_token = str(form.get("state_token") or "")
    raw_pages = str(form.get("permitted_pages") or "").strip()
    try:
        permitted_pages = tuple(
            int(part) for part in raw_pages.split(",") if part.strip()
        )
    except ValueError as exc:
        raise HTTPException(400, "permitted_pages must be integers") from exc
    path = staged_file(sha256)
    if path is None:
        raise HTTPException(
            409, "the staged bytes are no longer available; re-upload the source"
        )
    staged = StagedDraftSource(
        sha256=sha256,
        filename=filename,
        suffix=path.suffix.lower(),
        stored_path=path,
        doc_type=doc_type,
    )
    try:
        receipt = request_intake_draft(
            session,
            project_id=project.id,
            staged=staged,
            principal=principal,
            client_factory=client_factory,
            expected_sha256=sha256,
            permitted_pages=permitted_pages,
            state_token=state_token,
        )
    except IntakeDraftRefused as exc:
        raise HTTPException(409, exc.detail) from exc
    except IntakeDraftConfigurationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return RedirectResponse(
        f"/projects/{project.slug}/sources/drafts/{receipt.public_id}",
        status_code=303,
    )


@app.get(
    "/projects/{slug}/sources/drafts/{public_id}", response_class=HTMLResponse
)
def read_source_intake_draft(
    request: Request,
    slug: str,
    public_id: str,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Read one retained non-authoritative draft; a GET never spends.

    The suggestions are shown with the source passage each was read from and any
    uncertainty. Nothing here is preselected or registered: the ordinary
    registration controls stay the person's, independently reconstructed.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    receipt = session.scalars(
        select(SourceIntakeDraftRequest).where(
            SourceIntakeDraftRequest.public_id == public_id,
            SourceIntakeDraftRequest.project_id == project.id,
        )
    ).first()
    if receipt is None:
        raise HTTPException(404, "no intake draft for this project")
    return TEMPLATES.TemplateResponse(
        request,
        "source_intake_draft.html",
        {
            "project": project,
            "receipt": receipt,
            "source": receipt.source_json or {},
        },
    )


@app.post("/projects/{slug}/sources/confirm")
def source_confirm(
    slug: str,
    sha256: str = Form(...),
    filename: str = Form(...),
    doc_type: str = Form(...),
    binding_fingerprint: str = Form(...),
    source_delivery_id: int = Form(...),
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Bind the previewed source to the acting person and register it.

    The delivery the person previewed is confirmed by identity rather than
    looked up again from the bytes (#823): the posted row is re-proved against
    this project, these bytes and the ``stored`` disposition before anything is
    written, so a second definition of which delivery these bytes arrived on
    never gets the chance to disagree with the ledger's.
    """
    project = _project(session, slug, principal, designation=access.COORDINATION)
    confirm_intake(
        session,
        project=project,
        sha256=sha256,
        filename=filename,
        doc_type=doc_type,
        binding_fingerprint=binding_fingerprint,
        principal=principal,
        source_delivery_id=source_delivery_id,
    )
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


# --- The exact source behind one citation (#831) ---------------------------
#
# Every surface that prints a citation links to `source_passage`: a Review row,
# a Record value, a follow-up bundle, the source register. One screen rather
# than one per surface, because the thing being answered — what does the
# document say around this quote — is the same question wherever it is asked,
# and because the project gate, the Source Passage Check and the separation of
# a storage failure from a finding about the document are exactly the parts
# that must not be reimplemented four times.
#
# `source_document_original` is the other half: the registered bytes
# themselves. It is deliberately a separate route from the view, because the
# view is a page a coordinator reads and the original is the file itself, and
# `tests/test_artifact_authorization.py` is entitled to find the second and
# hold it to the byte-serving rules. The legacy `/page-image` route stays
# outside the live-pilot manifest; nothing here revives it.


@app.get("/sources/{slug}/passage/{segment_id}", response_class=HTMLResponse)
def source_passage(
    request: Request,
    slug: str,
    segment_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Open one cited passage at its place in the source it was read from."""
    project = _project(session, slug, principal)
    try:
        view = read_source_passage(
            session, project_id=project.id, segment_id=segment_id
        )
    except SourcePassageNotFound:
        raise HTTPException(404, "no such source passage in this project") from None
    return TEMPLATES.TemplateResponse(
        request, "source_passage.html", {"project": project, "view": view}
    )


@app.get("/sources/{slug}/document/{document_id}/original")
def source_document_original(
    slug: str,
    document_id: int,
    principal: HumanPrincipal = Depends(get_human_principal),
    session: Session = Depends(get_session),
):
    """Serve the registered original of one source document to its members.

    The document is resolved inside the project gate rather than by id alone,
    so a guessed id cannot reach another project's source. Bytes that cannot
    be retrieved are a retrieval failure and answer as one; they are never
    reported as something about the document itself.
    """
    project = _project(session, slug, principal)
    document = session.get(Document, document_id)
    if document is None or document.project_id != project.id:
        raise HTTPException(404, "no such source document in this project")
    try:
        path = stored_file(document)
    except (StorageError, OSError) as error:
        path, failure = None, type(error).__name__
    else:
        failure = "ObjectMissing"
    if path is None or not Path(path).exists():
        log_source_bytes_retrieval_failure(
            project_id=project.id,
            document_id=int(document.id),
            registered_sha256=document.sha256,
            failure=failure,
            detail="the registered original could not be retrieved for download",
        )
        raise HTTPException(404, "the registered original could not be retrieved")
    return FileResponse(
        path,
        media_type=_ORIGINAL_MEDIA_TYPES.get(
            Path(document.filename).suffix.lower(), "application/octet-stream"
        ),
        filename=document.filename,
    )


# The media types the product's own source classes arrive as. Anything else is
# served as opaque bytes: guessing a type for an unknown suffix would invite a
# browser to render a customer's file as markup.
_ORIGINAL_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".csv": "text/csv",
}


@app.post("/intake/inbound")
async def receive_inbound_mail(
    request: Request,
    inbound_token: str | None = Header(default=None, alias="X-Corridor-Inbound-Token"),
    delivered_to: str | None = Header(default=None, alias="X-Corridor-Delivered-To"),
    delivery_id: str | None = Header(default=None, alias="X-Corridor-Delivery-Id"),
    session: Session = Depends(get_machine_session),
):
    """Server-to-server receipt boundary for one authenticated mail transport.

    This is not an ordinary customer route: it refuses unless deployment
    supplied the sender authentication secret. It does not accept a project id,
    actor, or route from a client.

    Two things are authenticated here and they are not the same thing. The
    shared secret authenticates the *transport*; ``X-Corridor-Delivered-To``
    carries the envelope recipient that transport actually delivered to, and
    that alias is what binds the customer and project (#511, ADR-0078). The
    header is the MTA's report of the SMTP recipient, not a header from the
    message — the message's own ``To``/``Cc`` are inside the untrusted body and
    bind nothing.

    A delivery arriving without that alias falls back to the frozen
    global-address path (ADR-0059), which infers the project from content and
    exists only while deployments move their transports onto bound aliases.
    """
    if (
        not settings.inbound_webhook_secret
        or inbound_token is None
        or not secrets.compare_digest(inbound_token, settings.inbound_webhook_secret)
    ):
        raise HTTPException(401, "inbound sender is not authorized")
    raw_bytes = await request.body()
    try:
        if delivered_to:
            received = email_intake.receive_pushed_message(
                session,
                credential=push_intake.PushCredential(
                    channel="project_alias", material=delivered_to
                ),
                raw_bytes=raw_bytes,
                transport_delivery_id=delivery_id,
            )
        elif settings.inbound_service_address:
            received = email_intake.receive_message(
                session,
                raw_bytes=raw_bytes,
                service_address=settings.inbound_service_address,
            )
        else:
            raise HTTPException(401, "inbound sender is not authorized")
    except push_intake.PushDeliveryRefused as exc:
        # The refusal is a record, not an absence (ADR-0089): the delivery was
        # bound and its digest and reason are in the ledger, so the response
        # refuses and the record it just made is committed rather than rolled
        # back with it.
        session.commit()
        raise HTTPException(400, str(exc)) from exc
    except push_intake.PushIntakeRefused as exc:
        raise HTTPException(403, str(exc)) from exc
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


@app.post("/projects/{slug}/contacts/{contact_id}/correct")
async def correct_onboarding_contact(
    slug: str, contact_id: int, request: Request,
    session: Session = Depends(get_session),
    principal: HumanPrincipal = Depends(get_human_principal),
):
    """One attributable onboarding correction through the existing auth boundary."""
    from sqlalchemy.exc import DBAPIError
    from corridor.project_contacts import ContactInput, ContactImportRefused, correct_contact

    project = _project(session, slug, principal, designation=access.COORDINATION)
    from corridor.operating_mode import is_adopted_baseline
    if not is_adopted_baseline(session, project.id):
        _refuse_legacy_project_under_the_boundary(request, project)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"contact", "reason", "idempotency_key"}:
            raise ValueError("correction requires contact, reason and idempotency_key")
        changed = correct_contact(session, project_id=project.id, contact_id=contact_id,
            replacement=ContactInput(**body["contact"]), principal=principal,
            reason=body["reason"], idempotency_key=body["idempotency_key"])
    except (ContactImportRefused, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except DBAPIError as exc:
        raise HTTPException(409, "contact correction conflicts with the retained predecessor") from exc
    response = {"contact_id": changed.id, "corrects_id": changed.corrects_id,
                "corrected_by": changed.corrected_by, "record": changed.values_json}
    session.commit()
    return response


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
