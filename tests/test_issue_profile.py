"""What a project is configured to externally issue, and when (#640, ADR-0091).

Every instant here is declared. Nothing in this file reads a clock: the ticket
is made of effective instants and reporting cutoffs, and a test that asked the
machine what time it was could not prove that an earlier cutoff still reads the
configuration it was decided under.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.baseline_adoption import FormatIdentity, effective_baseline_formats
from corridor.config import settings
from corridor.db import Session, engine
from corridor.issue_profile import (
    ArtifactEntry,
    CoverageRequirement,
    DecisionBlockingPolicy,
    IssueProfileDeclaration,
    IssueProfileRefused,
    RendererRevision,
    UPDATED_UCM,
    effective_issue_inventory,
    issue_profile_history,
    prepared_candidate_is_stale,
    register_issue_profile,
)
from corridor.models import IssueProfile, Project
from corridor.principals import HumanPrincipal

from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes


COORDINATOR = HumanPrincipal("local:coordinator")
RELEASER = HumanPrincipal("local:releaser")
OPERATOR = HumanPrincipal("local:operator")

# Three declared instants. The first configuration takes effect in January, the
# second in March, and every reading below is taken against one of them.
JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
FEBRUARY = datetime(2026, 2, 9, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 2, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 6, tzinfo=timezone.utc)

UCM_RENDERER = RendererRevision(identity="ucm-workbook-render", version="v3")
REPORT_RENDERER = RendererRevision(identity="coordination-report", version="v2")
SUMMARY_RENDERER = RendererRevision(identity="accepted-change-summary", version="v1")
CHASE_RENDERER = RendererRevision(identity="chase-list", version="v1")
SIDECAR_RENDERER = RendererRevision(identity="provenance-sidecar", version="v1")

# A second project's baseline. Every value differs from the first project's,
# because a Source Fact digest is bound to one project and an identical row
# adopted twice is the same Fact.
OTHER_ROWS = [
    ["ZZ-9", "Bluebonnet Electric Cooperative", "Gas", "24 in", "HDPE", "SR-XY",
     "2249+00", "2250+00", "Adjust", "2027-03-01", "2027-02-01",
     "vault at station", "UCM-9001", "https://ucm.example/records/9001"],
]


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
    return tmp_path / "files"


def _project(session, prefix="issue-profile"):
    row = Project(
        slug=f"{prefix}-{uuid4().hex[:8]}",
        name="Issue Profile Test",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


def _member(session, project, principal, designations):
    enroll_member(
        session,
        project_id=project.id,
        email=f"{principal.subject.split(':')[1]}@example.test",
        principal=principal,
        display_name=principal.subject,
        designations=designations,
        operator=OPERATOR,
    )


@pytest.fixture
def project(session, tmp_path, store):
    """One adopted project with a registered output template and field mapping.

    The profile names registrations, so the project has to hold them; adopting
    a baseline is how a project comes by both (#509).
    """

    row = _project(session)
    adopt(session, row, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    _member(session, row, COORDINATOR, [COORDINATION])
    _member(session, row, RELEASER, [EXTERNAL_RELEASE])
    return row


def _formats(session, project) -> tuple[FormatIdentity, FormatIdentity]:
    registered = effective_baseline_formats(session, project.id)
    return (
        FormatIdentity(
            kind="output_template",
            identity=registered["output_template"].format_identity,
            version=registered["output_template"].format_version,
            content_sha256=registered["output_template"].content_sha256,
        ),
        FormatIdentity(
            kind="field_mapping",
            identity=registered["field_mapping"].format_identity,
            version=registered["field_mapping"].format_version,
            content_sha256=registered["field_mapping"].content_sha256,
        ),
    )


def _declaration(session, project, *, artifacts=(), coverage=(), policies=()):
    template, mapping = _formats(session, project)
    return IssueProfileDeclaration(
        updated_ucm=UCM_RENDERER,
        output_template=template,
        field_mapping=mapping,
        configured_artifacts=tuple(artifacts),
        coverage_requirements=tuple(coverage),
        decision_blocking_policies=tuple(policies),
    )


def _register(
    session,
    project,
    declaration,
    *,
    effective_from=JANUARY,
    key="profile-1",
    identity="customer-issue",
    principal=COORDINATOR,
):
    return register_issue_profile(
        session,
        project_id=project.id,
        profile_identity=identity,
        declaration=declaration,
        effective_from=effective_from,
        principal=principal,
        idempotency_key=key,
    )


# --- What a project issues -------------------------------------------------


def test_a_ucm_only_project_issues_exactly_the_updated_workbook(session, project):
    """ADR-0091's floor: the mandatory member and nothing else."""

    _register(session, project, _declaration(session, project))

    inventory = effective_issue_inventory(session, project.id, FEBRUARY)

    assert inventory is not None
    assert inventory.artifact_types == (UPDATED_UCM,)
    assert inventory.issues(UPDATED_UCM)
    assert not inventory.issues("weekly_coordination_report")
    assert inventory.renderer_for(UPDATED_UCM) == UCM_RENDERER


def test_a_project_that_also_issues_the_weekly_report_says_so(session, project):
    _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[
                ArtifactEntry("weekly_coordination_report", REPORT_RENDERER)
            ],
        ),
    )

    inventory = effective_issue_inventory(session, project.id, FEBRUARY)

    assert inventory.artifact_types == (
        UPDATED_UCM,
        "weekly_coordination_report",
    )
    assert inventory.renderer_for("weekly_coordination_report") == REPORT_RENDERER


def test_a_complete_profile_carries_every_configured_artifact_and_its_rules(
    session, project
):
    """The full shape: five artifacts, coverage, and a blocking customer rule."""

    coverage = [
        CoverageRequirement(
            requirement="all_delivered_sources_read",
            statement="Every source delivered before the cutoff has been read.",
        )
    ]
    policies = [
        DecisionBlockingPolicy(
            policy="owner_sign_off",
            required_decision="Utility owner sign-off recorded",
            statement="This client will not accept an issue before sign-off.",
        )
    ]
    _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[
                ArtifactEntry("accepted_change_summary", SUMMARY_RENDERER),
                ArtifactEntry("chase_list", CHASE_RENDERER),
                ArtifactEntry("weekly_coordination_report", REPORT_RENDERER),
                ArtifactEntry("provenance_sidecar", SIDECAR_RENDERER),
            ],
            coverage=coverage,
            policies=policies,
        ),
    )

    inventory = effective_issue_inventory(session, project.id, FEBRUARY)

    assert inventory.artifact_types == (
        UPDATED_UCM,
        "accepted_change_summary",
        "chase_list",
        "provenance_sidecar",
        "weekly_coordination_report",
    )
    assert inventory.coverage_requirements == tuple(coverage)
    assert inventory.decision_blocking_policies == tuple(policies)


def test_the_inventory_names_the_template_and_mapping_it_renders_through(
    session, project
):
    template, mapping = _formats(session, project)
    _register(session, project, _declaration(session, project))

    inventory = effective_issue_inventory(session, project.id, FEBRUARY)

    assert inventory.output_template == template
    assert inventory.field_mapping == mapping


def test_a_project_with_no_registered_profile_reads_as_no_inventory(
    session, project
):
    """An explicit absence. "Issues nothing" is not a state a project reaches."""

    assert effective_issue_inventory(session, project.id, FEBRUARY) is None


# --- Derived at a cutoff, never stored per week -----------------------------


def test_the_effective_inventory_is_derived_from_the_cutoff(session, project):
    """One reading per cutoff, from the same two rows, forever."""

    _register(session, project, _declaration(session, project), key="v1")
    _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
        ),
        effective_from=MARCH,
        key="v2",
    )

    assert effective_issue_inventory(session, project.id, JANUARY).profile_version == 1
    before = effective_issue_inventory(session, project.id, FEBRUARY)
    after = effective_issue_inventory(session, project.id, APRIL)

    assert before.profile_version == 1
    assert before.artifact_types == (UPDATED_UCM,)
    assert after.profile_version == 2
    assert after.artifact_types == (UPDATED_UCM, "chase_list")


def test_a_cutoff_before_the_first_configuration_reads_nothing(session, project):
    _register(session, project, _declaration(session, project), effective_from=MARCH)

    assert effective_issue_inventory(session, project.id, JANUARY) is None
    assert effective_issue_inventory(session, project.id, APRIL) is not None


def test_a_later_registration_cannot_change_an_earlier_cutoff(session, project):
    """The whole point of the timing rule, asserted as a before-and-after."""

    _register(session, project, _declaration(session, project), key="v1")
    february_before = effective_issue_inventory(session, project.id, FEBRUARY)

    _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
        ),
        effective_from=MARCH,
        key="v2",
    )

    assert effective_issue_inventory(session, project.id, FEBRUARY) == february_before


def test_a_reading_requires_a_declared_cutoff(session, project):
    with pytest.raises(IssueProfileRefused, match="never supplies one from a clock"):
        effective_issue_inventory(session, project.id, datetime(2026, 2, 9))


# --- History ---------------------------------------------------------------


def test_a_profile_change_writes_a_new_version_and_edits_nothing(session, project):
    first = _register(session, project, _declaration(session, project), key="v1")
    original = (
        first.id,
        first.content_sha256,
        first.effective_from,
        first.ucm_renderer_version,
    )

    second = _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
        ),
        effective_from=MARCH,
        key="v2",
    )
    session.expire_all()
    kept = session.get_one(IssueProfile, original[0])

    assert second.profile_version == 2
    assert second.supersedes_id == first.id
    assert (
        kept.id,
        kept.content_sha256,
        kept.effective_from,
        kept.ucm_renderer_version,
    ) == original
    assert [row.profile_version for row in issue_profile_history(session, project.id)] == [
        1,
        2,
    ]


def test_a_version_effective_at_or_before_the_current_one_is_refused(
    session, project
):
    _register(session, project, _declaration(session, project), key="v1")

    for instant in (JANUARY, datetime(2025, 12, 1, tzinfo=timezone.utc)):
        with pytest.raises(DBAPIError, match="cannot be reconfigured after the fact"):
            with session.begin_nested():
                _register(
                    session,
                    project,
                    _declaration(session, project),
                    effective_from=instant,
                    key=f"v2-{instant.isoformat()}",
                )


def test_replaying_one_registration_returns_the_same_version(session, project):
    declaration = _declaration(session, project)
    first = _register(session, project, declaration, key="same")
    again = _register(session, project, declaration, key="same")

    assert again.id == first.id
    assert len(issue_profile_history(session, project.id)) == 1


def test_one_key_cannot_be_rebound_to_different_content(session, project):
    _register(session, project, _declaration(session, project), key="same")

    with pytest.raises(DBAPIError, match="bound to different content"):
        _register(
            session,
            project,
            _declaration(
                session,
                project,
                artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
            ),
            key="same",
        )


def test_a_second_lineage_in_one_project_is_refused(session, project):
    _register(session, project, _declaration(session, project), key="v1")

    with pytest.raises(DBAPIError, match="one profile lineage"):
        _register(
            session,
            project,
            _declaration(session, project),
            effective_from=MARCH,
            key="v2",
            identity="a-second-profile",
        )


def test_the_registration_records_who_configured_it_and_from_when(session, project):
    registered = _register(session, project, _declaration(session, project))

    assert registered.registered_by_principal == COORDINATOR.subject
    assert registered.effective_from == JANUARY


def test_the_stored_declaration_digests_to_what_the_row_records(session, project):
    declaration = _declaration(session, project)
    registered = _register(session, project, declaration)

    assert registered.declaration == declaration.declaration_json
    assert registered.content_sha256 == declaration.content_sha256


# --- Who may configure -----------------------------------------------------


def test_only_project_coordination_configures_the_issued_set(session, project):
    """The releaser authorizes a prepared package; they do not design one."""

    with pytest.raises(IssueProfileRefused, match="project-coordination decision"):
        _register(
            session, project, _declaration(session, project), principal=RELEASER
        )


def test_someone_outside_the_project_configures_nothing(session, project):
    with pytest.raises(IssueProfileRefused, match="project-coordination decision"):
        _register(
            session,
            project,
            _declaration(session, project),
            principal=HumanPrincipal("local:stranger"),
        )


# --- What a declaration may say --------------------------------------------


def test_the_updated_ucm_is_not_a_configurable_artifact(session, project):
    with pytest.raises(IssueProfileRefused, match="mandatory member"):
        _register(
            session,
            project,
            _declaration(
                session,
                project,
                artifacts=[ArtifactEntry(UPDATED_UCM, UCM_RENDERER)],
            ),
        )


def test_one_artifact_type_cannot_be_configured_twice(session, project):
    with pytest.raises(IssueProfileRefused, match="configured twice"):
        _register(
            session,
            project,
            _declaration(
                session,
                project,
                artifacts=[
                    ArtifactEntry("chase_list", CHASE_RENDERER),
                    ArtifactEntry("chase_list", REPORT_RENDERER),
                ],
            ),
        )


def test_an_unknown_artifact_type_is_refused(session, project):
    with pytest.raises(IssueProfileRefused, match="not a configurable artifact"):
        _register(
            session,
            project,
            _declaration(
                session,
                project,
                artifacts=[ArtifactEntry("invoice", REPORT_RENDERER)],
            ),
        )


def test_an_artifact_with_no_renderer_revision_is_refused(session, project):
    with pytest.raises(IssueProfileRefused, match="renderer version"):
        _register(
            session,
            project,
            _declaration(
                session,
                project,
                artifacts=[
                    ArtifactEntry("chase_list", RendererRevision("chase-list", "  "))
                ],
            ),
        )


def test_a_profile_needs_an_identity_and_an_idempotency_key(session, project):
    for override, expected in (
        ({"identity": "   "}, "needs an identity"),
        ({"key": "  "}, "needs an idempotency key"),
    ):
        with pytest.raises(IssueProfileRefused, match=expected):
            _register(session, project, _declaration(session, project), **override)


def test_a_declaration_cannot_name_a_mapping_where_a_template_belongs(
    session, project
):
    _, mapping = _formats(session, project)

    with pytest.raises(IssueProfileRefused, match="names a 'field_mapping'"):
        _register(
            session,
            project,
            IssueProfileDeclaration(
                updated_ucm=UCM_RENDERER,
                output_template=mapping,
                field_mapping=mapping,
            ),
        )


def test_a_profile_naming_another_projects_registration_is_refused(
    session, tmp_path, store, project
):
    other = _project(session, prefix="issue-profile-other")
    adopt(
        session,
        other,
        workbook_bytes(tmp_path / "b.xlsx", OTHER_ROWS),
        tmp_path,
        source_identity="UCM workbook revision D",
    )
    _, other_mapping = _formats(session, other)
    template, _ = _formats(session, project)

    assert other_mapping != _formats(session, project)[1], (
        "the two projects must hold different mapping registrations, or this "
        "test proves nothing about crossing a project boundary"
    )

    with pytest.raises(DBAPIError, match="not a registration of project"):
        _register(
            session,
            project,
            IssueProfileDeclaration(
                updated_ucm=UCM_RENDERER,
                output_template=template,
                field_mapping=other_mapping,
            ),
        )


def test_a_declaration_needs_a_business_instant_that_is_not_a_clock_reading(
    session, project
):
    with pytest.raises(IssueProfileRefused, match="nothing here reads a clock"):
        _register(
            session,
            project,
            _declaration(session, project),
            effective_from=datetime(2026, 1, 5),
        )


# --- What PostgreSQL refuses regardless of this module ----------------------


def _writer_refused(session, pattern: str, statement: str, **params):
    """Prove PostgreSQL refuses a raw write made by the command's own role.

    The role is taken before the savepoint so that rolling the savepoint back
    leaves a usable transaction to reset it in.
    """

    session.execute(text("set local role corridor_fact_decision_writer"))
    with pytest.raises(DBAPIError, match=pattern):
        with session.begin_nested():
            session.execute(text(statement), params)
    session.execute(text("reset role"))


def test_the_database_refuses_a_second_entry_of_one_artifact_type(session, project):
    registered = _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
        ),
    )

    _writer_refused(
        session,
        "uq_project_issue_profile_artifacts_type",
        "insert into project_issue_profile_artifacts ("
        "profile_id, project_id, artifact_type, renderer_identity, "
        "renderer_version) values (:profile, :project, 'chase_list', "
        "'other', 'v9')",
        profile=registered.id,
        project=project.id,
    )


def test_the_database_refuses_a_ucm_entry_in_the_configured_set(session, project):
    registered = _register(session, project, _declaration(session, project))

    _writer_refused(
        session,
        "ck_project_issue_profile_artifacts_type",
        "insert into project_issue_profile_artifacts ("
        "profile_id, project_id, artifact_type, renderer_identity, "
        "renderer_version) values (:profile, :project, 'updated_ucm', "
        "'r', 'v1')",
        profile=registered.id,
        project=project.id,
    )


def test_nothing_changes_a_registered_profile_in_place(session, project):
    """Two independent refusals, and the command's role holds neither privilege."""

    registered = _register(session, project, _declaration(session, project))

    for statement in (
        "update project_issue_profiles set ucm_renderer_version = 'v9'"
        " where id = :id",
        "delete from project_issue_profiles where id = :id",
    ):
        with pytest.raises(DBAPIError, match="only by the typed registration"):
            with session.begin_nested():
                session.execute(text(statement), {"id": registered.id})

    # The trigger is the second line: the role that owns the command was never
    # granted a way to change a row it already wrote.
    held = tuple(
        session.execute(
            text(
                "select has_table_privilege('corridor_fact_decision_writer', "
                f"'public.project_issue_profiles', '{act}')"
            )
        ).scalar_one()
        for act in ("UPDATE", "DELETE")
    )
    assert held == (False, False)


def test_a_runtime_capability_reads_the_profile_and_writes_none_of_it(
    session, project
):
    """The privileges the new tables carry, asserted rather than assumed."""

    privileges = {
        (login, table, act): session.execute(
            text(
                f"select has_table_privilege('{login}', 'public.{table}', '{act}')"
            )
        ).scalar_one()
        for login in ("corridor_web", "corridor_worker")
        for table in (
            "project_issue_profiles",
            "project_issue_profile_artifacts",
        )
        for act in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }

    assert {key for key, held in privileges.items() if held} == {
        (login, table, "SELECT")
        for login in ("corridor_web", "corridor_worker")
        for table in (
            "project_issue_profiles",
            "project_issue_profile_artifacts",
        )
    }


def test_the_database_refuses_a_digest_that_is_not_the_declarations(
    session, project
):
    declaration = _declaration(session, project)
    template, mapping = _formats(session, project)
    registered_ids = effective_baseline_formats(session, project.id)

    _writer_refused(
        session,
        "ck_project_issue_profiles_digest",
        "insert into project_issue_profiles ("
        "project_id, profile_identity, profile_version, content_sha256,"
        " declaration, declaration_schema_version, effective_from,"
        " ucm_renderer_identity, ucm_renderer_version,"
        " output_template_format_id, field_mapping_format_id,"
        " registered_by_principal, idempotency_key) values ("
        ":project, 'customer-issue', 1, :digest, :declaration,"
        " 'issue-profile-v1', :effective, 'r', 'v1', :template,"
        " :mapping, 'local:coordinator', 'raw')",
        project=project.id,
        digest="c" * 64,
        declaration=declaration.declaration_json,
        effective=JANUARY,
        template=registered_ids["output_template"].id,
        mapping=registered_ids["field_mapping"].id,
    )
    assert (template, mapping) == _formats(session, project)


def test_the_database_refuses_a_profile_with_no_ucm_renderer(session, project):
    declaration = _declaration(session, project)
    registered_ids = effective_baseline_formats(session, project.id)

    _writer_refused(
        session,
        "ucm_renderer_identity",
        "insert into project_issue_profiles ("
        "project_id, profile_identity, profile_version, content_sha256,"
        " declaration, declaration_schema_version, effective_from,"
        " ucm_renderer_version,"
        " output_template_format_id, field_mapping_format_id,"
        " registered_by_principal, idempotency_key) values ("
        ":project, 'customer-issue', 1, :digest, :declaration,"
        " 'issue-profile-v1', :effective, 'v1', :template,"
        " :mapping, 'local:coordinator', 'raw')",
        project=project.id,
        digest=declaration.content_sha256,
        declaration=declaration.declaration_json,
        effective=JANUARY,
        template=registered_ids["output_template"].id,
        mapping=registered_ids["field_mapping"].id,
    )


def test_the_database_refuses_a_blank_profile_identity(session, project):
    declaration = _declaration(session, project)
    registered_ids = effective_baseline_formats(session, project.id)

    _writer_refused(
        session,
        "ck_project_issue_profiles_identity",
        "insert into project_issue_profiles ("
        "project_id, profile_identity, profile_version, content_sha256,"
        " declaration, declaration_schema_version, effective_from,"
        " ucm_renderer_identity, ucm_renderer_version,"
        " output_template_format_id, field_mapping_format_id,"
        " registered_by_principal, idempotency_key) values ("
        ":project, '   ', 1, :digest, :declaration,"
        " 'issue-profile-v1', :effective, 'r', 'v1', :template,"
        " :mapping, 'local:coordinator', 'raw')",
        project=project.id,
        digest=declaration.content_sha256,
        declaration=declaration.declaration_json,
        effective=JANUARY,
        template=registered_ids["output_template"].id,
        mapping=registered_ids["field_mapping"].id,
    )


def test_the_database_refuses_a_blank_idempotency_key(session, project):
    declaration = _declaration(session, project)
    registered_ids = effective_baseline_formats(session, project.id)

    _writer_refused(
        session,
        "ck_project_issue_profiles_key",
        "insert into project_issue_profiles ("
        "project_id, profile_identity, profile_version, content_sha256,"
        " declaration, declaration_schema_version, effective_from,"
        " ucm_renderer_identity, ucm_renderer_version,"
        " output_template_format_id, field_mapping_format_id,"
        " registered_by_principal, idempotency_key) values ("
        ":project, 'customer-issue', 1, :digest, :declaration,"
        " 'issue-profile-v1', :effective, 'r', 'v1', :template,"
        " :mapping, 'local:coordinator', '  ')",
        project=project.id,
        digest=declaration.content_sha256,
        declaration=declaration.declaration_json,
        effective=JANUARY,
        template=registered_ids["output_template"].id,
        mapping=registered_ids["field_mapping"].id,
    )


def test_the_database_refuses_a_backdated_successor(session, project):
    """The command refuses it too; this is the floor under the command."""

    first = _register(session, project, _declaration(session, project))
    declaration = _declaration(session, project)
    registered_ids = effective_baseline_formats(session, project.id)

    _writer_refused(
        session,
        "ck_project_issue_profiles_succession",
        "insert into project_issue_profiles ("
        "project_id, profile_identity, profile_version, content_sha256,"
        " declaration, declaration_schema_version, effective_from,"
        " ucm_renderer_identity, ucm_renderer_version,"
        " output_template_format_id, field_mapping_format_id,"
        " supersedes_id, supersedes_version, supersedes_effective_from,"
        " registered_by_principal, idempotency_key) values ("
        ":project, 'customer-issue', 2, :digest, :declaration,"
        " 'issue-profile-v1', :effective, 'r', 'v1', :template,"
        " :mapping, :prior, 1, :prior_effective,"
        " 'local:coordinator', 'raw')",
        project=project.id,
        digest=declaration.content_sha256,
        declaration=declaration.declaration_json,
        effective=JANUARY,
        template=registered_ids["output_template"].id,
        mapping=registered_ids["field_mapping"].id,
        prior=first.id,
        prior_effective=JANUARY,
    )


# --- Staleness -------------------------------------------------------------


def test_a_candidate_prepared_against_the_effective_profile_stands(session, project):
    registered = _register(session, project, _declaration(session, project))
    inventory = effective_issue_inventory(session, project.id, FEBRUARY)

    assert not prepared_candidate_is_stale(
        inventory, profile_id=registered.id, profile_version=1
    )


def test_a_profile_change_makes_an_existing_candidate_stale(session, project):
    """It never edits the candidate: the candidate keeps claiming what it claimed."""

    first = _register(session, project, _declaration(session, project), key="v1")
    _register(
        session,
        project,
        _declaration(
            session,
            project,
            artifacts=[ArtifactEntry("chase_list", CHASE_RENDERER)],
        ),
        effective_from=MARCH,
        key="v2",
    )

    bound = {"profile_id": first.id, "profile_version": 1}

    assert not prepared_candidate_is_stale(
        effective_issue_inventory(session, project.id, FEBRUARY), **bound
    )
    assert prepared_candidate_is_stale(
        effective_issue_inventory(session, project.id, APRIL), **bound
    )


def test_a_candidate_is_stale_where_nothing_is_configured(session, project):
    assert prepared_candidate_is_stale(None, profile_id=1, profile_version=1)
