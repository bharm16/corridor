"""Operator-facing compare-and-swap controls for Development Ledger retirement."""

from __future__ import annotations

from datetime import date
import hashlib
import json

import pytest
from sqlalchemy import func, select

from corridor.db import Session, engine
from corridor.extraction_runs import record_extraction_run
from corridor.legacy_ledger_archive import plan_retirement
from corridor.legacy_ledger_archive_cli import main
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    LegacyLedgerArchive,
    Project,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    transaction.rollback()
    connection.close()


class _OpenSession:
    """Use the test transaction without letting a CLI close its Session."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        session = self.session

        class Context:
            def __enter__(self):
                return session

            def __exit__(self, *_):
                return False

        return Context()


@pytest.fixture
def legacy_project(session):
    project = Project(
        slug="archive-cli-test",
        name="Archive CLI Test",
        agency="TxDOT",
        is_synthetic=False,
    )
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        registry_id="archive-cli-matrix",
        sha256="ac" * 32,
        filename="archive-cli-matrix.pdf",
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
            text="FOC1-1 AT&T Texas Telecom",
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
        title="Telecom - AT&T Texas",
        location_desc="IH 45",
        status="identified",
    )
    session.add(dependency)
    session.flush()
    evidence = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas Telecom",
        verified=True,
        satisfies_requirement=False,
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
    return project


def _json_output(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_plan_prints_stable_json_and_does_not_mutate_the_ledger(
    session, legacy_project, capsys
):
    factory = _OpenSession(session)

    assert main(["plan", legacy_project.slug], session_factory=factory) == 0
    first = capsys.readouterr()
    assert first.err == ""
    assert main(["plan", legacy_project.slug], session_factory=factory) == 0
    second = capsys.readouterr()

    assert second.err == ""
    assert first.out == second.out
    payload = json.loads(first.out)
    assert payload == {
        "content_sha256": plan_retirement(session, legacy_project.id).content_sha256,
        "counts": {
            "assertions": 1,
            "audit_log": 1,
            "dependencies": 1,
            "evidence_links": 1,
        },
        "project_id": legacy_project.id,
        "project_slug": legacy_project.slug,
        "ref_code_high_watermark": 141,
    }
    assert session.scalar(
        select(func.count(Dependency.id)).where(
            Dependency.project_id == legacy_project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == legacy_project.id
        )
    ) == 0


def test_plan_fails_cleanly_on_a_mixed_human_and_legacy_ledger(
    session, legacy_project, capsys
):
    document = session.scalars(
        select(Document).where(Document.project_id == legacy_project.id)
    ).one()
    candidate = Candidate(
        project_id=legacy_project.id,
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
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        model="legacy-model",
        citations_verified=True,
        state="accepted",
    )
    session.add(candidate)
    session.flush()
    dependency = Dependency(
        project_id=legacy_project.id,
        ref_code="DEP-00142",
        source_ref="FOC2-2",
        dep_type="utility_relocation",
        title="Telecom - Human Utility",
        location_desc="IH 45 frontage road",
        status="identified",
    )
    session.add(dependency)
    session.flush()
    evidence = EvidenceLink(
        dependency_id=dependency.id,
        document_id=document.id,
        page_no=1,
        quote="FOC1-1 AT&T Texas Telecom",
        verified=True,
        satisfies_requirement=False,
    )
    session.add(evidence)
    session.flush()
    session.add(
        Assertion(
            dependency_id=dependency.id,
            field_name="utility_id",
            asserted_value="FOC2-2",
            evidence_link_id=evidence.id,
            doc_date=document.doc_date,
        )
    )
    session.add(
        AuditLog(
            actor="local:alice",
            human_principal="local:alice",
            action="accept_candidate",
            entity_type="dependency",
            entity_id=dependency.id,
            after_json={
                "candidate_id": candidate.id,
                "ref_code": dependency.ref_code,
                "fields": {"utility_id": "FOC2-2"},
            },
        )
    )
    session.flush()

    assert (
        main(["plan", legacy_project.slug], session_factory=_OpenSession(session))
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attributable or unknown Admission" in captured.err
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == legacy_project.id
        )
    ) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["retire", "archive-cli-test"],
        [
            "retire",
            "archive-cli-test",
            "--expected-sha256",
            "0" * 64,
        ],
        [
            "retire",
            "archive-cli-test",
            "--expected-dependency-count",
            "1",
        ],
    ],
)
def test_retire_has_no_implicit_destructive_default(argv, capsys):
    class MustNotConnect:
        def __call__(self):
            raise AssertionError("argument validation must happen before DB access")

    assert main(argv, session_factory=MustNotConnect()) == 2
    assert "required" in capsys.readouterr().err


def test_retire_refuses_digest_drift_without_deleting_rows(
    session, legacy_project, capsys
):
    status = main(
        [
            "retire",
            legacy_project.slug,
            "--expected-sha256",
            "0" * 64,
            "--expected-dependency-count",
            "1",
        ],
        session_factory=_OpenSession(session),
    )

    assert status == 1
    assert "does not match" in capsys.readouterr().err
    assert session.scalar(
        select(func.count(Dependency.id)).where(
            Dependency.project_id == legacy_project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count(LegacyLedgerArchive.id)).where(
            LegacyLedgerArchive.project_id == legacy_project.id
        )
    ) == 0


def test_retire_verify_and_export_use_the_sealed_archive(
    session, legacy_project, tmp_path, capsys
):
    factory = _OpenSession(session)
    archived_slug = legacy_project.slug
    plan = plan_retirement(session, legacy_project.id)

    assert main(
        [
            "retire",
            archived_slug,
            "--expected-sha256",
            plan.content_sha256,
            "--expected-dependency-count",
            str(plan.counts["dependencies"]),
        ],
        session_factory=factory,
    ) == 0
    retired = _json_output(capsys)
    assert retired == {
        "archive_id": retired["archive_id"],
        "content_sha256": plan.content_sha256,
        "dependency_count": 1,
        "project_id": legacy_project.id,
        "project_slug": archived_slug,
    }
    assert session.scalar(
        select(func.count(Dependency.id)).where(
            Dependency.project_id == legacy_project.id
        )
    ) == 0

    archive_id = retired["archive_id"]
    # Operator readback is the sealed receipt, not mutable live metadata.
    legacy_project.slug = "archive-cli-renamed-after-retirement"
    session.flush([legacy_project])
    assert main(["verify", str(archive_id)], session_factory=factory) == 0
    verified = _json_output(capsys)
    assert verified == {
        "archive_id": archive_id,
        "content_sha256": plan.content_sha256,
        "counts": {
            "assertions": 1,
            "audit_log": 1,
            "dependencies": 1,
            "evidence_links": 1,
        },
        "format_version": "legacy-ledger-v2",
        "project_id": legacy_project.id,
        "project_slug": archived_slug,
        "ref_code_high_watermark": 141,
        "retired_by": "system:legacy-ledger-retirement/v1",
    }

    output = tmp_path / "archive.json"
    assert main(
        ["export", str(archive_id), str(output)], session_factory=factory
    ) == 0
    exported = _json_output(capsys)
    assert exported == {
        **verified,
        "path": str(output),
    }
    assert hashlib.sha256(output.read_bytes()).hexdigest() == plan.content_sha256


def test_unknown_project_and_archive_fail_without_a_traceback(session, capsys):
    factory = _OpenSession(session)

    assert main(["plan", "does-not-exist"], session_factory=factory) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main(["verify", "999999999"], session_factory=factory) == 1
    assert "does not exist" in capsys.readouterr().err
