---
status: superseded by ADR-0075
domain: product
scope: historical
---

# The machine keeps the record; people do coordination

> **Superseded by [ADR-0075](0075-corridor-maintains-the-accepted-coordination-baseline-from-project-evidence.md), 2026-09-01.** The research below stands as research; the spreadsheet-incumbent, market-emptiness, and extraction-economics premises are no longer the product's foundation.

On 2026-08-30 the maintainer asked whether this product is needed at all — whether existing systems already handle utility conflict coordination. Three research passes answered it: a competitive landscape of every findable system, the documented evidence of the problem, and how the record-to-action loop is occupied today. This ADR records the verdict, the value framing that survives it, and the open questions it produced.

## What the research established

**The incumbent is a spreadsheet.** The dominant practice is the SHRP2 R15B Excel utility conflict matrix plus email and a document management system, maintained by hand ([Iowa DOT's October 2025 UCM training](https://utilities.iowadot.gov/Utility%20Conflict%20Management%20Training%202025/Utility%20Conflict%20Management%202025-10-06.pdf) hands students an Excel template; [Michigan publishes its UCM as an .xls](https://www.michigan.gov/mdot/-/media/Project/Websites/MDOT/Business/Permits/Utility-Coordination/URTS-Utility-Conflict-Matrix-SHRP2-R15B.xls?rev=45033c6b9f434a35a79e21eb1d8d5fb2&hash=7B61120056147E9EE91F6CDB2D0BD69A); FDOT's PMG 245 directs the engineer of record to keep a conflict spreadsheet).

**The category died twice on the cost of manual entry.** TTI's UACT prototype (2007) put conflicts, agreements, documents, and stakeholders in one database and [never left prototype](https://static.tti.tamu.edu/tti.tamu.edu/documents/0-5475-1.pdf). The SHRP2 "advanced UCM" database was never deployed; the official [2019 lessons-learned document](https://shrp2.transportation.org/Documents/Renewal/Utilities%20final%20documents/SHRP2%20R15B%20Lessons%20Learned%2005_08_19.pdf) retreated to "when in doubt, pursue a standalone UCM implementation" — the spreadsheet — citing the staff burden of populating and maintaining conflict lists. The record cost more to keep than it returned, because people had to keep it.

**The problem is large, persistent, and officially still open.** A congressionally mandated [GAO study (1999)](https://www.gao.gov/products/rced-99-131) found 22 states with utility delays on more than 10% of projects and states largely unable to track the delays at all; [NCHRP's 2024 synthesis](https://www.nationalacademies.org/read/27859/chapter/4) calls construction-phase conflict management "a substantial gap"; FHWA's [current EDC-8 round (2026–27)](https://www.fhwa.dot.gov/innovation/everydaycounts/edc_8/) still leads with utility delays. The documented failures include precisely the dropped-ball kind: coordinators unaware the construction schedule changed, design decisions avoiding utilities left undocumented, a [state audit finding no centralized way to monitor relocation progress](https://omb.ri.gov/sites/g/files/xkgbur751/files/2022-10/RIDOT%20Utility%20Relocations%20Oversight%20Final%20Audit%20Report.pdf), and Michigan bolting a module onto its system just to record which utilities were contacted and who responded.

**Nobody does the reading; nobody does the watching; acting is barely occupied.** No system — commercial, state, or research — extracts coordination facts from the documents that already exist. The action layer is occupied by exactly one system, [PennDOT's URMS](https://www.pa.gov/agencies/penndot/programs-and-doing-business/road-design/right-of-way-grade-crossing-and-utilities/utility-coordination/urms-information): role-based task assignment, system-sent notices to utilities, generated clearances and notices to proceed — single-state, government-owned, fed by manual entry, credited with $33M+ savings. Even URMS has no chasing loop: no reminder engine, no call lists, no "no response in N weeks → next touch." The 811 ticket systems (Boss811, KorTerra, Irth) prove the dispatch-monitor-escalate pattern at scale in the adjacent damage-prevention niche; nobody applies it to relocations.

## The decision

**Corridor's value is the labor it removes and the attention it automates — never the record itself.**

1. **The machine does the reading.** Facts enter the record from the documents coordination already produces — minutes, matrices, agreements, correspondence — with no data-entry role anywhere. This is not a feature on top of the category; it is the organ whose absence killed the category's two prior attempts.
2. **The machine does the watching.** Slipped commitments, contradictions between sources, stale items, and unowned actions surface themselves on the Work List. The documented failure mode of current practice is silent: a dropped ball looks like a quiet week.
3. **The direction is a record people act out of.** Follow-ups, chase lists, and generated coordination paperwork, triggered by evidence in the record — a missed Promised For, a thread gone quiet, an unmet Required Documentation — not by position in a fixed state machine. This ground is empty everywhere, and URMS plus the 811 systems prove both halves of the pattern work.

**Citations are the trust floor, not the pitch.** Every record in this domain is derived from source documents; a hand-typed spreadsheet is "source-grounded" too. Provenance is the minimum for the record to be trusted at all, and no Corridor surface, document, or plan may present it as the differentiator.

**Extraction precision is the economic premise, not a quality metric.** The R15B failure was maintenance burden. If extraction quality forces a person to review everything, Corridor recreates that burden with extra steps. The measure that matters is displaced human-minutes per document — the fraction of facts that enter with no human touch — and it must be measured, not assumed.

## Bounds and recorded risks

- Every published ROI number in this category is agency-self-reported (TxDOT's $23M on $1.4M, Vermont's 65→25 conflicts). No independent audit exists. Corridor should instrument its own effect rather than lean on category claims.
- The top-ranked root cause of utility delay — utility owners' resources and priorities ([GAO 1999](https://www.gao.gov/products/rced-99-131)) — is not addressable by DOT-side software. The product enables the documented workaround (early identification, negotiated realistic schedules); it does not remove the cause.
- Large states build their own (Pennsylvania, Kentucky). The buyable market skews to the remaining DOTs and to the consultants currently paid to do this work by hand.
- The schedule source class follows practice, not imagination: pre-letting coordination runs on milestone and clearance-date tables (the SHRP2 UCM's one schedule field is a per-conflict Estimated Resolution Date; TxDOT SP 000-1461 hands the contractor a clearance-date table; design-build tracks per-adjustment dates in the Utility Tracking Report). Corridor ingests those date tables. Parsing raw CPM schedules is out of scope in every phase this product serves — the data flows coordination → CPM, not the reverse.

## Consequences

The open questions this verdict produces are tracked as issues: the extraction-economics gate, the evidence-driven chase loop, record-generated paperwork, the schedule source-class boundary, and the first-buyer question. ADR-0029's posture (rows enter mechanically; no gate in front of the list) and ADR-0042's authority rule are unchanged and are what make the automatic record possible.
