"""Read typed Fact values without pretending their empty scalar columns are values."""

from sqlalchemy import select
from corridor.models import FactAppliesTo, FactClosureResult, FactStatementTiming


def source_fact_value(session, fact):
    if fact.fact_type == "statement_timing":
        rows = session.scalars(select(FactStatementTiming).where(FactStatementTiming.fact_id == fact.id)
                               .order_by(FactStatementTiming.timing_role)).all()
        return {"timings": [{"role": row.timing_role, "text": row.text, "precision": row.precision,
            "start_date": row.start_date.isoformat() if row.start_date else None,
            "end_date": row.end_date.isoformat() if row.end_date else None} for row in rows]}
    if fact.fact_type == "applies_to":
        rows = session.scalars(select(FactAppliesTo).where(FactAppliesTo.fact_id == fact.id).order_by(FactAppliesTo.ordinal)).all()
        if any(row.dependency_id is not None for row in rows):
            return {"dependency_ids": [row.dependency_id for row in rows]}
        return {"mode": "selected" if rows else "unknown", "subject_keys": [row.record_subject_key for row in rows]}
    if fact.fact_type == "closure_result":
        closure = session.scalar(select(FactClosureResult).where(FactClosureResult.fact_id == fact.id))
        return {"closure_kind": closure.closure_kind if closure else None}
    if fact.text_value is not None:
        return fact.text_value
    if fact.date_value is not None:
        return fact.date_value.isoformat()
    if fact.external_org_value_id is not None:
        return {"external_org_id": fact.external_org_value_id}
    if fact.document_value_id is not None:
        return {"document_id": fact.document_value_id}
    return None
