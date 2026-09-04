---
status: accepted
domain: reports
scope: current product
amends:
  - ADR-0086
migration: the configured issue set is modelled and readable at a cutoff (#640), resolved into executable content (#641), and prepared into an immutable release candidate (#529); neither partner's actual issue obligations have been observed and nothing authorizes a package yet (#555, #533).
---

# The externally issued set is configured per project, and only the updated UCM is mandatory

**Amends ADR-0086.**

ADR-0086 decided that one attributable human authorization seals one immutable set of customer artifacts, and that the set — not the file — is the unit of external issue. It then fixed the first-slice set as four artifacts: the customer's updated native UCM, a change summary, a chase list, and the weekly Coordination Report, with a redline or provenance sidecar available where a customer wants one. That enumeration was repeated into #529, #534, #498, `roadmap.md`, and `docs/pilot-success-criteria.md`.

#560 asked whether the enumeration should survive contact with real partners, and framed the choice honestly as two legitimate options: retain the fixed four and recruit design partners who already issue all four, or make the updated UCM mandatory and the rest a per-project configured set reflecting what that partner already issues.

**The second option is chosen.** The updated native UCM is mandatory. The accepted-change summary, the chase list, the weekly Coordination Report, and any sidecar are **configured per project**, according to what that partner already issues to their own client. The configured set is fixed until an attributable configuration change; the coordinator does not choose files each week.

## ADR-0086's invariant was never the number four

The reason this is a narrow amendment rather than a reversal is worth stating precisely, because ADR-0086's argument is sound and this decision keeps all of it.

ADR-0086's actual invariant is **one coherent set**: one accepted Project Record revision, zero or one previous approved package, one source cutoff, one declared coverage state, one approved template and mapping set, and one authorization sealing all of it under one receipt. Every artifact reads the same five inputs, so "the workbook, the summary, the chase list, and the report cannot contradict one another about what the record said or when the window closed." That is the whole point, and none of it depends on how many artifacts are in the set. A package of one artifact and a package of five satisfy it identically.

What ADR-0086 rejected — and what stays rejected — is a coordinator assembling the week's issue by picking files. Its reasoning holds without change: weekly assembly turns an authorization into an editorial act and makes the issue series inconsistent from week to week for no customer benefit. **Participation is configuration**, decided once, changed only by an attributable act. This ADR widens *which* artifacts configuration can vary; it does not move that decision into the weekly loop.

The fixed enumeration, by contrast, carries a cost ADR-0086 could not see when it was written, because no partner had been observed yet. It creates pressure to generate an artifact **purely so that the pilot can claim Corridor automated it**. `docs/pilot-success-criteria.md` already forbids the claim that would follow: net coordinator time is measured against the partner's own pre-adoption baseline of work they actually performed. An artifact the partner never issued has a baseline of zero minutes, so producing it adds Corridor operations time and coordinator review time to the numerator and nothing to the denominator. It makes the measurement worse and the product wider at the same time.

**Corridor cannot claim time saved on an artifact the partner did not previously produce.** That rule was already in the measurement contract; this ADR restates it as a rule about the release architecture, so it is stated where the artifact set is defined and not only where the numbers are computed.

## What is internal behaviour and what is externally issued

The distinction the fixed enumeration blurred is between what Corridor *does* and what Corridor *hands to the customer's client*. Corridor's internal behaviour does not shrink because a partner does not issue a given artifact.

| Artifact | Internal behaviour | Externally issued |
|---|---|---|
| **Updated native UCM** | the accepted record rendered into the customer's own approved template (#495) | **always** — this is the mandatory member of every package |
| **Accepted-change summary** | always available internally: what the accepted record changed since the previous approved package | when configured |
| **Follow-up bundles and the chase view** | **always core internal behaviour** — #425 derives follow-up from accepted authority whether or not anything is issued | when configured |
| **Weekly Coordination Report** | always derivable from the frozen revision (#534) | when the partner already carries that reporting obligation |
| **Redline or provenance sidecar** | available | optional, when configured |

This answers #560's explicit question about the chase list, and it answers it in the direction that keeps the product intact: the chase list is **core internal behaviour that may or may not be issued**. A coordinator who never sends Corridor's chase list to anyone still gets their follow-up work derived from accepted decisions rather than free text, which is the whole of #425's value.

The updated UCM is the one artifact with no configuration switch, because it is the artifact the vertical slice is *defined by*. The goal in `roadmap.md` reads "return the customer's updated UCM"; a pilot week that issues no updated workbook has not demonstrated the loop, whatever else it produced. It is also the artifact the customer actually works from, which is ADR-0086's own reason for pulling it inside the sealed unit.

## The configured set is fixed until an attributable change

- The set is **project configuration**. It is recorded, and it changes only through an attributable configuration act with a named principal and a time.
- The **coordinator is not asked to choose artifacts each week.** The weekly interaction remains *authorize this issue*, exactly as ADR-0086 wrote it.
- **A change to the configured set is a material change under the measurement contract.** `docs/pilot-success-criteria.md` already classifies "artifact generation" as material, so a set change mid-window creates a recorded measurement boundary and results on either side are reported separately rather than pooled. Changing what a partner receives halfway through a measured pilot is permitted; concealing it, or reporting across it as one number, is not.
- The **package receipt is unchanged and needs no new field.** It already enumerates every artifact's identity and digest, so a set change is visible in the receipt series itself.
- **A one-artifact package is a package.** It carries the same five shared inputs, the same per-artifact digest, the same receipt, and the same single authorization.

## Considered options

**Retain the fixed four-artifact slice and recruit partners who already issue all four (#560's option 1).** Rejected, and it is a real option, not a straw one — it keeps ADR-0086 exactly as written, needs no new configuration surface, and makes every partner's package directly comparable. It was rejected for two reasons. First, it selects the market to fit the release architecture: it adds a partner-selection criterion to an already narrow design-partner funnel, at the stage where #428 has not yet validated that a buyer exists at all, and it makes the release format a reason to turn a willing partner away. Second, it does not actually remove the manufacturing pressure — it relocates it into recruitment, where a partner who issues three of the four will be asked to start issuing the fourth so the pilot can measure it, which is the same defect wearing a sales conversation's clothes.

**Let the coordinator choose which artifacts go out each week.** Rejected, exactly as ADR-0086 rejected it, and restated here so this amendment is not misread as reopening it. Weekly assembly makes the issue series inconsistent, turns an authorization into an editorial act, and gives the customer's client a different thing every week for no reason they can see.

**Make everything configurable, the UCM included.** Rejected. A configured set with no mandatory member is not a coherent product; it is a report builder. The updated workbook is what the slice returns and what the customer relies on, and a package that could legitimately contain nothing but a sidecar would make "one authorized release package" mean nothing in particular.

**Wait for #555 to observe both partners before deciding anything.** Rejected. The two options have genuinely different shapes in code — #529's candidate preparation and #533's authorization either enumerate a fixed set or read a configured one — and #529, #533, #534 and #425 are all live pilot-entry work. Holding the release architecture hostage to the first observation would either stall that work or let it be built against an enumeration the observation is expected to change. The decision that *the set is configured* does not depend on what any partner turns out to issue; only the contents of each partner's configuration do, and that is #555's job.

**Edit ADR-0086's artifact-set section in place.** Rejected under `docs/adr/README.md`. Code complying with ADR-0086 — a package builder that enumerates four artifacts — would fail review under this wording, and a normative "the configured first-slice set is" changes, so this is a new sequential decision carrying reciprocal `amends` metadata rather than an edit to an accepted body.

## Consequences

- **ADR-0086's section "The first-slice artifact set is configured, not chosen weekly" is amended in its enumeration only.** Its participation-is-configuration rule is kept and generalized. Its fixed list of four becomes: the updated native UCM always, and the change summary, chase list, weekly Coordination Report and any sidecar per project configuration.
- **Everything else in ADR-0086 is unchanged and still governs**: one authorization seals one immutable set; the five shared inputs; the per-artifact digest; the one package receipt and its enumerated contents; sealed-set immutability and no in-place regeneration; the last approved package as the comparison baseline, with no invented predecessor before the first; release as an act distinct from delivery; and the four blocking conditions, with honest adverse project conditions never blocking.
- **#555 now determines each partner's configured set** — which artifacts they already issue, the templates those follow, the approval path each takes inside the partner's organization, and where each is delivered. This ADR fixes the architecture; #555 fills in two instances of it.
- **#560 closes with this decision**, and its option-2 branch is the one that applies: this successor ADR carries the reciprocal metadata, the decision index is regenerated, and #529, #534, #498, `roadmap.md`, and `docs/pilot-success-criteria.md` are amended from the fixed-four wording to the configured-set rule.
- **`roadmap.md`'s pilot-entry requirement "Complete customer release — the configured artifact set of ADR-0086, sealed and authorized as one unit" is now literally true** rather than shorthand for four files. The issues behind it (#495, #534, #529, #533) are unchanged in scope; #534 and #425 produce their artifacts whether or not a given project issues them.
- **#425 is clarified as #560 required**: follow-up bundles and the chase view are core internal behaviour that always runs, and external issue of the chase list is configured.
- **The measurement contract gains no new rule, only a relocation.** "Do not claim time saved on an artifact the partner did not issue before adoption" now appears where the artifact set is defined, so a future reader deciding what to generate meets it before a future reader deciding what to report.
