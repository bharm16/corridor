"""Synthetic Key Date tables and the record they are compared against (#450).

Every file here is synthetic. It exercises the reader and the comparison, never
a claim about a real customer form (ADR-0046).

The adopted baseline carries a **Required By** column, because the whole point
of a moved key date is what it reaches: a Constraint's Required By is
calculated from the Key Date Version it serves, so a fixture whose accepted
record holds no Required By could not tell an impact derivation that found
nothing from one that found nothing because there was nothing to find.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from openpyxl import Workbook

from corridor.connectors.pull_connector import SourceEnvelope
from corridor.key_date_table import DECLARED_COLUMNS
from corridor.models import Project
from corridor.push_intake import (
    PushCredential,
    PushPayload,
    accept_delivery,
    bind_credential,
    register_push_credential,
)
from corridor.source_intake import StagedSource, validate_and_stage

from later_revision_support import BASELINE_ROWS, CUSTOMER, HEADINGS


# The adopted UCM baseline, with the Required By column the published
# vocabulary maps to `need_date`.  The first two conflicts are required by the
# relocation-construction key date; the third is required by a different one,
# so an impact derivation that named all three would be visibly wrong.
UCM_HEADINGS = [*HEADINGS, "Required By"]
UCM_REQUIRED_BY = ("2026-03-31", "2026-03-31", "2025-07-31")
UCM_ROWS = [
    [*row, required_by] for row, required_by in zip(BASELINE_ROWS, UCM_REQUIRED_BY)
]

# The design partner's Key Date table, in the shape `corpus/sh99-milestones.csv`
# holds: one code, one name, one scheduled date.
KEY_DATE_ROWS = [
    ["DESIGN", "Design completion", "2025-01-31"],
    ["ROW-UTIL-EXEC", "ROW and utility agreement execution", "2025-07-31"],
    ["RELO-CONSTR", "Relocation construction completion", "2026-03-31"],
]


def key_date_workbook(
    path: Path,
    rows: Sequence[Sequence[object]],
    *,
    headings: Sequence[str] | None = None,
) -> bytes:
    """One synthetic Key Date table workbook, headings first."""

    book = Workbook()
    sheet = book.active
    sheet.title = "Key Dates"
    sheet.append(list(DECLARED_COLUMNS if headings is None else headings))
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path.read_bytes()


def moved(rows: Sequence[Sequence[object]], code: str, date_text: str) -> list[list]:
    """The same table with one key date's scheduled date replaced."""

    return [
        [row[0], row[1], date_text] if row[0] == code else list(row) for row in rows
    ]


def deliver_export(
    session,
    project: Project,
    body: bytes,
    *,
    filename: str = "key-dates.xlsx",
    external_identity: str,
) -> tuple[StagedSource, SourceEnvelope]:
    """Take delivery of one export through the manual push ingress (#511).

    Each delivery registers its own credential, because a project takes more
    than one export in these tests and a credential is bound to the exact
    material it was registered with.
    """

    secret = f"secret-{project.slug}-{external_identity}"
    register_push_credential(
        session,
        customer=CUSTOMER,
        project=project,
        channel="webhook",
        material=secret,
    )
    binding = bind_credential(session, PushCredential("webhook", secret))
    receipt = accept_delivery(
        session,
        binding,
        PushPayload(
            body=body,
            filename=filename,
            transport_delivery_id=external_identity,
        ),
    )
    return validate_and_stage(body, filename), receipt.envelope
