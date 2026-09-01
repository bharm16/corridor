# Corridor ADR, code, and open-issue realignment review

**Repository:** `bharm16/corridor`  
**Reviewed branch outcome:** PR #504, merged into `main` as commit `52f16d4060877d0d8752c60eecceb632ece27b7b`  
**Open issues reviewed:** 49  
**Recommended issue actions:** 23 close, 23 amend, 3 keep  
**Recommended issue deletions:** 0

## Executive verdict

PR #504 is a strong strategic correction, but it is **not yet safe to use as the complete implementation constitution**.

The work correctly:
- repositions Corridor as a baseline-maintenance, change-control, and exception layer;
- preserves superseded ADRs instead of deleting them;
- introduces explicit ADR relationship metadata;
- makes the spine the target model;
- records deployment, connector, retention, and pilot intent;
- freezes broad feature expansion before commercial validation.

However, it also introduces several material design errors and leaves the current roadmap, issue graph, and runtime behavior contradictory. The highest-risk defects are:

1. **ADR-0077 applies one four-link evidence chain to every published value.** That model does not fit Derivations or Coordination Decisions, and semantic support cannot be a global property of a reusable passage.
2. **The generated ADR index infers authority from ADR number.** “Numerically latest” is not “governing,” proposed ADRs are treated as active, and accepted historical ADRs appear in the active set.
3. **The current Phase 1 roadmap remains the old readiness-ledger roadmap.** A preface does not make the old sequencing and exit criteria non-governing.
4. **ADR-0078 uses a pull-connector contract for push intake.** Webhook email and manual upload need a separate ingress contract behind a shared normalized source envelope.
5. **ADR-0080 is overbuilt for the first pilot and incomplete for real deletion.** It requires granular disposition before the first commercial record but does not solve receipt custody, referential evidence retention, partial failure, or backup expiration.
6. **ADR-0081's cutover gates are underspecified.** Active-only backfill cannot prove historical/as-of equivalence, and byte-identical output is the wrong gate where the new product intentionally changes behavior.
7. **The code still implements the superseded product.** New matrix rows are automatically admitted, structured-cell facts are automatically projected, email arrives through one global address and content routing, the recorded-verbal segment depends on a legacy statement row, and the current-record equivalence overlay covers only a narrow field subset.

The immediate correction should be a small ADR/governance PR before implementation work resumes. Then the active backlog should be reduced and reorganized around one paid vertical slice:

> Import one customer UCM → adopt one exact baseline → observe one new source → produce one or more Proposed Deltas → resolve them → return the customer's updated UCM, change summary, chase list, and weekly report → measure net customer and Corridor operations time.

---

## PR #504 verification

PR #504 was merged as one commit and changed 93 files. The PR has no recorded comments or review submissions. The commit message has no attribution trailer; the PR description does include a generated-with-Claude footer. No GitHub commit statuses or pull-request workflow runs are associated with the merge commit, so the stated local `make check` and pre-push results cannot be independently verified from GitHub.

The lifecycle claim should be worded precisely:

> No accepted **normative decision** was rewritten.

ADR-0034's body did change because a large explicitly historical/non-normative appendix was moved to `docs/operations/`. That maintenance is reasonable and consistent with the new lifecycle policy, but “no accepted body was rewritten” is literally inaccurate.

### The two submitted judgment calls

**ADR-0076 amending ADR-0010 and ADR-0035:** substantively justified, but not ideally located. The baseline/delta product needs a transparent change-inbox ordering rule, so the relationship is real. ADR-0076 should own only the fact that Proposed Deltas expose auditable consequence bands. ADR-0035 or a future work-list decision should own exact presentation ordering. This is not a reason to undo the relationship; it is a reason not to put more UI policy into ADR-0076.

**No attribution trailer:** correct at the commit level. The PR body footer is not a commit attribution trailer.

---

# ADR review

## ADR-0075 — accepted direction, amend through a successor only if discovery changes it

### What is right
- Correct initial wedge: late design through construction, after a working coordination record exists.
- Correct baseline-plus-delta workflow.
- Correct integration posture: complement PMIS/document control/GIS/SUE/CAD/scheduling rather than replacing them.
- Correct output focus: updated UCM, change summary, chase list, weekly report.
- Correct economic premise: net operator time at acceptable accuracy and freshness.

### Corrections
- “The coordination record is maintained by hand in whatever system holds it” is too absolute. Existing systems automate portions of maintenance and workflow; the defensible claim is that substantial cross-source reconciliation remains manual.
- “The first commercial buyer is a consultant” is a market hypothesis, not yet a repository-supported fact. Keep it as the provisional design-partner target, subject to amended #428.
- The freeze is good, but customer-format export and one real connector are not “broad feature expansion”; they are part of the first paid slice.

## ADR-0076 — correct constitution, incomplete operating model

### What is right
- Separates source capture from accepted-record change.
- Makes baseline adoption one attributable bulk decision rather than hundreds of clicks.
- Makes material changes Proposed Deltas.
- Restricts automation to exact classes and retains human resolution for ambiguity.
- Replaces ADR-0029's automatic accepted-record admission.

### Required clarification
- Define Proposed Delta identity, lifecycle, coalescing, supersession, reopening, staleness, and grouping.
- Separate **human baseline adoption** from “automatic projection”; baseline adoption is a bulk human command.
- Clarify that exact unchanged support transfer is not a new accepted value decision.
- “Apparent removal” should not be grouped with statements Corridor could not place; it is a record-change question with distinct risk.
- The architecture test should not be the sole enforcement mechanism. The target spine needs database privileges/guarded functions, with static checks as defense in depth.
- ADR-0034 should be marked `amended_by: ADR-0076` because its normative setup/backfill section still assumes ADR-0029 mechanical Record Inclusion.

## ADR-0077 — material defect; supersede before implementation

This is the most serious new ADR problem.

### Defect 1: one chain does not fit every value
The ADR says every value must expose:

> Source says → Corridor proposed → person/policy decided → current record now shows

That works for a source-backed accepted field. It does not fit:
- a Coordination Decision, whose provenance is the responsible human and current subject;
- a Derivation, whose provenance is rule/version + exact inputs + evaluation time;
- some Recorded Verbal Statements, whose source is a recorder-attested origin rather than a document proposal;
- registry metadata where the authority path is intentionally different.

A report cell must have one **valid provenance class**, not all four layers.

### Defect 2: semantic support is relational
A single passage can:
- support one proposed field;
- contradict another proposition;
- provide attribution context for a third;
- be irrelevant to a fourth.

Therefore `semantic_support_status` cannot be a global property of an EvidenceLink, Source Segment, or passage. It belongs on the relation between a typed proposition/fact/proposal and one or more Source Segments, with a role and assessment.

This is also consistent with ADR-0001's original rejection of putting asserted field meaning on evidence links: one quote often supports several fields.

### Defect 3: decision and support assessment are conflated
A Human Record Decision decides what the accepted record does. It does not necessarily decide whether a particular passage semantically supports a proposition. Those acts may occur together in one UI transaction, but the identities must remain distinct.

Create a successor ADR before #493 or any schema migration.

## ADR-0078 — correct boundary, wrong common interface

### What is right
- Project/customer binding must exist before content-driven routing.
- Connected sources are incremental and versioned.
- Content inference is fallback, not a tenant boundary.
- Schedule adapters consume relevant outputs and do not calculate CPM.

### Defect
`list_changes → fetch_version → get_metadata → acknowledge` is a **pull connector** contract. It does not model:
- inbound webhook email;
- project aliases;
- manual upload;
- a pushed schedule export.

Use three layers:
1. `SourceEnvelope` — one normalized downstream ingress record.
2. `PullConnector` — list/fetch/checkpoint.
3. `PushIntake` — authenticated, pre-bound delivery.

Define checkpoint semantics exactly: advance only after every change through the exact token is durably stored; replay after a crash must be idempotent.

## ADR-0079 — sound topology, tighten meanings

### What is right
- Modular monolith.
- Logical web + worker roles.
- Managed PostgreSQL and object storage.
- One database/object namespace per customer for the first commercial deployments.
- No Kubernetes/microservices/graph database.
- Explicit production run lineage and observability.

### Corrections
- One database per **customer** does not make the **project** boundary a database boundary. Projects remain authorization and data-partition boundaries inside the customer database.
- “Every run records environment, customer, project, source revision” is too universal. Migration, control-plane, customer-level connector, and maintenance runs may not have a project or source revision. Require fields by run kind.
- Split ambiguous “source revision” into code revision, input/source identity, extractor/policy version, and requested Project Record revision.
- “One application container” and “one worker container” should mean logical deployable roles, not a permanent one-replica limit.
- Define where cross-customer control-plane data, deployment inventory, hold state, and disposition receipts live.

## ADR-0080 — correct principle, wrong pilot sequence and incomplete deletion model

### What is right
- Append-only ordinary operation is distinct from governed disposition.
- Legal hold must stop deletion.
- Disposition needs an attributable plan and receipt.
- Intermediate data and record data need different policies.

### Corrections
- Do not require a full granular row-class disposition engine before the first paid pilot. With one database/object namespace per customer, the safe pilot posture is:
  - contractually declared retention;
  - legal hold;
  - export/custody transfer;
  - whole-customer-environment destruction;
  - backup/snapshot expiration;
  - external compliance receipt.
- A receipt cannot survive only in the same customer database being destroyed.
- A raw source cannot be disposed while a retained decision or released artifact still promises dereference to it, unless custody is transferred and the remaining record explicitly says the source is unavailable.
- Define overlapping hold/schedule precedence.
- Define resumable partial-failure behavior across PostgreSQL, object storage, encryption keys, and backups.
- Define PITR/snapshot expiration; otherwise “deleted” data remains in recoverable backups.
- Granular project/record-class deletion should be earned by a signed customer requirement, not built speculatively across 149 tables.

## ADR-0081 — correct target, incomplete exit criteria

### What is right
- Explicitly rejects the legacy `statement_id` dependency in the target model.
- Freezes legacy-only feature work.
- Defines backfill, reader switch, writer switch, rollback, and retirement as separate stages.
- Keeps rollback bounded rather than permanent dual-write.

### Corrections
- “All active legacy conclusions” is insufficient if current/as-of history and audit reconstruction must match. Migrate relevant historical lineage or declare a temporary compatibility reader with an expiry.
- The migration executor must not become the semantic author. Preserve the original human/system actor and record a separate migration executor/receipt.
- Use **semantic contract equivalence**, not universal identical rendering. Byte/cell equality is appropriate only for intentionally unchanged surfaces.
- Current code's equivalence overlay replaces only a narrow set of fields, so the existing green gate is not evidence that all current-record readers are spine-native.
- Define rollback start/end, allowed cohort, divergence thresholds, traffic owner, and decision authority.
- Stage 6 must include backup expiry and ADR-0080 disposition treatment.

---

# Lifecycle, index, and planning-document review

## ADR lifecycle policy

The keep/supersede/deprecate policy is good. Reciprocal metadata and cycle validation are useful.

Required changes:
- Proposed ADRs must be shown separately and must not supersede an accepted decision until accepted.
- `scope: historical` must exclude an ADR from the active current-decision set.
- A deprecated ADR may truthfully have `supersedes` history; it merely cannot name a current successor through its status.
- Reject duplicate ADR numbers, unknown relationship references, malformed filenames, and unknown domain/scope values.
- Consider moving implementation-completion state out of durable ADR frontmatter if `migration:` becomes routinely stale.

## Generated INDEX.md

Do not use the current “Latest governing” table as authority.

It currently:
- calls ADR-0073, a token-layer storage choice, the governing extraction decision;
- calls ADR-0041, an interface-authorship boundary, the governing human-work decision;
- includes proposed ADRs in the active set by implementation;
- includes accepted historical ADR-0021 in the active table.

ADR numbers establish chronology, not authority. Replace the section with:
- accepted current/optional decisions by domain;
- supersession-graph leaves;
- proposed decisions in a separate table;
- historical and superseded decisions separately.

Only add a “governing” field if it is explicit metadata with a defined meaning.

## Phase 1 roadmap

The roadmap is not actually aligned. It adds a baseline/delta preface but leaves the old:
- product goal;
- M0–M9 sequence;
- “build nothing past M5” rule;
- readiness-oriented exit criteria;
- integration timing;
- report-replacement criteria

as the current body.

Move the old roadmap to history and create a new current roadmap. A preface is insufficient because agents and issues still cite the old sections as normative sequencing.

## Pilot criteria

The pilot document is directionally good but not falsifiable enough:
- “roughly 10–15 minutes” is not a gate;
- no minimum partners, active projects, duration, or source-event count;
- no baseline-adoption accuracy/completeness sample;
- no source-arrival→proposal→decision latency threshold;
- no Corridor operations/setup/triage time;
- no compute/storage cost;
- 80% accept-without-edit can be achieved on trivial deltas while material changes fail;
- material false writes and misses are reported but have no pass ceiling;
- sampling rules are unspecified;
- “falling trend” has no required magnitude/window.

Rewrite it before pilot execution.

---

# Current-code review against ADR-0075–0081

## 1. The old automatic accepted-record path still runs

`admission.load_project` still:
1. declares a production run;
2. runs legacy dependency admission;
3. automatically includes current structured-cell facts;
4. runs event admission.

The module's own description still says conflicts and statements run unattended into the record. That is ADR-0029 behavior, now superseded by ADR-0076.

## 2. Structured-cell facts still auto-project accepted decisions

`include_current_structured_cell_facts` finds fact types with spreadsheet-cell automatic kinds and calls `include_structured_cell_fact_by_policy`, which creates/supersedes effective `FactDecision` rows.

It also selects only facts whose legacy Candidate is already accepted/merged into a legacy Dependency. The new spine is therefore not yet an independent baseline/delta record; it is downstream of the legacy accepted-record path.

## 3. No baseline or delta implementation exists

No code symbol for `AdoptBaseline`, `ProposedDelta`, or `ResolveDelta` exists. The terms appear only in documentation. There is no:
- baseline preview;
- baseline adoption revision;
- delta identity;
- comparison lifecycle;
- resolution command family;
- delta-backed change inbox.

## 4. Recorded-verbal provenance still depends on legacy tables

`SourceSegment(kind="recorded_verbal_statement")` carries `statement_id` pointing to the legacy External Party Statement table. Fact identity also uses that legacy statement id. ADR-0081 stage 1 is unimplemented and must precede new statement-only features.

## 5. Current-record equivalence is narrow

The current-view overlay used by `prove_reader_equivalence` replaces only selected dependency fields in a frozen **legacy** reading. It does not prove that every report, workbook, briefing, release, statement, support designation, work decision, or historical reading is sourced from the spine.

The Stage 3 test must become coverage-aware and semantic, with a declared field/record inventory.

## 6. Email intake still implements ADR-0059

The code receives one configured service address, stores raw bytes, then resolves the project from attachment/body/sender/thread evidence. That is precisely the global content-routing design ADR-0078 supersedes.

It also writes raw `.eml` bytes directly to `Path(settings.corpus_store)`, bypassing the not-yet-built storage interface.

## 7. Deployment and operations remain local

The repository still has:
- Docker Compose for PostgreSQL only;
- no application Dockerfile;
- no staging/production deployment workflow;
- no defined production secret store;
- no real inbound-mail provider configuration;
- no real outbound email adapter;
- no structured logging/APM.

The code does have a substantial, reusable Due Work runtime, so #488 should not rebuild scheduling primitives.

## 8. Retention implements only the old Class B path

The existing retention module expires intermediaries and has no Class A record disposition path. Do not bolt a full 149-table granular deleter onto it before the pilot; first implement the corrected customer-environment lifecycle.

## 9. The writer boundary remains convention-heavy

The repository acknowledges that `adjudicate` is the legacy Constraint writer but has no AST guard for that boundary. More importantly, AST alone is insufficient. The spine should be writable through restricted database functions/roles, with static checks and runtime bypass tests layered on top.

---

# Open issue dispositions

No open issue should be deleted. Close superseded, completed, or deliberately deferred issues with a reason and a reopen trigger; Git history and issue links are part of the architecture record.


| Issue | Current title | Recommendation | Why / exact change |
|---:|---|---|---|
| [#1](https://github.com/bharm16/corridor/issues/1) | Corridor v0 — Cited Readiness Ledger (M0-M5) | **CLOSE — superseded** | The umbrella describes the old readiness-ledger product and points at the old M0–M5 constitution. It is 95% complete but no longer governs the product. Close with a comment pointing to the rewritten #459 baseline-plus-delta program. Preserve completed child history; do not delete. |
| [#7](https://github.com/bharm16/corridor/issues/7) | File public-records requests, scoped to email correspondence | **CLOSE — deferred by strategy** | Public-records acquisition is no longer the critical path. The paid slice should start with a design partner’s explicitly bound mailbox/folder and current UCM. Close as not planned. Reopen only if a real evaluation gap cannot be filled by design-partner data. |
| [#70](https://github.com/bharm16/corridor/issues/70) | Run extraction through the Batch API at corpus scale | **CLOSE — deferred until measured trigger** | This is a provider-cost/throughput optimization unrelated to the new product risk. Current scale does not justify another asynchronous ingestion path. Close with reopen criteria: synchronous/flex extraction exceeds a declared latency, queue-capacity, or monthly-cost threshold on live pilot workloads. |
| [#149](https://github.com/bharm16/corridor/issues/149) | Model document-asserted work sequencing | **CLOSE — outside initial wedge** | Work-sequencing graphs expand Corridor toward schedule/work-package modeling. The new scope consumes relevant dates and dependencies but does not model construction sequencing broadly. Close. Reopen only when a paying design partner supplies recurring sequencing evidence that Required By/key-date linkage cannot represent. |
| [#151](https://github.com/bharm16/corridor/issues/151) | File NHHIP 3C-2 records request for recurring utility-status artifacts | **CLOSE — deferred by strategy** | More public corpus data is not the immediate commercial bottleneck. The first slice needs the customer’s real baseline and subsequent project evidence. Close as not planned for the pilot. Preserve the request template for later extraction evaluation. |
| [#290](https://github.com/bharm16/corridor/issues/290) | Build the read-only Evidence Investigator shadow harness for Unplaced Statements | **CLOSE — implemented** | The repository contains the investigator runtime, shadow/evaluation machinery, frozen cohorts, and outcome-capture work delivered by subsequent PRs. Close as completed. Do not use the still-open parent as active product work. |
| [#297](https://github.com/bharm16/corridor/issues/297) | Capture independent v2 human outcomes and baseline | **CLOSE — stop optional assistant program** | This belongs to the pre-pivot assistant-promotion experiment, which ADR-0075 freezes until the core paid slice is validated. Close as not planned. Retain the frozen cohort and receipts; create a fresh evaluation issue only if assistant display becomes a post-pilot priority. |
| [#298](https://github.com/bharm16/corridor/issues/298) | Publish v2 Evidence Investigator promotion receipt | **CLOSE — stop optional assistant program** | Publishing a promotion receipt has no value while customer-facing assistant display is frozen and the core baseline/delta workflow does not exist. Close as not planned; do not declare the old cohort a product gate. |
| [#328](https://github.com/bharm16/corridor/issues/328) | Choose assistant display configuration and evidence contract | **CLOSE — deferred** | This chooses presentation for an optional model assistant, not the change-control product. Close. Reopen after a paid pilot demonstrates statement-investigation review time is material and the frozen shadow data supports exposure. |
| [#336](https://github.com/bharm16/corridor/issues/336) | Confirm operations-configured project setup | **CLOSE — replaced by Adopt Baseline** | A generic setup acknowledgment was already rejected as ceremony. The new meaningful human act is atomic adoption of one exact baseline. Close as superseded by a new Adopt Baseline implementation issue. |
| [#357](https://github.com/bharm16/corridor/issues/357) | Add promotion-gated statement packet display, disabled by default | **CLOSE — deferred** | The display is explicitly outside the first paid slice and depends on the optional assistant program. Close with a post-pilot reopen trigger tied to measured review-time reduction and safety gates. |
| [#424](https://github.com/bharm16/corridor/issues/424) | Define extraction-economics gate: displaced human-minutes per document | **AMEND — make it the net pilot economics gate** | The title still centers extraction and zero-touch admission. ADR-0075 makes net operator time, freshness, accuracy, operations effort, and cost the economic premise. Retitle to “Measure net coordination-record economics by project-week and source class.” Add baseline operator time, Corridor operations/setup time, review time, source-arrival→proposal→decision latency, compute cost, coverage, material false-write/miss ceilings, and paid continuation. |
| [#425](https://github.com/bharm16/corridor/issues/425) | Spec evidence-driven chase loop | **AMEND — narrow to pilot chase-list output** | A chase list is part of the paid slice, but drafted outreach, call logging, automatic escalation, and a general action system are frozen. Retitle to “Generate the pilot chase list from unresolved deltas and current Follow-up Plans.” Exclude autonomous outreach, delivery, escalation, and new task machinery. |
| [#426](https://github.com/bharm16/corridor/issues/426) | Spec record-generated coordination paperwork | **CLOSE — expansion work** | Generated notices, clearances, and other formal paperwork are downstream acting features, not required to prove baseline maintenance. Close. Reopen after the UCM/change-summary/chase-list/report slice has paying users and a customer names one recurring artifact. |
| [#427](https://github.com/bharm16/corridor/issues/427) | Bound schedule source class to date tables; raw CPM parsing out of scope | **CLOSE — superseded by ADR-0078** | ADR-0078 now permits P6/MS Project/PMIS exports as source adapters while retaining the correct boundary that Corridor does not calculate CPM. Close as superseded. Carry the no-CPM-engine rule into amended #450 and connector docs. |
| [#428](https://github.com/bharm16/corridor/issues/428) | Decide first buyer: consultant wedge vs DOT direct | **AMEND — validate the provisional consultant hypothesis** | ADR-0075 prematurely records consultant-first as a decision, but the repository contains no completed buyer-discovery evidence. Retitle to “Validate consultant-first buyer, budget, sponsor, procurement, and pricing hypotheses.” Require interviews with consultant coordinators and agency sponsors; treat consultant-first as provisional until evidence passes. |
| [#443](https://github.com/bharm16/corridor/issues/443) | Multi-engine table layer | **CLOSE — defer deep extraction platform work** | A multi-engine table platform is architecture before evidence. The first pilot should prefer native UCM/workbook exports and use current PDF extraction where necessary. Close. Reopen only for a named pilot document family with measured failures that cannot be fixed in the existing tiered path. |
| [#444](https://github.com/bharm16/corridor/issues/444) | Cell reconstruction and PDF source segments | **CLOSE — defer until pilot evidence** | This is part of the same speculative PDF platform expansion. Close with a trigger: a design partner has only PDF baselines and current source segments cannot support the required delta fields at the pilot error ceiling. |
| [#445](https://github.com/bharm16/corridor/issues/445) | Structure-only semantic mapping | **CLOSE — defer until pilot evidence** | The broad semantic-mapping layer is not needed before the baseline/delta workflow is proven. Close. Recreate a source-family-specific issue if live pilot errors show the mapping layer is the bottleneck. |
| [#446](https://github.com/bharm16/corridor/issues/446) | Isolate transcription tier and enforce authority | **AMEND — preserve only the cross-cutting source-integrity rule** | The physical tier program is premature, but the invariant that model literals cannot silently become authoritative source values remains important. Retitle to “Enforce that source facts materialize from replayable Source Segments, never unverified model literals.” Remove broad engine-isolation and deprecation work; coordinate with #492. |
| [#447](https://github.com/bharm16/corridor/issues/447) | Shadow runs and selection gate | **CLOSE — defer extraction selection program** | This optimizes a future PDF engine stack, not the current product bottleneck. Close. Reopen with a named pilot source family, frozen cases, and a measured candidate-engine comparison. |
| [#448](https://github.com/bharm16/corridor/issues/448) | Bounded hard-page orchestration agent | **CLOSE — optional agent work** | ADR-0075 freezes agentic/hard-page expansion until the core paid slice is validated. Close as not planned. Keep the existing bounded harness available for targeted experiments, not roadmap work. |
| [#450](https://github.com/bharm16/corridor/issues/450) | Date-table adapters | **AMEND — source facts and proposed schedule deltas** | Dates remain core, but the current ticket assumes automatic accepted Record Inclusion and broad adapter coverage. Implement one design-partner format first. Capture immutable Key Date facts, compare to the accepted revision, create Proposed Deltas and impact facts, and never update Promised For or accepted Required By relationships without the applicable decision rule. |
| [#454](https://github.com/bharm16/corridor/issues/454) | Agreement clauses onto the spine | **CLOSE — defer source-family breadth** | Agreement-clause breadth is not needed for the first UCM/minutes/mail delta loop unless a design partner proves otherwise. Close. Reopen for one named agreement workflow and field set after the pilot source inventory is known. |
| [#455](https://github.com/bharm16/corridor/issues/455) | Email/MIME/attachments onto the spine | **AMEND — project-bound evidence capture, then deltas** | Email is likely pilot-relevant, but the body still follows old Record Inclusion semantics and the current global routing design. Split transport/routing from semantic capture. Bind customer/project before content, preserve raw MIME and attachments through the normalized ingress seam, capture source facts, and emit Proposed Deltas; no direct accepted-record write. |
| [#456](https://github.com/bharm16/corridor/issues/456) | Complete minutes semantics onto the spine | **AMEND — narrow to pilot-critical statement changes** | The complete minutes ontology is too broad, but commitments, changes to promised timing, completion reports, attribution, and scope are likely central to the first slice. Limit v1 to fields required by the design partner’s weekly update. Capture source facts/proposals, compare to the baseline/current accepted record, and create Proposed Deltas. Defer full prose taxonomy. |
| [#457](https://github.com/bharm16/corridor/issues/457) | Permanent-state dedup for new writes | **AMEND — make it an ADR-0081 convergence prerequisite** | Permanent-state dedup remains useful, but its references and sequencing predate ADR-0080/0081 and the baseline/delta model. Update to protect Source Facts, Proposed Deltas, decisions, revisions, and connector deliveries. Remove legacy-only expansion; define dedup identities per command and source version; reference governed retention, not ADR-0072. |
| [#458](https://github.com/bharm16/corridor/issues/458) | Final Project Record cutover | **AMEND — own ADR-0081 stages 5–6** | The ticket assumes #451 delivered the target identity and treats reader equality too narrowly. Current code still points verbal segments to legacy statements and overlays only a small part of the spine. Retitle to “Complete writer cutover, bounded shadow comparison, and legacy retirement.” Block on spine-native source identity, historical backfill, semantic-equivalence gates, and #457. Define rollback window, divergence metrics, decision owner, backup/disposition treatment, and removal of compatibility writes. |
| [#459](https://github.com/bharm16/corridor/issues/459) | Program: source-to-Project-Record redesign (ADR-0066..0072) | **AMEND — replace the central program** | This is the correct umbrella to retain, but every premise, ADR range, milestone, and ordering is stale. Retitle to “Program: baseline-plus-delta paid pilot and spine convergence (ADR-0075..0081).” Preserve completed old work in a historical section; add the new roadmap, issues, dependencies, pilot gate, and explicit frozen work. |
| [#460](https://github.com/bharm16/corridor/issues/460) | Delete legacy transcription tier after selection | **CLOSE — defer cleanup** | Physical deletion of an extraction tier is not a product milestone and no selected replacement has been justified by pilot evidence. Close. Reopen only after a measured replacement is active and the supported rollback window expires. |
| [#461](https://github.com/bharm16/corridor/issues/461) | Resolve PyMuPDF licensing | **KEEP — active blocker** | The commercial deployment cannot proceed while a core PDF dependency’s licensing posture is unresolved. Keep open and block external pilot deployment that uses the dependency. Record the decision and any replacement plan; do not broaden the ticket. |
| [#486](https://github.com/bharm16/corridor/issues/486) | Record change-control positioning research note and ADR amending ADR-0066 | **AMEND — finish the evidence record** | PR #504 supplied the positioning ADR but did not add the requested primary-source market research note, and ADR-0075 turns consultant-first into an accepted claim without completed discovery. Retitle to “Complete the evidence record for ADR-0075 and qualify buyer claims.” Add incumbent/product evidence, consultant/DOT interview findings, claim limits, and citations; update ADR only through a successor if the decision materially changes. |
| [#487](https://github.com/bharm16/corridor/issues/487) | Storage interface plus object-storage backend | **AMEND — retain, correct worker and disposition boundaries** | The code still writes directly to local paths and ADR-0079 requires object storage. This is pilot-critical. Keep the app-level content-addressed interface. Stage bytes locally for the isolated render subprocess; the parent persists inputs/outputs through object storage. Add conditional/idempotent writes, digest verification, migration, lifecycle/hold integration, and transactional reconciliation. |
| [#488](https://github.com/bharm16/corridor/issues/488) | Recurring operations under Due Work | **AMEND — finish and deploy only pilot-critical schedules** | The repository already has Due Work handlers for project processing, revision reconciliation, location discovery, notifications, outcome capture, and report publication. The ticket overstates the missing runtime. Rewrite around remaining gaps: connector checkpoint polling, retention sweep, delta generation, report preparation, production declarations, health/lag metrics, and restart drills. Defer broad notifications/escalations not needed by the pilot. |
| [#489](https://github.com/bharm16/corridor/issues/489) | Staging environment with web/worker/managed Postgres/object storage/migrations/backups | **AMEND — make it the design-partner shadow environment** | ADR-0079 now supplies the topology, but the ticket predates the corrected storage, identity, observability, and pilot requirements. Block on #461, #487, #490, #491, production authentication/project authorization, and backup restore proof. Deploy logical web/worker roles with replica-safe leases; prove migration, PITR/restore, object reconciliation, and customer isolation. |
| [#490](https://github.com/bharm16/corridor/issues/490) | Harden untrusted intake | **KEEP — active blocker** | The first connected source makes untrusted bytes a production boundary. Current intake needs bounded MIME/archive handling, quarantine, scanning, and worker limits. Keep. Add the object-store flow and a real scanner/provider decision before external data; retain fail-closed behavior and hostile-fixture tests. |
| [#491](https://github.com/bharm16/corridor/issues/491) | Structured logs and metrics | **AMEND — align with the actual pilot and value classes** | The new runbook has useful categories but misclassifies any reversal as a false write, assumes customer/project labels on every event, and omits several paid-pilot measures. Define low-cardinality labels; distinguish correction/new evidence from confirmed policy error; add source-arrival→capture→delta→decision latency, baseline/export time, coordinator/operations minutes, connector completeness, cost, and last-success age. Make report coverage gaps visible. |
| [#492](https://github.com/bharm16/corridor/issues/492) | Architecture test: authoritative record rows only writer modules | **AMEND — enforce separate append and authority boundaries** | A pure AST allowlist cannot prove ORM/raw-SQL authority, and Source Facts/Extracted Proposals are not accepted Project Record values. Define separate authorized appenders for segments/facts/proposals and accepted-record decisions. Use DB roles/SECURITY DEFINER functions for the canonical spine, static imports/construction allowlists as defense in depth, and runtime tests proving the app role cannot bypass the boundary. |
| [#493](https://github.com/bharm16/corridor/issues/493) | Present EvidenceLink.verified as Source Passage Check | **AMEND — block on corrected ADR-0077** | The label change is right, but ADR-0077’s proposed semantic-support model is incorrectly attached to evidence globally and overstates the four-layer chain. Limit this ticket to locator/source-passage validation and compatibility migration. Block semantic-support implementation until a successor to ADR-0077 places support on the fact/proposition-to-source relationship. |
| [#494](https://github.com/bharm16/corridor/issues/494) | One Work List reading for every proposed change | **AMEND — rebuild around Proposed Delta** | The body still preserves ADR-0029 automatic admission and treats legacy Extracted Proposals as the change model. Make Proposed Delta the unit. Show baseline value, incoming source value, exact source, affected fields/constraints/key dates when derivable, materiality facts, and allowed decisions. One item can group an atomic source change; no opaque score; stale deltas close/supersede deterministically. |
| [#495](https://github.com/bharm16/corridor/issues/495) | Export current Project Record in customer UCM layout | **AMEND — promote to first-slice output** | Customer-format round-trip is central to adoption, but the ticket assumes a generic canonical-to-header export and appends columns that may break customer templates. Use the adopted native workbook/export as the template. Preserve supported sheets, formulas, validation, formatting, hidden content, and unknown columns; write only mapped fields. Put provenance/change data in an optional sidecar or dedicated sheet. Bind output to baseline digest and Project Record revision. |
| [#496](https://github.com/bharm16/corridor/issues/496) | Connected-location contract and Box refit | **AMEND — pull connector only plus normalized ingress** | ADR-0078’s four-method contract fits pull locations but not webhook email or manual upload. Rename the interface PullConnector; define crash-safe checkpoint semantics (checkpoint only after durable storage through an exact token); normalize every result into a shared SourceEnvelope. Refit Box and leave push intake to a separate issue. |
| [#497](https://github.com/bharm16/corridor/issues/497) | Microsoft 365 connected location | **KEEP — deferred and customer-triggered** | M365 is a plausible first integration but must not be built from assumption. Keep blocked by #428, #496, and a signed design partner using SharePoint/OneDrive/shared mailbox. Implement only the required M365 surfaces and tenant-consent model. |
| [#498](https://github.com/bharm16/corridor/issues/498) | Pilot gate report | **AMEND — make it falsifiable** | The current criteria use “roughly,” omit sample size and operations cost, and let material misses/false writes be merely reported. Require declared partner/project counts, duration, source mix, baseline sample, comparison method, exact pass thresholds, material-field strata, coverage, latency, coordinator and Corridor-operations time, compute cost, and commercial outcome. A failed gate must produce the predeclared product response. |
| [#499](https://github.com/bharm16/corridor/issues/499) | Shadow comparison versus customer matrix revisions | **AMEND — compare Proposed Deltas, not old proposals** | This is valuable validation, but the customer matrix is a working reference, not unquestioned semantic gold, and the issue uses old pending-proposal semantics. Freeze baseline and successor versions, generate Proposed Deltas before seeing the customer update, then reconcile matches, Corridor-only findings, customer-only changes, and ambiguous cases with human review. Predeclare field matching, materiality, and denominator rules. |
| [#500](https://github.com/bharm16/corridor/issues/500) | Split route module | **CLOSE — defer refactor** | The 6,800-line route file is a real maintainability problem, but a broad move-only split immediately before the baseline/delta UI creates conflict without validating the product. Close for now. Reopen after the first-slice routes settle or when merge/conflict/review metrics show the file is actively blocking delivery. |
| [#501](https://github.com/bharm16/corridor/issues/501) | Split model module | **CLOSE — defer until spine retirement** | Splitting 149-table legacy-plus-spine models before ADR-0081 retirement would crystallize boundaries around structures scheduled for removal. Close. Recreate a post-cutover model-package ticket after #458, based on the surviving canonical schema. |
| [#502](https://github.com/bharm16/corridor/issues/502) | Reorganize package into bounded contexts | **CLOSE — premature umbrella** | This duplicates #500/#501 and asks for a broad package move while the target domain surface is still changing. Close. Revisit after the paid slice and canonical-spine cutover, using measured dependency/cohesion pain rather than a directory ideal. |
| [#503](https://github.com/bharm16/corridor/issues/503) | Decide retention governance, enterprise identity, and ADR-vs-policy split | **AMEND — retain only unresolved identity/authorization work** | PR #504 settled the ADR lifecycle and ADR-0080 attempted retention governance. The remaining material decision is production identity, membership, roles, deprovisioning, and customer isolation. Retitle to “Define pilot and enterprise identity, authorization, and deprovisioning.” Include customer/project membership, coordinator/releaser/operator roles, SSO timing, magic-link limits, service identities, offboarding, audit export, and one-database-per-customer control-plane access. |


# New issues to create


## N1. ADR-0082 — Provenance follows the value class; semantic support belongs to a proposition-source relation

**Priority:** P0 — before #493 or any semantic-support schema work

**Why:** ADR-0077 incorrectly requires the same four-layer chain for source-backed facts, derivations, Coordination Decisions, and Recorded Verbal Statements, and places semantic support too close to a globally reusable passage.

**Acceptance criteria:**

- Create a successor ADR to ADR-0077 and mark ADR-0077 superseded.
- Define separate valid provenance paths for source-backed facts, Recorded Verbal Statements, Coordination Decisions, and Derivations.
- Model semantic support on a typed relation between one proposition/fact/proposal and one or more Source Segments, with role and assessment; never as a global property of an EvidenceLink or Source Segment.
- Define who may record a support assessment and keep it distinct from the decision about what the Project Record currently shows.
- Keep `verified` only as a compatibility projection of locator/source-passage validation.
- Update ADR-0003/0017 relationships, report verification rules, and migration notes.


**Blocks / feeds:** #493, semantic-support implementation, pilot provenance QA


## N2. Fix ADR index semantics, lifecycle validation, and server-side ADR checks

**Priority:** P0 — immediately

**Why:** The generated index calls the numerically newest active ADR “governing,” includes proposed and accepted-historical ADRs as active, and therefore gives agents incorrect authority guidance.

**Acceptance criteria:**

- Remove numerical inference of a governing ADR; show accepted current/optional decisions and supersession-graph leaves instead, or require explicit `governs` metadata.
- List proposed ADRs separately and never let a proposed ADR supersede an accepted decision.
- Exclude `scope: historical` from the active table.
- Reject duplicate ADR numbers, invalid domains/scopes, unknown frontmatter keys, malformed references, and nonreciprocal relationships.
- Permit deprecated ADRs to retain truthful `supersedes` history while prohibiting a deprecated ADR from naming a successor through status.
- Require the architecture check as a GitHub status on ADR PRs; add branch/ruleset protection or an equivalent repository rule.


**Blocks / feeds:** reliance on docs/adr/INDEX.md by AGENTS.md, #459 roadmap rewrite


## N3. Replace the current Phase 1 roadmap and harden the paid-pilot contract

**Priority:** P0 — before reprioritizing implementation

**Why:** The roadmap still treats the old M0–M9 readiness-ledger sequence and exit criteria as operative, while the pilot criteria use non-falsifiable thresholds and omit operational cost, coverage, and latency.

**Acceptance criteria:**

- Move the prior M0–M9 roadmap to a clearly historical document; do not leave it as current sequencing.
- Publish a current roadmap: ADR corrections → design partner → staging foundations → Adopt Baseline → capture/delta/resolve → customer-format export/change summary/chase list/report → shadow pilot → gate decision → spine retirement.
- Declare exact partner/project counts, duration, source classes, sample sizes, and baseline measurement method.
- Set exact pass/fail thresholds for net coordinator time, Corridor operations time, latency, coverage, material false writes, material misses, false/low-value alerts, cost, and paid continuation.
- State the predeclared response to each failure mode.
- Update README, #459, and issue dependencies to point only at the new roadmap.


**Blocks / feeds:** #459, #498, #499, pilot start


## N4. Research and adopt glossary entries for Source Fact, Adopt Baseline, Proposed Delta, and Resolve Delta

**Priority:** P0/P1 — before customer UI copy

**Why:** PR #504 deliberately left the four new terms outside the governed glossary even though they now define the product.

**Acceptance criteria:**

- Follow docs/agents/domain.md and record primary-practice research plus the gap where Corridor uses a product-specific term.
- Define each term, its authority boundary, customer-facing label, retained implementation identifiers, and relationships to Extracted Proposal, Human Record Decision, Source Discrepancy, and Project Record revision.
- Update CONTEXT.md/CONTEXT-MAP.md and add the required ADR relationship without globally renaming historical identifiers.
- Add terminology tests or index checks required by the existing procedure.


**Blocks / feeds:** customer-facing #494, #495 copy


## N5. Adopt Baseline — preview and atomically import one customer UCM or system export

**Priority:** P0 — first product implementation

**Why:** This is the missing commercial onboarding act and replaces generic setup confirmation.

**Acceptance criteria:**

- Accept one exact native UCM/system export revision and retain its bytes/digest, source identity, sheet/record scope, importer, customer, project, exclusions, and mapping version.
- Produce a dry-run preview with row identity, duplicates, unmapped columns, unsupported values, excluded rows, and proposed Project Record subjects before authority changes.
- Require a named human to adopt the complete preview; write one atomic Project Record revision containing the baseline decisions or write nothing.
- Do not require per-row clicks and do not silently discard unknown columns or duplicate identifiers.
- Support idempotent replay and refuse changed bytes, stale previews, cross-project rows, or partial commits.
- Create the initial customer-format export fixture used by #495 and baseline sample used by #499.


**Blocks / feeds:** #494, #495, #499, paid pilot


## N6. Proposed Delta and Resolve Delta — canonical backend lifecycle

**Priority:** P0 — first product implementation

**Why:** The repository has Extracted Proposals and accepted FactDecisions but no durable baseline-comparison object or resolution lifecycle.

**Acceptance criteria:**

- Create typed Proposed Delta identities for new subject, changed field, timing change, owner/organization change, apparent removal, contradiction, schedule/key-date change, and support-only/no-semantic-change.
- Bind each delta to accepted baseline/current revision, incoming source revision/run/facts, comparison rule version, affected fields/subjects, and any deterministically derived impact.
- Define coalescing, supersession, stale-input refusal, reopening, and one-item grouping; the same source/version cannot create duplicate live deltas.
- Implement human accept, edit, reject, and defer commands plus only the exact automatic no-semantic-change/support-transfer classes allowed by ADR-0076.
- Each resolution writes one atomic Project Record revision with original decision authority and never mutates the source fact or delta history.
- Expose query seams for #494, #498, and #499 and migrate no legacy-only behavior into the new model.


**Blocks / feeds:** #494, #498, #499, pilot


## N7. Project-bound push intake and normalized SourceEnvelope

**Priority:** P1 — before live email/webhook intake

**Why:** ADR-0078’s pull connector contract does not fit push email/manual upload, and current code receives one global address then infers project identity from content.

**Acceptance criteria:**

- Define a SourceEnvelope shared after ingress: customer, project, channel, external identity/version, original timestamps, content digest/bytes reference, metadata, delivery identity, and idempotency key.
- Define a PushIntake contract separate from #496’s PullConnector.
- Bind inbound alias/webhook credentials to customer and project before body/attachment parsing; content-based inference may only route within an already bound boundary or enter bounded triage.
- Migrate the global-address path without losing raw MIME/thread history; prevent a message from moving between customer/project boundaries.
- Persist raw bytes through #487’s storage interface, not a direct Path write.
- Prove duplicate delivery, crash/retry, spoofed thread headers, wrong alias, ambiguous in-project evidence, and cross-customer refusal.


**Blocks / feeds:** #455, live mailbox pilot, #497 shared-mailbox mode


## N8. ADR-0081 stage 1 — replace legacy statement_id with a spine-native source-origin identity

**Priority:** P0 — before new statement features

**Why:** Current SourceSegments and fact identities still depend on legacy `dependency_events`, directly violating the target model.

**Acceptance criteria:**

- Introduce a spine-native Recorded Verbal source-origin/attestation identity carrying project, recorder, recorded time, conversation time, exact words/digest, correction lineage, and stable identifier.
- Point recorded-verbal Source Segments and Fact identity at the spine-native origin, not legacy statement rows.
- Backfill existing dual-written verbals with an attributable migration receipt and exact one-to-one reconciliation.
- Keep legacy foreign keys only in a temporary compatibility mapping, not in target tables or identity hashes.
- Update retention/hold reachability, current/as-of reads, idempotency, correction tests, and migration fingerprint.


**Blocks / feeds:** #458, new statement-source work


## N9. ADR-0081 stage 2 — backfill legacy history while preserving original authorship and authority

**Priority:** P1 — before reader cutover

**Why:** Migrating only active conclusions cannot support as-of history, audit reconstruction, or semantic-equivalence gates, and the migration executor must not become the semantic author.

**Acceptance criteria:**

- Inventory every legacy authoritative decision/support/disposition/history class and map it to spine-native facts, decisions, revisions, or explicit retained compatibility history.
- Preserve original human/system actor, event time, source identity, and decision type; record the migration executor separately.
- Declare which historical classes are fully migrated versus intentionally served through a compatibility reader, with expiry criteria.
- Produce per-project counts/digests and reversible migration receipts; reruns are idempotent.
- Prove current and selected as-of readings, corrections, Do Not Add/restore, discrepancy resolution, support designation, and verbal/statement lineage.


**Blocks / feeds:** #458, ADR-0081 reader cutover


## N10. Pilot customer-environment disposition, legal hold, and backup-expiration contract

**Priority:** P1 — before external pilot data

**Why:** ADR-0080 requires a full granular disposition system before the first commercial record, but one-database-per-customer permits a safer, smaller pilot boundary; its current design also leaves receipt custody and backup deletion unresolved.

**Acceptance criteria:**

- Create the necessary successor/amendment to ADR-0080: pilot disposition is export-and-destroy of the complete customer environment unless a contract requires project/record-class granularity.
- Define hold authority, precedence, scope, export/custody transfer, external compliance receipt storage, database/object-store deletion, key destruction where used, PITR/snapshot expiration, and completion states.
- Prevent source/evidence disposal while a retained decision or released artifact still requires dereference unless custody was transferred and the retained record explicitly discloses unavailability.
- Implement a dry-run manifest, stale-plan check, resumable execution/saga, partial-failure recovery, and restore/backups verification.
- Defer generic row-class deletion across all tables until a signed customer requirement justifies it.


**Blocks / feeds:** #489, external pilot onboarding, granular ADR-0080 implementation


# Recommended dependency order

## Phase 0 — correct the constitution
1. New ADR-0077 successor and provenance model.
2. ADR index/lifecycle fix and server-side ADR checks.
3. Current roadmap + exact pilot contract.
4. Governed glossary entries.
5. Correct ADR-0078/0080/0081 details through the lifecycle policy.

Do not begin `semantic_support_status`, granular disposition, or change-inbox schema work before these are settled.

## Phase 1 — secure the design partner and deploy the safe shell
1. Amend #428 and complete actual buyer/design-partner discovery.
2. Resolve #461.
3. Build #487, #490, #491, and amended #489.
4. Amend #496; build the project-bound push intake issue only for channels the partner needs.
5. Define production identity in amended #503.

## Phase 2 — build the paid vertical slice on the spine
1. Adopt Baseline.
2. Proposed Delta + Resolve Delta backend.
3. Amend/build #450, #455, and #456 only for partner source classes.
4. Amend/build #494.
5. Amend/build #495.
6. Amend/build narrowed #425.

Every new product capability should write the spine first. A compatibility write may preserve the legacy reader during migration, but no capability may exist only in legacy tables.

## Phase 3 — run the shadow pilot
1. Amend #424, #498, and #499.
2. Freeze each baseline and incoming-source event before coordinator action.
3. Measure coordinator time, Corridor operations time, latency, coverage, material errors, false/low-value work, and cost.
4. Produce the predeclared continue/narrow/stop decision.

## Phase 4 — finish canonical cutover
1. Spine-native verbal/source origin.
2. Historical migration preserving authorship.
3. Amended #457.
4. Semantic-equivalence reader gate and reader switch.
5. Amended #458 writer switch, bounded shadow, rollback decision, and legacy retirement.
6. Only then reconsider #500–#502 as fresh, post-cutover refactors.

---

# Recommended updated active backlog

After the closures and additions, the active near-term backlog should be approximately:

- ADR/provenance/index correction
- roadmap/pilot contract correction
- glossary terms
- #428 buyer/design-partner validation
- #461 licensing
- #487 storage
- #490 intake security
- #491 observability
- #503 identity/authorization
- #489 staging
- Adopt Baseline
- Proposed Delta / Resolve Delta
- #496 pull connectors
- project-bound push intake
- #450 schedule source if required
- #455 email source if required
- #456 minutes source if required
- #494 change inbox
- #495 customer UCM export
- #425 chase list
- #424 economics
- #498 pilot report
- #499 shadow comparison
- spine-native source identity
- historical backfill
- #457 dedup
- #458 cutover

Everything else should be closed as completed, superseded, or deferred behind measured pilot evidence.

---

# Final recommendation

Treat PR #504 as the **strategic pivot record**, not as a completed architecture convergence.

The correct next PR is not a feature. It is a small corrective constitution PR that:
- supersedes ADR-0077;
- fixes ADR index authority semantics;
- replaces the current roadmap;
- hardens the pilot contract;
- records the four new terms;
- qualifies ADR-0075's buyer claim;
- splits ADR-0078 into normalized ingress, pull, and push contracts;
- narrows ADR-0080 to a safe pilot disposition boundary;
- strengthens ADR-0081's history, authorship, equivalence, and rollback gates.

After that, the first implementation should be `Adopt Baseline`, followed immediately by the canonical Proposed Delta/Resolve Delta lifecycle. That is the shortest path from the current codebase to the product the new README now promises.
