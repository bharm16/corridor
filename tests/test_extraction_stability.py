"""Regression loop for ADR-0046 extraction repeatability failures."""

from datetime import date

from corridor.extract_minutes_v5 import PROMPT_VERSION, to_candidate
from corridor.models import DocPage, Document
from corridor.product_proving_run import ExtractionConfiguration, compare_candidate_sets


def _raw_candidate(candidate):
    return {
        "candidate_id": candidate.id,
        "project_id": candidate.project_id,
        "kind": candidate.kind,
        "source_document_id": candidate.source_document_id,
        "payload_json": candidate.payload_json,
        "source_pages": candidate.source_pages,
        "prompt_version": candidate.prompt_version,
        "model": candidate.model,
        "citations_verified": candidate.citations_verified,
        "state": candidate.state,
    }


def _matrix_candidate(prompt_version: str):
    return {
        "candidate_id": 1,
        "project_id": 1266,
        "kind": "dependency",
        "source_document_id": 1311,
        "payload_json": {
            "kind": "dependency",
            "fields": {
                "utility_id": "C1",
                "external_org": "ATT",
                "utility_type": "Communications",
                "station_from": "6029+38",
                "station_to": "6041+03",
            },
            "citations": [
                {
                    "document_id": 1311,
                    "page": 1,
                    "quote": (
                        "C1 ATT Communications SH99 6029+38 53 RT "
                        "6041+03 57 RT Underground Longitudinal Inside"
                    ),
                    "verified": True,
                    "whole_row": True,
                }
            ],
        },
        "source_pages": [1],
        "prompt_version": prompt_version,
        "model": "gpt-5.6-luna",
        "citations_verified": True,
        "state": "pending",
    }


def _configuration(prompt_version: str, prompt_sha256: str):
    return ExtractionConfiguration(
        prompt_version=prompt_version,
        model="gpt-5.6-luna",
        schema_version="matrix_candidate_shape_v1",
        prompt_sha256=prompt_sha256,
        schema_sha256="6" * 64,
        postprocessor_sha256="7" * 64,
        config_sha256="8" * 64,
    )


def test_proving_comparison_refuses_incompatible_extractor_lineage():
    baseline = _matrix_candidate("matrix_tiered_v2")
    fresh = _matrix_candidate("matrix_tiered_v3")

    try:
        compare_candidate_sets(
            document_id=1311,
            baseline_run_id=117,
            fresh_run_id=206506,
            baseline=[baseline],
            fresh=[fresh],
            baseline_configuration=_configuration("matrix_tiered_v2", "2" * 64),
            fresh_configuration=_configuration("matrix_tiered_v3", "3" * 64),
        )
    except ValueError as exc:
        assert "configuration-compatible" in str(exc)
    else:
        raise AssertionError(
            "incompatible extractor versions reached semantic Candidate comparison"
        )


def test_zero_candidate_runs_still_require_compatible_run_configuration():
    try:
        compare_candidate_sets(
            document_id=1435,
            baseline_run_id=1,
            fresh_run_id=2,
            baseline=[],
            fresh=[],
            baseline_configuration=ExtractionConfiguration(
                prompt_version="minutes_v4",
                model="gpt-5.6-luna",
                schema_version="minutes_v4",
                prompt_sha256="4" * 64,
                schema_sha256="6" * 64,
                postprocessor_sha256="7" * 64,
                config_sha256="8" * 64,
            ),
            fresh_configuration=ExtractionConfiguration(
                prompt_version="minutes_v4",
                model="gpt-5.6-luna",
                schema_version="minutes_v4",
                prompt_sha256="5" * 64,
                schema_sha256="6" * 64,
                postprocessor_sha256="7" * 64,
                config_sha256="8" * 64,
            ),
        )
    except ValueError as exc:
        assert "configuration-compatible" in str(exc)
    else:
        raise AssertionError("empty Candidate sets bypassed run configuration")


def test_same_minutes_evidence_produces_stable_candidate_meaning():
    quote = (
        "Equistar to research if they have as-built documentation at siphon "
        "ditch. Complete, \nStacy emailed the as built for both pipeline crossings "
        "on 2/12."
    )
    document = Document(
        id=1438,
        project_id=1266,
        sha256="d" * 64,
        filename="Meeting Notes/Equistar/2025.02.12 GPB1 Equistar notes final.pdf",
        doc_type="minutes",
        doc_date=date(2025, 2, 12),
        parse_status="parsed",
    )
    page = DocPage(
        document_id=1438,
        page_no=2,
        text=quote,
        text_source="text_layer",
    )
    baseline_item = {
        "event_type": "response",
        "description": (
            "Equistar's Stacy emailed the as-built documentation, completing "
            "the earlier research action."
        ),
        "external_org": "Equistar",
        "stated_party": None,
        "conflict_ref": "PL20",
        "committed_date": None,
        "previous_timing": None,
        "quote": quote,
        "confidence": 0.99,
    }
    fresh_item = {
        **baseline_item,
        "event_type": "closure",
        "description": "The action item is complete.",
    }
    baseline = to_candidate(document, page, baseline_item, "gpt-5.6-luna")
    fresh = to_candidate(document, page, fresh_item, "gpt-5.6-luna")

    comparison = compare_candidate_sets(
        document_id=1438,
        baseline_run_id=193811,
        fresh_run_id=206507,
        baseline=[_raw_candidate(baseline)],
        fresh=[_raw_candidate(fresh)],
        baseline_configuration=ExtractionConfiguration(
            prompt_version=PROMPT_VERSION,
            model="gpt-5.6-luna",
            schema_version=PROMPT_VERSION,
            prompt_sha256="4" * 64,
            schema_sha256="5" * 64,
            postprocessor_sha256="6" * 64,
            config_sha256="7" * 64,
        ),
        fresh_configuration=ExtractionConfiguration(
            prompt_version=PROMPT_VERSION,
            model="gpt-5.6-luna",
            schema_version=PROMPT_VERSION,
            prompt_sha256="4" * 64,
            schema_sha256="5" * 64,
            postprocessor_sha256="6" * 64,
            config_sha256="7" * 64,
        ),
    )

    assert comparison.equal
    assert baseline.prompt_version == fresh.prompt_version == PROMPT_VERSION
