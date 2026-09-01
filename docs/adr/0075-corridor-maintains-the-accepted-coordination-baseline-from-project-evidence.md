---
status: accepted
domain: product
scope: current product
supersedes:
  - ADR-0066
---

# Corridor maintains the accepted utility coordination baseline from project evidence

**Supersedes ADR-0066.**

ADR-0066 answered "is this product needed" on 2026-08-30 and recorded a product verdict on top of three research claims: the incumbent is a spreadsheet, nobody does the reading or the watching, and extraction precision is the economic premise. The 2026-09-01 strategic review found that those three claims were doing foundational work they cannot bear. Agencies and consultants do run utility management systems (PennDOT's URMS, Michigan's module, vendor PMIS and document-control products); coordinators do read the documents they receive; and the R15B failure was maintenance burden, which extraction precision alone neither causes nor cures. ADR-0066 made those claims the premise of its decision, so a superseding ADR is required rather than another amendment paragraph. The research ADR-0066 cites remains valid as research and is not retracted; what changes is what the product is built to be.

## Decision

Corridor's initial product is **a source-grounded change-control and exception layer that keeps an existing utility coordination record current from the documents, email, and schedule updates the project already produces.**

- **The initial workflow is late design through construction**, after a Utility Conflict Matrix (UCM) or comparable coordination record already exists. Corridor does not originate the record from a blank page.
- **The first commercial buyer is a utility-coordination, design, program-management, or construction-management consultant managing multiple projects.** Agency teams are users from the first pilot and later direct customers.
- **Corridor imports the existing UCM or utility-management-system export as the accepted starting baseline** (the Adopt Baseline operation of ADR-0076).
- **Corridor monitors new evidence and proposes changes to that baseline.** New sources become captured facts; differences from the accepted record become proposed deltas that a person or a separately released narrow policy resolves (ADR-0076).
- **Corridor generates the updated UCM, change summary, chase list, and weekly report in the customer's existing formats.** The customer's artifact is the output; Corridor does not ask the customer to adopt a new one.
- **Corridor integrates with, rather than attempts to replace, PMIS, document control, GIS, SUE, CAD, and CPM scheduling systems.** Sources enter through connectors (ADR-0078); schedule dates are ingested from the scheduling system's own exports, never recomputed.
- **Utilities are the first wedge.** Railroads, right-of-way, permits, environmental commitments, and other external-party constraints are later expansion areas. They are not current marketing claims.

### Claims the product may not make

No Corridor document, screen, plan, or sales material may claim that no utility software exists, that nobody reads construction documents, or that spreadsheets are the only incumbent. The accurate statement is narrower: the coordination record is maintained by hand in whatever system holds it, the evidence that should change it arrives faster than people can re-enter it, and the gap between a new source arriving and the record reflecting it is where commitments are missed.

### Outcomes Corridor controls

The directly controlled outcomes are record-maintenance time, evidence-to-record latency, exception detection, continuity when a coordinator changes, and report-preparation time. Delay prevention remains a downstream outcome to measure, never a promised effect. ADR-0066's caution that every published ROI number in this category is self-reported stands.

### The economic premise

ADR-0066's "extraction precision is the economic premise" is replaced with:

**The economic premise is net operator time removed while maintaining an acceptably current and accurate record.**

Measured, per customer and per project:

- operator minutes per active project per week;
- time from new source arrival to accepted update;
- proposed-delta accept-without-edit rate;
- edit, rejection, reversal, and false-write rates;
- material changes missed (sampled);
- false or low-value exception rate;
- paid continuation or expansion.

Extraction precision is one input to that equation, not the equation itself. A precise extraction that produces a low-value delta costs operator time; an imprecise one that is rejected costs more. The measure is the net.

## What survives from ADR-0066

The three mechanisms ADR-0066 named survive with narrower framing. The machine reads the documents coordination already produces, but what it produces from them is a captured source fact and a proposed delta, not an unattended record write (ADR-0076). The machine watches for slipped commitments, contradictions, stale items, and unowned actions, and surfaces them as exceptions. The record remains something people act out of. Citations remain the trust floor and never the pitch. The schedule source-class boundary (ingest date tables and exports; never parse or compute CPM) is unchanged and is restated in ADR-0078.

## Considered options

**Amend ADR-0066 in place.** Rejected. Its spreadsheet, market-emptiness, and extraction-economics claims are the premises of its decision, not consequences of it. A superseding ADR leaves the research citations readable as history without presenting the premise as current.

**Keep "the machine keeps the record" as the positioning and adjust marketing copy.** Rejected. The positioning drove architecture: mechanical record writes with no gate (ADR-0029) are what an unattended record-keeper needs, and the baseline-plus-delta model needs something different (ADR-0076). Positioning and write authority have to change together.

## Consequences

- ADR-0066 becomes `superseded by ADR-0075`. Its body is not rewritten.
- README, the roadmap, and every customer-facing description are updated in the same change so no "spreadsheet is the only incumbent" or "nobody reads" claim remains elsewhere.
- The first paid product slice is: import existing UCM, adopt baseline, connect one mailbox or folder, process one new source, show proposed deltas, accept/reject/edit, export the updated UCM and report. That slice precedes broader readiness checklists, mobile support, offline drafts, generalized notifications, and any further automatic policy family.
- The temporary product-validation gate for that slice lives in [docs/pilot-success-criteria.md](../pilot-success-criteria.md), not in an ADR. ADR-0046's simulated Product Test Run does not substitute for it.
- Until the baseline-plus-delta pilot is running, expansion of the following stops: the universal documentation-readiness system (ADR-0052, ADR-0056, ADR-0060); the generalized task-management surface (ADR-0035); phone and offline functionality; broad notification and escalation machinery; additional automatic Record Inclusion classes; global content-inferred email routing; new legacy-table capabilities (ADR-0081); and report formats beyond the customer's required UCM and weekly artifact. Those ADRs keep their status; their scope is recorded as optional module in the decision index.
