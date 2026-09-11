"""Reading back the exact bytes of one prepared or approved issue (#830).

The Issue section printed an artifact's type, renderer and digest and offered
no way to the file, and the only downloads in the application belonged to the
legacy single-report family outside the pilot surface. A package digest is not
a customer deliverable, so these are the properties of the way out.

What is proved here is mostly what the routes refuse to do. They accept no
storage key and no digest, so every identity is resolved on the server from
the candidate's own rows or from the immutable receipt. They filter on the
project as well as on the identifier, so no candidate id, issue number or
artifact type reaches bytes the caller's project does not own. They do not
catch the store's verification, so retained bytes that no longer hash to what
was recorded raise instead of being served, and a bundle is written only out
of a complete set that has already verified. And nothing they serve changes:
an issue approved before the record moved on answers with the bytes it was
approved with.

Nothing here reads a clock. Every cutoff, preparation instant and release
instant is declared, exactly as `test_issue_section` declares them.
"""

from __future__ import annotations

import io
import re
from uuid import uuid4
import zipfile

import pytest
from sqlalchemy import func, select

from corridor.access import COORDINATION, enroll_member
from corridor.models import Project, ReleaseCandidate, ReleasePackage
from corridor.object_storage import DigestMismatch
from corridor.principals import HumanPrincipal
from corridor.release_authorization import (
    authorize_release_package,
    candidate_set,
    package_set,
)
from corridor.release_candidate import ARTIFACT_SUFFIXES, current_release_candidate
from corridor.web.artifact_downloads import download_name, member_name

from browser_session_support import page_without_shell

from test_issue_section import (  # noqa: F401 -- fixtures used by name
    COORDINATOR,
    NOW,
    OPERATOR,
    RELEASER,
    adopted,
    approve,
    as_principal,
    client,
    configure,
    coverage_named,
    prepare,
    prose,
    store,
    week,
)


OUTSIDER = HumanPrincipal("local:outsider")


def candidate_url(adopted, candidate_id, artifact_type) -> str:
    return (
        f"/work/{adopted.project.slug}/issue/candidates/{candidate_id}"
        f"/artifacts/{artifact_type}"
    )


def issue_url(adopted, issue_number, artifact_type) -> str:
    return (
        f"/work/{adopted.project.slug}/issue/packages/{issue_number}"
        f"/artifacts/{artifact_type}"
    )


def bundle_url(adopted, issue_number) -> str:
    return f"/work/{adopted.project.slug}/issue/packages/{issue_number}/bundle"


def retained(store, artifact) -> bytes:
    return store.get(artifact.storage_key, sha256=artifact.content_sha256)


def corrupt(store, artifact, replacement: bytes = b"not what was sealed") -> None:
    """Overwrite one retained object behind the store's back.

    The store refuses to put different bytes under an existing key, which is
    the point of it; this reaches past that to produce the one state a
    download must never serve.
    """

    from pathlib import Path

    from corridor.config import settings

    path = Path(settings.corpus_store) / artifact.storage_key
    path.write_bytes(replacement)


def another_project(session) -> Project:
    """One other project this person is a full member of."""

    row = Project(
        slug=f"other-project-{uuid4().hex[:8]}",
        name="Another project",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    enroll_member(
        session,
        project_id=row.id,
        email="outsider@example.test",
        principal=OUTSIDER,
        display_name="Outsider",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    return row


# --- every artifact has a download, and it is the retained bytes -----------


def test_every_prepared_artifact_is_served_as_the_bytes_that_were_retained(
    session, adopted, client, store
):
    """The whole configured set, byte for byte, before anything is approved.

    This is the inspection ADR-0086 assumes happens before an authorization:
    the coordinator opens what would go to the customer. The response is the
    object the candidate's identity is bound to, not a re-render.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)

    artifacts = candidate_set(session, candidate)
    assert len(artifacts) == 3
    for artifact in artifacts:
        response = client.get(
            candidate_url(adopted, candidate.id, artifact.artifact_type)
        )
        assert response.status_code == 200, response.text
        assert response.content == retained(store, artifact)
        assert len(response.content) == artifact.byte_count
        # Handed back as a file to keep, and one the browser may not decide to
        # render as something else.
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"


def test_every_approved_artifact_is_served_as_the_bytes_the_receipt_sealed(
    session, adopted, client, store
):
    """The same set again, now addressed by the issue number a person uses."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()

    package = session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id == adopted.project.id
        )
    ).one()
    assert int(package.sequence_number) == 1
    for artifact in package_set(session, package):
        response = client.get(issue_url(adopted, 1, artifact.artifact_type))
        assert response.status_code == 200, response.text
        assert response.content == retained(store, artifact)


def test_a_bundle_carries_the_complete_approved_set_and_repeats_byte_for_byte(
    session, adopted, client, store
):
    """ADR-0086 makes the set the issue unit, so the set is one file.

    Collecting three files by hand and hoping you finished is a different act
    from taking the issue. The archive holds exactly the sealed members under
    safe names, and it is written from a fixed timestamp so the same package
    bundles to the same bytes however long after approval it is asked for.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()
    sealed = candidate_set(session, candidate)

    response = client.get(bundle_url(adopted, 1))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == [
        member_name(one.artifact_type) for one in sealed
    ]
    for one in sealed:
        assert archive.read(member_name(one.artifact_type)) == retained(store, one)
    assert client.get(bundle_url(adopted, 1)).content == response.content


def test_a_bundle_is_not_served_when_one_member_no_longer_verifies(
    session, adopted, client, store
):
    """The complete set verifies, or nothing is handed back.

    A partial bundle is the one thing a customer deliverable may not be, so
    every member is read and verified before the archive is written; there is
    no path that writes the two good members and omits the third.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()
    sealed = candidate_set(session, candidate)
    corrupt(store, sealed[-1])

    with pytest.raises(DigestMismatch):
        client.get(bundle_url(adopted, 1))
    # And the member that still verifies is still its own download: the
    # refusal is about the set, and it did not invalidate the rest of it.
    good = client.get(issue_url(adopted, 1, sealed[0].artifact_type))
    assert good.status_code == 200
    assert good.content == retained(store, sealed[0])


def test_changed_retained_bytes_raise_rather_than_being_served(
    session, adopted, client, store
):
    """`retrieve_*` reads through the content-addressed store, which verifies.

    Nothing in the route catches that. Serving bytes that no longer hash to
    what the candidate's identity was bound to would be the failure the digest
    exists to detect, so the request fails and no file is produced.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    artifact = candidate_set(session, candidate)[0]
    corrupt(store, artifact)

    with pytest.raises(DigestMismatch):
        client.get(candidate_url(adopted, candidate.id, artifact.artifact_type))


def test_an_earlier_issue_downloads_unchanged_after_the_record_moves_on(
    session, adopted, client, store
):
    """ADR-0086 as amended by ADR-0091: an approved set stays retrievable.

    A later candidate is prepared over the same project and becomes the
    current one. The first issue is not re-derived, re-rendered or
    recomputed — it is a content-addressed object named by an immutable
    receipt — so it answers with what it was approved with.
    """

    configure(session, adopted)
    first = prepare(session, adopted, store)
    assert approve(client, adopted, first.id).status_code == 201
    session.expire_all()
    before = {
        one.artifact_type: client.get(issue_url(adopted, 1, one.artifact_type)).content
        for one in candidate_set(session, first)
    }

    second = prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, "second-week").id,
    )
    session.expire_all()
    assert current_release_candidate(session, adopted.project.id).id == second.id

    after = {
        artifact_type: client.get(issue_url(adopted, 1, artifact_type)).content
        for artifact_type in before
    }
    assert after == before
    assert all(body for body in after.values())


# --- no identifier reaches another project's bytes -------------------------


def test_a_member_of_another_project_is_refused_every_identifier_of_this_one(
    session, adopted, client, store
):
    """The slug gate, on all three routes.

    A signed-in person who coordinates a different project names this
    project's slug, its candidate id and its issue number. The membership gate
    answers a non-member exactly as it answers a missing project, so project
    existence never leaks; the same principal is refused whether they ask for
    a prepared artifact, an approved one, or the bundle.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    authorize_release_package(
        session,
        project_id=adopted.project.id,
        candidate_id=candidate.id,
        releaser=RELEASER,
        authorized_at=NOW,
        store=store,
    )
    session.flush()
    artifact_type = candidate_set(session, candidate)[0].artifact_type
    another_project(session)
    as_principal(OUTSIDER)

    assert client.get(
        candidate_url(adopted, candidate.id, artifact_type)
    ).status_code == 404
    assert client.get(issue_url(adopted, 1, artifact_type)).status_code == 404
    assert client.get(bundle_url(adopted, 1)).status_code == 404

    # And the refusal was the gate, not an empty project: the same three
    # requests answer with the bytes for somebody this project knows.
    as_principal(RELEASER)
    assert client.get(
        candidate_url(adopted, candidate.id, artifact_type)
    ).status_code == 200
    assert client.get(issue_url(adopted, 1, artifact_type)).status_code == 200
    assert client.get(bundle_url(adopted, 1)).status_code == 200


def test_another_projects_candidate_id_and_issue_number_find_nothing_here(
    session, adopted, client, store
):
    """The row filter, from a project this person really may read.

    The candidate id is a table-wide sequence, so one project's id is a
    perfectly well-formed id to name from another. The issue number is the
    chain position inside one project, so every project has an issue 1 or
    none. Both are resolved with `project_id` in the same query, so the
    identifier a caller guesses is looked for only among the rows their own
    project owns.

    The package here is written by #533's command directly rather than through
    the approval route: one transaction declares one project-authorization
    scope, and the scope this test is about is the *other* project's.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    authorize_release_package(
        session,
        project_id=adopted.project.id,
        candidate_id=candidate.id,
        releaser=RELEASER,
        authorized_at=NOW,
        store=store,
    )
    session.flush()
    package = session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id == adopted.project.id
        )
    ).one()
    assert int(package.sequence_number) == 1
    artifact_type = candidate_set(session, candidate)[0].artifact_type
    mine = another_project(session)
    as_principal(OUTSIDER)

    for path in (
        f"/work/{mine.slug}/issue/candidates/{candidate.id}"
        f"/artifacts/{artifact_type}",
        f"/work/{mine.slug}/issue/packages/1/artifacts/{artifact_type}",
        f"/work/{mine.slug}/issue/packages/1/bundle",
    ):
        assert client.get(path).status_code == 404, path


def test_an_artifact_the_issue_does_not_hold_is_answered_as_missing(
    session, adopted, client, store
):
    """The artifact type is matched against the set, never used as a path."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()

    for artifact_type in ("provenance_sidecar", "..", "%2e%2e"):
        assert client.get(
            candidate_url(adopted, candidate.id, artifact_type)
        ).status_code == 404
        assert client.get(issue_url(adopted, 1, artifact_type)).status_code == 404


# --- the name a download lands under ---------------------------------------


def test_a_download_name_is_built_from_sanitized_parts_and_a_known_suffix():
    """Nothing a retained string carries reaches the header or the file system.

    The parts are lowercased and every run outside ``a-z0-9`` becomes a
    hyphen, so a separator, a quote, a newline or a percent escape in a slug
    or an artifact type cannot arrive as one. The suffix is looked up in
    #529's own table, and a type with no entry is named as opaque rather than
    as whatever it claims to be.
    """

    assert download_name(
        project_slug="north-ave", kind="issue", number=3, artifact_type="updated_ucm"
    ) == "north-ave-issue-3-updated-ucm" + ARTIFACT_SUFFIXES["updated_ucm"]
    assert (
        download_name(project_slug="north-ave", kind="issue", number=3)
        == "north-ave-issue-3-package.zip"
    )

    hostile = download_name(
        project_slug='../../"ev il\n',
        kind="candidate",
        number=41,
        artifact_type="../../etc/passwd",
    )
    assert hostile == "ev-il-candidate-41-etc-passwd.bin"
    assert not set(hostile) & set('/\\"\r\n %')
    assert member_name("weekly_coordination_report") == (
        "weekly-coordination-report" + ARTIFACT_SUFFIXES["weekly_coordination_report"]
    )
    assert member_name("provenance_sidecar") == "provenance-sidecar.bin"


# --- the section that offers them ------------------------------------------


def test_the_section_offers_a_working_file_for_every_prepared_artifact(
    session, adopted, client, store
):
    """Every link the page renders is followed, not merely counted.

    A path composed on a screen and a route declared in the application are
    two spellings of one thing, and this is the seam where they are compared:
    the test takes the hrefs the coordinator would click and asks whether the
    retained bytes come back.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    rendered = week(client, adopted)

    links = re.findall(r'href="(/work/[^"]*/issue/candidates/[^"]*)"', rendered)
    sealed = candidate_set(session, candidate)
    assert len(links) == len(sealed)
    for artifact, link in zip(sealed, links):
        assert str(candidate.id) in link
        response = client.get(link)
        assert response.status_code == 200, link
        assert response.content == retained(store, artifact)


def test_the_digest_and_the_renderer_move_into_an_expandable_detail(
    session, adopted, client, store
):
    """What a person reads and what a person proves are not the same row.

    The digest and the renderer identity are how a file is proved to be the
    sealed one, and they are not what the table is read for. They stay on the
    page and stop being the widest column on it.
    """

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    rendered = week(client, adopted)
    digest = candidate_set(session, candidate)[0].content_sha256

    assert digest in rendered
    details = re.findall(r"<details>(.*?)</details>", rendered, re.S)
    assert details, "the digests are expected inside an expandable detail"
    assert any(digest in one for one in details)
    assert any("Produced by" in one for one in details)


def test_an_approved_issue_offers_the_whole_set_and_each_file(
    session, adopted, client, store
):
    """The approved package, as one bundle and as individual files."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()

    rendered = week(client, adopted)
    assert bundle_url(adopted, 1) in rendered
    for artifact in candidate_set(session, candidate):
        assert issue_url(adopted, 1, artifact.artifact_type) in rendered
    body = prose(rendered)
    assert "Approved for sharing" in body
    assert "Approved and sent" not in body


def test_a_stale_candidate_is_inspectable_as_history_and_still_unapprovable(
    session, adopted, client, store
):
    """Both halves of the same state, in one reading.

    Approving the first candidate moves the release chain head, so the second
    prepared candidate no longer describes the project and is not offered for
    approval. Its files are still exactly what was proposed to the customer,
    so they are still openable — inspecting a candidate is not approving one.
    """

    configure(session, adopted)
    first = prepare(session, adopted, store)
    stale = prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, "prepared-early").id,
    )
    assert approve(client, adopted, first.id).status_code == 201
    session.expire_all()

    rendered = week(client, adopted)
    assert "Approve this issue for sharing" not in prose(rendered)
    for artifact in candidate_set(session, stale):
        link = candidate_url(adopted, stale.id, artifact.artifact_type)
        assert link in rendered
        response = client.get(link)
        assert response.status_code == 200
        assert response.content == retained(store, artifact)

    assert approve(client, adopted, stale.id).status_code == 409


def test_a_superseded_candidate_keeps_the_files_it_was_prepared_with(
    session, adopted, client, store
):
    """`FRESH_PREPARATION`'s promise, extended to the files themselves.

    "It stays listed here as it was prepared, so what was proposed to the
    customer and refused is not lost" is only half kept by a row that names a
    file nobody can open.
    """

    configure(session, adopted)
    first = prepare(session, adopted, store)
    prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, "second-week").id,
    )
    session.expire_all()

    rendered = week(client, adopted)
    assert "Earlier candidates for this issue" in prose(rendered)
    for artifact in candidate_set(session, first):
        link = candidate_url(adopted, first.id, artifact.artifact_type)
        assert link in rendered
        assert client.get(link).content == retained(store, artifact)


def test_a_candidate_of_this_project_is_the_only_candidate_the_route_resolves(
    session, adopted, client, store
):
    """The row filter, exercised where a guessed identifier would land."""

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    artifact_type = candidate_set(session, candidate)[0].artifact_type
    highest = session.scalar(select(func.max(ReleaseCandidate.id)))

    assert client.get(
        candidate_url(adopted, int(highest) + 1, artifact_type)
    ).status_code == 404
    assert client.get(bundle_url(adopted, 99)).status_code == 404


# --- the same packages, in the read-only Record view -----------------------


def test_the_record_view_lists_the_approved_package_with_its_fields_and_files(
    session, adopted, client, store
):
    """The investigation view stops reading only the legacy release family.

    It read `external_report_release_history` and nothing else, so a project
    that had approved three issues was told nothing had ever gone to its
    customer. The packages come from the release module's own
    `release_history` — the reader that already existed and was unwired — so
    there is one release history and not a second model of one.
    """

    from corridor.record_history import read_record_history
    from corridor.release_authorization import release_history

    configure(session, adopted)
    candidate = prepare(session, adopted, store)
    assert approve(client, adopted, candidate.id).status_code == 201
    session.expire_all()

    history = read_record_history(session, project_id=adopted.project.id)
    assert history.packages == release_history(session, adopted.project.id)
    entry = history.packages[0]

    rendered = client.get(f"/record/{adopted.project.slug}")
    assert rendered.status_code == 200, rendered.text
    body = prose(rendered.text)
    assert "Issue 1" in body
    assert str(entry.accepted_revision_id) in body
    assert entry.issue_profile_identity in body
    assert f"version {entry.issue_profile_version}" in body
    assert entry.authorized_by_principal in body
    assert entry.source_cutoff.date().isoformat() in body
    assert "this project's first issue" in body
    for artifact in entry.artifacts:
        assert artifact.content_sha256 in body
        assert issue_url(adopted, 1, artifact.artifact_type) in rendered.text
    assert bundle_url(adopted, 1) in rendered.text

    # Every link on that page is followed rather than counted.
    for link in re.findall(
        r'href="(/work/[^"]*/issue/packages/[^"]*)"', rendered.text
    ):
        assert client.get(link).status_code == 200, link

    # And it is still a reading: the one form on it is the GET search. The
    # navigation shell every customer page carries (#843) is not this page's
    # own control surface, so the page is read without it.
    own = page_without_shell(rendered.text).lower()
    assert 'method="post"' not in own
    assert own.count("<form") == 1


def test_the_download_routes_are_admitted_with_protected_readings():
    """Criterion 7: in the manifest, and reading nothing the revoke takes away.

    The boundary has two halves and they can drift, so a route enabled here
    is compared against the relations the migration revokes from the human web
    capability. `test_architecture` makes that comparison for the whole
    manifest; this names the three entries #830 added, so removing one from
    the manifest fails beside the feature rather than somewhere general.
    """

    from corridor import web_boundary

    keys = (
        ("GET", "/work/{slug}/issue/candidates/{candidate_id}/artifacts/{artifact_type}"),
        ("GET", "/work/{slug}/issue/packages/{issue_number}/artifacts/{artifact_type}"),
        ("GET", "/work/{slug}/issue/packages/{issue_number}/bundle"),
        ("GET", "/record/{slug}"),
    )
    for key in keys:
        route = web_boundary.PILOT_ROUTES[key]
        assert route.relations <= web_boundary.PROTECTED_RELATIONS, key
        assert not route.relations & web_boundary.DENIED_RELATIONS, key
    assert {
        "release_packages",
        "release_package_artifacts",
    } <= web_boundary.PILOT_ROUTES[("GET", "/record/{slug}")].relations
