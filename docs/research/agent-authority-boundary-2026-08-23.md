# Agent Authority Boundary for Corridor

Researched on August 23, 2026.

Question: what is the best authority boundary for Corridor if the goal is to remove unnecessary human work without weakening provenance, auditability, or product truth?

Decision objective: choose between four broad shapes for statement and coordination work:

1. human approval for everything;
2. unrestricted agent autonomy;
3. model confidence threshold autonomy;
4. deterministic policy automation for narrow provable classes, with abstention on ambiguity.

## Decision summary

The best-supported choice is:

- use deterministic policy automation for narrow, enumerated, replayable classes;
- keep the Evidence Investigator read-only and non-authoritative;
- reserve human authorship for decisions that still require judgment, attribution, scope selection, sufficiency judgment, or project accountability;
- turn every residual ambiguity into structured pending work with cited options, not blank text entry.

For Corridor, that means the right answer is not "human clicks everything" and not "the agent finishes the case when it feels confident." The right answer is:

- `system-policy` may write only where authoritative inputs plus versioned deterministic rules prove one outcome;
- `agent-assist` may rank, prefill, summarize, and abstain, but may not promote itself into Ledger state;
- `human` remains the author for statement acceptance, Commitment Scope choice, Ready judgment, Work Decisions, and report release.

## Repo facts that constrain the decision

Corridor already separates mechanical proof from human judgment.

- [ADR-0029](../adr/0029-admission-is-mechanical-on-verified-evidence-no-sign-off-gates-the-product.md) moves dependency admission to a mechanical path when the machine can anchor the row on verified Evidence, and keeps immutable receipts.
- [ADR-0026](../adr/0026-events-enter-by-authorized-deterministic-policy-a-model-may-only-demote.md) keeps the key rule for statements: deterministic policy may admit; a model may only demote to the human pile.
- [ADR-0034](../adr/0034-customer-work-is-domain-work-and-technical-operations-are-managed.md) says exact unchanged carry-forward is normal fail-closed processing under Corridor-managed versioned rules, while changed or ambiguous input abstains and leaves the Ledger unchanged.
- [ADR-0037](../adr/0037-ready-judgment-is-an-explicit-human-act.md) keeps Ready as a human judgment because exact text presence does not prove that the evidence satisfies the project’s sufficiency bar.
- [ADR-0041](../adr/0041-interface-operation-is-not-human-decision-authorship.md) forbids a software agent from originating a human-gated write under a human identity.
- `src/corridor/web/app.py` opens with the current UI seam: “The only path from a Candidate into the Ledger is a human keystroke” ([src/corridor/web/app.py](../../src/corridor/web/app.py)).
- `CONTEXT.md` defines Automatic Carry-Forward as fail-closed and explicitly says it never changes the adjudicated conclusion or originates readiness ([CONTEXT.md](../../CONTEXT.md)).

Observed consequence: Corridor is already designed around bounded policy automation plus abstention. The unresolved question is not whether automation is allowed. The unresolved question is which decision classes are actually provable.

## Primary-source findings

### 1. Oversight should match risk and role, not ideology

NIST’s AI RMF says organizations should document roles and responsibilities for human-AI configurations and oversight, and make leadership accountable for risk decisions. See:

- NIST AI RMF Core, `Govern 2.1`, `Govern 2.3`, and `Govern 3.2`:
  - https://airc.nist.gov/airmf-resources/airmf/5-sec-core/
- NIST AI RMF Appendix C:
  - https://airc.nist.gov/airmf-resources/airmf/appendices/app-c-ai-risk-management-and-human-ai-interaction/

Appendix C is especially useful for this decision. It says human-AI configurations span from fully autonomous to fully manual, some systems do not require human oversight, some specifically do, and roles should be clearly defined and differentiated. It also warns that human-AI interaction can either improve or worsen outcomes depending on how the team is organized.

NIST’s Generative AI Profile adds that generative systems may need different levels of oversight and may warrant extra human review, tracking, documentation, and management oversight:

- NIST AI 600-1:
  - https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf

Implication for Corridor: the right control is not “always require a person” and not “let the model decide.” It is an explicit authority matrix tied to risk, reversibility, and proof quality.

### 2. Deterministic workflows should stay deterministic when they are enough

OpenAI’s guide to building agents says agents are best for workflows where deterministic and rule-based approaches fall short, and says plainly that otherwise a deterministic solution may suffice. The same guide recommends risk-rating tools based on read-only versus write access, reversibility, permissions, and financial impact, and escalating to a human for high-risk actions when needed.

Source:

- OpenAI, *A practical guide to building agents*:
  - https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/

Implication for Corridor: if a case class can be proved mechanically, use policy code and receipts. Do not route it through an LLM just because an LLM is available. If a tool can write Ledger state, its risk is higher by definition than a read-only investigation tool.

### 3. Abstention can improve reliability, but it is not the same thing as raw confidence

Selective prediction literature supports abstention, but not the naive version.

- Geifman and El-Yaniv 2017 show selective classification improves performance by trading off coverage and risk:
  - https://arxiv.org/abs/1705.08500
- SelectiveNet 2019 says common rejection mechanisms are mostly confidence thresholds on a pre-trained network, and proposes end-to-end training because that baseline is weaker:
  - https://arxiv.org/abs/1901.09192
- Mozannar and Sontag 2020 formalize learning to defer to an expert from samples of expert decisions:
  - https://proceedings.mlr.press/v119/mozannar20b.html
- Guo et al. 2017 show modern neural networks are often poorly calibrated:
  - https://proceedings.mlr.press/v70/guo17a.html

Implication for Corridor:

- “abstain on ambiguity” is a defensible pattern;
- “use a high confidence threshold to decide when the model may write” is not;
- if Corridor ever wants a learned defer/promote boundary, it would need explicit training targets, real human outcome data, and class-specific evaluation, not a generic confidence slider.

### 4. Transportation and utility work still assigns accountable humans to ambiguous decisions

FHWA guidance is blunt about accountable project roles:

- FHWA supervising agency guidance says the State DOT must provide an “engineer in responsible charge” and says consultants cannot make critical project decisions that are best made by the agency on the public’s behalf:
  - https://www.fhwa.dot.gov/federal-aidessentials/companionresources/15supervising.pdf
- FHWA SUE scope says utility coordination work includes exercising professional judgment to correlate data from different sources and resolve conflicting information:
  - https://www.fhwa.dot.gov/programadmin/htmldoc4.cfm

Implication for Corridor: in the exact domain Corridor serves, conflicting evidence and project-consequential coordination decisions are still treated as accountable human work. That does not argue against automation. It argues for a boundary: automate transfer and proof work; keep accountable judgment human.

TxDOT guidance points the same way for consequential utility records:

- utility agreements have assigned approval authority and executed agreement
  assemblies; major scope changes require an amendment:
  - https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process/6-4-3-utility-agreements.html
  - https://www.txdot.gov/manuals/row/utl/chapter-9--forms-and-agreements/section-5--prepare-and-submit-the-agreement-assemb.html
- utility plans supporting cost participation should be signed and sealed by an
  engineer:
  - https://www.txdot.gov/manuals/row/utl/chapter-6--utility-plans-and-specifications/section-2--utility-plan-preparation.html
- FHWA requires determinations and source documents to become matters of record
  and calls for complete audit trails:
  - https://www.ecfr.gov/current/title-23/chapter-I/subchapter-G/part-635/subpart-A/section-635.123
  - https://www.fhwa.dot.gov/construction/cpmi04d1.cfm

Limitation: these rules govern official approvals, engineering products, and
project administration. They do not directly regulate an internal software
classification. They support named authority and durable records for
consequential acts; they do not justify making a person re-key facts or approve
every low-risk internal projection.

## Option comparison

| Option | Evidence quality | Main upside | Main failure |
| --- | --- | --- | --- |
| Human approval for everything | High | Low false-autonomy risk | Product collapses into clerical queue; contradicts Corridor ADRs and known throughput constraints |
| Unrestricted agent decisions | High | Maximum automation | Breaks authorship, provenance, and replayability; weakens audit truth |
| Confidence-threshold autonomy | High | Simple to ship | Confidence is not proof; selective-prediction literature treats reject logic as its own evaluated system |
| Deterministic policy automation with abstention | High | Matches repo architecture, receipts, and fail-closed behavior | Requires careful scoping and explicit abstention vocabulary |

Recommendation strength: strong in favor of option 4.

## Corridor-specific recommendation

### 1. Keep the Evidence Investigator non-authoritative

The Evidence Investigator should remain a bounded reader and packet-builder. It can:

- read only bound Evidence and project-scoped context;
- rank likely parties and dependencies;
- surface contradictions;
- propose a structured packet;
- abstain explicitly when source support is insufficient.

It should not:

- accept a statement;
- choose Commitment Scope;
- create or change a Work Decision;
- mark Ready;
- release a report;
- write under a human identity.

This is consistent with [ADR-0041](../adr/0041-interface-operation-is-not-human-decision-authorship.md), the read-only harness design already recorded in memory, and the current Evidence Investigator shadow setup.

### 2. Expand automation through policy families, not agent discretion

If Corridor wants less human work, the next automation step should be:

- define one narrow decision class;
- define exact proof obligations;
- implement it as deterministic code with a named system actor;
- emit immutable per-row outcomes and abstentions;
- measure false-write and undo rates on real hidden-shadow cases.

That is how dependency admission and carry-forward already work. It is the right shape for more automation.

### 3. Keep these decision classes human-authored

Evidence supports keeping these human for now:

- statement acceptance when attribution needs judgment;
- Commitment Scope choice among one, selected, all-active, or unknown;
- Ready sufficiency judgment;
- Work Decisions like Internal Owner, Next Action, Action Due Date, and Milestone Impact;
- external report release.

Reason: each of these either resolves conflicting information, applies project accountability, or asserts sufficiency that exact text presence alone does not prove.

### 4. Remove blank-field clerical work immediately

This is not a separate product philosophy question. The repo already decided it.

[ADR-0035](../adr/0035-human-work-is-attributable-guided-and-reversible.md) says:

- known facts should render as read-only context;
- the product must not present a blank text field for a known fact;
- human work should start only at a real unresolved decision;
- if Corridor lacks supported facts or cannot offer a safe structured choice, the Candidate stays pending rather than turning into an empty form.

Current UI state still violates that decision in places:

- `_coordinate.html` has blank owner, action, and due-date inputs for post-admission coordination ([src/corridor/web/templates/_coordinate.html](../../src/corridor/web/templates/_coordinate.html)).
- `statement_coordinate.html` is better, but it still makes the user perform too much reconstruction work in one form instead of shrinking the task to the smallest missing decision ([src/corridor/web/templates/statement_coordinate.html](../../src/corridor/web/templates/statement_coordinate.html)).

So the right product move is:

- prefill and bind all known facts server-side;
- present only the unresolved choice;
- if no safe choice exists, abstain and state the exact evidence gap;
- never ask the user to retype what Corridor already knows.

## Confidence-rated claims

### Claim 1

High confidence: Corridor should not grant unrestricted model-originated write authority for statement adjudication or Work Decisions.

Why:

- repo ADRs reject it;
- NIST requires explicit human-AI role definitions and oversight choices;
- FHWA guidance keeps critical project decisions with accountable humans.

### Claim 2

High confidence: Corridor should keep expanding automation through deterministic policy families with receipts and abstentions.

Why:

- this already matches dependency admission and carry-forward;
- OpenAI’s own guidance says deterministic solutions should stay deterministic when they suffice;
- this preserves replayability and makes failures inspectable.

### Claim 3

High confidence: raw confidence thresholds are not a safe promotion rule.

Why:

- modern models are often poorly calibrated;
- selective-classification work treats abstention as a separate risk/coverage design problem;
- learning-to-defer requires observed expert decisions and explicit training/evaluation.

### Claim 4

High confidence: the blank-field problem is a product bug, not a necessary cost of human oversight.

Why:

- ADR-0035 already rejects blank fields for known facts;
- structured cited choices are compatible with truthful human judgment;
- blank forms create clerical work without adding independent evidence.

## Recommendation

Adopt this explicit rule:

1. `system-policy` may write only for named classes where authoritative inputs plus deterministic rules prove one outcome.
2. `agent-assist` may read, rank, prefill, summarize, and abstain, but may not promote itself into Ledger state.
3. `human` remains the author for every class that still requires attribution judgment, scope choice, sufficiency judgment, or project-accountability judgment.
4. Every unresolved case becomes structured pending work with cited options or an explicit evidence gap, never a blank text workflow.

For the current product argument, that translates to:

- yes to eliminating empty text entry and clerical restatement;
- yes to stronger server-bound prefill and constrained choices;
- yes to deterministic automation for additional provable case classes;
- no to giving the Evidence Investigator autonomous write authority;
- no to confidence-threshold autonomy.

## High-impact unknowns

- Which statement-coordination cases are actually mechanical after party search, dependency shortlist, and quote validation?
- What false-write types matter most in practice: false attach, false scope expansion, false readiness, or false dismissal?
- How much human residue remains after aggressive prefill and option reduction?
- Which auto-writes are operationally reversible, not just technically reversible?

These unknowns can change how much automation Corridor should add. They do not support broad agent autonomy today.

## Falsification criteria

This recommendation should be revisited only if Corridor can show, on real hidden-shadow data, that a narrower learned or hybrid promote/defer rule:

- beats the current human-plus-policy baseline on materially important errors;
- preserves 100% citation and validator validity for non-abstaining outputs;
- does not increase false attaches, false scope expansion, or downstream undo/correct work;
- keeps authorship truthful;
- and is implemented with explicit receipts and monitoring rather than opaque confidence alone.

## Next step

The best next move is not another broad debate. It is one narrow product and architecture decision:

- write one ADR that lists each decision class, its allowed actor, proof bar, receipt type, abstention reasons, and reversibility requirements;
- then redesign the current statement and coordination screens so they ask only the smallest missing human decision and eliminate blank-field reconstruction work.
