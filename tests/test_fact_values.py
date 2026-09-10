"""One typed-value reading of a Fact, shared by the source and record sides.

``fact_values`` replaced five readings of "what value does this Fact carry"
with one.  These tests pin the shape it answers for every Fact type, prove the
captured-Fact reader and the record projection agree, and state the comparison
restriction ``delta_generation`` keeps deliberately.
"""

from datetime import date
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor.delta_generation import COMPARABLE_FACT_TYPES, accepted_values
from corridor.fact_decisions import include_structured_cell_fact_by_policy
from corridor.fact_types import (
    STRUCTURED_DATE_FACT_TYPES,
    STRUCTURED_TEXT_FACT_TYPES,
)
from corridor.fact_values import (
    read_fact_value,
    scalar_column_value,
    scalar_fact_value,
    typed_fact_value,
)
from corridor.models import ActiveExtractionRun, Candidate, Dependency, Document, ExtractedProposal, ExtractedProposalFact, ExtractionRun, Fact, FactAppliesTo, FactClosureResult, FactSource, FactStatementTiming, SourceSegment
from corridor.record_projection import (
    CurrentRecordValue,
    CurrentStatementTiming,
    read_current_project_record,
    record_value_payload,
)


# --- The shape, one Fact type at a time ------------------------------------


def test_a_text_cell_reads_as_its_exact_text():
    assert typed_fact_value("station_from", text_value="1149+00") == "1149+00"


def test_a_date_cell_reads_as_one_iso_calendar_date():
    assert typed_fact_value("committed_date", date_value=date(2026, 3, 1)) == "2026-03-01"


def test_a_support_document_fact_reads_as_the_registered_revision():
    assert typed_fact_value(
        "supporting_documentation_in_use", document_value_id=41
    ) == {"document_id": 41}


def test_a_registered_organization_reference_reads_as_that_reference():
    """Reached only by a row carrying the reference and no wording of its own."""

    assert typed_fact_value("external_org", external_org_value_id=8) == {
        "external_org_id": 8
    }
    assert (
        typed_fact_value("external_org", text_value="Oncor", external_org_value_id=8)
        == "Oncor"
    )


def test_a_fact_with_no_value_column_set_reads_as_no_value():
    assert typed_fact_value("station_from") is None


def test_applies_to_reads_as_a_scope_mode_and_its_subject_keys():
    assert typed_fact_value(
        "applies_to", applies_to_subject_keys=("UC-1", "UC-2")
    ) == {"mode": "selected", "subject_keys": ["UC-1", "UC-2"]}
    assert typed_fact_value("applies_to") == {"mode": "unknown", "subject_keys": []}


def test_closure_result_reads_as_its_typed_kind():
    assert typed_fact_value("closure_result", closure_kind="constraint_closed") == {
        "closure_kind": "constraint_closed"
    }
    assert typed_fact_value("closure_result") == {"closure_kind": None}


def test_statement_timing_reads_as_its_roles_in_role_order():
    timings = (
        CurrentStatementTiming("new", "by June", "month", date(2026, 6, 1), date(2026, 6, 30)),
        CurrentStatementTiming("previous", "sometime soon", "approximate", None, None),
    )
    assert typed_fact_value("statement_timing", statement_timings=timings) == {
        "timings": [
            {
                "role": "new",
                "text": "by June",
                "precision": "month",
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            },
            {
                "role": "previous",
                "text": "sometime soon",
                "precision": "approximate",
                "start_date": None,
                "end_date": None,
            },
        ]
    }


def test_the_record_projection_answers_the_same_shape_as_the_reader():
    """``record_value_payload`` decides nothing of its own about the shape."""

    value = CurrentRecordValue(
        project_id=1, dependency_id=None, subject_key="Utility Conflicts!3",
        fact_type="applies_to", text_value=None, date_value=None,
        date_range_start=None, date_range_end=None, external_org_value_id=None,
        document_value_id=None, decision_id=1, fact_id=1, revision_id=1,
        applies_to_subject_keys=("UC-9",),
    )
    assert record_value_payload(value) == typed_fact_value(
        "applies_to", applies_to_subject_keys=("UC-9",)
    )
    dated = CurrentRecordValue(
        project_id=1, dependency_id=None, subject_key="Utility Conflicts!3",
        fact_type="committed_date", text_value=None, date_value=date(2026, 3, 1),
        date_range_start=None, date_range_end=None, external_org_value_id=None,
        document_value_id=None, decision_id=2, fact_id=2, revision_id=1,
    )
    assert record_value_payload(dated) == "2026-03-01"


# --- The two entry points, and what each refuses ---------------------------


@pytest.mark.parametrize("fact_type", ["applies_to", "closure_result", "statement_timing"])
def test_the_sessionless_reader_refuses_a_satellite_fact_type(fact_type):
    """A pure comparison pass may not read a satellite as an empty satellite."""

    fact = Fact(fact_type=fact_type)
    with pytest.raises(ValueError, match="satellite table"):
        scalar_fact_value(fact)


def test_the_column_reader_reports_a_satellite_field_as_no_scalar_value():
    """The restriction ``proposed_delta_comparison.accepted_values`` depends on."""

    assert scalar_column_value("applies_to", None, None, None, None) is None
    assert scalar_column_value("station_from", "1149+00", None, None, None) == "1149+00"


def test_the_comparison_pass_compares_only_single_valued_fields():
    """Card 4's boundary, made explicit rather than left as an accident.

    ``delta_generation`` compares ``COMPARABLE_FACT_TYPES`` only, so an
    ``applies_to``, ``closure_result`` or ``statement_timing`` Fact produces no
    Proposed Delta at all.  Widening this is a comparison change with its own
    ticket; a reader that can now read those values does not make it.
    """

    assert COMPARABLE_FACT_TYPES == frozenset(
        (*STRUCTURED_TEXT_FACT_TYPES, *STRUCTURED_DATE_FACT_TYPES)
    )
    for fact_type in ("applies_to", "closure_result", "statement_timing", "statement_wording"):
        assert fact_type not in COMPARABLE_FACT_TYPES


# --- Reading captured Facts out of the database ----------------------------


@pytest.fixture
def document(session, project):
    row = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="matrix.xlsx",
        doc_type="matrix",
        numbering_scheme="project-unique",
        pages=1,
        parse_status="parsed",
    )
    session.add(row)
    session.flush()
    run = ExtractionRun(
        document_id=row.id,
        prompt_version="fact_values_fixture_v1",
        outcome="completed",
        candidate_count=0,
        page_errors=0,
    )
    session.add(run)
    session.flush()
    session.add(ActiveExtractionRun(document_id=row.id, extraction_run_id=run.id))
    session.flush()
    return row, run


def _segment(session, project, document, *, ordinal, cell, value):
    row = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="spreadsheet_cell",
        exact_text=value,
        content_sha256=sha256(f"{cell}:{value}".encode()).hexdigest(),
        ordinal=ordinal,
        sheet_name="Utility Conflicts",
        cell_range=cell,
    )
    session.add(row)
    session.flush()
    return row


def _fact(session, project, document, run, *, fact_type, transformation, **values):
    row = Fact(
        project_id=project.id,
        document_id=document.id if document is not None else None,
        extraction_run_id=run.id if run is not None else None,
        fact_type=fact_type,
        subject_kind=values.pop("subject_kind", "source_row"),
        subject_key=values.pop("subject_key", "Utility Conflicts!3"),
        transformation=transformation,
        recorded_by="extractor:fact_values_fixture_v1",
        content_sha256=sha256(uuid4().bytes).hexdigest(),
        **values,
    )
    session.add(row)
    session.flush()
    return row


def test_a_captured_text_fact_reads_its_exact_cell(session, project, document):
    doc, run = document
    fact = _fact(
        session, project, doc, run,
        fact_type="station_from", transformation="trim_cell_text_v1",
        text_value="1149+00",
    )
    assert read_fact_value(session, fact) == "1149+00"


def test_a_captured_date_fact_reads_one_iso_date(session, project, document):
    doc, run = document
    fact = _fact(
        session, project, doc, run,
        fact_type="committed_date", transformation="iso_date_cell_v1",
        date_value=date(2026, 3, 1),
    )
    assert read_fact_value(session, fact) == "2026-03-01"


def test_a_captured_applies_to_fact_reads_its_subject_keys_in_order(
    session, project, document
):
    doc, run = document
    fact = _fact(
        session, project, doc, run,
        fact_type="applies_to", transformation="structured_reference_set_v1",
    )
    segment = _segment(session, project, doc, ordinal=1, cell="K3", value="UC-2, UC-1")
    for ordinal, key in enumerate(("UC-2", "UC-1"), start=1):
        session.add(
            FactAppliesTo(
                project_id=project.id, fact_id=fact.id, record_subject_key=key,
                source_segment_id=segment.id, reference_text=key, ordinal=ordinal,
            )
        )
    session.flush()
    assert read_fact_value(session, fact) == {
        "mode": "selected", "subject_keys": ["UC-2", "UC-1"]
    }


def test_a_legacy_dependency_scope_is_identity_and_not_a_value(
    session, project, document
):
    """A ``dependency_id`` member stays out of the payload, by design.

    The projection reports the same scope as unknown and carries the legacy ids
    on ``applies_to_dependency_ids``, so the two readings agree and a row id is
    never compared against a native subject key as though it were a value.
    """

    doc, run = document
    target = Dependency(
        project_id=project.id, ref_code="DEP-00001",
        dep_type="utility_relocation", title="Target",
    )
    session.add(target)
    session.flush()
    fact = _fact(
        session, project, doc, run,
        fact_type="applies_to", transformation="structured_reference_set_v1",
    )
    session.add(
        FactAppliesTo(
            project_id=project.id, fact_id=fact.id, dependency_id=target.id, ordinal=1
        )
    )
    session.flush()
    assert read_fact_value(session, fact) == {"mode": "unknown", "subject_keys": []}


def test_a_captured_closure_fact_reads_its_typed_kind(session, project, document):
    doc, run = document
    fact = _fact(
        session, project, doc, run,
        fact_type="closure_result", transformation="typed_closure_result_v1",
    )
    session.add(
        FactClosureResult(
            project_id=project.id, fact_id=fact.id,
            closure_kind="source_marked_resolved",
        )
    )
    session.flush()
    assert read_fact_value(session, fact) == {
        "closure_kind": "source_marked_resolved"
    }


def test_an_unresolved_closure_fact_reads_as_no_kind(session, project, document):
    doc, run = document
    fact = _fact(
        session, project, doc, run,
        fact_type="closure_result", transformation="typed_closure_result_v1",
    )
    assert read_fact_value(session, fact) == {"closure_kind": None}


def test_a_captured_statement_timing_fact_reads_both_roles(session, project):
    fact = _fact(
        session, project, None, None,
        fact_type="statement_timing", transformation="typed_statement_timing_v1",
        subject_kind="statement_candidate", subject_key="statement:1",
    )
    session.add_all(
        (
            FactStatementTiming(
                project_id=project.id, fact_id=fact.id, timing_role="previous",
                text="by March 1", precision="day",
                start_date=date(2026, 3, 1), end_date=date(2026, 3, 1),
            ),
            FactStatementTiming(
                project_id=project.id, fact_id=fact.id, timing_role="new",
                text="sometime this summer", precision="approximate",
            ),
        )
    )
    session.flush()
    assert read_fact_value(session, fact) == {
        "timings": [
            {
                "role": "new", "text": "sometime this summer",
                "precision": "approximate", "start_date": None, "end_date": None,
            },
            {
                "role": "previous", "text": "by March 1", "precision": "day",
                "start_date": "2026-03-01", "end_date": "2026-03-01",
            },
        ]
    }


# --- The accepted side reads the same values -------------------------------


@pytest.fixture
def accepted_record(session, project, document):
    """Two accepted stationing values, through the released inclusion policy."""

    doc, run = document
    dependency = Dependency(
        project_id=project.id, ref_code="DEP-00001",
        dep_type="utility_relocation", title="Utility conflict",
        station_from="100+00", station_to="200+00",
    )
    session.add(dependency)
    session.flush()
    candidate = Candidate(
        project_id=project.id, kind="dependency", payload_json={"fields": {}},
        source_document_id=doc.id, source_pages=[1],
        prompt_version=run.prompt_version, citations_verified=True,
        state="accepted", merged_into=dependency.id,
    )
    session.add(candidate)
    session.flush()
    proposal = ExtractedProposal(
        project_id=project.id, document_id=doc.id, extraction_run_id=run.id,
        candidate_id=candidate.id, kind="dependency",
        subject_key="Utility Conflicts!3",
        candidate_metadata_json={"state": "pending", "source_pages": [1]},
    )
    session.add(proposal)
    session.flush()
    for ordinal, (fact_type, value) in enumerate(
        (("station_from", "100+00"), ("station_to", "200+00")), start=1
    ):
        segment = _segment(
            session, project, doc, ordinal=ordinal, cell=f"D{ordinal + 2}", value=value
        )
        fact = _fact(
            session, project, doc, run,
            fact_type=fact_type, transformation="trim_cell_text_v1",
            subject_key=proposal.subject_key, text_value=value,
        )
        session.add_all(
            (
                FactSource(
                    project_id=project.id, document_id=doc.id, fact_id=fact.id,
                    source_segment_id=segment.id, role="value_source", ordinal=1,
                ),
                ExtractedProposalFact(
                    project_id=project.id, document_id=doc.id,
                    extraction_run_id=run.id, proposal_id=proposal.id,
                    fact_id=fact.id, ordinal=ordinal,
                ),
            )
        )
        session.flush()
        include_structured_cell_fact_by_policy(
            session, fact, idempotency_key=f"fact-values:{fact_type}"
        )
    return project, proposal.subject_key


def test_the_comparison_pass_and_the_projection_read_one_accepted_value(
    session, accepted_record
):
    """``accepted_values`` is the projection's own reading, not a fourth copy."""

    project, subject_key = accepted_record
    projected = {
        (value.subject_key, value.fact_type): record_value_payload(value)
        for value in read_current_project_record(session, project.id)
    }
    assert accepted_values(session, project.id) == projected
    assert projected[(subject_key, "station_from")] == "100+00"
    assert projected[(subject_key, "station_to")] == "200+00"


def test_every_accepted_scalar_fact_reads_the_same_way_from_its_own_row(
    session, accepted_record
):
    project, _ = accepted_record
    for value in read_current_project_record(session, project.id):
        fact = session.scalar(select(Fact).where(Fact.id == value.fact_id))
        assert read_fact_value(session, fact) == record_value_payload(value)
