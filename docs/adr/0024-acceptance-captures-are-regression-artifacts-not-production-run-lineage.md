---
status: accepted
---

# Acceptance captures are regression artifacts, not production run lineage

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

The M8 acceptance fixture pins five `matrix_tiered_v3` extraction runs captured from real model calls over NHHIP revision PDFs byte-identical to the registered documents, and the temptation existed to register those captured runs as the pilot's production lineage rather than pay for extraction twice. Rejected: a production Extraction Run is created only by production extraction against registered documents. The capture's original runs existed only in a disposable database and no longer exist — recreating them live would be an import wearing a run's identity, and the run schema records no fixture origin, digest, capture time, or importer, so the disguise would be permanent (`models.py`). ADR-0019 governs declaring a run already in lineage; it does not authorize minting lineage from a capture. Semantic quality is deliberately not the argument: a fresh production run is exactly as unmeasured as the capture until human review, and the capture's claim boundary (mechanical correctness only) merely says so out loud. The discriminator is identity — a production run's lineage is true because the run happened where it says it happened. An acceptance capture verifies behavior: its model-free replays exercise the mechanical pipeline, and it never populates run lineage.

## Considered options

**Stand on the existing v2 runs.** Rejected: all five predate exact Extracted Proposal input capture, so Revision Comparison refuses them (`InexactExtractionInputs` — "run a fresh extraction before comparing it"), and v2 is neither the current prompt nor the version the M7 gate measured.

**Promote the pinned capture into lineage.** Rejected as above, despite byte-identical sources and a direct comparison finding no field or citation-content differences across the 3,235 v2/v3 Extracted Proposals — the capture is credible, and credibility is not lineage.

**Fresh v3 over the full five-revision chain.** Deferred, not rejected: extraction is scoped to what the pilot reads — the current revision and its predecessor for the "what changed" review path — and the remaining history is extracted only when a full historical review scope becomes an explicit requirement.

## Consequences

The pilot pays for extraction it already holds bytes for; that price is the cost of honest lineage, and the recorded benchmark for a comparable tiered corpus (~$0.15) says it is small. The exact current v3 cost is unrecorded, and Extraction Runs store neither tokens nor cost — if the number matters, the rehearsal record must note it, because lineage never will. The capture's replays continue to verify the mechanical pipeline; they say nothing about a fresh run's semantic quality, which remains unmeasured on NHHIP until human human record decision.
