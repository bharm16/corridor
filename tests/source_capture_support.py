"""One Document Rendition, its Source Segments, and the Source Facts captured from them.

Fifteen modules hand-built the same triple straight through the ORM: a
spreadsheet-cell Source Segment, the Source Fact materialized from it, and the
Fact Source that ties them.  ``src/corridor/source_append.py`` says the rule
those modules were outside of -- "an ORM write to any of those tables, from any
module, is refused by the database" -- but that revocation sits on the
capability logins, and the harness ``session`` binds the schema owner
(``tests/conftest.py``), which is not revoked.  So none of those writes reached
``append_source_segments`` or ``append_fact``.  The commands' scope checks on
typed references are also foreign keys, so those the fixture kept; what it lost
were the three the tables cannot state.  A fixture could store a cell digest
that was not the digest of its own exact text, and cite that cell from a Fact;
it could write a value its own declared transformation could not produce, which
is the misread class #446 closed; and it could not replay at all, because a
second write of one cell is a unique-constraint violation rather than the row
already there.  Nineteen files retyped ``trim_cell_text_v1`` twenty-eight times
to do it.

This module captures through those two commands instead, as the capability
login the extractor runs as, so a fixture cannot build a shape the product
could not.  The value, the transformation and the subject kind are not
parameters here at all: ``materializer.materialize_segment_value`` derives
them from the Fact type's released contract out of the segment's own exact
text, which is the same seal ``facts.py`` hands the command in production
(#446).

Every value is the caller's.  Nothing here reads a clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json

from sqlalchemy.orm import Session

from corridor.db_roles import WORKER_CAPABILITY_LOGIN
from corridor.fact_types import FACT_TYPE_CONTRACTS
from corridor.materializer import materialize_segment_value
from corridor.models import (
    ActiveExtractionRun,
    Document,
    ExtractionRun,
    Fact,
    Project,
    SourceSegment,
)
from corridor.source_append import SegmentValues, append_fact, append_source_segments

from harness_support import as_role


SHEET = "Utility Conflicts"
CAPTURE_PROMPT_VERSION = "source_capture_fixture_v1"


@contextmanager
def _as_capability_login(session: Session) -> Iterator[Session]:
    """Append as the login the runtime appends as, and stop however the body ends.

    The login, not the command owner, is the principal that makes the
    boundary real: ``corridor_source_append`` owns the ``SECURITY DEFINER``
    commands and therefore holds ``INSERT`` on the very tables they guard, so
    borrowing *it* would leave the ORM bypass open.  ``corridor_worker`` holds
    ``EXECUTE`` on the commands and no write at all, which is what makes a
    capture here reachable only through them.

    ``harness_support.as_role`` is the borrow itself, for every role this suite
    borrows, and it restores the principal that was in force rather than
    resetting to the session user -- so a capture inside a borrowed role hands
    that role back.  This seam was written out separately only while that was
    not true of it.

    What remains here is the flush.  Pending ORM state is written as the owner
    before the borrow, so an autoflush inside a command does not try to write
    the caller's unrelated rows as the login.
    """

    session.flush()
    with as_role(session, WORKER_CAPABILITY_LOGIN) as borrowed:
        yield borrowed


@dataclass
class Rendition:
    """One arriving Document Rendition and the Source Facts captured from it."""

    session: Session
    project: Project
    name: str
    subject_key: str = f"{SHEET}!1"
    sheet_name: str = SHEET
    column: str = "A"
    prompt_version: str = CAPTURE_PROMPT_VERSION
    doc_type: str = "matrix"
    # The rendition's own file identity, when a test reads it: its retained
    # digest, and the revision identity the source registry printed on it.
    document_sha256: str | None = None
    registry_id: str | None = None
    document: Document = field(init=False)
    run: ExtractionRun = field(init=False)
    _ordinals: dict[str, int] = field(default_factory=dict, init=False)
    _sequence: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.document = Document(
            project_id=self.project.id,
            sha256=self.document_sha256
            or sha256(f"{self.project.slug}:{self.name}".encode()).hexdigest(),
            registry_id=self.registry_id,
            filename=self.name,
            doc_type=self.doc_type,
            numbering_scheme="project-unique",
            pages=1,
            parse_status="parsed",
        )
        self.session.add(self.document)
        self.session.flush()
        self.run = ExtractionRun(
            document_id=self.document.id,
            prompt_version=self.prompt_version,
            outcome="completed",
            candidate_count=0,
            page_errors=0,
        )
        self.session.add(self.run)
        self.session.flush()
        self.session.add(
            ActiveExtractionRun(
                document_id=self.document.id, extraction_run_id=self.run.id
            )
        )
        self.session.flush()

    def segment(self, value: str, *, cell: str | None = None) -> SourceSegment:
        """Append one cell of this rendition, with no Source Fact captured from it.

        A replacement revision carries the same passage at a new locator, so a
        reading that has to find the passage in the successor needs the cell
        without a second Fact over it.  Appending one cell twice replays: the
        command returns the segment it already wrote rather than a second copy.
        """

        locator = cell if cell is not None else f"{self.column}{self._sequence + 1}"
        ordinal = self._ordinals.get(locator)
        if ordinal is None:
            self._sequence += 1
            ordinal = self._ordinals[locator] = self._sequence
        with _as_capability_login(self.session):
            (segment,) = append_source_segments(
                self.session,
                project_id=self.project.id,
                document_id=self.document.id,
                recorded_verbal_origin_id=None,
                segments=(
                    SegmentValues(
                        kind="spreadsheet_cell",
                        exact_text=value,
                        content_sha256=sha256(value.encode()).hexdigest(),
                        ordinal=ordinal,
                        sheet_name=self.sheet_name,
                        cell_range=locator,
                    ),
                ),
            )
        return segment

    def capture(
        self,
        *,
        fact_type: str,
        value: str,
        subject_key: str | None = None,
        cell: str | None = None,
    ) -> tuple[Fact, SourceSegment]:
        """Append one cell of this rendition and the Source Fact it materializes.

        ``value`` is the cell's exact text.  What the Fact says is whatever
        that text materializes to under the Fact type's released
        transformation, so a caller cannot state a value its own cell does not
        reproduce.  Capturing one cell twice replays: the commands return the
        segment and the Fact they already wrote rather than a second copy.
        """

        subject = subject_key if subject_key is not None else self.subject_key
        segment = self.segment(value, cell=cell)
        with _as_capability_login(self.session):
            materialized = materialize_segment_value(self.session, fact_type, segment)
            fact = append_fact(
                self.session,
                project_id=self.project.id,
                document_id=self.document.id,
                extraction_run_id=self.run.id,
                subject_kind=FACT_TYPE_CONTRACTS[fact_type].subject_kind,
                subject_key=subject,
                recorded_by=f"extractor:{self.prompt_version}",
                content_sha256=_capture_digest(
                    document_id=self.document.id,
                    prompt_version=self.prompt_version,
                    subject_key=subject,
                    segment_id=segment.id,
                    value=materialized,
                ),
                value=materialized,
            )
        return fact, segment


def _capture_digest(
    *,
    document_id: int,
    prompt_version: str,
    subject_key: str,
    segment_id: int,
    value,
) -> str:
    """The Fact's content digest, which ``append_fact`` reads as its replay key."""

    return sha256(
        json.dumps(
            {
                "document_id": document_id,
                "prompt_version": prompt_version,
                "fact_type": value.fact_type,
                "subject_key": subject_key,
                "text_value": value.text_value,
                "date_value": (
                    value.date_value.isoformat()
                    if value.date_value is not None
                    else None
                ),
                "external_org_value_id": value.external_org_value_id,
                "source_segment_id": segment_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
