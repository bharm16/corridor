# A model may brief on the record, never be it

The exception engine is deterministic by construction and blind by measurement.

Deterministic is the moat: this ledger's output is ammunition — utility-relocation delays end in claims, and the readiness report is the artifact a program manager puts in front of an auditor or opposing counsel. Its standing rests on three properties the rules provide for free. Asked twice, it answers the same. Asked *why*, it answers with a stored fact. And because it recomputes identically, "what changed since last week" is computable — the diff layer diffs the record, not a mood.

Blind is the cost, and the blindness is measurable in the stored data. WSDOT 9424's matrix prints, under `Relocation Schedule Status`, the sentence `NTP July or August 2019. Work will be coordinated with ST contractor.` — a schedule coupling to a second agency's contractor. No rule will ever fire on it, and the extraction pipeline holds it at three removes from anything a rule reads: the column is deliberately unmapped (the vocabulary declined it in writing), so the sentence survives only inside the citation's quote string, on a Candidate still pending review. The minutes corpus holds 1,629 pending events of the same character — commitments hedged, permits waited on, mobilization promised only after another party acts. A reader who saw all of it would worry about things the exception list cannot name.

A model can read all of it. The question this ADR settles is what the model's reading is *allowed to be*.

## Decision

A model may produce the **Briefing**: a narrative reading of one Dependency or one project — the record, its Exceptions, and its evidence trail, rendered as the paragraph a program manager would want a sharp deputy to write. For a relocation record it might read: *this is the one to watch — the relocation is committed for September, the owner has not appeared in a document in six weeks, and the July minutes say its contractor is waiting on a city franchise permit.* Every clause of which would carry a citation.

Four constraints, each inherited from a boundary this system already enforces:

1. **Every sentence carries a citation** — to an Evidence page, a named Assertion, or a named computed fact (an Exception, a Derivation). The report already holds this bar for cells (*no cell is bare*, ADR-0003); prose gets no exemption for being prose. A sentence that cannot cite is a sentence the Briefing may not contain.

2. **The Briefing is a view, never the record.** It is regenerable, labelled as model-drafted, and stamped with the prompt version and model that produced it (the provenance every Candidate already carries, v0-build-spec §7) **plus the evaluation time and ruleset version of the Exceptions it cites** — because Exceptions are computed at read time and stored nowhere, a citation to one is only re-checkable against the moment and ruleset that computed it. That is ADR-0003's own discipline for Derivations, applied to prose. Deleting every Briefing loses no fact. It is never stored as record, never diffed as record, never cited as record.

3. **What the model surfaces becomes record only through the front door.** If the Briefing notices what looks like a Commitment, a Committed Date Change, or a new risk, that observation enters as a **Candidate** with citations and waits for **Adjudication** — the only path into the Ledger, exactly as the glossary defines those terms and v0-build-spec §8 mandates for every extractor. The model proposes; a human accepts; nothing model-written touches the Ledger directly. The Briefing is an extractor whose input happens to be the whole record instead of one page.

4. **The floor is not negotiable.** Every fired Exception appears in or under the Briefing; the model may explain one, contextualize one, or argue one is less alarming than it looks — it may never omit or bury one. The deterministic list is the floor the narrative stands on, and the model does not get to talk the floor out of the report.

   **Project-scope amendment (#127, 2026-08-19).** A project Briefing floors
   the rule buckets from the Evaluation's shared facet view, each with its
   count and exact individual Exception refs underneath. Citing the bucket
   therefore covers every instance in it; an individual instance may still be
   cited when its detail matters. A one-Dependency Briefing keeps the
   instance-level floor. This changes the unit of coverage, not the rule:
   every fired Exception remains in or under the Briefing.

## Considered options

**Let the model replace the rule engine** — hand it the evidence, let it decide what is concerning. Rejected on all three moat properties at once: the same record on two days would brief two ways, which turns the week-over-week diff into noise; "why is this flagged" would answer with plausible prose instead of a checkable fact; and the standing of the output in a dispute would rest on a system that cannot show its work. The objection is not that models are careless — it is that this corpus has *measured* the failure class: the validation gate (#68) caught transcribed values that were not on the page at self-reported confidences of 0.98 and 0.99, which is why ADR-0006 lets code do everything a lookup can do and confines the model to what code cannot. This ADR draws the identical line one layer up: arithmetic and set membership are lookups over the record — code's; reading prose across 1,629 events and saying what it amounts to is not — the model's.

**Rules only** — extend the engine until it catches everything. Rejected as unfalsifiable in the wrong direction: a new rule can be written for any prose pattern *after* someone notices it, which means the rules encode last year's surprises. The Briefing exists to surface this year's.

**A stochastic score** — the model rates concern 1–10 from the evidence. Rejected as ADR-0010's weights with worse provenance: an unsourced constant at least sits still; an unsourced sample moves between runs.

## Consequences

**The Briefing is a component with a prompt version**, and bumping it means what it means everywhere else: briefings from different versions are different readings and are not pooled or compared (v0-build-spec §7's rule for every extractor).

**Its quality question is open, and the open part is named honestly.** What is checkable mechanically is presence and existence: every sentence cites, each cited page or Assertion exists, each quoted string appears where claimed, each cited Exception matches the stamped evaluation — the same class of check `verify.py` already performs, which answers *"does the quote appear on the page"* and deliberately not *"is the claim true."* Whether a sentence's claim actually **follows from** its citation is not mechanical — constraint 4 even licenses sentences that argue beyond their citations — and judging that is the reviewer's work, exactly as it is for any Candidate. There is no gold set for narratives, and this ADR does not pretend a metric for insight.

**The rules lose a burden.** They no longer carry the expectation of noticing everything — ADR-0010 already made them honest facts; this ADR makes them the floor of something that reads. The pressure to grow ever more rules for ever more prose patterns is redirected to the layer built for prose.

**Cost is bounded by regenerability.** A Briefing is drafted on demand or per report run, over one project's record — the same order of spend as a report, not a corpus re-extraction.

## Glossary

**Briefing** enters the glossary: a model-drafted narrative view of the record — cited sentence by sentence, floored by the Exceptions, stamped with its prompt version, model, evaluation time and ruleset version, never itself the record.
