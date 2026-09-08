# Retiring the incumbent PDF engines: the rollback record (#741)

ADR-0094 removes PyMuPDF and Tesseract from the product, and #741 requires
that "the rollback window is recorded before any legacy deletion". This is
that record. It exists so that the deletion is made against a written
statement of what could be undone and how, rather than against an assumption
that someone would work it out afterwards.

Nothing has been deleted at the time of writing. Every module, dependency,
package and image layer described below is still present.

## What the retirement removes

Two engines, in five places:

- **Two Python distributions in two uv projects.** The root project declares
  PyMuPDF and pytesseract; `workers/render` declares PyMuPDF. Each has its own
  lockfile.
- **One operating-system package.** The Dockerfile installs `tesseract-ocr`;
  the Python wrapper only shells out to it.
- **Eighteen source files.** `ENGINE_ALLOWLIST` in `tests/test_architecture.py`
  is the exact list, with the engine each file uses. #741's first acceptance
  criterion is that the list ends up empty.
- **CI setup.** `scripts/ci_environment.sh` installs the OCR engine beside the
  two `uv sync` calls.
- **The runtime images and their sizing notes.**

Of those eighteen files, eleven currently reach an engine through a
module-level import; the rest name the executable or the engine identity as a
string. The eleven, with the number of other modules that cannot import while
each one stands, are recorded in
`artifacts/pdf-engine-retirement/engine-absent-imports-20260908T150213Z.json`:

| File | Engine | Modules blocked behind it |
|---|---|---|
| `src/corridor/token_layers.py` | Tesseract | 204 |
| `src/corridor/geometry.py` | PyMuPDF | 48 |
| `src/corridor/ingest.py` | PyMuPDF | 33 |
| `src/corridor/page_inventory.py` | PyMuPDF | 11 |
| `src/corridor/extract_matrix.py` | PyMuPDF | 5 |
| `src/corridor/source_segments.py` | PyMuPDF | 5 |
| `src/corridor/gold.py` | PyMuPDF | 4 |
| `tests/test_extract_matrix.py` | PyMuPDF | 1 |
| `tests/test_m8_acceptance_capture.py` | PyMuPDF | 1 |
| `tests/test_page_inventory.py` | PyMuPDF | 1 |
| `tests/test_token_layers.py` | PyMuPDF | 1 |

## The retirement point

The retirement point is the last revision at which both engines are present,
installed, and exercised by the suite — in other words, the commit on `main`
that the removal is merged onto. The removal has not been written, so that
commit does not exist yet. What does exist is the state that makes the point
identifiable, recorded from the current revision:

- **The built image, with both engines still in it.**
  `artifacts/pdf-engine-retirement/image-audit-20260908T143957Z.json` records
  image id `sha256:b1431dda21c24d0c3ab8f5c575cdcc9dddaa22e8540f4cef3501a48c57ab70d4`,
  3179 MB uncompressed, carrying PyMuPDF 1.28.0 in the application
  environment, PyMuPDF 1.28.2 in the render worker's, pytesseract 0.3.13, and
  `tesseract-ocr` 5.3.0-2 with the executable at `/usr/bin/tesseract`. That
  audit deliberately exits non-zero: it is a before state, not a passing check.
- **The modules that still reach an engine**, in the table above.
- **The retained-citation population**, in
  `artifacts/pdf-engine-retirement/retained-citation-inventory-20260908T144346Z.json`:
  in the database this checkout is configured against, no persisted citation
  is referenced by any retained decision or released artifact at all.

**To be completed when the removal merges:** the removal commit and its
parent, written into this section. Until that line is filled in, the
deletions listed under "What the window gates" below have no recorded point
to roll back to and must not proceed.

## What a rollback would consist of

1. Revert the removal commit. Both lockfiles and the Dockerfile come back with
   it, so the pinned Python versions are restored exactly.
2. `uv sync --locked` for the root project and
   `uv sync --project workers/render --frozen` for the render worker.
3. Rebuild the image, which reinstalls the `tesseract-ocr` apt package, or
   redeploy an image built before the removal.

One part of that is not pinned by this repository. The Python distributions
come back at exactly the versions the reverted locks name. The `tesseract-ocr`
apt package comes back at whatever version the base image's package index
serves at rebuild time, which was 5.3.0-2 when the before state was recorded.
Only redeploying a retained pre-removal image restores that exactly.

## What a rollback does not have to restore

The retirement deletes code, dependencies and image layers. It deletes no
record data, and the reasons are structural rather than procedural:

- **Source Segments are append-only in the database.** A trigger raises
  `source segments are append-only` on every `UPDATE` and `DELETE`, so no step
  of this retirement can rewrite a retained citation's words or locator even
  by mistake. `tests/test_retained_history.py` holds that against a seeded
  retained citation.
- **A retained citation can still be verified with both engines gone.**
  `corridor.retained_history.replay_retained_reading` proves a retained
  reading from its own digests and the registered Document's bytes, and takes
  no reader of any kind. That was run in an environment with both
  distributions withheld from both uv projects and the OCR executable removed
  from `PATH`; the receipt is
  `artifacts/pdf-engine-retirement/engine-absent-suite-20260908T145929Z.json`.
- **Released bytes and immutable runs are untouched.** Nothing in the
  retirement's scope writes to them.

**The one thing that does not survive.** A *fresh reading at the original
physical location* of a locator the retired reader established cannot be done
once that reader is gone. This is not a gap left open by accident: it is
named in code as a separate basis from replay, it fails closed with the
reader it needed rather than answering from the record, and in the
engine-absent run it appears as the single skipped test among eighteen. Two
things keep it from being a loss of history here. No retained citation in the
inspected database depends on it, and a replay establishes the record's
integrity without it. Anyone who needs that fresh reading for a specific
historical citation must roll back, or must accept a different basis; a
change to that contract is the "explicit narrow compatibility decision" #741
requires, not something to be settled in passing.

## The rollback window

**What has actually been decided.** ADR-0095 records the maintainer's
acceptance of the replacement on 2026-09-08, on the evidence already
measured. Neither it nor ADR-0094, #727 or #741 sets any duration. **No
waiting period has been decided by anyone, and this record does not invent
one.**

The one durable statement that does exist comes from the ticket whose scope
#741 inherited: #460 required that "rollback after this point is by version
control revert only, and that is stated in the decision record." That is
stated here. After the deletion, there is no disabled fallback to re-enable,
no flag to flip and no retained code path to call; the way back is a revert.

**Why "wait N days" is the wrong shape for this window.** The option to roll
back has two halves with two different clocks:

- **Source rollback is not time-limited.** The revision, both lockfiles and
  the Dockerfile stay reachable in git. Reverting is as possible in a year as
  it is next week.
- **Runtime rollback is limited by artifact retention, not by a calendar.**
  What perishes is the built pre-removal image (the one recorded above was
  built locally for the audit and pushed nowhere) and the availability of the
  `tesseract-ocr` package at the version the base image served. Neither is
  governed by any retention policy this repository holds.

So what keeps this window open is retaining artifacts, and what closes it is
losing them — not the passage of time.

**What the maintainer still has to decide.** Two questions, both open:

1. Whether a pre-removal image is retained, where it lives, and for how long.
   Without one, runtime rollback means a rebuild whose OCR package version
   depends on what the base image serves that day.
2. What ends the window: a date, a condition, or nothing at all — that is,
   whether the gated deletions may proceed together with the engine removal.
   Answering "immediately" is a legitimate answer and is the maintainer's to
   give; this record's requirement is that the answer be recorded, not that it
   be long.

**What the window gates.** #741 removes the engines from source, dependencies,
CI and the images without waiting for anything. Four deletions wait for the
window to close, because each one removes a path that a rollback would want:

- the old table geometry (`src/corridor/geometry.py`);
- the old structure prompt path (`prompts/matrix_structure_v1.md`, `v2` and
  `v3`, reached through `STRUCTURE_PROMPT` in
  `src/corridor/extract_matrix.py`), replaced by `matrix_structure_ids_v1`
  under #737;
- the Tesseract token adapter (the OCR half of `src/corridor/token_layers.py`
  and `src/corridor/unreadable_cells.py`), replaced by #739;
- the legacy extraction path, #460's scope.

`workers/render/legacy_pymupdf.py` belongs with them. It is the render
worker's retained incumbent rasteriser, loaded only when a request asks for
the measured rollback, so deleting it *is* closing part of the window rather
than something to do inside it.

## How to check this record is still true

```bash
make image-engine-audit                       # what the built image contains
make test-engine-absent MODE=imports          # which modules still reach an engine
make retained-citation-inventory              # which persisted citations exist
```

The first two exit non-zero until the removal lands. That is the point: they
report a before state truthfully rather than describing the intended after
state.
