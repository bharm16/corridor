"""One immutable release candidate, prepared from one coherent reading (#529).

Every instant here is declared. Nothing in this file reads a clock: a candidate
is bound to a source cutoff and a preparation instant its caller supplies, and a
test that asked the machine what time it was could not prove that the same
inputs prepare the same candidate tomorrow.

The tests are grouped the way the ticket's rules are: what the configured set
contains, what the identity binds, how the three outcomes stay apart, what the
two transactions revalidate, and where the predecessor may and may not come
from.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from corridor.access import COORDINATION, EXTERNAL_RELEASE, enroll_member
from corridor.analytics import AnalyticsBinding, EventFamily, capture_events
from corridor.config import settings
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
    CoverageRequirement,
    DecisionBlockingPolicy,
    RendererRevision,
    UPDATED_UCM,
)
from corridor.issue_rendering import (
    NO_PRIOR_COMPARISON_STATEMENT,
    SECTION_COMMITMENTS,
    SECTION_CONSTRAINT_ALERTS,
    SECTION_FOLLOW_UP_PLANS,
    SECTION_KEY_DATES,
    SECTION_PENDING_COORDINATION,
    ReportSection,
    SourceCoverage,
    TemplateBinding,
)
from corridor.models import (
    BLOCKED,
    READY,
    READY_WITH_EXCEPTIONS,
    Document,
    Project,
    ReleaseCandidate,
    ReleaseCandidateArtifact,
    ReleasePackage,
    ReleasePreparationRefusal,
)
from corridor.object_storage import LocalFilesystemStore
from corridor.principals import HumanPrincipal
from corridor.release_authorization import authorize_release_package
from corridor.release_candidate import (
    ARTIFACT_INVOKERS,
    ARTIFACT_MISSING,
    CANDIDATE_IDENTITY_CONFLICT,
    INPUTS_CHANGED_WHILE_RENDERING,
    MIXED_READING,
    RENDERER_FAILED,
    UNSUPPORTED_ISSUE_CONFIGURATION,
    BoundPreparation,
    CoverageDeclaration,
    PreparationRefused,
    RenderInputs,
    attach_candidate,
    authorization_blockers,
    bind_preparation,
    candidate_artifacts,
    candidate_is_stale,
    content_declaration,
    emit_preparation,
    latest_authorized_package,
    render_candidate_artifacts,
)
from corridor.report_preparation import AUTHORIZED_PACKAGE_COMPARISON

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

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 3, 2, 7, 0, tzinfo=timezone.utc)
AUTHORIZED_AT = datetime(2026, 2, 2, 9, 0, tzinfo=timezone.utc)

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

COVERAGE_LINES = (
    SourceCoverage(
        source_name="Weekly utility conflict matrix",
        requirement="required",
        state="read",
        detail="the 2026-03-01 revision was read in full",
    ),
)

BINDING = AnalyticsBinding(
    code_revision="git:529",
    product_revision="2026.03",
    packetizer_rules_version="delta-partition-v2",
    enabled_feature_flags=("release_candidate",),
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return LocalFilesystemStore(tmp_path / "artifacts")


@pytest.fixture
def adopted(session, tmp_path, store):
    """One adopted project, its accepted revision, and its template bytes."""

    row = Project(
        slug=f"release-candidate-{uuid4().hex[:8]}",
        name="Release Candidate",
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
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, row, body, tmp_path)
    return _Adopted(project=row, revision_id=revision_id, template_bytes=body)


class _Adopted:
    def __init__(self, project, revision_id, template_bytes):
        self.project = project
        self.revision_id = revision_id
        self.template_bytes = template_bytes


def _preparation(project_id: int, revision_id: int, **overrides) -> dict:
    """One ``report_preparation`` reading, in the shape that pass returns."""

    body = {
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
    body.update(overrides)
    return body


def _configure(
    session,
    adopted,
    *,
    artifacts=(),
    coverage=(),
    policies=(),
    ucm=None,
    effective_from=JANUARY,
):
    return configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=effective_from,
        ucm=ucm or UCM_RENDERER,
        artifacts=tuple(artifacts),
        coverage=tuple(coverage),
        policies=tuple(policies),
    )


def _declare(session, adopted, *, lines=COVERAGE_LINES, variant="week"):
    """One confirmed coverage declaration this project may be prepared under.

    #675 made the declaration a persisted row a candidate names by foreign key
    rather than a value handed to ``bind_preparation``, so every preparation
    below starts from a confirmation somebody made.
    """

    return declare_coverage(
        session,
        adopted.project,
        cutoff=CUTOFF,
        principal=COORDINATOR,
        confirmed_at=PREPARED_AT,
        lines=lines,
        variant=variant,
    )


def _bind(session, adopted, **overrides):
    if "coverage_declaration_id" not in overrides:
        overrides["coverage_declaration_id"] = _declare(session, adopted).id
    arguments = {
        "project_id": adopted.project.id,
        "preparation": _preparation(adopted.project.id, adopted.revision_id),
        "source_cutoff": CUTOFF,
        "prepared_at": PREPARED_AT,
        "templates": TEMPLATE,
        "first_issue_behavior": NO_PRIOR_COMPARISON_STATEMENT,
        "template_bytes": adopted.template_bytes,
        "binding": BINDING,
    }
    arguments.update(overrides)
    return bind_preparation(session, **arguments)


def _prepare(session, adopted, store, **overrides):
    """Bind, render, and attach in one rollback-scoped session.

    The orchestrator's three real transactions are exercised separately; this
    is the seam-level path every content assertion below stands on.
    """

    bound = _bind(session, adopted, **overrides)
    rendered = render_candidate_artifacts(
        bound, template_bytes=adopted.template_bytes, session=session, store=store
    )
    candidate = attach_candidate(
        session,
        bound,
        rendered,
        prepared_by=COORDINATOR,
        preparation=_preparation(adopted.project.id, adopted.revision_id),
        templates=TEMPLATE,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
        template_bytes=adopted.template_bytes,
    )
    return bound, rendered, candidate


def _candidates(session, adopted):
    return tuple(
        session.scalars(
            select(ReleaseCandidate).where(
                ReleaseCandidate.project_id == adopted.project.id
            )
        ).all()
    )


def _artifact_rows(session, adopted):
    return tuple(
        session.scalars(
            select(ReleaseCandidateArtifact).where(
                ReleaseCandidateArtifact.project_id == adopted.project.id
            )
        ).all()
    )


def _refusals(session, adopted):
    return tuple(
        session.scalars(
            select(ReleasePreparationRefusal).where(
                ReleasePreparationRefusal.project_id == adopted.project.id
            )
        ).all()
    )


# --- what the configured set contains --------------------------------------


def test_a_ucm_only_profile_prepares_a_candidate_of_exactly_one_artifact(
    session, adopted, store
):
    """ADR-0091's one-artifact package is a package, and its UCM is a column."""

    _configure(session, adopted)

    bound, rendered, candidate = _prepare(session, adopted, store)

    assert bound.artifact_types == (UPDATED_UCM,)
    assert [one.artifact_type for one in rendered] == [UPDATED_UCM]
    assert candidate.ucm_renderer_identity == UCM_RENDERER_IDENTITY
    assert candidate.ucm_content_sha256 == rendered[0].sha256
    assert candidate_artifacts(session, candidate) == ()


def test_a_configured_weekly_report_joins_the_mandatory_ucm(session, adopted, store):
    _configure(session, adopted, artifacts=[WEEKLY])

    bound, rendered, candidate = _prepare(session, adopted, store)

    assert bound.artifact_types == (UPDATED_UCM, "weekly_coordination_report")
    rows = candidate_artifacts(session, candidate)
    assert [row.artifact_type for row in rows] == ["weekly_coordination_report"]
    assert rows[0].renderer_identity == WEEKLY_REPORT_IDENTITY


@pytest.mark.parametrize("chase_version", ["v1", "v2"])
def test_a_configured_chase_list_and_summary_join_the_mandatory_ucm(
    session, adopted, store, chase_version
):
    chase = ArtifactEntry("chase_list", RendererRevision(CHASE_LIST_IDENTITY, chase_version))
    _configure(session, adopted, artifacts=[chase, SUMMARY])

    bound, rendered, candidate = _prepare(session, adopted, store)
    artifact = next(artifact for artifact in rendered if artifact.artifact_type == "chase_list")
    assert artifact.renderer_version == chase_version
    assert json.loads(artifact.content)["rule_version"] == chase_version

    assert bound.artifact_types == (
        UPDATED_UCM,
        "accepted_change_summary",
        "chase_list",
    )
    assert [row.artifact_type for row in candidate_artifacts(session, candidate)] == [
        "accepted_change_summary",
        "chase_list",
    ]


def test_a_complete_multi_artifact_profile_prepares_every_configured_artifact(
    session, adopted, store
):
    _configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])

    bound, rendered, candidate = _prepare(session, adopted, store)

    assert bound.artifact_types == (
        UPDATED_UCM,
        "accepted_change_summary",
        "chase_list",
        "weekly_coordination_report",
    )
    assert len(rendered) == 4
    assert len({one.sha256 for one in rendered}) == 4
    assert len(candidate_artifacts(session, candidate)) == 3


def test_a_configured_renderer_this_release_cannot_invoke_refuses_preparation(
    session, adopted, store
):
    """An unregistered renderer is a configuration problem, never a fallback.

    A provenance sidecar is a configurable artifact type with no registered
    renderer contract at any version, so #641 reports it unsupported and this
    refuses rather than rendering it with "probably the current renderer".
    """

    _configure(
        session,
        adopted,
        artifacts=[
            ArtifactEntry(
                "provenance_sidecar", RendererRevision("sidecar", "v1")
            )
        ],
    )

    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted)

    assert refused.value.code == UNSUPPORTED_ISSUE_CONFIGURATION
    assert "not a renderer revision this release supports" in refused.value.sentence
    assert _candidates(session, adopted) == ()


def test_a_renderer_version_this_release_does_not_register_refuses_preparation(
    session, adopted, store
):
    """A version is not a decoration on an identity (#641)."""

    _configure(
        session,
        adopted,
        artifacts=[
            ArtifactEntry(
                "weekly_coordination_report",
                RendererRevision(WEEKLY_REPORT_IDENTITY, "v9"),
            )
        ],
    )

    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted)

    assert refused.value.code == UNSUPPORTED_ISSUE_CONFIGURATION



def test_the_follow_up_plans_parameter_refuses_a_shape_that_is_not_a_plan(
    session, adopted, store
):
    """A real type at the boundary, and a refusal rather than an empty section.

    ``follow_up_plans`` was ``Sequence[Any]`` here and in
    ``prepare_release_candidate``, and the renderer's own follow-up type shared
    not one field name with the reading the records produce. Nothing converted
    between them and nothing objected, so "Our next steps" was structurally
    empty in every production path (#425). The parameter now names the one
    reading type, and the wrong shape is a refusal.
    """

    _configure(session, adopted)

    class _Twin:
        plan_identity = "plan-88"
        subject_identity = "UC-1"
        assigned_to = "Dana Reyes"
        next_action = "Ask City Water to confirm the relocation date"

    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted, follow_up_plans=(_Twin(),))

    assert refused.value.code == MIXED_READING
    assert "retained records" in refused.value.sentence
    assert _candidates(session, adopted) == ()


# --- what the candidate identity binds -------------------------------------


# Every input ADR-0086 shares across one issue, as the key it appears under in
# the digested declaration. The list is spelled here rather than derived from
# the payload, because a test that read the payload back would pass whatever
# the payload happened to contain.
DECLARED_INPUTS = {
    "schema_version",
    "content_contract_version",
    "project_id",
    "accepted_revision_id",
    "previous_authorized_package",
    "source_cutoff",
    "coverage",
    "issue_profile",
    "output_template",
    "field_mapping",
    "configured_artifact_types",
    "renderers",
    "code_revision",
    "product_revision",
    "enabled_feature_flags",
    "derived_state",
    # #690: which retained report-preparation reading this candidate measured,
    # by receipt identity and result digest. The counts alone could not say
    # which receipt produced them, and the next issue's floor is read off this.
    "report_preparation",
}


def test_the_input_declaration_binds_every_input_one_issue_shares(
    session, adopted, store
):
    _configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])

    bound = _bind(session, adopted)
    payload = bound.as_input_payload()

    assert set(payload) == DECLARED_INPUTS
    assert payload["accepted_revision_id"] == adopted.revision_id
    # ADR-0086's explicit none, spelled rather than omitted.
    assert payload["previous_authorized_package"] is None
    assert payload["coverage"]["content_sha256"] == bound.coverage.content_sha256
    # #675: the confirmed declaration is bound by identity, not only by what
    # it said, so two confirmations of the same lines are two candidates.
    assert payload["coverage"]["declaration_id"] == bound.coverage.declaration_id
    assert payload["issue_profile"]["version"] == 1
    assert payload["configured_artifact_types"] == list(bound.artifact_types)
    assert {entry["renderer_version"] for entry in payload["renderers"]} == {
        UCM_RENDERER_VERSION,
        CHANGE_SUMMARY_VERSION,
        CHASE_LIST_VERSION,
        WEEKLY_REPORT_VERSION,
    }
    assert payload["code_revision"] == BINDING.code_revision
    assert payload["product_revision"] == BINDING.product_revision
    assert payload["enabled_feature_flags"] == ["release_candidate"]


def test_a_changed_value_in_any_bound_input_produces_a_different_candidate(
    session, adopted, store
):
    """Each of the 17 input mutations changes one real bound declaration.

    Binding requires one adoption. The mutations are independent dictionary
    operations, so each starts from a fresh copy without adopting again.
    """

    mutations = (
        ("accepted_revision", lambda p: p.update(accepted_revision_id=p["accepted_revision_id"] + 1)),
        ("previous_package", lambda p: p.update(previous_authorized_package={"package_id": 7})),
        ("source_cutoff", lambda p: p.update(source_cutoff="2026-03-09T06:00:00+00:00")),
        ("coverage", lambda p: p["coverage"].update(content_sha256="0" * 64)),
        ("issue_profile_version", lambda p: p["issue_profile"].update(version=2)),
        ("issue_profile_digest", lambda p: p["issue_profile"].update(content_sha256="0" * 64)),
        ("output_template", lambda p: p["output_template"].update(content_sha256="0" * 64)),
        ("field_mapping", lambda p: p["field_mapping"].update(content_sha256="0" * 64)),
        ("artifact_types", lambda p: p["configured_artifact_types"].append("chase_list")),
        ("renderer_version", lambda p: p["renderers"][0].update(renderer_version="v99")),
        ("code_revision", lambda p: p.update(code_revision="git:other")),
        ("product_revision", lambda p: p.update(product_revision="2027.01")),
        ("feature_flags", lambda p: p["enabled_feature_flags"].append("another")),
        ("coverage_state", lambda p: p["derived_state"].update(unread_source_count=1)),
        ("unmet_coverage", lambda p: p["derived_state"]["unmet_coverage"].append("unmet")),
        ("blocked_decision", lambda p: p["derived_state"]["blocked_decisions"].append(
            {"delta_id": 1, "policy": "resolve_before_issue:v1", "selector": "x"}
        )),
        ("frozen_reading", lambda p: p["derived_state"].update(reading_identity="0" * 64)),
    )

    _configure(session, adopted, artifacts=[WEEKLY])
    bound = _bind(session, adopted)
    original = bound.candidate_identity

    original_payload = bound.as_input_payload()
    for name, mutate in mutations:
        payload = deepcopy(original_payload)
        mutate(payload)
        changed = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        assert changed != original, f"mutating {name} did not change candidate identity"


def test_the_content_digest_additionally_binds_the_ordered_artifact_set(
    session, adopted, store
):
    """Two questions, two answers: same inputs, and same issue."""

    _configure(session, adopted, artifacts=[WEEKLY])
    bound, rendered, candidate = _prepare(session, adopted, store)

    declared = json.loads(candidate.content_declaration)
    assert declared["candidate_identity"] == candidate.candidate_identity
    assert [entry["artifact_type"] for entry in declared["artifacts"]] == [
        UPDATED_UCM,
        "weekly_coordination_report",
    ]
    assert [entry["content_sha256"] for entry in declared["artifacts"]] == [
        one.sha256 for one in rendered
    ]
    assert [entry["position"] for entry in declared["artifacts"]] == [1, 2]

    # The same inputs with different artifact bytes are the same candidate
    # identity and a different content digest.
    swapped = tuple(reversed(rendered))
    assert (
        sha256(content_declaration(bound, swapped).encode("utf-8")).hexdigest()
        != candidate.content_sha256
    )


def test_the_profile_term_is_not_the_whole_staleness_contract(
    session, adopted, store
):
    """``prepared_candidate_is_stale`` answers the profile part and no more.

    The accepted record moving after preparation makes a candidate stale with
    the profile untouched, which is exactly the case a contract defined by the
    profile helper alone would call fresh.
    """

    _configure(session, adopted)
    _, _, candidate = _prepare(session, adopted, store)
    assert candidate_is_stale(session, candidate, as_of=CUTOFF) == ()

    session.execute(text("set local role corridor_fact_decision_writer"))
    session.execute(
        text(
            "insert into project_record_revisions (project_id, command_type, "
            "human_principal, idempotency_key) values (:project, 'test', "
            "'local:coordinator', :key)"
        ),
        {"project": adopted.project.id, "key": f"later-{uuid4().hex[:8]}"},
    )
    session.execute(text("reset role"))

    reasons = candidate_is_stale(session, candidate, as_of=CUTOFF)
    assert any("accepted record moved" in reason for reason in reasons)
    assert authorization_blockers(session, candidate, as_of=CUTOFF) == reasons


# --- the mandatory UCM stays structural ------------------------------------


def test_the_database_refuses_an_updated_ucm_as_an_optional_artifact_row(
    session, adopted, store
):
    """#640's structural rule, carried onto the candidate (ADR-0091)."""

    _configure(session, adopted)
    _, rendered, candidate = _prepare(session, adopted, store)

    with pytest.raises(DBAPIError, match="ck_release_candidate_artifacts_type"):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into release_candidate_artifacts (candidate_id, "
                    "project_id, artifact_type, renderer_identity, "
                    "renderer_version, content_sha256, storage_key, byte_count, "
                    "position) values (:candidate, :project, 'updated_ucm', "
                    "'r', 'v1', :digest, 'aa/bb.xlsx', 10, 2)"
                ),
                {
                    "candidate": candidate.id,
                    "project": adopted.project.id,
                    "digest": rendered[0].sha256,
                },
            )


def test_the_database_refuses_a_second_row_of_one_artifact_type(
    session, adopted, store
):
    _configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = _prepare(session, adopted, store)

    with pytest.raises(DBAPIError, match="uq_release_candidate_artifacts_type"):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into release_candidate_artifacts (candidate_id, "
                    "project_id, artifact_type, renderer_identity, "
                    "renderer_version, content_sha256, storage_key, byte_count, "
                    "position) values (:candidate, :project, "
                    "'weekly_coordination_report', 'r', 'v1', :digest, "
                    "'aa/bb.txt', 10, 9)"
                ),
                {
                    "candidate": candidate.id,
                    "project": adopted.project.id,
                    "digest": "b" * 64,
                },
            )


def test_the_database_refuses_an_artifact_that_belongs_to_another_project(
    session, adopted, store, tmp_path
):
    """The composite key carries the project, so a cross-project set is unrepresentable."""

    _configure(session, adopted)
    _, _, candidate = _prepare(session, adopted, store)
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(
        DBAPIError, match="fk_release_candidate_artifacts_candidate"
    ):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into release_candidate_artifacts (candidate_id, "
                    "project_id, artifact_type, renderer_identity, "
                    "renderer_version, content_sha256, storage_key, byte_count, "
                    "position) values (:candidate, :project, 'chase_list', "
                    "'r', 'v1', :digest, 'aa/bb.json', 10, 2)"
                ),
                {
                    "candidate": candidate.id,
                    "project": other.id,
                    "digest": "c" * 64,
                },
            )


def test_nothing_changes_a_prepared_candidate_in_place(session, adopted, store):
    """ADR-0086's sealed-set immutability, as a database invariant."""

    _configure(session, adopted)
    _, _, candidate = _prepare(session, adopted, store)

    for statement in (
        "update release_candidates set readiness = 'ready' where id = :id",
        "delete from release_candidates where id = :id",
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            with session.begin_nested():
                session.execute(text(statement), {"id": candidate.id})


# --- three outcomes, kept apart --------------------------------------------


def _unread_source(session, adopted, name="late-matrix.xlsx"):
    """One delivered source that could not be read, so coverage is incomplete."""

    document = Document(
        project_id=adopted.project.id,
        sha256=sha256(f"{adopted.project.slug}:{name}".encode()).hexdigest(),
        filename=name,
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="failed",
    )
    session.add(document)
    session.flush()
    return document


REQUIRED_COVERAGE = CoverageRequirement(
    requirement="all_required_sources_read:v1",
    statement="Every delivered source is read before this project issues.",
)


def test_a_complete_but_blocked_candidate_exists_and_cannot_be_authorized(
    session, adopted, store
):
    """ADR-0086's coverage gate: complete, immutable, and not authorizable.

    Every configured artifact is rendered and retained — being blocked is not
    being incomplete — and the only way past it is a newly prepared candidate,
    because the coverage state that blocks it is bound into this one's own
    identity.
    """

    _configure(session, adopted, artifacts=[WEEKLY], coverage=[REQUIRED_COVERAGE])
    _unread_source(session, adopted)

    bound, rendered, candidate = _prepare(session, adopted, store)

    assert bound.readiness == BLOCKED
    assert candidate.readiness == BLOCKED
    assert bound.blockers == (REQUIRED_COVERAGE.statement,)
    assert len(rendered) == 2
    assert len(candidate_artifacts(session, candidate)) == 1
    assert store.exists(candidate.ucm_storage_key)

    blockers = authorization_blockers(session, candidate, as_of=CUTOFF)
    assert blockers
    assert "newly prepared candidate" in blockers[0]


def test_clearing_a_blocker_needs_a_newly_prepared_candidate(
    session, adopted, store
):
    """The blocked candidate keeps claiming what it claimed; a new one is made."""

    _configure(session, adopted, coverage=[REQUIRED_COVERAGE])
    unread = _unread_source(session, adopted)
    _, _, blocked = _prepare(session, adopted, store)
    assert blocked.readiness == BLOCKED

    unread.parse_status = "parsed"
    session.flush()
    bound, _, cleared = _prepare(session, adopted, store)

    assert bound.blockers == ()
    assert bound.readiness != BLOCKED
    # A different candidate, because the coverage state that blocked the first
    # one is bound into its identity: the blocker cannot be cleared in place.
    assert cleared.id != blocked.id
    assert cleared.candidate_identity != blocked.candidate_identity
    # And the blocked candidate is preserved exactly as it was prepared.
    session.refresh(blocked)
    assert blocked.readiness == BLOCKED
    assert authorization_blockers(session, cleared, as_of=CUTOFF) == ()


def test_a_candidate_with_disclosed_exceptions_is_prepared_and_authorizable(
    session, adopted, store
):
    """ADR-0086: honest adverse project conditions never block release."""

    _configure(session, adopted, artifacts=[WEEKLY])
    late = _declare(
        session,
        adopted,
        variant="weekly-coverage-late",
        lines=COVERAGE_LINES
        + (
            SourceCoverage(
                source_name="Utility owner email",
                requirement="optional",
                state="late",
                detail="it arrived after the cutoff and is excluded",
            ),
        ),
    )

    bound, _, candidate = _prepare(
        session, adopted, store, coverage_declaration_id=late.id
    )

    assert bound.readiness == READY_WITH_EXCEPTIONS
    assert candidate.readiness == READY_WITH_EXCEPTIONS
    assert bound.blockers == ()
    assert any(
        "Utility owner email is late" in sentence
        for sentence in bound.disclosed_exceptions
    )
    assert authorization_blockers(session, candidate, as_of=CUTOFF) == ()


def test_a_failed_render_leaves_no_candidate_and_no_partial_artifact_set(
    session, adopted, store, monkeypatch
):
    """A renderer that crashes leaves nothing usable behind, and one receipt.

    The weekly report is configured after the mandatory UCM, so the UCM has
    already rendered and been retained when the failure happens: this is
    exactly the case where a partial set could survive.
    """

    _configure(session, adopted, artifacts=[WEEKLY])
    bound = _bind(session, adopted)

    def explode(inputs):
        raise RuntimeError("the weekly renderer fell over")

    monkeypatch.setitem(
        ARTIFACT_INVOKERS,
        ("weekly_coordination_report", WEEKLY_REPORT_IDENTITY, WEEKLY_REPORT_VERSION),
        explode,
    )

    with pytest.raises(PreparationRefused) as refused:
        render_candidate_artifacts(
            bound,
            template_bytes=adopted.template_bytes,
            session=session,
            store=store,
        )

    assert refused.value.code == RENDERER_FAILED
    assert "the weekly renderer fell over" in refused.value.sentence
    assert _candidates(session, adopted) == ()
    assert _artifact_rows(session, adopted) == ()


def test_an_artifact_that_renders_no_bytes_is_a_failed_preparation(
    session, adopted, store, monkeypatch
):
    _configure(session, adopted, artifacts=[CHASE])
    bound = _bind(session, adopted)
    monkeypatch.setitem(
        ARTIFACT_INVOKERS,
        ("chase_list", CHASE_LIST_IDENTITY, CHASE_LIST_VERSION),
        lambda inputs: b"",
    )

    with pytest.raises(PreparationRefused) as refused:
        render_candidate_artifacts(
            bound,
            template_bytes=adopted.template_bytes,
            session=session,
            store=store,
        )

    assert refused.value.code == ARTIFACT_MISSING
    assert _candidates(session, adopted) == ()


def test_a_refused_preparation_records_one_bounded_reason(session, adopted, store):
    from corridor.release_candidate import record_refused_preparation

    _configure(session, adopted)
    receipt = record_refused_preparation(
        session,
        project_id=adopted.project.id,
        refusal=PreparationRefused(RENDERER_FAILED, "the renderer fell over"),
        refused_by=COORDINATOR,
        refused_at=PREPARED_AT,
        candidate_identity="d" * 64,
    )

    assert receipt.reason_code == RENDERER_FAILED
    assert _refusals(session, adopted) == (receipt,)

    with pytest.raises(DBAPIError, match="ck_release_preparation_refusals_code"):
        with session.begin_nested():
            session.execute(
                text(
                    "insert into release_preparation_refusals (project_id, "
                    "reason_code, reason, refused_by_principal, refused_at) "
                    "values (:project, 'it just did not work', 'because', "
                    "'local:coordinator', :at)"
                ),
                {"project": adopted.project.id, "at": PREPARED_AT},
            )


# --- the two transactions ---------------------------------------------------


def test_a_bound_input_that_changed_while_rendering_attaches_no_candidate(
    session, adopted, store
):
    """The second transaction re-derives the whole bound state and compares it."""

    _configure(session, adopted, artifacts=[WEEKLY], coverage=[REQUIRED_COVERAGE])
    bound = _bind(session, adopted)
    rendered = render_candidate_artifacts(
        bound, template_bytes=adopted.template_bytes, session=session, store=store
    )

    # A source is delivered and cannot be read while the artifacts are being
    # produced, so the coverage this candidate was bound to no longer holds.
    _unread_source(session, adopted)

    with pytest.raises(PreparationRefused) as refused:
        attach_candidate(
            session,
            bound,
            rendered,
            prepared_by=COORDINATOR,
            preparation=_preparation(adopted.project.id, adopted.revision_id),
            templates=TEMPLATE,
            first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
            template_bytes=adopted.template_bytes,
        )

    assert refused.value.code == INPUTS_CHANGED_WHILE_RENDERING
    assert _candidates(session, adopted) == ()
    assert _artifact_rows(session, adopted) == ()


def test_an_accepted_record_that_moved_while_rendering_attaches_no_candidate(
    session, adopted, store
):
    _configure(session, adopted)
    bound = _bind(session, adopted)
    rendered = render_candidate_artifacts(
        bound, template_bytes=adopted.template_bytes, session=session, store=store
    )

    session.execute(text("set local role corridor_fact_decision_writer"))
    session.execute(
        text(
            "insert into project_record_revisions (project_id, command_type, "
            "human_principal, idempotency_key) values (:project, 'test', "
            "'local:coordinator', :key)"
        ),
        {"project": adopted.project.id, "key": f"moved-{uuid4().hex[:8]}"},
    )
    session.execute(text("reset role"))

    with pytest.raises(PreparationRefused) as refused:
        attach_candidate(
            session,
            bound,
            rendered,
            prepared_by=COORDINATOR,
            preparation=_preparation(adopted.project.id, adopted.revision_id),
            templates=TEMPLATE,
            first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
            template_bytes=adopted.template_bytes,
        )

    assert refused.value.code == INPUTS_CHANGED_WHILE_RENDERING
    assert _candidates(session, adopted) == ()


def test_the_three_derived_artifacts_render_with_no_session_at_all(
    session, adopted, store
):
    """Proof that no transaction is held open across their rendering.

    They are pure over the bound readings: given ``session=None`` they produce
    byte-identical output, so there is nothing for a transaction to be open
    across. Only the updated UCM needs a session, because #495's renderer reads
    and renders in one call.
    """

    _configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    bound = _bind(session, adopted)

    for key, invoker in ARTIFACT_INVOKERS.items():
        artifact_type = key[0]
        if artifact_type == UPDATED_UCM:
            continue
        with_session = invoker(
            RenderInputs(
                bound=bound, template_bytes=adopted.template_bytes, session=session
            )
        )
        without = invoker(
            RenderInputs(
                bound=bound, template_bytes=adopted.template_bytes, session=None
            )
        )
        assert with_session == without, artifact_type
        assert without


def test_rendering_refuses_template_bytes_the_binding_never_saw(
    session, adopted, store
):
    _configure(session, adopted)
    bound = _bind(session, adopted)

    with pytest.raises(PreparationRefused, match="not the template bytes"):
        render_candidate_artifacts(
            bound, template_bytes=b"different", session=session, store=store
        )


# --- the predecessor --------------------------------------------------------


def _authorize_package(session, adopted, candidate, store, *, at=AUTHORIZED_AT):
    """One authorized package, through #533's real act.

    Written directly until #533 landed, which the receipt's columns no longer
    permit: a package binds the candidate it sealed, and there is no such thing
    as a receipt with a name and nothing behind it.
    """

    enroll_member(
        session,
        project_id=adopted.project.id,
        email="releaser@example.test",
        principal=RELEASER,
        display_name="Releaser",
        designations=[EXTERNAL_RELEASE],
        operator=OPERATOR,
    )
    return authorize_release_package(
        session,
        project_id=adopted.project.id,
        candidate_id=candidate.id,
        releaser=RELEASER,
        authorized_at=at,
        store=store,
        binding=BINDING,
    )


def test_the_predecessor_is_explicitly_absent_before_the_first_package(
    session, adopted, store
):
    """Not the latest render, not the latest candidate, not the baseline.

    The project here has an adopted baseline, an accepted revision, delivered
    documents, and a prepared candidate with its own preparation instant —
    every timestamp a predecessor could have been derived from. There is still
    no authorized package, so there is no predecessor.
    """

    _configure(session, adopted)
    _, _, first = _prepare(session, adopted, store)

    assert latest_authorized_package(session, adopted.project.id) is None
    assert first.previous_package_id is None
    assert json.loads(first.input_declaration)["previous_authorized_package"] is None

    # A second, later-prepared candidate is still not a predecessor.
    later = _bind(
        session,
        adopted,
        coverage_declaration_id=_declare(session, adopted, variant="second-week").id,
    )
    assert later.previous_package_id is None


def test_the_predecessor_is_the_authorized_package_once_one_exists(
    session, adopted, store
):
    _configure(session, adopted)
    _, _, first = _prepare(session, adopted, store)
    package = _authorize_package(session, adopted, first, store)

    bound = _bind(
        session,
        adopted,
        coverage_declaration_id=_declare(session, adopted, variant="second-week").id,
    )

    assert bound.previous_package_id == package.package_id
    assert bound.reading.previous_issue is not None
    assert bound.reading.previous_issue.issue_identity == package.package_identity
    # And the candidate prepared before it was authorized is now stale.
    assert any(
        "package was authorized" in reason
        for reason in candidate_is_stale(session, first, as_of=CUTOFF)
    )


# --- retained bytes, and convergence ---------------------------------------


def test_the_exact_bytes_and_digests_are_retained_through_the_storage_interface(
    session, adopted, store
):
    """#487's store holds every artifact, keyed by its own digest."""

    _configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    _, rendered, candidate = _prepare(session, adopted, store)

    assert store.get(candidate.ucm_storage_key, sha256=candidate.ucm_content_sha256)
    for row, produced in zip(
        candidate_artifacts(session, candidate),
        [one for one in rendered if one.artifact_type != UPDATED_UCM],
    ):
        held = store.get(row.storage_key, sha256=row.content_sha256)
        assert held == produced.content
        assert sha256(held).hexdigest() == row.content_sha256
        assert row.byte_count == len(held)


def test_a_repeated_preparation_converges_on_the_same_candidate(
    session, adopted, store
):
    """The same bound inputs are the same issue, written once."""

    _configure(session, adopted, artifacts=[WEEKLY])
    _, first_rendered, first = _prepare(session, adopted, store)
    _, second_rendered, second = _prepare(session, adopted, store)

    assert second.id == first.id
    assert second.candidate_identity == first.candidate_identity
    assert second.content_sha256 == first.content_sha256
    assert [one.sha256 for one in second_rendered] == [
        one.sha256 for one in first_rendered
    ]
    assert len(_candidates(session, adopted)) == 1
    assert len(_artifact_rows(session, adopted)) == 1


def test_a_repeated_preparation_with_different_bytes_overwrites_nothing(
    session, adopted, store
):
    """Same identity, different content: refused, and the first row stands."""

    _configure(session, adopted)
    bound, rendered, first = _prepare(session, adopted, store)
    original = first.content_sha256

    divergent = (replace(rendered[0], sha256=sha256(b"other").hexdigest()),)
    with pytest.raises(PreparationRefused) as refused:
        attach_candidate(
            session,
            bound,
            divergent,
            prepared_by=COORDINATOR,
            preparation=_preparation(adopted.project.id, adopted.revision_id),
            templates=TEMPLATE,
            first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
            template_bytes=adopted.template_bytes,
        )

    assert refused.value.code == CANDIDATE_IDENTITY_CONFLICT
    session.refresh(first)
    assert first.content_sha256 == original
    assert len(_candidates(session, adopted)) == 1


# --- what preparation does not do ------------------------------------------


def test_preparation_creates_no_report_approved_for_release_and_moves_no_baseline(
    session, adopted, store
):
    """ADR-0086: a prepared but unapproved set never advances the marker."""

    _configure(session, adopted, artifacts=[WEEKLY])
    before = session.scalar(
        text(
            "select count(*) from external_report_releases where project_id = :p"
        ),
        {"p": adopted.project.id},
    )

    _prepare(session, adopted, store)
    _prepare(
        session,
        adopted,
        store,
        coverage_declaration_id=_declare(session, adopted, variant="second-week").id,
    )

    after = session.scalar(
        text(
            "select count(*) from external_report_releases where project_id = :p"
        ),
        {"p": adopted.project.id},
    )
    assert (before, after) == (0, 0)
    assert session.scalars(
        select(ReleasePackage).where(
            ReleasePackage.project_id == adopted.project.id
        )
    ).all() == []
    # Two prepared candidates and the comparison predecessor is still absent.
    assert latest_authorized_package(session, adopted.project.id) is None


# --- the privileges the new relations carry --------------------------------


def test_a_runtime_capability_appends_a_candidate_and_can_never_change_one(session):
    """The privileges asserted rather than assumed (#640's discipline)."""

    privileges = {
        (login, table, act): session.execute(
            text(f"select has_table_privilege('{login}', 'public.{table}', '{act}')")
        ).scalar_one()
        for login in ("corridor_web", "corridor_worker")
        for table in (
            "release_packages",
            "release_candidates",
            "release_candidate_artifacts",
            "release_preparation_refusals",
        )
        for act in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }

    held = {key for key, granted in privileges.items() if granted}
    appendable = (
        "release_candidates",
        "release_candidate_artifacts",
        "release_preparation_refusals",
    )
    assert held == {
        (login, table, "SELECT")
        for login in ("corridor_web", "corridor_worker")
        for table in (
            "release_packages",
            *appendable,
        )
    } | {
        (login, table, "INSERT")
        for login in ("corridor_web", "corridor_worker")
        for table in appendable
    }


def test_every_release_relation_is_partitioned_by_project(session):
    """A candidate is customer content, so it carries #531's partition."""

    secured = {
        row[0]: row[1]
        for row in session.execute(
            text(
                "select relname, relrowsecurity from pg_class "
                " where relname in ('release_packages', 'release_candidates', "
                "'release_candidate_artifacts', 'release_preparation_refusals')"
            )
        ).all()
    }
    assert secured == {
        "release_packages": True,
        "release_candidates": True,
        "release_candidate_artifacts": True,
        "release_preparation_refusals": True,
    }
    policies = {
        row[0]
        for row in session.execute(
            text(
                "select policyname from pg_policies "
                " where tablename like 'release_%' and policyname like "
                "'%_project_partition'"
            )
        ).all()
    }
    assert policies == {
        "p_release_packages_project_partition",
        "p_release_candidates_project_partition",
        "p_release_candidate_artifacts_project_partition",
        "p_release_preparation_refusals_project_partition",
        # The request one coordinator submitted and what each attempt at it
        # produced, added with #675 and partitioned for the same reason: both
        # name what one customer was about to be sent, and why they were not.
        "p_release_preparation_requests_project_partition",
        "p_release_preparation_attempts_project_partition",
        # The authorized package's own enumeration, added with #533 and
        # partitioned for the same reason as everything above it.
        "p_release_package_artifacts_project_partition",
        # The reading one request was bound to and the occurrence published to
        # run it, added with #690. Both name which exact authorities one
        # customer's issue was prepared from.
        "p_release_preparation_readings_project_partition",
        "p_release_preparation_publications_project_partition",
    }


# --- the emission (#558) ----------------------------------------------------


def test_preparation_is_emitted_bound_to_the_code_and_product_revision(
    session, adopted, store
):
    _configure(session, adopted, artifacts=[WEEKLY])
    bound, _, candidate = _prepare(session, adopted, store)

    with capture_events() as collected:
        emit_preparation(
            bound,
            principal_subject=COORDINATOR.subject,
            surface="release_preparation",
            outcome=bound.readiness,
            candidate_identity=candidate.candidate_identity,
            content_sha256=candidate.content_sha256,
        )

    [event] = collected.events
    assert event.family is EventFamily.RELEASE_CANDIDATE_PREPARATION
    assert event.occurred_at == PREPARED_AT
    assert event.binding.code_revision == "git:529"
    assert event.binding.product_revision == "2026.03"
    assert event.binding.packetizer_rules_version == "delta-partition-v2"
    assert event.binding.enabled_feature_flags == ("release_candidate",)
    assert event.payload["candidate_identity"] == candidate.candidate_identity
    assert event.payload["configured_artifact_types"] == list(bound.artifact_types)
    assert event.payload["issue_profile_version"] == 1
    assert event.metric_labels == {
        "surface": "release_preparation",
        "status": bound.readiness,
    }


# --- the whole act, in three real transactions ------------------------------


class _ProbingStore:
    """A store that checks the project row is lockable on its first write.

    The first ``put`` happens as soon as the mandatory UCM has rendered, so a
    lock still held from the binding transaction would be held here. The probe
    runs on an independent connection with ``NOWAIT``, so it fails immediately
    rather than deadlocking the test.
    """

    backend = "filesystem"

    def __init__(self, inner, factory, project_id):
        self.inner = inner
        self.factory = factory
        self.project_id = project_id
        self.probes: list[bool] = []

    def put(self, key, data, *, sha256):
        with self.factory() as probe:
            try:
                probe.execute(
                    text("select id from projects where id = :id for update nowait"),
                    {"id": self.project_id},
                )
                self.probes.append(True)
            except DBAPIError:
                self.probes.append(False)
            probe.rollback()
        return self.inner.put(key, data, sha256=sha256)

    def exists(self, key):
        return self.inner.exists(key)

    def get(self, key, *, sha256):
        return self.inner.get(key, sha256=sha256)


def test_the_whole_preparation_runs_in_three_transactions_and_holds_no_lock(
    runtime_database, tmp_path, monkeypatch
):
    """The orchestrator, end to end, against real committed transactions.

    The binding transaction commits before a byte is produced, so the project
    row is lockable by an independent connection throughout the render phase;
    the attach transaction then re-derives everything and writes the complete
    set. If the lock were held across rendering — one long transaction rather
    than three — the probe inside the store would fail.
    """

    from corridor.release_candidate import prepare_release_candidate

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory

    with factory() as setup:
        project = Project(
            slug=f"release-runtime-{uuid4().hex[:8]}",
            name="Release Runtime",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush()
        enroll_member(
            setup,
            project_id=project.id,
            email="coordinator@example.test",
            principal=COORDINATOR,
            display_name="Coordinator",
            designations=[COORDINATION],
            operator=OPERATOR,
        )
        body = workbook_bytes(tmp_path / "runtime.xlsx", BASELINE_ROWS)
        revision_id, _ = adopt(setup, project, body, tmp_path)
        adopted = _Adopted(
            project=project, revision_id=revision_id, template_bytes=body
        )
        _configure(setup, adopted, artifacts=[WEEKLY, CHASE])
        declaration_id = _declare(setup, adopted).id
        setup.commit()
        project_id = project.id

    store = _ProbingStore(
        LocalFilesystemStore(tmp_path / "artifacts"), factory, project_id
    )

    outcome = prepare_release_candidate(
        factory,
        project_id=project_id,
        prepared_by=COORDINATOR,
        preparation=_preparation(project_id, revision_id),
        source_cutoff=CUTOFF,
        prepared_at=PREPARED_AT,
        coverage_declaration_id=declaration_id,
        templates=TEMPLATE,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
        template_bytes=body,
        binding=BINDING,
        store=store,
    )

    assert outcome.prepared, outcome.refusal_reason
    assert store.probes and all(store.probes), (
        "a project lock was held while the artifacts were being rendered"
    )
    with factory() as check:
        candidate = check.get_one(ReleaseCandidate, outcome.candidate_id)
        assert candidate.candidate_identity == outcome.candidate_identity
        assert candidate.readiness == outcome.readiness
        assert [row.artifact_type for row in candidate_artifacts(check, candidate)] == [
            "chase_list",
            "weekly_coordination_report",
        ]
        assert check.scalars(
            select(ReleasePreparationRefusal).where(
                ReleasePreparationRefusal.project_id == project_id
            )
        ).all() == []
        check.rollback()


# --- an explicit customer policy blocks; prose selects nothing --------------


RESOLVE_COMMITTED_DATE = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision="field:committed_date",
    statement="A moved Promised For is decided before this issue goes out.",
)

# The same policy written as a sentence a coordinator typed. It is a
# ``statement``, not a rule, and #641 refuses to execute it.
RESOLVE_BY_PROSE = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision="Utility owner sign-off recorded",
    statement="A moved Promised For is decided before this issue goes out.",
)

# The label a screen prints for the same canonical field. It selects nothing,
# deliberately: accepting it as an alias would be prose parsing with a lookup
# table in front of it (#641).
RESOLVE_BY_LABEL = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision="field:promised_for",
    statement="A moved Promised For is decided before this issue goes out.",
)


def _open_delta(session, adopted):
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


def test_an_explicit_customer_policy_blocks_on_an_undecided_difference(
    session, adopted, store
):
    """ADR-0086's fourth blocker, executed through #641's typed selector."""

    _configure(session, adopted, artifacts=[WEEKLY], policies=[RESOLVE_COMMITTED_DATE])
    _open_delta(session, adopted)

    bound, rendered, candidate = _prepare(session, adopted, store)

    assert bound.readiness == BLOCKED
    assert len(bound.blocked_decisions) == 1
    assert bound.blocked_decisions[0].selector == "field:committed_date"
    assert bound.blocked_decisions[0].field == "committed_date"
    # Complete but blocked: every configured artifact still rendered.
    assert len(rendered) == 2
    assert authorization_blockers(session, candidate, as_of=CUTOFF)


def test_a_policy_waiting_on_a_sentence_is_a_configuration_problem(
    session, adopted, store
):
    """A statement explains a policy to a person; it never selects a change."""

    _configure(session, adopted, policies=[RESOLVE_BY_PROSE])
    _open_delta(session, adopted)

    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted)

    assert refused.value.code == UNSUPPORTED_ISSUE_CONFIGURATION
    assert "not a decision selector" in refused.value.sentence


def test_the_screen_label_for_a_canonical_field_selects_nothing(
    session, adopted, store
):
    """``promised_for`` is presentation's wording for ``committed_date`` (#641).

    There is deliberately no alias, so a policy configured against the label
    is reported as a configuration problem rather than quietly matching the
    field it looks like.
    """

    _configure(session, adopted, policies=[RESOLVE_BY_LABEL])
    _open_delta(session, adopted)

    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted)

    assert refused.value.code == UNSUPPORTED_ISSUE_CONFIGURATION
    assert "field:promised_for" in refused.value.sentence


def test_an_undecided_difference_no_policy_selects_is_disclosed_not_blocking(
    session, adopted, store
):
    """ADR-0086: unresolved Proposed Deltas are rendered truthfully and go out."""

    _configure(session, adopted, artifacts=[WEEKLY])
    _open_delta(session, adopted)

    bound, _, candidate = _prepare(session, adopted, store)

    assert bound.blocked_decisions == ()
    assert bound.readiness == READY_WITH_EXCEPTIONS
    assert any(
        "is proposed and not decided" in sentence
        for sentence in bound.disclosed_exceptions
    )
    assert authorization_blockers(session, candidate, as_of=CUTOFF) == ()


# --- one coherent reading, and a refusal for anything else -----------------


def test_every_artifact_dereferences_the_same_five_shared_inputs(
    session, adopted, store
):
    """ADR-0086's invariant: one revision, one predecessor, one cutoff, one set."""

    _configure(session, adopted, artifacts=[SUMMARY, WEEKLY, CHASE])
    bound, _, candidate = _prepare(session, adopted, store)

    for row in candidate_artifacts(session, candidate):
        # The composite key is the dereference: an artifact names exactly one
        # candidate of exactly one project, and that candidate carries all five.
        owner = session.get_one(ReleaseCandidate, row.candidate_id)
        assert owner.id == candidate.id
        assert owner.project_id == row.project_id
        assert owner.accepted_revision_id == adopted.revision_id
        assert owner.previous_package_id is None
        assert owner.source_cutoff == CUTOFF
        assert owner.coverage_sha256 == bound.coverage.content_sha256
        assert owner.coverage_declaration_id == bound.coverage.declaration_id
        assert owner.output_template_format_id == bound.output_template_format_id
        assert owner.field_mapping_format_id == bound.field_mapping_format_id


@pytest.mark.parametrize(
    "predecessor",
    [
        pytest.param({}, id="missing"),
        pytest.param({"previous_authorized_package_id": False}, id="boolean"),
        pytest.param({"previous_authorized_package_id": 0}, id="zero"),
        pytest.param({"previous_authorized_package_id": -1}, id="negative"),
        pytest.param({"previous_authorized_package_id": "1"}, id="text"),
    ],
)
def test_an_external_window_requires_an_explicit_typed_predecessor(
    session, adopted, predecessor
):
    _configure(session, adopted, artifacts=[WEEKLY])
    preparation = _preparation(
        adopted.project.id, adopted.revision_id,
        comparison_baseline=AUTHORIZED_PACKAGE_COMPARISON,
        prior_delta_floor=0, prior_disposition_floor=0,
        **predecessor,
    )
    with pytest.raises(PreparationRefused) as refused:
        _bind(session, adopted, preparation=preparation)
    assert refused.value.code == MIXED_READING
    assert "explicit previous authorized package" in refused.value.sentence
    assert _candidates(session, adopted) == ()


def test_a_weekly_reading_from_another_project_refuses_preparation(
    session, adopted, store
):
    _configure(session, adopted)
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(PreparationRefused) as refused:
        _bind(
            session,
            adopted,
            preparation=_preparation(other.id, adopted.revision_id),
        )

    assert refused.value.code == "mixed_reading"


def test_a_weekly_reading_naming_a_revision_this_project_never_had_refuses(
    session, adopted, store
):
    _configure(session, adopted)

    with pytest.raises(PreparationRefused) as refused:
        _bind(
            session,
            adopted,
            preparation=_preparation(
                adopted.project.id, adopted.revision_id + 1_000_000
            ),
        )

    assert refused.value.code == "mixed_reading"


def test_a_profile_changed_after_preparation_makes_the_candidate_stale(
    session, adopted, store
):
    """A profile change never edits a prepared candidate (#640)."""

    _configure(session, adopted, artifacts=[WEEKLY])
    _, _, candidate = _prepare(session, adopted, store)
    assert candidate_is_stale(session, candidate, as_of=CUTOFF) == ()

    reconfigured_at = datetime(2026, 3, 5, 6, 0, tzinfo=timezone.utc)
    _configure(
        session,
        adopted,
        artifacts=[WEEKLY, CHASE],
        effective_from=reconfigured_at,
    )

    later = datetime(2026, 3, 9, 6, 0, tzinfo=timezone.utc)
    reasons = candidate_is_stale(session, candidate, as_of=later)
    assert any("configured to externally issue changed" in one for one in reasons)
    assert authorization_blockers(session, candidate, as_of=later)
    # The candidate keeps claiming exactly what it claimed.
    session.refresh(candidate)
    assert candidate.issue_profile_version == 1
    assert [row.artifact_type for row in candidate_artifacts(session, candidate)] == [
        "weekly_coordination_report"
    ]
