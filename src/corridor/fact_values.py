"""One reading of what a typed source Fact says (#790 follow-up, ADR-0067).

ADR-0067 makes a Fact one typed row, so "what value does this Fact carry" has
exactly one answer.  It had five.  ``source_fact_values.source_fact_value``
read the satellites through a session; ``delta_generation._typed_value`` read
the scalar columns only and returned ``None`` for every satellite type, which
is the copy the most modules imported; ``record_projection`` re-decoded the
same satellites onto ``CurrentRecordValue`` and answered a third shape; and
``delta_generation.accepted_values`` hand-wrote a fourth select for the scalar
half.  Three return shapes for one question meant a satellite Fact could be
compared against an accepted value as ``None`` on one path and as a typed
payload on another, and nothing in the codebase said which was the contract.

So the contract lives here, as one function over the value's *components*
rather than over any one row class.  ``typed_fact_value`` is pure: it takes the
scalar columns and the already-loaded satellite members, so the same rule
serves a captured ``Fact`` row (``read_fact_value``) and a projected
``CurrentRecordValue`` (``record_projection.record_value_payload``) without
either one re-deciding the shape.  Splitting it the other way was tried first
— a single session-bound reader that both paths called — and it forced the
projection, which has already loaded every satellite in one batched query, to
go back to the database once per row.

**One return shape per Fact type**, and the database guarantees the branches
are exclusive (``ck_facts_typed_value`` admits exactly one value column per
type):

* ``statement_timing`` -> ``{"timings": [{"role", "text", "precision",
  "start_date", "end_date"}]}``, ordered by role, dates ISO or ``None``.
* ``applies_to`` -> ``{"mode": "selected" | "unknown", "subject_keys": [...]}``.
  A legacy ``dependency_id`` member is a compatibility *identity*, not a value:
  the consumers that need it read ``CurrentRecordValue.applies_to_dependency_ids``
  or ``FactAppliesTo.dependency_id`` directly, and it stays out of the payload
  so a native subject key and a legacy row id are never compared as one value.
* ``closure_result`` -> ``{"closure_kind": str | None}``.
* a text-valued type -> the exact text.
* a date-valued type -> the ISO date string.
* ``supporting_documentation_in_use`` -> ``{"document_id": int}``, the one type
  whose value *is* a registered row rather than a cell.
* ``external_org`` carries its wording and its registered organization, and the
  wording is the value; ``{"external_org_id": int}`` is reached only by a row
  that carries the reference and no wording at all.
* anything with no value column set -> ``None``.

This module holds the rule and the two readings of it.  It imports the four
satellite models and nothing else: no renderer, no extractor, no model client.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Fact,
    FactAppliesTo,
    FactClosureResult,
    FactStatementTiming,
)

# The Fact types whose value is carried by a satellite table rather than by a
# scalar column on the Fact itself.
SATELLITE_FACT_TYPES = frozenset({"statement_timing", "applies_to", "closure_result"})


class TypedFactColumns(Protocol):
    """Anything carrying the value columns of ``facts``.

    A stored ``Fact`` and a ``materializer.MaterializedValue`` both do — the
    materialized value mirrors those columns deliberately, so the shadow
    comparison can read a frozen cell and the Fact it produced the same way.
    """

    fact_type: str
    text_value: str | None
    date_value: date | None
    external_org_value_id: int | None
    document_value_id: int | None


class StatementTimingMember(Protocol):
    """One timing a statement stated, as either a stored row or a projected one."""

    timing_role: str
    text: str
    precision: str
    start_date: date | None
    end_date: date | None


def typed_fact_value(
    fact_type: str,
    *,
    text_value: str | None = None,
    date_value: date | None = None,
    external_org_value_id: int | None = None,
    document_value_id: int | None = None,
    applies_to_subject_keys: Sequence[str] = (),
    closure_kind: str | None = None,
    statement_timings: Sequence[StatementTimingMember] = (),
) -> Any:
    """The one typed value of a Fact, from components already in hand.

    The module docstring states the shape this returns for each Fact type.
    Callers pass only the components their own row class holds; a satellite
    type whose members were not loaded reads as an empty satellite, which is
    why ``read_fact_value`` loads them and ``scalar_fact_value`` refuses the
    types that have them.
    """

    if fact_type == "statement_timing":
        return {
            "timings": [
                {
                    "role": item.timing_role,
                    "text": item.text,
                    "precision": item.precision,
                    "start_date": _iso(item.start_date),
                    "end_date": _iso(item.end_date),
                }
                for item in statement_timings
            ]
        }
    if fact_type == "applies_to":
        subject_keys = list(applies_to_subject_keys)
        return {
            "mode": "selected" if subject_keys else "unknown",
            "subject_keys": subject_keys,
        }
    if fact_type == "closure_result":
        return {"closure_kind": closure_kind}
    if text_value is not None:
        return text_value
    if date_value is not None:
        return date_value.isoformat()
    if external_org_value_id is not None:
        return {"external_org_id": int(external_org_value_id)}
    if document_value_id is not None:
        return {"document_id": int(document_value_id)}
    return None


def read_fact_value(session: Session, fact: Fact) -> Any:
    """The typed value of one captured Source Fact, satellites included.

    A scalar Fact costs no query: only the three satellite-bearing types read
    their members, so a comparison pass over hundreds of structured cells is
    no more expensive than reading the columns itself.
    """

    if fact.fact_type == "statement_timing":
        return typed_fact_value(
            fact.fact_type,
            statement_timings=tuple(
                session.scalars(
                    select(FactStatementTiming)
                    .where(FactStatementTiming.fact_id == fact.id)
                    .order_by(FactStatementTiming.timing_role)
                ).all()
            ),
        )
    if fact.fact_type == "applies_to":
        return typed_fact_value(
            fact.fact_type,
            applies_to_subject_keys=tuple(
                key
                for key in session.scalars(
                    select(FactAppliesTo.record_subject_key)
                    .where(FactAppliesTo.fact_id == fact.id)
                    .order_by(FactAppliesTo.ordinal)
                )
                if key is not None
            ),
        )
    if fact.fact_type == "closure_result":
        return typed_fact_value(
            fact.fact_type,
            closure_kind=session.scalar(
                select(FactClosureResult.closure_kind).where(
                    FactClosureResult.fact_id == fact.id
                )
            ),
        )
    return scalar_fact_value(fact)


def scalar_fact_value(fact: TypedFactColumns) -> Any:
    """The typed value of a Fact whose type carries it in its own columns.

    The comparison passes that produce Proposed Deltas from a workbook or an
    export revision are pure functions of already-captured Facts, so they read
    values without a session.  Every one of them selects single-valued fields
    first, and a satellite type reaching them would silently compare as an
    empty satellite, so this refuses instead.
    """

    if fact.fact_type in SATELLITE_FACT_TYPES:
        raise ValueError(
            f"{fact.fact_type!r} carries its value in a satellite table; read it "
            "with read_fact_value, which loads the members"
        )
    return typed_fact_value(
        fact.fact_type,
        text_value=fact.text_value,
        date_value=fact.date_value,
        external_org_value_id=fact.external_org_value_id,
        document_value_id=fact.document_value_id,
    )


def scalar_column_value(
    fact_type: str,
    text_value: str | None,
    date_value: date | None,
    external_org_value_id: int | None,
    document_value_id: int | None,
) -> Any:
    """The same reading, for a projection row read as plain columns.

    ``delta_generation.accepted_values`` selects the scalar columns of the
    current-record view and no satellites, so it names the reading it performs
    rather than passing a row object that does not exist.
    """

    if fact_type in SATELLITE_FACT_TYPES:
        return None
    return typed_fact_value(
        fact_type,
        text_value=text_value,
        date_value=date_value,
        external_org_value_id=external_org_value_id,
        document_value_id=document_value_id,
    )


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
