---
status: accepted
---

# Domain language follows researched construction practice

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** The complete glossary adoption extends this decision. The mapping below includes those later accepted names; the earlier spelling remains valid only in preserved history, source language, or implementation identifiers.

Corridor's earlier language made an individual construction condition sound like work ready to start. It also used names that conflict with established utility coordination and scheduling language. Decision: use researched industry terms where their meanings fit, and distinguish a supported project outcome from its documentation review and from permission to start construction. The [initial August 27, 2026 research](../research/construction-industry-terminology-2026-08-27.md) and [complete glossary review](../research/glossary-terminology-review-2026-08-27.md) supply the source basis and limits. The latter includes the [Milestone research](../research/milestone-terminology-2026-08-27.md).

## Adopted language

The [context map](../../CONTEXT-MAP.md) routes definitions to the Project Record or Corridor Operations glossary. The following tables map retained implementation and historical names; those names are not a second customer vocabulary. The complete glossary review accounts for every term, including terms retained with clearer definitions and the additional boundary concepts.

| Earlier language | Current language and scope |
| --- | --- |
| Dependency | **Constraint** for the external condition affecting specified construction; **Utility Conflict** for actual or potential utility interference. Name a permit, agreement, or access condition specifically where possible. |
| Ledger | **Constraint Records** are the accepted Constraints; Ledger remains the internal identifier. Customer views say **Utility conflicts** or **Constraint log** according to their population. The Project Record remains broader. |
| Readiness Requirement | **Required Documentation**: what the source records must establish for the particular condition. This is not a silent expansion into all construction acceptance criteria. |
| Ready / readiness judgment | A **Documentation Review** decides whether exact current Supporting Documentation meets the stated requirement. Display only the supported, scoped result, such as **Relocation Complete**, **Permit Issued**, **Agreement Executed**, or **No Utility Conflict**, with its source and review basis. There is no universal replacement status named Complete or Cleared. A legacy sufficiency marker alone cannot establish one of these specific outcomes. |
| Evidence | **Supporting Documentation** in domain explanations and customer views; exact registered source passages, verification, and page identity remain required for document-sourced authority. |
| External Party | **External Organization** retains the organizational boundary. Customer views name the organization or its actual role, such as **Utility Owner**. **External Party Statement** remains the statement category; show **Statement from [party name]** to the customer. |
| Internal Owner | **Assigned To** for the named project person managing the Next Action. Use Utility Coordinator only when that is the person's actual role. |
| Need Date | **Required By**, retaining the exact **Key Date Version** behind the date. |
| Committed Date | **Promised For**, preserving the organization's wording and precision. Keep it distinct from an estimate, actual completion, and the date of a review. |
| Committed Date Change | **Change to Promised Timing**, including day, month, and approximate timing. Preserve both attributable statements and their direction only when supported. |
| Commitment Closure | **Completion Reported**, naming the exact Commitment and its cited or Recorded Verbal Statement support. This alone does not complete a project action or satisfy every linked Constraint. |
| Milestone | Retain the technical schedule event; show **Key dates**, with the event name and scheduled date separately. A scheduled date does not establish that the event occurred. |
| Milestone Registration / Milestone Revision | **Key Date Version**, one preserved event/date record with its source row and fingerprint. It is not an entire schedule revision or necessarily an approved baseline. |
| Milestone Impact / Effect on Milestone | **Effect on Key Dates**, naming the affected events and the recorded person's decision. It is not a calculated project delay. |
| Resolution Strategy | **Utility Conflict Resolution Method**; show **Resolution method** and the actual action. Literal source fields such as `Resolution Strategy Selected` keep their spelling. |
| Criticality | Display the actual resolution method, such as relocation, removal, or abandonment. The retained work-type filter is not a critical-path calculation or an urgency score. |
| Commitment Scope | **Applies To** identifies the explicitly selected Constraints, or **Not yet known**. All-active means the original recorded set, not a set that silently expands. |
| Commitment Lineage | Keep the internal identity; show **Commitment history**. Separate promises and their corrections cannot be merged by a wording change. |
| Coordination Subject | Keep the internal one-subject rule; show **For: [constraint or commitment name]**. |
| Work Decision | **Coordination Decision**; the stored and publication class `WorkDecision` stays unchanged. |
| Coordination Plan | **Follow-up Plan** for the project's response to one Constraint or Commitment. |
| Assertion | Keep the internal provenance class; show **Source field value**, distinct from the project's conclusion. |
| Dispute | **Source Discrepancy**; show **Sources disagree**. It is not necessarily a contract dispute. |
| Settlement | **Discrepancy Resolution**; show **Record conclusion** for one field. It is not settlement of a contract claim. |
| Operative Support | **Supporting Documentation in Use**; show **Used for this value** or **Used for this review**. In use and current source revision are separate facts. |
| Derivation | Keep the internal provenance class; show **Calculated result** or **How this was calculated**, including nonnumeric rule results. |
| Verbal | **Recorded Verbal Statement**; the retained provenance class is `Verbal`. Name who heard it and when. It is not necessarily an audio recording or written confirmation. |
| Document of Record | **Preferred Source File**, the rendition used for citations. It does not confer legal record status or construction authority. |
| Numbering Scheme | **Row Identification Rule**, declared for that source rather than inferred from a repeated number. |
| Retired Row | **Retired Matrix Row**. Preserve the exact retirement wording; a populated facility row is not automatically retired. |
| No Conflict Confirmed | **No Utility Conflict**, limited to the facilities, location, and design supported by the reviewed documents. |
| Not Relevant | **Do Not Add**. Preserve the Extracted Proposal, reason, author, and reversible decision. |
| Dismissal | **Remove from Active Log**, restricted to incorrect entries. Preserve history and restoration; this does not report work complete. |
| Attention Reason / Work Item | Keep internal grouping identities; show **Why this needs attention** and the actual question or action. **Coordination item** is the fallback type label. |
| Exception | **Constraint Alert**, a derived attention condition. It is not an exception to utility accommodation rules or an urgency score. |
| Evaluation | Keep the internal calculation identity; show **Constraint Check** or **Checks as of [date]**. This is not a professional audit or schedule data date. |
| Report | **Coordination Report**; **Constraint status report** describes its coverage. |
| Approved Export | **Report Approved for Release**; show **Approved to share** for the exact retained PDF. This neither accepts work nor proves delivery. |
| Briefing | **Coordination Summary**, visibly an AI draft. Every required Constraint Alert remains represented, including members of a cited bucket. |

### Operations aliases

| Earlier language | Current language and scope |
| --- | --- |
| Active Run | **Current Production Run**, explicitly selected for current work, not necessarily the newest attempt or a running process. |
| Candidate | **Extracted Proposal**, retaining its original content and actual handling outcome. |
| Admission | **Record Inclusion**, by a permitted human act or an exact deterministic rule. Extraction alone is not inclusion. |
| Adjudication | **Human Record Decision**, not a construction contract adjudication process. |
| Unplaced Statement | **Statement Needing Clarification**, naming whether speaker, timing, or affected Constraints is unresolved. Do not apply this pending-proposal label to an already recorded statement. |
| Supersession Review | **Document Revision Review**, current work caused by a replaced source or incomplete processing. |
| Reconfirmation | **Human Support Update**, replacing support for the same established conclusion. |
| Automatic Carry-Forward | **Automatic Support Update**, limited to exact, unique, unchanged source replacement. |
| Carry-Forward Policy | **Automatic Support Update Rules**, released engineering rules, not project authorization. |
| Carry-Forward Run | **Support Update Run Record**, retaining every applied and abstained outcome. |
| Revision Processing | **Document Revision Processing**, with comparison verification before any update. |
| Cohort Receipt | **Rehearsal Input Manifest**, preserving exact immutable membership and the permitted work boundary. |
| Lane | **Processing Scope**, the internal set of offered items and permitted operations, not just a view filter. |
| Evidence Investigator | **Statement Review Assistant** in explanations; keep existing technical identifiers and its bounded read-only authority. |
| Product Proving Run | **Product Test Run**, retaining ADR-0046's exact inputs, frontend exercise, restoration, two-pass requirement, and claim limits. |

Commitment, Next Action, Action Due Date, Supersession, Document, Extraction Run, Revision Comparison, Extraction Measurement, and Abstention retain their distinctions. A completed policy Abstention is not an execution failure; timeout, budget exhaustion, and harness failure remain failures. Where prose needs a verb or person, use ordinary language such as "the assigned person" or "a report of completion" rather than forcing field labels into sentences.

The broader industry definitions also matter. A Utility Conflict Matrix may contain potential conflicts or unresolved methods. A Utility Inventory may contain conflict notes or resolution information. Their meanings are not exclusive parser categories; adopting the definitions does not change existing extraction or inclusion policy.

The source distinctions are important. [LCI](https://leanconstruction.org/lean-topics/last-planner-system/) uses make-ready for preparing work to be performed. [AACE](https://library.aacei.org/terminology/welcome.shtml) distinguishes an activity dependency, acceptance criteria, and critical-path terminology. [TxDOT](https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process.html) distinguishes adjustment verification from utility certification, which can address arrangements for remaining work. These sources do not mandate every label in the table; the scoped product wording is an explicit choice.

## Research before a terminology proposal

Before an agent suggests, introduces, renames, or materially redefines a domain term, it must research applicable industry standards and primary practice sources. This applies to conversation and planning as well as glossaries, ADRs, screens, reports, and domain concepts in code. The operational procedure and completion criteria are in [the agent domain guide](../agents/domain.md#research-before-proposing-terminology); `AGENTS.md` makes that procedure a required entry point.

Prefer the responsible agency's current material and recognized industry bodies over a plausible-sounding synonym. Record the source, date or edition, relevant definition, and scope. Separate established terminology from a proposed plain-language label. If no exact counterpart is supported, explain the gap and obtain explicit agreement on the product wording before adopting it. A technical implementation name or historical identifier does not become customer language by default.

## Preserved decisions and implementation boundary

This amends vocabulary throughout the earlier ADRs. It does not change their authority boundaries:

- Quote verification establishes that a passage occurs in its source. A human Documentation Review judges whether exact current Supporting Documentation meets the exact stated requirement; automatic processing cannot originate that judgment.
- Exact unchanged support can inherit an established judgment under the Automatic Support Update Rules. Loss of applicable support removes the current conclusion without erasing the earlier review.
- A report of completion concerns one Commitment. It neither completes an internal Next Action nor proves every related construction condition.
- A specific outcome must be supported for its exact facilities, location, and scope. A missing document does not establish that physical work is unfinished. A legacy `is_ready` value cannot by itself manufacture a relocation completion, permit, or no-conflict finding.
- Internal publication remains automatic. External release still binds a designated human to fixed PDF bytes. An internal documentation review is not contractual acceptance, utility certification, or authorization for field work.
- The retired mutable Dependency lifecycle remains retired. Clearer labels do not add an Open → Ready → Complete state machine.

The runtime still uses identifiers such as `Dependency`, `EvidenceLink`, `evidence_required`, `is_ready`, `is_critical`, and `MilestoneRegistration`. Terminology adoption changes presentation and explanations; it does not rename schema objects, rewrite historical receipts, or implement the pending functional audit fixes. Existing filenames, identifiers, literal source quotations, and explicitly historical decision text retain their original spelling. Amendment notes identify how to read them now.

The requirement-revision lifecycle and the policy for reviewing Effect on Key Dates after schedule changes remain separate design decisions. Adopting these labels does not decide either policy. Additional glossary entries clarify existing concepts or deferred boundaries; they do not implement document transmittals, acknowledgments, formal contract acceptance, or new schedule calculations.

## Alternatives rejected

**One generic completion word for all records.** This obscures whether design is complete, an agreement is executed, a utility conflict no longer exists, or relocation work is finished.

**Invent the name first and research it later.** A persuasive explanation can hide a mismatch with practice. The evidence must precede the proposal.

**Copy an agency status list into a mutable project-wide lifecycle.** Agency labels have particular populations and authority. Use their meanings where they fit without discarding Corridor's separate facts, scope, review, and history.
