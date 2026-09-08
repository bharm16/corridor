# Matrix routing and engine retirement receipt (#766)

The production Matrix route uses the selected native configuration and records
its `matrix_structure_ids_v1` prompt and `native-matrix-source-fields-v1`
schema. Missing, disabled, stale and out-of-scope selections are Processing
Failures. Native capture owns its one sealed run; refused observations and
artifact registrations survive, while partial or invalid successful results
roll back. Source capture grants no accepted-record authority.

Implementation and reviewed corrections: `d6249bdeff183f7fddaeedcae55cb83440b3b172`.
The complete suite ran at `57bf880`, whose only additional files are the
built-image audit receipts. Both independent reviews report no remaining
Standards or Spec findings.

## Verified results

- `make check`: lint, type checking and compilation passed; 54 structural
  checks passed.
- `make test-engine-absent MODE=suite`: **5,496 passed, 6 skipped**, zero
  failures/errors, pytest 487.43 seconds. The complete running/completed
  lifecycle and exact JUnit counts are retained in
  [the engine-absence receipt](../../artifacts/pdf-engine-retirement/engine-absent-suite-20260908T192213Z.json).
- `make image-engine-audit`: exited zero. Image
  `sha256:1e40384966549e773d10e9012749356f2254792e6fe05a85056d8fd12cb7d964`
  contains neither retired engine in either Python environment, and no
  Tesseract executable or OS package. All 25 declared notice files are
  present; the image also carries its installed binary licence files. The
  native runtime configuration loads without Git, a key or a model request.
  [JSON audit](../../artifacts/pdf-engine-retirement/image-audit-20260908T191211Z.json),
  [readable audit](../../artifacts/pdf-engine-retirement/image-audit-20260908T191211Z.md).
- The corpus test ingests the retained public WSDOT gas listing, runs the
  configured production route as `corridor_worker`, records 14 Candidates,
  and dereferences their native citations. It also preserves a seeded legacy
  citation and its original-byte integrity. HTTP is a test double returning
  the frozen answer; this is plumbing and preservation proof, not a new
  model-quality measurement. A provider-refusal variant proves failure
  observations remain registered without partial Facts or Candidates.
- [The current citation inventory](../../artifacts/pdf-engine-retirement/retained-citation-inventory-20260908T185019Z.json)
  found zero referenced citations across seven reference tables and fourteen
  retained anchors. This describes the inspected database, not other customer
  environments or a passing nonempty historical population.

The six skips are disclosed: two separately managed SH99 populations are
unavailable, the shared NHHIP population is absent, macOS does not enforce the
tested `RLIMIT_AS`, the optional legacy-development login is absent, and fresh
reconstruction by the retired prose reader is unavailable. The retained-reading
integrity tests and both corpus production-route cases passed. No skipped case
is presented as proof of its missing source or unavailable operation.

The earlier failed complete-suite receipt is retained unchanged. It identified
native-library discovery in the isolated macOS environment, a copied worker's
environment assumption, obsolete incumbent test setup/identity assertions, and
a clean-tree acceptance check affected by concurrently written audit receipts.
The corrected full run started from a clean tree and passed. No test was
deselected to obtain the passing receipt.

The first Linux PR run exposed one test portability error: the corpus test
compared its PNG to a historical macOS PNG hash. The corrected assertion keeps
that historical hash intact and verifies the current run's transmitted image
against its own render receipt, 110 DPI and the known 1870 by 1210 geometry.
Both corpus cases were rerun in the engine-absent environment. Production
source and dependencies are unchanged from the complete-suite/image proofs
above; the required PR gate validates the revised test on Linux.

## Preservation and deployment decision

The remaining legacy geometry and structure/transcription flow are removed;
`ENGINE_ALLOWLIST` is empty and both projects' installed dependencies exclude
the retired engines. The four old prompt files survive byte for byte in the
[archival manifest](../../artifacts/pdf-engine-retirement/legacy-prompts/manifest.json).
Old immutable runs, Source Segments, reference CSVs and released bytes are
preserved. Rollback remains the version-control procedure recorded before
deletion in [the rollback record](pdf-engine-retirement-rollback.md).

This receipt supplies the verified image evidence for #461's existing
replacement decision and #741's remaining retirement criteria. #535's licence
criterion becomes verification of the actual activation image's engine absence
and required notices. Customer authorization and actual deployment selection
remain their separate recorded acts; this software delivery creates neither.

See [the route runbook](native-matrix-production-route.md) for configuration,
selection, refusal and test-operation details.
