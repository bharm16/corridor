# Incumbent and adjacent-product evidence for ADR-0075

Prepared 2026-09-02 for #556, the incumbent-evidence half of #486, under the
procedure in [docs/agents/domain.md](../agents/domain.md#research-before-proposing-terminology).
It records, for every product, program, and statistic that
[ADR-0075](../adr/0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md),
its research predecessor [ADR-0066](../adr/0066-the-machine-keeps-the-record-people-do-coordination.md),
the [competitive review of 2026-09-02](competitive-review-2026-09-02.md), and #486
name: the primary source, its date, what it claims, what it does not claim,
and the class of evidence it is. Every source was read on 2026-09-02; each
entry says which URL was read and, where the reading was a PDF, that the text
was extracted from the downloaded file.

**What this note does not do.** It does not decide who the buyer is. ADR-0083
records that consultant-first is a hypothesis; the buyer decision belongs to
#428 and the synthesis to #486. It does not amend ADR-0075; where a source
weakens a claim, the note says so and leaves the ADR body alone, as
[docs/adr/README.md](../adr/README.md) requires.

## Evidence classes used below

| Class | Meaning |
|---|---|
| **Official documentation** | A manual, user guide, design manual, or training page published by the agency that operates the system. Describes what the system does; says nothing about outcomes. |
| **Agency self-report** | An award submission, case study, or newsletter written by the agency about its own system, including any savings or delay figures. Not independently audited. |
| **Vendor self-description** | A product or marketing page written by the vendor. Feature lists are taken as claims, not as observed behavior. |
| **Release materials only** | The live product is authenticated, so workflow evidence comes from the operator's published release notes, newsletters, or guides rather than a hands-on session. |
| **Independent research** | NCHRP, GAO, SHRP2, or a state audit: a body that does not sell or operate the system. |
| **Trade press** | A journalist's article. Statements attributed to a vendor stay vendor claims; statements attributed to a named customer are customer self-reports. |

A statement is marked **unsourced** when no primary source was found today
and **open for #486** when only the interview work in #428 can settle it.

## Utility coordination systems

### PennDOT Utility Relocation Management System (URMS)

**Primary sources read.**

- *Publication 16, Design Manual Part 5 (DM-5), Utility Relocation*, September
  2023 edition with Change No. 1 dated 2025-09-29, Chapter 2 "Utility
  Relocation Management System (URMS)". PDF at
  https://www.pa.gov/content/dam/copapwp-pagov/en/penndot/documents/public/pubsforms/publications/pub-16m/pub%2016m.pdf.
  Official documentation.
- *URMS User Guide*, Release 5.1, issued 2022-07-08. PDF at
  https://www.pa.gov/content/dam/copapwp-pagov/en/penndot/documents/programs-and-doing-business/roadwaydesignenvironment/roaddesign/right-ofwayandutilities/utilityrelocation/documents/urms%20user%20guide.pdf.
  Official documentation.
- *URMS newsletter, Volume 1*, 2020-10-22. PDF at
  https://www.pa.gov/content/dam/copapwp-pagov/en/penndot/documents/programs-and-doing-business/roadwaydesignenvironment/roaddesign/right-ofwayandutilities/utilityrelocation/documents/1st%20urms%20newsletter.pdf.
  Release materials.
- *Utility Relocation Management System: Reducing Construction Delays and
  Costs*, PennDOT's NASCIO 2024 State IT Recognition Awards submission,
  project dates August 2017 to March 2023. PDF linked from
  https://www.nascio.org/awards-library/awards/utility-relocation-management-system/.
  Agency self-report.
- The URMS information page at
  https://www.pa.gov/agencies/penndot/programs-and-doing-business/road-design/right-of-way-grade-crossing-and-utilities/utility-coordination/urms-information
  returned only site navigation to both a fetch and a curl on 2026-09-02; its
  content is not relied on here.

**What the sources claim.** DM-5 Chapter 2 describes URMS as a web-based
design and construction collaboration application for Department personnel,
consultants, utilities, and contractors, and lists ten user roles including
"PennDOT Consultant" and "Utility Consultant/Contractor". DM-5's procedural
chapters route required steps through URMS: the district notifies utilities to
verify involvement through URMS, test-hole requests are made in URMS, the SUE
impact evaluation is completed in URMS, engineering authorizations and
notifications are issued through URMS, and supporting work must be identified
in URMS by the utility. The user guide's table of contents shows a
dashboard, upcoming tasks, active projects, exporting, notifications and
deadlines, project status progression, milestones, project health views,
payments, project documents with document versioning, a project checklist,
integration with PennDOT's One Map, consultant agreements, and a "Resolve
Conflicts" workflow with engineering authorization. The NASCIO submission
says the Utility Conflict Matrix "is at the heart of the URMS application",
that URMS expanded the SHRP2 model, that GIS calculates coordinates from
stationing, that agreements, permits, and clearances are generated from
UCM data, and that URMS receives construction site-activity updates from
PennDOT's ECMS and then sends notifications to utilities when a delay or
stoppage is attributed to a utility. It also says lists export to Excel.

**Statistics in the NASCIO submission (all agency self-report).** Cost
savings "of over $33 million through early identification of utility
conflicts"; "over $4 million" from administrative efficiencies; "zero
compensable utility delays for the 2022-2024 construction seasons";
approximately 90 percent of PennDOT projects require utility relocation;
over 500 utility companies and 2,250 of their staff registered; an itemized
automation table totalling $3,851,484 for November 2020 through 2024-05-01
computed as estimated hours saved per document type times a rate. The
submission also repeats a Texas A&M Transportation Institute 2016 estimate
that one month of design-phase utility delay costs $420,000 to $1.3 million;
that figure is quoted second-hand and was not verified against a TTI
publication today. The submission's "industry leader" and "no other product"
sentences are the agency's opinion of its own system.

**What the sources do not claim.** No source describes URMS reading an
incoming document, email, or meeting minutes, or comparing arriving evidence
against the conflict record. The site-activity notification is a structured
system-to-system event from ECMS, not document reading. No source describes
importing a customer's existing UCM spreadsheet as a baseline; the UCM is
authored inside URMS. No source states that consultants are *required* to
use URMS in those words; the requirement is implicit in DM-5 routing the
procedural steps through it, and PennDOT's own page language (per search
snippets not independently confirmed today) is that utilities and consultants
are "invited" to register. #428's "PennDOT mandates URMS" wording should be
read as "DM-5 procedures run through URMS", pending the interviews.

**Evidence class.** Official documentation for features; agency self-report
for every outcome figure; release materials only for the live workflow, since
the application is authenticated.

**Manages what Corridor deliberately does not.** Agreements and reimbursement,
permits (highway occupancy permits generated from UCM data), real property
interest requests, invoices and payments, formal approvals and authorizations,
generated clearance documents, GIS location, and role-based task management
across a statewide program.

**Reconciliation loop.** Not described. URMS is the system of record; the UCM
is maintained by users in its forms.

**Correction to ADR-0066's research.** ADR-0066 says URMS "has no chasing
loop: no reminder engine". The user guide lists notifications, tasks, and
deadlines, upcoming-task and project-health pages, and the NASCIO submission
describes automatic notifications on construction site-activity delays. The
narrower statement that survives is that URMS's reminders are driven by its
own workflow state and structured feeds, not by evidence read from documents.
ADR-0066 is superseded, so no edit is needed; #486 should not carry the
sentence forward.

### Kentucky Utilities and Rail Tracking System (KURTS), KURTS Mobile, and "KURTS 2.0"

**Primary sources read.** All are Kentucky Transportation Cabinet content
published through the Kentucky Transportation Center's Highway Knowledge
Portal (official documentation):

- *Tracking Utility Conflicts*, created 2024-09-30, updated 2025-12-16,
  https://kp.uky.edu/knowledge-portal/articles/tracking-utility-conflicts-3wh/.
- *Utility Relocation Field Inspections*, created 2024-11-08, updated
  2025-02-04, https://kp.uky.edu/knowledge-portal/articles/utility-relocation-field-inspections/.
- *KURTS Mobile Field Data Collection*, created 2025-08-28,
  https://kp.uky.edu/knowledge-portal/uncategorized/kurts-mobile-field-data-collection.
- *Project Management and Utility Coordination*, created 2024-06-05, updated
  2025-11-24, https://kp.uky.edu/knowledge-portal/articles/project-management-and-utility-coordination/.
- *Utility Engineering and Coordination* (time-management series), created
  2022-11-07, updated 2025-04-29,
  https://kp.uky.edu/knowledge-portal/articles/time-management-for-highway-project-development-15-utility-engineering-and-coordination/.
- The KURTS application landing page https://apps.transportation.ky.gov/KURTS/Default.aspx
  is an authenticated login and exposes no content. The KURTS user agreement
  PDF (https://transportation.ky.gov/RightofWay/Guidance%20Documents/KURTS%20User%20Agreement.PDF)
  is an access request form for public and private sector team members.

**What the sources claim.** KYTC keeps its Utility Conflict Matrix in KURTS
and describes it as the tool to identify, organize, analyze, track, manage,
and mitigate conflicts. The UCM is viewable in three forms: tabular,
individual conflict record, and spatial map with APWA colour coding. Utility
staff, design engineers, and project management personnel can add and review
conflict information from desktop or from KURTS Mobile, which is built on
ArcGIS Field Maps, works offline, and syncs to the KURTS database. KURTS
Mobile records relocation inspections (location, progress percentage,
status, photos, crew, materials, depth) and potential conflicts. KURTS also
generates and records U-phase estimates and funding requests, holds the
project utility contact list, accepts uploaded relocation plans and
estimates, and supports electronic invoice submission and agreement review.
The *Tracking Utility Conflicts* page says the matrix must be updated at
every project stage, including after coordination meetings and when new
utility information arrives.

**What the sources do not claim.** No page describes KURTS reading an email,
a set of minutes, or a document and deriving a change to a conflict record;
updates are entered by people. No page mentions Excel import of an existing
matrix. No public KYTC page found today uses the name "KURTS 2.0"; searches
for that term returned only the user agreement and unrelated KYTC pages. The
competitive review's "KURTS 2.0" label is therefore **unsourced** here; the
functionality it attributes to "KURTS 2.0" (mobile field capture, offline
inspections, spatial UCM) is all documented under the names KURTS and KURTS
Mobile.

**Evidence class.** Official documentation. No outcome statistics were found.
Release materials only for the live workflow, since the application is
authenticated.

**Manages what Corridor deliberately does not.** GIS conflict authoring and
map symbology, mobile field inspection with photos, U-phase funding requests
and estimates, invoices, agreements, and the utility contact list.

**Reconciliation loop.** Not described. The instruction to update the matrix
"at every stage" is addressed to people.

### Michigan DOT URTS with the R15B Utility Conflict Matrix and Tracking modules

ADR-0075's context sentence names "Michigan's module". Sources read:

- *Better Managing Utility Conflicts in Michigan through the SHRP2 Solution:
  Identifying and Managing Utility Conflicts (R15B)*, AASHTO/FHWA SHRP2 case
  study, undated but describing a March 2016 webinar and April 2016 training
  as "earlier this year". PDF at
  https://shrp2.transportation.org/documents/home/MichiganDOTR15BCaseStudyFINAL.pdf.
  Agency self-report published by the program sponsor.
- MDOT's published SHRP2 R15B Utility Conflict Matrix workbook, which still
  answers HTTP 200 as `application/vnd.ms-excel` (72,192 bytes) at the URL
  ADR-0066 cites. Official documentation.

**What the case study claims.** MDOT's Utility Relocation Tracking System
(URTS) had been in use for 20 years; SHRP2 implementation assistance awarded
in 2014 funded embedding the R15B UCM into URTS, an interface between URTS and
MDOT's ProjectWise document management system, limited external access for
utility companies and design consultants, and a new "Tracking" module that
records which utility companies were contacted, when, and who responded. A
custom reporting feature ran over budget and schedule. This supports
ADR-0066's sentence about Michigan adding a module to record contacts and
responses.

**What it does not claim.** No results or savings figures. No description of
reading documents or reconciling them with the matrix.

**Evidence class.** Agency self-report (quotes from one MDOT specialist)
published by the program sponsor.

**Manages what Corridor does not.** GIS presence lookup, document management
through ProjectWise, contact-and-response logging.

**Reconciliation loop.** Not described.

### UTrak (BEM Systems)

**Primary source read.** Vendor product page https://bemsys.com/utrak/, fact
sheet marked updated November 2025, copyright 2000-2026. Vendor
self-description.

**What it claims.** A "Utility Tracking Management System" covering
identification of utility owners within project limits, utility
investigation, conflict identification, site plans, agreements with utility
companies and municipalities, permit tracking and permit document generation,
SUE process tracking, work authorizations and modifications, reimbursable
costs and invoice monitoring, relocation and construction schedule
prioritization, ArcGIS mapping with parcel attributes and as-builts, and a
master list of utility companies. The page says the system "has been proven
to be of significant value" and names no customer, statistic, or price.

**What it does not claim.** Reading documents or email, detecting changes
against existing records, importing an existing matrix as a baseline.

**Evidence class.** Vendor self-description only. No agency documentation of
a UTrak deployment was found today.

**Manages what Corridor does not.** The full relocation lifecycle: owner
directory, permits, SUE, agreements, costs, invoices, GIS.

**Reconciliation loop.** Not described.

### Delasoft Utility Collaboration Manager

**Primary sources read.** Vendor product page
https://www.delasoft.com/utility-collaboration-manager and the DOT solutions
page https://www.delasoft.com/dot-solutions. Vendor self-description.

**What it claims.** Customizable workflows, Esri ArcGIS integration for
"visual tracking of route activities and permit segments", an interactive
utility company directory, streamlined permit review and task delegation,
and communication with utility companies. The DOT solutions page shows state
seals (Wisconsin, Louisiana, South Carolina, Arizona, Virginia, North
Carolina, Delaware, Oregon) for Delasoft as a company, not for this product
specifically. Testimonials are unattributed to metrics ("countless hours").

**What it does not claim.** Reading documents or email, change detection,
baseline import, any outcome statistic, any price.

**Evidence class.** Vendor self-description only. The competitive review's
judgement that the screenshots are too generic for a usability rating stands.

**Manages what Corridor does not.** Permits and permit review, delegated
workflow design, directory, GIS.

**Reconciliation loop.** Not described.

## Adjacent document-control systems

### Oracle Aconex

**Primary sources read.**

- Product page https://www.oracle.com/construction-engineering/aconex/ (read
  via curl; the site refuses automated fetches). Vendor self-description.
- Help article *Compare files in the Online Viewer*,
  https://help.aconex.com/workflows/compare-files-during-a-review/. Official
  product documentation.
- *Four principles of document revision management*, Oracle blog, 2020-06-01,
  https://www.oracle.com/construction-engineering/aconex/four-principles-of-document-revision-management/.
  Vendor guidance.
- Press release *New Oracle Aconex Capabilities Improve Project Transparency
  and Control*, Austin, 2026-04-13,
  https://www.prnewswire.com/news-releases/new-oracle-aconex-capabilities-improve-project-transparency-and-control-302739966.html.
  Vendor self-description.

**What they claim.** A single document register with version control, an
"unalterable audit trail", correspondence and workflow modules, model
coordination, Primavera schedule integration, and per-organization data
ownership. File Comparison compares the current PDF version with any previous
version of the same document and highlights text or drawing differences;
PDF only, scanned files unsupported. The April 2026 release adds Document
Process comment management with an automated Review Matrix, Inspection and
Test Plans, and an Observation capability. The revision-management blog is
about numbering and revision-coding discipline.

**What they do not claim.** No AI feature was announced in the April 2026
release. File Comparison is a visual diff between two versions of one file;
nothing describes deriving a structured change to a record from that diff,
nor comparing a document against a register of commitments.

**Evidence class.** Vendor self-description and official product
documentation. No independent outcome evidence was sought; Aconex is
adjacent, not a UCM competitor.

**Manages what Corridor does not.** Document control, transmittals,
contractual correspondence, review and approval workflow, model
coordination, inspection and test plans.

**Reconciliation loop.** Not described. The nearest capability is a visual
two-version diff of a single PDF.

### Bentley ProjectWise

**Primary sources read.**

- *New in ProjectWise Explorer 2026.0.0*, Bentley product documentation,
  https://docs.bentley.com/LiveContent/web/ProjectWise%20Explorer-v2026/ReadMe/en/topics/757583/pwe-wn_2026_0_0.html.
  Official product documentation.
- *New in ProjectWise Web*, release notes November 2021 to March 2026,
  https://docs.bentley.com/LiveContent/web/ProjectWise%20Web%20and%20Drive-vlatest/Help/en/topics/2243324/GUID-7DAAE4A6-71BB-4E79-A415-370E89C132B8.html.
  Official product documentation.
- Press release *Bentley Systems Advances Infrastructure AI with New
  Applications and Industry Collaboration*, Amsterdam, 2025-10-15, read via the
  Yahoo Finance syndication
  https://finance.yahoo.com/news/bentley-systems-advances-infrastructure-ai-075300966.html
  because bentley.com serves a sign-in shell to automated readers. Vendor
  self-description.
- ENR, *Bentley Unveils Platform Upgrades; Redoubles AI Investment*,
  2025-10-23, https://www.enr.com/articles/61673-bentley-unveils-platform-upgrades-redoubles-ai-investment.
  Trade press.
- AEC Magazine, *Bentley Systems shapes its AI future*, 2025-12-02,
  https://aecmag.com/ai/bentley-systems-shapes-its-ai-future/. Trade press.

**What they claim.** The 2026.0.0 Explorer notes list "Google-like
AI-enhanced searches", cross-datasource search, and Bentley Copilot
integrated with search results, alongside bookmarks, a tabbed interface, a
dashboard, and Office co-authoring. The October 2025 release announces
AI-powered search in ProjectWise that returns AI-generated summaries without
opening files, early access December 2025 and general availability in 2026.
ENR and AEC Magazine report the same search and Copilot capabilities.
ProjectWise's long-standing version and revision handling is documented in
the "Working with Versions" help.

**What they do not claim.** None of the five sources says that ProjectWise AI
flags document revisions or identifies what changed and where. The
ProjectWise Web release notes through March 2026 contain no AI, Copilot, or
change-detection item. The competitive review's sentence "ProjectWise now
advertises AI that flags document revisions and identifies what changed and
where" is therefore **unsourced** as of today. The closest sourced claim of
that shape belongs to Trunk Tools (below), not Bentley. If the review's
author saw such a Bentley page, its URL should be added here; the search
snippet phrase "automated clash detection and version compare" could not be
located on a page that automated readers can open.

**Evidence class.** Official product documentation and vendor
self-description; trade press corroborates only the search and Copilot
features.

**Manages what Corridor does not.** Engineering document control, CAD
integration, versions, workflows, access control, portfolios.

**Reconciliation loop.** Not described.

## Adjacent construction-AI products

### Trunk Tools

**Primary sources read.**

- Product page https://trunktools.com/product/, undated. Vendor
  self-description.
- ENR, *Trunk Tools Launches Cortex AI Platform to Interpret Construction
  Drawings*, 2026-06-17,
  https://www.enr.com/articles/63178-trunk-tools-launches-cortex-ai-platform-to-interpret-construction-drawings.
  Trade press, with a named customer.

**What they claim.** Agents for submittal discrepancy detection, drawing
revision review ("clouded and unclouded changes" with visual overlays and a
sheet-linked list), jobsite Q&A with cited sources over documents, drawings,
schedules, RFIs, and meeting minutes, RFI checking, and bid analysis. The
product page's "$10,000+" rework figure is a vendor illustration. In ENR the
CEO says of revised drawing sets, "We can tell you exactly what changed", and
that Cortex analyzes how drawing changes affect related project
documentation; those are vendor claims. Cleveland Construction, a customer,
reports submittal review time falling from about two hours to under ten
minutes and average submittal cycle time from 55 to 13 days; those are
customer self-reports, unaudited. Gilbane's deployment is reported by ENR.

**What they do not claim.** Any utility coordination, UCM, agency workflow,
or maintaining a coordination record; the object of comparison is the drawing
set and the specification, and the user is a general or trade contractor.

**Evidence class.** Vendor self-description plus trade press carrying
customer self-reports. Funding figures ($40M Series B, 2025) appear in the
vendor's own announcement and were not verified independently.

**Relevance.** This is the one product in the set whose public claim is
"detect what changed between revisions and trace the effect on related
documents". It is the reason the competitive review says change detection is
not a moat by itself. It does not touch an accepted coordination record.

### Pelles

**Primary sources read.** Product pages https://www.pelles.ai/ (statistics
note "last updated August 2026") and https://www.pelles.ai/pelles-core
(marked May 2026). Vendor self-description.

**What they claim.** Four applications for trade contractors: DoubleCheck
(cited Q&A across drawings, specs, contracts, schedules, addenda), Compare
(matches sheets across revisions and addenda and summarizes what changed in
the user's scope), Workflows (contract review, quote comparison, schedule
building, submittal preparation from templates), and Organization Agents.
Statistics: "3,000+" projects, "85,000+" issues caught early, "100h+" hours
saved per project, and a customer quote of 70-80 percent review time saved;
all vendor-published, one attributed to a named customer.

**What they do not claim.** Utility coordination, agency use, or maintaining
a coordination record. Target users are MEP and specialty contractors and
self-performing GCs.

**Evidence class.** Vendor self-description; customer quotes are vendor-
selected testimonials.

### Document Crunch

**Primary sources read.** https://www.documentcrunch.com/crunch-ai and
https://www.documentcrunch.com/construction-contract-review, copyright 2026.
Vendor self-description.

**What they claim.** CrunchAI reads whole contracts, specifications, and
insurance documents; flags unfavourable clauses and hidden duties; connects
across documents ("the spec contradicts the contract"); cites page and
clause; integrates with Word for redlining. Statistic: "Users often report
reducing review times by up to 80%". Named customers appear as logos and
testimonials.

**What they do not claim.** Revision-to-revision change detection,
reconciliation against a structured register, utility coordination, or agency
use. The product is contract risk review for commercial construction.

**Evidence class.** Vendor self-description only.

## Statistics quoted by ADR-0075, ADR-0066, the competitive review, and #486

ADR-0075 itself quotes no statistic; it inherits ADR-0066's research and
states that every published ROI number in the category is self-reported. The
table covers every figure the positioning work relies on.

| Statement as used | Primary source and date | What the source actually says | Class | Status |
|---|---|---|---|---|
| URMS "credited with $33M+ savings" (ADR-0066) | PennDOT NASCIO 2024 submission | "cost savings of over $33 million through early identification of utility conflicts", plus "$4 million" administrative and an itemized $3.85M automation table; method is estimated hours times rate, computed by PennDOT | Agency self-report | Cited; must be labelled self-reported wherever repeated |
| "Zero compensable utility delays 2022-2024" | Same | Stated as fact by PennDOT; no audit reference | Agency self-report | Cited; self-reported |
| GAO 1999: "22 states with utility delays on more than 10% of projects" (ADR-0066) | GAO/RCED-99-131, 1999-06-09, https://www.govinfo.gov/content/pkg/GAOREPORTS-RCED-99-131/pdf/GAOREPORTS-RCED-99-131.pdf, pp. 5-7 | 42 states answered for FY1997-98; 20 reported delays on 0-10 percent of projects, 8 on 11-20, 6 on 21-30, 8 above 30; 9 gave no estimate. 8+6+8 = 22 above ten percent | Independent research | Cited and arithmetically correct; note the 9 non-responders and that the figures are state estimates |
| GAO 1999: "states largely unable to track the delays at all" (ADR-0066) | Same, p. 7 | GAO says the full extent "is not known" and gives Connecticut as an example of a state that does not keep track of all delays; it does not generalize to "largely unable" | Independent research | Overstated; the defensible sentence is that GAO found the reported figures understate delays because at least one state counted only documented delays |
| NCHRP 2024 synthesis calls construction-phase conflict management "a substantial gap" (ADR-0066) | NCHRP Web-Only Document 396, *Strategies to Address Utility Issues During Highway Construction*, Quiroga, Naranjo, Shukla, Cooper (TTI and HDR), 2024, NCHRP Project 15-69, Summary, https://www.nationalacademies.org/read/27859/chapter/2 | The sentence is that "a substantial knowledge gap remains" about managing utility conflicts during construction | Independent research | Cited; the source says knowledge gap, and the document is a Web-Only Document, not a Synthesis |
| NCHRP 2024 change-order cause figures (#486) | NCHRP Research Report 1110, *Minimizing Utility Issues During Construction: A Guide*, same authors, 2024, Chapter 2, https://www.nationalacademies.org/read/27860/chapter/3 | Over 150,000 change-order and claim records from six unnamed state DOTs; 11,803 classified utility-related by AI models; of those, 32.7% errors and omissions in PS&E, 22.8% inaccurate or incomplete utility data, 12.4% owner-, contractor-, or utility-initiated changes, 11.2% utility owner scheduling delays, 4.2% differing site conditions, 3.7% constructability, 2.2% deficient relocation work, 1.9% right-of-way delays, 9.0% other; "56 percent" for the top two. A separate literature figure: a little over 5 percent of one DOT's change orders were utility-related (FDOT, 5% of 2,616) | Independent research | Cited; when quoting, say the percentages are shares of utility-related change orders, not of all change orders, and that classification was model-assisted |
| WOD 396 practitioner survey | Same WOD 396 Summary | 194 responses, 192 from 44 states and 2 from Canada, across owners, consultants, contractors, and utility owners | Independent research | Available for #486 if a survey base is needed |
| "FHWA's current EDC-8 round (2026-27) still leads with utility delays" (ADR-0066) | https://www.fhwa.dot.gov/innovation/everydaycounts/edc_8/ | Six innovations; Subsurface Utility Engineering is one; the page says unexpected utility hits are a major cause of delays, added costs, and safety risks | Official documentation | "Leads with" is unsupported; the defensible statement is that SUE is one of six EDC-8 innovations and FHWA names utility hits as a major delay cause |
| SHRP2 lessons learned "retreated to 'when in doubt, pursue a standalone UCM implementation'" (ADR-0066) | *Identifying and Managing Utility Conflicts R15B Lessons Learned*, AASHTO, 2019-05-08, https://shrp2.transportation.org/Documents/Renewal/Utilities%20final%20documents/SHRP2%20R15B%20Lessons%20Learned%2005_08_19.pdf | Heading reads exactly that; the stated reason is IT engagement and scheduling difficulty in pilot timeframes, and the document says IT solutions "should always be an option". A separate heading says upfront costs include staff to "populate and maintain utility conflict lists" | Independent research (program sponsor) | Cited; ADR-0066 joins two separate findings. The standalone advice was not justified by staff burden in the source |
| "The SHRP2 'advanced UCM' database was never deployed" (ADR-0066) | Same | The R15B product included a standalone conflict-list template, a utility conflict data model and database, and a training course; 18 state DOTs received grants, and implementations ranged from standalone lists to enterprise system modules. The document does not say whether any state deployed the R15B database itself | Independent research | Not supported as written; the source neither confirms nor denies deployment of the database, and enterprise modules were built (Michigan and Pennsylvania above) |
| Iowa DOT hands students an Excel template (ADR-0066) | *Utility Conflict Management (UCM)*, Iowa DOT class, Ames, 2025-10-06, slide deck https://utilities.iowadot.gov/Utility%20Conflict%20Management%20Training%202025/Utility%20Conflict%20Management%202025-10-06.pdf | Class materials list a "Utility conflict analysis template (Excel)" and exercises produce a conflict list in Excel | Official documentation | Cited |
| FDOT PMG 245 directs a conflict spreadsheet (ADR-0066) | *PMG 245 Utility Coordination*, FDOT Project Management Guide, 2023-12-01 | The EOR, DPM, and UAO identify conflicts and a tracking system is established "by creating a utility matrix or spreadsheet" | Official documentation | Cited |
| Michigan publishes its UCM as .xls (ADR-0066) | MDOT utility coordination page; the workbook URL answers 200 today | A 72 KB Excel workbook titled URTS Utility Conflict Matrix SHRP2 R15B | Official documentation | Cited |
| RIDOT audit found "no centralized way to monitor relocation progress" (ADR-0066) | *RIDOT Utility Relocations Oversight Final Audit Report*, RI Office of Internal Audit, 2022-10-04, https://omb.ri.gov/sites/g/files/xkgbur751/files/2022-10/RIDOT%20Utility%20Relocations%20Oversight%20Final%20Audit%20Report.pdf | Findings are about purchase orders approved late or missing backup, force-account estimates not itemized, invoice approvals, one instance of work not monitored by a resident engineer, change orders lacking backup, and work performed before change-order approval. No finding uses the words centralized or progress monitoring | Independent research (state audit) | Not supported by the source as phrased; the audit is about reimbursement controls. #486 should drop or reword it |
| TTI 2016: one month of design-phase utility delay costs $420,000 to $1.3 million | Quoted inside the NASCIO submission; TTI report not located today | Second-hand | Agency self-report quoting research | Uncited at the primary level; do not repeat without the TTI citation |
| "KURTS 2.0" (competitive review) | No KYTC source found | KURTS and KURTS Mobile are the documented names | — | Unsourced |
| "ProjectWise now advertises AI that flags document revisions and identifies what changed and where" (competitive review) | Bentley release notes and October 2025 press release | AI search and summaries; Copilot in search results; no change-detection claim | Official documentation | Unsourced as stated; the claim of that shape is Trunk Tools' |
| "Consultants and utility companies work inside URMS" (competitive review) | DM-5 Chapter 2 and the NASCIO submission | Roles for consultants exist; 2,250 utility staff and contractors registered (self-reported) | Official documentation, agency self-report | Cited |

## Does any public material describe the accepted-record reconciliation loop?

The loop Corridor targets is: adopt the customer's existing record as the
accepted baseline; capture arriving evidence (revised matrix, email, minutes,
schedule export) as source facts; compute typed differences from the accepted
record; have a person resolve them; and return the customer's own artifact at
a new revision.

**Finding.** None of the eleven sources sets reviewed describes that loop or
any three consecutive steps of it.

- URMS, KURTS, URTS, UTrak, and Delasoft UCM are systems of record in which
  people author and update conflicts. URMS's one automated inbound feed is a
  structured site-activity event from ECMS that triggers notifications; it
  does not read a document or propose a record change.
- Aconex File Comparison and Pelles Compare diff two versions of a file or
  sheet set. Trunk Tools additionally claims to trace a drawing change to
  affected specifications, RFIs, and submittals. All three operate on
  drawings and specifications for contractors, and none holds or updates a
  coordination record; the output is a highlighted diff or a list, not a
  proposed revision to an accepted register.
- Document Crunch reads contracts for risk; it does not compare revisions.
- ProjectWise's announced AI is search and summarization.

**Limits of the finding.** URMS, KURTS, UTrak, and Delasoft UCM are
authenticated or demo-only; their public materials may lag the product. The
review covered only the products the positioning work names; Procore,
Autodesk Construction Cloud, Bluebeam, Trimble, and the 811-ticket vendors
were not examined. Absence of a description is not evidence of absence.
The competitive review's own caveat, that this is not proof no incumbent has
private or newer capabilities, stands. What can be said is narrower and is
the claim ADR-0083 already sharpened: the public record of these systems
shows workflow, forms, GIS, permits, agreements, and file diffs, and does not
show cross-source reconciliation against an accepted coordination record;
that reconciliation remains manual in every documented workflow.

## Terms observed in the sources

Recorded under step 4 of the terminology procedure; no new term is proposed.

- **Utility Conflict Matrix (UCM).** Used by SHRP2 R15B (2019), PennDOT
  (NASCIO 2024), KYTC (2024-2025), MDOT (2016), and Iowa DOT (2025) for the
  tabular conflict record. FDOT PMG 245 (2023) says "utility matrix or
  spreadsheet". Corridor's use of "UCM" for the customer's accepted record
  matches this scope. Delasoft uses "UCM" as the abbreviation of its product
  name, Utility Collaboration Manager; the two must not be conflated in
  customer copy.
- **Supersede.** Aconex's term for replacing a document with a new revision
  in the register. Corridor's Proposed Delta "superseded" state (ADR-0083)
  means a newer source version coalesces an open delta, and CONTEXT.md also
  uses "superseded" for a Document Revision replaced by a later one. The word
  is the same in all three places; the object differs. No change is proposed.
- **Tracking module.** MDOT's name for a log of utility contacts and
  responses. Corridor's follow-up bundles (#425) are the nearest concept; no
  label change is proposed.
- **Revision and version.** ProjectWise distinguishes version labels from the
  active version; Aconex numbers versions within a revision code. Corridor's
  Project Record Revision is a record-level concept, not a file-level one.

## Open for #486

1. Whether any design partner runs URMS-, KURTS-, or UTrak-style systems and
   still maintains a separate consultant-side matrix, which is the situation
   ADR-0075's buyer hypothesis assumes. Only #428's interviews can answer it.
2. Whether "PennDOT mandates URMS" is experienced as a mandate by consultant
   firms (the #428 signal test).
3. A primary citation for the TTI 2016 delay-cost figure if #486 wants to use
   it.
4. A Bentley source for AI change detection, if one exists; otherwise the
   competitive review's sentence should be corrected in the synthesis.
5. Whether to drop the RIDOT sentence and the "advanced UCM database was
   never deployed" sentence from any restatement of ADR-0066's research.
