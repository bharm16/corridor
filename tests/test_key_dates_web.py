"""HTTP flow for the Key dates import preview and attributable confirm (#337)."""

import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.key_date_drafting import (
    KeyDateDraftRow,
    KeyDateDraftRuntimeOutput,
    QuarantinedSequencing,
    SourceBoundKeyDateDraftRequest,
    draft_key_dates,
)
from corridor.milestones import import_xer, preview_import
from corridor.models import DocPage, Document, Milestone, MilestoneRegistration, Project
from corridor.principals import HumanPrincipal
from corridor.web.app import app, get_human_principal, get_session

TEST_PRINCIPAL = HumanPrincipal("local:test-reviewer")

CSV = "code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-11-01\nLET,Letting,2027-01-20\n"


def xer_bytes():
    fields = ["task_id", "proj_id", "task_code", "task_name", "task_type", "target_end_date"]
    lines = [
        "\t".join(["ERMHDR", "19.12", "2026-08-29", "P", "a", "a", "db", "US", "USD"]),
        "\t".join(["%T", "TASK"]),
        "\t".join(["%F", *fields]),
        "\t".join(["%R", "102", "1", "UTIL-CLEAR", "Utility clearance complete", "TT_FinMile", "2026-11-01 00:00"]),
        "%E",
    ]
    return "\n".join(lines).encode("cp1252")


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: TEST_PRINCIPAL
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client_without_session(session):
    """No signed-in session: exercise the real fail-closed identity gate (#331)."""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(member_project):
    return member_project(TEST_PRINCIPAL)


def codes(session, project):
    return set(
        session.scalars(
            select(Milestone.code).where(Milestone.project_id == project.id)
        ).all()
    )


def _confirm_fields(session, project, content, source_name="typed.csv"):
    preview = preview_import(
        session, project_id=project.id, content=content.encode(), source_name=source_name
    )
    return {
        "source_name": source_name,
        "content": content,
        "expected_sha256": preview.source_sha256,
        "expected_predecessors": json.dumps(preview.predecessors),
    }


def test_landing_lists_registered_key_dates(client, session, project):
    import_xer(session, project_id=project.id, content=xer_bytes(), source_name="seg.xer")
    response = client.get(f"/key-dates/{project.slug}")
    assert response.status_code == 200
    assert "UTIL-CLEAR" in response.text
    assert "Utility clearance complete" in response.text


def test_preview_shows_classified_rows_and_fingerprint_without_writing(
    client, session, project
):
    response = client.post(
        f"/key-dates/{project.slug}/preview",
        data={"source_name": "typed.csv", "content": CSV},
    )
    assert response.status_code == 200
    assert "New key date" in response.text
    assert sha256(CSV.encode()).hexdigest() in response.text
    # A preview writes nothing.
    assert codes(session, project) == set()


def test_confirm_imports_under_the_configured_person(client, session, project):
    fields = _confirm_fields(session, project, CSV)
    response = client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/key-dates/{project.slug}?imported=")
    registrations = session.scalars(
        select(MilestoneRegistration)
        .join(Milestone, MilestoneRegistration.milestone_id == Milestone.id)
        .where(Milestone.project_id == project.id)
    ).all()
    assert {r.recorded_by for r in registrations} == {"local:test-reviewer"}
    assert codes(session, project) == {"UTIL-CLEAR", "LET"}


def test_confirm_is_unavailable_without_a_signed_in_session(
    client_without_session, session, project
):
    fields = _confirm_fields(session, project, CSV)
    response = client_without_session.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 401
    assert codes(session, project) == set()


def test_preview_of_a_malformed_sheet_refuses(client, session, project):
    response = client.post(
        f"/key-dates/{project.slug}/preview",
        data={"source_name": "bad.csv", "content": "code,name\nX,no date\n"},
    )
    assert response.status_code == 422
    assert "Not imported" in response.text
    assert codes(session, project) == set()


def test_confirm_against_changed_bytes_refuses_and_presents_new_state(
    client, session, project
):
    # Preview one file, then confirm different bytes carrying the old fingerprint.
    fields = _confirm_fields(session, project, CSV)
    changed = CSV.replace("2026-11-01", "2026-12-15")
    fields["content"] = changed
    response = client.post(
        f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False
    )
    assert response.status_code == 409
    assert "Refused" in response.text
    assert codes(session, project) == set()


def test_import_is_scoped_to_its_project(client, session, project):
    other = Project(slug="kd-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    fields = _confirm_fields(session, project, CSV)
    client.post(f"/key-dates/{project.slug}/confirm", data=fields, follow_redirects=False)
    assert codes(session, project) == {"UTIL-CLEAR", "LET"}
    assert codes(session, other) == set()


def test_source_bound_draft_uses_existing_preview_then_ordinary_confirmation(
    client, session, project
):
    quote = "UTIL-CLEAR | Utility clearance | 2026-11-01"
    document = Document(
        project_id=project.id,
        sha256=sha256(quote.encode()).hexdigest(),
        filename="schedule-summary.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=2, text=quote, text_source="cells"))
    session.flush()

    class Runtime:
        identity = {
            "adapter": "fake-key-date-draft-v1",
            "model": "fake-model",
            "prompt_version": "key-date-draft-test-v1",
            "configuration": {"temperature": 0},
        }

        def draft(self, request, pages, budget):
            return KeyDateDraftRuntimeOutput(
                rows=(
                    KeyDateDraftRow(
                        code="UTIL-CLEAR",
                        name="Utility clearance",
                        scheduled_for="2026-11-01",
                        precision="day",
                        page_no=2,
                        quote=quote,
                    ),
                ),
                    usage={"input_tokens": 10, "output_tokens": 5, "elapsed_ms": 2, "spend_usd_micros": 0},
            )

    draft = draft_key_dates(
        session,
        SourceBoundKeyDateDraftRequest(
            project_id=project.id,
            document_id=document.id,
            source_sha256=document.sha256,
            allowed_pages=(2,),
            requested_by=TEST_PRINCIPAL,
        ),
        runtime=Runtime(),
    )
    response = client.get(f"/key-dates/{project.slug}/drafts/{draft.receipt_id}/preview")
    assert response.status_code == 200
    assert "Drafted from schedule-summary.pdf" in response.text
    assert "Source page 2" in response.text
    assert quote in response.text
    preview = preview_import(
        session,
        project_id=project.id,
        content=draft.csv_content,
        source_name=draft.source_name,
    )
    response = client.post(
        f"/key-dates/{project.slug}/confirm",
        data={
            "source_name": draft.source_name,
            "content": draft.csv_content.decode(),
            "expected_sha256": preview.source_sha256,
            "expected_predecessors": json.dumps(preview.predecessors),
            "draft_receipt_id": str(draft.receipt_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert codes(session, project) == {"UTIL-CLEAR"}


def test_unresolved_and_quarantined_sequencing_stay_visible_without_an_importer(
    client, session, project
):
    quote = "UTIL-CLEAR | Utility clearance | 2026-11-01"
    relationship = "UTIL-RELO must finish before UTIL-CLEAR"
    text = f"{quote}\n{relationship}"
    document = Document(
        project_id=project.id,
        sha256=sha256(text.encode()).hexdigest(),
        filename="schedule-summary.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=2, text=text, text_source="cells"))
    session.flush()

    class Runtime:
        identity = {
            "adapter": "fake-key-date-draft-v1",
            "model": "fake-model",
            "prompt_version": "key-date-draft-test-v1",
            "configuration": {"temperature": 0},
        }

        def draft(self, request, pages, budget):
            return KeyDateDraftRuntimeOutput(
                rows=(
                    KeyDateDraftRow(
                        code="UTIL-CLEAR",
                        name="Utility clearance",
                        scheduled_for="2026-11-01",
                        precision="day",
                        page_no=2,
                        quote=quote,
                    ),
                ),
                sequencing=(QuarantinedSequencing(page_no=2, quote=relationship),),
                usage={"input_tokens": 9, "output_tokens": 4, "elapsed_ms": 1, "spend_usd_micros": 0},
            )

    draft = draft_key_dates(
        session,
        SourceBoundKeyDateDraftRequest(
            project_id=project.id,
            document_id=document.id,
            source_sha256=document.sha256,
            allowed_pages=(2,),
            requested_by=TEST_PRINCIPAL,
        ),
        runtime=Runtime(),
    )
    assert draft.rows == ()

    response = client.get(f"/key-dates/{project.slug}/drafts/{draft.receipt_id}/preview")
    assert response.status_code == 200
    assert "Quarantined sequencing" in response.text
    assert relationship in response.text
    assert "no rows were imported" in response.text
    assert "Confirm import" not in response.text
    assert session.scalars(select(Milestone)).all() == []


def test_tampered_draft_content_refuses_the_draft_bound_confirmation(
    client, session, project
):
    quote = "UTIL-CLEAR | Utility clearance | 2026-11-01"
    document = Document(
        project_id=project.id,
        sha256=sha256(quote.encode()).hexdigest(),
        filename="schedule-summary.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=2, text=quote, text_source="cells"))
    session.flush()

    class Runtime:
        identity = {
            "adapter": "fake-key-date-draft-v1",
            "model": "fake-model",
            "prompt_version": "key-date-draft-test-v1",
            "configuration": {"temperature": 0},
        }

        def draft(self, request, pages, budget):
            return KeyDateDraftRuntimeOutput(
                rows=(
                    KeyDateDraftRow(
                        code="UTIL-CLEAR",
                        name="Utility clearance",
                        scheduled_for="2026-11-01",
                        precision="day",
                        page_no=2,
                        quote=quote,
                    ),
                ),
                usage={"input_tokens": 9, "output_tokens": 4, "elapsed_ms": 1, "spend_usd_micros": 0},
            )

    draft = draft_key_dates(
        session,
        SourceBoundKeyDateDraftRequest(
            project_id=project.id,
            document_id=document.id,
            source_sha256=document.sha256,
            allowed_pages=(2,),
            requested_by=TEST_PRINCIPAL,
        ),
        runtime=Runtime(),
    )
    preview = preview_import(
        session,
        project_id=project.id,
        content=draft.csv_content,
        source_name=draft.source_name,
    )
    tampered = "code,name,need_date\nUTIL-CLEAR,Utility clearance,2026-12-25\n"
    response = client.post(
        f"/key-dates/{project.slug}/confirm",
        data={
            "source_name": draft.source_name,
            "content": tampered,
            "expected_sha256": preview.source_sha256,
            "expected_predecessors": json.dumps(preview.predecessors),
            "draft_receipt_id": str(draft.receipt_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert session.scalars(select(Milestone)).all() == []
