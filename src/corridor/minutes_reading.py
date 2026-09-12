"""Derive source clarification and scope context from retained minutes readings.

This is a reading, not another queue or decision lifecycle. The latest captured
revision of each declared source family supplies its outstanding source questions;
Proposed Delta disposition and supersession remain owned by their existing layer.

A question a coordinator has already disposed of (#833) drops out here: the
reading derives from the immutable capture outcome **and** its effective
`minutes_question_dispositions`, so a resolved, interpreted, excluded or
clarified question stops being counted as waiting on the coordinator's
judgement without the capture ever being rewritten.
"""

from dataclasses import dataclass
from sqlalchemy import select
from corridor.minutes_question_disposition import (
    PRIMARY_ROW,
    RESOLUTION_DIMENSION,
    permitted_actions,
    question_identity,
)
from corridor.models import Document, MinutesCapture, MinutesQuestionDisposition, SourceSegment
from corridor.statement_values import UNKNOWN_SCOPE_LABEL


REASONS = {
    "attribution_unresolved": "Stated By needs clarification",
    "person_attribution_unresolved": "The named speaker needs clarification",
    "predecessor_unresolved": "The earlier Commitment is not yet identified",
    "new_timing_unresolved": "Promised For needs clarification",
    "required_by_is_not_promised_timing": "Required By does not state the party's Promised Timing",
    "completion_not_explicit": "The source does not clearly report completion",
    "scope_unresolved": "Applies To needs clarification",
    "source_statement_unresolved": "Statement needs clarification",
    "unproposed_source_statement": "Statement needs review",
    "new_commitment_has_predecessor": "The Commitment identity needs clarification",
}


@dataclass(frozen=True)
class MinutesQuestion:
    document_id: int
    filename: str
    segment_id: int
    page_no: int | None
    text: str
    reasons: tuple[str, ...]
    #: The identity of the capture revision this question was read from, and the
    #: raw reason codes, so a disposition can be bound to the exact question (#833).
    capture_id: int = 0
    source_family: str = ""
    question_id: str = ""
    reason_codes: tuple[str, ...] = ()
    #: What the rendered page was decided against; the disposition command
    #: refuses a stale or concurrent submission that names another.
    decision_generation: int = 0
    #: The matrix row and the primary permitted action for this question.
    matrix_row: int = 4
    permitted_actions: tuple[str, ...] = ()
    #: For a Row-2 question, which resolution control the form presents.
    resolution_dimension: str | None = None


def read_minutes_work(session, *, project_id, as_of):
    latest = {}
    for row in session.scalars(select(MinutesCapture).where(MinutesCapture.project_id == project_id,
        MinutesCapture.recorded_at <= as_of).order_by(MinutesCapture.id)):
        latest[row.source_family] = row
    effective, chain_length = {}, {}
    for disposition in session.scalars(select(MinutesQuestionDisposition).where(
            MinutesQuestionDisposition.project_id == project_id).order_by(
            MinutesQuestionDisposition.decision_generation)):
        key = (disposition.source_family, disposition.question_identity)
        effective[key] = disposition
        chain_length[key] = chain_length.get(key, 0) + 1
    questions, contexts = [], {}
    for row in latest.values():
        document = session.get_one(Document, row.document_id)
        for outcome in row.output_json["outcomes"]:
            segment = session.get_one(SourceSegment, outcome["segment_id"])
            if outcome["status"] == "unresolved":
                reason_codes = tuple(outcome["reasons"])
                identity = question_identity(row.source_family, segment.exact_text, reason_codes)
                key = (row.source_family, identity)
                # A recorded disposition (resolve, interpret, exclude or clarify)
                # settles or retains the question; either way it stops waiting on
                # the coordinator, so it drops out of the reading (#833).
                if key not in effective:
                    primary = reason_codes[0] if reason_codes else "source_statement_unresolved"
                    questions.append(MinutesQuestion(document.id, document.filename, segment.id, segment.page_no,
                        segment.exact_text, tuple(REASONS.get(reason, "Statement needs review") for reason in reason_codes),
                        capture_id=row.id, source_family=row.source_family, question_id=identity,
                        reason_codes=reason_codes, decision_generation=chain_length.get(key, 0),
                        matrix_row=PRIMARY_ROW.get(primary, 4), permitted_actions=permitted_actions(primary),
                        resolution_dimension=RESOLUTION_DIMENSION.get(primary)))
            for delta_id in outcome["delta_ids"]:
                label = segment.exact_text.split(":", 1)[0]
                contexts[delta_id] = {
                    "subject_name": f"Statement from {label}",
                    "scope_attention": (UNKNOWN_SCOPE_LABEL,) if outcome["scope_state"] == "unknown" else (),
                }
    return tuple(questions), contexts


@dataclass(frozen=True)
class ClarifiedQuestion:
    """A question retained as a named clarification request (#833, Row 4).

    It no longer waits on the coordinator's own judgement, so it is not counted
    with the open questions; it stays readable here because the ending #833
    asks for is to *retain* the question, not to make it disappear.
    """

    filename: str
    page_no: int | None
    text: str
    reasons: tuple[str, ...]
    clarification_question: str
    responsible_party: str
    requested_by: str


def read_minutes_clarifications(session, *, project_id, as_of):
    """The retained clarification requests standing against the latest reading."""

    latest = {}
    for row in session.scalars(select(MinutesCapture).where(MinutesCapture.project_id == project_id,
        MinutesCapture.recorded_at <= as_of).order_by(MinutesCapture.id)):
        latest[row.source_family] = row
    effective = {}
    for disposition in session.scalars(select(MinutesQuestionDisposition).where(
            MinutesQuestionDisposition.project_id == project_id).order_by(
            MinutesQuestionDisposition.decision_generation)):
        effective[(disposition.source_family, disposition.question_identity)] = disposition
    clarified = []
    for row in latest.values():
        document = session.get_one(Document, row.document_id)
        for outcome in row.output_json["outcomes"]:
            if outcome["status"] != "unresolved":
                continue
            segment = session.get_one(SourceSegment, outcome["segment_id"])
            identity = question_identity(row.source_family, segment.exact_text, tuple(outcome["reasons"]))
            disposition = effective.get((row.source_family, identity))
            if disposition is not None and disposition.disposition == "clarify":
                clarified.append(ClarifiedQuestion(
                    document.filename, segment.page_no, segment.exact_text,
                    tuple(REASONS.get(reason, "Statement needs review") for reason in outcome["reasons"]),
                    disposition.detail.get("clarification_question", ""),
                    disposition.detail.get("responsible_party", ""),
                    disposition.decided_by))
    return tuple(clarified)
