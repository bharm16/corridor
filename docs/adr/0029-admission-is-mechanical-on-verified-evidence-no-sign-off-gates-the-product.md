---
status: accepted
---

# Record Inclusion is mechanical on verified Supporting Documentation, and no sign-off gates the product

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> **Amended by ADR-0034.** The Automatic Support Update authorization carve-out below no
> longer applies. Automatic Support Update now runs as normal fail-closed processing
> under Corridor-managed versioned rules; ADR-0022's proof and receipt boundaries
> survive without a project authorization gate. The body below records the earlier
> scope boundary; its statement that Automatic Support Update still requires authorization is
> not current.

The SH 99 load screen made a drift visible: two sign-off checklists stood between an opened project and the first useful pixel, and the operator's verdict was that the gate defeated the automation it guarded. The product is not the record; the record is plumbing. What Corridor sells is the surfaced conflict — the pipe in the way of the road, with the quote on the page behind it — so the project team can chase it to resolution. Decision: **a row enters the Ledger mechanically, from extraction, when the machine can anchor it; nothing the user must sign, authorize, or unlock stands in front of the list.** One matrix suffices: its conflicts surface day one. A second revision is a signal, not a gate — byte-identical rows are confirmed, differing fields become a Source Discrepancy carried on the row with both Assertions and both pages, and resolving a Source Discrepancy is Human Record Decision a reviewer takes up when it matters. Rows the machine misread — citation unverified, identifier missing — enter the same list flagged and sorted last, never a side queue. A dated statement from the minutes attaches itself when it names exactly one row; the unclear remainder sits in one visible pile for a human to attach or toss. Junk is dismissed with a reason and kept in history, never hard-deleted.

This supersedes the accountable-human authorization at the center of ADR-0026 and ADR-0027 and the human-only Record Inclusion of ADR-0020. What those decisions built survives without the gate: record inclusion remains a named, versioned, deterministic, replayable policy; every mechanical act writes an immutable receipt; a model may still flag and order and may never admit; extractors still produce only Extracted Proposals, and adjudicate remains the only writer in code — now invoked by the pipeline when documents land rather than behind a signature. ADR-0003's bar for publication is untouched: what a Coordination Report prints still carries provenance. The Automatic Support Update family (ADR-0022) is out of scope here and keeps its authorization until its own decision.

## Considered options

**Keep the human authorization and improve its ergonomics** — preview first, one click, one signature covering both policies. Rejected: the signature bought a provenance claim ("a human authorized the rules") that the operator judges nobody is buying. The verified citation does the trust work; the ceremony gated the product.

**Surface unproven rows as Extracted Proposals dressed to look like record rows.** Rejected: every downstream reader — Constraint Alerts, Coordination Reports, Coordination Decisions, Coordination Summaries — would pay a two-worlds tax forever. The identity enters; the uncertainty rides on the row.

**Corroboration as a gate** — two agreeing revisions required before a row enters, the shipped ADR-0027 shape. Rejected: a one-matrix project, which is every project on day one, would show nothing actionable.

## Consequences

The load screen is deleted; record inclusion policies run as pipeline stages under a system actor when documents land. The PolicyApproval requirement drops; PolicyRun receipts and outcome tables stay. CONTEXT.md is rewritten alongside this decision: the product line, Ledger, Record Inclusion, Human Record Decision, and a new Source Discrepancy entry. The queue becomes a working list whose row actions are settle, verify, attach, and dismiss; the follow-up screens — assignment, Next Action, and Promised For tracking — are the next build.
