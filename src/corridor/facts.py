"""Append and replay typed source Facts over exact Source Segments.

Facts answer what one Document rendition said; they do not settle what the
Project Record says. Verified structured cells qualify for automatic Record
Inclusion; Minutes statement wording stays pending for a human decision. Both
materialized values reproduce through named transformations, and role-tagged
support stays constrained to the Fact's own rendition (ADR-0067, ADR-0069,
ADR-0070).  Every value an appender here hands to the append command is a
``MaterializedValue`` from ``materializer.py`` (#446): a Candidate's field is
compared with what its cell materializes and is never itself written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.candidates import propose, dedupe_hint
from corridor.models import (
    Candidate,
    Dependency,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactAppliesTo,
    FactClosureResult,
    FactClosureSource,
    FactDisposition,
    FactSource,
    FactStatementTiming,
    SourceSegment,
)
from corridor.fact_types import (
    FACT_TYPE_CONTRACTS,
    STRUCTURED_CELL_FACT_TYPES,
    FactTypeContract,
)
from corridor.materializer import (
    FactReplayMismatch,
    FactValidationError,
    MaterializedValue,
    clean_pdf_source_text,
    materialize_pdf_segment_value,
    materialize_pdf_marked_resolution,
    replay_pdf_materialized_value,
    materialize_document_reference,
    materialize_prose_wording,
    materialize_quoted_statement_wording,
    materialize_segment_value,
    materialize_typed_satellite,
    transform,
    validated_scalar_value,
)
from corridor.native_matrix_bindings import (
    NativeMatrixMapping, NativeFieldBinding, NativeRowBinding, READER_PATH, native_replay_index,
)
from corridor.reader_segments import NativeCellIndex, pdf_cell_id
from corridor.token_layers import NativePdfReading, read_native_pdf
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.sheets import column_mapping
from corridor.source_append import (
    ClosureValues,
    TimingValues,
    append_extracted_proposal,
    append_fact,
)
from corridor.source_segments import (
    append_source_segment,
    dereference_source_segment,
    replay_recorded_verbal_statement,
    spreadsheet_replay,
)
from corridor.statement_values import StatementTiming
from corridor.prose_spans import PROSE_SEGMENT_KINDS, is_prose_segment, prose_segment_filter


@dataclass(frozen=True)
class AppliesToFactValue:
    """One replayed, FK-backed Applies To reference set."""

    dependency_ids: tuple[int, ...]
    record_subject_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClosureFactValue:
    """One replayed typed closure source result."""

    closure_kind: str
    successor_dependency_id: int | None
    governing_source_segment_ids: tuple[int, ...]


@dataclass(frozen=True)
class StatementTimingFactValue:
    """One replayed set of a Recorded Verbal Statement's stated timings."""

    timings: tuple[tuple[str, StatementTiming], ...]


@dataclass(frozen=True)
class NativeFieldMaterialization:
    """One source-bound field, including an explicit refusal to mint a Fact."""

    row: NativeRowBinding
    field: NativeFieldBinding
    value: MaterializedValue | None
    sources_json: str
    outcome_json: str

    @property
    def sources(self) -> dict:
        return json.loads(self.sources_json)

    @property
    def outcome(self) -> dict:
        return json.loads(self.outcome_json)


def materialize_native_matrix_fields(
    session: Session, document: Document, mapping: NativeMatrixMapping,
) -> tuple[NativeFieldMaterialization, ...]:
    """Dereference once per scoped ID; seal values before writing any Fact."""
    mapping.certify(document)
    index = NativeCellIndex(document, mapping.native_reading)
    selected: dict[str, SourceSegment] = {}
    prepared = []
    for row in mapping.rows:
        for field in row.fields:
            sources = {}
            segments = {}
            for role, references in (("value_source", field.value_sources), ("context", field.context_sources)):
                sources[role], segments[role] = [], []
                for reference in references:
                    if reference.scoped_id not in selected:
                        selected[reference.scoped_id] = reference.select(session, document, index)
                    segment = selected[reference.scoped_id]
                    segments[role].append(segment)
                    sources[role].append({
                        "segment_id": segment.id, "scoped_id": reference.scoped_id,
                        "model_id": reference.model_id,
                    })
            value, status, reason = _materialize_native_field(session, row, field, segments)
            outcome = {
                "row_id": row.row_id, "local_row_id": row.local_row_id,
                "page": row.page_no, "field": field.name,
                "status": status, "reason": reason,
                "value_source_ids": [segment.id for segment in segments["value_source"]],
                "context_source_ids": [segment.id for segment in segments["context"]],
            }
            prepared.append(NativeFieldMaterialization(
                row, field, value, json.dumps(sources, sort_keys=True), json.dumps(outcome, sort_keys=True),
            ))
    return tuple(prepared)


def _materialize_native_field(session, row, field, segments):
    if row.disposition != "extracted":
        return None, "not_extracted", row.reason
    try:
        if field.context_sources:
            headers, marks = segments["value_source"], segments["context"]
            value = materialize_pdf_marked_resolution(
                headers, marks,
                preceding_header_page_no=(headers[0].page_no if headers[0].page_no < row.page_no else None),
            )
        else:
            value = materialize_pdf_segment_value(session, field.name, segments["value_source"][0])
    except FactValidationError:
        if FACT_TYPE_CONTRACTS[field.name].value_class == "date":
            return None, "refused", "non_iso_date"
        raise
    scalar = value.date_value.isoformat() if value.date_value is not None else value.text_value
    if scalar != field.text:
        if value.date_value is not None:
            return None, "refused", "non_iso_date"
        raise FactReplayMismatch("native proposal field differs from its materialized source")
    return value, "materialized", "source_materialized"


def append_native_matrix_facts(
    session: Session, document: Document, run: ExtractionRun,
    prepared: tuple[NativeFieldMaterialization, ...],
) -> tuple[Fact, ...]:
    """Append captured values only; proposal compatibility is the next phase."""
    appended = []
    for field in prepared:
        if field.value is None:
            continue
        value = field.value
        appended.append(append_fact(
            session, project_id=document.project_id, document_id=document.id,
            extraction_run_id=run.id, subject_kind="source_row",
            subject_key=field.row.row_id, recorded_by=f"extractor:{run.prompt_version}",
            content_sha256=_fact_digest(
                run_identity={
                    # Equal values under a later semantic mapping are a new
                    # run's captured Facts. Content retry is resolved before
                    # this phase; a Fact owned by an older run cannot support
                    # the new run's Extracted Proposal.
                    "extraction_run_id": run.id,
                    "native_mapping_sha256": run.row_accounting_json["native_mapping"]["identity"],
                    "document_id": document.id, "prompt_version": run.prompt_version,
                    "schema_version": run.schema_version, "model": run.model,
                    "extractor_config_sha256": run.extractor_config_sha256,
                },
                subject_kind="source_row", subject_key=field.row.row_id, value=value,
            ),
            value=value,
        ))
    return tuple(appended)


def native_matrix_candidates(
    document: Document, mapping: NativeMatrixMapping,
    prepared: tuple[NativeFieldMaterialization, ...],
) -> tuple[Candidate, ...]:
    """Compatibility projection, constructed only after the source Facts append."""
    by_row = {}
    for field in prepared:
        by_row.setdefault(field.row.row_id, []).append(field)
    candidates = []
    for row in mapping.rows:
        if row.disposition != "extracted":
            continue
        fields = {field.field.name: field.field.text for field in by_row[row.row_id]}
        candidate = propose(
            document, kind="dependency", fields=fields, page_no=row.page_no,
            quote="", quote_verified=False, whole_row=False,
            confidence=row.confidence, prompt_version=mapping.config_record["prompt_version"],
            model=mapping.config_record["model"], tier=READER_PATH,
            dedupe=dedupe_hint(fields.get("external_org", ""), fields.get("utility_id", ""),
                               fields.get("station_from", fields.get("location_start", ""))),
            text_source="native_pdf_segments", unmapped=row.unmapped,
        )
        candidate.payload_json.update({
            "native_row_id": row.row_id, "local_row_id": row.local_row_id,
            "page": row.page_no,
            "field_sources": {field.field.name: field.sources for field in by_row[row.row_id]},
            "field_materialization": [field.outcome for field in by_row[row.row_id]],
        })
        # No invented row quotation is marked verified. Exact native locators
        # travel explicitly; compatibility admission remains disabled.
        candidate.payload_json["citations"][0].update({
            "reading_sha256": mapping.native_reading.reading_sha256,
            "table_index": row.table_index, "source_row": row.row,
            "source_segment_ids": sorted({
                source["segment_id"] for field in by_row[row.row_id]
                for references in field.sources.values() for source in references
            }),
        })
        candidates.append(candidate)
    return tuple(candidates)


def rows_carry_source_facts(candidates: tuple[Candidate, ...]) -> bool:
    """Whether these rows are ones the scoped Fact command would materialize.

    A question about rows, and only that. It was the answer to "which command
    records this reading", which is a question about the reader that produced
    them: a reading of no rows returns False here whatever read it, so a native
    spreadsheet reading of the published empty template chose the legacy command
    purely for having found no conflicts. A routed reading declares its output
    class instead (`pipeline.ROUTE_OUTPUTS`) and this predicate checks it
    (`require_source_fact_class`). The unrouted page batch in `extract_batch`
    still decides from its rows.
    """

    carries_structured_cells = any(
        candidate.payload_json.get("tier") == "native"
        and candidate.kind == "dependency"
        and any(
            candidate.payload_json.get("fields", {}).get(name)
            for name in STRUCTURED_CELL_FACT_TYPES
        )
        for candidate in candidates
    )
    return carries_structured_cells or any(
        _is_statement_wording_candidate(candidate) for candidate in candidates
    )


def require_source_fact_class(
    candidates: tuple[Candidate, ...], *, declares_source_facts: bool
) -> None:
    """Refuse rows that contradict the output class their route declared.

    The declaration is what chooses the command, so this is the consistency
    check on it rather than the decision. It is one-directional on purpose. Rows
    that materialize into Facts on a route that declared legacy Extracted
    Proposals are a defect: the Facts would simply never be appended, silently.
    A `source_facts` route with no such rows is not a defect at all -- it is a
    reading with nothing in it, which still owns its segments, its receipt and
    its lineage.
    """

    if rows_carry_source_facts(candidates) and not declares_source_facts:
        raise ValueError(
            "extraction rows materialize source Facts on a route that declared "
            "legacy Extracted Proposals"
        )


def append_structured_cell_facts(
    session: Session,
    document: Document,
    run: ExtractionRun,
    candidates: tuple[Candidate, ...],
) -> tuple[Fact, ...]:
    """Append every controlled value from native spreadsheet rows."""

    native = [
        candidate
        for candidate in candidates
        if candidate.payload_json.get("tier") == "native"
        and candidate.kind == "dependency"
        and any(
            candidate.payload_json.get("fields", {}).get(name)
            for name in STRUCTURED_CELL_FACT_TYPES
        )
    ]
    if not native:
        return ()
    accepted_segment_kinds = frozenset({"spreadsheet_cell"})
    segments = tuple(
        session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.document_id == document.id,
                SourceSegment.kind.in_(accepted_segment_kinds),
            )
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    if not segments:
        raise FactValidationError("native structured facts require cell segments")
    by_locator = {
        (segment.sheet_name, segment.cell_range): segment for segment in segments
    }
    rows = _segment_rows(segments)
    appended: list[Fact] = []
    for candidate in native:
        citation = (candidate.payload_json.get("citations") or [{}])[0]
        sheet_name = citation.get("sheet_name")
        table_row = citation.get("table_row")
        if not isinstance(sheet_name, str) or not isinstance(table_row, int):
            raise FactValidationError(
                "native structured Candidate needs an explicit sheet and table row"
            )
        if sheet_name not in rows:
            raise FactValidationError("Candidate sheet has no Source Segments")
        header_row, mapping = _header_mapping(rows[sheet_name])
        source_row = header_row + table_row
        fields = candidate.payload_json.get("fields") or {}
        for column_index, fact_type in sorted(mapping.items()):
            contract = FACT_TYPE_CONTRACTS.get(fact_type)
            if contract is None or not fields.get(fact_type):
                continue
            cell_range = _cell_range(column_index + 1, source_row)
            segment = by_locator.get((sheet_name, cell_range))
            if segment is None:
                raise FactValidationError(
                    f"structured source cell is absent: {sheet_name}!{cell_range}"
                )
            if segment.kind not in contract.accepted_segment_kinds:
                raise FactValidationError(
                    f"{fact_type} does not accept {segment.kind} support"
                )
            if fact_type == "applies_to":
                if segment.exact_text.strip() != str(fields[fact_type]).strip():
                    raise FactReplayMismatch(
                        f"Candidate {fact_type} does not reproduce from {cell_range}"
                    )
                dependency_ids = _resolve_applies_to_dependencies(
                    session, document.project_id, segment.exact_text
                )
                fact = _append_fact_envelope(
                    session,
                    document,
                    run,
                    fact_type=fact_type,
                    subject_kind=contract.subject_kind,
                    subject_key=f"{sheet_name}!{source_row}",
                    segment=segment,
                    structured_value={"dependency_ids": dependency_ids},
                    applies_to=dependency_ids,
                )
                appended.append(fact)
                continue
            materialized = materialize_segment_value(session, fact_type, segment)
            value = materialized.scalar
            if value != fields[fact_type]:
                candidate_value = _candidate_typed_value(contract, fields[fact_type])
                if value != candidate_value:
                    raise FactReplayMismatch(
                        f"Candidate {fact_type} does not reproduce from {cell_range}"
                    )
            fact = append_fact(
                session,
                project_id=document.project_id,
                document_id=document.id,
                extraction_run_id=run.id,
                subject_kind=contract.subject_kind,
                subject_key=f"{sheet_name}!{source_row}",
                recorded_by=f"extractor:{run.prompt_version}",
                content_sha256=_fact_digest(
                    run_identity={
                        "document_id": document.id,
                        "prompt_version": run.prompt_version,
                        "schema_version": run.schema_version,
                        "model": run.model,
                        "extractor_config_sha256": run.extractor_config_sha256,
                    },
                    subject_kind=contract.subject_kind,
                    subject_key=f"{sheet_name}!{source_row}",
                    value=materialized,
                ),
                value=materialized,
            )
            appended.append(fact)
            if fact_type == "marked_resolution":
                closure_contract = FACT_TYPE_CONTRACTS["closure_result"]
                closure = _append_fact_envelope(
                    session,
                    document,
                    run,
                    fact_type="closure_result",
                    subject_kind=closure_contract.subject_kind,
                    subject_key=f"{sheet_name}!{source_row}",
                    segment=segment,
                    structured_value={
                        "closure_kind": "source_marked_resolved",
                        "successor_dependency_id": None,
                    },
                    closure=ClosureValues(
                        closure_kind="source_marked_resolved",
                        successor_dependency_id=None,
                        governing_source_segment_ids=(segment.id,),
                    ),
                )
                appended.append(closure)
    session.flush()
    return tuple(appended)


def append_statement_wording_facts(
    session: Session,
    document: Document,
    run: ExtractionRun,
    candidates: tuple[Candidate, ...],
) -> tuple[Fact, ...]:
    """Append human-gated statement wording supported by exact Minutes spans."""

    statements = tuple(
        candidate
        for candidate in candidates
        if _is_statement_wording_candidate(candidate)
    )
    if not statements:
        return ()
    segments = tuple(
        session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.document_id == document.id,
                prose_segment_filter(SourceSegment),
            )
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    # This refused when a document had only the reader's spans, because the
    # incumbent's were the selected ones and #447 owned the act of changing
    # that. #447 recorded the maintainer's acceptance as a selection basis
    # (ADR-0095) and #741 removed the incumbent, so there is no unselected
    # second path left to refuse in favour of: the reader's page spans are the
    # exact Minutes spans a statement resolves against.
    by_page_and_text: dict[tuple[int, str], list[SourceSegment]] = {}
    segment_by_id = {segment.id: segment for segment in segments}
    for segment in segments:
        if segment.page_no is not None:
            by_page_and_text.setdefault(
                (segment.page_no, segment.exact_text), []
            ).append(segment)

    contract = FACT_TYPE_CONTRACTS["statement_wording"]
    appended: list[Fact] = []
    for candidate in statements:
        fields = candidate.payload_json.get("fields") or {}
        description = fields.get("description")
        citations = candidate.payload_json.get("citations") or []
        if not isinstance(description, str) or not description.strip():
            raise FactValidationError("statement Candidate needs exact wording")
        if len(citations) != 1:
            raise FactValidationError("statement wording needs one exact page citation")
        citation = citations[0]
        page_no = citation.get("page")
        quote = citation.get("quote")
        if (
            citation.get("verified") is not True
            or citation.get("document_id") != document.id
            or isinstance(page_no, bool)
            or not isinstance(page_no, int)
            or quote != description
        ):
            raise FactValidationError("statement wording citation is not exact")
        attribution = fields.get("stated_party") or fields.get("external_org")
        if not isinstance(attribution, str):
            raise FactValidationError("statement wording needs exact attribution")
        declared_links = candidate.payload_json.get("fact_sources")
        if declared_links is not None:
            if not isinstance(declared_links, list) or any(
                not isinstance(link, dict)
                or set(link) != {"segment_id", "role"}
                or isinstance(link.get("segment_id"), bool)
                or not isinstance(link.get("segment_id"), int)
                or not isinstance(link.get("role"), str)
                for link in declared_links
            ):
                raise FactValidationError("statement Fact source references are invalid")
            links = tuple(
                (link["role"], link["segment_id"]) for link in declared_links
            )
            if (
                {role for role, _segment_id in links} != contract.required_roles
                or len(links) != len(contract.required_roles)
            ):
                raise FactValidationError(
                    "statement Fact source roles do not match its contract"
                )
            try:
                value_segment = segment_by_id[
                    next(
                        segment_id
                        for role, segment_id in links
                        if role == "value_source"
                    )
                ]
                attribution_segment = segment_by_id[
                    next(
                        segment_id
                        for role, segment_id in links
                        if role == "attribution_source"
                    )
                ]
            except (KeyError, StopIteration) as exc:
                raise FactValidationError(
                    "statement Fact source belongs to another rendition"
                ) from exc
            if (
                value_segment.page_no != page_no
                or value_segment.exact_text != description
                or value_segment.kind not in contract.accepted_segment_kinds
                or attribution_segment.kind not in contract.accepted_segment_kinds
            ):
                raise FactValidationError(
                    "statement wording does not replay from declared sources"
                )
            if attribution not in attribution_segment.exact_text:
                raise FactValidationError(
                    "statement attribution does not replay from declared source"
                )
        else:
            matching = by_page_and_text.get((page_no, description), [])
            if len(matching) != 1:
                raise FactValidationError(
                    "statement wording must resolve to one exact prose span"
                )
            value_segment = attribution_segment = matching[0]
            if value_segment.kind not in contract.accepted_segment_kinds:
                raise FactValidationError(
                    "statement wording requires prose span support"
                )
            if attribution not in attribution_segment.exact_text:
                raise FactValidationError(
                    "statement wording needs exact attribution in its source span"
                )
            links = (
                ("value_source", value_segment.id),
                ("attribution_source", attribution_segment.id),
            )
        if candidate.id is None:
            raise FactValidationError("statement Candidate needs an immutable identity")
        subject_key = f"candidate:{candidate.id}"
        materialized = materialize_prose_wording(
            value_segment, attribution_segment, attribution=attribution
        )
        if materialized.text_value != description:
            raise FactReplayMismatch(
                "statement wording does not reproduce from its value source"
            )
        fact = append_fact(
            session,
            project_id=document.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            subject_kind=contract.subject_kind,
            subject_key=subject_key,
            recorded_by=f"extractor:{run.prompt_version}",
            content_sha256=_fact_digest(
                run_identity={
                    "document_id": document.id,
                    "prompt_version": run.prompt_version,
                    "schema_version": run.schema_version,
                    "model": run.model,
                    "extractor_config_sha256": run.extractor_config_sha256,
                },
                subject_kind=contract.subject_kind,
                subject_key=subject_key,
                value=materialized,
            ),
            value=materialized,
        )
        appended.append(fact)
    session.flush()
    return tuple(appended)


def _is_statement_wording_candidate(candidate: Candidate) -> bool:
    if candidate.kind != "event":
        return False
    fields = candidate.payload_json.get("fields") or {}
    description = fields.get("description")
    attribution = fields.get("stated_party") or fields.get("external_org")
    citations = candidate.payload_json.get("citations") or []
    declared_links = candidate.payload_json.get("fact_sources")
    declared_roles = (
        {
            link.get("role")
            for link in declared_links
            if isinstance(link, dict)
        }
        if isinstance(declared_links, list)
        else set()
    )
    has_declared_sources = declared_roles == FACT_TYPE_CONTRACTS[
        "statement_wording"
    ].required_roles
    if (
        not isinstance(description, str)
        or not description.strip()
        or not isinstance(attribution, str)
        or (attribution not in description and not has_declared_sources)
        or len(citations) != 1
    ):
        return False
    citation = citations[0]
    page_no = citation.get("page")
    return (
        citation.get("verified") is True
        and citation.get("document_id") == candidate.source_document_id
        and isinstance(page_no, int)
        and not isinstance(page_no, bool)
        and citation.get("quote") == description
    )


_STATEMENT_SOURCE_KINDS = frozenset(
    {"recorded_verbal_statement", *PROSE_SEGMENT_KINDS}
)


def _statement_source_run_identity(segment: SourceSegment) -> dict[str, object]:
    """A stable, reproducible identity for a document-less statement Fact digest.

    A Recorded Verbal Statement identifies by the recorder's attestation — its
    spine-native Recorded Verbal origin; a Meeting Notes passage has no
    attestation, so it identifies by the exact segment that carries it.  Both
    are unique per statement and reproduce on replay.

    The verbal branch named the legacy ``dependency_events`` row until #512,
    which put a legacy Project Record key inside the Fact identity hash.
    ADR-0081 stage 1 replaced it, and the transition reconciled every digest it
    changed against an attributable receipt (``recorded_verbal_origin_fact_digests``).
    """

    if segment.kind == "recorded_verbal_statement":
        return {"recorded_verbal_origin_id": segment.recorded_verbal_origin_id}
    return {"source_segment_id": segment.id}


def _certify_statement_source_segment(segment: SourceSegment) -> None:
    """Fail closed unless the value-source segment proves its own stored words."""

    if segment.kind == "recorded_verbal_statement":
        replay_recorded_verbal_statement(segment)
    elif is_prose_segment(segment):
        if sha256(segment.exact_text.encode("utf-8")).hexdigest() != (
            segment.content_sha256
        ):
            raise FactReplayMismatch(
                "prose segment digest does not match its stored words"
            )
    else:
        raise FactValidationError("statement Fact source is not a supported segment")


def append_recorded_statement_timing_fact(
    session: Session,
    *,
    segment: SourceSegment,
    subject_key: str,
    timings: tuple[tuple[str, StatementTiming], ...],
    recorded_by: str,
) -> Fact:
    """Append one human-attributed statement_timing Fact over a statement segment.

    A verbal or a Meeting Notes passage may support the timing (ADR-0074): the
    fact type is source-neutral.  The Fact is document-less and human-attributed;
    the segment supplies evidence context, and the timing set the party stated is
    carried in the typed satellite.  Integrity is the digest's self-consistency
    with that stored set — there are no external bytes to dereference.
    """

    if segment.kind not in _STATEMENT_SOURCE_KINDS:
        raise FactValidationError(
            "statement timing requires a verbal or prose statement segment"
        )
    if not subject_key.strip():
        raise FactValidationError("statement timing needs a statement subject")
    if not recorded_by.strip():
        raise FactValidationError("statement timing needs a recorder attribution")
    ordered = _validated_statement_timings(timings)
    segment = append_source_segment(session, segment)
    structured = _statement_timing_structured(ordered)
    materialized = materialize_typed_satellite("statement_timing", segment)
    return append_fact(
        session,
        project_id=segment.project_id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="statement_candidate",
        subject_key=subject_key,
        recorded_by=recorded_by,
        content_sha256=_fact_digest(
            run_identity=_statement_source_run_identity(segment),
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialized,
            structured_value=structured,
        ),
        value=materialized,
        timings=tuple(
            TimingValues(
                role=role,
                text=timing.text,
                precision=timing.precision,
                start_date=timing.start_date,
                end_date=timing.end_date,
            )
            for role, timing in ordered
        ),
    )


def replay_recorded_statement_timing_fact(
    session: Session, fact: Fact
) -> StatementTimingFactValue:
    """Replay a statement_timing Fact from its own words and stored timing set.

    There is no Document to dereference (ADR-0033/0068): the value source is the
    self-certifying verbal segment, and the stored timing members must reproduce
    the Fact digest exactly.  A tampered word, digest, or timing member fails
    closed rather than returning a value.
    """

    if fact.fact_type != "statement_timing":
        raise FactValidationError("Fact is not a recorded statement timing")
    sources = tuple(
        session.scalars(
            select(FactSource).where(
                FactSource.fact_id == fact.id,
                FactSource.role == "value_source",
            )
        ).all()
    )
    if len(sources) != 1:
        raise FactValidationError("statement timing Fact needs one value source")
    segment = session.get(SourceSegment, sources[0].source_segment_id)
    if segment is None or segment.project_id != fact.project_id:
        raise FactValidationError("statement timing Fact source is missing")
    _certify_statement_source_segment(segment)
    rows = tuple(
        session.scalars(
            select(FactStatementTiming)
            .where(FactStatementTiming.fact_id == fact.id)
            .order_by(FactStatementTiming.timing_role)
        ).all()
    )
    if not rows:
        raise FactValidationError("statement timing Fact has no stored timing members")
    timings = tuple(
        (
            row.timing_role,
            StatementTiming(
                text=row.text,
                precision=row.precision,
                start_date=row.start_date,
                end_date=row.end_date,
            ),
        )
        for row in rows
    )
    expected = _fact_digest(
        run_identity=_statement_source_run_identity(segment),
        subject_kind="statement_candidate",
        subject_key=fact.subject_key,
        value=materialize_typed_satellite("statement_timing", segment),
        structured_value=_statement_timing_structured(timings),
    )
    if expected != fact.content_sha256:
        raise FactReplayMismatch(
            "statement timing set does not reproduce the Fact digest"
        )
    return StatementTimingFactValue(timings)


def append_recorded_statement_wording_fact(
    session: Session,
    *,
    segment: SourceSegment,
    subject_key: str,
    description: str,
    recorded_by: str,
) -> Fact:
    """Append one human-attributed statement_wording Fact over a statement segment.

    A Recorded Verbal Statement's own words self-certify; a Meeting Notes passage
    supports the same wording as evidence (ADR-0074).  Either way the segment
    carries both roles the wording contract requires, and the Fact is
    document-less and human-attributed.
    """

    if segment.kind not in _STATEMENT_SOURCE_KINDS:
        raise FactValidationError(
            "statement wording requires a verbal or prose statement segment"
        )
    if not subject_key.strip():
        raise FactValidationError("statement wording needs a statement subject")
    if not recorded_by.strip():
        raise FactValidationError("statement wording needs a recorder attribution")
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
    segment = append_source_segment(session, segment)
    materialized = materialize_quoted_statement_wording(segment, description)
    return append_fact(
        session,
        project_id=segment.project_id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="statement_candidate",
        subject_key=subject_key,
        recorded_by=recorded_by,
        content_sha256=_fact_digest(
            run_identity=_statement_source_run_identity(segment),
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialized,
        ),
        value=materialized,
    )


def append_recorded_applies_to_fact(
    session: Session,
    *,
    segment: SourceSegment,
    subject_key: str,
    dependency_ids: tuple[int, ...],
    recorded_by: str,
) -> Fact:
    """Append one human-attributed Applies To Fact for a statement's scope.

    The scope is the coordinator's decision — one, several, or not yet known —
    carried in the ``fact_applies_to`` satellite exactly as the spreadsheet path
    carries it, but sourced from a document-less statement segment (verbal or a
    Meeting Notes passage).  An unknown scope is an explicit empty member set,
    never an omitted Fact (ADR-0074).
    """

    if segment.kind not in _STATEMENT_SOURCE_KINDS:
        raise FactValidationError(
            "statement Applies To requires a verbal or prose statement segment"
        )
    if not subject_key.strip():
        raise FactValidationError("statement Applies To needs a statement subject")
    if not recorded_by.strip():
        raise FactValidationError("statement Applies To needs a recorder attribution")
    if len(set(dependency_ids)) != len(dependency_ids):
        raise FactValidationError("statement Applies To members must be unique")
    segment = append_source_segment(session, segment)
    structured = {"dependency_ids": list(dependency_ids)}
    materialized = materialize_typed_satellite("applies_to", segment)
    return append_fact(
        session,
        project_id=segment.project_id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="statement_candidate",
        subject_key=subject_key,
        recorded_by=recorded_by,
        content_sha256=_fact_digest(
            run_identity=_statement_source_run_identity(segment),
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialized,
            structured_value=structured,
        ),
        value=materialized,
        applies_to=dependency_ids,
    )


def replay_recorded_statement_wording_fact(
    session: Session, fact: Fact
) -> str:
    """Replay a verbal statement_wording Fact from its own recorded words."""

    if fact.fact_type != "statement_wording":
        raise FactValidationError("Fact is not a recorded statement wording")
    segment = _statement_value_segment(session, fact)
    _certify_statement_source_segment(segment)
    try:
        materialized = materialize_quoted_statement_wording(
            segment, fact.text_value or ""
        )
    except FactValidationError as exc:
        raise FactReplayMismatch(
            "statement wording does not reproduce the Fact digest"
        ) from exc
    expected = _fact_digest(
        run_identity=_statement_source_run_identity(segment),
        subject_kind="statement_candidate",
        subject_key=fact.subject_key,
        value=materialized,
    )
    if expected != fact.content_sha256:
        raise FactReplayMismatch(
            "statement wording does not reproduce the Fact digest"
        )
    return materialized.text_value


def replay_recorded_applies_to_fact(
    session: Session, fact: Fact
) -> AppliesToFactValue:
    """Replay a verbal Applies To Fact from its stored scope members."""

    if fact.fact_type != "applies_to":
        raise FactValidationError("Fact is not an applies_to fact")
    segment = _statement_value_segment(session, fact)
    _certify_statement_source_segment(segment)
    dependency_ids = tuple(
        session.scalars(
            select(FactAppliesTo.dependency_id)
            .where(FactAppliesTo.fact_id == fact.id)
            .order_by(FactAppliesTo.ordinal)
        ).all()
    )
    expected = _fact_digest(
        run_identity=_statement_source_run_identity(segment),
        subject_kind="statement_candidate",
        subject_key=fact.subject_key,
        value=materialize_typed_satellite("applies_to", segment),
        structured_value={"dependency_ids": list(dependency_ids)},
    )
    if expected != fact.content_sha256:
        raise FactReplayMismatch("verbal Applies To does not reproduce the Fact digest")
    return AppliesToFactValue(dependency_ids)


def _statement_value_segment(session: Session, fact: Fact) -> SourceSegment:
    sources = tuple(
        session.scalars(
            select(FactSource).where(
                FactSource.fact_id == fact.id,
                FactSource.role == "value_source",
            )
        ).all()
    )
    if len(sources) != 1:
        raise FactValidationError("statement Fact needs one value source")
    segment = session.get(SourceSegment, sources[0].source_segment_id)
    if segment is None or segment.project_id != fact.project_id:
        raise FactValidationError("statement Fact source is missing")
    if segment.kind not in _STATEMENT_SOURCE_KINDS:
        raise FactValidationError("statement Fact source is not a statement segment")
    return segment


def _validated_statement_timings(
    timings: tuple[tuple[str, StatementTiming], ...],
) -> tuple[tuple[str, StatementTiming], ...]:
    if not timings:
        raise FactValidationError("a statement timing Fact needs at least one timing")
    roles = [role for role, _timing in timings]
    if len(set(roles)) != len(roles):
        raise FactValidationError("statement timing roles must be unique")
    if any(role not in ("previous", "new") for role in roles):
        raise FactValidationError("statement timing role must be previous or new")
    if "previous" in roles and "new" not in roles:
        raise FactValidationError(
            "a previous timing only stands beside the new timing it changed"
        )
    for _role, timing in timings:
        if not isinstance(timing, StatementTiming):
            raise FactValidationError("statement timing must be a stated timing")
        if not timing.text.strip():
            raise FactValidationError("a stated timing needs the party's words")
        if timing.precision not in ("day", "month", "approximate"):
            raise FactValidationError("stated timing precision is not supported")
    return tuple(sorted(timings, key=lambda item: item[0]))


def _statement_timing_structured(
    timings: tuple[tuple[str, StatementTiming], ...],
) -> dict:
    return {
        "timings": [
            {
                "role": role,
                "text": timing.text,
                "precision": timing.precision,
                "start_date": (
                    timing.start_date.isoformat()
                    if timing.start_date is not None
                    else None
                ),
                "end_date": (
                    timing.end_date.isoformat()
                    if timing.end_date is not None
                    else None
                ),
            }
            for role, timing in timings
        ]
    }


def append_supporting_documentation_fact(
    session: Session,
    *,
    project_id: int,
    subject_key: str,
    document_id: int,
    recorded_by: str,
) -> Fact:
    """Append or reuse the relationship Fact for one supporting document.

    Supporting Documentation in Use relates one Project Record subject to one
    immutable document revision (ADR-0074 stage 3).  The registered document
    row is the identity — there is no segment to replay — and the digest keeps
    one relationship Fact per (subject, document): a re-designation after a
    resolution re-decides the same Fact rather than rewriting a set.
    """

    if not subject_key.strip():
        raise FactValidationError("supporting documentation needs a record subject")
    if not recorded_by.strip():
        raise FactValidationError("supporting documentation needs a recorder")
    document = session.get(Document, document_id)
    if document is None or document.project_id != project_id:
        raise FactValidationError(
            "supporting documentation needs a registered project document"
        )
    materialized = materialize_document_reference(document_id)
    digest = _fact_digest(
        run_identity={"document_value_id": document_id},
        subject_kind="record_subject",
        subject_key=subject_key,
        value=materialized,
    )
    existing = session.scalar(
        select(Fact).where(Fact.content_sha256 == digest)
    )
    if existing is not None:
        return existing
    return append_fact(
        session,
        project_id=project_id,
        document_id=None,
        extraction_run_id=None,
        subject_kind="record_subject",
        subject_key=subject_key,
        recorded_by=recorded_by,
        content_sha256=digest,
        value=materialized,
    )


def append_extracted_proposals(
    session: Session,
    document: Document,
    run: ExtractionRun,
    candidates: tuple[Candidate, ...],
    facts: tuple[Fact, ...],
) -> tuple[ExtractedProposal, ...]:
    """Group one source row's Facts by reference without copying their values."""

    by_subject: dict[str, list[Fact]] = {}
    for fact in facts:
        by_subject.setdefault(fact.subject_key, []).append(fact)
    candidates_by_subject = {}
    for candidate in candidates:
        if candidate.payload_json.get("tier") == READER_PATH:
            subject = candidate.payload_json.get("native_row_id")
            if not isinstance(subject, str) or not subject:
                raise FactValidationError("native proposal needs its scoped source row")
            candidates_by_subject[subject] = candidate
            continue
        if _is_statement_wording_candidate(candidate) and candidate.id is not None:
            candidates_by_subject[f"candidate:{candidate.id}"] = candidate
            continue
        citation = (candidate.payload_json.get("citations") or [{}])[0]
        sheet_name = citation.get("sheet_name")
        table_row = citation.get("table_row")
        if isinstance(sheet_name, str) and isinstance(table_row, int):
            rows = _segment_rows(
                tuple(
                    session.scalars(
                        select(SourceSegment).where(
                            SourceSegment.document_id == document.id,
                            SourceSegment.sheet_name == sheet_name,
                        )
                    ).all()
                )
            )[sheet_name]
            header_row, _mapping = _header_mapping(rows)
            candidates_by_subject[f"{sheet_name}!{header_row + table_row}"] = candidate
    proposals = []
    for subject_key, subject_facts in sorted(by_subject.items()):
        candidate = candidates_by_subject.get(subject_key)
        if candidate is None:
            raise FactValidationError("Fact subject has no Extracted Proposal identity")
        proposal = append_extracted_proposal(
            session,
            project_id=document.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            candidate_id=candidate.id,
            kind=candidate.kind,
            subject_key=subject_key,
            candidate_metadata=_proposal_candidate_metadata(candidate),
            fact_ids=tuple(
                fact.id
                for fact in sorted(subject_facts, key=lambda item: item.fact_type)
            ),
        )
        proposals.append(proposal)
    session.flush()
    return tuple(proposals)


def correct_fact(
    session: Session,
    predecessor: Fact,
    *,
    source_segment_id: int,
    principal: HumanPrincipal,
) -> FactDisposition:
    """Append a corrected reading and typed predecessor disposition."""

    actor = require_human_principal(principal)
    segment = session.get(SourceSegment, source_segment_id)
    if segment is None or segment.document_id != predecessor.document_id:
        raise FactValidationError("corrected Fact source belongs to another rendition")
    contract = FACT_TYPE_CONTRACTS.get(predecessor.fact_type)
    if contract is None or segment.kind not in contract.accepted_segment_kinds:
        raise FactValidationError("corrected Fact source kind violates its contract")
    materialized = materialize_segment_value(session, predecessor.fact_type, segment)
    successor = append_fact(
        session,
        project_id=predecessor.project_id,
        document_id=predecessor.document_id,
        extraction_run_id=predecessor.extraction_run_id,
        subject_kind=predecessor.subject_kind,
        subject_key=predecessor.subject_key,
        recorded_by=actor.subject,
        content_sha256=_fact_digest(
            run_identity={
                "document_id": predecessor.document_id,
                "extraction_run_id": predecessor.extraction_run_id,
                "correction_of": predecessor.id,
            },
            subject_kind=predecessor.subject_kind,
            subject_key=predecessor.subject_key,
            value=materialized,
        ),
        value=materialized,
    )
    disposition = FactDisposition(
        project_id=successor.project_id,
        predecessor_fact_id=predecessor.id,
        successor_fact_id=successor.id,
        kind="source_reading_correction",
        recorded_by=actor.subject,
    )
    session.add(disposition)
    session.flush()
    return disposition


def proposal_input_snapshots(session: Session, run: ExtractionRun) -> list[dict]:
    """Project immutable proposal Fact references into legacy reader shape."""

    proposals = session.scalars(
        select(ExtractedProposal)
        .where(ExtractedProposal.extraction_run_id == run.id)
        .order_by(ExtractedProposal.id)
    ).all()
    snapshots = []
    for proposal in proposals:
        metadata = proposal.candidate_metadata_json
        links = session.scalars(
            select(ExtractedProposalFact)
            .where(ExtractedProposalFact.proposal_id == proposal.id)
            .order_by(ExtractedProposalFact.ordinal)
        ).all()
        fields = deepcopy(metadata.get("event_fields") or {})
        if (metadata.get("payload_json") or {}).get("tier") == READER_PATH:
            fields.update(_native_refused_proposal_fields(session, proposal, metadata))
        for link in links:
            fact = session.get(Fact, link.fact_id)
            if fact is None:
                continue
            if fact.fact_type == "closure_result":
                continue
            field_name = (
                "description"
                if fact.fact_type == "statement_wording"
                else fact.fact_type
            )
            if fact.fact_type == "applies_to":
                source = session.scalar(
                    select(SourceSegment)
                    .join(FactSource, FactSource.source_segment_id == SourceSegment.id)
                    .where(
                        FactSource.fact_id == fact.id,
                        FactSource.role == "value_source",
                    )
                )
                fields[field_name] = source.exact_text if source is not None else None
            else:
                fields[field_name] = (
                    fact.date_value.isoformat()
                    if fact.date_value is not None
                    else fact.text_value
                )
        payload = dict(metadata.get("payload_json") or {})
        payload["fields"] = fields
        snapshots.append(
            {
                "candidate_id": proposal.candidate_id,
                "project_id": proposal.project_id,
                "kind": proposal.kind,
                "source_document_id": proposal.document_id,
                "payload_json": payload,
                "source_pages": list(metadata.get("source_pages") or []),
                "confidence": metadata.get("confidence"),
                "prompt_version": metadata.get("prompt_version"),
                "model": metadata.get("model"),
                "citations_verified": metadata.get("citations_verified"),
                "state": metadata.get("state"),
            }
        )
    return snapshots


def _proposal_candidate_metadata(candidate: Candidate) -> dict:
    """Seal mutable metadata without copying source field or quote values."""

    payload = candidate.payload_json or {}
    event_fields = deepcopy(payload.get("fields") or {})
    event_fields.pop("description", None)
    citations = [
        {
            key: citation.get(key)
            for key in (
                "document_id",
                "page",
                "verified",
                "whole_row",
                "table_row",
                "sheet_name",
            )
            if key in citation
        }
        for citation in (payload.get("citations") or [])
    ]
    native_metadata = {
        key: deepcopy(payload[key])
        for key in ("native_row_id", "local_row_id", "page", "field_sources", "field_materialization")
        if payload.get("tier") == READER_PATH and key in payload
    }
    return {
        "source_pages": list(candidate.source_pages or []),
        "confidence": candidate.confidence,
        "prompt_version": candidate.prompt_version,
        "model": candidate.model,
        "citations_verified": candidate.citations_verified,
        "state": candidate.state,
        "event_fields": event_fields if candidate.kind == "event" else {},
        "payload_json": {
            "kind": payload.get("kind"),
            "citations": citations,
            "unverified_fields": list(payload.get("unverified_fields") or []),
            "unmapped_columns": list(payload.get("unmapped_columns") or []),
            "low_confidence_tokens": list(payload.get("low_confidence_tokens") or []),
            "tier": payload.get("tier"),
            "text_source": payload.get("text_source"),
            **native_metadata,
        },
    }


def _native_refused_proposal_fields(session, proposal, metadata) -> dict[str, str]:
    """A declined date Fact does not erase the source-bound proposal wording."""
    payload = metadata["payload_json"]
    document = session.get_one(Document, proposal.document_id)
    fields = {}
    for outcome in payload["field_materialization"]:
        if outcome["status"] != "refused":
            continue
        name = outcome["field"]
        if (outcome["reason"] != "non_iso_date"
                or FACT_TYPE_CONTRACTS[name].value_class != "date"):
            raise FactValidationError("unknown native field materialization refusal")
        sources = payload["field_sources"][name]
        if len(sources["value_source"]) != 1 or sources["context"]:
            raise FactValidationError("a refused date needs its exact source cell")
        reference = sources["value_source"][0]
        segment = session.get(SourceSegment, reference["segment_id"])
        if (segment is None or segment.project_id != proposal.project_id
                or segment.document_id != proposal.document_id or segment.kind != "pdf_cell"):
            raise FactValidationError("a refused date source crosses its proposal")
        scoped = pdf_cell_id(document, segment.reading_sha256, segment.page_no,
                             segment.table_index, segment.cell_row, segment.cell_column)
        if (scoped != reference["scoped_id"]
                or scoped.rsplit(":c", 1)[0] != payload["native_row_id"]):
            raise FactValidationError("a refused date source crosses its row")
        fields[name] = clean_pdf_source_text(segment.exact_text)
    return fields


def replay_fact(
    session: Session, document: Document, fact: Fact, path: Path | str,
    *, native_reading: NativePdfReading | None = None,
) -> str | date | AppliesToFactValue | ClosureFactValue:
    """Replay one typed Fact from original bytes through its named transform."""

    return _replay_fact(session, document, fact, path, native_reading=native_reading)


def replay_spreadsheet_facts(
    session: Session, document: Document, facts: Iterable[Fact], path: Path | str,
) -> tuple[str | date | AppliesToFactValue | ClosureFactValue, ...]:
    """Replay a whole workbook batch or refuse it, with one source decode.

    Each Fact retains the single-replay support, locator, digest, and typed-value
    checks. The source context must close successfully before any result leaves
    this operation, including when a later Fact or changed source is refused.
    """

    batch = tuple(facts)
    if any(fact.document_id != document.id or fact.project_id != document.project_id
           for fact in batch):
        raise FactValidationError("Fact belongs to another Document")
    with spreadsheet_replay(document, path) as replay_cell:
        result = tuple(
            _replay_fact(session, document, fact, path, replay_cell=replay_cell)
            for fact in batch
        )
    return result


def _replay_fact(
    session: Session, document: Document, fact: Fact, path: Path | str,
    *, native_reading: NativePdfReading | None = None,
    replay_cell: Callable[[SourceSegment], str] | None = None,
) -> str | date | AppliesToFactValue | ClosureFactValue:
    with session.no_autoflush:
        sources = tuple(
            session.scalars(
                select(FactSource)
                .where(FactSource.fact_id == fact.id)
                .order_by(FactSource.role, FactSource.ordinal)
            ).all()
        )
        contract = FACT_TYPE_CONTRACTS.get(fact.fact_type)
        if contract is None:
            raise FactValidationError(f"unknown Fact type {fact.fact_type!r}")
        segments = tuple(session.get(SourceSegment, source.source_segment_id) for source in sources)
        if replay_cell is not None and any(
            segment is not None and segment.kind != "spreadsheet_cell" for segment in segments
        ):
            raise FactValidationError("spreadsheet Fact replay needs cell sources")
        # The native replay below is the matrix mapping's: value and context
        # cells of one sealed reading, in the order that mapping produced them
        # (#737, #758). A statement Fact is a different contract with a
        # different role shape -- it is document-less and human-attributed --
        # and its value source is a prose span, which since #741 is a
        # `pdf_span` on the page stream rather than the retired reader's
        # `prose_span`. Sending it into the matrix replay would fail it on the
        # roles rather than on anything about the reading.
        if fact.subject_kind != "statement_candidate" and any(
            segment is not None and segment.kind in {"pdf_cell", "pdf_span"}
            for segment in segments
        ):
            return _replay_native_fact(session, document, fact, sources, segments, path, native_reading)
        roles = {source.role for source in sources}
        if roles != contract.required_roles:
            raise FactValidationError("Fact support roles do not match its contract")
        if fact.fact_type == "applies_to" and fact.subject_kind == "statement_candidate" and fact.document_id is not None:
            return _replay_minutes_scope(session, document, fact, sources, path)
        value_sources = tuple(
            source for source in sources if source.role == "value_source"
        )
        if len(value_sources) != 1:
            raise FactValidationError("Fact needs one value source")
        segment = session.get(SourceSegment, value_sources[0].source_segment_id)
    if segment is None or segment.document_id != fact.document_id:
        raise FactValidationError("Fact source belongs to another rendition")
    if segment.kind not in contract.accepted_segment_kinds:
        raise FactValidationError("Fact source kind does not match its contract")
    exact = (replay_cell(segment) if replay_cell is not None
             else dereference_source_segment(document, segment, path))
    if fact.fact_type == "statement_timing":
        from corridor.statement_timing_parser import promised_timing_options

        options = [option.value for option in promised_timing_options(exact)]
        rows = session.scalars(select(FactStatementTiming).where(FactStatementTiming.fact_id == fact.id)
                               .order_by(FactStatementTiming.timing_role)).all()
        if not rows:
            raise FactReplayMismatch("minutes timing has no stated member")
        for row in rows:
            observed = {"text": row.text, "precision": row.precision,
                        "start_date": row.start_date.isoformat() if row.start_date else None,
                        "end_date": row.end_date.isoformat() if row.end_date else None}
            if observed not in options:
                raise FactReplayMismatch("minutes timing does not replay from its exact source")
        return StatementTimingFactValue(tuple((row.timing_role, StatementTiming(
            text=row.text, precision=row.precision, start_date=row.start_date, end_date=row.end_date)) for row in rows))
    if segment.kind == "email_span":
        from corridor.materializer import materialize_email_metadata, materialize_prose_wording

        if fact.fact_type == "statement_wording":
            attribution_sources = [source for source in sources if source.role == "attribution_source"]
            if len(attribution_sources) != 1:
                raise FactValidationError("email Fact requires one attribution source")
            attribution = session.get(SourceSegment, attribution_sources[0].source_segment_id)
            if attribution is None:
                raise FactValidationError("email attribution source is missing")
            attribution_text = dereference_source_segment(document, attribution, path)
            materialize_prose_wording(segment, attribution, attribution=attribution_text)
        else:
            if materialize_email_metadata(segment).fact_type != fact.fact_type:
                raise FactValidationError("email metadata Fact has the wrong part type")
    if fact.fact_type == "applies_to":
        expected_ids = _resolve_applies_to_dependencies(
            session, fact.project_id, exact
        )
        stored_ids = tuple(
            session.scalars(
                select(FactAppliesTo.dependency_id)
                .where(FactAppliesTo.fact_id == fact.id)
                .order_by(FactAppliesTo.ordinal)
            ).all()
        )
        if stored_ids != expected_ids:
            raise FactReplayMismatch("Applies To members do not reproduce")
        return AppliesToFactValue(stored_ids)
    if fact.fact_type == "closure_result":
        result = session.scalar(
            select(FactClosureResult).where(FactClosureResult.fact_id == fact.id)
        )
        governing = tuple(
            session.scalars(
                select(FactClosureSource.source_segment_id)
                .where(FactClosureSource.fact_id == fact.id)
                .order_by(FactClosureSource.ordinal)
            ).all()
        )
        if result is None or not governing:
            raise FactValidationError("closure Fact is missing its typed satellites")
        for source_segment_id in governing:
            governing_segment = session.get(SourceSegment, source_segment_id)
            if governing_segment is None or governing_segment.document_id != document.id:
                raise FactValidationError("closure governing source crosses rendition")
            if replay_cell is not None:
                replay_cell(governing_segment)
            else:
                dereference_source_segment(document, governing_segment, path)
        if result.closure_kind == "source_marked_resolved" and not exact.strip():
            raise FactReplayMismatch("source closure mark is empty")
        if result.closure_kind == "completion_reported":
            from corridor.statement_values import reports_completion

            if not reports_completion(exact):
                raise FactReplayMismatch("source does not explicitly report completion")
        return ClosureFactValue(
            closure_kind=result.closure_kind,
            successor_dependency_id=result.successor_dependency_id,
            governing_source_segment_ids=governing,
        )
    replayed = validated_scalar_value(contract, exact)
    materialized = fact.date_value if fact.date_value is not None else fact.text_value
    if replayed != materialized:
        raise FactReplayMismatch("materialized Fact value does not reproduce")
    return replayed


def _replay_minutes_scope(session, document, fact, sources, path):
    from corridor.statement_values import states_unknown_scope

    text_by_id = {}
    for source in sources:
        segment = session.get(SourceSegment, source.source_segment_id)
        if segment is None:
            raise FactReplayMismatch("minutes scope source is missing")
        text_by_id[segment.id] = dereference_source_segment(document, segment, path)
    rows = session.scalars(select(FactAppliesTo).where(FactAppliesTo.fact_id == fact.id)
                           .order_by(FactAppliesTo.ordinal)).all()
    if not rows:
        if not any(states_unknown_scope(text) for text in text_by_id.values()):
            raise FactReplayMismatch("empty minutes scope was not explicitly stated unknown")
        return AppliesToFactValue(())
    for row in rows:
        if (row.record_subject_key is None or not row.reference_text
                or row.reference_text not in text_by_id.get(row.source_segment_id, "")):
            raise FactReplayMismatch("minutes scope member does not replay from its own source")
    return AppliesToFactValue((), tuple(row.record_subject_key for row in rows))


def _replay_native_fact(session, document, fact, sources, segments, path, reading):
    if fact.document_id != document.id or fact.project_id != document.project_id:
        raise FactValidationError("native Fact belongs to another Document")
    if sha256(Path(path).read_bytes()).hexdigest() != document.sha256:
        raise FactReplayMismatch("native Fact source bytes changed")
    if not segments or any(segment is None for segment in segments):
        raise FactValidationError("native Fact is missing its source")
    if reading is None:
        native = segments[0].reader_identity["native_layer"]
        reading = read_native_pdf(
            path, source_sha256=document.sha256,
            engine=native["configuration"]["reader_engine"], dpi=native["dpi"],
        )
    if not isinstance(reading, NativePdfReading) or reading.rendition_sha256 != document.sha256:
        raise FactValidationError("native Fact replay needs the same sealed reading")
    expected = native_replay_index(reading)
    grouped = {"value_source": [], "context": []}
    for source, segment in zip(sources, segments, strict=True):
        encoded = expected.get((segment.kind, segment.ordinal))
        if (segment.project_id != fact.project_id or segment.document_id != fact.document_id
                or encoded is None or any(getattr(segment, key) != value for key, value in json.loads(encoded).items())):
            raise FactReplayMismatch("native Fact locator does not reproduce from its reading")
        if source.role not in grouped or source.ordinal != len(grouped[source.role]) + 1:
            raise FactValidationError("native Fact source roles or order differ from the replay contract")
        grouped[source.role].append(segment)
    materialized = replay_pdf_materialized_value(
        session, fact.fact_type, fact.transformation,
        grouped["value_source"], grouped["context"],
    )
    stored = fact.date_value if fact.date_value is not None else fact.text_value
    if materialized.scalar != stored:
        raise FactReplayMismatch("native Fact value does not reproduce from its ordered sources")
    return materialized.scalar


def replay_proposed_fact_value(
    fact_type: str, exact_value_sources: tuple[str, ...]
) -> str:
    """Replay a proposed typed value before it reaches the append command."""

    contract = FACT_TYPE_CONTRACTS.get(fact_type)
    if contract is None:
        raise FactValidationError(f"unknown Fact type {fact_type!r}")
    if len(exact_value_sources) != 1:
        raise FactValidationError("Fact proposal needs one value source")
    return transform(contract.transformation, exact_value_sources[0])


def _segment_rows(segments: tuple[SourceSegment, ...]) -> dict[str, dict[int, dict[int, str]]]:
    rows: dict[str, dict[int, dict[int, str]]] = {}
    for segment in segments:
        column_letters, row_number = coordinate_from_string(segment.cell_range)
        column_number = column_index_from_string(column_letters)
        rows.setdefault(segment.sheet_name, {}).setdefault(row_number, {})[
            column_number
        ] = segment.exact_text
    return rows


def _header_mapping(rows: dict[int, dict[int, str]]) -> tuple[int, dict[int, str]]:
    for row_number in sorted(rows):
        cells = rows[row_number]
        values = [cells.get(index, "") for index in range(1, max(cells) + 1)]
        mapping = column_mapping(values)
        if any(name in FACT_TYPE_CONTRACTS for name in mapping.values()):
            return row_number, mapping
    raise FactValidationError("segment sheet has no controlled structured-cell header")


def _cell_range(column_number: int, row_number: int) -> str:
    return f"{get_column_letter(column_number)}{row_number}"


def _candidate_typed_value(contract: FactTypeContract, value: object) -> str | date:
    if contract.value_class == "date":
        return validated_scalar_value(contract, str(value))
    return str(value).strip()


def _resolve_applies_to_dependencies(
    session: Session, project_id: int, exact_text: str
) -> tuple[int, ...]:
    references = tuple(
        value.strip() for value in exact_text.split(",") if value.strip()
    )
    if not references or len(set(references)) != len(references):
        raise FactValidationError("Applies To needs a non-empty unique reference set")
    rows = session.execute(
        select(Dependency.ref_code, Dependency.id).where(
            Dependency.project_id == project_id,
            Dependency.ref_code.in_(references),
        )
    ).all()
    by_reference = {reference: dependency_id for reference, dependency_id in rows}
    if set(by_reference) != set(references):
        raise FactValidationError(
            "Applies To references must exactly name registered project Constraints"
        )
    return tuple(by_reference[reference] for reference in references)


def _append_fact_envelope(
    session: Session,
    document: Document,
    run: ExtractionRun,
    *,
    fact_type: str,
    subject_kind: str,
    subject_key: str,
    segment: SourceSegment,
    structured_value: object,
    applies_to: tuple[int, ...] | None = None,
    closure: ClosureValues | None = None,
) -> Fact:
    """Append one structured-value Fact whose value lives in a typed satellite."""

    materialized = materialize_typed_satellite(fact_type, segment)
    return append_fact(
        session,
        project_id=document.project_id,
        document_id=document.id,
        extraction_run_id=run.id,
        subject_kind=subject_kind,
        subject_key=subject_key,
        recorded_by=f"extractor:{run.prompt_version}",
        content_sha256=_fact_digest(
            run_identity={
                "document_id": document.id,
                "prompt_version": run.prompt_version,
                "schema_version": run.schema_version,
                "model": run.model,
                "extractor_config_sha256": run.extractor_config_sha256,
            },
            subject_kind=subject_kind,
            subject_key=subject_key,
            value=materialized,
            structured_value=structured_value,
        ),
        value=materialized,
        applies_to=applies_to,
        closure=closure,
    )


def fact_identity_digest(
    *,
    run_identity: dict[str, object],
    subject_kind: str,
    subject_key: str,
    value: MaterializedValue,
    structured_value: object | None = None,
) -> str:
    """The Fact identity recipe, for a writer of captures outside this module.

    #842's source-grounded re-capture appends a Source Fact the same way every
    other capture is appended, and the digest is what makes an exact replay of
    that correction the same Fact rather than a second one
    (``uq_facts_content_sha256``). It reads this recipe rather than spelling
    one of its own: a second digest recipe would give one capture two
    identities, which is what the uniqueness constraint exists to prevent.
    """

    return _fact_digest(
        run_identity=run_identity,
        subject_kind=subject_kind,
        subject_key=subject_key,
        value=value,
        structured_value=structured_value,
    )


def _fact_digest(
    *,
    run_identity: dict[str, object],
    subject_kind: str,
    subject_key: str,
    value: MaterializedValue,
    structured_value: object | None = None,
) -> str:
    digest_input = {
        "run": run_identity,
        "fact_type": value.fact_type,
        "subject_kind": subject_kind,
        "subject_key": subject_key,
        "text_value": value.text_value,
        "date_value": (
            value.date_value.isoformat() if value.date_value is not None else None
        ),
        "external_org_value_id": value.external_org_value_id,
        "structured_value": structured_value,
        "source_links": [
            {"role": role, "source_segment_id": segment_id}
            for role, segment_id in value.source_links
        ],
    }
    return sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
