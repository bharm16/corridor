# Machine-reference methods (#738)

Existing reference files retain their original method and bytes when an
extractor changes. The WSDOT 9424 and 9540 reference triplets are reused;
separate validity notes describe their scope. No replacement enumeration or
fresh WSDOT score was produced for #738.

Two method contracts are supported:

| Method | Version | Authoring input | Output name |
|---|---|---|---|
| `pymupdf-table-grid` | `1` | Archived references only; authoring retired by #766 | `<project>.machine.csv` |
| `native-pdf-cell-grid` | `1` | Fresh registered PDF bytes and typed native cell values, independent deterministic WSDOT recipe | `<project>.native-pdf-cell-grid-v1.machine.csv` |

Every CSV has an adjacent `.machine.md` sidecar and `.machine.scope.json`
manifest. Archived filenames and method identities remain unchanged. New
authoring defaults to `native-pdf-cell-grid`; explicitly requesting the retired
method refuses. Unknown methods or versions refuse; a filename alone does not
establish a method.

The native recipe reads every registered matrix document in the project,
ordered by content digest. It uses `tagged` at 36 dpi and the existing WSDOT
printed owner/identifier/resolution headings and retirement vocabulary. It
never reads Candidates, extraction runs, model column mappings or retained
answers. Source-reference multiplicities are preserved. No proposed field
value or row disposition supplies the reference denominator.

The supported native layout has one eligible WSDOT grid per page, a header
within the first six reconstructed rows, and a recommended-resolution anchor
in an eligible grid of the document. Continuation pages can repeat supported
headings without repeating the group anchor. Multiple eligible grids,
including a reader-combined table containing two complete headers, refuse.
An anchored but unsupported grid also refuses. Every populated page needs
supported headings; headerless continuation content cannot silently leave the
denominator. Row-spanning cells are unsupported by this version and refuse
before publication, including an owner spanning two otherwise separate rows.
An unrelated larger grid cannot win by row count. Known nonempty reader cells
lacking unique typed source values refuse instead of becoming apparent blank
slots. These bounds do not establish completeness outside the recipe's
recognized source scope.

Native manifests retain document hashes, each exact reading/result identity,
reader configuration and native recipe/rule fingerprints. Fingerprints cover
the native recipe and actual shared rule data/functions. They do not hash
whole legacy modules or appendable experiment logs. Native reader and mapping
semantics are unchanged.

Canonical completion queries moved from `extraction_runs.py` to the
engine-independent `extraction_run_queries.py`; the old module re-exports the
same functions. This changes the `extraction_runs.py` byte fingerprint used
by native matrix configurations. The retained #737 receipts remain exact
evidence of their historical revisions; they are not rewritten or relabeled
as measurements of this later revision.

For a genuinely needed new reference on an unspent source:

```bash
make gold ARGS="<project> --author --method=native-pdf-cell-grid --directory=out/references"
```

The spent/completed-target check happens before an authoring reader runs.
Creation is exclusive. An interrupted three-file publication can finish only
when any existing sibling bytes match exactly; divergent bytes refuse.
The reference cannot be published under a different project name. WSDOT
9540 is protected by both its project identity and its six preserved source
hashes, so a renamed project, another method or a new output directory cannot
create a fresh enumeration or score from that spent population.

Archive loading and regeneration are separate operations. The evaluator
loads either declared CSV/method contract without invoking an authoring engine
or requiring its historical build. It verifies the exact reference digest,
project, selected document-hash set, method/version and unchanged limitations.
Native provenance is validated as archived data; its old engine and recipe
fingerprints need not equal today's installation. The actual loaded method
appears in human output and structured measurement artifacts. Exact completed
Extraction Run selection and membership checks remain in place.

```bash
make eval ARGS="<unspent-project> out/references/<reference>.csv --reference-manifest=out/references/<reference>.scope.json --database-url=<disposable-url> --extraction-run=<id>"
```

A separate read-only native replay requires the original registered bytes and
the recorded configuration. It refuses an unavailable recipe, changed source,
reader/result mismatch or different CSV bytes, and writes nothing:

```bash
make gold ARGS="<unspent-project> --replay out/references/<project>.native-pdf-cell-grid-v1.machine.csv"
```

Legacy references lack a recorded reader configuration suitable for exact
regeneration; replay refuses rather than inventing it. Their archived CSVs
remain loadable. WSDOT 9540's spent guard remains effective for regeneration
and fresh evaluation. #737's labeled historical retained-answer/reference
comparison remains separate.

Both methods are semi-independent ceilings. Native references share reader
reconstruction and WSDOT vocabulary with the native extractor; old references
retain their original PyMuPDF limitation. Three-column CSVs measure source-ID
enumeration and critical-subset coverage. They do not supply independent field,
row-disposition or physical-occurrence gold, and do not select production.

## Verification for #738

The corrected implementation passed 228 focused tests across native-reference,
gold, evaluation, shared Extraction Run and native-matrix seams, plus all 40
architecture checks and the normal Ruff/mypy/compile checks. The focused tests
use real harness-owned PostgreSQL databases and source-authored synthetic
PDFs; no real WSDOT reference was re-authored or freshly scored.

The archived-scoring regression commits an exact run and reference in an
isolated database, then invokes the actual `measure()` and artifact path from
a fresh subprocess with PDF engine imports blocked. Separate source-authored
regressions prove repeated refusal of a spanning owner over two identified
rows and a populated continuation page without headings, while a supported
headed continuation replays exactly. Original source and reference bytes stay
unchanged across successful replay and refused retries.

Complete logs and generated synthetic artifacts are retained outside the
checkout under `/Users/bryceharmon/Desktop/corridor-evidence/2026-09-08-program727/`:
`738-review-fixes-focused.log`, `738-review-fixes-check.log`, and
`738-review-fix-artifacts/`. Earlier a43ebf1 logs are intermediate evidence;
the corrected gates above supersede their completion claim, not their bytes.
