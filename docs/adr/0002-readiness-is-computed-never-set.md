---
status: accepted
---

# A documentation requirement is met through reviewed sources, not a status click

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

`ready` was a value in the `dependencies.status` enum, which would let a reviewer click a Constraint into a supported conclusion without proof — the precise failure this product exists to prevent, and inconsistent with the Constraint Alert engine's own rule that derived conditions are queries rather than stored state.

Whether the stated documentation requirement is met is a derived condition. A person's Documentation Review judges whether exact current verified Supporting Documentation meets the stated `evidence_required` requirement. The system derives the result from that judgment and the supporting sources; it does not accept a free-standing status click. A customer view names only the supported result, such as Relocation Complete or Permit Issued, for the reviewed scope and with its review basis.

This ADR originally removed `ready` while retaining `identified | in_progress | committed | blocked | closed`. ADR-0044 later retired the entire mutable lifecycle. Neither that historical enum nor a replacement completion enum is restored by the terminology amendment.

## Considered options

- **Typed closure taxonomy** (`executed_agreement | relocation_complete | permit_issued | …`), with sufficiency fully machine-computed. Originally deferred rather than rejected: enumerating closure document types before a corpus existed would encode a guess into the schema, and a wrong guess would cost a migration plus re-adjudication. The original M7/M8 proposal to revisit an enum was not authority to remove the human sufficiency judgment; ADR-0037 preserves that judgment.
- **Any verified Supporting Documentation on a report of completion.** Rejected: the bar becomes "some document mentioned this", which is weak enough that `ready` stops meaning anything.

## Consequences

`MISSING_EVIDENCE` cannot fire when current verified Supporting Documentation meets the Constraint's stated requirement. The key date rollup must describe the documentation requirements reviewed, not imply that all construction is authorized to start. A person who knows a relocation is complete but holds no required source record cannot mark its Required Documentation met. That absence concerns the supporting records; it does not prove that the physical work is unfinished.
