"""Append and replay typed source Facts over exact Source Segments.

Facts answer what one Document rendition said; they do not settle what the
Project Record says. Verified structured cells qualify for automatic Record
Inclusion; Minutes statement wording stays pending for a human decision. Both
materialized values reproduce through named transformations, and role-tagged
support stays constrained to the Fact's own rendition (ADR-0067, ADR-0069,
ADR-0070).
"""

from __future__ import annotations

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
    ExternalOrg,
    SourceSegment,
)
from corridor.fact_types import (
    FACT_TYPE_CONTRACTS,
    STRUCTURED_CELL_FACT_TYPES,
    FactTypeContract,
)
from corridor.identity import normalize_party
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.sheets import column_mapping
from corridor.source_segments import dereference_source_segment


class FactValidationError(ValueError):
    """A proposed Fact cannot replay from its declared source support."""


class FactReplayMismatch(FactValidationError):
    """A materialized Fact value differs from its replayed transformation."""


@dataclass(frozen=True)
class AppliesToFactValue:
    """One replayed, FK-backed Applies To reference set."""

    dependency_ids: tuple[int, ...]


@dataclass(frozen=True)
class ClosureFactValue:
    """One replayed typed closure source result."""

    closure_kind: str
    successor_dependency_id: int | None
    governing_source_segment_ids: tuple[int, ...]


def carries_source_facts(candidates: tuple[Candidate, ...]) -> bool:
    """Whether this extraction output belongs at the scoped Fact command."""

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
                    transformation=contract.transformation,
                    segment=segment,
                    structured_value={"dependency_ids": dependency_ids},
                )
                session.add_all(
                    FactAppliesTo(
                        project_id=document.project_id,
                        fact_id=fact.id,
                        dependency_id=dependency_id,
                        ordinal=ordinal,
                    )
                    for ordinal, dependency_id in enumerate(dependency_ids, 1)
                )
                appended.append(fact)
                continue
            value = _validated_scalar_value(contract, segment.exact_text)
            if value != fields[fact_type]:
                candidate_value = _candidate_typed_value(contract, fields[fact_type])
                if value != candidate_value:
                    raise FactReplayMismatch(
                        f"Candidate {fact_type} does not reproduce from {cell_range}"
                    )
            external_org_id = (
                _exact_registered_external_org_id(session, str(value))
                if fact_type == "external_org"
                else None
            )
            fact = Fact(
                project_id=document.project_id,
                document_id=document.id,
                extraction_run_id=run.id,
                fact_type=fact_type,
                subject_kind=contract.subject_kind,
                subject_key=f"{sheet_name}!{source_row}",
                text_value=value if isinstance(value, str) else None,
                date_value=value if isinstance(value, date) else None,
                date_range_start=None,
                date_range_end=None,
                external_org_value_id=external_org_id,
                document_value_id=None,
                transformation=contract.transformation,
                recorded_by=f"extractor:{run.prompt_version}",
                content_sha256=_fact_digest(
                    run_identity={
                        "document_id": document.id,
                        "prompt_version": run.prompt_version,
                        "schema_version": run.schema_version,
                        "model": run.model,
                        "extractor_config_sha256": run.extractor_config_sha256,
                    },
                    fact_type=fact_type,
                    subject_kind=contract.subject_kind,
                    subject_key=f"{sheet_name}!{source_row}",
                    text_value=value if isinstance(value, str) else None,
                    date_value=value if isinstance(value, date) else None,
                    external_org_value_id=external_org_id,
                    source_links=(("value_source", segment.id),),
                ),
            )
            session.add(fact)
            session.flush([fact])
            session.add(
                FactSource(
                    project_id=document.project_id,
                    document_id=document.id,
                    fact_id=fact.id,
                    source_segment_id=segment.id,
                    role="value_source",
                    ordinal=1,
                )
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
                    transformation=closure_contract.transformation,
                    segment=segment,
                    structured_value={
                        "closure_kind": "source_marked_resolved",
                        "successor_dependency_id": None,
                    },
                )
                session.add(
                    FactClosureResult(
                        project_id=document.project_id,
                        fact_id=closure.id,
                        closure_kind="source_marked_resolved",
                        successor_dependency_id=None,
                    )
                )
                session.add(
                    FactClosureSource(
                        project_id=document.project_id,
                        document_id=document.id,
                        fact_id=closure.id,
                        source_segment_id=segment.id,
                        ordinal=1,
                    )
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
                SourceSegment.kind == "prose_span",
            )
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    by_page_and_text: dict[tuple[int, str], list[SourceSegment]] = {}
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
        matching = by_page_and_text.get((page_no, description), [])
        if len(matching) != 1:
            raise FactValidationError(
                "statement wording must resolve to one exact prose span"
            )
        segment = matching[0]
        if segment.kind not in contract.accepted_segment_kinds:
            raise FactValidationError("statement wording requires prose span support")
        attribution = fields.get("stated_party") or fields.get("external_org")
        if not isinstance(attribution, str) or attribution not in description:
            raise FactValidationError(
                "statement wording needs exact attribution in its source span"
            )
        if candidate.id is None:
            raise FactValidationError("statement Candidate needs an immutable identity")
        subject_key = f"candidate:{candidate.id}"
        links = (
            ("value_source", segment.id),
            ("attribution_source", segment.id),
        )
        fact = Fact(
            project_id=document.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            fact_type="statement_wording",
            subject_kind=contract.subject_kind,
            subject_key=subject_key,
            text_value=description,
            date_value=None,
            date_range_start=None,
            date_range_end=None,
            external_org_value_id=None,
            document_value_id=None,
            transformation=contract.transformation,
            recorded_by=f"extractor:{run.prompt_version}",
            content_sha256=_fact_digest(
                run_identity={
                    "document_id": document.id,
                    "prompt_version": run.prompt_version,
                    "schema_version": run.schema_version,
                    "model": run.model,
                    "extractor_config_sha256": run.extractor_config_sha256,
                },
                fact_type="statement_wording",
                subject_kind=contract.subject_kind,
                subject_key=subject_key,
                text_value=description,
                source_links=links,
            ),
        )
        session.add(fact)
        session.flush([fact])
        session.add_all(
            FactSource(
                project_id=document.project_id,
                document_id=document.id,
                fact_id=fact.id,
                source_segment_id=segment_id,
                role=role,
                ordinal=1,
            )
            for role, segment_id in links
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
    if (
        not isinstance(description, str)
        or not description.strip()
        or not isinstance(attribution, str)
        or attribution not in description
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
        proposal = ExtractedProposal(
            project_id=document.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            candidate_id=candidate.id,
            kind=candidate.kind,
            subject_key=subject_key,
            candidate_metadata_json=_proposal_candidate_metadata(candidate),
        )
        session.add(proposal)
        session.flush([proposal])
        for ordinal, fact in enumerate(sorted(subject_facts, key=lambda item: item.fact_type), 1):
            session.add(
                ExtractedProposalFact(
                    project_id=document.project_id,
                    document_id=document.id,
                    extraction_run_id=run.id,
                    proposal_id=proposal.id,
                    fact_id=fact.id,
                    ordinal=ordinal,
                )
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
    value = _validated_scalar_value(contract, segment.exact_text)
    text_value = value if isinstance(value, str) else None
    date_value = value if isinstance(value, date) else None
    external_org_value_id = (
        _exact_registered_external_org_id(session, text_value)
        if predecessor.fact_type == "external_org" and text_value is not None
        else None
    )
    links = tuple((role, segment.id) for role in sorted(contract.required_roles))
    successor = Fact(
        project_id=predecessor.project_id,
        document_id=predecessor.document_id,
        extraction_run_id=predecessor.extraction_run_id,
        fact_type=predecessor.fact_type,
        subject_kind=predecessor.subject_kind,
        subject_key=predecessor.subject_key,
        text_value=text_value,
        date_value=date_value,
        date_range_start=None,
        date_range_end=None,
        external_org_value_id=external_org_value_id,
        document_value_id=None,
        transformation=predecessor.transformation,
        recorded_by=actor.subject,
        content_sha256=_fact_digest(
            run_identity={
                "document_id": predecessor.document_id,
                "extraction_run_id": predecessor.extraction_run_id,
                "correction_of": predecessor.id,
            },
            fact_type=predecessor.fact_type,
            subject_kind=predecessor.subject_kind,
            subject_key=predecessor.subject_key,
            text_value=text_value,
            date_value=date_value,
            external_org_value_id=external_org_value_id,
            source_links=links,
        ),
    )
    session.add(successor)
    session.flush([successor])
    session.add_all(
        FactSource(
            project_id=successor.project_id,
            document_id=successor.document_id,
            fact_id=successor.id,
            source_segment_id=linked_segment_id,
            role=role,
            ordinal=1,
        )
        for role, linked_segment_id in links
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
        },
    }


def replay_fact(
    session: Session, document: Document, fact: Fact, path: Path | str
) -> str | date | AppliesToFactValue | ClosureFactValue:
    """Replay one typed Fact from original bytes through its named transform."""

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
        roles = {source.role for source in sources}
        if roles != contract.required_roles:
            raise FactValidationError("Fact support roles do not match its contract")
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
    exact = dereference_source_segment(document, segment, path)
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
            dereference_source_segment(document, governing_segment, path)
        if result.closure_kind == "source_marked_resolved" and not exact.strip():
            raise FactReplayMismatch("source closure mark is empty")
        return ClosureFactValue(
            closure_kind=result.closure_kind,
            successor_dependency_id=result.successor_dependency_id,
            governing_source_segment_ids=governing,
        )
    replayed = _validated_scalar_value(contract, exact)
    materialized = fact.date_value if fact.date_value is not None else fact.text_value
    if replayed != materialized:
        raise FactReplayMismatch("materialized Fact value does not reproduce")
    return replayed


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


def _transform(name: str, exact_text: str) -> str | date:
    if name == "trim_cell_text_v1":
        return exact_text.strip()
    if name == "exact_prose_span_v1":
        return exact_text
    if name == "iso_date_cell_v1":
        try:
            return date.fromisoformat(exact_text.strip().split(" ", 1)[0])
        except ValueError as exc:
            raise FactValidationError(
                f"structured date is not an ISO calendar date: {exact_text!r}"
            ) from exc
    raise FactValidationError(f"unknown Fact transformation {name!r}")


def _candidate_typed_value(contract: FactTypeContract, value: object) -> str | date:
    if contract.value_class == "date":
        return _validated_scalar_value(contract, str(value))
    return str(value).strip()


def _validated_scalar_value(
    contract: FactTypeContract, exact_text: str
) -> str | date:
    value = _transform(contract.transformation, exact_text)
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


def _exact_registered_external_org_id(session: Session, wording: str) -> int | None:
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
    transformation: str,
    segment: SourceSegment,
    structured_value: object,
) -> Fact:
    fact = Fact(
        project_id=document.project_id,
        document_id=document.id,
        extraction_run_id=run.id,
        fact_type=fact_type,
        subject_kind=subject_kind,
        subject_key=subject_key,
        text_value=None,
        date_value=None,
        date_range_start=None,
        date_range_end=None,
        external_org_value_id=None,
        document_value_id=None,
        transformation=transformation,
        recorded_by=f"extractor:{run.prompt_version}",
        content_sha256=_fact_digest(
            run_identity={
                "document_id": document.id,
                "prompt_version": run.prompt_version,
                "schema_version": run.schema_version,
                "model": run.model,
                "extractor_config_sha256": run.extractor_config_sha256,
            },
            fact_type=fact_type,
            subject_kind=subject_kind,
            subject_key=subject_key,
            text_value=None,
            source_links=(("value_source", segment.id),),
            structured_value=structured_value,
        ),
    )
    session.add(fact)
    session.flush([fact])
    session.add(
        FactSource(
            project_id=document.project_id,
            document_id=document.id,
            fact_id=fact.id,
            source_segment_id=segment.id,
            role="value_source",
            ordinal=1,
        )
    )
    return fact


def _fact_digest(
    *,
    run_identity: dict[str, object],
    fact_type: str,
    subject_kind: str,
    subject_key: str,
    text_value: str | None,
    source_links: tuple[tuple[str, int], ...],
    date_value: date | None = None,
    external_org_value_id: int | None = None,
    structured_value: object | None = None,
) -> str:
    value = {
        "run": run_identity,
        "fact_type": fact_type,
        "subject_kind": subject_kind,
        "subject_key": subject_key,
        "text_value": text_value,
        "date_value": date_value.isoformat() if date_value is not None else None,
        "external_org_value_id": external_org_value_id,
        "structured_value": structured_value,
        "source_links": [
            {"role": role, "source_segment_id": segment_id}
            for role, segment_id in source_links
        ],
    }
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
