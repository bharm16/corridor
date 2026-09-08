---
status: accepted
domain: extraction
scope: current product
amends:
  - ADR-0094
---

# The maintainer accepts the PDF replacement on the evidence already measured

**Amends ADR-0094.** It moves one clause: what qualifies the replacement for
production. Everything else in ADR-0094 stands, including the engine choice,
the unconfirmed-reading rule, the outbound-authorization rule, the evidence
retention rule and the locator rule.

## Context

ADR-0094 sent the replacement through a selection gate (#447) that qualifies a
named configuration by measurement before anyone may select it. The gate was
built. The evidence it demands was not finished, and finishing it means a
fresh-model campaign over a frozen corpus, an independently authored Reference
Dataset covering fields, physical occurrences, dispositions, refusals and
diagnostics, and further comparative measurement.

The maintainer decided on 2026-09-08 not to buy that assurance. His words: "I'm
done with the pdf replacement shit. It works." The evidence he is accepting on
already exists and is considerable:

- The reader passes the exact paired-rendition gate on 262 of 263 development
  pairs and 70 of 70 holdout pairs, reproduced inside Corridor.
- The cell-ID semantics tier matched 162 of 162 and 192 of 192 rows against the
  WSDOT machine references.
- The integrated replay reproduces all 466 required rows and 2,466 Source Facts
  across seven Documents, twice, with identical canonical output.
- The one disagreement with the incumbent, on four WSDOT 9540 listings, he
  adjudicated against the page: the incumbent undercounted a merged header
  spanning two columns, and the replacement is right.
- Routing, rendering geometry, native text and page inventory each carry their
  own recorded regression against the incumbent.

Two facts about that evidence must not be lost by accepting it. It does not
establish independent full-field accuracy, because the references share a
reader (ADR-0023). And the 5.1% of registered prose locators that cannot be
rebound under the new reading remain exactly what they were measured to be.

## Decision

**A maintainer's recorded acceptance is a selection basis in its own right,
beside a passing qualification gate.** Selection may proceed on either.

An acceptance is not a passing gate and is never recorded as one. An incomplete
or failed qualification stays incomplete or failed in its own receipt, as the
historical measurement it is. Nothing relabels it.

An acceptance record binds, and the selection command refuses it otherwise:

- the exact configuration and the implementation revision it accepts;
- the processing scope it covers, by source class and source identity;
- the evidence the maintainer relied on, named and reachable;
- the limits that evidence does not establish, stated plainly;
- the attributable maintainer, the decision's own words, and its timestamp.

**Only the maintainer's own principal may record an acceptance.** No worker, no
web request and no automated path may grant one to itself, and the record-write
boundary refuses an acceptance that does not carry that principal. Authorization
checks, source-scope enforcement, configuration integrity and the accepted-record
boundary are untouched: an acceptance settles *whether the configuration is good
enough*, never *who may run it*, *on what*, or *what it may write*.

**What this removes as a completion requirement**, for this replacement only: a
fresh-model qualification campaign, an independently authored Reference Dataset,
and further comparative accuracy measurement. Those remain available and remain
the right instrument for a future claim that needs them.

**What it does not remove.** The authorized outbound boundary is real missing
functionality and is still required, because the native path cannot execute
without it. So is the scanned integration (#739) and the removal of the old
engines from source, dependencies, CI and the runtime image with its audit
(#741). Accepting a reader does not build a caller or delete a dependency.

**What it does not touch.** The provider posture (#732) and the customer
authorization (#522) stay open until their actual facts exist. Their absence may
keep live customer processing disabled; it does not block completing the
adapter, removing the old libraries, or closing the software tickets this
decision amends.

## Considered options

**Finish the qualification campaign.** Rejected by the maintainer as assurance he
is not buying. The campaign is not wrong, and nothing here says its result would
have been bad; he judged the existing evidence sufficient for this change and
declined to spend more on the question.

**Record the unperformed qualification as passed.** Rejected outright. It would
put a false statement in the permanent record, and the gate's whole purpose is
that a receipt means what it says.

**Delete the gate's refusal so selection succeeds.** Rejected. The refusal is
correct for a qualification basis, and removing a check in one layer while the
database and the runtime readback still refuse produces a system that disagrees
with itself. The acceptance is added as a distinct basis, enforced the same way
at every layer.

**Keep the gate and let the maintainer waive it case by case.** Rejected as a
general waiver mechanism. A waiver dismisses a result; this decision records a
different basis, with its own required fields and its own visible limits.

## Consequences

ADR-0094 carries `amended_by: ADR-0095`. #447's acceptance criteria change to
match: the qualification campaign is no longer required for its closure, and the
attributable selection act, the retained decision record and the intact legacy
path until #741 remain.

A reader of any Extraction Run can still ask what authorized its configuration
and get a truthful answer: either a qualification that passed, or a maintainer
who accepted it on named evidence with named limits. The two never read alike.

The measurements this decision relies on keep their exact scope. Accepting the
replacement is not a claim that it was proved accurate beyond what those
measurements establish, and no report, receipt or customer-facing statement may
present it as one.
