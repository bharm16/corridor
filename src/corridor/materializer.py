"""The one deterministic path from a Source Segment to a Source Fact value (#446).

A model may choose which segment supports a Fact; it may never supply the
value.  The 5.1% vision-misread class (``tests/fixtures/vision-misreads.json``)
was exactly a model literal reaching the record with a citation beside it, and
the earlier defence — each appender comparing the model's field to the cell
before writing the model's field — was a convention every new appender had to
remember.  This module makes it a type: ``append_fact`` accepts only a
``MaterializedValue``, and a ``MaterializedValue`` is sealed here, from a
segment's exact text through the Fact type's released transformation, with the
segment references and the materializer version it carries.  Constructing one
from a literal, or altering one with ``dataclasses.replace``, fails closed.

The seal is a per-process key, so nothing outside this module can mint or
re-mint a value; the architecture test additionally proves no other module
names the constructor and that no module on the fact path can import a model
client (ADR-0076 as amended by ADR-0083; enforcement composes with #492's
append commands, which remain the only writers).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
import hmac
import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.fact_types import (
    FACT_TYPE_CONTRACTS,
    PDF_MARKED_RESOLUTION_TRANSFORMATION,
    PDF_TEXT_TRANSFORMATION,
    FactTypeContract,
)
from corridor.identity import normalize_party
from corridor.models import ExternalOrg, SourceSegment


MATERIALIZER_VERSION = "fact_materializer_v1"

_SEAL_KEY = secrets.token_bytes(32)


class FactValidationError(ValueError):
    """A proposed Fact cannot replay from its declared source support."""


class FactReplayMismatch(FactValidationError):
    """A materialized Fact value differs from its replayed transformation."""


class MaterializationRefused(FactValidationError):
    """A Source Fact value was offered that no Source Segment materializes."""


@dataclass(frozen=True)
class MaterializedValue:
    """One Source Fact value with the segment references that reproduce it.

    Only this module constructs one.  The value fields mirror the columns of
    ``facts`` exactly so the append command reads them off the type rather
    than accepting them as parameters.
    """

    fact_type: str
    transformation: str
    source_links: tuple[tuple[str, int], ...]
    text_value: str | None = None
    date_value: date | None = None
    external_org_value_id: int | None = None
    document_value_id: int | None = None
    materializer_version: str = MATERIALIZER_VERSION
    seal: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        expected = _seal(
            fact_type=self.fact_type,
            transformation=self.transformation,
            source_links=self.source_links,
            text_value=self.text_value,
            date_value=self.date_value,
            external_org_value_id=self.external_org_value_id,
            document_value_id=self.document_value_id,
            materializer_version=self.materializer_version,
        )
        if not hmac.compare_digest(self.seal, expected):
            raise MaterializationRefused(
                "a Source Fact value materializes only from a Source Segment"
            )

    @property
    def scalar(self) -> str | date | None:
        return self.date_value if self.date_value is not None else self.text_value


def _seal(**fields) -> bytes:
    message = repr(tuple(sorted(fields.items()))).encode("utf-8")
    return hmac.new(_SEAL_KEY, message, sha256).digest()


def _sealed(
    *,
    fact_type: str,
    transformation: str,
    source_links: tuple[tuple[str, int], ...],
    text_value: str | None = None,
    date_value: date | None = None,
    external_org_value_id: int | None = None,
    document_value_id: int | None = None,
) -> MaterializedValue:
    fields = dict(
        fact_type=fact_type,
        transformation=transformation,
        source_links=source_links,
        text_value=text_value,
        date_value=date_value,
        external_org_value_id=external_org_value_id,
        document_value_id=document_value_id,
        materializer_version=MATERIALIZER_VERSION,
    )
    return MaterializedValue(**fields, seal=_seal(**fields))


def materialize_segment_value(
    session: Session, fact_type: str, segment: SourceSegment
) -> MaterializedValue:
    """Materialize one scalar Fact value from a segment's exact text."""

    if segment.kind in {"pdf_cell", "pdf_span"}:
        return materialize_pdf_segment_value(session, fact_type, segment)
    contract = _scalar_contract(fact_type)
    _certify(segment, fact_type, contract)
    value = validated_scalar_value(contract, segment.exact_text)
    external_org_value_id = (
        exact_registered_external_org_id(session, value)
        if fact_type == "external_org" and isinstance(value, str)
        else None
    )
    return _sealed(
        fact_type=fact_type,
        transformation=contract.transformation,
        source_links=tuple(
            (role, segment.id) for role in sorted(contract.required_roles)
        ),
        text_value=value if isinstance(value, str) else None,
        date_value=value if isinstance(value, date) else None,
        external_org_value_id=external_org_value_id,
    )


def clean_pdf_source_text(exact_text: str) -> str:
    """Replay the measured native listing's whitespace rule without a model import."""

    return re.sub(r"\s+", " ", exact_text.replace("\n", " ")).strip()


def materialize_pdf_segment_value(
    session: Session, fact_type: str, segment: SourceSegment
) -> MaterializedValue:
    """Capture a PDF cell, or an organization span, through its named text rule.

    Calendar dates retain the existing ISO-only contract. A seasonal or
    month-only source phrase remains available to the proposal, but cannot
    acquire invented date precision by passing through this materializer.
    """

    contract = _scalar_contract(fact_type)
    # A statement Fact's value is the exact prose span, under
    # `exact_prose_span_v1` and with an attribution source beside it
    # (`materialize_prose_wording`). Since #741 its value source is a
    # `pdf_span` like a native cell's, so the kind alone no longer separates
    # the two contracts and this says which one is being asked for. Folding
    # whitespace into a statement's wording, or writing it with one role, would
    # be a different Fact wearing the same words.
    if contract.subject_kind != "source_row":
        raise FactValidationError(
            f"{fact_type} does not materialize as a PDF cell reading"
        )
    _certify_pdf_segment(segment, fact_type, contract)
    if contract.value_class == "date":
        value = validated_scalar_value(contract, segment.exact_text)
        transformation = contract.transformation
    else:
        value = clean_pdf_source_text(segment.exact_text)
        if not value:
            raise FactValidationError("PDF text Fact cannot be empty")
        transformation = PDF_TEXT_TRANSFORMATION
    return _sealed(
        fact_type=fact_type,
        transformation=transformation,
        source_links=(("value_source", segment.id),),
        text_value=value if isinstance(value, str) else None,
        date_value=value if isinstance(value, date) else None,
        external_org_value_id=(
            exact_registered_external_org_id(session, value)
            if fact_type == "external_org" and isinstance(value, str)
            else None
        ),
    )


def materialize_pdf_marked_resolution(
    headers: Sequence[SourceSegment],
    marks: Sequence[SourceSegment],
    *,
    preceding_header_page_no: int | None = None,
) -> MaterializedValue:
    """Read marked resolution headings from ordered, paired PDF cell evidence.

    Only selected, nonempty mark cells are supplied: the native reading keeps
    empty cells without inventing Source Segments for them. A spanning header
    may supply several marked columns; it is retained once and replayed once
    per selected mark. A carried header must name its preceding page, which
    the resulting immutable value-source links preserve for later replay.
    """

    headers, marks = tuple(headers), tuple(marks)
    _ordered_pdf_cells(headers, "headers")
    _ordered_pdf_cells(marks, "marks")
    if any(_pdf_scope(segment) != _pdf_scope(headers[0]) for segment in marks):
        raise FactValidationError("PDF resolution support crosses its reading scope")
    header_page, mark_page = headers[0].page_no, marks[0].page_no
    if header_page == mark_page:
        if preceding_header_page_no is not None:
            raise FactValidationError("same-page PDF headers are not carried headers")
        if headers[0].table_index != marks[0].table_index or any(
            header.cell_row + header.row_span > marks[0].cell_row
            for header in headers
        ):
            raise FactValidationError("PDF resolution header must precede its body row")
    elif header_page >= mark_page or preceding_header_page_no != header_page:
        raise FactValidationError("PDF resolution needs its declared preceding header page")

    selected: list[str] = []
    used_headers: set[int] = set()
    for mark in marks:
        matching = tuple(
            header for header in headers
            if header.cell_column <= mark.cell_column
            and mark.cell_column + mark.column_span
            <= header.cell_column + header.column_span
        )
        if len(matching) != 1:
            raise FactValidationError("PDF resolution mark has no matching header column span")
        header = matching[0]
        selected.append(clean_pdf_source_text(header.exact_text))
        used_headers.add(header.id)
    if used_headers != {header.id for header in headers}:
        raise FactValidationError("PDF resolution header has no corresponding selected mark")
    return _sealed(
        fact_type="resolution_strategy",
        transformation=PDF_MARKED_RESOLUTION_TRANSFORMATION,
        source_links=(
            *(("value_source", header.id) for header in headers),
            *(("context", mark.id) for mark in marks),
        ),
        text_value="; ".join(selected),
    )


def replay_pdf_materialized_value(
    session: Session,
    fact_type: str,
    transformation: str,
    value_segments: Sequence[SourceSegment],
    context_segments: Sequence[SourceSegment],
) -> MaterializedValue:
    """Rebuild a PDF value after its ordered source links have been dereferenced."""

    if transformation == PDF_MARKED_RESOLUTION_TRANSFORMATION:
        if fact_type != "resolution_strategy":
            raise FactValidationError("PDF marked resolution has the wrong Fact type")
        header_page = value_segments[0].page_no if value_segments else None
        mark_page = context_segments[0].page_no if context_segments else None
        return materialize_pdf_marked_resolution(
            value_segments,
            context_segments,
            preceding_header_page_no=header_page if header_page != mark_page else None,
        )
    if len(value_segments) != 1 or context_segments:
        raise FactValidationError("PDF scalar Fact needs exactly one value source")
    value = materialize_pdf_segment_value(session, fact_type, value_segments[0])
    if value.transformation != transformation:
        raise FactValidationError("PDF Fact transformation does not match its contract")
    return value


def _pdf_scope(segment: SourceSegment) -> tuple:
    return (
        segment.project_id, segment.document_id, segment.rendition_sha256,
        segment.reading_sha256, segment.reader_identity,
    )


def _certify_pdf_segment(
    segment: SourceSegment, fact_type: str, contract: FactTypeContract
) -> None:
    _certify(segment, fact_type, contract)
    if segment.kind not in {"pdf_cell", "pdf_span"}:
        raise FactValidationError("PDF materialization needs native PDF support")
    if (
        segment.project_id is None or segment.document_id is None
        or not segment.rendition_sha256 or not segment.reading_sha256
        or not segment.reader_identity or segment.page_no is None
    ):
        raise FactValidationError("PDF Fact support needs its complete reading scope")
    if segment.kind == "pdf_cell" and (
        segment.table_index is None or segment.cell_row is None
        or segment.cell_column is None or segment.row_span is None
        or segment.column_span is None or segment.table_index < 0
        or segment.cell_row < 0 or segment.cell_column < 0
        or segment.row_span < 1 or segment.column_span < 1
    ):
        raise FactValidationError("PDF Fact support needs its complete cell locator")


def _ordered_pdf_cells(segments: tuple[SourceSegment, ...], role: str) -> None:
    if not segments:
        raise FactValidationError(f"PDF resolution needs nonempty {role}")
    contract = FACT_TYPE_CONTRACTS["resolution_strategy"]
    prior_end = -1
    ids: set[int] = set()
    for segment in segments:
        _certify_pdf_segment(segment, "resolution_strategy", contract)
        if _pdf_scope(segment) != _pdf_scope(segments[0]) or (
            segment.page_no, segment.table_index, segment.cell_row
        ) != (segments[0].page_no, segments[0].table_index, segments[0].cell_row):
            raise FactValidationError(f"PDF resolution {role} cross their reading or row")
        if segment.id in ids or segment.cell_column < prior_end:
            raise FactValidationError(f"PDF resolution {role} need unique ordered columns")
        if not clean_pdf_source_text(segment.exact_text):
            raise FactValidationError(f"PDF resolution {role} cannot be empty")
        ids.add(segment.id)
        prior_end = segment.cell_column + segment.column_span


def materialize_prose_wording(
    value_segment: SourceSegment,
    attribution_segment: SourceSegment,
    *,
    attribution: str,
) -> MaterializedValue:
    """Materialize statement wording as one exact prose span with attribution."""

    contract = FACT_TYPE_CONTRACTS["statement_wording"]
    _certify(value_segment, "statement_wording", contract)
    _certify(attribution_segment, "statement_wording", contract)
    if value_segment.kind == "email_span" or attribution_segment.kind == "email_span":
        if (value_segment.kind != "email_span" or attribution_segment.kind != "email_span"
                or value_segment.document_id != attribution_segment.document_id
                or value_segment.location_json.get("section") != "body"
                or attribution_segment.location_json.get("section") != "header"
                or attribution_segment.location_json.get("part_path") != []
                or attribution_segment.location_json.get("header_name") != "from"):
            raise FactValidationError("email wording requires authored text and its own From header")
    if not attribution.strip() or attribution not in attribution_segment.exact_text:
        raise FactValidationError(
            "statement attribution does not replay from declared source"
        )
    return _sealed(
        fact_type="statement_wording",
        transformation=contract.transformation,
        source_links=(
            ("value_source", value_segment.id),
            ("attribution_source", attribution_segment.id),
        ),
        text_value=validated_scalar_value(contract, value_segment.exact_text),
    )


def materialize_email_metadata(segment: SourceSegment) -> MaterializedValue:
    """Materialize retained transport evidence without accepted-record authority."""
    section = (segment.location_json or {}).get("section")
    if section not in {"header", "attachment"}:
        raise FactValidationError("email metadata needs a header or attachment boundary")
    fact_type = f"email_{section}"
    contract = FACT_TYPE_CONTRACTS[fact_type]
    _certify(segment, fact_type, contract)
    return _sealed(fact_type=fact_type, transformation=contract.transformation,
                   source_links=(("value_source", segment.id),), text_value=segment.exact_text)


def materialize_quoted_statement_wording(
    segment: SourceSegment, description: str
) -> MaterializedValue:
    """Materialize a recorder's statement wording that its segment carries.

    A Recorded Verbal Statement's segment is the words themselves; a Meeting
    Notes passage carries the words inside it (ADR-0074).  Either way the
    wording is dereferenced to the segment before it becomes a value.
    """

    contract = FACT_TYPE_CONTRACTS["statement_wording"]
    _certify(segment, "statement_wording", contract)
    if not description.strip():
        raise FactValidationError("statement wording needs the party's words")
    if segment.kind == "recorded_verbal_statement":
        if description != segment.exact_text:
            raise FactValidationError(
                "statement wording must be the recorder's exact words"
            )
    elif description not in segment.exact_text:
        raise FactValidationError(
            "statement wording must appear in its cited passage"
        )
    return _sealed(
        fact_type="statement_wording",
        transformation=contract.transformation,
        source_links=(
            ("value_source", segment.id),
            ("attribution_source", segment.id),
        ),
        text_value=description,
    )


def materialize_typed_satellite(
    fact_type: str, segment: SourceSegment
) -> MaterializedValue:
    """Materialize the segment reference of a Fact whose value is a typed satellite.

    Applies To members, closure results, and stated timings are foreign keys,
    enumerations, and human-entered members carried by the append command's
    typed satellites; the value columns stay empty and the segment is the
    reference.
    """

    contract = FACT_TYPE_CONTRACTS.get(fact_type)
    if contract is None or contract.value_class not in (
        "reference_set",
        "closure_result",
        "statement_timing",
    ):
        raise FactValidationError(f"{fact_type!r} does not carry a typed satellite")
    _certify(segment, fact_type, contract)
    return _sealed(
        fact_type=fact_type,
        transformation=contract.transformation,
        source_links=(("value_source", segment.id),),
    )


def materialize_document_reference(document_id: int) -> MaterializedValue:
    """Materialize the relationship to one registered document revision."""

    contract = FACT_TYPE_CONTRACTS["supporting_documentation_in_use"]
    return _sealed(
        fact_type="supporting_documentation_in_use",
        transformation=contract.transformation,
        source_links=(),
        document_value_id=document_id,
    )


def transform(name: str, exact_text: str) -> str | date:
    """Apply one released transformation to a segment's exact text."""

    if name == "trim_cell_text_v1":
        return exact_text.strip()
    if name == PDF_TEXT_TRANSFORMATION:
        return clean_pdf_source_text(exact_text)
    if name in {"exact_prose_span_v1", "exact_email_part_v1"}:
        return exact_text
    if name == "iso_date_cell_v1":
        try:
            return date.fromisoformat(exact_text.strip().split(" ", 1)[0])
        except ValueError as exc:
            raise FactValidationError(
                f"structured date is not an ISO calendar date: {exact_text!r}"
            ) from exc
    raise FactValidationError(f"unknown Fact transformation {name!r}")


def validated_scalar_value(
    contract: FactTypeContract, exact_text: str
) -> str | date:
    """Transform exact text and validate it under the Fact type's rule."""

    value = transform(contract.transformation, exact_text)
    if contract.validation_rule in {
        "non_empty_replay_exact",
        "non_empty_replay_exact_optional_registered_alias",
    }:
        if not isinstance(value, str) or not value.strip():
            raise FactValidationError("structured text Fact cannot be empty")
    elif contract.validation_rule == "iso_calendar_date_replay_exact":
        if not isinstance(value, date):
            raise FactValidationError("structured date Fact must be a calendar date")
    elif contract.validation_rule != "exact_attributed_prose_span":
        raise FactValidationError(
            f"Fact validation rule is not scalar: {contract.validation_rule!r}"
        )
    return value


def exact_registered_external_org_id(session: Session, wording: str) -> int | None:
    """The one registered organization this wording names, or none."""

    wanted = normalize_party(wording)
    matches = {
        organization.id
        for organization in session.scalars(select(ExternalOrg).order_by(ExternalOrg.id))
        if any(
            normalize_party(spelling) == wanted
            for spelling in (organization.name, *(organization.aliases or ()))
            if spelling
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _scalar_contract(fact_type: str) -> FactTypeContract:
    contract = FACT_TYPE_CONTRACTS.get(fact_type)
    if contract is None:
        raise FactValidationError(f"unknown Fact type {fact_type!r}")
    if contract.value_class not in ("text", "date", "external_org_wording"):
        raise FactValidationError(f"{fact_type!r} does not materialize a scalar value")
    return contract


def _certify(
    segment: SourceSegment, fact_type: str, contract: FactTypeContract
) -> None:
    """Fail closed unless the segment is a stored row whose digest proves its words."""

    if segment.id is None:
        raise MaterializationRefused("a Source Fact value needs a stored Source Segment")
    if segment.kind not in contract.accepted_segment_kinds:
        raise FactValidationError(f"{fact_type} does not accept {segment.kind} support")
    if segment.kind == "recorded_verbal_statement" and (
        segment.recorded_verbal_origin_id is None or segment.document_id is not None
    ):
        raise FactValidationError(
            "recorded verbal statement segment locator is incomplete"
        )
    if sha256(segment.exact_text.encode("utf-8")).hexdigest() != segment.content_sha256:
        raise FactReplayMismatch("segment digest does not match its stored words")
