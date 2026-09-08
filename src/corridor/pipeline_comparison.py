"""Compare source-bound native output without confusing storage identity with it.

Native Fact digests and scoped row IDs deliberately belong to one database
run. Comparing those IDs made independently persisted equal readings differ;
comparing only text lost repeated physical occurrences. This boundary replays
persisted Facts, retains physical source locators and ordered support roles,
and compares exact records with multiplicity. It does not rebind historical
citations, judge truth, label references independent, or approve a pipeline.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.facts import proposal_input_snapshots, replay_fact
from corridor.models import Document, Fact, FactSource, SourceSegment
from corridor.reader_segments import NativeCellIndex

if TYPE_CHECKING:
    from corridor.native_matrix import NativeMatrixExtraction


OUTPUT_SCHEMA = "corridor.native-pipeline-output.v1"
_ROLES = ("value_source", "context")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _ordered(values: list[dict]) -> list[dict]:
    """Order records, retaining every occurrence instead of indexing by a key."""
    def key(value):
        if "location" in value:
            identity = [value["location"], value.get("field")]
        elif "page" in value:
            identity = [value["page"]]
        elif "name" in value:
            identity = [value["name"]]
        else:
            identity = [value.get("row"), value.get("column")]
        return _json(identity), _json(value)
    return sorted(values, key=key)


def _physical_segment(segment: SourceSegment) -> dict:
    location = deepcopy(segment.location_json)
    glyphs = location.pop("glyphs")
    physical = [
        {key: value for key, value in glyph.items() if key not in {"source_index", "object_id"}}
        for glyph in glyphs
    ]
    for source_key, physical_key in (
        ("outside_source_indices", "outside_table_glyphs"),
        ("ambiguous_source_indices", "ambiguous_glyphs"),
    ):
        if source_key in location:
            indices = set(location.pop(source_key))
            location[physical_key] = [
                value for glyph, value in zip(glyphs, physical, strict=True)
                if glyph["source_index"] in indices
            ]
    # Reading identities and page-text hashes belong to the enclosing chain
    # receipt. They are not independent physical anchors for comparing readers.
    location.pop("page_text_sha256", None)
    location["glyphs"] = physical
    return {
        "source_sha256": segment.rendition_sha256,
        "kind": segment.kind,
        "page": segment.page_no,
        "table": segment.table_index,
        "row": segment.cell_row,
        "column": segment.cell_column,
        "row_span": segment.row_span,
        "column_span": segment.column_span,
        "span_stream": segment.span_stream,
        "start_offset": segment.start_offset,
        "end_offset": segment.end_offset,
        "exact_text": segment.exact_text,
        "content_sha256": segment.content_sha256,
        "location": location,
    }


def _row_location(row) -> dict:
    return {"page": row.page_no, "table": row.table_index, "row": row.row}


def _row_geometry(row, pages: dict[int, dict]) -> dict:
    table = pages[row.page_no]["tables"]["value"][row.table_index]
    return {
        "table_box": deepcopy(table["box"]),
        "cells": _ordered([
            {key: deepcopy(cell[key]) for key in ("row", "column", "row_span", "column_span", "box")}
            for cell in table["structured_cells"]
            if cell["row"] <= row.row < cell["row"] + cell["row_span"]
        ]),
    }


def _proposal(snapshot: dict, document: Document, fields: dict[str, dict], outcomes: list[dict]) -> dict:
    if snapshot["project_id"] != document.project_id or snapshot["source_document_id"] != document.id:
        raise ValueError("native proposal crosses the canonical document")
    payload = deepcopy(snapshot["payload_json"])
    raw_sources = payload.pop("field_sources")
    if set(raw_sources) != set(fields):
        raise ValueError("native proposal field source membership differs from its mapping")
    for name, field in fields.items():
        if raw_sources[name] != field["raw_sources"]:
            raise ValueError("native proposal source roles differ from its bound mapping")
    if payload.pop("field_materialization") != outcomes:
        raise ValueError("native proposal materialization outcomes differ from its run")
    payload.pop("native_row_id")
    payload.pop("local_row_id")
    for citation in payload["citations"]:
        if citation.pop("document_id") != document.id:
            raise ValueError("native proposal citation crosses the canonical document")
        citation["source_sha256"] = document.sha256
    return {
        "kind": snapshot["kind"],
        "source_pages": deepcopy(snapshot["source_pages"]),
        "confidence": snapshot["confidence"],
        "citations_verified": snapshot["citations_verified"],
        "payload": payload,
    }


def canonical_native_output(
    session: Session, document: Document, extraction: NativeMatrixExtraction,
    *, source_path: Path | str,
) -> dict:
    """Replay the actual persisted output into storage-independent source records.

    The original path is mandatory; a Document filename cannot identify its
    original bytes. Configuration and reading identities remain in the caller's
    chain receipt. This value retains the physical locators and exact reading
    offsets, but never treats comparison as permission to rewrite either.
    """
    mapping, run = extraction.mapping, extraction.run
    mapping.certify(document)
    if sha256(Path(source_path).read_bytes()).hexdigest() != document.sha256:
        raise ValueError("canonical output source bytes differ from the Document")
    if run.document_id != document.id or run.outcome != "completed" or run.page_errors != 0:
        raise ValueError("canonical native output requires its completed document run")
    accounting = run.row_accounting_json
    if (accounting["native_mapping"]["identity"] != mapping.identity
            or accounting["native_mapping"]["pages"] != mapping.pages
            or list(extraction.field_outcomes) != accounting["field_materialization"]):
        raise ValueError("canonical native output mapping differs from its persisted run")
    index = NativeCellIndex(document, mapping.native_reading)
    native_pages = {page["number"]: page for page in mapping.native_reading.pages}
    row_by_id = {row.row_id: row for row in mapping.rows}
    if len(row_by_id) != len(mapping.rows):
        raise ValueError("canonical mapping contains duplicated row identities")
    outcomes = {}
    for outcome in extraction.field_outcomes:
        key = (outcome["row_id"], outcome["field"])
        if key in outcomes:
            raise ValueError("canonical run contains duplicate field outcomes")
        outcomes[key] = outcome
    snapshots = {}
    for snapshot in proposal_input_snapshots(session, run):
        row_id = snapshot["payload_json"]["native_row_id"]
        if row_id in snapshots or row_id not in row_by_id:
            raise ValueError("canonical run contains an extra or duplicated proposal row")
        snapshots[row_id] = snapshot

    selected = {}
    rows = []
    expected_fact_keys = set()
    expected_fact_sources = {}
    used_outcomes = set()
    for row in mapping.rows:
        fields = {}
        raw_outcomes = []
        for field in row.fields:
            key = (row.row_id, field.name)
            if field.name in fields or key not in outcomes:
                raise ValueError("canonical row has missing or duplicate field outcomes")
            outcome = outcomes[key]
            used_outcomes.add(key)
            raw_outcomes.append(outcome)
            roles, raw_roles = {}, {}
            for role, references in (("value_source", field.value_sources), ("context", field.context_sources)):
                roles[role], raw_roles[role] = [], []
                for reference in references:
                    if reference.scoped_id not in selected:
                        selected[reference.scoped_id] = reference.select(session, document, index)
                    segment = selected[reference.scoped_id]
                    roles[role].append(_physical_segment(segment))
                    raw_roles[role].append({"segment_id": segment.id, "scoped_id": reference.scoped_id,
                                            "model_id": reference.model_id})
                if outcome[f"{role}_ids" if role == "value_source" else "context_source_ids"] != [
                    entry["segment_id"] for entry in raw_roles[role]
                ]:
                    raise ValueError("field outcome source order differs from its mapping")
            if outcome["status"] == "materialized":
                expected_fact_keys.add(key)
                expected_fact_sources[key] = [
                    {"role": role, "ordinal": ordinal, "segment": segment}
                    for role in _ROLES for ordinal, segment in enumerate(roles[role], start=1)
                ]
            fields[field.name] = {"name": field.name, "text": field.text, "sources": roles,
                                  "materialization": {"status": outcome["status"], "reason": outcome["reason"]},
                                  "raw_sources": raw_roles}
        snapshot = snapshots.pop(row.row_id, None)
        if (snapshot is not None) != (row.disposition == "extracted"):
            raise ValueError("proposal population differs from the mapped row dispositions")
        proposal = _proposal(snapshot, document, fields, raw_outcomes) if snapshot is not None else None
        rows.append({
            "location": _row_location(row), "geometry": _row_geometry(row, native_pages),
            "disposition": row.disposition, "reason": row.reason,
            "confidence": row.confidence, "unmapped": list(row.unmapped),
            "fields": _ordered([{key: value for key, value in field.items() if key != "raw_sources"}
                                for field in fields.values()]),
            "proposal": proposal,
        })
    if used_outcomes != set(outcomes) or snapshots:
        raise ValueError("canonical run has outcomes outside its complete row population")

    facts = []
    seen_fact_keys = Counter()
    persisted = session.scalars(select(Fact).where(Fact.extraction_run_id == run.id)).all()
    if Counter(fact.id for fact in persisted) != Counter(fact.id for fact in extraction.facts):
        raise ValueError("canonical extraction does not carry its complete persisted Fact population")
    for fact in persisted:
        if fact.subject_key not in row_by_id:
            raise ValueError("native Fact names a row outside its mapping")
        key = (fact.subject_key, fact.fact_type)
        seen_fact_keys[key] += 1
        value = replay_fact(session, document, fact, source_path, native_reading=mapping.native_reading)
        if not isinstance(value, (str, date)):
            raise ValueError("native matrix canonicalization received an unsupported Fact value")
        links = session.execute(
            select(FactSource, SourceSegment)
            .join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
            .where(FactSource.fact_id == fact.id)
            .order_by(FactSource.role, FactSource.ordinal)
        ).all()
        sources = [
            {"role": role, "ordinal": link.ordinal, "segment": _physical_segment(segment)}
            for role in _ROLES for link, segment in links if link.role == role
        ]
        if sources != expected_fact_sources.get(key):
            raise ValueError("native Fact source roles differ from its mapped field")
        facts.append({"location": _row_location(row_by_id[fact.subject_key]), "field": fact.fact_type,
                      "value": value.isoformat() if isinstance(value, date) else value,
                      "transformation": fact.transformation, "sources": sources})
    if seen_fact_keys != Counter({key: 1 for key in expected_fact_keys}):
        raise ValueError("materialized field outcomes differ from the persisted Fact population")
    pages = [
        {"page": page["number"], "geometry": deepcopy(native_pages[page["number"]]["geometry"]),
         "structure": deepcopy(page["structure"]),
         "reading": {key: deepcopy(value) for key, value in page["reading"].items() if key != "rows"}}
        for page in mapping.pages
    ]
    return {"schema_version": OUTPUT_SCHEMA, "source_sha256": document.sha256,
            "pages": _ordered(pages), "rows": _ordered(rows), "facts": _ordered(facts)}


def _normalized_output(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "source_sha256", "pages", "rows", "facts"}
            or value["schema_version"] != OUTPUT_SCHEMA):
        raise ValueError("pipeline output has an unsupported canonical schema")
    source = value["source_sha256"]
    if not isinstance(source, str) or len(source) != 64 or any(c not in "0123456789abcdef" for c in source):
        raise ValueError("pipeline output requires its source digest")
    for key in ("pages", "rows", "facts"):
        if not isinstance(value[key], list) or any(not isinstance(item, dict) for item in value[key]):
            raise ValueError(f"pipeline output {key} must retain an ordered record population")
    page_numbers = [page.get("page") for page in value["pages"]]
    if (not page_numbers or any(type(page) is not int or page < 1 for page in page_numbers)
            or len(set(page_numbers)) != len(page_numbers)):
        raise ValueError("pipeline output must identify its complete, distinct page population")
    for row in value["rows"]:
        _validate_row_location(row.get("location"), page_numbers)
        geometry = row.get("geometry")
        if (not isinstance(geometry, dict) or not geometry.get("cells")
                or not isinstance(geometry.get("table_box"), list)):
            raise ValueError("pipeline row requires its physical table and cell geometry")
        fields = row.get("fields")
        if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
                or any(not isinstance(field.get("name"), str) or not field["name"] for field in fields)
                or len({field["name"] for field in fields}) != len(fields)):
            raise ValueError("pipeline row requires distinct named fields")
        for field in fields:
            sources = field.get("sources")
            if (not isinstance(field.get("text"), str) or not isinstance(sources, dict)
                    or set(sources) != set(_ROLES) or not sources["value_source"]):
                raise ValueError("pipeline field requires exact text and ordered source roles")
            for role in _ROLES:
                if not isinstance(sources[role], list):
                    raise ValueError("pipeline field source roles must be ordered lists")
                for segment in sources[role]:
                    _validate_physical_segment(segment, source, page_numbers)
    for fact in value["facts"]:
        _validate_row_location(fact.get("location"), page_numbers)
        if (not isinstance(fact.get("field"), str) or not isinstance(fact.get("value"), str)
                or not isinstance(fact.get("transformation"), str) or not fact.get("sources")):
            raise ValueError("pipeline Fact requires its typed replayed value and ordered support")
        for link in fact["sources"]:
            if (not isinstance(link, dict) or link.get("role") not in _ROLES
                    or type(link.get("ordinal")) is not int or link["ordinal"] < 1):
                raise ValueError("pipeline Fact source role is invalid")
            _validate_physical_segment(link.get("segment"), source, page_numbers)
    _json(value)  # Refuse non-JSON values and non-finite numbers before comparing.
    return {**deepcopy(value), **{key: _ordered(deepcopy(value[key])) for key in ("pages", "rows", "facts")}}


def _validate_row_location(location: object, pages: list[int]) -> None:
    if (not isinstance(location, dict) or set(location) != {"page", "table", "row"}
            or any(type(number) is not int for number in location.values())
            or location["page"] not in pages or location["table"] < 0 or location["row"] < 0):
        raise ValueError("pipeline row requires its source page/table/row location")


def _validate_physical_segment(segment: object, source: str, pages: list[int]) -> None:
    if (not isinstance(segment, dict) or segment.get("source_sha256") != source
            or segment.get("kind") not in {"pdf_cell", "pdf_span"}
            or segment.get("page") not in pages
            or not isinstance(segment.get("exact_text"), str) or not segment["exact_text"]
            or segment.get("content_sha256") != sha256(segment["exact_text"].encode()).hexdigest()):
        raise ValueError("pipeline source segment requires its exact source identity and text")
    location = segment.get("location")
    if (not isinstance(location, dict) or not isinstance(location.get("geometry"), dict)
            or not isinstance(location.get("glyphs"), list) or not location["glyphs"]):
        raise ValueError("pipeline source segment requires physical glyph and page anchors")


def _differences(expected: Any, actual: Any, path: str) -> list[dict]:
    if type(expected) is not type(actual):
        return [{"path": path, "kind": "type", "expected": expected, "actual": actual}]
    if isinstance(expected, dict):
        differences = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                differences.append({"path": f"{path}.{key}", "kind": "membership",
                                    "expected": expected.get(key), "actual": actual.get(key)})
            else:
                differences.extend(_differences(expected[key], actual[key], f"{path}.{key}"))
        return differences
    if isinstance(expected, list):
        differences = []
        for index in range(max(len(expected), len(actual))):
            if index >= min(len(expected), len(actual)):
                differences.append({"path": f"{path}[{index}]", "kind": "membership",
                                    "expected": expected[index] if index < len(expected) else None,
                                    "actual": actual[index] if index < len(actual) else None})
            else:
                differences.extend(_differences(expected[index], actual[index], f"{path}[{index}]"))
        return differences
    return [] if expected == actual else [{"path": path, "kind": "value", "expected": expected, "actual": actual}]


def compare_pipeline_outputs(expected: dict, actual: dict) -> dict:
    """Exact structured comparison; neither equality nor a positive count proves truth.

    `equal` can establish repeatability for empty output. `passed` additionally
    requires a nonempty row denominator, so empty extraction never passes this
    quality comparison. Reference independence and qualification are separate.
    """
    expected, actual = _normalized_output(expected), _normalized_output(actual)
    differences = _differences(expected, actual, "$")
    expected_rows = Counter(_json(row) for row in expected["rows"])
    actual_rows = Counter(_json(row) for row in actual["rows"])
    nonempty = bool(expected["rows"]) and bool(actual["rows"])
    return {
        "equal": not differences, "passed": not differences and nonempty,
        "differences": differences, "difference_count": len(differences),
        "expected_rows": len(expected["rows"]), "actual_rows": len(actual["rows"]),
        "matched_rows": sum((expected_rows & actual_rows).values()),
        "expected_facts": len(expected["facts"]), "actual_facts": len(actual["facts"]),
        "expected_sha256": sha256(_json(expected).encode()).hexdigest(),
        "actual_sha256": sha256(_json(actual).encode()).hexdigest(),
        "coverage_refusal": None if nonempty else "empty_row_denominator",
    }
