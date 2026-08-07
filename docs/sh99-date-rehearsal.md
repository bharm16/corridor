# The date rehearsal: SH 99 Grand Parkway, bounded

Decided 2026-08-07. The City of Houston rehearsal proved the half of the product that
organizes conflicts — the Ledger, Adjudication, the cited report. This rehearsal proves
the half that reads dates: events on the record, Committed Dates, slips, and the
Exceptions that fire on them. The feed is SH 99 Grand Parkway, whose meeting minutes
have already been extracted: 1,401 dependency Candidates and 1,629 event Candidates,
all pending, none admitted.

## What the interview settled

Three decisions, reached by grilling on 2026-08-07 and recorded before any build:

1. **Events enter by policy or by Adjudication, and a model may only demote**
   (ADR-0026). The operator refused per-row clicking of near-certain records; the
   answer is a named, versioned event-admission policy the operator authorizes once,
   whose every check is a replayable computation. Events failing any check fall to
   Adjudication. A model verdict may flag an event into the human pile, and can never
   admit one.
2. **A project-side actor can never set a Committed Date.** The data forced this: most
   extracted SH 99 "commitments" are LJA — the project's own engineer — taking action
   items, not an External Party promising delivery. The masquerade rule the
   coordination lanes already enforce for dates extends to events' actors.
3. **The cohort is selected by stated rule, and the rule was adopted, not each row.**
   A conflict enters if at least one dated commitment, slip, or closure event
   references it. That selects 37 conflicts — pipeline parties almost entirely: Energy
   Transfer, Kinder Morgan Tejas, Enterprise, Denbury Green, Buckeye, Chevron, Air
   Products, Air Liquide, DOW, ExxonMobil, Equistar, Florida Gas Transmission, Shell,
   Phillips66, CenterPoint (electric) — carrying roughly 267 dated date-bearing events.
   The operator adopted the rule's output in place of per-row review
   (out/sh99-date-cohort-approval.txt records this; the per-row wizard at
   scripts/sh99-cohort-wizard.sh would supersede it if ever run).

## What the data honestly holds

Read before scoping, twice — the first reading got it wrong. The minutes' event dates
are overwhelmingly meeting dates: only 24 of 1,629 events carry an explicit
delivery date. The date machinery cannot be demonstrated on delivery-date *pairs*,
because none exist. What the data does hold, verified quote by quote:

- **Three real slips**, stated in the minutes themselves. Kinder Morgan: "The March
  2026 completion timeline seems unattainable. Propose extending to May 16th."
  Kinder Morgan again: a schedule "pushed on to 06/2026 but is dependent on receiving
  drawings in a timely manner." Enterprise: "ROW, Util Agreement, Execution – 7/2025
  04/2026" — the old date struck through beside the new one. All three are Candidates
  with verified citations (ids 7296, 7306, 7554). None carries a conflict reference,
  so attaching each to its Dependency is a human act in the queue — which is the
  residue path working as designed, not a defect.
- **The slips contradict Milestones the project already holds.** Three Milestones are
  loaded: design completion 2025-01-31, ROW and utility agreement execution
  2025-07-31, relocation construction completion 2026-03-31. Enterprise's slip moves
  the second; Kinder Morgan's calls the third unattainable. An External Party telling
  the project it will miss a date the project is holding is the exact event the
  product exists to catch.
- **One real overdue**: Air Products committed to 2025-05-08 on PL35, which has
  passed; whether a closure event answers it is Adjudication's question to settle on
  the record.

## The claim to earn

*For a bounded set of Grand Parkway conflicts, Corridor turns two years of meeting
minutes into a cited, dated record on each conflict — the project's own action items
distinguished from the External Party's statements — and surfaces what that record
supports: commitments whose stated date passed without closure, and an External Party
moving a date the project is holding.*

## Exit criteria

1. The event admission path exists per ADR-0026: policy gate first, human residue
   second, model demotes only, receipts and abstention mirroring the carry-forward
   family.
2. The actor boundary is enforced and tested: no project-side event sets a Committed
   Date.
3. At least one of the three real slips is on the record — attached by human act,
   both dates preserved, cited to its page.
4. OVERDUE fires on a real Committed Date and appears in a report section with
   field-exact provenance.
5. The work is timed, end to end. The timing is the acceptance measurement and the
   only sanctioned source for any future effort estimate — the City rehearsal's
   140 decisions/hour rule applies unchanged.

## Boundaries stated up front

- Slip is demonstrated from explicit minute statements, not from delivery-date pairs;
  a revision-to-revision slip demonstration waits for a stream that carries real
  delivery dates. The verified routes to such a stream — NCDOT's public bid proposals
  with day-precision utility commitments, and TxDOT ROW-U-35 assemblies by records
  request — are documented in docs/research/dated-commitment-sources.md and are not
  this rehearsal's path.
- DUE_SOON stays dark unless cohort Dependencies are linked to the loaded Milestones
  during Adjudication; linking is permitted, not required, and no criterion depends
  on it.
- The cost of event admission at scale is measured here, not solved here; the policy
  gate is the proposed answer and this rehearsal is its first test.
- WSDOT 9424's `committed_date` values are a mislabeled *Relocation Estimated Date*
  column — the DOT's own estimate, not a party's promise (see the research note).
  They are out of scope and the field naming defect is its own follow-up.

## Build order

1. Register the cohort boundary: the 37 conflicts, the rule, and the adoption record,
   as a pinned set the queue lane reads — the Cohort Receipt pattern, derived from a
   stated rule over the event stream rather than a Revision Comparison; the glossary
   entry lands with the build.
2. Admit the 37 Dependencies through the queue's bounded lane, City-rehearsal style.
3. Build the event-admission policy machinery: policy document, authorization,
   per-event outcome receipts, abstention; then run it over the cohort's events.
4. Adjudicate the residue in the queue — including attaching the three slips by hand.
5. The Exceptions already read `committed_date` and closure events; verify OVERDUE
   and MISSING_DATE against the admitted record rather than building anything new.
6. Ship the report section with field-exact provenance; run it; record the timing.

Each step is a ticket with its own branch and PR, red test first, full suite once per
batch, review after — the pattern that carried #166–#178.
