---
status: accepted
domain: supporting-documentation
scope: current product
amended_by:
  - ADR-0034
---

# Exact unchanged support may carry forward under an authorized policy

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> **Partially superseded by ADR-0034.** Automatic Support Update is now normal
> fail-closed processing and no longer requires project authorization. The exact
> eligibility proof, role boundaries, Abstention behavior, immutable receipts, and
> prohibition on originating a Documentation Review judgment remain in force. The body below records the
> historical decision; its authorization and Human Support Update requirements are
> not current and must not be implemented.

Human Support Update remains the act of reviewing and moving Supporting Documentation in Use, but an exact successor row can add no new judgment for a reviewer to make. Under this historical policy, a project could authorize a named, versioned set of Automatic Support Update Rules that performed Automatic Support Update only for an exact, unique, mechanically verified unchanged correspondence. The act moves only the publication and Documentation Review support scopes a human previously established; it never performs Record Inclusion, changes Constraint fields, creates a Documentation Review judgment, or treats model confidence as proof.

Eligibility fails closed. A project with no active policy retains Human Support Update; policy absence is not Abstention. Once an active policy evaluates affected work, the predecessor must have attributable Record Inclusion and trustworthy support history; the successor must be current, actionable, citation-verified, and linked through immutable exact Extraction Runs and a valid Revision Comparison; the correspondence must be one-to-one and unchanged; the admitted conclusion must not contain a human edit the successor source cannot prove; and the approved policy version must still be active for the project. Missing or unsafe inputs, structural fan-in or fan-out, ambiguity, change, corruption, or provenance failure produce Abstention and leave the Ledger untouched.

Each policy evaluation writes an immutable Support Update Run Record and durable, versioned per-row outcomes for both carried and abstained work. Repeating the same versioned Abstention does not manufacture another outcome. Each successful Automatic Support Update also writes an immutable transfer receipt binding the project authorization, policy version, exact comparison and finding, successor Extracted Proposal, inherited scopes, before-and-after Supporting Documentation, and input identity. The historical authorization included a digest of the deployed safety-rule source files as well as the matcher configuration; a change to either paused the old policy until a principal authorized the replacement. Qualifying rows required no per-row click.

Document Revision Processing owns the production order: create one exact Revision Comparison, read-verify its immutable content, and only then invoke the already-authorized Automatic Support Update Rules. Comparison remains Ledger-write-free. The orchestrator adds no authority of its own and cannot enable a policy as a side effect.

## Considered options

**Require Human Support Update for every unchanged row.** Rejected because the exact-match processing scope already establishes every fact the click could add; repeated clicks create attribution ceremony rather than judgment.

**Always enable automation when code is deployed.** Rejected because a deployment must not silently change a project's publication behavior or risk posture.

**Automatically resolve changed or ambiguous rows.** Rejected because consensus, similarity, and model confidence do not supply an independent semantic oracle. Incompleteness through Abstention is safer and more honest than a forced Ledger conclusion.

## Consequences

Audit and Document Revision Review must distinguish Automatic Support Update from Human Support Update while accepting both as valid support lineage. Reviewer status reports durable recorded Abstentions honestly rather than recomputing history away or counting identical reruns repeatedly. A carried Documentation Review support scope inherits an earlier human sufficiency judgment; it cannot make a Constraint's documentation requirement met without a prior attributable human judgment. Under the historical authorization policy, projects without an authorized set of Automatic Support Update Rules retained Human Support Update. ADR-0034 supersedes that authorization condition.
