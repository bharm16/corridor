"""Approving what a project externally issues, as one attributable act (#828).

Every instant here is declared. The approval instant is the effective instant,
so a test that read a clock could not say which version an earlier reporting
cutoff was configured under.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from corridor.access import COORDINATION, EXTERNAL_RELEASE
from corridor.issue_content import (
    CHASE_LIST_IDENTITY,
    UNREGISTERED_RENDERER,
    UNSUPPORTED_DECISION_SELECTOR,
)
from corridor.issue_profile import (
    ArtifactEntry,
    DecisionBlockingPolicy,
    RendererRevision,
    effective_issue_inventory,
    issue_profile_history,
    prepared_candidate_is_stale,
)
from corridor.issue_profile_approval import (
    UNCONFIGURABLE_ARTIFACT_TYPES,
    IssueProfileApprovalRefused,
    approve_issue_profile,
    propose_issue_profile,
)
from corridor.principals import HumanPrincipal
from corridor import refusals

from access_support import seed_membership
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import CHASE_RENDERER, REPORT_RENDERER, configure_issue


COORDINATOR = HumanPrincipal("local:coordinator")
RELEASER = HumanPrincipal("local:releaser")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
MARCH = datetime(2026, 3, 2, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 6, tzinfo=timezone.utc)

WEEKLY = "weekly_coordination_report"
CHASE = "chase_list"


@pytest.fixture
def adopted(session, member_project, tmp_path):
    """One adopted project whose coordinator may configure what it issues."""

    project = member_project(COORDINATOR, designations=[COORDINATION])
    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    return project


def approve(session, project, artifact_types, *, at, principal=COORDINATOR):
    """Approve exactly what the page would have shown for this selection."""

    proposal = propose_issue_profile(
        session,
        project_id=project.id,
        artifact_types=artifact_types,
        effective_from=at,
    )
    return approve_issue_profile(
        session,
        project_id=project.id,
        artifact_types=artifact_types,
        effective_from=at,
        supersedes_profile_id=proposal.supersedes_profile_id,
        supersedes_version=proposal.supersedes_version,
        content_sha256=proposal.content_sha256,
        principal=principal,
    )


def test_the_first_approval_establishes_what_this_project_issues(session, adopted):
    """A project with no configuration can be configured, by one named person."""

    proposal = propose_issue_profile(
        session, project_id=adopted.id, artifact_types=(WEEKLY,), effective_from=MARCH
    )
    assert proposal.profile_version == 1
    assert proposal.supersedes_profile_id is None
    assert proposal.artifact_types == (WEEKLY,)
    assert proposal.supported

    outcome = approve(session, adopted, (WEEKLY,), at=MARCH)

    assert outcome.registered is True
    assert outcome.profile_version == 1
    inventory = effective_issue_inventory(session, adopted.id, MARCH)
    assert inventory is not None
    assert set(inventory.artifact_types) == {"updated_ucm", WEEKLY}
    assert inventory.registered_by_principal == COORDINATOR.subject
    assert inventory.content_sha256 == outcome.content_sha256


def test_an_approval_carries_the_renderer_revision_the_project_already_pinned(
    session, adopted
):
    """A change about one artifact never upgrades another's renderer.

    The project is configured with the released v1 chase list, which is still a
    registered contract. Adding the weekly report must leave the chase list at
    v1: an approval about participation is not an approval of a different
    rendering.
    """

    pinned = RendererRevision(identity=CHASE_LIST_IDENTITY, version="v1")
    configure_issue(
        session,
        adopted,
        principal=COORDINATOR,
        effective_from=JANUARY,
        artifacts=(ArtifactEntry(artifact_type=CHASE, renderer=pinned),),
    )

    approve(session, adopted, (CHASE, WEEKLY), at=MARCH)

    inventory = effective_issue_inventory(session, adopted.id, MARCH)
    assert inventory.renderer_for(CHASE) == pinned
    assert inventory.renderer_for(WEEKLY) == REPORT_RENDERER


def test_an_unchanged_approval_registers_nothing(session, adopted):
    """Nothing changed, so there is no next version to attribute to anybody."""

    configure_issue(
        session,
        adopted,
        principal=COORDINATOR,
        effective_from=JANUARY,
        artifacts=(ArtifactEntry(artifact_type=CHASE, renderer=CHASE_RENDERER),),
    )
    before = issue_profile_history(session, adopted.id)

    outcome = approve(session, adopted, (CHASE,), at=MARCH)

    assert outcome.registered is False
    assert outcome.profile_version == 1
    assert issue_profile_history(session, adopted.id) == before


def test_the_same_approval_submitted_twice_converges(session, adopted):
    """A resubmitted Post is the same approval, not a competing one."""

    proposal = propose_issue_profile(
        session, project_id=adopted.id, artifact_types=(WEEKLY,), effective_from=MARCH
    )
    submission = {
        "project_id": adopted.id,
        "artifact_types": (WEEKLY,),
        "supersedes_profile_id": proposal.supersedes_profile_id,
        "supersedes_version": proposal.supersedes_version,
        "content_sha256": proposal.content_sha256,
        "principal": COORDINATOR,
    }

    first = approve_issue_profile(session, effective_from=MARCH, **submission)
    # The browser sends it again, at a later declared instant. The predecessor
    # has legitimately moved, and the resubmission must not be answered as a
    # concurrent change by somebody else.
    second = approve_issue_profile(session, effective_from=APRIL, **submission)

    assert first.registered is True
    assert second.registered is False
    assert second.profile_version == first.profile_version
    assert len(issue_profile_history(session, adopted.id)) == 1


def test_a_concurrent_approval_of_something_else_is_refused(session, adopted):
    """What the page bound is not what it would replace any more."""

    proposal = propose_issue_profile(
        session, project_id=adopted.id, artifact_types=(WEEKLY,), effective_from=MARCH
    )
    # Somebody else approves a different configuration in the meantime.
    approve(session, adopted, (CHASE,), at=MARCH)

    with pytest.raises(IssueProfileApprovalRefused) as refused:
        approve_issue_profile(
            session,
            project_id=adopted.id,
            artifact_types=(WEEKLY,),
            effective_from=APRIL,
            supersedes_profile_id=proposal.supersedes_profile_id,
            supersedes_version=proposal.supersedes_version,
            content_sha256=proposal.content_sha256,
            principal=COORDINATOR,
        )

    assert refused.value.refusal_kind == refusals.STALE
    assert "changed by someone else" in str(refused.value)
    assert len(issue_profile_history(session, adopted.id)) == 1


def test_an_approval_of_bytes_the_page_did_not_show_is_refused(session, adopted):
    """The digest binds the exact proposal; a different one registers nothing."""

    with pytest.raises(IssueProfileApprovalRefused) as refused:
        approve_issue_profile(
            session,
            project_id=adopted.id,
            artifact_types=(WEEKLY,),
            effective_from=MARCH,
            supersedes_profile_id=None,
            supersedes_version=None,
            content_sha256="0" * 64,
            principal=COORDINATOR,
        )

    assert refused.value.refusal_kind == refusals.STALE
    assert issue_profile_history(session, adopted.id) == ()


def test_an_artifact_this_release_cannot_render_is_refused_at_approval(
    session, adopted
):
    """#828's third criterion, with the case `pilot-success-criteria` names.

    The provenance sidecar is a configurable artifact type with no registered
    renderer contract at any version, so a profile selecting it refused
    *preparation* a week later. It is refused here instead, by name.
    """

    assert UNCONFIGURABLE_ARTIFACT_TYPES == ("provenance_sidecar",)

    proposal = propose_issue_profile(
        session,
        project_id=adopted.id,
        artifact_types=("provenance_sidecar",),
        effective_from=MARCH,
    )
    assert not proposal.supported
    assert [problem.code for problem in proposal.problems] == [UNREGISTERED_RENDERER]
    assert "the provenance sidecar" in proposal.problems[0].sentence

    with pytest.raises(IssueProfileApprovalRefused) as refused:
        approve_issue_profile(
            session,
            project_id=adopted.id,
            artifact_types=("provenance_sidecar",),
            effective_from=MARCH,
            supersedes_profile_id=None,
            supersedes_version=None,
            content_sha256=proposal.content_sha256,
            principal=COORDINATOR,
        )

    assert "the provenance sidecar" in str(refused.value)
    assert issue_profile_history(session, adopted.id) == ()


def test_a_policy_this_release_cannot_execute_names_the_customer_facing_field(
    session, adopted
):
    """#670's sentence is the refusal, rather than a second diagnosis here.

    A decision-blocking policy carried forward from the configuration in force
    is part of what an approval would register, so a selector this release
    cannot match stops the approval and says which canonical field to configure
    and which label a customer reads it under.
    """

    configure_issue(
        session,
        adopted,
        principal=COORDINATOR,
        effective_from=JANUARY,
        policies=(
            DecisionBlockingPolicy(
                policy="resolve_before_issue:v1",
                required_decision="field:promised_for",
                statement="Promised For changes are decided before this issue.",
            ),
        ),
    )

    proposal = propose_issue_profile(
        session, project_id=adopted.id, artifact_types=(WEEKLY,), effective_from=MARCH
    )
    assert [problem.code for problem in proposal.problems] == [
        UNSUPPORTED_DECISION_SELECTOR
    ]

    with pytest.raises(IssueProfileApprovalRefused) as refused:
        approve_issue_profile(
            session,
            project_id=adopted.id,
            artifact_types=(WEEKLY,),
            effective_from=MARCH,
            supersedes_profile_id=proposal.supersedes_profile_id,
            supersedes_version=proposal.supersedes_version,
            content_sha256=proposal.content_sha256,
            principal=COORDINATOR,
        )

    # #670's whole point: the canonical selector to configure, and the label a
    # customer reads that field under, in the sentence that refuses.
    assert "use field:committed_date, shown as Promised for" in str(refused.value)
    assert len(issue_profile_history(session, adopted.id)) == 1


def test_only_a_project_coordination_designation_may_approve(session, member_project, tmp_path):
    """The registration command owns the rule; this is not a second gate."""

    project = member_project(RELEASER, designations=[EXTERNAL_RELEASE])
    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    seed_membership(session, project, COORDINATOR, designations=[COORDINATION])

    from corridor.issue_profile import IssueProfileRefused

    with pytest.raises(IssueProfileRefused) as refused:
        approve(session, project, (WEEKLY,), at=MARCH, principal=RELEASER)

    assert "project-coordination decision" in str(refused.value)
    assert issue_profile_history(session, project.id) == ()


def test_the_updated_ucm_is_not_a_selection_anyone_can_make(session, adopted):
    """ADR-0091's mandatory member has no configuration switch to offer."""

    with pytest.raises(IssueProfileApprovalRefused) as refused:
        propose_issue_profile(
            session,
            project_id=adopted.id,
            artifact_types=("updated_ucm",),
            effective_from=MARCH,
        )

    assert refused.value.refusal_kind == refusals.MALFORMED_INPUT


def test_an_approved_change_makes_the_prepared_issue_stale(session, adopted):
    """#828's fourth criterion, under #529's own rule and no new one.

    ``prepared_candidate_is_stale`` is the profile term of
    ``release_candidate.candidate_staleness_reasons``, and it is what a
    candidate prepared against the previous version is read by. Nothing here
    reaches into a candidate: a version is registered, and the comparison
    answers differently. `test_issue_path_end_to_end` and
    `test_release_candidate` carry the same rule against a real prepared row.
    """

    first = approve(session, adopted, (WEEKLY,), at=MARCH)
    in_force = effective_issue_inventory(session, adopted.id, MARCH)
    assert not prepared_candidate_is_stale(
        in_force,
        profile_id=first.profile_id,
        profile_version=first.profile_version,
    )

    second = approve(session, adopted, (WEEKLY, CHASE), at=APRIL)

    assert second.registered is True
    later = effective_issue_inventory(session, adopted.id, APRIL)
    assert prepared_candidate_is_stale(
        later,
        profile_id=first.profile_id,
        profile_version=first.profile_version,
    )
