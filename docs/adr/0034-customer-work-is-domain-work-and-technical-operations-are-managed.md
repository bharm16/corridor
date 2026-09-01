---
status: accepted
domain: operations
scope: current product
amends:
  - ADR-0022
  - ADR-0029
amended_by:
  - ADR-0049
  - ADR-0078
  - ADR-0079
  - ADR-0076
---

# Customer work is domain work, and technical operations are managed

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Corridor's first users are construction workers and project coordinators. The
current operating path asks a person to understand an Current Production Run id, inspect an Automatic Support Update Rules digest, and use terminal commands before the product can do
ordinary work. Those are system-maintenance choices, not project decisions. A
customer cannot give meaningful authorization to rules they cannot inspect, and a
project coordinator should not have to understand extraction lineage to use the
Constraint Log.

Decision: **customer users do domain work; Corridor manages technical operations.**
A customer-facing workflow never requires a command line, a database query, an
internal id, an Current Production Run choice, a policy digest, or knowledge of Automatic Support Update. Customer screens name the project question: choose the assigned
person, record a Next Action, say what an External Organization stated, resolve a Source Discrepancy,
or judge whether Supporting Documentation meets the stated requirement. Technical
failures become work for Corridor operations, not disguised work for the customer.

Corridor operations owns first-release project setup. It creates the project,
registers its Documents and External Organizations, connects the agreed document
location, and loads the project team. The customer confirms that setup; the
customer does not configure extraction rules or safety policies. After setup,
Corridor monitors the connected location and processes new revisions. Manual upload
is a fallback for a Document received elsewhere. Supersession still requires
Supporting Documentation that establishes the replacement; neither automation nor
an operator may infer it from a filename or date.

When an incoming Document uses an unfamiliar External Organization name, the coordinator
confirms a suggested existing party or creates a new one; Corridor records a
confirmed alias for later Documents. That is a domain identity judgment, not a
technical ingestion task. First-release accounts belong only to the project team.
External Organizations do not receive Corridor accounts; their statements enter through
Documents and attributable Recorded Verbal Statements.

## Automatic Support Update is normal fail-closed processing

Automatic Support Update no longer requires project authorization. When Document Revision Processing proves an exact, unique, mechanically verified, unchanged successor row,
Corridor moves only the Supporting Documentation in Use roles that already carry a human judgment.
It does not perform Record Inclusion, change a Constraint's conclusion, or originate
a Documentation Review. Changed, ambiguous, incomplete, or unverifiable input
produces Abstention and leaves the Ledger unchanged. A successful transfer appears in plain project history
and does not interrupt a user. A material change creates a plain-language task about
what changed; the user never sees a Human Support Update lane.

This decision **supersedes ADR-0022's project-authorization requirement**, including
its rule that the absence of an active authorization sends exact unchanged rows to
Human Support Update. ADR-0022's eligibility proof, separation of roles,
fail-closed behavior, immutable Support Update Run Record and outcome receipts, and ban on
judging that a documentation requirement is met remain in force. Rule versions and
source digests remain in the receipts for replay and audit. Changes to those rules pass through Corridor's
engineering and release controls; asking a project coordinator to sign a digest is
not a safety control.

## Current Production Run selection is automatic but never means latest

Normal Document processing declares its single successful production Extraction Run
as the Current Production Run. The declaration remains explicit, attributable in run lineage,
and replayable; automatic does not mean inferred from the greatest id or newest
timestamp. A test, evaluation, acceptance replay, experiment, or unrelated backfill
must carry lineage that makes it ineligible for automatic production selection. It
can never replace the Current Production Run.

If production processing does not yield one safe choice, Corridor selects nothing
new. If a prior valid Current Production Run exists, it remains declared; if none exists, the
Document remains without one. Corridor operations receives the technical detail and
resolves it. The customer sees only the consequence in project language, such as
"A newer document needs attention. Some supporting documents are no longer current."
The existing Ledger and Coordination Decisions remain usable. Provenance still fails closed: a
superseded Document cannot regain actionable Extracted Proposals. A documentation requirement
is no longer treated as met when all applicable support cites superseded Documents.
This refines ADR-0019's explicit-declaration rule; it does not permit an implicit latest-run rule or weaken
its successor fail-closed behavior. Acceptance captures remain outside production
lineage under ADR-0024.

## Internal operations may remain technical

Trained Corridor operators may use CLI tools in the first release. An operation that
affects the Ledger or its operative lineage writes an attributable, immutable
receipt. Its project consequence appears in plain history, while run ids, digests,
and other technical detail stay behind an audit view. Internal tools are not a
customer workflow merely because Corridor can use them to investigate or recover
from a failure.

The customer identity boundary is separate: every human act uses the person's
individual identity, captured by the product rather than typed into a form. The
first customer release uses email magic-link sign-in; enterprise SSO may follow.
The internal #196 rehearsal may use one seeded coordinator identity, but that is a
rehearsal shortcut, not the production sign-in contract.

## Safe failure behavior

A processing failure does not lock the Ledger or erase coordination state. Corridor
operations receives an actionable alert. Affected users receive a plain consequence
only when current project work is affected. Exact Automatic Support Update succeeds quietly in
history; changed Supporting Documentation, a documentation requirement no longer
met, or a current commitment affected by a new Document becomes prioritized domain
work. The system never guesses, silently
publishes stale support, or asks a customer to repair lineage.

## #196 sequencing

This ADR records the target operating contract; it does not put the Current Production Run and
Automatic Support Update redesign on #196's critical path. #196 is the first narrow coordinator
slice. Before feature work, the later local statement-publication hardening commit
and this corrected domain contract are each transplanted or applied to fresh current
`main`, independently reviewed, and delivered as separate changes. The divergent
working branch is never merged wholesale.

Corridor operations then rehearses the SH 99 mechanical Record Inclusion backfill against
an isolated clone of a pinned current database and current source. The receipt audit
compares exact before and after Ledger rows, Extracted Proposal states, Policy Runs, outcomes,
and reason codes; it never substitutes guessed totals. Candidate 7587 abstains, and
Candidates 7129 and 7296 remain pending for guided Human Record Decision bound to Supporting
Documentation. A second run may append an immutable run receipt but creates no duplicate Ledger fact.
Only after that proof and a separate explicit approval may operations mutate the
shared SH 99 database.

Setup and operations time are measured separately. The simulated coordinator then
works only through the product interface with a seeded individual identity. A
customer-facing CLI command, database query, or manual data repair fails the
rehearsal. The result is an internal workflow measurement with provisional timing,
not target-user validation. Magic-link sign-in, notification delivery, connected
ingestion, production roles, construction-worker permissions, and the operations
redesign must exist before customer use but may follow the #196 slice. Legacy
Extracted Proposals remain immutable; changing the minutes prompt or extractor is a separate
measured issue rather than a prerequisite that rewrites the rehearsal input.

## Considered options

**Teach coordinators the CLI or build them an administrative screen.** Rejected.
Changing the shape of the same technical ceremony does not turn run lineage or a
policy digest into project work.

**Keep customer authorization for Automatic Support Update.** Rejected. Exact
unchanged Supporting Documentation adds no new judgment, and a signature from
someone who cannot evaluate the rules creates false assurance. The eligibility proof, Abstention, and
receipts are the safety controls.

**Select the latest completed Extraction Run.** Rejected. An experiment, evaluation,
or backfill could silently change the Ledger. Production purpose and explicit
lineage, not time or id order, determine eligibility.

**Block the entire Ledger while technical processing needs attention.** Rejected.
Existing Constraints and Coordination Decisions remain useful. Only conclusions that need
current Supporting Documentation, including conclusions from Documentation Review,
must fail closed.

## Consequences

The implementation must distinguish production Extraction Runs from experiments,
evaluations, acceptance replays, and other non-production work before automatic
Current Production Run selection ships. Automatic Support Update must stop treating a project
PolicyApproval as execution authority while retaining versioned policy and outcome
receipts. Corridor needs operations alerting and a runbook for ambiguous or failed
processing. Customer documentation and screens must remove CLI instructions,
internal ids, Current Production Run selection, Automatic Support Update authorization, and Human Support Update
as customer tasks.

Managed setup and connected ingestion create service work for Corridor in the first
release. That is intentional: the company owns technical complexity until a safe,
plain self-service workflow exists. The permission boundary for construction workers
remains explicitly deferred; this ADR grants them no Ledger-write authority.

## Historical decision inventories

The historical interview decision inventory and the 2026-08-12 post-foundation choice ledger were moved verbatim to [docs/operations/adr-0034-decision-inventories.md](../operations/adr-0034-decision-inventories.md) on 2026-09-01. They remain the loss-prevention record the interview required; this ADR keeps only the managed-operations decision.
