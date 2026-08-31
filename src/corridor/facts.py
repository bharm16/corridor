"""Append and replay typed source Facts over exact Source Segments.

Facts answer what one Document rendition said; they do not settle what the
Project Record says.  This first contract covers structured stationing cells.
The materialized value is reproducible through a named transformation, and the
role-tagged support link is constrained to the Fact's own rendition (ADR-0067,
ADR-0069).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Candidate,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    Fact,
    FactDisposition,
    FactSource,
    SourceSegment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.sheets import column_mapping
from corridor.source_segments import dereference_source_segment


class FactValidationError(ValueError):
    """A proposed Fact cannot replay from its declared source support."""


class FactReplayMismatch(FactValidationError):
    """A materialized Fact value differs from its replayed transformation."""


@dataclass(frozen=True)
class FactTypeContract:
    """Released code contract for one controlled Fact type."""

    value_class: str
    subject_kind: str
    transformation: str
    automatic_segment_kinds: frozenset[str]


FACT_TYPE_CONTRACTS = {
    name: FactTypeContract(
        value_class="text",
        subject_kind="source_row",
        transformation="trim_cell_text_v1",
        automatic_segment_kinds=frozenset({"spreadsheet_cell"}),
    )
    for name in ("station_from", "station_to")
}


def carries_source_facts(candidates: tuple[Candidate, ...]) -> bool:
    """Whether this extraction output belongs at the scoped Fact command."""

    return any(
        candidate.payload_json.get("tier") == "native"
        and candidate.kind == "dependency"
        and any(
            candidate.payload_json.get("fields", {}).get(name)
            for name in FACT_TYPE_CONTRACTS
        )
        for candidate in candidates
    )


def append_stationing_facts(
    session: Session,
    document: Document,
    run: ExtractionRun,
    candidates: tuple[Candidate, ...],
) -> tuple[Fact, ...]:
    """Append station start/end observations for native spreadsheet rows."""

    native = [
        candidate
        for candidate in candidates
        if candidate.payload_json.get("tier") == "native"
        and candidate.kind == "dependency"
        and any(
            candidate.payload_json.get("fields", {}).get(name)
            for name in FACT_TYPE_CONTRACTS
        )
    ]
    if not native:
        return ()
    accepted_segment_kinds = frozenset().union(
        *(contract.automatic_segment_kinds for contract in FACT_TYPE_CONTRACTS.values())
    )
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
        raise FactValidationError("native stationing facts require cell segments")
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
                "native stationing Candidate needs an explicit sheet and table row"
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
                    f"stationing source cell is absent: {sheet_name}!{cell_range}"
                )
            if segment.kind not in contract.automatic_segment_kinds:
                raise FactValidationError(
                    f"{fact_type} does not accept {segment.kind} support"
                )
            value = _transform(contract.transformation, segment.exact_text)
            if value != fields[fact_type]:
                raise FactReplayMismatch(
                    f"Candidate {fact_type} does not reproduce from {cell_range}"
                )
            fact = Fact(
                project_id=document.project_id,
                document_id=document.id,
                extraction_run_id=run.id,
                fact_type=fact_type,
                subject_kind=contract.subject_kind,
                subject_key=f"{sheet_name}!{source_row}",
                text_value=value,
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
                    fact_type=fact_type,
                    subject_key=f"{sheet_name}!{source_row}",
                    text_value=value,
                    source_segment_ids=(segment.id,),
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
    session.flush()
    return tuple(appended)


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
    value = _transform(predecessor.transformation, segment.exact_text)
    successor = Fact(
        project_id=predecessor.project_id,
        document_id=predecessor.document_id,
        extraction_run_id=predecessor.extraction_run_id,
        fact_type=predecessor.fact_type,
        subject_kind=predecessor.subject_kind,
        subject_key=predecessor.subject_key,
        text_value=value,
        date_value=None,
        date_range_start=None,
        date_range_end=None,
        external_org_value_id=None,
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
            subject_key=predecessor.subject_key,
            text_value=value,
            source_segment_ids=(segment.id,),
        ),
    )
    session.add(successor)
    session.flush([successor])
    session.add(
        FactSource(
            project_id=successor.project_id,
            document_id=successor.document_id,
            fact_id=successor.id,
            source_segment_id=segment.id,
            role="value_source",
            ordinal=1,
        )
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
        fields = {
            fact.fact_type: fact.text_value
            for link in links
            if (fact := session.get(Fact, link.fact_id)) is not None
        }
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
) -> str:
    """Replay one typed Fact from original bytes through its named transform."""

    with session.no_autoflush:
        sources = tuple(
            session.scalars(
                select(FactSource)
                .where(FactSource.fact_id == fact.id, FactSource.role == "value_source")
                .order_by(FactSource.ordinal)
            ).all()
        )
        if len(sources) != 1:
            raise FactValidationError("stationing Fact needs one value source")
        segment = session.get(SourceSegment, sources[0].source_segment_id)
    if segment is None or segment.document_id != fact.document_id:
        raise FactValidationError("Fact source belongs to another rendition")
    exact = dereference_source_segment(document, segment, path)
    replayed = _transform(fact.transformation, exact)
    if replayed != fact.text_value:
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
        if "station_from" in mapping.values() or "station_to" in mapping.values():
            return row_number, mapping
    raise FactValidationError("segment sheet has no stationing header")


def _cell_range(column_number: int, row_number: int) -> str:
    return f"{get_column_letter(column_number)}{row_number}"


def _transform(name: str, exact_text: str) -> str:
    if name == "trim_cell_text_v1":
        return exact_text.strip()
    raise FactValidationError(f"unknown Fact transformation {name!r}")


def _fact_digest(
    *,
    run_identity: dict[str, object],
    fact_type: str,
    subject_key: str,
    text_value: str,
    source_segment_ids: tuple[int, ...],
) -> str:
    value = {
        "run": run_identity,
        "fact_type": fact_type,
        "subject_kind": "source_row",
        "subject_key": subject_key,
        "text_value": text_value,
        "source_segment_ids": list(source_segment_ids),
    }
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
