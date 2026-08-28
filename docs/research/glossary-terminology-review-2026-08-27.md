# Complete Corridor glossary terminology review

> **Adoption update:** These recommendations were subsequently accepted under [ADR-0048](../adr/0048-complete-glossary-adoption-preserves-record-and-source-identity.md). See the [adoption and coverage receipt](glossary-terminology-implementation-2026-08-27.md) for current definitions, implementation surfaces, and compatibility boundaries. The research text below retains its original research-time status and findings.

Research date: August 27, 2026. **Research and recommendations only; no terminology is adopted by this report.**

## Coverage and scope

**Every current entry is covered: 72/72 terms — 54 Project Record and 18 Corridor Operations.** Seventy-one terms received a fresh primary-source review. Milestone is included as PR09 from the completed research, as requested; it was not omitted or needlessly researched again.

The inventory is the set of bold term headings in both glossaries at commit f69f502b8dcc1b42385ab319274b7868d247f350. Definitions and existing avoid-lists were considered. Source snippets alone were not accepted as a basis: the researchers read the relevant primary passages, with editions and sections recorded below. Some older sources establish stable usage, not current project-specific obligations.

Current meaning reproduces the glossary, not a claim that every runtime path already enforces it. Proposed meaning describes a naming recommendation. Existing implementation defects and the earlier audit work remain separate.

This report cites 90 distinct primary-source base URLs, including the carried-forward Milestone sources and proposed additions. A source can be an agency standard/manual, an industry-body definition, a technical specification, or a provider documenting its own product. Those categories are not interchangeable evidence of universal construction usage.

The supporting standalone [Milestone research](milestone-terminology-2026-08-27.md) remains available. Its recommendation is integrated here: retain the technical event concept and use **Key dates** for the customer view, with event name and scheduled date kept separate.

### How to read the recommendations

- **Keep / explain:** the existing name is defensible; improve its explanation or show the actual object.
- **Clarify:** the name fits, but its definition, scope, or field label needs precision.
- **Rename / clearer wording:** a better plain label is proposed. This does not claim a new universal standard.
- **Internal name / clearer UI:** preserve a useful technical name or identity; customers see the actual question or result.

Each original term has its current definition, industry fit, recommendation, proposed meaning, reasoning, alternatives, primary-source basis, and research scope. A finding of no exact counterpart means none was found in the stated sources; it is not proof that no organization anywhere uses the phrase.

## Main conclusions

1. **Keep real construction language where it fits.** Constraint, Utility Conflict, Utility Owner, Stationing, Commitment, and Milestone have support. Do not replace correct terms solely to make every label different.
2. **Remove misleading authority signals.** Source discrepancies, internal record decisions, and generated alerts are not automatically contractual disputes, adjudication, or engineering exceptions. The individual entries distinguish the actual process from familiar words with stronger meanings.
3. **Separate meaning from surface wording.** A named record concept, a button such as Assigned To, and a technical execution name do not need the same style. Use actual parties, events, and actions in the interface.
4. **Several definitions need correction, not just synonyms.** A Utility Inventory is not defined by forbidden resolution columns; a Utility Conflict Matrix can include potential or no-conflict rows. Supporting documentation still in use can refer to a replaced document. Those are semantic issues to design carefully before implementation.
5. **Preserve identity and authority.** A commitment history still belongs to one stable commitment. An approved report is not a delivered report. A source check is not a human sufficiency judgment. A failed processing attempt is not a successful abstention.

These conclusions are synthesized recommendations, not statements that an agency endorsed Corridor. The source basis and qualifications are attached to every term below.

## Complete decision index

| ID | Current term | Recommendation type | Preferred term or presentation |
| --- | --- | --- | --- |
| PR01 | [Constraint](#pr01-constraint) | Clarify | Constraint |
| PR02 | [Utility Conflict](#pr02-utility-conflict) | Clarify | Utility Conflict |
| PR03 | [External Party](#pr03-external-party) | Rename / clearer wording | External Organization |
| PR04 | [Utility Owner](#pr04-utility-owner) | Keep / explain | Utility Owner |
| PR05 | [Project Record](#pr05-project-record) | Clarify | Project Record (coordination scope) |
| PR06 | [Ledger](#pr06-ledger) | Internal name / clearer UI | Constraint records; retain Ledger as an internal identifier |
| PR07 | [Constraint Log](#pr07-constraint-log) | Keep / explain | Constraint Log |
| PR08 | [Stationing](#pr08-stationing) | Clarify | Stationing; Station / Station Range for location fields |
| PR09 | [Milestone](#pr09-milestone) | Clarify; research reused | Milestone |
| PR10 | [Milestone Revision](#pr10-milestone-revision) | Rename / clearer wording | Key date version |
| PR11 | [Effect on Milestone](#pr11-effect-on-milestone) | Rename / clearer wording | Effect on key dates |
| PR12 | [Resolution Strategy](#pr12-resolution-strategy) | Clarify | Utility Conflict Resolution Method |
| PR13 | [Required By](#pr13-required-by) | Keep / explain | Required By |
| PR14 | [External Party Statement](#pr14-external-party-statement) | Keep / explain | External Party Statement |
| PR15 | [Commitment](#pr15-commitment) | Keep / explain | Commitment |
| PR16 | [Promised For](#pr16-promised-for) | Keep / explain | Promised For |
| PR17 | [Committed Date Change](#pr17-committed-date-change) | Rename / clearer wording | Change to promised timing |
| PR18 | [Completion Reported](#pr18-completion-reported) | Keep / explain | Completion Reported |
| PR19 | [Commitment Scope](#pr19-commitment-scope) | Rename / clearer wording | Applies To |
| PR20 | [Commitment Lineage](#pr20-commitment-lineage) | Keep / explain | Commitment history |
| PR21 | [Coordination Subject](#pr21-coordination-subject) | Rename / clearer wording | Coordination Subject (internal) |
| PR22 | [Assigned To](#pr22-assigned-to) | Keep / explain | Assigned To |
| PR23 | [Next Action](#pr23-next-action) | Keep / explain | Next Action |
| PR24 | [Action Due Date](#pr24-action-due-date) | Keep / explain | Action Due Date |
| PR25 | [Work Decision](#pr25-work-decision) | Rename / clearer wording | Coordination decision |
| PR26 | [Coordination Plan](#pr26-coordination-plan) | Rename / clearer wording | Follow-up plan |
| PR27 | [Assertion](#pr27-assertion) | Internal name / clearer UI | Assertion internally; Source field value for customers |
| PR28 | [Dispute](#pr28-dispute) | Rename / clearer wording | Source discrepancy |
| PR29 | [Settlement](#pr29-settlement) | Rename / clearer wording | Discrepancy resolution |
| PR30 | [Supporting Documentation](#pr30-supporting-documentation) | Keep / explain | Supporting Documentation |
| PR31 | [Operative Support](#pr31-operative-support) | Rename / clearer wording | Supporting documentation in use |
| PR32 | [Derivation](#pr32-derivation) | Keep / explain | Calculated result |
| PR33 | [Verbal](#pr33-verbal) | Rename / clearer wording | Recorded verbal statement |
| PR34 | [Document of Record](#pr34-document-of-record) | Rename / clearer wording | Preferred source file |
| PR35 | [Supersession](#pr35-supersession) | Keep / explain | Supersession |
| PR36 | [Numbering Scheme](#pr36-numbering-scheme) | Rename / clearer wording | Row identification rule |
| PR37 | [Utility Conflict Matrix](#pr37-utility-conflict-matrix) | Clarify | Utility Conflict Matrix |
| PR38 | [Utility Inventory](#pr38-utility-inventory) | Clarify | Utility Inventory |
| PR39 | [Retired Row](#pr39-retired-row) | Clarify | Retired Matrix Row |
| PR40 | [Required Documentation](#pr40-required-documentation) | Keep / explain | Required Documentation |
| PR41 | [Documentation Review](#pr41-documentation-review) | Keep / explain | Documentation Review |
| PR42 | [Relocation Complete](#pr42-relocation-complete) | Keep / explain | Relocation Complete |
| PR43 | [Permit Issued](#pr43-permit-issued) | Keep / explain | Permit Issued |
| PR44 | [Agreement Executed](#pr44-agreement-executed) | Rename / clearer wording | Agreement Executed |
| PR45 | [No Conflict Confirmed](#pr45-no-conflict-confirmed) | Rename / clearer wording | No Utility Conflict |
| PR46 | [Not Relevant](#pr46-not-relevant) | Rename / clearer wording | Do Not Add |
| PR47 | [Dismissal](#pr47-dismissal) | Rename / clearer wording | Remove from Active Log |
| PR48 | [Attention Reason](#pr48-attention-reason) | Internal name / clearer UI | Attention Reason internally; Why this needs attention for customers |
| PR49 | [Work Item](#pr49-work-item) | Internal name / clearer UI | Work Item internally; Coordination item for customers |
| PR50 | [Exception](#pr50-exception) | Rename / clearer wording | Constraint Alert |
| PR51 | [Evaluation](#pr51-evaluation) | Keep / explain | Evaluation (internal); Constraint Check (product) |
| PR52 | [Report](#pr52-report) | Keep / explain | Coordination Report |
| PR53 | [Approved Export](#pr53-approved-export) | Rename / clearer wording | Report Approved for Release |
| PR54 | [Briefing](#pr54-briefing) | Rename / clearer wording | Coordination Summary |
| OP01 | [Document](#op01-document) | Keep / explain | Document |
| OP02 | [Extraction Run](#op02-extraction-run) | Keep / explain | Extraction Run |
| OP03 | [Active Run](#op03-active-run) | Rename / clearer wording | Current Production Run |
| OP04 | [Candidate](#op04-candidate) | Rename / clearer wording | Extracted Proposal |
| OP05 | [Admission](#op05-admission) | Rename / clearer wording | Record Inclusion |
| OP06 | [Adjudication](#op06-adjudication) | Rename / clearer wording | Human Record Decision |
| OP07 | [Abstention](#op07-abstention) | Keep / explain | Abstention |
| OP08 | [Unplaced Statement](#op08-unplaced-statement) | Rename / clearer wording | Statement Needing Clarification |
| OP09 | [Revision Comparison](#op09-revision-comparison) | Keep / explain | Revision Comparison |
| OP10 | [Supersession Review](#op10-supersession-review) | Rename / clearer wording | Document Revision Review |
| OP11 | [Reconfirmation](#op11-reconfirmation) | Rename / clearer wording | Human Support Update |
| OP12 | [Automatic Carry-Forward](#op12-automatic-carry-forward) | Rename / clearer wording | Automatic Support Update |
| OP13 | [Carry-Forward Policy](#op13-carry-forward-policy) | Rename / clearer wording | Automatic Support Update Rules |
| OP14 | [Carry-Forward Run](#op14-carry-forward-run) | Rename / clearer wording | Support Update Run Record |
| OP15 | [Revision Processing](#op15-revision-processing) | Keep / explain | Document Revision Processing |
| OP16 | [Extraction Measurement](#op16-extraction-measurement) | Keep / explain | Extraction Measurement |
| OP17 | [Cohort Receipt](#op17-cohort-receipt) | Rename / clearer wording | Rehearsal Input Manifest |
| OP18 | [Lane](#op18-lane) | Rename / clearer wording | Processing Scope |

## Project Record: every term

### PR01 Constraint

**Current meaning:** An external condition that must be addressed for a specified construction activity to start, progress, or finish as planned.

**Industry fit:** Established construction-planning term, especially in the Last Planner System; Corridor uses a narrower external-condition subset.

**Recommendation:** **Constraint** (Clarify). Customer wording: **Construction constraint; name the particular permit, access condition, or utility conflict**.

**Proposed meaning:** An external condition that can prevent specified construction work from starting, progressing, or finishing as planned.

**Why:** Keep the accepted term. LCI uses it for impediments to planned work, including information, approvals, resources, and prerequisite work. Corridor's external-only boundary is a product scope choice, not the industry definition. A missing excavation permit is a constraint; obtaining it is an action. Add a short description of the affected construction work to explain the label. Do not infer that a recorded constraint is presently blocking all construction.

**Alternatives and limits:** **Prerequisite:** Useful for a specific required condition, but does not express every continuing impediment. **Dependency:** Usually expresses a relationship between activities; it is not the condition itself. **Blocker:** Plain language, but implies an immediate obstruction that may not yet exist.

**Primary-source basis:**
- Constraints impede planned activities; dependence describes relationships between tasks. [Lean Construction Institute glossary — Constraint; Dependence][r001].
- Constraints include directives, resources, and prerequisite work; keep their connection to the affected task. [Lean Project Consulting, Last Planner System Essentials — Identifying Constraints, page 4][r002].

**Research scope:** LCI definitions and first-party Last Planner practice; not claimed as a universal synonym for every scheduling use of constraint.

### PR02 Utility Conflict

**Current meaning:** Actual or potential interference between utility facilities and proposed construction.

**Industry fit:** Established highway utility-coordination term, corroborated by federal and state guidance.

**Recommendation:** **Utility Conflict** (Clarify). Customer wording: **Utility conflict**.

**Proposed meaning:** Actual or potential incompatibility between a utility facility and proposed construction, including required clearances and access.

**Why:** Keep the term and explain the incompatibility. VDOT includes direct interference, inadequate clearance, unavailable maintenance access, and construction that threatens a facility. Thus, a pipe and drain need not physically intersect to have a conflict. FHWA compares utilities with proposed highway plans and investigates uncertain conflicts. 'Potential' must remain distinct from confirmed. A conflict can be avoided through highway design changes; the label does not establish that utility relocation is necessary or that anyone has disagreed.

**Alternatives and limits:** **Clash:** Often suggests geometric collision and can omit clearance, access, and other conflicts. **Utility relocation:** Names one possible response, not the underlying problem. **Utility issue:** Too broad to identify interference with the construction design.

**Primary-source basis:**
- Conflicts include physical interference, clearance deficiencies, maintenance access problems, and threatened utility facilities. [VDOT Utility Manual of Instructions — 7.3.2 Conflict Determination, printed pages 51-52][r003].
- Compare existing utilities with proposed plans; distinguish resolvable conflicts from uncertain cases requiring investigation. [FHWA, SUE Then and Now — Typical SUE practice: conflict matrix and further investigations][r004].

**Research scope:** FHWA utility engineering guidance, VDOT conflict determination, and current TxDOT identification guidance.

### PR03 External Party

**Current meaning:** An organization outside the project team that is responsible for a Constraint or makes an attributable statement recorded by Corridor.

**Industry fit:** Product umbrella; external stakeholder and third party are established nearby terms, not exact equivalents.

**Recommendation:** **External Organization** (Rename / clearer wording). Customer wording: **Organization, or its known role: Utility owner, Permitting agency, Railroad**.

**Proposed meaning:** An organization outside the project team that is responsible for a tracked condition or is the source of a recorded statement.

**Why:** External Organization is proposed plain product wording, not a claimed industry standard. FHWA stakeholders include people and public groups as well as organizations. FTA's third-party terminology has a particular sponsor/agreement boundary. Neither term by itself establishes responsibility or authorship. Show 'Organization: water district' and identify the actual speaker separately. Preserve the current organizational boundary and do not turn a mentioned company into the author of a promise.

**Alternatives and limits:** **External stakeholder:** Established but broader; affected residents are stakeholders without being responsible organizations. **Third party:** Accepted in transportation agreements, but its contractual reference point must be stated. **Utility company:** Excludes permitting agencies, railroads, and some public utility owners.

**Primary-source basis:**
- External stakeholders are people, groups, or organizations outside the defined system that interact with it. [FHWA Guidelines for Virtual Transportation Management Center Development — 3.2.2.1 Stakeholder Identification][r005].
- Third-party agreements have a project-sponsor scope that generally excludes its primary consultants and contractors. [FTA Oversight Procedure 39 — 6.2.1 Definitions, page 6][r006].

**Research scope:** FHWA/TxDOT external-party and stakeholder guidance, FTA third-party coordination, and agency utility roles; no exact standard for Corridor's organization-and-attribution umbrella found.

### PR04 Utility Owner

**Current meaning:** The External Party that owns the utility facilities involved in a project.

**Industry fit:** Established and explicitly defined in highway utility rules.

**Recommendation:** **Utility Owner** (Keep / explain). Customer wording: **Utility owner**.

**Proposed meaning:** The organization that owns the specified utility facility.

**Why:** This is already the clearest term for the intended role. Iowa explicitly defines the utility owner by ownership of a facility; TxDOT uses utility owners throughout identification and coordination. A municipal water district qualifies even though it is not a private company. Keep the facility relationship visible. Ownership is not the same as being assigned the project's next action, being the project's client, or speaking for the organization. Do not automatically broaden this field to an operator or the owner of every attachment on a shared pole.

**Alternatives and limits:** **Utility company:** Can obscure municipal, cooperative, or other public ownership. **Owner/operator:** Use only when the source actually distinguishes or expressly combines these roles. **Responsible party:** Responsibility can belong to a contractor or coordinator who does not own the facility.

**Primary-source basis:**
- A utility owner owns a utility facility; ownership is distinct from a designated representative. [Iowa Administrative Code 761, chapter 115: Utility Accommodation — 115.2, Utility owner and Utility facility, page 4][r007].
- Coordination includes facility owners and rural water authorities; owners supply information about their facilities. [TxDOT Project Development Process Manual — 6.2.2 Utility Identification][r008].

**Research scope:** Iowa utility definitions and TxDOT utility-identification practice corroborate direct fit; contractual definitions may vary by project.

### PR05 Project Record

**Current meaning:** The authoritative set of Constraints, accepted External Party Statements, Supporting Documentation, Verbals, Work Decisions, Milestone Revisions, and publication receipts for one project.

**Industry fit:** Project records is established construction language; Corridor's authoritative coordination aggregate is product-specific.

**Recommendation:** **Project Record (coordination scope)** (Clarify). Customer wording: **Project records / Coordination records**.

**Proposed meaning:** The coordination facts, supporting documents, decisions, schedule references, and publication history recorded for one project in Corridor.

**Why:** Caltrans uses construction project records for the whole collection of construction files. Corridor contains a narrower set, so the familiar term needs a scope statement. Keep Project Record as the context name, but explain what it contains clearly. A reviewed utility letter and its decision belong here; this does not mean Corridor holds every inspection, payment, contract change, or agency record. Internal authority over recorded coordination facts does not confer contractual authority over the entire project.

**Alternatives and limits:** **Project file:** Familiar, but can sound like a file folder and still imply the whole contract record. **Coordination records:** Useful plain scope qualifier, not asserted as a standardized record class. **As-built records:** Only one specialized subset; cannot cover promises, decisions, and schedules.

**Primary-source basis:**
- Construction project records encompass all material in the construction files, not only a coordination log. [Caltrans Construction Manual: Project Records and Reports — 5-104A General][r009].
- Project-record conclusions require an identified basis; records and verbal reporting must not be overstated. [FHWA Construction Program Management and Inspection Guide — Appendix F, Project Records][r010].

**Research scope:** Caltrans and FHWA construction recordkeeping; no standard identified for Corridor's exact aggregate or its internal authority boundary.

### PR06 Ledger

**Current meaning:** The working set of Constraints within the Project Record.

**Industry fit:** Internal technical name; no exact construction counterpart for this working-set abstraction found.

**Recommendation:** **Constraint records; retain Ledger as an internal identifier** (Internal name / clearer UI). Customer wording: **Constraints or Utility conflicts, according to the records shown**.

**Proposed meaning:** The accepted Constraint records within Corridor's larger Project Record.

**Why:** Customers already know these as constraints. TxDOT's project-management catalog uses ledger for invoices, while utility guidance uses a conflict matrix or list. Neither establishes Ledger as the accepted name for this working set. 'Constraint records' is proposed explanatory wording; it does not rename the existing reader/writer boundary. The Constraint Log is a view of these records, and the full Project Record also holds material that does not belong to this set.

**Alternatives and limits:** **Constraint Log:** Appropriate for the user view, not an automatic replacement for the storage abstraction. **Audit trail:** Describes change history, not the current working set. **Register:** Familiar but adds no clarity beyond naming the records themselves.

**Primary-source basis:**
- The agency distinguishes its invoice ledger from project coordination logs and registers. [TxDOT project and portfolio management publications — Tool catalog: Invoice Ledger; action, issue, and change logs][r011].
- The utility-conflict tool is a matrix with spreadsheet and database forms, not a ledger-defined domain abstraction. [FHWA SHRP2: Identifying and Managing Utility Conflicts — Solution][r012].

**Research scope:** TxDOT project-management and utility material plus FHWA conflict-management guidance; no exact industry term for Corridor's internal set found.

### PR07 Constraint Log

**Current meaning:** A view of the Constraints being coordinated, the people assigned to their next actions, and the relevant dates.

**Industry fit:** Established Last Planner construction-management artifact.

**Recommendation:** **Constraint Log** (Keep / explain). Customer wording: **Constraint log; Utility conflicts for a utility-only view**.

**Proposed meaning:** A list of conditions affecting planned work, the people handling them, the required dates, and the commitments to resolve them.

**Why:** LCI names the responsible individual and agreed date; Lean Project Consulting separately records need dates and promised dates. That distinction supports Corridor's separate date fields. Show the affected work: a drainage permit needed before trench excavation belongs on this log. Do not claim that a list of constraints is the construction schedule or that all items use one universal completion workflow. The general log may contain permits and access conditions; a Utility Conflict Matrix has a narrower utility focus.

**Alternatives and limits:** **Issue Log:** Broader: an issue need not constrain specified construction work. **Action Item Log:** Tracks actions; a constraint is the condition those actions address. **Constraint Register:** Understandable alternative, but no stronger fit than the established log label.

**Primary-source basis:**
- The log associates constraints with individuals promising to resolve them and agreed dates. [Lean Construction Institute glossary — Constraint Log][r001].
- The project constraint log includes need dates, promised dates, request status, and who needs resolution. [Lean Project Consulting, Last Planner System Essentials — Identifying Constraints and Managing Constraints, pages 4-5][r002].

**Research scope:** LCI glossary and original practitioner guide corroborate usage within the Last Planner approach, not every project-control method.

### PR08 Stationing

**Current meaning:** A position along the project alignment, such as `245+00`.

**Industry fit:** Established highway survey and plan-reading terminology.

**Recommendation:** **Stationing; Station / Station Range for location fields** (Clarify). Customer wording: **Location: alignment name and station / station range**.

**Proposed meaning:** Stationing numbers positions along a named alignment. A station identifies a position; a station range identifies a length of that alignment.

**Why:** Distinguish the reference system from a position within it. Caltrans specifies stationing and alignment annotations; TxDOT defines the station measurement. Show 'Mainline, Sta. 245+00' rather than only '245+00.' Do not turn a station number into coordinates or assume the same number on another alignment denotes the same place. State the drawing's units and preserve any station-equation context. A readable Location heading can explain the field without discarding source notation.

**Alternatives and limits:** **Chainage:** A regional alternative; use when the source drawings use it, not as a wholesale Texas-project rename. **Milepost:** A different route-reference system; not an interchangeable substitute. **Location:** Good heading, but too broad to replace the underlying station and alignment information.

**Primary-source basis:**
- Station numbers use a defined measurement convention and are annotated along the alignment. [Caltrans Plans Preparation Manual, chapter 2, section 1 — Stationing, printed page 2-8][r013].
- A station uses horizontal distance and the definition distinguishes customary and metric measurement. [TxDOT Glossary, S — Station][r014].
- A station equation equates two station numbers at a centerline point. [TxDOT Glossary, E — Equation][r015].
- Chainage is distance along the road or railway centreline used as a location reference. [Transport Scotland, A75 Springholm and Crocketford Improvements, Stage 1 glossary — Chainage][r016].

**Research scope:** TxDOT glossary, Caltrans plan conventions, and FHWA plan-reading guidance; no reason to invent a replacement technical term.

### PR09 Milestone

**Current meaning:** A named event in the project schedule with no duration, such as the start of a construction phase.

**Industry fit:** Established scheduling term; completed research reused

**Recommendation:** **Milestone** (Clarify; research reused). Customer wording: **Key dates**.

**Proposed meaning:** A named schedule event with no duration, such as construction starting or a phase finishing; the event and its scheduled date are separate.

**Why:** The concept is not invented. Corridor stores a named event and a date rather than a work activity with duration, so the industry term fits. TxDOT also uses Key Dates (Milestones), which is clearer for a customer view. Show the event name, date, and source revision; a list of linked constraints does not prove the event happened.

**Alternatives and limits:** **Deadline:** Not every scheduled event is a contractual or mandatory limit. **Activity:** The work consumes time; its start or completion event does not. **Constraints by key date:** Useful proposed heading for the current report grouping, not a standard name for milestone achievement.

**Primary-source basis:**
- Distinguishes point events from work activities. [AACE 10S-90: Cost Engineering Terminology — Milestone; Key Events; Activity][r017].
- Explicitly uses Key Dates (Milestones). [TxDOT Schedule Guide for Transportation Development Projects — Create Initial Schedule p7; Appendix A pp11-12][r018].
- Separates event markers, detailed work, and summaries. [GAO Schedule Assessment Guide GAO-16-89G — Milestone, Detail, and Summary Activities pp13-14][r019].
- Highway schedules distinguish milestones from tasks. [FHWA CFL Guidelines for Developing CPM Schedules — 4.3 p16; 5.4 p26][r020].
- Milestones have no duration; planned and actual dates differ. [NJDOT Scheduling Manual for Design Projects — 3.0 Definitions; 5.4 Schedule Updates][r021].
- Recognizes separate start and finish event types. [Oracle Primavera Cloud Activity Types — Start Milestone and Finish Milestone][r022].

**Research scope:** Reused the completed milestone-terminology-2026-08-27.md research as requested; no new Milestone research was required for this pass.

### PR10 Milestone Revision

**Current meaning:** An immutable record of one version of a Milestone with its exact source row and source digest.

**Industry fit:** Internal source-version concept; not an exact construction standard.

**Recommendation:** **Key date version** (Rename / clearer wording). Customer wording: **Key date version; From schedule [revision]**.

**Proposed meaning:** One preserved version of a named key date, including its source row and source fingerprint. Earlier versions remain available.

**Why:** A captured row is not the whole schedule revision and may be the first version. Example: Drainage start, September 1, from schedule revision B. Registration does not prove approval or authorize moving links to another version.

**Alternatives and limits:** **Schedule revision / Baseline:** The former concerns the schedule; the latter requires an established performance reference.

**Primary-source basis:**
- Revising a schedule differs from updating its recorded progress. [AACE 10S-90: Cost Engineering Terminology — Schedule Revision (October 2018); Schedule Update (November 2020)][r017].
- Distinguishes revisions from progress updates. [GSA 552.236-15: Schedules for Construction Contracts — Paragraphs (h)-(i)][r023].

**Research scope:** AACE, GSA scheduling clauses, TxDOT scheduling guidance, and exact-name searches; no standard equivalent for an immutable source-row snapshot found.

### PR11 Effect on Milestone

**Current meaning:** A Work Decision that states whether a Committed Date Change affects named Milestones, does not affect them, or has an effect that is not yet known.

**Industry fit:** Schedule impact is established; Corridor's three-answer decision is narrower.

**Recommendation:** **Effect on key dates** (Rename / clearer wording). Customer wording: **Does this changed promise affect a key date?**.

**Proposed meaning:** A named project person's answer about whether changed promised timing affects identified key dates: affects, does not affect, or not yet known.

**Why:** Names the actual question without claiming a calculated delay. Example: the coordinator says a later pole-move promise affects drainage start. Preserve the decision and assessed schedule context. The separate question of when schedule changes require reassessment remains an open policy choice under ADR-0045.

**Alternatives and limits:** **Time impact analysis / Project delay:** Implies analysis or a demonstrated consequence that this decision does not provide.

**Primary-source basis:**
- Potential impacts, schedule analysis, and accepted date adjustments are separate steps. [TxDOT: Time Impact Analysis Recordkeeper Job Aid — Pages 2-3: notice, analysis, adjustment][r024].

**Research scope:** TxDOT schedule-impact guidance and AACE schedule terms; no standardized equivalent for this exact three-answer human decision.

### PR12 Resolution Strategy

**Current meaning:** The recorded way a Utility Conflict will be addressed, such as relocation, removal, protection in place, or a highway design change, supported by its source or attributable human conclusion.

**Industry fit:** Established utility-conflict concept; agency wording varies between strategies, alternatives, and methods of adjustment.

**Recommendation:** **Utility Conflict Resolution Method** (Clarify). Customer wording: **Resolution method, followed by the actual action**.

**Proposed meaning:** The recorded approach to addressing a utility conflict, such as relocating the facility, protecting it, or changing the highway design.

**Why:** 'Resolution method' is clearer product wording for the established concept, not a mandated national field name. FHWA describes design alternatives to relocation. VDOT separately discusses the proposed method of adjustment. 'Relocate water main' tells users more than an abstract severity label. The method does not prove that work occurred, establish schedule criticality, authorize field work, or resolve a disagreement between reviewers. Preserve the source or named decision behind it.

**Alternatives and limits:** **Recommended action or resolution:** Appropriate where the source is a recommendation; do not present it as already selected. **Adjustment method:** Useful for utility work, but can omit highway redesign that avoids utility work. **Disposition:** Abstract and easily confused with review or record status.

**Primary-source basis:**
- Conflict responses include drainage, structural, slope, and other highway design changes that can avoid relocation. [FHWA Avoiding Utility Relocations — V.2 Design Strategies and Alternatives, especially V.2.2-V.2.5][r025].
- Discuss the method and tentative route or location for each proposed utility relocation. [VDOT Utility Manual of Instructions — 7.3.6 Proposed Method of Adjustment, printed page 53][r003].

**Research scope:** FHWA conflict/design guidance and VDOT adjustment procedures; the product label must not imply more approval than the source supplies.

### PR13 Required By

**Current meaning:** The date by which a Constraint's stated requirement must be met, derived from the Milestone Revision it serves.

**Industry fit:** Established need-date concept; clear product wording.

**Recommendation:** **Required By** (Keep / explain). Customer wording: **Required by [date]**.

**Proposed meaning:** The date by which the specified construction condition must be met, calculated from the exact key-date version that the constraint serves.

**Why:** It says when the project needs the result. It does not say when another party promised it. Show its basis beside it: Required by August 1; serves drainage start. A revised promise must not overwrite this date. A scheduler's date and a contractual deadline also need distinct attribution.

**Alternatives and limits:** **Need Date:** Established Last Planner wording, but no clearer than the current label. **Deadline / Due date:** Can obscure whether the date is contractual or belongs to an internal action.

**Primary-source basis:**
- Constraint logs separate need dates from promised dates. [Lean Project Consulting, Last Planner System Essentials — Page 4: Identifying Constraints][r002].

**Research scope:** Last Planner constraint management and TxDOT contract-time versus planned-finish guidance; current meaning fits the need-date concept.

### PR14 External Party Statement

**Current meaning:** An attributable Commitment, Committed Date Change, or report of completion preserved with its timing, source, and Commitment Scope.

**Industry fit:** Product category; correspondence and meeting minutes are related source forms, not exact equivalents.

**Recommendation:** **External Party Statement** (Keep / explain). Customer wording: **Statement from [party name]**.

**Proposed meaning:** A recorded promise, change to its timing, or report of completion, attributed to the outside party that made it.

**Why:** The heading states who spoke. Keep the source, original timing and applicable constraints visible. Example: Statement from Equistar: provide the title record in January. A project invitation mentioning Equistar is not Equistar's statement. Do not merge this category into project action items.

**Alternatives and limits:** **Correspondence:** Names a communication or document, not one statement; it also excludes an oral statement. **Notice:** Can imply formal notification requirements.

**Primary-source basis:**
- Utility communication records include correspondence, discussions, and follow-up actions. [TxDOT SH 288 Technical Provisions — 6.2.2.1-6.2.2.2, pp. 6-4 to 6-5][r026].

**Research scope:** TxDOT utility communications, NDDOT utility meetings, LCI promises, and exact-name search; no standard covering exactly these three statement types.

### PR15 Commitment

**Current meaning:** An attributable External Party Statement about what that party will deliver and when.

**Industry fit:** Established in Lean construction and Last Planner practice.

**Recommendation:** **Commitment** (Keep / explain). Customer wording: **[Party] committed to [deliverable]**.

**Proposed meaning:** A promise attributable to an outside party about what it will deliver and when, preserving the words and timing the source supports.

**Why:** The term fits. Example: the power company promises to move identified poles in September. Corridor records that promise; it does not certify the party has the resources to perform it. A forecast, request, or invitation alone is not a commitment. Corridor's month-level statements are broader than a weekly production plan's exact-day commitments.

**Alternatives and limits:** **Promise:** Useful plain explanation of Commitment. **Reliable Promise / Obligation:** The former adds assurances; the latter may imply a legal duty.

**Primary-source basis:**
- A promise names a performer, result, and future timing; reliability adds performer assurances. [Lean Construction Institute glossary — Promise; Reliable Promise][r001].

**Research scope:** LCI dictionary and Ballard-Tommelein benchmark; established method vocabulary, not a universal contractual category.

### PR16 Promised For

**Current meaning:** The timing an External Party stated it would deliver, preserved at the precision the statement supports.

**Industry fit:** Plain product label for the established promised-date concept.

**Recommendation:** **Promised For** (Keep / explain). Customer wording: **Promised for [source-supported timing]**.

**Proposed meaning:** When the outside party said it would deliver, shown with the same precision and qualifications as its statement.

**Why:** Pairs well with Required by. Promised for January 2027 must not become January 1. Display approximate wording honestly; do not label a forecast as a promise. The source establishes timing; separate rules govern past-due calculations.

**Alternatives and limits:** **Promised date:** Established, but a date picker can falsely demand an exact day. **Expected completion:** Can include estimates without any promise.

**Primary-source basis:**
- Records a promised date separately from project need. [Lean Project Consulting, Last Planner System Essentials — Page 4: Identifying Constraints][r002].
- Distinguishes day, month, and approximate representations. [Library of Congress: Extended Date/Time Format Specification — Level 0 Date; Level 1 Qualification][r027].

**Research scope:** Last Planner promise-date practice and authoritative date representation; no reason to replace this clear existing label.

### PR17 Committed Date Change

**Current meaning:** An External Party Statement replacing one attributable timing with another for the same Commitment, with direction retained when supported.

**Industry fit:** Renegotiated promises are established; this exact record type is product-specific.

**Recommendation:** **Change to promised timing** (Rename / clearer wording). Customer wording: **Promise changed: [previous timing] → [new timing]**.

**Proposed meaning:** A statement by the same outside party replacing its earlier delivery timing for the same commitment, with both versions retained.

**Why:** The proposed wording covers January, an exact day, and approximate timing. Example: January changes to February for the same title record. Record earlier or later only when supported. This is not a project schedule edit or acceptance of a contractual extension; internal follow-up remains separate.

**Alternatives and limits:** **Slip:** Assumes movement is later. **Revised commitment:** Could also change the deliverable; this concept concerns timing.

**Primary-source basis:**
- Parties notify the planner when a promise needs revocation or renegotiation. [Lean Project Consulting, Last Planner System Essentials — Page 5: Managing Constraints][r002].

**Research scope:** Last Planner managing-constraints practice, AACE change terminology, TxDOT schedule guidance; no exact standard record name found.

### PR18 Completion Reported

**Current meaning:** An attributable External Party Statement that one exact Commitment is complete, supported by verified source records or preserved as a Verbal.

**Industry fit:** Close fit to an established completion-reporting distinction in Last Planner.

**Recommendation:** **Completion Reported** (Keep / explain). Customer wording: **Reported complete by [party]**.

**Proposed meaning:** A supported statement from the outside party that one identified commitment is complete, preserved with its documentary or oral source.

**Why:** This is deliberately weaker than accepted work. Example: the utility reports the poles moved; a separate documentation review may still be needed. Verifying that the report exists is not physical inspection. Do not complete internal follow-up actions automatically, or close every promise by that company.

**Alternatives and limits:** **Declaration of completion:** Used in Last Planner, but less direct and potentially formal sounding. **Complete / Accepted:** Hides who made the claim or implies acceptance.

**Primary-source basis:**
- The performer reports completion; inspection and the customer's acceptance are separate. [Ballard and Tommelein: 2020 Last Planner System Benchmark — 8.2.9, printed p. 82][r028].

**Research scope:** Last Planner benchmark and TxDOT utility recordkeeping; keep the source-report boundary rather than use a generic closed status.

### PR19 Commitment Scope

**Current meaning:** The explicit set of Constraints to which an External Party Statement applies, or the statement that scope is not yet known; all-active records the set at the time of the decision.

**Industry fit:** Established scope terminology, but Corridor uses a narrower record relationship.

**Recommendation:** **Applies To** (Rename / clearer wording). Customer wording: **Applies to: [named constraints] / Not yet known**.

**Proposed meaning:** The explicitly recorded constraints covered by a statement, or a recorded finding that those links are not yet known.

**Why:** Show which constraints the statement concerns. Example: a pole-move promise applies to conflicts 12 and 14. An all-active selection must preserve its original membership, not expand later. Unknown links do not mean there is no promise.

**Alternatives and limits:** **Scope of work:** Describes work or deliverables, not merely links to existing records. **Affected constraints:** Could imply a schedule impact rather than simple applicability.

**Primary-source basis:**
- Scope concerns the activity's or project's work and delivered results. [AACE 10S-90: Cost Engineering Terminology — Scope (January 2003)][r017].
- Construction software separately identifies links between records. [Autodesk Build: Issues References and Attachments — About Issue References][r029].

**Research scope:** AACE scope, construction information frameworks, Autodesk references, and exact-name search; no standard equivalent for Corridor's snapshotted membership set.

### PR20 Commitment Lineage

**Current meaning:** One External Party Commitment across append-only factual corrections and statement versions.

**Industry fit:** Internal record-identity concept; no exact construction counterpart established.

**Recommendation:** **Commitment history** (Keep / explain). Customer wording: **History of this commitment**.

**Proposed meaning:** The preserved sequence of statements and factual corrections belonging to one identified commitment, with earlier records retained.

**Why:** Show one promise and how its record changed. Internally, Commitment Lineage preserves that identity across corrections; the history label must not weaken it. Two utility promises remain distinct. Scope links must not create duplicate promises or internal plans.

**Alternatives and limits:** **Revision history:** Acceptable heading if it identifies which commitment. **Contract amendment:** A factual record correction does not amend a contract.

**Primary-source basis:**
- Records actions and named users against an individual issue. [Autodesk BIM 360: Issues in Field Management — Issue Activity Log][r030].
- Utility agreement amendments have a separate formal process. [TxDOT SH 288 Technical Provisions — 6.1.3.2, p. 6-4][r026].

**Research scope:** Exact-name search, LCI commitments, TxDOT amendments, Autodesk record histories; no construction standard defines this correction-chain identity.

### PR21 Coordination Subject

**Current meaning:** The one Constraint or accepted Commitment Lineage that a Work Decision concerns.

**Industry fit:** Internal union of record types; not an established construction object.

**Recommendation:** **Coordination Subject (internal)** (Rename / clearer wording). Customer wording: **For: [constraint or commitment name]**.

**Proposed meaning:** The single constraint or accepted commitment identity to which an internal coordination decision belongs.

**Why:** Display the actual record and its name instead of teaching a new object. Example: For Equistar's January title-record commitment. This still works before affected constraints are known. One decision must not silently target several records, and linking the statement to constraints must not copy its plan.

**Alternatives and limits:** **Issue:** Suggests a problem and loses the distinction between constraints and accepted promises. **Work package:** Implies a defined execution package, not a decision's target.

**Primary-source basis:**
- References connect distinct records rather than making them the same item. [Autodesk Build: Issues References and Attachments — About Issue References][r029].

**Research scope:** Exact-name search, LCI dictionary, AACE work-package terminology, and construction-software references; no standard equivalent for this exact two-type target.

### PR22 Assigned To

**Current meaning:** The named project-team member accountable for the current Next Action on one Coordination Subject.

**Industry fit:** Common construction-software field; consistent with agency responsibility tracking.

**Recommendation:** **Assigned To** (Keep / explain). Customer wording: **Assigned to [project person]**.

**Proposed meaning:** The named project-team member responsible for the current internal follow-up action on one constraint or commitment.

**Why:** It is already clear. Example: Maria follows up with the utility; the utility still performs its promised relocation. Show the action beside the person so assignment cannot look like ownership of the facilities or authority to accept external work. Retain Corridor's named-person rule even where other tools also permit companies or roles.

**Alternatives and limits:** **Responsible person:** Reasonable but not clearer. **Owner / Utility coordinator:** Owner is overloaded; not every assignee holds that job title.

**Primary-source basis:**
- Uses Assigned to for the record's selected member, role, or company. [Autodesk Build: Create Issues — Create Issues, default fields][r031].
- Names the person responsible for follow-up. [TxDOT SH 288 Technical Provisions — 6.2.2.2, p. 6-5][r026].

**Research scope:** TxDOT utility-meeting requirements and Autodesk construction documentation corroborate the responsibility pattern.

### PR23 Next Action

**Current meaning:** The project-controlled step that must happen next for one Coordination Subject.

**Industry fit:** Plain field label aligned with established construction action-item practice.

**Recommendation:** **Next Action** (Keep / explain). Customer wording: **Next action: [specific project-controlled step]**.

**Proposed meaning:** The next step the project team will take for one named constraint or commitment.

**Why:** Example: call the utility to obtain its revised relocation timing. That is different from the utility's promise to relocate poles. A completed call completes this action, not the relocation. Keep the wording specific enough to know when the project step is done.

**Alternatives and limits:** **Action item:** Established name for a tracked task, often including assignment and date; the field here names its next step. **Task:** Could be mistaken for scheduled construction work.

**Primary-source basis:**
- Construction coordination meetings establish action items for next steps. [FHWA: Work Zone Impacts Assessment During Construction — 7.4, Step 1: Coordinate Pre-Construction Activities][r032].
- Tracks follow-up actions separately from discussed issues. [TxDOT SH 288 Technical Provisions — 6.2.2.2, p. 6-5][r026].

**Research scope:** FHWA construction coordination, TxDOT utility minutes, and LCI task/assignment definitions; existing label is suitable.

### PR24 Action Due Date

**Current meaning:** The date the project set for its own Next Action.

**Industry fit:** Established task-management date pattern; product qualifier prevents ambiguity.

**Recommendation:** **Action Due Date** (Keep / explain). Customer wording: **Action due [date]**.

**Proposed meaning:** The date the project team sets for completing its own current next action.

**Why:** This is the third distinct date: what the project needs, what the outside party promises, and when the coordinator must act. Example: follow up August 20 about work promised for August 30 and required September 1. Preserve a structured unknown-date reason where no due date exists.

**Alternatives and limits:** **Due date:** Fine within an action panel, ambiguous in a mixed report. **Target date:** Can be mistaken for a project schedule forecast.

**Primary-source basis:**
- Provides a due-date field for an assigned issue. [Autodesk Build: Create Issues — Create Issues, default fields][r031].
- Follow-up entries carry a target date for resolution. [TxDOT SH 288 Technical Provisions — 6.2.2.2, p. 6-5][r026].

**Research scope:** Agency utility meeting records and construction-software task dates; no need for a replacement term.

### PR25 Work Decision

**Current meaning:** An attributable project-team decision that changes project-controlled coordination state for one Coordination Subject.

**Industry fit:** Product-specific decision subtype; industry decision logs are broader.

**Recommendation:** **Coordination decision** (Rename / clearer wording). Customer wording: **Decision by [name]: [assignment, action, due date, or key-date effect]**.

**Proposed meaning:** A recorded project-team choice about its own coordination response for one identified constraint or commitment.

**Why:** Work Decision does not explain what was decided. The proposed name keeps these choices separate from source statements, documentation judgments, and contractual approvals. Example: Maria assigns a follow-up call for Friday. That decision cannot prove the utility moved a pole or close its promise.

**Alternatives and limits:** **Decision log:** Names a collection, not one decision. **Instruction / Approval:** Can imply authority over external work that this record does not grant.

**Primary-source basis:**
- Requires project files to retain a decision log; its context is design decisions. [Iowa DOT Design Manual 1C-8: Documenting Design Decisions — Opening requirement][r033].

**Research scope:** Iowa DOT decision documentation, TxDOT utility minutes, LCI planning, and exact-name searches; no standard matching Corridor's restricted decision type.

### PR26 Coordination Plan

**Current meaning:** The current assigned person, Next Action, and Action Due Date for one Coordination Subject, plus Effect on Milestone when the subject is a Committed Date Change.

**Industry fit:** Potential collision with established, broader utility coordination plans.

**Recommendation:** **Follow-up plan** (Rename / clearer wording). Customer wording: **Follow-up plan**.

**Proposed meaning:** The current project response for one constraint or commitment: assigned person, next action, action due date, and any required key-date effect decision.

**Why:** The label fits one record's response: Maria will call the utility Friday. Keep the plan on that record and preserve its separate decisions. This is not the utility's construction plan or promise.

**Alternatives and limits:** **Utility coordination plan:** Agency usage includes wider phasing and scheduling. **Action plan:** Possible, but may suggest a complete sequence beyond one next action.

**Primary-source basis:**
- Requires utility-company phasing and scheduling requirements. [NDDOT: Utility Coordination Special Provision Example — Page 2, B: Utility Coordination Plan][r034].
- Uses follow-up actions, responsible people, and target dates. [TxDOT SH 288 Technical Provisions — 6.2.2.2, p. 6-5][r026].

**Research scope:** NDDOT provisions, Alberta's coordination-plan deliverable, TxDOT minutes; no standard exact counterpart for Corridor's small response bundle.

### PR27 Assertion

**Current meaning:** One source Document's claim about one field of one Constraint.

**Industry fit:** Established technical term; no exact construction counterpart found.

**Recommendation:** **Assertion internally; Source field value for customers** (Internal name / clearer UI). Customer wording: **Source field value**.

**Proposed meaning:** The value one document states for one field, retained with its source reference and kept separate from the project's conclusion.

**Why:** Source field value distinguishes this record from an External Party Statement about a promise or completion. The current model stores a field name, asserted value, and supporting source. Keep Assertion as an internal identifier. The value can be normalized; the cited passage retains the source wording. A value in a matrix is not automatically the project's accepted conclusion or a verified physical fact.

**Alternatives and limits:** **Source statement:** Defensible, but can be confused with the separate External Party Statement category. **Fact / Claim:** Fact overstates truth; Claim can suggest a contractual demand.

**Primary-source basis:**
- A statement expresses a relationship between identified subjects and values; this is a technical data model, not a construction acceptance rule. [W3C, RDF 1.1 Concepts and Abstract Syntax — 1.2 Resources and Statements; 3.1 Triples][r035].
- Document omissions, errors, and discrepancies require correction or interpretation; the specification does not treat every discrepancy as an established contract claim. [TxDOT, Standard Specifications, Items 1–10 — Item 5, Article 4: Coordination of Plans, Specifications, and Special Provisions][r036].
- Implementation boundary checked in [src/corridor/models.py](../../src/corridor/models.py#L2900); this is local evidence of Corridor's meaning, not an industry standard.

**Research scope:** Checked TxDOT discrepancy language and W3C RDF/PROV statements. No exact highway name found for the per-field object; proposed wording is product-specific.

### PR28 Dispute

**Current meaning:** An unsettled disagreement among retained verified Assertions about one Constraint field.

**Industry fit:** Established construction word, but materially different in contract administration.

**Recommendation:** **Source discrepancy** (Rename / clearer wording). Customer wording: **Sources disagree**.

**Proposed meaning:** Two or more retained, verified source statements give incompatible answers for the same field, and no applicable human conclusion resolves them.

**Why:** TxDOT uses discrepancy for inconsistent information. Caltrans distinguishes discrepancy or confusion from a contract dispute following disagreement with a response. Corridor detects conflicting records, not necessarily disagreement between people or a claim for money or time. If two matrices disagree over relocation versus protection, show both statements and the affected field. Resolving the record does not resolve a contract dispute.

**Alternatives and limits:** **Document discrepancy:** Also suitable, but Source discrepancy aligns with the source-level comparison. **Utility conflict:** Already means interference between utility facilities and construction. **Nonconformance:** Would imply failure to meet a requirement, not merely conflicting records.

**Primary-source basis:**
- Document omissions, errors, and discrepancies require correction or interpretation; the specification does not treat every discrepancy as an established contract claim. [TxDOT, Standard Specifications, Items 1–10 — Item 5, Article 4: Coordination of Plans, Specifications, and Special Provisions][r036].
- A contract dispute concerns contractor–agency disagreement over the contract; claim settlement compromises contract requirements through authorized procedures. [Caltrans Construction Manual, Section 5-4 — 5-401 General; 5-408 Claim Settlement][r037].

**Research scope:** Read TxDOT Item 5 Article 4 and Caltrans 5-401/5-408. Discrepancy is corroborated construction usage; Source discrepancy is the proposed scoped label.

### PR29 Settlement

**Current meaning:** An attributable human conclusion about one disputed field that preserves every Assertion it considered.

**Industry fit:** Established contract term; misleading for this narrow record decision.

**Recommendation:** **Discrepancy resolution** (Rename / clearer wording). Customer wording: **Record conclusion**.

**Proposed meaning:** A named person's conclusion for one field with conflicting source statements, preserving the statements considered and the decision's scope.

**Why:** Caltrans claim settlement compromises contract requirements through authorized procedures. Corridor instead records the conclusion the project will use for one field. Discrepancy resolution describes this narrower act. A reviewer can conclude protect in place after considering two matrices without changing a utility contract. Preserve the sources, decision maker, and scope. A later conflicting statement can require a fresh decision.

**Alternatives and limits:** **Clarification:** Familiar, but may suggest an explanation issued by a designer rather than this internal decision. **RFI response:** Use only if an actual request-for-information process exists. **Agreement:** Would incorrectly imply that affected parties reached agreement.

**Primary-source basis:**
- A contract dispute concerns contractor–agency disagreement over the contract; claim settlement compromises contract requirements through authorized procedures. [Caltrans Construction Manual, Section 5-4 — 5-401 General; 5-408 Claim Settlement][r037].
- Document omissions, errors, and discrepancies require correction or interpretation; the specification does not treat every discrepancy as an established contract claim. [TxDOT, Standard Specifications, Items 1–10 — Item 5, Article 4: Coordination of Plans, Specifications, and Special Provisions][r036].

**Research scope:** Checked Caltrans 5-403/5-408 and TxDOT correction/interpretation language. No exact standardized noun found for the retained-source, per-field decision.

### PR30 Supporting Documentation

**Current meaning:** Verified source passages from registered Documents that support a recorded fact, with exact document and page references.

**Industry fit:** Established construction language; Corridor's passage-level meaning is narrower.

**Recommendation:** **Supporting Documentation** (Keep / explain). Customer wording: **Supporting documents; Cited passage**.

**Proposed meaning:** The verified parts of identified source documents used to support a recorded statement, with an exact document and page or row reference.

**Why:** FHWA uses supporting project documents for inspection records and other project paperwork. Corridor adds the precise link to what supports a statement. Keep the familiar heading and show Cited passage for the individual evidence unit. A completion letter can support one relocation statement without proving all project conditions. Checking the source wording and judging that a requirement is met remain separate.

**Alternatives and limits:** **Evidence:** Valid general language, but does not itself explain the document, locator, or limited claim. **Proof:** Overstates what a verified passage establishes. **Attachments:** Shows storage, not why a record supports the statement.

**Primary-source basis:**
- Supporting project documents include inspection records and project paperwork. Reviewing these records informs, but is distinct from, the oversight agency's final acceptance. [FHWA, Federal-aid Essentials: Project Closeout — Companion guide, pages 1–2][r038].
- Derivation connects a generated entity to the entity used to produce it; quotation is the copying of source content, not certification of its truth. [W3C, PROV-DM: The PROV Data Model — 5.2.1 Derivation; 5.2.3 Quotation][r039].

**Research scope:** Read FHWA Project Closeout and W3C quotation provenance. The industry phrase fits; passage-level verification is Corridor's specific meaning.

### PR31 Operative Support

**Current meaning:** The current Supporting Documentation used to publish a Constraint's facts or support an established Documentation Review.

**Industry fit:** No exact construction equivalent found; a technical selection of supporting sources.

**Recommendation:** **Supporting documentation in use** (Rename / clearer wording). Customer wording: **Used for this value / Used for this review**.

**Proposed meaning:** The supporting passages designated for a current published value or documentation judgment, with the purpose identified.

**Why:** Document control distinguishes information available for use from replaced information, but does not define Corridor's field-level selection as Operative Support. A matrix can support the Utility Owner while a completion letter supports the documentation judgment. Show each purpose. In use does not mean current revision: a selected passage can still refer to a superseded document and require attention. Preserve both support roles.

**Alternatives and limits:** **Current supporting documentation:** Could imply the cited revision is current when it is merely still being used. **Latest document:** Recency does not designate which passage supports a value. **Source of truth:** Hides conflicting records and overstates authority.

**Primary-source basis:**
- The site distinguishes documents replaced by successors from canceled documents without replacements, and controls which revisions are available for use. [Four Rivers Nuclear Partnership / DOE Paducah, Document Control Process — CP3-OP-0025, Appendix A, document-status definitions; 4.2 Document Control][r040].

**Research scope:** Checked DOE document control, NARA record copies, and ADR-0016/0017. No exact industry term found for this role-scoped selection.

### PR32 Derivation

**Current meaning:** A value computed from exact Project Record inputs under a stated ruleset and Evaluation date.

**Industry fit:** Established provenance term; technical for a customer-facing computed value.

**Recommendation:** **Calculated result** (Keep / explain). Customer wording: **How this was calculated**.

**Proposed meaning:** A result produced from identified project inputs by a stated calculation or rule, for the date being assessed.

**Why:** W3C derivation describes an output produced from an earlier entity, a broader concept than arithmetic. The technical term fits; Calculated result explains the output more directly. For example, days late should expose its source dates, the date being assessed, and the rule. It is not a utility owner's statement. Preserve exact inputs and reproducibility, including nonnumeric results of deterministic rules.

**Alternatives and limits:** **Calculation:** Good for the operation; Calculated result distinguishes its output. **Estimate:** Would wrongly suggest every result is approximate. **Inference:** Could imply an uncertain judgment rather than an explicit deterministic rule.

**Primary-source basis:**
- Derivation connects a generated entity to the entity used to produce it; quotation is the copying of source content, not certification of its truth. [W3C, PROV-DM: The PROV Data Model — 5.2.1 Derivation; 5.2.3 Quotation][r039].

**Research scope:** Read W3C PROV-DM 5.2 and WSDOT generated-record guidance. No special highway noun is needed for this technical class.

### PR33 Verbal

**Current meaning:** An External Party Statement heard by a named project person on a stated date and preserved as an append-only source.

**Industry fit:** Conversation records are established practice; Verbal alone is product shorthand.

**Recommendation:** **Recorded verbal statement** (Rename / clearer wording). Customer wording: **Reported by phone or in conversation**.

**Proposed meaning:** What an External Party said, recorded by the named person who heard it, with the conversation date, timing as stated, and explicit scope.

**Why:** Caltrans instructs staff to identify speakers and record what was said; WSDOT keeps conversation records in project diaries. Recorded verbal statement describes Corridor's narrower item without implying a document or audio recording exists. If a representative says work will finish in September, preserve that precision and who heard it. Recording the statement does not make it written utility confirmation or a formal instruction.

**Alternatives and limits:** **Record of conversation:** Established practice, but broader than one statement captured from that conversation. **Verbal commitment:** Too narrow: the item may report a changed date or completion. **Field observation:** Would imply direct observation of work instead of something heard.

**Primary-source basis:**
- Records should identify the speaker and preserve what was said, rather than substitute a general conclusion about a conversation. [Caltrans Construction Manual, Section 3-5 — 3-521D Documentation Guidelines for Disputes][r041].
- Project diaries can preserve conversations, meetings, contractor agreements, and matters affecting completion. [WSDOT Construction Manual, Chapter 10: Documentation — 10-3.2B Construction Project Diaries][r042].

**Research scope:** Read Caltrans 3-521D and WSDOT 10-3.2B. The practice is established; the exact proposed source-type name is product wording.

### PR34 Document of Record

**Current meaning:** The rendition that Supporting Documentation cites when one Document is published in several formats; a structured original outranks its printed rendering.

**Industry fit:** Records authority terms exist, but do not establish Corridor's rendition preference.

**Recommendation:** **Preferred source file** (Rename / clearer wording). Customer wording: **File used for citations**.

**Proposed meaning:** The registered file used for citations when one document has several formats; Corridor prefers the structured original to its printed rendering.

**Why:** NARA bases record status on business use, not original-versus-copy status. WSDOT as-built plans describe physical construction changes. Corridor instead chooses the rendition most useful for accurate reading. Document of Record suggests authority beyond this preference. If a matrix is issued as XLSX and PDF, identify the cited file and do not treat the two formats as independent corroboration.

**Alternatives and limits:** **Native file:** Describes the originating format, not which file Corridor selects for citations. **Record copy:** Means a designated retained record, not automatic priority of a spreadsheet. **Record drawing / As-built:** Concerns the constructed works, not alternate formats of any document.

**Primary-source basis:**
- Record status depends on business use, not whether a copy is original. A recordkeeping copy is not necessarily the original file. [NARA, Records Basics — When Is A Copy A Record; Types of copies][r043].
- As-built plans record changes to the physical works; they are not simply the preferred file format for citing a document. [WSDOT Construction Manual, Chapter 10: Documentation — 10-3.6 As-Built Plans and Shop Drawings][r044].

**Research scope:** Checked NARA copies, WSDOT as-built definitions, and ADR-0005. No universal structured-file precedence rule found; proposed wording identifies Corridor's preference.

### PR35 Supersession

**Current meaning:** The registered relation that one Document replaced another as the current revision.

**Industry fit:** Established document-control concept; replacement verbs are clearer in the interface.

**Recommendation:** **Supersession** (Keep / explain). Customer wording: **Replaces / Replaced by**.

**Proposed meaning:** The recorded relationship that identifies the document revision replacing an earlier revision for current use.

**Why:** DOE distinguishes superseded documents with successors from canceled documents without them. UK government data guidance records explicit replacement links. The concept fits. Revision C replaces Revision B is easier than a Supersession event. The relationship preserves history, does not prove every detail changed, and does not establish a change to construction. Continue to require the registered relationship; filenames and upload dates are insufficient.

**Alternatives and limits:** **Obsolete:** Does not identify the successor and can include documents with no replacement. **Deleted:** Would imply loss of history. **Newer version:** Age alone does not establish that it replaces the cited document.

**Primary-source basis:**
- The site distinguishes documents replaced by successors from canceled documents without replacements, and controls which revisions are available for use. [Four Rivers Nuclear Partnership / DOE Paducah, Document Control Process — CP3-OP-0025, Appendix A, document-status definitions; 4.2 Document Control][r040].
- Explicit replacement links identify revised data and preserve its history; the replacement has its own identifier. The guidance concerns datasets, not contractual approval. [UK Government Digital Service, Record information about data sets you share with others — Using supersededBy; Using supersedes][r045].

**Research scope:** Read DOE CP3-OP-0025 Appendix A and GDS supersededBy guidance. Established term; explicit registration remains Corridor policy.

### PR36 Numbering Scheme

**Current meaning:** The registered rule that gives matrix rows project-unique or per-party identity.

**Industry fit:** ID rules are established; matrix numbering scope is not universal.

**Recommendation:** **Row identification rule** (Rename / clearer wording). Customer wording: **How matrix rows are identified**.

**Proposed meaning:** The recorded rule that says whether a matrix row is identified by its conflict number alone or by its owner and conflict number together.

**Why:** TxDOT defines Utility Conflict ID as project-unique. FDOT's SR 789 matrix restarts Conflict # within each owner section. The evidence rules out assuming one numbering standard. Corridor must state the applicable rule, which Row identification rule explains more directly. AT&T row 1 and Comcast row 1 can be different records. The printed number and the full row identity must stay distinct.

**Alternatives and limits:** **Conflict number:** Names a source field, not the rule establishing uniqueness. **Unique ID:** Too vague about the owner or project scope. **Document numbering convention:** Would suggest file identifiers rather than rows inside a matrix.

**Primary-source basis:**
- Utility Conflict ID is unique within the transportation project. Operational status includes a separate out-of-service value for a utility facility. [TxDOT, Utility Conflict Analysis Template — Data Dictionary, rows 98 and 158; Drop-Down Lists][r046].
- Separate owner sections restart the conflict numbers, demonstrating that a row's number alone is not always project-unique. [FDOT, SR 789 at Broadway Utility Conflict Matrix — Pages 1–2, Utility Agency Owner and Conflict #][r047].

**Research scope:** Verified TxDOT Data Dictionary and FDOT pages 1–2; searched agency numbering terminology. Row identification rule is proposed product language.

### PR37 Utility Conflict Matrix

**Current meaning:** A Document that lists Utility Conflicts and how they will be addressed.

**Industry fit:** Established federal/state utility-coordination tool; Utility Conflict List is an accepted equivalent in Iowa.

**Recommendation:** **Utility Conflict Matrix** (Clarify). Customer wording: **Utility conflict matrix; Utility conflict list where simpler presentation is useful**.

**Proposed meaning:** A table that records utility facilities, their relationship to proposed construction, and the investigation or action needed to address conflicts.

**Why:** Keep the title; correct the overly conclusive definition. Iowa expressly includes no-conflict and potential-conflict entries; FHWA uses the matrix throughout investigation and design. A matrix need not already state a final resolution for every row. It can be a database, not only a document. Corridor may register a particular issued matrix as a Document, as its ingestion boundary. An empty resolution cell is uncertainty to preserve, not a reason to pretend the source is not a matrix.

**Alternatives and limits:** **Utility Conflict List:** A documented agency synonym; preserve the actual source title. **Utility Inventory:** Identifies facilities; does not itself communicate the conflict-analysis purpose. **Clash report:** Usually narrower than the matrix's investigation, ownership, action, and resolution information.

**Primary-source basis:**
- Conflict list/matrix tracks facilities relative to road design, including no-conflict and potential-conflict records. [Iowa Administrative Code 761, chapter 115: Utility Accommodation — 115.2, Utility conflict list, page 4][r007].
- The UCM supports conflict identification and solutions; implementations include spreadsheets and databases. [FHWA SHRP2: Identifying and Managing Utility Conflicts — Solution and implementation examples][r012].

**Research scope:** FHWA SHRP2, Iowa definitions, and TxDOT conflict-analysis practice; the document-only and every-row-resolved limits are not industry requirements.

### PR38 Utility Inventory

**Current meaning:** A Document that lists utility features and may identify interference but does not state Resolution Strategies.

**Industry fit:** Established utility-identification concept; the current prohibition on resolution information is a Corridor classification rule.

**Recommendation:** **Utility Inventory** (Clarify). Customer wording: **Utility inventory; help text: facilities identified within the project limits**.

**Proposed meaning:** A record of utility facilities and their owners within a defined project area, with available location and descriptive information.

**Why:** Keep the term; remove the ban on resolution information. VDOT inventories existing utilities and owners; Maryland investigates facilities and collects information through inventory forms and questionnaires. They define purpose, not prohibited columns. A facility list can include conflict notes without becoming proof that a resolution was selected. Document any exclusive extraction categories internally, or use 'Facility inventory without a recorded resolution' as product wording. Do not misstate industry usage to fit a parser.

**Alternatives and limits:** **Utility facility inventory:** A useful explicit form when Utility might be mistaken for a company. **Utility owner list:** Lists organizations; it does not identify each physical facility. **Utility Conflict Matrix:** A conflict-analysis tool; an inventory alone does not establish that analysis occurred.

**Primary-source basis:**
- Identify utilities within project limits and their owners, and enter the information in the management system. [VDOT Utility Manual of Instructions — 4.3 Utility Inventory, printed page 33][r003].
- Inventory field facilities and owners, then collect and retain further utility information through agency forms. [MDOT SHA Utility Procedures — Section 1, procedures 1-2A through 1-2C, pages 5-6][r048].

**Research scope:** VDOT and MDOT SHA inventory procedures, with FHWA facility-inventory research as corroboration; no industry prohibition on added resolution fields found.

### PR39 Retired Row

**Current meaning:** A Utility Conflict Matrix row whose only content is an identifier and a retirement phrase, rather than a Constraint.

**Industry fit:** Source-specific bookkeeping; no standard construction class found.

**Recommendation:** **Retired Matrix Row** (Clarify). Customer wording: **Row marked not used, or the actual retirement wording**.

**Proposed meaning:** A source matrix row containing only an explicit retirement or unused-row marker, optionally with its identifier, and no substantive conflict information.

**Why:** Adding Matrix states what is retired. Keep the exact content guard: a populated row saying Not Used still requires review, and an empty slot without an explicit retirement marker is not automatically excluded. WSDOT's examples show why the phrase alone is insufficient. Retired describes the source entry, not an abandoned utility or known history of prior use.

**Alternatives and limits:** **Unused matrix row:** Can incorrectly include empty slots awaiting information. **Abandoned facility / Deleted row:** The former describes a physical asset; the latter implies erased source history.

**Primary-source basis:**
- Some numbered slots contain only the note Not Used; populated row 210 also has that note. The words alone do not identify an empty slot. [WSDOT Contract 9424, Appendix U2: Existing Utility Listing — PDF pages 5 and 9][r049].
- Utility Conflict ID is unique within the transportation project. Operational status includes a separate out-of-service value for a utility facility. [TxDOT, Utility Conflict Analysis Template — Data Dictionary, rows 98 and 158; Drop-Down Lists][r046].

**Research scope:** Searched TxDOT, FDOT, and FHWA row terminology; verified WSDOT pages 5 and 9. No universal term found; retain Corridor's explicit rule.

### PR40 Required Documentation

**Current meaning:** The stated source records and content needed to demonstrate that one specified construction condition has been met.

**Industry fit:** Established construction phrase; the required records depend on purpose.

**Recommendation:** **Required Documentation** (Keep / explain). Customer wording: **Documents required for this condition**.

**Proposed meaning:** The stated records and information needed to demonstrate that one specified construction condition has been met.

**Why:** FHWA checks that required documentation is present during closeout. This supports the phrase, not a universal checklist for every Constraint. A relocation condition might need a completion record naming the facilities; another might need an executed agreement. Show both the condition and the record required. Missing documentation means the condition is not demonstrated in Corridor, not necessarily that work is unfinished.

**Alternatives and limits:** **Acceptance criteria:** Broader requirements for the work itself; not a direct replacement for the required records. **Required submittals:** Appropriate only for records formally required through a submittal process. **Readiness requirement:** Does not make clear whether it concerns work starting, finishing, or documentation.

**Primary-source basis:**
- Supporting project documents include inspection records and project paperwork. Reviewing these records informs, but is distinct from, the oversight agency's final acceptance. [FHWA, Federal-aid Essentials: Project Closeout — Companion guide, pages 1–2][r038].
- Submittals are contract-required information or samples, with distinct action and informational categories; they are not a synonym for every supporting record. [Caltrans Construction Manual, Section 3-5 — 3-511 Submittals][r041].

**Research scope:** Read FHWA closeout and Caltrans submittal definitions. Keep the established phrase with its specific condition and scope.

### PR41 Documentation Review

**Current meaning:** A named person's judgment of whether exact current Supporting Documentation meets the Required Documentation for one specified condition.

**Industry fit:** Established construction review language; Corridor's judgment is narrower.

**Recommendation:** **Documentation Review** (Keep / explain). Customer wording: **Do these documents meet this requirement?**.

**Proposed meaning:** A named person's judgment of whether the exact current supporting passages satisfy the stated documentation requirement for one condition.

**Why:** WSDOT documentation reviews check records and recordkeeping. Corridor's narrower act is understandable when the screen states its question. Finding a quotation is only a source check; deciding a letter covers these poles is a human judgment. Preserve the requirement and sources reviewed. A positive review neither authorizes unrelated work nor establishes contract acceptance; changed requirements require checking whether the earlier judgment still applies.

**Alternatives and limits:** **Approval:** Can imply contract authority beyond this documentation judgment. **Verified:** Ambiguous between matching source wording and judging sufficiency. **Document completeness check:** Could check presence alone without judging content against the requirement.

**Primary-source basis:**
- Documentation reviews check records and recordkeeping procedures; this agency process is broader than Corridor's judgment against one stated requirement. [WSDOT Construction Manual, Chapter 10: Documentation — 10-5.1 Region Project Documentation Reviews][r050].
- Supporting project documents include inspection records and project paperwork. Reviewing these records informs, but is distinct from, the oversight agency's final acceptance. [FHWA, Federal-aid Essentials: Project Closeout — Companion guide, pages 1–2][r038].

**Research scope:** Read WSDOT 10-5.1 and FHWA's separate acceptance step. Keep the familiar phrase with the explicit Corridor boundary.

### PR42 Relocation Complete

**Current meaning:** The supported conclusion that the specified relocation work is complete for the named utility facilities and location.

**Industry fit:** Established construction outcome; not a universal acceptance status.

**Recommendation:** **Relocation Complete** (Keep / explain). Customer wording: **Relocation complete**.

**Proposed meaning:** Current reviewed documents support that the specified utility relocation work at the named location is finished.

**Why:** The wording names the actual result. TxDOT separates adjustment verification from project utility certification. Corridor must preserve that distinction and show the source, reviewer, and scope. Missing paperwork means completion is unconfirmed, not necessarily unfinished.

**Alternatives and limits:** **Adjustment complete:** Useful agency wording for a broader adjustment; do not silently broaden relocation. **Accepted / certified / cleared:** Could imply formal authority or wider project clearance that this finding does not establish.

**Primary-source basis:**
- Adjustment verification concerns site completion; utility certification can include arrangements for unfinished work. [TxDOT Project Development Process Manual — 6.4.4.1; 6.4.5][r051].
- Completed work, inspection, and formal contract acceptance remain distinct steps. [Caltrans Construction Manual, Section 3-5 — 3-523A–C][r041].

**Research scope:** TxDOT adjustment guidance; Caltrans construction inspections; federal quality-control completion terminology.

### PR43 Permit Issued

**Current meaning:** The identified permit has been issued by its issuing authority for its stated scope and conditions.

**Industry fit:** Established agency language; scope belongs to the particular permit.

**Recommendation:** **Permit Issued** (Keep / explain). Customer wording: **Named permit issued**.

**Proposed meaning:** The responsible authority issued this identified permit for the activities and conditions stated in it.

**Why:** Issuance is an administrative event, not completion of work. A reviewed copy supports that event. It does not show that every permit is obtained or every condition for starting work is satisfied. Issue date and current validity must remain distinct.

**Alternatives and limits:** **Permit approved:** Use only when the agency actually records approval; approval and issuance may be separate steps. **Work authorized:** Too broad without checking the permit's conditions and the other applicable authorizations.

**Primary-source basis:**
- An encroachment permit authorizes specified right-of-way work for the permittee or agent; it conveys no property right. [Caltrans Source Inspection Guidelines — Appendix 1: Encroachment Permit][r052].
- Permits cover specific kinds of right-of-way work. [ALDOT Utility Accommodation — Permitting on ROW][r053].

**Research scope:** Caltrans permit definitions and environmental permit requirements; TxDOT installation guidance.

### PR44 Agreement Executed

**Current meaning:** The identified agreement has been signed by its required parties for its stated scope.

**Industry fit:** Established contract-administration language.

**Recommendation:** **Agreement Executed** (Rename / clearer wording). Customer wording: **Agreement signed by all required parties**.

**Proposed meaning:** The identified agreement has met the execution formalities required for that agreement, with the relevant version and supporting record identified.

**Why:** TxDOT's cited procedure uses all required signatures for full execution. Other agreement regimes can require additional formalities, so preserve the source's particular meaning. Do not infer an effective date, authority to start, or completed work merely from signatures. Use Signed by all parties when that is all the source establishes.

**Alternatives and limits:** **Agreement complete:** Ambiguous between finished drafting, signatures, and completed performance. **Agreement effective:** May depend on additional conditions; cannot be inferred solely from signatures.

**Primary-source basis:**
- Full execution requires all parties' signatures; written work authorization can still be required before work or costs begin. [TxDOT Negotiated Contracts Policy Manual — Contract Execution][r054].
- Required contract signatures depend on entity type and signatory authority. [Federal Acquisition Regulation — 4.102][r055].
- The procedure defines execution by completed agreement formalities, not just an unqualified signature count. [FTA Oversight Procedure 39 — 6.2.1 Definitions, p6][r006].

**Research scope:** TxDOT contract execution and utility-agreement requirements; Caltrans cooperative-agreement definitions.

### PR45 No Conflict Confirmed

**Current meaning:** The supported conclusion that the identified utility facilities do not interfere with the specified construction.

**Industry fit:** No conflict and not in conflict are established utility-coordination terms; confirmed is Corridor's review qualifier.

**Recommendation:** **No Utility Conflict** (Rename / clearer wording). Customer wording: **No utility conflict**.

**Proposed meaning:** Reviewed documentation supports that the identified utility facilities do not interfere with the specified construction design.

**Why:** Keep facilities, location, design revision, and source visible. A no-conflict result can arise without relocation. It must not mean all project utilities are clear.

**Alternatives and limits:** **No relocation required:** Suitable only when supported; absence of relocation alone does not establish absence of interference. **Cleared:** Does not explain whether work finished, design changed, or a formal certification occurred.

**Primary-source basis:**
- Design changes or further utility investigation can establish that facilities may remain without conflict. [TxDOT Austin Utility Status Report Guideline — Process 5, pp. 9–10][r056].
- Lists a letter retaining utility facilities with no project conflict. [ALDOT Utility Accommodation — SAHD No. 5 listing][r053].

**Research scope:** TxDOT status guidance; ALDOT SAHD No. 5 no-conflict retention form listing.

### PR46 Not Relevant

**Current meaning:** The reversible human disposition of a Candidate that does not belong in the Project Record.

**Industry fit:** Plain software disposition; no exact construction-standard equivalent found.

**Recommendation:** **Do Not Add** (Rename / clearer wording). Customer wording: **Do not add to project record**.

**Proposed meaning:** A person decides that this proposed entry does not belong in the project record; the decision remains reversible.

**Why:** The phrase tells the user what the button does. Preserve the proposed entry, reason, actor, and history. An irrelevant extraction is not a rejected contractor submittal, defective work, or a no-conflict conclusion. The current label describes relevance but hides the practical effect.

**Alternatives and limits:** **Reject:** Formal submittal rejection can require correction and resubmission; that is a different workflow. **Delete / not applicable:** Delete suggests erasure; not applicable can be mistaken for an engineering scope conclusion.

**Primary-source basis:**
- A disapproved contractual submittal requires resubmission, rather than exclusion of an extracted suggestion. [USACE RMS Submittal Codes — Code E][r057].

**Research scope:** TxDOT utility dispositions; USACE submittal decisions; FHWA record-management guidance. No exact counterpart identified.

### PR47 Dismissal

**Current meaning:** The reversible human act that removes an admitted junk Constraint from current work while preserving its history.

**Industry fit:** No exact construction-standard equivalent found for this reversible record cleanup.

**Recommendation:** **Remove from Active Log** (Rename / clearer wording). Customer wording: **Remove incorrect entry from active log**.

**Proposed meaning:** A person removes an incorrectly admitted constraint from current work while preserving its record and the ability to restore it.

**Why:** This says which list changes and avoids claiming that construction finished. Restrict the action to erroneous entries. A real constraint resolved through design or completed work needs a supported outcome, not dismissal. Retained history is essential to distinguish correction of the record from changing the project.

**Alternatives and limits:** **Close out:** Construction closeout concerns completion, records, and financial obligations; removing junk does none of those. **Cancel constraint / delete:** Could imply scope cancellation or destruction of the historical record.

**Primary-source basis:**
- Record management and contract closeout are distinct; closeout addresses completed work and settled obligations. [FHWA Project Management Plan Guidance — Project Documentation & Reporting; Project Closeout][r058].

**Research scope:** FHWA record/closeout guidance; TxDOT conflict outcomes; USACE review dispositions. No exact counterpart identified.

### PR48 Attention Reason

**Current meaning:** A current derived reason that one Work Item needs human attention.

**Industry fit:** Product-specific signal; no exact industry-standard term found.

**Recommendation:** **Attention Reason internally; Why this needs attention for customers** (Internal name / clearer UI). Customer wording: **Why this needs attention**.

**Proposed meaning:** A current condition explains why one coordination question or follow-up action needs a person's attention.

**Why:** There is no exact industry counterpart for this derived grouping. The plain question explains the effect without creating another status. It includes missing assignments, unknown scope, and actions becoming due, not just document review. Preserve several reasons under one item and do not turn them into an urgency score.

**Alternatives and limits:** **Review reason:** Too narrow for assignment and follow-up work that does not require a documentation review. **Reason for variance:** Last Planner uses it for causes of missed promises; many reasons here occur before a missed promise.

**Primary-source basis:**
- The term concerns causes of missed assignment promises, not every question awaiting review. [Lean Construction Institute glossary — Reason for Variance][r001].
- Implementation boundary checked in [src/corridor/work_list.py](../../src/corridor/work_list.py#L76); this is local evidence of Corridor's meaning, not an industry standard.

**Research scope:** LCI planning terminology; FHWA action-item reporting; USACE quality issues. No exact counterpart identified.

### PR49 Work Item

**Current meaning:** One current project question anchored to a Candidate or Coordination Subject and presented with all of its Attention Reasons.

**Industry fit:** Generic workflow wording; construction work and coordination actions have narrower meanings.

**Recommendation:** **Work Item internally; Coordination item for customers** (Internal name / clearer UI). Customer wording: **The actual question or next action; Coordination item if a type label is needed**.

**Proposed meaning:** One current coordination question or follow-up need, tied to a proposed item, constraint, or commitment and shown with all its attention reasons.

**Why:** Coordination item is proposed product wording, not an industry standard. It covers both decisions and actions due. Review item would narrow the work to reviewing documents, while Construction activity would imply scheduled field work. Use a specific title such as Assign a person to follow up with the utility. Keep one item per existing subject and retain the current grouping rules.

**Alternatives and limits:** **Review item:** Does not comfortably include due actions and follow-up after completion. **Action item:** Established for an assigned action; not every current question has an agreed action yet.

**Primary-source basis:**
- Action items/outstanding issues are reported with status, responsible people, and due dates. [CDOT US 36 Project Management Plan — 8.B.3][r059].
- Implementation boundary checked in [src/corridor/work_list.py](../../src/corridor/work_list.py#L244); this is local evidence of Corridor's meaning, not an industry standard.

**Research scope:** FHWA project controls; LCI tasks; construction review terminology. Coordination item is proposed product wording.

### PR50 Exception

**Current meaning:** A derived fact that a Constraint needs attention, grouped by its rule and ordered only by that rule's own quantity.

**Industry fit:** Established word with a conflicting TxDOT meaning; no exact equivalent for Corridor's rule output.

**Recommendation:** **Constraint Alert** (Rename / clearer wording). Customer wording: **Constraint alerts**.

**Proposed meaning:** An automatic check identifies a current condition on a constraint that needs attention.

**Why:** An overdue promise and missing documentation are different alerts; group by rule without inventing a combined urgency score. TxDOT's utility exception concerns a proposed departure from accommodation rules. Corridor must not imply that authority. Alerts also do not establish defective construction or a critical-path delay.

**Alternatives and limits:** **Utility exception:** Already names a formal engineering approval process. **Nonconformance / deficiency:** These concern work failing requirements, not every missing record or late promise.

**Primary-source basis:**
- UAR exceptions seek approval for installations that depart from specified accommodation rules. [TxDOT Austin Utility Exception Request — pp. 1, 4][r060].
- Deficiency/rework lists identify contract-noncompliant work requiring correction. [UFGS 01 45 00 — 1.8.6][r061].

**Research scope:** TxDOT UAR exceptions; federal quality control; project reporting.

### PR51 Evaluation

**Current meaning:** One project's Exceptions computed for one date, ruleset, threshold set, and Project Record input.

**Industry fit:** Internal calculation object; no construction-standard equivalent for this exact bundle.

**Recommendation:** **Evaluation (internal); Constraint Check (product)** (Keep / explain). Customer wording: **Checks as of [date]**.

**Proposed meaning:** The results from applying one selected set of checks to one project's recorded facts for one specified date.

**Why:** The user needs to know what was checked and when, not learn a generic software noun. Preserve the exact rules, thresholds, and inputs for reproducibility. An automated check is not a professional audit or a new human judgment. Its calculation date must not masquerade as the schedule's last verified update.

**Alternatives and limits:** **Risk assessment:** Would overstate the calculation; alerts do not establish probability or consequence. **Schedule data date:** GAO uses this for the actual/remaining-work boundary, not the date a report happens to be calculated.

**Primary-source basis:**
- A schedule status/data date is distinct from viewing or saving the schedule. [GAO Schedule Assessment Guide — Best Practice 9, p. 122][r062].

**Research scope:** GAO schedule assessments; FHWA reporting; LCI analysis terminology. No exact counterpart identified.

### PR52 Report

**Current meaning:** A structured publication view of one Evaluation and the Project Record values it presents.

**Industry fit:** Established generic document type; exact report names depend on purpose and agency.

**Recommendation:** **Coordination Report** (Keep / explain). Customer wording: **Constraint status report**.

**Proposed meaning:** A dated presentation of recorded constraints, commitments, project decisions, and results from the same set of checks.

**Why:** The qualified name states the coverage. Internal reports may refresh automatically; a released copy remains fixed. TxDOT's Utility Status Report primarily communicates a utility's schedule, whereas Corridor also presents review and support gaps. Calling every Corridor report an official USR would conceal that difference.

**Alternatives and limits:** **Utility status report:** Established and suitable for a genuinely utility-specific view; do not imply Austin template compliance. **Project progress report:** Suggests broader construction progress, cost, quality, and schedule coverage than Corridor supplies.

**Primary-source basis:**
- The USR tracks a utility/project schedule and emphasizes future completion forecasts. [TxDOT Austin Utility Status Report Guideline — Processes 2–3, pp. 2–3][r056].

**Research scope:** TxDOT and FHWA Utility Status Reports; FHWA project reporting. Coordination Report is scoped product wording.

### PR53 Approved Export

**Current meaning:** The immutable external Report artifact that a designated project person released.

**Industry fit:** Software-oriented label; construction document control distinguishes authorization, transmission, receipt, and acceptance.

**Recommendation:** **Report Approved for Release** (Rename / clearer wording). Customer wording: **Approved to share**.

**Proposed meaning:** A named authorized person approved this exact retained PDF for external sharing.

**Why:** Export can mean downloading any file; approved can be mistaken for acceptance of the work. The proposed wording identifies the actual permission. Preserve the fixed bytes, version, approver, and time. Internal reports remain automatic. A later delivery or acknowledgment requires separate evidence and must not be inferred.

**Alternatives and limits:** **Issued report / transmittal:** Could imply the document was sent to identified recipients; release alone does not establish that. **Certified report:** Implies a certification Corridor does not provide.

**Primary-source basis:**
- The form separates transmission details from approval action and acknowledgment. [USACE ENG Form 4025 — Sections I–II; instructions 9–10][r063].

**Research scope:** USACE transmittals; UK BIM authorization/status guidance; Corridor ADR-0040. Proposed product label, not a standard certification.

### PR54 Briefing

**Current meaning:** A cited model-drafted narrative view of the Project Record that cannot change it or omit its Exceptions.

**Industry fit:** Common communication word; not a standardized model-generated construction record.

**Recommendation:** **Coordination Summary** (Rename / clearer wording). Customer wording: **Coordination summary — AI draft**.

**Proposed meaning:** A source-linked draft narrative explains the current project record without changing its facts or decisions.

**Why:** Summary tells users they are reading an explanation of existing information. Preserve every required Exception, individually or through a bucket retaining all its members; merely mentioning each category is insufficient. The narrative cannot originate a project decision, imply human review, or claim broader progress coverage than the inputs support.

**Alternatives and limits:** **Executive summary:** Established, but can imply broad project coverage and management endorsement; use only when those are accurate. **Assessment / recommendation:** Would imply independent judgment beyond a grounded narrative of the existing record.

**Primary-source basis:**
- An executive summary concisely presents current project status and significant issues before management meetings. [CDOT US 36 Project Management Plan — 8.B.1][r059].
- Implementation boundary checked in [src/corridor/briefing.py](../../src/corridor/briefing.py#L182); this is local evidence of Corridor's meaning, not an industry standard.

**Research scope:** FHWA/CDOT reporting and FTA monthly monitoring summaries; no exact standardized AI-summary counterpart.

## Corridor Operations: every term

### OP01 Document

**Current meaning:** A registered source artifact whose identity, type, date, and renditions are known to Corridor.

**Industry fit:** Established term; Corridor adds registration requirements.

**Recommendation:** **Document** (Keep / explain). Customer wording: **Source document**.

**Proposed meaning:** Source material registered with an identity, type, date, and known renditions. A spreadsheet and its PDF can be renditions of one document rather than two independent sources.

**Why:** Document is familiar and sufficiently broad. Registration describes Corridor's handling, not a different construction object. Keep document identity separate from its files and from the selected passages used to support a conclusion. Calling every upload evidence would erase that distinction.

**Alternatives and limits:** **Information container:** Established BIM wording, but more abstract and broader than needed. **File:** Use for an actual stored file, not the registered document across renditions.

**Primary-source basis:**
- Documents can exist in many media, including paper, electronic files, and photographs; document control is distinct from medium. [ISO/TC 176 guidance on documented information — Sections 2–3; document forms and control][r064].

**Research scope:** Checked ISO documented-information guidance and UK BIM information-container usage. No clearer universal replacement is needed.

### OP02 Extraction Run

**Current meaning:** One immutable attempt to read one Document with a stated extractor configuration.

**Industry fit:** Established technical composition; exact immutability rule is Corridor-specific.

**Recommendation:** **Extraction Run** (Keep / explain). Customer wording: **Document processing attempt, only in technical history**.

**Proposed meaning:** One recorded attempt to extract information from one exact Document using an identified extractor configuration. Its input, configuration, outcome, and output identity remain tied to that attempt.

**Why:** Run already has an explicit execution meaning in data engineering. Extraction specifies the work. The term does not claim success or production eligibility. A second attempt must remain a different run even if it uses the same document; the history must not be replaced.

**Alternatives and limits:** **Job:** Can mean the reusable work definition rather than one execution. **Pass:** Could suggest successful testing instead of an attempted extraction.

**Primary-source basis:**
- A run is one time-specific occurrence of a job, with its own identifier. [OpenLineage Object Model — Job; Run; Dataset][r065].
- Runs record a code execution's metadata and artifacts. [MLflow Tracking — Concepts: Runs][r066].

**Research scope:** Corroborated technical usage in OpenLineage and MLflow; no construction replacement fits this internal operation.

### OP03 Active Run

**Current meaning:** The one production Extraction Run declared for current work on a Document.

**Industry fit:** Corridor selection concept; active is ambiguous in technical use.

**Recommendation:** **Current Production Run** (Rename / clearer wording). Customer wording: **Normally hidden; explain document-processing consequences instead**.

**Proposed meaning:** The explicitly selected, eligible production Extraction Run whose outputs are used for a Document's current work. It is not necessarily the newest attempt or one that is still executing.

**Why:** Current and production describe why this run matters. Keep the attributable selection record and safe eligibility rules. A test run or a failed attempt must not replace it by timestamp. This wording does not create a new operator approval or change ADR-0034's automatic selection contract.

**Alternatives and limits:** **Latest run:** Recency does not establish production eligibility. **Approved run:** Suggests a separate human approval that the contract does not require.

**Primary-source basis:**
- A named alias identifies the specific version used for production; this is an analogy, not a run-selection standard. [MLflow Model Registry Workflows — Model version aliases][r067].

**Research scope:** Checked production aliasing and run lifecycle terminology. No standard names Corridor's exact per-document selection mechanism.

### OP04 Candidate

**Current meaning:** A cited extraction proposal for a Constraint or External Party Statement, preserved alongside the outcome of its handling.

**Industry fit:** Generic technical word; not an established construction record type.

**Recommendation:** **Extracted Proposal** (Rename / clearer wording). Customer wording: **Proposed constraint / Proposed statement, with its actual handling outcome**.

**Proposed meaning:** One source-cited extraction result proposed for inclusion as a Constraint or External Party Statement. Preserve the extracted item and the outcome of its handling, including after acceptance or dismissal.

**Why:** Extracted Proposal states both origin and provisional authority. Extracted item is plainer than Candidate but can still look like an accepted fact. Preserve the original proposal even after it is recorded or excluded; only unresolved proposals should say Pending review. This is proposed product wording, not a construction standard.

**Alternatives and limits:** **Finding:** Can imply that a reviewer has already established the conclusion. **Prediction:** Valid extraction terminology, but awkward and overly model-specific for project users.

**Primary-source basis:**
- Textract describes returned values as predictions and provides a separate confidence field. [Amazon Textract API: Prediction — Prediction contents][r068].

**Research scope:** Checked official document-extraction vocabulary and the construction glossary. Extracted Proposal is a proposed plain-language label, not an industry standard.

### OP05 Admission

**Current meaning:** The recording of a Candidate's supported fact through human Adjudication or an exact deterministic policy, while preserving the extraction proposal.

**Industry fit:** Corridor-specific authority transition; no matching construction term identified.

**Recommendation:** **Record Inclusion** (Rename / clearer wording). Customer wording: **Added to Project Record / Recorded by exact rule**.

**Proposed meaning:** Recording the supported fact from an extracted item in the Project Record through a person's permitted decision or an exact deterministic rule, while retaining the original proposal and its handling history.

**Why:** Inclusion names the destination and does not imply a human must approve every supported fact. Import or extraction cannot express the authority boundary. A policy decision and the actual record change must remain distinguishable, with proof and attribution retained for the write.

**Alternatives and limits:** **Import:** Describes moving data, not deciding whether it belongs in the authoritative record. **Approval:** Incorrectly implies a general human sign-off and can suggest construction authorization.

**Primary-source basis:**
- Policy queries return decisions for an application to enforce; the decision is distinct from executing the application operation. [Integrating Open Policy Agent — Policy decision architecture][r069].

**Research scope:** Checked policy-engine and document-control language. Record Inclusion is a proposed product term for this exact boundary.

### OP06 Adjudication

**Current meaning:** The human act that decides an unresolved Candidate, Dispute, or Dismissal.

**Industry fit:** Established construction term with a materially different, formal meaning.

**Recommendation:** **Human Record Decision** (Rename / clearer wording). Customer wording: **Add record, Resolve disagreement, or Remove from current work**.

**Proposed meaning:** An attributable person's decision on an unresolved extracted item, conflicting project information, or removal from current work. It changes only the record state that this person is authorized to decide.

**Why:** In UK construction, adjudication is a process for resolving contract disputes through an independent adjudicator. Corridor's routine record decisions are not that process. The proposed term names the actor and object without implying legal authority. Documentation Review remains the narrower judgment about required supporting records.

**Alternatives and limits:** **Review:** Can describe reading without a recorded decision; use as an action only with a stated outcome. **Approval:** Can be mistaken for acceptance of construction work or permission to proceed.

**Primary-source basis:**
- Construction adjudication resolves contract disputes through an independent adjudicator under a prescribed process. [Construction Industry Council: Low Value Disputes Adjudication — Opening definition][r070].

**Research scope:** Checked the UK construction industry body's definition. Human Record Decision is proposed wording, not a statutory term.

### OP07 Abstention

**Current meaning:** The result when an automation cannot prove that one exact write is allowed; it leaves the Project Record unchanged.

**Industry fit:** Established machine-learning term; Corridor's policy use is narrower and explicit.

**Recommendation:** **Abstention** (Keep / explain). Customer wording: **Not applied automatically — [specific reason]**.

**Proposed meaning:** A completed policy assessment that cannot establish that one exact record change is permitted, so that change is not made. Keep the reason and unchanged-record outcome.

**Why:** The term usefully distinguishes withholding a decision from rejecting the underlying statement. It must not conceal execution failure: a timeout, exhausted budget, or broken harness is a failed process, even if no record changed. A read-only assistant's unsupported-answer outcome needs its own clearly scoped explanation.

**Alternatives and limits:** **Rejected:** Incorrectly judges the substance of the extracted statement. **Low confidence:** Not all refusals concern probability; identity, freshness, and authority may be missing.

**Primary-source basis:**
- Selective classification can withhold predictions, trading coverage against error. [El-Yaniv and Wiener: Noise-free Selective Classification — Abstract][r071].
- Policy non-applicability and processing indeterminacy are distinct outcomes. [OASIS XACML 3.0 — Sections 7.10–7.14][r072].

**Research scope:** Checked selective-prediction research and policy standards; neither makes a failed runtime a successful abstention.

### OP08 Unplaced Statement

**Current meaning:** An External Party Statement Candidate whose attribution, timing, or Commitment Scope still needs bounded human work.

**Industry fit:** Product-specific phrase; placement obscures several different missing facts.

**Recommendation:** **Statement Needing Clarification** (Rename / clearer wording). Customer wording: **Statement to review — speaker / timing / affected constraints unclear**.

**Proposed meaning:** An extracted External Party Statement whose speaker, timing, or affected Constraints still needs a permitted human decision before the relevant fact can be recorded.

**Why:** Unplaced sounds like a missing location or folder, yet the unresolved issue can be who spoke or what date was promised. Name the missing fact on each item. Do not apply this pending-candidate label to an already recorded statement with accepted unknown scope; show only its remaining work.

**Alternatives and limits:** **Unassigned statement:** Confuses missing scope or attribution with the project person's action assignment. **RFI:** A formal request for information is a different document and workflow.

**Primary-source basis:**
- Document-extraction results may require human scrutiny before use; the application determines the relevant checks. [Amazon Textract Best Practices — Use Confidence Scores][r073].

**Research scope:** Checked document-extraction review and construction document-control usage. No standard covers Corridor's exact unresolved-fact combination; this is proposed product wording.

### OP09 Revision Comparison

**Current meaning:** An immutable comparison of two exact Extraction Runs for predecessor and successor Documents.

**Industry fit:** Established document-control operation; Corridor adds exact-run provenance.

**Recommendation:** **Revision Comparison** (Keep / explain). Customer wording: **Compare document revisions**.

**Proposed meaning:** A preserved comparison of the exact extraction outputs from one predecessor Document and its registered successor. It records matched rows, differences, missing rows, and uncertain correspondences without silently revising earlier results.

**Why:** Comparison describes examining two versions without promising a perfect match. It is clearer than inventing another noun. Keep the historical result separate from today's work caused by the revision. Failed extraction cannot establish that a source row disappeared.

**Alternatives and limits:** **Revision diff:** Can suggest exact row correspondence even when matching remains ambiguous. **Change report:** Collides with the project's published Report and can imply accepted changes.

**Primary-source basis:**
- Users can select two file versions and compare their differences. [Autodesk Docs: View Version History — Version History Menu Options][r074].

**Research scope:** Checked construction document software and W3C revision provenance. The operation is familiar; immutable extraction inputs remain Corridor's explicit extension.

### OP10 Supersession Review

**Current meaning:** The current operations work needed because Operative Support still uses a superseded Document or revision processing is incomplete.

**Industry fit:** Document replacement is established; this worklist's scope is Corridor-specific.

**Recommendation:** **Document Revision Review** (Rename / clearer wording). Customer wording: **Newer document needs attention**.

**Proposed meaning:** The current work needed when recorded conclusions still rely on a replaced document, or processing the replacement has not finished. The worklist changes as source support is updated or the resulting questions are answered.

**Why:** Revision Review tells the operator what caused the work. Supersession remains the registered relationship between documents, not an instruction to approve the replacement. Show whether the missing step is extraction, source comparison, or a project decision; do not hide technical failure behind Awaiting review.

**Alternatives and limits:** **Revision comparison:** That is the immutable comparison, not today's unresolved work. **Stale records:** Can incorrectly imply the accepted conclusion is false rather than inadequately supported.

**Primary-source basis:**
- Revision control tracks changes between versions and the versions shared with others; it is distinct from suitability status. [UK BIM Framework Guidance Part C — Section 5, pages 17–20][r075].

**Research scope:** Checked UK BIM revision-control guidance and the local comparison/review separation. Document Revision Review is a plain-language product label.

### OP11 Reconfirmation

**Current meaning:** The human act that moves established Operative Support to current Supporting Documentation without changing the admitted conclusion.

**Industry fit:** No exact industry equivalent identified for this source-support transfer.

**Recommendation:** **Human Support Update** (Rename / clearer wording). Customer wording: **Confirm replacement supporting document**.

**Proposed meaning:** A person's attributable act replacing the current supporting source for an already accepted conclusion, without changing that conclusion or originating a new judgment that a documentation requirement is met.

**Why:** Reconfirmation does not identify what is confirmed. Limit the act to replacing support for the same conclusion. A changed conclusion needs its decision flow. This is not document reapproval or confirmation that construction was inspected.

**Alternatives and limits:** **Reapproval:** Suggests a fresh authorization of the underlying work or conclusion. **Source verification:** Checking a passage alone is not the whole act of updating support.

**Primary-source basis:**
- Revision provenance and attributed activities can record a changed supporting source; PROV prescribes no automatic validity transfer. [W3C, PROV-DM: The PROV Data Model — Sections 5.2.2 and 5.3.3][r076].
- Revision identity and information suitability are managed separately. [UK BIM Framework Guidance Part C — Sections 5–6][r075].

**Research scope:** Checked provenance, construction revision control, and reconfirmation usage. The proposal describes Corridor's mechanism, not a standard procedure.

### OP12 Automatic Carry-Forward

**Current meaning:** The fail-closed policy act that moves established Operative Support to exact unchanged current Supporting Documentation.

**Industry fit:** Carry-forward has other established uses; the exact support operation is custom.

**Recommendation:** **Automatic Support Update** (Rename / clearer wording). Customer wording: **Supporting document updated; recorded conclusion unchanged**.

**Proposed meaning:** A deterministic rule updates an existing supporting-source link only after proving an exact, unique, unchanged replacement. It preserves the established conclusion and human judgment and makes no new judgment for the project.

**Why:** Support Update states what moves. Carry-forward could mean money, schedule data, or copied content. Retain the exact-match restrictions: no new facts, no guessed equivalence, no expanding scope, and a preserved refusal when proof is missing.

**Alternatives and limits:** **Automatic approval:** Falsely grants the machine human decision authority. **Synchronization:** Suggests unrestricted updating toward the newest data.

**Primary-source basis:**
- Derivation links related source entities but does not itself establish that a conclusion remains valid. [W3C, PROV-DM: The PROV Data Model — Section 5.2][r076].
- Carry-forward here moves purchase-order obligations between budget periods, illustrating a different established meaning. [Oracle Financials: Purchase Order Carry Forward — Opening description][r077].

**Research scope:** Checked document revision and financial carry-forward usage. No exact standard was established; this is proposed internal wording.

### OP13 Carry-Forward Policy

**Current meaning:** The Corridor-released, named, and versioned rules for Automatic Carry-Forward.

**Industry fit:** Policy is established technical language; this named policy family is Corridor-specific.

**Recommendation:** **Automatic Support Update Rules** (Rename / clearer wording). Customer wording: **Normally hidden; technical audit names the rule version**.

**Proposed meaning:** The named, versioned rules released by Corridor that determine whether an automatic supporting-source update is permitted. They are engineering controls, not a customer's approval of a project conclusion.

**Why:** Rules tells an operator what is being executed. Preserve the released version, digest, eligibility predicates, and refusal reasons so the decision can be explained and repeated. Renaming must not resurrect the superseded customer authorization gate or weaken the ban on originating a Documentation Review.

**Alternatives and limits:** **Authorization:** Can wrongly imply project permission or blanket consent to future changes. **Reviewer bot:** Suggests discretionary judgment rather than fixed predicates.

**Primary-source basis:**
- Policy-as-code evaluates structured facts under explicit rules. [Open Policy Agent documentation — Policy decision and enforcement introduction][r078].
- Policies identify their version and the requests to which they apply. [OASIS XACML 3.0 — PolicyId, Version, and Target examples][r072].

**Research scope:** Checked OPA and OASIS policy terminology; no construction term replaces this engineering rule set.

### OP14 Carry-Forward Run

**Current meaning:** The immutable receipt for one Carry-Forward Policy evaluation and its carried or abstained outcomes.

**Industry fit:** Run and decision log are established; the immutable receipt is product-specific.

**Recommendation:** **Support Update Run Record** (Rename / clearer wording). Customer wording: **Automatic update history, with applied and not-applied results**.

**Proposed meaning:** The immutable record of one support-update rule execution: exact inputs, policy version, and every applied or abstained outcome. A later attempt creates another record.

**Why:** The current definition calls a receipt a run. Adding Record makes the retained object explicit, while Run still groups one execution's outcomes. The record must include refusals, not only successful changes. A generic log label must not weaken immutability, exact input identity, or attribution.

**Alternatives and limits:** **Decision log:** Established software term, but does not by itself promise an immutable complete run record. **Receipt:** Useful technically, but customers may interpret it as acknowledgement of document delivery.

**Primary-source basis:**
- Decision events retain policy identity, inputs, result, revision metadata, and trace identifiers. [Open Policy Agent: Decision Logs — Decision Log Service API][r079].

**Research scope:** Checked policy decision logging and run models. Support Update Run Record is proposed terminology; it does not claim OPA guarantees Corridor's immutability.

### OP15 Revision Processing

**Current meaning:** The ordered operation that verifies a Revision Comparison before it invokes the Carry-Forward Policy.

**Industry fit:** Familiar technical phrase; exact stage ordering belongs to Corridor.

**Recommendation:** **Document Revision Processing** (Keep / explain). Customer wording: **Processing newer document / Specific unresolved consequence**.

**Proposed meaning:** The ordered operation that verifies the preserved comparison between exact document runs before applying the rules for an automatic supporting-source update.

**Why:** This is machinery, not a project decision. Document identifies what is revised. Keep comparison verification ahead of permitted updates. A completed comparison is not necessarily completed source maintenance or a reviewed project conclusion.

**Alternatives and limits:** **Revision approval:** Misstates authority and treats processing success as human approval. **Change control:** An established but broader management discipline, not this narrow automated sequence.

**Primary-source basis:**
- The common-data-environment workflow distinguishes information states and tracks revisions. [UK BIM Framework Guidance Part C — Sections 2 and 5][r075].
- Applications can enforce rule decisions as part of their processing flow. [Open Policy Agent documentation — Policy decision and enforcement introduction][r078].

**Research scope:** Checked construction document-control and policy-processing sources. The compound is explanatory; no external standard specifies Corridor's operation order.

### OP16 Extraction Measurement

**Current meaning:** A scored comparison of exact Extraction Runs with a declared reference and its limits.

**Industry fit:** Measurement is standard technical language; the compound is an explicit scoped label.

**Recommendation:** **Extraction Measurement** (Keep / explain). Customer wording: **Normally internal; Extraction results compared with [reference]**.

**Proposed meaning:** A scored comparison of exact extraction outputs with an identified reference, including the reference's coverage, origin, shared dependencies, and limits.

**Why:** Measurement avoids colliding with the Project Record's Evaluation and does not promise complete truth. Official document-extraction systems compare predictions with labelled test data. Corridor's machine-produced reference is weaker: agreement cannot establish independent completeness or unqualified recall. The label should never hide that difference or turn one test set into an industry benchmark.

**Alternatives and limits:** **Accuracy:** Too broad and potentially unsupported by the measured denominator. **Benchmark:** Can suggest an external, stable, representative comparison standard that this reference lacks.

**Primary-source basis:**
- Document AI compares extracted predictions with test annotations to compute precision, recall, and F1. [Google Cloud Document AI: Evaluate performance — Evaluation metrics and test annotations][r080].

**Research scope:** Checked document-AI evaluation and local ADR-0023. Keep the scoped label; report agreement honestly when the reference is machine-authored.

### OP17 Cohort Receipt

**Current meaning:** The immutable membership of one bounded rehearsal population.

**Industry fit:** Cohort is a grouping term; manifest is the closer technical object.

**Recommendation:** **Rehearsal Input Manifest** (Rename / clearer wording). Customer wording: **Normally hidden; Cases included in this test**.

**Proposed meaning:** The immutable list of exact items included in one bounded rehearsal, with the selection rule, source identities, and digest that fix its membership.

**Why:** Manifest names an inventory. It is clearer than Receipt, which says little about what was retained. The proposed term must preserve the fact that these members bound permitted rehearsal actions; it is not merely a convenient view. A saved query that later returns different items is not this manifest.

**Alternatives and limits:** **Test dataset:** Related, but may imply labelled expected answers or allow mutable contents. **Sample:** Does not express exact membership, provenance, or the mutation boundary.

**Primary-source basis:**
- A manifest explicitly lists payload file identities and checksums; Corridor extends the inventory idea to identified rehearsal items. [RFC 8493: The BagIt File Packaging Format — Sections 1.3 and 2.1.3][r081].

**Research scope:** Checked digital-preservation manifests and ML evaluation datasets. The proposed compound is not a construction term or a claim of BagIt conformance.

### OP18 Lane

**Current meaning:** A bounded operations path whose offered Candidates and allowed mutations share one scope.

**Industry fit:** Established in BPMN, but that meaning does not define Corridor's action scope.

**Recommendation:** **Processing Scope** (Rename / clearer wording). Customer wording: **Keep hidden; one Work List with clear permitted actions**.

**Proposed meaning:** The explicit set of items and permitted operations for one bounded processing or review path. What the interface offers and what the system may change must obey the same scope.

**Why:** Lane invites a second queue, swimlane, or road-lane interpretation. BPMN uses lanes to organize activities, not to establish Corridor's record-write permission. Scope states the important rule. Renaming must preserve project, receipt-membership, identity, and allowed-action checks; it is not permission to treat the boundary as a filter.

**Alternatives and limits:** **Workstream:** Describes a group of work, not the limits on a write. **View or filter:** Presentation choices cannot establish mutation permission.

**Primary-source basis:**
- A lane partitions a process to organize and categorize activities; it is not defined as a data authorization boundary. [OMG Business Process Model and Notation — Section 10.8, printed page 304][r082].

**Research scope:** Checked the formal BPMN meaning and local lane checks. Processing Scope is proposed internal wording, not a standard construction lifecycle.

## Cross-term consistency and adoption cautions

- **External organization versus statement author:** PR03 names an organization; PR14 names its recorded promise/change/completion statement. The party mentioned, affected party, and actual speaker must remain distinct.
- **Source field value versus External Party Statement:** PR27 is one document’s value for one field; PR14 is the separate commitment-related fact. Source field value avoids giving both the same customer name.
- **Coordination decision versus human record decision versus Documentation Review:** PR25 changes the project’s follow-up response; OP06 decides permitted record changes; PR41 judges the stated documentation requirement. Do not collapse these into one Approval.
- **Completion reported versus completion supported versus contract acceptance:** a party’s statement, a scoped reviewed conclusion, and a formal agency/contract act retain separate meanings and authors.
- **Supporting documentation in use versus current document revision:** PR31 identifies what a value or judgment relies on. PR35 identifies replacement. The role can remain selected while its source has become superseded.
- **Key dates versus required, promised, estimated, and actual dates:** event identity, schedule revision, project need, external promise, prediction, and actual completion remain distinguishable.
- **Work item and Attention Reason are not only document review:** they also cover assignment, due actions, and follow-up. The consolidated labels deliberately avoid narrowing all of them to Review item/Review reason.
- **Statement history is not a substitute for stable identity:** the PR20 display change must not merge two deliverables or turn an immutable lineage into a mutable note.
- **Record Inclusion and support updates are not construction approval:** deterministic rules retain their exact proof, source, and refusal boundaries. User-facing wording must not restore an obsolete blanket human approval gate.
- **Exclusion is not resolution:** Do Not Add and Remove from Active Log apply to the defined proposal/erroneous-record cases; they must not hide a real constraint just because it is inconvenient.
- **Inventory and matrix corrections can affect classification:** the suggested industry definitions do not authorize extractor, admission, or source-priority changes. Separate the source’s purpose from which fields it actually supplies.
- **Future adoption is a separate task:** update agreed definitions, relevant ADRs, UI copy, and tests together; preserve compatibility identifiers and historical receipts until an explicitly scoped migration. Nothing here settles the open requirement-revision lifecycle or schedule-impact reassessment policy.

## Additional terms worth defining

These additions are separately assessed; they do not inflate the 72-term coverage count. Additions describing existing behavior can make the glossary clearer. Boundary definitions prevent misinterpretation. New workflows remain deferred unless separately requested.

| Candidate addition | Suggested treatment | Why it fills a gap |
| --- | --- | --- |
| Utility Facility | Define existing concept | The glossary names Utility Owner and Utility Conflict but leaves their physical object implicit. A facility is neither its owner nor the conflict. This distinction prevents one owner's multiple facilities or shared pole attachments from being collapsed into one condition. |
| Alignment | Define existing concept | Stationing already depends on alignment identity. The same station number on Mainline and Ramp B can identify different locations. Define the word users must supply rather than hiding it inside the Stationing definition. |
| Stated By | Define existing concept | Attribution is already an ADR-0036 fact but is missing as an explicit glossary entry. 'Stated by' tells the user why a quoted promise belongs to this party. |
| Deliverable / Promised work | Define existing concept | Makes 'what was promised' explicit and helps distinguish two promises by the same party. Use Deliverable in the glossary and Promised work or Promised document where the result permits a more concrete screen label. |
| Timing Precision | Define existing concept | The existing precision rule is central to truthful display and past-due findings but lacks its own definition. A month is not an invented first-of-month day; approximation and incomplete precision should remain distinguishable. |
| Schedule Data Date | Define boundary; workflow/data separate | Prevents users from confusing schedule currency with import time, issue date, or Corridor's evaluation date. Useful when presenting a source schedule version. |
| Cited passage | Define existing concept | Separates the passage actually checked from an entire attached document. It can be a UI sublabel under Supporting Documentation rather than another independent domain entity. |
| Document revision | Define existing concept | Distinguishes changed content from a different file format. A PDF and XLSX can be renditions of one revision; neither automatically replaces the other. |
| Contract Acceptance | Define boundary; workflow/data separate | A glossary boundary helps prevent Documentation Review or Completion Reported from being mistaken for legal or contractual acceptance. This is not a proposal to add an acceptance workflow to Corridor. |
| Document Transmittal | Defer capability; define boundary only | Prevents approval of a fixed report from being mistaken for sending it. USACE ENG 4025 records sender, recipient, document items, and transmittal identity separately from approval. |
| Receipt Acknowledgment | Defer capability; define boundary only | Receipt is distinct from approval to share and from acceptance of the document's content. USACE uses an acknowledgment action separately from approval and records actual receipt dates. |
| Utility Accommodation Rules Exception | Define boundary; workflow/data separate | This real industry concept should be distinguishable from automatically generated constraint alerts, particularly for TxDOT users. |
| Processing Failure | Define existing concept | Abstention alone cannot describe all unchanged-record outcomes. The user must be able to tell a justified decision not to act from a process that failed. OpenLineage explicitly distinguishes failure and abnormal termination from completion. |
| Statement Review Assistant | Define existing concept | The actor is already used but absent from the glossary. Statement narrows its scope more accurately than Document; Assistant explains its non-authoritative role. The proposed name is not supplied by an industry standard. |
| Product Test Run | Define existing concept | ADR-0046 defines a major operation missing from the glossary. Test Run is plainer than Proving Run; it avoids suggesting that a bounded simulated exercise proves industry acceptance or real practitioner performance. |
| Reference Dataset | Define existing concept | Measurements depend on a reference, yet the glossary does not name it. Official evaluation tools use evaluation datasets; Corridor must distinguish machine-generated comparison data from independently verified answers. |
| Audit Trail | Define existing concept | The system already relies on audit history, but the glossary does not explain it. This is separate from current Constraint records or a processing result. |
| Provenance / Source traceability | Define existing concept | This explains the central promise of Corridor. Run Provenance is the Operations-specific form: document its exact inputs, configuration, outputs, and execution identity. |
| Source Passage Check | Define existing concept | Separates mechanical citation checking from a human judgment that the documentation meets the requirement. |
| Document Rendition | Define existing concept | A rendition is the representation; File format is its metadata, such as PDF or XLSX. Another format must not become a falsely independent source or a newer revision merely because it is uploaded later. |
| Activity | Define existing concept | Constraint and Milestone definitions both depend on this distinction. It helps explain what construction is waiting to perform. |
| Actual Completion Date | Define boundary; field work separate | Keeps completion separate from a promised date, estimate, report date, and review timestamp. |
| Estimated Completion Date | Define boundary; field work separate | An estimate is not a promise or a completed fact. This distinction is already needed to interpret statements truthfully. |
| Right of Way (ROW) | Define reference term | The term already occurs in project materials and examples. It is different from a general promise, a permit, or a temporary right of entry. |

### Additional 01: Utility Facility

**Suggested definition:** The physical utility infrastructure being discussed, such as a pole, pipe, conduit, cable, or related structure.

**Reason:** The glossary names Utility Owner and Utility Conflict but leaves their physical object implicit. A facility is neither its owner nor the conflict. This distinction prevents one owner's multiple facilities or shared pole attachments from being collapsed into one condition.

**Scope:** Clarification of an existing concept. A future facility identity model or asset register would be a separate feature, not authorized by this terminology addition.

**Sources:** [Iowa Administrative Code 761, chapter 115: Utility Accommodation][r007].

**Source context:** Iowa 761-115.2, Utility facility, page 4; current supplement 2026-06-24. Explicitly names poles, pipes, conduits, cables, and other utility structures.

### Additional 02: Alignment

**Suggested definition:** The named line used to describe a roadway's route and locate its stations; horizontal alignment describes the route in plan view.

**Reason:** Stationing already depends on alignment identity. The same station number on Mainline and Ramp B can identify different locations. Define the word users must supply rather than hiding it inside the Stationing definition.

**Scope:** Clarification of existing location semantics. Supporting new coordinate systems or engineering geometry remains separate implementation work.

**Sources:** [TxDOT Glossary H][r083], [Caltrans Plans Preparation Manual, chapter 2, section 1][r013].

**Source context:** TxDOT Glossary, Horizontal alignment, and Caltrans Plans Preparation Manual September 2018, Stationing, page 2-8.

### Additional 03: Stated By

**Suggested definition:** The organization that made the statement, and the individual speaker when identified by the source, distinct from the organization merely mentioned or affected.

**Reason:** Attribution is already an ADR-0036 fact but is missing as an explicit glossary entry. 'Stated by' tells the user why a quoted promise belongs to this party.

**Scope:** Existing ADR-0036 attribution concept. Organization-only documentary attribution remains permitted. The named internal recorder required for a verbal statement is separate from an optional identified external speaker.

**Sources:** [Lean Construction Institute glossary][r001], [Ballard and Tommelein: 2020 Last Planner System Benchmark][r028].

**Source context:** LCI Dictionary: Promise and Performer; Last Planner benchmark 8.2.9, printed pp. 81-82.

### Additional 04: Deliverable / Promised work

**Suggested definition:** The result, document, service, or defined work the party committed to provide.

**Reason:** Makes 'what was promised' explicit and helps distinguish two promises by the same party. Use Deliverable in the glossary and Promised work or Promised document where the result permits a more concrete screen label.

**Scope:** Document the existing 'what it will deliver' part of Commitment; not a new work-breakdown or contract-management feature.

**Sources:** [AACE 10S-90: Cost Engineering Terminology][r017], [Ballard and Tommelein: 2020 Last Planner System Benchmark][r028].

**Source context:** AACE Deliverable (June 2007); Last Planner benchmark 8.2.9.

### Additional 05: Timing Precision

**Suggested definition:** How specifically the source states timing, such as an exact day, a month, or approximate wording.

**Reason:** The existing precision rule is central to truthful display and past-due findings but lacks its own definition. A month is not an invented first-of-month day; approximation and incomplete precision should remain distinguishable.

**Scope:** Document existing ADR-0036 behavior. Established date-representation concept; no construction-specific label claimed, and no EDTF implementation mandated.

**Sources:** [Library of Congress: Extended Date/Time Format Specification][r027].

**Source context:** Library of Congress EDTF Level 0 Date and Level 1 Qualification of a date.

### Additional 06: Schedule Data Date

**Suggested definition:** The cutoff through which the source schedule includes actual progress, separating recorded progress from its forecast.

**Reason:** Prevents users from confusing schedule currency with import time, issue date, or Corridor's evaluation date. Useful when presenting a source schedule version.

**Scope:** Established scheduling term. Add as a boundary definition now only if useful; capturing a missing data-date field would be a separately approved feature, not a terminology edit.

**Sources:** [AACE 10S-90: Cost Engineering Terminology][r017], [GSA 552.236-15: Schedules for Construction Contracts][r023].

**Source context:** AACE Data Date (October 2018); GSA 552.236-15 paragraphs (h)-(i).

### Additional 07: Cited passage

**Suggested definition:** The exact part of a source document shown beside a recorded statement, with its document and page or row locator.

**Reason:** Separates the passage actually checked from an entire attached document. It can be a UI sublabel under Supporting Documentation rather than another independent domain entity.

**Scope:** Plain product label informed by quotation provenance; adds no new authority or workflow.

**Sources:** [W3C, PROV-DM: The PROV Data Model][r076].

### Additional 08: Document revision

**Suggested definition:** An identifiable issued version of a document; its replacement relationship is recorded separately.

**Reason:** Distinguishes changed content from a different file format. A PDF and XLSX can be renditions of one revision; neither automatically replaces the other.

**Scope:** Established document-control language; clarify an existing concept, not invent a new lifecycle.

**Sources:** [Four Rivers Nuclear Partnership / DOE Paducah, Document Control Process][r040], [UK Government Digital Service, Record information about data sets you share with others][r045].

### Additional 09: Contract Acceptance

**Suggested definition:** The formal act by the authorized project or oversight party accepting contract work under the governing contract.

**Reason:** A glossary boundary helps prevent Documentation Review or Completion Reported from being mistaken for legal or contractual acceptance. This is not a proposal to add an acceptance workflow to Corridor.

**Scope:** Established construction-administration term; terminology boundary only, with jurisdiction-specific authority.

**Sources:** [FHWA, Federal-aid Essentials: Project Closeout][r038], [Caltrans Construction Manual, Section 3-5][r041], [UFGS 01 45 00][r061], [Caltrans Source Inspection Guidelines][r052].

### Additional 10: Document Transmittal

**Suggested definition:** A recorded sending of identified document versions from a sender to named recipients, for a stated purpose and date.

**Reason:** Prevents approval of a fixed report from being mistaken for sending it. USACE ENG 4025 records sender, recipient, document items, and transmittal identity separately from approval.

**Scope:** New workflow if Corridor records sending. Add a boundary/reference definition now only if adopted; ADR-0040 expressly leaves transmittal outside the existing release slice.

**Sources:** [USACE ENG Form 4025][r063].

### Additional 11: Receipt Acknowledgment

**Suggested definition:** Evidence that the intended recipient acknowledged receiving an identified document transmission.

**Reason:** Receipt is distinct from approval to share and from acceptance of the document's content. USACE uses an acknowledgment action separately from approval and records actual receipt dates.

**Scope:** New workflow/data if tracked by Corridor; not a rename of the current release receipt.

**Sources:** [USACE RMS Submittals and Transmittals][r084], [USACE ENG Form 4025][r063].

### Additional 12: Utility Accommodation Rules Exception

**Suggested definition:** An agency decision allowing a specified utility accommodation that departs from an identified requirement of its accommodation rules.

**Reason:** This real industry concept should be distinguishable from automatically generated constraint alerts, particularly for TxDOT users.

**Scope:** Reference distinction or future typed agency-approval fact. A new workflow is not authorized merely by adding its definition.

**Sources:** [TxDOT Austin Utility Exception Request][r060].

### Additional 13: Processing Failure

**Suggested definition:** An attempt that did not complete the required processing contract, including transport failure, exhausted resource budget, or invalid output.

**Reason:** Abstention alone cannot describe all unchanged-record outcomes. The user must be able to tell a justified decision not to act from a process that failed. OpenLineage explicitly distinguishes failure and abnormal termination from completion.

**Scope:** Existing runtime outcomes; terminology must preserve failed receipts and must not count them as successful abstentions.

**Sources:** [OpenLineage Run Cycle][r085].

**Existing implementation:** [src/corridor/evidence_investigator_runtime.py](../../src/corridor/evidence_investigator_runtime.py), [src/corridor/evidence_investigator_evaluation.py](../../src/corridor/evidence_investigator_evaluation.py).

### Additional 14: Statement Review Assistant

**Suggested definition:** The existing Evidence Investigator: a bounded, read-only assistant gathering supported options for one unresolved statement, without authority to make the project decision.

**Reason:** The actor is already used but absent from the glossary. Statement narrows its scope more accurately than Document; Assistant explains its non-authoritative role. The proposed name is not supplied by an industry standard.

**Scope:** Existing concept and proposed plain-language alias; no new access, autonomous writing, or authority. Keep Evidence Investigator identifiers.

**Sources:** [NIST AI RMF Core][r086].

**Existing implementation:** [src/corridor/evidence_investigator.py](../../src/corridor/evidence_investigator.py).

### Additional 15: Product Test Run

**Suggested definition:** The existing Product Proving Run: one bounded raw-document-to-published-report exercise through the actual application, with a stated simulated practitioner and retained success or failure receipt.

**Reason:** ADR-0046 defines a major operation missing from the glossary. Test Run is plainer than Proving Run; it avoids suggesting that a bounded simulated exercise proves industry acceptance or real practitioner performance.

**Scope:** Existing ADR-0046 concept; preserve its exact inputs, restoration, frontend, two-pass gates, and claim limits. Not construction acceptance testing.

**Sources:** [NIST AI RMF Core][r086], [MLflow Tracking][r066].

**Existing implementation:** [docs/adr/0046-product-proving-runs-start-from-bounded-raw-documents.md](../../docs/adr/0046-product-proving-runs-start-from-bounded-raw-documents.md), [src/corridor/product_proving_execution.py](../../src/corridor/product_proving_execution.py).

### Additional 16: Reference Dataset

**Suggested definition:** The identified comparison data used for an extraction measurement, with origin, coverage, and whether expected values were independently established.

**Reason:** Measurements depend on a reference, yet the glossary does not name it. Official evaluation tools use evaluation datasets; Corridor must distinguish machine-generated comparison data from independently verified answers.

**Scope:** Existing measurement concept; preserve ADR-0023's machine-reference limits. Does not add a founder-labeling requirement or claim human gold.

**Sources:** [MLflow Evaluation Datasets][r087], [Google Cloud Document AI: Evaluate performance][r080].

**Existing implementation:** [docs/adr/0023-a-machine-reference-is-a-semi-independent-ceiling-not-human-gold.md](../../docs/adr/0023-a-machine-reference-is-a-semi-independent-ceiling-not-human-gold.md), [src/corridor/eval.py](../../src/corridor/eval.py).

### Additional 17: Audit Trail

**Suggested definition:** The chronological record of changes and their authors, times, and affected records.

**Reason:** The system already relies on audit history, but the glossary does not explain it. This is separate from current Constraint records or a processing result.

**Scope:** Existing concept in audit.py and AuditLog. The definition does not claim every attempted operation is logged or that a log alone proves correctness.

**Sources:** [NIST CSRC Audit Trail glossary][r088], [FHWA Computerization of Construction Record][r089].

**Source context:** NIST audit-trail definitions; FHWA Computerization of Construction Record, 21 September1989, Reliability of Records. The latter is historical construction usage, not a current legal compliance opinion.

### Additional 18: Provenance / Source traceability

**Suggested definition:** Information linking a recorded result to its sources, processing steps, and responsible people or systems.

**Reason:** This explains the central promise of Corridor. Run Provenance is the Operations-specific form: document its exact inputs, configuration, outputs, and execution identity.

**Scope:** Existing cross-context concept. Source traceability is the plain customer explanation; provenance is established technical language. It does not certify physical truth.

**Sources:** [W3C PROV Overview][r090], [W3C, PROV-DM: The PROV Data Model][r076], [OpenLineage Object Model][r065].

**Source context:** W3C PROV Overview introduction and PROV-DM; OpenLineage object model.

### Additional 19: Source Passage Check

**Suggested definition:** A check that a cited passage is present in the identified source page or row.

**Reason:** Separates mechanical citation checking from a human judgment that the documentation meets the requirement.

**Scope:** Existing verify.py behavior. This is proposed plain wording; no exact construction-wide term was found. Preserve the specific matching method and its limits rather than implying a field inspection.

**Sources:** [W3C, PROV-DM: The PROV Data Model][r091].

**Source context:** W3C quotation provenance is a comparator, not a prescription of Corridor matching. Local basis: src/corridor/verify.py.

### Additional 20: Document Rendition

**Suggested definition:** A particular file representation of the same document revision, such as its issued spreadsheet or PDF representation.

**Reason:** A rendition is the representation; File format is its metadata, such as PDF or XLSX. Another format must not become a falsely independent source or a newer revision merely because it is uploaded later.

**Scope:** Existing rendition concept. Use File and Format as distinct customer fields, preserve exact file identity, and do not assert equivalence without evidence. This proposed label does not redefine document authority.

**Sources:** [DCMI Metadata Terms][r092], [DCMI Metadata Terms][r093].

**Source context:** DCMI distinguishes a related resource in another format from the format attribute of that resource.

### Additional 21: Activity

**Suggested definition:** A defined piece of work that takes time, unlike its start or finish event.

**Reason:** Constraint and Milestone definitions both depend on this distinction. It helps explain what construction is waiting to perform.

**Scope:** Reference definition for an existing scheduling concept. Adding a full activity network, duration model, or resource plan would be separate implementation.

**Sources:** [NJDOT Scheduling Manual for Design Projects][r021].

**Source context:** NJDOT Scheduling Manual for Design Projects, 3.0 Definitions.

### Additional 22: Actual Completion Date

**Suggested definition:** The supported date when the specified work was completed.

**Reason:** Keeps completion separate from a promised date, estimate, report date, and review timestamp.

**Scope:** Boundary definition; populate only when the source establishes actual completion timing. Capturing a distinct new field is not authorized by this research.

**Sources:** [Oracle Primavera Cloud Activities Fields][r094], [TxDOT Austin Utility Status Report Guideline][r056].

**Source context:** Oracle Activities Fields, Actual Finish; Austin USR p13 estimated versus actual dates.

### Additional 23: Estimated Completion Date

**Suggested definition:** A prediction of when specified work will finish, with its source and basis.

**Reason:** An estimate is not a promise or a completed fact. This distinction is already needed to interpret statements truthfully.

**Scope:** Boundary definition; do not create a forecast or call a source estimate a commitment. A forecast feature would need separate specifications.

**Sources:** [TxDOT Austin Utility Status Report Guideline][r056], [NJDOT Scheduling Manual for Design Projects][r021].

**Source context:** Austin USR pp8 and13; NJDOT Schedule Updates, forecast versus actual finish.

### Additional 24: Right of Way (ROW)

**Suggested definition:** Land or a property interest acquired or devoted to transportation use.

**Reason:** The term already occurs in project materials and examples. It is different from a general promise, a permit, or a temporary right of entry.

**Scope:** Industry reference definition; a title determination, parcel system, or legal clearance decision is not implied.

**Sources:** [TxDOT Glossary R][r095].

**Source context:** TxDOT Glossary, Right of way; distinguish adjacent Right of entry definition.

## Method, evidence limits, and completion record

Five independent research groups covered the 71 freshly reviewed entries; the completed Milestone review supplies PR09. The parent checked the exact inventory, reconciled naming collisions, inspected relevant code paths, and independently verified key agency/standards passages. Each term is a research decision, not a popularity poll; no adoption rates or universal terminology claims are inferred from one manual.

Primary sources include FHWA and multiple state DOTs, USACE/FTA, construction-industry organizations, government recordkeeping guidance, W3C/DCMI/OASIS/OMG/RFC specifications, and first-party software documentation for internal concepts. Provider documentation demonstrates that provider’s usage, not construction-wide acceptance. Jurisdiction-specific legal terms are included to identify boundaries, not to give a legal opinion about a project.

Some official PDF or spreadsheet browser fetches failed; the relevant official files were then read through a direct public download. No claim depends on bypassing an access restriction, gated membership material, a search snippet alone, or an assumed production workflow. Source editions and scope are retained below and in each entry.

The proposed labels are not adopted. No glossary, ADR, application, schema, tracker, or development Project Record data was changed. The standalone Milestone note is preserved and incorporated. Runtime tests were not needed for a research-only artifact.

### Inventory verification

- Project Record: 54/54 entries accounted for.
- Corridor Operations: 18/18 entries accounted for.
- Freshly researched: 71/71 assigned entries.
- Completed research incorporated: Milestone (PR09).
- Missing or duplicate original terms: 0.
- Every original term includes a recommendation, meaning, rationale, alternatives, source basis, and bounded research scope.

Glossary snapshot fingerprints:

| File | SHA-256 |
| --- | --- |
| CONTEXT.md | 4d0b71053975469ff94cdcaa3f021275b3d9167ac3747d863d8a6eda73fb477d |
| docs/operations/CONTEXT.md | a7ae277eb6d59e9a18a6937bf55737cf4e0ae3eaf69acd7461f10f6235f348a8 |

## Primary-source index

The links in each term identify the relevant passage. This index records source titles and editions without treating every source as a universal standard.

- [Lean Construction Institute glossary][s001] — Undated live glossary; checked 2026-08-27.

- [Lean Project Consulting, Last Planner System Essentials][s002] — Copyright 2005, 2011; hosted by LCI UK.

- [VDOT Utility Manual of Instructions][s003] — 12th edition, June 2024.

- [FHWA, SUE Then and Now][s004] — Historical ASCE 38-02 discussion; live page checked 2026-08-27.

- [FHWA Guidelines for Virtual Transportation Management Center Development][s005] — FHWA-HOP-14-016, 2014.

- [FTA Oversight Procedure 39][s006] — October 2023.

- [Iowa Administrative Code 761, chapter 115: Utility Accommodation][s007] — IAC supplement 2026-06-24; definitions effective 2025-06-18.

- [TxDOT Project Development Process Manual][s008] — Current online section checked 2026-08-27.

- [Caltrans Construction Manual: Project Records and Reports][s009] — Current online manual checked 2026-08-27.

- [FHWA Construction Program Management and Inspection Guide][s010] — 2004 guide; current hosted copy checked 2026-08-27.

- [TxDOT project and portfolio management publications][s011] — Live catalog checked 2026-08-27.

- [FHWA SHRP2: Identifying and Managing Utility Conflicts][s012] — Live SHRP2 implementation page checked 2026-08-27.

- [Caltrans Plans Preparation Manual, chapter 2, section 1][s013] — September 2018, U.S. Customary Units.

- [TxDOT Glossary, S][s014] — Undated online entry checked 2026-08-27.

- [TxDOT Glossary, E][s015] — Undated online entry checked 2026-08-27.

- [Transport Scotland, A75 Springholm and Crocketford Improvements, Stage 1 glossary][s016] — Live project glossary checked 2026-08-27.

- [FHWA Avoiding Utility Relocations][s017] — Historical agency guidance, live copy checked 2026-08-27.

- [MDOT SHA Utility Procedures][s018] — Issued 2021-05-31.

- [AACE 10S-90: Cost Engineering Terminology][s019] — Online; checked 2026-08-27; entry dates below.

- [GSA 552.236-15: Schedules for Construction Contracts][s020] — March 2019 clause; GSAM effective 2026-06-13.

- [TxDOT: Time Impact Analysis Recordkeeper Job Aid][s021] — March 2025.

- [TxDOT SH 288 Technical Provisions][s022] — December 2014, RFP Addendum 8; project-specific contract.

- [Library of Congress: Extended Date/Time Format Specification][s023] — February 4, 2019.

- [Ballard and Tommelein: 2020 Last Planner System Benchmark][s024] — Lean Construction Journal 2021, pp. 53-155.

- [Autodesk Build: Issues References and Attachments][s025] — Undated; checked 2026-08-27.

- [Autodesk BIM 360: Issues in Field Management][s026] — Undated; checked 2026-08-27.

- [Autodesk Build: Create Issues][s027] — Undated; checked 2026-08-27.

- [FHWA: Work Zone Impacts Assessment During Construction][s028] — August 2006, FHWA-HOP-05-068; checked 2026-08-27.

- [Iowa DOT Design Manual 1C-8: Documenting Design Decisions][s029] — January 20, 2026 update.

- [NDDOT: Utility Coordination Special Provision Example][s030] — Revision 2019-09-27; official example hosted by NDSU UGPTI.

- [W3C, RDF 1.1 Concepts and Abstract Syntax][s031] — W3C Recommendation, 25 February 2014.

- [TxDOT, Standard Specifications, Items 1–10][s032] — 2024.

- [Caltrans Construction Manual, Section 5-4][s033] — December 2020; current online section accessed 27 August 2026.

- [FHWA, Federal-aid Essentials: Project Closeout][s034] — August 2012.

- [W3C, PROV-DM: The PROV Data Model][s035] — W3C Recommendation, 30 April 2013.

- [Four Rivers Nuclear Partnership / DOE Paducah, Document Control Process][s036] — FRev. 3B, effective 15 November 2023.

- [Caltrans Construction Manual, Section 3-5][s037] — November 2024.

- [WSDOT Construction Manual, Chapter 10: Documentation][s038] — M 41-01.50, August 2026.

- [NARA, Records Basics][s039] — Current online guidance, accessed 27 August 2026.

- [UK Government Digital Service, Record information about data sets you share with others][s040] — Published 7 August 2020; accessed 27 August 2026.

- [TxDOT, Utility Conflict Analysis Template][s041] — Official live workbook, accessed 27 August 2026; no edition asserted.

- [FDOT, SR 789 at Broadway Utility Conflict Matrix][s042] — Plans dated 20 November 2025.

- [WSDOT Contract 9424, Appendix U2: Existing Utility Listing][s043] — Form dated 30 April 2020; retrieved live 27 August 2026.

- [TxDOT Project Development Process Manual][s044] — Online; checked 2026-08-27.

- [Caltrans Source Inspection Guidelines][s045] — Online; checked 2026-08-27.

- [ALDOT Utility Accommodation][s046] — Online; checked 2026-08-27.

- [TxDOT Negotiated Contracts Policy Manual][s047] — Online; checked 2026-08-27.

- [Federal Acquisition Regulation][s048] — FAC 2026-01.

- [TxDOT Austin Utility Status Report Guideline][s049] — 2025-09-24.

- [USACE RMS Submittal Codes][s050] — Online; checked 2026-08-27.

- [FHWA Project Management Plan Guidance][s051] — May 2017.

- [CDOT US 36 Project Management Plan][s052] — 2012-02-29.

- [TxDOT Austin Utility Exception Request][s053] — 2024-09-10.

- [UFGS 01 45 00][s054] — 08/2023; Change 3, 08/2025.

- [GAO Schedule Assessment Guide][s055] — December 2015.

- [USACE ENG Form 4025][s056] — May 2017.

- [ISO/TC 176 guidance on documented information][s057] — ISO 9001:2015 guidance, N1286.

- [OpenLineage Object Model][s058] — Live specification, accessed 27 August 2026.

- [MLflow Tracking][s059] — Live documentation, accessed 27 August 2026.

- [MLflow Model Registry Workflows][s060] — Live documentation, accessed 27 August 2026.

- [Amazon Textract API: Prediction][s061] — Live documentation, accessed 27 August 2026.

- [Integrating Open Policy Agent][s062] — Live documentation, accessed 27 August 2026.

- [Construction Industry Council: Low Value Disputes Adjudication][s063] — Current page; UK Construction Act context, accessed 27 August 2026.

- [El-Yaniv and Wiener: Noise-free Selective Classification][s064] — JMLR 11, 2010.

- [OASIS XACML 3.0][s065] — OASIS Standard, 22 January 2013.

- [Amazon Textract Best Practices][s066] — Live documentation, accessed 27 August 2026.

- [Autodesk Docs: View Version History][s067] — Live documentation, accessed 27 August 2026.

- [UK BIM Framework Guidance Part C][s068] — Edition 1, September 2020; UK ISO 19650 guidance.

- [Oracle Financials: Purchase Order Carry Forward][s069] — Release 26B.

- [Open Policy Agent documentation][s070] — Live documentation, accessed 27 August 2026.

- [Open Policy Agent: Decision Logs][s071] — Live documentation, accessed 27 August 2026.

- [Google Cloud Document AI: Evaluate performance][s072] — Live documentation, accessed 27 August 2026.

- [RFC 8493: The BagIt File Packaging Format][s073] — Informational RFC, October 2018.

- [OMG Business Process Model and Notation][s074] — Version 2.0.2, January 2014.

- [TxDOT Schedule Guide for Transportation Development Projects][s075] — May2023.

- [GAO Schedule Assessment Guide GAO-16-89G][s076] — December2015.

- [FHWA CFL Guidelines for Developing CPM Schedules][s077] — October2006.

- [NJDOT Scheduling Manual for Design Projects][s078] — Page updated 2020-01-17.

- [Oracle Primavera Cloud Activity Types][s079] — Live help checked 2026-08-27.

- [TxDOT Glossary H][s080] — Live glossary checked2026-08-27.

- [USACE RMS Submittals and Transmittals][s081] — 22 April2022.

- [OpenLineage Run Cycle][s082] — Current specification documentation.

- [NIST AI RMF Core][s083] — AI RMF1.0 framework; current page checked2026-08-27.

- [MLflow Evaluation Datasets][s084] — Current first-party documentation.

- [NIST CSRC Audit Trail glossary][s085] — Current glossary, sources include CNSSI4009-2022 and SP800-53Rev5.

- [FHWA Computerization of Construction Record][s086] — 21 September1989; historical term usage.

- [W3C PROV Overview][s087] — Working Group Note,30 April2013.

- [DCMI Metadata Terms][s088] — Current reference; checked2026-08-27.

- [Oracle Primavera Cloud Activities Fields][s089] — Live help checked2026-08-27.

- [TxDOT Glossary R][s090] — Live glossary checked2026-08-27.

[s001]: <https://leanconstruction.org/glossary>
[s002]: <https://leanconstruction.org.uk/wp-content/uploads/2018/10/Last-Planner-System-Essentials-LPC.pdf>
[s003]: <https://www.vdot.virginia.gov/media/vdotvirginiagov/doing-business/technical-guidance-and-support/technical-guidance-documents/right-of-way-and-utilities/RWU-Utility-Manual-2024_acc09242024_PM.pdf>
[s004]: <https://www.fhwa.dot.gov/programadmin/history.cfm>
[s005]: <https://ops.fhwa.dot.gov/publications/fhwahop14016/ch3.htm>
[s006]: <https://www.transit.dot.gov/sites/fta.dot.gov/files/2024-11/Oversight-Procedure-39-Review-of-Third-Party-Agreements-for-Major-Capital-Projects.pdf>
[s007]: <https://www.legis.iowa.gov/docs/iac/chapter/761.115.pdf>
[s008]: <https://www.txdot.gov/content/txdotoms/us/en/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-2-row-and-utility-data-collection/6-2-2-utility-identification.html>
[s009]: <https://dot.ca.gov/programs/construction/construction-manual/section-5-1-project-records-and-reports>
[s010]: <https://www.fhwa.dot.gov/construction/cpmi04f1.cfm>
[s011]: <https://www.txdot.gov/business/resources/project-and-portfolio-management/ppm-publications.html>
[s012]: <https://www.fhwa.dot.gov/goshrp2/Solutions/PlanningEnvironment/R15B/Identifying_and_Managing_Utility_Conflicts>
[s013]: <https://dot.ca.gov/-/media/dot-media/programs/design/documents/cadd/ppm-text-ch2-sect1-a11y.pdf>
[s014]: <https://www.txdot.gov/manuals/itd/glo/s.html>
[s015]: <https://www.txdot.gov/manuals/itd/glo/e.html>
[s016]: <https://www.transport.gov.scot/publication/dmrb-stage-1-report-a75-springholm-and-crocketford-improvements/glossary>
[s017]: <https://www.fhwa.dot.gov/utilities/utilityrelo/5.cfm>
[s018]: <https://roads.maryland.gov/OOC/2021_MDOT_SHA_Utility_Procedures.pdf>
[s019]: <https://library.aacei.org/terminology/welcome.shtml>
[s020]: <https://www.acquisition.gov/gsam/552.236-15>
[s021]: <https://www.txdot.gov/content/dam/docs/division/cst/construction-recordkeeping/job-aids/ja-time-impact-analysis.pdf>
[s022]: <https://ftp.txdot.gov/pub/txdot-info/hou/sh288_toll_lanes/rfp/addendum-8/tp.pdf>
[s023]: <https://www.loc.gov/standards/datetime/edtf.html>
[s024]: <https://leanconstruction.org/wp-content/uploads/2022/08/LCJ_21_001.pdf>
[s025]: <https://help.autodesk.com/cloudhelp/ENU/Build-Issues/files/Issues_References_Attachments.html>
[s026]: <https://help.autodesk.com/cloudhelp/ENU/BIM360D-Field-Management/files/GUID-3DA3F94B-1EAC-4348-BC8B-EEE6BAECA04F.html>
[s027]: <https://help.autodesk.com/cloudhelp/ENG/Build-Issues/files/Issues_Create.html>
[s028]: <https://ops.fhwa.dot.gov/wz/resources/final_rule/wzi_guide/sec7.htm>
[s029]: <https://iowadot.gov/design-manual/chapter-1-general-information/1c-8-documenting-design-decisions>
[s030]: <https://www.ugpti.org/dotsc/prepguide/specprov/downloads/XXX(14)-Utility-Coordination-Example.pdf>
[s031]: <https://www.w3.org/TR/rdf11-concepts>
[s032]: <https://www.txdot.gov/content/dam/docs/specifications/2024/2024-specifications-items-1-10.pdf>
[s033]: <https://dot.ca.gov/programs/construction/construction-manual/section-5-4-disputes>
[s034]: <https://highways.fhwa.dot.gov/sites/fhwa.dot.gov/files/02projcloseout.pdf>
[s035]: <https://www.w3.org/TR/prov-dm>
[s036]: <https://pubdocs.pad.pppo.gov/Northwest%20Plume/Paducah%20Plans%20and%20%20Procedures%28updated%20August%205%2C%202025%29/CP3-OP-0025%20FR3B%20Document%20Control%20Process%20-%20Final%20Clean.pdf>
[s037]: <https://dot.ca.gov/programs/construction/construction-manual/section-3-5-control-of-work>
[s038]: <https://www.wsdot.wa.gov/publications/manuals/fulltext/M41-01/Chapter10.pdf>
[s039]: <https://www.archives.gov/records-mgmt/scheduling/basics>
[s040]: <https://www.gov.uk/guidance/record-information-about-data-sets-you-share-with-others>
[s041]: <https://www.txdot.gov/content/dam/docs/division/row/utl/utility-conflict-analysis-template.xlsx>
[s042]: <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015201-utility-conflict-matrix.pdf>
[s043]: <https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/Appendices/U/U2/U2-Existing%20Utility%20Listing.pdf>
[s044]: <https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process.html>
[s045]: <https://mets.dot.ca.gov/manuals/SIGLA>
[s046]: <https://www.dot.state.al.us/business/permits/uasuPermits.html>
[s047]: <https://www.txdot.gov/manuals/csd/ncp/standards_for_contracts/contracting_requirements-i1007955/contract_execution-i1009565.html>
[s048]: <https://www.acquisition.gov/far/4.102>
[s049]: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-guideline.pdf>
[s050]: <https://rms.usace.army.mil/datafiles/helpvideos/qcsabout/Advanced/Content/Topics/FAQ_COM_2/What%20do%20the%20Submittal%20Codes%20Mean.htm>
[s051]: <https://www.fhwa.dot.gov/majorprojects/pmp/guidance17.cfm>
[s052]: <https://www.fhwa.dot.gov/majorprojects/pmp/pmp_us36.cfm>
[s053]: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-exceptions-for-projects-guideline.pdf>
[s054]: <https://www.wbdg.org/FFC/NAVFAC/NDBM/UFGS/01_45_00.pdf>
[s055]: <https://www.gao.gov/assets/gao-16-89g.pdf>
[s056]: <https://www.publications.usace.army.mil/Portals/76/Publications/EngineerForms/Eng_Form_4025_2017May.pdf>
[s057]: <https://www.iso.org/files/live/sites/isoorg/files/standards/docs/en/iso_9001_2015_guidance_documented_information.pdf>
[s058]: <https://openlineage.io/docs/spec/object-model>
[s059]: <https://www.mlflow.org/docs/latest/ml/tracking>
[s060]: <https://www.mlflow.org/docs/latest/ml/model-registry/workflow>
[s061]: <https://docs.aws.amazon.com/textract/latest/APIReference/API_Prediction.html>
[s062]: <https://www.openpolicyagent.org/docs/integration>
[s063]: <https://www.cic.org.uk/services/adjudication/cic-low-value-disputes-adjudication>
[s064]: <https://jmlr.csail.mit.edu/papers/v11/el-yaniv10a.html>
[s065]: <https://docs.oasis-open.org/xacml/3.0/xacml-3.0-core-spec-os-en.html>
[s066]: <https://docs.aws.amazon.com/textract/latest/dg/textract-best-practices.html>
[s067]: <https://help.autodesk.com/cloudhelp/ENU/Docs-Files/files/file-folder-actions/View_Version_History_Docs.html>
[s068]: <https://www.ukbimframework.org/wp-content/uploads/2021/02/Guidance-Part-C_Facilitating-the-common-data-environment-workflow-and-technical-solutions_Edition-1.pdf>
[s069]: <https://docs.oracle.com/en/cloud/saas/financials/26b/faups/purchase-order-carry-forward.html>
[s070]: <https://www.openpolicyagent.org/docs>
[s071]: <https://www.openpolicyagent.org/docs/management-decision-logs>
[s072]: <https://docs.cloud.google.com/document-ai/docs/evaluate>
[s073]: <https://www.rfc-editor.org/rfc/rfc8493.html>
[s074]: <https://www.omg.org/spec/BPMN/2.0.2/PDF>
[s075]: <https://ftp.txdot.gov/pub/txdot-info/tpd/project-portfolio/schedule-guide.pdf>
[s076]: <https://www.gao.gov/assets/d1689G.pdf>
[s077]: <https://highways.fhwa.dot.gov/federal-lands/pddm/cfl/cfl-guide-develop-cpm.pdf>
[s078]: <https://nj.gov/transportation/eng/documents/scheduling/schedmanual.shtm>
[s079]: <https://docs.oracle.com/cd/E80480_01/help/en/user/88237.htm>
[s080]: <https://www.txdot.gov/manuals/itd/glo/h.html>
[s081]: <https://rms.usace.army.mil/datafiles/RMSBrownBag-Submittals-Transmittals-22APR2022.pdf>
[s082]: <https://openlineage.io/docs/spec/run-cycle>
[s083]: <https://airc.nist.gov/airmf-resources/airmf/5-sec-core>
[s084]: <https://mlflow.org/docs/latest/genai/datasets>
[s085]: <https://csrc.nist.gov/glossary/term/audit_trail>
[s086]: <https://www.fhwa.dot.gov/construction/contracts/890921.cfm>
[s087]: <https://www.w3.org/TR/prov-overview>
[s088]: <https://www.dublincore.org/specifications/dublin-core/dcmi-terms>
[s089]: <https://primavera.oraclecloud.com/help/en/user/95116.htm>
[s090]: <https://www.txdot.gov/manuals/itd/glo/r.html>
[r001]: <https://leanconstruction.org/glossary/>
[r002]: <https://leanconstruction.org.uk/wp-content/uploads/2018/10/Last-Planner-System-Essentials-LPC.pdf>
[r003]: <https://www.vdot.virginia.gov/media/vdotvirginiagov/doing-business/technical-guidance-and-support/technical-guidance-documents/right-of-way-and-utilities/RWU-Utility-Manual-2024_acc09242024_PM.pdf>
[r004]: <https://www.fhwa.dot.gov/programadmin/history.cfm>
[r005]: <https://ops.fhwa.dot.gov/publications/fhwahop14016/ch3.htm>
[r006]: <https://www.transit.dot.gov/sites/fta.dot.gov/files/2024-11/Oversight-Procedure-39-Review-of-Third-Party-Agreements-for-Major-Capital-Projects.pdf>
[r007]: <https://www.legis.iowa.gov/docs/iac/chapter/761.115.pdf>
[r008]: <https://www.txdot.gov/content/txdotoms/us/en/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-2-row-and-utility-data-collection/6-2-2-utility-identification.html>
[r009]: <https://dot.ca.gov/programs/construction/construction-manual/section-5-1-project-records-and-reports>
[r010]: <https://www.fhwa.dot.gov/construction/cpmi04f1.cfm>
[r011]: <https://www.txdot.gov/business/resources/project-and-portfolio-management/ppm-publications.html>
[r012]: <https://www.fhwa.dot.gov/goshrp2/Solutions/PlanningEnvironment/R15B/Identifying_and_Managing_Utility_Conflicts>
[r013]: <https://dot.ca.gov/-/media/dot-media/programs/design/documents/cadd/ppm-text-ch2-sect1-a11y.pdf>
[r014]: <https://www.txdot.gov/manuals/itd/glo/s.html>
[r015]: <https://www.txdot.gov/manuals/itd/glo/e.html>
[r016]: <https://www.transport.gov.scot/publication/dmrb-stage-1-report-a75-springholm-and-crocketford-improvements/glossary/>
[r017]: <https://library.aacei.org/terminology/welcome.shtml>
[r018]: <https://ftp.txdot.gov/pub/txdot-info/tpd/project-portfolio/schedule-guide.pdf#page=8>
[r019]: <https://www.gao.gov/assets/d1689G.pdf#page=27>
[r020]: <https://highways.fhwa.dot.gov/federal-lands/pddm/cfl/cfl-guide-develop-cpm.pdf#page=26>
[r021]: <https://nj.gov/transportation/eng/documents/scheduling/schedmanual.shtm>
[r022]: <https://docs.oracle.com/cd/E80480_01/help/en/user/88237.htm>
[r023]: <https://www.acquisition.gov/gsam/552.236-15>
[r024]: <https://www.txdot.gov/content/dam/docs/division/cst/construction-recordkeeping/job-aids/ja-time-impact-analysis.pdf>
[r025]: <https://www.fhwa.dot.gov/utilities/utilityrelo/5.cfm>
[r026]: <https://ftp.txdot.gov/pub/txdot-info/hou/sh288_toll_lanes/rfp/addendum-8/tp.pdf>
[r027]: <https://www.loc.gov/standards/datetime/edtf.html>
[r028]: <https://leanconstruction.org/wp-content/uploads/2022/08/LCJ_21_001.pdf>
[r029]: <https://help.autodesk.com/cloudhelp/ENU/Build-Issues/files/Issues_References_Attachments.html>
[r030]: <https://help.autodesk.com/cloudhelp/ENU/BIM360D-Field-Management/files/GUID-3DA3F94B-1EAC-4348-BC8B-EEE6BAECA04F.html>
[r031]: <https://help.autodesk.com/cloudhelp/ENG/Build-Issues/files/Issues_Create.html>
[r032]: <https://ops.fhwa.dot.gov/wz/resources/final_rule/wzi_guide/sec7.htm>
[r033]: <https://iowadot.gov/design-manual/chapter-1-general-information/1c-8-documenting-design-decisions>
[r034]: <https://www.ugpti.org/dotsc/prepguide/specprov/downloads/XXX(14)-Utility-Coordination-Example.pdf>
[r035]: <https://www.w3.org/TR/rdf11-concepts/#section-triples>
[r036]: <https://www.txdot.gov/content/dam/docs/specifications/2024/2024-specifications-items-1-10.pdf#page=33>
[r037]: <https://dot.ca.gov/programs/construction/construction-manual/section-5-4-disputes>
[r038]: <https://highways.fhwa.dot.gov/sites/fhwa.dot.gov/files/02projcloseout.pdf#page=2>
[r039]: <https://www.w3.org/TR/prov-dm/#term-Derivation>
[r040]: <https://pubdocs.pad.pppo.gov/Northwest%20Plume/Paducah%20Plans%20and%20%20Procedures%28updated%20August%205%2C%202025%29/CP3-OP-0025%20FR3B%20Document%20Control%20Process%20-%20Final%20Clean.pdf#page=20>
[r041]: <https://dot.ca.gov/programs/construction/construction-manual/section-3-5-control-of-work>
[r042]: <https://www.wsdot.wa.gov/publications/manuals/fulltext/M41-01/Chapter10.pdf#page=13>
[r043]: <https://www.archives.gov/records-mgmt/scheduling/basics>
[r044]: <https://www.wsdot.wa.gov/publications/manuals/fulltext/M41-01/Chapter10.pdf>
[r045]: <https://www.gov.uk/guidance/record-information-about-data-sets-you-share-with-others>
[r046]: <https://www.txdot.gov/content/dam/docs/division/row/utl/utility-conflict-analysis-template.xlsx>
[r047]: <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015201-utility-conflict-matrix.pdf>
[r048]: <https://roads.maryland.gov/OOC/2021_MDOT_SHA_Utility_Procedures.pdf>
[r049]: <https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/Appendices/U/U2/U2-Existing%20Utility%20Listing.pdf>
[r050]: <https://www.wsdot.wa.gov/publications/manuals/fulltext/M41-01/Chapter10.pdf#page=25>
[r051]: <https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process.html>
[r052]: <https://mets.dot.ca.gov/manuals/SIGLA/>
[r053]: <https://www.dot.state.al.us/business/permits/uasuPermits.html>
[r054]: <https://www.txdot.gov/manuals/csd/ncp/standards_for_contracts/contracting_requirements-i1007955/contract_execution-i1009565.html>
[r055]: <https://www.acquisition.gov/far/4.102>
[r056]: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-guideline.pdf>
[r057]: <https://rms.usace.army.mil/datafiles/helpvideos/qcsabout/Advanced/Content/Topics/FAQ_COM_2/What%20do%20the%20Submittal%20Codes%20Mean.htm>
[r058]: <https://www.fhwa.dot.gov/majorprojects/pmp/guidance17.cfm>
[r059]: <https://www.fhwa.dot.gov/majorprojects/pmp/pmp_us36.cfm>
[r060]: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-exceptions-for-projects-guideline.pdf>
[r061]: <https://www.wbdg.org/FFC/NAVFAC/NDBM/UFGS/01_45_00.pdf>
[r062]: <https://www.gao.gov/assets/gao-16-89g.pdf>
[r063]: <https://www.publications.usace.army.mil/Portals/76/Publications/EngineerForms/Eng_Form_4025_2017May.pdf>
[r064]: <https://www.iso.org/files/live/sites/isoorg/files/standards/docs/en/iso_9001_2015_guidance_documented_information.pdf>
[r065]: <https://openlineage.io/docs/spec/object-model/>
[r066]: <https://www.mlflow.org/docs/latest/ml/tracking/>
[r067]: <https://www.mlflow.org/docs/latest/ml/model-registry/workflow/>
[r068]: <https://docs.aws.amazon.com/textract/latest/APIReference/API_Prediction.html>
[r069]: <https://www.openpolicyagent.org/docs/integration>
[r070]: <https://www.cic.org.uk/services/adjudication/cic-low-value-disputes-adjudication>
[r071]: <https://jmlr.csail.mit.edu/papers/v11/el-yaniv10a.html>
[r072]: <https://docs.oasis-open.org/xacml/3.0/xacml-3.0-core-spec-os-en.html>
[r073]: <https://docs.aws.amazon.com/textract/latest/dg/textract-best-practices.html>
[r074]: <https://help.autodesk.com/cloudhelp/ENU/Docs-Files/files/file-folder-actions/View_Version_History_Docs.html>
[r075]: <https://www.ukbimframework.org/wp-content/uploads/2021/02/Guidance-Part-C_Facilitating-the-common-data-environment-workflow-and-technical-solutions_Edition-1.pdf>
[r076]: <https://www.w3.org/TR/prov-dm/>
[r077]: <https://docs.oracle.com/en/cloud/saas/financials/26b/faups/purchase-order-carry-forward.html>
[r078]: <https://www.openpolicyagent.org/docs>
[r079]: <https://www.openpolicyagent.org/docs/management-decision-logs>
[r080]: <https://docs.cloud.google.com/document-ai/docs/evaluate>
[r081]: <https://www.rfc-editor.org/rfc/rfc8493.html>
[r082]: <https://www.omg.org/spec/BPMN/2.0.2/PDF>
[r083]: <https://www.txdot.gov/manuals/itd/glo/h.html>
[r084]: <https://rms.usace.army.mil/datafiles/RMSBrownBag-Submittals-Transmittals-22APR2022.pdf>
[r085]: <https://openlineage.io/docs/spec/run-cycle/>
[r086]: <https://airc.nist.gov/airmf-resources/airmf/5-sec-core/>
[r087]: <https://mlflow.org/docs/latest/genai/datasets/>
[r088]: <https://csrc.nist.gov/glossary/term/audit_trail>
[r089]: <https://www.fhwa.dot.gov/construction/contracts/890921.cfm>
[r090]: <https://www.w3.org/TR/prov-overview/>
[r091]: <https://www.w3.org/TR/prov-dm/#term-Quotation>
[r092]: <https://www.dublincore.org/specifications/dublin-core/dcmi-terms/#isFormatOf>
[r093]: <https://www.dublincore.org/specifications/dublin-core/dcmi-terms/#format>
[r094]: <https://primavera.oraclecloud.com/help/en/user/95116.htm>
[r095]: <https://www.txdot.gov/manuals/itd/glo/r.html>
