"""Retained-reading regressions must fail beyond identifier-only agreement (#737)."""

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from corridor import native_matrix_measurement as measurement
from corridor_pdf_reader.replacement import semantics


@pytest.fixture
def retained():
    return {"pages": [{
        "number": 1,
        "listing": "The exact listing, including local IDs.",
        "structure": {
            "is_utility_matrix": True, "matrix_table": 0, "header_row": 0,
            "columns": [{"index": 1, "canonical_field": "utility_id"}],
            "page_attributes": {"external_org": None}, "mapping_confidence": 0.9,
        },
        "reading": {
            "number": 1, "is_utility_matrix": True, "matrix_table": 0,
            "header_row": 0, "mapping_confidence": 0.9,
            "mapping": {"fields": {"1": "utility_id"}, "marks": {"2": "Relocate"}, "unmapped": ["Estimated date"]},
            "page_attributes": {}, "refused": ["column index 9 is not in the table"],
            "rows": [
                {"row_id": "structure:1:table:0:row:1", "row": 1,
                 "disposition": "extracted", "reason": "candidate_recorded", "fields": {
                     "utility_id": {"text": "1", "cells": ["t0r1c1"]},
                     "external_org": {"text": "Exact Utilities", "cells": ["t0r1c0"]},
                     "resolution_strategy": {"text": "Relocate", "cells": ["t0r1c2"]},
                 }},
                {"row_id": "structure:1:table:0:row:2", "row": 2,
                 "disposition": "skipped", "reason": "retired_row", "fields": {
                     "utility_id": {"text": "2", "cells": ["t0r2c1"]},
                 }},
                {"row_id": "structure:1:table:0:row:3", "row": 3,
                 "disposition": "skipped", "reason": "insufficient_mapped_fields", "fields": {
                     "utility_id": {"text": "3", "cells": ["t0r3c1"]},
                 }},
            ],
        },
    }]}


def test_exact_reading_accepts_extra_page_provenance_and_preserves_every_row(retained):
    actual = deepcopy(retained["pages"])
    actual[0]["native_reading_sha256"] = "a" * 64

    result = measurement.compare_retained_reading(retained, actual)

    assert result["pass"] is True
    assert result["expected_sha256"] == result["actual_sha256"]
    assert result["compared_rows"] == result["actual_rows"] == 3
    assert result["reasons"] == {"candidate_recorded": 1, "retired_row": 1, "insufficient_mapped_fields": 1}
    assert result["field_values"] == 5


@pytest.mark.parametrize("mutation", [
    "field_text", "local_reference", "excluded_field", "reason", "disposition",
    "mapping", "mapping_confidence", "page_refusal", "listing", "raw_structure",
    "duplicate_excluded_row", "missing_excluded_row",
])
def test_changed_decisions_fail_even_when_final_machine_population_matches(retained, mutation):
    actual = deepcopy(retained)
    page = actual["pages"][0]
    reading = page["reading"]
    rows = reading["rows"]
    if mutation == "field_text":
        rows[0]["fields"]["external_org"]["text"] = "Wrong Utilities"
    elif mutation == "local_reference":
        rows[0]["fields"]["external_org"]["cells"] = ["t0r2c0"]
    elif mutation == "excluded_field":
        rows[1]["fields"]["utility_id"]["text"] = "wrong excluded value"
    elif mutation == "reason":
        rows[1]["reason"] = "missing_required_fields"
    elif mutation == "disposition":
        rows[1]["disposition"] = "blank"
    elif mutation == "mapping":
        reading["mapping"]["marks"]["2"] = "Protect"
    elif mutation == "mapping_confidence":
        reading["mapping_confidence"] = 0.8
    elif mutation == "page_refusal":
        reading["refused"] = []
    elif mutation == "listing":
        page["listing"] += " Changed."
    elif mutation == "raw_structure":
        page["structure"]["header_row"] = None
    elif mutation == "duplicate_excluded_row":
        rows.append(deepcopy(rows[1]))
    elif mutation == "missing_excluded_row":
        rows.pop()

    assert measurement.score_machine_identifiers([actual], Counter({"1": 1}))["pass"] is True
    comparison = measurement.compare_retained_reading(retained, actual["pages"])
    assert comparison["pass"] is False
    assert comparison["differences"]


def test_machine_matching_keeps_duplicate_multiplicity(retained):
    reference = Counter({"1": 2})
    one = measurement.score_machine_identifiers([retained], reference)
    two = measurement.score_machine_identifiers([retained, retained], reference)

    assert one["pass"] is False and one["matched_rows"] == 1
    assert one["missing"] == [{"source_ref": "1", "count": 1}]
    assert two["pass"] is True and two["matched_rows"] == 2


def test_machine_reference_ids_do_not_claim_scoped_source_occurrence_identity(retained):
    actual = deepcopy(retained)
    actual["pages"][0]["number"] = 2

    assert measurement.score_machine_identifiers([actual], Counter({"1": 1}))["pass"] is True
    assert measurement.compare_retained_reading(retained, actual["pages"])["pass"] is False


@pytest.fixture
def recorded_case(tmp_path, retained):
    directory = tmp_path / "retained"
    directory.mkdir()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"fixture source bytes; this test does not parse PDF")
    image = directory / "page-1.png"
    image.write_bytes(b"fixture raster bytes; this test does not render")
    document = {
        **retained, "source": str(source), "model": "gpt-5.6-luna",
        "prompt_version": "matrix_structure_ids_v1",
        "usage": {"calls": 1, "prompt_tokens": 7000, "completion_tokens": 200, "cached_tokens": 0},
    }
    (directory / "document.json").write_text(json.dumps(document))
    entries = [{"path": path.name, "sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
               for path in sorted(directory.iterdir())]
    manifest = {"retained_at": str(directory), "files": len(entries), "bytes": sum(entry["bytes"] for entry in entries), "entries": entries}
    path = tmp_path / "fixture.manifest.json"
    path.write_text(json.dumps(manifest))
    specification = {
        "name": "fixture", "manifest": path.name,
        "manifest_sha256": sha256(path.read_bytes()).hexdigest(),
        "source_sha256": sha256(source.read_bytes()).hexdigest(), "pages": 1,
        "expected_rows": 3,
        "expected_reasons": {"candidate_recorded": 1, "retired_row": 1, "insufficient_mapped_fields": 1},
    }
    return measurement.load_case(specification, repo_root=tmp_path)


def _configuration():
    return json.loads(measurement.DATASET_PATH.read_bytes())["historical_configuration"]


def _request(case):
    return {
        "system": semantics.PROMPT_PATH.read_text(),
        "user": case.document["pages"][0]["listing"],
        "schema": deepcopy(semantics.STRUCTURE_SCHEMA),
        "images": [case.images[1]],
    }


def test_recorded_client_returns_raw_answer_but_never_labels_history_as_new_calls(recorded_case):
    client = measurement.RecordedStructureClient(recorded_case, _configuration())
    original = deepcopy(recorded_case.document)

    answer = client.complete(**_request(recorded_case))
    answer["mapping_confidence"] = 0.1
    client.require_complete()

    assert recorded_case.document == original
    assert client.replayed_calls == 1
    assert client.calls == client.prompt_tokens == client.completion_tokens == client.cached_tokens == 0
    assert recorded_case.document["usage"]["calls"] == 1
    with pytest.raises(measurement.ReplayRefusal, match="unrecorded"):
        client.complete(**_request(recorded_case))


@pytest.mark.parametrize("mutation", ["system", "schema", "listing", "missing_image", "changed_image", "unconsumed"])
def test_recorded_client_refuses_changed_measured_inputs(recorded_case, mutation):
    client = measurement.RecordedStructureClient(recorded_case, _configuration())
    request = _request(recorded_case)
    if mutation == "system":
        request["system"] += "changed"
    elif mutation == "schema":
        request["schema"]["additionalProperties"] = True
    elif mutation == "listing":
        request["user"] += "changed"
    elif mutation == "missing_image":
        request["images"] = []
    elif mutation == "changed_image":
        recorded_case.images[1].write_bytes(b"different resolution")

    with pytest.raises(measurement.ReplayRefusal):
        client.require_complete() if mutation == "unconsumed" else client.complete(**request)
    assert client.replayed_calls == client.calls == 0


@pytest.mark.parametrize("artifact", ["manifest", "document", "image", "source"])
def test_retained_artifacts_are_digest_checked_before_parsing(recorded_case, artifact):
    root = recorded_case.directory.parent
    target = {
        "manifest": root / recorded_case.specification["manifest"],
        "document": recorded_case.directory / "document.json",
        "image": recorded_case.images[1], "source": recorded_case.source,
    }[artifact]
    target.write_bytes(b"untrusted invalid JSON or source")

    with pytest.raises(measurement.ReplayRefusal, match="digest"):
        measurement.load_case(recorded_case.specification, repo_root=root)


def test_registry_pins_all_seven_manifests_and_both_machine_references():
    dataset = json.loads(measurement.DATASET_PATH.read_bytes())
    measurement.verify_measured_configuration(dataset["historical_configuration"])
    cases = dataset["cases"]
    assert len(cases) == 7
    assert sum(case["pages"] for case in cases) == 20
    assert Counter({cohort: sum(case["expected_rows"] for case in cases if case["cohort"] == cohort)
                    for cohort in ("wsdot-9424", "wsdot-9540")}) == {"wsdot-9424": 265, "wsdot-9540": 201}
    for case in cases:
        measurement.verified_bytes(measurement.REPO_ROOT / case["manifest"], case["manifest_sha256"])
    for reference in dataset["machine_references"].values():
        counts = measurement.machine_reference_counts(measurement.REPO_ROOT / reference["path"], reference["sha256"])
        assert counts.total() == reference["expected_rows"]


def _field_outcomes(retained):
    return [{
        "row_id": f"scoped:{row['row_id']}", "local_row_id": row["row_id"],
        "page": page["number"], "field": field,
        "status": "materialized" if row["disposition"] == "extracted" else "not_extracted",
        "reason": "source_value" if row["disposition"] == "extracted" else row["reason"],
        "value_source_ids": [index], "context_source_ids": [],
    } for index, (page, row, field) in enumerate((
        (page, row, field) for page in retained["pages"]
        for row in page["reading"]["rows"] for field in row["fields"]
    ), start=1)]


def test_non_iso_date_is_separate_from_an_extracted_rows_disposition(retained):
    row = retained["pages"][0]["reading"]["rows"][0]
    row["fields"]["committed_date"] = {"text": "Summer 2020", "cells": ["t0r1c19"]}
    outcomes = _field_outcomes(retained)
    outcome = next(outcome for outcome in outcomes if outcome["field"] == "committed_date")
    outcome.update(status="refused", reason="non_iso_date")

    result = measurement.compare_field_outcomes(retained["pages"], outcomes)

    assert result["pass"] is True
    assert result["statuses"] == {"materialized": 3, "not_extracted": 2, "refused": 1}
    assert result["non_iso_date_refusals"][0]["text"] == "Summer 2020"
    assert row["disposition"] == "extracted"
    outcome.update(status="materialized", reason="invented_precision")
    assert measurement.compare_field_outcomes(retained["pages"], outcomes)["pass"] is False


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "excluded_materialized", "unbound"])
def test_every_field_has_one_separately_bound_outcome(retained, mutation):
    outcomes = _field_outcomes(retained)
    if mutation == "missing":
        outcomes.pop()
    elif mutation == "duplicate":
        outcomes.append(deepcopy(outcomes[0]))
    elif mutation == "excluded_materialized":
        outcomes[-1]["status"] = "materialized"
    elif mutation == "unbound":
        outcomes[0]["value_source_ids"] = []
    assert measurement.compare_field_outcomes(retained["pages"], outcomes)["pass"] is False


def _proposal(retained):
    row = retained["pages"][0]["reading"]["rows"][0]
    return {
        "page": 1, "local_row_id": row["row_id"], "native_row_id": f"scoped:{row['row_id']}",
        "fields": {field: value["text"] for field, value in row["fields"].items()},
        "field_sources": {
            field: {"value_source": [{"segment_id": index, "scoped_id": f"scoped:{field}", "model_id": value["cells"][0]}], "context": []}
            for index, (field, value) in enumerate(row["fields"].items(), start=1)
        },
    }


@pytest.mark.parametrize("mutation", [None, "value", "source", "missing", "duplicate"])
def test_persisted_proposal_values_and_bindings_must_match_the_reading(retained, mutation):
    proposals = [_proposal(retained)]
    bound_sources = {(1, proposals[0]["local_row_id"], field): deepcopy(roles)
                     for field, roles in proposals[0]["field_sources"].items()}
    if mutation == "value":
        proposals[0]["fields"]["external_org"] = "Different Utilities"
    elif mutation == "source":
        proposals[0]["field_sources"]["external_org"]["value_source"][0]["scoped_id"] = "another_document"
    elif mutation == "missing":
        proposals.clear()
    elif mutation == "duplicate":
        proposals.append(deepcopy(proposals[0]))
    result = measurement.compare_persisted_proposals(retained["pages"], _field_outcomes(retained), proposals, bound_sources)
    assert result["pass"] is (mutation is None)


@pytest.mark.parametrize("mutation", [None, "row_reason", "field_status", "reading_identity"])
def test_committed_run_receipt_must_preserve_row_and_field_decisions(retained, mutation):
    outcomes = _field_outcomes(retained)
    base = {"rows": [{"row_id": "scoped:1", "disposition": "extracted", "reason": "candidate_recorded"}]}
    receipt = {
        **deepcopy(base), "schema_version": "native-matrix-row-accounting-v1",
        "native_mapping": {"identity": "mapping-1", "reading_sha256": "a" * 64, "pages": retained["pages"]},
        "field_materialization": deepcopy(outcomes),
    }
    if mutation == "row_reason":
        receipt["rows"][0]["reason"] = "retired_row"
    elif mutation == "field_status":
        receipt["field_materialization"][0]["status"] = "refused"
    elif mutation == "reading_identity":
        receipt["native_mapping"]["reading_sha256"] = "b" * 64

    result = measurement.compare_persisted_row_accounting(
        base, "mapping-1", "a" * 64, retained["pages"], outcomes, receipt,
    )
    assert result["pass"] is (mutation is None)


def test_only_added_duplicate_unmapped_diagnostics_keep_required_parity_separate(retained):
    actual = deepcopy(retained["pages"])
    actual[0]["reading"]["mapping"]["unmapped"].append("Estimated date")
    original = deepcopy(retained)

    full = measurement.compare_retained_reading(retained, actual)
    rows = measurement.compare_required_rows(retained, actual)
    diagnostics = measurement.compare_mapping_diagnostics(retained, actual)

    assert full["pass"] is False and full["differences"]
    assert rows["pass"] is True
    assert diagnostics["classification"] == "duplicate_unmapped_heading_diagnostic"
    assert diagnostics["required_semantic_output_unchanged"] is True
    assert diagnostics["diagnostics"][0]["actual_unmapped"] == ["Estimated date", "Estimated date"]
    assert retained == original


@pytest.mark.parametrize("mutation", [
    "new_heading", "removed_heading", "reordered_headings", "refusal", "confidence",
    "matrix_table", "header_row", "raw_structure", "listing", "field_text",
])
def test_a_diagnostic_exception_never_hides_a_substantive_reading_change(retained, mutation):
    retained["pages"][0]["reading"]["mapping"]["unmapped"] = ["Estimated date", "Permit status"]
    actual = deepcopy(retained["pages"])
    page = actual[0]
    reading = page["reading"]
    reading["mapping"]["unmapped"] = ["Estimated date", "Estimated date", "Permit status"]
    if mutation == "new_heading":
        reading["mapping"]["unmapped"].append("New field")
    elif mutation == "removed_heading":
        reading["mapping"]["unmapped"] = ["Estimated date", "Estimated date"]
    elif mutation == "reordered_headings":
        reading["mapping"]["unmapped"] = ["Permit status", "Estimated date", "Estimated date"]
    elif mutation == "refusal":
        reading["refused"] = []
    elif mutation == "confidence":
        reading["mapping_confidence"] = 1.0
    elif mutation == "matrix_table":
        reading["matrix_table"] = 1
    elif mutation == "header_row":
        reading["header_row"] = 1
    elif mutation == "raw_structure":
        page["structure"]["header_row"] = None
    elif mutation == "listing":
        page["listing"] += " changed"
    elif mutation == "field_text":
        reading["rows"][0]["fields"]["external_org"]["text"] = "Wrong Utilities"

    diagnostics = measurement.compare_mapping_diagnostics(retained, actual)
    assert diagnostics["classification"] == "other_difference"
    assert diagnostics["required_semantic_output_unchanged"] is False
    assert measurement.compare_retained_reading(retained, actual)["pass"] is False


def test_an_adjudicated_difference_leaves_the_exact_regression_result_untouched(retained):
    """A recorded adjudication explains a difference; it never erases one.

    The retained reading is the incumbent's answer, not the document's. When
    the source shows the incumbent was wrong, the honest record keeps the
    exact regression failing and says which differences a person settled
    against the page, so `pass` still means "identical to the incumbent" and a
    reader can see what was adjudicated and why (ADR-0023).
    """
    actual = deepcopy(retained["pages"])
    actual[0]["reading"]["mapping"]["unmapped"] = ["AGREEMENT STATUS", "FRANCHISE (F) AND NUMBER"]
    adjudications = [{
        "source_sha256": "b" * 64,
        "path": "pages[0].reading.mapping.unmapped",
        "reason": "length",
        "expected": 1,
        "actual": 2,
        "verdict": "the page prints one merged header over two columns",
    }]

    settled = measurement.compare_retained_reading(
        retained, actual, source_sha256="b" * 64, adjudications=adjudications,
    )

    assert settled["pass"] is False, "the exact regression still reports the difference"
    assert len(settled["differences"]) == 2
    assert len(settled["adjudicated"]) == 1
    assert settled["adjudicated"][0]["verdict"].startswith("the page prints")
    assert [d["path"] for d in settled["unadjudicated_differences"]] == [
        "pages[0].reading.mapping.unmapped[0]"
    ]
    assert settled["unadjudicated_pass"] is False


def test_an_adjudication_only_settles_the_exact_difference_it_names(retained):
    """A near-miss adjudication settles nothing: the values must match exactly."""
    actual = deepcopy(retained["pages"])
    actual[0]["reading"]["mapping"]["unmapped"] = ["SOMETHING ELSE"]
    adjudications = [{
        "source_sha256": "b" * 64,
        "path": "pages[0].reading.mapping.unmapped[0]",
        "reason": "value",
        "expected": "AGREEMENT STATUS",
        "actual": "FRANCHISE (F) AND NUMBER",
        "verdict": "settles a different reading",
    }]

    settled = measurement.compare_retained_reading(
        retained, actual, source_sha256="b" * 64, adjudications=adjudications,
    )

    assert settled["adjudicated"] == []
    assert len(settled["unadjudicated_differences"]) == len(settled["differences"]) == 1
    assert settled["unadjudicated_pass"] is False


def test_an_adjudication_for_another_document_settles_nothing(retained):
    actual = deepcopy(retained["pages"])
    actual[0]["reading"]["mapping"]["unmapped"] = ["FRANCHISE (F) AND NUMBER"]
    difference = measurement.compare_retained_reading(retained, actual)["differences"][0]
    adjudications = [{**difference, "source_sha256": "c" * 64, "verdict": "another document"}]

    settled = measurement.compare_retained_reading(
        retained, actual, source_sha256="b" * 64, adjudications=adjudications,
    )

    assert settled["adjudicated"] == []
    assert settled["unadjudicated_differences"] == settled["differences"]
