"""Retain what Corridor sent, so silence can become a fact (#652).

The chase list (#425) has a fully built ``unanswered_request`` band, but it
fires only for a *retained outgoing request* whose declared response boundary
has passed. ADR-0090 retired the legacy ``STALE`` alert for the opposite of
this: it fired on quiet that nobody was owed, treating an empty inbox as
evidence. A no-response finding therefore needs the request itself on record —
what was asked, of whom, covering which Utility Conflicts, when it went, and
the boundary it declared — and until this module existed there was no table to
write and ``follow_up_bundles.read_retained_outgoing_requests`` returned
nothing.

This module is the seam that retains one, and records a received response that
stops the silence clock. It is deliberately thin: the boundary lives in the
database, not here. A retained outgoing request is Corridor-originated
correspondence — not source-derived evidence, not an accepted-record decision —
so it reuses the record-decision role and the immutable-receipt idiom the
baseline-adoption receipt established. ``append_outgoing_request`` and
``append_outgoing_request_response`` are ``SECURITY DEFINER`` commands owned by
that role; a guard trigger refuses every write that does not arrive through
them, so an ORM insert from any module is refused by PostgreSQL rather than by
a convention this module asks callers to keep. Both commands converge a replay
on the row they already wrote.

What is out of scope (#652): drafting or sending the request, delivery
confirmation, automatic escalation, and full receipt tracking. Recording that a
reply arrived, sufficiently to stop the clock, is all the response half does.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256
import json

from sqlalchemy import bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from corridor.models import OutgoingRequest, OutgoingRequestResponse


class OutgoingRequestRefused(ValueError):
    """A caller cannot retain an outgoing request or record its response."""


def retain_outgoing_request(
    session: Session,
    *,
    project_id: int,
    follow_up_plan_id: int,
    external_organization: str,
    question: str,
    covered_subject_keys: Sequence[str],
    sent_on: date,
    sent_by_principal: str,
    expected_response_by: date,
    idempotency_key: str,
    responsible_role: str | None = None,
    content_sha256: str | None = None,
    sent_bytes: bytes | None = None,
    boundary_rule_version: str | None = None,
    boundary_interval_days: int | None = None,
) -> OutgoingRequest:
    """Retain one outgoing request, or return the one a replay already wrote.

    ``content_sha256`` is the digest of what was sent. It is derived from
    ``sent_bytes`` when Corridor kept the exact request and those bytes are the
    only thing passed; a caller that never kept the bytes — whoever sent it, by
    whatever means — passes the digest alone. Silence before
    ``expected_response_by`` is not a finding, which the command enforces along
    with project scope of the Follow-up Plan and the digest/bytes agreement.
    """

    if not sent_by_principal.strip():
        raise OutgoingRequestRefused("a retained outgoing request names its sender")
    if not idempotency_key.strip():
        raise OutgoingRequestRefused("a retained outgoing request needs an idempotency key")
    subjects = tuple(covered_subject_keys)
    if not subjects:
        raise OutgoingRequestRefused(
            "a retained outgoing request covers at least one Utility Conflict"
        )
    if content_sha256 is None:
        if sent_bytes is None:
            raise OutgoingRequestRefused(
                "a retained outgoing request needs the exact bytes or their digest"
            )
        content_sha256 = sha256(sent_bytes).hexdigest()

    request_id = session.scalar(
        select(
            func.append_outgoing_request(
                project_id,
                follow_up_plan_id,
                external_organization,
                responsible_role,
                question,
                cast(bindparam(None, json.dumps(list(subjects))), JSONB),
                content_sha256,
                sent_bytes,
                sent_on,
                sent_by_principal,
                expected_response_by,
                boundary_rule_version,
                boundary_interval_days,
                idempotency_key,
            )
        )
    )
    return session.get_one(OutgoingRequest, int(request_id))


def record_outgoing_request_response(
    session: Session,
    *,
    project_id: int,
    request_id: int,
    received_on: date,
    recorded_by_principal: str,
) -> OutgoingRequestResponse:
    """Record that a reply arrived, stopping one request's silence clock.

    Append-only and idempotent: a replay of the same received day returns the
    row already written, and a second, different day is refused. Once recorded,
    ``read_retained_outgoing_requests`` no longer returns the request as of any
    cutoff on or after the received day.
    """

    if not recorded_by_principal.strip():
        raise OutgoingRequestRefused("a recorded response names who recorded it")

    response_id = session.scalar(
        select(
            func.append_outgoing_request_response(
                project_id,
                request_id,
                received_on,
                recorded_by_principal,
            )
        )
    )
    return session.get_one(OutgoingRequestResponse, int(response_id))
