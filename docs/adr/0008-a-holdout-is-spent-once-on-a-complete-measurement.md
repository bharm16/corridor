---
status: accepted
domain: extraction
scope: current product
---

# A holdout is spent once, on a complete measurement, with labelling blind to extraction

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

> The hand-labelling method in this decision is superseded by ADR-0023. The one-shot holdout, predeclared criteria, no-retake rule, and requirement to publish limitations remain in force.

`phase-1-roadmap.md` makes M7 unskippable — *"the only objective measure that the core works"* — and defines it as done when there is **≥95% recall on labeled critical dependencies**, 100% citation validity, and metrics recorded every run.

That gate cannot be run today, for two reasons that have nothing to do with the extractor and everything to do with missing preconditions nobody had named:

- **There is no hand-labeled gold set.** #59 recorded that the slice was "tracked separately"; it was not, and no issue existed for it.
- **Nothing had been marked critical at that point.** ADR-0007 addressed the missing definition; ADR-0009 later replaced its interpretation with a Utility Conflict Resolution Method work-type subset.

M7 also contains a contradiction on its face: it asks for a *"gold set on a held-out project"*, but hand-labelling requires reading the document, and reading it is what spends the seal.

Three decisions resolve this, and they generalise past WSDOT 9540 to every holdout after it.

**A holdout buys one complete measurement, or none.** It is not spent on a partial gate. We could measure overall recall, precision and citation validity today and defer critical-recall, but the seal does not survive to be used twice — and the deferred half is the ≥95% bar the gate exists for. A one-shot asset spent on a partial answer is spent for nothing.

**The seal protects against tuning, not against reading.** That is what dissolves the contradiction. Reading the document to label it is the measurement, not a leak. What may never happen is changing code, prompts or vocabulary on the strength of what that reading showed — FDOT SR 789's seal is spent for exactly this reason and could not be reclaimed.

**Labelling is blind to extraction, and the order is load-bearing.** Extract first, without looking at the results. Then hand-label from the document. Then score. Labelling first means the extraction is no longer cold; labelling while looking at the output means the labels drift to fit what was found. With one person doing both, sequence is the only available blindfold.

The success criteria — what recall counts as a pass, what field-token failure rate counts as a pass, whether a fallback to the transcription tier counts against the result, and what happens on a fail — are written down **before** the document is fetched. Deciding afterwards is the same contamination in slower motion.

## Consequences

M7 was blocked on a work-type subset definition per layout and a hand-labeled slice. ADR-0009 supplies the accepted subset; ADR-0023 later replaces hand labelling with a declared machine reference and its limits. Naming the missing preconditions was the point — they had been invisible, and an invisible blocker on an unskippable gate is how a project discovers in week eleven that week seven never finished.

Each layout's Utility Conflict Resolution Method fields for the work-type subset must be pinned from an **unsealed** document. For 9540 that is sibling contract 9424, which is what it was fetched for.

The corpus needs a successor holdout ready before this one is spent, or the next quality question has nothing honest to measure against. `corpus-acquisition-spec.md` §7.2 records that WSDOT publishes 392 contracts this way, so the supply exists; what does not exist is the habit of sealing the next one early.

An eval that cannot enumerate a layout must say so rather than report zero (#78). A holdout scored by a broken measurement is a holdout wasted just as surely as one that was tuned against.
