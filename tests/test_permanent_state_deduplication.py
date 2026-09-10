"""A duplicate is unrepresentable in every family ADR-0081 converges on (#457).

Each of these five families already knew what made a row the same row, and
each derived that identity where the row was written: inside a
``SECURITY DEFINER`` command's body, or in the Python that called it.  A
convention held by writers is not a property of the record, so these tests are
paired on purpose.  The replay half proves the command still converges on the
row it already wrote; the refusal half proves PostgreSQL now refuses the
duplicate on its own, presented raw by the very role that owns the family's
writes.  Only the second half is new, and only the second half survives a
writer that forgets.

The refusals connect through ``set local role`` rather than the runtime logins
(``tests/test_database_authority.py`` holds that boundary): the point here is
not that the application cannot write these tables, it is that not even their
owning role can write a second copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from corridor.config import settings
from corridor.materializer import materialize_quoted_statement_wording
from corridor.models import DeltaDeferral, DeltaGroup, Document, Fact, ProjectRecordRevision, ProposedDelta, SourceDelivery, SourceSegment
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
    record_delta_deferral,
)
from corridor import push_intake
from corridor.db_roles import RECORD_DECISION_ROLE
from corridor.source_append import SegmentValues, append_fact, append_source_segments


DECISION_ROLE = RECORD_DECISION_ROLE
DEFERRED_AT = datetime(2026, 6, 1, 15, 30, tzinfo=timezone.utc)
DEFERRED_UNTIL = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
WORDS = "Equistar will submit the exhibit."


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    return tmp_path / "store"


def _as_decision_role(session) -> None:
    """Write as the role that owns accepted authority, not as the owner."""

    session.execute(text(f"set local role {DECISION_ROLE}"))


def _minutes(session, project, name="minutes.pdf") -> Document:
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.slug}:{name}".encode()).hexdigest(),
        filename=name,
        doc_type="minutes",
    )
    session.add(document)
    session.flush()
    return document


def _wording_segment(session, project, words) -> SourceSegment:
    document = _minutes(session, project)
    (segment,) = append_source_segments(
        session,
        project_id=project.id,
        document_id=document.id,
        recorded_verbal_origin_id=None,
        segments=(
            SegmentValues(
                kind="prose_span",
                exact_text=words,
                content_sha256=sha256(words.encode()).hexdigest(),
                ordinal=1,
                page_no=1,
                start_offset=0,
                end_offset=len(words),
            ),
        ),
    )
    return segment


def _append_wording_fact(session, project, segment, words) -> Fact:
    return append_fact(
        session,
        project_id=project.id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="statement_candidate",
        subject_key=f"candidate:{segment.id}",
        recorded_by="local:test",
        content_sha256=sha256(f"wording:{segment.id}".encode()).hexdigest(),
        value=materialize_quoted_statement_wording(segment, words),
    )


def _wording_fact(session, project, words=WORDS) -> Fact:
    return _append_wording_fact(
        session, project, _wording_segment(session, project, words), words
    )


def _fact_count(session, project) -> int:
    return session.scalar(
        select(func.count()).select_from(Fact).where(Fact.project_id == project.id)
    )


# --- Source Facts ----------------------------------------------------------


def test_a_replayed_source_fact_append_returns_the_fact_already_recorded(
    session, project
):
    """The digest is the Fact, so appending it again is not a second Fact."""

    segment = _wording_segment(session, project, WORDS)
    first = _append_wording_fact(session, project, segment, WORDS)
    replayed = _append_wording_fact(session, project, segment, WORDS)

    assert replayed.id == first.id
    assert _fact_count(session, project) == 1


def test_a_second_source_fact_carrying_one_identity_digest_is_refused(
    session, project
):
    """Not a race the command wins: an exact copy is refused by the table."""

    fact = _wording_fact(session, project)

    with pytest.raises(IntegrityError, match="uq_facts_content_sha256"):
        session.execute(
            text(
                "insert into facts (project_id, document_id, extraction_run_id, "
                "  fact_type, subject_kind, subject_key, text_value, "
                "  transformation, recorded_by, content_sha256) "
                "select project_id, document_id, extraction_run_id, fact_type, "
                "  subject_kind, subject_key, text_value, transformation, "
                "  recorded_by, content_sha256 from facts where id = :fact_id"
            ),
            {"fact_id": fact.id},
        )


def test_a_source_fact_with_no_identity_digest_is_refused(session, project):
    """A Fact with no identity was the duplicate nothing could even name."""

    fact = _wording_fact(session, project)

    with pytest.raises(IntegrityError, match="content_sha256"):
        session.execute(
            text(
                "insert into facts (project_id, document_id, extraction_run_id, "
                "  fact_type, subject_kind, subject_key, text_value, "
                "  transformation, recorded_by, content_sha256) "
                "select project_id, document_id, extraction_run_id, fact_type, "
                "  subject_kind, subject_key, text_value, transformation, "
                "  recorded_by, null from facts where id = :fact_id"
            ),
            {"fact_id": fact.id},
        )


# --- Proposed Deltas -------------------------------------------------------


def _delta(field: str = "committed_date", proposed="2026-09-01") -> ProposedDeltaValues:
    return ProposedDeltaValues(
        change_type="modify",
        target=ExistingSubjectTarget("UTL-101", field),
        accepted_value="2026-08-01",
        proposed_value=proposed,
    )


def _group_count(session, project) -> int:
    return session.scalar(
        select(func.count())
        .select_from(DeltaGroup)
        .where(DeltaGroup.project_id == project.id)
    )


def test_a_replayed_source_version_reuses_its_group_and_its_deltas(session, project):
    """A retried append leaves no second group behind, and no second delta."""

    first = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(),),
    )
    replayed = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(),),
    )

    assert [row.id for row in replayed] == [row.id for row in first]
    assert replayed[0].group_id == first[0].group_id
    assert _group_count(session, project) == 1
    assert session.scalar(
        select(func.count())
        .select_from(ProposedDelta)
        .where(ProposedDelta.project_id == project.id)
    ) == 1


def test_a_later_batch_of_one_source_version_joins_the_group_it_opened(
    session, project
):
    """``delta_generation`` takes one source version in budgeted batches.

    Every batch used to open another group for the same atomic source change,
    so a version taken in three passes left three groups describing one.
    """

    first = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(),),
    )
    second = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(field="need_date", proposed="2026-10-01"),),
    )

    assert second[0].id != first[0].id
    assert second[0].group_id == first[0].group_id
    assert _group_count(session, project) == 1


def test_a_second_group_for_one_source_version_is_refused(session, project):
    """The absent document and the absent statement are the same absence."""

    create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(),),
    )

    with pytest.raises(IntegrityError, match="uq_delta_groups_source_change"):
        session.execute(
            text(
                "insert into delta_groups (project_id, source_family, "
                "  source_revision, document_id, statement_id) "
                "values (:project_id, 'workbook', 'rev-1', null, null)"
            ),
            {"project_id": project.id},
        )


# --- Decisions -------------------------------------------------------------


def _one_delta(session, project) -> ProposedDelta:
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="workbook",
        source_revision="rev-1",
        deltas=(_delta(),),
    )
    return delta


def test_a_replayed_deferral_returns_the_receipt_already_written(session, project):
    """Scheduling writes no revision, so the act carries no idempotency key.

    Its identity is what it already stores: the delta, the instant it was
    scheduled at, and the person who scheduled it (ADR-0084).
    """

    delta = _one_delta(session, project)
    first = record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=delta.id,
        deferred_at=DEFERRED_AT,
        scheduled_by_principal="local:coordinator",
        deferred_until=DEFERRED_UNTIL,
        wake_condition="utility responds",
    )
    replayed = record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=delta.id,
        deferred_at=DEFERRED_AT,
        scheduled_by_principal="local:coordinator",
        deferred_until=DEFERRED_UNTIL,
        wake_condition="utility responds",
    )

    assert replayed.id == first.id
    assert session.scalar(
        select(func.count())
        .select_from(DeltaDeferral)
        .where(DeltaDeferral.delta_id == delta.id)
    ) == 1


def test_a_second_deferral_of_one_scheduling_act_is_refused(session, project):
    delta = _one_delta(session, project)
    project_id, delta_id = project.id, delta.id
    record_delta_deferral(
        session,
        project_id=project_id,
        delta_id=delta_id,
        deferred_at=DEFERRED_AT,
        scheduled_by_principal="local:coordinator",
        deferred_until=DEFERRED_UNTIL,
    )
    session.flush()

    # The identifiers are read before the role changes: the decision role
    # holds no read on `projects`, so an ORM refresh under it would fail for
    # the wrong reason.
    _as_decision_role(session)
    with pytest.raises(IntegrityError, match="uq_delta_deferrals_occurrence"):
        session.execute(
            text(
                "insert into delta_deferrals (project_id, delta_id, deferred_at, "
                "  deferred_until, wake_condition, scheduled_by_principal, reason) "
                "values (:project_id, :delta_id, :deferred_at, :deferred_until, "
                "  null, 'local:coordinator', 'a retry that slipped the command')"
            ),
            {
                "project_id": project_id,
                "delta_id": delta_id,
                "deferred_at": DEFERRED_AT,
                "deferred_until": DEFERRED_UNTIL,
            },
        )


def test_one_revision_cannot_decide_one_fact_twice(session, project):
    """A revision's decision is read back by ``revision_id``, so it is one row.

    The predecessor is inserted already superseded, which is exactly the shape
    the effectiveness index permits: without this constraint one revision
    could carry the same Fact twice and that read would be picking one of a
    set rather than reading a row.
    """

    fact = _wording_fact(session, project)
    session.flush()
    _as_decision_role(session)
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions (project_id, command_type, "
            "  human_principal, released_policy, idempotency_key) "
            "values (:project_id, 'record_human_fact_decision', 'local:coordinator', "
            "  null, :key) returning id"
        ),
        {"project_id": project.id, "key": f"decide:{fact.id}"},
    )
    successor_id = session.scalar(text("select nextval('fact_decisions_id_seq')"))
    session.execute(
        text(
            "insert into fact_decisions (project_id, fact_id, subject_key, "
            "  fact_type, revision_id, disposition, superseded_by) "
            "values (:project_id, :fact_id, :subject_key, :fact_type, "
            "  :revision_id, 'include', :successor_id)"
        ),
        {
            "project_id": project.id,
            "fact_id": fact.id,
            "subject_key": fact.subject_key,
            "fact_type": fact.fact_type,
            "revision_id": revision_id,
            "successor_id": successor_id,
        },
    )

    with pytest.raises(IntegrityError, match="uq_fact_decisions_revision_fact"):
        session.execute(
            text(
                "insert into fact_decisions (id, project_id, fact_id, subject_key, "
                "  fact_type, revision_id, disposition, superseded_by) "
                "values (:successor_id, :project_id, :fact_id, :subject_key, "
                "  :fact_type, :revision_id, 'include', null)"
            ),
            {
                "successor_id": successor_id,
                "project_id": project.id,
                "fact_id": fact.id,
                "subject_key": fact.subject_key,
                "fact_type": fact.fact_type,
                "revision_id": revision_id,
            },
        )


# --- Project Record revisions ----------------------------------------------


def test_a_replayed_resolution_revision_returns_the_revision_already_opened(
    session, project
):
    first = session.scalar(
        select(
            func.open_delta_resolution_revision(
                project.id, "local:coordinator", "resolve:packet-1"
            )
        )
    )
    replayed = session.scalar(
        select(
            func.open_delta_resolution_revision(
                project.id, "local:coordinator", "resolve:packet-1"
            )
        )
    )

    assert replayed == first
    assert session.scalar(
        select(func.count())
        .select_from(ProjectRecordRevision)
        .where(ProjectRecordRevision.project_id == project.id)
    ) == 1


def test_a_project_record_revision_cannot_carry_a_blank_idempotency_key(
    session, project
):
    """Every command refuses a blank key; the column used to accept one.

    A blank key is the same non-identity for every command, so two revisions
    carrying it would collide on a key that identifies nothing.
    """

    _as_decision_role(session)
    with pytest.raises(
        IntegrityError, match="ck_project_record_revisions_idempotency_key"
    ):
        session.execute(
            text(
                "insert into project_record_revisions (project_id, command_type, "
                "  human_principal, released_policy, idempotency_key) "
                "values (:project_id, 'resolve_delta', 'local:coordinator', null, '')"
            ),
            {"project_id": project.id},
        )


# --- Connector deliveries --------------------------------------------------


def _binding(session, project):
    push_intake.register_push_credential(
        session,
        customer="Alpha Engineering",
        project=project,
        channel="project_alias",
        material=f"intake+{project.slug}@corridor.test",
    )
    return push_intake.bind_credential(
        session,
        push_intake.PushCredential(
            channel="project_alias",
            material=f"intake+{project.slug}@corridor.test",
        ),
    )


def _payload(body: bytes = b"%PDF-1.7\nrelocation exhibit\n%%EOF"):
    return push_intake.PushPayload(
        body=body,
        filename="exhibit.pdf",
        transport_delivery_id="transport-0001",
    )


def _delivery_dispositions(session, project) -> list[str]:
    return list(
        session.scalars(
            select(SourceDelivery.disposition)
            .where(SourceDelivery.project_id == project.id)
            .order_by(SourceDelivery.id)
        ).all()
    )


def test_a_replayed_delivery_returns_the_envelope_already_taken(session, project):
    """One delivery, taken once, and the replay recorded as its own outcome."""

    binding = _binding(session, project)
    first = push_intake.accept_delivery(session, binding, _payload())
    replayed = push_intake.accept_delivery(session, binding, _payload())

    assert replayed.delivery_id == first.delivery_id
    assert replayed.replayed is True
    assert replayed.envelope.idempotency_key == first.envelope.idempotency_key
    assert _delivery_dispositions(session, project) == ["stored", "duplicate"]


def test_a_second_delivery_of_one_envelope_is_refused(session, project):
    """The structural key stands even where the derived key would not.

    ADR-0083's idempotency key is a digest of this triple, and a row that
    carries a wrong one is refused by the identity trigger first. The envelope
    constraint is what is left when the derivation is not performed at all: the
    trigger is disabled here on purpose, because the point of a structural key
    is that it does not depend on anybody deriving anything. ADR-0089 widened
    the key by the disposition, so this insert repeats an outcome of a delivery
    the ledger already holds.
    """

    binding = _binding(session, project)
    push_intake.accept_delivery(session, binding, _payload())
    session.flush()
    project_id = project.id
    session.execute(
        text(
            "alter table source_deliveries disable trigger "
            "trg_source_deliveries_identity"
        )
    )

    with pytest.raises(IntegrityError, match="uq_source_deliveries_observation"):
        session.execute(
            text(
                "insert into source_deliveries (credential_id, customer, "
                "  project_id, transport, channel, configuration_identity, "
                "  external_identity, external_version, original_timestamps_json, "
                "  content_sha256, bytes_reference, metadata_json, "
                "  delivery_identity, idempotency_key, service_identity, "
                "  run_identity, disposition) "
                "select credential_id, customer, project_id, transport, channel, "
                "  configuration_identity, external_identity, external_version, "
                "  original_timestamps_json, content_sha256, bytes_reference, "
                "  metadata_json, delivery_identity, :key, service_identity, "
                "  'a second run', disposition from source_deliveries "
                " where project_id = :project_id"
            ),
            {
                "project_id": project_id,
                "key": sha256(b"a key nobody derived").hexdigest(),
            },
        )


def test_a_delivery_whose_identity_is_not_its_own_is_refused(session, project):
    """ADR-0083's identity is re-derived by the database, never supplied.

    The unique key was computed in Python and the database never checked it
    against the row it was stored on, so a writer that derived it wrongly — or
    reused another delivery's — satisfied the constraint and defeated the
    de-duplication it was there to provide. This row claims the identity of
    the delivery already taken while naming a different transport delivery.
    """

    binding = _binding(session, project)
    push_intake.accept_delivery(session, binding, _payload())
    session.flush()
    project_id = project.id

    with pytest.raises(IntegrityError, match="source_delivery:delivery_identity"):
        session.execute(
            text(
                "insert into source_deliveries (credential_id, customer, "
                "  project_id, transport, channel, configuration_identity, "
                "  external_identity, external_version, original_timestamps_json, "
                "  content_sha256, bytes_reference, metadata_json, "
                "  delivery_identity, idempotency_key, service_identity, "
                "  run_identity, disposition) "
                "select credential_id, customer, project_id, transport, channel, "
                "  configuration_identity, 'transport-0002', external_version, "
                "  original_timestamps_json, content_sha256, bytes_reference, "
                "  metadata_json, delivery_identity, :key, service_identity, "
                "  run_identity, disposition from source_deliveries "
                " where project_id = :project_id"
            ),
            {
                "project_id": project_id,
                "key": sha256(b"a key nobody derived").hexdigest(),
            },
        )
