---
status: accepted
domain: supporting-documentation
scope: current product
amends:
  - ADR-0003
---

# A calculated result names what it covers, and a row with nothing to measure is not published

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Amends the enforcement of [ADR-0003](0003-every-published-number-carries-provenance.md). Its rule stands unchanged; what changes is that the verifier now checks it.

ADR-0003 originally defined two provenance classes, later extended by ADR-0025 and ADR-0033. It said that a Derivation "drills through to those records' Evidence" — now called Supporting Documentation — so a percentage can lead a reader to its sources. `assert_no_bare_cells` tested that `provenance` was not `None`. A `Derivation(RULESET_VERSION, ())` passed, rendered as `[v0.2 over 0 records]`, and drilled through to nothing. The renderer carried an `or "computed"` fallback that existed only to paper over the empty case. The whole "Changes since last report" section was built that way.

The check now refuses an unresolvable Derivation. The customer explanation is **How this was calculated**; `Derivation` remains the internal identity for results of stated rules, including nonnumeric results. Three consequences worth stating, because each is a place where the obvious fix is the wrong one:

- **A Change cites the Constraint it describes**, and the snapshot records the id beside the ref_code so it can. Only a record that left the Ledger before ids were recorded — four such runs were stored when this decision was made — falls back to naming the run it was compared against, via `Derivation.scope`.
- **A count of none names what it counted**: the matching subset where there is one, the population where there is not. The historical label "Ready 0 of 12" derives from the twelve it examined. The same rule applies to a count of Constraints whose stated documentation requirements are met.
- **A key date with no Constraints linked is named in the section note, not published as a row.** This is the visible behaviour change. The historical output `Ready 0 · At risk 0 · Blocked 0` reads as a measurement of that key date's records, and there are none; the zeroes are the absence of a population, not a finding about one. Citing the key date's own id instead was considered and rejected: `record_ids` holds Constraint ids everywhere else, and one row where it means something different is worse than an omission the note states.

`Derivation.scope` covers the case where no record can answer at all — an empty Ledger has nothing to drill to, and saying so is provenance where an empty tuple was not.

## Consequences

A report that rendered before may now raise `BareCell`. That is the point: it was rendering cells whose markers pointed at nothing. Any new `_derived(..., ())` call site is a compile-time-adjacent error rather than a silently bare cell.
