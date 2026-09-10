"""Replay retained structure answers through the native matrix adapter (#737).

Identifier-only matching hid different row decisions in the former experiment.
This driver therefore compares every retained field, local reference, mapping,
refusal, disposition and reason before separately scoring machine reference
IDs with multiplicity. The recorded client checks the measured inputs and
returns only the preserved raw structure answer; it has no network transport.

The explicit command uses the shared guarded disposable-PostgreSQL provisioner
to exercise Source Fact and Extracted Proposal persistence. It never connects
the adapter to a model or selects a Current Production Run. Retained readings
are regression references, not independently established field gold.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
import csv
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import io
from itertools import groupby
import json
import os
from pathlib import Path
import sys
from typing import Any

from corridor.extractor_lineage import DEPLOYED_NATIVE_MATRIX_REQUEST
from corridor.llm import RequestConfiguration
from corridor_pdf_reader.replacement import semantics


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = REPO_ROOT / "gold/native-matrix/v1/dataset.json"
# Differences a person settled by reading the page. Absent file means none.
ADJUDICATIONS_PATH = REPO_ROOT / "gold/native-matrix/v1/adjudications.json"
RECEIPT_SCHEMA = "corridor.native-matrix-measurement.v1"
MEASURED_PAGE_KEYS = ("number", "structure", "reading", "listing")


class ReplayRefusal(ValueError):
    """A retained input or adapter result did not meet the replay contract."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def verified_bytes(path: Path, expected: str, size: int | None = None) -> bytes:
    """Verify the exact bytes before a caller parses a retained artifact."""

    value = path.read_bytes()
    if sha256(value).hexdigest() != expected or (size is not None and len(value) != size):
        raise ReplayRefusal(f"retained input digest or byte count changed: {path}")
    return value


@dataclass(frozen=True)
class RetainedCase:
    specification: dict[str, Any]
    manifest: dict[str, Any]
    document: dict[str, Any]
    directory: Path
    source: Path
    images: dict[int, Path]
    image_digests: dict[int, str]

    @property
    def name(self) -> str:
        return self.specification["name"]


def load_case(
    specification: dict[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    results_root: Path | None = None,
) -> RetainedCase:
    """Pin manifest, answers, images and source before reading model output."""

    manifest = json.loads(verified_bytes(
        repo_root / specification["manifest"], specification["manifest_sha256"]
    ))
    directory = (
        results_root / specification["name"]
        if results_root is not None else Path(manifest["retained_at"])
    )
    files = {}
    entries = {}
    for entry in manifest["entries"]:
        name = entry["path"]
        if Path(name).name != name or name in entries:
            raise ReplayRefusal("manifest paths must be unique filenames")
        entries[name] = entry
        files[name] = verified_bytes(directory / name, entry["sha256"], entry["bytes"])
    if len(files) != manifest["files"] or sum(map(len, files.values())) != manifest["bytes"]:
        raise ReplayRefusal("manifest file or byte accounting differs from its entries")
    document = json.loads(files["document.json"])
    source_digest = specification["source_sha256"]
    source = Path(document["source"])
    verified_bytes(source, source_digest)
    numbers = [page["number"] for page in document["pages"]]
    if len(numbers) != len(set(numbers)) or len(numbers) != specification["pages"]:
        raise ReplayRefusal("retained page membership is duplicated or incomplete")
    rows = [row for page in document["pages"] for row in page["reading"]["rows"]]
    if (len(rows) != specification["expected_rows"]
            or Counter(row["reason"] for row in rows) != specification["expected_reasons"]):
        raise ReplayRefusal("retained row population differs from the registered reference")
    expected_files = {"document.json", *(f"page-{number}.png" for number in numbers)}
    if set(files) != expected_files:
        raise ReplayRefusal("retained images do not exactly cover the recorded pages")
    return RetainedCase(
        specification, manifest, document, directory, source,
        {number: directory / f"page-{number}.png" for number in numbers},
        {number: entries[f"page-{number}.png"]["sha256"] for number in numbers},
    )


def verify_measured_configuration(configuration: Mapping[str, Any]) -> None:
    """No prompt, schema or frozen semantics changes hide inside a replay."""

    verified_bytes(semantics.PROMPT_PATH, configuration["prompt_sha256"])
    verified_bytes(Path(semantics.__file__), configuration["semantics_sha256"])
    if digest(semantics.STRUCTURE_SCHEMA) != configuration["schema_sha256"]:
        raise ReplayRefusal("measured strict schema changed")
    if semantics.PROMPT_VERSION != configuration["prompt_version"]:
        raise ReplayRefusal("measured prompt version changed")


class RecordedStructureClient:
    """Check exact request inputs and return the next preserved raw answer.

    Its `configuration()` states the configuration the retained answers were
    produced under, taken from the retained dataset rather than asserted: the
    dataset records the model and the reasoning effort, and the deployed
    endpoint is named for the one they were recorded against because no
    dataset field holds it. Nothing here opens a connection.
    """

    def __init__(self, case: RetainedCase, configuration: Mapping[str, Any]):
        verify_measured_configuration(configuration)
        if (
            case.document["model"] != configuration["model"]
            or case.document["prompt_version"] != configuration["prompt_version"]
        ):
            raise ReplayRefusal("retained model or prompt identity differs from the dataset")
        self.case = case
        self.configuration = configuration
        self._configuration = RequestConfiguration(
            model=configuration["model"], effort=configuration["reasoning_effort"],
            flex=False,
            base_url=configuration.get("provider_base_url", DEPLOYED_NATIVE_MATRIX_REQUEST.base_url),
        )
        self.model = self._configuration.model
        self.image_detail = configuration["image_detail"]
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.replayed_calls = 0

    def configuration(self) -> RequestConfiguration:
        return self._configuration

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        images: Sequence[Path | str] = (),
        logprobs: bool = False,
    ) -> dict[str, Any]:
        if logprobs:
            raise ReplayRefusal("the retained dataset holds no output logprobs")
        if self.replayed_calls >= len(self.case.document["pages"]):
            raise ReplayRefusal("adapter asked for an unrecorded structure answer")
        expected = self.case.document["pages"][self.replayed_calls]
        if sha256(system.encode()).hexdigest() != self.configuration["prompt_sha256"]:
            raise ReplayRefusal("adapter changed the measured system prompt")
        if digest(schema) != self.configuration["schema_sha256"]:
            raise ReplayRefusal("adapter changed the measured strict schema")
        if user != expected["listing"]:
            raise ReplayRefusal(f"adapter changed page {expected['number']}'s measured listing")
        number = expected["number"]
        if len(images) != 1 or Path(images[0]).resolve() != self.case.images[number].resolve():
            raise ReplayRefusal("adapter did not use the retained page image context")
        verified_bytes(Path(images[0]), self.case.image_digests[number])
        self.replayed_calls += 1
        return deepcopy(expected["structure"])

    def require_complete(self) -> None:
        if self.replayed_calls != len(self.case.document["pages"]):
            raise ReplayRefusal("adapter did not consume every retained page answer")


def _differences(expected: Any, actual: Any, path: str) -> list[dict[str, Any]]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key in sorted(expected.keys() | actual.keys()):
            if key not in expected or key not in actual:
                result.append({"path": f"{path}.{key}", "reason": "key_membership"})
            else:
                result.extend(_differences(expected[key], actual[key], f"{path}.{key}"))
        return result
    if isinstance(expected, list) and isinstance(actual, list):
        result = []
        if len(expected) != len(actual):
            result.append({"path": path, "reason": "length", "expected": len(expected), "actual": len(actual)})
        for index, (left, right) in enumerate(zip(expected, actual)):
            result.extend(_differences(left, right, f"{path}[{index}]"))
        return result
    if type(expected) is not type(actual) or expected != actual:
        return [{"path": path, "reason": "value", "expected": expected, "actual": actual}]
    return []


def _settles(adjudication: Mapping[str, Any], difference: Mapping[str, Any], source_sha256: str) -> bool:
    """One adjudication settles one difference, matched on every recorded field.

    Exactness is the point. An adjudication that matched on the path alone
    would absorb whatever else later appeared at that path, which is how a
    settled question becomes a blind spot.
    """

    if adjudication.get("source_sha256") != source_sha256:
        return False
    return all(
        adjudication.get(key) == difference.get(key)
        for key in ("path", "reason", "expected", "actual")
        if key in difference
    )


def load_adjudications(path: Path = ADJUDICATIONS_PATH) -> list[dict[str, Any]]:
    """Every recorded adjudication, or none when the file is absent."""

    if not path.exists():
        return []
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != "corridor.native-matrix-adjudications.v1":
        raise ValueError("adjudications file declares an unknown schema")
    return list(record["adjudications"])


def compare_retained_reading(
    retained: Mapping[str, Any],
    actual_pages: Sequence[Mapping[str, Any]],
    *,
    source_sha256: str = "",
    adjudications: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compare complete semantic output, including rows the adapter excludes.

    The retained reading is the incumbent's answer, not the document's. Where
    a person reads the page and finds the incumbent wrong, the finding is
    recorded as an adjudication rather than written back over the retained
    bytes, which stay first-write-only evidence (ADR-0023). So `pass` and
    `differences` keep their exact meaning, "identical to the incumbent", and
    `unadjudicated_differences` reports what no one has settled against the
    source. A difference is settled only by an adjudication that names this
    document and matches the difference on every field.
    """

    actual = [{key: page.get(key) for key in MEASURED_PAGE_KEYS} for page in actual_pages]
    # Dataclass tuples become JSON arrays, exactly as in the retained file.
    actual = json.loads(canonical_bytes(actual))
    expected = [{key: page[key] for key in MEASURED_PAGE_KEYS} for page in retained["pages"]]
    differences = _differences(expected, actual, "pages")
    seen = set()
    rows = []
    for page in actual:
        for row in (page.get("reading") or {}).get("rows", []):
            key = (page["number"], row["row_id"])
            if key in seen:
                differences.append({"path": str(key), "reason": "duplicate_row_disposition"})
            seen.add(key)
            rows.append(row)
    adjudicated = [
        {**difference, "verdict": adjudication.get("verdict", "")}
        for difference in differences
        for adjudication in adjudications
        if _settles(adjudication, difference, source_sha256)
    ]
    settled = [{key: value for key, value in entry.items() if key != "verdict"} for entry in adjudicated]
    unadjudicated = [difference for difference in differences if difference not in settled]
    return {
        "pass": not differences,
        "adjudicated": adjudicated,
        "unadjudicated_differences": unadjudicated,
        "unadjudicated_pass": not unadjudicated,
        "expected_sha256": digest(expected),
        "actual_sha256": digest(actual),
        "compared_pages": len(expected),
        "compared_rows": sum(len(page["reading"]["rows"]) for page in expected),
        "actual_rows": len(rows),
        "field_values": sum(len(row["fields"]) for row in rows),
        "reasons": dict(sorted(Counter(row["reason"] for row in rows).items())),
        "differences": differences,
        "scope": "Exact retained-reading regression, including field text/local IDs and all row/page decisions; no independent field gold.",
    }


def compare_required_rows(retained, actual_pages) -> dict[str, Any]:
    """Keep exact row values/references/decisions separate from page diagnostics."""

    expected = [{"page": page["number"], "rows": page["reading"]["rows"]}
                for page in retained["pages"]]
    actual = json.loads(canonical_bytes([
        {"page": page["number"], "rows": page["reading"]["rows"]} for page in actual_pages
    ]))
    differences = _differences(expected, actual, "rows")
    return {"pass": not differences, "expected_sha256": digest(expected), "actual_sha256": digest(actual),
            "compared_rows": sum(len(page["rows"]) for page in expected), "differences": differences,
            "scope": "Exact page/row membership, field text, local references, disposition and reason for every body row."}


def compare_mapping_diagnostics(retained, actual_pages) -> dict[str, Any]:
    """Classify repeated unmapped headings without changing full-reading parity."""

    expected = deepcopy([{key: page[key] for key in MEASURED_PAGE_KEYS} for page in retained["pages"]])
    actual = json.loads(canonical_bytes([{key: page[key] for key in MEASURED_PAGE_KEYS} for page in actual_pages]))
    diagnostics = []
    duplicates_only = True
    for left, right in zip(expected, actual):
        left_mapping, right_mapping = left["reading"].get("mapping"), right["reading"].get("mapping")
        if left_mapping is None or right_mapping is None:
            continue
        earlier, current = left_mapping.pop("unmapped"), right_mapping.pop("unmapped")
        if earlier == current:
            continue
        left_groups = [(heading, len(list(group))) for heading, group in groupby(earlier)]
        right_groups = [(heading, len(list(group))) for heading, group in groupby(current)]
        repeated = (len(left_groups) == len(right_groups) and all(
            left_heading == right_heading and right_count >= left_count
            for (left_heading, left_count), (right_heading, right_count) in zip(left_groups, right_groups)
        ))
        duplicates_only = duplicates_only and repeated
        diagnostics.append({"page": left["number"], "retained_unmapped": earlier,
                            "actual_unmapped": current, "only_added_adjacent_duplicates": repeated})
    other_differences = _differences(expected, actual, "pages_except_unmapped_diagnostics")
    allowed = duplicates_only and not other_differences
    return {
        "classification": ("identical" if not diagnostics else "duplicate_unmapped_heading_diagnostic") if allowed else "other_difference",
        "only_duplicate_unmapped_headings": allowed and bool(diagnostics),
        "required_semantic_output_unchanged": allowed,
        "diagnostics": diagnostics, "other_differences": other_differences,
        "selection_owner": "#447",
        "limits": "Full-reading parity remains separate. Historical header-span geometry was not retained; shared listing/image bytes do not establish topology equality.",
    }


def machine_reference_counts(path: Path, expected_sha256: str) -> Counter[str]:
    source = verified_bytes(path, expected_sha256).decode("utf-8")
    rows = csv.DictReader(io.StringIO(source))
    if rows.fieldnames != ["source_ref", "page", "critical"]:
        raise ReplayRefusal("machine reference columns changed")
    return Counter(row["source_ref"].strip() for row in rows)


def score_machine_identifiers(
    actual_documents: Sequence[Mapping[str, Any]], reference: Counter[str]
) -> dict[str, Any]:
    """Match utility_id multiplicity; the CSV cannot name a scoped occurrence."""

    found = Counter(
        row["fields"]["utility_id"]["text"].strip()
        for document in actual_documents for page in document["pages"]
        for row in page["reading"]["rows"] if row["disposition"] == "extracted"
    )
    def entries(counts):
        return [{"source_ref": identifier, "count": count}
                for identifier, count in sorted(counts.items())]
    return {
        "pass": found == reference,
        "reference_rows": reference.total(),
        "extracted_rows": found.total(),
        "matched_rows": (found & reference).total(),
        "missing": entries(reference - found),
        "extra": entries(found - reference),
        "scope": "Historical machine source-reference multiplicities only. The CSV does not establish document/page/row occurrence identity, critical or field accuracy. WSDOT 9540 is spent; this is not a new generalization score.",
    }


def compare_field_outcomes(
    pages: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Require one separately recorded outcome for every mapped row field."""

    from corridor.fact_types import STRUCTURED_DATE_FACT_TYPES

    expected = {
        (page["number"], row["row_id"], field): (row, value)
        for page in pages for row in page["reading"]["rows"]
        for field, value in row["fields"].items()
    }
    seen = Counter()
    differences = []
    non_iso_dates = []
    for outcome in outcomes:
        key = (outcome["page"], outcome["local_row_id"], outcome["field"])
        seen[key] += 1
        if key not in expected:
            differences.append({"key": key, "reason": "unexpected_field_outcome"})
            continue
        row, value = expected[key]
        status = outcome["status"]
        if row["disposition"] != "extracted":
            if status != "not_extracted":
                differences.append({"key": key, "reason": "excluded_row_field_materialized"})
            continue
        if status not in {"materialized", "refused"} or not outcome.get("reason"):
            differences.append({"key": key, "reason": "missing_materialization_decision"})
        if not outcome.get("value_source_ids"):
            differences.append({"key": key, "reason": "extracted_field_lacks_source_binding"})
        if key[2] in STRUCTURED_DATE_FACT_TYPES and not _is_iso_date(value["text"]):
            non_iso_dates.append({
                "page": key[0], "local_row_id": key[1], "field": key[2],
                "text": value["text"], "cells": value["cells"],
                "status": status, "reason": outcome["reason"],
            })
            if status != "refused" or outcome["reason"] != "non_iso_date":
                differences.append({"key": key, "reason": "non_iso_date_was_not_explicitly_refused"})
    for key in expected.keys() | seen.keys():
        if seen[key] != 1:
            differences.append({"key": key, "reason": "field_outcome_count", "actual": seen[key]})
    return {
        "pass": not differences,
        "expected_fields": len(expected), "actual_outcomes": len(outcomes),
        "statuses": dict(sorted(Counter(outcome["status"] for outcome in outcomes).items())),
        "refusal_reasons": dict(sorted(Counter(
            outcome["reason"] for outcome in outcomes if outcome["status"] == "refused"
        ).items())),
        "non_iso_date_refusals": non_iso_dates,
        "differences": differences,
        "scope": "Per-field Fact materialization decisions, separate from extracted/excluded row accounting.",
    }


def _is_iso_date(text: str) -> bool:
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def implementation_identity() -> dict[str, str]:
    """Record exact current code bytes even when a worktree is not committed."""

    names = (
        "native_matrix_measurement.py", "native_matrix.py", "native_matrix_bindings.py",
        "facts.py", "materializer.py", "fact_types.py", "source_append.py",
        "extractor_lineage.py", "extraction_runs.py", "row_accounting.py",
        "reader_segments.py", "native_segment_projection.py", "token_layers.py",
    )
    # The schema is a package of family modules since card 21; every one of them
    # is read, so a family added later cannot drop out of the recorded identity.
    schema = tuple(
        f"models/{path.name}"
        for path in sorted((REPO_ROOT / "src" / "corridor" / "models").glob("*.py"))
    )
    return {
        f"src/corridor/{name}": sha256((REPO_ROOT / "src/corridor" / name).read_bytes()).hexdigest()
        for name in (*names, *schema)
    }


def compare_persisted_proposals(
    pages: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    payloads: Sequence[Mapping[str, Any]],
    bound_sources: Mapping[tuple[int, str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Read proposals back and compare values and bound source identities."""

    expected = {
        (page["number"], row["row_id"]): row
        for page in pages for row in page["reading"]["rows"]
        if row["disposition"] == "extracted"
    }
    indexed_outcomes = {
        (outcome["page"], outcome["local_row_id"], outcome["field"]): outcome
        for outcome in outcomes
    }
    differences = []
    seen = Counter()
    for payload in payloads:
        key = (payload["page"], payload["local_row_id"])
        seen[key] += 1
        if key not in expected:
            differences.append({"key": key, "reason": "unexpected_proposal"})
            continue
        expected_fields = {name: value["text"] for name, value in expected[key]["fields"].items()}
        differences.extend(_differences(expected_fields, payload["fields"], f"{key}.fields"))
        sources = payload["field_sources"]
        if sources.keys() != expected_fields.keys():
            differences.append({"key": key, "reason": "proposal_source_field_membership"})
        for field, source_roles in sources.items():
            outcome = indexed_outcomes.get((*key, field))
            if outcome is None:
                differences.append({"key": (*key, field), "reason": "proposal_without_field_outcome"})
                continue
            differences.extend(_differences(
                bound_sources.get((*key, field)), source_roles, f"{key}.{field}.source_bindings"
            ))
            for role in ("value_source", "context"):
                links = source_roles[role]
                outcome_key = "value_source_ids" if role == "value_source" else "context_source_ids"
                if ([link["segment_id"] for link in links] != outcome[outcome_key]
                        or any(not isinstance(link["segment_id"], int) or link["segment_id"] <= 0 for link in links)):
                    differences.append({"key": (*key, field), "reason": f"proposal_{role}_binding"})
    for key in expected.keys() | seen.keys():
        if seen[key] != 1:
            differences.append({"key": key, "reason": "proposal_count", "actual": seen[key]})
    return {"pass": not differences, "expected_proposals": len(expected),
            "persisted_proposals": len(payloads), "differences": differences}


def persisted_bound_sources(session, document, mapping):
    """Certify persisted cells/spans, including fields that refused a Fact."""

    from corridor.reader_segments import NativeCellIndex

    index = NativeCellIndex(document, mapping.native_reading)
    selected = {}
    fields = {}
    for row in mapping.rows:
        for field in row.fields:
            roles = {}
            for role, references in (("value_source", field.value_sources), ("context", field.context_sources)):
                roles[role] = []
                for reference in references:
                    if reference.scoped_id not in selected:
                        selected[reference.scoped_id] = reference.select(session, document, index)
                    roles[role].append({"segment_id": selected[reference.scoped_id].id,
                                        "scoped_id": reference.scoped_id, "model_id": reference.model_id})
            fields[(row.page_no, row.local_row_id, field.name)] = roles
    return fields


def persisted_fact_evidence(session, document, run_id, reading, source, outcomes, pages, payloads):
    """Replay committed Facts with one validated reading and retain ordered links."""

    from sqlalchemy import select
    from corridor.facts import replay_fact
    from corridor.models import Fact, FactSource, SourceSegment

    expected = {
        (outcome["row_id"], outcome["field"]): outcome
        for outcome in outcomes if outcome["status"] == "materialized"
    }
    row_fields = {(page["number"], row["row_id"]): row["fields"]
                  for page in pages for row in page["reading"]["rows"]}
    proposal_sources = {(payload["native_row_id"], field): roles
                        for payload in payloads for field, roles in payload["field_sources"].items()}
    records = []
    seen = Counter()
    differences = []
    for fact in session.scalars(select(Fact).where(Fact.extraction_run_id == run_id).order_by(Fact.id)):
        key = (fact.subject_key, fact.fact_type)
        seen[key] += 1
        replayed = replay_fact(session, document, fact, source, native_reading=reading)
        value = replayed.isoformat() if isinstance(replayed, date) else replayed
        if key in expected:
            outcome = expected[key]
            retained_value = row_fields[(outcome["page"], outcome["local_row_id"])][outcome["field"]]["text"]
            if value != retained_value:
                differences.append({"key": key, "reason": "persisted_fact_value", "expected": retained_value, "actual": value})
        links = []
        for link, segment in session.execute(
            select(FactSource, SourceSegment)
            .join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
            .where(FactSource.fact_id == fact.id)
            .order_by(FactSource.role, FactSource.ordinal)
        ):
            links.append({
                "role": link.role, "ordinal": link.ordinal, "segment_id": segment.id,
                "kind": segment.kind, "page": segment.page_no,
                "table": segment.table_index, "row": segment.cell_row,
                "column": segment.cell_column, "text": segment.exact_text,
                "content_sha256": segment.content_sha256,
                "reading_sha256": segment.reading_sha256,
            })
        for role in {link["role"] for link in links}:
            ordinals = [link["ordinal"] for link in links if link["role"] == role]
            if ordinals != list(range(1, len(ordinals) + 1)):
                differences.append({"key": key, "reason": "fact_source_role_ordinals"})
        for role in ("value_source", "context"):
            actual_ids = [link["segment_id"] for link in links if link["role"] == role]
            expected_ids = [link["segment_id"] for link in proposal_sources.get(key, {}).get(role, [])]
            if actual_ids != expected_ids:
                differences.append({"key": key, "reason": f"persisted_fact_{role}_binding"})
        records.append({
            "id": fact.id, "subject_key": fact.subject_key, "field": fact.fact_type,
            "value": value,
            "transformation": fact.transformation, "content_sha256": fact.content_sha256,
            "sources": links,
        })
    for key in expected.keys() | seen.keys():
        if seen[key] != (1 if key in expected else 0):
            differences.append({"key": key, "reason": "persisted_fact_count", "actual": seen[key]})
    return records, {
        "pass": not differences, "expected_facts": len(expected),
        "persisted_and_replayed_facts": len(records), "differences": differences,
    }


def compare_persisted_row_accounting(base, identity, reading_sha256, pages, outcomes, persisted):
    expected = {
        **base, "schema_version": "native-matrix-row-accounting-v1",
        "native_mapping": {"identity": identity, "reading_sha256": reading_sha256, "pages": pages},
        "field_materialization": outcomes,
    }
    differences = _differences(
        json.loads(canonical_bytes(expected)), json.loads(canonical_bytes(persisted)), "row_accounting"
    )
    return {"pass": not differences, "expected_sha256": digest(expected),
            "actual_sha256": digest(persisted), "differences": differences}


def replay_case(database, case: RetainedCase, configuration, output: Path) -> dict[str, Any]:
    """Execute the actual adapter, commit, and read its values back for scoring."""

    from sqlalchemy import select
    from corridor.models import Candidate, Document, ExtractionRun, Project
    from corridor.native_matrix import extract_native_matrix
    from corridor.token_layers import read_native_pdf

    client = RecordedStructureClient(case, configuration)
    reading = read_native_pdf(
        case.source, source_sha256=case.specification["source_sha256"],
        engine=configuration["native_engine"], dpi=configuration["native_dpi"],
    )
    with database.session_factory() as session:
        project = Project(slug=case.name, name=f"Offline retained-answer replay: {case.name}")
        session.add(project)
        session.flush()
        document = Document(
            project_id=project.id, sha256=reading.rendition_sha256,
            filename=Path(case.document["source"]).name, doc_type="matrix",
            pages=len(reading.pages), parse_status="parsed", numbering_scheme="project-unique",
        )
        session.add(document)
        session.flush()
        extraction = extract_native_matrix(
            session, document, reading=reading, source_path=case.source,
            client=client, images=case.images, document_label=Path(case.document["source"]).name,
        )
        client.require_complete()
        run_id, document_id = extraction.run.id, document.id
        pages, outcomes = extraction.mapping.pages, list(extraction.field_outcomes)
        session.commit()
    comparison = compare_retained_reading(
        case.document, pages,
        source_sha256=reading.rendition_sha256,
        adjudications=load_adjudications(),
    )
    row_comparison = compare_required_rows(case.document, pages)
    diagnostics = compare_mapping_diagnostics(case.document, pages)
    topology = []
    raw_pages = {page["number"]: page for page in reading.pages}
    for diagnostic in diagnostics["diagnostics"]:
        page = next(page for page in pages if page["number"] == diagnostic["page"])
        body = page["reading"]
        repeated = Counter(diagnostic["actual_unmapped"]) - Counter(diagnostic["retained_unmapped"])
        headers = raw_pages[page["number"]]["tables"]["value"][body["matrix_table"]]["structured_cells"]
        topology.append({"page": page["number"], "current_header_cells": [
            {key: cell[key] for key in ("row", "column", "row_span", "column_span", "text")}
            for cell in headers if cell["row"] == body["header_row"] and semantics.clean(cell["text"]) in repeated
        ], "historical_header_geometry": "not_retained"})
    diagnostics["current_topology"] = topology
    field_comparison = compare_field_outcomes(pages, outcomes)
    with database.session_factory() as session:
        document = session.get(Document, document_id)
        run = session.get(ExtractionRun, run_id)
        payloads = [candidate.payload_json for candidate in session.scalars(
            select(Candidate).where(Candidate.extraction_run_id == run_id).order_by(Candidate.id)
        )]
        bound_sources = persisted_bound_sources(session, document, extraction.mapping)
        proposal_comparison = compare_persisted_proposals(pages, outcomes, payloads, bound_sources)
        fact_records, fact_comparison = persisted_fact_evidence(
            session, document, run_id, reading, case.source, outcomes, pages, payloads,
        )
        run_receipt = {
            "id": run.id, "row_accounting": run.row_accounting_json,
            "extractor_configuration": run.extractor_config_json,
            "extractor_config_sha256": run.extractor_config_sha256,
        }
        accounting_comparison = compare_persisted_row_accounting(
            extraction.mapping.row_accounting, extraction.mapping.identity,
            reading.reading_sha256, pages, outcomes, run.row_accounting_json,
        )
    document_output = {"source": str(case.source), "pages": pages, "field_outcomes": outcomes,
                       "proposals": payloads, "facts": fact_records, "run": run_receipt}
    target = output / f"{case.name}.json"
    target.write_text(json.dumps(document_output, indent=1) + "\n")
    required_passed = diagnostics["required_semantic_output_unchanged"] and all(
        result["pass"] for result in (row_comparison, field_comparison, proposal_comparison, fact_comparison, accounting_comparison)
    )
    return {
        "name": case.name, "cohort": case.specification["cohort"],
        "source_sha256": reading.rendition_sha256, "reading_sha256": reading.reading_sha256,
        "reader_identity": reading.identity,
        "manifest_sha256": case.specification["manifest_sha256"],
        "input_files": case.manifest["entries"],
        "output": {"file": target.name, "sha256": sha256(target.read_bytes()).hexdigest()},
        "retained_reading_comparison": comparison,
        "required_row_parity": row_comparison,
        "full_reading_parity": comparison["pass"],
        "mapping_diagnostics": diagnostics,
        "field_materialization": field_comparison,
        "persisted_proposals": proposal_comparison, "persisted_facts": fact_comparison,
        "persisted_row_accounting": accounting_comparison,
        "row_accounting_sha256": digest(run_receipt["row_accounting"]),
        "extractor_config_sha256": run_receipt["extractor_config_sha256"],
        "historical_usage": case.document["usage"],
        "new_usage": {"provider_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "cached_tokens": 0, "replayed_answers": client.replayed_calls, "provider_cost": 0},
        "pass": required_passed,
        "status": ("passed" if comparison["pass"] else "passed_with_diagnostic_differences") if required_passed else "failed",
        "required_outcomes_pass": required_passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new receipt directory")
    database_args = parser.add_mutually_exclusive_group(required=True)
    database_args.add_argument("--postgres-admin-url", help="local PostgreSQL 16 admin URL; only a new disposable database is migrated")
    database_args.add_argument("--postgres-admin-url-env", help="name of an environment variable containing the local PostgreSQL 16 admin URL")
    parser.add_argument("--results-root", type=Path, help="directory holding the seven manifest-named retained answer directories")
    args = parser.parse_args(argv)
    admin_url = args.postgres_admin_url or os.environ.get(args.postgres_admin_url_env, "")
    if not admin_url:
        parser.error("the requested PostgreSQL admin URL environment variable is empty or absent")
    if args.output.exists():
        parser.error("output already exists; choose a new receipt directory")
    dataset_bytes = DATASET_PATH.read_bytes()
    dataset = json.loads(dataset_bytes)
    configuration = dataset["historical_configuration"]
    verify_measured_configuration(configuration)
    cases = [load_case(specification, results_root=args.results_root)
             for specification in dataset["cases"]]
    references = {
        cohort: machine_reference_counts(REPO_ROOT / reference["path"], reference["sha256"])
        for cohort, reference in dataset["machine_references"].items()
    }
    implementation = implementation_identity()
    args.output.mkdir(parents=True)
    from corridor.m8_acceptance_database import provision_disposable_postgres

    results = []
    with provision_disposable_postgres(
        admin_url, repo_root=REPO_ROOT, label="native_matrix_measure",
    ) as database:
        database_receipt = {"name": database.name, "postgres_version": database.postgres_version,
                            "migration_head": database.migration_head, "disposable": True}
        for case in cases:
            result = replay_case(database, case, configuration, args.output)
            results.append(result)
            print(f"{case.name}: {result['retained_reading_comparison']['actual_rows']} rows; required_outcomes={result['required_outcomes_pass']}; full_reading_parity={result['full_reading_parity']}", flush=True)
    machine_scores = {}
    if implementation_identity() != implementation:
        raise ReplayRefusal("implementation bytes changed during this replay; choose a new output directory")
    for cohort, reference in references.items():
        outputs = [json.loads(verified_bytes(args.output / result["output"]["file"], result["output"]["sha256"]))
                   for result in results if result["cohort"] == cohort]
        machine_scores[cohort] = score_machine_identifiers(outputs, reference)
    passed = all(result["pass"] for result in results) and all(result["pass"] for result in machine_scores.values())
    full_parity = all(result["full_reading_parity"] for result in results)
    status = ("passed" if full_parity else "passed_with_diagnostic_differences") if passed else "failed"
    receipt = {
        "schema_version": RECEIPT_SCHEMA, "mode": "offline_retained_answer_replay",
        "pass": passed, "dataset_sha256": sha256(dataset_bytes).hexdigest(),
        "required_outcomes_pass": passed, "status": status,
        "full_reading_parity": full_parity,
        "retained_at": str(args.output.resolve()),
        "machine_references": dataset["machine_references"],
        "historical_configuration": configuration, "implementation_files": implementation,
        "database": {**database_receipt, "dropped_after_replay": True},
        "documents": results, "machine_identifier_matching": machine_scores,
        "limitations": dataset["limitations"],
        "historical_usage": dict(sum((Counter(case.document["usage"]) for case in cases), Counter())),
        "new_usage": {"provider_calls": 0, "provider_cost": 0, "replayed_answers": sum(len(case.document["pages"]) for case in cases)},
    }
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Offline retained-answer replay {status}; full_reading_parity={full_parity}: {args.output / 'receipt.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
