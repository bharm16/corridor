"""Resolving one Proposed Delta through the trusted decision primitive (#519).

Accept, edit, and reject are semantic dispositions: each records one
attributable decision and, standalone, one atomic Project Record revision.
Defer is Work List scheduling and writes no revision (ADR-0084, ADR-0085).
The same validation and decision-construction seam serves #526's packet-owned
transaction, an edited value can never be unsupported free text, and every
authoritative write goes through the record-decision role's command.

Every time in these tests is supplied by the caller. Nothing compares a
server-assigned timestamp with a logical one, and every "did the record move"
question is answered from append-only identifiers.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events
from corridor.delta_refusals import (
    BOTH,
    DATABASE_ONLY,
    PYTHON_PRECHECK,
    REFUSAL_CODES,
    REFUSAL_TOKEN,
    REFUSAL_VOCABULARY,
    REVIEW_PACKET_CODES,
    REVIEW_PACKET_TOKEN,
    REVIEW_PACKET_VOCABULARY,
    UNCLASSIFIED_REFUSAL,
    database_refusal_code,
)
from corridor.delta_resolution import (
    ACCEPT,
    CONSTRAINED_EDIT,
    COORDINATION_NEEDED,
    DEFER,
    DEFERRED,
    EDIT,
    REFUSED,
    REJECT,
    RESOLVED,
    STALE,
    UNSUPPORTED,
    CapturedSupport,
    ChildDecisionRequest,
    DeltaResolutionRefused,
    FreeText,
    LosslessNormalization,
    NamedComposition,
    RecordEffect,
    Refusal,
    SeparateSourceOrigin,
    cited_support_assessments,
    commit_child_decision,
    defer_delta,
    delta_effect_kind,
    incoming_source_facts,
    live_delta_status,
    open_resolution_revision,
    resolve_delta,
    validate_child_decision,
)
from corridor.delta_resolution import _refusal
from corridor.migrations.source_append_commands.resolve_delta import (
    RESOLVE_DELTA_REFUSAL_CODES,
)
from corridor.models import (
    DeltaDecisionSupport,
    DeltaDeferral,
    DeltaDisposition,
    DeltaRecordDecision,
    Fact,
    FactDecision,
    FactSource,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
    SourceSegment,
    SupportAssessment,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
)
from corridor.db_roles import RECORD_DECISION_ROLE
from harness_support import adopt_baseline_facts, as_role
from source_capture_support import Rendition
from delta_supersession_support import record_delta_supersession
from corridor.support_assessments import FactProposition, record_support_assessment


ALICE = HumanPrincipal("local:alice")
BOB = HumanPrincipal("local:bob")
DECIDED_AT = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
ASSESSED_AT = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
SUBJECT = "Utility Conflicts!7"


def _rendition(session: Session, project: Project, name: str) -> Rendition:
    """One arriving workbook rendition, concerning this module's one subject."""

    return Rendition(session, project, name, subject_key=SUBJECT)


def _support(
    session: Session,
    project: Project,
    fact: Fact,
    segment: SourceSegment,
    *,
    assessment: str = "supported",
    evidence_role: str = "value_support",
    at: datetime = ASSESSED_AT,
) -> SupportAssessment:
    return record_support_assessment(
        session,
        project_id=project.id,
        proposition=FactProposition(fact.id),
        source_segment_ids=[segment.id],
        evidence_role=evidence_role,
        assessment=assessment,
        authority=ALICE,
        assessed_at=at,
    )


def _delta(
    session: Session,
    project: Project,
    *,
    field: str = "station_from",
    accepted_value: object = "1149+00",
    proposed_value: object = "1200+00",
    change_type: str = "modify",
    subject: str = SUBJECT,
    source_revision: str = "rev-1",
    document_id: int | None = None,
    baseline_revision: int | None = None,
) -> ProposedDelta:
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="ucm-workbook",
        source_revision=source_revision,
        document_id=document_id,
        deltas=[
            ProposedDeltaValues(
                change_type=change_type,
                target=ExistingSubjectTarget(subject_identity=subject, field=field),
                accepted_value=accepted_value,
                proposed_value=proposed_value,
                accepted_baseline_revision=(
                    f"revision:{baseline_revision}"
                    if baseline_revision is not None
                    else None
                ),
            )
        ],
        is_complete_enumerative_source=change_type == "apparent_removal",
        row_accounting_sealed=change_type == "apparent_removal",
    )
    return delta


def _adopt(session: Session, project: Project, fact: Fact, key: str) -> int:
    """One accepted baseline decision, written as the record-decision role."""

    return adopt_baseline_facts(session, project, fact, key=key)


def _request(delta: ProposedDelta, **overrides) -> ChildDecisionRequest:
    values = {
        "project_id": delta.project_id,
        "delta_id": delta.id,
        "action": ACCEPT,
        "principal": ALICE,
        "idempotency_key": f"resolve:{delta.id}:{uuid4().hex[:8]}",
        "decided_at": DECIDED_AT,
    }
    values.update(overrides)
    return ChildDecisionRequest(**values)


def _revision_count(session: Session, project: Project) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ProjectRecordRevision)
            .where(ProjectRecordRevision.project_id == project.id)
        )
    )


# --- The three semantic dispositions --------------------------------------


def test_accept_records_the_incoming_proposition_as_the_effective_value(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-b.xlsx")
    accepted_fact, accepted_segment = rendition.capture(
        fact_type="station_from", value="1149+00"
    )
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, incoming_segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, incoming_segment)
    delta = _delta(session, project, baseline_revision=baseline)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    assert outcome.wrote_revision
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.subject_key == SUBJECT,
            FactDecision.fact_type == "station_from",
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert [decision.fact_id for decision in effective] == [incoming.id]
    assert effective[0].revision_id == outcome.revision_id

    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.disposition == ACCEPT
    assert decision.effect_kind == "changed_field"
    assert decision.decided_by_principal == ALICE.subject
    assert decision.observed_accepted_revision_id == baseline
    assert [row.id for row in cited_support_assessments(session, decision)] == [
        assessment.id
    ]
    revision = session.get(ProjectRecordRevision, outcome.revision_id)
    assert revision.command_type == "resolve_delta"
    assert revision.human_principal == ALICE.subject
    assert revision.released_policy is None
    assert live_delta_status(session, delta.id) == "resolved"


def test_edit_retains_the_incoming_proposition_delta_support_and_authority(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-c.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, _ = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    chosen, chosen_segment = rendition.capture(
        fact_type="station_from", value="1205+50", cell="A10"
    )
    assessment = _support(session, project, chosen, chosen_segment)
    delta = _delta(session, project, baseline_revision=baseline)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            observed_accepted_revision_id=baseline,
            edit_basis=CapturedSupport(fact_id=chosen.id),
            support_assessment_ids=(assessment.id,),
            rationale="the later row states the surveyed station",
        ),
    )

    assert outcome.status == RESOLVED
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.disposition == EDIT
    assert decision.edit_basis == {"kind": "captured_support", "fact_id": chosen.id}
    assert decision.decided_by_principal == ALICE.subject
    # The incoming proposition, the delta, and the support all remain readable.
    kept = session.get(ProposedDelta, delta.id)
    assert kept.proposed_value == "1200+00"
    assert kept.accepted_value == "1149+00"
    assert session.get(Fact, incoming.id) is not None
    assert session.get(SupportAssessment, assessment.id).superseded_by is None
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "station_from",
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert [decision.fact_id for decision in effective] == [chosen.id]


def test_reject_records_that_the_current_accepted_position_stands(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-d.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    delta = _delta(session, project, baseline_revision=baseline)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=REJECT,
            observed_accepted_revision_id=baseline,
            rationale="the workbook row was keyed to the wrong conflict",
        ),
    )

    assert outcome.status == RESOLVED
    assert outcome.wrote_revision
    assert outcome.fact_decision_ids == ()
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert [decision.fact_id for decision in effective] == [accepted_fact.id]
    assert effective[0].revision_id == baseline
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.disposition == REJECT
    assert decision.revision_id == outcome.revision_id


def test_defer_leaves_the_delta_open_and_writes_no_project_record_revision(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-e.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    delta = _delta(session, project, baseline_revision=baseline)
    before = _revision_count(session, project)

    outcome = defer_delta(
        session,
        _request(
            delta,
            action=DEFER,
            deferred_until=DECIDED_AT + timedelta(days=30),
            wake_condition="a newer workbook revision for this conflict",
            deferral_reason="waiting on the utility's own survey",
        ),
    )

    assert outcome.status == DEFERRED
    assert outcome.revision_id is None
    assert not outcome.wrote_revision
    assert _revision_count(session, project) == before
    receipt = session.get(DeltaDeferral, outcome.deferral_id)
    assert receipt.scheduled_by_principal == ALICE.subject
    assert receipt.deferred_until == DECIDED_AT + timedelta(days=30)
    assert receipt.wake_condition == "a newer workbook revision for this conflict"
    # The delta stays open: no disposition and no record decision exist.
    assert (
        session.scalar(
            select(func.count())
            .select_from(DeltaDisposition)
            .where(DeltaDisposition.delta_id == delta.id)
        )
        == 0
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(DeltaRecordDecision)
            .where(DeltaRecordDecision.delta_id == delta.id)
        )
        == 0
    )
    assert live_delta_status(session, delta.id) == "deferred"


def test_a_deferred_delta_is_still_resolvable_when_it_returns(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-e2.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project, baseline_revision=baseline)

    defer_delta(
        session,
        _request(delta, action=DEFER, wake_condition="the utility replies"),
    )
    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    assert live_delta_status(session, delta.id) == "resolved"


# --- One declared vocabulary, two enforcing halves ------------------------
#
# ADR-0083 keeps both halves: PostgreSQL is the authority and the Python
# pre-check is the readable copy that tells a coordinator what is wrong before
# anything is read for writing. What these tests hold still is the part that was
# never written down — which codes exist, what each one's status is, and which
# half is allowed to raise it. They need no database: every input is source text
# or a pure function.

# Every declared code, its outcome status, and its sole raiser, in the module's
# own order. Asserted whole, so a new code cannot arrive without a reader
# deciding which half owns it and what a screen does with it.
DECLARED_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    ("already_effective", REFUSED, DATABASE_ONLY),
    ("already_resolved", REFUSED, BOTH),
    ("ambiguous_effective_decision", REFUSED, DATABASE_ONLY),
    ("append_only", REFUSED, DATABASE_ONLY),
    ("constrained_edit", CONSTRAINED_EDIT, PYTHON_PRECHECK),
    ("cross_project_delta", REFUSED, BOTH),
    ("cross_project_fact", REFUSED, BOTH),
    ("cross_project_revision", REFUSED, DATABASE_ONLY),
    ("external_fact_needs_coordination", COORDINATION_NEEDED, PYTHON_PRECHECK),
    ("field_mismatch", REFUSED, BOTH),
    ("invalid_action", REFUSED, BOTH),
    ("key_bound_to_other_content", REFUSED, DATABASE_ONLY),
    ("missing_decided_at", REFUSED, DATABASE_ONLY),
    ("missing_idempotency_key", REFUSED, DATABASE_ONLY),
    ("missing_principal", REFUSED, DATABASE_ONLY),
    ("missing_record_effect", REFUSED, BOTH),
    ("missing_support", UNSUPPORTED, BOTH),
    ("missing_wake_condition", REFUSED, DATABASE_ONLY),
    ("organization_change_kind_required", REFUSED, PYTHON_PRECHECK),
    ("stale_accepted_revision", STALE, BOTH),
    ("subject_mismatch", REFUSED, BOTH),
    ("superseded_delta", REFUSED, BOTH),
    ("unauthorized_writer", REFUSED, DATABASE_ONLY),
    ("unsupported_free_text", CONSTRAINED_EDIT, PYTHON_PRECHECK),
)

# The Review Packet family (#526, #835): every ``review_packet:<code>`` token
# the packet, Follow-up Plan, closure and reversal commands raise, with the
# status a screen routes on and which half raises it. Only ``missing_support`` leaves REFUSED,
# because none of the others carries the structured refresh a STALE,
# UNSUPPORTED, CONSTRAINED_EDIT or COORDINATION_NEEDED outcome promises.
DECLARED_PACKET_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    ("already_closed", REFUSED, DATABASE_ONLY),
    ("already_resolved", REFUSED, BOTH),
    ("already_reversed", REFUSED, DATABASE_ONLY),
    ("child_identity_mismatch", REFUSED, DATABASE_ONLY),
    ("cross_project_delta", REFUSED, BOTH),
    ("cross_project_plan", REFUSED, DATABASE_ONLY),
    ("cross_project_receipt", REFUSED, DATABASE_ONLY),
    ("cross_project_revision", REFUSED, DATABASE_ONLY),
    ("duplicate_child", REFUSED, DATABASE_ONLY),
    ("empty_packet", REFUSED, DATABASE_ONLY),
    ("invalid_closure_kind", REFUSED, DATABASE_ONLY),
    ("invalid_grouping_key", REFUSED, DATABASE_ONLY),
    ("invalid_outcome", REFUSED, BOTH),
    ("key_bound_to_other_content", REFUSED, DATABASE_ONLY),
    ("later_act_depends", REFUSED, DATABASE_ONLY),
    ("missing_cancellation_reason", REFUSED, DATABASE_ONLY),
    ("missing_decided_at", REFUSED, DATABASE_ONLY),
    ("missing_grouping_rule", REFUSED, DATABASE_ONLY),
    ("missing_idempotency_key", REFUSED, DATABASE_ONLY),
    ("missing_principal", REFUSED, DATABASE_ONLY),
    ("missing_question", REFUSED, BOTH),
    ("missing_responsible_party", REFUSED, BOTH),
    ("missing_revision", REFUSED, DATABASE_ONLY),
    ("missing_source_revision", REFUSED, DATABASE_ONLY),
    ("missing_successor_plan", REFUSED, DATABASE_ONLY),
    ("missing_support", UNSUPPORTED, BOTH),
    ("successor_on_other_delta", REFUSED, DATABASE_ONLY),
    ("superseded_delta", REFUSED, BOTH),
    ("unexpected_revision", REFUSED, DATABASE_ONLY),
    ("unordered_children", REFUSED, DATABASE_ONLY),
)

# The concurrency refusals: each is decided by state the pre-check cannot hold
# still, so a Python guess at one is either a lie or a race.
DATABASE_OWNED_CONCURRENCY = (
    "already_effective",
    "ambiguous_effective_decision",
    "key_bound_to_other_content",
    "missing_decided_at",
    "missing_idempotency_key",
    "missing_principal",
)


def _precheck_codes() -> set[str]:
    """Every code the readable half actually raises, read from its own source.

    One constructor builds every pre-check ``Refusal``, so the codes it raises
    are exactly the first argument of every ``_refusal`` call in the module. A
    reason spelled as a bare literal at a raise site would not be found here,
    which is the point: it would also not be declared.
    """

    module = ast.parse(
        Path("src/corridor/delta_resolution.py").read_text(encoding="utf-8")
    )
    return {
        code
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_refusal"
        and node.args
        for code in _named_codes(node.args[0])
    }


def _packet_precheck_codes() -> set[str]:
    """Every reason the packet's readable half writes into a ``Refusal`` itself.

    ``review_packets`` has no single constructor: each pre-check site builds a
    ``Refusal`` with a ``reason=`` keyword, so the codes it raises are exactly
    the literal reasons of those calls. A reason forwarded from another
    refusal (``reason=refusal.reason``) is that refusal's, not this half's.
    """

    module = ast.parse(
        Path("src/corridor/review_packets.py").read_text(encoding="utf-8")
    )
    return {
        code
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Refusal"
        for keyword in node.keywords
        if keyword.arg == "reason"
        and not isinstance(keyword.value, ast.Attribute)
        for code in _named_codes(keyword.value)
        if code is not None
    }


def _named_codes(argument: ast.expr) -> tuple[str, ...]:
    """The codes one ``_refusal`` first argument can name.

    One site chooses between two of them — a delta that has left the actionable
    set is either resolved or superseded — so a conditional names both.
    """

    if isinstance(argument, ast.Constant):
        return (argument.value,)
    if isinstance(argument, ast.IfExp):
        return _named_codes(argument.body) + _named_codes(argument.orelse)
    raise AssertionError(
        f"line {argument.lineno} names its refusal code in a form no reader can "
        "check; a code is a literal, or a choice between literals"
    )


def test_the_declared_vocabulary_gives_every_code_a_status_and_one_raiser() -> None:
    declared = tuple(
        (entry.code, entry.status, entry.raiser) for entry in REFUSAL_VOCABULARY
    )

    assert declared == DECLARED_VOCABULARY
    assert set(REFUSAL_CODES) == {code for code, _, _ in DECLARED_VOCABULARY}
    # The unclassifiable fallback is not a code either half raises, so a raise
    # site cannot reach it by name.
    assert UNCLASSIFIED_REFUSAL.code not in REFUSAL_CODES


def test_every_refusal_the_command_raises_is_declared() -> None:
    """The plpgsql is the authority, parsed with the runtime's own expression."""

    source = Path(
        "src/corridor/migrations/source_append_commands/resolve_delta.py"
    ).read_text(encoding="utf-8")
    raised = set(REFUSAL_TOKEN.findall(source))

    assert raised == set(RESOLVE_DELTA_REFUSAL_CODES)
    assert raised == {
        entry.code
        for entry in REFUSAL_VOCABULARY
        if entry.raiser in (DATABASE_ONLY, BOTH)
    }


def test_the_precheck_raises_exactly_the_codes_it_is_declared_to_own() -> None:
    raised = _precheck_codes()

    assert raised == {
        entry.code
        for entry in REFUSAL_VOCABULARY
        if entry.raiser in (PYTHON_PRECHECK, BOTH)
    }
    assert {code for code in raised if REFUSAL_CODES[code].raiser == DATABASE_ONLY} == set()


def test_the_precheck_cannot_anticipate_a_database_only_refusal() -> None:
    """A concurrency refusal is the command's to raise, never a Python guess."""

    for code in DATABASE_OWNED_CONCURRENCY:
        assert REFUSAL_CODES[code].raiser == DATABASE_ONLY
        with pytest.raises(DeltaResolutionRefused):
            _refusal(code, delta_id=1)


def test_a_declared_sentence_is_the_words_the_refusal_carries() -> None:
    """The customer sentence is declared once, not written again at each site."""

    for entry in REFUSAL_VOCABULARY:
        if entry.sentence is None or entry.raiser == DATABASE_ONLY:
            continue
        built = _refusal(entry.code, delta_id=1)
        assert built.detail == entry.sentence
        assert built.status == entry.status
        assert built.reason == entry.code

    # A code raised for several reasons declares no sentence, and must say which
    # of them this refusal is rather than repeating its own name back.
    assert REFUSAL_CODES["invalid_action"].sentence is None
    with pytest.raises(DeltaResolutionRefused):
        _refusal("invalid_action", delta_id=1)


def test_a_database_message_is_mapped_through_the_declared_vocabulary() -> None:
    assert database_refusal_code(
        "resolve_delta:already_effective Source Fact 7 is already the effective "
        "accepted value"
    ) == REFUSAL_CODES["already_effective"]
    assert database_refusal_code("resolve_delta:missing_support ...").status == UNSUPPORTED
    # A message with no readable token still comes back structured.
    assert database_refusal_code("deadlock detected") == UNCLASSIFIED_REFUSAL
    # A token the vocabulary does not declare is not guessed at either.
    assert database_refusal_code("resolve_delta:unheard_of ...") == UNCLASSIFIED_REFUSAL


# --- The Review Packet family's declaration (#526) ------------------------


def test_the_packet_vocabulary_gives_every_code_a_status_and_one_raiser() -> None:
    declared = tuple(
        (entry.code, entry.status, entry.raiser)
        for entry in REVIEW_PACKET_VOCABULARY
    )

    assert declared == DECLARED_PACKET_VOCABULARY
    assert set(REVIEW_PACKET_CODES) == {
        code for code, _, _ in DECLARED_PACKET_VOCABULARY
    }
    assert UNCLASSIFIED_REFUSAL.code not in REVIEW_PACKET_CODES
    # The two statuses the packet module used to map by hand are unchanged.
    assert REVIEW_PACKET_CODES["missing_support"].status == UNSUPPORTED
    assert all(
        entry.status == REFUSED
        for entry in REVIEW_PACKET_VOCABULARY
        if entry.code != "missing_support"
    )


def test_every_refusal_the_packet_commands_raise_is_declared() -> None:
    """The packet, Follow-up Plan and reversal plpgsql, parsed with the runtime's expression."""

    # Two blocks of plpgsql, one family. #835's closure command acts on the
    # relation `review_packets` creates and raises the same token, so both are
    # parsed here rather than a third vocabulary being opened for six codes.
    source = "".join(
        Path(f"src/corridor/migrations/source_append_commands/{name}.py").read_text(
            encoding="utf-8"
        )
        for name in ("review_packets", "follow_up_plan_closure")
    )
    raised = set(REVIEW_PACKET_TOKEN.findall(source))

    assert raised == {
        entry.code
        for entry in REVIEW_PACKET_VOCABULARY
        if entry.raiser in (DATABASE_ONLY, BOTH)
    }
    # The two commands never share a token expression, so neither half can read
    # the other family's code as its own.
    assert not REFUSAL_TOKEN.findall(source)


def test_the_packet_declaration_names_which_half_raises_each_code() -> None:
    """A code declared BOTH is one the readable half writes itself; DATABASE_ONLY is not."""

    precheck = _packet_precheck_codes()

    assert {
        entry.code for entry in REVIEW_PACKET_VOCABULARY if entry.raiser == BOTH
    } <= precheck
    assert {
        entry.code
        for entry in REVIEW_PACKET_VOCABULARY
        if entry.raiser == DATABASE_ONLY
    }.isdisjoint(precheck)
    assert not any(
        entry.raiser == PYTHON_PRECHECK for entry in REVIEW_PACKET_VOCABULARY
    )


def test_a_packet_database_message_is_mapped_through_its_own_family() -> None:
    assert (
        database_refusal_code(
            "review_packet:later_act_depends a later decision already superseded "
            "a value this packet made effective"
        )
        == REVIEW_PACKET_CODES["later_act_depends"]
    )
    assert database_refusal_code("review_packet:missing_support ...").status == UNSUPPORTED
    assert database_refusal_code("review_packet:unheard_of ...") == UNCLASSIFIED_REFUSAL


# --- Refusals, before any authoritative write -----------------------------


def test_a_stale_accepted_revision_is_refused_before_any_write(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-f.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    stale_revision = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project, baseline_revision=stale_revision)

    # Someone else moved the accepted value while the coordinator was reading.
    other, other_segment = rendition.capture(
        fact_type="station_from", value="1180+00", cell="A11"
    )
    other_support = _support(session, project, other, other_segment)
    moved = _delta(session, project, proposed_value="1180+00", source_revision="rev-2")
    resolve_delta(
        session,
        _request(
            moved,
            action=ACCEPT,
            observed_accepted_revision_id=stale_revision,
            record_effects=(RecordEffect(fact_id=other.id),),
            support_assessment_ids=(other_support.id,),
        ),
    )
    revisions_before = _revision_count(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=stale_revision,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == STALE
    assert outcome.refusal.reason == "stale_accepted_revision"
    # Structured enough for the Work List to refresh without losing selections.
    assert outcome.refusal.current_accepted_revision_id > stale_revision
    assert outcome.refusal.delta_status == "open"
    assert outcome.refusal.subject_identity == SUBJECT
    assert outcome.refusal.field == "station_from"
    assert _revision_count(session, project) == revisions_before
    assert live_delta_status(session, delta.id) == "open"


def test_the_database_refuses_a_stale_accepted_revision_on_its_own(
    session: Session, project: Project
) -> None:
    """The Python check is the readable half; PostgreSQL is the authority."""

    rendition = _rendition(session, project, "ucm-rev-f2.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project, baseline_revision=baseline)
    validated = validate_child_decision(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )
    assert not isinstance(validated, Refusal)

    # The accepted value moves between validation and the write, exactly as a
    # second coordinator's Save would move it.
    other, other_segment = rendition.capture(
        fact_type="station_from", value="1180+00", cell="A11"
    )
    other_support = _support(session, project, other, other_segment)
    moved = _delta(session, project, proposed_value="1180+00", source_revision="rev-2")
    assert (
        resolve_delta(
            session,
            _request(
                moved,
                action=ACCEPT,
                observed_accepted_revision_id=baseline,
                record_effects=(RecordEffect(fact_id=other.id),),
                support_assessment_ids=(other_support.id,),
            ),
        ).status
        == RESOLVED
    )
    revisions_before = _revision_count(session, project)

    outcome = commit_child_decision(session, validated)

    assert outcome.status == STALE
    assert outcome.refusal.reason == "stale_accepted_revision"
    assert _revision_count(session, project) == revisions_before


def test_a_superseded_delta_is_refused(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-g.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    prior = _delta(session, project, source_revision="rev-1")
    newer = _delta(
        session, project, proposed_value="1210+00", source_revision="rev-2"
    )
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=prior.id,
        superseding_delta_id=newer.id,
    )

    outcome = resolve_delta(
        session,
        _request(
            prior,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == REFUSED
    assert outcome.refusal.reason == "superseded_delta"
    assert outcome.refusal.delta_status == "superseded"


def test_a_cross_project_delta_is_refused(session: Session, project: Project) -> None:
    other = Project(
        slug=f"resolve-delta-other-{uuid4().hex[:8]}",
        name="Other",
        is_synthetic=True,
    )
    session.add(other)
    session.flush()
    delta = _delta(session, project)

    outcome = resolve_delta(
        session, _request(delta, action=REJECT, project_id=other.id)
    )

    assert outcome.status == REFUSED
    assert outcome.refusal.reason == "cross_project_delta"


def test_an_invalid_action_is_refused(session: Session, project: Project) -> None:
    delta = _delta(session, project)

    outcome = resolve_delta(session, _request(delta, action="approve"))

    assert outcome.status == REFUSED
    assert outcome.refusal.reason == "invalid_action"


def test_a_delta_already_resolved_is_refused_and_corrected_by_a_later_decision(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-h.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project, baseline_revision=baseline)
    first = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )
    assert first.status == RESOLVED

    again = resolve_delta(
        session,
        _request(
            delta,
            action=REJECT,
            observed_accepted_revision_id=first.revision_id,
        ),
    )
    assert again.status == REFUSED
    assert again.refusal.reason == "already_resolved"

    # A wrong decision is corrected by a later attributable decision over a
    # newly proposed delta, never by editing the first one.
    correction_delta = _delta(
        session,
        project,
        accepted_value="1200+00",
        proposed_value="1149+00",
        source_revision="rev-correction",
    )
    correction_support = _support(
        session, project, accepted_fact, _segment_of(session, accepted_fact)
    )
    correction = resolve_delta(
        session,
        _request(
            correction_delta,
            action=ACCEPT,
            principal=BOB,
            observed_accepted_revision_id=first.revision_id,
            record_effects=(RecordEffect(fact_id=accepted_fact.id),),
            support_assessment_ids=(correction_support.id,),
        ),
    )
    assert correction.status == RESOLVED
    first_decision = session.get(DeltaRecordDecision, first.decision_id)
    assert first_decision.decided_by_principal == ALICE.subject
    assert first_decision.revision_id == first.revision_id
    superseded = session.get(FactDecision, first.fact_decision_ids[0])
    assert superseded.superseded_by == correction.fact_decision_ids[0]


def _segment_of(session: Session, fact: Fact) -> SourceSegment:
    return session.scalars(
        select(SourceSegment)
        .join(FactSource, FactSource.source_segment_id == SourceSegment.id)
        .where(FactSource.fact_id == fact.id)
    ).one()


# --- Support is never inferred from a locator -----------------------------


def test_a_decision_without_named_support_is_refused(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-i.xlsx")
    incoming, _ = rendition.capture(fact_type="station_from", value="1200+00")
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
        ),
    )

    assert outcome.status == UNSUPPORTED
    assert outcome.refusal.reason == "missing_support"


def test_a_valid_source_passage_check_never_substitutes_for_support(
    session: Session, project: Project
) -> None:
    """The quotation is exactly where it was cited, and that is not support."""

    rendition = _rendition(session, project, "ucm-rev-j.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    # The Source Passage Check's whole claim is that the stored digest still
    # certifies the stored words, and here it does.
    assert segment.content_sha256 == sha256(segment.exact_text.encode()).hexdigest()
    # This module never reads that check at all.
    assert "locator_validation" not in (
        Path("src/corridor/delta_resolution.py").read_text(encoding="utf-8")
    )
    # A context assessment is not value support either.
    context = _support(
        session, project, incoming, segment, evidence_role="context"
    )
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(context.id,),
        ),
    )

    assert outcome.status == UNSUPPORTED
    assert outcome.refusal.reason == "missing_support"


def test_a_contradicted_assessment_is_not_value_support(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-k.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    contradicted = _support(
        session, project, incoming, segment, assessment="contradicted"
    )
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(contradicted.id,),
        ),
    )

    assert outcome.status == UNSUPPORTED


# --- The constrained edit -------------------------------------------------


def test_an_edit_composes_supported_facts_under_a_named_transformation(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-l.xlsx")
    first, _ = rendition.capture(fact_type="station_from", value="1200+00")
    second, _ = rendition.capture(
        fact_type="station_to", value="1260+00", cell="B9"
    )
    composed, composed_segment = rendition.capture(
        fact_type="station_from", value="1200+00 1260+00", cell="C9"
    )
    assessment = _support(session, project, composed, composed_segment)
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=NamedComposition(
                transformation="concatenate_ordered_v1",
                input_fact_ids=(first.id, second.id),
                result_fact_id=composed.id,
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.edit_basis["transformation"] == "concatenate_ordered_v1"
    assert decision.edit_basis["input_fact_ids"] == [first.id, second.id]


def test_an_edit_whose_composition_does_not_replay_is_refused(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-m.xlsx")
    first, _ = rendition.capture(fact_type="station_from", value="1200+00")
    second, _ = rendition.capture(
        fact_type="station_to", value="1260+00", cell="B9"
    )
    wrong, wrong_segment = rendition.capture(
        fact_type="station_from", value="1200+00 to 1260+00", cell="C9"
    )
    assessment = _support(session, project, wrong, wrong_segment)
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=NamedComposition(
                transformation="concatenate_ordered_v1",
                input_fact_ids=(first.id, second.id),
                result_fact_id=wrong.id,
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == CONSTRAINED_EDIT
    assert "did not reproduce" in outcome.refusal.detail


def test_an_edit_may_perform_a_proven_lossless_normalization(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-n.xlsx")
    raw, _ = rendition.capture(
        fact_type="station_from", value="1200+00  to  1260+00"
    )
    normalized, normalized_segment = rendition.capture(
        fact_type="station_from", value="1200+00 to 1260+00", cell="A12"
    )
    # The captured values have to differ for the normalization to normalize
    # anything: a cell's released transformation already trims its ends, so
    # only internal spacing reaches a Fact for an edit to canonicalize.
    assert raw.text_value != normalized.text_value
    assessment = _support(session, project, normalized, normalized_segment)
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=LosslessNormalization(
                normalization="collapse_internal_whitespace_v1",
                input_fact_id=raw.id,
                result_fact_id=normalized.id,
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.edit_basis["normalization"] == "collapse_internal_whitespace_v1"


def test_a_normalization_that_loses_a_value_is_refused(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-o.xlsx")
    raw, _ = rendition.capture(fact_type="station_from", value="1200+00")
    different, different_segment = rendition.capture(
        fact_type="station_from", value="1300+00", cell="A12"
    )
    assessment = _support(session, project, different, different_segment)
    delta = _delta(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=LosslessNormalization(
                normalization="trim_whitespace_v1",
                input_fact_id=raw.id,
                result_fact_id=different.id,
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == CONSTRAINED_EDIT
    assert "value-preserving" in outcome.refusal.detail


def test_an_edit_may_cite_a_separately_attributable_source_origin(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-p.xlsx")
    verbal_rendition = _rendition(session, project, "call-notes.pdf")
    stated, stated_segment = verbal_rendition.capture(
        fact_type="station_from", value="1212+00"
    )
    assessment = _support(session, project, stated, stated_segment)
    delta = _delta(session, project, document_id=rendition.document.id)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=SeparateSourceOrigin(
                origin="recorded_verbal_statement", fact_id=stated.id
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.edit_basis["origin"] == "recorded_verbal_statement"


@pytest.mark.parametrize(
    "field_name",
    ["external_org", "committed_date", "need_date", "closure_result"],
)
def test_free_text_for_an_external_fact_is_coordination_needed(
    session: Session, project: Project, field_name: str
) -> None:
    delta = _delta(session, project, field=field_name, proposed_value="CenterPoint")
    before = _revision_count(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=EDIT,
            edit_basis=FreeText(text="CenterPoint Energy (per phone call)"),
        ),
    )

    assert outcome.status == COORDINATION_NEEDED
    assert outcome.refusal.open_question
    assert field_name in outcome.refusal.open_question
    assert outcome.refusal.proposed_value == "CenterPoint"
    # The proposed value stays unaccepted and the delta stays open.
    assert _revision_count(session, project) == before
    assert live_delta_status(session, delta.id) == "open"


def test_free_text_on_any_other_field_is_a_constrained_edit_refusal(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="notes", proposed_value="see sheet 2")

    outcome = resolve_delta(
        session,
        _request(delta, action=EDIT, edit_basis=FreeText(text="looks about right")),
    )

    assert outcome.status == CONSTRAINED_EDIT
    assert outcome.refusal.reason == "unsupported_free_text"


def test_an_edit_without_any_basis_is_refused(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="notes")

    outcome = resolve_delta(session, _request(delta, action=EDIT))

    assert outcome.status == CONSTRAINED_EDIT


# --- One revision, standalone; none, inside a packet ----------------------


def test_a_standalone_resolution_writes_exactly_one_revision(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-q.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    before = _revision_count(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    assert _revision_count(session, project) == before + 1
    decisions = session.scalars(
        select(DeltaRecordDecision).where(
            DeltaRecordDecision.revision_id == outcome.revision_id
        )
    ).all()
    assert len(decisions) == 1


def test_a_packet_child_contributes_to_one_revision_and_creates_none(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-r.xlsx")
    first, first_segment = rendition.capture(
        fact_type="station_from", value="1200+00", subject_key="Utility Conflicts!7"
    )
    second, second_segment = rendition.capture(
        fact_type="station_to",
        value="1260+00",
        subject_key="Utility Conflicts!7",
        cell="B9",
    )
    first_support = _support(session, project, first, first_segment)
    second_support = _support(session, project, second, second_segment)
    first_delta = _delta(session, project, field="station_from")
    second_delta = _delta(
        session,
        project,
        field="station_to",
        accepted_value="1250+00",
        proposed_value="1260+00",
    )
    before = _revision_count(session, project)

    packet_revision = open_resolution_revision(
        session,
        project_id=project.id,
        principal=ALICE,
        idempotency_key=f"packet:{uuid4().hex[:10]}",
    )
    outcomes = [
        resolve_delta(
            session,
            _request(
                delta,
                action=ACCEPT,
                record_effects=(RecordEffect(fact_id=fact.id),),
                support_assessment_ids=(support.id,),
            ),
            revision_id=packet_revision,
        )
        for delta, fact, support in (
            (first_delta, first, first_support),
            (second_delta, second, second_support),
        )
    ]

    assert [outcome.status for outcome in outcomes] == [RESOLVED, RESOLVED]
    assert {outcome.revision_id for outcome in outcomes} == {packet_revision}
    assert all(outcome.packet_owned for outcome in outcomes)
    assert all(not outcome.wrote_revision for outcome in outcomes)
    # One revision for the whole packet, and no intermediate revisions.
    assert _revision_count(session, project) == before + 1
    # Each child keeps its own identity inside that one revision.
    decisions = session.scalars(
        select(DeltaRecordDecision)
        .where(DeltaRecordDecision.revision_id == packet_revision)
        .order_by(DeltaRecordDecision.id)
    ).all()
    assert [decision.delta_id for decision in decisions] == [
        first_delta.id,
        second_delta.id,
    ]
    assert len({decision.disposition_id for decision in decisions}) == 2


def test_a_packet_of_deferrals_alone_writes_no_revision(
    session: Session, project: Project
) -> None:
    first_delta = _delta(session, project, field="station_from")
    second_delta = _delta(
        session, project, field="station_to", proposed_value="1260+00"
    )
    before = _revision_count(session, project)

    for delta in (first_delta, second_delta):
        outcome = defer_delta(
            session,
            _request(delta, action=DEFER, wake_condition="the utility replies"),
        )
        assert outcome.status == DEFERRED

    assert _revision_count(session, project) == before


def test_standalone_and_packet_children_are_semantically_identical(
    session: Session, project: Project
) -> None:
    """The same input validates to the same decision content in both contexts."""

    rendition = _rendition(session, project, "ucm-rev-s.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    request = _request(
        delta,
        action=ACCEPT,
        record_effects=(RecordEffect(fact_id=incoming.id),),
        support_assessment_ids=(assessment.id,),
    )

    standalone = validate_child_decision(session, request)
    packet_child = validate_child_decision(session, request)

    assert standalone == packet_child
    # And the only difference at commit time is who owns the revision.
    packet_revision = open_resolution_revision(
        session,
        project_id=project.id,
        principal=ALICE,
        idempotency_key=f"packet:{uuid4().hex[:10]}",
    )
    committed = commit_child_decision(
        session, packet_child, revision_id=packet_revision
    )
    assert committed.revision_id == packet_revision
    assert committed.packet_owned


# --- Typed effects --------------------------------------------------------


def test_a_new_subject_creates_its_subject_and_initial_decisions_atomically(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-t.xlsx")
    subject = "Utility Conflicts!99"
    owner, owner_segment = rendition.capture(
        fact_type="external_org", value="CenterPoint", subject_key=subject
    )
    kind, kind_segment = rendition.capture(
        fact_type="utility_type", value="Gas", subject_key=subject, cell="B99"
    )
    owner_support = _support(session, project, owner, owner_segment)
    kind_support = _support(session, project, kind, kind_segment)
    (delta,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="ucm-workbook",
        source_revision="rev-new-subject",
        document_id=rendition.document.id,
        deltas=[
            ProposedDeltaValues(
                change_type="add",
                target=ProposedSubjectTarget(
                    subject_identity=subject,
                    proposed_fields=("external_org", "utility_type"),
                ),
                proposed_value={"external_org": "CenterPoint", "utility_type": "Gas"},
            )
        ],
    )
    assert delta_effect_kind(delta) == "new_subject"
    before = _revision_count(session, project)

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(
                RecordEffect(fact_id=owner.id),
                RecordEffect(fact_id=kind.id),
            ),
            support_assessment_ids=(owner_support.id, kind_support.id),
        ),
    )

    assert outcome.status == RESOLVED
    assert outcome.effect_kind == "new_subject"
    assert len(outcome.fact_decision_ids) == 2
    assert _revision_count(session, project) == before + 1
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.subject_key == subject,
            FactDecision.superseded_by.is_(None),
        )
    ).all()
    assert {decision.fact_type for decision in effective} == {
        "external_org",
        "utility_type",
    }
    assert {decision.revision_id for decision in effective} == {outcome.revision_id}


def test_a_timing_change_preserves_its_predecessor_lineage(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-u.xlsx")
    promised, _ = rendition.capture(
        fact_type="committed_date", value="2026-10-01",)
    baseline = _adopt(session, project, promised, f"adopt-{project.id}")
    moved, moved_segment = rendition.capture(
        fact_type="committed_date",
        value="2026-11-15",
        cell="D9",
    )
    assessment = _support(session, project, moved, moved_segment)
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-10-01",
        proposed_value="2026-11-15",
        baseline_revision=baseline,
    )
    assert delta_effect_kind(delta) == "timing"

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=moved.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.effect_kind == "timing"
    predecessor = session.scalars(
        select(FactDecision).where(
            FactDecision.fact_id == promised.id,
        )
    ).one()
    # The predecessor is retired, not deleted: its row, its revision, and its
    # Source Fact all remain readable.
    assert predecessor.superseded_by == outcome.fact_decision_ids[0]
    assert predecessor.revision_id == baseline
    assert session.get(Fact, promised.id) is not None


@pytest.mark.parametrize(
    "change_kind", ["correction", "changed_ownership"]
)
def test_an_organization_change_says_which_kind_it_was(
    session: Session, project: Project, change_kind: str
) -> None:
    rendition = _rendition(session, project, f"ucm-rev-v-{change_kind}.xlsx")
    incoming, segment = rendition.capture(
        fact_type="external_org", value="CenterPoint Energy"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(
        session,
        project,
        field="external_org",
        accepted_value="CenterPoint",
        proposed_value="CenterPoint Energy",
    )
    assert delta_effect_kind(delta) == "organization"

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            organization_change_kind=change_kind,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    assert decision.effect_kind == "organization"
    assert decision.organization_change_kind == change_kind


def test_an_organization_change_without_its_kind_is_refused(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-w.xlsx")
    incoming, segment = rendition.capture(
        fact_type="external_org", value="CenterPoint Energy"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(
        session,
        project,
        field="external_org",
        accepted_value="CenterPoint",
        proposed_value="CenterPoint Energy",
    )

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == REFUSED
    assert outcome.refusal.reason == "organization_change_kind_required"


def test_an_apparent_removal_records_disposition_without_deleting_anything(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-x.xlsx")
    accepted_fact, accepted_segment = rendition.capture(
        fact_type="station_from", value="1149+00"
    )
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    assessment = _support(session, project, accepted_fact, accepted_segment)
    delta = _delta(
        session,
        project,
        change_type="apparent_removal",
        accepted_value="1149+00",
        proposed_value=None,
        baseline_revision=baseline,
    )
    assert delta_effect_kind(delta) == "apparent_removal"

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(
                RecordEffect(fact_id=accepted_fact.id, disposition="do_not_add"),
            ),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    assert outcome.effect_kind == "apparent_removal"
    # Nothing is deleted: the Source Fact, the delta, the support, and the
    # predecessor decision all remain, and the new decision states the
    # disposition the record now takes.
    assert session.get(Fact, accepted_fact.id) is not None
    assert session.get(ProposedDelta, delta.id) is not None
    assert session.get(SupportAssessment, assessment.id) is not None
    effective = session.get(FactDecision, outcome.fact_decision_ids[0])
    assert effective.disposition == "do_not_add"
    predecessor = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.revision_id == baseline,
        )
    ).one()
    assert predecessor.superseded_by == effective.id


def test_a_contradiction_retains_every_proposition(
    session: Session, project: Project
) -> None:
    workbook = _rendition(session, project, "ucm-rev-y.xlsx")
    email = _rendition(session, project, "utility-email.pdf")
    from_workbook, _ = workbook.capture(fact_type="station_from", value="1200+00")
    from_email, email_segment = email.capture(
        fact_type="station_from", value="1180+00"
    )
    assessment = _support(session, project, from_email, email_segment)
    delta = _delta(
        session,
        project,
        accepted_value="1200+00",
        proposed_value="1180+00",
        document_id=email.document.id,
    )

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            contradiction=True,
            record_effects=(RecordEffect(fact_id=from_email.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    assert outcome.effect_kind == "contradiction"
    # Both propositions survive the resolution.
    assert session.get(Fact, from_workbook.id) is not None
    assert session.get(Fact, from_email.id) is not None
    kept = session.get(ProposedDelta, delta.id)
    assert kept.accepted_value == "1200+00"
    assert kept.proposed_value == "1180+00"


def test_a_schedule_key_date_creates_the_required_by_decision(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "schedule-rev-b.xlsx")
    required_by, segment = rendition.capture(
        fact_type="need_date", value="2027-03-01",)
    assessment = _support(session, project, required_by, segment)
    delta = _delta(
        session,
        project,
        field="need_date",
        accepted_value="2027-01-15",
        proposed_value="2027-03-01",
    )
    assert delta_effect_kind(delta) == "schedule_key_date"

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=required_by.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.effect_kind == "schedule_key_date"
    effective = session.scalars(
        select(FactDecision).where(
            FactDecision.project_id == project.id,
            FactDecision.fact_type == "need_date",
            FactDecision.superseded_by.is_(None),
        )
    ).one()
    assert effective.fact_id == required_by.id


def test_a_closure_records_the_exact_supported_outcome_and_scope(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "closure-letter.pdf")
    marked, segment = rendition.capture(
        fact_type="marked_resolution", value="Relocated 2026-08-30"
    )
    assessment = _support(session, project, marked, segment)
    scope = _support(
        session, project, marked, segment, evidence_role="scope", at=ASSESSED_AT
    )
    delta = _delta(
        session,
        project,
        field="marked_resolution",
        accepted_value=None,
        proposed_value="Relocated 2026-08-30",
        document_id=rendition.document.id,
    )
    assert delta_effect_kind(delta) == "closure"

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=marked.id),),
            support_assessment_ids=(assessment.id, scope.id),
            effective_value={"outcome": "Relocated 2026-08-30"},
        ),
    )

    assert outcome.effect_kind == "closure"
    decision = session.get(DeltaRecordDecision, outcome.decision_id)
    cited = cited_support_assessments(session, decision)
    assert {row.evidence_role for row in cited} == {"value_support", "scope"}
    disposition = session.get(DeltaDisposition, outcome.disposition_id)
    assert disposition.effective_value == {"outcome": "Relocated 2026-08-30"}


# --- Authority ------------------------------------------------------------


def test_a_delta_decision_cannot_be_written_outside_the_command(
    session: Session, project: Project
) -> None:
    """Even the schema owner writes a delta decision only through the command."""

    delta = _delta(session, project)
    with pytest.raises(DBAPIError) as raised:
        session.execute(
            text(
                "insert into delta_record_decisions ("
                "project_id, delta_id, disposition_id, revision_id, disposition,"
                " effect_kind, decided_by_principal, idempotency_key, decided_at"
                ") values (:project_id, :delta_id, 1, 1, 'accept', 'changed_field',"
                " 'local:alice', 'raw', now())"
            ),
            {"project_id": project.id, "delta_id": delta.id},
        )
    assert "requires the typed decision command" in str(raised.value)
    session.rollback()


def test_a_delta_decision_cannot_be_updated_or_deleted(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-z.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )
    assert outcome.status == RESOLVED

    # The schema owner reaches the guard: only the command writes a decision.
    with pytest.raises(DBAPIError) as owner_write:
        with session.begin_nested():
            session.execute(
                text(
                    "update delta_record_decisions set disposition = 'reject' "
                    "where id = :id"
                ),
                {"id": outcome.decision_id},
            )
    assert "requires the typed decision command" in str(owner_write.value)

    # The decision role itself holds no update or delete at all, so the
    # append-only rule is a privilege as well as a trigger.
    for statement in (
        "update delta_record_decisions set disposition = 'reject' where id = :id",
        "delete from delta_record_decisions where id = :id",
    ):
        with as_role(session, RECORD_DECISION_ROLE):
            with pytest.raises(DBAPIError) as role_write:
                with session.begin_nested():
                    session.execute(text(statement), {"id": outcome.decision_id})
        assert "permission denied" in str(role_write.value)


def test_a_replay_of_the_same_act_returns_what_it_already_wrote(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-aa.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    request = _request(
        delta,
        action=ACCEPT,
        record_effects=(RecordEffect(fact_id=incoming.id),),
        support_assessment_ids=(assessment.id,),
    )
    first = resolve_delta(session, request)
    assert first.created

    validated = validate_child_decision(session, request)
    # Validation now refuses, because the delta is resolved; the command
    # itself still converges on the row a replay already wrote.
    assert isinstance(validated, Refusal)
    replay = commit_child_decision(
        session,
        _forced_replay(request, first),
    )
    assert replay.status == RESOLVED
    assert not replay.created
    assert replay.revision_id == first.revision_id
    assert replay.decision_id == first.decision_id


def _forced_replay(request, first):
    from corridor.delta_resolution import ValidatedChildDecision

    return ValidatedChildDecision(
        project_id=request.project_id,
        delta_id=request.delta_id,
        disposition=request.action,
        effect_kind="changed_field",
        organization_change_kind=None,
        principal_subject=ALICE.subject,
        idempotency_key=request.idempotency_key,
        observed_accepted_revision_id=None,
        decided_at=DECIDED_AT,
        effective_value=None,
        rationale=None,
        edit_basis=None,
        record_effects=(
            {"fact_id": effect.fact_id, "disposition": effect.disposition}
            for effect in request.record_effects
        ),
        support_assessment_ids=tuple(request.support_assessment_ids),
    )


# --- The versioned event contract (#558) ----------------------------------


def test_every_decision_and_a_stale_refusal_emit_versioned_events(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-bb.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    accepted_delta = _delta(session, project, baseline_revision=baseline)
    stale_delta = _delta(
        session, project, proposed_value="1210+00", source_revision="rev-2"
    )
    rejected_delta = _delta(
        session,
        project,
        field="notes",
        accepted_value="none",
        proposed_value="see sheet 2",
        source_revision="rev-3",
    )

    with capture_events() as collected:
        first = resolve_delta(
            session,
            _request(
                accepted_delta,
                action=ACCEPT,
                observed_accepted_revision_id=baseline,
                record_effects=(RecordEffect(fact_id=incoming.id),),
                support_assessment_ids=(assessment.id,),
            ),
        )
        stale = resolve_delta(
            session,
            _request(
                stale_delta,
                action=ACCEPT,
                observed_accepted_revision_id=baseline,
                record_effects=(RecordEffect(fact_id=incoming.id),),
                support_assessment_ids=(assessment.id,),
            ),
        )
        rejected = resolve_delta(
            session,
            _request(
                rejected_delta,
                action=REJECT,
                observed_accepted_revision_id=first.revision_id,
            ),
        )
        deferred = defer_delta(
            session,
            _request(
                _delta(
                    session,
                    project,
                    field="utility_type",
                    accepted_value="Gas",
                    proposed_value="Electric",
                    source_revision="rev-4",
                ),
                action=DEFER,
                wake_condition="the utility replies",
            ),
        )

    assert first.status == RESOLVED
    assert stale.status == STALE
    assert rejected.status == RESOLVED
    assert deferred.status == DEFERRED

    events = collected.by_family(EventFamily.CHILD_DECISION)
    assert len(events) == 4
    outcomes = [event.payload["outcome"] for event in events]
    assert outcomes == [RESOLVED, STALE, RESOLVED, DEFERRED]
    for event in events:
        assert event.version
        binding = event.binding
        assert binding.code_revision
        assert binding.product_revision
        assert binding.packetizer_rules_version
        assert binding.template_identity
        assert binding.mapping_identity
        assert isinstance(binding.enabled_feature_flags, tuple)
        assert binding.source_configuration == {}
        assert binding.connector_configuration == {}
        # Identity never reaches a metric label.
        assert set(event.metric_labels) == {"action", "outcome", "refusal_reason"}
    assert events[1].payload["refusal_reason"] == "stale_accepted_revision"
    assert events[0].occurred_at == DECIDED_AT


# --- Reading helpers ------------------------------------------------------


def test_incoming_source_facts_finds_the_deltas_exact_subject_and_field(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-cc.xlsx")
    wanted, _ = rendition.capture(fact_type="station_from", value="1200+00")
    rendition.capture(fact_type="station_to", value="1260+00", cell="B9")
    rendition.capture(
        fact_type="station_from",
        value="1300+00",
        subject_key="Utility Conflicts!8",
        cell="A20",
    )
    delta = _delta(session, project)

    assert [fact.id for fact in incoming_source_facts(session, delta)] == [wanted.id]


def test_every_structured_result_carries_what_a_reading_needs_to_refresh(
    session: Session, project: Project
) -> None:
    """Stale, support, constrained-edit, and coordination-needed all refresh.

    None of them carries anything the coordinator typed, so a Work List that
    re-reads on one of these never has to discard an unsaved selection.
    """

    rendition = _rendition(session, project, "ucm-rev-dd.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)

    unsupported = resolve_delta(
        session,
        _request(
            _delta(session, project, baseline_revision=baseline),
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
        ),
    )
    constrained = resolve_delta(
        session,
        _request(
            _delta(
                session,
                project,
                field="notes",
                accepted_value="none",
                proposed_value="see sheet 2",
                source_revision="rev-notes",
            ),
            action=EDIT,
            edit_basis=FreeText(text="looks fine"),
        ),
    )
    coordination = resolve_delta(
        session,
        _request(
            _delta(
                session,
                project,
                field="external_org",
                accepted_value="CenterPoint",
                proposed_value="CenterPoint Energy",
                source_revision="rev-org",
            ),
            action=EDIT,
            edit_basis=FreeText(text="CenterPoint Energy, per phone call"),
        ),
    )
    # Move the record so the fourth reading is genuinely stale.
    moved, moved_segment = rendition.capture(
        fact_type="station_from", value="1180+00", cell="A11"
    )
    moved_support = _support(session, project, moved, moved_segment)
    resolve_delta(
        session,
        _request(
            _delta(
                session, project, proposed_value="1180+00", source_revision="rev-moved"
            ),
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=moved.id),),
            support_assessment_ids=(moved_support.id,),
        ),
    )
    stale = resolve_delta(
        session,
        _request(
            _delta(
                session,
                project,
                proposed_value="1200+00",
                source_revision="rev-stale",
            ),
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert unsupported.status == UNSUPPORTED
    assert constrained.status == CONSTRAINED_EDIT
    assert coordination.status == COORDINATION_NEEDED
    assert stale.status == STALE
    for outcome in (unsupported, constrained, coordination, stale):
        refusal = outcome.refusal
        assert refusal.delta_id
        assert refusal.subject_identity
        assert refusal.field
        assert refusal.delta_status == "open"
        assert refusal.detail
    # Where an accepted value stands, the reading is told which revision it
    # stands on; where none does, it is told that too.
    assert unsupported.refusal.current_accepted_revision_id == baseline
    assert stale.refusal.current_accepted_revision_id > baseline
    assert constrained.refusal.current_accepted_revision_id is None
    # Only the coordination result carries a question for a Follow-up Plan.
    assert coordination.refusal.open_question
    assert stale.refusal.open_question is None


def test_nothing_a_resolution_touches_is_ever_updated_or_deleted(
    session: Session, project: Project
) -> None:
    rendition = _rendition(session, project, "ucm-rev-ee.xlsx")
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00"
    )
    assessment = _support(session, project, incoming, segment)
    delta = _delta(session, project)
    fact_digest = incoming.content_sha256
    delta_digest = delta.content_sha256
    assessment_digest = assessment.content_sha256

    outcome = resolve_delta(
        session,
        _request(
            delta,
            action=ACCEPT,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )

    assert outcome.status == RESOLVED
    session.expire_all()
    assert session.get(Fact, incoming.id).content_sha256 == fact_digest
    assert session.get(ProposedDelta, delta.id).content_sha256 == delta_digest
    reread = session.get(SupportAssessment, assessment.id)
    assert reread.content_sha256 == assessment_digest
    assert reread.superseded_by is None
    # The citation is its own row, so the assessment itself is untouched.
    cited = session.scalars(
        select(DeltaDecisionSupport).where(
            DeltaDecisionSupport.decision_id == outcome.decision_id
        )
    ).all()
    assert [row.support_assessment_id for row in cited] == [assessment.id]
    assert [row.ordinal for row in cited] == [1]


def test_a_rejected_value_recurring_from_a_newer_source_is_a_new_open_delta(
    session: Session, project: Project
) -> None:
    """Reject settles this occurrence; recurrence is #518's own lifecycle."""

    rendition = _rendition(session, project, "ucm-rev-ff.xlsx")
    accepted_fact, _ = rendition.capture(fact_type="station_from", value="1149+00")
    baseline = _adopt(session, project, accepted_fact, f"adopt-{project.id}")
    rejected = _delta(session, project, baseline_revision=baseline)
    assert (
        resolve_delta(
            session,
            _request(
                rejected,
                action=REJECT,
                observed_accepted_revision_id=baseline,
                rationale="the workbook row was keyed to the wrong conflict",
            ),
        ).status
        == RESOLVED
    )

    # A newer source revision says the same thing again.
    recurred = _delta(session, project, source_revision="rev-2")

    assert recurred.id != rejected.id
    assert live_delta_status(session, rejected.id) == "resolved"
    assert live_delta_status(session, recurred.id) == "open"
    incoming, segment = rendition.capture(
        fact_type="station_from", value="1200+00", cell="A9"
    )
    assessment = _support(session, project, incoming, segment)
    outcome = resolve_delta(
        session,
        _request(
            recurred,
            action=ACCEPT,
            observed_accepted_revision_id=baseline,
            record_effects=(RecordEffect(fact_id=incoming.id),),
            support_assessment_ids=(assessment.id,),
        ),
    )
    assert outcome.status == RESOLVED
