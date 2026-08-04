# An Exception is a fact to filter, not a score to rank

Amends [ADR-0009](0009-the-document-records-a-resolution-strategy-not-a-criticality.md): its consequence *"`severity = rule severity × criticality` needs restating"* was answered by restating the multiplier as ×3. This ADR abolishes the multiplication instead. Implementation is #112.

The exception engine computes eight rules, and every rule is a fact: `OVERDUE` means a committed date passed with no closure event, `MISSING_EVIDENCE` means not one verified document backs the record. Each is computed at read time, citable, and either true or false. That half of the design survives this ADR untouched.

Then the engine multiplies. Every rule carries a weight — `MISSING_EVIDENCE` and `CONTRADICTION` 5.0, `OVERDUE` 4.0, `DUE_SOON` and `MISSING_DATE` 3.0, `STALE` and `MISSING_OWNER` 2.0, `ORPHAN` 1.0 — and the product of weight × criticality (×3 when the strategy is critical) is published as `severity`, which sorts every exception list. **Those constants have no source.** No document asserts that missing evidence is 1.25× as bad as an overdue commitment; no published method defines the scale; nobody can defend 5.0 against 4.5. They are ADR-0009's finding, reincarnated one layer up: that ADR deleted `critical | high | normal` from the Dependency because *a scale nothing can assert is a reviewer's opinion wearing a document's clothes* — and then the same opinion survived on the Exception, wearing arithmetic's.

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

An Exception is presented by its **facts**, and only its facts:

- **Group by rule** — the category *is* the finding: "141 missing a date, 141 missing an owner." What is wrong, counted.
- **Within a rule, sort by its own quantity** — days overdue, days until the need date, days since a document last spoke. `OVERDUE 40 days` before `OVERDUE 3 days` is an ordering a reader can check against the record.
- **Filter by Criticality** — ADR-0009's reading (does the strategy commit the External Party to substantial work — relocation, removal, or abandonment) becomes a dimension a reader slices on: "show me the overdue criticals" is a query; ×3 was an editorial. Criticality never multiplies and never orders.

The numeric weights, `CRITICAL_WEIGHT`, and the `severity` field are removed.

**No total order across categories is sanctioned, including a lexicographic one.** An earlier draft of this ADR allowed a flat "top items" list composed lexicographically from facts, and the idea does not survive its own test: a lexicographic key priority is a weighted sum whose exchange rate went to infinity — putting overdue-days first asserts that one day overdue outranks a critical relocation needed in two, by any margin, on exactly as much published authority as 5.0-over-4.0. "40 days overdue" and "two sources disagree about the owner" do not share a unit, and any device that forces them into one sequence — multiplication, key order, anything — is the same undefended exchange rate in a different costume.

Where a surface can show only one flat list, that list is **presentation, not measurement**: it declares the single quantity it is ordered by, orders by nothing else, and where that quantity is absent it says so rather than faking an order. The live corpus makes this concrete — today all 141 Dependencies have no committed date, no need date, and no milestone, so *any* ordering rule over them degenerates to ref-code order. The honest rendering of that state is "141 rows, no dates known," not an alphabetical list wearing a ranking's clothes — which is precisely the pathology the output block above exhibits.

Thresholds survive; weights do not. The distinction: `STALE_DAYS = 14` *defines a predicate* — "no document in 14 days" is a checkable fact with a unit once the threshold fires, and the threshold is published per-project configuration (v0-build-spec §9). A weight defines nothing; it only moves a row past another row for no statable reason.

## Considered options

**Keep the weights and tune them.** Rejected because there is nothing to tune against. A ranking model earns its constants from outcomes — which flagged records actually slipped — and no such outcome data exists or is close to existing. Until it does, tuned constants are the same opinion with more decimal places.

**Derive one scalar from the facts.** Rejected: the facts are incommensurable, and every exchange rate between them is the weight problem again — see the lexicographic paragraph above, which is this option's limiting case.

**Score with a model.** Considered and deliberately split off: what a model can add over the record is narrative, not arithmetic, and it must not be this list's floor. That boundary is [ADR-0011](0011-a-model-may-brief-on-the-record-never-be-it.md)'s subject.

## Consequences

**`exceptions.py` loses `severity` and `CRITICAL_WEIGHT`; `RULESET_VERSION` bumps** (#112). The version exists precisely because published orderings changed meaning; every report run records which ruleset produced it.

**The severity readers are exactly four, and each is restated in #112.** `ledger.py`'s `worst_severity` property; the report's `_critical_items` ranking term and its per-rule "worst offender" pick; and the web ledger and dependency views, which sort and colour by `e.severity`. `changes.py` is *not* among them — verified against the code, it diffs rule names and criticality transitions and never reads a severity, so the week-over-week diff survives this ADR untouched.

**The report's double-weighting question, deferred in writing, is now answered.** `report._critical_items` documents that criticality is applied twice — once as `base`, once inside `worst_severity` — and defers the correction: *"changing how the weekly report ranks is a decision about the report and not about this schema change, and #96 asked only that the ranking still work."* This is that decision arriving: the section keeps its declared quantity — need-date proximity, which is its own stated purpose — as labelled presentation, criticality becomes the filter that scopes the section, and the severity term disappears with the scalar.

**v0-build-spec §9's severity sentence, §10's Critical-items ranking, and §10's "worst offenders" are superseded**, and the spec is edited in place with pointers in this change — §10 item 2's `(need-date proximity × criticality)` and item 3's severity-defined "worst" would otherwise keep describing the abolished arithmetic. The rule table in §9 stands unchanged.

**The glossary's Exception entry drops severity; Criticality's avoid-list no longer carves out an exception for it.** Severity is no longer a property of anything in this system.

**What this does not change:** the rules, their predicates, their thresholds, the fact that everything is computed at read time and stored nowhere (ADR-0002's discipline), and `MISSING_EVIDENCE`'s by-construction relationship to readiness. The engine's facts were always right. This ADR deletes the opinions that were standing on them.
