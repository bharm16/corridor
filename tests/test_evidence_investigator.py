"""Public Evidence Investigator behavior for one Unplaced Statement."""

import asyncio
import hashlib
from dataclasses import fields as dataclass_fields
from datetime import date

import pytest
from sqlalchemy import delete

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
    ExternalOrg,
    PolicyRun,
    Project,
)
from corridor.principals import HumanPrincipal

RECORDER = HumanPrincipal("local:evidence-investigator-test")


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


def _unplaced_statement(session, project, *, quote=None, kind="event"):
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
            text=quote,
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
        UsageRuntime(turns=5),
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
