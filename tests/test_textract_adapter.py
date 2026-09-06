"""The Textract adapter refuses before it calls, and everything it keeps is identified (#732, ADR-0094).

The acceptance rule is zero outbound requests when the authorization is
absent or mismatched, and it is proved with a transport double that fails
the test on any call: the boundary is opened against it with each kind of
gap, and the refusal must arrive with the double untouched, no cache
directory on disk, and a Processing Failure record naming every failing
field. The rest of the file proves what the adapter keeps once a request is
covered: the six identity fields on every cache entry, one charge for one
raster under two renditions, a hit in the next Extraction Run, retries and
failed attempts counted apart from calls, the native-glyph re-map with
run-mate rescue off, and the guards that keep the imported client behind
the boundary and out of production.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from corridor_pdf_reader import provenance
from corridor_pdf_reader.textract import remap as imported_remap
from corridor_pdf_reader.textract.blocks import METHOD
from corridor_pdf_reader.textract.tests.helpers import Page, minimal_pdf
from corridor_pdf_reader.textract_adapter.boundary import (
    AuthorizedTextract,
    TextractProcessingFailure,
    open_boundary,
)
from corridor_pdf_reader.textract_adapter.identity import (
    ADAPTER_VERSION,
    NormalizationConfiguration,
    RequestConfiguration,
    adapter_identity,
    normalize,
    raw_response_digest,
    reading_digest,
)
from corridor_pdf_reader.textract_adapter.records import (
    PROVIDER_POSTURE,
    CustomerAuthorization,
    ExperimentScope,
    RequestBoundary,
    mismatches,
)
from corridor_pdf_reader.textract_adapter.rendering import native_glyphs, rasterize_page

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "corridor_pdf_reader"
FIXTURES = PACKAGE_ROOT / "textract" / "tests" / "fixtures"
POSTURE_DOCUMENT = REPO_ROOT / PROVIDER_POSTURE.document
# The posture the maintainer has not yet accepted refuses every customer
# record; the tests of the other fields run against an accepted copy so each
# assertion names exactly the field it is about.
ACCEPTED_POSTURE = replace(PROVIDER_POSTURE, status="accepted")
DATASET = "pdf-reader-comparison true-pairs/exact ten development pairs"


class FailingService:
    """The transport double: any outbound request fails the test."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.calls += 1
        pytest.fail("an outbound Textract request was made")


class ClientError(Exception):
    """botocore's shape: a `response` with an Error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class RecordedService:
    """Answers every request with one retained response, after the scripted errors."""

    def __init__(self, response: dict[str, Any], failures: list[str] | None = None) -> None:
        self.response = response
        self.failures = list(failures or [])
        self.calls: list[dict[str, Any]] = []

    def analyze_document(self, *, Document: dict[str, Any], FeatureTypes: list[str]) -> dict[str, Any]:
        self.calls.append({"bytes": len(Document["Bytes"]), "features": list(FeatureTypes)})
        if self.failures:
            raise ClientError(self.failures.pop(0))
        return self.response


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def authorization(**overrides: Any) -> CustomerAuthorization:
    fields: dict[str, Any] = dict(
        record_id="auth-0001",
        customer="customer-a",
        projects=frozenset({"project-1"}),
        source_classes=frozenset({"scanned-pdf"}),
        purposes=frozenset({"scanned-page-reading"}),
        stages=frozenset({"shadow"}),
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        posture_digest=PROVIDER_POSTURE.digest,
        signed_by="a named person",
        signed_on="2026-09-06",
    )
    fields.update(overrides)
    return CustomerAuthorization(**fields)


def experiment(**overrides: Any) -> ExperimentScope:
    fields: dict[str, Any] = dict(
        record_id="exp-0001",
        dataset=DATASET,
        dataset_digest="ca68b55a",
        purpose="extraction-measurement",
        scope="the ten pairs' pages and their scan twins, lanes A, B and C",
        source_classes=frozenset({"public-reference-corpus", "synthetic"}),
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        posture_digest=PROVIDER_POSTURE.digest,
        recorded_by="a named person",
        recorded_on="2026-09-06",
    )
    fields.update(overrides)
    return ExperimentScope(**fields)


def request(**overrides: Any) -> RequestBoundary:
    fields: dict[str, Any] = dict(
        project="project-1",
        source_class="scanned-pdf",
        purpose="scanned-page-reading",
        region="us-east-2",
        posture_identity=PROVIDER_POSTURE.identity,
        stage="shadow",
    )
    fields.update(overrides)
    return RequestBoundary(**fields)


def experiment_request(**overrides: Any) -> RequestBoundary:
    return request(**{"project": DATASET, "source_class": "synthetic", "purpose": "extraction-measurement", "stage": "experiment", **overrides})


CONFIGURATION = RequestConfiguration(dpi=36)


def raster_of(tmp_path: Path, name: str = "page.pdf", **pdf: Any):
    return rasterize_page(minimal_pdf(tmp_path / name, text="Total", **pdf), 1, CONFIGURATION)


def opened(tmp_path: Path, service: Any, record: Any = None, boundary: RequestBoundary | None = None, **options: Any) -> AuthorizedTextract:
    options.setdefault("configuration", CONFIGURATION)
    options.setdefault("extraction_run", "run-1")
    options.setdefault("posture", ACCEPTED_POSTURE)
    return open_boundary(
        record if record is not None else authorization(),
        boundary or request(),
        cache_root=tmp_path / "cache",
        service=service,
        sleep=lambda seconds: None,
        **options,
    )


def response_around_total() -> dict[str, Any]:
    """Textract's view of a page whose only text is 'Total' at (100, 700) PDF points."""
    page = Page()
    word = page.word("TOTAL", (100, 82, 132, 93))
    page.line([word])
    cell = page.cell(1, 1, (90, 75, 160, 100), [word])
    page.cell(1, 2, (160, 75, 230, 100))
    page.table((90, 75, 230, 100), [cell, page.blocks[-1]])
    return page.response()


# --- zero outbound requests -----------------------------------------------------

REFUSALS = {
    "absent": (None, request(), {"authorization-absent"}),
    "project": (authorization(), request(project="project-9"), {"project"}),
    "source-class": (authorization(), request(source_class="email"), {"source-class"}),
    "purpose-by-record": (authorization(), request(purpose="image-region-reading"), {"purpose"}),
    "purpose-outside-posture": (authorization(purposes=frozenset({"anything"})), request(purpose="anything"), {"purpose"}),
    "region-in-request": (authorization(), request(region="us-west-2"), {"region"}),
    "region-in-record": (authorization(region="us-west-2"), request(), {"region"}),
    "posture-identity-in-request": (authorization(), request(posture_identity="some-other-posture"), {"posture-identity"}),
    "posture-identity-in-record": (authorization(posture_identity="some-other-posture"), request(), {"posture-identity"}),
    "posture-digest": (authorization(posture_digest="0" * 64), request(), {"posture-digest"}),
    "stage": (authorization(), request(stage="authoritative"), {"stage"}),
    "experiment-scope-for-a-customer-stage": (experiment(), experiment_request(stage="shadow"), {"stage"}),
    "experiment-scope-for-another-dataset": (experiment(), experiment_request(project="project-1"), {"project"}),
    "experiment-scope-for-another-purpose": (experiment(purpose="something-else"), experiment_request(), {"purpose"}),
    "customer-record-for-an-experiment": (authorization(), experiment_request(), {"project", "source-class", "purpose", "stage"}),
    "not-a-record": ({"record_id": "auth-0001"}, request(), {"record-kind"}),
    "everything-at-once": (authorization(), request(project="project-9", source_class="email", stage="authoritative"), {"project", "source-class", "stage"}),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_an_absent_or_mismatched_authorization_is_refused_with_zero_outbound_requests(tmp_path, name):
    record, boundary, expected = REFUSALS[name]
    service = FailingService()

    with pytest.raises(TextractProcessingFailure) as caught:
        open_boundary(record, boundary, extraction_run="run-1", cache_root=tmp_path / "cache", service=service, posture=ACCEPTED_POSTURE)

    failure = caught.value
    assert {entry.split(":")[0] for entry in failure.mismatches} == expected
    assert failure.outbound_requests == 0
    assert failure.reason == ("authorization-absent" if record is None else "authorization-refused")
    assert service.calls == 0
    assert not (tmp_path / "cache").exists(), "a refusal touches nothing on disk"
    record_kept = failure.record()
    assert record_kept["kind"] == "processing-failure"
    assert record_kept["outbound_requests"] == 0
    assert record_kept["request"] == boundary.as_dict()
    assert record_kept["mismatches"] == list(failure.mismatches)


def test_mismatches_name_every_failing_field_not_the_first():
    found = mismatches(authorization(), request(project="project-9", source_class="email", stage="authoritative"), ACCEPTED_POSTURE)

    assert [entry.split(":")[0] for entry in found] == ["source-class", "project", "stage"]
    assert "project-9" in found[1] and "project-1" in found[1]
    assert mismatches(authorization(), request(), ACCEPTED_POSTURE) == ()
    assert mismatches(experiment(), experiment_request()) == ()


def test_a_customer_record_is_refused_while_the_posture_is_only_proposed(tmp_path):
    """The posture document says no customer page may be transmitted before the
    maintainer accepts it; the boundary enforces that sentence rather than
    relying on nobody signing a record against a proposed posture. An
    experiment scope is not gated on it: replay of retained responses and any
    live measurement call are governed by their own recorded scope."""
    assert PROVIDER_POSTURE.status == "proposed"
    service = FailingService()

    with pytest.raises(TextractProcessingFailure) as caught:
        open_boundary(authorization(), request(), extraction_run="run-1", cache_root=tmp_path / "cache", service=service)

    assert [entry.split(":")[0] for entry in caught.value.mismatches] == ["posture-status"]
    assert caught.value.outbound_requests == 0
    assert service.calls == 0
    assert mismatches(experiment(), experiment_request()) == ()


def test_a_raster_outside_the_declared_configuration_is_refused_before_any_request(tmp_path):
    service = FailingService()
    adapter = opened(tmp_path, service, configuration=RequestConfiguration(dpi=300))

    with pytest.raises(TextractProcessingFailure) as caught:
        adapter.analyze_page(raster_of(tmp_path), rendition_sha256="r", page_number=1)

    assert caught.value.reason == "raster-configuration-mismatch"
    assert caught.value.outbound_requests == 0 and service.calls == 0
    assert adapter.receipt.as_dict()["counts"] == {
        "pages_requested": 1, "hits": 0, "calls": 1 - 1, "retries": 0, "failed_attempts": 0, "outbound_requests": 0, "failures": 1,
    }


# --- what a covered request keeps ------------------------------------------------


def test_a_covered_request_constructs_the_client_and_records_every_identity_field(tmp_path):
    service = RecordedService(fixture("holmberg-5007-page2-clean")["response"])
    adapter = opened(tmp_path, service)
    raster = raster_of(tmp_path)

    reading = adapter.analyze_page(raster, rendition_sha256="rendition-1", page_number=2)

    assert service.calls == [{"bytes": len(raster.png), "features": ["TABLES"]}]
    assert len(reading.page["tables"]) == 1 and len(reading.page["tables"][0]["cells"]) == 16
    assert reading.page["number"] == 2 and reading.page["tables"][0]["method"] == METHOD
    binding = reading.binding
    assert (binding.rendition_sha256, binding.page_number, binding.cached) == ("rendition-1", 2, False)
    assert binding.raster_sha256 == raster.sha256 and binding.model_version == "1.0"
    assert binding.raw_response_digest == raw_response_digest(service.response)
    assert binding.normalized_reading_digest == reading_digest(reading.page)
    assert binding.frame == {"size": [612.0, 792.0], "rotation": 0, "dpi": 36, "pixels": [306, 396], "mode": "L"}
    assert binding.normalization == {"text_source": "textract-words", "parser": METHOD, "remap_margin": 0.0, "rescue_runs": False}
    assert binding.counts == {"tables": 1, "cells": 16, "outside": len(reading.page["outside"]), "clipped": 0}

    identity = json.loads(adapter.identity_path(raster.sha256).read_text(encoding="utf-8"))
    scope = identity["scope"]
    assert scope["authorization_boundary"] == {"kind": "customer-authorization", **request().as_dict()}
    assert scope["operation"] == "AnalyzeDocument" and scope["feature_types"] == ["TABLES"]
    assert scope["region"] == "us-east-2"
    assert scope["request"]["dpi"] == 36 and scope["request"]["mode"] == "L" and scope["request"]["submission"] == "bytes"
    assert scope["adapter"]["adapter_version"] == ADAPTER_VERSION
    assert scope["adapter"]["source_commit"] == provenance.SOURCE_COMMIT
    assert scope["adapter"]["rasterizer"]["pypdfium2"] == "5.13.0"
    assert scope["digest"] == adapter.scope.digest == adapter.directory.name
    assert identity["raster_sha256"] == raster.sha256 and identity["raster_bytes"] == len(raster.png)
    assert identity["model_version"] == "1.0"
    assert identity["raw_response_digest"] == binding.raw_response_digest
    assert identity["request_identity"] == {
        "request_id": None,
        "requested_at": identity["request_identity"]["requested_at"],
        "transport": "boto3:unspecified:us-east-2",
        "attempts": 1,
        "record_id": "auth-0001",
        "retained": False,
    }
    assert identity["bindings"] == [binding.as_dict()]
    assert json.loads((adapter.directory / "scope.json").read_text(encoding="utf-8")) == scope
    assert (adapter.directory / f"{raster.sha256}.json").exists(), "the imported client's own entry, in the scope"

    receipt = adapter.receipt.as_dict()
    assert receipt["kind"] == "textract-cost-receipt" and receipt["extraction_run"] == "run-1"
    assert receipt["authorization"] == {"kind": "customer-authorization", "record_id": "auth-0001"}
    assert receipt["counts"] == {"pages_requested": 1, "hits": 0, "calls": 1, "retries": 0, "failed_attempts": 0, "outbound_requests": 1, "failures": 0}
    assert receipt["charged_pages"] == 1 and receipt["list_charge_usd"] == 0.015
    assert receipt["provider_model_versions"] == ["1.0"]
    assert receipt["bindings"] == [binding.as_dict()]
    assert json.loads(json.dumps(receipt)) == receipt


def test_the_provider_model_is_recorded_and_never_pinned():
    identity = adapter_identity()

    assert identity["provider_model"]["pinned"] is False
    assert identity["provider_model"]["reported_as"] == "AnalyzeDocumentModelVersion"
    assert identity["local_reader"] == {"module": "corridor_pdf_reader.replacement.reader.read_pdf", "engine": "tagged", "dpi": 36}
    assert identity["parser"]["method"] == METHOD
    assert identity["rasterizer"]["pypdfium2"] == "5.13.0" and identity["rasterizer"]["pdfium"]
    assert identity["source_commit"] == provenance.SOURCE_COMMIT


def test_one_raster_under_two_renditions_is_two_bindings_and_one_charge(tmp_path):
    service = RecordedService(fixture("holmberg-5007-page2-clean")["response"])
    adapter = opened(tmp_path, service)
    raster = raster_of(tmp_path)

    printed = adapter.analyze_page(raster, rendition_sha256="print", page_number=2)
    twin = adapter.analyze_page(raster, rendition_sha256="scan-twin", page_number=2)

    assert len(service.calls) == 1
    assert (printed.binding.cached, twin.binding.cached) == (False, True)
    assert printed.binding.raw_response_digest == twin.binding.raw_response_digest
    assert printed.binding.normalized_reading_digest == twin.binding.normalized_reading_digest
    assert [(b.rendition_sha256, b.page_number) for b in adapter.receipt.bindings] == [("print", 2), ("scan-twin", 2)]
    counts = adapter.receipt.as_dict()["counts"]
    assert (counts["calls"], counts["hits"], counts["outbound_requests"]) == (1, 1, 1)
    assert adapter.receipt.as_dict()["charged_pages"] == 1
    identity = json.loads(adapter.identity_path(raster.sha256).read_text(encoding="utf-8"))
    assert [b["rendition_sha256"] for b in identity["bindings"]] == ["print", "scan-twin"]


def test_a_response_one_run_fetched_is_a_hit_in_the_next_run_and_is_not_charged_again(tmp_path):
    raster = raster_of(tmp_path)
    first = opened(tmp_path, RecordedService(fixture("holmberg-5007-page2-clean")["response"]), extraction_run="run-1")
    first.analyze_page(raster, rendition_sha256="r", page_number=1)

    second = opened(tmp_path, FailingService(), extraction_run="run-2")
    reading = second.analyze_page(raster, rendition_sha256="r", page_number=1)

    assert reading.binding.cached is True and reading.binding.extraction_run == "run-2"
    assert reading.binding.raw_response_digest == first.receipt.bindings[0].raw_response_digest
    receipt = second.receipt.as_dict()
    assert receipt["counts"]["hits"] == 1 and receipt["counts"]["calls"] == 0 and receipt["charged_pages"] == 0
    identity = json.loads(second.identity_path(raster.sha256).read_text(encoding="utf-8"))
    assert [b["extraction_run"] for b in identity["bindings"]] == ["run-1", "run-2"]


def test_a_different_boundary_is_a_different_scope_and_its_own_call(tmp_path):
    record = authorization(stages=frozenset({"shadow", "authoritative"}))
    raster = raster_of(tmp_path)
    shadow_service = RecordedService(fixture("holmberg-5007-page2-clean")["response"])
    shadow = opened(tmp_path, shadow_service, record, request(stage="shadow"))
    shadow.analyze_page(raster, rendition_sha256="r", page_number=1)

    authoritative_service = RecordedService(fixture("holmberg-5007-page2-clean")["response"])
    authoritative = opened(tmp_path, authoritative_service, record, request(stage="authoritative"))
    reading = authoritative.analyze_page(raster, rendition_sha256="r", page_number=1)

    assert shadow.scope.digest != authoritative.scope.digest
    assert shadow.directory != authoritative.directory
    assert reading.binding.cached is False and len(authoritative_service.calls) == 1
    assert len(shadow_service.calls) == 1


def test_a_re_signed_record_over_the_same_boundary_reuses_the_retained_response(tmp_path):
    raster = raster_of(tmp_path)
    first = opened(tmp_path, RecordedService(fixture("holmberg-5007-page2-clean")["response"]), authorization(record_id="auth-0001"))
    first.analyze_page(raster, rendition_sha256="r", page_number=1)

    second = opened(tmp_path, FailingService(), authorization(record_id="auth-0002", signed_on="2026-10-01"))
    reading = second.analyze_page(raster, rendition_sha256="r", page_number=1)

    assert second.scope.digest == first.scope.digest
    assert reading.binding.cached is True and reading.binding.record_id == "auth-0002"
    assert reading.binding.request_identity["record_id"] == "auth-0001", "the response names the record it was obtained under"


def test_retries_are_counted_apart_from_calls_and_hits(tmp_path):
    service = RecordedService(fixture("synthetic-grid-merged")["response"], failures=["ThrottlingException", "ProvisionedThroughputExceededException"])
    naps: list[float] = []
    adapter = open_boundary(authorization(), request(), extraction_run="run-1", cache_root=tmp_path / "cache", service=service, configuration=CONFIGURATION, sleep=naps.append, posture=ACCEPTED_POSTURE)
    raster = raster_of(tmp_path)

    reading = adapter.analyze_page(raster, rendition_sha256="r", page_number=1)

    assert len(service.calls) == 3 and len(naps) == 2
    counts = adapter.receipt.as_dict()["counts"]
    assert counts == {"pages_requested": 1, "hits": 0, "calls": 1, "retries": 2, "failed_attempts": 0, "outbound_requests": 3, "failures": 0}
    assert adapter.receipt.as_dict()["charged_pages"] == 1
    assert reading.binding.request_identity["attempts"] == 3


def test_a_provider_refusal_is_a_processing_failure_with_its_requests_counted(tmp_path):
    service = RecordedService(fixture("synthetic-grid-merged")["response"], failures=["InvalidParameterException"])
    adapter = opened(tmp_path, service)
    raster = raster_of(tmp_path)

    with pytest.raises(TextractProcessingFailure) as caught:
        adapter.analyze_page(raster, rendition_sha256="r", page_number=1)

    assert caught.value.reason == "provider-call-failed" and caught.value.outbound_requests == 1
    assert "InvalidParameterException" in caught.value.detail
    receipt = adapter.receipt.as_dict()
    assert receipt["counts"] == {"pages_requested": 1, "hits": 0, "calls": 0, "retries": 0, "failed_attempts": 1, "outbound_requests": 1, "failures": 1}
    assert receipt["failures"][0]["reason"] == "provider-call-failed" and receipt["failures"][0]["outbound_requests"] == 1
    assert receipt["charged_pages"] == 0 and receipt["bindings"] == []
    assert not adapter.identity_path(raster.sha256).exists()


def test_an_exhausted_page_budget_is_a_processing_failure_with_zero_further_requests(tmp_path):
    service = RecordedService(fixture("synthetic-grid-merged")["response"])
    adapter = opened(tmp_path, service, max_pages=1)
    first = raster_of(tmp_path, "one.pdf")
    second = raster_of(tmp_path, "two.pdf", width=500.0)
    adapter.analyze_page(first, rendition_sha256="a", page_number=1)

    with pytest.raises(TextractProcessingFailure) as caught:
        adapter.analyze_page(second, rendition_sha256="b", page_number=1)

    assert caught.value.reason == "resource-budget-exhausted" and caught.value.outbound_requests == 0
    assert len(service.calls) == 1
    assert adapter.analyze_page(first, rendition_sha256="a", page_number=1).binding.cached, "the budget stops calls, not reads"
    counts = adapter.receipt.as_dict()["counts"]
    assert (counts["calls"], counts["hits"], counts["failures"], counts["outbound_requests"]) == (1, 1, 1, 1)


def test_a_retained_response_replays_through_the_boundary_without_a_request(tmp_path):
    raster = raster_of(tmp_path)
    retained = fixture("status-mobility-fy2018-page2-clean")
    entry = {
        "sha256": raster.sha256,
        "bytes": len(raster.png),
        "feature_types": ["TABLES"],
        "requested_at": "2026-09-05T20:00:00+00:00",
        "transport": "connector:s3:corridor-textract-rung-9593",
        "attempts": 1,
        "response": retained["response"],
    }
    adapter = opened(tmp_path, FailingService())
    adapter.retain(entry)

    reading = adapter.analyze_page(raster, rendition_sha256="r", page_number=2)

    assert reading.binding.cached is True
    assert reading.binding.request_identity["retained"] is True
    assert reading.binding.request_identity["transport"] == "connector:s3:corridor-textract-rung-9593"
    assert reading.binding.raw_response_digest == raw_response_digest(retained["response"])
    expected = normalize(retained["response"], number=2, size=(612.0, 792.0), rotation=0)
    assert reading.page == expected and reading.binding.normalized_reading_digest == reading_digest(expected)
    assert len(reading.page["tables"]) == 2
    counts = adapter.receipt.as_dict()["counts"]
    assert (counts["hits"], counts["calls"], counts["outbound_requests"]) == (1, 0, 0)


def test_an_entry_that_is_not_a_response_is_refused_at_retention(tmp_path):
    adapter = opened(tmp_path, FailingService())

    with pytest.raises(TextractProcessingFailure) as caught:
        adapter.retain({"sha256": "abc", "response": {}})

    assert caught.value.reason == "retained-entry-invalid" and caught.value.outbound_requests == 0


# --- the page shape and the native-glyph re-map ----------------------------------


def test_native_glyphs_fill_textract_geometry_and_textract_words_are_never_stored(tmp_path):
    pdf = minimal_pdf(tmp_path / "total.pdf", text="Total")
    configuration = RequestConfiguration(dpi=72)
    raster = rasterize_page(pdf, 1, configuration)
    service = RecordedService(response_around_total())
    adapter = opened(tmp_path, service, configuration=configuration)
    glyphs = native_glyphs(pdf, 1)

    words = adapter.analyze_page(raster, rendition_sha256="scan", page_number=1)
    remapped = adapter.analyze_page(raster, rendition_sha256="print", page_number=1, native_glyphs=glyphs)

    assert words.page["tables"][0]["cells"][0]["text"] == "TOTAL"
    assert remapped.page["tables"][0]["cells"][0]["text"] == "Total"
    assert remapped.page["tables"][0]["cells"][1]["text"] == ""
    assert remapped.page["text_source"] == "pdfium-glyphs"
    assert all("word_ids" not in cell for table in remapped.page["tables"] for cell in table["cells"])
    assert remapped.binding.normalization == {"text_source": "pdfium-glyphs", "parser": METHOD, "remap_margin": 0.0, "rescue_runs": False}
    assert words.binding.normalization["text_source"] == "textract-words"
    assert remapped.binding.raw_response_digest == words.binding.raw_response_digest
    assert remapped.binding.normalized_reading_digest != words.binding.normalized_reading_digest
    assert len(service.calls) == 1, "one response, two readings, one charge"


def test_run_mate_rescue_and_the_polygon_margin_are_off_by_default():
    assert NormalizationConfiguration() == NormalizationConfiguration(remap_margin=0.0, rescue_runs=False)
    parameters = inspect.signature(imported_remap.remap_page).parameters
    assert parameters["rescue_runs"].default is False and parameters["margin"].default == 0.0
    page = normalize(fixture("synthetic-grid-merged")["response"], number=1, size=(612.0, 792.0), rotation=0)
    assert "remap_rescue_runs" not in page and "remap_margin" not in page


def test_blocks_outside_every_table_are_kept_as_evidence():
    page = normalize(fixture("synthetic-grid-merged")["response"], number=1, size=(612.0, 792.0), rotation=0)

    assert [item["text"] for item in page["outside"]] == ["Schedule of Values", "Page 1"], "LINE and WORD blocks no cell owns are strings outside the tables, not discarded"
    assert all({"text", "box", "confidence", "block_id"} <= set(item) for item in page["outside"])
    cells = page["tables"][0]["cells"]
    assert all({"row", "column", "row_span", "column_span", "box", "confidence", "block_ids", "word_ids"} <= set(cell) for cell in cells)
    assert {(cell["row"], cell["column"], cell["column_span"]) for cell in cells} >= {(0, 0, 1), (0, 1, 2), (1, 2, 1)}


# --- the posture and the guards --------------------------------------------------


def test_the_adapter_binds_to_the_posture_document_by_digest():
    assert hashlib.sha256(POSTURE_DOCUMENT.read_bytes()).hexdigest() == PROVIDER_POSTURE.digest
    front_matter = yaml.safe_load(POSTURE_DOCUMENT.read_text(encoding="utf-8").split("---\n")[1])

    assert front_matter["identity"] == PROVIDER_POSTURE.identity
    assert front_matter["status"] == PROVIDER_POSTURE.status
    assert front_matter["provider"] == PROVIDER_POSTURE.provider
    assert front_matter["operation"] == PROVIDER_POSTURE.operation
    assert front_matter["feature_types"] == list(PROVIDER_POSTURE.feature_types)
    assert front_matter["region"] == PROVIDER_POSTURE.region
    assert front_matter["permitted_purposes"] == list(PROVIDER_POSTURE.permitted_purposes)
    assert front_matter["retention"] == PROVIDER_POSTURE.retention
    assert front_matter["ai_services_opt_out"] == PROVIDER_POSTURE.ai_services_opt_out
    assert front_matter["permissions"] == PROVIDER_POSTURE.permissions
    assert front_matter["bound_by"] == "src/corridor_pdf_reader/textract_adapter/records.py"


def test_the_posture_is_a_separate_record_from_any_customer_authorization():
    record = authorization()

    assert record.kind == "customer-authorization" and experiment().kind == "experiment-scope"
    assert record.as_dict()["posture_identity"] == PROVIDER_POSTURE.identity
    assert "retention" not in record.as_dict() and "customer" not in PROVIDER_POSTURE.as_dict()


NETWORK_MODULES = ("client", "transport", "read", "semantics")
IMPORTED_ROOT = PACKAGE_ROOT / "textract"
BOUNDARY = PACKAGE_ROOT / "textract_adapter" / "boundary.py"
SCAN_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "workers", REPO_ROOT / "scripts", REPO_ROOT / "tests")
PRODUCTION_ROOTS = (REPO_ROOT / "src" / "corridor", REPO_ROOT / "workers" / "render", REPO_ROOT / "scripts")


def _python_files(root: Path):
    """Every module under the root but hidden directories (a worker's own .venv) and this guard."""
    for path in sorted(root.rglob("*.py")):
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path == Path(__file__).resolve():
            continue
        yield path


def _network_module_imports(path: Path) -> list[str]:
    """Import statements that reach the imported client, transport, harness driver or semantics runner."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "corridor_pdf_reader.textract" or any(alias.name.startswith(f"corridor_pdf_reader.textract.{name}") for name in NETWORK_MODULES):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "corridor_pdf_reader" and any(alias.name == "textract" for alias in node.names):
                found.append("corridor_pdf_reader.textract")
            if node.module == "corridor_pdf_reader.textract":
                found.extend(f"{node.module}.{alias.name}" for alias in node.names if alias.name in NETWORK_MODULES)
            for name in NETWORK_MODULES:
                if node.module == f"corridor_pdf_reader.textract.{name}" or node.module.startswith(f"corridor_pdf_reader.textract.{name}."):
                    found.append(node.module)
    source = path.read_text(encoding="utf-8")
    if "import_module(" in source and "corridor_pdf_reader.textract" in source:
        found.append("dynamic import")
    return found


def test_only_the_boundary_imports_the_imported_client_or_transport():
    """Diagnostics, retries, shadow runs or a future caller cannot reach a call without the check."""
    offenders = {}
    for root in SCAN_ROOTS:
        for path in _python_files(root):
            if path == BOUNDARY or IMPORTED_ROOT in path.parents:
                continue
            found = _network_module_imports(path)
            if found:
                offenders[str(path.relative_to(REPO_ROOT))] = found

    assert offenders == {}
    assert _network_module_imports(BOUNDARY) == ["corridor_pdf_reader.textract.client"]


def _textract_client_constructions(path: Path) -> list[int]:
    """Lines that build a boto3 client for the service: `<session or boto3>.client("textract", ...)`."""
    lines = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "client":
            continue
        names = [arg.value for arg in node.args[:1] if isinstance(arg, ast.Constant)]
        names += [kw.value.value for kw in node.keywords if kw.arg == "service_name" and isinstance(kw.value, ast.Constant)]
        if "textract" in names:
            lines.append(node.lineno)
    return lines


def test_the_boto3_service_name_appears_only_where_the_boundary_constructs_the_live_client():
    """A caller cannot build its own Textract client either: the service name is the boundary's."""
    offenders = {}
    for root in (REPO_ROOT / "src", REPO_ROOT / "workers", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
        for path in _python_files(root):
            if path == BOUNDARY or IMPORTED_ROOT in path.parents:
                continue
            found = _textract_client_constructions(path)
            if found:
                offenders[str(path.relative_to(REPO_ROOT))] = found

    assert offenders == {}
    assert len(_textract_client_constructions(BOUNDARY)) == 1
    assert _textract_client_constructions(IMPORTED_ROOT / "client.py") == [150], "the imported client's own construction, unreachable but through the boundary"


# The rung and the adapter, the two module trees no production path may reach.
# The wider rule -- which production modules may import the reader package at
# all -- is `tests/test_pdf_reader_package.py`, which carries the reasoned
# exceptions (#740 entered PDFium for glyph geometry from three modules). This
# rule is about the provider: nothing in production may reach a Textract call.
TEXTRACT_TREES = (
    "corridor_pdf_reader.textract",
    "corridor_pdf_reader.textract_adapter",
)


def _reaches_textract(name: str) -> bool:
    return any(name == tree or name.startswith(f"{tree}.") for tree in TEXTRACT_TREES)


def test_no_production_module_imports_the_textract_rung_or_the_adapter():
    offenders = []
    for root in PRODUCTION_ROOTS:
        for path in _python_files(root):
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source, filename=str(path))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(_reaches_textract(name) for name in names):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
                    break
            if "import_module(" in source and any(tree in source for tree in TEXTRACT_TREES):
                offenders.append(f"{path.relative_to(REPO_ROOT)} (dynamic import)")

    assert offenders == []
