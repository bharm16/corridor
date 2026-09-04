"""Authorizing one prepared candidate, and the receipt it writes (#533).

Every instant here is declared. Nothing in this file reads a clock: a release
is recorded at the business instant its caller supplies, and a test that asked
the machine what time it was could not prove that the predecessor is the chain
head rather than the newest timestamp — which is the whole of #634's principle
as this ticket applies it.

The tests are grouped the way the ticket's rules are: what the receipt binds,
what the composite key makes unrepresentable, what refuses the act, what the
predecessor may and may not be, what the two "has it been issued" questions
answer, and — separately, over the real ``corridor_web`` login against
committed rows — where the external-release designation is actually enforced.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import NullPool

from corridor import access
from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.analytics import AnalyticsBinding, EventFamily, capture_events
from corridor.config import settings
from corridor.db import Session, engine
from corridor.issue_profile import UPDATED_UCM
from corridor.issue_rendering import NO_PRIOR_COMPARISON_STATEMENT
from corridor.models import (
    Project,
    ProjectRecordRevision,
    ReleaseCandidate,
    ReleasePackage,
    ReleasePackageArtifact,
)
from corridor.object_storage import LocalFilesystemStore
from corridor.principals import HumanPrincipal
from corridor.release_authorization import (
    ARTIFACT_MISSING,
    CANDIDATE_BLOCKED,
    CANDIDATE_STALE,
    DIGEST_MISMATCH,
    NOT_DESIGNATED,
    NO_SUCH_CANDIDATE,
    RECEIPT_SCHEMA_VERSION,
    TEMPLATE_OR_MAPPING_REPLACED,
    AuthorizationRefused,
    authorize_release_package,
    candidate_set,
    current_authorized_package,
    current_issue_state_differences,
    current_issue_state_is_issued,
    package_set,
    receipt_declaration,
    receipt_payload,
    release_history,
    retrieve_released_artifact,
    revision_has_been_issued,
)
from corridor.issue_content import (
    CHANGE_SUMMARY_IDENTITY,
    CHANGE_SUMMARY_VERSION,
    CHASE_LIST_IDENTITY,
    CHASE_LIST_VERSION,
    UCM_RENDERER_IDENTITY,
    UCM_RENDERER_VERSION,
    WEEKLY_REPORT_IDENTITY,
    WEEKLY_REPORT_VERSION,
)
from corridor.issue_profile import (
    ArtifactEntry,
    DecisionBlockingPolicy,
    RendererRevision,
)
from corridor.issue_rendering import (
    SECTION_COMMITMENTS,
    SECTION_CONSTRAINT_ALERTS,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_KEY_DATES,
    SECTION_PENDING_COORDINATION,
    ReportSection,
    SourceCoverage,
    TemplateBinding,
)
from corridor.release_candidate import (
    attach_candidate,
    bind_preparation,
    latest_authorized_package,
    render_candidate_artifacts,
)

from coverage_support import declare_coverage
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import (
    Rendition,
    append_deltas,
    configure_issue,
    modify,
    subject,
)


COORDINATOR = HumanPrincipal("local:coordinator")
OPERATOR = HumanPrincipal("local:operator")
RELEASER = HumanPrincipal("local:releaser")
UNDESIGNATED = HumanPrincipal("local:reader")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 3, 2, 7, 0, tzinfo=timezone.utc)
RELEASED_AT = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
LATER_RELEASE = datetime(2026, 3, 9, 8, 0, tzinfo=timezone.utc)
BACKDATED_RELEASE = datetime(2026, 3, 3, 8, 0, tzinfo=timezone.utc)

BINDING = AnalyticsBinding(
    code_revision="git:533",
    product_revision="2026.03",
    packetizer_rules_version="delta-partition-v2",
    enabled_feature_flags=("release_authorization",),
)

WEB_PASSWORD = os.environ.get("CORRIDOR_WEB_DB_PASSWORD") or "corridor_web"


UCM_RENDERER = RendererRevision(UCM_RENDERER_IDENTITY, UCM_RENDERER_VERSION)
SUMMARY = ArtifactEntry(
    "accepted_change_summary",
    RendererRevision(CHANGE_SUMMARY_IDENTITY, CHANGE_SUMMARY_VERSION),
)
WEEKLY = ArtifactEntry(
    "weekly_coordination_report",
    RendererRevision(WEEKLY_REPORT_IDENTITY, WEEKLY_REPORT_VERSION),
)
CHASE = ArtifactEntry(
    "chase_list", RendererRevision(CHASE_LIST_IDENTITY, CHASE_LIST_VERSION)
)

TEMPLATE = TemplateBinding(
    template_identity="partner-weekly",
    template_version="3",
    mapping_identity="partner-weekly-mapping",
    mapping_version="2",
    sections=(
        ReportSection(key=SECTION_CONSTRAINT_ALERTS, heading="Items needing attention"),
        ReportSection(key=SECTION_COMMITMENTS, heading="Utility commitments"),
        ReportSection(key=SECTION_KEY_DATES, heading="Dates we need work by"),
        ReportSection(key=SECTION_FOLLOW_UP_PLANS, heading="Our next steps"),
        ReportSection(
            key=SECTION_PENDING_COORDINATION, heading="Open questions with owners"
        ),
    ),
)

# A moved committed date nobody has decided, with a customer policy that waits
# on exactly that field. #641 executes the selector; this is what makes a
# candidate `blocked` rather than merely honest about an open difference.
RESOLVE_COMMITTED_DATE = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision="field:committed_date",
    statement="A moved Promised For is decided before this issue goes out.",
)


class Adopted:
    def __init__(self, project, revision_id, template_bytes):
        self.project = project
        self.revision_id = revision_id
        self.template_bytes = template_bytes


def coverage_named(
    session, adopted, identity: str = "weekly-coverage-2026-03-02", *, exception: bool = False
):
    """One confirmed coverage declaration, optionally with an honest exception.

    #675 made this a persisted row a candidate names by foreign key rather than
    a value a caller composes, so a fixture confirms one before it prepares.
    """

    return declare_coverage(
        session,
        adopted.project,
        cutoff=CUTOFF,
        principal=COORDINATOR,
        confirmed_at=PREPARED_AT,
        variant=identity,
        lines=(
            SourceCoverage(
                source_name="Weekly utility conflict matrix",
                requirement="required",
                state="late" if exception else "read",
                detail=(
                    "the 2026-03-01 revision had not arrived by the cutoff"
                    if exception
                    else "the 2026-03-01 revision was read in full"
                ),
            ),
        ),
    )


def preparation_reading(project_id: int, revision_id: int) -> dict:
    """One ``report_preparation`` reading, in the shape that pass returns."""

    return {
        "schema_version": "report-preparation-result-v1",
        "project_id": project_id,
        "configuration_version": "report-preparation-v1",
        "observed_at": PREPARED_AT.isoformat(),
        "health": "healthy",
        "window_start": "",
        "through_delta_id": 10**9,
        "through_disposition_id": 10**9,
        "accepted_revision_id": revision_id,
        "resolved_accepted": 0,
        "resolved_edited": 0,
        "resolved_rejected": 0,
        "proposed_new": 0,
        "open_actionable": 0,
        "open_deferred": 0,
        "superseded": 0,
    }


def configure(
    session,
    adopted,
    *,
    artifacts=(),
    coverage=(),
    policies=(),
    effective_from=JANUARY,
):
    return configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=effective_from,
        ucm=UCM_RENDERER,
        artifacts=tuple(artifacts),
        coverage=tuple(coverage),
        policies=tuple(policies),
    )


def bind(session, adopted, **overrides):
    if "coverage_declaration_id" not in overrides:
        overrides["coverage_declaration_id"] = coverage_named(session, adopted).id
    arguments = {
        "project_id": adopted.project.id,
        "preparation": preparation_reading(adopted.project.id, adopted.revision_id),
        "source_cutoff": CUTOFF,
        "prepared_at": PREPARED_AT,
        "templates": TEMPLATE,
        "first_issue_behavior": NO_PRIOR_COMPARISON_STATEMENT,
        "template_bytes": adopted.template_bytes,
        "binding": BINDING,
    }
    arguments.update(overrides)
    return bind_preparation(session, **arguments)


def prepare(session, adopted, store, **overrides):
    """Bind, render and attach one candidate in this rollback-scoped session."""

    bound = bind(session, adopted, **overrides)
    rendered = render_candidate_artifacts(
        bound, template_bytes=adopted.template_bytes, session=session, store=store
    )
    candidate = attach_candidate(
        session,
        bound,
        rendered,
        prepared_by=COORDINATOR,
        preparation=preparation_reading(adopted.project.id, adopted.revision_id),
        templates=TEMPLATE,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
        template_bytes=adopted.template_bytes,
    )
    return bound, rendered, candidate


def open_delta(session, adopted):
    """One unresolved proposed change to the accepted Promised For."""

    rendition = Rendition(session=session, project=adopted.project, name="later.xlsx")
    rendition.capture(
        fact_type="committed_date",
        value="2026-06-01",
        subject_key=subject(3),
        date_value=date(2026, 6, 1),
    )
    return append_deltas(
        session,
        adopted.project,
        rendition,
        source_revision="UCM workbook revision D",
        values=[
            modify(
                subject_key=subject(3),
                field_name="committed_date",
                accepted_value="2026-03-01",
                proposed_value="2026-06-01",
                baseline_revision=adopted.revision_id,
            )
        ],
    )



@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return LocalFilesystemStore(tmp_path / "artifacts")


@pytest.fixture
def adopted(session, tmp_path, store):
    """One adopted project with a coordinator and a designated releaser."""

    row = Project(
        slug=f"release-authorization-{uuid4().hex[:8]}",
        name="Release Authorization",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    enroll_member(
        session,
        project_id=row.id,
        email="coordinator@example.test",
        principal=COORDINATOR,
        display_name="Coordinator",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    enroll_member(
        session,
        project_id=row.id,
        email="releaser@example.test",
        principal=RELEASER,
        display_name="Releaser",
        designations=[EXTERNAL_RELEASE],
        operator=OPERATOR,
    )
    # A member with no write designation at all. Membership is the read
    # boundary; it is not authority to issue anything.
    enroll_member(
        session,
        project_id=row.id,
        email="reader@example.test",
        principal=UNDESIGNATED,
        display_name="Reader",
        designations=[],
        operator=OPERATOR,
    )
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, row, body, tmp_path)
    return Adopted(project=row, revision_id=revision_id, template_bytes=body)


def _authorize(session, adopted, candidate, store, *, at=RELEASED_AT, by=RELEASER):
    return authorize_release_package(
        session,
        project_id=adopted.project.id,
        candidate_id=candidate.id,
        releaser=by,
        authorized_at=at,
        store=store,
        binding=BINDING,
    )


def _package(session, authorization) -> ReleasePackage:
    return session.get_one(ReleasePackage, authorization.package_id)


def _next_candidate(session, adopted, store, *, identity="second-week"):
    """A fresh candidate prepared after whatever has happened so far."""

    _, _, candidate = prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, identity).id,
    )
    return candidate


# --- what one authorization writes -----------------------------------------


def test_the_first_authorization_seals_the_set_and_invents_no_predecessor(
    session, adopted, store
):
    """ADR-0086: a project's first issue is valid with nothing before it.

    The project here has an adopted baseline, an accepted revision, delivered
    documents and a prepared candidate — every timestamp a predecessor could
    have been derived from. The receipt still says, explicitly, that there is
    none.
    """

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, _, candidate = prepare(session, adopted, store)

    authorization = _authorize(session, adopted, candidate, store)
    package = _package(session, authorization)

    assert authorization.created
    assert authorization.issue_number == 1
    assert authorization.previous_package_id is None
    assert package.previous_package_id is None
    assert package.previous_sequence_number is None
    assert package.sequence_number == 1
    assert package.candidate_id == candidate.id
    assert package.accepted_revision_id == adopted.revision_id
    assert package.authorized_by_principal == RELEASER.subject
    assert package.authorized_at == RELEASED_AT

    receipt = json.loads(package.receipt_declaration)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["previous_authorized_package"] is None
    # The identity is the digest of the exact bytes, checked by the database.
    assert package.package_identity == sha256(
        package.receipt_declaration.encode("utf-8")
    ).hexdigest()


def test_the_receipt_binds_every_input_adr_0086_names(session, adopted, store):
    """One receipt, and it enumerates the whole set rather than pointing at it."""

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, _, candidate = prepare(session, adopted, store)

    package = _package(session, _authorize(session, adopted, candidate, store))
    receipt = json.loads(package.receipt_declaration)

    assert receipt["candidate_identity"] == candidate.candidate_identity
    assert receipt["content_sha256"] == candidate.content_sha256
    assert receipt["accepted_revision_id"] == adopted.revision_id
    assert receipt["source_cutoff"] == CUTOFF.isoformat()
    assert receipt["coverage"]["identity"] == candidate.coverage_identity
    assert receipt["coverage"]["content_sha256"] == candidate.coverage_sha256
    assert receipt["issue_profile"]["version"] == candidate.issue_profile_version
    assert receipt["issue_profile"]["content_sha256"] == candidate.issue_profile_sha256
    assert receipt["output_template"]["format_id"] == (
        candidate.output_template_format_id
    )
    assert receipt["field_mapping"]["format_id"] == candidate.field_mapping_format_id
    assert receipt["authorized_by_principal"] == RELEASER.subject
    assert receipt["authorized_at"] == RELEASED_AT.isoformat()

    # The same columns on the row, so a reader needs no JSON to answer them.
    assert package.coverage_sha256 == candidate.coverage_sha256
    assert package.issue_profile_sha256 == candidate.issue_profile_sha256
    assert package.content_sha256 == candidate.content_sha256


def test_the_receipt_enumerates_every_artifact_identity_and_digest(
    session, adopted, store
):
    """The enumeration is copied by the database from the candidate's own rows."""

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, rendered, candidate = prepare(session, adopted, store)

    package = _package(session, _authorize(session, adopted, candidate, store))
    sealed = package_set(session, package)

    assert [one.artifact_type for one in sealed] == [
        one.artifact_type for one in rendered
    ]
    assert [one.content_sha256 for one in sealed] == [one.sha256 for one in rendered]
    assert sealed[0].artifact_type == UPDATED_UCM
    assert [one.position for one in sealed] == [1, 2, 3, 4]
    receipt = json.loads(package.receipt_declaration)
    assert [entry["content_sha256"] for entry in receipt["artifacts"]] == [
        one.sha256 for one in rendered
    ]
    assert all(entry["byte_count"] > 0 for entry in receipt["artifacts"])


def test_the_mandatory_ucm_is_columns_so_a_package_without_one_cannot_exist(
    session, adopted, store
):
    """#529's structural decision, carried into the receipt."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    package = _package(session, _authorize(session, adopted, candidate, store))

    assert package.ucm_content_sha256 == candidate.ucm_content_sha256
    assert package.ucm_storage_key == candidate.ucm_storage_key
    # `updated_ucm` is not an admitted artifact type in the enumeration table.
    session.add(
        ReleasePackageArtifact(
            package_id=package.id,
            project_id=adopted.project.id,
            artifact_type=UPDATED_UCM,
            renderer_identity="whatever",
            renderer_version="1",
            content_sha256="0" * 64,
            storage_key="00/" + "0" * 64 + ".xlsx",
            byte_count=1,
            position=9,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --- the composite binding -------------------------------------------------


def test_a_receipt_whose_revision_disagrees_with_its_candidate_is_unrepresentable(
    session, adopted, store
):
    """The point of the composite key, proved by trying to write the bad row.

    The receipt states the revision explicitly *and* proves it is the revision
    the candidate was prepared from, because both facts are one foreign key.
    Copying the candidate's revision into an independent column and hoping the
    two agree is what ``external_report_releases`` did, except that it did not
    even have the column at all (#635).

    Nothing else about the row is wrong: it is the project's first package, so
    it needs no predecessor, claims sequence one, and names a revision that
    really exists. Only the pairing is a lie, and only the pairing is refused.
    """

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    other_revision = _later_revision(session, adopted, "disagreeing-revision")
    declaration = '{"schema_version":"release-package-receipt-v1"}'

    with pytest.raises(IntegrityError) as refused:
        session.execute(
            text(
                "insert into release_packages ("
                "  project_id, package_identity, accepted_revision_id,"
                "  authorized_by_principal, authorized_at, candidate_id,"
                "  candidate_identity, content_sha256, sequence_number,"
                "  source_cutoff, coverage_identity, coverage_sha256,"
                "  issue_profile_id, issue_profile_identity,"
                "  issue_profile_version, issue_profile_sha256,"
                "  output_template_format_id, field_mapping_format_id,"
                "  ucm_renderer_identity, ucm_renderer_version,"
                "  ucm_content_sha256, ucm_storage_key, ucm_byte_count,"
                "  receipt_declaration, receipt_schema_version"
                ") select project_id,"
                "  encode(sha256(convert_to(:receipt, 'utf8')), 'hex'),"
                "  :other_revision, :who, :at, id,"
                "  candidate_identity, content_sha256, 1,"
                "  source_cutoff, coverage_identity, coverage_sha256,"
                "  issue_profile_id, issue_profile_identity,"
                "  issue_profile_version, issue_profile_sha256,"
                "  output_template_format_id, field_mapping_format_id,"
                "  ucm_renderer_identity, ucm_renderer_version,"
                "  ucm_content_sha256, ucm_storage_key, ucm_byte_count,"
                "  :receipt, 'release-package-receipt-v1'"
                " from release_candidates where id = :id"
            ),
            {
                "receipt": declaration,
                "other_revision": other_revision,
                "who": RELEASER.subject,
                "at": RELEASED_AT,
                "id": candidate.id,
            },
        )
    session.rollback()
    assert "fk_release_packages_candidate" in str(refused.value)


def test_a_candidate_cannot_be_attached_to_two_divergent_releases(
    session, adopted, store
):
    """One receipt per candidate, enforced by a key rather than by a reader."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    package = _package(session, _authorize(session, adopted, candidate, store))

    with pytest.raises(IntegrityError) as refused:
        session.execute(
            text(
                "insert into release_packages ("
                "  project_id, package_identity, accepted_revision_id,"
                "  authorized_by_principal, authorized_at, candidate_id,"
                "  candidate_identity, content_sha256, sequence_number,"
                "  source_cutoff, coverage_identity, coverage_sha256,"
                "  issue_profile_id, issue_profile_identity,"
                "  issue_profile_version, issue_profile_sha256,"
                "  output_template_format_id, field_mapping_format_id,"
                "  ucm_renderer_identity, ucm_renderer_version,"
                "  ucm_content_sha256, ucm_storage_key, ucm_byte_count,"
                "  receipt_declaration, receipt_schema_version"
                ") select project_id,"
                "  encode(sha256(convert_to(receipt_declaration || ' ',"
                "    'utf8')), 'hex'), accepted_revision_id,"
                "  authorized_by_principal, authorized_at, candidate_id,"
                "  candidate_identity, content_sha256, 1,"
                "  source_cutoff, coverage_identity, coverage_sha256,"
                "  issue_profile_id, issue_profile_identity,"
                "  issue_profile_version, issue_profile_sha256,"
                "  output_template_format_id, field_mapping_format_id,"
                "  ucm_renderer_identity, ucm_renderer_version,"
                "  ucm_content_sha256, ucm_storage_key, ucm_byte_count,"
                "  receipt_declaration || ' ', receipt_schema_version"
                " from release_packages where id = :id"
            ),
            {"id": package.id},
        )
    session.rollback()
    assert "uq_release_packages_candidate" in str(refused.value)


def test_a_sealed_receipt_cannot_be_edited_or_deleted(session, adopted, store):
    """#529's immutability trigger covers the receipt and its enumeration."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    package = _package(session, _authorize(session, adopted, candidate, store))

    for statement in (
        "update release_packages set authorized_by_principal = 'x' where id = :id",
        "delete from release_packages where id = :id",
        "delete from release_package_artifacts where package_id = :id",
    ):
        with pytest.raises(DBAPIError) as refused:
            with session.begin_nested():
                session.execute(text(statement), {"id": package.id})
        assert "immutable" in str(refused.value)

    session.expire_all()
    assert session.get_one(ReleasePackage, package.id).authorized_by_principal == (
        RELEASER.subject
    )


# --- what refuses the act ---------------------------------------------------


def test_a_blocked_candidate_cannot_be_authorized(session, adopted, store):
    """ADR-0086: an explicit customer policy is one of the four things that block.

    Clearing it needs a newly prepared candidate, because the decision state
    that blocks is bound into this candidate's own identity.
    """

    configure(
        session, adopted, artifacts=[WEEKLY], policies=[RESOLVE_COMMITTED_DATE]
    )
    open_delta(session, adopted)
    _, _, candidate = prepare(session, adopted, store)
    assert candidate.readiness == "blocked"

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, candidate, store)

    assert refused.value.code == CANDIDATE_BLOCKED
    assert "newly prepared candidate" in refused.value.sentence
    assert _packages(session, adopted) == []


def test_a_candidate_whose_record_moved_is_refused_with_nothing_released(
    session, adopted, store
):
    """The stale-preview blocker: the reviewed set is no longer what would seal."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    _later_revision(session, adopted, "record-moved")

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, candidate, store)

    assert refused.value.code == CANDIDATE_STALE
    assert "prepare a fresh candidate" in refused.value.sentence
    assert _packages(session, adopted) == []


def test_a_replaced_template_refuses_the_act(session, adopted, store):
    """A registration replaced between preparation and release is not releasable."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    _replace_output_template(session, adopted)

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, candidate, store)

    assert refused.value.code == TEMPLATE_OR_MAPPING_REPLACED
    assert _packages(session, adopted) == []


def test_a_missing_retained_artifact_refuses_with_no_partial_release(
    session, adopted, store, tmp_path
):
    """Revalidation reads the bytes back; it never re-renders them."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    (tmp_path / "artifacts" / candidate.ucm_storage_key).unlink()

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, candidate, store)

    assert refused.value.code == ARTIFACT_MISSING
    assert _packages(session, adopted) == []


def test_changed_retained_bytes_refuse_the_act(session, adopted, store, tmp_path):
    """A digest that no longer matches is a refusal, never a quiet release."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    held = tmp_path / "artifacts" / candidate.ucm_storage_key
    held.unlink()
    held.write_bytes(b"not the workbook anybody reviewed")

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, candidate, store)

    assert refused.value.code == DIGEST_MISMATCH
    assert _packages(session, adopted) == []


def test_a_candidate_prepared_before_an_earlier_issue_is_refused(
    session, adopted, store
):
    """Two candidates, one issue: the older one no longer names the baseline.

    Its ``previous_package_id`` is the predecessor the project had when it was
    prepared, and the chain head has moved past it. Releasing it anyway would
    hand the customer a "changes since" window measured from the wrong issue.
    """

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, older = prepare(session, adopted, store)
    newer = _next_candidate(session, adopted, store)
    _authorize(session, adopted, newer, store)

    with pytest.raises(AuthorizationRefused) as refused:
        _authorize(session, adopted, older, store, at=LATER_RELEASE)

    assert refused.value.code == CANDIDATE_STALE
    assert "comparison baseline" in refused.value.sentence
    assert [row.sequence_number for row in _packages(session, adopted)] == [1]


def test_the_command_refuses_a_receipt_that_misdescribes_the_sealed_set(
    session, adopted, store
):
    """The receipt bytes are the caller's, so the command checks them.

    Everything else the receipt records is a column with a key or a foreign key
    behind it. The artifact enumeration is prose in a text column, so the
    command compares it against the rows the candidate actually holds before it
    will store it.
    """

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    honest = json.loads(
        receipt_declaration(
            receipt_payload(
                candidate,
                candidate_set(session, candidate),
                previous=None,
                sequence_number=1,
                authorized_by_principal=RELEASER.subject,
                authorized_at=RELEASED_AT,
            )
        )
    )
    short = dict(honest, artifacts=honest["artifacts"][:-1])

    with pytest.raises(DBAPIError, match="does not enumerate the artifacts"):
        with session.begin_nested():
            session.scalar(
                select(
                    func.authorize_release_package(
                        adopted.project.id,
                        candidate.id,
                        RELEASER.subject,
                        RELEASED_AT,
                        json.dumps(short, sort_keys=True, separators=(",", ":")),
                    )
                )
            )
    assert _packages(session, adopted) == []


def test_authorization_names_a_candidate_that_was_prepared(session, adopted, store):
    configure(session, adopted, artifacts=[WEEKLY])
    prepare(session, adopted, store)

    with pytest.raises(AuthorizationRefused) as refused:
        authorize_release_package(
            session,
            project_id=adopted.project.id,
            candidate_id=10**9,
            releaser=RELEASER,
            authorized_at=RELEASED_AT,
            store=store,
            binding=BINDING,
        )

    assert refused.value.code == NO_SUCH_CANDIDATE


def test_a_release_is_recorded_at_a_declared_instant_never_a_clock_reading(
    session, adopted, store
):
    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)

    with pytest.raises(ValueError, match="nothing here reads a clock"):
        authorize_release_package(
            session,
            project_id=adopted.project.id,
            candidate_id=candidate.id,
            releaser=RELEASER,
            authorized_at=datetime(2026, 3, 2, 8, 0),
            store=store,
        )


# --- idempotent replay ------------------------------------------------------


def test_re_authorizing_the_same_candidate_returns_the_existing_release(
    session, adopted, store
):
    """Idempotent, and not a second issue: the release that exists comes back."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)

    first = _authorize(session, adopted, candidate, store)
    replay = _authorize(session, adopted, candidate, store, at=LATER_RELEASE)

    assert first.created is True
    assert replay.created is False
    assert replay.package_id == first.package_id
    assert replay.package_identity == first.package_identity
    assert replay.issue_number == 1
    assert len(_packages(session, adopted)) == 1
    # The replay did not move the recorded release time either.
    assert _package(session, replay).authorized_at == RELEASED_AT


# --- the predecessor --------------------------------------------------------


def test_a_later_authorization_has_exactly_one_predecessor(session, adopted, store):
    configure(session, adopted, artifacts=[WEEKLY])
    _, _, first_candidate = prepare(session, adopted, store)
    first = _authorize(session, adopted, first_candidate, store)

    second_candidate = _next_candidate(session, adopted, store)
    second = _authorize(session, adopted, second_candidate, store, at=LATER_RELEASE)

    package = _package(session, second)
    assert second.issue_number == 2
    assert second.previous_package_id == first.package_id
    assert package.previous_sequence_number == 1
    receipt = json.loads(package.receipt_declaration)
    assert receipt["previous_authorized_package"] == {
        "package_id": first.package_id,
        "package_identity": first.package_identity,
        "sequence_number": 1,
        "accepted_revision_id": adopted.revision_id,
    }


def test_the_predecessor_is_the_chain_head_and_never_the_newest_timestamp(
    session, adopted, store
):
    """#634's principle, applied here: the chain orders releases, not the clock.

    The second release is recorded at an instant three months *before* the
    first. It is still the first's successor, and the third candidate's
    predecessor is it — not the release carrying the newest ``authorized_at``.
    A predecessor chosen by timestamp would hand the third issue the wrong
    comparison baseline and silently reopen a window the customer has seen.
    """

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, first_candidate = prepare(session, adopted, store)
    first = _authorize(session, adopted, first_candidate, store, at=LATER_RELEASE)

    second_candidate = _next_candidate(session, adopted, store)
    second = _authorize(
        session, adopted, second_candidate, store, at=BACKDATED_RELEASE
    )

    assert _package(session, second).authorized_at < _package(
        session, first
    ).authorized_at
    assert second.previous_package_id == first.package_id

    head = current_authorized_package(session, adopted.project.id)
    assert head is not None and head.id == second.package_id
    assert latest_authorized_package(session, adopted.project.id).id == (
        second.package_id
    )

    third_candidate = _next_candidate(session, adopted, store, identity="third-week")
    assert third_candidate.previous_package_id == second.package_id


def test_a_prepared_but_unauthorized_candidate_never_advances_the_baseline(
    session, adopted, store
):
    """ADR-0086, stated as the thing the next issue's comparison reads."""

    configure(session, adopted, artifacts=[WEEKLY])
    prepare(session, adopted, store)
    prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, "second-week").id,
    )

    assert current_authorized_package(session, adopted.project.id) is None
    assert _packages(session, adopted) == []

    _, _, candidate = prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(session, adopted, "third-week").id,
    )
    authorization = _authorize(session, adopted, candidate, store)

    assert authorization.issue_number == 1
    assert authorization.previous_package_id is None
    head = current_authorized_package(session, adopted.project.id)
    assert head.candidate_id == candidate.id


def test_the_newly_approved_issue_is_the_sole_comparison_baseline(
    session, adopted, store
):
    """The next candidate compares against the issue just approved, and only it."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    authorization = _authorize(session, adopted, candidate, store)

    bound = bind(
        session,
        adopted,
        coverage_declaration_id=coverage_named(session, adopted, "second-week").id,
    )

    assert bound.previous_package_id == authorization.package_id
    assert bound.reading.previous_issue is not None
    assert bound.reading.previous_issue.issue_identity == (
        authorization.package_identity
    )
    assert bound.reading.previous_issue.accepted_revision_id == adopted.revision_id


# --- the two questions #635 asked -------------------------------------------


def test_has_this_revision_ever_been_issued(session, adopted, store):
    """The first question, answerable because the receipt binds a real key."""

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)

    assert not revision_has_been_issued(
        session, project_id=adopted.project.id, revision_id=adopted.revision_id
    )

    _authorize(session, adopted, candidate, store)

    assert revision_has_been_issued(
        session, project_id=adopted.project.id, revision_id=adopted.revision_id
    )
    assert not revision_has_been_issued(
        session, project_id=adopted.project.id, revision_id=adopted.revision_id + 10**6
    )


def test_has_the_current_issue_state_been_issued(session, adopted, store):
    """The second, different question — the one #537's portfolio needs.

    A project can answer yes to "has this revision been issued" and no to this
    on the same afternoon, which is why they are two functions and not one.
    """

    configure(session, adopted, artifacts=[WEEKLY])
    assert current_issue_state_differences(
        session, adopted.project.id, as_of=RELEASED_AT
    ) == ("this project has not authorized an issue yet",)

    _, _, candidate = prepare(session, adopted, store)
    _authorize(session, adopted, candidate, store)

    assert current_issue_state_is_issued(
        session, adopted.project.id, as_of=RELEASED_AT
    )

    _later_revision(session, adopted, "record-moved-again")

    differences = current_issue_state_differences(
        session, adopted.project.id, as_of=RELEASED_AT
    )
    assert differences and "accepted record moved" in differences[0]
    # The revision that *was* issued is still recorded as issued: history and
    # currency are different facts.
    assert revision_has_been_issued(
        session, project_id=adopted.project.id, revision_id=adopted.revision_id
    )


def test_a_reconfigured_issue_set_makes_the_current_state_unissued(
    session, adopted, store
):
    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    _authorize(session, adopted, candidate, store)

    configure(
        session,
        adopted,
        artifacts=[WEEKLY, CHASE],
        effective_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    differences = current_issue_state_differences(
        session, adopted.project.id, as_of=RELEASED_AT
    )
    assert any("configured to externally issue changed" in one for one in differences)


# --- retrieval and history --------------------------------------------------


def test_every_released_artifact_stays_byte_exact_after_the_record_changes(
    session, adopted, store
):
    """ADR-0040's rule, widened to the set: the customer's copy does not move."""

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, rendered, candidate = prepare(session, adopted, store)
    package = _package(session, _authorize(session, adopted, candidate, store))
    before = {one.artifact_type: one.content for one in rendered}

    # The Project Record moves on afterwards.
    _later_revision(session, adopted, "record-moved-after-release")
    open_delta(session, adopted)

    for artifact_type, expected in before.items():
        held = retrieve_released_artifact(
            session, package, artifact_type, store=store
        )
        assert held == expected
        assert sha256(held).hexdigest() == next(
            one.content_sha256
            for one in package_set(session, package)
            if one.artifact_type == artifact_type
        )


def test_retrieval_refuses_an_artifact_this_issue_does_not_contain(
    session, adopted, store
):
    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    package = _package(session, _authorize(session, adopted, candidate, store))

    with pytest.raises(LookupError, match="does not contain"):
        retrieve_released_artifact(session, package, "chase_list", store=store)


def test_release_history_presents_the_set_without_internal_receipt_identifiers(
    session, adopted, store
):
    """The complete set, the actor, the time, the revision, the predecessor.

    What is deliberately absent by default is the receipt's own row id, its
    digest identity, the candidate it sealed, and where the bytes are kept.
    ``with_identifiers`` adds the first three back for support work.
    """

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, _, first_candidate = prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=coverage_named(
            session, adopted, "week-one", exception=True
        ).id,
    )
    _authorize(session, adopted, first_candidate, store)
    second_candidate = _next_candidate(session, adopted, store)
    _authorize(session, adopted, second_candidate, store, at=LATER_RELEASE)

    history = release_history(session, adopted.project.id)

    assert [entry.issue_number for entry in history] == [1, 2]
    assert [entry.previous_issue_number for entry in history] == [None, 1]
    first, second = history
    assert first.authorized_by_principal == RELEASER.subject
    assert first.authorized_at == RELEASED_AT
    assert first.accepted_revision_id == adopted.revision_id
    assert first.source_cutoff == CUTOFF
    # #675 derives the identity from the confirmed declaration rather than
    # letting a caller name it, so it is the declaration's own.
    assert first.coverage_identity.startswith("issue-coverage:2026-03-02:")
    assert first.exceptions and "Weekly utility conflict matrix" in (
        first.exceptions[0]
    )
    assert [one.artifact_type for one in first.artifacts] == [
        UPDATED_UCM,
        "accepted_change_summary",
        "chase_list",
        "weekly_coordination_report",
    ]
    assert all(len(one.content_sha256) == 64 for one in first.artifacts)
    assert not hasattr(first.artifacts[0], "storage_key")
    assert (first.package_id, first.package_identity, first.candidate_id) == (
        None,
        None,
        None,
    )
    assert second.issue_number == 2

    with_ids = release_history(session, adopted.project.id, with_identifiers=True)
    assert with_ids[0].package_id is not None
    assert with_ids[0].candidate_id == first_candidate.id


# --- what release deliberately does not do ---------------------------------


def test_release_creates_no_email_transmittal_receipt_or_delivery_status(
    session, adopted, store
):
    """ADR-0086: authorizing a package is not sending it.

    Asserted by counting every table in the schema before and after, so a
    delivery record written into a relation this test never heard of would
    fail it too. Only the receipt and its enumeration may gain a row.
    """

    configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, _, candidate = prepare(session, adopted, store)
    before = _row_counts(session)

    _authorize(session, adopted, candidate, store)

    after = _row_counts(session)
    grew = {
        name: (before[name], count)
        for name, count in after.items()
        if count != before.get(name)
    }
    assert set(grew) == {"release_packages", "release_package_artifacts"}
    assert session.scalar(
        text("select count(*) from external_report_releases where project_id = :p"),
        {"p": adopted.project.id},
    ) == 0


def test_historical_external_report_releases_are_left_exactly_as_they_were(
    session, adopted, store
):
    """#635: unknown history stays unknown by name, and is never a predecessor.

    No row is migrated, none is adopted as a predecessor, and no revision link
    is inferred from a timestamp. The first authorized package is issue one
    even where a legacy PDF release already exists for the project.
    """

    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)
    legacy = session.execute(
        text(
            "select count(*) from external_report_releases where project_id = :p"
        ),
        {"p": adopted.project.id},
    ).scalar_one()

    authorization = _authorize(session, adopted, candidate, store)

    assert authorization.issue_number == 1
    assert authorization.previous_package_id is None
    assert (
        session.execute(
            text(
                "select count(*) from external_report_releases "
                " where project_id = :p"
            ),
            {"p": adopted.project.id},
        ).scalar_one()
        == legacy
    )


# --- the emission (#558) ----------------------------------------------------


def test_authorization_refusal_and_replay_are_all_emitted(session, adopted, store):
    configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = prepare(session, adopted, store)

    with capture_events() as collected:
        _authorize(session, adopted, candidate, store)
        _authorize(session, adopted, candidate, store)
        with pytest.raises(AuthorizationRefused):
            authorize_release_package(
                session,
                project_id=adopted.project.id,
                candidate_id=10**9,
                releaser=RELEASER,
                authorized_at=RELEASED_AT,
                store=store,
                binding=BINDING,
            )

    assert [event.metric_labels["status"] for event in collected.events] == [
        "authorized",
        "replayed",
        "refused",
    ]
    authorized = collected.events[0]
    assert authorized.family is EventFamily.RELEASE_AUTHORIZATION
    assert authorized.occurred_at == RELEASED_AT
    assert authorized.binding.code_revision == "git:533"
    assert authorized.binding.product_revision == "2026.03"
    assert authorized.binding.packetizer_rules_version == "delta-partition-v2"
    assert authorized.binding.enabled_feature_flags == ("release_authorization",)
    assert authorized.payload["candidate_identity"] == candidate.candidate_identity
    assert authorized.payload["accepted_revision_id"] == adopted.revision_id
    assert authorized.payload["issue_number"] == 1
    assert collected.events[2].payload["refusal_code"] == NO_SUCH_CANDIDATE
    assert all(
        event.metric_labels == {
            "surface": "release_authorization",
            "status": event.metric_labels["status"],
        }
        for event in collected.events
    )


# --- the privileges and partition the new relations carry -------------------


def test_no_runtime_login_may_write_a_package_of_its_own(session):
    """Asserted rather than assumed (#529's and #640's discipline).

    ``release_packages`` stays read-only to every runtime login, exactly as
    #529 left it, and its enumeration joins it. The only writer is the command,
    which proves the designation first.
    """

    privileges = {
        (login, table, act): session.execute(
            text(f"select has_table_privilege('{login}', 'public.{table}', '{act}')")
        ).scalar_one()
        for login in ("corridor_web", "corridor_worker")
        for table in ("release_packages", "release_package_artifacts")
        for act in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }

    assert {key for key, granted in privileges.items() if granted} == {
        (login, table, "SELECT")
        for login in ("corridor_web", "corridor_worker")
        for table in ("release_packages", "release_package_artifacts")
    }


def test_only_the_web_capability_may_execute_the_authorization_command(session):
    """A background run carries no person's release authority."""

    executable = {
        login: session.execute(
            text(
                "select has_function_privilege("
                f"'{login}', 'public.authorize_release_package(bigint, bigint, "
                "character varying, timestamp with time zone, text)', 'EXECUTE')"
            )
        ).scalar_one()
        for login in ("corridor_web", "corridor_worker")
    }
    assert executable == {"corridor_web": True, "corridor_worker": False}
    assert session.execute(
        text(
            "select p.proowner::regrole::text from pg_proc p "
            " where p.proname = 'authorize_release_package'"
        )
    ).scalar_one() == "corridor_fact_decision_writer"


def test_the_package_enumeration_is_partitioned_by_project(session):
    """A sealed package is customer content, so it carries #531's partition."""

    assert session.execute(
        text(
            "select relrowsecurity from pg_class "
            " where relname = 'release_package_artifacts'"
        )
    ).scalar_one()
    policies = {
        row[0]
        for row in session.execute(
            text(
                "select policyname from pg_policies "
                " where tablename = 'release_package_artifacts'"
            )
        ).all()
    }
    assert "p_release_package_artifacts_project_partition" in policies


# --- the designation, proved through the real corridor_web login ------------


def _web_url(database_name: str) -> str:
    return (
        make_url(settings.database_url)
        .set(database=database_name, username="corridor_web", password=WEB_PASSWORD)
        .render_as_string(hide_password=False)
    )


@pytest.fixture
def committed_candidate(runtime_database, tmp_path, monkeypatch):
    """One committed project with one prepared candidate and three members."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    backing = LocalFilesystemStore(tmp_path / "artifacts")
    factory = runtime_database.session_factory

    with factory() as setup:
        project = Project(
            slug=f"release-web-{uuid4().hex[:8]}",
            name="Release Web",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush()
        for principal, email, name, designations in (
            (COORDINATOR, "coordinator@example.test", "Coordinator", [COORDINATION]),
            (RELEASER, "releaser@example.test", "Releaser", [EXTERNAL_RELEASE]),
            (UNDESIGNATED, "reader@example.test", "Reader", []),
        ):
            enroll_member(
                setup,
                project_id=project.id,
                email=email,
                principal=principal,
                display_name=name,
                designations=designations,
                operator=OPERATOR,
            )
        body = workbook_bytes(tmp_path / "web.xlsx", BASELINE_ROWS)
        revision_id, _ = adopt(setup, project, body, tmp_path)
        holder = Adopted(
            project=project, revision_id=revision_id, template_bytes=body
        )
        configure(setup, holder, artifacts=[WEEKLY])
        _, _, candidate = prepare(setup, holder, backing)
        setup.commit()
        identity = (project.id, candidate.id)
    return _Committed(
        project_id=identity[0],
        candidate_id=identity[1],
        store=backing,
        database=runtime_database,
    )


class _Committed:
    def __init__(self, project_id, candidate_id, store, database):
        self.project_id = project_id
        self.candidate_id = candidate_id
        self.store = store
        self.database = database


@pytest.fixture
def web_connection(runtime_database):
    """A connection held by the deployed web capability, not by the owner."""

    web_engine = create_engine(
        _web_url(runtime_database.name), poolclass=NullPool, future=True
    )
    with web_engine.connect() as connection:
        yield connection
    web_engine.dispose()


def test_the_web_login_cannot_insert_a_package_without_the_command(
    committed_candidate, web_connection
):
    """If it could, every designation proof below would be theatre."""

    with pytest.raises(ProgrammingError) as refused:
        web_connection.execute(
            text(
                "insert into release_packages (project_id, package_identity) "
                "values (1, 'x')"
            )
        )
    web_connection.rollback()
    assert "permission denied for table release_packages" in str(refused.value)


def test_the_designated_releaser_authorizes_through_the_real_web_login(
    committed_candidate, web_connection
):
    """The whole path, as deployed: partition, command, receipt."""

    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web,
            principal_subject=RELEASER.subject,
            project_id=committed_candidate.project_id,
        )
        authorization = authorize_release_package(
            web,
            project_id=committed_candidate.project_id,
            candidate_id=committed_candidate.candidate_id,
            releaser=RELEASER,
            authorized_at=RELEASED_AT,
            store=committed_candidate.store,
            binding=BINDING,
        )
        assert authorization.created
        assert authorization.issue_number == 1
        web.rollback()


def test_a_member_without_the_external_release_designation_is_refused_by_postgresql(
    committed_candidate, web_connection
):
    """The check the amendment asks for: inside PostgreSQL, not at the route.

    The reader here holds an active membership of this project — the partition
    opens for them — and no release designation. The database is what refuses,
    as the command's own owner, so a second caller that forgets the web route's
    check still releases nothing.
    """

    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web,
            principal_subject=UNDESIGNATED.subject,
            project_id=committed_candidate.project_id,
        )
        with pytest.raises(AuthorizationRefused) as refused:
            authorize_release_package(
                web,
                project_id=committed_candidate.project_id,
                candidate_id=committed_candidate.candidate_id,
                releaser=UNDESIGNATED,
                authorized_at=RELEASED_AT,
                store=committed_candidate.store,
                binding=BINDING,
            )
        assert refused.value.code == NOT_DESIGNATED
        # The refusal is recoverable: only the savepoint was given up.
        assert web.scalar(text("select 1")) == 1
        assert (
            web.scalars(
                select(ReleasePackage).where(
                    ReleasePackage.project_id == committed_candidate.project_id
                )
            ).all()
            == []
        )
        web.rollback()


def test_the_coordinator_who_configured_the_issue_may_not_authorize_it(
    committed_candidate, web_connection
):
    """Designations are separate; one is not the others (#331, ADR-0034)."""

    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web,
            principal_subject=COORDINATOR.subject,
            project_id=committed_candidate.project_id,
        )
        with pytest.raises(AuthorizationRefused) as refused:
            authorize_release_package(
                web,
                project_id=committed_candidate.project_id,
                candidate_id=committed_candidate.candidate_id,
                releaser=COORDINATOR,
                authorized_at=RELEASED_AT,
                store=committed_candidate.store,
                binding=BINDING,
            )
        assert refused.value.code == NOT_DESIGNATED
        web.rollback()


def test_a_forged_principal_buys_nothing(committed_candidate, web_connection):
    """The roster is consulted, never the string the caller passed.

    The connection is authenticated and holds a legitimate partition for this
    project. It then names a principal nobody enrolled. The command reads the
    roster as its owner and finds nothing, so the invented subject releases
    exactly as much as it should: nothing.
    """

    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web,
            principal_subject=RELEASER.subject,
            project_id=committed_candidate.project_id,
        )
        with pytest.raises(AuthorizationRefused) as refused:
            authorize_release_package(
                web,
                project_id=committed_candidate.project_id,
                candidate_id=committed_candidate.candidate_id,
                releaser=HumanPrincipal("local:invented-releaser"),
                authorized_at=RELEASED_AT,
                store=committed_candidate.store,
                binding=BINDING,
            )
        assert refused.value.code == NOT_DESIGNATED
        web.rollback()


def test_a_withdrawn_designation_takes_effect_on_the_next_authorization(
    committed_candidate, web_connection
):
    """Read from the roster every time, so a withdrawal is felt at once."""

    with committed_candidate.database.session_factory.begin() as owner:
        owner.execute(
            text(
                "update project_roster_entries set can_release_externally = false "
                " where project_id = :p and principal_subject = :s"
            ),
            {"p": committed_candidate.project_id, "s": RELEASER.subject},
        )

    with OrmSession(bind=web_connection) as web:
        access.open_project_partition(
            web,
            principal_subject=RELEASER.subject,
            project_id=committed_candidate.project_id,
        )
        with pytest.raises(AuthorizationRefused) as refused:
            authorize_release_package(
                web,
                project_id=committed_candidate.project_id,
                candidate_id=committed_candidate.candidate_id,
                releaser=RELEASER,
                authorized_at=RELEASED_AT,
                store=committed_candidate.store,
                binding=BINDING,
            )
        assert refused.value.code == NOT_DESIGNATED
        web.rollback()


# --- helpers ----------------------------------------------------------------


def _later_revision(session, adopted, key: str) -> int:
    """One more accepted revision, so the candidate's is no longer the newest.

    Written through the record-decision role, because PostgreSQL refuses a
    Project Record revision from anybody else — the same door
    ``packet_review_support.move_accepted_value`` uses.
    """

    project_id = adopted.project.id
    session.flush()
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, idempotency_key"
            ") values (:project, 'resolve_delta', :who, :key) returning id"
        ),
        {
            "project": project_id,
            "who": COORDINATOR.subject,
            "key": f"{key}:{uuid4().hex[:8]}",
        },
    )
    session.execute(text("reset role"))
    session.expire_all()
    return int(revision_id)


def _replace_output_template(session, adopted) -> int:
    """Register a different output template and supersede the one in force."""

    project_id = adopted.project.id
    session.flush()
    session.execute(text("set local role corridor_fact_decision_writer"))
    # The same order the adoption command uses: claim the successor's id,
    # retire the predecessor against it, then insert. The unique index on the
    # effective registration is not deferrable, so the other order fails.
    replacement = int(
        session.scalar(text("select nextval('project_baseline_formats_id_seq')"))
    )
    session.execute(
        text(
            "update project_baseline_formats set superseded_by = :new "
            " where project_id = :project and format_kind = 'output_template'"
            "   and superseded_by is null"
        ),
        {"new": replacement, "project": project_id},
    )
    session.execute(
        text(
            "insert into project_baseline_formats ("
            "id, project_id, format_kind, format_identity, format_version,"
            " content_sha256, registered_by_principal, idempotency_key"
            ") values (:id, :project, 'output_template', 'partner-weekly', 'v9',"
            " :digest, :who, :key)"
        ),
        {
            "id": replacement,
            "project": project_id,
            "digest": sha256(b"partner-weekly:v9").hexdigest(),
            "who": COORDINATOR.subject,
            "key": f"template:{uuid4().hex[:10]}",
        },
    )
    session.execute(text("reset role"))
    session.expire_all()
    return replacement


def _packages(session, adopted) -> list[ReleasePackage]:
    return list(
        session.scalars(
            select(ReleasePackage).where(
                ReleasePackage.project_id == adopted.project.id
            )
        ).all()
    )


def _row_counts(session) -> dict[str, int]:
    names = [
        row[0]
        for row in session.execute(
            text(
                "select table_name from information_schema.tables "
                " where table_schema = 'public' and table_type = 'BASE TABLE' "
                " order by table_name"
            )
        ).all()
    ]
    counts = session.execute(
        text(
            " union all ".join(
                f"select '{name}' as t, count(*) as n from public.{name}"
                for name in names
            )
        )
    ).all()
    return {row[0]: int(row[1]) for row in counts}
