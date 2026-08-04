import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.models import (
    Assertion,
    AuditLog,
    Candidate,
    Dependency,
    Document,
    Project,
)
from corridor.web.app import app, get_session
from corridor.web.queue import build_view, next_candidate


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def client(session):
    """The app shares the test's transaction, so nothing is committed."""
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    p = Project(slug="web-test", name="Web Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def document(session, project):
    d = Document(
        project_id=project.id,
        sha256="d" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(d)
    session.flush()
    return d


def make_candidate(session, project, document, *, verified=True, uid="FOC1-1"):
    c = Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": {
                "utility_id": uid,
                "external_org": "AT&T Texas (SWBT)",
                "station_from": "1149+00",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": f"{uid} AT&T Texas (SWBT)",
                    "verified": verified,
                    "whole_row": True,
                }
            ],
            "confidence": 1.0,
            "dedupe_hint": "x",
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="txdot_ucm_v1",
        citations_verified=verified,
    )
    session.add(c)
    session.flush()
    return c


def test_queue_shows_the_next_pending_candidate(client, session, project, document):
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "AT&amp;T Texas (SWBT)" in r.text
    assert "1149+00" in r.text


def test_unverified_candidates_sink_but_are_never_hidden(
    client, session, project, document
):
    """A quote that could not be found is a signal, not noise."""
    bad = make_candidate(session, project, document, verified=False, uid="BAD-1")
    good = make_candidate(session, project, document, verified=True, uid="GOOD-1")

    assert next_candidate(session, project.id).id == good.id

    good.state = "accepted"
    session.flush()
    assert next_candidate(session, project.id).id == bad.id

    r = client.get(f"/queue/{project.slug}")
    assert "Citation unverified" in r.text


def test_a_verified_candidate_is_labelled_verified(client, session, project, document):
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert "Citation verified" in r.text
    assert "Citation unverified" not in r.text


def test_merge_is_unavailable_when_there_is_nothing_to_merge_into(
    client, session, project, document
):
    """No existing dependency for this party means no merge, not a bad one."""
    make_candidate(session, project, document)
    r = client.get(f"/queue/{project.slug}")
    assert "disabled" in r.text
    assert "Nothing to merge into" in r.text


def test_merge_suggestions_appear_once_a_dependency_exists(
    client, session, project, document
):
    """The same facility, seen again in a later revision.

    This staged both candidates on one document until #46. A matrix lists
    each facility once, so that pair could only ever have been two
    facilities — the merge case needs a second revision to be real.
    """
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )

    later = Document(
        project_id=project.id,
        sha256="e" * 64,
        filename="nhhip-seg3c2-utilities-inventory-4-30-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=28,
    )
    session.add(later)
    session.flush()
    make_candidate(session, project, later, uid="FOC1-1")

    r = client.get(f"/queue/{project.slug}")
    assert "DEP-00001" in r.text
    # The reason is visible, not just the ranking.
    assert "station" in r.text and "text" in r.text


def test_a_sibling_row_of_the_same_matrix_is_not_suggested(
    client, session, project, document
):
    """#46: 94 of 96 AT&T rows drew a suggestion against their own siblings."""
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    make_candidate(session, project, document, uid="FOC1-2")

    r = client.get(f"/queue/{project.slug}")
    assert "DEP-00001" not in r.text
    assert "Nothing to merge into" in r.text


def test_merging_from_the_queue_adds_to_the_existing_dependency(
    client, session, project, document
):
    first = make_candidate(session, project, document)
    client.post(
        f"/candidates/{first.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    target = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()

    second = make_candidate(session, project, document, uid="FOC1-2")
    r = client.post(
        f"/candidates/{second.id}/merge",
        data={"slug": project.slug, "dependency_id": target.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert second.state == "merged"
    assert second.merged_into == target.id
    # Still one dependency: merging must not create a second.
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )


def test_merging_into_another_projects_dependency_is_refused(
    client, session, project, document
):
    """Two ledgers that were never the same thing must not be joined."""
    other = Project(slug="web-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    stray = Dependency(
        project_id=other.id,
        ref_code="DEP-00001",
        dep_type="utility_relocation",
        title="unrelated",
        status="identified",
    )
    session.add(stray)
    session.flush()

    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/merge",
        data={"slug": project.slug, "dependency_id": stray.id},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert candidate.state == "pending"


def test_accepting_creates_a_dependency_and_advances(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert candidate.state == "accepted"
    assert session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()


def test_edit_then_accept_records_the_edited_values(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC1-1",
            "field_external_org": "AT&T Texas",
            "field_station_from": "1150+00",
        },
        follow_redirects=False,
    )
    dep = session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).one()
    station = session.scalars(
        select(Assertion).where(
            Assertion.dependency_id == dep.id, Assertion.field_name == "station_from"
        )
    ).one()
    assert station.asserted_value == "1150+00"


def test_an_edit_is_audited_against_the_original_extraction(
    client, session, project, document
):
    """What the extractor said must survive the reviewer changing it."""
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/edit-accept",
        data={
            "slug": project.slug,
            "field_utility_id": "FOC1-1",
            "field_external_org": "AT&T Texas (SWBT)",
            "field_station_from": "1150+00",
        },
        follow_redirects=False,
    )
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "edit_candidate", AuditLog.entity_id == candidate.id
        )
    ).one()
    assert entry.before_json["fields"]["station_from"] == "1149+00"
    assert entry.after_json["fields"]["station_from"] == "1150+00"


def test_rejecting_records_a_reason_and_creates_no_dependency(
    client, session, project, document
):
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/reject",
        data={"slug": project.slug, "reason": "duplicate"},
        follow_redirects=False,
    )
    assert candidate.state == "rejected"
    assert not session.scalars(
        select(Dependency).where(Dependency.project_id == project.id)
    ).all()
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "reject_candidate")
    ).one()
    assert entry.after_json["reason"] == "duplicate"


def test_an_unknown_reject_reason_is_refused(client, session, project, document):
    candidate = make_candidate(session, project, document)
    r = client.post(
        f"/candidates/{candidate.id}/reject",
        data={"slug": project.slug, "reason": "because"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert candidate.state == "pending"


def test_adjudicating_twice_is_refused(client, session, project, document):
    """Two tabs or a double submit would create two Dependencies from one row."""
    candidate = make_candidate(session, project, document)
    client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    again = client.post(
        f"/candidates/{candidate.id}/accept",
        data={"slug": project.slug},
        follow_redirects=False,
    )
    assert again.status_code == 409
    assert (
        len(
            session.scalars(
                select(Dependency).where(Dependency.project_id == project.id)
            ).all()
        )
        == 1
    )


def test_an_empty_queue_says_so(client, session, project):
    r = client.get(f"/queue/{project.slug}")
    assert r.status_code == 200
    assert "Queue empty" in r.text


def test_a_missing_page_image_is_a_404_not_a_crash(client, session, document):
    r = client.get(f"/page-image/{document.id}/1")
    assert r.status_code == 404


# ------------------------------ what the queue surfaces about a row (#71)


def rich_candidate(session, project, document, **payload):
    c = make_candidate(session, project, document)
    c.payload_json = {**c.payload_json, **payload}
    session.flush()
    return c


def test_the_queue_shows_which_tier_read_the_row(session, project, document):
    """A transcribed row deserves different weight from one read off the
    text layer, the same way `text_source: ocr` already does — and today a
    reviewer cannot tell without opening the payload."""
    rich_candidate(session, project, document, tier="transcribe",
                   text_source="ocr")

    view = build_view(session, next_candidate(session, project.id))

    assert view.tier == "transcribe"
    assert view.text_source == "ocr"


def test_the_queue_lists_headers_the_vocabulary_could_not_place(
    session, project, document
):
    """The queue telling a human "the document says something the Ledger
    has no field for" — the trigger for a deliberate vocabulary extension,
    which is not an extractor's decision to make. Reconstructing these from
    the payload by hand is how #85 and #97 were investigated."""
    rich_candidate(
        session, project, document,
        unmapped_columns=["Retain and Protect", "Abandon / Deactivate"],
    )

    view = build_view(session, next_candidate(session, project.id))

    assert view.unmapped_columns == ["Retain and Protect", "Abandon / Deactivate"]


def test_an_unverified_row_names_the_field_that_failed(
    session, project, document
):
    """"Unverified" that names the suspect value instead of only sinking
    the row. A reviewer who cannot see *which* field is unsupported has to
    re-verify all of them."""
    rich_candidate(
        session, project, document,
        unverified_fields=["station_from"],
        low_confidence_tokens=["1149"],
    )

    view = build_view(session, next_candidate(session, project.id))

    assert view.unverified_fields == ["station_from"]
    assert view.low_confidence_tokens == ["1149"]


def test_a_clean_row_surfaces_nothing_extra(session, project, document):
    """The common case stays quiet. A queue that flags every row flags
    nothing."""
    make_candidate(session, project, document)

    view = build_view(session, next_candidate(session, project.id))

    assert view.unmapped_columns == []
    assert view.unverified_fields == []
    assert view.low_confidence_tokens == []
    assert view.tier is None


def test_the_queue_page_renders_what_it_surfaces(client, session, project, document):
    rich_candidate(
        session, project, document,
        tier="transcribe",
        unmapped_columns=["Retain and Protect"],
        unverified_fields=["station_from"],
    )
    session.commit()

    body = client.get(f"/queue/{project.slug}").text

    assert "transcribe" in body
    assert "Retain and Protect" in body
    assert "station_from" in body
