---
status: accepted
---

# Authority follows proof: policy writes exact cases, agents assist, and humans decide ambiguity

Corridor minimizes human work without confusing model confidence with authority.
A named, versioned deterministic policy may write only when authoritative inputs
and replayable rules prove one outcome. Model assistance may read, rank, prefill,
summarize, question, and abstain, but it never promotes its own semantic judgment
into Ledger state. Adjudication remains human only for the residue that requires
attribution judgment, scope choice, sufficiency judgment, or project
accountability.

This decision consolidates the authority boundaries spread across ADR-0022,
ADR-0026, ADR-0029, ADR-0035, ADR-0037, ADR-0039, ADR-0040, and ADR-0041. Its
research basis is recorded in
[`docs/research/agent-authority-boundary-2026-08-23.md`](../research/agent-authority-boundary-2026-08-23.md).

## Authority table

| Domain act | Allowed author | Required proof | Required durable record |
| --- | --- | --- | --- |
| Create a Candidate | Extractor, including a model-backed extractor | Registered source identity, cited pages, prompt/model lineage | Extraction Run and immutable Candidate payload |
| Admit a Dependency or External Party statement in an enumerated exact class | Named deterministic Admission policy | Current Active Run, verified Evidence, one complete replayable eligibility result | Policy version and digest, run, per-Candidate outcome, exact created identities |
| Carry established Operative Support to exact unchanged current Evidence | Carry-Forward Policy | Released policy version and digest, exact unique unchanged correspondence, current Evidence | Carry-Forward Run and per-row carried or abstained outcome |
| Produce an Evidence Investigator packet | Model assistance | Server-bound Candidate, project, Evidence, opaque references, budgets, deterministic validation | Non-authoritative investigation run, redacted steps, packet or explicit Abstention |
| Choose one, selected, or all-active Commitment Scope when Evidence does not already prove it | Human Adjudication | Visible verified Evidence and current human-readable Dependency choices | Scope decision, principal, guided grouping receipt |
| Mark a Candidate Not Relevant, dismiss a Dependency, or settle a Dispute | Human Adjudication | The current record, Evidence, and structured reason or conclusion | Attributable append-only disposition, dismissal, or Settlement |
| Judge Ready | Human judgment; Automatic Carry-Forward may inherit an earlier exact judgment | Verified Evidence against the project-specific sufficiency bar | Evidence sufficiency receipt and principal, or exact carry-forward receipt |
| Set Internal Owner, Next Action, Action Due Date, or Milestone Impact | Human Work Decision | Current Coordination Subject and project context | Append-only Work Decision and principal |
| Release an external Report | Designated human | Fixed rendered artifact, Evaluation, provenance coverage, and release review | Approved Export receipt naming exact bytes and releaser |

Human involvement is not a clerical confirmation step. ADR-0035 applies across
this table: known facts render read-only; only the smallest unresolved decision
is asked; and an Evidence or authority gap keeps the Candidate pending instead
of becoming a blank form.

## Confidence is not proof

A model probability, self-reported confidence, agreement between models, or
second model review is not an Admission predicate. Confidence may order work or
trigger demotion, but it cannot supply Evidence, resolve ambiguity, establish a
project Work Decision, or authorize a write.

A learned promote/defer boundary may be reconsidered only after it has exact
class-specific outcome labels, calibrated risk/coverage measurements, a false-
write ceiling, and the same immutable input/output receipts as a deterministic
policy. Until then, the model side of the boundary remains recommendation and
Abstention only.

## First new automatic class: attributable party-level Commitment with unknown scope

The first extension to Event Admission records a real party-level Commitment
without asking a person to choose **Commitment Scope not yet known** when that is
already the only honest scope result.

The policy may admit this class only when all of these predicates hold:

1. the current event Candidate belongs to the declared Active Run;
2. every Candidate citation is mechanically verified against registered Evidence;
3. the Candidate proposes one Commitment, not a Committed Date Change or closure;
4. exact Evidence supports one nonempty description and one timing at its stated precision;
5. the stated External Party resolves through one exact registered name or alias and is not the project side;
6. the affected External Party is the same resolved party;
7. no previous timing is present;
8. no conflict reference is present; a present but missing, ambiguous, or mismatched reference remains human scope work; and
9. every ordinary statement validator and current-state fingerprint check passes under the project lock.

The result is one attributable External Party Commitment with
`Commitment Scope not yet known`. It creates no Dependency projection and no
Coordination Plan. The accepted party-level Commitment then appears as a Work
Item only for its actual residue, such as unknown scope, missing Internal Owner,
or missing Next Action.

The policy receipt binds the Candidate, project, source Document, Active Run,
Evidence quote and page, resolved External Party, timing wording and precision,
policy version and digest, eligibility fingerprint, created Commitment Lineage,
statement event, unknown-scope decision, and Candidate disposition. Repeating the
same versioned input returns the existing result or a zero-new-outcome run; it
cannot duplicate the Commitment.

The reason vocabulary must distinguish at least:

- citation or Evidence failure;
- event type outside this class;
- missing, fuzzy, ambiguous, or project-side stated party;
- affected-party disagreement;
- missing or invalid timing or precision;
- previous timing present;
- conflict reference present;
- stale Active Run, Candidate, Evidence, party, or project state; and
- statement validator or write-integrity failure.

Every failed predicate produces an Admission Abstention and leaves the Candidate
pending. A model may add suspicion and demote an otherwise eligible Candidate,
but it cannot make an ineligible Candidate pass.

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
rubber-stamping, and contradicts existing mechanical Admission and Carry-Forward
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

Event Admission is amended to support the exact unknown-scope Commitment class
above. ADR-0041 still forbids a model or software operator from impersonating a
human, but it does not prevent an explicitly authorized deterministic policy from
writing under its own system identity. ADR-0037, ADR-0039, and ADR-0040 continue
to reserve Ready, residual statement Adjudication and Work Decisions, and Report
release to their named human authorities.

The success measure is not maximum automation. It is minimum human residue at a
declared false-write ceiling, with every automatic outcome reproducible and every
Abstention specific enough to become one bounded Work Item.
