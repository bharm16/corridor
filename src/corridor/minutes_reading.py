"""Derive source clarification and scope context from retained minutes readings.

This is a reading, not another queue or decision lifecycle. The latest captured
revision of each declared source family supplies its outstanding source questions;
Proposed Delta disposition and supersession remain owned by their existing layer.
"""

from dataclasses import dataclass
from sqlalchemy import select
from corridor.models import Document, MinutesCapture, SourceSegment


REASONS = {
    "attribution_unresolved": "Stated By needs clarification",
    "person_attribution_unresolved": "The named speaker needs clarification",
    "predecessor_unresolved": "The earlier Commitment is not yet identified",
    "new_timing_unresolved": "Promised For needs clarification",
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


def read_minutes_work(session, *, project_id, as_of):
    latest = {}
    for row in session.scalars(select(MinutesCapture).where(MinutesCapture.project_id == project_id,
        MinutesCapture.recorded_at <= as_of).order_by(MinutesCapture.id)):
        latest[row.source_family] = row
    questions, contexts = [], {}
    for row in latest.values():
        document = session.get_one(Document, row.document_id)
        for outcome in row.output_json["outcomes"]:
            segment = session.get_one(SourceSegment, outcome["segment_id"])
            if outcome["status"] == "unresolved":
                questions.append(MinutesQuestion(document.id, document.filename, segment.id, segment.page_no,
                    segment.exact_text, tuple(REASONS.get(reason, "Statement needs review") for reason in outcome["reasons"])))
            for delta_id in outcome["delta_ids"]:
                label = segment.exact_text.split(":", 1)[0]
                contexts[delta_id] = {
                    "subject_name": f"Statement from {label}",
                    "scope_attention": ("Applies To: not yet known",) if outcome["scope_state"] == "unknown" else (),
                }
    return tuple(questions), contexts
