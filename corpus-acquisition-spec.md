# Corpus Acquisition Spec

Companion to `v0-build-spec.md`. Covers M0, which runs from week 0 in parallel with the build.

## 1. Why this document exists

There are zero documents in hand. The original plan budgeted half a week for downloading a corpus that was assumed to be sitting on public portals. It is not: portals publish templates and process manuals in abundance, and filled project artifacts rarely.

That makes corpus assembly the schedule's critical path rather than its warm-up, and it has to run asynchronously — public-records requests are measured in weeks, and the wait runs while code is written.

## 2. What the corpus must contain

The data model needs two structurally different things, and no single public source supplies both.

**Spine** — a set of Dependency records: external parties, locations, stationing, types, owners. A static point-in-time snapshot is fine.

> filled utility conflict matrix · utility agreements and amendments · utility special provisions · SUE reports · plan general notes

**Stream** — *dated* assertions about those same dependencies that change over time, so `slip`, `STALE`, `OVERDUE`, and `CONTRADICTION` have something to fire on.

> serial monthly/quarterly program status reports · meeting minutes · board and committee packets · correspondence

A bid package is all spine and no stream: nothing in it ever changes state, so a readiness ledger built from it can never demonstrate readiness *changing*. A status-report archive is the reverse. **Both roles, same project** is the requirement.

**Stream is a property of a document set, not of a document.** A single status report is not a stream; three consecutive editions are. This is why Project A's five dated matrix revisions are each tagged `role: spine` — every one of them is a register of dependency records — while the *series* supplies the stream. A manifest entry's `role` describes what a document contains; the stream emerges from having several of them across time.

The corollary bites: a stream source covering a *different* project cannot be borrowed. The Denton and Rockwall reports are genuine streams, but they track Denton- and Rockwall-area projects, not NHHIP Segment 3C-2, so they belong to their own project rather than to Project A's manifest. Documents that cannot merge into one ledger are not a stream for that ledger.

### Minimum viable corpus, per project

| | |
|---|---|
| Spine | A filled conflict matrix, **or** ≥2 utility agreements plus the utility special provisions |
| Stream | ≥3 consecutive editions of a serial status report, **or** ≥3 meeting minutes covering the same project |
| Schedule | A milestone list — extractable from a schedule narrative, letting date, or phasing plan |
| Evidence | Plan sheets and drawings, registered as documents and linked manually. Not extracted in v0. |

## 3. Project selection criteria

Candidates meeting these criteria are identified in §7.1.

**Hard requirements**

- Project A: spine + stream, both publicly reachable.
- Project B: spine + stream, **plus a filled Utility Conflict Matrix or equivalent professionally authored conflict enumeration.** This is the eval gold set; without it the M7 gate falls back to hand-labeling and Week 7 gets materially more expensive.

**Strong preferences**

- **Different agencies for A and B.** M6 claims the pipeline generalizes with config-only changes. If both projects come from the same DOT, that claim is barely tested — same matrix layout, same document conventions, same vocabulary. Different agencies make M6 a real test. Accept same-agency only if that is genuinely all that's available, and say so when stating the M6 result.
- **Strong-sunshine jurisdiction** (see §5), so the records-request upgrade path is viable.
- **Tractable size** — roughly 100–300 documents. Urban corridor reconstruction or a major interchange has real utility complexity; a mega-program has a document set that will consume the schedule.
- **Recent** — in design or construction within roughly the last five years, so serial status reports exist and records retention schedules still cover the correspondence.

## 4. The manifest

Curated intent and fetch outcome are kept in separate files, the way a lockfile separates from a dependency list.

**`corpus/manifest.yaml`** — hand-edited, checked in. Curation stays human because judging *"is this the utility special provisions"* is the hard part; fetching is not.

```yaml
project: <slug>
agency: <agency>
sources:
  - url: https://example.gov/path/to/doc.pdf
    doc_type: matrix        # matrix|minutes|agreement|email|plan|schedule|spec|status_report|other
    role: spine             # spine|stream|schedule|evidence
    title: "Utility Conflict Matrix, Rev C"
    doc_date: 2024-03-14
    notes: "Appendix D of the procurement library"
```

**`corpus/manifest.lock.json`** — generated. Records per source: `sha256`, `retrieved_at`, `local_path`, `http_status`, `content_type`, `bytes`.

### Fetcher behavior

- **Idempotent.** A source whose `sha256` matches the lock is skipped.
- **Drift is recorded, never overwritten.** If a URL now returns different bytes, keep both files and flag it. Public documents get revised by addenda — detecting that is M8's document-supersession feature arriving for free, and it is real evidence that revision handling matters.
- **Nothing is ever deleted.**
- **Polite.** Rate-limited, with a user agent that identifies the project and a contact address.
- **Host quirks are not optional.** Box requires a browser user agent and `GET` rather than `HEAD`; Wayback requires status and MIME checks and a truncation check. See §7.5 — every one of these silently produces a wrong answer rather than an error, and a fetcher that ignores them will report live sources as dead.
- **A source may be a member of an archive.** The documents that matter most on Project A are only reachable inside a 208 MB zip (§7.6), so a manifest entry needs an optional member path alongside its URL, and the fetcher needs to resolve it. Range-reading the archive directory avoids downloading the whole thing.

`source_url` and `retrieved_at` flow into `documents` at ingest. A citation that bottoms out at *"a file on my laptop"* is not a citation — corpus provenance is part of the product's claim, not bookkeeping.

## 5. Public-records requests

**Reprioritized by the source sweep.** This spec originally assumed records requests were the only route to meeting minutes and correspondence. They are not: SH 99 Grand Parkway publishes utility owner meeting notes outright, and dated conflict-matrix revisions and permit-status series are public on several projects (§7). Requests are now an **upgrade path, not a prerequisite** — nothing in the build blocks on them.

Still file them in **week 0**. Filing costs an hour, the wait is asynchronous, and the one thing no public source has yielded is genuine **email correspondence** between a DOT and a utility owner — which is what the email extractor needs. The expensive mistake is discovering in week 5 that it has nothing to eat and starting the clock then.

**Scope every request to the same project as the spine.** Minutes from an unrelated project produce a third disconnected document set that cannot merge into one ledger — and merging is the interaction most in need of exercise.

### Scoping a request so it's cheap and fast

- Name **one** project by its official identifier — CSJ (Texas), FPID (Florida), EA (California), or the contract number. Vague requests get broad searches and large fee estimates.
- Give a **date range**.
- Name document categories **explicitly** rather than asking for "the project file": utility coordination meeting agendas and minutes; correspondence with utility owners; the utility conflict matrix or utility conflict list and its revisions; utility agreements and amendments; utility relocation schedules; utility clearance certification.
- Request **native electronic format** — searchable PDF or original XLSX, not scans.
- Ask for a **fee estimate before production**, and offer to narrow. This converts a surprise invoice into a negotiation.
- Ask whether any of it is **already published**, which sometimes returns a URL the same day.

### Request template

> Under [STATUTE], I request the following records concerning [PROJECT NAME], [PROJECT ID], for the period [START] to [END]:
>
> 1. Agendas and minutes of utility coordination meetings.
> 2. Correspondence, including email, between department staff and utility owners regarding utility relocation for this project.
> 3. The utility conflict matrix or utility conflict list for this project, including all revisions.
> 4. Utility agreements and amendments executed for this project.
> 5. Utility relocation schedules and any utility clearance certification.
>
> I request native electronic format where available. If any portion is already published online, a link is sufficient. Please provide a fee estimate before producing records if costs will exceed [AMOUNT], and I am glad to narrow the scope to reduce cost.

Per-state mechanics — statutes, portals, timelines, fees, and whether employee email is routinely released — are in §7.

## 6. Synthetic scaffolding

`phase-1-roadmap.md` rule 2 permits synthetic fixtures to exercise code paths and forbids them from contributing to any quality claim. That boundary is **enforced structurally, not by discipline**:

- Synthetic documents belong to a project with `is_synthetic = true`.
- `make eval` refuses to run against a synthetic project.
- Reports generated from a synthetic project are watermarked.

A promise not to mix them is worth less than a system that cannot.

Legitimate uses: ingest and OCR paths, re-ingest idempotency, extraction output schema, merge blocking and scoring, every exception rule including boundary days, report rendering and provenance enforcement.

## 7. Candidate sources

Assembled by a research sweep across six channels, then independently re-verified by fetching every URL and reading the actual bytes. Claims below survived that second pass; corrections from it are noted inline. 54 leads confirmed, 22 partial, 8 dead.

**The headline result overturns this spec's original premise.** Utility coordination meeting notes, dated conflict-matrix revisions, and permit-status time series *are* published without a records request. Public procurement libraries are far richer than portal search suggests, because the artifacts sit inside project document libraries rather than being individually indexed.

### 7.1 Project roles — decided (#3)

There was never a tradeoff about which projects to *have*; the only real decision was which single project gets **sealed as the eval holdout**, since M7's recall number is honest only if measured on documents never used for prompt development. The right question turned out to be *which corpus can we afford to lock away* — and the answer is the one we'd miss least.

| | Project | Role |
|---|---|---|
| **A** | **TxDOT NHHIP Segment 3C-2**, Harris County (CCSJ 0500-08-001) | Develop freely. Five dated UCM revisions (supersession stream), SUE A/C, 11 executed agreements to 1963. In the ledger. |
| **B** | **FDOT SR 789 @ Broadway Roundabout**, Longboat Key (FPID 453730-1-52-01) | **Sealed until M7.** Manifest may be written (URLs only); no extraction or prompt iteration against it. Different agency and layout make M6's config-only claim a real test; 66 fully-populated rows are the gold set. It drew the short straw because it is the corpus development needs least — static, spine-only, and five TxDOT matrices already cover matrix development. |
| **C** | **TxDOT SH 99 Grand Parkway Segment B-1** | Develop freely. Its `Utility Owner Meeting Notes` are the only coordination minutes anywhere in the public corpus — sealing it would have sterilized the one document set the minutes extractor needs. **145 dated per-owner notes, 16 owners, biweekly, Apr 2024–May 2025**, plus three dated UCMs and two dated permit-status exports (deferred). |

### 7.2 Spine — confirmed

**TxDOT design-build Reference Information Documents.** 21 of 30 alternative-delivery projects have live `/rid.html` pages. Parse the program index rather than constructing slugs — guessed slugs mostly fail.

- **NHHIP 3C-2** — [RID index](https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/rid.html). **Opened and verified** (work item 3), no longer manifest-asserted. All five dated matrices extracted and read:

  | Revision | Title | Rows | Data Source col | Conflict col | SUE col |
  |---|---|---:|:---:|:---:|:---:|
  | 6/20/2025 | Utility **Inventory** Matrix | 622 | — | — | — |
  | 7/22/2025 | Utility **Conflict** Matrix | 522 | — | — | — |
  | 10/24/2025 | Utility **Inventory** Matrix | 680 | ✓ | — | ✓ |
  | 12/15/2025 | Utility **Conflict** Matrix | 561 | ✓ | ✓ | ✓ |
  | 2/13/2026 | Utility **Conflict** Matrix | 707 | — | ✓ | ✓ |

  Every row carries stationing (`1149+00` → `1153+17`), owner, type, size, material, OH/UG, baseline, parallel/crossing/perpendicular, alignment, start/end location, offsets, and L/R. Owners in the current revision: AT&T Texas (SWBT) 141, Comcast 60, Lumen 37, Verizon/MCI 36, Phonoscope 14, CenterPoint Energy 7, and 8 more.

  **Utility IDs are stable across revisions**, which is what makes this a stream rather than five unrelated snapshots: 66–87% carry over between consecutive revisions, and **254 IDs appear in all five** — a trackable cohort spanning eight months.
- **SH 99 Grand Parkway Seg B-1** — [RID](https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/sh99-grand-parkway-segb1/rid.html). Contains `Utility Owner Coordination/Utility Owner Meeting Notes and Exhibits.zip`, three dated UCMs, and two dated `RULIS_Utility_Permit_Applications_and_Status` zips — a literal permit-status time series.
- **US 290 Design-Build** — [RID](https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/us290-db/rid.html). One filled UCM (8/19/2024), SUE from two vendors three years apart. Spine only, no stream.
- **I-35 NEX South** — [RID](https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/i35-nex-south/rid.html). UCMs are **XLSX, not PDF** — structured rows. Two versions eleven days apart: one contradiction test case, not a series.

**WSDOT open contracts.** [ftp.wsdot.wa.gov/contracts/](https://ftp.wsdot.wa.gov/contracts/) — 392 contracts, browsable, no request required. Contract 9540 (I-5 to SR 509) has an Appendix U utility file and Appendix RR railroad file; sibling 9424 (SR 509 Stage 1B) has a structurally identical Appendix U on the same corridor — the cleanest same-shape A/B pair found.

**TxDOT letting proposals.** Bid proposals carry filled utility tables with real stationing (`Cinco MUD 1 crossing SBFR at STA 2414+50`), railroad DOT crossing numbers, and right-of-way parcel tables. Free, loginless, curl-able. Three cautions from verification: the artifact is **not** reliably numbered `SP 000-225` (the number varies per project — grep the title *"Important Notice to Contractors"* and the phrase *"have not been cleared"*); the **column schema has at least three variants**, including a four-column layout with no stationing; and the base rate is **6.25%**, not the 17% first claimed — budget ~16 downloads at ~7 MB each per usable record set.

**FDOT Utility Work Schedules (Form 710-010-05).** [ftp.fdot.gov](https://ftp.fdot.gov/public/folder/hkswlk59g0qrnsajuh3xxg/permitsandorutilityworkschedu/), organized district → FPID → per-owner PDF, mandated per project so coverage is systematic. Section C carries `Act. No. | Work Activity Description | Dependent Activity | Calendar Days Prior to/During` — a per-owner dependency record with **explicit dependency edges and durations**. See §7.6. Caveats: the folder 403s so filenames must be found via search indexing; the PDFs are scanner output and need OCR, so a first-pass `pdftotext` returning little would wrongly suggest they're blank.

### 7.3 Stream — confirmed

**Texas county and city monthly transportation reports** — the strongest verified slip signals found anywhere.

- [Denton Friday Staff Reports](https://www.cityofdenton.com/510/Friday-Staff-Reports-to-City-Council) — I-35 North corridor, per-owner buckets. Verified transitions Feb→May 2026: Bolivar WSC, CenturyLink/Brightspeed, and UTRWD all move from *currently relocating* to *clear of construction*. Verified slip on CSJ 0196-01-109: Utility Relocations Complete March 2026 → April 2026. **The utility-bucket schema is only confirmed present from ~10/2025 onward** — the 2017-2026 archive exists but the payload does not span it; bound the start date before sizing.
- [Rockwall County Consortium Reports](https://www.rockwall.com/documents/ConsortiumReports/2026/February%202026.pdf) — FM 552 (CSJ 1017-01-015) shows Utility Relocations Complete: December 2021 → November 2024 → December 2026. A five-year multi-hop slip chain on one named project.
- [Kaufman County Transportation Reports](https://www.kaufmancounty.net/DocumentCenter/View/6189/08-August-2023-Kaufman-County-Transportation-Report) — per-owner narrative form.

**Arkansas utility-owner minutes** — the coordination seen from the *other side of the table*.

- [Centerton Waterworks & Sewer Commission](https://centertonutilities.com/documents/626/Commission_Meeting_09.16.2025.pdf) — ARDOT Hwy 102 relocations. Verified 4-5 month slip within the series.
- [Cabot Water & Wastewater Commission](https://cabotwaterworks.com/documents/1057/Commission_Meeting_Packet_2026-02-26.pdf) — minutes plus a capital project ledger across four named ARDOT projects.

**Transit and program status reporting.**

- **HART Honolulu monthly progress reports** — a *Utility Agreements Status Matrix* republished monthly, with variance computed in days (Airport Section Utilities: 228, 217, 219, 228, 216, 372 days late). Verified honestly: only **11 editions carry the matrix** (Mar-Dec 2012 consecutive, plus Nov 2014), not the 23 first claimed; five early editions are scanned images needing OCR.
- **Purple Line FTA monitoring reports** — 24 monthly editions, a dedicated *Utility and Third Party Agreements* section with per-owner narrative: CSX, WMATA, Verizon, WSSC, Pepco, MARC, Washington Gas.
- [HRBT Expansion monthly construction reports](https://hrtac.org/197/Construction-Reports---HRBT-Expansion-Pr) — 75 editions with a printed contract-vs-forecast slip table. **Utility density is thin and uneven** — sampled editions yield 0-7 mentions, mean ~2. Archive closed August 2025.
- [California HSR Central Valley Status Report](https://hsr.ca.gov/wp-content/uploads/2025/12/CVSR-2025-12-Data-2025-10-FINAL-V0-A11Y.pdf) — per-utility-type counts whose denominators move between editions while the program total stays pinned. A contradiction signal in itself.

### 7.4 Gold set — confirmed filled matrices

- [FDOT SR 789, FPID 453730-1-52-01](https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27114/45373015201-utility-conflict-matrix.pdf) — 66 rows, 9 owners, fully populated.
- [CDOT US 6 Bridges utility matrix](https://web.archive.org/web/2id_/https://www.codot.gov/projects/US6Bridges/other-documents/rfp/addendum-2/reference-documents/bk-2-sec-7/utility-matrix-11-27-12-revisions-ii.pdf) — 79 conflict IDs, richest schema, with inter-utility sequencing prose (*"CENTURY LINK CAN'T BE RELOCATED UNTIL XCEL HAS COMPLETE THEIR WORK"*).
- [FDOT I-75, FPID 444008-4](https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy24-25/ad--25115/444008-4-utility-conflict-matrix-(no-conflicts).pdf) — an explicit *no conflicts* matrix. Keep it: a correct empty result is a test case extraction will otherwise fail.

### 7.5 Retrieval mechanics

Verified the hard way; each of these silently produces wrong results.

- **Box shared links return 404 to `HEAD` and to default curl user-agents**, and resolve fine on `GET` with a browser UA. A checker that only HEADs will report every TxDOT RID folder as dead. Box pages are JS-rendered: regex the embedded JSON for `"name"`, `"size"`, `"typedID":"f_<id>"`, then download via `https://app.box.com/index.php?rm=box_download_shared_file&shared_name=<SHARED_ID>&file_id=f_<FILE_ID>`. Zips are downloadable unauthenticated via HTTP 206 range requests.
- **A URL appearing in Wayback CDX output does not mean it is retrievable** — CDX lists URLs captured only as 404s and 301s. Request `fl=timestamp,statuscode` and filter `statuscode==200`.
- **Wayback `id_` replay can return a 403 or an HTML error page that looks like success by HTTP code.** Always check `file -b --mime-type`.
- **Wayback captures can be silently truncated.** Compare `x-archive-orig-x-crawler-content-length` against `content-length`; a mismatch means an unusable capture despite the 200.
- **Treat a CDX digest change as a hypothesis, not evidence.** One lead claimed two dated states of the same register; cell-by-cell diff of the full 195×53 sheet found *zero* differing cells — the binary delta was OLE container metadata from a re-save.
- Scanner-output PDFs (FDOT work schedules, early HART editions) need OCR. A thin first-pass `pdftotext` means "scan", not "blank".

### 7.6 Two findings that affect the build

**Document series change schema mid-stream — including within a single project's own matrix.** This is now confirmed at the sharpest possible altitude. Across NHHIP 3C-2's five revisions of *the same document*: the title alternates between "Utility **Inventory** Matrix" and "Utility **Conflict** Matrix"; a `Data Source` column appears in revisions 3 and 4 and is absent in 1, 2, and 5; `Potential Conflict` appears only from revision 4; `SUE Level` only from revision 3; and `Size (inches, strands)` becomes `Size (in)`. At least three distinct column schemas in eight months, from one author, on one project.

The Rockwall reports do the same thing — per-owner narrative lines under *"Status of utilities in conflict"* in October 2024, a four-bucket form by February 2026 — as do TxDOT bid proposals (three confirmed variants, one lacking stationing entirely).

The extractor is written against a *record type* with layout variants, never against one observed layout. A parser keyed to one revision's shape returns zero rows on the others **without erroring** — which is exactly how this was nearly mis-reported during verification: a first-pass regex tuned to the 2/13/2026 layout found 345 rows there and 0 in two other revisions that in fact hold 622 and 561.

**Individual documents are not addressable; only the archive is.** The five matrices exist solely inside the 208 MB Box package. Their filenames on the TxDOT CDN return **HTTP 404 with a 145 KB HTML error page** — a well-formed body and a status code you must actually check. The manifest therefore cannot point at a document; it points at an archive plus a member path. Work item 5's fetcher and work item 4's manifest schema both have to account for that.

Reading the archive does not require downloading it: `zipfile.ZipFile` accepts any seekable file object, so an HTTP-range-backed reader lists a 208 MB zip and extracts single members over a few hundred KB of transfer.

**FDOT Utility Work Schedules encode dependency-to-dependency edges.** The `Dependent Activity` column states that one owner's work activity depends on another's. The v0 data model has no Dependency→Dependency relation — dependencies link to milestones, not to each other. This is a real gap in the model, not merely an unsupported source, and it may be the more natural spine shape for the domain. **Deliberately deferred:** v0 ingests these as ordinary Dependencies and drops the edge. Revisit before M8; it likely warrants an ADR.

## 8. Work items

| # | Item | Exit criteria |
|---|---|---|
| 1 | ~~Shortlist candidate projects~~ | **Done** — §7, 54 verified leads across six channels |
| 2 | Confirm Project A and Project B | §7.1 pairing accepted or the SH 99 alternative chosen, with the M6 trade-off recorded |
| 3 | ~~Open the NHHIP utilities package~~ | **Done** — all 21 members listed and all five dated matrices extracted and read. Findings in §7.2 and §7.6. The 39 MB agreements package is still unopened; lower risk, since nothing downstream depends on it yet. |
| 4 | Write `corpus/manifest.yaml` for Project A | Every §2 minimum-viable row satisfied |
| 5 | Build the manifest fetcher | `make corpus` resolves the manifest to files on disk; re-run is a no-op; drift is flagged; Box and Wayback quirks in §7.5 covered by tests |
| 6 | File records requests | Scoped to **email correspondence** (§5), the one artifact class no public source yielded. Tracking numbers recorded. |
| 7 | Bound the Denton series | Determine the earliest issue carrying the utility-bucket schema. The archive spans 2017-2026; the payload does not. |
| 8 | Build synthetic fixture set | Covers every code path in §6; `is_synthetic` enforcement verified by a failing-then-passing test |
| 9 | Repeat 4–5 for Project B | Held out — not opened during prompt development |

## 9. If the corpus does not materialize

The §7 sweep retired most of the original risks here — filled matrices, dated revisions, and coordination minutes all turned out to be public. What remains:

- ~~The NHHIP zips don't contain what the index says.~~ **Retired.** Opened and verified: 622–707 rows per revision, full stationing, 254 utility IDs trackable across all five. Project A is confirmed on observed evidence.
- **Cross-revision tracking is harder than the row counts suggest.** Utility IDs are stable, but the schema is not: columns that carry conflict status and SUE level exist in only some revisions, so a field can appear to "change" between revisions when it was simply absent from one of them. Distinguishing *absent* from *changed* matters directly to `CONTRADICTION`, which must not fire on a column that did not exist.
- **All records requests refused or priced out.** Everything except the email extractor proceeds unaffected. That extractor gets built and stays unexercised on real correspondence; record it as a known gap rather than letting it look tested. No public source in six channels yielded DOT-to-utility email.
- **Denton's utility-bucket schema turns out to be a short window.** The verified transitions are real but concentrated in late 2025 onward. If the window is too thin, Rockwall's FM 552 five-year slip chain is the stronger stream anyway.
- **Project B never materializes.** M6's config-only generalization claim cannot be made. Do not substitute a second document set from Project A and call it generalization.
