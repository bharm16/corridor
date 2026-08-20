# Public Status And Correspondence Gap Check For NHHIP #151 And #7

Researched on August 20, 2026 against official TxDOT and FDOT sources only.

Question: before filing the open-records requests tracked in GitHub issues #151 and #7, what records are already public for:

- TxDOT NHHIP Segment 3C-2, CCSJ `0500-08-001`
- FDOT SR 789 @ Broadway Roundabout, FPID `453730-1-52-01`

Decision objective: determine whether the existing public record now satisfies either request, or whether the requests should still be filed after first ingesting already-public material.

## Scope and bounded method

Read-only checks performed:

- live GitHub issues `#151` and `#7`
- local corpus manifests and prior research notes
- current TxDOT NHHIP 3C-2 pre-procurement and RID pages
- current TxDOT Box-backed RID utility bundle
- current FDOT District One marketing library page for Ad `#27114`
- directly downloadable project files linked from those official pages

Boundaries:

- only first-party agency pages and first-party file hosts (`txdot.gov`, `txdot.box.com` / `public.boxcloud.com`, `fdot.gov`, `fdotwww.blob.core.windows.net`)
- no claim here that a document does not exist anywhere outside those bounded official searches
- no records were filed and no project state was changed

## Current issue targets

Issue `#151` asks for a separate TxDOT request, scoped to NHHIP 3C-2, for:

- recurring utility-status reports or trackers
- utility coordination agendas, minutes, and action logs
- planned, committed, and actual relocation or clearance dates

Issue `#7` remains scoped to DOT-to-utility email correspondence for both project scopes. In the current corpus those scopes are:

- TxDOT NHHIP Segment 3C-2, CCSJ `0500-08-001`
- FDOT SR 789 @ Broadway Roundabout, FPID `453730-1-52-01`

## Findings by source

### 1. TxDOT already publishes a recurring NHHIP project-status series

Official page:

- <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/pre-procurement.html>

Current dated files listed there as of August 20, 2026:

- June 30, 2025 status summary
- August 26, 2025 status summary
- October 31, 2025 status summary
- December 19, 2025 status summary
- February 27, 2026 clean and redline
- July 21, 2026 status summary

What this proves:

- there is a real public recurring series for this exact project, not just a one-off comparator
- the utility section changes over time
- TxDOT publicly discloses project-level utility coordination state and some projected dates

Verified examples from the utility section:

- June 30, 2025 says TxDOT had completed a draft utility inventory matrix, mailed notices of proposed construction, was collecting utility-owner information, and anticipated a final UCM plus limited Level A/B SUE by `June 2026`
  Source: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-project-status-summary-20250630.pdf>
- July 21, 2026 says TxDOT has a draft UCM plus DGN/KMZ files in the RIDs, is scheduling meetings with utility owners to validate strip maps, and now anticipates the final UCM, utility exhibit, and limited Level A/B SUE by `March 2027`
  Source: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-pre-procurement-project-status-summary-20260721.pdf>
- July 21, 2026 also says advance utility relocation dates for the CenterPoint transmission work will be included in the draft RFP, but it does not itself publish those dates
  Source: same July 21, 2026 summary

Downloadability:

- yes by browser `GET`; the files are publicly readable from the TxDOT page

Current-corpus overlap:

- not currently listed in `corpus/manifest.yaml`

Implication for `#151`:

- this series partially satisfies the "recurring utility-status" need at a project-summary level
- it does not satisfy the narrower need for per-utility or per-conflict trackers, agendas, minutes, action logs, or actual clearance dates

### 2. TxDOT’s live RID utilities bundle is broader than the current local corpus

Official RID page:

- <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/rid.html>

Current utilities bundle on that page:

- `Utilities` updated August 14, 2026
- public ZIP: `nhhip3c2-utilities-rid-20260814.zip`

Public download path resolved from the Box shared link:

- <https://txdot.app.box.com/index.php?rm=box_download_shared_file&shared_name=51c7p764gr6pxivj2ati0qktrz7bq6w3&file_id=f_2406098312527>

Verified ZIP contents relevant to utility work:

- `nhhip-seg3c2-utilities-inventory.pdf`
- `nhhip-seg3c2-utilities-inventory-7-22-2025.pdf`
- `nhhip-seg3c2-utilities-inventory-10-24-2025.pdf`
- `nhhip-seg3c2-utilities-inventory-12-15-2025.pdf`
- `nhhip-seg3c2-utilities-inventory-2-13-2026.pdf`
- `nhhip-3c2-subsurface-utility-engineering-quality-level-a-test-hole-data.pdf`
- `nhhip-3c2-subsurface-utility-engineering-quality-level-c-utility-verification-data.pdf`
- `nhhip-3c2-utility-strip-maps-12-15-2025.pdf`
- `nhhip-3c2-utility-strip-maps-2-13-2026.pdf`
- `nhhip-3c2-ih69-utility-strip-maps-dgns.zip`
- `nhhip-3c2-ih69-utility-strip-maps-dgns-2-17-2026.zip`
- `nhhip-3c2-ih10-utility-strip-map-dgns.zip`
- `nhhip-3c2-ih10-utility-strip-map-dgns-2-17-2026.zip`
- `nhhip-3c2-2d-utilities-all-12-15-2025.dgn`
- `nhhip-3c2-2d-utilities-all-12-15-2025.kmz`
- `nhhip-3c2-2d-utilities-all-2-13-2026.dgn`
- `nhhip-3c2-2d-utilities-all-2-13-2026.kmz`
- `nhhip-seg3c2-utilities-its-transtar-2-13-2026.pdf`
- `nhhip-seg3c2-utilities-storm-drain-2-13-2026.pdf`
- `nhhip-seg3c2-utilities-txdot-electric-2-13-2026.pdf`

What this proves:

- TxDOT publicly publishes more utility evidence than the current corpus has ingested
- the extra public material is mostly geometry, strip-map, and utility-category evidence
- it is still not a recurring utility-status tracker, meeting-minute archive, or action log

Downloadability:

- yes; the Box file is a public `application/zip` download with `Content-Length: 217975219`

Current-corpus overlap:

- already in corpus: the five matrix revisions and the two SUE PDFs
- not in corpus: strip maps, DGN/KMZ utility files, sanitary sewer DGN/KMZ, ITS/TranStar, storm-drain, and TxDOT-electric utility PDFs

Implication for `#151`:

- ingest these public utility files first because they are free evidence
- they do not remove the need for a request if the product needs actual status trackers, coordination logs, or dated relocation/clearance records

### 3. TxDOT also publishes meeting-adjacent procurement artifacts, but not utility coordination minutes

Same pre-procurement page:

- July 28, 2025 workshop documents
- September 9-11, 2025 round-one one-on-one documents
- January 13-14, 2026 round-two one-on-one documents
- August 10, 2026 workshop documents

Examples:

- August 10, 2026 workshop presentation:
  <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c-2-industry-ws-presentation-20260810.pdf>
- August 10, 2026 attendee list:
  <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-industry-ws-attendee-list-20260810.pdf>
- Round one one-on-one meetings document:
  <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-round-one-pre-procurement-one-on-one-meetings.pdf>

What this proves:

- TxDOT does publish workshop and one-on-one procurement materials
- these are project-development / industry-partnering artifacts, not the recurring utility coordination agendas, minutes, or action logs named in `#151`

Downloadability:

- yes by public page access

Current-corpus overlap:

- not currently listed in the manifest

Implication for `#151`:

- these are useful context artifacts, but they are not substitutes for the missing utility worklist / follow-up record

### 4. FDOT District One now publicly publishes more for FPID 453730-1-52-01 than the repo currently tracks

Official page:

- <https://www.fdot.gov/procurement/marketingD1/default.shtm>

Verified public project files under Ad `#27114` for FPID `453730-1-52-01`:

- utility conflict matrix
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015201-utility-conflict-matrix.pdf?sfvrsn=9a82c4db_1>
- utility work schedule for utilities
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/2025-11-21_phase-iv-erc_gmd-broadway-rab_utility-work-schedule_utilities.pdf?sfvrsn=ccb7e38a_1>
- utility-work-by-highway-contractor plans
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015601-plans-10-utilitywork.pdf?sfvrsn=46946e47_1>
- roadway plans with verified-utility sections
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015201-plans-01-roadway.pdf?sfvrsn=621543dd_1>

What the utility work schedule proves:

- this project does have a public utility schedule artifact
- it is signed by the UAO, EOR, and FDOT utilities roles
- it gives project identity, UAO contacts, and relative work durations

Key details from the public UWS:

- `Financial Project ID: 453730-1-52-01`
- `FDOT Plans Dated: 11/21/2025`
- `Utility Company: Town of Longboat Key Utilities (Contingency/ Back-Out UWS)`
- special condition says the UAO must complete bidding / permitting / mobilization steps no later than `08/26/2026 (Letting Date)`
- section A says `212` days during FDOT project construction
- section C lists utility facilities, station/offset ranges, dependent activity, TCP phase, and consecutive calendar days

What the utility-work-by-highway-contractor plans prove:

- the project publishes named utility owners and utility adjustment sheets
- the public plans include verified-utility summaries and specific water / sewer / utility adjustment plan sheets
- this is much more than a bare conflict matrix

Downloadability:

- yes; all tested files returned `HTTP/1.1 200 OK`

Current-corpus overlap:

- current local manifest for `fdot-sr789` only names the utility conflict matrix
- the UWS, utility-work plans, and roadway/verified-utility plan set are not in the current manifest

Implication for `#7`:

- public non-email utility artifacts for the FDOT scope are broader than the old issue text implies
- none of the official public files found here are DOT-to-utility email correspondence

### 5. I did not find public DOT-to-utility correspondence for either issue-#7 scope

TxDOT bounded official search:

- NHHIP pre-procurement page
- NHHIP RID page
- linked workshop, one-on-one, status-summary, and RID utility materials

FDOT bounded official search:

- District One marketing page
- all visible Ad `#27114` utility-related project files

Observed result:

- public project summaries, workshop materials, matrices, strip maps, UWS forms, utility plans, and verified-utility sheets are available
- I did not find public email chains, letter correspondence packets, or DOT-to-utility message archives for either project scope in those bounded official sources

This is a bounded absence only. It is not proof that the agencies hold no such records.

## Recommendation

### Issue #151

Do not file `#151` blind from the old assumption set.

First do a public-data intake pass for the NHHIP materials that are already available now:

- the six dated TxDOT status summaries on the pre-procurement page
- the August 14, 2026 RID utilities ZIP, especially the strip maps and utility-category PDFs not already in the corpus

Then still file a narrowed TxDOT records request unless those public files prove sufficient after ingestion.

Reason:

- the public record now covers recurring project-level utility status and a larger utility evidence bundle than the local corpus currently reflects
- but it still does not expose the exact artifacts `#151` was targeting most directly: recurring utility-owner trackers, utility coordination agendas / minutes / action logs, or planned / committed / actual relocation or clearance dates at the utility-owner or facility level

Recommended narrowed ask after public ingestion:

- existing recurring utility-owner status trackers or spreadsheets
- utility coordination agendas, minutes, and action logs
- existing documents that state planned, committed, or actual utility relocation / clearance dates
- exclude the already-public pre-procurement status-summary series and the RID utility bundle by URL/date so the search stays cheap

Confidence: medium-high.

### Issue #7

The correspondence request still looks necessary for both scopes.

Reason:

- public official sources now provide more non-email utility artifacts for both projects
- they still do not surface the DOT-to-utility email layer that `#7` was explicitly created to obtain

For the FDOT scope especially, the request can now be kept tighter because the public side already covers:

- the conflict matrix
- a signed utility work schedule
- utility-work-by-highway-contractor plans
- verified-utility plan sheets

So the remaining gap is more clearly correspondence, not general utility documentation.

Confidence: high on the gap, medium on completeness outside this bounded official search.

## High-impact next step

Before filing anything:

1. register the public NHHIP status-summary series in the corpus plan as `status_report` / `stream`
2. register the public NHHIP RID utility-bundle additions that are not already in `manifest.yaml`
3. register the public FDOT `453730-1-52-01` UWS and utility-work plan artifacts if the project is still in scope for public-source comparison work
4. re-evaluate `#151` against that enlarged public intake
5. if the gap remains, file the narrowed `#151` request
6. keep `#7` focused on correspondence for both scopes

## Bottom line

The public record is materially better than the existing issue text assumes.

- `#151`: public materials now justify an ingest-first pass before filing, but they do not yet eliminate the likely need for a request.
- `#7`: public materials still do not replace a correspondence request.
