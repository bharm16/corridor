# Construction industry terminology for Corridor

Researched on August 27, 2026.

**Status: research completed; vocabulary direction accepted on August 27, 2026.** [ADR-0047](../adr/0047-domain-language-follows-researched-construction-practice.md) records the adopted language and its limits. This note preserves the research and proposed display examples; it is not evidence that the application has been migrated. The implementation observations below describe the pre-adoption baseline.

## Answer

There is established industry language, but there is no single vocabulary that maps exactly to all Corridor concepts. Agency terminology, construction planning terminology, and contractual acceptance terminology have different scopes.

For Corridor's utility work, **Utility conflict** is the strongest established name for the interference being managed. For the broader collection of outside conditions that affect construction, **Constraint** is a recognized planning term. In the construction planning methods examined, **Ready** describes work that can be performed, so it is a poor unexplained label for an individual condition that has already been satisfied. [FHWA utility conflicts][fhwa-ucm], [LCI glossary][lci-glossary], [CII release planning][cii]

The naming correction should separate three questions:

1. What must happen: relocate a line, obtain a permit, execute an agreement, or establish that no interference exists?
2. What source records and review support that conclusion?
3. Can the particular construction activity proceed, considering its other requirements?

This separation is our product recommendation based on the sources below. It is not a new industry standard or a claim that Corridor already answers the third question completely.

## 1. The sources support the user's concern about Ready

The Lean Construction Institute describes constraints as things that obstruct planned activity. Its make-ready process removes those obstacles before assigning work. It separately addresses promises and completed work. **Ready to relocate poles and poles already relocated are different facts.** [LCI Last Planner System][lci-lps]

Construction Industry Institute material corroborates the distinction. Installation work package development and release planning identifies and manages constraints so crews receive work they can execute. That is preparation for execution, not a declaration that the same work has finished. Corridor is not a complete work-package release system and should not imply that it is. [CII release planning][cii]

Therefore, do not solve the current ambiguity by adding a generic Open → Ready → Complete sequence. A requirement, an outside party's promised delivery, a completed activity, and a document review are different subjects.

## 2. Utility conflict is a real industry term

TxDOT's utility process separates identifying interference, coordinating with the utility owner, making agreements, and performing adjustments. Its guidance requires documenting conflicts and considering avoidance or accommodation, rather than assuming that moving every facility is the only solution. [TxDOT utility process, §§6.4.1–6.4.4][txdot-process]

**Recommended use:** call a physical utility interference a **Utility conflict**, including a clearly identified potential conflict where uncertainty remains. Do not call an unrelated permit or access agreement a utility conflict. A broader **Constraints** view could include distinct types such as utility conflicts, permits, and access requirements. **External constraints** would be Corridor's qualifying product label, not a nationally mandated class.

In scheduling terminology, a dependency is a relationship between activities. It is not normally the name of a permit, interfering pole, or document waiting for review. This makes Dependency defensible as an internal abstraction but less useful as the main customer label. [AACE 10S-90, Dependency][aace]

## 3. Completion, clearance, and certification cannot be merged

Austin District's guidance distinguishes **Utility No Longer in Conflict** from **Adjustment Completed**. The former covers facilities absent from the project limits or found not to interfere; the latter applies when required relocation has been completed. A utility ID covers an owner on a project and can contain several conflicts, so its status cannot be copied mechanically onto each Corridor record. These are district labels, not a universal national lifecycle. [Austin Utility ID Guideline, pp. 1–2, 5][utility-id]

The Austin Utility Status Report demonstrates that additional investigation or a design change can establish that facilities may remain in place. Thus, eliminating a conflict need not involve completed relocation. [Austin USR Guideline, pp. 9–10][usr]

TxDOT also treats physical adjustment verification separately from utility certification. The certification can cover arrangements to complete remaining work in coordination with construction. A certification is not automatically proof that every relocation is finished. [TxDOT utility process, §§6.4.4.1–6.4.5][txdot-process]

**Recommended use:** show the particular supported result: **Relocation complete**, **No conflict confirmed**, **Permit issued**, or **Agreement executed**. Show its location, scope, source, and review basis. Do not generate any of those results merely from an old Ready flag.

When the source only reports completion and the required review is outstanding, say so. **Reported complete — review pending** is proposed product copy, not a formal industry status.

## 4. Required outcome and required documentation are different

**Acceptance criteria** is an established term for the requirements a delivered product or service must meet under its contract or agreement. It describes what is acceptable, not merely which file must be uploaded. [AACE 10S-90, Acceptance Criteria][aace]

Lean practice uses **Conditions of Satisfaction** and **Hand-off Criteria** for agreed requirements on a delivered result. Those are also broader than a requirement for documentary support. [LCI glossary][lci-glossary]

Construction quality practice separately maintains inspection and work records and checks work against contract requirements. For example, USACE distinguishes its quality requirements, construction records, quality reviews, and acceptance inspections. That supports separating the required result from documentation and review; it does not mean that a Corridor review is a USACE inspection. [USACE ER 1180-1-6, §§6–7][usace]

For the current `evidence_required` concept, **Required documentation** or **Evidence required** is a clearer label than Readiness Requirement. This is our product recommendation; the research did not establish an exact standard term for Corridor's particular human sufficiency judgment.

If a record also needs to specify the physical or contractual result, use a separate explanation such as **Required outcome**. Do not silently broaden the existing documentation field into a full set of construction acceptance criteria. Defining or adding that distinction in storage would require design work.

Formal acceptance also carries authority that internal source review does not. Under the federal construction inspection clause, inspection does not itself imply acceptance; acceptance follows its specified process. Use **Reviewed by** for Corridor's internal review unless it is recording a separately authorized acceptance. [FAR 52.246-12, paragraphs (c) and (i)][far]

## 5. Promises, estimates, and completed work need different dates

The Last Planner practitioner guide hosted by LCI UK explicitly separates the date a constraint needs to be removed from the date someone promises to remove it. It also distinguishes declaring work complete from releasing the following work, which can depend on quality assurance. This is established practitioner guidance, not a universal contract clause. [LPC, Last Planner System: Just the Essentials, pp. 4–6][lpc]

Austin's USR separately records estimates and actual completion. Its completion date does not overwrite the earlier anticipated date. [Austin USR Guideline, p. 13][usr]

For Corridor, preserve these distinctions:

| Product label | What it means |
| --- | --- |
| **Required by** | When the project needs the specific condition satisfied. Retain the schedule event and revision behind it. |
| **Promised for** | When the outside organization said it would deliver. A month remains a month. |
| **Estimated completion** | A prediction, if supplied; do not turn it into a promise. |
| **Actual completion** | When the named work was completed, if the record supports that date. |
| **Reviewed on** | When someone reviewed the supporting records. This is not the completion date. |

These display labels are recommendations. Need Date and Commitment already have useful industry counterparts; there is no reason to erase the distinction between the project's need and the outside party's promise.

## 6. Recommended mapping

This table proposes customer language. It does not authorize a global code rename or establish new facts from old records.

| Current Corridor term | Recommended customer language | Important limit |
| --- | --- | --- |
| Dependency | **Utility conflict** for utility interference; **Constraint** for the broader planning collection | Distinguish potential interference from confirmed interference. Do not force unknown-scope promises into a conflict record. |
| Ledger | **Utility conflicts** or **Constraint log**, depending on the view | The underlying Project Record remains broader than either collection. |
| Readiness Requirement | **Required documentation** | Explain the result the documents must establish. Acceptance criteria is broader. |
| Ready | **The specific supported outcome**, plus its review basis | A generic fallback such as **Requirement met — reviewed** is our own wording. It must name the requirement. |
| Evidence | **Supporting documents**, with the exact source excerpt available | Keep the exact quotation/page identity internally. Document presence alone establishes nothing about completion. |
| Ready judgment | **Review of required documentation** | This must still record whether the documents meet the stated requirement, not just that somebody opened them. |
| External Party | **Utility owner** in a utility view; the actual organization elsewhere | Do not use bare Owner for both the utility company and the project team's assignee. |
| Internal Owner | **Assigned to** or the actual role, such as **Utility coordinator** | A person managing follow-up is not necessarily responsible for performing the utility work. |
| Commitment | **Commitment**, with explanatory copy **What they promised** | A promise is different from the project team's next action. |
| Committed Date | **Promised for** | Preserve source precision and distinguish forecasts. |
| Need Date | **Required by** | Preserve the exact schedule basis. |
| Commitment Closure | **Completion reported**, with the exact promised deliverable named | A closure statement does not automatically satisfy all requirements for a linked conflict. |
| Next Action / Action Due Date | **Next action / Action due** | Keep the internal action separate from the outside party's promise. |
| Milestone | **Milestone** or the event's actual name | This is established scheduling language. |
| Milestone Registration | **Milestone revision** or **Schedule revision**, as appropriate to the screen | Do not call every imported revision an approved baseline. |
| Milestone Impact | **Effect on [named milestone]** | The decision must retain its schedule revision; a later promise alone does not prove project delay. |
| Resolution Strategy | **Resolution strategy** or **Planned response** | An agreed response is not proof its work has been performed. |
| Criticality | **The actual work type**, such as relocation, removal, or abandonment | This flag is not a critical-path calculation. |

The terminology families are supported by the [LCI glossary][lci-glossary], [AACE terminology][aace], [LPC constraint guidance][lpc], and [TxDOT utility practice][txdot-process]. The specific UI wording and mapping are our recommendations.

### Another important naming collision: Criticality

AACE defines a critical activity by its position on the critical path. Corridor instead uses Criticality for particular kinds of utility work. Displaying those records simply as critical can suggest schedule analysis that the flag does not perform. Name the work type; reserve critical-path claims for an actual schedule calculation. [AACE 10S-90, Critical Activity and Critical Path][aace]

## 7. A plain example of the proposed product language

The following is hypothetical, not a change to a real project:

> **Utility conflict:** Pole 42 is in the drainage excavation area.
>
> **Planned response:** Relocate Pole 42 outside that area.
>
> **Required by:** September 1, for the named drainage milestone and schedule revision.
>
> **Utility owner's commitment:** Relocate Pole 42 by August 25.
>
> **Required documentation:** A completion record that identifies Pole 42 and establishes the required relocation.
>
> **Source reports:** Relocation completed August 24.
>
> **Project review:** The named person judged the current record sufficient for this requirement.
>
> **Result:** Relocation complete for Pole 42, supported by the reviewed record.

If the required record is missing, say **Completion not yet confirmed — required documentation missing**. Do not assert that the pole is still in place unless a source supports that claim.

If a design change means the pole can stay, show **No conflict confirmed**, with the design and review basis. Do not invent a relocation completion.

Neither outcome means that every other condition for excavation has been checked. This is the distinction between one constraint being addressed and the whole activity being ready to start.

## 8. What this changes in the implementation plan

1. Agree on the customer language before implementing the affected screens. At the research baseline, the glossary prohibited several words that the relevant industry actually uses. ADR-0047 and the updated glossary now replace those blanket exclusions; they were not industry rules.
2. Keep the accepted authority boundaries: a source claim, quote check, human sufficiency decision, internal action, and formal release remain separate.
3. Improve labels and explanations without reviving the retired mutable Dependency status enum. Current outcomes can remain derived from supported facts and attributable decisions.
4. Treat any new distinction between an outcome, its documentation requirements, and its review as an explicit design question. It is not a safe mechanical rename of `is_ready`.
5. Preserve each commitment and its scope. Completing design, obtaining a permit, and finishing relocation must not hide one another behind a single date or completion flag.
6. Test the resulting wording with a utility coordinator using actual records. These sources establish credible terminology, not that Corridor's exact screen design has passed practitioner validation.

No recommendation here grants Corridor authority to certify utility clearance, accept construction on behalf of an agency, or authorize field work.

## Sources and limits

The core synthesis used the following primary sources, accessed on August 27, 2026. A current agency manual and an industry body's own glossary are primary evidence of their terminology. Neither establishes universal usage across every contractor or jurisdiction.

| Source | Edition or location used | What it establishes |
| --- | --- | --- |
| [FHWA SHRP2 utility conflicts][fhwa-ucm] | Official R15B program page; Solution section | Utility Conflict Matrix is an established transportation tool. The page is program history, not a current count of adopting agencies. |
| [TxDOT Project Development Process Manual][txdot-process] | Online §§6.4.1–6.4.5 | Conflict, adjustment, owner/coordinator, verification, certification distinctions. |
| [Austin Utility IDs in TxConnect Guideline][utility-id] | September 10, 2024; pp. 1–2, 5 | No-conflict and adjustment-completed outcomes differ; utility ID scope. |
| [Austin Utility Status Report Guideline][usr] | September 24, 2025; pp. 9–10, 13 | Avoided conflict can need no relocation; estimated and completed dates differ. |
| [LCI glossary][lci-glossary] | Named entries | Constraint, constraint log, make-ready, promise, and conditions of satisfaction. |
| [LCI Last Planner System][lci-lps] | Five Conversations and Eight Key Elements sections | Preparation, promises, and completed work are different planning questions. |
| [CII IWP Development & Release Planning][cii] | June 5, 2023; public abstract | Constraint management prepares work for crew execution. No claim depends on gated presentation content. |
| [AACE Recommended Practice 10S-90][aace] | Online revision February 18, 2026; named entries | Dependency, acceptance criteria, and critical-path terminology. |
| [LPC Last Planner System: Just the Essentials][lpc] | Copyright 2005/2011; pp. 4–6; hosted by LCI UK | Need versus promise dates; completion declaration versus release of following work. This is practitioner-authored guidance. |
| [USACE ER 1180-1-6][usace] | February 6, 2025; effective March 6, 2025; §§6–7 | Requirements, documentation, QA, and acceptance are distinct. The dates inside the document govern, not its filename. |
| [FAR 52.246-12][far] | Current published clause; paragraphs (c), (i) | Inspection does not imply contractual acceptance. Included as a terminology boundary, not legal advice about a particular contract. |

Additional search results and agent notes were used to cross-check the source selection. The recommendations above do not depend on vendor marketing, unavailable CII member downloads, or the eCFR page that returned an access challenge. Older guidance is labeled by its date and used for stable terminology, not as proof of current project-specific obligations.

Research does not replace a project's contracts, agency procedures, professional judgment, or field verification. No claim of production readiness, successful new acceptance testing, or practitioner validation is made.

[txdot-process]: https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process.html
[fhwa-ucm]: https://www.fhwa.dot.gov/goshrp2/Solutions/PlanningEnvironment/R15B/Identifying_and_Managing_Utility_Conflicts
[utility-id]: https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-id-guideline.pdf
[usr]: https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-guideline.pdf
[lci-glossary]: https://leanconstruction.org/glossary/
[lci-lps]: https://leanconstruction.org/lean-topics/last-planner-system/
[cii]: https://www.construction-institute.org/iwp-development-release-planning
[aace]: https://library.aacei.org/terminology/welcome.shtml
[lpc]: https://leanconstruction.org.uk/wp-content/uploads/2018/10/Last-Planner-System-Essentials-LPC.pdf
[usace]: https://www.publications.usace.army.mil/Portals/76/Publications/EngineerRegulations/ER%201180-1-6_Construction%20Quality%20Management_2025%2003%2020%20-%20Final.pdf
[far]: https://www.acquisition.gov/far/52.246-12
