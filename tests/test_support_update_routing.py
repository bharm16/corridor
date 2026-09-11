"""Routing the unresolved consequence of ineligible replacement support.

These tests pin the read seam that replaces the retired generic support-update
ceremony (ADR-0034, ADR-0037): exact unchanged support flows through the managed
automatic path and produces no coordination question, while every changed,
ambiguous, dropped, edited, unsupported, or failed-citation row becomes one
specific routed consequence, and every technical failure stays with operations.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor.adjudicate import accept_candidate
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.ledger import mark_satisfies
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import (
    create_revision_comparison,
    read_revision_comparison,
)
from corridor.supersession import (
    SupersessionDeclaration,
    register_supersessions,
)
from corridor.work_list import build_work_list
from corridor import support_update_routing as routing


REVIEWER = HumanPrincipal("local:support-update-routing-reviewer")


def _document(session, project, *, registry_id, sha, filename, page_text):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _fields(*, utility_id="FOC1-1", station_from="100+00", baseline=None):
    fields = {
        "utility_id": utility_id,
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }
    if baseline is not None:
        fields["baseline"] = baseline
    return fields


def _quote(fields):
    return " ".join(
        value
        for value in (
            fields["utility_id"],
            fields["external_org"],
            fields["utility_type"],
            fields["station_from"],
            fields.get("baseline"),
        )
        if value
    )


def _candidate(project, document, fields, *, quote=None, citation_count=1):
    citation = {
        "document_id": document.id,
        "page": 1,
        "quote": quote if quote is not None else _quote(fields),
        "verified": True,
    }
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [dict(citation) for _ in range(citation_count)],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document, *candidates):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed(
    session,
    *,
    prior_satisfying=False,
    predecessor_baseline=None,
    successor_rows=({},),
    successor_quote=None,
    successor_citation_count=1,
    admitted_station=None,
    successor_failed=False,
    build_comparison=True,
    matcher_version="revision-correspondence-v2",
):
    """Seed one superseded, admitted Constraint and its successor revision.

    ``successor_rows`` is a tuple of field-override dicts for the successor
    rows: an empty dict reproduces an unchanged row, ``{"external_org": ...}``
    a changed row (identity preserved), ``{"station_from": ...}`` an unmatched
    row, two same-baseline rows an ambiguous match, and an empty tuple a
    dropped row.
    """

    project = Project(
        slug=f"support-update-routing-{uuid4().hex}",
        name="Support Update Routing",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == "AT&T")) is None:
        session.add(ExternalOrg(name="AT&T", aliases=[]))
        session.flush()

    predecessor_fields = _fields(baseline=predecessor_baseline)
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha="a",
        filename="revision-a.pdf",
        page_text=_quote(predecessor_fields),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha="b",
        filename="revision-b.pdf",
        page_text="FOC1-1 AT&T Telecom 100+00 200+00 IH-69 "
        + (predecessor_baseline or ""),
    )
    _document(
        session,
        project,
        registry_id="INDEX",
        sha="c",
        filename="index.pdf",
        page_text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidate = _candidate(project, predecessor, predecessor_fields)
    predecessor_run = _completed_run(session, predecessor, predecessor_candidate)
    if admitted_station is not None:
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == predecessor.id)
        ).one()
        page.text = f"{page.text} {admitted_station}"
        edited = deepcopy(predecessor_candidate.payload_json)
        edited["fields"] = {**edited["fields"], "station_from": admitted_station}
        predecessor_candidate.payload_json = edited
        session.flush([page, predecessor_candidate])

    dependency = accept_candidate(session, predecessor_candidate, principal=REVIEWER)
    dependency.evidence_required = "approved relocation closeout"
    old_evidence = session.scalars(
        select(EvidenceLink)
        .where(EvidenceLink.dependency_id == dependency.id)
        .order_by(EvidenceLink.id)
    ).one()
    if prior_satisfying:
        mark_satisfies(session, dependency.id, old_evidence.id, principal=REVIEWER)

    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidates = []
    for overrides in successor_rows:
        fields = _fields(baseline=predecessor_baseline)
        fields.update(overrides)
        successor_candidates.append(
            _candidate(
                project,
                successor,
                fields,
                quote=successor_quote,
                citation_count=successor_citation_count,
            )
        )
    if successor_failed:
        successor_run = record_extraction_run(
            session,
            successor,
            prompt_version="matrix-v1",
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model="test-model",
            error_detail="page extraction failed",
            schema_version="candidate-v1",
            allow_unsealed_legacy=True,
        )
    else:
        successor_run = _completed_run(session, successor, *successor_candidates)

    comparison = None
    if build_comparison and not successor_failed:
        comparison = create_revision_comparison(
            session,
            predecessor_extraction_run_id=predecessor_run.id,
            successor_extraction_run_id=successor_run.id,
            matcher_version=matcher_version,
        )
    session.flush()
    return {
        "project": project,
        "predecessor": predecessor,
        "successor": successor,
        "dependency": dependency,
        "predecessor_candidate": predecessor_candidate,
        "successor_candidates": tuple(successor_candidates),
        "predecessor_run": predecessor_run,
        "successor_run": successor_run,
        "comparison": comparison,
    }


def _one(session, project_id):
    consequences = routing.route_support_update_consequences(session, project_id)
    assert len(consequences) == 1, [c.reason for c in consequences]
    return consequences[0]


# --- each destination's own next step, beside the destinations --------------


def test_every_customer_destination_names_its_own_next_step():
    """The five next steps the Constraint screen used to mint, at their owner.

    Each sentence lived only inside `dependency.html` and nothing asserted any
    of them, while this module already held the destination identifiers and the
    plain project language of `explanation` beside them. These are the exact
    words that screen rendered, moved unchanged; changing one is a terminology
    decision (`docs/agents/domain.md`), not an edit to this test.
    """

    assert routing.destination_next_step(routing.SOURCE_DISCREPANCY) == (
        "Resolve the source discrepancy below, or record Needs clarification "
        "to keep it open."
    )
    assert routing.destination_next_step(routing.DOCUMENTATION_REVIEW) == (
        "Review the documentation against the stated requirement below."
    )
    assert routing.destination_next_step(routing.FAILED_CITATION) == (
        "Check the citation on the supporting documents below before it is used."
    )
    assert routing.destination_next_step(routing.GUIDED_STATEMENT) == (
        "Coordinate the correct statement from the kept alternatives."
    )
    assert routing.destination_next_step(routing.CORRECTION_REMOVAL) == (
        "Remove this entry from the active log, or correct it, using the "
        "controls below."
    )
    # An operations problem is never a customer task: a coordinator is not
    # asked to repair processing, so the screen prints nothing for it.
    assert routing.destination_next_step(routing.OPERATIONS) == ""
    # Every destination this module routes to answers here, so a new one
    # cannot be added without deciding what it asks a coordinator to do.
    for destination in routing.DESTINATIONS:
        routing.destination_next_step(destination)
    with pytest.raises(ValueError):
        routing.destination_next_step("not_a_destination")


def test_a_routed_consequence_carries_its_next_step_beside_its_explanation():
    """The screen reads the consequence, never the destination string itself."""

    consequence = routing.RoutedSupportConsequence(
        dependency_id=1,
        reason="comparison_changed",
        status="changed",
        destination=routing.SOURCE_DISCREPANCY,
        is_operations=False,
        explanation="the newer document states a different value",
        comparison_id=None,
        finding_id=None,
        predecessor_document_id=None,
        successor_document_id=None,
    )

    assert consequence.next_step == routing.destination_next_step(
        routing.SOURCE_DISCREPANCY
    )
    assert consequence.next_step != consequence.explanation


# --- exact unchanged support produces no coordination question -------------


def test_unchanged_replacement_has_no_customer_consequence(session):
    scenario = _seed(session, successor_rows=({},))

    consequences = routing.route_support_update_consequences(
        session, scenario["project"].id
    )
    by_dependency = routing.customer_consequences_by_dependency(
        session, scenario["project"].id
    )

    assert consequences == ()
    assert by_dependency == {}


# --- changed value routes to Source Discrepancy, with before/after ---------


def test_changed_value_routes_to_source_discrepancy(session):
    scenario = _seed(
        session,
        prior_satisfying=False,
        successor_rows=({"utility_type": "Gas"},),
    )

    consequence = _one(session, scenario["project"].id)

    assert consequence.dependency_id == scenario["dependency"].id
    assert consequence.status == "changed"
    assert consequence.destination == routing.SOURCE_DISCREPANCY
    assert consequence.is_customer
    assert "different value" in consequence.explanation


def test_changed_source_context_retains_before_and_after(session):
    scenario = _seed(
        session,
        prior_satisfying=False,
        successor_rows=({"utility_type": "Gas"},),
    )
    consequence = _one(session, scenario["project"].id)

    context = routing.changed_source_context(session, consequence)

    assert context is not None
    assert context.predecessor_document_name == "revision-a.pdf"
    assert context.successor_document_name == "revision-b.pdf"
    utility = next(c for c in context.changed_values if c.field == "utility_type")
    assert utility.before == "Telecom"
    assert utility.after == "Gas"
    assert not context.ambiguous
    assert any(row.quote for row in context.successor_rows)
    assert any(row.document_id == scenario["successor"].id for row in context.successor_rows)


# --- changed documentation support routes to Documentation Review ----------


def test_changed_readiness_support_routes_to_documentation_review(session):
    scenario = _seed(
        session,
        prior_satisfying=True,
        successor_rows=({"utility_type": "Gas"},),
    )

    consequence = _one(session, scenario["project"].id)

    assert consequence.destination == routing.DOCUMENTATION_REVIEW
    assert consequence.is_customer
    assert "requirement" in consequence.explanation


# --- dropped row routes to correction/removal ------------------------------


def test_dropped_row_routes_to_correction_removal(session):
    scenario = _seed(session, successor_rows=())

    consequence = _one(session, scenario["project"].id)

    assert consequence.destination == routing.CORRECTION_REMOVAL
    assert consequence.status == "dropped"
    assert consequence.is_customer


# --- ambiguous match routes to guided statement, alternatives preserved ----


def test_ambiguous_match_routes_to_guided_statement_and_preserves_all(session):
    scenario = _seed(
        session,
        predecessor_baseline="IH-69",
        successor_rows=({"utility_id": "FOC9-9", "station_from": "101+50"},),
    )

    consequence = _one(session, scenario["project"].id)

    assert consequence.status == "ambiguous"
    assert consequence.destination == routing.GUIDED_STATEMENT
    context = routing.changed_source_context(session, consequence)
    assert context is not None
    assert context.ambiguous
    # The successor alternative is kept, never reduced to an invented match.
    assert len(context.successor_rows) >= 1


# --- edited conclusion stays unresolved and routes to a customer decision --


def test_edited_conclusion_stays_unresolved_and_routes(session):
    scenario = _seed(
        session,
        prior_satisfying=False,
        admitted_station="100 + 00",
        successor_rows=({"station_from": "100 + 00"},),
    )

    consequence = _one(session, scenario["project"].id)

    assert consequence.reason == "admission_fields_changed"
    assert consequence.is_customer
    assert consequence.destination in {
        routing.SOURCE_DISCREPANCY,
        routing.DOCUMENTATION_REVIEW,
    }


# --- failed citation routes to failed-citation clarification ---------------


def test_failed_citation_routes_to_failed_citation(session):
    scenario = _seed(
        session,
        successor_rows=({},),
        successor_citation_count=2,
    )

    consequence = _one(session, scenario["project"].id)

    assert consequence.reason == "successor_provenance_unsafe"
    assert consequence.destination == routing.FAILED_CITATION
    assert consequence.is_customer


# --- technical failures stay in operations, never a customer question ------


def test_missing_comparison_is_operations_not_a_customer_question(session):
    scenario = _seed(
        session,
        successor_rows=({"utility_type": "Gas"},),
        build_comparison=False,
    )

    consequence = _one(session, scenario["project"].id)
    by_dependency = routing.customer_consequences_by_dependency(
        session, scenario["project"].id
    )
    operations = routing.operations_consequences(session, scenario["project"].id)

    assert consequence.destination == routing.OPERATIONS
    assert consequence.is_operations
    assert consequence.dependency_id not in by_dependency
    assert consequence in operations


def test_failed_successor_extraction_is_operations(session):
    scenario = _seed(session, successor_failed=True)

    consequence = _one(session, scenario["project"].id)

    assert consequence.status == "extraction_failed"
    assert consequence.destination == routing.OPERATIONS
    assert consequence.is_operations
    assert "operations" in consequence.explanation


# --- one grouped customer question per affected Constraint ------------------


def test_customer_consequences_are_one_per_dependency(session):
    scenario = _seed(
        session,
        prior_satisfying=False,
        successor_rows=({"utility_type": "Gas"},),
    )

    by_dependency = routing.customer_consequences_by_dependency(
        session, scenario["project"].id
    )

    assert set(by_dependency) == {scenario["dependency"].id}
    assert by_dependency[scenario["dependency"].id].destination == (
        routing.SOURCE_DISCREPANCY
    )


# --- the routed consequence surfaces once on the Work List -----------------


def test_changed_consequence_surfaces_as_one_work_list_item(session):
    scenario = _seed(
        session,
        prior_satisfying=False,
        successor_rows=({"utility_type": "Gas"},),
    )

    work_list = build_work_list(session, scenario["project"].id)

    items = [
        item
        for item in (*work_list.immediate, *work_list.backlog)
        if item.dependency_id == scenario["dependency"].id
    ]
    assert len(items) == 1
    assert "support_changed_value" in items[0].attention_reason_codes


def test_operations_consequence_stays_off_the_work_list(session):
    scenario = _seed(session, successor_failed=True)

    work_list = build_work_list(session, scenario["project"].id)

    items = [
        item
        for item in (*work_list.immediate, *work_list.backlog)
        if item.dependency_id == scenario["dependency"].id
        and any(
            code in item.attention_reason_codes
            for code in routing.WORK_LIST_REASON_CODES
        )
    ]
    assert items == []
