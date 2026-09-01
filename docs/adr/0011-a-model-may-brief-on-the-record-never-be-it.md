---
status: accepted
domain: extraction
scope: current product
---

# A model may brief on the record, never be it

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

The constraint check engine is deterministic by construction and blind by measurement.

Deterministic is the moat: this Ledger's output is ammunition — utility-relocation delays end in claims, and the Coordination Report is the artifact a program manager puts in front of an auditor or opposing counsel. Its standing rests on three properties the rules provide for free. Asked twice, it answers the same. Asked *why*, it answers with a stored fact. And because it recomputes identically, "what changed since last week" is computable — the diff layer diffs the record, not a mood.

Blind is the cost, and the blindness is measurable in the stored data. WSDOT 9424's matrix prints, under `Relocation Schedule Status`, the sentence `NTP July or August 2019. Work will be coordinated with ST contractor.` — a schedule coupling to a second agency's contractor. No rule will ever fire on it, and the extraction pipeline holds it at three removes from anything a rule reads: the column is deliberately unmapped (the vocabulary declined it in writing), so the sentence survives only inside the citation's quote string, on an Extracted Proposal still pending review. The minutes corpus holds 1,629 pending events of the same character — commitments hedged, permits waited on, mobilization promised only after another party acts. A reader who saw all of it would worry about things the constraint alert list cannot name.

A model can read all of it. The question this ADR settles is what the model's reading is *allowed to be*.

## Decision

A model may produce the **Coordination Summary**: a narrative reading of one Constraint or one project — the record, its Constraint Alerts, and its Supporting Documentation, rendered as the paragraph a program manager would want a sharp deputy to write. For a relocation record it might read: *this is the one to watch — the relocation is promised for September, the Utility Owner has not appeared in a document in six weeks, and the July minutes say its contractor is waiting on a city franchise permit.* Every clause of which would carry a citation.

Four constraints, each inherited from a boundary this system already enforces:

1. **Every sentence carries a citation** — to a page of Supporting Documentation, a named Assertion, or a named computed fact (a Constraint Alert, a Derivation). The Coordination Report already holds this bar for cells (*no cell is bare*, ADR-0003); prose gets no exemption for being prose. A sentence that cannot cite is a sentence the Coordination Summary may not contain.

2. **The Coordination Summary is a view, never the record.** It is regenerable, labelled as model-drafted, and stamped with the prompt version and model that produced it (the provenance every Extracted Proposal already carries, v0-build-spec §7) **plus the evaluation time and ruleset version of the Constraint Alerts it cites** — because Constraint Alerts are computed at read time and stored nowhere, a citation to one is only re-checkable against the moment and ruleset that computed it. That is ADR-0003's own discipline for Derivations, applied to prose. Deleting every Coordination Summary loses no fact. It is never stored as record, never diffed as record, never cited as record.

3. **What the model surfaces becomes record only through the front door.** If the Coordination Summary notices what looks like a Commitment, a Change to Promised Timing, or a new risk, that observation enters as an **Extracted Proposal** with citations. It reaches the Project Record only through Human Record Decision or an exact deterministic Record Inclusion class under ADR-0042; the model verdict itself never admits.

4. **The floor is not negotiable.** Every fired Constraint Alert appears in or under the Coordination Summary; the model may explain one, contextualize one, or argue one is less alarming than it looks — it may never omit or bury one. The deterministic list is the floor the narrative stands on, and the model does not get to talk the floor out of the report.

   **Project-scope amendment (#127, 2026-08-19).** A project Coordination Summary floors
   the rule buckets from the Evaluation's shared facet view, each with its
   count and exact individual Constraint Alert refs underneath, so citing a bucket
   covers every instance while individual instances remain citable when their
   detail matters and a single-Constraint Coordination Summary keeps its instance-level
   floor — the unit of coverage changes, but every fired Constraint Alert remains in
   or under the Coordination Summary.

## Considered options

**Let the model replace the rule engine** — hand it the Supporting Documentation, let it decide what is concerning. Rejected on all three moat properties at once: the same record on two days would brief two ways, which turns the week-over-week diff into noise; "why is this flagged" would answer with plausible prose instead of a checkable fact; and the standing of the output in a dispute would rest on a system that cannot show its work. The objection is not that models are careless — it is that this corpus has *measured* the failure class: the validation gate (#68) caught transcribed values that were not on the page at self-reported confidences of 0.98 and 0.99, which is why ADR-0006 lets code do everything a lookup can do and confines the model to what code cannot. This ADR draws the identical line one layer up: arithmetic and set membership are lookups over the record — code's; reading prose across 1,629 events and saying what it amounts to is not — the model's.

**Rules only** — extend the engine until it catches everything. Rejected as unfalsifiable in the wrong direction: a new rule can be written for any prose pattern *after* someone notices it, which means the rules encode last year's surprises. The Coordination Summary exists to surface this year's.

**A stochastic score** — the model rates concern 1–10 from the Supporting Documentation. Rejected as ADR-0010's weights with worse provenance: an unsourced constant at least sits still; an unsourced sample moves between runs.

## Consequences

**The Coordination Summary is a component with a prompt version**, and bumping it means what it means everywhere else: summaries from different versions are different readings and are not pooled or compared (v0-build-spec §7's rule for every extractor).

**Its quality question is open, and the open part is named honestly.** What is checkable mechanically is presence and existence: every sentence cites, each cited page or Assertion exists, each quoted string appears where claimed, each cited Constraint Alert matches the stamped evaluation — the same class of check `verify.py` already performs, which answers *"does the quote appear on the page"* and deliberately not *"is the claim true."* Whether a sentence's claim actually **follows from** its citation is not mechanical — constraint 4 even licenses sentences that argue beyond their citations — and judging that is the reviewer's work, exactly as it is for any Extracted Proposal. There is no gold set for narratives, and this ADR does not pretend a metric for insight.

**The rules lose a burden.** They no longer carry the expectation of noticing everything — ADR-0010 already made them honest facts; this ADR makes them the floor of something that reads. The pressure to grow ever more rules for ever more prose patterns is redirected to the layer built for prose.

**Cost is bounded by regenerability.** A Coordination Summary is drafted on demand or per report run, over one project's record — the same order of spend as a report, not a corpus re-extraction.

## Glossary

**Coordination Summary** enters the glossary: a model-drafted narrative view of the record — cited sentence by sentence, floored by the Constraint Alerts, stamped with its prompt version, model, evaluation time and ruleset version, never itself the record.
