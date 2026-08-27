---
status: accepted
---

# Domain language follows researched construction practice

Corridor's earlier language made an individual construction condition sound like work ready to start. It also used names that conflict with established utility coordination and scheduling language. Decision: use researched industry terms where their meanings fit, and distinguish a supported project outcome from its documentation review and from permission to start construction. The [August 27, 2026 research](../research/construction-industry-terminology-2026-08-27.md) supplies the source basis and limits; this ADR records the adopted language.

## Adopted language

The Project Record glossary owns the definitions. The following table also maps retained implementation and historical names; those names are not a second customer vocabulary.

| Earlier language | Current language and scope |
| --- | --- |
| Dependency | **Constraint** for the external condition affecting specified construction; **Utility Conflict** for actual or potential utility interference. Name a permit, agreement, or access condition specifically where possible. |
| Ledger | The internal working set of Constraints; customer views say **Utility conflicts** or **Constraint log** according to their population. The Project Record remains broader. |
| Readiness Requirement | **Required Documentation**: what the source records must establish for the particular condition. This is not a silent expansion into all construction acceptance criteria. |
| Ready / readiness judgment | A **Documentation Review** decides whether exact current Supporting Documentation meets the stated requirement. Display only the supported, scoped result, such as **Relocation Complete**, **Permit Issued**, **Agreement Executed**, or **No Conflict Confirmed**, with its source and review basis. There is no universal replacement status named Complete or Cleared. |
| Evidence | **Supporting Documentation** in domain explanations and customer views; exact registered source passages, verification, and page identity remain required for document-sourced authority. |
| External Party | Retain this broader organization concept; say **Utility Owner** when the organization owns utility facilities. |
| Internal Owner | **Assigned To** for the named project person managing the Next Action. Use Utility Coordinator only when that is the person's actual role. |
| Need Date | **Required By**, retaining the exact Milestone Revision behind the date. |
| Committed Date | **Promised For**, preserving the External Party's wording and precision. Keep it distinct from an estimate, actual completion, and the date of a review. |
| Commitment Closure | **Completion Reported**, naming the exact Commitment and its cited or Verbal support. This alone does not complete a project action or satisfy every linked Constraint. |
| Milestone Registration | **Milestone Revision**, the immutable source-bound record. It is not necessarily an approved schedule baseline. |
| Milestone Impact | **Effect on Milestone**, naming the affected Milestones and the recorded decision. |
| Criticality | Display the actual **Resolution Strategy**, such as relocation, removal, or abandonment. The retained work-type filter is not a critical-path calculation or an urgency score. |

Commitment, Committed Date Change, Commitment Scope, Commitment Lineage, Next Action, Action Due Date, Work Decision, and the other unaffected record concepts retain their meanings. Promised For is a field label, not a new event type. Where prose needs a verb or person, use ordinary language such as "the assigned person" or "a report of completion" rather than forcing field labels into sentences.

The source distinctions are important. [LCI](https://leanconstruction.org/lean-topics/last-planner-system/) uses make-ready for preparing work to be performed. [AACE](https://library.aacei.org/terminology/welcome.shtml) distinguishes an activity dependency, acceptance criteria, and critical-path terminology. [TxDOT](https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process.html) distinguishes adjustment verification from utility certification, which can address arrangements for remaining work. These sources do not mandate every label in the table; the scoped product wording is an explicit choice.

## Research before a terminology proposal

Before an agent suggests, introduces, renames, or materially redefines a domain term, it must research applicable industry standards and primary practice sources. This applies to conversation and planning as well as glossaries, ADRs, screens, reports, and domain concepts in code. The operational procedure and completion criteria are in [the agent domain guide](../agents/domain.md#research-before-proposing-terminology); `AGENTS.md` makes that procedure a required entry point.

Prefer the responsible agency's current material and recognized industry bodies over a plausible-sounding synonym. Record the source, date or edition, relevant definition, and scope. Separate established terminology from a proposed plain-language label. If no exact counterpart is supported, explain the gap and obtain explicit agreement on the product wording before adopting it. A technical implementation name or historical identifier does not become customer language by default.

## Preserved decisions and implementation boundary

This amends vocabulary throughout the earlier ADRs. It does not change their authority boundaries:

- Quote verification establishes that a passage occurs in its source. A human Documentation Review judges whether exact current Supporting Documentation meets the exact stated requirement; automatic processing cannot originate that judgment.
- Exact unchanged support can inherit an established judgment under the existing Carry-Forward rules. Loss of applicable support removes the current conclusion without erasing the earlier review.
- A report of completion concerns one Commitment. It neither completes an internal Next Action nor proves every related construction condition.
- A specific outcome must be supported for its exact facilities, location, and scope. A missing document does not establish that physical work is unfinished. A legacy `is_ready` value cannot by itself manufacture a relocation completion, permit, or no-conflict finding.
- Internal publication remains automatic. External release still binds a designated human to fixed PDF bytes. An internal documentation review is not contractual acceptance, utility certification, or authorization for field work.
- The retired mutable Dependency lifecycle remains retired. Clearer labels do not add an Open → Ready → Complete state machine.

The runtime still uses identifiers such as `Dependency`, `EvidenceLink`, `evidence_required`, `is_ready`, `is_critical`, and `MilestoneRegistration`. This documentation change does not rename schema objects, rewrite historical receipts, or implement the pending audit fixes. Existing filenames, identifiers, literal source quotations, and explicitly historical decision text retain their original spelling. Amendment notes identify how to read them now.

The requirement-revision lifecycle and the policy for reviewing Effect on Milestone after schedule changes remain separate design decisions. Adopting these labels does not decide either policy.

## Alternatives rejected

**One generic completion word for all records.** This obscures whether design is complete, an agreement is executed, a utility conflict no longer exists, or relocation work is finished.

**Invent the name first and research it later.** A persuasive explanation can hide a mismatch with practice. The evidence must precede the proposal.

**Copy an agency status list into a mutable project-wide lifecycle.** Agency labels have particular populations and authority. Use their meanings where they fit without discarding Corridor's separate facts, scope, review, and history.
