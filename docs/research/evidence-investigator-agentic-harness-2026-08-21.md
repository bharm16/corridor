# Evidence Investigator into an Agentic Harness

Researched on August 21, 2026.

Question: how should Corridor build the read-only Evidence Investigator into an agentic harness without weakening the current provenance-first, human-gated statement flow?

Decision objective: choose the smallest harness that materially improves coordinator throughput while preserving Corridor's current authority, stale-write, and provenance invariants.

## Recommendation

Build the Evidence Investigator as one read-only agent behind a Corridor-owned interface. Use the OpenAI Agents SDK as the first runtime adapter if a transport spike proves that it preserves Corridor's required `store: false`, strict-schema, tracing-off, and budget behavior; otherwise implement the same interface with a direct Responses adapter. Do not introduce a multi-agent runtime.

Scope the first version narrowly:

- only `WorkItem(kind="candidate")` / `unplaced_statement` investigation;
- no coordination-plan authoring;
- no Internal Owner / Next Action / Milestone Impact / Ready suggestions;
- no user-facing production rollout until project-scoped auth and membership exist.

Reason:

- Corridor already has the right domain split: candidate/read models on one side, atomic statement/work-decision writers on the other. `work_list` is explicitly the coordinator read seam for statement work outside the Ledger, while `coordinate_statement()` is the grouped human save that writes statement, scope, disposition, and plan decisions atomically (`src/corridor/work_list.py:1-12,646-728`; `src/corridor/statement_coordination.py:1-12,184-325`).
- Corridor already owns a deliberately small Responses client with strict JSON schema, explicit `reasoning.effort`, and `store: false` (`src/corridor/llm.py:1-36,102-182`).
- The repo does not currently depend on the OpenAI SDK or Agents SDK; `pyproject.toml` only declares `httpx` plus the app/runtime stack (`pyproject.toml:6-22`).
- Corridor's authority model is explicit: a software agent may operate the interface only as the instrument of a named human's already-made choice; it may not originate a human-gated write under that human's identity (ADR-0041, `docs/adr/0041-interface-operation-is-not-human-decision-authorship.md:1-7`).

So the first harness should be:

1. one investigator module;
2. one bounded model/tool loop;
3. a small allowlisted set of read-only tools;
4. a strict structured output;
5. a deterministic validator;
6. a visually separate human decision that still calls the existing writer and never consumes the packet as a write payload.

## Runtime decision: isolate the framework, then prove the transport

The current official OpenAI docs establish the available surfaces:

- The Responses API supplies strict function calling and response continuation ([Function calling](https://developers.openai.com/api/docs/guides/function-calling), [Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)).
- The Agents SDK owns the repeated model/tool loop, typed outputs, local run context, guardrails, and result state ([Agent definitions](https://developers.openai.com/api/docs/guides/agents/define-agents), [Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents), [Results and state](https://developers.openai.com/api/docs/guides/agents/results)).
- OpenAI's current guidance says to start with one focused agent and add agents only when separate ownership, tools, or approval policies require them ([Agent definitions](https://developers.openai.com/api/docs/guides/agents/define-agents)).
- Built-in SDK tracing is enabled in the normal server-side path, so real project Evidence requires an explicit tracing/privacy decision rather than accepting defaults ([Integrations and observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)).

Corridor's current `StructuredClient` is not an agent runtime. It performs one strict completion and has no function-call execution or reasoning-item continuation (`src/corridor/llm.py:80-89,158-263`). Extending it directly would therefore build a second loop inside the extraction client, not merely reuse an existing one.

Corridor's bottleneck is not missing runtime orchestration. It is missing a bounded read-only investigator that can:

- gather the right evidence packet,
- rank plausible scope targets,
- surface contradictions,
- prepare a cited option packet,
- and stop before the write boundary.

The domain module must not care which transport owns the loop. Put the runtime behind a narrow interface, and run a synthetic transport spike before real Evidence is sent. The spike must prove:

- every request has `store: false`;
- every tool and final output uses strict schemas;
- remote raw-content tracing is disabled for real project runs;
- parallel tool calls are disabled because the local read repository is stateful;
- turn, tool-call, token, timeout, and retry limits fail closed;
- usage and final validation facts can be captured in a Corridor-owned local receipt.

Recommendation after that spike:

- Prefer one Agents SDK `Agent` adapter if it satisfies the requirements, because it avoids hand-writing function-call continuation while preserving a small tool surface and typed result.
- Fall back to a separate direct Responses adapter if the SDK cannot preserve the transport/privacy contract. Do not modify `StructuredClient` or make existing extractors depend on agent orchestration.
- Do not use SDK sessions, handoffs, approvals, hosted tools, MCP, Agent Builder, or remote traces in v1. The human gate remains Corridor's existing UI and command seam, outside the agent run.

## Current Corridor seams and invariants

### 1. Work discovery and queue boundary

`work_list` is the right harness entry seam.

- The module exists specifically because coordinator work cannot be safely reduced to either Ledger rows or exceptions; unknown-scope commitments are real work without inventing a Dependency (`src/corridor/work_list.py:1-12`).
- It exposes one `WorkItem` per current coordinator question and derives `candidate` items for pending statement proposals (`src/corridor/work_list.py:103-142,646-728`).
- Candidate timing is explicitly kept as source wording rather than normalized fact (`src/corridor/work_list.py:689-704`).

Implication: the investigator should run only against one existing `WorkItem(kind="candidate")` or one pending `Candidate`, not against accepted statements, plan follow-up items, or a broad project crawl.

### 2. Explicit statement and plan authority

Corridor already distinguishes external-party fact from project response.

- `record_external_party_statement()` is the atomic writer for statement facts: it validates durable statement facts, locks the project, resolves scope, appends event/timing/evidence, and refreshes projections in one nested transaction (`src/corridor/external_statements.py:146-173,189-280`).
- `work_decisions` is the only writer for what the project decided: internal owner, next action, milestone impact, and deferral (`src/corridor/work_decisions.py:1-12,83-116,131-213`).
- ADR-0038 states that each Work Decision has exactly one subject, either one Dependency or one accepted commitment lineage, never both (`docs/adr/0038-a-coordination-plan-follows-one-work-subject-and-never-rewrites-an-external-fact.md:11-19,44-59`).

Implication: the investigator may surface exact source wording, possible party resolutions, and possible scope matches, but it may not choose any of them. V1 stops before plan drafting. It must not collapse statement fact, scope, and project response into one mutable "AI answer" or silently manufacture a Dependency plan.

### 3. Human-only write authorship

- Corridor rejects free-text role labels at the typed human principal seam (`src/corridor/principals.py:1-10,29-65`).
- ADR-0041 forbids an agent from originating a human-gated Ledger or Work Decision write under a human's identity (`docs/adr/0041-interface-operation-is-not-human-decision-authorship.md:1-7`).

Implication: the harness must stop at an unaccepted option packet. The existing web submit path remains the authoritative act.

### 4. Current-state and stale-read discipline

- Current statement/event/work-decision readers share common filters so reversed or superseded grouped saves do not leak as current state (`src/corridor/statement_lifecycle.py:22-109`).
- `coordinate_statement()` carries explicit predecessor identities, re-checks them under lock, and refuses stale resubmission without partial second save (`src/corridor/statement_coordination.py:95-145,196-205`; `tests/test_statement_coordination.py:466-493`).
- Undo is append-only compensation over an exact grouped receipt, and it refuses when later work depends on the save (`src/corridor/statement_coordination.py:334-410`; `tests/test_statement_coordination.py:637-759`).

Implication: the investigator cannot hand back an unversioned suggestion. It needs a read fingerprint and must be revalidated before the human submit path uses it.

### 5. Active Run and declared scope discipline

- Active Runs are declared, not inferred (`src/corridor/extraction_runs.py:163-203,407-418`; `tests/test_extraction_runs.py:63-69`).
- Candidate mutation must resolve through the declared Active Run, with exact historical override only when explicitly selected (`src/corridor/adjudicate.py:247-258`).

Implication: investigator tools must read from the same declared Active Run and current-state rules that the real writer and reviewer already rely on.

## Recommended harness architecture

### Deep module shape

Create one deep module:

`src/corridor/evidence_investigator.py`

Public interface:

```python
class InvestigationRuntime(Protocol):
    async def run(
        self,
        case: InvestigationCase,
        tools: InvestigationTools,
        budget: InvestigationBudget,
    ) -> RawInvestigationOutput: ...


async def investigate_candidate(
    session: Session,
    candidate_id: int,
    *,
    runtime: InvestigationRuntime,
    budget: InvestigationBudget = InvestigationBudget(),
) -> InvestigationResult: ...
```

The caller should not know about tool orchestration, retrieval order, ranking internals, or prompt shape. It should provide a candidate id and receive one validated result.

That module should own:

- gathering read-only tool context;
- the model loop;
- budget enforcement;
- result validation;
- read-fingerprint creation;
- and final investigation-packet shaping.

Internal adapters can stay small and private.

### Case binding and read-only tool set

Do not expose generic SQL, filesystem, shell, or broad ORM tools. Keep the tool surface narrow and typed.

The server binds one `InvestigationCase` before the model runs. That initial case contains the pending Candidate's proposal fields, current `WorkItem` reason, registered Evidence references, Active Run identity, and a read fingerprint. `project_id`, `candidate_id`, database sessions, and raw ORM objects stay in local run context and are never model-controlled arguments.

Mint opaque per-run references such as `E1`, `P1`, and `D1`. The run context owns their mapping to database identities. A model-emitted reference that was not minted by the harness is invalid rather than an invitation to query an id.

Recommended function tools:

1. `read_candidate_evidence(evidence_ref, focus, max_chars)`

Purpose: read only one registered page already cited by the bound Candidate. Source text is returned as untrusted Evidence, never instructions.

Return shape:

```json
{
  "evidence_ref": "E1",
  "document_name": "minutes.pdf",
  "page_no": 7,
  "candidate_quote": "The March 2026 completion timeline seems unattainable...",
  "page_context": "...",
  "text_source": "text_layer",
  "supporting_evidence_eligible": true,
  "truncated": false
}
```

The tool refuses any Evidence reference outside the bound Candidate. It returns diagnostic context distinctly when the registered page lacks the rendered/cell context required by the existing guided Save (`src/corridor/web/app.py:1236-1333`; `tests/test_statement_coordination.py:1405-1525`).

2. `search_project_parties(query, limit)`

Purpose: resolve possible registered parties without exposing the global External Party table.

Return shape:

```json
{
  "parties": [
    {
      "party_ref": "P1",
      "name": "Kinder Morgan",
      "matched_registered_spellings": ["Kinder Morgan", "KM"]
    }
  ]
}
```

Only parties already present in the bound project through active Dependencies or current statements are searchable in v1. The model cannot supply a project id or resolve another project's parties.

3. `shortlist_active_dependencies(party_ref, source_ref, station_text, terms, limit)`

Purpose: return a bounded shortlist of plausible Dependencies for the investigator to compare.

Return shape:

```json
{
  "candidates_considered": 8,
  "dependencies": [
    {
      "dependency_ref": "D1",
      "ref_code": "KM-31",
      "title": "KM 20-inch line",
      "party_ref": "P1",
      "station_from": "6608+70",
      "station_to": "6616+50",
      "status": "identified",
      "deterministic_signals": ["registered_party_match"]
    }
  ]
}
```

Implementation note: make this deterministic and project-scoped; include only non-dismissed, non-closed Dependencies, matching the current scope UI (`src/corridor/web/app.py:1024-1033`). Reuse exact alias and station parsing rules where they apply. Do not reuse `merge.rank_matches()` blindly: it is shaped for Dependency Candidates and matrix fields, not statement scope (`src/corridor/merge.py:151-311`).

4. `read_dependency_context(dependency_ref)`

Purpose: give the investigator the current state of one shortlisted dependency.

Return shape:

```json
{
  "dependency_ref": "D1",
  "headline": "KM 20-inch line",
  "external_org_name": "Kinder Morgan",
  "current_statement": { "...": "..." },
  "current_statement_evidence": [
    {
      "evidence_ref": "E2",
      "page_no": 7,
      "quote": "..."
    }
  ]
}
```

Grounding: current statement evidence through scope is already a shared read seam (`src/corridor/dependency_events.py:109-160`). V1 deliberately excludes roster, Internal Owner, Next Action, due dates, Milestone Impact, and other plan state.

5. `read_party_statement_context(party_ref)`

Purpose: expose current open unknown-scope or party-level statement context for the same party, so the investigator can avoid laundering a party-level fact into a dependency-level option.

Grounding: ADR-0038 and `work_list` both preserve party-level work without inventing Dependency scope (`docs/adr/0038-a-coordination-plan-follows-one-work-subject-and-never-rewrites-an-external-fact.md:44-59,83-90`; `src/corridor/work_list.py:1-12,163-168`).

Final validation is not a model tool. After the runtime returns structured output, Corridor validates every reference, quote, option, and fingerprint itself. The model does not get to call a tool that labels its own output valid.

### Tool schema discipline

Use strict JSON Schema for every tool input and the final output. OpenAI's function-calling docs explicitly support JSON-Schema-defined tools and strict mode, and `tool_choice` can be constrained to the allowed tool subset ([Function calling](https://developers.openai.com/api/docs/guides/function-calling)).

Corridor should mirror its current extraction seam here:

- `strict: true`
- no free-form custom tools
- no generic "search anything" tool
- enums for finding, assessment, status, and abstention kinds
- opaque, per-run reference strings rather than database ids
- explicit required fields
- no plan fields in tool inputs or outputs for v1

## State machine, budgets, and stopping rules

The harness should be a small deterministic loop, not an open-ended agent.

Recommended state machine:

```text
bind_current_case_and_mint_refs
  -> one bounded agent run over allowlisted read tools
  -> deterministic output validation
  -> optional one repair with structured validation errors
  -> validate again
  -> options_available | abstain | failed | stale
```

Hard limits:

- max agent turns: 4 per run
- max tool calls: 6 across the investigation
- max shortlisted dependencies returned to the model: 5
- max dependency contexts inspected: 5
- parallel tool calls: disabled
- max complete runs: 2
  - run 1: investigate
  - run 2: one repair only when deterministic validation returns a structured, repairable refusal
- initial max wall-clock budget: 30 seconds server-side, then tune from measured latency
- max token budget: explicit per request; start low and measure

Stop outcomes:

- `options_available`
- `needs_human_judgment`
- `abstain_insufficient_evidence`
- `abstain_ambiguous_scope`
- `abstain_ambiguous_party`
- `abstain_validation_failed`
- `abstain_budget_exhausted`

Abstain rather than guess when:

- the source quote does not support the stated speaker or timing;
- no Evidence supports narrowing a real party-level statement to a Dependency;
- the packet would require evidence not present in the Candidate's registered pages;
- deterministic validation refuses the investigation packet twice;
- the read fingerprint changes during the run.

This matches Corridor's existing fail-closed posture: if the current rules cannot prove the move honestly, the command refuses (`src/corridor/statement_coordination.py:73-92,196-214,320-323`; `src/corridor/external_statements.py:167-188`).

## Output contract

The result must be structured enough that the UI can render it and Corridor can revalidate it. The submit path must never accept it as input. It is an investigation packet, not a hidden write payload.

Suggested result schema:

```json
{
  "status": "options_available",
  "case_ref": "C1",
  "source_findings": [
    {
      "kind": "possible_new_timing_wording",
      "verbatim_text": "May 16th",
      "evidence_refs": ["E1"],
      "assessment": "ambiguous_without_explicit_year"
    },
    {
      "kind": "possible_previous_timing_wording",
      "verbatim_text": "March 2026",
      "evidence_refs": ["E1"],
      "assessment": "direct_source_wording"
    }
  ],
  "party_options": [
    {
      "party_ref": "P1",
      "display_name": "Kinder Morgan",
      "evidence_refs": ["E1"],
      "supporting_facts": ["registered spelling appears on the page"],
      "contradicting_facts": []
    }
  ],
  "dependency_options": [
      {
        "dependency_ref": "D1",
        "rank": 1,
        "why": "Same party and closest station context",
        "supporting_facts": ["party_match"],
        "contradicting_facts": ["no explicit dependency quote"]
      }
  ],
  "human_questions": [
    "Does this quote support dependency-level scope, or only party-level timing?",
    "What year, if any, does May 16th refer to?"
  ]
}
```

Corridor wraps this model output with the server-created read fingerprint and validator result; the model does not emit either.

Critical constraints:

- No `scope_mode`, selected party, selected Dependency, accepted timing normalization, or write-ready draft in the output. Ranked options are okay; selection is not.
- No hidden "approved" flag. The result is an investigation packet, not an adjudication.
- No plan authorship laundering. V1 must not return Internal Owner, Next Action, Milestone Impact, or Ready suggestions.

This is directly required by ADR-0039 and ADR-0041 (`docs/adr/0039-guided-statement-adjudication-is-evidence-bound-atomic-and-reversible.md:18-45,85-92`; `docs/adr/0041-interface-operation-is-not-human-decision-authorship.md:1-7`).

## Trace, receipt, and privacy design

The harness needs durable receipts, but they should be Corridor-owned. The model-generated packet does not carry database identities or its own fingerprint; Corridor attaches the server-created read fingerprint and raw identity mapping to the run receipt after validation.

Recommended persistence:

### `evidence_investigation_runs`

Immutable run header:

- `id`
- `project_id`
- `candidate_id`
- `status`
- `model`
- `prompt_version`
- `created_at`
- `completed_at`
- `tool_round_count`
- `model_call_count`
- `candidate_payload_sha256`
- `active_run_id`
- `read_fingerprint_json`

### `evidence_investigation_steps`

One row per tool call or model call:

- `evidence_investigation_run_id`
- `step_index`
- `kind` (`tool_request`, `tool_result`, `model_request`, `model_result`, `validator_result`)
- `name`
- `redacted_payload_json` containing opaque refs, normalized arguments, result identities, counts, and statuses only
- `payload_sha256`

### `evidence_investigation_packets`

Final validated investigation packet:

- `evidence_investigation_run_id`
- `packet_json`
- `validator_ok`
- `validator_refusal_code`

Write these as one terminal append-only receipt set after the run ends, including failed and abstained outcomes. If a later asynchronous worker needs mutable execution state, keep that in a separate operational job table; do not make the receipt mutable to double as a job queue.

Why local instead of SDK traces first:

- Corridor already treats authoritative grouped actions as first-class receipts (`src/corridor/models.py:1978-2035`).
- Corridor already uses append-only audit and grouped receipts rather than opaque log strings (`src/corridor/models.py:860-969,1322-1405,1978-2035`).
- Official OpenAI docs say built-in tracing is available in the Agents SDK, but that is optional observability, not a replacement for Corridor's own durable provenance model ([Integrations and observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)).

Privacy recommendation:

- Store ids, hashes, and normalized structured facts locally.
- Do not duplicate full page text or raw evidence dumps into run tables when that text already exists in `DocPage` and `EvidenceLink`.
- Disable remote raw-content tracing by default for real project runs. If SDK traces are later enabled in a non-production environment, use redacted or synthetic fixtures and never treat the trace as the canonical production record.
- Store no model chain-of-thought or reasoning narrative. Preserve model/prompt/tool versions, issued references, normalized tool arguments, referenced row hashes, usage, timing, validator results, and the final packet.

## Deterministic guardrails and stale-read validation

The harness needs more than prompt instructions.

Required guardrails:

1. Read-only adapter boundary

The model must only see investigator tools. Do not register any writer:

- no `record_external_party_statement()`
- no `record_statement_scope_decision()`
- no `coordinate_statement()`
- no `assign_internal_owner()`
- no `set_next_action()`

Also register no hosted tools, web search, MCP, shell, filesystem, code execution, or computer-use capability.

2. Project and reference capability boundary

The model never chooses `project_id`, `candidate_id`, `document_id`, `external_org_id`, or `dependency_id`. Tools resolve only opaque references minted into the current run context and apply project/current-state filters again on every call.

3. Untrusted-document boundary

Candidate quotes and page text are untrusted source data. Pass them in delimited data fields, label them as Evidence rather than instructions, and ignore any embedded request to call tools, reveal other records, change policy, or take action. This prompt rule is defense in depth; least-privilege tools, opaque refs, budgets, and final validation are the actual enforcement.

4. Deterministic validation gate

Every non-abstaining result must pass a Corridor-owned validator before the UI shows options.

5. Read fingerprint gate

Before displaying a stored packet, compare:

- candidate state
- candidate payload hash
- candidate source document id
- declared Active Run id
- any current statement lineage ids surfaced to the model
- any current scope/decision ids surfaced to the model

If any differ, mark the packet stale and rerun investigation. The later human submit still performs the existing independent under-lock predecessor checks; it never trusts the investigation fingerprint.

This is the read-side analogue of the existing optimistic concurrency checks (`src/corridor/statement_coordination.py:95-145,196-205`).

6. Citation validity gate

Every evidence item in the model output must name an opaque `evidence_ref` plus exact verbatim wording. The validator resolves that ref through the server-owned run mapping and confirms:

- project-scoped `document_id`
- registered `page_no`
- exact `quote` or exact source substring

And the validator must confirm the quote still resolves as valid project evidence before returning `options_available`.

7. No unsupported normalization

V1 returns exact timing wording and ambiguity, not accepted `StatementTiming`. It may not invent day precision, a missing year, dependency scope, or party authorship when the source only supports weaker forms. Corridor already treats timing precision and evidence support as first-class statement facts (`src/corridor/work_list.py:165-168`; `src/corridor/statement_coordination.py:206-214`; `docs/adr/0039-guided-statement-adjudication-is-evidence-bound-atomic-and-reversible.md:23-45`).

## Shadow-mode eval plan

Corridor should not ship this on intuition. Build a replay dataset first.

### Dataset

Build the first trustworthy set prospectively:

1. Before a coordinator opens an Unplaced Statement, freeze the exact model-visible case packet and fingerprint.
2. Run the investigator in hidden shadow mode.
3. Let the coordinator use the ordinary UI without seeing the agent output.
4. Record the later human choice, correction, Undo, and elapsed review time as labels.

This avoids leaking later accepted statements, scope decisions, or Work Decisions into the model's tools. Historical adjudicated cases may supplement the set only when the replay reconstructs the state visible at the decision time from immutable Extraction Run inputs and excludes every later answer-bearing row.

Stratify the set across:

- single-dependency attach cases
- unknown-scope accepted cases
- not-relevant cases
- party ambiguity cases
- timing ambiguity cases
- later-corrected cases

Keep the canonical dataset and graders local to Corridor. OpenAI's eval guidance recommends task-specific datasets, logging during development, representative historical/production cases, and calibrating automated scoring against human labels ([Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).

### Graders

Use a mix of deterministic and model-assisted grading:

Deterministic:

- schema validity
- deterministic-validator pass rate
- zero unauthorized tool usage
- citation validity
- fingerprint freshness
- top-k containment of eventual human dependency choice

Human-scored:

- whether the investigation packet reduced review effort
- whether abstention was correct
- whether ranked options were honest and useful

Model-assisted:

- pairwise comparison of packet quality only after it is calibrated against human labels; official guidance recommends validating agreement with human labels before scaling LLM-as-judge usage ([Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).

### Metrics

Primary:

- `validator_pass_rate`
- `citation_validity_rate`
- `unsafe_single_scope_rate`
- `top1_match_rate`
- `top3_match_rate`
- `correct_abstain_rate`
- `human_option_agreement_rate`
- `median_review_seconds_saved`

Guardrail metrics:

- `write_tool_calls` must stay `0`
- `stale_packet_display_rate`
- `post-submit validator refusal rate`

### Promotion gates

Do not expose investigation packets in the UI until shadow mode meets all of these:

- 100% schema-valid outputs
- 100% citation-valid outputs
- 100% deterministic-validator pass for non-abstaining outputs
- 0 unauthorized write attempts
- 0 packets that silently force dependency scope when the labeled answer is unknown-scope
- top-3 target containment high enough to help a reviewer materially; set the exact threshold after baseline replay

OpenAI's current agent-evals guidance also says to move from individual traces to repeatable datasets and eval runs once you know what good looks like ([Evaluate agent workflows](https://developers.openai.com/api/docs/guides/agent-evals)).

Note on legacy Evals: OpenAI's current docs say the legacy Evals platform becomes read-only on October 31, 2026 and is scheduled to shut down on November 30, 2026, so Corridor should not make it a required dependency for this harness. Keep the replay corpus and gate assertions local to the repo first ([Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).

## Incremental file and test plan

### Phase 1: domain-owned read harness

Add:

- `src/corridor/evidence_investigator.py`
  - public `investigate_candidate()`
  - case binding, opaque refs, fingerprinting, and final validator
- `src/corridor/evidence_investigator_runtime.py`
  - `InvestigationRuntime` protocol
  - first runtime adapter selected by the transport spike
- `prompts/evidence_investigator_v1.md`
  - immutable versioned instructions; document text is untrusted data
- `tests/test_evidence_investigator.py`
  - deterministic harness tests with stub model

No migration yet for the pure module, transport-spike, and stub-test phase. Live shadow runs require Phase 2 receipts before they are used for evaluation; do not scatter untracked JSON logs through the repo.

### Phase 2: durable run receipts

Add:

- `src/corridor/migrations/versions/..._add_evidence_investigation_runs.py`
- `src/corridor/models.py`
  - `EvidenceInvestigationRun`
  - `EvidenceInvestigationStep`
  - `EvidenceInvestigationPacket`
- `tests/test_evidence_investigator_persistence.py`

Pattern these after the existing grouped receipt and append-only models, not after mutable task tables (`src/corridor/models.py:1978-2035`).

### Phase 3: web integration

Add:

- one project-scoped read route in `src/corridor/web/app.py` for `candidate_id -> investigation packet`
- one template partial for rendering ranked options, contradictions, evidence packet, and abstention reasons

Do not change the submit path. The existing save endpoints should continue to call the existing command layer.

Rollout rule:

- keep Phase 3 internal-only or shadow-only until Corridor has real project-scoped auth and membership enforcement for coordinator access;
- do not expose this in a production coordinator UI that still relies on a single configured local principal and lacks project membership enforcement (`src/corridor/web/app.py:207-221`; `src/corridor/principals.py:1-10,29-65`).

### Test matrix

Required tests:

- returns abstain when evidence does not support stated party
- returns abstain when only party-level scope is supported
- returns ranked options without forced selection
- refuses fabricated or cross-project opaque references
- ignores instruction-like text embedded in Evidence and remains unable to call non-read tools
- excludes closed and dismissed Dependencies from scope options
- revalidation fails if candidate hash or current lineage changed
- no options shown unless deterministic validation passes
- write tools are unreachable from the harness
- frozen pre-decision replay packet matches labeled top-k expectation on fixtures

## Non-goals

Do not include any of these in the first harness:

- autonomous writes
- multi-agent delegation
- generic project search over all candidates
- direct SQL or shell tools
- SDK-managed approvals as the canonical human gate
- direct reuse or expansion of extraction `StructuredClient` as an agent loop
- any suggestion of Internal Owner, Next Action, Milestone Impact, or Ready in v1
- any production user-facing rollout before project-scoped auth and membership exist
- replacing `work_list`
- replacing `coordinate_statement()`
- replacing grouped receipts or audit logs
- broad cross-project "research assistant" behavior

## Final call

The Evidence Investigator is a good agentic fit, but only as a bounded read-only copilot.

Build it as a deep local module with one runtime adapter and current Corridor readers. Keep the writer boundary exactly where it is. Start with Unplaced Statement evidence and possible-scope investigation only. Use strict schemas, opaque references, deterministic read adapters, final validation, and a prospective shadow dataset. Prefer the Agents SDK adapter only if the transport spike proves Corridor's storage, tracing, and budget requirements; otherwise use a separate direct Responses adapter without changing extraction's one-shot client.
