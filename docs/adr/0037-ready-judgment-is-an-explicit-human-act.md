---
status: accepted
---

# Documentation Review is an explicit human act

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Record Inclusion is mechanical (ADR-0029), and exact unchanged Supporting Documentation in Use may move
without a reviewer adding judgment (ADR-0022, as amended by ADR-0034). A
Documentation Review answers a separate question: whether exact current Supporting
Documentation meets the stated requirement for one Constraint. Deterministic
processing cannot originate that judgment.

Decision: a **Documentation Review** is a guided human act over one stated
requirement and one exact set of Supporting Documentation: **does this documentation
meet the stated requirement for this Constraint?** The implementation retains the
`evidence_required` field name. The screen shows the Required Documentation, the
source passages, and the Supporting Documentation in Use that a Yes answer would establish. The
available answers
are **Yes**, **Not yet**, and **Needs clarification**. One designated project person
is the default judge. Fine-grained construction-worker permission splits remain
deferred.

Whether the documentation requirement is met remains a Derivation, never a mutable
status. When current support lapses through supersession, loss of support, or a
changed current record, Corridor stops showing the requirement as met and records
visible high-priority work. It notifies the assigned person and the person who made
the prior judgment, showing the requirement and newer Supporting Documentation. The
earlier judgment remains in history; nobody must manually reverse it.

The product shows only the supported outcome for the stated scope, such as
Relocation Complete or Permit Issued, with its source and review basis. Meeting a
documentation requirement does not authorize field work or establish that every
construction constraint is satisfied. Missing documentation does not establish
that physical work is unfinished. The retained `is_ready` identifier cannot by
itself manufacture a specific outcome (ADR-0047).

## Specific changed-record work replaces a generic support-update queue

The generic customer support-update queue disappears. Exact unchanged support moves
automatically under the fail-closed proof bar. Changed, ambiguous, dropped, or
otherwise ineligible work becomes a specific question on the affected record:
resolve a Source Discrepancy, verify a citation, attach a statement, review new Supporting
Documentation against the stated requirement, or remove an incorrect entry from the active log. Durable receipts
remain, but the user works the project question rather than transfer mechanics.

Citation checking is exception-driven rather than blanket review. Corridor verifies
quotes mechanically. Human citation work is reserved for failed verification,
superseded Supporting Documentation in Use, contradictions that matter to a current conclusion,
the Documentation Review itself, or an external release integrity blocker.

User-facing history remains plain first and technical second. It names the
Documentation Review, any later loss of applicable support, actor, time,
requirement, and exact Supporting Documentation. Policy digests and support-update
receipts stay available behind technical details; they do
not become customer tasks.

## Considered options

**Let verified source text satisfy the requirement automatically.** Rejected. Exact
text presence does not prove that the text meets the project's stated requirement.

**Keep a generic Human Support Update queue.** Rejected. An exact unchanged transfer adds
no judgment, while a changed record presents a more specific question than
“reconfirm.”

**Require two people for every Documentation Review.** Deferred. One designated
person is the first-release default; a project may require a second person later when its
contract justifies that control.

## Consequences

Documentation Review needs a dedicated flow that shows the requirement and its
Supporting Documentation, with one designated default human. Loss of applicable
support creates visible work and preserves the earlier judgment. The supersession
surface presents concrete record questions rather than a generic support-update
queue. This ADR does not govern internal Coordination Report generation or
external artifact release; ADR-0040 owns that separate lifecycle.

## Decision map

This ADR records decisions 27-29, 31, 33, and 41 from the original
product-workflow interview. The 2026-08-12 post-foundation interview did not change
the human sufficiency-judgment boundary. ADR-0047 replaces the earlier Ready label
without deciding requirement-revision storage or historical migration policy.
