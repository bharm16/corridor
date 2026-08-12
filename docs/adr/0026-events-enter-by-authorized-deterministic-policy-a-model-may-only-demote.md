# Events enter by deterministic policy, and a model may only demote

> **Amended by ADR-0029 and ADR-0036.** The accountable-human authorization in
> this decision is superseded: the policy runs as a pipeline stage without a
> sign-off. Its single-Dependency, affected-party-equals-speaker, and exact-date
> inputs are also superseded. Current statement admission preserves the stated
> party, source precision, and explicit one, selected, all-active, or unknown scope;
> a later attributable timing is a Committed Date Change. Deterministic replayable
> checks, receipts, the actor-masquerade boundary, and demote-only model participation
> remain in force.

The date half of the product needs External Party statements on the Ledger —
Commitments, Committed Date Changes, and closure statements extracted from meeting
minutes — and they arrive in volume: SH 99 Grand Parkway alone holds 1,629 event
Candidates, 267 of them dated commitment/change/closure Candidates referencing a
known conflict. The City rehearsal's own record says the way back is never humans
clearing thousands of Candidates; the operator refused per-row clicking of
near-certain records outright. The retained decision is that an event Candidate
enters only through a **named, versioned, deterministic event-admission policy** or
through Adjudication, and every policy check remains a replayable computation. Under
the current contract, that policy verifies Evidence without inventing speaker,
precision, or scope; an event it cannot prove eligible remains pending for guided
Adjudication. A rerun of the same policy version over the same inputs reaches the
same outcomes or refuses.

A model may participate on one side only. A model verdict may **demote** — flag an event out of the mechanical path into the human pile, order the queue, annotate suspicion — and may never **promote**: no event enters the Ledger because a model judged it correct. This is the same line ADR-0025 drew for coordination state (suggest, never decide) applied to admission, and it is what keeps the provenance story whole: every event on the Ledger traces to a human Adjudication or to a named deterministic rule under Corridor-managed engineering and release controls that anyone can re-run. "A model checked the model" is the story every competitor already tells.

One boundary rides with the machinery because the SH 99 data forced it: the extractor's "commitment" type conflates an External Party promising delivery with the project's own engineer taking an action item — most SH 99 commitments are LJA's. An event whose actor is the project side can never set a Committed Date; the masquerade rule the coordination lanes already enforce for dates extends to events' actors.

## Considered options

**Human Adjudication of every event, City-lane style.** Rejected: the bounded City cohort was 17 records; the event stream is an order of magnitude larger per party and recurs with every meeting cycle forever. The measured 140 decisions/hour makes a single party's backlog tractable once, not a workflow.

**A model as verifier that admits.** A second model pass reading each event against its source page and approving what checks out would clear more per pass, including the semantic cases. Rejected: a model verdict is not replayable — model versions drift, and the receipt could not promise that re-running it reproduces the outcome — and it moves the one claim that differentiates the record. Revisitable with eyes open if the human residue measures larger than the mechanical yield.

## Consequences

New machinery mirrors the carry-forward family: a policy document with a version,
per-event outcome receipts under one policy digest, and Abstention — an event the
policy cannot prove eligible is left pending, never forced. Corridor engineering
and release controls govern a changed policy version; neither a customer nor a
project coordinator authorizes its execution. The Adjudication queue gains the
residue, and any model assist writes ordering hints, never admission reasons. The
SH 99 date exercise is the first consumer; its glossary entries (the policy's name
among them) land with the build.
