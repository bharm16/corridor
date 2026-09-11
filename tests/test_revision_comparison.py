"""Revision Comparison receipts and global correspondence contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from collections import Counter
from datetime import date, datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

import corridor.revision_comparison as revision_comparison
from corridor.db import Session
from corridor.extraction_runs import record_extraction_run
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExtractionRun,
    Project,
    RevisionComparisonFinding,
    RevisionComparisonRun,
)
from corridor.revision_comparison import (
    AmbiguousRevisionComparison,
    CorruptRevisionComparison,
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
    IncompleteSuccessorExtraction,
    InexactExtractionInputs,
    InvalidRevisionPair,
    MissingExtractionRun,
    RevisionComparisonError,
    UnsupportedComparisonShape,
    _ScoredEdge,
    _assignment_regret_pairs,
    _bounded_ambiguities,
    _global_matching,
    create_revision_comparison,
    list_revision_comparisons,
    read_revision_comparison,
)
from corridor.supersession import SupersessionDeclaration, register_supersessions
from committed_scenario_support import delete_committed_project


@pytest.fixture
def consecutive_nhhip_documents(session):
    project = Project(
        slug="revision-comparison-nhhip",
        name="NHHIP Segment 3C-2",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()

    source = _document(
        session,
        project,
        registry_id="nhhip-rid-index-2026-05-01",
        filename="nhhip-rid-index.pdf",
        doc_type="other",
    )
    session.add(DocPage(document_id=source.id, page_no=4, text="RID index"))
    predecessor = _document(
        session,
        project,
        registry_id="nhhip-ucm-2025-06-20",
        filename="nhhip-utilities-inventory-2025-06-20.pdf",
    )
    successor = _document(
        session,
        project,
        registry_id="nhhip-ucm-2025-07-22",
        filename="nhhip-utility-conflict-matrix-2025-07-22.pdf",
    )
    session.flush()
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=successor.registry_id,
                replacement_date=date(2025, 7, 22),
                source_registry_id=source.registry_id,
                source_page=4,
            )
        ],
        project_id=project.id,
    )
    return project, predecessor, successor


def _document(
    session,
    project,
    *,
    registry_id: str,
    filename: str,
    doc_type: str = "matrix",
):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=hashlib.sha256(registry_id.encode()).hexdigest(),
        filename=filename,
        doc_type=doc_type,
        parse_status="parsed",
        pages=4,
    )
    session.add(document)
    session.flush()
    return document


def _run(
    session,
    document,
    rows,
    *,
    prompt_version,
    model,
    schema_version,
    kind="dependency",
):
    candidates = []
    for fields in rows:
        candidate = Candidate(
            project_id=document.project_id,
            kind=kind,
            payload_json={
                "kind": kind,
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": " | ".join(str(value) for value in fields.values()),
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "unverified_fields": [],
                "unmapped_columns": [],
                "low_confidence_tokens": [],
                "tier": "structure",
                "dedupe_hint": "|".join(str(value) for value in fields.values()),
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version=prompt_version,
            model=model,
            citations_verified=True,
        )
        session.add(candidate)
        candidates.append(candidate)
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model=model,
        schema_version=schema_version,
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run, candidates


def _clone_comparison_receipt(session, comparison):
    readback = read_revision_comparison(session, comparison.id)
    duplicate = RevisionComparisonRun(
        project_id=comparison.project_id,
        predecessor_document_id=comparison.predecessor_document_id,
        successor_document_id=comparison.successor_document_id,
        predecessor_extraction_run_id=comparison.predecessor_extraction_run_id,
        successor_extraction_run_id=comparison.successor_extraction_run_id,
        predecessor_schema_version=comparison.predecessor_schema_version,
        successor_schema_version=comparison.successor_schema_version,
        predecessor_prompt_version=comparison.predecessor_prompt_version,
        successor_prompt_version=comparison.successor_prompt_version,
        predecessor_model=comparison.predecessor_model,
        successor_model=comparison.successor_model,
        matcher_version=comparison.matcher_version,
        matcher_config=dict(comparison.matcher_config),
        predecessor_inputs_json=list(comparison.predecessor_inputs_json),
        successor_inputs_json=list(comparison.successor_inputs_json),
        finding_count=comparison.finding_count,
        content_sha256=comparison.content_sha256,
    )
    session.add(duplicate)
    session.flush([duplicate])
    for finding in readback.findings:
        session.add(
            RevisionComparisonFinding(
                revision_comparison_run_id=duplicate.id,
                ordinal=finding.ordinal,
                state=finding.state,
                predecessor_candidate_ids=list(
                    finding.predecessor_candidate_ids
                ),
                successor_candidate_ids=list(finding.successor_candidate_ids),
                match_score=finding.match_score,
                field_changes=list(finding.field_changes),
                matcher_detail=dict(finding.matcher_detail),
            )
        )
    session.flush()
    duplicate.sealed_at = datetime.now(timezone.utc)
    session.flush([duplicate])
    return duplicate


def _insert_unsealed_comparison_fixture(
    session,
    project,
    predecessor,
    successor,
    predecessor_run,
    successor_run,
    *,
    predecessor_inputs=None,
    successor_inputs=None,
):
    comparison = RevisionComparisonRun(
        project_id=project.id,
        predecessor_document_id=predecessor.id,
        successor_document_id=successor.id,
        predecessor_extraction_run_id=predecessor_run.id,
        successor_extraction_run_id=successor_run.id,
        predecessor_schema_version=predecessor_run.schema_version,
        successor_schema_version=successor_run.schema_version,
        predecessor_prompt_version=predecessor_run.prompt_version,
        successor_prompt_version=successor_run.prompt_version,
        predecessor_model=predecessor_run.model,
        successor_model=successor_run.model,
        matcher_version=DEFAULT_MATCHER_VERSION,
        matcher_config=dict(DEFAULT_MATCHER_CONFIG),
        predecessor_inputs_json=(
            [] if predecessor_inputs is None else predecessor_inputs
        ),
        successor_inputs_json=(
            [] if successor_inputs is None else successor_inputs
        ),
        finding_count=0,
        content_sha256="0" * 64,
    )
    session.add(comparison)
    session.flush([comparison])
    return comparison


def _committed_comparison_pair():
    with Session() as setup:
        project = Project(
            slug=f"revision-comparison-race-{uuid4().hex}",
            name="Revision Comparison Race",
            is_synthetic=True,
        )
        setup.add(project)
        setup.flush([project])
        source = _document(
            setup,
            project,
            registry_id="race-index",
            filename="race-index.pdf",
            doc_type="other",
        )
        setup.add(DocPage(document_id=source.id, page_no=1, text="revision index"))
        predecessor = _document(
            setup,
            project,
            registry_id="race-predecessor",
            filename="race-predecessor.pdf",
        )
        successor = _document(
            setup,
            project,
            registry_id="race-successor",
            filename="race-successor.pdf",
        )
        setup.flush()
        register_supersessions(
            setup,
            [
                SupersessionDeclaration(
                    predecessor_registry_id=predecessor.registry_id,
                    successor_registry_id=successor.registry_id,
                    replacement_date=date(2026, 8, 28),
                    source_registry_id=source.registry_id,
                    source_page=1,
                )
            ],
            project_id=project.id,
        )
        predecessor_run, _ = _run(
            setup,
            predecessor,
            [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
            prompt_version="matrix_tiered_v2",
            model="gpt-test",
            schema_version="matrix-schema-v2",
        )
        successor_run, _ = _run(
            setup,
            successor,
            [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
            prompt_version="matrix_tiered_v3",
            model="gpt-test",
            schema_version="matrix-schema-v3",
        )
        ids = (project.id, predecessor_run.id, successor_run.id)
        setup.commit()
        return ids


def _delete_committed_comparison_project(project_id):
    delete_committed_project(project_id, session_factory=Session)


@pytest.mark.parametrize(
    ("doc_type", "kind", "fields"),
    [
        (
            "minutes",
            "event",
            {
                "conflict_ref": "PL41",
                "event_type": "status_change",
                "description": "The relocation status did not change.",
            },
        ),
        (
            "agreement",
            "dependency",
            {
                "title": "Relocate facilities",
                "external_org": "City of Houston",
                "obligation": "Relocate water and sewer facilities.",
            },
        ),
    ],
)
def test_unsupported_candidate_shape_fails_closed_without_a_partial_receipt(
    session, consecutive_nhhip_documents, doc_type, kind, fields
):
    """The effective matcher refuses shapes without a correspondence contract."""

    project, predecessor, successor = consecutive_nhhip_documents
    predecessor.doc_type = doc_type
    successor.doc_type = doc_type
    predecessor_run, _ = _run(
        session,
        predecessor,
        [fields],
        prompt_version=f"{doc_type}_v1",
        model="gpt-test",
        schema_version=f"{doc_type}-schema-v1",
        kind=kind,
    )
    successor_run, _ = _run(
        session,
        successor,
        [fields],
        prompt_version=f"{doc_type}_v2",
        model="gpt-test",
        schema_version=f"{doc_type}-schema-v2",
        kind=kind,
    )

    with pytest.raises(
        UnsupportedComparisonShape,
        match=(
            "matcher custom-correspondence-v9 supports only "
            "utility-matrix dependency rows"
        ),
    ):
        create_revision_comparison(
            session,
            predecessor_run.id,
            successor_run.id,
            matcher_version="  custom-correspondence-v9  ",
        )

    assert session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.project_id == project.id
        )
    ).all() == []


def test_completed_zero_row_runs_remain_comparable_without_a_row_shape(
    session, consecutive_nhhip_documents
):
    """An empty successful extraction contains no unsupported observations."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor.doc_type = "agreement"
    successor.doc_type = "agreement"
    predecessor_run, _ = _run(
        session,
        predecessor,
        [],
        prompt_version="agreement_v1",
        model="gpt-test",
        schema_version="agreement-schema-v1",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="agreement_v2",
        model="gpt-test",
        schema_version="agreement-schema-v2",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert read_revision_comparison(session, comparison.id).findings == ()


def test_consecutive_nhhip_runs_persist_an_exact_reviewer_readable_receipt(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, predecessor_candidates = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC1-1",
                "external_org": "AT&T",
                    "utility_type": "Telecom",
                    "station_from": "100+00",
                    "baseline": "IH 69",
                    "potential_conflict": "N",
            },
            {
                "utility_id": "FOC1-2",
                "external_org": "CenterPoint",
                    "utility_type": "Gas",
                    "station_from": "200+00",
                    "baseline": "IH 69",
                    "potential_conflict": "N",
            },
            {
                "utility_id": "FOC1-3",
                "external_org": "Comcast",
                    "utility_type": "Telecom",
                    "station_from": "300+00",
                    "baseline": "IH 69",
                    "potential_conflict": "Y",
            },
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test-predecessor",
        schema_version="matrix-schema-v2",
    )
    successor_run, successor_candidates = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC1-1",
                "external_org": "AT&T",
                    "utility_type": "Telecom",
                    "station_from": "100+00",
                    "baseline": "IH69",
                    "potential_conflict": "N",
            },
            {
                "utility_id": "FOC1-2",
                "external_org": "CenterPoint",
                    "utility_type": "Gas",
                    "station_from": "200+00",
                    "baseline": "IH69",
                    "potential_conflict": "Y",
            },
            {
                "utility_id": "FOC1-4",
                "external_org": "Kinder Morgan",
                    "utility_type": "Pipeline",
                    "station_from": "400+00",
                    "baseline": "IH69",
                    "potential_conflict": "Y",
            },
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test-successor",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
    )
    session.flush()
    readback = read_revision_comparison(session, comparison.id)

    assert isinstance(comparison, RevisionComparisonRun)
    assert comparison.predecessor_document_id == predecessor.id
    assert comparison.successor_document_id == successor.id
    assert comparison.predecessor_extraction_run_id == predecessor_run.id
    assert comparison.successor_extraction_run_id == successor_run.id
    assert comparison.predecessor_schema_version == "matrix-schema-v2"
    assert comparison.successor_schema_version == "matrix-schema-v3"
    assert comparison.predecessor_prompt_version == "matrix_tiered_v2"
    assert comparison.successor_prompt_version == "matrix_tiered_v3"
    assert comparison.predecessor_model == "gpt-test-predecessor"
    assert comparison.successor_model == "gpt-test-successor"
    assert comparison.matcher_version == DEFAULT_MATCHER_VERSION
    assert comparison.generated_at is not None

    assert all(
        isinstance(finding, RevisionComparisonFinding)
        for finding in readback.findings
    )
    assert Counter(finding.state for finding in readback.findings) == {
        "added": 1,
        "dropped": 1,
        "unchanged": 1,
        "changed": 1,
    }
    changed = next(
        finding for finding in readback.findings if finding.state == "changed"
    )
    assert changed.field_changes == [
        {"field": "potential_conflict", "before": "N", "after": "Y"}
    ]
    assert {
        item["candidate_id"] for item in readback.predecessor_inputs
    } == {candidate.id for candidate in predecessor_candidates}
    assert {
        item["candidate_id"] for item in readback.successor_inputs
    } == {candidate.id for candidate in successor_candidates}


def test_unique_maximum_cardinality_assignment_is_not_locally_ambiguous(
    session, consecutive_nhhip_documents
):
    """A near edge is certain when forcing it would reduce cardinality."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, predecessor_candidates = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC14-69",
                "external_org": "Comcast",
                "utility_type": "Telecom",
            },
            {
                "utility_id": "FOC14-OTHER",
                "external_org": "Comcast",
                "utility_type": "Telecom",
                "station_from": "200+00",
            },
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, successor_candidates = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC14-69",
                "external_org": "Comcast",
                "utility_type": "Telecom",
            },
            {
                "utility_id": "FOC14-69",
                "external_org": "Comcast",
                "utility_type": "Telecom",
                "station_from": "200+00",
            },
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_config={"minimum_score": 0.70},
    )
    readback = read_revision_comparison(session, comparison.id)

    assert len(readback.findings) == 2
    assert {
        (
            tuple(finding.predecessor_candidate_ids),
            tuple(finding.successor_candidate_ids),
        )
        for finding in readback.findings
    } == {
        ((predecessor_candidates[0].id,), (successor_candidates[0].id,)),
        ((predecessor_candidates[1].id,), (successor_candidates[1].id,)),
    }
    assert {finding.state for finding in readback.findings}.isdisjoint(
        {"ambiguous", "added", "dropped", "split", "combined"}
    )


def test_changed_ids_without_complete_coordinates_remain_unmatched(
    session, consecutive_nhhip_documents
):
    """Cohort similarity cannot justify either matching or disappearance."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC1-1",
                "external_org": "AT&T",
                "utility_type": "Telecom",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, [successor_candidate] = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC9-999",
                "external_org": "AT&T",
                "utility_type": "Telecom",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    findings = read_revision_comparison(session, comparison.id).findings

    assert [finding.state for finding in findings] == ["unmatched", "unmatched"]
    assert {
        (
            tuple(finding.predecessor_candidate_ids),
            tuple(finding.successor_candidate_ids),
        )
        for finding in findings
    } == {
        ((predecessor_candidate.id,), ()),
        ((), (successor_candidate.id,)),
    }


def test_plausible_but_not_strong_station_identity_is_ambiguous(
    session, consecutive_nhhip_documents
):
    """A possible renumbering inside tolerance is never added plus dropped."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, [successor_candidate] = _run(
        session,
        successor,
        [
            {
                "utility_id": "B",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "101+50",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "ambiguous"
    assert finding.predecessor_candidate_ids == [predecessor_candidate.id]
    assert finding.successor_candidate_ids == [successor_candidate.id]
    [alternative] = finding.matcher_detail["alternatives"]
    assert alternative["weak_identity"] is True
    assert alternative["chosen"] is False


def test_exact_id_only_rows_can_succeed_when_every_row_corresponds(
    session, consecutive_nhhip_documents
):
    """Incomplete coordinates are safe when no disappearance is inferred."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "unchanged"


def test_duplicate_source_id_cannot_override_a_contradictory_owner(
    session, consecutive_nhhip_documents
):
    """NHHIP repeats IDs, so an ID is evidence and never unique identity."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "47",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "47",
                "external_org": "Comcast",
                "utility_type": "Telecom",
                "station_from": "200+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_owner_change_with_exact_ref_and_station_is_a_changed_row(
    session, consecutive_nhhip_documents
):
    """Physical identity preserves review-visible owner annotation changes."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC2-1",
                "external_org": "AT&T LNS (Metro, TCA)",
                "utility_type": "Telecom",
                "station_from": "1149+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC2-1",
                "external_org": "AT&T LNS (TCA)",
                "utility_type": "Telecom",
                "station_from": "1149+00",
                "baseline": "IH69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.field_changes == [
        {
            "field": "external_org",
            "before": "AT&T LNS (Metro, TCA)",
            "after": "AT&T LNS (TCA)",
        }
    ]


def test_owner_change_with_only_weak_station_similarity_stays_ambiguous(
    session, consecutive_nhhip_documents
):
    """A renamed owner needs strong physical identity before assignment."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, predecessor_candidates = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC2-1",
                "external_org": "AT&T LNS (Metro, TCA)",
                "utility_type": "Telecom",
                "station_from": "1149+00",
                "baseline": "IH69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, successor_candidates = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC2-1",
                "external_org": "AT&T LNS (TCA)",
                "utility_type": "Telecom",
                "station_from": "1150+50",
                "baseline": "IH69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "ambiguous"
    assert tuple(finding.predecessor_candidate_ids) == (
        predecessor_candidates[0].id,
    )
    assert tuple(finding.successor_candidate_ids) == (
        successor_candidates[0].id,
    )
    assert {item["score"] for item in finding.matcher_detail["alternatives"]} == {
        0.64444444
    }


def test_owner_change_with_only_weak_station_identity_is_ambiguous(
    session, consecutive_nhhip_documents
):
    """A repeated id plus a possible coordinate match cannot prove an owner edit."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "47",
                "external_org": "AT&T",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "47",
                "external_org": "Comcast",
                "station_from": "101+50",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "ambiguous"
    [alternative] = finding.matcher_detail["alternatives"]
    assert alternative["weak_identity"] is True


def test_placeholder_owner_does_not_contradict_a_named_successor_owner(
    session, consecutive_nhhip_documents
):
    """Unknown is missing party identity, not a party named Unknown."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "T41",
                "external_org": "Unknown",
                "utility_type": "Telecom",
                "station_from": "1130+58",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "T41",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "1130+58",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.field_changes == [
        {"field": "external_org", "before": "Unknown", "after": "AT&T"}
    ]


def test_placeholder_source_ids_do_not_create_row_identity(
    session, consecutive_nhhip_documents
):
    """No ID on two rows is absent identity, not an exact identifier."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "No ID",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "No ID",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "200+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_placeholder_locations_do_not_create_row_identity(
    session, consecutive_nhhip_documents
):
    """An all-placeholder location tuple contains no physical identity."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A-17",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "location_start": "N/A",
                "alignment": "Unknown",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "A-18",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "location_start": "N/A",
                "alignment": "Unknown",
                "station_from": "200+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_fuzzy_location_identity_is_not_lost_before_scoring(
    session, consecutive_nhhip_documents
):
    """A scorer-valid renamed location cannot become dropped plus added."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A-17",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "location_start": "Main Street at First Avenue",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "A-18",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "location_start": "Main St at First Avenue",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.match_score is not None
    assert finding.match_score >= DEFAULT_MATCHER_CONFIG["minimum_score"]


def test_station_containment_in_a_long_span_is_not_lost_before_scoring(
    session, consecutive_nhhip_documents
):
    """A point inside a long predecessor span remains a valid edge."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A-17",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "0+00",
                "station_to": "1000+00",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "A-18",
                "external_org": "AT&T",
                "utility_type": "Telecom",
                "station_from": "500+00",
                "station_to": "501+00",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.match_score is not None
    assert finding.match_score >= DEFAULT_MATCHER_CONFIG["minimum_score"]


def test_equal_station_numbers_on_different_baselines_are_not_identity(
    session, consecutive_nhhip_documents
):
    """Stationing is a coordinate only within its named baseline."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "utility_type": "Electric",
                "station_from": "1100+28",
                "baseline": "IH69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "E103",
                "external_org": "CenterPoint",
                "utility_type": "Electric",
                "station_from": "1100+28",
                "baseline": "IH10",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_repeated_source_id_cannot_override_contradictory_coordinates(
    session, consecutive_nhhip_documents
):
    """A reused matrix id is not identity across incompatible baselines."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC14-1",
                "external_org": "Comcast",
                "utility_type": "Fiber Optic Cable",
                "station_from": "1080+93",
                "baseline": "IH 69",
                "alignment": "Eastex Freeway",
                "location_start": "North of McKay Drive",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC14-1",
                "external_org": "Comcast",
                "utility_type": "Telecom",
                "station_from": "1107+21",
                "baseline": "IH 10",
                "alignment": "Katy Freeway",
                "location_start": "East of Beltway 8",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_exact_foc8_baseline_correction_corresponds_as_changed(
    session, consecutive_nhhip_documents
):
    """The real FOC8-3 row is re-stationed across a corrected baseline."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [
            {
                "alignment": "Rothwell Street",
                "baseline": "IH 10",
                "data_source": "SUE",
                "external_org": "Verizon/MCI",
                "location_end": "IH 69",
                "location_start": "Nance Street",
                "offset_from": "388",
                "offset_side": "R",
                "offset_to": "883",
                "oh_ug": "UG",
                "orientation": "Perpendicular",
                "potential_conflict": "Y",
                "size": "FOC",
                "station_from": "1112+62",
                "station_to": "1116+18",
                "sue_level": "A",
                "utility_id": "FOC8-3",
                "utility_type": "Telecom",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, [successor_candidate] = _run(
        session,
        successor,
        [
            {
                "alignment": "Rothwell Street",
                "baseline": "IH 69",
                "data_source": "SUE",
                "external_org": "Verizon/MCI",
                "location_end": "Nance Street",
                "location_start": "Grayson Street",
                "material": "FOC",
                "offset_from": "212",
                "offset_side": "L",
                "offset_to": "750",
                "oh_ug": "UG",
                "orientation": "Crossing",
                "potential_conflict": "Y",
                "station_from": "1116+95",
                "station_to": "1119+20",
                "sue_level": "A",
                "utility_id": "FOC8-3",
                "utility_type": "Telecom",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.predecessor_candidate_ids == [predecessor_candidate.id]
    assert finding.successor_candidate_ids == [successor_candidate.id]
    assert {change["field"] for change in finding.field_changes} >= {
        "baseline",
        "station_from",
        "station_to",
    }
    assert any(
        signal["name"] == "station_across_corrected_baseline"
        for signal in finding.matcher_detail["signals"]
    )


def test_one_location_anchor_routes_cross_baseline_identity_to_ambiguity(
    session, consecutive_nhhip_documents
):
    """One shared place is review evidence, not assignable baseline correction."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "FOC14-1",
                "external_org": "Comcast",
                "station_from": "1080+93",
                "baseline": "IH 69",
                "location_start": "Main Street at First Avenue",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "FOC14-1",
                "external_org": "Comcast",
                "station_from": "1107+21",
                "baseline": "IH 10",
                "location_start": "Main Street at First Avenue",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "ambiguous"
    [alternative] = finding.matcher_detail["alternatives"]
    assert alternative["weak_identity"] is True
    assert alternative["chosen"] is False
    assert all(
        signal["name"] != "station_across_corrected_baseline"
        for signal in alternative["signals"]
    )


def test_equivalent_baseline_and_station_formatting_is_unchanged(
    session, consecutive_nhhip_documents
):
    """Printed spacing does not turn the same coordinate into review work."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "utility_type": "Electric",
                "station_from": "STA 1100+28",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "utility_type": "Electric",
                "station_from": "1100+28",
                "baseline": "IH69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "unchanged"
    assert finding.field_changes == []


def test_station_placeholder_and_missing_value_compare_as_absent(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "station_from": "1100+28",
                "station_to": "N/A",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "station_from": "1100+28",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "unchanged"
    assert finding.field_changes == []


def test_station_placeholder_to_real_coordinate_remains_a_change(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "station_from": "1100+28",
                "station_to": "NA",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "E41",
                "external_org": "CenterPoint",
                "station_from": "1100+28",
                "station_to": "1101+00",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "changed"
    assert finding.field_changes == [
        {"field": "station_to", "before": "NA", "after": "1101+00"}
    ]


def test_near_threshold_alternative_is_preserved_as_ambiguity(
    session, consecutive_nhhip_documents
):
    """A .59 alternative keeps a .61 match uncertain at a .60 threshold."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, predecessor_candidates = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A",
                "station_from": "100+00",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, successor_candidates = _run(
        session,
        successor,
        [
            {"utility_id": "A", "station_from": "101+95"},
            {"utility_id": "A", "station_from": "102+05"},
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_config={
            "minimum_score": 0.60,
            "ambiguity_margin": 0.04,
            "weights": {
                "station": 1.0,
                "source_ref": 0.0,
                "owner": 0.0,
                "type": 0.0,
                "location": 0.0,
            },
        },
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "ambiguous"
    assert finding.predecessor_candidate_ids == [predecessor_candidates[0].id]
    assert set(finding.successor_candidate_ids) == {
        candidate.id for candidate in successor_candidates
    }
    assert sum(
        alternative["below_threshold"]
        for alternative in finding.matcher_detail["alternatives"]
    ) == 2


def test_lone_below_threshold_edge_remains_added_and_dropped(
    session, consecutive_nhhip_documents
):
    """Plausibility for ambiguity does not lower the assignment threshold."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A",
                "station_from": "100+00",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [
            {
                "utility_id": "A",
                "station_from": "102+05",
                "baseline": "IH 69",
            }
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_config={
            "minimum_score": 0.60,
            "ambiguity_margin": 0.04,
            "weights": {
                "station": 1.0,
                "source_ref": 0.0,
                "owner": 0.0,
                "type": 0.0,
                "location": 0.0,
            },
        },
    )

    assert Counter(
        finding.state
        for finding in read_revision_comparison(session, comparison.id).findings
    ) == {"dropped": 1, "added": 1}


def test_infeasible_stronger_displaced_pair_does_not_override_assignment(
    session, consecutive_nhhip_documents
):
    """A stronger edge is irrelevant when forcing it loses cardinality."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, predecessor_candidates = _run(
        session,
        predecessor,
        [
            {
                "utility_id": "A",
                "external_org": "Owner X",
                "utility_type": "Telecom",
                "station_from": "100+00",
            },
            {
                "utility_id": "A",
                "external_org": "Owner X",
                "utility_type": "Pipeline",
            },
        ],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, successor_candidates = _run(
        session,
        successor,
        [
            {
                "utility_id": "A",
                "external_org": "Owner X",
                "utility_type": "Telco",
            },
            {
                "external_org": "Owner X",
                "utility_type": "Telecom",
                "station_from": "101+00",
            },
        ],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    findings = read_revision_comparison(session, comparison.id).findings

    assert len(findings) == 2
    assert {
        (
            tuple(finding.predecessor_candidate_ids),
            tuple(finding.successor_candidate_ids),
        )
        for finding in findings
    } == {
        ((predecessor_candidates[0].id,), (successor_candidates[1].id,)),
        ((predecessor_candidates[1].id,), (successor_candidates[0].id,)),
    }
    assert all(finding.state != "ambiguous" for finding in findings)


def test_sparse_global_assignment_beats_greedy_cardinality():
    """The assignment primitive finds two rows where greedy finds one."""

    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.90, ()),
        (1, 11): _ScoredEdge(1, 11, 0.82, ()),
        (2, 10): _ScoredEdge(2, 10, 0.85, ()),
    }

    assert {
        (edge.predecessor_id, edge.successor_id)
        for edge in _global_matching(edges)
    } == {(1, 11), (2, 10)}


def test_assignment_regret_at_exact_margin_returns_full_alternating_witness():
    """A four-point loss at integer scale is reviewer-visible, inclusively."""

    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.60, ()),
        (1, 20): _ScoredEdge(1, 20, 0.62, ()),
        (2, 10): _ScoredEdge(2, 10, 0.62, ()),
        (2, 20): _ScoredEdge(2, 20, 0.68, ()),
    }
    matched = _global_matching(edges)

    assert {
        (edge.predecessor_id, edge.successor_id) for edge in matched
    } == {(1, 10), (2, 20)}
    assert _assignment_regret_pairs(edges, matched, 0.04) == set(edges)


def test_assignment_regret_just_outside_margin_is_not_uncertain():
    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.60, ()),
        (1, 20): _ScoredEdge(1, 20, 0.62, ()),
        (2, 10): _ScoredEdge(2, 10, 0.62, ()),
        (2, 20): _ScoredEdge(2, 20, 0.680001, ()),
    }
    matched = _global_matching(edges)

    assert _assignment_regret_pairs(edges, matched, 0.04) == set()


def test_assignment_regret_exact_tie_returns_both_assignments():
    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.60, ()),
        (1, 20): _ScoredEdge(1, 20, 0.62, ()),
        (2, 10): _ScoredEdge(2, 10, 0.62, ()),
        (2, 20): _ScoredEdge(2, 20, 0.64, ()),
    }
    matched = _global_matching(edges)

    assert _assignment_regret_pairs(edges, matched, 0.0) == set(edges)


def test_assignment_regret_rejects_infeasible_lone_cross_edge():
    """A locally tempting edge is irrelevant if forcing it loses cardinality."""

    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.99, ()),
        (2, 20): _ScoredEdge(2, 20, 0.60, ()),
        (2, 10): _ScoredEdge(2, 10, 0.70, ()),
    }
    matched = _global_matching(edges)

    assert _assignment_regret_pairs(edges, matched, 0.20) == set()


def test_assignment_regret_handles_unmatched_left_swap():
    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.90, ()),
        (2, 10): _ScoredEdge(2, 10, 0.88, ()),
    }
    matched = _global_matching(edges)

    assert _assignment_regret_pairs(edges, matched, 0.02) == set(edges)


def test_assignment_regret_handles_unmatched_right_swap():
    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.90, ()),
        (1, 20): _ScoredEdge(1, 20, 0.88, ()),
    }
    matched = _global_matching(edges)

    assert _assignment_regret_pairs(edges, matched, 0.02) == set(edges)


def test_assignment_regret_keeps_independent_groups_separate():
    edges = {
        (1, 10): _ScoredEdge(1, 10, 0.60, ()),
        (1, 20): _ScoredEdge(1, 20, 0.62, ()),
        (2, 10): _ScoredEdge(2, 10, 0.62, ()),
        (2, 20): _ScoredEdge(2, 20, 0.68, ()),
        (3, 30): _ScoredEdge(3, 30, 0.60, ()),
        (3, 40): _ScoredEdge(3, 40, 0.62, ()),
        (4, 30): _ScoredEdge(4, 30, 0.62, ()),
        (4, 40): _ScoredEdge(4, 40, 0.68, ()),
    }
    matched = _global_matching(edges)
    regret_pairs = _assignment_regret_pairs(edges, matched, 0.04)

    assert regret_pairs == set(edges)
    findings, predecessor_ids, successor_ids = _bounded_ambiguities(
        edges,
        matched,
        {
            "weak_identity": set(),
            "below_threshold": set(),
            "assignment_regret": regret_pairs,
        },
        DEFAULT_MATCHER_CONFIG,
    )
    assert len(findings) == 2
    assert {finding.predecessor_ids for finding in findings} == {(1, 2), (3, 4)}
    assert predecessor_ids == {1, 2, 3, 4}
    assert successor_ids == {10, 20, 30, 40}


def test_station_neighbor_chain_overflow_becomes_bounded_unmatched_rows():
    """Corridor-scale uncertainty never becomes one giant reviewer task."""

    rows_per_side = DEFAULT_MATCHER_CONFIG["max_ambiguity_rows"] // 2 + 1
    matched = [
        _ScoredEdge(index, 1000 + index, 0.95, ())
        for index in range(1, rows_per_side + 1)
    ]
    edges = {
        (edge.predecessor_id, edge.successor_id): edge for edge in matched
    }
    regret_pairs = set()
    for index in range(1, rows_per_side):
        pair = (index, 1000 + index + 1)
        edges[pair] = _ScoredEdge(*pair, 0.94, ())
        regret_pairs.add(pair)

    findings, predecessor_ids, successor_ids = _bounded_ambiguities(
        edges,
        matched,
        {
            "weak_identity": set(),
            "below_threshold": set(),
            "assignment_regret": regret_pairs,
        },
        DEFAULT_MATCHER_CONFIG,
    )

    assert len(findings) == rows_per_side * 2
    assert {finding.state for finding in findings} == {"unmatched"}
    assert predecessor_ids == set(range(1, rows_per_side + 1))
    assert successor_ids == set(range(1001, 1001 + rows_per_side))
    summaries = {
        tuple(sorted(finding.matcher_detail["ambiguity_group"].items()))
        for finding in findings
    }
    assert len(summaries) == 1
    [summary] = summaries
    summary = dict(summary)
    assert summary["predecessor_count"] == rows_per_side
    assert summary["successor_count"] == rows_per_side
    assert summary["alternative_count"] == (rows_per_side * 2) - 1
    assert all(
        len(finding.matcher_detail["top_alternatives"])
        <= DEFAULT_MATCHER_CONFIG["max_ambiguity_alternatives"]
        for finding in findings
    )


def test_failed_or_absent_successor_cannot_create_a_comparison(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    failed_successor = record_extraction_run(
        session,
        successor,
        prompt_version="matrix_tiered_v3",
        candidate_count=0,
        page_errors=1,
        outcome="failed",
        model="gpt-test",
        schema_version="matrix-schema-v3",
        error_detail="upstream unavailable",
        allow_unsealed_legacy=True,
    )
    session.flush()

    with pytest.raises(IncompleteSuccessorExtraction, match="dropped|vanished"):
        create_revision_comparison(
            session, predecessor_run.id, failed_successor.id
        )
    with pytest.raises(MissingExtractionRun, match="does not exist"):
        create_revision_comparison(session, predecessor_run.id, 9_999_999)
    with pytest.raises(IntegrityError, match="receipts are immutable"):
        with session.begin_nested():
            session.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == failed_successor.id)
                .values(outcome="completed", page_errors=0)
            )

    assert session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.project_id == project.id
        )
    ).all() == []


def test_completed_zero_row_successor_can_prove_predecessor_rows_dropped(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run = record_extraction_run(
        session,
        successor,
        prompt_version="matrix_tiered_v3",
        candidate_count=0,
        page_errors=0,
        model="gpt-test",
        schema_version="matrix-schema-v3",
        allow_unsealed_legacy=True,
    )
    session.flush()

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    assert finding.state == "dropped"
    assert finding.predecessor_candidate_ids == [predecessor_candidate.id]
    assert finding.successor_candidate_ids == []


def test_non_successor_documents_are_rejected_even_inside_one_project(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    unrelated = _document(
        session,
        project,
        registry_id="nhhip-unrelated-registered-document",
        filename="unrelated.pdf",
    )
    unrelated_run, _ = _run(
        session,
        unrelated,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    with pytest.raises(InvalidRevisionPair, match="declared"):
        create_revision_comparison(
            session, predecessor_run.id, unrelated_run.id
        )

    assert predecessor.superseded_by == successor.id


def test_registry_edge_changed_during_compute_is_rejected_after_lock(
    session, consecutive_nhhip_documents, monkeypatch
):
    """Optimistic matching never seals a receipt against stale registry state."""

    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    real_compare = revision_comparison._compare_inputs

    def compare_then_change_registry(*args, **kwargs):
        findings = real_compare(*args, **kwargs)
        session.execute(
            text(
                "update documents set superseded_by = null, "
                "superseded_on = null, "
                "supersession_source_document_id = null, "
                "supersession_source_page = null where id = :document_id"
            ),
            {"document_id": predecessor.id},
        )
        return findings

    monkeypatch.setattr(
        revision_comparison, "_compare_inputs", compare_then_change_registry
    )

    with pytest.raises(InvalidRevisionPair, match="declared"):
        create_revision_comparison(
            session, predecessor_run.id, successor_run.id
        )

    assert session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.predecessor_extraction_run_id
            == predecessor_run.id,
            RevisionComparisonRun.successor_extraction_run_id
            == successor_run.id,
        )
    ).all() == []
    session.refresh(predecessor)
    assert predecessor.superseded_by == successor.id


def test_new_matcher_version_or_configuration_appends_without_rewriting(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    original = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    original_content = (
        original.matcher_version,
        dict(original.matcher_config),
        original.content_sha256,
    )

    rerun = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_version="revision-correspondence-v2",
        matcher_config={"minimum_score": 0.75},
    )
    session.expire(original)

    assert rerun.id != original.id
    assert (
        original.matcher_version,
        dict(original.matcher_config),
        original.content_sha256,
    ) == original_content
    assert rerun.matcher_version == "revision-correspondence-v2"
    assert rerun.matcher_config["minimum_score"] == 0.75
    assert [
        item.id
        for item in list_revision_comparisons(
            session, predecessor_run.id, successor_run.id
        )
    ] == [original.id, rerun.id]


def test_identical_execution_returns_the_verified_retained_comparison(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    original = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    original_readback = read_revision_comparison(session, original.id)

    retained = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_config=dict(DEFAULT_MATCHER_CONFIG),
    )
    retained_readback = read_revision_comparison(session, retained.id)

    assert retained.id == original.id
    assert retained.content_sha256 == original.content_sha256
    assert retained_readback.findings == original_readback.findings
    assert [
        comparison.id
        for comparison in list_revision_comparisons(
            session, predecessor_run.id, successor_run.id
        )
    ] == [original.id]


def test_identical_execution_refuses_preexisting_ambiguous_history(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    original = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    duplicate = _clone_comparison_receipt(session, original)

    with pytest.raises(AmbiguousRevisionComparison, match="identical execution"):
        create_revision_comparison(
            session, predecessor_run.id, successor_run.id
        )

    assert [
        comparison.id
        for comparison in list_revision_comparisons(
            session, predecessor_run.id, successor_run.id
        )
    ] == [original.id, duplicate.id]


def test_identical_execution_refuses_an_unsealed_retained_receipt(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    unsealed = _insert_unsealed_comparison_fixture(
        session,
        project,
        predecessor,
        successor,
        predecessor_run,
        successor_run,
    )

    with pytest.raises(CorruptRevisionComparison, match="not sealed"):
        create_revision_comparison(
            session, predecessor_run.id, successor_run.id
        )

    assert session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.predecessor_extraction_run_id
            == predecessor_run.id,
            RevisionComparisonRun.successor_extraction_run_id
            == successor_run.id,
        )
    ).all() == [unsealed]


def test_identical_execution_refuses_a_corrupt_retained_receipt(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    corrupt = _insert_unsealed_comparison_fixture(
        session,
        project,
        predecessor,
        successor,
        predecessor_run,
        successor_run,
    )
    corrupt.sealed_at = datetime.now(timezone.utc)
    session.flush([corrupt])

    with pytest.raises(CorruptRevisionComparison, match="digest"):
        create_revision_comparison(
            session, predecessor_run.id, successor_run.id
        )

    assert [
        comparison.id
        for comparison in list_revision_comparisons(
            session, predecessor_run.id, successor_run.id
        )
    ] == [corrupt.id]


def test_identical_execution_refuses_changed_retained_input_identity(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    mismatched = _insert_unsealed_comparison_fixture(
        session,
        project,
        predecessor,
        successor,
        predecessor_run,
        successor_run,
        predecessor_inputs=[],
        successor_inputs=[],
    )

    with pytest.raises(RevisionComparisonError, match="input identity"):
        create_revision_comparison(
            session, predecessor_run.id, successor_run.id
        )

    assert session.scalars(
        select(RevisionComparisonRun).where(
            RevisionComparisonRun.predecessor_extraction_run_id
            == predecessor_run.id,
            RevisionComparisonRun.successor_extraction_run_id
            == successor_run.id,
        )
    ).all() == [mismatched]


def test_concurrent_identical_executions_converge_on_one_comparison():
    project_id, predecessor_run_id, successor_run_id = _committed_comparison_pair()
    ready = Barrier(2)

    def compare_in_own_transaction():
        with Session() as competing:
            ready.wait(timeout=2)
            comparison = create_revision_comparison(
                competing, predecessor_run_id, successor_run_id
            )
            comparison_id = comparison.id
            competing.commit()
            return comparison_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            comparison_ids = tuple(pool.map(lambda _: compare_in_own_transaction(), range(2)))

        assert comparison_ids[0] == comparison_ids[1]
        with Session() as verification:
            assert [
                comparison.id
                for comparison in list_revision_comparisons(
                    verification, predecessor_run_id, successor_run_id
                )
            ] == [comparison_ids[0]]
    finally:
        _delete_committed_comparison_project(project_id)


def test_readback_uses_input_snapshots_after_candidate_state_or_payload_changes(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T", "count": 0}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T", "count": False}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )

    predecessor_candidate.payload_json = {
        **predecessor_candidate.payload_json,
        "fields": {"utility_id": "EDITED"},
    }
    predecessor_candidate.state = "rejected"
    session.flush()
    readback = read_revision_comparison(session, comparison.id)

    assert readback.predecessor_inputs[0]["state"] == "pending"
    assert readback.predecessor_inputs[0]["payload_json"]["fields"] == {
        "utility_id": "FOC1-1",
        "external_org": "AT&T",
        "count": 0,
    }
    [changed] = readback.findings
    assert changed.state == "changed"
    assert changed.field_changes == [
        {"field": "count", "before": 0, "after": False}
    ]


def test_readback_digest_survives_jsonb_negative_zero_canonicalization(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T", "offset": -0.0}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T", "offset": 0.0}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    comparison = create_revision_comparison(
        session,
        predecessor_run.id,
        successor_run.id,
        matcher_config={"weights": {"location": -0.0}},
    )
    comparison_id = comparison.id
    session.flush()
    session.expire_all()

    readback = read_revision_comparison(session, comparison_id)

    assert readback.comparison.matcher_config["weights"]["location"] == 0.0
    assert readback.predecessor_inputs[0]["payload_json"]["fields"]["offset"] == 0.0
    assert [finding.state for finding in readback.findings] == ["unchanged"]


def test_comparison_uses_run_snapshot_when_candidate_was_edited_before_creation(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, [predecessor_candidate] = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    predecessor_candidate.payload_json = {
        **predecessor_candidate.payload_json,
        "fields": {"utility_id": "HUMAN-EDIT", "external_org": "AT&T"},
    }
    predecessor_candidate.state = "accepted"
    session.flush()

    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    readback = read_revision_comparison(session, comparison.id)

    assert [finding.state for finding in readback.findings] == ["unchanged"]
    assert readback.predecessor_inputs[0]["payload_json"]["fields"] == {
        "utility_id": "FOC1-1",
        "external_org": "AT&T",
    }
    assert readback.predecessor_inputs[0]["state"] == "pending"


def test_nonzero_legacy_run_without_exact_inputs_fails_closed(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    legacy = ExtractionRun(
        document_id=predecessor.id,
        prompt_version="legacy-v1",
        outcome="completed",
        candidate_count=1,
        page_errors=0,
        model="legacy-model",
        schema_version="legacy-schema",
        candidate_inputs_json=None,
    )
    session.add(legacy)
    session.flush()
    session.add(
        Candidate(
            project_id=predecessor.project_id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {"utility_id": "FOC1-1"},
            },
            source_document_id=predecessor.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="legacy-v1",
            model="legacy-model",
            citations_verified=True,
        )
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    session.flush()

    with pytest.raises(InexactExtractionInputs, match="fresh extraction"):
        create_revision_comparison(session, legacy.id, successor_run.id)


def test_database_rejects_mutation_or_extension_of_a_sealed_receipt(
    session, consecutive_nhhip_documents
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [{"utility_id": "FOC1-1", "external_org": "AT&T"}],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )
    comparison = create_revision_comparison(
        session, predecessor_run.id, successor_run.id
    )
    [finding] = read_revision_comparison(session, comparison.id).findings

    with pytest.raises(IntegrityError, match="append-only"):
        with session.begin_nested():
            session.execute(
                update(RevisionComparisonRun)
                .where(RevisionComparisonRun.id == comparison.id)
                .values(matcher_version="rewritten")
            )
    with pytest.raises(IntegrityError, match="receipts are immutable"):
        with session.begin_nested():
            session.execute(
                update(ExtractionRun)
                .where(ExtractionRun.id == predecessor_run.id)
                .values(candidate_inputs_json=[])
            )
    with pytest.raises(IntegrityError, match="append-only"):
        with session.begin_nested():
            session.execute(
                delete(RevisionComparisonFinding).where(
                    RevisionComparisonFinding.id == finding.id
                )
            )
    with pytest.raises(IntegrityError, match="sealed"):
        with session.begin_nested():
            session.add(
                RevisionComparisonFinding(
                    revision_comparison_run_id=comparison.id,
                    ordinal=2,
                    state="added",
                    predecessor_candidate_ids=[],
                    successor_candidate_ids=[999_999],
                    match_score=None,
                    field_changes=[],
                    matcher_detail={"reason": "should not append"},
                )
            )
            session.flush()


def test_database_refuses_to_seal_or_commit_an_incomplete_receipt(
    session, consecutive_nhhip_documents
):
    project, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    with pytest.raises(IntegrityError, match="cannot seal"):
        with session.begin_nested():
            incomplete = RevisionComparisonRun(
                project_id=project.id,
                predecessor_document_id=predecessor.id,
                successor_document_id=successor.id,
                predecessor_extraction_run_id=predecessor_run.id,
                successor_extraction_run_id=successor_run.id,
                predecessor_schema_version=predecessor_run.schema_version,
                successor_schema_version=successor_run.schema_version,
                predecessor_prompt_version=predecessor_run.prompt_version,
                successor_prompt_version=successor_run.prompt_version,
                predecessor_model=predecessor_run.model,
                successor_model=successor_run.model,
                matcher_version=DEFAULT_MATCHER_VERSION,
                matcher_config={"minimum_score": 0.6},
                predecessor_inputs_json=[],
                successor_inputs_json=[],
                finding_count=1,
                content_sha256="0" * 64,
            )
            session.add(incomplete)
            session.flush()
            session.execute(
                update(RevisionComparisonRun)
                .where(RevisionComparisonRun.id == incomplete.id)
                .values(sealed_at=text("now()"))
            )

    with pytest.raises(IntegrityError, match="commit sealed and complete"):
        with session.begin_nested():
            unsealed = RevisionComparisonRun(
                project_id=project.id,
                predecessor_document_id=predecessor.id,
                successor_document_id=successor.id,
                predecessor_extraction_run_id=predecessor_run.id,
                successor_extraction_run_id=successor_run.id,
                predecessor_schema_version=predecessor_run.schema_version,
                successor_schema_version=successor_run.schema_version,
                predecessor_prompt_version=predecessor_run.prompt_version,
                successor_prompt_version=successor_run.prompt_version,
                predecessor_model=predecessor_run.model,
                successor_model=successor_run.model,
                matcher_version=DEFAULT_MATCHER_VERSION,
                matcher_config={"minimum_score": 0.6},
                predecessor_inputs_json=[],
                successor_inputs_json=[],
                finding_count=0,
                content_sha256="0" * 64,
            )
            session.add(unsealed)
            session.flush()
            session.execute(
                text(
                    "set constraints "
                    "revision_comparison_runs_must_commit_sealed immediate"
                )
            )

    with pytest.raises(IntegrityError, match="begin unsealed"):
        with session.begin_nested():
            presealed = RevisionComparisonRun(
                project_id=project.id,
                predecessor_document_id=predecessor.id,
                successor_document_id=successor.id,
                predecessor_extraction_run_id=predecessor_run.id,
                successor_extraction_run_id=successor_run.id,
                predecessor_schema_version=predecessor_run.schema_version,
                successor_schema_version=successor_run.schema_version,
                predecessor_prompt_version=predecessor_run.prompt_version,
                successor_prompt_version=successor_run.prompt_version,
                predecessor_model=predecessor_run.model,
                successor_model=successor_run.model,
                matcher_version=DEFAULT_MATCHER_VERSION,
                matcher_config={"minimum_score": 0.6},
                predecessor_inputs_json=[],
                successor_inputs_json=[],
                finding_count=0,
                content_sha256="0" * 64,
                sealed_at=datetime.now(timezone.utc),
            )
            session.add(presealed)
            session.flush()


@pytest.mark.parametrize(
    "matcher_config,error",
    [
        ({"station_tolerance_ft": float("inf")}, "numeric"),
        ({"weights": {"station": float("nan")}}, "non-negative"),
        (
            {
                "weights": {
                    "station": 1e308,
                    "source_ref": 1e308,
                    "owner": 1e308,
                    "type": 1e308,
                    "location": 1e308,
                }
            },
            "total must be finite",
        ),
        ({"station_tolerance_ft": 1e-308}, "between"),
        ({"station_tolerance_ft": 10**400}, "numeric"),
        ({"weights": {"station": 10**400}}, "non-negative"),
    ],
)
def test_matcher_configuration_rejects_non_finite_numbers(
    session, consecutive_nhhip_documents, matcher_config, error
):
    _, predecessor, successor = consecutive_nhhip_documents
    predecessor_run, _ = _run(
        session,
        predecessor,
        [],
        prompt_version="matrix_tiered_v2",
        model="gpt-test",
        schema_version="matrix-schema-v2",
    )
    successor_run, _ = _run(
        session,
        successor,
        [],
        prompt_version="matrix_tiered_v3",
        model="gpt-test",
        schema_version="matrix-schema-v3",
    )

    with pytest.raises(RevisionComparisonError, match=error):
        create_revision_comparison(
            session,
            predecessor_run.id,
            successor_run.id,
            matcher_config=matcher_config,
        )
