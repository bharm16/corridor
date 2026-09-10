"""Build one frozen Evidence Investigator case and the human acts that follow it.

The human-outcome reading and the two records written from it (the one-time
shadow outcome and the cutoff-correct capture result) are proved against the
same fixtures: a frozen case, a Not Relevant decision at a chosen instant, a
real guided Save with a chosen Commitment Scope, a compensating reversal at a
chosen instant. Both test files used to carry their own copies of these
builders; one module keeps the reading and each writer proved on the same
fixture.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
from uuid import uuid4

from corridor.evidence_investigator import (
    InvestigationBudget,
    InvestigationPacket,
    InvestigationRunOutput,
)
from corridor.evidence_investigator_runtime import RuntimeIdentity
from corridor.evidence_investigator_shadow import run_shadow_batch
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
)
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import (
    AuditLog,
    Candidate,
    CandidateDisposition,
    Dependency,
    DocPage,
    Document,
    EventAdmissionOutcome,
    EvidenceInvestigationShadowCase,
    ExternalOrg,
    PolicyRun,
    ProjectRosterEntry,
    StatementCoordinationReversal,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import (
    StatementCoordinationDraft,
    coordinate_statement,
)


RECORDER = HumanPrincipal("local:capture-test")
HOUR = timedelta(hours=1)


def identity(**overrides) -> RuntimeIdentity:
    values = {
        "adapter": "capture-contract-v1",
        "adapter_contract_version": "capture-contract-v1",
        "model": "capture-investigator",
        "prompt_version": "capture-prompt-v1",
        "prompt_sha256": "1" * 64,
        "transport_gate_sha256": "a" * 64,
    }
    values.update(overrides)
    return RuntimeIdentity(**values)


class StubRuntime:
    async def run(self, case, tools, budget):
        return InvestigationRunOutput(
            packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
            turns=1,
            input_tokens=20,
            output_tokens=10,
        )


def unplaced_statement(session, project, *, quote=None) -> Candidate:
    quote = quote or (
        "Kinder Morgan expects the relocation to finish near Station 6609+00 "
        "during June 2026."
    )
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{project.id}:{quote}:{uuid4()}".encode()).hexdigest(),
        filename="minutes/capture.pdf",
        doc_type="minutes",
        doc_date=date(2026, 5, 4),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
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
                {"document_id": document.id, "page": 1, "quote": quote, "verified": True}
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
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    admission = PolicyRun(
        project_id=project.id,
        family="event-admission",
        policy_approval_id=None,
        policy_version="capture-test",
        policy_sha256="a" * 64,
        abstention_reason_version="capture-test",
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
    return candidate


def freeze_case(session, project, *, identity_override=None) -> EvidenceInvestigationShadowCase:
    """Freeze one current Unplaced Statement as a shadow case with a stub run."""
    runtime_identity = identity_override or identity(model=f"shadow-{uuid4().hex[:8]}")
    unplaced_statement(session, project)
    [shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=StubRuntime,
            identity=runtime_identity,
            budget=InvestigationBudget(),
        )
    )
    return shadow.case


def record_disposition(
    session, candidate_id, disposition, *, created_at, reason=None
) -> CandidateDisposition:
    row = CandidateDisposition(
        candidate_id=candidate_id,
        disposition=disposition,
        reason=reason if disposition == "not_relevant" else None,
        recorded_by="local:capture-test",
        created_at=created_at,
    )
    session.add(row)
    session.flush([row])
    return row


def reverse(
    session,
    candidate_id,
    *,
    created_at,
    disposition_id=None,
    receipt_id=None,
) -> StatementCoordinationReversal:
    """Append the compensating act for one disposition or one grouped Save."""
    audit = AuditLog(
        actor="local:capture-test",
        action="undo_disposition" if receipt_id is None else "undo_coordinated_statement",
        entity_type="candidate",
        entity_id=candidate_id,
    )
    session.add(audit)
    session.flush([audit])
    reversal = StatementCoordinationReversal(
        candidate_disposition_id=disposition_id,
        receipt_id=receipt_id,
        candidate_id=candidate_id,
        audit_log_id=audit.id,
        recorded_by="local:capture-test",
        created_at=created_at,
    )
    session.add(reversal)
    session.flush([reversal])
    return reversal


def accepted_case_with_scope(session, project, *, scope=None):
    """A frozen case whose candidate is accepted through the real guided Save.

    Returns the case, the one Constraint the party has, the guided Save result,
    and the document the statement cites, so a later factual correction can cite
    the same page.
    """

    party = ExternalOrg(name="Kinder Morgan")
    session.add(party)
    roster = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:dana",
        display_name="Dana Fields",
    )
    session.add(roster)
    session.flush()
    quote = "Kinder Morgan will provide the relocation schedule by June 1, 2026."
    corrected_quote = "Kinder Morgan will provide the relocation schedule by July 1, 2026."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{project.id}:acc:{uuid4()}".encode()).hexdigest(),
        filename="accepted.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(document_id=document.id, page_no=1, text=f"{quote}\n{corrected_quote}")
    )
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {"event_type": "commitment", "description": quote},
            "citations": [
                {"document_id": document.id, "page": 1, "quote": quote, "verified": True}
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="capture-accepted",
        model="capture-accepted",
        citations_verified=True,
    )
    run = record_extraction_run(
        session, document, prompt_version="capture-accepted", candidate_count=1,
        page_errors=0, candidates=[candidate], model="capture-accepted",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    dependency = Dependency(
        project_id=project.id, ref_code=f"DEP-{uuid4().hex[:8]}",
        dep_type="utility_relocation", title="KM line",
        station_from="6608+70", station_to="6616+50", external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    frozen_at = datetime.now(timezone.utc) - (6 * HOUR)
    case = EvidenceInvestigationShadowCase(
        public_id=str(uuid4()),
        project_id=project.id,
        candidate_id=candidate.id,
        extraction_run_id=candidate.extraction_run_id,
        candidate_payload_sha256="b" * 64,
        read_fingerprint="c" * 64,
        model="capture-accepted",
        prompt_version="capture-accepted",
        prompt_sha256="1" * 64,
        adapter_contract_version="capture-contract-v1",
        tool_contract_version="tool-contract-v1",
        transport_gate_sha256="a" * 64,
        budget_json={},
        case_json={},
        registered_evidence_json=[],
        option_population_json={},
        option_population_sha256="d" * 64,
        frozen_at=frozen_at,
    )
    session.add(case)
    session.flush([case])
    draft = StatementCoordinationDraft(
        candidate_id=candidate.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        event_date=date(2025, 1, 16),
        description=quote,
        new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
        previous_timing=None,
        evidence=(CitedStatementEvidence(document.id, 1, quote),),
        scope=scope if scope is not None else StatementScope.selected((dependency.id,)),
        internal_owner_roster_entry_id=roster.id,
        next_action="Confirm the revised completion plan with Kinder Morgan",
        action_due_date=date(2026, 2, 1),
        action_due_date_unknown_reason=None,
        milestone_impact=None,
    )
    result = coordinate_statement(session, draft, principal=RECORDER)
    return case, dependency, result, document
