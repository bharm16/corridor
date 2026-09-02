# Where a real dated commitment can be found, and what we already hold

Researched 2026-08-07. The question was narrow: where can Corridor get source documents in
which a named external party states a date by which it will finish work, ideally published in
revisions so that a date moving later is visible by comparing two editions? The question was
asked because the SH 99 meeting-minutes corpus, which looked like the answer, is not: of the
1,629 event Candidates extracted from it, 24 carry a Committed Date, and reading those 24 shows
most of them are meeting logistics rather than relocation promises.

Two searches ran — one over what is already in the local database and corpus, one over the open
web — and the second was then re-run adversarially against the first, opening the documents
rather than trusting the titles. That third pass killed a substantial fraction of what the
second pass had reported, which is why several sources appear below under "dead ends" with a
note about what specifically failed.

## The answer

**The single best route is NCDOT's bid proposals, and no records request is needed.** North
Carolina publishes every letting's plans and proposals on an open IIS file server at
[xfer.services.ncdot.gov/dsplan/](https://xfer.services.ncdot.gov/dsplan/) — directory browsing
enabled, plain `curl` with no cookies and no user-agent spoofing, HTTP 200 at every level down
to the individual PDF. Inside each project proposal is a Project Special Provision titled
*Utilities by Others*, and where that provision carries dates it states them in exactly the form
Corridor needs. From Hoke County R-5709C, at
`2025 Highway Letting/01-21-25/Plans and Proposals/HOKE_R-5709C_C204992/Project Proposal Addendum No.2.pdf`:

> BrightSpeed will complete the relocation of their facilities by April 30th, 2025.
>
> Windstream will complete relocation of their facilities from station 310+00 - 441+00 -L- by June 2, 2025.

A named External Party, a calendar date to the day, a station range, and a declarative future
commitment. Jackson County R-5600 (letting 11-18-25) is denser still — eleven dated commitments
across six utilities, including Duke Energy committed phase by phase against station ranges
(*"Phase 2 starting on UO-5 STA. 34+75.00 to UO-6 STA. 48+80.00 by October 19, 2026."*) and five
per-company completion dates (*"Optimum has a completion date of January 26, 2027."*,
*"Frontier has a completion date of June 15, 2027."*).

The revision property holds too, though it is rare. A single letting folder retains only one
proposal PDF — all 117 project folders in the 2025 crawl hold exactly one — so revisions are not
retained in the ordinary case. But when a contract is **postponed to a later letting** it gets a
folder under both letting dates and both proposals survive. Guilford County U-4758, contract
C204971, is such a pair:

| | `08-19-25/.../Project Proposal.pdf` (provision footer 5/13/25) | `09-16-25/.../Project Proposal Addendum2.pdf` (footer 9/10/25) |
|---|---|---|
| Segra | Conflicts will be cleared by June 30, 2026. | Conflicts will be cleared by June 30, 2026, **except for line locations on Parcel 165 which will be cleared by January 31, 2028.** |
| Lumen | Conflict resolution date for areas 1 and 2 is **September 30, 2026**. | Conflict resolution date for areas 1 and 2 is **September 1, 2025**. |
| Duke Energy | Conflicts for Areas 1, 2 and 3 will be cleared by June 30, 2026. | Conflicts for Area 1 will be cleared by September 1, 2025. Conflicts for Areas 2 and 3 will be cleared by June 30, 2026. |

That is a nineteen-month slip on a carved-out parcel, a thirteen-month pull-in, and a scope
split, all between two public revisions of the same document, with the provision footer dates
versioning the pair cleanly. It is the only place in this entire investigation where an external
party's own stated completion date is seen moving between two published editions of the same
document.

**Be honest about how much of that exists: almost none.** Across five years crawled in full
(2022–2026, 491 distinct contract ids) only four contracts appear in more than one letting.
Three were opened; Guilford yields the movement above, Buncombe I-2513AA/AB has a byte-identical
*Utilities by Others* section between its two versions with no dates in it at all, and
Wake/Franklin I-6001 has no such section in either version. So the yield is roughly one usable
retrospective slip pair per several years — enough to build and test a revision comparison
against real data, not enough to be a dataset. Volume in slips has to come from polling project
folders forward, between advertisement and letting, which is a cheap cron job against a
browsable directory tree.

Volume in *dates*, as opposed to slips, is better: roughly 100 projects a year across twelve or
thirteen lettings, of which the non-resurfacing projects carry a *Utilities by Others* section
about 60% of the time and roughly a third of those sections carry at least one calendar date.
Call it fifteen to twenty-five date-bearing proposals a year for 2024 onward, each carrying
between one and eleven dated commitments.

Two limits on that estimate, both found by opening files rather than by reasoning about the
archive. The year folders go back to 2004 but the harvest window does not: a 2015 proposal
(Wilkes R-2603) has no *Utilities by Others* provision at all, and three 2022 proposals that do
have one carry names and contacts only, with a blanket *"The conflicting facilities of these
concerns will be adjusted prior to the date of availability"* and no calendar dates anywhere.
Explicit dates appear in the 2024, 2025 and 2026 samples. And the 2015-era addendum PDFs, which
looked like a second retained-revision channel, are scans — 52 of 53 harvested extract to under
120 bytes of text.

**The fastest route to dated commitments, though, is not a fetch at all.** Five genuine slip
events are already sitting in the local database, extracted, cited, verified, and never
adjudicated. That is the next section.

## What we already hold

Everything in this section was re-verified against the live database on 2026-08-07, not carried
over from the earlier passes.

### The WSDOT committed dates are WSDOT's own estimate, and the field name overstates them

This is the finding that matters most, because it is about data Corridor has already stored
under a name that means something stronger than the document says.

Corridor holds 44 Candidates from WSDOT contract 9424 carrying a `committed_date`, and they are
the largest such population in the database:

```sql
select p.slug, count(*) as candidates,
       count(*) filter (where nullif(c.payload_json->'fields'->>'committed_date','') is not null)
from candidates c join projects p on p.id = c.project_id group by 1 order by 1;

 fdot-sr789         |   66 |  0
 nhhip-3c2          | 4647 |  0
 sh99-grand-parkway | 3030 | 24
 wsdot-9424         |  162 | 44
 wsdot-9540         |  192 |  0
```

`CONTEXT.md` defines a Committed Date as *"The date an External Party stated it would deliver."*
The source column does not say that. The document is
`corpus/files/e6/e619a4abf6044ee17415d4f6933ec9ef674c67735befb172a8d6da5a691e4b9a.pdf`
(document id 21976,
[U2-Existing Utility Listing.pdf](https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/Appendices/U/U2/U2-Existing%20Utility%20Listing.pdf)),
and its page-1 title block reads:

```
Project Owner: WSDOT      Utility Conflict Matrix Developed/Revised By:   Tom Spangenberg
```

The column those 44 values come from is headed, stacked vertically over three lines,
`Relocation` / `Estimated` / `Date`, with `Relocation` / `Schedule` / `Status` beside it. So the
field is labelled *Estimated*, its author is a WSDOT employee, and there is no utility
signature, concurrence, or countersignature block anywhere on the sheet. A search of the whole
matrix for commitment language — *will complete*, *committed to*, *has agreed*, *agrees to*,
*shall complete* — returns nothing. Nobody at Highline Water District, Puget Sound Energy,
Comcast or CenturyLink ever asserted these dates; WSDOT estimated them.

The values are also thinner than the count suggests. There are fifteen distinct strings across
the 44 rows, and only 26 rows carry anything date-shaped:

```
N/A                                          14      Apr-20                 5
Don't have any relocation plans at this time  2      Summer 2020            4
Will be relocated with bridge construction    1      Jul-19                 4
Need scheduling coordination with ST contractor 1    Sep-19                 3
                                                     Summer 2019, Sep-20, Jun-19   2 each
                                                     Jul-20, May-20, Dec-20, Spring 2019   1 each
```

Granularity is month-year or a bare season. Fourteen rows say `N/A`. Four carry prose where a
date should be.

The conformed revision of this same appendix exists and is downloadable
([ConformedRFP/Appendices/U/U2/U2 Existing Utility Listing.pdf](https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/ConformedRFP/Appendices/U/U2/U2%20Existing%20Utility%20Listing.pdf)
— note the original uses a hyphen after `U2` and the conformed a space), and it does not help.
Comparing the estimated-date cell for all 148 conflict ids present in both revisions: 23 carry a
date or season, and all 23 are identical. Zero dates moved. The only structural change is that
14 rows are deleted outright (ids 48, 49, 50, 54, 135, 201, 202, 207, 223–227, 239 — the
conformed id sequence runs 45, 46, 47, 51, 52), taking the row count from 162 to 148.

**Consequence.** Ingesting the conformed revision as a second spine source would demonstrate
fourteen row deletions and no date change, which is not the capability the project is missing.
And the 44 stored values should not be presented as external-party commitments. Whether the
right fix is a distinct field, a source-scoped qualifier, or simply refusing to map this column
is an adjudication question, not an extractor question — but the current mapping asserts more
than
[the page](https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/Appendices/U/U2/U2-Existing%20Utility%20Listing.pdf)
states.

### The SH 99 minutes hold five slip events, and nobody has looked at them

The 24 SH 99 Candidates carrying a Committed Date are, as suspected, mostly not commitments:
thirteen of the 24 name LJA, the design consultant, and the quotes behind them are meeting
scheduling (*"LJA to send out bi-weekly meetings for Thursday's 1:00 pm to 1:30 pm starting
8/15."*, Candidate 5712), file transfers already completed (*"Complete, Peter Vinje emailed on
10/17."*, Candidate 6700), and workshop invitations (*"Kinder Morgan will be invited to the
TxDOT-Utility Owners-DB Proposers Workshop on May 8th, 2025."*, Candidate 7582). The single
closest thing to a real commitment is Candidate 7129, *"Equistar to provide a chain of title on
the ROW agreement that is in DOW's name (Due date of 01/2025)"* — a document delivery, coerced
to `2025-01-01`.

The valuable material was extracted under a different field and has therefore gone unnoticed.
`payload_json->'fields'->>'event_type'` on the 1,629 SH 99 event Candidates distributes as
commitment 862, response 499, status_change 207, closure 45, escalation 11, and **slip 5**.
Those five, with their verified citations, are:

| Candidate | External Party | Cited quote |
|---:|---|---|
| 7296 | Kinder Morgan | The March 2026 completion timeline seems unattainable. Propose extending to May 16th. |
| 7554 | Enterprise | ROW, Util Agreement, Execution – 7/2025 04/2026, Parcel 120 needed (currently 04/2026) |
| 7306 | Kinder Morgan | pushed on to 06/2026 but is dependent on receiving drawings in a timely manner. |
| 7099 | Energy Transfer | to ET 11/04. Complete Peter Vinje emailed the files through Mimecast on 12/02. |
| 7317 | LJA | ROW maps sheets are expected to be delivered with easements at a much later date than originally anticipated. |

The first two are the useful ones. Kinder Morgan 7296 is an external party moving its own
completion date later, in its own voice, in minutes Corridor already holds. Enterprise 7554 is
better still, because it is an inline redline — the old value and the new value on one line — so
the slip is extractable from a single document with no revision comparison required. And it
directly contradicts a Milestone already loaded: `milestones` row 70 for project 1266 is
`ROW-UTIL-EXEC`, need date `2025-07-31`, source `sh99-milestones.csv`, while the April 2025
minutes say that milestone moved to 04/2026. Row 71, `RELO-CONSTR`, need date `2026-03-31`, is
contradicted the same way by Kinder Morgan 7296.

A Need Date the project holds, plus a dated external contradiction of it already extracted and
cited, is a demonstrable slip using nothing but material in the database today. This is the
cheapest path to the capability by a wide margin — the acquisition cost is zero and the work is
adjudication, not ingest.

Candidate 7317 is worth naming as the counter-example: it is a slip in every ordinary sense and
it carries no date at all, so it cannot enter a dated ledger. Candidate 7099 is a retrospective
lateness rather than a forward commitment moving.

### The Ledger itself holds no date

```
dependencies                     17 rows (all project 84, NHHIP)
  committed_date not null         0
  need_date not null              0
dependency_events                 0 rows
candidates                     8,097   (158 accepted, 7,939 pending)
```

Every dated thing described in this document lives in `candidates`, unadjudicated. The revision
machinery is real and populated, and it is pointed at documents that carry no dates: NHHIP has
five genuine dated revisions of the same matrix (registry ids `nhhip-ucm-2025-06-20` through
`nhhip-ucm-2026-02-13`, chained through `superseded_by`) and **zero** Candidates with a
Committed Date across all 4,647. That is the exact inverse of what is needed.

### The rest of the corpus has no date field to find

Checked exhaustively rather than sampled. The FDOT SR 789 conflict matrix
(`corpus/fdot-sr789.yaml`, 66 Candidates) has no date column — the form's ten headers are
`Conflict No. | Sheet No. | Utility Agency Owner (UAO) | Facility Description | Station and Offset | LT/RT | Conflict | Vvh No. | Resolved? Yes/No | Comments/Resolutions`.
WSDOT 9540's 192 Candidates from six Appendix U3 listings carry none. The cross-agency set
carries none: the FDOT I-75 matrix is the deliberate no-conflicts empty case, the CDOT US 6
matrix has inter-utility sequencing prose but no dates, and the TxDOT Utility Conflict Analysis
Template is a blank form. The three SH 99 matrices are Utility Inventories whose only unmapped
column is `Early TxDOT Utility Activity`, blank in the rows read. Across all five dated NHHIP
revisions the twenty extracted field names include no date.

One loose end worth recording: `corpus/files` holds 191 files against 190 registered documents.
The unregistered one is the TxDOT Utility Conflict Analysis Template from
`corpus/cross-agency.yaml` — downloaded 2026-08-04, never ingested. It has zero data rows, so it
holds no dates, but its Data Dictionary is the canonical vocabulary this whole search was
hunting for: *"Estimated Resolution Date | Date that utility conflict is anticipated to be
resolved"* and *"Status Achieved Date"* on the Utility Conflicts sheet, plus project-level
*"Ready-to-let (RTL) Date"*, *"Letting Date"*, *"ROW Authorization Date"*. Nothing in the repo
can cite those terms today because the workbook was never ingested.

## What would have to be fetched, ranked by how fast it could put dated commitments into Corridor

**1. Nothing — adjudicate the five SH 99 slip Candidates.** Zero acquisition. The evidence is
extracted, the citations are verified, and two of the five are genuine external-party date
movements that contradict Milestones already loaded. The work is a decision, not a download.

**2. NCDOT *Utilities by Others* proposals.** Fully public, browsable, born-digital text for the
2024-onward window, high date density where dates appear, and one verified retrospective slip
pair. The retrieval recipe that was verified working: `GET https://xfer.services.ncdot.gov/dsplan/`,
then `<YEAR> Highway Letting/<MM-DD-YY>/Plans and Proposals/<PROJECT>/`, parse the IIS listing
for `<A HREF>` entries, skip any project whose folder name contains `CPT` (resurfacing contracts
never carry the provision, and proposals run 10–45 MB each), pull the single
`Project Proposal*.pdf`, and slice the text between the `PROJECT SPECIAL PROVISIONS / Utilities
by Others` heading and the next provision heading — Erosion Control follows it in every sample.
To find slips, key on the contract id (`C\d{6}`) across letting folders and diff any contract
appearing in more than one.

The extraction hazard is specific and worth writing into the prompt: the dominant phrasing in
these provisions is a **relative** milestone, not a date — *"Conterra will relocate their
facilities within the project limits by the Date of Availability."* (Onslow U-5789) — and every
page carries a provision revision footer (`1/7/2025`, `9/10/25`, `5/13/25`) that a naive date
regex will read as the commitment. In Onslow U-5789 a naive scan flags `4/11/25` next to
Conterra when the actual sentence contains no calendar date at all.

The strategic caveat is the one `../history/corpus-acquisition-spec-2026-07.md` §2 already states: a stream covering
a different project cannot be borrowed. NCDOT is a third project, with its own spine and its own
ledger, not an upgrade to NHHIP or SH 99.

**3. The SH 99 Utility Owner Coordination meeting notes, fetched properly.** Already registered
in `corpus/sh99-grand-parkway.yaml` and already the source of the five slips above, but the
retrieval is worth correcting because the parent archive is 1.12 GB and does not need to be
pulled whole. Box honours HTTP Range on
`https://app.box.com/index.php?rm=box_download_shared_file&shared_name=<hash>&file_id=f_1864505972449`
(returns 206 with `content-range`), and the meeting-notes member is stored uncompressed inside
the parent, so a single range request for bytes 33721707..121719304 yields a directly openable
88 MB ZIP. One trap for any ingester walking this archive: the parent also contains a nested KMZ
stored uncompressed, whose internal `PK` signatures appear *before* the parent's own central
directory, so a naive `rfind()` for the end-of-central-directory lands on the KMZ's and returns a
bogus single-entry `doc.kml` listing.

What the notes carry, verbatim, from
`Meeting Notes/Centerpoint Electric Transmission/2024.10.29 GPB1 CNP Elec Trans notes final.pdf`
under the heading *Project Timeline and Deadlines*:

```
1. Design Completion – 01/2025
2. ROW, Util Agreement, Execution – 7/2025
3. Relocation Construction Completion – 3/2026
```

132 of the 145 note PDFs carry such a block, across 15 owner folders, at biweekly-to-monthly
cadence from July 2024 to May 2025. Milestone labels are stable and machine-parseable
(`<Milestone> – MM/YYYY`), all 145 PDFs have a text layer, and dates demonstrably move: CenterPoint's
Design Completion goes 01/2025 (10/29 note) to 06/2025 (12/17 note); Energy Transfer's goes
01/2025 to 03/2025 and its Relocation Construction Completion passes through `TBD`.

**Read the attribution caveat before ingesting these as commitments.** The dated block is not a
utility-authored promise. It is introduced in the kickoff notes as *"Project Schedule / Important
Completion Dates"*, justified by TxDOT's own procurement milestone (*"Design must be in the TxDOT
RFP (April 2025), prior to the 'let date'"*), and the identical triplet 01/2025, 7/2025, 3/2026
appears in every owner's kickoff note. At kickoff it is one DOT need-date broadcast to fifteen
owners, not fifteen commitments. What redeems it is that the block is thereafter maintained
per-owner and diverges under the owner's own constraints — CenterPoint's 2024.08.06 note records
*"with current CNPE requirement for pre-payment on both agreements (Engineering and
Construction), design would not be feasible by this date and would push the dates below"*, and
the date subsequently moves. The right framing is *the schedule of record for a named utility's
relocation, as maintained by the DOT's coordinator*, which is still exactly the dependency
Corridor tracks — but it is not the utility speaking, and a cited system of record should not
say it is.

**4. Rockwall County Consortium Reports.** Monthly, predictable URL pattern
`https://www.rockwall.com/documents/ConsortiumReports/{YYYY}/{Month}%20{YYYY}.pdf`, 56 editions
returning HTTP 200 between February 2021 and July 2026, clean text layer. Each edition carries a
per-project block with a labelled field, and the same field on the same project moves across
editions. On CSJ 1017-01-015 the field `Utility Relocations Complete` reads January 2024 in the
Feb 2022 edition, June 2024 in Feb 2023, July 2026 in Aug 2025, and December 2026 in Feb 2026 —
a roughly 35-month slip visible by diffing one field on one project. The 2023–2024 editions also
carry per-owner narrative naming the party and its own date (*"Zayo: Anticipate start of
relocations in December 2023 and completion in February 2024."*, *"NTMWD: Relocations began on
June 10, 2024. Anticipate clearance in September 2024."*), though those lines thin out after 2024
into bucket lists (*"Utilities that are clear: AT&T, Atmos, Cash SUD, NTMWD, and OneOK"*).

Two honest limits. The structured field is the consortium's projection for the project as a
whole, not a signed utility commitment, and the owner names sit in adjacent bucket lists rather
than on the date itself — joining a name to a date is an inference the adjudicator would have to
make explicit. And the layout shifts: the Feb 2022 and Feb 2023 editions position field labels
differently from Aug 2025 and Feb 2026, so a parser keyed to one layout returns nothing on the
other rather than failing loudly.

The City of Denton Friday Staff Reports
([cityofdenton.com/510](https://www.cityofdenton.com/510/Friday-Staff-Reports-to-City-Council))
are the same genre at weekly cadence with the same caveats, and are already named in
`../history/corpus-acquisition-spec-2026-07.md` §7.3.

**5. Executed Standard Utility Agreements found in county and city agenda packets.** The
highest-fidelity single record found anywhere, and the worst corpus. Fort Bend County's executed
agreement with Southcross Gulf Coast Transmission Ltd.
([agendalink.fortbendcountytx.gov/mindocs/2018/CCTR/20180605_3064/minutes/2914_26J_signed_agreement.pdf](https://agendalink.fortbendcountytx.gov/mindocs/2018/CCTR/20180605_3064/minutes/2914_26J_signed_agreement.pdf))
carries, at Exhibit C:

```
EXHIBIT C
Utility's Schedule of Work and Estimated Date of Completion
Estimated Start Date: 6/18/2018
Estimated Duration: 14 Calendar Days
Estimated Completion Date: 7/2/2018
(Contingent to UA being executed, need 10 days to notify contractor to start.)
```

Day precision, executed, genuinely external counterparty. The binding-obligation language is in
the same package but on a different form — `Form ROW-U-JUAA Page 2 of 2`, not the agreement body:
*"Utility will, by written notice, advise TxDOT of the beginning and completion dates of the
adjustment, removal, or relocation, and, thereafter, agrees to perform such work diligently, and
to conclude said adjustment, removal, or relocation by the stated completion date."* The
agreement body itself states the schedule relatively (*"work shall commence within thirty (30)
working days after the execution of this Agreement"*). Note also that the parties are Fort Bend
County and the utility; TxDOT is not a party, and the project is only identified by CSJ. Calling
this a *TxDOT* Standard Utility Agreement, as an earlier pass did, is wrong.

The City of Corinth I-35 utility relocation agreement
([mccmeetings.blob.core.usgovcloudapi.net/corinthtx-pubu/MEET-Packet-624d42bcfb0f494db731521b2cf155fe.pdf](https://mccmeetings.blob.core.usgovcloudapi.net/corinthtx-pubu/MEET-Packet-624d42bcfb0f494db731521b2cf155fe.pdf))
is the same genre and adds the thing every other source lacks — an amendment that states the old
date, the new date and the cause in one document: *"Article III of the Agreement provided that
work was to be completed no later than December 31, 2025"*, amended to *"no later than December
31, 2026"*, because *"due to delays by TxDot, Contractor has been unable to commence and complete
the work within the time frame required by the Agreement."*

Why this is fifth rather than first: there is no index. These surface only through full-text
search across 254 Texas county portals plus city councils. Harvest a handful as gold-standard
examples of what a real dated commitment and a real amendment look like; do not plan a corpus
around them.

**6. The IDOT I-80 ArcGIS tracker, as a fixture only.**
[services6.arcgis.com/.../I_80_Utility_Conflicts_and_Land_Acquisition_Tracker_WFL1/FeatureServer/0/query](https://services6.arcgis.com/35T2FAnjA7GU1K13/arcgis/rest/services/I_80_Utility_Conflicts_and_Land_Acquisition_Tracker_WFL1/FeatureServer/0/query?where=1%3D1&outFields=*&returnGeometry=false&f=json)
answers anonymously and returns 151 typed rows with real `esriFieldTypeDate` columns. Seven rows
carry both an anticipated and an actual completion date, so a slip is computable from one
snapshot with no diffing at all: Nicor's 4" gas main on contract 62R30 anticipated 2023-06-22 and
actual 2023-06-29; ComEd pole and 12kV on 62P67 anticipated 2023-09-26, actual 2023-10-02. Three
of the seven slipped, two landed exactly, two finished early.

That is the whole of it, and it is a fixture rather than a source. Seven dated rows out of 151.
No provenance for the anticipated dates — every descriptive field on the service, the layer and
the web map is an empty string, and the `Notes_Comments` column is plainly the construction
manager's voice (*"Does this have to move?"*, *"Why are these relocated? Relocate by 4/1/25"*),
so nothing establishes that a utility authored the commitment. No revision history: the layer
reports `isDataVersioned=false`, a `historicMoment` query silently returns the current count, and
`dataLastEditDate` is 2025-01-15, meaning it has been dormant for about nineteen months and
forward polling would accumulate nothing. The documents that would prove commitment — the
`Permit_Status`, `Correspondence` and `Utility_Requirements` columns — are SharePoint URLs under
`bowmanconsultinggroup.sharepoint.com` that return 403 anonymously.

Take it as a shape-validation fixture that exercises an anticipated-versus-actual model against
seven real rows with three real slips. It carries no licence and can be unshared without notice;
mirror anything used. The more durable asset is the discovery technique:
`https://www.arcgis.com/sharing/rest/search` with
`q=(title:"Utility Relocations" OR title:"Utility Conflict") AND type:"Feature Service"` is worth
re-running periodically.

## What needs a records request

Four leads are structurally correct and inaccessible. Each is a real requirement written into a
published rule, with no filled instance published anywhere.

**TxDOT Standard Utility Agreement assemblies (ROW-U-35, Attachment C).** The ROW Utilities
Manual ([txdot.gov/content/dam/txdotoms/row/utl/utl.pdf](https://www.txdot.gov/content/dam/txdotoms/row/utl/utl.pdf))
states at line 6834 of the extracted text: *"A schedule for accomplishing the utility work must be
included with the agreement submission. For simple adjustments, a proposed starting date and
estimated completion date will suffice."* Austin District's SUA checklist
([sua-checklist.pdf](https://www.txdot.gov/content/dam/docs/district/aus/specinfo/sua-checklist.pdf))
is blunter: *"Attachment C ‑ construction start and end dates listed; start date must be after
execution date of the agreement."* The rule mandates calendar dates, which is what makes this the
best of the request-gated leads — and the revision mechanism is named too, *"Any changes, minor or
major, require execution of form ROW-U-COA Standard Utility Agreement – Supplemental Agreement"*
(line 7518). No executed assembly was located. Corridor already holds eleven TxDOT agreements for
NHHIP and none of them are ROW-U-35s; they are 1963–2022 maintenance, signal and parking
instruments with no relocation dates. A request against NHHIP or SH 99 would slot straight into
an existing project, which none of the public sources above can do.

**TxDOT Austin District Utility Status Report workbooks.** The guideline
([utility-status-report-guideline.pdf](https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-guideline.pdf))
describes exactly the artifact wanted — per-utility milestone rows with distinct *Estimated Date*
and *Date Completed* cells, edited by the external party (*"The recipient (utility) can then edit
the data (dates) and send it back"*), and reissued through the project's life. But the guideline
carries no real dates; the only ones in it are instructional examples inside editing walkthroughs.
Austin District covers neither of Corridor's projects, which is the reason this ranks below the
ROW-U-35 request rather than above it.

**The DB Contractor Utility Tracking Report.** The SH 99 design-build specification
([sh99-final-db-specs.pdf](https://www.txdot.gov/content/dam/docs/division/ald/sh99-segment-b/rfp/sh99-final-db-specs.pdf))
mandates it at Item 14.2.9: *"DB Contractor shall maintain a UTR in tabular form... shall submit
the UTR to TxDOT on a monthly basis"*, with fields including *"Scheduled start and completion date
for construction of each Utility Adjustment"* and *"Percent complete of construction"*. A monthly
per-adjustment dated table is the ideal shape. No UTR is published. File against a project with a
UTR actually running — I-35 NEX, Southeast Connector, Oak Hill Parkway, LBJ East — rather than
SH 99 B-1.

**The unsimplified MD 97 utility relocation schedule.** MDOT SHA publishes three revisions of an
MD 97 Utility Relocation Schedule to ArcGIS, and revision 1's PDF metadata names its source file
`MD 97 - Utility Relocation Schedule_Simplified_2026-05-13.xls`. The word *Simplified* is the
lead: request the unsimplified workbook, citing the published PDF as proof the record exists. The
published PDFs themselves are dead — see below.

Two records requests are already tracked in the issue tracker and are more likely to pay than any
of these: #151 (NHHIP recurring utility-status artifacts) and #7 (email correspondence, both
projects).

## Dead ends

These are recorded because each one looked right, and several were reported as promising by an
earlier pass before being opened.

**The whole DOT "utility work schedule" form family commits to durations, not dates.** This is the
single most useful negative finding, because it invalidates an entire genre. FDOT's Utility Work
Schedule (Form 710-010-05) is a genuinely signed external-party commitment — *"I have reviewed the
FDOT plans referenced above and submit this utility work schedule in compliance with UAM Section 5
and agree to be bound by the terms of this utility work schedule"*, signed by a named UAO
representative with a date and countersigned by FDOT. But Section C's columns are
`Act. No. | Utility Facility | From Station/Offset | To Station/Offset | Utility Work Activity Description | Dependent Activity | TCP Phase | Consecutive Calendar Days: Prior to Const. / During Const.`
and Section A totals them as *"Days prior to FDOT project construction: 0   Days during FDOT
project construction: 18"*. There is no absolute-date column. Forty-two of these were opened
across every populated district; every calendar date found in a machine scan of Section C
resolved to either the form revision footer (December 14, 2016) or the plan-set reference date.
A signature date is when the promise was made, not what was promised. INDOT's Utility Relocation
Work Plan is the same shape, and says so explicitly:
*"All estimate dates are based upon CenterPoint Energy's receipt of an approved work plan &
notice-to-proceed."* GDOT's Utility Adjustment Schedule is the same again, and is additionally not
downloadable — form 6863-9b states the schedules *"will be made available for examination by the
Contractor at the Department's District Office."*

**`../history/corpus-acquisition-spec-2026-07.md` line 170 should be corrected on four points.** It describes the
FDOT UWS in a way that reads as though the form is a schedule of dates; it is not, and this
document is the measurement behind that sentence. Three of its supporting claims are also wrong:
the share is **not** 403 and does not require harvesting search-engine URLs — folder URLs return
200 and the Cerberus web client is fully enumerable anonymously by posting to
`/public/op/<shareid>/get_dir` with `cd=<path>` and the `csrftoken` sent as a *form field* (an
`X-CSRF-Token` header is rejected), with `deepSearch=true` giving recursive filename search; the
share holds **1,390** UWS PDFs across 797 project folders in nine roots, not dozens; and it is not
uniformly machine-readable — 9 of a 36-document random sample have no text layer at all and need
OCR. Revisions also survive, contrary to the earlier read: `/district5/FPID 23839555201, etc/Utility Work Schedules/`
holds both `Comcast  (1).pdf` and `Comcast (1) Revised.pdf`, same FPID and same signer, whose
Section A goes from *"Days prior: 85 / Days during: 4"* (signed 10/11/19) to *"Days prior: 0 /
Days during: 96"* (signed 02/05/2020). That is a real diffable change in an external party's
committed schedule — and it is a duration reallocation, not a slip, which is precisely why the
genre fails.

**Blank templates that look like filled schedules.** Four separate leads collapsed into this one
category and all four were reported as date-bearing before being opened. TxDOT's design-build
utility agreement forms
([sh99-final-db-spec-attach.pdf](https://www.txdot.gov/content/dam/docs/division/ald/sh99-segment-b/rfp/sh99-final-db-spec-attach.pdf))
read literally *"On or before [Month] [Day], 20[19]."*, and their checklist prints
*"Estimated Start Date:"* and *"Estimated Completion or Duration:"* followed by a blank rule and a
bare `, 20`. Caltrans Section 5-1.36C(3) *Utility Relocation and Date of the
Relocation* is a caption followed by the header row `Utility   Location   Date` and then blank
lines, followed by a drafting instruction to the specification writer. ODOT's UT-14 *Memorandum of
Understanding with Utilities* is captioned *Executed MOUs* but names its counterparty as the
literal string `UTILITY, a private utility` with all fourteen signature lines blank. GDOT form
6863-9b is boilerplate with no project and no utility names.

**Documents whose only dates are event dates.** The FDOT SR 60 Cherry Hill Dispute Review Board
decision is live and every quote from it checks out, but a grep for commitment language returns
nothing — every date in it is a claim-submission, letter or meeting date (*"29 June 2004 - CHC
submitted a claim"*, *"12 April 2004 - DRB Meeting #11"*), and its one schedule reference is again
a duration (*"Verizon had 13 days of work scheduled during Project construction"*). NCDOT's Utility
Project Outline Report is the same: every date is a page footer or an observation stamp, and its
commitments are relative (*"This line will conflict with bridge construction and will be removed
before construction begins."*).

**Status words are not dates.** The MD 97 Utility Relocation Schedule PDFs (ArcGIS items
`9bcc31de2fba4f46b780e9c5c2f40d7a`, `4158e226c70b499b81f094fd5dfc7f3f`,
`096625a0bea74d3e9f5bfaa5a1a2caa1`) publish three revisions and the statuses do change between
them — Fiberlight/AT&T design and permitting reads `Complete`, then `In Progress`, then `Pending`.
Every stream object in all three files was decompressed and every text-showing operator scanned:
the only date rendered as text in each is the document's own publish date. What each utility row
carries is a status word and a coloured rectangle under a month column. A status word regressing
through an unordered vocabulary is a different signal from a date moving later, and it cannot be
differenced. The geometric fallback does not rescue it either — revision 1 is
Print-To-PDF output with the bars flattened to a single raster image, and revisions 2 and 3 are
drawn at different scales (month-column pitch 22.29pt against 18.48pt), so cross-revision bar
diffing would need per-revision calibration to yield, at best, a month.

**ODOT Cleveland Innerbelt UT-07** was reported as a revision-visible date mutation and is not
one. The base revision has no End Date field at all — its columns are
`Relocated By | Timing | Duration | Schedule`, against Addendum001's
`Relocated By | Timing | Start Date | Duration (mo.) | End Date` — so base-to-Add001 is a schema
replacement, and the base's date value `1/1/2010` appears on all 77 rows paired with Timing
`Unknown` and Duration `0 months`, which is a template default. The document was reissued four
times, not twice, and across the three revisions that have an End Date the six dated rows are
byte-identical, every one reading `Advance 3/19/2010 17.5 8/26/2011`. The matrix legend, repeated
on eight pages, explains why: *"Blank spaces under 'Relocation Information' in the matrix
indicates information to be determined by the DBT"* — that block is designated for a design-build
team that had not yet been selected. The start date 3/19/2010 is the RFP's own issue date — its
siblings in the same directory are `Proposal-103000_2010-03-19.pdf` and
`Document_Revision_Summary_2010-03-19.doc` — and every dated row carries the same duration, 17.5
months, and the same end date. It is one computed blanket window applied mechanically to the rows
marked *Advance*, not a per-utility negotiated date.

**Aggregate reporting.** The California High-Speed Rail Central Valley Status Report is monthly
and has a utility relocation section, and it is counts only: *"Relocated: 1,634 (90%); In
Progress: 75 (4%); Not Started: 117 (6%)"*. No named utility, no per-utility date. NCTCOG's
Partner Progress Reports have no utility content at all — a grep for "utility" over the February
2024 RTC edition returns zero hits.

**Login walls.** FDOT's PSEE Utility Module redirects to a sign-in form for RACF or ISA account
holders with no public listing, no API and no sample. Caltrans' bid document portal serves a
ServiceNow HTML login page under a URL that returns HTTP 200 with a `.pdf` extension, which will
silently poison any naive fetch. HART Honolulu's report page is JavaScript-rendered and returns
only `Loading...` placeholders to a static fetch — the spec's claim of eleven editions carrying a
*Utility Agreements Status Matrix* with variance in days remains unreproduced and needs a
browser-driven pass rather than `WebFetch`.

**Two SH 99 archive members deferred as "M4+ material" were opened and are not worth fetching.**
The two dated `RULIS_Utility_Permit_Applications_and_Status` zips (342 MB and 375 MB) look like an
ideal revision pair. Their index workbooks carry the columns
`Permit ID | Utility Owner | Permit Status | Folder Name | Comment` — no date column of any kind,
statuses reading `Reviewed` and `Approved`, comments reading *"Emailed back to utility with
comments"*. The contents are third-party permit applications to occupy right-of-way, not
relocation schedules. `Utility Owner Information.zip` (23 MB) is sixteen generic crossing and
encroachment standards documents.

**Two smaller corrections.** The WSDOT contracts root has 393 folders today, so
`../history/corpus-acquisition-spec-2026-07.md` §7.2's figure of 392 is accurate and current, not stale. And no other
Texas county publishes the Rockwall-style consortium report — a search for the exact field string
`"Utility Relocations Complete:"` outside Rockwall returns only Rockwall's own files and generic
TxDOT manual pages, so the format is one county's local practice rather than a Dallas District
standard and does not scale to a second corridor.

## The shape of the problem

Three patterns recur across everything opened, and they are worth stating plainly because they
predict where the next search should and should not go.

The industry's standard forms commit utilities to **durations anchored to a notice to proceed**,
not to calendar dates. FDOT, INDOT and GDOT all use this shape, and it is a deliberate design —
the utility cannot promise a date for work whose start it does not control. Any corpus built on
these forms would need Corridor to model *N days relative to a named construction phase*, and to
join an NTP date the document does not contain.

Where calendar dates do appear in a recurring published artifact, they are usually **the DOT's own
estimate about a third party** rather than the third party's promise. WSDOT's *Relocation Estimated
Date*, Rockwall's *Utility Relocations Complete*, ODOT's End Date and Bowman's
*Anticipated_Completion_Date* are all this. They are still useful — a project's expectation moving
later is a real signal — but the field name and the report language have to say whose expectation
it is.

The genuine external-party dated promise appears in two places: **bid special provisions**, where
a DOT tells bidders when a named utility will be out of the way and therefore states it in
declarative future tense, and **executed agreements**, where the utility signs. The first is
public and crawlable in North Carolina. The second is public only where a city or county happens
to be a party and attaches the executed instrument to an agenda packet.
