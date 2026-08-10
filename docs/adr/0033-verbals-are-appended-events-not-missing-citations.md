# A verbal is an appended event, never a missing citation

The freshest commitment often arrives by phone. Keeping it outside Corridor
leaves the ledger stale and moves the coordinator's real work back to a
spreadsheet; treating the call as a document would make a report more
trustworthy-looking precisely when it is less trustworthy. Decision: a
**Verbal** is an explicit `DependencyEvent` source kind, recorded by a human
from the conflict page with the stated party, what was said, the conversation
date and the promised date. It is append-only at the database. The current
Committed Date remains the existing projection of the newest commitment or
slip by when it was stated, so a later verbal supersedes without erasing the
earlier statement and a later date is a Slip.

The same actor boundary as ADR-0026 applies. The stated party must resolve,
through its registered aliases, to the Dependency's External Party; a
project-side party, mismatch, dismissed record, missing date or incomplete
call fact refuses. The recorder is resolved from deployment identity, never a
form field. The audit trail records that person and the resulting event.

A Verbal is the fourth report provenance class, after Assertion, Derivation
and Work Decision. It resolves to the event, stated party, recorder and
conversation date. It cannot be constructed as an Assertion or a Derivation,
and `assert_no_bare_cells` treats it as a first-class, non-bare source. A
document-only report is a read-time switch: it recomputes Committed Dates and
date-based exceptions from verified cited events only, omits Verbal provenance,
and falls back to the newest cited event's own page citation. No second
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
useful, but the earlier promise and the fact it slipped would disappear. The
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
