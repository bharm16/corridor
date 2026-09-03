# Competitive review, 2026-09-02

Received after #548's final status and the ADR-0084 through ADR-0086 contract pass. Its verdict is that Corridor should be the reconciliation and weekly-close layer beside URMS, KURTS, UTrak, Delasoft, and document control rather than another utility-management system. Its ticket consequences were applied the same day, with corrections recorded in the to-tickets pass: #555, #556, #557, #558, #559, #560, #561, #562, #563, #564 created; #425, #428, #486, #489, #494, #503, #509, #518, #519, #522, #526, #527, #528, #529, #532, #533, #535, #537, #548 amended; #459 reduced to a thin program map. Two recommendations were deliberately not applied as written: the four-artifact release set stays ADR-0086's until #560 decides it, and #548 stays open until ADR-0087's selected-gate clause is reconciled. Reproduced verbatim.

---

*Published KURTS conflict-management example: the incumbent mental model is a map of conflict objects, backed by individual records and forms. It is useful spatially, but it is not a weekly evidence-reconciliation experience.*

# Verdict
**Corridor is targeting the right market gap, but only if it refuses to become another utility-management system.**
PennDOT URMS, Kentucky KURTS, UTrak, and Delasoft already cover substantial portions of the conventional category: projects, utility owners, individual conflicts, GIS, schedules, permits, agreements, approvals, billing, contacts, documents, inspections, and workflow states. FHWA-sponsored programs also show that digital Utility Conflict Matrices and related databases have been pursued by multiple states for years.
Corridor's current roadmap is materially different:
> Adopt the customer's exact accepted record, observe later evidence, identify what would change, ask for the smallest coherent human decision, and return the customer's own updated issue package.
That is the correct product. The roadmap now encodes the full loop from baseline adoption through Proposed Deltas, packet review, accepted follow-up, and a revision-bound release package.
The clearest competitive framing is:
> **URMS, KURTS, UTrak, and Delasoft manage the state of utility coordination. Corridor should manage the change to that state.**
As a replacement for those systems, Corridor would look incomplete. As the reconciliation and weekly-close layer that plugs into them, it can be substantially better at the specific work coordinators still perform manually.

# What the existing systems actually provide
Public UI evidence is uneven. KURTS exposes useful training screenshots and descriptions. PennDOT's live URMS is authenticated, so its detailed workflow evidence comes primarily from official 2020–2021 release materials rather than a current hands-on session. UTrak and Delasoft descriptions are vendor claims, not independently measured usability evidence.

| System | Publicly visible product and UX model | Where it is stronger than Corridor | What Corridor should take from it |
|---|---|---|---|
| **PennDOT URMS** | A statewide, role-based project workflow. Public materials describe individual conflict pages, project checklists, affected/not-affected decisions, workflow notifications, health pages, contacts, agreement and invoice lists, schedule imports, generated documents, approvals, and Excel exports. Utility companies and consultants work inside the system. | End-to-end agency process control, external participation, agreements, reimbursement, formal approvals, generated relocation paperwork. | Role clarity, immutable approval history, external-system identifiers, and eventual import/export integration. Do **not** copy its module tree, numerous status forms, or conflict-by-conflict routine maintenance. |
| **Kentucky KURTS / KURTS 2.0** | The UCM can be viewed in tabular, individual-conflict, and spatial forms. Its map uses utility colors and conflict symbols. KURTS also supports mobile field capture and relocation inspections, including offline-oriented workflows. | GIS, location capture, field inspection, mobile use, and direct conflict authoring. | Add small, read-only spatial context to a decision when coordinates or stationing exist. Do not build a GIS editor or field-inspection platform for the first product. |
| **UTrak** | A broad commercial utility-relocation platform covering owner identification, investigation, conflicts, plans, agreements, permitting, SUE, work authorizations, costs, invoices, schedules, communications, and ArcGIS-backed mapping. | Complete relocation lifecycle, master utility directory, costs, permitting, communications, and GIS. | Preserve contacts and external facility identifiers during import. Integrate with exports rather than reconstructing this lifecycle. |
| **Delasoft Utility Collaboration Manager** | A configurable enterprise portal positioned around government procedures, utility-company communication, permit review, task delegation, ArcGIS, a utility-company directory, and customizable workflows. The publicly available screenshots are too generic to support a serious visual usability rating. | Agency customization, stakeholder directory, permits, delegated workflow, and enterprise integration. | A minimal project-contact source is necessary. A generalized workflow designer and permit system are not. |
| **Aconex and ProjectWise** | Adjacent rather than direct UCM competitors. They manage documents, revisions, correspondence, reviews, approvals, audit trails, portfolios, models, and formal project collaboration. ProjectWise now advertises AI that flags document revisions and identifies what changed and where. | Document control, contractual communication, external collaboration, drawing/model workflows, delivery maturity, and enterprise integrations. | Corridor must ingest from these systems and return authorized packages to them. "AI finds document changes" is no longer a defensible moat by itself. |

## The competitive truth
Corridor should be deliberately worse at GIS authoring, permits, reimbursement, inspection, document control, and general task management.
It should be dramatically better at:
- determining whether new evidence would change the **accepted** coordination record;
- putting routine revision changes into one safe batch instead of forty forms;
- consolidating several sources that bear on one actual coordination question;
- distinguishing accepted obligations from unresolved incoming claims;
- preserving the customer's workbook or system-export format; and
- producing one internally consistent issue package from one accepted revision.
In the public materials I reviewed, I did not find another system describing that complete semantic loop. That is not proof that no incumbent has private or newer capabilities. It is enough to show that Corridor should compete on this workflow rather than on generic utility tracking.

# Where the roadmap is already stronger than the incumbents
## 1. Baseline adoption is designed correctly
Issue #509 separates spreadsheet mechanics from project-authority decisions. Corridor operations handles formulas, hidden content, mappings, unsupported workbook features, row identity, and round-trip integrity. The coordinator sees only material project questions and adopts the complete baseline in one act—no row-by-row onboarding. That is considerably better than asking the user to rebuild the UCM inside a web form.
Keep that separation. It is one of the best decisions in the backlog.
## 2. #527 and #528 are the real product
Issue #527 turns a source revision containing many compatible exact changes into one bounded review, while exceptions remain separate. Issue #528 combines a revised UCM, minutes, email, and schedule evidence when they concern one real coordination question.
This is Corridor's strongest differentiator. It should not be presented as an implementation detail underneath a generic "change inbox." It should define the product:
> **Routine changes together; genuine questions separately.**
The normal unit in URMS- or KURTS-style systems is a project, conflict, form, or workflow task. Corridor's normal unit should be the **coherent decision**.
## 3. Accepted-authority follow-up is substantially better than a task list
Issue #425 correctly prevents an unresolved Proposed Delta from becoming an external obligation. It derives follow-up from accepted commitments, accepted dependencies, and explicit Follow-up Plans, then groups work by the recipient, coherent ask, and due window.
That avoids a common enterprise-software failure: converting every ambiguous system event into another task for a person.
## 4. The release package is a meaningful differentiator
Issues #529 and #533 bind the updated UCM, change summary, chase list, and weekly report to one accepted revision, cutoff, coverage state, predecessor, and template set. A later record decision makes a prepared candidate stale, and one authorized act seals the package.
That is stronger than four independent "Export" buttons. It also provides a clean handoff boundary to Aconex, ProjectWise, SharePoint, Box, or an agency portal.
## 5. The intended coordinator workflow is correctly attention-first
Issue #536 defines one project path—review, accepted follow-up, issue—with the project opening at the first incomplete section. Issue #537 gives a multi-project coordinator one primary next state per project and lets quiet projects consume no click.
That is the right answer to incumbents' project dashboards and module navigation. Corridor's home screen should be an **attention reading**, not a project-information portal.

# The principal roadmap problems
## 1. Real-customer learning is gated far too late
The current #535 requires nearly the complete accepted-record, review, follow-up, release, authorization, and portfolio workflow before real project bytes may enter authoritative processing. It also says isolated pre-activation partner data may not run extraction or create Proposed Deltas.
That is a waterfall gate disguised as safety.
It creates a serious risk: Corridor may spend months proving synthetic fixtures while learning only near the end that a partner's workbook, minutes, email conventions, identifiers, or source-revision behavior differ materially from the corpus.
Replace it with three explicit activation stages:
1. **Customer-approved compatibility intake**
   Real workbook or export bytes in an isolated environment; no accepted-record writes, no external release, and deterministic processing only unless the model-provider gate is complete. Output is a capability and mapping report.
2. **Non-authoritative shadow processing**
   Captured sources may produce frozen shadow Proposed Deltas for comparison, but cannot change an accepted record or produce a customer issue.
3. **Authoritative measured activation**
   The full #492/#520/#509 review, release, authorization, and measurement gates apply.
Safety remains intact. Product learning moves months earlier.
## 2. Market and workflow evidence arrives after too much product work
The roadmap places completion of #486 in Phase 3, although #486 says the primary-source incumbent record is still missing and that consultant-first remains a qualified hypothesis. #428 also explicitly says buyer, budget, procurement path, and consultant incentives are unresolved.
Incumbent research and workflow observation belong before the production UX is locked.
Expand this discovery work beyond interviews about willingness to pay. Observe actual weekly closes:
- what source arrives;
- how the coordinator decides what changed;
- what they search for;
- which artifacts really exist;
- who authorizes them;
- where the package is finally delivered; and
- how much of the work is billable versus merely administrative.
The existing eight-week pilot contract is an excellent measurement contract, but it is too late to function as formative product discovery.
## 3. Several issue contracts are already stale
The roadmap now references ADR-0084 through ADR-0086 and includes the packet, release, project-workflow, and portfolio tickets. The #459 program issue still describes ADR-0075 through ADR-0083 and an older Phase 2 that omits much of that product shape.
There are also contradictory dependency descriptions:
- #522 still says live-data activation is in #489.
- #489 explicitly says it is synthetic-only and that live activation moved to #535.
- #503's original dependency text still says it feeds production authorization in #489, while its amendment points to #531 and #535.
Do not keep a manually maintained second roadmap in #459. Reduce it to a short generated tracker or update it automatically from the authoritative roadmap and native dependency graph.
## 4. Measurement instrumentation is sequenced after the portfolio
#532 currently depends on #537 even though it defines events for source arrival, delta creation, packet use, decisions, follow-up, release preparation, repair time, and cost.
That is backward. Instrumentation should land with each primitive:
- source events with intake;
- delta events with #518;
- decision events with #519/#526;
- release events with #529/#533;
- portfolio-open and no-click behavior later with #537.
Split #532 into a core event contract and a later portfolio extension. Otherwise the team will retrofit analytics after the behavior has already shipped.
## 5. The follow-up workflow assumes a contact source that does not exist
UTrak and Delasoft explicitly include utility-company directories, and URMS contains utility contacts and service-area defaults. Corridor should not build a statewide directory, but #425 cannot produce a genuinely contact-ready bundle unless it can resolve at least a responsible role or project contact.
Add a narrow ticket:
> **Project contact source and recipient resolution for follow-up bundles**
For the pilot, it should import contacts from the adopted UCM, a small CSV, a connected mailbox directory, or a system export; permit an onboarding correction; retain the source and effective dates; and avoid becoming a generalized CRM.
## 6. Authorization stops one operation too early
#533 intentionally authorizes but does not deliver the issue. That is defensible for the first pilot, but the finished product still leaves a manual system transition: download files, find the correct destination, upload them, and possibly construct a transmittal.
Add a separate, partner-triggered issue after #533:
> **Handoff one authorized release to the partner-selected document system**
The first implementation could be a verified ZIP/download plus a SharePoint, Box, Aconex, or ProjectWise destination. It should transmit the already authorized bytes and receipt—not regenerate anything and not become document control.
## 7. The four-artifact package is still an assumption
The roadmap and #529 assume the updated UCM, change summary, chase list, and weekly report. That is valid only when the design partner already produces all four. The pilot contract correctly says Corridor cannot claim savings for work the partner did not perform before adoption.
There are two legitimate options:
- select pilot partners that already issue all four; or
- define the release candidate as the partner's configured required issue package, with the UCM mandatory and the other artifacts enabled only when they already exist in that workflow.
Do not manufacture a chase list solely so the pilot can claim it automated one.
## 8. Development velocity is now a roadmap risk
#548 records a regression from two executable migration revisions to 23, along with a roughly eight-minute PR gate and materially slower non-slow and slow suites. That tax will apply to nearly every upcoming spine, authority, delta, and release change.
Move #548 to the immediate frontier. It is not polish. It changes how quickly every product-risk hypothesis can be tested.

# Exact backlog changes I recommend
| Ticket | Recommended decision |
|---|---|
| **#428 and #486** | Pull the incumbent research portion of #486 into Phase 1. Add workflow observation and prototype testing to buyer interviews. Explicitly test whether the four-artifact workflow and consultant-first buyer are real. |
| **#548** | Start now, before the next migration-heavy core feature. |
| **#459** | Amend immediately through ADR-0086 and the current issue set, then reduce it to a generated or minimally maintained program index. |
| **#522** | Split customer-data handling from model-provider authorization. Point both to #535, not #489. |
| **#503** | Remove the stale #489 production-auth reference; preserve #531/#535 as the implementation and activation path. |
| **#489** | Remove #490 as a hard blocker on standing up a synthetic infrastructure shell. #490 should block enabling untrusted intake and later live-data stages, not creating containers, PostgreSQL, storage, logs, and backups. |
| **#535** | Split into compatibility intake, non-authoritative shadow processing, and authoritative measured activation. Remove #537 and complete release authorization as prerequisites for the first two stages. |
| **#532 and #537** | Remove #537 as a blocker for core analytics. Require #537 before the four-project measured cohort begins, not before the first controlled real-data shadow project. |
| **#494, #527, #528** | Make #494 the shared query and exactly-once-actionability shell. Do not ship an interim one-card-per-delta inbox. The first real review UI should be #527's source-revision batch, followed by #528's focused cross-source question. |
| **#425** | Add a child ticket for project contact resolution. Expand "contact-ready" to include a copy-ready subject, exact ask, current accepted position, affected conflicts, due date, and supporting references. Sending can remain out of scope. |
| **#529 and #533** | Add a later handoff adapter for authorized bytes. Keep preparation, authorization, and delivery as separate authority boundaries. |
| **#536 and #537** | Add a small shared front-end component contract and accessibility acceptance criteria. Do not create a large design-system program. |
| **New parallel ticket** | Add read-only spatial context and source-system deep links when stationing, coordinates, or an external record ID exist. Do not make it a pilot-entry blocker. |
| **#497, #450, #455, #456** | Keep strictly partner-triggered, as the roadmap currently says. Do not build every connector and source family in advance. |
| **#512, #513, #457, #458** | Keep parallel and outside the measured pilot. The current roadmap is correct not to make legacy retirement the customer-value gate. |
I would **not** weaken #492, #446, #509, #518, #519, #520, #526, #527, #528, #529, or #533. Those contain the authority, trust, review, and release behavior that makes Corridor more than another document-extraction interface.

# The UI Corridor should build
The finished product should feel like **a pull request and release pipeline for the accepted UCM**, not like a second utility database.
## 1. Portfolio: an attention list, not a dashboard
No map, charts, or generic project-health score should dominate this screen.
```text
THIS WEEK — September 2 cutoff
Project             Next required action                     Other current work
I-35 North          Review Aug 31 UCM revision               2 follow-ups due
SH-71 East          Resolve one conflicting date             —
US-290 Utilities    Issue package ready for authorization    1 follow-up due
FM-620               No action required                       —
```
Each project appears once. The primary state must say **why** attention is needed. A colored dot without the reason is insufficient.
Quiet projects can sit in a collapsed "No action required" section while still satisfying #537's exactly-once rule.
## 2. Project: one visible path
```text
Review  →  Follow up  →  Issue
```
Opening the project lands on the first incomplete section. The full Project Record, source history, and audit are one step away, but they are not prerequisite navigation.
This is exactly the direction in #536. Preserve it.
## 3. Routine source revision: a spreadsheet-density batch
```text
Revised UCM received August 31
182 unchanged
40 exact changes ready for review
4 held out for individual decisions
[✓] U-014  Promised for   Sep 15  →  Sep 29   Row 18
[✓] U-022  Status         Design  →  Approved Row 31
[✓] U-031  Owner          City    →  County   Row 47
...
```
The right pane shows the selected source row and accepted value. The bottom action reads:
> **Use 36 selected changes**
Not "Accept packet," "Resolve deltas," or "Run admission."
Exceptions such as ambiguous identity, apparent removal, conflicting sources, or uncertain scope appear as separate focused decisions.
## 4. Focused question: accepted position, evidence, consequence
The focused layout should have three conceptual columns:
```text
CURRENT ACCEPTED POSITION | NEW EVIDENCE | WHAT THIS WOULD CHANGE
```
For U-042, show the accepted commitment, each relevant UCM/email/minutes/schedule passage, and the affected UCM cells, follow-up, key date, and report sections. Contradicting passages remain side by side.
Primary actions should use project language:
- **Use the incoming value**
- **Keep the current value**
- **Edit and use**
- **Ask for clarification**
A dated **Snooze until…** remains secondary.
## 5. Spatial context: small and optional
Borrow KURTS's strongest idea without inheriting its product scope. On a focused conflict, show:
- station or offset;
- small read-only map;
- utility type;
- external GIS or system-of-record link; and
- affected project limits.
The map is context, not the workspace.
## 6. Follow-up: communication bundles, not tasks
Group by the interaction the coordinator will actually perform:
```text
Austin Water — confirm revised completion date by Sep 8
Affects: U-042, U-043
Current accepted position: October 15
Incoming statement: "work should be complete in late November"
Question: Confirm the committed completion date and affected facilities.
```
Provide **Copy brief** or **Create draft**. Do not introduce assignees, kanban columns, story points, arbitrary priorities, or a general task lifecycle.
## 7. Issue: one package with one readiness result
The Issue section should show all required artifacts and one textual readiness state:
```text
READY WITH DISCLOSED EXCEPTIONS
Accepted revision: 18
Source cutoff: Sep 2, 10:00 AM
Coverage: UCM, shared mailbox, schedule current
Open review items: 1 — does not change accepted values
✓ Updated native UCM
✓ Change summary
✓ Chase list
✓ Weekly report
```
The coordinator previews the package; an authorized releaser performs one authorization. Individual artifact downloads remain available, but they all belong to the same release identity.
## 8. Record and operations remain secondary
The current queue already contains several good interaction ingredients: split evidence panes, before/after differences, source crops, keyboard hints, sticky actions, and an explicit explanation of why an item is presented. Preserve those patterns. However, it is still titled "Proposed constraints," navigates individual cases, and contains test-manifest concepts that should disappear from the coordinator-facing product.
The dense ledger and long dependency-detail page should become search and investigation surfaces—not the project landing experience. The ledger also explicitly relies on color alone to indicate lateness, which must be fixed with text or iconography before the workflow is treated as accessible.
A small shared server-rendered component layer is sufficient. There is no reason to pause the product for a React rewrite or an enterprise design system.

# Recommended build order
## 1. Evidence and development speed
Complete the incumbent portion of #486, conduct real workflow observation under #428, prototype #527/#528/#536 with realistic fixtures, and land #548.
## 2. Safe core
Build #492, the minimal #487/#491 environment foundation, #530, #446, and #520. Resolve the PDF licensing decision in parallel.
## 3. Baseline and decision primitives
Build #509, #518, #519, and #526. Instrument each event as it lands rather than waiting for the final portfolio.
## 4. Real-data compatibility and shadow learning
Run one approved customer workbook and source through the isolated compatibility lane. Then enable non-authoritative shadow Proposed Deltas when the relevant governance and intake gates are complete.
## 5. The complete customer-value loop
Ship #527, #528, #425 with contact resolution, #495/#534/#529/#533, and #536. Build only the source class and connector the partner actually uses.
## 6. Multi-project measurement
Add #537 before the measured four-project cohort, finish #532 analytics, then run #499/#424/#498 against the predeclared pilot contract.

# Bottom line
Do **not** make Corridor more like URMS or KURTS.
Make it fit immediately beside URMS, KURTS, UTrak, Delasoft, Excel, Aconex, ProjectWise, SharePoint, and Box—reading what those environments produce, preserving their identifiers and formats, and returning an authorized update.
The north-star demonstration should be:
> A revised UCM, an email, and meeting minutes arrive. Corridor presents forty routine changes as one review, one contradiction as one focused question, one accepted clarification bundle, and one coherent customer package. The coordinator finishes the project's weekly close in under fifteen minutes without reconstructing context outside the product.
That is a defensible product. Another map-and-form utility portal is not.
