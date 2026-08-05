# A Derivation names what it covers, and a row with nothing to measure is not published

Amends the enforcement of [ADR-0003](0003-every-published-number-carries-provenance.md). Its rule stands unchanged; what changes is that the verifier now checks it.

ADR-0003 says every published cell is an Assertion or a Derivation, and that a Derivation "drills through to those records' Evidence" — the argument being that making a percentage clickable down to the evidence beneath it is the strongest moment in the report. `assert_no_bare_cells` tested that `provenance` was not `None`. A `Derivation(RULESET_VERSION, ())` passed, rendered as `[v0.2 over 0 records]`, and drilled through to nothing. The renderer carried an `or "computed"` fallback that existed only to paper over the empty case. The whole "Changes since last report" section was built that way.

The check now refuses an unresolvable Derivation. Three consequences worth stating, because each is a place where the obvious fix is the wrong one:

- **A Change cites the Dependency it describes**, and the snapshot records the id beside the ref_code so it can. Only a record that left the Ledger before ids were recorded — four such runs are stored — falls back to naming the run it was compared against, via `Derivation.scope`.
- **A count of none names what it counted**: the matching subset where there is one, the population where there is not. "Ready 0 of 12" derives from the twelve it examined.
- **A Milestone with no Dependencies linked is named in the section note, not published as a row.** This is the visible behaviour change. Publishing `Ready 0 · At risk 0 · Blocked 0` for it reads as a measurement of that milestone's records, and there are none; the four zeroes are the absence of a population, not a finding about one. Citing the Milestone's own id instead was considered and rejected: `record_ids` holds Dependency ids everywhere else, and one row where it means something different is worse than an omission the note states.

`Derivation.scope` covers the case where no record can answer at all — an empty Ledger has nothing to drill to, and saying so is provenance where an empty tuple was not.

## Consequences

A report that rendered before may now raise `BareCell`. That is the point: it was rendering cells whose markers pointed at nothing. Any new `_derived(..., ())` call site is a compile-time-adjacent error rather than a silently bare cell.
