"""The recorded set of authorized source bindings, and what it refuses (#886).

The gate that reads this record is proved in `tests/test_activation_runtime.py`;
what is proved here is the record itself: that the digest comes from the rows,
that a version is bound to its terms once, that withdrawing a binding leaves the
version it replaced readable, and that nothing but the command writes one.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor.source_authorization import (
    AuthorizedSourceBinding,
    SourceAuthorizationRefused,
    authorization_in_force,
    authorized_bindings,
    record_source_authorization,
    recorded_authorizations,
    source_binding_standing,
)

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
CUSTOMER = "fixture-customer"
ENVIRONMENT = "fixture-env"
IDENTITY = "fixture-source-authorization"

ALIAS = AuthorizedSourceBinding(
    channel="project_alias", configuration_identity="credential:7",
    permitted_source_classes=("email", "ucm_revision"),
    authentication_mode="machine_credential")
UPLOAD = AuthorizedSourceBinding(
    channel="product_upload", configuration_identity="product-upload",
    permitted_source_classes=("ucm_revision",),
    authentication_mode="human_principal")
SECOND_ALIAS = AuthorizedSourceBinding(
    channel="project_alias", configuration_identity="credential:9",
    permitted_source_classes=("minutes",),
    authentication_mode="machine_credential")


def record(session, project, bindings, *, version=1, identity=IDENTITY,
           governing_version="2026-09-01"):
    return record_source_authorization(
        session, project_id=project.id, authorization_identity=identity,
        authorization_version=version, customer=CUSTOMER, environment=ENVIRONMENT,
        governing_authorization_identity="customer-authorization-1",
        governing_authorization_version=governing_version, bindings=bindings,
        issued_at=NOW, issued_by_actor="operations:issuer",
        recorded_by_actor="operations:recorder")


def standing(session, project, binding, **overrides):
    values = dict(project_id=project.id, customer=CUSTOMER, channel=binding.channel,
                  configuration_identity=binding.configuration_identity,
                  configuration_version=binding.configuration_version,
                  authentication_mode=binding.authentication_mode)
    return source_binding_standing(session, **(values | overrides))


def test_the_recorded_set_carries_two_configurations_on_one_channel(session, project):
    """Two aliases into one project are two bindings, each on its own terms.

    The key is the whole channel/configuration/version triple, which is what a
    delivery carries, so naming the same channel twice is ordinary rather than
    a conflict the record has to reject.
    """
    recorded = record(session, project, [ALIAS, SECOND_ALIAS, UPLOAD])
    assert recorded.created is True

    in_force = authorization_in_force(session, project.id)
    assert in_force.binding_set_sha256 == recorded.binding_set_sha256
    assert authorized_bindings(session, in_force) == (UPLOAD, ALIAS, SECOND_ALIAS)
    assert standing(session, project, SECOND_ALIAS).permitted is True


def test_the_digest_is_the_databases_and_covers_every_field_of_the_set(session, project):
    """One canonical form, derived from the rows, indifferent to submission order.

    The digest is what the activation receipt binds, so it has to move when the
    set's content moves and stay still when only the order does. Nothing in
    Python recomputes it: the command derives it from what it writes.
    """

    def digest_of(bindings):
        savepoint = session.begin_nested()
        try:
            return record(session, project, bindings).binding_set_sha256
        finally:
            savepoint.rollback()

    ordered = digest_of([ALIAS, UPLOAD])
    assert digest_of([UPLOAD, ALIAS]) == ordered
    assert digest_of([ALIAS, UPLOAD, SECOND_ALIAS]) != ordered
    assert digest_of([
        ALIAS,
        AuthorizedSourceBinding(
            channel=UPLOAD.channel,
            configuration_identity=UPLOAD.configuration_identity,
            permitted_source_classes=("schedule_export",),
            authentication_mode=UPLOAD.authentication_mode),
    ]) != ordered
    assert digest_of([]) != ordered


def test_recording_the_same_version_again_replays_and_different_terms_refuse(
    session, project
):
    """A version names one set of terms, once, exactly as an onboarding grant does."""
    first = record(session, project, [ALIAS, UPLOAD])
    again = record(session, project, [UPLOAD, ALIAS])
    assert (again.created, again.authorization_id) == (False, first.authorization_id)

    with pytest.raises(SourceAuthorizationRefused) as refusal:
        with session.begin_nested():
            record(session, project, [ALIAS])
    assert refusal.value.code == "version_bound_to_other_terms"


@pytest.mark.parametrize("version", [1, 2])
def test_a_version_that_does_not_advance_is_refused(session, project, version):
    """Withdrawing or widening the set is always forward, never a rewrite."""
    record(session, project, [ALIAS, UPLOAD], version=2)
    with pytest.raises(SourceAuthorizationRefused) as refusal:
        with session.begin_nested():
            record(session, project, [ALIAS], version=version)
    assert refusal.value.code in {"version_went_backwards", "version_bound_to_other_terms"}


def test_a_second_identity_for_one_project_is_refused(session, project):
    """One project records its authorized sources in one place, under one name."""
    record(session, project, [ALIAS])
    with pytest.raises(SourceAuthorizationRefused) as refusal:
        with session.begin_nested():
            record(session, project, [ALIAS], version=2, identity="other-authorization")
    assert refusal.value.code == "authorization_identity_differs"


def test_an_empty_version_withdraws_everything_and_keeps_the_history(session, project):
    """Revocation is a version whose set is empty, and it erases nothing.

    There is no `revoked` column and no event relation for the same reason #827
    has no `consumed` column: what stopped is derivable from the record, and
    the terms a delivery was already taken under have to stay readable.
    """
    record(session, project, [ALIAS, UPLOAD])
    withdrawn = record(session, project, [], version=2)

    assert standing(session, project, ALIAS).reason == "source_binding_not_authorized"
    assert standing(session, project, UPLOAD).reason == "source_binding_not_authorized"

    history = recorded_authorizations(session, project.id)
    assert [record_.authorization_version for record_ in history] == [1, 2]
    assert authorized_bindings(session, history[0]) == (UPLOAD, ALIAS)
    assert authorized_bindings(session, history[1]) == ()
    assert authorization_in_force(session, project.id).binding_set_sha256 == (
        withdrawn.binding_set_sha256
    )


def test_the_standing_names_why_a_selection_is_not_authorized(session, project):
    """Four different answers, because an operator acts on each differently."""
    assert standing(session, project, ALIAS).reason == "no_source_authorization"

    record(session, project, [ALIAS, UPLOAD])
    assert standing(session, project, ALIAS).permitted is True
    assert standing(session, project, ALIAS).permitted_source_classes == (
        "email", "ucm_revision"
    )
    assert standing(session, project, ALIAS, customer="another-customer").reason == (
        "source_authorization_names_another_customer"
    )
    assert standing(session, project, ALIAS, configuration_version="revision-2").reason == (
        "source_binding_not_authorized"
    )
    assert standing(
        session, project, UPLOAD, authentication_mode="machine_credential"
    ).reason == "source_authentication_mode_not_authorized"


def test_a_malformed_or_duplicated_binding_set_is_refused(session, project):
    """The set is canonicalized before it is digested, so it has to be a set."""
    with pytest.raises(SourceAuthorizationRefused) as refusal:
        with session.begin_nested():
            record(session, project, [ALIAS, ALIAS])
    assert refusal.value.code == "duplicate_binding"

    with pytest.raises(SourceAuthorizationRefused) as refusal:
        AuthorizedSourceBinding(
            channel="project_alias", configuration_identity="credential:7",
            permitted_source_classes=(), authentication_mode="machine_credential")
    assert refusal.value.code == "malformed_binding"


def test_a_recorded_authorization_cannot_be_edited_or_written_raw(session, project):
    """Immutable, and only through the command, as every other receipt here is."""
    record(session, project, [ALIAS])
    for statement in (
        "update project_source_authorizations set customer = 'other'",
        "delete from project_source_authorization_bindings",
        "insert into project_source_authorizations (project_id, authorization_identity,"
        " authorization_version, customer, environment,"
        " governing_authorization_identity, governing_authorization_version,"
        " binding_set_sha256, issued_at, issued_by_actor, recorded_by_actor)"
        " values (:project, 'raw', 9, 'c', 'e', 'g', 'v', :digest, now(), 'a', 'b')",
    ):
        with pytest.raises(DBAPIError):
            with session.begin_nested():
                session.execute(text(statement),
                                {"project": project.id, "digest": "d" * 64})
