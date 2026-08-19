# Report Comparators for Issue #150

Researched on August 18, 2026 against official TxDOT and FDOT materials only.

Question: what do the Austin District Utility Status Report 3.0, the recurring NHHIP 3C-2 project-development status summaries, and published FDOT utility instruments carry that Corridor's current report does or does not carry?

Current Corridor baseline used for comparison: the weekly report carries summary
counts plus `Milestone readiness`, `Critical items`, `Coordination`, `External Party
commitments`, `Exceptions`, `Changes since last report`, `Aging`, and a complete
appendix. Its dependency-level `Coordination` section shows the current Internal Owner,
Next Action, and Action Due date. Its party-level section shows External Party,
supported statement, timing and precision, statement type, Commitment Scope,
open/past-due status, Internal Owner, Next Action, Action Due, and Milestone Impact.
Every cell is bound to an Assertion, Derivation, Work Decision, or attributable Verbal
(`src/corridor/report.py`, `src/corridor/report_release.py`).

## 1. Austin District Utility Status Report 3.0 is the strongest format comparator

Official sources:

- Austin District standards page, which publishes both the guideline and the current workbook template: <https://www.txdot.gov/about/districts/austin-district/district-standards.html>
- Utility Status Report Guideline, printed September 24, 2025: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-guideline.pdf>
- Utility Status Report template workbook: <https://www.txdot.gov/content/dam/docs/district/aus/specinfo/utility-status-report-template.xlsx>

What the official Austin material says:

- The USR is "a tool developed by utility coordinators to help communicate realistic schedules" and is "designed to be used by all parties including our utility partners" (guideline p.1).
- Austin says the USR is required for all Austin District projects to update utility data in TxDOT Connect, and that it exists because the Utility Conflict Matrix does not communicate realistic schedules well enough on its own (guideline pp.1-2).
- Austin distinguishes the two artifacts sharply:
  - the UCM is conflict-by-conflict and is developed early and updated through design;
  - the USR tracks schedule progress for a project-specific utility ID, not for each conflict ID (guideline p.2).
- USR 3.0 is workbook-based, formula-driven, and utility-specific:
  - one `Project Info` sheet holds highway, CCSJ, project ID, letting date, project design milestones, project type, project size, and ROW context;
  - one tab per utility ID carries that utility's schedule;
  - the workbook calculates milestone dates from durations and project parameters rather than requiring every date to be keyed manually (guideline pp.3-5; template `Project Info` sheet).
- Austin's utility tab is a milestone chain, not a freeform status note. The standard non-reimbursable tab carries:
  - `Description of Milestone`
  - `Milestone Type`
  - `Assigned`
  - `Duration (DAYS)`
  - `Estimated Date`
  - `Date Complete`
  - `Comments`
  - plus utility contact fields and brief notes (template `U_0001`).
- The default milestone chain is operationally specific. It includes, for example:
  - `Existing Utility Layout complete, provided to TxDOT`
  - `Existing Utility Layout provided to utility to verify`
  - `Send NOPC letter signed by TxDOT PM`
  - `Utility ID FORM submitted to TxDOT`
  - `Preliminary Utility Conflicts Identified (NOC letter)`
  - `Utility Relocation Design Begins`
  - `FINAL Utility Conflicts Identified (NOC letter 03-03)`
  - `Utility Relocation Design Complete`
  - `Utility Design approved by project team`
  - `NORA letter signed by TxDOT PM and sent to Utility`
  - permit, materials, bidding, construction, splicing/testing, and final removal/abandonment completion steps (template `U_0001` rows 21-41).
- The reimbursable and joint-bid variants add agreement and funding gates that the non-reimbursable tab does not have, including proof of property interest, forced betterment, easement value, utility agreement drafting/review/execution, AFA steps, payment receipt, and joint-bid letting alignment (guideline pp.5-6; hidden workbook tabs unhidden locally from the official template).
- Austin explicitly uses the sheet to expose:
  - the last milestone completed,
  - the next milestone due,
  - and the last scheduled milestone, which functions as the projected "all conflicts clear for this utility" date (guideline p.5).
- Austin also defines a concrete "all conflicts cleared" workflow: if the utility is later found not to be in conflict, the schedule is rewritten so the next milestone becomes `Utility not in conflict - No Relocation Required`, the rest of the chain is deleted, and the conflict-cleared date is entered as the estimated date with completion left blank so the summary still surfaces the update (guideline pp.9-10).

What Austin carries that Corridor's current report does not:

- A full utility-by-utility milestone lattice, including predecessor gates owned by the project team, the utility, Austin utilities staff, and contractors.
- Explicit estimated-versus-complete date pairs at each milestone step.
- Assignment at the milestone-step level (`Assigned`) rather than only one current `Internal Owner` / `Next Action`.
- Schedule variants for reimbursable, joint-bid, and AFA-dependent utility work.
- A first-class projected "all clear" date for each utility ID.

What Corridor already does better:

- Corridor is materially stronger on provenance. The current report freezes supported statements, timing precision, scope, and work-decision provenance. Austin's workbook is a coordination artifact, not an evidence-bound statement surface.
- Corridor preserves party-level uncertainty cleanly. Austin assumes the utility schedule can be expressed as one utility-ID timeline.

What matters for the Phase-1 practitioner-run exit test:

- Austin is the closest external proof that a useful practitioner report is not just "what was said" but "what milestone is next, who owes it, and when the utility will actually be clear."
- Corridor's current report has owner/action/date fields, but not the milestone chain that lets a coordinator run the whole utility meeting from the artifact alone.

## 2. NHHIP 3C-2 gives the public recurring comparator, but it is intentionally high-level

Official index page:

- Pre-procurement page listing the recurring series: <https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/pre-procurement.html>

Current dated editions listed there as of August 18, 2026:

1. June 30, 2025: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-project-status-summary-20250630.pdf>
2. August 26, 2025: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-project-status-summary-20250826.pdf>
3. October 31, 2025: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-project-status-summary-20251031.pdf>
4. December 19, 2025: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-pre-procurement-project-status-summary-20251219.pdf>
5. February 27, 2026 clean: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-pre-procurement-project-status-summary-clean-20260227.pdf>
6. July 21, 2026, published in change-marked form: <https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-pre-procurement-project-status-summary-20260721.pdf>

The page also publishes one February 27, 2026 redline companion. It is a second
presentation of the February update, not a seventh dated edition:
<https://www.txdot.gov/content/dam/docs/division/ald/nhhip-3c-2/nhhip-3c2-pre-procurement-project-status-summary-redline-20260227.pdf>.
The page does not currently provide a separate clean July companion, so the July file
must not be described as a clean edition.

What the series carries:

- It is genuinely recurring, project-matched, and public. That makes it useful as a corpus comparator and a "does the public-facing story move over time?" baseline.
- Each edition has a dedicated `Utility Information, Coordination, and Relocation` section.
- The June 30, 2025 edition reports:
  - a draft utility inventory matrix in the RIDs,
  - available SUE data,
  - City of Houston commitment coordination,
  - mailed notices of proposed construction,
  - development of a utility conflict matrix,
  - target completion of a final UCM, utility exhibit, and limited Level A/B SUE by June 2026,
  - specific sanitary sewer validation work,
  - advance transmission-line relocation coordination,
  - and a categorized list of main utility providers within the project limits (June 30, 2025 summary, `Utility Information, Coordination, and Relocation` section).
- The February 27, 2026 clean edition advances the same section to:
  - a draft UCM plus DGN and KMZ files in the RIDs,
  - utility-owner responses to the notice of proposed construction,
  - meetings to validate utilities on utility strip maps,
  - draft strip maps in the RIDs,
  - final UCM / utility exhibit / limited SUE anticipated by Summer 2026,
  - surveyed and pending sewer-line files,
  - and a refined utility-owner list that now distinguishes electric distribution and transmission (February 27, 2026 clean summary, pp.9-10).
- The July 21, 2026 edition keeps the same structure but visibly updates timing and roster details:
  - final UCM / utility exhibit / limited SUE slips from `Summer 2026` to `March 2027`,
  - railroad Exhibit A timing also slips,
  - and the fiber-owner list expands to include Lumen, Phonoscope, Zayo Group, and Logix Fiber Network (July 21, 2026 summary, `Utility Information, Coordination, and Relocation` section).

What NHHIP carries that Corridor's current report does not:

- A public, longitudinal, project-level status narrative for one real project.
- Explicit movement of package-level utility deliverables over time across published editions.
- Utility-owner roster changes and RID-availability status in a form suitable for public procurement context.

What Corridor already does better:

- Corridor is much finer grained. NHHIP does not expose per-owner next milestones, per-conflict dependencies, exact attributable statements, or evidence-backed current commitments.
- NHHIP is not a utility worklist. It is a project-development status summary for procurement audiences.

What matters for the Phase-1 practitioner-run exit test:

- This series is evidence that recurring public status documents exist and evolve.
- It is not a replacement for an owner-by-owner coordination artifact. It belongs in the corpus plan as a recurring comparator, not as the target shape of the weekly meeting report.

## 3. FDOT's official statewide instruments are process-heavy, schedule-aware, and not a weekly utility report

Official statewide entry points:

- FDOT Utilities landing page: <https://www.fdot.gov/programmanagement/utilities/default.shtm>
- Utility Accommodation Manual (current utility page link): <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/programmanagement/programmanagement/utilities/docs/uam/uam2017.pdf?sfvrsn=d97fd3dd_0>
- Utility Procedures Manual, Topic No. 710-030-001, adopted March 10, 2021: <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/programmanagement/utilities/docs/uam/700-030-001-adopted.pdf?sfvrsn=5c101b2c_17>
- Current District One procurement library, which publishes filled schedules and
  conflict matrices: <https://www.fdot.gov/procurement/marketingD1/default.shtm>
- Filled City of Sebring Water Utility Work Schedule, FPID 452621-1-52-01:
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27112/452621-1-52-01-city-of-sebring-uws-water.pdf>
- Filled Utility Conflict Matrix, FPID 451361-1-52-01:
  <https://fdotwww.blob.core.windows.net/sitefinity/docs/default-source/procuement_marketingd1/documents/fy26-27/ad--27112/45136115201-phiiir_utility-conflict-matrix.pdf>

What the official statewide materials establish:

- FDOT's utility rule formally incorporates three statewide forms: `Utility Permit`, `Utility Work Schedule`, and `Utility Work Estimate` (UAM, Rule 14-46.001(2)(a)-(c)).
- FDOT's manualized process starts with notices and conflict identification, not with a public weekly dashboard:
  - kickoff discussion covers project issues, schedules, roles, utility-locate accuracy, methods, and all known or potential utility conflicts (UPM §3.4);
  - first utility-owner contact must include current plans, a markup-return deadline no earlier than 30 days, and reimbursement-responsibility documentation requests (UPM §3.5);
  - field review documents attendees, utilities in the project limits, known conflicts, and possible resolutions (UPM §3.6).
- FDOT does use a conflict-tracking instrument, but it is intentionally loose statewide:
  - the DUO "may provide a spreadsheet (often referred to as a conflict matrix) or other document" for the EOR to track utility conflict resolution for each project phase (UPM §3.8.1);
  - for utilities remaining in place, the DUO should collect No-Conflict Letters from utility representatives (UPM §3.8.2).
- FDOT's strongest schedule instrument is the Utility Work Schedule, and it is more dependency-aware than Corridor's current report:
  - if work depends on the contractor, another utility, or another party, a Utility Work Schedule covering those dependent activities must be obtained from that performing party;
  - the schedule must include the date the party will submit the Utility Permit;
  - the EOR must review and sign the schedule to ensure conflicts are resolved and the work is compatible with traffic-control phasing, construction sequence, and other utility work;
  - the DUO then reviews and approves it, compares it against the Utility Work Estimate, and checks dependency alignment before certification (UPM §3.11.1).
- The filled City of Sebring schedule shows what that authority becomes in a real
  project artifact: project and plan identity, UAO contacts, UAO/EOR/FDOT signatures,
  special conditions, then activity number, facility, from/to station and offset,
  work description, dependent activity, TCP phase, and consecutive calendar days
  before/during construction. It also requires written notice before starting,
  stopping, resuming, and completing work. Those are relative durations and control
  events, not planned calendar start/finish dates.
- The filled 451361-1 matrix shows the district variation the UPM permits: conflict
  number, owner, facility description, station, offset, baseline, conflict description,
  verification-hole reference, comments, and resolution state. It is a conflict
  register, not a recurring status report.
- FDOT's certification gate is schedule-and-agreement based:
  - before advertisement, the DUO certifies that all necessary utility agreements and Utility Work Schedules have been executed and that the certification letter states the status of each utility owner as it pertains to the project (UPM §3.13).
- FDOT also retains a statutory notice path when a utility blocks the project:
  - the DUO must send a notice under section 337.403(1), Florida Statutes, stating that the utility is unreasonably interfering with the state road work and generally describing the work required;
  - if the utility does not respond or cannot perform, a second notice informs the utility that FDOT will perform the work and charge the utility (UPM §3.12.3).

Bounded absence result:

- I found official statewide FDOT materials for permits, work schedules, estimates, agreements, notices, conflict tracking, and certification.
- I did not find an official statewide recurring weekly utility-status-report template comparable to Austin's USR by searching the FDOT Utilities landing page, the current UAM, and the current UPM. That is a bounded search result only, not proof that no district-level or project-level weekly artifact exists anywhere in Florida.

What FDOT carries that Corridor's current report does not:

- Explicit predecessor/dependent-activity scheduling tied to permit submission timing and construction phasing.
- Formal EOR and DUO signoff workflow for the schedule itself.
- A statewide notice/escalation path when the utility does not act.
- Certification language that all necessary utility agreements and schedules are executed before advertisement.

What Corridor already does better:

- Corridor publishes attributable statements, timing precision, open/past-due status, and work decisions in one evidence-bound report surface.
- FDOT's manuals are process authorities; they do not give Corridor's freeze-and-provenance behavior.

What matters for the Phase-1 practitioner-run exit test:

- FDOT is the clearest official proof that a useful utility coordination artifact must express dependencies between utility work, permits, contractor sequencing, and agreement/certification status.
- Corridor's current report has action ownership but not the schedule grammar to express those predecessor relationships directly.

## 4. Comparison against Corridor's current report

| Theme | Austin USR 3.0 | NHHIP recurring summaries | FDOT statewide instruments | Corridor current report |
|---|---|---|---|---|
| Primary unit | one utility ID tab | one project summary edition | one project process package | one dependency / one party statement |
| Schedule grain | step-by-step milestone chain | package-level narrative | work schedule plus dependent activities | current statement plus one current plan |
| Who owes the next move | explicit `Assigned` per milestone | usually implicit or organizational | EOR, DUO, UAO, contractor responsibilities defined by procedure | `Internal Owner` plus `Next Action` |
| Dates | estimated and completed dates for every milestone | milestone and anticipated-deliverable dates at project level | permit timing, dependent activities, certification timing | commitment timing, action due, milestone impact |
| Conflict handling | schedule can be rewritten to `No Relocation Required` | reports evolving matrix / SUE / strip-map status | conflict matrix or equivalent, no-conflict letters, statutory notices | evidence-backed dependency and commitment facts |
| Best use as comparator | report/worklist shape | recurring corpus and public change log | process and dependency grammar | evidence-bound truth surface |

## 5. Recommendation

Do not imitate any one external artifact whole.

For the practitioner-run Phase-1 exit test, the strongest externally grounded
hypothesis is that the gap is schedule expressiveness, not provenance. Corridor is
already stronger than these comparators on provenance, but only a practitioner-run
meeting can establish which additional schedule fields are actually necessary.

The Austin and FDOT material together suggest the surface to test:

- one coordination view that can answer, for each utility-facing subject, `what phase are we in`, `what is the next milestone`, `who owes it`, `what predecessor is blocking it`, `when is it estimated`, `when was it completed`, and `when is the utility clear`;
- plus explicit gates for notices, agreements, permits, and dependent activities where they matter.

The NHHIP series should be added to the corpus plan as a recurring public comparator, but it is not the target operating report for the weekly meeting.

Do not copy Austin's milestone taxonomy or FDOT's certification workflow into Corridor
from desk research alone. First test whether a practitioner needs the generic concepts -
current phase, next milestone, assignee, predecessor, estimated/completed dates, and
clearance - then model only the concepts the meeting actually uses.

## 6. Ambiguities and cautions

- Austin's USR is district-specific and utility-ID-centric. It is a strong format comparator, not a statewide norm and not evidence that its exact milestone taxonomy should become Corridor's.
- FDOT's `conflict matrix` is not exposed as one statewide standardized template in the manuals; the UPM deliberately allows "a spreadsheet ... or other document." That weakens any claim that Florida has one canonical matrix artifact.
- The February 27, 2026 NHHIP redline is a change-marked companion to the clean
  edition, not a new dated edition. The July 21 file is itself change-marked and has no
  separate clean companion on the current index.
