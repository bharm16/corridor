"""Development Ledger retirement through one immutable archive seam."""

from __future__ import annotations

from datetime import date
import hashlib
import json

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from corridor.adjudicate import accept_candidate
from corridor.changes import diff_since_last
from corridor.db import Session, engine
from corridor.exceptions import evaluate_project
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.legacy_ledger_archive import (
    CorruptLegacyLedgerArchive,
    LegacyLedgerArchiveError,
    RETIREMENT_ACTOR,
    RetirementTargetDrift,
    export_archive,
    plan_retirement,
    retire_legacy_ledger,
    verify_archive,
)
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEvidenceSufficiency,
    DocPage,
    Document,
    EvidenceLink,
    LegacyLedgerArchive,
    OperativeSupport,
    Project,
    ReportRun,
)
from corridor.principals import HumanPrincipal
import corridor.legacy_ledger_archive as archive_module

DECLARER = HumanPrincipal("local:legacy-ledger-archive-declarer")


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def legacy_ledger(session):
    project = Project(
        slug="legacy-ledger-test",
        name="Legacy Ledger Test",
        agency="TxDOT",
        is_synthetic=False,
    )
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        registry_id="legacy-matrix",
        sha256="ab" * 32,
        filename="legacy-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
        doc_date=date(2026, 8, 1),
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=(
                "FOC1-1 AT&T Texas Telecom\n"
                "AT&T Texas will provide its design package in January 2027."
            ),
            text_source="text_layer",
        )
    )
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC1-1",
                "external_org": "AT&T Texas",
                "utility_type": "Telecom",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        extraction_run_id=None,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        model="legacy-model",
        citations_verified=True,
        state="pending",
    )
    session.add(candidate)
    session.flush()
    record_extraction_run(
        session,
        document,
        prompt_version="txdot_ucm_v1",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="legacy-model",
        schema_version="dependency-v1",
    )
    candidate.state = "accepted"
    dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00141",
        source_ref="FOC1-1",
        dep_type="utility_relocation",
        title="Telecom — AT&T Texas",
        location_desc="IH 45",
    )
    session.add(dependency)
    session.flush()
    evidence = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas Telecom",
        verified=True,
    )
    session.add(evidence)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="utility_id",
            asserted_value="FOC1-1",
            evidence_link_id=evidence.id,
            doc_date=document.doc_date,
        )
    )
    session.add(
        AuditLog(
            actor="agent",
            human_principal=None,
            action="accept_candidate",
            entity_type="dependency",
            entity_id=dependency.id,
            after_json={
                "candidate_id": candidate.id,
                "ref_code": dependency.ref_code,
                "fields": {"utility_id": "FOC1-1"},
            },
        )
    )
    session.flush()
    return project, document, candidate, dependency


def test_plan_is_read_only_and_names_the_exact_legacy_graph(session, legacy_ledger):
    project, document, candidate, dependency = legacy_ledger

    plan = plan_retirement(session, project.id)

    assert plan.counts == {
        "dependencies": 1,
        "assertions": 1,
        "evidence_links": 1,
        "audit_log": 1,
    }
    assert plan.ref_code_high_watermark == 141
    assert len(plan.content_sha256) == 64
    assert plan.content["project"]["slug"] == "legacy-ledger-test"
    assert plan.content["dependencies"][0]["id"] == dependency.id
    assert plan.content["evidence_links"][0]["document_id"] == document.id
    assert plan.content["audit_log"][0]["actor"] == "agent"
    assert plan.content["audit_log"][0]["human_principal"] is None
    assert plan.content["originating_candidates"][0]["id"] == candidate.id
    assert session.get(Dependency, dependency.id) is dependency


def test_unrelated_candidate_backlog_does_not_change_digest_or_block_retirement(
    session, legacy_ledger
):
    project, document, candidate, _ = legacy_ledger
    unrelated = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC9-9",
                "external_org": "Unrelated Telecom",
                "utility_type": "Telecom",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        extraction_run_id=None,
        source_pages=[1],
        confidence=0.25,
        prompt_version="txdot_ucm_v1",
        model="legacy-model",
        citations_verified=True,
        state="pending",
    )
    session.add(unrelated)
    session.flush()

    plan = plan_retirement(session, project.id)
    assert "candidate_counts" not in plan.content
    assert [row["id"] for row in plan.content["originating_candidates"]] == [
        candidate.id
    ]

    unrelated.state = "rejected"
    session.flush()

    replanned = plan_retirement(session, project.id)
    assert replanned.content == plan.content
    assert replanned.content_sha256 == plan.content_sha256

    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )

    session.refresh(unrelated)
    assert unrelated.state == "rejected"
    assert session.get(Candidate, unrelated.id) is unrelated


def test_retirement_seals_the_graph_and_only_removes_active_ledger_rows(
    session, legacy_ledger
):
    project, _, candidate, dependency = legacy_ledger
    report = ReportRun(
        project_id=project.id,
        ruleset_version="v0.3",
        snapshot_json={
            "ruleset_version": "v0.3",
            "dependencies": {dependency.ref_code: {"id": dependency.id}},
        },
    )
    session.add(report)
    session.flush()
    original_candidate = (
        candidate.state,
        candidate.payload_json,
        candidate.extraction_run_id,
        candidate.adjudicated_at,
    )
    original_audit_ids = tuple(
        session.scalars(
            select(AuditLog.id)
            .where(
                AuditLog.entity_type == "dependency",
                AuditLog.entity_id == dependency.id,
            )
            .order_by(AuditLog.id)
        ).all()
    )
    plan = plan_retirement(session, project.id)

    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    readback = verify_archive(session, archive.id)

    assert isinstance(archive, LegacyLedgerArchive)
    assert readback.content == plan.content
    assert readback.archive.content_sha256 == plan.content_sha256
    assert session.scalar(
        select(func.count(Dependency.id)).where(Dependency.project_id == project.id)
    ) == 0
    assert session.scalar(
        select(func.count(Assertion.id)).where(
            Assertion.dependency_id == dependency.id
        )
    ) == 0
    assert session.scalar(
        select(func.count(EvidenceLink.id)).where(
            EvidenceLink.dependency_id == dependency.id
        )
    ) == 0
    session.refresh(candidate)
    assert (
        candidate.state,
        candidate.payload_json,
        candidate.extraction_run_id,
        candidate.adjudicated_at,
    ) == original_candidate
    assert tuple(
        session.scalars(
            select(AuditLog.id)
            .where(AuditLog.id.in_(original_audit_ids))
            .order_by(AuditLog.id)
        ).all()
    ) == original_audit_ids
    assert session.get(ReportRun, report.id) is report

    repeated = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    assert repeated.id == archive.id


def test_retirement_archives_and_deletes_events_support_and_ready_evidence(
    session, legacy_ledger
):
    project, document, _, dependency = legacy_ledger
    event = DependencyEvent(
        project_id=project.id,
        affected_external_org_id=dependency.external_org_id,
        stated_external_org_id=dependency.external_org_id,
        scope_mode="selected",
        event_type="commitment",
        event_date=date(2026, 8, 2),
        description="AT&T committed to relocate by August 15",
        created_by="local:archive-tester",
    )
    session.add(event)
    session.flush()
    session.add(DependencyEventScope(event_id=event.id, dependency_id=dependency.id))
    session.flush()
    ready_evidence = session.scalars(
        select(EvidenceLink).where(EvidenceLink.dependency_id == dependency.id)
    ).one()
    session.add(
        DependencyEvidenceSufficiency(
            dependency_id=dependency.id,
            evidence_link_id=ready_evidence.id,
            scope_link_id=None,
        )
    )
    event_evidence = EvidenceLink(
        dependency_id=None,
        document_id=document.id,
        page_no=1,
        quote="AT&T committed to relocate by August 15",
        verified=True,
    )
    session.add(event_evidence)
    session.flush()
    session.add(
        DependencyEventEvidence(
            event_id=event.id,
            evidence_link_id=event_evidence.id,
            recorded_by="local:archive-tester",
        )
    )
    support = OperativeSupport(
        dependency_id=dependency.id,
        evidence_link_id=ready_evidence.id,
        role="publication",
        field_name=None,
        designated_by="agent",
    )
    session.add(support)
    session.flush()

    plan = plan_retirement(session, project.id)

    [archived_event] = plan.content["dependency_events"]
    assert archived_event["id"] == event.id
    assert archived_event["event_type"] == "commitment"
    assert archived_event["scope_mode"] == "selected"
    assert archived_event["event_date"] == "2026-08-02"
    assert archived_event["description"] == "AT&T committed to relocate by August 15"
    [archived_scope] = plan.content["dependency_event_scopes"]
    [archived_scope_decision] = plan.content["dependency_event_scope_decisions"]
    assert archived_scope == {
        "id": archived_scope["id"],
        "event_id": event.id,
        "scope_decision_id": archived_scope_decision["id"],
        "dependency_id": dependency.id,
        "recorded_by": "local:archive-tester",
    }
    assert archived_scope_decision == {
        "id": archived_scope_decision["id"],
        "event_id": event.id,
        "scope_mode": "selected",
        "supersedes_scope_decision_id": None,
        "decided_by": "local:archive-tester",
        "created_at": archived_scope_decision["created_at"],
    }
    assert plan.content["operative_support"] == [
        {
            "id": support.id,
            "dependency_id": dependency.id,
            "evidence_link_id": ready_evidence.id,
            "scope_link_id": None,
            "role": "publication",
            "field_name": None,
            "designated_by": "agent",
            "designated_at": plan.content["operative_support"][0][
                "designated_at"
            ],
        }
    ]
    assert all(
        "satisfies_requirement" not in row and "event_id" not in row
        for row in plan.content["evidence_links"]
    )
    assert plan.content["dependency_evidence_sufficiencies"] == [
        {
            "id": plan.content["dependency_evidence_sufficiencies"][0]["id"],
            "dependency_id": dependency.id,
            "evidence_link_id": ready_evidence.id,
            "scope_link_id": None,
            "created_at": plan.content["dependency_evidence_sufficiencies"][0][
                "created_at"
            ],
        }
    ]
    assert plan.content["dependency_event_evidence"] == [
        {
            "evidence_link_id": event_evidence.id,
            "event_id": event.id,
            "recorded_by": "local:archive-tester",
            "created_at": plan.content["dependency_event_evidence"][0][
                "created_at"
            ],
        }
    ]

    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    readback = verify_archive(session, archive.id)

    assert readback.content == plan.content
    assert session.scalar(
        select(func.count(DependencyEvent.id)).where(
            DependencyEvent.project_id == project.id
        )
    ) == 0
    assert session.scalar(
        select(func.count(OperativeSupport.id)).where(
            OperativeSupport.dependency_id == dependency.id
        )
    ) == 0
    assert session.scalar(
        select(func.count(EvidenceLink.id)).where(
            EvidenceLink.dependency_id == dependency.id
        )
    ) == 0


def test_retirement_archives_and_deletes_unknown_scope_statements(
    session, legacy_ledger
):
    from corridor.external_statements import (
        CitedStatementEvidence,
        StatementScope,
        StatementTiming,
        record_external_party_statement,
    )
    from corridor.models import ExternalOrg

    project, document, _, _ = legacy_ledger
    party = ExternalOrg(name="AT&T Texas")
    session.add(party)
    session.flush()
    statement = record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party="AT&T Texas",
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=None,
        description="AT&T Texas will provide its design package in January 2027.",
        new_timing=StatementTiming.month("January 2027", 2027, 1),
        scope=StatementScope.unknown(),
        created_by="corridor:event-admission",
        evidence=CitedStatementEvidence(
            document_id=document.id,
            page_no=1,
            quote="AT&T Texas will provide its design package in January 2027.",
        ),
    )

    plan = plan_retirement(session, project.id)
    assert [event["id"] for event in plan.content["dependency_events"]] == [
        statement.id
    ]
    assert plan.content["dependency_event_scopes"] == []
    assert plan.content["dependency_event_evidence"] == [
        {
            "evidence_link_id": plan.content["dependency_event_evidence"][0][
                "evidence_link_id"
            ],
            "event_id": statement.id,
            "recorded_by": "corridor:event-admission",
            "created_at": plan.content["dependency_event_evidence"][0][
                "created_at"
            ],
        }
    ]

    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )

    assert session.get(DependencyEvent, statement.id) is None
    assert session.scalar(
        select(func.count(DependencyEventEvidence.evidence_link_id)).where(
            DependencyEventEvidence.event_id == statement.id
        )
    ) == 0


def test_plan_and_archive_preserve_additional_direct_dependency_audit_history(
    session, legacy_ledger
):
    project, _, _, dependency = legacy_ledger
    extra_audit = AuditLog(
        actor="agent",
        human_principal=None,
        action="mark_satisfies_requirement",
        entity_type="dependency",
        entity_id=dependency.id,
        before_json={"satisfies_requirement": False},
        after_json={"satisfies_requirement": True},
    )
    session.add(extra_audit)
    session.flush()

    plan = plan_retirement(session, project.id)

    direct_entries = [
        row
        for row in plan.content["audit_log"]
        if row["entity_type"] == "dependency" and row["entity_id"] == dependency.id
    ]
    assert len(direct_entries) == 2
    assert direct_entries[0]["action"] == "accept_candidate"
    assert direct_entries[1] == {
        "id": extra_audit.id,
        "actor": "agent",
        "human_principal": None,
        "action": "mark_satisfies_requirement",
        "entity_type": "dependency",
        "entity_id": dependency.id,
        "before_json": {"satisfies_requirement": False},
        "after_json": {"satisfies_requirement": True},
        "ts": direct_entries[1]["ts"],
    }

    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    readback = verify_archive(session, archive.id)

    assert readback.content == plan.content
    assert readback.archive.audit_log_count == 2


def test_retirement_flushes_pending_session_state_before_global_refresh(
    session, legacy_ledger, monkeypatch
):
    """The fresh post-lock snapshot must not erase unrelated caller edits."""

    project, _, _, _ = legacy_ledger
    unrelated = Project(
        slug="archive-unrelated-project",
        name="Before retirement",
        is_synthetic=True,
    )
    session.add(unrelated)
    session.flush([unrelated])
    plan = plan_retirement(session, project.id)
    lock_project = archive_module.lock_project

    def lock_then_dirty(db, project_id):
        lock_project(db, project_id)
        unrelated.name = "Pending edit survives"

    monkeypatch.setattr(archive_module, "lock_project", lock_then_dirty)

    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )

    session.refresh(unrelated)
    assert unrelated.name == "Pending edit survives"


def test_retirement_appends_one_honestly_attributed_project_audit(
    session, legacy_ledger
):
    project, _, _, _ = legacy_ledger
    plan = plan_retirement(session, project.id)

    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )

    [entry] = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "project",
            AuditLog.entity_id == project.id,
            AuditLog.action == "retire_legacy_ledger",
        )
    ).all()
    assert entry.actor == RETIREMENT_ACTOR
    assert entry.human_principal is None
    assert entry.before_json == {
        "dependency_count": 1,
        "content_sha256": plan.content_sha256,
    }
    assert entry.after_json == {
        "archive_id": archive.id,
        "dependency_count": 0,
        "content_sha256": plan.content_sha256,
    }


def test_retired_reference_codes_are_never_reused(session, legacy_ledger):
    project, document, _, _ = legacy_ledger
    plan = plan_retirement(session, project.id)
    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
    )
    candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC1-1",
                "external_org": "AT&T Texas",
                "utility_type": "Telecom",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="post-retirement-v1",
        model="test-model",
        citations_verified=True,
        state="pending",
    )
    session.add(candidate)
    session.flush()
    run = record_extraction_run(
        session,
        document,
        prompt_version="post-retirement-v1",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-model",
        schema_version="dependency-v1",
    )
    declare_active_run(session, document.id, run.id, principal=DECLARER)

    dependency = accept_candidate(
        session,
        candidate,
        principal=HumanPrincipal("local:post-retirement-reviewer"),
    )

    assert dependency.ref_code == "DEP-00142"


def test_retirement_starts_a_new_report_history_boundary(session, legacy_ledger):
    project, _, _, dependency = legacy_ledger
    previous = ReportRun(
        project_id=project.id,
        ruleset_version="v0.3",
        snapshot_json={
            "ruleset_version": "v0.3",
            "dependencies": {dependency.ref_code: {"id": dependency.id}},
        },
    )
    session.add(previous)
    session.flush()
    plan = plan_retirement(session, project.id)
    retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
    )

    diff = diff_since_last(
        session,
        project.id,
        evaluation=evaluate_project(session, project.id),
    )

    assert diff.is_first_report
    assert diff.changes == []


def test_export_is_the_exact_canonical_content_covered_by_the_digest(
    session, legacy_ledger, tmp_path
):
    project, _, _, _ = legacy_ledger
    plan = plan_retirement(session, project.id)
    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
    )

    path = export_archive(session, archive.id, tmp_path / "legacy-ledger.json")
    exported = path.read_bytes()

    assert hashlib.sha256(exported).hexdigest() == archive.content_sha256
    assert json.loads(exported)["audit_log"][0]["actor"] == "agent"


def test_digest_drift_aborts_before_archive_or_retirement(session, legacy_ledger):
    project, _, _, dependency = legacy_ledger
    plan = plan_retirement(session, project.id)
    dependency.title = "changed after planning"
    session.flush()

    with pytest.raises(RetirementTargetDrift, match="expected digest"):
        retire_legacy_ledger(
            session,
            project.id,
            expected_sha256=plan.content_sha256,
        )

    assert session.get(Dependency, dependency.id) is dependency
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == project.id
        )
    ) == 0


def test_failure_after_deletions_rolls_back_archive_rows_and_audit(
    session, legacy_ledger, monkeypatch
):
    project, _, _, dependency = legacy_ledger
    plan = plan_retirement(session, project.id)

    def fail_retirement_audit(*_args, **_kwargs):
        raise RuntimeError("injected retirement audit failure")

    monkeypatch.setattr(archive_module.audit, "record", fail_retirement_audit)

    with pytest.raises(RuntimeError, match="injected retirement audit failure"):
        retire_legacy_ledger(
            session,
            project.id,
            expected_sha256=plan.content_sha256,
            expected_dependency_count=1,
        )

    assert session.get(Dependency, dependency.id) is dependency
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == project.id
        )
    ) == 0
    assert session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_type == "project",
            AuditLog.entity_id == project.id,
            AuditLog.action == "retire_legacy_ledger",
        )
    ) == 0


def test_empty_without_archive_and_archive_plus_new_rows_fail_closed(
    session, legacy_ledger
):
    project, _, _, _ = legacy_ledger
    plan = plan_retirement(session, project.id)
    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
        expected_dependency_count=1,
    )
    # Simulate the otherwise impossible pre-receipt empty state in a distinct
    # project rather than weakening or deleting the immutable real archive.
    empty_project = Project(
        slug="empty-ledger-without-archive",
        name="Empty Ledger Without Archive",
        is_synthetic=False,
    )
    session.add(empty_project)
    session.flush()
    with pytest.raises(LegacyLedgerArchiveError, match="no active Ledger"):
        retire_legacy_ledger(
            session,
            empty_project.id,
            expected_sha256="0" * 64,
            expected_dependency_count=1,
        )

    new_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00142",
        dep_type="utility_relocation",
        title="Illegitimate post-retirement row",
    )
    session.add(new_dependency)
    session.flush()
    with pytest.raises(LegacyLedgerArchiveError, match="active Dependencies"):
        retire_legacy_ledger(
            session,
            project.id,
            expected_sha256=archive.content_sha256,
            expected_dependency_count=1,
        )


def test_retirement_refuses_to_capture_a_human_admission(session, legacy_ledger):
    project, _, _, dependency = legacy_ledger
    admission = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "dependency",
            AuditLog.entity_id == dependency.id,
        )
    ).one()
    admission.actor = "local:alice"
    admission.human_principal = "local:alice"
    session.flush()

    with pytest.raises(LegacyLedgerArchiveError, match="attributable"):
        plan_retirement(session, project.id)

    assert session.get(Dependency, dependency.id) is dependency
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == project.id
        )
    ) == 0


def test_plan_fails_closed_on_a_mixed_human_and_legacy_ledger(
    session, legacy_ledger
):
    project, document, _, _ = legacy_ledger
    human_candidate = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": "FOC2-2",
                "external_org": "Human Utility",
                "utility_type": "Telecom",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": "FOC1-1 AT&T Texas Telecom",
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        extraction_run_id=None,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        model="legacy-model",
        citations_verified=True,
        state="accepted",
    )
    session.add(human_candidate)
    session.flush()
    human_dependency = Dependency(
        project_id=project.id,
        ref_code="DEP-00142",
        source_ref="FOC2-2",
        dep_type="utility_relocation",
        title="Telecom - Human Utility",
        location_desc="IH 45 frontage road",
    )
    session.add(human_dependency)
    session.flush()
    human_evidence = EvidenceLink(
        dependency_id=human_dependency.id,
        document_id=document.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas Telecom",
        verified=True,
    )
    session.add(human_evidence)
    session.flush()
    session.add(
        Assertion(
            dependency_id=human_dependency.id,
            field_name="utility_id",
            asserted_value="FOC2-2",
            evidence_link_id=human_evidence.id,
            doc_date=document.doc_date,
        )
    )
    session.add(
        AuditLog(
            actor="local:alice",
            human_principal="local:alice",
            action="accept_candidate",
            entity_type="dependency",
            entity_id=human_dependency.id,
            after_json={
                "candidate_id": human_candidate.id,
                "ref_code": human_dependency.ref_code,
                "fields": {"utility_id": "FOC2-2"},
            },
        )
    )
    session.flush()

    with pytest.raises(LegacyLedgerArchiveError, match="attributable or unknown"):
        plan_retirement(session, project.id)

    assert session.scalar(
        select(func.count(Dependency.id)).where(Dependency.project_id == project.id)
    ) == 2
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == project.id
        )
    ) == 0


def test_verify_rejects_a_receipt_whose_content_does_not_match_its_digest(session):
    project = Project(
        slug="corrupt-legacy-archive",
        name="Corrupt Legacy Archive",
        is_synthetic=False,
    )
    session.add(project)
    session.flush()
    archive = LegacyLedgerArchive(
        project_id=project.id,
        format_version="legacy-ledger-v1",
        content_json={},
        content_sha256="0" * 64,
        dependency_count=0,
        assertion_count=0,
        evidence_link_count=0,
        audit_log_count=0,
        ref_code_high_watermark=0,
        retired_by="system:test",
    )
    session.add(archive)
    session.flush()

    with pytest.raises(CorruptLegacyLedgerArchive, match="digest does not match"):
        verify_archive(session, archive.id)


def test_verify_reads_a_slip_era_archive_without_reviving_slip_writes(session):
    project = Project(
        slug="slip-era-legacy-archive",
        name="Slip-era Legacy Archive",
        is_synthetic=False,
    )
    session.add(project)
    session.flush()
    content = {
        "project": {"id": project.id, "slug": project.slug},
        "dependencies": [],
        "assertions": [],
        "evidence_links": [],
        "audit_log": [],
        "dependency_events": [{"event_type": "slip", "id": 44}],
        "ref_code_high_watermark": 0,
    }
    archive = LegacyLedgerArchive(
        project_id=project.id,
        format_version="legacy-ledger-v1",
        content_json=content,
        content_sha256=archive_module._content_sha256(content),
        dependency_count=0,
        assertion_count=0,
        evidence_link_count=0,
        audit_log_count=0,
        ref_code_high_watermark=0,
        retired_by="system:test",
    )
    session.add(archive)
    session.flush()

    assert verify_archive(session, archive.id).content["dependency_events"] == [
        {"event_type": "slip", "id": 44}
    ]


@pytest.mark.parametrize("mutation", ["update", "delete", "truncate"])
def test_database_rejects_archive_mutation(session, legacy_ledger, mutation):
    project, _, _, _ = legacy_ledger
    plan = plan_retirement(session, project.id)
    archive = retire_legacy_ledger(
        session,
        project.id,
        expected_sha256=plan.content_sha256,
    )
    if mutation == "update":
        statement = (
            update(LegacyLedgerArchive)
            .where(LegacyLedgerArchive.id == archive.id)
            .values(retired_by="rewritten")
        )
    elif mutation == "delete":
        statement = delete(LegacyLedgerArchive).where(
            LegacyLedgerArchive.id == archive.id
        )
    else:
        statement = text("truncate table legacy_ledger_archives")

    with pytest.raises(DBAPIError, match="Legacy Ledger archives are immutable"):
        with session.begin_nested():
            session.execute(statement)
