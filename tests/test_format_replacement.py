"""Replacing the output template or the field mapping in force (#829).

The page is driven the way the two people drive it: Corridor operations signs
in and offers the customer's new form, and the coordinator opens the link
operations hands them and approves. Both halves go through a real session
established by the magic-link flow and submit exactly the fields the rendered
form emitted, because a form that omits the request-forgery token is not
"unprotected" but inoperable and a test that replaced the check could not tell
the two apart (#821).

The case the validation exists for is #597's: a successor form that stops
carrying two values in two columns and starts carrying one combined range in
the same two columns prints identical headings and identical drop-downs, so no
digest over its bytes can tell. Both directions are exercised below.

Nothing reads a clock: the request instant is declared through
``get_review_clock``.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from datetime import datetime, timezone
import html
import re
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.access import (
    COORDINATION,
    EXTERNAL_RELEASE,
    TECHNICAL_OPERATIONS,
    enroll_member,
)
from corridor.accepted_field_reading import read_accepted_field_population
from corridor.baseline_adoption import effective_baseline_formats
from corridor.field_mapping_manifest import (
    COMBINED_RANGE,
    ONE_VALUE_PER_COLUMN,
    manifest_from_declaration,
)
from corridor.format_replacement import compare_mappings
from corridor.models import (
    BaselineFormat,
    BaselineFormatManifest,
    BaselineFormatObject,
    Project,
    ReleasePackage,
)
from corridor.object_storage import content_store
from corridor.principals import HumanPrincipal
from corridor.release_authorization import (
    authorize_release_package,
    package_set,
    retrieve_released_artifact,
)
from corridor.web import auth
from corridor.web.app import app, get_review_clock, get_session

from browser_session_support import form_fields, sign_in, submit_form
from record_counts import nothing_written
from later_revision_support import BASELINE_ROWS, HEADINGS, adopt, workbook_bytes
from test_issue_section import (
    Adopted as PreparedProject,
    configure as configure_issued_set,
    prepare as prepare_candidate,
)


COORDINATOR = HumanPrincipal("local:coordinator")
OPERATOR = HumanPrincipal("local:operator")
MEMBER = HumanPrincipal("local:member")
ENROLLER = HumanPrincipal("local:enroller")
RELEASER = HumanPrincipal("local:releaser")

NOW = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)

STATION_RANGE = "station_from-station_to"


# --- the customer's forms --------------------------------------------------


def combined_rows(rows=BASELINE_ROWS):
    """The same three conflicts, with the station range written as one value.

    Column 7 is `Start Station` and column 8 is `End Station`. The form still
    prints both, and every heading and every drop-down is identical to the
    adopted one -- which is exactly why no reading of the bytes can tell that
    the second column stopped carrying a value.
    """

    changed = []
    for row in rows:
        one = list(row)
        one[6] = f"{row[6]} - {row[7]}"
        one[7] = ""
        changed.append(one)
    return changed


def shifted_headings():
    """A form that stops heading `Material` and starts heading a subtype."""

    headings = list(HEADINGS)
    headings[4] = "Utility Subtype"
    return headings


@dataclass(frozen=True)
class Adoption:
    """One adopted project, and what a later issue is prepared from."""

    project: Project
    revision_id: int
    template_bytes: bytes


@pytest.fixture
def adoption(session, tmp_path):
    """One adopted project and the three standings this page distinguishes.

    The coordinator holds both designations, because one rollback-scoped
    session is one transaction and a project-authorization scope is declared
    transaction-locally: two signed-in people in one test would conflict over
    it in the harness and never in a deployment, where each request is its own
    transaction. So the end-to-end path is walked by one person who may do both
    halves, and each gate is proved on its own by somebody who may do one.
    """

    row = Project(
        slug=f"format-replacement-{uuid4().hex[:8]}",
        name="Format Replacement",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    for principal, email, name, designations in (
        (
            COORDINATOR,
            "coordinator@example.test",
            "Coordinator",
            [COORDINATION, TECHNICAL_OPERATIONS],
        ),
        (OPERATOR, "operator@example.test", "Operator", [TECHNICAL_OPERATIONS]),
        (MEMBER, "member@example.test", "Member", []),
    ):
        enroll_member(
            session,
            project_id=row.id,
            email=email,
            principal=principal,
            display_name=name,
            designations=designations,
            operator=ENROLLER,
        )
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, row, body, tmp_path)
    return Adoption(project=row, revision_id=revision_id, template_bytes=body)


@pytest.fixture
def adopted(adoption) -> Project:
    """The adopted project itself, which is all most of this file needs."""

    return adoption.project


@pytest.fixture
def sender():
    """The replaceable mail seam, as a non-sending capture: a link is never sent."""

    return auth.RecordingEmailSender()


@pytest.fixture
def browser(session, sender):
    """Signed-in browsers, with identity left to the real session-cookie path."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[auth.get_email_sender] = lambda: sender
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with ExitStack() as open_browsers:

        def signed_in(email: str) -> TestClient:
            client = open_browsers.enter_context(
                TestClient(app, base_url="https://testserver")
            )
            sign_in(client, sender, email)
            return client

        yield signed_in
    app.dependency_overrides.clear()


# --- driving the page the way a person drives it ---------------------------


def prose(body: str) -> str:
    """The page as a reader receives it, with markup escaping undone."""

    return html.unescape(body)


def read(client, project) -> str:
    response = client.get(f"/template-and-mapping/{project.slug}")
    assert response.status_code == 200, response.text
    return response.text


def offer(
    client,
    project,
    body: bytes,
    *,
    kind: str,
    identity: str,
    version: str,
    combined=(),
    name: str = "successor.xlsx",
):
    """Submit the offer form the page rendered, with the file attached."""

    page = read(client, project)
    fields = form_fields(page, "/validate")
    assert fields is not None, "the page rendered no offer form"
    data = {
        auth.CSRF_FIELD: fields[auth.CSRF_FIELD],
        "kind": kind,
        "identity": identity,
        "version": version,
        "combined": list(combined),
    }
    return client.post(
        f"/template-and-mapping/{project.slug}/validate",
        data=data,
        files={"upload": (name, body, "application/vnd.ms-excel")},
        headers={auth.CSRF_HEADER: fields[auth.CSRF_FIELD]},
        follow_redirects=False,
    )


def approve(client, project, body: str):
    """Submit exactly the hidden fields the approval form rendered."""

    fields = form_fields(body, "/register")
    assert fields is not None, "the page rendered no approval form"
    return submit_form(
        client, f"/template-and-mapping/{project.slug}/register", fields
    )


def handoff_link(project, body: str) -> str:
    """The link an approval form's own fields amount to, as a GET.

    Operations hands the person who approves a URL and not a session: the
    offered bytes are content-addressed, so the digest and the declaration it
    was validated under are the whole proposal.
    """

    fields = form_fields(body, "/register")
    assert fields is not None, "the page rendered no approval form"
    form = re.search(
        r'<form[^>]*action="[^"]*/register"[^>]*>(.*?)</form>', body, re.S
    )
    assert form is not None
    query = [
        ("kind", fields["kind"]),
        ("identity", fields["identity"]),
        ("version", fields["version"]),
        ("staged", fields["staged_sha256"]),
    ] + [
        ("combined", value)
        for value in re.findall(
            r'name="combined" value="([^"]+)"', form.group(1)
        )
    ]
    return f"/template-and-mapping/{project.slug}?" + urlencode(query)


def registrations(session, project, kind: str):
    return tuple(
        session.scalars(
            select(BaselineFormat)
            .where(
                BaselineFormat.project_id == project.id,
                BaselineFormat.format_kind == kind,
            )
            .order_by(BaselineFormat.id)
        ).all()
    )


def in_force(session, project, kind: str):
    return effective_baseline_formats(session, project.id)[kind]


def registered_manifest(session, project):
    row = in_force(session, project, "field_mapping")
    stored = session.get(BaselineFormatManifest, int(row.id))
    assert stored is not None
    return manifest_from_declaration(stored.declaration)


# --- criterion 1: operations submits and sees the validation result --------


def test_a_replacement_template_that_combines_a_mapped_value_is_reported(
    session, adopted, browser, tmp_path
):
    """#829's first criterion, on #597's case.

    The offered form heads the same columns with the same drop-downs and
    carries the station range as one value. Nothing about its bytes says so, so
    the refusal comes from reading its own populated rows through the
    composition the registered revision declares -- and it names the worksheet
    row it read.
    """

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    response = offer(
        browser("operator@example.test"),
        adopted,
        successor,
        kind="output_template",
        identity="Customer standard UCM export",
        version="2027.1",
    )

    assert response.status_code == 200, response.text
    body = prose(response.text)
    assert "This file cannot be registered as offered" in body
    assert "combines two values across ' - '" in body
    assert "Utility Conflicts!3" in body
    # The recovery is named, and it is the other registration on this page.
    assert "carries a different mapping revision" in body
    # Operations sees its own reading of the file beside the refusal.
    assert "What Corridor operations read in this file" in body
    assert "Utility Conflicts" in body
    # Nothing was registered by reading a file.
    assert len(registrations(session, adopted, "output_template")) == 1


def test_a_replacement_mapping_that_does_not_reproduce_the_form_is_reported(
    session, adopted, browser, tmp_path
):
    """The other direction: a declaration the customer's own values contradict.

    Operations offers the combined form and declares nothing about the range,
    which claims one value per column. The declaration is refused before
    anybody can approve it, by the round trip over the form's own populated
    example rather than by anything read off its headings.
    """

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    response = offer(
        browser("operator@example.test"),
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
    )

    assert response.status_code == 200, response.text
    body = prose(response.text)
    assert "This file cannot be registered as offered" in body
    assert "the representative example contradicts the declaration" in body
    assert "combines two values across ' - '" in body


def test_the_declared_combined_range_reads_as_the_revision_it_claims_to_be(
    adopted, browser, tmp_path
):
    """The same file, with the composition declared, proves out."""

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    response = offer(
        browser("operator@example.test"),
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )

    assert response.status_code == 200, response.text
    body = prose(response.text)
    assert "This file reads as the registration it claims to be" in body


def test_only_technical_operations_may_offer_a_replacement(adopted, browser, tmp_path):
    """The operations reading is operations', which is #509's split."""

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    client = browser("member@example.test")
    page = client.get(f"/template-and-mapping/{adopted.slug}")
    assert page.status_code == 200, page.text

    response = client.post(
        f"/template-and-mapping/{adopted.slug}/validate",
        data={
            auth.CSRF_FIELD: client.cookies.get(auth.CSRF_COOKIE),
            "kind": "output_template",
            "identity": "Customer standard UCM export",
            "version": "2027.1",
        },
        files={"upload": ("successor.xlsx", successor, "application/vnd.ms-excel")},
        follow_redirects=False,
    )

    assert response.status_code == 403, response.text


# --- criterion 2: the comparison, and one attributable act -----------------


def test_the_comparison_names_what_moved_what_joined_and_what_left(
    session, adopted, browser, tmp_path
):
    """#829's second criterion: truthful, field by field.

    The offered form combines the station range, stops heading `Material` and
    starts heading `Utility Subtype`. Each of those is a different answer, and
    the page gives each one rather than scoring the change.
    """

    successor = workbook_bytes(
        tmp_path / "shifted.xlsx",
        [[*row[:4], "Distribution", *row[5:]] for row in combined_rows()],
        headings=shifted_headings(),
    )
    response = offer(
        browser("operator@example.test"),
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-2027-form",
        version="v1",
        combined=[STATION_RANGE],
    )

    assert response.status_code == 200, response.text
    body = prose(response.text)
    assert "This file reads as the registration it claims to be" in body

    # A field that moved and whose meaning changed says both.
    assert "read from 'Start Station' now and 'Start Station', 'End Station'" in body
    assert (
        "carried as 1 → 1 under one_value_per_column_v1 now and as "
        "1 → 2 under combined_range_v1" in body
    )
    # One that joined, and one that left, with the promise that matters beside
    # the one that left.
    flowing = " ".join(body.split())
    assert "The registered revision carries no column for it." in flowing
    assert "The proposed revision carries no column for it." in flowing
    assert "Its accepted value is unchanged." in flowing
    # And one that did not move at all.
    assert "Nothing." in flowing


def test_the_coordinator_approves_the_link_operations_handed_them(
    session, adopted, browser, tmp_path
):
    """One attributable act, through the form the page rendered.

    Operations validates; the coordinator opens the same proposal from its
    staged digest and approves it. Nothing about the submission is composed
    here -- the approval carries exactly the hidden fields, including the
    request-forgery token, that the rendered form emitted.
    """

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    coordinator = browser("coordinator@example.test")
    validated = offer(
        coordinator,
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )
    assert validated.status_code == 200, validated.text

    handed = coordinator.get(handoff_link(adopted, validated.text))
    assert handed.status_code == 200, handed.text
    assert "customer-combined-range" in prose(handed.text)

    response = approve(coordinator, adopted, handed.text)

    assert response.status_code == 201, response.text
    assert "customer-combined-range v1 is in force" in prose(response.text)
    current = in_force(session, adopted, "field_mapping")
    assert (current.format_identity, current.format_version) == (
        "customer-combined-range",
        "v1",
    )
    assert current.registered_by_principal == COORDINATOR.subject
    # The registration records what it declares and not only that it exists.
    manifest = registered_manifest(session, adopted)
    assert manifest.mapping_for("station_from").composition == COMBINED_RANGE
    assert manifest.mapping_for("utility_id").composition == ONE_VALUE_PER_COLUMN
    # The predecessor stays readable: a package already issued was rendered
    # through it.
    assert len(registrations(session, adopted, "field_mapping")) == 2


def accepted_record(session, project) -> tuple:
    """Every accepted value of this project, with the identity that decided it.

    The fingerprint the population carries and then every field in it, because
    the fingerprint says two readings differ and this says which value moved.
    """

    population = read_accepted_field_population(session, project.id)
    return (
        population.fingerprint,
        population.revision_id,
        tuple(
            sorted(
                (
                    field.fact_subject_key,
                    field.fact_type,
                    str(field.value),
                    field.fact_id,
                    field.decision_id,
                    field.revision_id,
                )
                for record in population.records
                for field in record.fields.values()
            )
        ),
    )


def issued_state(session, package) -> dict:
    """What an approved package says: its receipt, its digests and its bytes.

    ``retrieve_released_artifact`` reads through the content-addressed store,
    which verifies the digest on the way out, so a retained object rewritten
    behind the store's back raises here rather than comparing unequal (#830).
    """

    session.expire(package)
    sealed = package_set(session, package)
    return {
        "identity": package.package_identity,
        "accepted_revision_id": int(package.accepted_revision_id),
        "output_template_format_id": int(package.output_template_format_id),
        "field_mapping_format_id": int(package.field_mapping_format_id),
        "sealed": tuple(
            (one.artifact_type, one.content_sha256, one.storage_key, one.byte_count)
            for one in sealed
        ),
        "bytes": {
            one.artifact_type: retrieve_released_artifact(
                session, package, one.artifact_type
            )
            for one in sealed
        },
    }


def approve_an_issue(session, adoption: Adoption):
    """One approved issue of this project, rendered through the mapping in force.

    Built through #529's and #533's own fixtures rather than reconstructed
    here, because an issue this test claims a replacement cannot reach is only
    that claim if it is the issue the authorization command would accept.
    """

    enroll_member(
        session,
        project_id=adoption.project.id,
        email="releaser@example.test",
        principal=RELEASER,
        display_name="Releaser",
        designations=[EXTERNAL_RELEASE],
        operator=ENROLLER,
    )
    prepared = PreparedProject(
        project=adoption.project,
        revision_id=adoption.revision_id,
        template_bytes=adoption.template_bytes,
    )
    configure_issued_set(session, prepared)
    candidate = prepare_candidate(session, prepared, content_store())
    authorize_release_package(
        session,
        project_id=adoption.project.id,
        candidate_id=candidate.id,
        releaser=RELEASER,
        authorized_at=NOW,
    )
    session.flush()
    return session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id == adoption.project.id
        )
    ).one()


def test_registering_a_replacement_changes_no_accepted_value(
    session, adoption, adopted, browser, tmp_path
):
    """#829's second criterion, over the values themselves and the row counts.

    ADR-0076 records the template and mapping identities apart from the adopted
    data baseline exactly so a layout change cannot move the record.
    ``record_counts.nothing_written`` counts every row of every project-scoped
    table this project holds, before and after, so the proof is not a family
    somebody picked: a Fact decided, a revision opened, a segment appended or a
    source row rewritten would each change a count whichever value it touched.
    The two tables the registration writes on purpose are named; the other two
    hundred stay guarded.

    A count is not the whole proof, because it sees no value changed in place
    and no same-count substitution. So beside it: every accepted value of this
    project with the Fact, decision and revision that made it effective, read
    before and after; and an issue already approved through the mapping being
    replaced, whose receipt, sealed digests and retrieved bytes are read the
    same way -- through the store that verifies them (#830).
    """

    package = approve_an_issue(session, adoption)
    issued = issued_state(session, package)
    assert issued["bytes"], "the approved issue sealed no artifact to compare"
    before = accepted_record(session, adopted)
    assert before[2], "this project accepted no value for a replacement to move"

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    coordinator = browser("coordinator@example.test")
    validated = offer(
        coordinator,
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )
    handed = coordinator.get(handoff_link(adopted, validated.text))

    with nothing_written(
        session,
        adopted.id,
        apart_from={
            # The registration itself and what it declares: those two rows are
            # the act, and neither is an accepted value. Everything else this
            # project holds -- every Fact, decision, revision, segment and
            # source row among them -- is counted and unchanged.
            "project_baseline_formats",
            "project_baseline_format_manifests",
        },
    ):
        assert approve(coordinator, adopted, handed.text).status_code == 201

    # The values themselves, not only how many rows hold them.
    assert accepted_record(session, adopted) == before
    # And the issue already approved through the replaced mapping: the same
    # receipt, the same sealed digests, the same bytes.
    assert issued_state(session, package) == issued


def test_a_conforming_successor_template_is_registered_over_its_exact_bytes(
    session, adopted, browser, tmp_path
):
    """The other kind: a form that still means what the mapping says.

    Every heading, drop-down and composition is the registered revision's, so
    nothing stands in the way and the coordinator registers it. An output
    template is registered over its exact bytes (#690), so the registration
    carries the retained object it was verified against.
    """

    successor = workbook_bytes(tmp_path / "successor.xlsx", BASELINE_ROWS[:2])
    coordinator = browser("coordinator@example.test")
    validated = offer(
        coordinator,
        adopted,
        successor,
        kind="output_template",
        identity="customer-2027-form",
        version="v1",
    )
    assert validated.status_code == 200, validated.text
    assert "This file reads as the registration it claims to be" in prose(
        validated.text
    )
    # A template replacement is not a mapping change, so the page asks the
    # coordinator no field-by-field question about one.
    assert "What each field means now" not in prose(validated.text)

    handed = coordinator.get(handoff_link(adopted, validated.text))
    response = approve(coordinator, adopted, handed.text)

    assert response.status_code == 201, response.text
    current = in_force(session, adopted, "output_template")
    assert (current.format_identity, current.format_version) == (
        "customer-2027-form",
        "v1",
    )
    assert current.content_sha256 == sha256(successor).hexdigest()
    retained = session.get(BaselineFormatObject, int(current.id))
    assert retained is not None and retained.byte_count == len(successor)
    assert content_store().get(
        retained.storage_key, sha256=current.content_sha256
    ) == successor


def test_a_member_who_holds_no_coordination_designation_registers_nothing(
    session, adopted, browser, tmp_path
):
    """The approval is the coordinator's act, and the operator is not one."""

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    operator = browser("operator@example.test")
    validated = offer(
        operator,
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )

    response = approve(operator, adopted, validated.text)

    assert response.status_code == 403, response.text
    assert len(registrations(session, adopted, "field_mapping")) == 1


def test_the_same_approval_submitted_twice_registers_one(
    session, adopted, browser, tmp_path
):
    """A resubmitted approval converges rather than opening a second one.

    Unchanged is decided before stale for this reason: the registration the
    first submission made is now the one in force, so recomposing the same
    offer against it digests to exactly what it registered. Answering the
    second click with "somebody else changed this" would be untrue and
    unrecoverable from the page.
    """

    successor = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    coordinator = browser("coordinator@example.test")
    validated = offer(
        coordinator,
        adopted,
        successor,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )
    handed = coordinator.get(handoff_link(adopted, validated.text))

    first = approve(coordinator, adopted, handed.text)
    second = approve(coordinator, adopted, handed.text)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert "Nothing changed" in prose(second.text)
    assert len(registrations(session, adopted, "field_mapping")) == 2


def test_an_approval_after_someone_else_registered_is_refused(
    session, adopted, browser, tmp_path
):
    """The proposal binds the registration it would supersede, and it moved."""

    combined = workbook_bytes(tmp_path / "combined.xlsx", combined_rows())
    coordinator = browser("coordinator@example.test")
    validated = offer(
        coordinator,
        adopted,
        combined,
        kind="field_mapping",
        identity="customer-combined-range",
        version="v1",
        combined=[STATION_RANGE],
    )
    handed = coordinator.get(handoff_link(adopted, validated.text))

    # Somebody else registers a different revision in the meantime.
    other = offer(
        coordinator,
        adopted,
        workbook_bytes(tmp_path / "renamed.xlsx", combined_rows()),
        kind="field_mapping",
        identity="customer-combined-range",
        version="v2",
        combined=[STATION_RANGE],
        name="renamed.xlsx",
    )
    assert approve(coordinator, adopted, other.text).status_code == 201

    response = approve(coordinator, adopted, handed.text)

    assert response.status_code == 409, response.text
    assert "replaced by someone else" in prose(response.text)
    assert len(registrations(session, adopted, "field_mapping")) == 2


# --- criterion 4: the boundary, and the token ------------------------------


def test_every_form_on_the_page_carries_the_forgery_token(adopted, browser):
    """#821's rule, on this page, read off the page itself.

    The architecture check proves the markup emits the field; this proves the
    field a browser receives is the token its own session was issued, because
    every write above went through it.
    """

    client = browser("operator@example.test")
    body = read(client, adopted)
    forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>.*?</form>", body, re.S)
    assert forms, "the page rendered no write form at all"
    for form in forms:
        assert f'name="{auth.CSRF_FIELD}"' in form


def test_a_legacy_project_has_no_registrations_to_replace(
    session, browser, project
):
    """A project that adopted no baseline is answered as a missing one."""

    enroll_member(
        session,
        project_id=project.id,
        email="coordinator@example.test",
        principal=COORDINATOR,
        display_name="Coordinator",
        designations=[COORDINATION],
        operator=ENROLLER,
    )
    response = browser("coordinator@example.test").get(
        f"/template-and-mapping/{project.slug}"
    )

    assert response.status_code == 404, response.text


# --- the comparison itself, without a browser ------------------------------


def test_a_revision_compared_with_itself_reports_no_difference(
    session, adopted, tmp_path
):
    """The comparison is truthful in the direction that is easy to get wrong."""

    manifest = registered_manifest(session, adopted)
    rows = compare_mappings(manifest, manifest)

    assert rows, "the registered revision carries fields to compare"
    assert all(row.state == "unchanged" for row in rows)
    assert all(row.differences == () for row in rows)
