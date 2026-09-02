---
status: accepted
domain: reports
scope: current product
amends:
  - ADR-0040
  - ADR-0053
migration: no ReleasePackage exists; preparation, the package receipt, and the authorization gate are unimplemented (#529, #533).
---

# One authorized release package is the external issue unit

**Amends ADR-0040 and ADR-0053.**

ADR-0040 decided that external release is an attributable human authorization of one already-rendered, fixed PDF artifact, and that PDF is the only released format in the first slice. ADR-0053 decided that a report's comparison baseline is the last Report Approved for Release. Both were written when the released thing was a single Coordination Report PDF.

The paid vertical slice returns four artifacts to the customer, not one: their updated native UCM workbook, a change summary, a chase list, and the weekly Coordination Report. If each were released separately, a coordinator would perform four authorizations a week, four receipts would disagree about which Project Record revision they covered, and ADR-0053's baseline marker would have four candidates. This ADR narrowly amends the **release-unit** clauses of both.

Everything else in ADR-0040 is unchanged and still governs: individual attribution, immutable bytes bound by digest, the exhaustive integrity gate, honest adverse content as a nonblocker, and release as an act distinct from delivery. ADR-0053's approval-driven comparison principle is likewise unchanged — only the thing that gets approved is now a set.

## One authorization seals one immutable set

**One attributable human authorization seals one immutable set of configured customer artifacts.** The unit is the set, not the file. `ReleasePackage` is an internal technical name; the customer sees their own issue in project language.

Every artifact in a package is generated from exactly one of each of the following, shared across the whole set:

- one **accepted Project Record revision**;
- **zero or one previous approved package** (zero only for a project's first);
- one **source cutoff** — the point past which later-arriving sources are excluded;
- one **declared coverage state** — what was and was not reviewed for this issue; and
- one **approved template and mapping set** — the customer's own UCM template and the field mapping into it.

Because every artifact reads the same five inputs, the workbook, the summary, the chase list, and the report cannot contradict one another about what the record said or when the window closed.

Each artifact remains **fixed by its own digest**, exactly as ADR-0040 requires of the released PDF. **One package receipt binds the complete set** and enumerates:

- every artifact's identity and SHA-256 digest;
- the accepted Project Record revision;
- the previous approved package, or an explicit statement that none exists;
- the source cutoff;
- the declared coverage state;
- the template and mapping identities;
- the releasing person's identity; and
- the release timestamp.

The set is immutable once sealed. A changed artifact, a different revision, a different cutoff, or a regenerated workbook requires a **new package**; Corridor never regenerates a sealed package in place.

## The first-slice artifact set is configured, not chosen weekly

The configured first-slice set is the **updated native UCM, change summary, chase list, and weekly Coordination Report**. A **redline or provenance sidecar may participate** where a customer wants one.

Which artifacts participate is **project configuration, decided once**. The coordinator is not asked to choose variants every week; the weekly interaction is *authorize this issue*, not *assemble this issue*. Optional artifacts are configured outside that interaction.

## The comparison baseline is the last approved package

ADR-0053's marker moves from the last released PDF to the **last approved package**.

- **Before a project's first approved package there is no comparison predecessor.** The configured outputs render current accepted state only, and where the customer's format has a "changes since" region they **state that no prior approved issue exists**. Corridor does not invent a prior report occurrence, and does not treat the adopted baseline itself as a previous issue.
- **Thereafter the previous approved package is the baseline** — not an internal snapshot, not a prepared-but-unapproved set, and not the newest render of any kind.
- **A prepared but unapproved set never advances the baseline.** Preparing a candidate for review, discarding it, and preparing another leaves the marker where it was.

This preserves ADR-0053's reason exactly: the audience's last view is the last thing approved for them, so "what changed" means changed since that.

## Release stays separate from delivery

Unchanged from ADR-0040. Authorizing a package is not sending it. Email, document-control delivery, receipt, and transmittal status remain outside Corridor's first slice, and sending the same sealed package again is not another release.

## What blocks authorization

Only these block:

- **integrity** — an artifact lacks supported provenance, the set combines inconsistent inputs, or Corridor cannot seal and retain the exact bytes;
- **stale preview** — the accepted Project Record revision moved after the candidate was prepared, so the reviewed preview no longer matches what would be sealed;
- **required coverage** — the project's configured coverage requirement for an issue is unmet; or
- **explicit customer policy** — a rule the customer configured for their own issues.

**Honest adverse project conditions never block release.** Unresolved Proposed Deltas, unknown scope, overdue commitments, and missing follow-up plans are rendered truthfully and the package goes out. ADR-0040's reasoning stands: the gate protects integrity, not appearances. Hiding a live problem to make an issue look complete would make it less trustworthy, not more ready.

## Terminology

This decision introduces **no new customer-facing term**. `ReleasePackage` is internal. The customer-facing act remains the existing **Report Approved for Release** sense from the glossary, widened from one PDF to the configured set; the glossary entry needs that widening when the package ships. No new label is proposed here, so the terminology-research procedure in `docs/agents/domain.md` is not triggered.

## Considered options

**Keep releasing one PDF and let the workbook go out informally.** Rejected. The workbook is the artifact the customer actually works from. Leaving it outside the sealed unit means the thing most likely to be relied on is the one thing with no digest, no receipt, and no named approver.

**Release each artifact separately with its own receipt.** Rejected. Four authorizations a week for one coordination act, four receipts that can disagree about the covering revision, and no single answer to "which issue did the customer receive".

**Let the coordinator pick which artifacts go out each week.** Rejected. It turns a weekly authorization into weekly assembly, and it makes the issue series inconsistent from week to week for no customer benefit. Participation is configuration.

**Advance the comparison baseline on preparation rather than approval.** Rejected for ADR-0053's original reason: a prepared set the customer never saw would silently consume part of the next issue's changes.

**Treat the adopted baseline as the first package.** Rejected. The baseline is the customer's own artifact adopted as the starting record, not an issue Corridor authorized and returned. Calling it a predecessor would claim a prior issue occurrence that never happened.

## Consequences

- ADR-0040's "Release binds exact content and context" section now describes one member of a set; the package receipt is the outer binding, and the per-artifact digest rule is unchanged. Its PDF-only clause is widened to the configured set — the workbook is released as the customer's native format, not as a PDF rendering of it.
- ADR-0053's baseline selection changes from "newest released report run" to "last approved package"; its no-false-occurrence and settings-changed rules are unchanged.
- #529 carries preparation of the candidate from one coherent reading; #533 carries authorization and the receipt. #495 renders the UCM, #534 the change summary and weekly report, and #425 the chase list.
- The release store must retain the bytes of every artifact in a package, not only a PDF.
