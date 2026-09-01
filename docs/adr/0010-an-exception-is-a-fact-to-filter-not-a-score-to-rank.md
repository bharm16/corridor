---
status: accepted
domain: project-record
scope: current product
amends:
  - ADR-0009
amended_by:
  - ADR-0076
---

# A Constraint Alert is a fact to filter, not a score to rank

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Amends [ADR-0009](0009-the-document-records-a-resolution-strategy-not-a-criticality.md): its consequence *"`severity = rule severity × criticality` needs restating"* was answered by restating the multiplier as ×3. This ADR abolishes the multiplication instead. Implementation is #112.

The Constraint Alert engine computes eight rules, and every rule is a fact: `OVERDUE` means the date promised by an External Organization passed without a report of completion, `MISSING_EVIDENCE` means not one verified source backs the record. Each is computed at read time, citable, and either true or false. That half of the design survives this ADR untouched.

The historical engine then multiplied. Every rule carried a weight — `MISSING_EVIDENCE` and `CONTRADICTION` 5.0, `OVERDUE` 4.0, `DUE_SOON` and `MISSING_DATE` 3.0, `STALE` and `MISSING_OWNER` 2.0, `ORPHAN` 1.0 — and the product of weight × criticality (×3 for the selected work-type subset) was published as `severity`, which sorted every Constraint Alert list. **Those constants have no source.** No document asserts that missing Supporting Documentation is 1.25× as bad as an overdue Commitment; no published method defines the scale; nobody can defend 5.0 against 4.5. They are ADR-0009's finding, reincarnated one layer up: that ADR deleted `critical | high | normal` from the Constraint because *a scale nothing can assert is a reviewer's opinion wearing a document's clothes* — and then the same opinion survived on the Constraint Alert, wearing arithmetic's.

The glossary licensed it, in as many words: *"severity is a property of an Exception, not of a Dependency."* This ADR ends the property, not just the placement.

## What the scalar actually produces

The live corpus, through the current engine:

```
NHHIP Segment 3C-2 — ruleset v0.1: 564 exceptions across 141 dependencies
   141  MISSING_DATE
   141  MISSING_OWNER
   141  STALE
   141  ORPHAN

worst 10:
  sev 3.0  DEP-00001  MISSING_DATE  no committed date from the external party
  sev 3.0  DEP-00002  MISSING_DATE  no committed date from the external party
  sev 3.0  DEP-00003  MISSING_DATE  no committed date from the external party
  ... (ten identical rows)
```

The four-line count table answers the reader's question — *what is wrong, and how much of it* — and the ranked list beneath it answers nothing: ten indistinguishable rows whose "worst"ness is an artifact of ref-code ordering among invented equals. The summary is already a facet view. The scalar adds a number to it that no one can act on and no one could audit.

## Decision

A Constraint Alert is presented by its **facts**, and only its facts:

- **Group by rule** — the category *is* the finding: missing the External Organization's promised timing or missing an assigned project person. What is wrong, counted.
- **Within a rule, sort by its own quantity** — days overdue, days until Required By, days since a document last spoke. `OVERDUE 40 days` before `OVERDUE 3 days` is an ordering a reader can check against the record.
- **Filter by the defined Utility Conflict Resolution Method subset** — ADR-0009's reading (does the strategy commit the External Organization to relocation, removal, or abandonment) becomes a dimension a reader slices on. Asking for overdue records in that subset is a query; ×3 was an editorial. The work-type subset never multiplies and never orders, and the view states the actual work it includes.

The numeric weights, `CRITICAL_WEIGHT`, and the `severity` field are removed.

**No total order across categories is sanctioned, including a lexicographic one.** An earlier draft of this ADR allowed a flat "top items" list composed lexicographically from facts, and the idea does not survive its own test: a lexicographic key priority is a weighted sum whose exchange rate went to infinity — putting overdue-days first asserts that one day overdue outranks a relocation required in two days, by any margin, on exactly as much published authority as 5.0-over-4.0. "40 days overdue" and "two sources disagree about the owner" do not share a unit, and any device that forces them into one sequence — multiplication, key order, anything — is the same undefended exchange rate in a different costume.

Where a surface can show only one flat list, that list is **presentation, not measurement**: it declares the single quantity it is ordered by, orders by nothing else, and where that quantity is absent it says so rather than faking an order. The corpus at the time of this decision made this concrete — all 141 Constraints lacked Promised For, Required By, and key date values, so *any* ordering rule over them degenerated to ref-code order. The honest rendering of that state is "141 rows, no dates known," not an alphabetical list wearing a ranking's clothes — which is precisely the pathology the output block above exhibits.

Thresholds survive; weights do not. The distinction: `STALE_DAYS = 14` *defines a predicate* — "no document in 14 days" is a checkable fact with a unit once the threshold fires, and the threshold is published per-project configuration (v0-build-spec §9). A weight defines nothing; it only moves a row past another row for no statable reason.

## Considered options

**Keep the weights and tune them.** Rejected because there is nothing to tune against. A ranking model earns its constants from outcomes — which flagged records actually slipped — and no such outcome data exists or is close to existing. Until it does, tuned constants are the same opinion with more decimal places.

**Derive one scalar from the facts.** Rejected: the facts are incommensurable, and every exchange rate between them is the weight problem again — see the lexicographic paragraph above, which is this option's limiting case.

**Score with a model.** Considered and deliberately split off: what a model can add over the record is narrative, not arithmetic, and it must not be this list's floor. That boundary is [ADR-0011](0011-a-model-may-brief-on-the-record-never-be-it.md)'s subject.

## Consequences

**`exceptions.py` loses `severity` and `CRITICAL_WEIGHT`; `RULESET_VERSION` bumps** (#112). The version exists precisely because published orderings changed meaning; every report run records which ruleset produced it.

**The four historical severity readers were restated in #112.** `ledger.py`'s `worst_severity` property; the Coordination Report's `_critical_items` ranking term and its per-rule "worst offender" pick; and the web Ledger and Constraint views, which sorted and coloured by `e.severity`. `changes.py` was *not* among them — it compared rule names and transitions in work-type subset membership and never read a severity, so the week-over-week diff survived this ADR untouched.

**The Coordination Report's double-weighting question, deferred in writing, is answered here.** `report._critical_items` documented that criticality was applied twice — once as `base`, once inside `worst_severity` — and deferred the correction: *"changing how the weekly report ranks is a decision about the report and not about this schema change, and #96 asked only that the ranking still work."* The section keeps its declared quantity — proximity to Required By — as labelled presentation, the work-type subset scopes the section, and the severity term disappears with the scalar.

**v0-build-spec §9's severity sentence, §10's Critical-items ranking, and §10's "worst offenders" are superseded**, and the spec is edited in place with pointers in this change — §10 item 2's `(need-date proximity × criticality)` and item 3's severity-defined "worst" would otherwise keep describing the abolished arithmetic. The rule table in §9 stands unchanged.

**The Constraint Alert definition does not include severity.** The former Criticality label also grants no exception to that rule; ADR-0047 removes it as customer language. Severity is not a property of anything in this system.

**What this does not change:** the rules, their predicates, their thresholds, the fact that everything is computed at read time and stored nowhere (ADR-0002's discipline), and `MISSING_EVIDENCE`'s relationship to whether the documentation requirement is met. This ADR removes the unsupported ranking; it does not change the authority of the underlying facts.
