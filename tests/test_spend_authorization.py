"""The one spend-authorization declaration the five assistance families reference (#811)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.models import (
    CoordinationSummaryConfiguration,
    Project,
    SPEND_OPERATIONS,
    SourceIntakeDraftConfiguration,
    SpendAuthorization,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.spend_authorization import (
    InvalidSpendAuthorization,
    declare_spend_authorization,
)


ACTOR = HumanPrincipal("local:spend-declarer")


def _complete(project, **overrides):
    declaration = dict(
        project_id=project.id,
        principal=ACTOR,
        operation="coordination_summary",
        model="gpt-5.6-luna",
        max_input_tokens=8_000,
        max_output_tokens=500,
        timeout_seconds=30,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
    )
    declaration.update(overrides)
    return declaration


def test_a_complete_declaration_is_one_attributable_row(session, project):
    authorization = declare_spend_authorization(session, **_complete(project))

    stored = session.get(SpendAuthorization, authorization.id)
    assert stored.declared_by == ACTOR.subject
    assert stored.operation == "coordination_summary"
    assert stored.project_id == project.id
    assert stored.model == "gpt-5.6-luna"
    assert stored.effective_from is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "  "},
        {"max_input_tokens": 0},
        {"max_input_tokens": 200_001},
        {"max_output_tokens": 0},
        {"max_output_tokens": 20_001},
        {"timeout_seconds": 0},
        {"timeout_seconds": 601},
        {"max_requests": 2},
        {"retry_policy": "exponential"},
        {"retention_policy": "retained_indefinitely"},
        {"retention_policy": ""},
        {"observation_context": "customer_facing"},
        {"operation": "key_date_drafting"},
    ],
)
def test_the_nine_checks_and_the_operation_scope_refuse_in_python(
    session, project, overrides
):
    with pytest.raises(InvalidSpendAuthorization):
        declare_spend_authorization(session, **_complete(project, **overrides))


def test_the_declaring_actor_must_be_a_typed_human_principal(session, project):
    with pytest.raises(InvalidHumanPrincipal):
        declare_spend_authorization(
            session, **_complete(project, principal="local:free-text")
        )


def _raw_row(project_id, **overrides):
    row = dict(
        project_id=project_id,
        operation="coordination_summary",
        model="gpt-5.6-luna",
        max_input_tokens=8_000,
        max_output_tokens=500,
        timeout_seconds=30,
        max_requests=1,
        retry_policy="none",
        retention_policy="class_b_30_days",
        observation_context="internal_working_view",
        declared_by=ACTOR.subject,
    )
    row.update(overrides)
    return row


_INSERT = text(
    "insert into spend_authorizations (project_id, operation, model, "
    "max_input_tokens, max_output_tokens, timeout_seconds, max_requests, "
    "retry_policy, retention_policy, observation_context, declared_by) values "
    "(:project_id, :operation, :model, :max_input_tokens, :max_output_tokens, "
    ":timeout_seconds, :max_requests, :retry_policy, :retention_policy, "
    ":observation_context, :declared_by) returning id"
)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"operation": "key_date_drafting"}, "ck_spend_authorization_operation"),
        ({"model": "   "}, "ck_spend_authorization_model"),
        ({"max_input_tokens": 200_001}, "ck_spend_authorization_input_budget"),
        ({"max_output_tokens": 0}, "ck_spend_authorization_output_budget"),
        ({"timeout_seconds": 601}, "ck_spend_authorization_timeout"),
        ({"max_requests": 2}, "ck_spend_authorization_one_request"),
        ({"retry_policy": "exponential"}, "ck_spend_authorization_no_retry"),
        ({"retention_policy": "forever"}, "ck_spend_authorization_retention"),
        ({"observation_context": " "}, "ck_spend_authorization_context"),
        ({"declared_by": " "}, "ck_spend_authorization_actor"),
    ],
)
def test_the_database_holds_the_same_checks_once(session, project, overrides, constraint):
    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.execute(_INSERT, _raw_row(project.id, **overrides))
    assert constraint in str(refused.value)


def test_a_declaration_is_immutable(session, project):
    authorization = declare_spend_authorization(session, **_complete(project))

    with pytest.raises(DBAPIError, match="immutable"):
        with session.begin_nested():
            session.execute(
                text("update spend_authorizations set max_input_tokens = 1 where id = :id"),
                {"id": authorization.id},
            )
    with pytest.raises(DBAPIError, match="immutable"):
        with session.begin_nested():
            session.execute(
                text("delete from spend_authorizations where id = :id"),
                {"id": authorization.id},
            )


def test_two_equal_declarations_are_two_acts(session, project):
    first = declare_spend_authorization(session, **_complete(project))
    second = declare_spend_authorization(
        session, **_complete(project, principal=HumanPrincipal("local:second-declarer"))
    )

    assert first.id != second.id
    assert (first.declared_by, second.declared_by) == (
        ACTOR.subject,
        "local:second-declarer",
    )


def test_a_declaration_for_one_operation_backs_no_other(session, project):
    intake = declare_spend_authorization(
        session, **_complete(project, operation="source_intake_draft")
    )

    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.add(
                CoordinationSummaryConfiguration(
                    project_id=project.id,
                    authorization_id=intake.id,
                    source_scope="all_sources",
                    prompt_version="briefing_v2",
                )
            )
            session.flush()
    assert "fk_coordination_summary_configurations_authorization" in str(refused.value)

    with session.begin_nested():
        session.add(
            SourceIntakeDraftConfiguration(
                project_id=project.id,
                authorization_id=intake.id,
                prompt_version="source_intake_draft_v1",
            )
        )
        session.flush()


def test_a_declaration_for_one_project_backs_no_other(session, project):
    other = Project(slug="spend-authorization-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    theirs = declare_spend_authorization(session, **_complete(other))

    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.add(
                CoordinationSummaryConfiguration(
                    project_id=project.id,
                    authorization_id=theirs.id,
                    source_scope="all_sources",
                    prompt_version="briefing_v2",
                )
            )
            session.flush()
    assert "fk_coordination_summary_configurations_authorization" in str(refused.value)


def test_one_declaration_backs_one_configuration(session, project):
    authorization = declare_spend_authorization(session, **_complete(project))
    session.add(
        CoordinationSummaryConfiguration(
            project_id=project.id,
            authorization_id=authorization.id,
            source_scope="all_sources",
            prompt_version="briefing_v2",
        )
    )
    session.flush()

    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.add(
                CoordinationSummaryConfiguration(
                    project_id=project.id,
                    authorization_id=authorization.id,
                    source_scope="documents_only",
                    prompt_version="briefing_v2",
                )
            )
            session.flush()
    assert "uq_coordination_summary_configurations_authorization" in str(refused.value)


def test_a_configuration_reads_its_bounds_through_the_declaration(session, project):
    authorization = declare_spend_authorization(session, **_complete(project))
    configuration = CoordinationSummaryConfiguration(
        project_id=project.id,
        authorization_id=authorization.id,
        source_scope="all_sources",
        prompt_version="briefing_v2",
    )
    session.add(configuration)
    session.flush()
    session.expire_all()

    read = session.get(CoordinationSummaryConfiguration, configuration.id)
    assert (read.model, read.max_input_tokens, read.timeout_seconds) == (
        "gpt-5.6-luna",
        8_000,
        30,
    )
    assert read.retention_policy == "class_b_30_days"
    assert read.authorization.declared_by == ACTOR.subject
    assert set(SPEND_OPERATIONS) == {
        "coordination_summary",
        "production_run_explanation",
        "extraction_failure_diagnosis",
        "revision_change_explanation",
        "source_intake_draft",
    }
