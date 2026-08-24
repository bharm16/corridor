---
status: accepted
---

# A verbal is an appended event, never a missing citation

> **Partially superseded by ADR-0036.** A later attributable timing is a
> Committed Date Change, not a `Slip`; timing keeps its source precision, and a
> Verbal may carry party-level or multi-Dependency scope. Append-only provenance,
> named recorder, conversation date, stated-party attribution, corrections, and
> honest report rendering remain in force. The former single-Dependency,
> exact-date shape is not current.

The freshest commitment often arrives by phone. Keeping it outside Corridor
leaves the ledger stale and moves the coordinator's real work back to a
spreadsheet; treating the call as a document would make a report more
trustworthy-looking precisely when it is less trustworthy. Decision: a
**Verbal** is an explicit append-only statement event recorded by a human with the
stated party, what was said, conversation date, timing as stated, and explicit scope
state. A Verbal may begin party-level or apply to one or several Dependencies; scope
does not establish who spoke. The current Committed Date remains the existing
compatibility projection of the current attributable statement by when it was stated.
Under ADR-0036, a later attributable timing is a Committed Date Change; it does not
erase the earlier statement, its wording, or its precision.

The actor boundary from ADR-0026 applies. Verified Evidence must establish the
stated party through its registered aliases; a project-side actor, unsupported
attribution, or incomplete call fact cannot create a Committed Date projection. The
recorder is resolved from deployment identity, never a form field. The audit trail
records that person and the resulting event.

A Verbal is the fourth report provenance class, after Assertion, Derivation
and Work Decision. It resolves to the event, stated party, recorder and
conversation date. It cannot be constructed as an Assertion or a Derivation,
and `assert_no_bare_cells` treats it as a first-class, non-bare source. A
document-only report is a read-time switch: it recomputes Committed Dates and
date-based exceptions from verified cited events only, omits Verbal provenance,
and falls back to the current cited event's own page citation. No second
Committed Date is stored.

## Considered options

**A separate verbals table.** Rejected. It would give a Dependency one history
of machine-extracted statements and a second history of what the coordinator
heard, then require every reader to combine both before it could answer which
statement is newest.

**Treat a missing EvidenceLink as a verbal.** Rejected. Missing evidence is a
defect or incomplete import, not a source declaration. Inferring provenance
from absence would let a broken citation masquerade as an intentional record.

**Overwrite the Committed Date directly.** Rejected. The later date would be
useful, but the earlier promise and the fact its timing changed would disappear. The
event projection already supplies the correct update rule without a second
source of truth.

**Store a second, document-only Committed Date.** Rejected. A record would
hold two answers to one question. The report filters the event history at read
time instead.

## Consequences

The Ledger list and record detail visibly mark a verbal with who heard it and
when. The report distinguishes it from a page citation, and exposes how much
date coverage rests on verbals. A document-only report remains suitable for an
agency handoff, while the everyday report can honestly show the fresher call.
