"""Bounded transport and append-only receipts for Evidence Investigation.

The domain investigation contract remains in :mod:`evidence_investigator`.
This module owns transport policy and durable terminal receipts; it receives
no writer other than the three receipt tables below.

The earlier one-shot ``StructuredClient`` could not run a tool loop or retain
terminal attempt metadata. This separate direct Responses adapter preserves
that extraction seam while making every investigation attempt locally legible.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from sqlalchemy.orm import Session

from corridor.evidence_investigator import (
    TOOL_CONTRACT_VERSION,
    INVESTIGATION_PACKET_SCHEMA,
    DependencyOption,
    EvidenceFact,
    InvestigationAbstention,
    InvestigationBudget,
    InvestigationPacket,
    InvestigationResult,
    InvestigationRunOutput,
    InvestigationRuntime,
    InvestigationRuntimeAbstention,
    InvestigationStep,
    PreparedInvestigation,
    PossibleParty,
    SourceFinding,
    investigate_candidate,
)
from corridor.models import (
    Candidate,
    EvidenceInvestigationPacketReceipt,
    EvidenceInvestigationRun,
    EvidenceInvestigationStepReceipt,
)

PROMPT_VERSION = "evidence-investigator-v1"
PROMPT_PATH = Path("prompts/evidence_investigator_v1.md")
ADAPTER = "direct-responses-v1"


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_json(value: object) -> str:
    """One canonical JSON digest shared by every investigator receipt layer."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def transport_gate_receipt() -> dict:
    """Synthetic, content-free proof of the direct Responses wire contract."""
    contract = {
        "adapter": ADAPTER,
        "store": False,
        "strict_tools": True,
        "strict_final_schema": True,
        "parallel_tool_calls": False,
        "raw_remote_tracing": False,
        "sessions": False,
        "handoffs": False,
        "hosted_tools": False,
        "serial_execution": True,
        "budget_fields": [
            "max_turns",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
            "timeout_seconds",
            "max_retries",
            "max_repairs",
        ],
    }
    return {**contract, "sha256": sha256_json(contract)}


TRANSPORT_GATE = transport_gate_receipt()


@dataclass(frozen=True)
class RuntimeIdentity:
    adapter: str
    model: str
    prompt_version: str
    transport_gate_sha256: str

    def __post_init__(self) -> None:
        if len(self.transport_gate_sha256) != 64:
            raise ValueError("transport gate identity must be a SHA-256 digest")
        if not all((self.adapter, self.model, self.prompt_version)):
            raise ValueError("runtime identity fields must be non-empty")


@dataclass(frozen=True)
class ReceiptedInvestigation:
    run: EvidenceInvestigationRun
    result: InvestigationResult | InvestigationAbstention


class _ObservedRuntime:
    def __init__(self, runtime: InvestigationRuntime) -> None:
        self.runtime = runtime
        self.output = None

    async def run(self, case, tools, budget):
        self.output = await self.runtime.run(case, tools, budget)
        return self.output


class DirectResponsesInvestigationRuntime:
    """One serial tool-calling agent over the stateless Responses API."""

    transport_gate_sha256 = TRANSPORT_GATE["sha256"]

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        prompt: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for Evidence Investigation")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.prompt = prompt if prompt is not None else PROMPT_PATH.read_text()
        self._client = client

    async def run(self, case, tools, budget):
        input_items: list[dict] = [
            {
                "role": "user",
                "content": json.dumps(asdict(case), sort_keys=True),
            }
        ]
        steps: list[InvestigationStep] = []
        input_tokens = output_tokens = repairs = 0
        tool_calls = 0
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=budget.timeout_seconds)
        try:
            for turn in range(1, budget.max_turns + 1):
                allow_tools = (
                    tool_calls < budget.max_tool_calls
                    and turn <= budget.max_turns - budget.max_repairs - 1
                )
                payload = self._payload(
                    input_items, tools, budget, allow_tools=allow_tools
                )
                started = time.monotonic()
                response = await client.post(
                    f"{self.base_url}/responses",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                response.raise_for_status()
                body = response.json()
                usage = body.get("usage") or {}
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
                output = body.get("output") or []
                calls = [item for item in output if item.get("type") == "function_call"]
                if calls:
                    if len(calls) != 1:
                        raise RuntimeError("parallel tool calls are forbidden")
                    call = calls[0]
                    tool_calls += 1
                    arguments = json.loads(call.get("arguments") or "{}")
                    try:
                        result, step = await self._invoke_tool(
                            tools,
                            call.get("name", ""),
                            arguments,
                            elapsed_ms,
                            {
                                "input_tokens": int(usage.get("input_tokens", 0)),
                                "output_tokens": int(usage.get("output_tokens", 0)),
                            },
                        )
                    except (ValueError, TypeError) as exc:
                        steps.append(
                            InvestigationStep(
                                step_type="tool",
                                name=call.get("name", "unknown"),
                                opaque_references=tuple(
                                    sorted(set(_opaque_refs(arguments)))
                                ),
                                normalized_arguments=json.loads(
                                    json.dumps(arguments)
                                ),
                                result_summary={
                                    "status": "refused",
                                    "error_type": type(exc).__name__,
                                },
                                usage={
                                    "input_tokens": int(
                                        usage.get("input_tokens", 0)
                                    ),
                                    "output_tokens": int(
                                        usage.get("output_tokens", 0)
                                    ),
                                },
                                elapsed_ms=elapsed_ms,
                                request_sha256=sha256_json(arguments),
                                result_sha256=sha256_json(
                                    {
                                        "status": "refused",
                                        "error_type": type(exc).__name__,
                                    }
                                ),
                            )
                        )
                        return InvestigationRuntimeAbstention(
                            reason="authority_gap",
                            detail=(
                                "A requested reference or capability was not issued "
                                "for this bound case."
                            ),
                            turns=turn,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            repairs=repairs,
                            steps=tuple(steps),
                        )
                    steps.append(step)
                    input_items.extend(output)
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.get("call_id"),
                            "output": json.dumps(result, sort_keys=True),
                        }
                    )
                    continue
                packet_data = _output_json(body)
                packet = _packet_from_json(packet_data)
                validation_error = tools.validate_packet(packet)
                if validation_error:
                    steps.append(
                        InvestigationStep(
                            step_type="validation",
                            name="packet_validator",
                            opaque_references=tuple(
                                sorted(set(_opaque_refs(packet_data)))
                            ),
                            normalized_arguments={},
                            result_summary={
                                "status": "repairable"
                                if repairs < budget.max_repairs
                                else "refused",
                                "error": validation_error,
                            },
                            usage={
                                "input_tokens": int(usage.get("input_tokens", 0)),
                                "output_tokens": int(usage.get("output_tokens", 0)),
                            },
                            elapsed_ms=elapsed_ms,
                            request_sha256=sha256_json(payload),
                            result_sha256=sha256_json(packet_data),
                        )
                    )
                    if repairs < budget.max_repairs:
                        repairs += 1
                        input_items.extend(output)
                        input_items.append(
                            {
                                "role": "user",
                                "content": (
                                    "Repair the packet without adding authority or new references. "
                                    "For every Evidence fact, copy exact_quote verbatim from a "
                                    "candidate_quote, page_context, or exact_quote returned by a "
                                    "bound read tool; otherwise remove that fact or option. "
                                    f"Validator error: {validation_error}"
                                ),
                            }
                        )
                        continue
                    return InvestigationRuntimeAbstention(
                        reason="validation_failure",
                        detail="The repaired packet failed deterministic validation.",
                        turns=turn,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        repairs=repairs,
                        steps=tuple(steps),
                    )
                steps.append(
                    InvestigationStep(
                        step_type="model",
                        name="final_packet",
                        opaque_references=tuple(sorted(set(_opaque_refs(packet_data)))),
                        normalized_arguments={},
                        result_summary={"status": "packet", "field_count": len(packet_data)},
                        usage={
                            "input_tokens": int(usage.get("input_tokens", 0)),
                            "output_tokens": int(usage.get("output_tokens", 0)),
                        },
                        elapsed_ms=elapsed_ms,
                        request_sha256=sha256_json(payload),
                        result_sha256=sha256_json(packet_data),
                    )
                )
                return InvestigationRunOutput(
                    packet=packet,
                    turns=turn,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    repairs=repairs,
                    steps=tuple(steps),
                )
            return InvestigationRuntimeAbstention(
                reason="budget_exhaustion",
                detail="The bounded runtime exhausted its turn budget without a packet.",
                turns=budget.max_turns,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                repairs=repairs,
                steps=tuple(steps),
            )
        except Exception as exc:  # noqa: BLE001 - becomes a terminal receipt
            return InvestigationRuntimeAbstention(
                reason="runtime_failure",
                detail=f"{type(exc).__name__}: {exc}",
                turns=min(budget.max_turns, max(1, len(steps) + 1)),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                repairs=repairs,
                steps=tuple(steps),
            )
        finally:
            if own_client:
                await client.aclose()

    def _payload(self, input_items, tools, budget, *, allow_tools: bool) -> dict:
        return {
            "model": self.model,
            "instructions": self.prompt,
            "input": input_items,
            "tools": [
                {
                    "type": "function",
                    "name": name,
                    "description": f"Bound read-only Corridor capability: {name}",
                    "parameters": schema,
                    "strict": True,
                }
                for name, schema in tools.schemas.items()
            ],
            "tool_choice": "auto" if allow_tools else "none",
            "parallel_tool_calls": False,
            "max_tool_calls": budget.max_tool_calls,
            "max_output_tokens": budget.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "evidence_investigation_packet",
                    "strict": True,
                    "schema": INVESTIGATION_PACKET_SCHEMA,
                }
            },
            "store": False,
        }

    async def _invoke_tool(
        self, tools, name: str, arguments: dict, elapsed_ms: int, usage: dict
    ):
        if name not in tools.schemas:
            raise ValueError("the model requested an unavailable capability")
        method = getattr(tools, name)
        signature = inspect.signature(method)
        if "terms" in arguments:
            arguments["terms"] = tuple(arguments["terms"])
        bound = signature.bind(**arguments)
        result_object = await method(*bound.args, **bound.kwargs)
        result = asdict(result_object)
        summary = _result_summary(result)
        step = InvestigationStep(
            step_type="tool",
            name=name,
            opaque_references=tuple(sorted(set(_opaque_refs({"arguments": arguments, "result": result})))),
            normalized_arguments=json.loads(json.dumps(arguments)),
            result_summary=summary,
            usage=usage,
            elapsed_ms=elapsed_ms,
            request_sha256=sha256_json(arguments),
            result_sha256=sha256_json(result),
        )
        return result, step


def _output_json(body: dict) -> dict:
    if body.get("status") == "incomplete":
        raise RuntimeError("the model response was incomplete")
    message = next(
        (item for item in body.get("output") or [] if item.get("type") == "message"),
        None,
    )
    if message is None:
        raise RuntimeError("the model returned no final message")
    content = message.get("content") or []
    refusal = next((item for item in content if item.get("type") == "refusal"), None)
    if refusal is not None:
        raise RuntimeError("the model refused the bounded investigation")
    part = next((item for item in content if item.get("type") == "output_text"), None)
    if part is None:
        raise RuntimeError("the model returned no structured output")
    return json.loads(part.get("text") or "{}")


def _fact(value: dict) -> EvidenceFact:
    return EvidenceFact(value["evidence_ref"], value["exact_quote"])


def _packet_from_json(value: dict) -> InvestigationPacket:
    return InvestigationPacket(
        source_findings=tuple(SourceFinding(**item) for item in value["source_findings"]),
        possible_parties=tuple(
            PossibleParty(
                party_ref=item["party_ref"],
                supporting_facts=tuple(_fact(fact) for fact in item["supporting_facts"]),
                contradicting_facts=tuple(_fact(fact) for fact in item["contradicting_facts"]),
            )
            for item in value["possible_parties"]
        ),
        dependency_options=tuple(
            DependencyOption(
                dependency_ref=item["dependency_ref"],
                rank=item["rank"],
                supporting_facts=tuple(_fact(fact) for fact in item["supporting_facts"]),
                contradicting_facts=tuple(_fact(fact) for fact in item["contradicting_facts"]),
            )
            for item in value["dependency_options"]
        ),
        human_questions=tuple(value["human_questions"]),
    )


def _opaque_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_opaque_refs(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_opaque_refs(item))
    elif isinstance(value, str) and re.fullmatch(r"[EPDS][A-Za-z0-9_-]+", value):
        refs.append(value)
    return refs


def _result_summary(value: dict) -> dict:
    summary: dict[str, object] = {"status": "ok"}
    for key, item in value.items():
        if isinstance(item, (list, tuple)):
            summary[f"{key}_count"] = len(item)
    refs = sorted(set(_opaque_refs(value)))
    if refs:
        summary["issued_references"] = refs
    return summary


async def run_receipted_investigation(
    session: Session,
    candidate_id: int,
    *,
    runtime: InvestigationRuntime,
    identity: RuntimeIdentity,
    budget: InvestigationBudget,
    prepared: PreparedInvestigation | None = None,
) -> ReceiptedInvestigation:
    """Run exactly one eligible Candidate and append its terminal receipt."""
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate does not exist; no receipt identity can be bound")
    payload_sha256 = sha256_json(candidate.payload_json)
    started_at = datetime.now(timezone.utc)
    observed = _ObservedRuntime(runtime)
    runtime_gate = getattr(runtime, "transport_gate_sha256", None)
    if runtime_gate is not None and runtime_gate != identity.transport_gate_sha256:
        raise ValueError("runtime does not match the synthetic transport gate")
    result = await investigate_candidate(
        session,
        candidate_id,
        runtime=observed,
        budget=budget,
        prepared=prepared,
    )
    completed_at = datetime.now(timezone.utc)
    output = observed.output
    usage = {
        "turns": getattr(output, "turns", 0),
        "input_tokens": getattr(output, "input_tokens", 0),
        "output_tokens": getattr(output, "output_tokens", 0),
        "retries": getattr(output, "retries", 0),
        "repairs": getattr(output, "repairs", 0),
    }
    run = EvidenceInvestigationRun(
        public_id=str(uuid.uuid4()),
        project_id=candidate.project_id,
        candidate_id=candidate.id,
        extraction_run_id=candidate.extraction_run_id,
        terminal_status=result.status,
        reason=getattr(result, "reason", None),
        detail=getattr(result, "detail", None),
        adapter=identity.adapter,
        model=identity.model,
        prompt_version=identity.prompt_version,
        tool_contract_version=TOOL_CONTRACT_VERSION,
        validator_version=result.validator_version,
        transport_gate_sha256=identity.transport_gate_sha256,
        candidate_payload_sha256=payload_sha256,
        read_fingerprint=result.read_fingerprint,
        budget_json=asdict(budget),
        usage_json=usage,
        started_at=started_at,
        completed_at=completed_at,
    )
    session.add(run)
    session.flush([run])
    if result.packet is not None:
        packet = asdict(result.packet)
        session.add(
            EvidenceInvestigationPacketReceipt(
                run_id=run.id,
                packet_json=packet,
                validator_outcome="valid",
                packet_sha256=sha256_json(packet),
                non_authoritative=True,
            )
        )
        session.flush()
    for ordinal, step in enumerate(getattr(output, "steps", ()), start=1):
        session.add(
            EvidenceInvestigationStepReceipt(
                run_id=run.id,
                ordinal=ordinal,
                step_type=step.step_type,
                name=step.name,
                opaque_references_json=list(step.opaque_references),
                normalized_arguments_json=step.normalized_arguments,
                result_summary_json=step.result_summary,
                usage_json=step.usage,
                elapsed_ms=step.elapsed_ms,
                request_sha256=step.request_sha256,
                result_sha256=step.result_sha256,
            )
        )
    session.flush()
    return ReceiptedInvestigation(run=run, result=result)
