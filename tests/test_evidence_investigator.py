"""Public Evidence Investigator behavior for one Unplaced Statement."""

import asyncio
import hashlib
import json
from dataclasses import fields as dataclass_fields
from datetime import date
from pathlib import Path

import pytest
import httpx
from sqlalchemy import delete
from sqlalchemy.orm import attributes

from corridor.db import Session, engine
from corridor.evidence_investigator import (
    INVESTIGATION_CASE_SCHEMA,
    INVESTIGATION_PACKET_SCHEMA,
    DependencyOption,
    EvidenceFact,
    InvestigationBudget,
    InvestigationPacket,
    InvestigationRunOutput,
    InvestigationRuntimeAbstention,
    PossibleParty,
    SourceFinding,
    investigate_candidate,
)
from corridor.evidence_investigator_runtime import (
    ADAPTER_CONTRACT_VERSION,
    DirectResponsesInvestigationRuntime,
    PROMPT_SHA256,
    RuntimeIdentity,
    TRANSPORT_GATE,
    configured_runtime_identity,
    run_receipted_investigation,
)
from corridor.evidence_investigator_shadow import (
    capture_shadow_outcome,
    observe_shadow_review,
    run_shadow_batch,
    run_v2_shadow_cohort,
    write_shadow_cohort_manifest,
)
from corridor.evidence_investigator_evaluation import (
    EvaluationRefusal,
    EvaluationRules,
    evaluate_shadow_runs,
    verify_evaluation_receipt,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EventAdmissionOutcome,
    EvidenceInvestigationPacketReceipt,
    EvidenceInvestigationRun,
    EvidenceInvestigationStepReceipt,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowExecution,
    EvidenceInvestigationShadowOutcome,
    EvidenceInvestigationEvaluationReceipt,
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import mark_statement_not_relevant

RECORDER = HumanPrincipal("local:evidence-investigator-test")


def _identity(**overrides):
    values = {
        "adapter": "stub-contract-v1",
        "adapter_contract_version": "stub-contract-v1",
        "model": "stub-investigator",
        "prompt_version": "evidence-investigator-test-v1",
        "prompt_sha256": "1" * 64,
        "transport_gate_sha256": "a" * 64,
    }
    values.update(overrides)
    return RuntimeIdentity(**values)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug="evidence-investigator-test",
        name="Evidence Investigator Test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    return project


def _unplaced_statement(
    session, project, *, quote=None, page_text=None, kind="event"
):
    quote = quote or (
        "Kinder Morgan expects the relocation to finish near Station 6609+00 "
        "during June 2026."
    )
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="minutes/evidence-investigator.pdf",
        doc_type="minutes",
        doc_date=date(2026, 5, 4),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text if page_text is not None else quote,
            text_source="cells",
        )
    )
    candidate = Candidate(
        project_id=project.id,
        kind=kind,
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "event_date": "2026-05-04",
                "description": quote,
                "external_org": "Kinder Morgan",
                "committed_date": "June 2026",
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.82,
        prompt_version="minutes-v-test",
        model="test-extractor",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes-v-test",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-extractor",
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    admission = PolicyRun(
        project_id=project.id,
        family="event-admission",
        policy_approval_id=None,
        policy_version="evidence-investigator-test",
        policy_sha256="a" * 64,
        abstention_reason_version="evidence-investigator-test",
        applied_count=0,
        abstained_count=1,
    )
    session.add(admission)
    session.flush()
    session.add(
        EventAdmissionOutcome(
            policy_run_id=admission.id,
            candidate_id=candidate.id,
            outcome="abstained",
            reason="no_conflict_reference",
        )
    )
    session.flush()
    return candidate, quote


def test_investigator_issues_a_literal_page_quote_for_a_normalized_citation(
    session, project
):
    stored_quote = "Kinder Morgan will finish in June 2026."
    literal_page_quote = "Kinder   Morgan will finish in June 2026."
    candidate, _quote = _unplaced_statement(
        session,
        project,
        quote=stored_quote,
        page_text=f"Meeting notes\n{literal_page_quote}\nEnd notes",
    )

    class Runtime:
        async def run(self, case, tools, budget):
            [evidence_ref] = case.evidence_refs
            evidence = await tools.read_candidate_evidence(
                evidence_ref, focus="exact commitment wording", max_chars=500
            )
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding(
                            evidence_ref=evidence_ref,
                            exact_quote=evidence.candidate_quote,
                            observation="The source states a month-level Commitment.",
                        ),
                    ),
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=("Which Commitment Scope applies?",),
                ),
                turns=2,
                input_tokens=50,
                output_tokens=20,
            )

    result = asyncio.run(
        investigate_candidate(
            session,
            candidate.id,
            runtime=Runtime(),
            budget=InvestigationBudget(),
        )
    )

    assert result.status == "human_judgment_needed"
    assert result.packet.source_findings[0].exact_quote == literal_page_quote


def test_investigator_does_not_present_a_fuzzy_near_miss_as_an_exact_quote(
    session, project
):
    stored_quote = "Kinder Morgan will finish in June 2026."
    literal_page_quote = "Kinder Morgan will finish in July 2026."
    candidate, _quote = _unplaced_statement(
        session,
        project,
        quote=stored_quote,
        page_text=f"Meeting notes\n{literal_page_quote}\nEnd notes",
    )

    class Runtime:
        async def run(self, case, tools, budget):
            [evidence_ref] = case.evidence_refs
            evidence = await tools.read_candidate_evidence(
                evidence_ref, focus="exact commitment wording", max_chars=500
            )
            assert evidence.candidate_quote is None
            assert literal_page_quote in evidence.page_context
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding(
                            evidence_ref=evidence_ref,
                            exact_quote=literal_page_quote,
                            observation="The page states different timing.",
                        ),
                    ),
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=("Which timing is attributable?",),
                ),
                turns=2,
                input_tokens=50,
                output_tokens=20,
            )

    result = asyncio.run(
        investigate_candidate(
            session,
            candidate.id,
            runtime=Runtime(),
            budget=InvestigationBudget(),
        )
    )

    assert result.status == "human_judgment_needed"
    assert result.packet.source_findings[0].exact_quote == literal_page_quote


def test_investigate_candidate_binds_capabilities_and_validates_unaccepted_options(
    session, project
):
    candidate, quote = _unplaced_statement(session, project)
    party = ExternalOrg(
        name="Kinder Morgan evidence-investigator-test",
        aliases=["Kinder Morgan"],
    )
    session.add(party)
    session.flush()
    dependency = Dependency(
        project_id=project.id,
        ref_code="KM-31",
        dep_type="utility_relocation",
        title="Kinder Morgan 20-inch line",
        station_from="6608+70",
        station_to="6616+50",
        external_org_id=party.id,
        status="identified",
    )
    session.add(dependency)
    session.flush()

    class StubRuntime:
        calls = 0

        async def run(self, case, tools, budget):
            self.calls += 1
            assert case.candidate.kind == "extracted_proposal_context"
            assert case.attention_reason == "unplaced_statement"
            [evidence_ref] = case.evidence_refs
            evidence = await tools.read_candidate_evidence(
                evidence_ref, focus="timing and station", max_chars=2_000
            )
            [possible_party] = (
                await tools.search_project_parties("Kinder Morgan", limit=5)
            ).parties
            [option] = (
                await tools.shortlist_active_dependencies(
                    possible_party.party_ref,
                    source_ref=evidence_ref,
                    station_text="6609+00",
                    terms=("20-inch", "line"),
                    limit=5,
                )
            ).dependencies
            await tools.read_dependency_context(option.dependency_ref)
            await tools.read_party_statement_context(possible_party.party_ref)
            fact = EvidenceFact(evidence_ref=evidence_ref, exact_quote=quote)
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding(
                            evidence_ref=evidence_ref,
                            exact_quote=evidence.candidate_quote,
                            observation="The source gives month-level timing.",
                        ),
                    ),
                    possible_parties=(
                        PossibleParty(
                            party_ref=possible_party.party_ref,
                            supporting_facts=(fact,),
                            contradicting_facts=(),
                        ),
                    ),
                    dependency_options=(
                        DependencyOption(
                            dependency_ref=option.dependency_ref,
                            rank=1,
                            supporting_facts=(fact,),
                            contradicting_facts=(),
                        ),
                    ),
                    human_questions=("Does June 2026 mean the end of the month?",),
                ),
                turns=1,
                input_tokens=600,
                output_tokens=300,
            )

    runtime = StubRuntime()
    result = asyncio.run(
        investigate_candidate(
            session,
            candidate.id,
            runtime=runtime,
            budget=InvestigationBudget(),
        )
    )

    assert runtime.calls == 1
    assert result.status == "options_available"
    assert result.packet is not None
    assert result.packet.dependency_options[0].rank == 1
    assert result.packet.dependency_options[0].dependency_ref.startswith("D-")
    assert result.packet.possible_parties[0].party_ref.startswith("P-")
    assert result.read_fingerprint
    assert candidate.state == "pending"
    assert dependency.status == "identified"


class _EmptyRuntime:
    def __init__(self):
        self.calls = 0

    async def run(self, case, tools, budget):
        self.calls += 1
        return InvestigationRunOutput(
            packet=InvestigationPacket((), (), (), ("What should the human decide?",)),
            turns=1,
            input_tokens=1,
            output_tokens=1,
        )


def _investigate(session, candidate_id, runtime, *, budget=None):
    return asyncio.run(
        investigate_candidate(
            session,
            candidate_id,
            runtime=runtime,
            budget=budget or InvestigationBudget(),
        )
    )


def test_ineligible_candidates_refuse_before_runtime_invocation(session, project):
    runtime = _EmptyRuntime()

    missing = _investigate(session, 9_999_999, runtime)
    candidate, _ = _unplaced_statement(session, project)
    candidate.state = "accepted"
    accepted = _investigate(session, candidate.id, runtime)

    assert (missing.status, missing.reason) == ("abstained", "candidate_not_found")
    assert (accepted.status, accepted.reason) == ("abstained", "candidate_ineligible")
    assert runtime.calls == 0


def test_non_event_undeclared_and_superseded_candidates_refuse_before_runtime(
    session, project
):
    runtime = _EmptyRuntime()
    non_event, _ = _unplaced_statement(
        session,
        project,
        quote="A Dependency proposal is not a statement.",
        kind="dependency",
    )
    non_event_result = _investigate(session, non_event.id, runtime)

    undeclared, _ = _unplaced_statement(
        session, project, quote="This Candidate loses its Active Run projection."
    )
    session.execute(
        delete(ActiveExtractionRun).where(
            ActiveExtractionRun.document_id == undeclared.source_document_id
        )
    )
    undeclared_result = _investigate(session, undeclared.id, runtime)

    superseded, _ = _unplaced_statement(
        session, project, quote="This Candidate belongs to a replaced document."
    )
    predecessor = session.get(Document, superseded.source_document_id)
    predecessor.registry_id = "investigator-predecessor"
    successor = Document(
        project_id=project.id,
        registry_id="investigator-successor",
        sha256=hashlib.sha256(b"investigator-successor").hexdigest(),
        filename="minutes/investigator-successor.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(successor)
    session.flush()
    session.add(
        DocPage(
            document_id=successor.id,
            page_no=1,
            text="This revision replaces the predecessor.",
            text_source="cells",
        )
    )
    session.flush()
    predecessor.superseded_by = successor.id
    predecessor.superseded_on = date(2026, 5, 6)
    predecessor.supersession_source_document_id = successor.id
    predecessor.supersession_source_page = 1
    session.flush()
    superseded_result = _investigate(session, superseded.id, runtime)

    assert (non_event_result.status, non_event_result.reason) == (
        "abstained",
        "candidate_ineligible",
    )
    assert (undeclared_result.status, undeclared_result.reason) == (
        "abstained",
        "undeclared_active_run",
    )
    assert (superseded_result.status, superseded_result.reason) == (
        "abstained",
        "candidate_historical",
    )
    assert runtime.calls == 0


def test_candidate_excluded_by_the_work_list_reader_refuses_before_runtime(
    session, project, monkeypatch
):
    """Fail closed if the Work List and Candidate-scope readers ever diverge.

    The current queries share the same pending/current/Active Run predicates,
    so no persisted row can naturally pass the earlier guards while being
    absent here.  This boundary test preserves the explicit defensive branch
    if either public reader later changes independently.
    """
    candidate, _ = _unplaced_statement(session, project)
    runtime = _EmptyRuntime()
    monkeypatch.setattr(
        "corridor.evidence_investigator.waiting_statements",
        lambda session, project_id, *, include_attachability: [],
    )

    result = _investigate(session, candidate.id, runtime)

    assert (result.status, result.reason) == (
        "abstained",
        "candidate_not_in_work_list",
    )
    assert runtime.calls == 0


def test_investigator_refuses_malformed_candidate_evidence_before_runtime(
    session, project
):
    candidate, _document = _unplaced_statement(session, project)
    candidate.payload_json["citations"][0]["document_id"] = True
    runtime = _EmptyRuntime()

    result = _investigate(session, candidate.id, runtime)

    assert (result.status, result.reason) == (
        "abstained",
        "evidence_unavailable",
    )
    assert runtime.calls == 0


def test_candidate_evidence_retains_diagnostic_only_distinction(session, project):
    candidate, _ = _unplaced_statement(session, project)
    page = (
        session.query(DocPage).filter_by(document_id=candidate.source_document_id).one()
    )
    page.text_source = "ocr"
    seen = {}

    class Runtime(_EmptyRuntime):
        async def run(self, case, tools, budget):
            seen["page"] = await tools.read_candidate_evidence(
                case.evidence_refs[0], focus="source", max_chars=500
            )
            return await super().run(case, tools, budget)

    result = _investigate(session, candidate.id, Runtime())

    assert result.status == "human_judgment_needed"
    assert seen["page"].supporting_evidence_eligible is False
    assert seen["page"].page_context


def test_party_and_dependency_reads_cannot_enumerate_other_projects_or_inactive_rows(
    session, project
):
    candidate, _ = _unplaced_statement(session, project)
    local_party = ExternalOrg(name="Scoped Utility evidence-investigator-test")
    closed_only_party = ExternalOrg(
        name="Closed Only Utility evidence-investigator-test"
    )
    global_party = ExternalOrg(name="Global Only evidence-investigator-test")
    other_project = Project(
        slug="other-evidence-investigator-test",
        name="Other Evidence Investigator Test",
        is_synthetic=True,
    )
    session.add_all([local_party, closed_only_party, global_party, other_project])
    session.flush()
    session.add_all(
        [
            Dependency(
                project_id=project.id,
                ref_code="OPEN-1",
                dep_type="utility_relocation",
                title="Scoped open record",
                external_org_id=local_party.id,
                status="identified",
            ),
            Dependency(
                project_id=project.id,
                ref_code="CLOSED-1",
                dep_type="utility_relocation",
                title="Scoped closed record",
                external_org_id=closed_only_party.id,
                status="closed",
            ),
            Dependency(
                project_id=other_project.id,
                ref_code="OTHER-1",
                dep_type="utility_relocation",
                title="Other project record",
                external_org_id=global_party.id,
                status="identified",
            ),
        ]
    )
    session.flush()

    class Runtime(_EmptyRuntime):
        async def run(self, case, tools, budget):
            assert not (await tools.search_project_parties("Global", limit=5)).parties
            assert not (
                await tools.search_project_parties("Closed Only", limit=5)
            ).parties
            [party] = (await tools.search_project_parties("Scoped", limit=5)).parties
            shortlist = await tools.shortlist_active_dependencies(
                party.party_ref,
                source_ref=case.evidence_refs[0],
                station_text=None,
                terms=(),
                limit=5,
            )
            assert [item.ref_code for item in shortlist.dependencies] == ["OPEN-1"]
            return await super().run(case, tools, budget)

    result = _investigate(session, candidate.id, Runtime())

    assert result.status == "human_judgment_needed"


def test_fabricated_packet_references_and_quotes_fail_closed(session, project):
    candidate, quote = _unplaced_statement(session, project)

    class FabricatedReferenceRuntime:
        async def run(self, case, tools, budget):
            await tools.read_candidate_evidence(
                case.evidence_refs[0], focus="source", max_chars=500
            )
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding("E-fabricated", quote, "A claimed finding."),
                    ),
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=(),
                ),
                turns=1,
                input_tokens=1,
                output_tokens=1,
            )

    fabricated = _investigate(session, candidate.id, FabricatedReferenceRuntime())

    class FabricatedQuoteRuntime:
        async def run(self, case, tools, budget):
            await tools.read_candidate_evidence(
                case.evidence_refs[0], focus="source", max_chars=500
            )
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding(
                            case.evidence_refs[0],
                            "The source promises an exact day.",
                            "A claimed finding.",
                        ),
                    ),
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=(),
                ),
                turns=1,
                input_tokens=1,
                output_tokens=1,
            )

    fabricated_quote = _investigate(session, candidate.id, FabricatedQuoteRuntime())

    assert (fabricated.status, fabricated.reason) == (
        "abstained",
        "validation_failure",
    )
    assert (fabricated_quote.status, fabricated_quote.reason) == (
        "abstained",
        "validation_failure",
    )


def test_exact_page_text_that_was_not_issued_to_the_runtime_is_rejected(
    session, project
):
    candidate, quote = _unplaced_statement(session, project)
    hidden_text = "A separate sentence outside the bounded tool result."
    page = (
        session.query(DocPage).filter_by(document_id=candidate.source_document_id).one()
    )
    page.text = f"{quote}\n{hidden_text}"

    class Runtime:
        async def run(self, case, tools, budget):
            await tools.read_candidate_evidence(
                case.evidence_refs[0], focus="candidate", max_chars=len(quote)
            )
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(
                        SourceFinding(
                            case.evidence_refs[0],
                            hidden_text,
                            "This text was never returned by the bounded read.",
                        ),
                    ),
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=(),
                ),
                turns=1,
                input_tokens=10,
                output_tokens=10,
            )

    result = _investigate(session, candidate.id, Runtime())

    assert (result.status, result.reason) == (
        "abstained",
        "validation_failure",
    )
    assert "not returned by a bound Evidence read" in result.detail


def test_changed_context_withholds_an_otherwise_valid_packet(session, project):
    candidate, _ = _unplaced_statement(session, project)

    class StaleRuntime(_EmptyRuntime):
        async def run(self, case, tools, budget):
            candidate.payload_json = {
                **candidate.payload_json,
                "fields": {
                    **candidate.payload_json["fields"],
                    "description": "Changed after the case was bound.",
                },
            }
            return await super().run(case, tools, budget)

    result = _investigate(session, candidate.id, StaleRuntime())

    assert (result.status, result.reason) == ("abstained", "stale_input")
    assert result.packet is None


def test_tool_and_reported_usage_budgets_fail_closed(session, project):
    candidate, _ = _unplaced_statement(session, project)

    class ToolBudgetRuntime:
        async def run(self, case, tools, budget):
            for _ in range(2):
                await tools.read_candidate_evidence(
                    case.evidence_refs[0], focus="source", max_chars=500
                )
            raise AssertionError("the second call must exhaust the tool budget")

    tool_budget = _investigate(
        session,
        candidate.id,
        ToolBudgetRuntime(),
        budget=InvestigationBudget(max_tool_calls=1),
    )

    class TokenBudgetRuntime(_EmptyRuntime):
        async def run(self, case, tools, budget):
            output = await super().run(case, tools, budget)
            return InvestigationRunOutput(
                packet=output.packet,
                turns=output.turns,
                input_tokens=budget.max_input_tokens + 1,
                output_tokens=output.output_tokens,
            )

    token_budget = _investigate(session, candidate.id, TokenBudgetRuntime())

    assert (tool_budget.status, tool_budget.reason) == (
        "abstained",
        "budget_exhaustion",
    )
    assert (token_budget.status, token_budget.reason) == (
        "abstained",
        "budget_exhaustion",
    )


def test_turn_output_token_retry_repair_and_timeout_budgets_fail_closed(
    session, project
):
    candidate, _ = _unplaced_statement(session, project)

    class UsageRuntime:
        def __init__(self, **overrides):
            self.overrides = overrides

        async def run(self, case, tools, budget):
            values = {
                "turns": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "retries": 0,
                "repairs": 0,
                **self.overrides,
            }
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ()), **values
            )

    overages = (
        UsageRuntime(turns=InvestigationBudget().max_turns + 1),
        UsageRuntime(output_tokens=4_001),
        UsageRuntime(retries=1),
        UsageRuntime(repairs=2),
    )
    for runtime in overages:
        result = _investigate(session, candidate.id, runtime)
        assert (result.status, result.reason) == (
            "abstained",
            "budget_exhaustion",
        )

    class TimeoutRuntime:
        async def run(self, case, tools, budget):
            await asyncio.sleep(0.05)
            raise AssertionError("the wall-clock budget must cancel this run")

    timeout = _investigate(
        session,
        candidate.id,
        TimeoutRuntime(),
        budget=InvestigationBudget(timeout_seconds=0.001),
    )
    assert (timeout.status, timeout.reason) == (
        "abstained",
        "budget_exhaustion",
    )


def test_dependency_detail_read_budget_fails_closed(session, project):
    candidate, _ = _unplaced_statement(session, project)
    party = ExternalOrg(name="Detail Budget Utility evidence-investigator-test")
    session.add(party)
    session.flush()
    session.add(
        Dependency(
            project_id=project.id,
            ref_code="DETAIL-1",
            dep_type="utility_relocation",
            title="Detail budget record",
            external_org_id=party.id,
            status="identified",
        )
    )
    session.flush()

    class Runtime:
        async def run(self, case, tools, budget):
            [match] = (
                await tools.search_project_parties("Detail Budget", limit=5)
            ).parties
            [dependency] = (
                await tools.shortlist_active_dependencies(
                    match.party_ref,
                    source_ref=case.evidence_refs[0],
                    station_text=None,
                    terms=(),
                    limit=5,
                )
            ).dependencies
            await tools.read_dependency_context(dependency.dependency_ref)
            await tools.read_dependency_context(dependency.dependency_ref)
            raise AssertionError("the second detail read must exhaust its budget")

    result = _investigate(
        session,
        candidate.id,
        Runtime(),
        budget=InvestigationBudget(max_dependency_detail_reads=1),
    )

    assert (result.status, result.reason) == (
        "abstained",
        "budget_exhaustion",
    )


def test_packet_contract_has_no_decision_or_coordination_plan_fields():
    names = {field.name for field in dataclass_fields(InvestigationPacket)}
    forbidden = {
        "selected_party",
        "selected_dependency",
        "scope_mode",
        "accepted_timing",
        "approval",
        "candidate_disposition",
        "internal_owner",
        "next_action",
        "action_due_date",
        "milestone_impact",
        "ready",
    }

    assert not names & forbidden


def test_final_packet_schema_is_strict_at_every_nested_object():
    def assert_strict_objects(schema):
        if not isinstance(schema, dict):
            return
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            assert set(schema.get("required", ())) == set(schema.get("properties", ()))
        for value in schema.values():
            if isinstance(value, dict):
                assert_strict_objects(value)
            elif isinstance(value, list):
                for item in value:
                    assert_strict_objects(item)

    for name in (
        "source_findings",
        "possible_parties",
        "dependency_options",
    ):
        assert (
            INVESTIGATION_PACKET_SCHEMA["properties"][name]["items"]["type"] == "object"
        )
    party_schema = INVESTIGATION_PACKET_SCHEMA["properties"]["possible_parties"][
        "items"
    ]
    assert party_schema["properties"]["supporting_facts"]["items"]["type"] == "object"
    assert_strict_objects(INVESTIGATION_PACKET_SCHEMA)


def test_case_schema_labels_proposal_context_and_is_strict():
    assert INVESTIGATION_CASE_SCHEMA["additionalProperties"] is False
    assert set(INVESTIGATION_CASE_SCHEMA["required"]) == set(
        INVESTIGATION_CASE_SCHEMA["properties"]
    )
    candidate = INVESTIGATION_CASE_SCHEMA["properties"]["candidate"]
    assert candidate["additionalProperties"] is False
    assert candidate["properties"]["kind"] == {
        "type": "string",
        "const": "extracted_proposal_context",
    }
    assert set(candidate["required"]) == set(candidate["properties"])


def test_runtime_values_must_match_the_strict_packet_types(session, project):
    candidate, _ = _unplaced_statement(session, project)

    class Runtime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=[],  # type: ignore[arg-type]
                    possible_parties=(),
                    dependency_options=(),
                    human_questions=(),
                ),
                turns=1,
                input_tokens=1,
                output_tokens=1,
            )

    result = _investigate(session, candidate.id, Runtime())

    assert (result.status, result.reason) == (
        "abstained",
        "validation_failure",
    )


def test_runtime_can_return_a_bounded_semantic_abstention(session, project):
    candidate, _ = _unplaced_statement(session, project)

    class Runtime:
        async def run(self, case, tools, budget):
            return InvestigationRuntimeAbstention(
                reason="ambiguous_scope",
                detail="The registered Evidence supports party-level context only.",
                turns=1,
                input_tokens=200,
                output_tokens=30,
            )

    result = _investigate(session, candidate.id, Runtime())

    assert (result.status, result.reason) == ("abstained", "ambiguous_scope")
    assert result.packet is None


def test_party_context_exposes_current_unknown_scope_evidence_without_staling_case(
    session, project
):
    candidate, _ = _unplaced_statement(session, project)
    party = ExternalOrg(name="Party Context Utility evidence-investigator-test")
    session.add(party)
    session.flush()
    quote = "Party Context Utility expects field work during July 2026."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="minutes/party-context.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=quote,
            text_source="cells",
        )
    )
    record_external_party_statement(
        session,
        project_id=project.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        source_kind="cited",
        event_date=date(2026, 5, 5),
        description=quote,
        new_timing=StatementTiming.month("July 2026", 2026, 7),
        scope=StatementScope.unknown(),
        created_by=RECORDER.subject,
        evidence=CitedStatementEvidence(document.id, 1, quote),
    )

    class Runtime:
        async def run(self, case, tools, budget):
            [match] = (
                await tools.search_project_parties("Party Context", limit=5)
            ).parties
            [statement] = (
                await tools.read_party_statement_context(match.party_ref)
            ).statements
            [evidence] = statement.evidence
            return InvestigationRunOutput(
                packet=InvestigationPacket(
                    source_findings=(),
                    possible_parties=(
                        PossibleParty(
                            party_ref=match.party_ref,
                            supporting_facts=(
                                EvidenceFact(
                                    evidence.evidence_ref, evidence.exact_quote
                                ),
                            ),
                            contradicting_facts=(),
                        ),
                    ),
                    dependency_options=(),
                    human_questions=("Is party-level scope still correct?",),
                ),
                turns=1,
                input_tokens=200,
                output_tokens=80,
            )

    result = _investigate(session, candidate.id, Runtime())

    assert result.status == "options_available"
    assert result.packet.possible_parties[0].supporting_facts[0].exact_quote == quote


def test_receipted_investigation_appends_one_terminal_non_authoritative_result(
    session, project
):
    run_count = session.query(EvidenceInvestigationRun).count()
    packet_count = session.query(EvidenceInvestigationPacketReceipt).count()
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("What should the human decide?",)),
                turns=1,
                input_tokens=120,
                output_tokens=30,
            )

    identity = _identity()
    first = asyncio.run(
        run_receipted_investigation(
            session,
            candidate.id,
            runtime=StubRuntime(),
            identity=identity,
            budget=InvestigationBudget(),
        )
    )
    second = asyncio.run(
        run_receipted_investigation(
            session,
            candidate.id,
            runtime=StubRuntime(),
            identity=identity,
            budget=InvestigationBudget(),
        )
    )

    assert first.run.public_id != second.run.public_id
    assert first.run.terminal_status == "human_judgment_needed"
    assert first.run.model == "stub-investigator"
    assert first.run.prompt_version == "evidence-investigator-test-v1"
    assert first.run.prompt_sha256 == "1" * 64
    assert first.run.adapter_contract_version == "stub-contract-v1"
    assert len(first.run.candidate_payload_sha256) == 64
    assert first.run.candidate_payload_sha256 == second.run.candidate_payload_sha256
    assert session.query(EvidenceInvestigationRun).count() == run_count + 2
    packets = session.query(EvidenceInvestigationPacketReceipt).order_by(
        EvidenceInvestigationPacketReceipt.id.desc()
    ).limit(2).all()
    assert session.query(EvidenceInvestigationPacketReceipt).count() == packet_count + 2
    assert len(packets) == 2
    assert all(packet.non_authoritative for packet in packets)
    assert all(packet.validator_outcome == "valid" for packet in packets)


def test_runtime_refuses_prompt_bytes_that_do_not_match_receipt_identity(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)
    runtime = DirectResponsesInvestigationRuntime(
        model="investigator-test",
        api_key="not-a-real-key",
        prompt="bytes that do not match the sealed v2 identity",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)),
    )

    with pytest.raises(ValueError, match="prompt bytes"):
        asyncio.run(
            run_receipted_investigation(
                session,
                candidate.id,
                runtime=runtime,
                identity=RuntimeIdentity(
                    adapter="direct-responses-v2",
                    adapter_contract_version=ADAPTER_CONTRACT_VERSION,
                    model="investigator-test",
                    prompt_version="evidence-investigator-v2",
                    prompt_sha256=PROMPT_SHA256,
                    transport_gate_sha256=TRANSPORT_GATE["sha256"],
                ),
                budget=InvestigationBudget(),
            )
        )
    asyncio.run(runtime._client.aclose())


def test_runtime_loads_the_sealed_v2_prompt_independent_of_process_cwd(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    runtime = DirectResponsesInvestigationRuntime(
        model="investigator-test",
        api_key="not-a-real-key",
    )

    assert runtime.prompt == (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "evidence_investigator_v2.md"
    ).read_text(encoding="utf-8")
    assert runtime.prompt_sha256 == PROMPT_SHA256


def test_evaluator_refuses_mixed_or_unverifiable_configuration(
    session, project, tmp_path
):
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("What is supported?",)),
                turns=1,
                input_tokens=10,
                output_tokens=10,
            )

    runs = []
    for prompt_sha256 in ("1" * 64, "2" * 64):
        [shadow] = asyncio.run(
            run_shadow_batch(
                session,
                project.id,
                runtime_factory=StubRuntime,
                identity=_identity(prompt_sha256=prompt_sha256),
                budget=InvestigationBudget(),
            )
        )
        runs.append(shadow.investigation.run.public_id)

    with pytest.raises(EvaluationRefusal, match="one exact configuration"):
        evaluate_shadow_runs(
            session,
            runs,
            rules=EvaluationRules(min_cases=1, required_strata=()),
            human_scores={},
            output_dir=tmp_path,
        )


def test_evaluator_refuses_mixed_tool_contract_versions(
    session, project, tmp_path
):
    first, _quote = _unplaced_statement(session, project)
    second, _quote = _unplaced_statement(
        session, project, quote="CenterPoint will finish in August 2027."
    )

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("What is supported?",)),
                turns=1,
                input_tokens=10,
                output_tokens=10,
            )

    results = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            candidate_ids=(first.id, second.id),
            runtime_factory=StubRuntime,
            identity=_identity(model="mixed-tool-contract-test"),
            budget=InvestigationBudget(),
        )
    )
    changed_run = results[1].investigation.run
    original_version = changed_run.tool_contract_version
    changed_run.tool_contract_version = "evidence-investigator-tools-v1"

    with session.no_autoflush, pytest.raises(
        EvaluationRefusal, match="one exact configuration"
    ):
        evaluate_shadow_runs(
            session,
            [item.investigation.run.public_id for item in results],
            rules=EvaluationRules(min_cases=1, required_strata=()),
            human_scores={},
            output_dir=tmp_path,
        )
    attributes.set_committed_value(
        changed_run, "tool_contract_version", original_version
    )


def test_direct_transport_is_stateless_strict_serial_and_locally_receipted(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["store"] is False
        assert payload["parallel_tool_calls"] is False
        assert payload["max_tool_calls"] == 6
        assert payload["text"]["format"]["strict"] is True
        assert all(tool["strict"] is True for tool in payload["tools"])
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 50, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "source_findings": [],
                                        "possible_parties": [],
                                        "dependency_options": [],
                                        "human_questions": [
                                            "Which registered party made this statement?"
                                        ],
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    runtime = DirectResponsesInvestigationRuntime(
        model="investigator-test",
        api_key="not-a-real-key",
        base_url="https://example.test/v1",
        prompt="immutable test prompt",
        client=client,
    )
    receipt = asyncio.run(
        run_receipted_investigation(
            session,
            candidate.id,
            runtime=runtime,
            identity=RuntimeIdentity(
                adapter="direct-responses-v2",
                adapter_contract_version=ADAPTER_CONTRACT_VERSION,
                model="investigator-test",
                prompt_version="evidence-investigator-test-v2",
                prompt_sha256=hashlib.sha256(b"immutable test prompt").hexdigest(),
                transport_gate_sha256=TRANSPORT_GATE["sha256"],
            ),
            budget=InvestigationBudget(),
        )
    )
    asyncio.run(client.aclose())

    assert receipt.run.usage_json == {
        "turns": 1,
        "input_tokens": 50,
        "output_tokens": 20,
        "retries": 0,
        "repairs": 0,
    }
    assert receipt.run.terminal_status == "human_judgment_needed"
    assert receipt.run.prompt_sha256 == hashlib.sha256(
        b"immutable test prompt"
    ).hexdigest()
    assert receipt.run.adapter_contract_version == ADAPTER_CONTRACT_VERSION
    [step] = session.query(EvidenceInvestigationStepReceipt).filter_by(
        run_id=receipt.run.id
    ).all()
    assert step.usage_json == {"input_tokens": 50, "output_tokens": 20}


def test_direct_transport_reserves_a_final_turn_inside_the_measured_token_budget(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)
    requests = 0
    evidence_ref = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests, evidence_ref
        requests += 1
        payload = json.loads(request.content)
        if evidence_ref is None:
            case = json.loads(payload["input"][0]["content"])
            [evidence_ref] = case["evidence_refs"]
        if requests <= 6:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "usage": {"input_tokens": 2_000, "output_tokens": 20},
                    "output": [
                        {
                            "type": "function_call",
                            "name": "read_candidate_evidence",
                            "call_id": f"call-{requests}",
                            "arguments": json.dumps(
                                {
                                    "evidence_ref": evidence_ref,
                                    "focus": "party, timing, and location",
                                    "max_chars": 500,
                                }
                            ),
                        }
                    ],
                },
            )
        assert payload["tool_choice"] == "none"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 2_000, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "source_findings": [],
                                        "possible_parties": [],
                                        "dependency_options": [],
                                        "human_questions": [
                                            "Which option should the human inspect?"
                                        ],
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    runtime = DirectResponsesInvestigationRuntime(
        model="investigator-test",
        api_key="not-a-real-key",
        base_url="https://example.test/v1",
        prompt="immutable test prompt",
        client=client,
    )
    receipt = asyncio.run(
        run_receipted_investigation(
            session,
            candidate.id,
            runtime=runtime,
            identity=RuntimeIdentity(
                adapter="direct-responses-v2",
                adapter_contract_version=ADAPTER_CONTRACT_VERSION,
                model="investigator-test",
                prompt_version="evidence-investigator-test-v2",
                prompt_sha256=hashlib.sha256(b"immutable test prompt").hexdigest(),
                transport_gate_sha256=TRANSPORT_GATE["sha256"],
            ),
            budget=InvestigationBudget(),
        )
    )
    asyncio.run(client.aclose())

    assert requests == 7
    assert receipt.run.terminal_status == "human_judgment_needed"
    assert receipt.run.usage_json["input_tokens"] == 14_000


def test_direct_transport_repairs_a_paraphrase_within_the_repair_budget(
    session, project
):
    candidate, quote = _unplaced_statement(session, project)
    requests = 0
    evidence_ref = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests, evidence_ref
        requests += 1
        payload = json.loads(request.content)
        if evidence_ref is None:
            case = json.loads(payload["input"][0]["content"])
            [evidence_ref] = case["evidence_refs"]
        if requests == 1:
            output = [
                {
                    "type": "function_call",
                    "name": "read_candidate_evidence",
                    "call_id": "read-evidence",
                    "arguments": json.dumps(
                        {
                            "evidence_ref": evidence_ref,
                            "focus": "party and timing",
                            "max_chars": 500,
                        }
                    ),
                }
            ]
        else:
            if requests == 3:
                repair = payload["input"][-1]["content"]
                assert "copy exact_quote verbatim" in repair
            exact_quote = "Enterprise expects completion in June 2026."
            if requests == 3:
                exact_quote = quote
            output = [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "source_findings": [
                                        {
                                            "evidence_ref": evidence_ref,
                                            "exact_quote": exact_quote,
                                            "observation": "The source states timing.",
                                        }
                                    ],
                                    "possible_parties": [],
                                    "dependency_options": [],
                                    "human_questions": [
                                        "Which Dependency should the human inspect?"
                                    ],
                                }
                            ),
                        }
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 7_000, "output_tokens": 100},
                "output": output,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    runtime = DirectResponsesInvestigationRuntime(
        model="investigator-test",
        api_key="not-a-real-key",
        base_url="https://example.test/v1",
        prompt="immutable test prompt",
        client=client,
    )
    receipt = asyncio.run(
        run_receipted_investigation(
            session,
            candidate.id,
            runtime=runtime,
            identity=RuntimeIdentity(
                adapter="direct-responses-v2",
                adapter_contract_version=ADAPTER_CONTRACT_VERSION,
                model="investigator-test",
                prompt_version="evidence-investigator-test-v2",
                prompt_sha256=hashlib.sha256(b"immutable test prompt").hexdigest(),
                transport_gate_sha256=TRANSPORT_GATE["sha256"],
            ),
            budget=InvestigationBudget(),
        )
    )
    asyncio.run(client.aclose())

    assert requests == 3
    assert receipt.run.terminal_status == "human_judgment_needed"
    assert receipt.result.packet is not None
    assert receipt.run.usage_json["input_tokens"] == 21_000
    assert receipt.run.usage_json["repairs"] == 1


def test_prospective_shadow_freezes_before_review_and_associates_hidden_outcome(
    session, project
):
    case_count = session.query(EvidenceInvestigationShadowCase).count()
    execution_count = session.query(EvidenceInvestigationShadowExecution).count()
    outcome_count = session.query(EvidenceInvestigationShadowOutcome).count()
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
                turns=1,
                input_tokens=80,
                output_tokens=20,
            )

    identity = _identity(model="shadow-test", transport_gate_sha256="b" * 64)
    [shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=identity,
            budget=InvestigationBudget(),
        )
    )
    duplicate = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=identity,
            budget=InvestigationBudget(),
        )
    )

    assert duplicate == ()
    assert shadow.case.candidate_id == candidate.id
    assert shadow.execution.execution_status == "human_judgment_needed"
    assert session.query(EvidenceInvestigationShadowCase).count() == case_count + 1
    assert (
        session.query(EvidenceInvestigationShadowExecution).count()
        == execution_count + 1
    )
    session.refresh(candidate)
    assert candidate.state == "pending"

    observe_shadow_review(
        session, candidate.id, boundary="start", principal=RECORDER
    )
    mark_statement_not_relevant(
        session,
        candidate.id,
        reason="outside_project_scope",
        confirmed=True,
        principal=RECORDER,
    )
    observe_shadow_review(session, candidate.id, boundary="end", principal=RECORDER)
    outcome = capture_shadow_outcome(session, shadow.case.public_id)

    assert outcome.candidate_disposition == "not_relevant"
    assert outcome.scope_mode is None
    assert outcome.unresolved is False
    assert "not_relevant" in outcome.strata_json
    assert outcome.review_seconds is not None
    assert session.query(EvidenceInvestigationShadowOutcome).count() == outcome_count + 1


def test_shadow_records_stale_when_bound_state_changes_during_execution(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)

    class MutatingRuntime:
        async def run(self, case, tools, budget):
            candidate.payload_json = {
                **candidate.payload_json,
                "post_freeze_change": True,
            }
            session.flush([candidate])
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("Which state is current?",)),
                turns=1,
                input_tokens=20,
                output_tokens=10,
            )

    [shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=MutatingRuntime,
            identity=_identity(model="stale-test", transport_gate_sha256="d" * 64),
            budget=InvestigationBudget(),
        )
    )

    assert shadow.investigation.run.reason == "stale_input"
    assert shadow.execution.execution_status == "stale"
    assert shadow.investigation.result.packet is None


def test_one_human_outcome_cannot_be_counted_by_two_shadow_cases(session, project):
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
                turns=1,
                input_tokens=20,
                output_tokens=10,
            )

    cases = []
    for model in ("duplicate-label-a", "duplicate-label-b"):
        [shadow] = asyncio.run(
            run_shadow_batch(
                session,
                project.id,
                runtime_factory=StubRuntime,
                identity=_identity(model=model, transport_gate_sha256="e" * 64),
                budget=InvestigationBudget(),
            )
        )
        cases.append(shadow)
    mark_statement_not_relevant(
        session,
        candidate.id,
        reason="outside_project_scope",
        confirmed=True,
        principal=RECORDER,
    )
    capture_shadow_outcome(session, cases[0].case.public_id)

    with pytest.raises(
        ValueError, match="human outcome already captured for another shadow case"
    ):
        capture_shadow_outcome(session, cases[1].case.public_id)


def test_v2_cohort_freezes_an_explicit_hidden_reproducible_manifest(
    session, project, tmp_path
):
    outcome_count = session.query(EvidenceInvestigationShadowOutcome).count()
    first, _quote = _unplaced_statement(session, project)
    second, _quote = _unplaced_statement(
        session, project, quote="CenterPoint needs a human scope decision in May 2027."
    )

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("What is supported?",)),
                turns=1,
                input_tokens=10,
                output_tokens=10,
            )

    cohort = asyncio.run(
        run_v2_shadow_cohort(
            session,
            project.id,
            candidate_ids=(second.id, first.id),
            selection_rule="operator-declared:two-current-unplaced-statements",
            runtime_factory=StubRuntime,
            identity=configured_runtime_identity("cohort-test"),
            budget=InvestigationBudget(),
        )
    )
    path = tmp_path / "cohort.json"
    write_shadow_cohort_manifest(cohort, path)
    manifest = json.loads(path.read_text())

    assert manifest["project"] == {"id": project.id, "slug": project.slug}
    assert manifest["candidate_ids"] == [second.id, first.id]
    assert manifest["selection_rule"].startswith("operator-declared:")
    assert manifest["prompt_version"] == "evidence-investigator-v2"
    assert manifest["prompt_sha256"] == PROMPT_SHA256
    assert manifest["tool_contract_version"] == "evidence-investigator-tools-v2"
    assert manifest["adapter_contract_version"] == ADAPTER_CONTRACT_VERSION
    assert len(manifest["read_fingerprints"]) == 2
    assert len(manifest["dataset_membership"]) == 2
    assert manifest["execution_summary"] == {"validated_packet": 2}
    assert len(manifest["manifest_sha256"]) == 64
    assert all(item["hidden"] for item in manifest["dataset_membership"])
    assert session.query(EvidenceInvestigationShadowOutcome).count() == outcome_count
    with pytest.raises(FileExistsError):
        write_shadow_cohort_manifest(cohort, path)


def test_v2_cohort_refuses_implicit_cross_project_or_mixed_configuration(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)
    other = Project(slug="other-v2-cohort", name="Other v2 cohort", is_synthetic=True)
    session.add(other)
    session.flush([other])

    class StubRuntime:
        async def run(self, case, tools, budget):
            raise AssertionError("refusal must happen before runtime")

    with pytest.raises(ValueError, match="current Unplaced Statement"):
        asyncio.run(
            run_v2_shadow_cohort(
                session,
                other.id,
                candidate_ids=(candidate.id,),
                selection_rule="operator-declared:foreign",
                runtime_factory=StubRuntime,
                identity=configured_runtime_identity("cohort-test"),
                budget=InvestigationBudget(),
            )
        )
    mixed = _identity(model="cohort-test")
    with pytest.raises(ValueError, match="sealed Evidence Investigator v2"):
        asyncio.run(
            run_v2_shadow_cohort(
                session,
                project.id,
                candidate_ids=(candidate.id,),
                selection_rule="operator-declared:mixed",
                runtime_factory=StubRuntime,
                identity=mixed,
                budget=InvestigationBudget(),
            )
        )


def test_v2_cohort_refuses_a_candidate_with_an_earlier_human_outcome(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)

    class FirstRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("What is supported?",)),
                turns=1,
                input_tokens=10,
                output_tokens=10,
            )

    [earlier] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            candidate_ids=(candidate.id,),
            runtime_factory=FirstRuntime,
            identity=_identity(model="earlier-shadow"),
            budget=InvestigationBudget(),
        )
    )
    capture_shadow_outcome(session, earlier.case.public_id)

    class ForbiddenRuntime:
        async def run(self, case, tools, budget):
            raise AssertionError("contaminated case reached the runtime")

    with pytest.raises(ValueError, match="before any prior human review or outcome"):
        asyncio.run(
            run_v2_shadow_cohort(
                session,
                project.id,
                candidate_ids=(candidate.id,),
                selection_rule="operator-declared:contaminated",
                runtime_factory=ForbiddenRuntime,
                identity=configured_runtime_identity("cohort-test"),
                budget=InvestigationBudget(),
            )
        )


def test_v2_cohort_refuses_a_candidate_reviewed_before_its_shadow_freeze(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)
    observe_shadow_review(session, candidate.id, boundary="start", principal=RECORDER)

    class ForbiddenRuntime:
        async def run(self, case, tools, budget):
            raise AssertionError("previously reviewed case reached the runtime")

    with pytest.raises(ValueError, match="before any prior human review or outcome"):
        asyncio.run(
            run_v2_shadow_cohort(
                session,
                project.id,
                candidate_ids=(candidate.id,),
                selection_rule="operator-declared:reviewed-before-freeze",
                runtime_factory=ForbiddenRuntime,
                identity=configured_runtime_identity("cohort-test"),
                budget=InvestigationBudget(),
            )
        )


def test_shadow_capture_refuses_counting_one_human_outcome_twice_for_one_candidate(
    session, project
):
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
                turns=1,
                input_tokens=80,
                output_tokens=20,
            )

    first_identity = _identity(model="shadow-test", transport_gate_sha256="d" * 64)
    [first_shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=first_identity,
            budget=InvestigationBudget(),
        )
    )
    candidate.payload_json = {
        **candidate.payload_json,
        "fields": {
            **candidate.payload_json["fields"],
            "description": "Reworded proposal after the hidden run",
        },
    }
    attributes.flag_modified(candidate, "payload_json")
    second_identity = _identity(
        model="shadow-test",
        prompt_version="evidence-investigator-test-v2",
        prompt_sha256="2" * 64,
        transport_gate_sha256="e" * 64,
    )
    [second_shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=second_identity,
            budget=InvestigationBudget(),
        )
    )

    mark_statement_not_relevant(
        session,
        candidate.id,
        reason="outside_project_scope",
        confirmed=True,
        principal=RECORDER,
    )

    first_outcome = capture_shadow_outcome(session, first_shadow.case.public_id)

    assert first_outcome.candidate_disposition == "not_relevant"
    with pytest.raises(
        ValueError,
        match="human outcome already captured for another shadow case",
    ):
        capture_shadow_outcome(session, second_shadow.case.public_id)


def test_shadow_evaluation_writes_reproducible_machine_and_human_receipts(
    session, project, tmp_path
):
    evaluation_count = session.query(EvidenceInvestigationEvaluationReceipt).count()
    candidate, _quote = _unplaced_statement(session, project)

    class StubRuntime:
        async def run(self, case, tools, budget):
            return InvestigationRunOutput(
                packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
                turns=1,
                input_tokens=60,
                output_tokens=15,
            )

    identity = _identity(model="evaluation-test", transport_gate_sha256="c" * 64)
    [shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=identity,
            budget=InvestigationBudget(),
        )
    )
    mark_statement_not_relevant(
        session,
        candidate.id,
        reason="outside_project_scope",
        confirmed=True,
        principal=RECORDER,
    )
    capture_shadow_outcome(session, shadow.case.public_id)
    scores = {
        shadow.case.public_id: {
            "packet_usefulness": 3,
            "correct_abstention": None,
            "misleading_ranking": False,
            "review_effort": "bounded",
        }
    }
    evaluation = evaluate_shadow_runs(
        session,
        [shadow.investigation.run.public_id],
        rules=EvaluationRules(min_cases=1, required_strata=("not_relevant",)),
        human_scores=scores,
        output_dir=tmp_path,
    )

    assert evaluation.receipt.status == "passed"
    assert evaluation.receipt.gates_json["schema_valid"] is True
    assert evaluation.receipt.gates_json["citation_valid"] is True
    assert evaluation.receipt.identity_json["ui_enabled"] is False
    assert json.loads(evaluation.machine_path.read_text())["status"] == "passed"
    assert "No coordinator UI was enabled" in evaluation.summary_path.read_text()
    assert (
        session.query(EvidenceInvestigationEvaluationReceipt).count()
        == evaluation_count + 1
    )

    insufficient = evaluate_shadow_runs(
        session,
        [shadow.investigation.run.public_id],
        rules=EvaluationRules(min_cases=2, required_strata=("not_relevant",)),
        human_scores=scores,
        output_dir=tmp_path / "insufficient",
    )
    assert insufficient.receipt.status == "insufficient"
    assert "sample_size" in insufficient.receipt.limitations_json
    assert verify_evaluation_receipt(evaluation.receipt) is True

    original_status = shadow.execution.execution_status
    shadow.execution.execution_status = "stale"
    with session.no_autoflush:
        stale = evaluate_shadow_runs(
            session,
            [shadow.investigation.run.public_id],
            rules=EvaluationRules(min_cases=1, required_strata=("not_relevant",)),
            human_scores=scores,
            output_dir=tmp_path / "stale",
        )
    attributes.set_committed_value(
        shadow.execution, "execution_status", original_status
    )
    assert stale.receipt.status == "failed"
    assert stale.receipt.gates_json["zero_stale_packets"] is False

    original_case = shadow.case.case_json
    shadow.case.case_json = {**original_case, "later_human_outcome": "not_relevant"}
    with session.no_autoflush, pytest.raises(EvaluationRefusal, match="later human answer"):
        evaluate_shadow_runs(
            session,
            [shadow.investigation.run.public_id],
            rules=EvaluationRules(min_cases=1, required_strata=("not_relevant",)),
            human_scores=scores,
            output_dir=tmp_path / "contaminated",
        )
    attributes.set_committed_value(shadow.case, "case_json", original_case)

    original_summary = evaluation.receipt.summary_markdown
    evaluation.receipt.summary_markdown = original_summary + "tampered"
    assert verify_evaluation_receipt(evaluation.receipt) is False
    attributes.set_committed_value(
        evaluation.receipt, "summary_markdown", original_summary
    )

    with pytest.raises(EvaluationRefusal, match="required human score fields"):
        evaluate_shadow_runs(
            session,
            [shadow.investigation.run.public_id],
            rules=EvaluationRules(min_cases=1, required_strata=("not_relevant",)),
            human_scores={shadow.case.public_id: {"packet_usefulness": 3}},
            output_dir=tmp_path / "partial-human-score",
        )
