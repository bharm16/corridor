---
status: accepted
domain: reports
scope: current product
---

# External Coordination Report release seals one fixed PDF artifact

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

An internal Coordination Report and an externally released artifact are different products of
the Ledger. The working Coordination Report must stay current without a sign-off gate. An
external recipient, however, must receive the exact artifact a named project person
authorized, not a live page or filesystem path whose contents can drift after the
act.

Decision: **internal Coordination Reports update automatically; external Coordination Report release is an
attributable human authorization of one already-rendered, fixed PDF artifact.** PDF
is the only released format in the first slice. Additional formats require later
decisions about their rendering and integrity; they are not implied by PDF release.

## The party-level section tells the unresolved truth

The Coordination Report contains an **External Organization commitments** section. Every open,
attributable party-level Commitment whose Constraint scope is not yet known appears,
not merely the past-due ones. Commitments with Completion Reported remain available in history rather
than occupying the open coordination section.

Each entry carries:

- External Organization and the supported statement;
- timing as stated and its precision;
- **Applies To**, including an explicit **Not yet known** when the affected Constraints are unknown;
- the current statement-level Follow-up Plan;
- open and past-due status; and
- exact Supporting Documentation or Recorded Verbal Statement provenance.

Unknown scope remains party-level. It does not supply a Promised For value, a
completion finding, or a Constraint Alert for a Constraint. Known-scope statement rendering stays with the linked
Constraint views; it is not part of this unknown-scope section contract.

## Release binds exact content and context

One release receipt binds:

- the exact PDF bytes or their immutable content-addressed object;
- a SHA-256 digest of those bytes;
- the Evaluation date and ruleset version;
- the provenance mode;
- the exact covered records and statement versions;
- the releasing person's identity; and
- the release timestamp.

The released artifact is immutable. Changed bytes, a different Evaluation, changed
covered records, or a new Coordination Report require a new release; Corridor never regenerates
an earlier release in place. Sending the same sealed artifact through email or a
document-control system is not another release. Delivery, receipt, and transmittal
status remain outside Corridor's first slice.

Only an integrity failure blocks release:

- published content lacks supported provenance;
- the artifact combines inconsistent Evaluation inputs; or
- Corridor cannot seal and retain the exact bytes.

Unknown scope, an overdue Commitment, a visible missing Follow-up Plan, or other
honestly rendered adverse content does not block release. Hiding those facts would
make the artifact less trustworthy, not more ready.

A designated project person releases external Coordination Reports. The internal #196 rehearsal
may use its seeded coordinator as that person. This proves only the release
interaction and receipt; production authentication, roles, worker permissions, and
delivery remain deferred.

## Considered options

**Approve a live Coordination Report URL.** Rejected. Its contents can change after approval, so
the act authorizes no stable artifact.

**Treat an internal Coordination Report run or filesystem path as a release.** Rejected. A path
does not bind bytes, actor, time, or immutable content and may be overwritten.

**Block release while the Coordination Report contains unknown scope, overdue work, or a missing
plan.** Rejected. Those are honest project conditions. The release gate protects
integrity, not appearances.

**Send the artifact from Corridor as part of release.** Rejected for the first
slice. Authorization and delivery have different actors, evidence, and failure
modes; the project team keeps its current email or document-control process.

## Consequences

Coordination Report generation and external release require separate records and commands. The
release store must retain fixed bytes or immutable content-addressed storage, not
only a mutable path. A released PDF remains retrievable and verifiable by digest
after the Ledger changes. Internal Coordination Reports remain automatic and need no approval.

## Decision map

This ADR records decisions 34-36 and 79 from the original product-workflow
interview, and the 2026-08-12 post-foundation decisions P44-P50: all open
unknown-scope Commitments in the party section; complete entry fields; automatic
internal Coordination Reports; PDF-first release; exact receipt content; exhaustive integrity
blockers; truthful nonblockers; release separate from delivery; and the seeded
rehearsal releaser boundary.
