"""The identity and authorization export a pilot customer's review asks for (#531).

#503 ratified the identity system and then named one thing it could not do:
hand a customer, in JSON or CSV, the history of who was given access to what,
who signed in, and who was taken off.  That history already exists — every act
is an ``audit_log`` entry written in the same transaction as the thing it
attributes — so this module is a reader, not a second ledger.  A separate
identity-event table would be a second copy of the same facts and the first
place the two would disagree.

The export is ordered and paged by ``audit_log.id``, never by a timestamp.  The
id is the append-only watermark: a caller that exported through id 9100 asks
for what came after 9100 and gets exactly that, with no window where two
entries share a clock reading and one is missed.  The recorded time is carried
in the rows for the reader's benefit; nothing here decides anything by it.

Only the identity and authorization actions are exported.  Ledger acts are a
different question with a different audience, and an "audit export" that
handed over the whole coordination history under the name of an access review
would be leaking, not disclosing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import csv
from dataclasses import dataclass
import io
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import AuditLog

# Every act that grants, exercises, or withdraws access.  Enrollment and
# deprovisioning are project-scoped; sign-in, sign-out, and the offboarding
# summary name the person.
IDENTITY_ACTIONS: tuple[str, ...] = (
    audit.ENROLL_PROJECT_MEMBER,
    audit.DEPROVISION_PROJECT_MEMBER,
    audit.DEPROVISION_PRINCIPAL,
    audit.SIGN_IN,
    audit.SIGN_OUT,
)

FIELDS: tuple[str, ...] = (
    "id",
    "action",
    "actor",
    "human_principal",
    "entity_type",
    "entity_id",
    "recorded_at",
    "before",
    "after",
)


@dataclass(frozen=True, slots=True)
class IdentityEvent:
    """One exported access act, in the terms the audit log recorded it."""

    id: int
    action: str
    actor: str
    human_principal: str | None
    entity_type: str
    entity_id: int
    recorded_at: str
    before: dict | None
    after: dict | None

    def as_row(self) -> dict[str, object]:
        return {
            "id": self.id,
            "action": self.action,
            "actor": self.actor,
            "human_principal": self.human_principal,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "recorded_at": self.recorded_at,
            "before": self.before,
            "after": self.after,
        }


def identity_events(
    session: Session, *, after_id: int | None = None, limit: int | None = None
) -> tuple[IdentityEvent, ...]:
    """Every access act after ``after_id``, oldest first.

    ``after_id`` is the id the previous export ended on, so consecutive
    exports abut exactly.  ``limit`` bounds one page; the caller resumes from
    the last id it received.
    """
    query = (
        select(AuditLog)
        .where(AuditLog.action.in_(IDENTITY_ACTIONS))
        .order_by(AuditLog.id)
    )
    if after_id is not None:
        query = query.where(AuditLog.id > after_id)
    if limit is not None:
        query = query.limit(limit)
    return tuple(_event(entry) for entry in session.scalars(query).all())


def _event(entry: AuditLog) -> IdentityEvent:
    return IdentityEvent(
        id=entry.id,
        action=entry.action,
        actor=entry.actor,
        human_principal=entry.human_principal,
        entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        recorded_at=entry.ts.isoformat() if entry.ts is not None else "",
        before=entry.before_json,
        after=entry.after_json,
    )


def export_json(events: Iterable[IdentityEvent]) -> str:
    """The export as one JSON array, one object per act."""
    return json.dumps([event.as_row() for event in events], indent=2, sort_keys=True)


def export_csv(events: Iterable[IdentityEvent]) -> str:
    """The export as CSV, with the two detail maps carried as JSON strings.

    A spreadsheet cannot hold a nested map, and flattening one into columns
    would invent a schema the audit log does not have.  Serializing the detail
    keeps the export lossless and keeps the flat columns — who, what act, which
    project — usable as columns.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for event in events:
        row = event.as_row()
        row["before"] = "" if row["before"] is None else json.dumps(
            row["before"], sort_keys=True
        )
        row["after"] = "" if row["after"] is None else json.dumps(
            row["after"], sort_keys=True
        )
        writer.writerow(row)
    return buffer.getvalue()


def export(
    session: Session,
    *,
    fmt: str = "json",
    after_id: int | None = None,
    limit: int | None = None,
) -> str:
    """One call for the whole export, in the format the caller asked for."""
    events: Sequence[IdentityEvent] = identity_events(
        session, after_id=after_id, limit=limit
    )
    if fmt == "json":
        return export_json(events)
    if fmt == "csv":
        return export_csv(events)
    raise ValueError(f"unknown export format {fmt!r}; use 'json' or 'csv'")
