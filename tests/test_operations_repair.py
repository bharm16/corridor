"""Operations repairs a blocked source under policy, and leaves a receipt (#842).

The 2026-09-10 customer-journey audit found a processing failure could become a
permanent dead end: the standing pass excluded a failed parse and a permanently
unreadable reading for ever, the legacy operations screens are outside the
pilot boundary, and the coordinator's page could name the owner of a blocked
source with no act behind that name.

These tests hold both halves of the repair. The first half is that the exit
exists and is attributable: the designation is read from the roster, the act
writes one receipt, the standing pass takes the source again because of that
receipt, and the register prints what was done. The second half is the three
things a repair may not do, each proved by the refusal it raises and by the
record it left alone -- a held document stays held, a reading refused for want
of a customer authorization is not re-read, and a source that was read is not
re-read to change what its captured facts say.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select

from corridor import access, audit, web_boundary
from corridor.config import settings
from corridor.extraction_runs import record_extraction_run
from corridor.intake_hardening import quarantine_document
from corridor.models import (
    AuditLog,
    Document,
    DocumentQuarantine,
    Fact,
    PageProcessingFailure,
    ProposedDelta,
    SourceSegment,
)
from corridor.operations_repair import (
    CORRECTED_MAPPING,
    RETRY_PROCESSING,
    OperationsRepairRefused,
    repair_source_processing,
)
from corridor.principals import HumanPrincipal
from corridor.project_processing import _eligible_documents
from corridor.provider_authorization import AUTHORIZATION_ABSENT
from corridor.source_register import read_source_register

from access_support import seed_membership
from later_revision_support import BASELINE_ROWS, PRINCIPAL, adopt, workbook_bytes
from pdf_fixture_support import PdfFixture

OPERATOR = HumanPrincipal("local:sam-okafor")
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def project(member_project):
    """One project whose acting principal is a designated technical operator."""

    return member_project(OPERATOR, designations=(access.TECHNICAL_OPERATIONS,))


def _pdf() -> bytes:
    fixture = PdfFixture()
    page = fixture.add_page(height=180)
    page.text((72, 100), "Utility Conflict Matrix")
    page.text((72, 130), "FOC1-1  AT&T Texas  Telecom  STA 1149+00 to 1153+17")
    return fixture.tobytes()


def _source(
    session,
    project,
    *,
    marker="a",
    parse_status="parsed",
    body: bytes | None = None,
):
    """One registered source, with its bytes in the store when it needs them.

    ``marker`` only varies the placeholder bytes: ``documents`` is unique on
    (project, digest), so two sources in one project are two different files.
    """

    payload = body if body is not None else f"%PDF-1.4 placeholder {marker}\n".encode()
    sha = hashlib.sha256(payload).hexdigest()
    shard = Path(settings.corpus_store) / sha[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f"{sha}.pdf").write_bytes(payload)
    document = Document(
        project_id=project.id,
        sha256=sha,
        filename=f"{sha[:8]}.pdf",
        doc_type="matrix",
        parse_status=parse_status,
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _run(session, document, outcome):
    return record_extraction_run(
        session,
        document,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0 if outcome == "completed" else 1,
        outcome=outcome,
        model=MODEL,
        schema_version=SCHEMA_VERSION,
        allow_unsealed_legacy=True,
    )


def _receipts(session, document):
    return session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == audit.REPAIR_SOURCE_PROCESSING,
            AuditLog.entity_id == document.id,
        )
    )


def _row(session, project, document):
    register = read_source_register(session, project_id=project.id)
    return next(row for row in register.rows if row.document_id == document.id)


def test_a_permanently_unreadable_source_is_taken_again_after_a_repair(
    session, project, store
):
    """The dead end, and the one exit from it.

    ``_eligible_documents`` is the selection under test and is asked directly:
    the alternative is a committed multi-transaction pass over its own
    database, which proves the same rule far more expensively. Its
    ``excluded`` accounting is what ``process_project`` reports, so the two
    readings are the same one.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    session.flush()

    eligible, excluded = _eligible_documents(session, project.id)
    assert [item.id for item in eligible] == []
    assert excluded["unreadable_permanent"] == 1

    outcome = repair_source_processing(
        session, document_id=document.id, principal=OPERATOR
    )

    assert outcome.blocked_by == "unreadable_permanent"
    assert outcome.outcome == "readmitted"
    assert _receipts(session, document) == 1

    eligible, excluded = _eligible_documents(session, project.id)
    assert [item.id for item in eligible] == [document.id]
    assert excluded["unreadable_permanent"] == 0


def test_a_source_that_fails_the_same_way_again_is_excluded_again(
    session, project, store
):
    """A receipt is not a permanent exemption from being excluded again.

    It names the reading it repaired, so it re-admits the source once. A second
    identical failure is a newer reading the receipt does not cover, and the
    pass stops taking the source again rather than reading it for ever because
    one repair exists somewhere in its history.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    session.flush()
    repair_source_processing(session, document_id=document.id, principal=OPERATOR)
    assert [item.id for item in _eligible_documents(session, project.id)[0]] == [
        document.id
    ]

    _run(session, document, "unreadable")
    session.flush()

    eligible, excluded = _eligible_documents(session, project.id)
    assert [item.id for item in eligible] == []
    assert excluded["unreadable_permanent"] == 1


def test_a_failed_parse_is_repaired_by_the_bounded_reparse(session, project, store):
    """The other permanent exclusion, and the act #350 already built for it.

    The repair delegates rather than re-parsing itself, so both entries are in
    the record: what operations asked for, and what the parse did.
    """

    document = _source(session, project, parse_status="failed", body=_pdf())

    outcome = repair_source_processing(
        session, document_id=document.id, principal=OPERATOR
    )

    assert outcome.blocked_by == "failed_parse"
    assert outcome.outcome == "recovered"
    assert document.parse_status == "parsed"
    assert _receipts(session, document) == 1
    assert session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == audit.RECOVER_DOCUMENT_PARSE,
            AuditLog.entity_id == document.id,
        )
    ) == 1


def test_repairing_a_source_needs_the_technical_operations_designation(
    session, member_project, store
):
    """Membership is not authority, and the roster is read live.

    The same principal is refused as an ordinary member and admitted as a
    designated operator, so what changes the answer is the designation rather
    than anything cached about the session.
    """

    project = member_project(OPERATOR, designations=())
    document = _source(session, project)
    _run(session, document, "unreadable")
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        repair_source_processing(
            session, document_id=document.id, principal=OPERATOR
        )
    assert refused.value.reason == "not_designated"
    assert _receipts(session, document) == 0

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    assert (
        repair_source_processing(
            session, document_id=document.id, principal=OPERATOR
        ).outcome
        == "readmitted"
    )


def test_a_repair_does_not_release_a_held_document(session, project, store):
    """Refusal one: a malware finding and every other hold keep their own act.

    The repair calls ``intake_hardening``'s existing gate rather than a second
    opinion about what is held, and the quarantine row is still there
    afterwards: there is no generic release-quarantine control here.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    quarantine_document(
        session, document.id, "malware_detected: threat detected (EICAR-Test-Signature)"
    )
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        repair_source_processing(
            session, document_id=document.id, principal=OPERATOR
        )

    assert refused.value.reason == "held_in_quarantine"
    assert "malware_detected" in str(refused.value)
    assert session.get(DocumentQuarantine, document.id) is not None
    assert _receipts(session, document) == 0
    # And the pass still will not take it, for the reason it always would not.
    assert _eligible_documents(session, project.id)[1]["held_quarantined"] == 1


def test_a_reading_refused_for_want_of_an_authorization_is_not_retried(
    session, project, store
):
    """Refusal two: what is missing is a signed customer decision, not a retry.

    The retained Processing Failure carries the provider boundary's own reason,
    so this asks the record rather than re-deriving whether the project is
    authorized. Reading the source again would reach the same boundary and be
    refused by it again, and the repair says so instead of pretending to fix it.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    session.add(
        PageProcessingFailure(
            document_id=document.id,
            page_number=1,
            engine="textract",
            configuration_json={},
            region_id="page-1",
            scope_json={},
            error_type=AUTHORIZATION_ABSENT,
            error_message="no customer authorization was given",
        )
    )
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        repair_source_processing(
            session, document_id=document.id, principal=OPERATOR
        )

    assert refused.value.reason == "authorization_missing"
    assert AUTHORIZATION_ABSENT in str(refused.value)
    assert "signed" in str(refused.value)
    assert _receipts(session, document) == 0
    assert [item.id for item in _eligible_documents(session, project.id)[0]] == []


def test_a_repair_never_changes_what_a_captured_fact_says(session, project, store):
    """Refusal three: source-fact semantics are not reachable from here.

    Two halves. A source that was read is refused outright, because "read it
    again" is not how a capture that is wrong about its source is put right.
    And a repair that does run writes nothing into the relations that hold what
    a source says, counted across the act rather than asserted about one value.
    """

    read = _source(session, project, marker="read")
    _run(session, read, "completed")
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        repair_source_processing(session, document_id=read.id, principal=OPERATOR)
    assert refused.value.reason == "already_read"
    assert _receipts(session, read) == 0

    blocked = _source(session, project, marker="blocked")
    _run(session, blocked, "unreadable")
    session.flush()
    semantic = (Fact, SourceSegment, ProposedDelta)
    before = [
        session.scalar(select(func.count()).select_from(model)) for model in semantic
    ]

    repair_source_processing(session, document_id=blocked.id, principal=OPERATOR)

    assert [
        session.scalar(select(func.count()).select_from(model)) for model in semantic
    ] == before


def test_a_corrected_mapping_repair_is_refused_until_the_correction_is_registered(
    session, project, store, tmp_path
):
    """A corrected-mapping repair names the revision it reads under.

    Approving a mapping revision is the project-coordination designation's act,
    enforced in PostgreSQL (#597) and offered by #829; this only re-reads under
    one, and a project that has registered none has nothing to have corrected.
    The registration here is the real command, through an adoption, because an
    ORM insert into ``project_baseline_formats`` is refused by the database --
    which is the rule this refusal sits downstream of.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        repair_source_processing(
            session,
            document_id=document.id,
            principal=OPERATOR,
            procedure=CORRECTED_MAPPING,
        )
    assert refused.value.reason == "no_registered_mapping"
    assert _receipts(session, document) == 0

    seed_membership(session, project, PRINCIPAL, designations=access.DESIGNATIONS)
    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)

    outcome = repair_source_processing(
        session,
        document_id=document.id,
        principal=OPERATOR,
        procedure=CORRECTED_MAPPING,
    )
    assert outcome.procedure == CORRECTED_MAPPING
    assert outcome.under_mapping
    assert _receipts(session, document) == 1
    # The register names the revision rather than describing the correction.
    assert outcome.under_mapping in _row(session, project, document).repair.sentence


def test_the_register_shows_what_operations_did_about_a_blocked_source(
    session, project, store
):
    """The receipt is on the page the coordinator is already looking at.

    Before the repair the row names an owner and says no repair is recorded;
    after it the row says what was done, by whom, and that nothing has read the
    source since. A repair that worked stays visible once the state settles,
    which is what stops a rescue from being free.
    """

    document = _source(session, project)
    _run(session, document, "unreadable")
    session.flush()

    before = _row(session, project, document)
    assert before.owner == "Corridor Operations"
    assert before.repair is not None
    assert before.repair.recorded is False
    assert before.repair.sentence == "No repair has been recorded for this source."

    repair_source_processing(session, document_id=document.id, principal=OPERATOR)

    after = _row(session, project, document)
    assert after.repair.recorded is True
    assert after.repair.procedure == RETRY_PROCESSING
    assert after.repair.performed_by == OPERATOR.subject
    assert after.repair.performed_at is not None
    assert after.repair.read_again is False
    assert "put this source back to the processing pass" in after.repair.sentence
    assert "Nothing has read it since." in after.repair.sentence

    _run(session, document, "completed")
    session.flush()

    settled = _row(session, project, document)
    assert settled.state == "processed"
    assert settled.is_blocked is False
    # The repair does not vanish because it worked.
    assert settled.repair is not None
    assert settled.repair.read_again is True


def test_the_legacy_operations_routes_stay_outside_the_pilot_boundary(session):
    """#842's fourth criterion, written as an assertion rather than an absence.

    The repair is a runbook precisely so that nothing had to be added to the
    enabled surface to reach it, and the legacy operations screens stay where
    ADR-0081 left them.
    """

    from starlette.routing import Route

    from corridor.web.app import app

    served = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in (route.methods or ())
    }
    operations = {key for key in served if key[1].startswith("/operations/")}

    assert operations, "the legacy operations routes were renamed or removed"
    assert operations & set(web_boundary.PILOT_ROUTES) == set()
    assert not any(
        "repair" in template for _method, template in web_boundary.PILOT_ROUTES
    )
    assert web_boundary.unprotected_route_relations() == ()
