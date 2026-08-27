"""Corridor-managed Automatic Carry-Forward at the public domain seam.

These tests deliberately exercise the same real-Postgres boundary used by
Supersession Review. Automatic Carry-Forward is not a faster spelling of
human Reconfirmation: a released deterministic policy may inherit only support
that human decisions already established, without project authorization.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from corridor import audit
from corridor import automatic_carry_forward as automatic
from corridor.adjudicate import accept_candidate
from corridor.automatic_carry_forward import (
    ABSTENTION_REASON_VERSION,
    automatic_carry_forward_status,
    run_automatic_carry_forward,
)
from corridor.db import Session, engine
from corridor.exceptions import evaluate as evaluate_exceptions
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    ActiveExtractionRun,
    Assertion,
    AuditLog,
    PolicyApproval,
    AutomaticCarryForwardOutcome,
    AutomaticCarryForwardReceipt,
    PolicyRun,
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionRun,
    OperativeSupport,
    Project,
    ReconfirmationReceipt,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.operative_support import resolve_operative_support
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import (
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
    create_revision_comparison,
    read_revision_comparison,
)
from corridor.supersession import (
    SupersessionDeclaration,
    register_supersessions,
)
from corridor.supersession_review import (
    build_reviewer_worklist,
    reconfirm_operative_support,
)
from corridor.support_transfer import prove_support_transfer


REVIEWER = HumanPrincipal("local:carry-forward-reviewer")
APPROVER = HumanPrincipal("local:carry-forward-approver")
POLICY_VERSION = "automatic-carry-forward-v2"
MACHINE_ACTOR = "corridor:automatic-carry-forward"


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug=f"carry-forward-policy-{uuid4().hex}",
        name="Carry-Forward Policy",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


@dataclass(frozen=True)
class TransitionScenario:
    project: Project
    predecessor: Document
    successor: Document
    index: Document
    predecessor_candidate: Candidate
    successor_candidate: Candidate | None
    predecessor_run: object
    successor_run: object
    comparison: object
    finding: object
    dependency: Dependency
    old_evidence: EvidenceLink


@dataclass(frozen=True)
class TerminalTransition:
    document: Document
    candidate: Candidate
    run: object
    comparison: object
    finding: object


def _document(
    session,
    project: Project,
    *,
    registry_id: str,
    sha_character: str,
    filename: str,
    page_text: str,
) -> Document:
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha_character * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _fields(*, station_from: str = "100+00") -> dict[str, str]:
    return {
        "utility_id": "FOC1-1",
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }


def _quote(fields: dict[str, str]) -> str:
    return " ".join(
        (
            fields["utility_id"],
            fields["external_org"],
            fields["utility_type"],
            fields["station_from"],
        )
    )


def _candidate(
    project: Project,
    document: Document,
    fields: dict[str, str],
    *,
    citation_count: int = 1,
) -> Candidate:
    citation = {
        "document_id": document.id,
        "page": 1,
        "quote": _quote(fields),
        "verified": True,
    }
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [dict(citation) for _ in range(citation_count)],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document: Document, *candidates: Candidate):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed_transition(
    session,
    *,
    prior_satisfying: bool = True,
    successor_station: str = "100+00",
    successor_citation_count: int = 1,
    successor_dropped: bool = False,
    matcher_version: str = DEFAULT_MATCHER_VERSION,
    matcher_config: dict | None = None,
    unattributable_admission: bool = False,
    stale_successor_after_comparison: bool = False,
    admitted_station: str | None = None,
) -> TransitionScenario:
    project = Project(
        slug=f"automatic-carry-forward-{uuid4().hex}",
        name="Automatic Carry-Forward",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])

    predecessor_fields = _fields()
    successor_fields = _fields(station_from=successor_station)
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha_character="a",
        filename="revision-a.pdf",
        page_text=_quote(predecessor_fields),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha_character="b",
        filename="revision-b.pdf",
        page_text=_quote(successor_fields),
    )
    index = _document(
        session,
        project,
        registry_id="INDEX",
        sha_character="c",
        filename="index.pdf",
        page_text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidate = _candidate(project, predecessor, predecessor_fields)
    predecessor_run = _completed_run(session, predecessor, predecessor_candidate)
    if admitted_station is not None:
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == predecessor.id)
        ).one()
        page.text = f"{page.text} {admitted_station}"
        edited_payload = deepcopy(predecessor_candidate.payload_json)
        edited_payload["fields"] = {
            **edited_payload["fields"],
            "station_from": admitted_station,
        }
        predecessor_candidate.payload_json = edited_payload
        session.flush([page, predecessor_candidate])
    dependency = accept_candidate(session, predecessor_candidate, principal=REVIEWER)
    dependency.evidence_required = "approved relocation closeout"
    old_evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).one()
    if prior_satisfying:
        mark_satisfies(
            session,
            dependency.id,
            old_evidence.id,
            principal=REVIEWER,
        )

    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidate = None
    successor_candidates: tuple[Candidate, ...] = ()
    if not successor_dropped:
        successor_candidate = _candidate(
            project,
            successor,
            successor_fields,
            citation_count=successor_citation_count,
        )
        successor_candidates = (successor_candidate,)
    successor_run = _completed_run(session, successor, *successor_candidates)
    comparison_kwargs = {"matcher_version": matcher_version}
    if matcher_config is not None:
        comparison_kwargs["matcher_config"] = matcher_config
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_run.id,
        successor_extraction_run_id=successor_run.id,
        **comparison_kwargs,
    )
    finding = next(
        finding
        for finding in read_revision_comparison(session, comparison.id).findings
        if predecessor_candidate.id in finding.predecessor_candidate_ids
    )

    if unattributable_admission:
        admission = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id == dependency.id,
                AuditLog.action == audit.ACCEPT_CANDIDATE,
            )
        ).one()
        admission.actor = "legacy-import"
        admission.human_principal = None
    if stale_successor_after_comparison:
        assert successor_candidate is not None
        stale_payload = deepcopy(successor_candidate.payload_json)
        stale_payload["fields"] = {
            **stale_payload["fields"],
            "station_from": "999+00",
        }
        successor_candidate.payload_json = stale_payload
    session.flush()

    return TransitionScenario(
        project=project,
        predecessor=predecessor,
        successor=successor,
        index=index,
        predecessor_candidate=predecessor_candidate,
        successor_candidate=successor_candidate,
        predecessor_run=predecessor_run,
        successor_run=successor_run,
        comparison=comparison,
        finding=finding,
        dependency=dependency,
        old_evidence=old_evidence,
    )


def _append_terminal_transition(
    session, scenario: TransitionScenario
) -> TerminalTransition:
    fields = _fields()
    terminal = _document(
        session,
        scenario.project,
        registry_id="REV-C",
        sha_character="d",
        filename="revision-c.pdf",
        page_text=_quote(fields),
    )
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-B",
                successor_registry_id="REV-C",
                replacement_date=date(2026, 8, 2),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=scenario.project.id,
    )
    candidate = _candidate(scenario.project, terminal, fields)
    run = _completed_run(session, terminal, candidate)
    comparison = create_revision_comparison(
        session,
        predecessor_extraction_run_id=scenario.successor_run.id,
        successor_extraction_run_id=run.id,
    )
    finding = next(
        finding
        for finding in read_revision_comparison(session, comparison.id).findings
        if scenario.successor_candidate.id in finding.predecessor_candidate_ids
    )
    return TerminalTransition(
        document=terminal,
        candidate=candidate,
        run=run,
        comparison=comparison,
        finding=finding,
    )


def _dependency_state(dependency: Dependency) -> tuple:
    return (
        dependency.ref_code,
        dependency.source_ref,
        dependency.dep_type,
        dependency.title,
        dependency.location_desc,
        dependency.station_from,
        dependency.station_to,
        dependency.external_org_id,
        dependency.milestone_id,
        dependency.resolution_strategy,
        dependency.committed_date,
        dependency.need_date,
        dependency.evidence_required,
        dependency.notes,
    )


def _assertion_state(session, dependency_id: int) -> tuple:
    return tuple(
        session.execute(
            select(
                Assertion.id,
                Assertion.field_name,
                Assertion.asserted_value,
                Assertion.evidence_link_id,
            )
            .where(Assertion.dependency_id == dependency_id)
            .order_by(Assertion.id)
        ).all()
    )


def _direct_sufficiency(session, dependency_id: int, evidence_link_id: int) -> bool:
    return (
        session.scalar(
            select(DependencyEvidenceSufficiency.id).where(
                DependencyEvidenceSufficiency.dependency_id == dependency_id,
                DependencyEvidenceSufficiency.evidence_link_id == evidence_link_id,
                DependencyEvidenceSufficiency.scope_link_id.is_(None),
            )
        )
        is not None
    )


def _ledger_mutation_state(session, scenario: TransitionScenario) -> tuple:
    candidate = scenario.successor_candidate
    return (
        _dependency_state(scenario.dependency),
        (
            candidate.state,
            candidate.merged_into,
            deepcopy(candidate.payload_json),
        )
        if candidate is not None
        else None,
        _assertion_state(session, scenario.dependency.id),
        tuple(
            session.execute(
                select(
                    EvidenceLink.id,
                    EvidenceLink.document_id,
                    EvidenceLink.page_no,
                    EvidenceLink.quote,
                    EvidenceLink.verified,
                    DependencyEvidenceSufficiency.id.is_not(None),
                )
                .where(EvidenceLink.dependency_id == scenario.dependency.id)
                .outerjoin(
                    DependencyEvidenceSufficiency,
                    (
                        DependencyEvidenceSufficiency.dependency_id
                        == EvidenceLink.dependency_id
                    )
                    & (
                        DependencyEvidenceSufficiency.evidence_link_id
                        == EvidenceLink.id
                    )
                    & DependencyEvidenceSufficiency.scope_link_id.is_(None),
                )
                .order_by(EvidenceLink.id)
            ).all()
        ),
        tuple(
            session.execute(
                select(
                    OperativeSupport.id,
                    OperativeSupport.evidence_link_id,
                    OperativeSupport.role,
                    OperativeSupport.field_name,
                    OperativeSupport.designated_by,
                )
                .where(OperativeSupport.dependency_id == scenario.dependency.id)
                .order_by(OperativeSupport.id)
            ).all()
        ),
        tuple(
            session.execute(
                select(
                    AuditLog.id,
                    AuditLog.actor,
                    AuditLog.human_principal,
                    AuditLog.action,
                    AuditLog.before_json,
                    AuditLog.after_json,
                )
                .where(
                    AuditLog.entity_type == audit.DEPENDENCY,
                    AuditLog.entity_id == scenario.dependency.id,
                )
                .order_by(AuditLog.id)
            ).all()
        ),
    )


def _automatic_entries(session, dependency_id: int) -> tuple[AuditLog, ...]:
    return tuple(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id == dependency_id,
                AuditLog.action == "automatic_carry_forward",
            )
            .order_by(AuditLog.id)
        ).all()
    )


def _carry_runs(session, project_id: int) -> tuple[PolicyRun, ...]:
    return tuple(
        session.scalars(
            select(PolicyRun)
            .where(PolicyRun.project_id == project_id)
            .order_by(PolicyRun.id)
        ).all()
    )


def _carry_outcomes(
    session, project_id: int
) -> tuple[AutomaticCarryForwardOutcome, ...]:
    return tuple(
        session.scalars(
            select(AutomaticCarryForwardOutcome)
            .where(AutomaticCarryForwardOutcome.project_id == project_id)
            .order_by(AutomaticCarryForwardOutcome.id)
        ).all()
    )


def _human_reconfirm(session, scenario, transition) -> EvidenceLink:
    [review] = build_reviewer_worklist(session, scenario.project.id).reconfirmation
    return reconfirm_operative_support(
        session,
        project_id=scenario.project.id,
        dependency_id=scenario.dependency.id,
        predecessor_document_id=review.predecessor_document_id,
        successor_candidate_id=transition.candidate.id,
        comparison_id=transition.comparison.id,
        finding_id=review.finding_id,
        scope_fingerprint=review.scope_fingerprint,
        principal=REVIEWER,
    )


def _delete_committed_carry_forward_project(project_id: int) -> None:
    with Session() as cleanup:
        cleanup.execute(text("set local session_replication_role = replica"))
        document_ids = tuple(
            cleanup.scalars(
                select(Document.id).where(Document.project_id == project_id)
            ).all()
        )
        dependency_ids = tuple(
            cleanup.scalars(
                select(Dependency.id).where(Dependency.project_id == project_id)
            ).all()
        )
        candidate_ids = tuple(
            cleanup.scalars(
                select(Candidate.id).where(Candidate.project_id == project_id)
            ).all()
        )
        comparison_ids = tuple(
            cleanup.scalars(
                select(RevisionComparisonRun.id).where(
                    RevisionComparisonRun.project_id == project_id
                )
            ).all()
        )
        run_ids = tuple(
            cleanup.scalars(
                select(PolicyRun.id).where(PolicyRun.project_id == project_id)
            ).all()
        )

        cleanup.execute(
            delete(AutomaticCarryForwardOutcome).where(
                AutomaticCarryForwardOutcome.run_id.in_(run_ids)
            )
        )
        cleanup.execute(delete(PolicyRun).where(PolicyRun.id.in_(run_ids)))
        cleanup.execute(
            delete(AutomaticCarryForwardReceipt).where(
                AutomaticCarryForwardReceipt.dependency_id.in_(dependency_ids)
            )
        )
        cleanup.execute(
            delete(ReconfirmationReceipt).where(
                ReconfirmationReceipt.dependency_id.in_(dependency_ids)
            )
        )
        cleanup.execute(
            delete(PolicyApproval).where(PolicyApproval.project_id == project_id)
        )
        cleanup.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == audit.PROJECT,
                AuditLog.entity_id == project_id,
            )
        )
        cleanup.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id.in_(dependency_ids),
            )
        )
        cleanup.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == audit.CANDIDATE,
                AuditLog.entity_id.in_(candidate_ids),
            )
        )
        cleanup.execute(
            delete(OperativeSupport).where(
                OperativeSupport.dependency_id.in_(dependency_ids)
            )
        )
        cleanup.execute(
            delete(Assertion).where(Assertion.dependency_id.in_(dependency_ids))
        )
        cleanup.execute(
            delete(EvidenceLink).where(EvidenceLink.dependency_id.in_(dependency_ids))
        )
        cleanup.execute(
            delete(RevisionComparisonFinding).where(
                RevisionComparisonFinding.revision_comparison_run_id.in_(comparison_ids)
            )
        )
        cleanup.execute(
            delete(RevisionComparisonRun).where(
                RevisionComparisonRun.id.in_(comparison_ids)
            )
        )
        cleanup.execute(delete(Candidate).where(Candidate.project_id == project_id))
        cleanup.execute(delete(Dependency).where(Dependency.project_id == project_id))
        cleanup.execute(
            delete(ActiveExtractionRun).where(
                ActiveExtractionRun.document_id.in_(document_ids)
            )
        )
        cleanup.execute(
            delete(ExtractionRun).where(ExtractionRun.document_id.in_(document_ids))
        )
        cleanup.execute(
            update(Document)
            .where(Document.project_id == project_id)
            .values(
                superseded_by=None,
                superseded_on=None,
                supersession_source_document_id=None,
                supersession_source_page=None,
            )
        )
        cleanup.execute(delete(DocPage).where(DocPage.document_id.in_(document_ids)))
        cleanup.execute(delete(Document).where(Document.project_id == project_id))
        cleanup.execute(delete(Project).where(Project.id == project_id))
        cleanup.commit()


def test_automatic_carry_forward_is_normal_processing(session):
    scenario = _seed_transition(session)

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert len(result.carried) == 1
    assert result.abstentions == ()
    assert build_reviewer_worklist(session, scenario.project.id).reconfirmation == ()
    assert len(_automatic_entries(session, scenario.dependency.id)) == 1


def test_support_transfer_proof_freezes_one_exact_current_read(session):
    scenario = _seed_transition(session)
    [review] = build_reviewer_worklist(session, scenario.project.id).reconfirmation

    proof = prove_support_transfer(
        session,
        project_id=scenario.project.id,
        review=review,
    )

    assert proof.dependency.id == scenario.dependency.id
    assert proof.comparison.id == scenario.comparison.id
    assert proof.finding.id == scenario.finding.id
    assert proof.successor_candidate.id == scenario.successor_candidate.id
    assert proof.citation == {
        "document_id": scenario.successor.id,
        "page": 1,
        "quote": "FOC1-1 AT&T Telecom 100+00",
        "verified": True,
    }
    assert proof.scope_fingerprint == review.scope_fingerprint


def test_released_policy_is_current_without_project_authorization(session, project):
    before = tuple(session.scalars(select(AuditLog.id)).all())

    status = automatic_carry_forward_status(session, project.id)

    assert status.policy_version == POLICY_VERSION
    assert len(status.policy_sha256) == 64
    assert status.eligible_count == 0
    assert tuple(session.scalars(select(AuditLog.id)).all()) == before


def test_exact_success_moves_support_without_admitting_or_revising(session):
    scenario = _seed_transition(session)
    dependency_before = _dependency_state(scenario.dependency)
    candidate_payload_before = deepcopy(scenario.successor_candidate.payload_json)
    assertions_before = _assertion_state(session, scenario.dependency.id)

    result = run_automatic_carry_forward(session, scenario.project.id)

    [receipt] = result.carried
    assert result.abstentions == ()
    assert receipt.dependency_id == scenario.dependency.id
    assert receipt.successor_candidate_id == scenario.successor_candidate.id
    assert receipt.policy_approval_id is None
    assert receipt.policy_version == POLICY_VERSION
    assert len(receipt.policy_sha256) == 64

    support = resolve_operative_support(session, (scenario.dependency.id,))[
        scenario.dependency.id
    ]
    assert support.publication.document_id == scenario.successor.id
    assert support.is_ready is True
    assert {evidence.document_id for evidence in support.current_readiness} == {
        scenario.successor.id
    }
    assert _dependency_state(scenario.dependency) == dependency_before
    assert scenario.successor_candidate.state == "pending"
    assert scenario.successor_candidate.merged_into is None
    assert scenario.successor_candidate.payload_json == candidate_payload_before
    assert _assertion_state(session, scenario.dependency.id) == assertions_before
    session.refresh(scenario.old_evidence)
    assert scenario.old_evidence.verified is True
    assert _direct_sufficiency(
        session, scenario.dependency.id, scenario.old_evidence.id
    )

    worklist = build_reviewer_worklist(session, scenario.project.id)
    assert worklist.reconfirmation == ()
    assert worklist.ordinary == ()
    rules = {
        exception.rule
        for exception in evaluate_exceptions(session, scenario.project.id)
        if exception.dependency_id == scenario.dependency.id
    }
    assert "SUPERSEDED_CITATION" not in rules


def test_a_human_edited_conclusion_carries_only_when_successor_proves_it(
    session,
):
    scenario = _seed_transition(
        session,
        admitted_station="100 + 00",
        successor_station="100 + 00",
    )
    [review] = build_reviewer_worklist(session, scenario.project.id).ordinary
    assert review.reason == "admission_fields_changed"

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert len(result.carried) == 1
    assert result.abstentions == ()
    support = resolve_operative_support(session, (scenario.dependency.id,))[
        scenario.dependency.id
    ]
    assert support.publication.document_id == scenario.successor.id
    assert scenario.dependency.station_from == "100 + 00"


def test_a_human_edit_abstains_when_successor_does_not_prove_it(session):
    scenario = _seed_transition(
        session,
        admitted_station="100 + 00",
        successor_station="100+00",
    )
    before = _ledger_mutation_state(session, scenario)

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert result.carried == ()
    assert {item.reason for item in result.abstentions} == {
        "successor_fields_not_exact"
    }
    assert _ledger_mutation_state(session, scenario) == before


@pytest.mark.parametrize("prior_satisfying", [False, True])
def test_readiness_is_inherited_but_never_invented(session, prior_satisfying):
    scenario = _seed_transition(session, prior_satisfying=prior_satisfying)
    evidence_required = scenario.dependency.evidence_required

    result = run_automatic_carry_forward(session, scenario.project.id)

    [receipt] = result.carried
    evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == scenario.dependency.id)
        .order_by(EvidenceLink.id.desc())
    ).first()
    assert evidence.document_id == scenario.successor.id
    assert (
        _direct_sufficiency(session, scenario.dependency.id, evidence.id)
        is prior_satisfying
    )
    support = resolve_operative_support(session, (scenario.dependency.id,))[
        scenario.dependency.id
    ]
    assert support.is_ready is prior_satisfying
    assert scenario.dependency.evidence_required == evidence_required

    entry = session.get(AuditLog, receipt.audit_log_id)
    inherited_roles = {moved["role"] for moved in entry.after_json["moved_scopes"]}
    assert ("readiness" in inherited_roles) is prior_satisfying


@pytest.mark.parametrize(
    ("matcher_version", "matcher_config"),
    [
        ("revision-correspondence-v2-experiment", None),
        (
            DEFAULT_MATCHER_VERSION,
            {
                **DEFAULT_MATCHER_CONFIG,
                "minimum_score": 0.55,
                "weights": dict(DEFAULT_MATCHER_CONFIG["weights"]),
            },
        ),
    ],
)
def test_unapproved_matcher_policy_pauses_automation(
    session, matcher_version, matcher_config
):
    scenario = _seed_transition(
        session,
        matcher_version=matcher_version,
        matcher_config=matcher_config,
    )
    before = _ledger_mutation_state(session, scenario)

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert result.carried == ()
    assert {item.reason for item in result.abstentions} == {
        "comparison_policy_unapproved"
    }
    assert _ledger_mutation_state(session, scenario) == before
    assert (
        len(build_reviewer_worklist(session, scenario.project.id).reconfirmation) == 1
    )


def test_released_policy_digest_changes_with_deployed_source_bytes(session, project):
    drifted_runtime = automatic.AutomaticCarryForwardRuntime.deployed(
        source_overrides={"corridor.automatic_carry_forward": b"policy-drift-v1"}
    )
    deployed = automatic_carry_forward_status(session, project.id)
    drifted = automatic_carry_forward_status(
        session, project.id, _runtime=drifted_runtime
    )

    assert deployed.policy_sha256 != drifted.policy_sha256


def test_released_policy_digest_changes_when_supersession_source_bytes_change(
    session, project
):
    base_runtime = automatic.AutomaticCarryForwardRuntime.deployed()
    drifted_runtime = automatic.AutomaticCarryForwardRuntime.deployed(
        source_overrides={"corridor.supersession": b"supersession-rules-drift-v1"}
    )

    original = automatic_carry_forward_status(
        session, project.id, _runtime=base_runtime
    )
    replacement = automatic_carry_forward_status(
        session, project.id, _runtime=drifted_runtime
    )

    assert original.policy_sha256 != replacement.policy_sha256


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("changed", "comparison_changed"),
        ("dropped", "comparison_dropped"),
        ("normalized_only", "successor_fields_not_exact"),
        ("multiple_citations", "successor_provenance_unsafe"),
        ("unattributable_admission", "admission_not_attributable"),
        ("stale_successor", "successor_candidate_changed"),
    ],
)
def test_unsafe_or_inexact_rows_abstain_without_ledger_writes(
    session, case, expected_reason
):
    options = {
        "successor_station": (
            "101+00"
            if case == "changed"
            else "100 + 00"
            if case == "normalized_only"
            else "100+00"
        ),
        "successor_citation_count": (2 if case == "multiple_citations" else 1),
        "successor_dropped": case == "dropped",
        "unattributable_admission": case == "unattributable_admission",
        "stale_successor_after_comparison": case == "stale_successor",
    }
    scenario = _seed_transition(session, **options)
    before = _ledger_mutation_state(session, scenario)

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert result.carried == ()
    assert {item.reason for item in result.abstentions} == {expected_reason}
    assert _ledger_mutation_state(session, scenario) == before
    worklist = build_reviewer_worklist(session, scenario.project.id)
    if case == "normalized_only":
        assert len(worklist.reconfirmation) == 1
    else:
        assert any(
            review.dependency_id == scenario.dependency.id
            for review in worklist.ordinary
        )


def test_batch_replay_is_idempotent(session):
    scenario = _seed_transition(session)

    first = run_automatic_carry_forward(session, scenario.project.id)
    state_after_first = _ledger_mutation_state(session, scenario)
    second = run_automatic_carry_forward(session, scenario.project.id)

    assert len(first.carried) == 1
    assert first.abstentions == ()
    assert second.carried == ()
    assert second.abstentions == ()
    assert _ledger_mutation_state(session, scenario) == state_after_first
    assert len(_automatic_entries(session, scenario.dependency.id)) == 1


def test_machine_act_has_honest_audit_identity_and_exact_receipt(session):
    scenario = _seed_transition(session)

    result = run_automatic_carry_forward(session, scenario.project.id)

    [receipt] = result.carried
    [entry] = _automatic_entries(session, scenario.dependency.id)
    assert receipt.audit_log_id == entry.id
    assert receipt.policy_approval_id is None
    assert entry.actor == MACHINE_ACTOR
    assert entry.human_principal is None
    assert entry.action == "automatic_carry_forward"
    assert "policy_approval_id" not in entry.after_json
    assert entry.after_json["policy_version"] == receipt.policy_version
    assert entry.after_json["policy_sha256"] == receipt.policy_sha256
    assert entry.after_json["comparison_id"] == scenario.comparison.id
    assert entry.after_json["finding_id"] == scenario.finding.id
    assert entry.after_json["predecessor_candidate_id"] == (
        scenario.predecessor_candidate.id
    )
    assert entry.after_json["successor_candidate_id"] == (
        scenario.successor_candidate.id
    )
    assert entry.after_json["new_evidence_link_id"] == (receipt.new_evidence_link_id)
    assert entry.after_json["origin_admission_audit_id"] is not None
    assert entry.after_json["predecessor_support_transfer_audit_id"] is None
    assert entry.before_json["operative_scopes"]
    assert entry.after_json["moved_scopes"]
    assert entry.after_json["scope_fingerprint"]
    support_transfers = audit.support_transfer_records_for_dependencies(
        session, (scenario.dependency.id,)
    )[scenario.dependency.id]
    [support_transfer] = support_transfers
    assert support_transfer.action == audit.AUTOMATIC_CARRY_FORWARD
    assert support_transfer.audit_id == entry.id
    assert not session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == scenario.dependency.id,
            AuditLog.action == audit.RECONFIRM_OPERATIVE_SUPPORT,
        )
    ).all()


def test_actor_human_principal_mismatch_abstains_without_trigger_crashing(
    session,
):
    scenario = _seed_transition(session)
    before = _ledger_mutation_state(session, scenario)
    admission = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == scenario.dependency.id,
            AuditLog.action == audit.ACCEPT_CANDIDATE,
        )
    ).one()
    admission.actor = "legacy-import"
    assert admission.human_principal == REVIEWER.subject
    session.flush([admission])
    before = _ledger_mutation_state(session, scenario)

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert result.carried == ()
    assert {item.reason for item in result.abstentions} == {
        "admission_not_attributable"
    }
    assert _ledger_mutation_state(session, scenario) == before
    assert _automatic_entries(session, scenario.dependency.id) == ()


def test_tampered_released_policy_identity_invalidates_machine_lineage(session):
    scenario = _seed_transition(session, prior_satisfying=False)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    terminal = _append_terminal_transition(session, scenario)
    entry = session.get(AuditLog, receipt.audit_log_id)
    entry.after_json = {**entry.after_json, "policy_sha256": "0" * 64}
    session.flush([entry])

    worklist = build_reviewer_worklist(session, scenario.project.id)

    assert receipt.policy_approval_id is None
    assert worklist.reconfirmation == ()
    review = next(
        item
        for item in worklist.ordinary
        if item.dependency_id == scenario.dependency.id
    )
    assert review.successor_candidate_id == terminal.candidate.id
    assert review.reason == "reconfirmation_history_corrupt"


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [
        ("human", "automatic"),
        ("automatic", "human"),
        ("automatic", "automatic"),
    ],
)
def test_sequential_support_transfers_retain_original_human_lineage(
    session, first_kind, second_kind
):
    scenario = _seed_transition(session)
    if first_kind == "human":
        first_transition = TerminalTransition(
            document=scenario.successor,
            candidate=scenario.successor_candidate,
            run=scenario.successor_run,
            comparison=scenario.comparison,
            finding=scenario.finding,
        )
        _human_reconfirm(session, scenario, first_transition)
    else:
        [first_receipt] = run_automatic_carry_forward(
            session, scenario.project.id
        ).carried

    terminal = _append_terminal_transition(session, scenario)
    if second_kind == "human":
        _human_reconfirm(session, scenario, terminal)
    else:
        [second_receipt] = run_automatic_carry_forward(
            session, scenario.project.id
        ).carried

    transfer_entries = tuple(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id == scenario.dependency.id,
                AuditLog.action.in_(
                    (
                        audit.RECONFIRM_OPERATIVE_SUPPORT,
                        "automatic_carry_forward",
                    )
                ),
            )
            .order_by(AuditLog.id)
        ).all()
    )
    assert [entry.action for entry in transfer_entries] == [
        (
            audit.RECONFIRM_OPERATIVE_SUPPORT
            if first_kind == "human"
            else "automatic_carry_forward"
        ),
        (
            audit.RECONFIRM_OPERATIVE_SUPPORT
            if second_kind == "human"
            else "automatic_carry_forward"
        ),
    ]
    admission = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == audit.DEPENDENCY,
            AuditLog.entity_id == scenario.dependency.id,
            AuditLog.action == audit.ACCEPT_CANDIDATE,
        )
    ).one()
    assert all(
        entry.after_json["origin_admission_audit_id"] == admission.id
        for entry in transfer_entries
    )
    assert (
        transfer_entries[1].after_json["predecessor_support_transfer_audit_id"]
        == transfer_entries[0].id
    )
    support = resolve_operative_support(session, (scenario.dependency.id,))[
        scenario.dependency.id
    ]
    assert support.publication.document_id == terminal.document.id
    assert support.is_ready is True
    worklist = build_reviewer_worklist(session, scenario.project.id)
    assert worklist.reconfirmation == ()
    assert worklist.ordinary == ()

    if first_kind == "automatic":
        assert first_receipt.policy_approval_id is None
    if second_kind == "automatic":
        assert second_receipt.policy_approval_id is None


def test_machine_receipts_are_immutable(session):
    scenario = _seed_transition(session)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    receipt_type = type(receipt)

    with pytest.raises(IntegrityError, match="append-only|immutable"):
        with session.begin_nested():
            session.execute(
                update(receipt_type)
                .where(receipt_type.audit_log_id == receipt.audit_log_id)
                .values(after_json={})
            )
    with pytest.raises(IntegrityError, match="append-only|immutable"):
        with session.begin_nested():
            session.execute(
                delete(receipt_type).where(
                    receipt_type.audit_log_id == receipt.audit_log_id
                )
            )
    with pytest.raises(IntegrityError, match="append-only|immutable"):
        with session.begin_nested():
            session.execute(
                text(
                    "set constraints "
                    "policy_runs_must_match_outcomes, "
                    "automatic_carry_forward_outcomes_must_match_runs immediate"
                )
            )
            session.execute(
                text(f"truncate table {receipt_type.__tablename__} cascade")
            )

    assert session.get(receipt_type, receipt.audit_log_id) is not None


def test_database_rejects_a_receipt_bound_to_a_changed_finding(session):
    scenario = _seed_transition(session)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    receipt_type = type(receipt)
    finding_type = type(scenario.finding)
    values = {
        column.name: getattr(receipt, column.name)
        for column in receipt_type.__table__.columns
        if column.name != "created_at"
    }

    # Bypass the immutable-history triggers only to construct the corrupt
    # pre-insert state. The receipt trigger itself is restored before the act
    # under test and must reject the semantically unsafe binding.
    session.execute(text("set local session_replication_role = replica"))
    session.execute(
        delete(receipt_type).where(receipt_type.audit_log_id == receipt.audit_log_id)
    )
    session.execute(
        update(finding_type)
        .where(finding_type.id == scenario.finding.id)
        .values(state="changed")
    )
    session.execute(text("set local session_replication_role = origin"))

    with pytest.raises(IntegrityError, match="binding is invalid"):
        with session.begin_nested():
            session.execute(insert(receipt_type).values(**values))


def test_database_rejects_a_receipt_whose_evidence_is_not_the_exact_citation(
    session,
):
    scenario = _seed_transition(session)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    receipt_type = type(receipt)
    values = {
        column.name: getattr(receipt, column.name)
        for column in receipt_type.__table__.columns
        if column.name != "created_at"
    }

    session.execute(text("set local session_replication_role = replica"))
    session.execute(
        delete(receipt_type).where(receipt_type.audit_log_id == receipt.audit_log_id)
    )
    session.execute(
        update(EvidenceLink)
        .where(EvidenceLink.id == receipt.new_evidence_link_id)
        .values(quote="forged receipt citation")
    )
    session.execute(text("set local session_replication_role = origin"))

    with pytest.raises(IntegrityError, match="binding is invalid"):
        with session.begin_nested():
            session.execute(insert(receipt_type).values(**values))


def test_database_binds_receipt_citation_to_the_immutable_comparison_input(
    session,
):
    scenario = _seed_transition(session)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    assert scenario.successor_candidate is not None
    receipt_type = type(receipt)
    values = {
        column.name: getattr(receipt, column.name)
        for column in receipt_type.__table__.columns
        if column.name != "created_at"
    }
    forged_payload = deepcopy(scenario.successor_candidate.payload_json)
    forged_payload["citations"][0]["quote"] = "forged live candidate citation"

    session.execute(text("set local session_replication_role = replica"))
    session.execute(
        delete(receipt_type).where(receipt_type.audit_log_id == receipt.audit_log_id)
    )
    session.execute(
        update(Candidate)
        .where(Candidate.id == scenario.successor_candidate.id)
        .values(payload_json=forged_payload)
    )
    session.execute(
        update(EvidenceLink)
        .where(EvidenceLink.id == receipt.new_evidence_link_id)
        .values(quote="forged live candidate citation")
    )
    session.execute(text("set local session_replication_role = origin"))

    with pytest.raises(IntegrityError, match="binding is invalid"):
        with session.begin_nested():
            session.execute(insert(receipt_type).values(**values))


def test_database_rejects_a_receipt_with_an_unrelated_human_admission(session):
    scenario = _seed_transition(session)
    [receipt] = run_automatic_carry_forward(session, scenario.project.id).carried
    assert scenario.successor_candidate is not None
    unrelated_admission = audit.record(
        session,
        principal=REVIEWER,
        action=audit.ACCEPT_CANDIDATE,
        entity_type=audit.DEPENDENCY,
        entity_id=scenario.dependency.id,
        after={
            "candidate_id": scenario.successor_candidate.id,
            "fields": deepcopy(scenario.successor_candidate.payload_json["fields"]),
        },
    )
    receipt_type = type(receipt)
    act = session.get(AuditLog, receipt.audit_log_id)
    forged_after = deepcopy(act.after_json)
    forged_after["origin_admission_audit_id"] = unrelated_admission.id
    values = {
        column.name: getattr(receipt, column.name)
        for column in receipt_type.__table__.columns
        if column.name != "created_at"
    }
    values["origin_admission_audit_id"] = unrelated_admission.id
    values["after_json"] = forged_after

    session.execute(text("set local session_replication_role = replica"))
    session.execute(
        delete(receipt_type).where(receipt_type.audit_log_id == receipt.audit_log_id)
    )
    session.execute(
        update(AuditLog)
        .where(AuditLog.id == receipt.audit_log_id)
        .values(after_json=forged_after)
    )
    session.execute(text("set local session_replication_role = origin"))

    with pytest.raises(IntegrityError, match="binding is invalid"):
        with session.begin_nested():
            session.execute(insert(receipt_type).values(**values))


def test_a_late_readiness_refusal_leaves_zero_partial_writes(session, monkeypatch):
    scenario = _seed_transition(session, prior_satisfying=True)
    rendered_worklist = build_reviewer_worklist(session, scenario.project.id)
    [rendered_review] = rendered_worklist.reconfirmation

    # Model the row changing after the batch rendered its candidate but before
    # its mutation seam. The project lock prevents this in ordinary operation;
    # the forced seam proves even a late fail-closed decision happens before
    # Evidence, designation, or machine-audit writes.
    mark_satisfies(
        session,
        scenario.dependency.id,
        scenario.old_evidence.id,
        principal=REVIEWER,
    )
    before = _ledger_mutation_state(session, scenario)
    monkeypatch.setattr(
        automatic,
        "build_reviewer_worklist",
        lambda *_args, **_kwargs: rendered_worklist,
    )

    result = run_automatic_carry_forward(session, scenario.project.id)

    assert result.carried == ()
    assert {item.reason for item in result.abstentions} == {"readiness_source_changed"}
    assert _ledger_mutation_state(session, scenario) == before
    assert _automatic_entries(session, scenario.dependency.id) == ()


def test_released_policy_runs_write_durable_run_and_outcome_receipts(session):
    carry = _seed_transition(session)
    abstain = _seed_transition(session, successor_station="101+00")

    carry_result = run_automatic_carry_forward(session, carry.project.id)
    abstain_result = run_automatic_carry_forward(session, abstain.project.id)

    [carry_run] = session.scalars(
        select(PolicyRun)
        .where(PolicyRun.project_id == carry.project.id)
        .order_by(PolicyRun.id)
    ).all()
    [carry_outcome] = session.scalars(
        select(AutomaticCarryForwardOutcome)
        .where(AutomaticCarryForwardOutcome.project_id == carry.project.id)
        .order_by(AutomaticCarryForwardOutcome.id)
    ).all()
    [abstain_run] = session.scalars(
        select(PolicyRun)
        .where(PolicyRun.project_id == abstain.project.id)
        .order_by(PolicyRun.id)
    ).all()
    [abstain_outcome] = session.scalars(
        select(AutomaticCarryForwardOutcome)
        .where(AutomaticCarryForwardOutcome.project_id == abstain.project.id)
        .order_by(AutomaticCarryForwardOutcome.id)
    ).all()

    assert len(carry_result.carried) == 1
    assert carry_result.abstentions == ()
    assert carry_run.applied_count == 1
    assert carry_run.abstained_count == 0
    assert carry_outcome.run_id == carry_run.id
    assert carry_outcome.outcome == "carried"
    assert carry_outcome.reason is None
    assert carry_outcome.receipt_audit_log_id == carry_result.carried[0].audit_log_id

    assert abstain_result.carried == ()
    assert len(abstain_result.abstentions) == 1
    assert abstain_run.applied_count == 0
    assert abstain_run.abstained_count == 1
    assert abstain_outcome.run_id == abstain_run.id
    assert abstain_outcome.outcome == "abstained"
    assert abstain_outcome.reason == "comparison_changed"
    assert abstain_outcome.reason_version == ABSTENTION_REASON_VERSION
    assert abstain_outcome.receipt_audit_log_id is None


def test_rerunning_identical_dropped_abstention_does_not_duplicate_durable_outcomes(
    session,
):
    scenario = _seed_transition(session, successor_dropped=True)

    first = run_automatic_carry_forward(session, scenario.project.id)
    status_after_first = automatic_carry_forward_status(session, scenario.project.id)
    second = run_automatic_carry_forward(session, scenario.project.id)
    status_after_second = automatic_carry_forward_status(session, scenario.project.id)
    runs = _carry_runs(session, scenario.project.id)
    outcomes = _carry_outcomes(session, scenario.project.id)

    assert first.carried == ()
    assert {item.reason for item in first.abstentions} == {"comparison_dropped"}
    assert second.carried == ()
    assert {item.reason for item in second.abstentions} == {"comparison_dropped"}
    assert len(runs) == 2
    assert (runs[0].applied_count, runs[0].abstained_count) == (0, 1)
    assert (runs[1].applied_count, runs[1].abstained_count) == (0, 0)
    assert len(outcomes) == 1
    assert outcomes[0].reason == "comparison_dropped"
    assert outcomes[0].successor_candidate_id is None
    assert status_after_first.abstention_counts == {"comparison_dropped": 1}
    assert status_after_second.abstention_counts == {"comparison_dropped": 1}


def test_read_only_status_exposes_policy_eligibility_and_lifetime_carries(session):
    scenario = _seed_transition(session)
    before = _ledger_mutation_state(session, scenario)

    available = automatic_carry_forward_status(session, scenario.project.id)

    assert available.policy_version == POLICY_VERSION
    assert len(available.policy_sha256) == 64
    assert available.eligible_count == 1
    assert available.carried_count == 0
    assert available.abstention_reason_version == ABSTENTION_REASON_VERSION
    assert available.abstention_counts == {}
    assert _ledger_mutation_state(session, scenario) == before

    run_automatic_carry_forward(session, scenario.project.id)
    carried = automatic_carry_forward_status(session, scenario.project.id)
    assert carried.carried_count == 1
    assert carried.eligible_count == 0
    assert carried.abstention_counts == {}


def test_status_keeps_durable_abstentions_after_human_reconfirmation_clears_work(
    session,
):
    scenario = _seed_transition(session, successor_station="100 + 00")

    first = run_automatic_carry_forward(session, scenario.project.id)
    status_before_human = automatic_carry_forward_status(session, scenario.project.id)
    _human_reconfirm(
        session,
        scenario,
        TerminalTransition(
            document=scenario.successor,
            candidate=scenario.successor_candidate,
            run=scenario.successor_run,
            comparison=scenario.comparison,
            finding=scenario.finding,
        ),
    )

    status_after_human = automatic_carry_forward_status(session, scenario.project.id)

    assert first.carried == ()
    assert {item.reason for item in first.abstentions} == {"successor_fields_not_exact"}
    assert status_before_human.abstention_counts == {"successor_fields_not_exact": 1}
    assert status_after_human.eligible_count == 0
    assert status_after_human.abstention_counts == {"successor_fields_not_exact": 1}


def test_cross_session_human_reconfirmation_beats_a_stale_machine_runner():
    project_id: int | None = None
    with Session() as setup:
        scenario = _seed_transition(setup)
        project_id = scenario.project.id
        dependency_id = scenario.dependency.id
        evidence_before = tuple(
            setup.scalars(
                select(EvidenceLink.id)
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        )
        support_before = tuple(
            setup.execute(
                select(
                    OperativeSupport.id,
                    OperativeSupport.evidence_link_id,
                    OperativeSupport.role,
                    OperativeSupport.field_name,
                )
                .where(OperativeSupport.dependency_id == dependency_id)
                .order_by(OperativeSupport.id)
            ).all()
        )
        setup.commit()

    stale_machine = Session()
    human_reviewer = Session()
    try:
        [stale_review] = build_reviewer_worklist(
            stale_machine, project_id
        ).reconfirmation
        machine_status_before = automatic_carry_forward_status(
            stale_machine, project_id
        )
        assert machine_status_before.eligible_count == 1

        [human_review] = build_reviewer_worklist(
            human_reviewer,
            project_id,
        ).reconfirmation
        reconfirm_operative_support(
            human_reviewer,
            project_id=project_id,
            dependency_id=dependency_id,
            predecessor_document_id=human_review.predecessor_document_id,
            successor_candidate_id=human_review.successor_candidate_id,
            comparison_id=human_review.comparison_id,
            finding_id=human_review.finding_id,
            scope_fingerprint=human_review.scope_fingerprint,
            principal=REVIEWER,
        )
        human_reviewer.commit()

        evidence_ids_before_machine = tuple(
            stale_machine.scalars(
                select(EvidenceLink.id)
                .where(EvidenceLink.dependency_id == dependency_id)
                .order_by(EvidenceLink.id)
            ).all()
        )
        support_before_machine = tuple(
            stale_machine.execute(
                select(
                    OperativeSupport.id,
                    OperativeSupport.evidence_link_id,
                    OperativeSupport.role,
                    OperativeSupport.field_name,
                )
                .where(OperativeSupport.dependency_id == dependency_id)
                .order_by(OperativeSupport.id)
            ).all()
        )
        machine_audits_before = tuple(
            stale_machine.scalars(
                select(AuditLog.id)
                .where(
                    AuditLog.entity_type == audit.DEPENDENCY,
                    AuditLog.entity_id == dependency_id,
                    AuditLog.action == audit.AUTOMATIC_CARRY_FORWARD,
                )
                .order_by(AuditLog.id)
            ).all()
        )

        result = run_automatic_carry_forward(stale_machine, project_id)

        assert result.carried == ()
        assert result.abstentions == ()
        assert (
            tuple(
                stale_machine.scalars(
                    select(EvidenceLink.id)
                    .where(EvidenceLink.dependency_id == dependency_id)
                    .order_by(EvidenceLink.id)
                ).all()
            )
            == evidence_ids_before_machine
        )
        assert (
            tuple(
                stale_machine.execute(
                    select(
                        OperativeSupport.id,
                        OperativeSupport.evidence_link_id,
                        OperativeSupport.role,
                        OperativeSupport.field_name,
                    )
                    .where(OperativeSupport.dependency_id == dependency_id)
                    .order_by(OperativeSupport.id)
                ).all()
            )
            == support_before_machine
        )
        assert (
            tuple(
                stale_machine.scalars(
                    select(AuditLog.id)
                    .where(
                        AuditLog.entity_type == audit.DEPENDENCY,
                        AuditLog.entity_id == dependency_id,
                        AuditLog.action == audit.AUTOMATIC_CARRY_FORWARD,
                    )
                    .order_by(AuditLog.id)
                ).all()
            )
            == machine_audits_before
        )
        assert stale_review.successor_candidate_id is not None
        assert evidence_before != ()
        assert support_before != ()
    finally:
        stale_machine.rollback()
        stale_machine.close()
        human_reviewer.rollback()
        human_reviewer.close()
        if project_id is not None:
            _delete_committed_carry_forward_project(project_id)


@pytest.mark.parametrize(
    ("scenario_options", "reason"),
    [
        ({"successor_station": "101+00"}, "comparison_changed"),
        ({"successor_station": "100 + 00"}, "successor_fields_not_exact"),
    ],
)
def test_status_uses_the_same_versioned_abstention_reasons_as_execution(
    session, scenario_options, reason
):
    scenario = _seed_transition(session, **scenario_options)

    status = automatic_carry_forward_status(session, scenario.project.id)
    result = run_automatic_carry_forward(session, scenario.project.id)

    assert status.abstention_reason_version == ABSTENTION_REASON_VERSION
    assert status.abstention_counts == {reason: 1}
    assert status.eligible_count == 0
    assert {item.reason for item in result.abstentions} == {reason}
    assert {item.reason_version for item in result.abstentions} == {
        ABSTENTION_REASON_VERSION
    }


def test_database_rejects_an_abstained_outcome_with_unknown_reason_or_version(
    session,
):
    scenario = _seed_transition(session, successor_station="101+00")
    policy_status = automatic_carry_forward_status(session, scenario.project.id)
    [abstention] = run_automatic_carry_forward(session, scenario.project.id).abstentions

    with pytest.raises(IntegrityError, match="binding is invalid"):
        with session.begin_nested():
            run = PolicyRun(
                family="automatic-carry-forward",
                project_id=scenario.project.id,
                policy_approval_id=None,
                policy_version=policy_status.policy_version,
                policy_sha256=policy_status.policy_sha256,
                abstention_reason_version=ABSTENTION_REASON_VERSION,
                applied_count=0,
                abstained_count=1,
            )
            session.add(run)
            session.flush([run])
            session.add(
                AutomaticCarryForwardOutcome(
                    run_id=run.id,
                    project_id=scenario.project.id,
                    policy_approval_id=None,
                    dependency_id=abstention.dependency_id,
                    outcome="abstained",
                    reason="forged_reason",
                    reason_version="forged-version-v1",
                    receipt_audit_log_id=None,
                    comparison_id=abstention.comparison_id,
                    finding_id=abstention.finding_id,
                    predecessor_candidate_id=abstention.predecessor_candidate_id,
                    successor_candidate_id=abstention.successor_candidate_id,
                )
            )
            session.flush()


def test_deferred_run_count_constraints_reject_inflated_runs_and_extra_outcomes(
    session,
):
    carry = _seed_transition(session)
    count_drift = _seed_transition(session, successor_station="101+00")
    carry_policy = automatic_carry_forward_status(session, carry.project.id)
    count_drift_policy = automatic_carry_forward_status(session, count_drift.project.id)
    [receipt] = run_automatic_carry_forward(session, carry.project.id).carried

    with pytest.raises(
        IntegrityError, match="runs must reconcile their recorded outcomes"
    ):
        with session.begin_nested():
            inflated = PolicyRun(
                family="automatic-carry-forward",
                project_id=carry.project.id,
                policy_approval_id=None,
                policy_version=carry_policy.policy_version,
                policy_sha256=carry_policy.policy_sha256,
                abstention_reason_version=ABSTENTION_REASON_VERSION,
                applied_count=2,
                abstained_count=0,
            )
            session.add(inflated)
            session.flush([inflated])
            session.execute(
                text(
                    "set constraints "
                    "policy_runs_must_match_outcomes, "
                    "automatic_carry_forward_outcomes_must_match_runs immediate"
                )
            )

    with pytest.raises(
        IntegrityError, match="runs must reconcile their recorded outcomes"
    ):
        with session.begin_nested():
            run = PolicyRun(
                family="automatic-carry-forward",
                project_id=count_drift.project.id,
                policy_approval_id=None,
                policy_version=count_drift_policy.policy_version,
                policy_sha256=count_drift_policy.policy_sha256,
                abstention_reason_version=ABSTENTION_REASON_VERSION,
                applied_count=0,
                abstained_count=1,
            )
            session.add(run)
            session.flush([run])
            session.add(
                AutomaticCarryForwardOutcome(
                    run_id=run.id,
                    project_id=count_drift.project.id,
                    policy_approval_id=None,
                    dependency_id=count_drift.dependency.id,
                    outcome="abstained",
                    reason="comparison_changed",
                    reason_version=ABSTENTION_REASON_VERSION,
                    receipt_audit_log_id=None,
                    comparison_id=count_drift.comparison.id,
                    finding_id=count_drift.finding.id,
                    predecessor_candidate_id=count_drift.predecessor_candidate.id,
                    successor_candidate_id=count_drift.successor_candidate.id,
                )
            )
            session.add(
                AutomaticCarryForwardOutcome(
                    run_id=run.id,
                    project_id=count_drift.project.id,
                    policy_approval_id=None,
                    dependency_id=count_drift.dependency.id,
                    outcome="abstained",
                    reason="comparison_ambiguous",
                    reason_version=ABSTENTION_REASON_VERSION,
                    receipt_audit_log_id=None,
                    comparison_id=count_drift.comparison.id,
                    finding_id=count_drift.finding.id,
                    predecessor_candidate_id=count_drift.predecessor_candidate.id,
                    successor_candidate_id=count_drift.successor_candidate.id,
                )
            )
            session.flush()
            session.execute(
                text(
                    "set constraints "
                    "policy_runs_must_match_outcomes, "
                    "automatic_carry_forward_outcomes_must_match_runs immediate"
                )
            )
