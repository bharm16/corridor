# Are structured renditions actually obtainable? — spreadsheet source availability

Researched 2026-08-28. Every claim below is marked **VERIFIED** (I opened the file, read the
archive's own directory, or the owning page states the format) or **REPORTED** (a primary
source says so, but I could not open the artifact itself).

## The question

A proposed intake policy says: *"request the structured rendition at project onboarding —
every structured file eliminates model extraction for that document."* The second half of
that sentence is already true in code: `src/corridor/ingest.py` routes `.xlsx`/`.xlsm`
files to `_extract_sheets`, which reads cells natively with no model call, and ADR-0005
makes the structured original the Preferred Source File when both renditions exist. The
open question is the first half: does a structured rendition exist to be requested — for
the projects already registered in this repo, and for the typical new highway project an
operator would onboard?

## Method

First, an inventory of every manifest in `corpus/*.yaml` and its `.lock.json`, which
records the actual bytes fetched (content type, size, member names). Second, online
research against primary sources only: txdot.gov and its Box shares, fhwa.dot.gov,
trb.org, ftp.wsdot.wa.gov, and docs.oracle.com. Where a manifest's Box archive was
publicly fetchable without login, I range-read the zip's central directory to list its
members without downloading the archive — the same trick `make corpus` uses. One such
fetch failed and is recorded as a failure rather than retried, per the ground rules.

## Findings: the projects already registered

The short version: **every data-carrying document in the corpus today is a PDF.** The only
spreadsheet registered anywhere is a blank form.

### nhhip-3c2 (Project A — TxDOT NHHIP Segment 3C-2)

`corpus/manifest.yaml` registers 19 sources: the RID index (PDF, fetched directly from
txdot.gov), five dated Utility Conflict/Inventory Matrix revisions (`doc_type: matrix`),
eleven executed agreements with the City of Houston, METRO, and Harris County dated 1963
through 2022 (`doc_type: agreement`), and two SUE test-hole/verification reports
(`doc_type: plan`). `corpus/manifest.lock.json` shows every one of them is a PDF, reached
as members of two public Box zip archives (`app.box.com` shared-download URLs) linked from
TxDOT's RID page. **VERIFIED** from the lock file and manifests.

No spreadsheet rendition exists to register. ADR-0005 states it outright: "Project A
publishes no spreadsheet at all — its utilities archive holds 21 members and zero," and
`docs/research/nhhip-public-status-and-correspondence-gap-2026-08-20.md` lists the current
utilities archive's members as of 2026-08-20 — a 218 MB zip, every member `.pdf`.
**VERIFIED** in the repo as of 2026-08-20. My attempt to re-list that archive live today
failed: the Box redirect resolved but the `public.boxcloud.com` CDN returned HTTP 500 to
the range request. Recorded, not retried.

### sh99-grand-parkway (Project C — TxDOT SH 99 Grand Parkway Segment B-1)

`corpus/sh99-grand-parkway.yaml` registers 159 sources: three dated UCM revisions
(matrix), 145 dated per-owner coordination meeting notes (minutes), and eleven exhibits
and SUE sheets (plan). The lock file shows all 159 are PDFs, members of one public Box
utilities archive (with the meeting notes nested a zip deeper). **VERIFIED**.

I range-read that archive's central directory today (1,118,321,268 bytes, 21 top-level
members). The three UCMs and every meeting note are PDF, but the archive is not purely
PDF: it also holds `SH99_PROBE_DEPTH_SUMMARY_TABLE_1-15-2025.xlsx` and
`SH99_TEST_HOLE_INDEX_1-15-2025.xls` (SUE test-hole tables in spreadsheet form, neither
registered in the manifest), four MicroStation `.dgn` design files, two `.kmz` and one
`.kml` geospatial file, and the two deferred RULIS permit-status zips. **VERIFIED** by
reading the archive directory. So TxDOT does publish some structured files on this project
— just not for the matrix, minutes, or agreements classes.

The project's schedule is different: `corpus/sh99-milestones.csv` is a three-row CSV
(design completion, ROW/utility execution, relocation construction completion) that lives
in this repo and feeds `make milestones`. It is the v0 stopgap that
`src/corridor/milestones.py` names — "CSV is the v0 stopgap. P6 XER import is M9." It is
repo-authored, not agency-published. **VERIFIED**.

### wsdot-9424 and wsdot-9540 (WSDOT reference corpora)

Contract 9424 (SR 509 Completion Stage 1B) registers three PDFs from
`ftp.wsdot.wa.gov` — the Appendix U2 existing utility listing (WSDOT's equivalent of a
UCM), the U3 utility owner contact list, and the U8 conceptual relocations. Contract 9540
(SR 167 Completion Stage 1b) registers six Appendix U3 utility listing PDFs, partitioned
by utility type. **VERIFIED** from the lock files.

I browsed the FTP tree directly today (it is loginless and directory-listable). Every file
under 9424's Appendix U (U1 through U8 — assignment of permit rights, utility listing,
contact list, trust-fund agreements, as-builts, easements and franchises, service
agreement checklist, conceptual relocations) is a PDF, and every file under 9540's U3 is a
PDF, including the composite utility plans. **VERIFIED** by reading the listings. No
spreadsheet renditions appear anywhere in either contract's utility appendix.

### fdot-sr789 and cross-agency

FDOT SR 789's one matrix is a PDF from FDOT's District One procurement blob store
(**VERIFIED**, lock file records `application/pdf`). `corpus/cross-agency.yaml` holds the
two other filled-matrix layout sources — FDOT I-75 and CDOT US 6, both PDF — plus the one
and only spreadsheet in the entire corpus: TxDOT's published **Utility Conflict Analysis
Template**, fetched from txdot.gov as a real `.xlsx` (lock file records the
`spreadsheetml` content type). It is a blank form — seven sheets, a 40-column conflict
schema, a 113-column data dictionary, controlled vocabularies, zero data rows.
**VERIFIED**. I re-checked the URL today: HTTP 200, content type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, 72,538 bytes.

## Findings: new projects, by agency

### TxDOT — the structured original exists, and at least one project publishes it

Three facts line up here.

First, TxDOT's own process requires the spreadsheet to exist. The Project Development
Process Manual (§6.4.1, Determination of Utility Conflicts) says: "All utility conflicts
should be documented using ROW's Utility Conflict Analysis Template." That template is
published on TxDOT's utility forms page as an XLSX (verified live, above), and the ROW
Utilities Manual's Utility Cooperative Management Process chapters describe the
coordination practice it serves. So on a TxDOT project, the PDF matrices in this corpus
are printouts of an Excel workbook that the project office maintains — which is exactly
what ADR-0005 already concluded from the documents themselves. **VERIFIED** (manual page
and template file both opened).

Second, at least one TxDOT design-build project publishes that workbook rather than a
printout. I opened the I-35 NEX South RID utilities archive today (a 50.9 MB zip behind
the txdot.box.com share on the RID page) and read its directory:
`Utilities/I-35_NEX_SOUTH_UCM_22.02.07.xlsx` and
`Utilities/I-35 NEX_SOUTH_Potential_Utility_Conflicts.xlsx` are both there, alongside
PDFs and `.dgn` files. **VERIFIED** — this upgrades the claim in
`corpus-acquisition-spec.md` §7.2 ("UCMs are XLSX, not PDF") from a recorded sweep result
to a directly re-confirmed fact.

Third, the publication choice is per-project and mostly lands on PDF. NHHIP 3C-2 and SH 99
both publish PDF-only matrices from the same kind of RID page (verified above), and the
RID index pages themselves expose only Box folder links with no format information — you
cannot tell from the portal what you will get. NHHIP's program page publishes its
pre-procurement status summaries as PDFs. **VERIFIED**.

The consequence for onboarding: on a TxDOT project the structured rendition almost
certainly *exists*, but on four of the five agency corpora in this repo it is not what the
agency *published*. Getting it means asking the project office — which is precisely the
"request native electronic format — searchable PDF or original XLSX, not scans" line
already in `corpus-acquisition-spec.md` §5's records-request template, and which an
operator onboarding a live client project (rather than scraping a public portal) is well
positioned to do, because the client's own utility coordinator maintains the workbook.

### FHWA / SHRP2 — the national guidance ships an Excel UCM template

The FHWA GoSHRP2 page for R15B ("Identifying and Managing Utility Conflicts") describes
the product as the Utility Conflict Matrix and companion report, a one-day training
course, and a data model and database, and states the products are available as "UCM in
Excel and Access." **VERIFIED** (owning page states it). The TRB SHRP2 training-materials
page links the actual files, and I checked the matrix template itself:
`onlinepubs.trb.org/onlinepubs/shrp2/R15BTrainingMaterials/UtilityConflictMatrix.xls`
returns HTTP 200, content type `application/vnd.ms-excel`, 71,680 bytes, last modified
April 2011. **VERIFIED** — the reported Excel UCM template is real and still downloadable.
TRB also publishes the Access database (38.8 MB `.mdb`, shipped with a `.zzz` extension to
dodge mail filters), an ERwin data model, an Oracle export schema, and a PDF data
dictionary.

One catch that matters to Corridor specifically: the SHRP2 template is `.xls` — the old
binary format — and `ingest.py` deliberately excludes `.xls` from `SPREADSHEET_SUFFIXES`
because openpyxl cannot read it. The SH 99 archive's test-hole index is also `.xls`. A
structured rendition in `.xls` form today falls into neither the sheet path nor the PDF
path; it would need a one-time conversion at intake. That is a real, if small, hole in
"every structured file eliminates model extraction."

### WSDOT — publishes PDF, at scale, loginless

WSDOT's contracts FTP (392 contracts per the acquisition spec's sweep) is browsable
without any login, and everything I opened in the utility appendices of both registered
contracts — utility listings, trust-fund agreements, contact lists, franchises,
as-builts — is PDF. **VERIFIED** by reading the live directory listings today. I found no
spreadsheet renditions anywhere in either contract's Appendix U. Whether WSDOT maintains
the listings in a workbook internally, nothing public says; for WSDOT documents obtained
from the public archive, plan on PDF and model extraction.

### Schedules — the structured rendition is the native format

Oracle's own P6 Professional Importing and Exporting Guide lists XER as a supported
format: "The Primavera PM XER file format enables you to export data between P6
Professional release 5.0 or more recent versions," exported via File → Export → "Primavera
PM - (XER)". The same page lists native spreadsheet (XLS) export. **VERIFIED** (owning
documentation page). So for schedules the situation inverts: there is no "PDF original"
to work around — the schedule lives in a scheduling tool, and a structured export is a
menu item for whoever runs it. The repo's M9 plan (`milestones.py`, `models.py`) already
assumes exactly this. The caveat is the same as for matrices: public portals do not
publish XER files (none of the corpora here include one), so the export has to be
requested from the project team. Note also, from
`docs/research/dated-commitment-sources.md`: the DOT "utility work schedule" *form*
family (FDOT Form 710-010-05, GDOT's 6863-9b) is signed PDFs committing to durations, not
a P6 schedule — requesting "the schedule" from an agency can return a signed form rather
than an export, and the two are different document classes.

## What this means for the proposed onboarding policy

**The policy is partially realistic, and the split is clean by document class.**

**Matrices: realistic to request, unrealistic to expect from portals.** The structured
original exists on essentially every TxDOT project because TxDOT's manual mandates
authoring conflicts in an XLSX template, and I-35 NEX South proves an agency will sometimes
publish it. But four of the five agency corpora in this repo publish only the printout, and
the portals don't say what format you'll get before you open the archive. So the
onboarding request is worth making every time — a live client's coordinator has the
workbook — while the pipeline must keep full PDF matrix extraction for the majority of
documents where the request fails or the project is historical. For WSDOT and FDOT public
documents, assume PDF.

**Schedules: realistic.** XER export is a standard, documented P6 capability, and the
operator's counterpart on a live project can produce it on demand. This is the strongest
case for the policy, and M9 already plans for it. Ask at onboarding; the CSV stopgap
covers projects where nobody will.

**Minutes: not realistic.** Meeting notes are prose. The 145 SH 99 notes are PDFs, and
their "structured original" would at best be a Word file — not row-shaped, and not
something the deterministic sheet reader could use anyway. There is no spreadsheet
rendition to request, so this class stays on model extraction regardless of intake policy.

**Agreements: not realistic.** Executed agreements are signed instruments — NHHIP's run
from 1963 to 2022 and are scans of paper. TxDOT publishes its agreement *forms* (ROW-U-35
Standard Utility Agreement, ROW-U-MA Master Utility Agreement) as DOCX templates, but the
document Corridor needs is the executed copy, which is inherently a signed PDF. No
structured rendition exists for this class.

Two smaller consequences. The policy's premise that a structured file eliminates model
extraction holds only for `.xlsx`/`.xlsm`; both the SHRP2 template and one SH 99 table are
`.xls`, which the sheet reader deliberately rejects, so intake should either convert
`.xls` on receipt or the request should specify "xlsx". And the SH 99 archive shows
agencies do publish structured files (`.xlsx`, `.dgn`, `.kmz`) next to the PDFs — a
listing pass over an archive's members at onboarding, rather than only registering the
documents someone expected, is how a structured rendition that does exist would actually
get found.

## Sources

Repo files (all paths from the repo root):

- `corpus/manifest.yaml` and `corpus/manifest.lock.json` — NHHIP 3C-2 registrations, all PDF.
- `corpus/sh99-grand-parkway.yaml` and `.lock.json` — 159 SH 99 registrations, all PDF.
- `corpus/wsdot-9424.yaml`, `corpus/wsdot-9540.yaml` and their lock files — WSDOT PDFs.
- `corpus/fdot-sr789.yaml` and lock file — FDOT SR 789 matrix PDF.
- `corpus/cross-agency.yaml` and lock file — the TxDOT template XLSX, the only spreadsheet registered.
- `corpus/sh99-milestones.csv` — the repo-authored schedule CSV.
- `corpus-acquisition-spec.md` §5 (native-format request language), §7.2 (RID survey, I-35 NEX XLSX, WSDOT's 392 contracts), §7.4.
- `docs/adr/0005-the-structured-original-is-the-document-of-record.md` — the I-35 NEX workbook internals, "Project A publishes no spreadsheet at all."
- `docs/research/nhhip-public-status-and-correspondence-gap-2026-08-20.md` — NHHIP utilities archive member list, 2026-08-20.
- `docs/research/dated-commitment-sources.md` — utility work schedule form family; MD 97 `.xls` provenance.
- `src/corridor/ingest.py` (`SPREADSHEET_SUFFIXES`, `_extract_sheets`), `src/corridor/docs.py` (`STRUCTURED_SUFFIXES`, `document_of_record`), `src/corridor/milestones.py` (CSV stopgap, XER as M9).

Online, all fetched 2026-08-28:

- TxDOT Utility Conflict Analysis Template (XLSX, live, 72,538 bytes): <https://www.txdot.gov/content/dam/docs/division/row/utl/utility-conflict-analysis-template.xlsx>
- TxDOT PDP Manual §6.4.1, Determination of Utility Conflicts: <https://www.txdot.gov/manuals/des/pdp/chapter-6--right-of-way-and-utilities/6-4-utility-accommodation-process/6-4-1-determination-of-utility-conflicts.html>
- TxDOT utility forms and publications (template XLSX; agreement forms DOCX): <https://www.txdot.gov/business/resources/utility-accommodations/forms-publications.html>
- TxDOT ROW Utilities Manual, Utility Cooperative Management Process: <https://www.txdot.gov/manuals/row/utl/chapter-2--txdot-utility-cooperative-management-pr.html>
- I-35 NEX South RID page: <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/i35-nex-south/rid.html> — utilities Box share `nj56xaqh2dqoj88euo0zqndyk8mfw0a3` (`i35nexso-rid-utilities.zip`, 50,875,051 bytes; central directory read by range request; two `.xlsx` UCMs).
- NHHIP 3C-2 RID page: <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/rid.html> — Box folder links only; live archive listing attempt returned HTTP 500 from the Box CDN.
- SH 99 Grand Parkway Segment B-1 RID page: <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/sh99-grand-parkway-segb1/rid.html> — Box folder links; utilities archive central directory read by range request (1,118,321,268 bytes, 21 members).
- FHWA GoSHRP2 R15B product page ("UCM in Excel and Access"): <https://www.fhwa.dot.gov/goshrp2/Solutions/Renewal/R15B/Identifying_and_Managing_Utility_Conflicts>
- TRB SHRP2 R15B training materials (file list with formats): <https://www.trb.org/StrategicHighwayResearchProgram2SHRP2/Pages/Training_Materials_for_Identification_of_Utility_C_709.aspx>
- SHRP2 UCM Excel template (XLS, live, 71,680 bytes): <https://onlinepubs.trb.org/onlinepubs/shrp2/R15BTrainingMaterials/UtilityConflictMatrix.xls>
- WSDOT contract 9424 Appendix U directory listings (all PDF): <https://ftp.wsdot.wa.gov/contracts/9424-SR509CompletionStage1B/RFP/Appendices/U/>
- WSDOT contract 9540 Appendix U3 directory listing (all PDF): <https://ftp.wsdot.wa.gov/contracts/9540-I-5toSR509NewExpressway/RFP/Appendices/U/U3/>
- Oracle P6 Professional Importing and Exporting Guide, supported file formats (XER, XLS): <https://docs.oracle.com/cd/F51303_01/English/admin/p6_pro_importing_exporting/import_export_file_formats.htm>
