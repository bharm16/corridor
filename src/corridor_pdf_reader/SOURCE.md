# Source of this package

`corridor_pdf_reader` is the measured paired-rendition reader, imported from
commit `c39363e26c2726b61c4e707f589093c67173e538` (short `c39363e`, branch
`codex/standalone-comparison`) of the maintainer's local standalone repository
`pdf-reader-comparison`, which has no remote (#729). The commit is the merge
of the Textract rung; the two commits after it on that branch touch only
`HANDOFF.md`. This file records what was taken, how, what was checked before
the import began, and what was deliberately changed.

## What was imported

| Package path | At the commit | Why |
|---|---|---|
| `replacement/` | `replacement/` | The reader: PDFium glyphs, Excel's structure tree through pypdf, the deterministic reconstructor, the slim page with cell IDs, the OpenAI semantics tier, its prompt, vocabulary, lock and third-party notices |
| `bootstrap/` | `bootstrap/` | The paired-rendition harness: corpus, sealed split (`pairs.json`, seed 720), answer-key builder, reader driver, scorer, tally, inspect, exactness, the loop log, and its two test modules |
| `paired_trial/number_format.mjs`, `package.json`, `pnpm-lock.yaml` | same | `bootstrap/fmt.mjs` imports `../paired_trial/number_format.mjs`, which needs `ssf` 0.11.2 from npm; the rest of `paired_trial/` is an earlier experiment and stays behind |
| `corpus/fixtures/*.pdf`, `corpus/fixtures.json` | same | Eleven tiny synthetic PDFs (four rotations with and without a CropBox, a raster-only page, malformed bytes, a password) generated independently of every engine, for bounded fixture tests |
| `receipts/notes/{HANDOFF,SEMANTICS-RESULTS,TEXTRACT-RESULTS}.md` | repository root | The logs the ticket names, beside the baseline receipts |

Not imported: `textract/` (#732's), `tests/test_replacement.py` (it builds
its PDFs with reportlab and compares against the PyMuPDF baseline in the
comparison harness, neither of which Corridor may carry; the bounded fixture
tests in `tests/test_pdf_reader_package.py` cover the same fixture PDFs
through this package instead), and the comparison, docling and parity-debug
harnesses, which measured other engines.

Every imported file is listed in `source-manifest.json` with its git blob id
(from `git ls-tree -r -l c39363e`), the SHA-256 of the archived bytes and its
size. `provenance.verify()` recomputes all of it, and
`tests/test_pdf_reader_package.py` fails if anything differs.

The commands, run from the Corridor worktree with the source worktree at
`/Users/bryceharmon/Desktop/pdf-reader-comparison-base` (read-only; nothing
was copied from a working tree):

```bash
git -C /Users/bryceharmon/Desktop/pdf-reader-comparison-base archive c39363e \
    replacement bootstrap paired_trial/number_format.mjs paired_trial/package.json \
    paired_trial/pnpm-lock.yaml corpus/fixtures corpus/fixtures.json \
  | tar -x -C src/corridor_pdf_reader
git -C /Users/bryceharmon/Desktop/pdf-reader-comparison-base archive c39363e \
    HANDOFF.md SEMANTICS-RESULTS.md TEXTRACT-RESULTS.md \
  | tar -x -C src/corridor_pdf_reader/receipts/notes
git -C /Users/bryceharmon/Desktop/pdf-reader-comparison-base ls-tree -r -l c39363e <the same paths>
```

## The one change: the import prefix

The two packages import each other by absolute name (`from replacement.layout
import ...`, `from bootstrap.corpus import ...`). Under Corridor they are
`corridor_pdf_reader.replacement` and `corridor_pdf_reader.bootstrap`, so
every such statement gained the prefix: sixteen files, twenty-nine lines,
nothing but `from X` becoming `from corridor_pdf_reader.X`
(`provenance.rewrite_imports`). `provenance.restore_imports` is the exact
inverse, and the parity test proves that restoring every rewritten file
yields bytes with the recorded blob id, and that the abstract syntax trees
differ only in those module names.

Two layouts would have avoided the rewrite and were rejected:

- Installing `replacement` and `bootstrap` as top-level packages keeps the
  bytes but puts two generic names into every Corridor environment.
- An import hook that serves the byte-identical files under the new names
  would load the same file under two module names, and neither mypy nor ruff
  could follow it.

Three lines in the imported code are now inert and were left as they are:
`bootstrap/read.py` and `bootstrap/reference.py` insert the package root
into `sys.path` (a no-op for the rewritten imports), `reference.py` also adds
a `paired_trial/vendor` directory that did not exist at the commit, and
`reference.py` writes a scratch copy of a zip-behind-`.xls` workbook under
`<package>/tmp/`, which `make pdf-reader-reproduce` creates and git ignores.

`bootstrap/LOOP-LOG.md` is the one file that grows: ADR-0008 requires every
holdout access to be appended to the experiment log, and the reproduction
scores the spent holdout. The manifest marks it `appended`; the parity test
checks that the original bytes are still its prefix.

## Preflight, checked before the import began (2026-09-06)

| Required input | Checked | Result |
|---|---|---|
| Source | `git cat-file -t c39363e` is `commit`; `git diff --stat c39363e e435aa6` (the branch head) touches only `HANDOFF.md`; files taken with `git archive c39363e`, never from a working tree; every archived blob hashes to its `ls-tree` id | 77 files, 77 blob ids matched |
| Receipts | `results/loop-020` (SUMMARY.md, summary.json, read-receipts.json, 263 scores, 335 reads), `results/loop-reference-v6` (524 keys + receipts.json, 63.8 MB), `results/syn-reference-v1` (351 keys, 24.6 MB), `results/syn-003`, the Textract worktree's `results/` lanes, `bootstrap/LOOP-LOG.md`, `HANDOFF.md`, `SEMANTICS-RESULTS.md`, `TEXTRACT-RESULTS.md` all readable; every directory digested into `receipts/` | readable; the Textract worktree's own rebuild of the exact-set key is byte-identical to the main checkout's on all 333 pairs |
| Corpus | Every selected file of `TRUE_PAIRS_ROOT=.../true-pairs/exact` hashed against `MANIFEST.csv` (`pdf_sha256`, `book_sha256`): 333 pairs, 666 files, 190,312,360 bytes; `MANIFEST.csv` sha256 `ca68b55a…7427a52`; every pair key present in `bootstrap/pairs.json` (sha256 `529c5ffb…bb281f`, seed 720): 263 development pairs / 1,589 pages, 70 holdout pairs / 409 pages | 0 mismatches, 0 missing, 0 keys outside the split |
| Defaults | `bootstrap/read.py` reads at `dpi=36` with `--jobs 4`; loop-020's `read-receipts.json` records engine `tagged`; run-mate rescue is a Textract lane-A variant in `textract/remap.py` (#732's) and no identifier in `replacement/` or `bootstrap/` mentions it, so it is off by construction; no argument default was changed | matches the measured configuration |
| Environment | `pyproject.toml` declares pypdfium2 5.13.0, pypdf 6.17.0, pdf-oxide 0.3.77 (main), mypy (dev), xlrd 2.0.2 (`pdf-reader-experiment` group; openpyxl was already declared); `uv.lock` resolves with manylinux x86_64/aarch64 and macOS arm64 wheels for all three engines; `make pdf-reader-node` runs `npm ci` from `paired_trial/package-lock.json`, whose integrity hashes equal the imported `pnpm-lock.yaml`'s | the declared setup installs everything the reproduction needs; see the reproduction receipt for the fresh environment it ran in |

## The measured configuration, unchanged

- Reader: `replacement.reader.read_pdf(..., engine="tagged")`, as
  `bootstrap/read.py` calls it, `dpi=36`, images not retained.
- Harness: `bootstrap.reference` (openpyxl, xlrd, SSF through `fmt.mjs`),
  `bootstrap.read --engine tagged --jobs 4`, `bootstrap.score`,
  `bootstrap.tally`; split `bootstrap/pairs.json`; corpus the exact set.
- Pinned engines: `replacement/pyproject.toml` and `replacement/uv.lock`
  (retained unchanged) and Corridor's `pyproject.toml` name the same
  versions; `tests/test_pdf_reader_package.py` checks the installed ones.
- Baseline: loop-020, 262 of 263 development pairs, 1,589 of 1,589
  development pages, 483,210 of 483,336 reference cells; 70 of 70 holdout
  pairs, 409 of 409 holdout pages (`receipts/loop-020/`).

Any change to the reader is a new configuration for #731's gate; this
package is the baseline configuration and is not selected for production by
anything here (#447 owns native selection).

## Checks

- `make check` runs ruff with Corridor's selection and mypy under the source
  repository's settings (`check_untyped_defs`, `disallow_untyped_defs`) on
  this package only. pypdf's own annotations are skipped for mypy because the
  source checked `replacement/` in an environment where pypdf was absent.
- `make test` collects the imported `bootstrap/tests` through
  `tests/test_pdf_reader_imported_tests.py`, and the package's own bounded
  tests need no corpus, no node and no network.
- The 333-pair reproduction is `make pdf-reader-reproduce`, an explicit
  experiment outside CI; its receipt lives in `receipts/`.
