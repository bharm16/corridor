---
status: accepted
---

# Readiness is computed from evidence, never set

`ready` was a value in the `dependencies.status` enum, which would let a reviewer click a Dependency into readiness without proof — the precise failure this product exists to prevent, and inconsistent with the exception engine's own rule that derived conditions are queries rather than stored state.

Ready is now a derived predicate: a reviewer marks a verified Evidence link as meeting the Dependency's `evidence_required` bar, and readiness follows from that mark. The reviewer judges sufficiency; the system refuses readiness without the evidence. `ready` is removed from the stored status enum, which retains `identified | in_progress | committed | blocked | closed`.

## Considered options

- **Typed closure taxonomy** (`executed_agreement | relocation_complete | permit_issued | …`), with readiness fully machine-computed. Deferred rather than rejected: enumerating closure document types before any corpus exists encodes a guess into the schema, and a wrong guess costs a migration plus re-adjudication. Promote to an enum in M7/M8, once real adjudications show what closure documents actually look like.
- **Any verified evidence on a closure event.** Rejected: the bar becomes "some document mentioned this", which is weak enough that `ready` stops meaning anything.

## Consequences

`MISSING_EVIDENCE` cannot fire on a ready Dependency by construction. The milestone readiness rollup becomes an evidence claim rather than an opinion survey. A reviewer who knows a relocation is complete but holds no document saying so cannot record it as ready — deliberate, and the resulting friction is a signal about the corpus rather than a defect in the tool.
