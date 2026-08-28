---
status: accepted
---

# Authority follows proof: policy writes exact cases, agents assist, and humans decide ambiguity

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Corridor minimizes human work without confusing model confidence with authority.
A named, versioned deterministic policy may write only when authoritative inputs
and replayable rules prove one outcome. Model assistance may read, rank, prefill,
summarize, question, and abstain, but it never promotes its own semantic judgment
into Ledger state. Human Record Decisions cover the unresolved work that requires
attribution judgment, scope choice, sufficiency judgment, or project
accountability.

This decision consolidates the authority boundaries spread across ADR-0022,
ADR-0026, ADR-0029, ADR-0035, ADR-0037, ADR-0039, ADR-0040, and ADR-0041. Its
research basis is recorded in
[`docs/research/agent-authority-boundary-2026-08-23.md`](../research/agent-authority-boundary-2026-08-23.md).

## Authority table

| Domain act | Allowed author | Required proof | Required durable record |
| --- | --- | --- | --- |
| Create an Extracted Proposal | Extractor, including a model-backed extractor | Registered source identity, cited pages, prompt/model lineage | Extraction Run and immutable Extracted Proposal payload |
| Admit a Constraint or External Organization statement in an enumerated exact class | Named deterministic Record Inclusion policy | Current Production Run, verified Supporting Documentation, one complete replayable eligibility result | Policy version and digest, run, per-proposal outcome, exact created identities |
| Carry established Supporting Documentation in Use to exact unchanged current Supporting Documentation | Automatic Support Update Rules | Released policy version and digest, exact unique unchanged correspondence, current Supporting Documentation | Support Update Run Record and per-row carried or abstained outcome |
| Produce a Statement Review Assistant packet | Model assistance | Server-bound Extracted Proposal, project, Supporting Documentation, opaque references, budgets, deterministic validation | Non-authoritative investigation run, redacted steps, supported options or an explicit unsupported-answer outcome; execution failure remains a failure |
| Choose one, selected, or all-active statement scope when Supporting Documentation does not already prove it | Human Record Decision | Visible verified Supporting Documentation and current human-readable Constraint choices | Scope decision, principal, guided grouping receipt |
| Choose Do Not Add for an Extracted Proposal, remove an incorrect Constraint from the active log, or resolve a Source Discrepancy | Human Record Decision | The current record, Supporting Documentation, and structured reason or conclusion | Attributable append-only disposition, removal, or Discrepancy Resolution |
| Perform a Documentation Review | Human judgment; Automatic Support Update may inherit an earlier exact judgment | Exact verified Supporting Documentation against the stated Required Documentation | Documentation Review receipt and principal, or exact support-update receipt |
| Set Assigned To, Next Action, Action Due Date, or Effect on Key Dates | Human Coordination Decision | Current Coordination Subject and project context | Append-only Coordination Decision and principal |
| Release an external Coordination Report | Designated human | Fixed rendered artifact, Evaluation, provenance coverage, and release review | Report Approved for Release receipt naming exact bytes and releaser |

Human involvement is not a clerical confirmation step. ADR-0035 applies across
this table: known facts render read-only; only the smallest unresolved decision
is asked; and a gap in Supporting Documentation or authority keeps the Extracted Proposal
pending instead of becoming a blank form.

## Confidence is not proof

A model probability, self-reported confidence, agreement between models, or
second model review is not a Record Inclusion predicate. Confidence may order work or
trigger demotion, but it cannot supply Supporting Documentation, resolve ambiguity, establish a
project Coordination Decision, or authorize a write.

A learned promote/defer boundary may be reconsidered only after it has exact
class-specific outcome labels, calibrated risk/coverage measurements, a false-
write ceiling, and the same immutable input/output receipts as a deterministic
policy. Until then, the model side of the boundary remains recommendation and
Abstention only.

## First new automatic class: attributable party-level Commitment with unknown scope

The first extension to Event Record Inclusion records a real party-level Commitment
without asking a person to choose **Applies To: Not yet known** when that is
already the only honest scope result.

The policy may admit this class only when all of these predicates hold:

1. the current event Extracted Proposal belongs to the declared Current Production Run;
2. every Extracted Proposal citation is mechanically verified against registered Supporting Documentation;
3. the Extracted Proposal proposes one Commitment, not a Change to Promised Timing or closure;
4. exact Supporting Documentation supports one nonempty description and one timing at its stated precision;
5. the stated External Organization resolves through one exact registered name or alias and is not the project side;
6. the affected External Organization is the same resolved party;
7. no previous timing is present;
8. no conflict reference is present; a present but missing, ambiguous, or mismatched reference remains human scope work; and
9. every ordinary statement validator and current-state fingerprint check passes under the project lock.

The result is one attributable External Organization Commitment with
`Commitment Scope not yet known`, shown as **Applies To: Not yet known**. It creates
no Constraint projection and no Follow-up Plan. The recorded unknown scope remains
visible as an Attention Reason; it is not another confirmation question. The
accepted Commitment appears as one coordination item for the remaining work, such
as the assigned person or Next Action. A later scope correction requires the
explicit Correct flow and its verified source support.

The policy receipt binds the Extracted Proposal, project, source Document, Current Production Run,
Supporting Documentation quote and page, resolved External Organization, timing wording and precision,
policy version and digest, eligibility fingerprint, created Commitment Lineage,
statement event, unknown-scope decision, and Extracted Proposal disposition. Repeating the
same versioned input returns the existing result or a zero-new-outcome run; it
cannot duplicate the Commitment.

The reason vocabulary must distinguish at least:

- citation or Supporting Documentation failure;
- event type outside this class;
- missing, fuzzy, ambiguous, or project-side stated party;
- affected-party disagreement;
- missing or invalid timing or precision;
- previous timing present;
- conflict reference present;
- stale Current Production Run, Extracted Proposal, Supporting Documentation, party, or project state; and
- statement validator or write-integrity failure.

Every failed eligibility predicate in a completed policy assessment produces a
Record Inclusion Abstention and leaves the Extracted Proposal
pending. A model may add suspicion and demote an otherwise eligible Extracted Proposal,
but it cannot make an ineligible Extracted Proposal pass.

The Statement Review Assistant retains the internal Evidence Investigator
identifiers and its read-only authority. A timeout, exhausted budget, or failed
harness is an execution failure, not a successful policy Abstention. The name does
not alter either record-writing permissions or the terminal validation boundary.

## Expansion rule

Future automatic classes are added one at a time. Each addition must name:

- the exact domain act;
- authoritative inputs and deterministic predicates;
- the system actor and policy digest;
- the created and protected state;
- the Abstention reasons;
- idempotency, stale-state, and project-isolation checks;
- reversibility or compensation behavior;
- false-write, correction, and incident measures; and
- the evidence that justified removing that class from human work.

An automatic class is suspended when its policy code changes without a new
version, its receipts cannot be reproduced, a protected-state or project-
isolation check fails, or measured false writes exceed its declared ceiling.

## Considered options

**Human approval for every case.** Rejected. It creates clerical queues, invites
rubber-stamping, and contradicts existing mechanical Record Inclusion and Automatic Support Update
policies. A click adds no independent truth when exact rules already prove the
outcome.

**Unrestricted model authority.** Rejected. Model behavior and confidence are not
replayable proof, and semantic mistakes would enter an authoritative project
record without a defensible author.

**Model confidence above a threshold.** Rejected. Confidence is not necessarily
calibrated, and selective prediction requires its own outcome data, risk/coverage
design, and monitoring. A threshold cannot replace a proof obligation.

**Deterministic policy automation with fail-closed Abstention.** Accepted. It
matches Corridor's provenance model, removes mechanical human labor, and keeps
ambiguous or accountable judgment visible without forcing a conclusion.

## Consequences

Event Record Inclusion is amended to support the exact unknown-scope Commitment class
above. ADR-0041 still forbids a model or software operator from impersonating a
human, but it does not prevent an explicitly authorized deterministic policy from
writing under its own system identity. ADR-0037, ADR-0039, and ADR-0040 continue
to reserve Documentation Review, residual statement Human Record Decision and Coordination Decisions, and Coordination Report
release to their named human authorities.

The success measure is not maximum automation. It is minimum human residue at a
declared false-write ceiling, with every automatic outcome reproducible and every
Abstention specific enough to become one bounded Work Item.
