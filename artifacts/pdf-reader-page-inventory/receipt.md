# #734: the Page Inventory and the routing decision from the paired-rendition reader

The adapter this receipt measures is disabled. Enabling it is one setting,
`reader_page_inventory` on `corridor.config.Settings`
(`CORRIDOR_READER_PAGE_INVENTORY`), and merging it selects nothing: #447 owns
native selection, #739 owns scanned selection, and the incumbent inventory is
unchanged and remains the default.

Reproduce with:

```bash
make page-inventory-routing-replay ARGS="--output-dir artifacts/pdf-reader-page-inventory \
    --holdout-actor <actor> --holdout-reason <reason>"
```

## What was measured

The frozen Stage 1 routing corpus (`gold/pdf/v1/stage1-routing-gold.json`,
dataset version `2026-08-31.2`): five pages whose OCR-needed labels were
authored and independently checked before any router was measured against
them — three native matrix pages, one image-only agreement page and one mixed
native/scanned agreement page. The reader-backed inventory decided all five
again, from the registered bytes, and the decisions were scored by the same
evaluator the frozen receipt used (`make page-inventory-eval`).

`stage1-routing-run.json`, `stage1-routing-evaluation.json` and
`stage1-routing-replay.json` here are that run, its evaluation and the
per-page comparison, produced 2026-09-07 in 5.4 s. The content store is read
read-only, by digest, and nothing is written into it.

**FDOT holdout access.** One of the five pages is the spent holdout family
(ADR-0008). The access is the 2026-09-07 line of
`gold/pdf/v1/holdout-access.jsonl`, with this lane as the actor and this
predeclared replay as the reason. The command refuses to run at all without an
actor and a reason rather than quietly measuring the other four.

## The numbers

Per page, the gold label beside the recorded decision. `incumbent` is the
frozen `gold/pdf/v1/stage1-routing-run.json`; `reader` is this run.

| Page class | Page | OCR needed (gold) | incumbent | reader | Changed |
|---|---|---|---|---|---|
| page-scoped-owner-matrix (holdout) | p.2 | no | native | native | no |
| marked-resolution-matrix | p.5 | no | native | native | no |
| ambiguous-retirement-matrix | p.9 | no | native | native | no |
| image-only-agreement | p.1 | yes | ocr | ocr | no |
| mixed-native-scanned-agreement | p.1 | yes | both | both | no |

**False "OCR not needed" rate: 0.0** (0 of the 2 pages that need OCR were
routed native). **Unnecessary-OCR rate: 0.0** (0 of the 3 pages that do not
need OCR were sent to OCR). Confusion: 2 true positive, 3 true negative, 0
false negative, 0 false positive. Those are the same numbers the frozen
receipt records for the incumbent inventory, and the by-page-class breakdown
is identical too.

**No page's routing changed, so there is no routing difference to explain.**
What follows are the inventory differences behind those identical decisions,
recorded because they exist, not because they moved anything.

## The differences behind the identical decisions

**The recorded native text length differs on every text-bearing page, and not
one glyph does.** 1,713 → 1,638 characters on the holdout page, 3,697 → 3,656,
2,874 → 2,830 and 1,035 → 1,000 on the other four. On the three non-holdout
text pages the two readings' non-whitespace characters were compared
character by character: the counts are equal (2,916, 2,274 and 847) and the
multiset difference is empty in both directions. The whole difference is
whitespace — the reader joins words with one space and lines with one
newline, and the incumbent preserves the page's own spacing. The holdout page
was not compared that way, because doing so would spend a second holdout
access to explain a number that changes no decision; its reader glyph count
is 1,381 and its difference is the same size and shape as the others.

Nothing in the routing rules reads that length. The retired character-count
comparator does — it calls a page scanned below 50 characters — and all four
lengths are far above it under either reading, so the retired rule's recorded
50 % false "OCR not needed" rate on the mixed page is unchanged as well.

**Both OCR-routed pages are cut into the same regions, and on the rotated
one the regions are not in the same place.** The image-only page: 6 embedded
image regions under both inventories, coverage 0.99999993 under both, and the
same 5 region identities sent to OCR — the sixth is below the 2 % coverage
floor under both. The mixed page: 1 image region, coverage 1.0, one OCR
region beside a native region, under both. The mixed page routes `both`
because the inventory sees an image region on a page that also carries native
glyphs; that is the case the retired character-count rule missed, and it is
still caught.

The image-only page is rotated 180 degrees, and there the two inventories
disagree about where its six strips are. The incumbent records them in the
unrotated frame and the reader records them displayed, so each box is the
other's mirror: the incumbent's `image-2` is 0 → 120,240 thousandths across
the page and the reader's is 491,760 → 612,000. The render an OCR region is
cut from is the rotated page — the render worker renders with rotation
applied and clips within rotated page bounds — so the frame decides which
pixels an OCR call actually reads. Measured on a synthetic 180-degree page
with one embedded patch and PyMuPDF's own `get_pixmap`, the fraction of the
patch's pixels inside the recorded image region is **0.0 under the incumbent
and 1.0 under the reader-backed inventory**. `tests/test_page_inventory.py`
holds that measurement.

This changes no routing decision — the page is image-only and routes to OCR
under both — and it is not this ticket's to celebrate: it says the incumbent
has been handing OCR the wrong strip of a rotated page, and the replacement
does not.

**Table-region evidence differs, and no routing depends on it.** The two
matrix pages: the incumbent records one region of 30 × 28 and one of 61 × 28;
the reader records 34 × 29 and 65 × 29. On the mixed page the incumbent finds
no table region at all and the reader finds one, 13 × 12, with text in 15 of
its cells. These are two different table reconstructions of the same pages,
which is the point of ADR-0094 and the material #736 and #737 consume; the
router only ever asks whether a table region was read at all.

**Glyph coverage and vector density agree closely.** Coverage 0.07708173
against 0.07710677 and 0.05967886 against 0.05969674 on the two matrix pages —
the two readings measure the same glyphs with slightly different box
conventions. The vector density is equal to ten decimal places on both
(0.0001237869 and 0.0001567967), from two independently written counts of the
page's painted paths. Rotation agrees on the four non-holdout pages, 180
degrees on the image-only one and 0 on the other three, and the reader reads
0 on the holdout page.

**No structural trigger fired on any of the five pages.** Every table region
the reader found on a text-layer page had text in some of its cells, which is
what the trigger is the absence of. That is the expected result on a corpus
of native matrix pages and one scanned agreement, and it means this receipt
proves the trigger is recorded, not that it has been observed in the field.

## What this receipt does not claim

Five pages. It says that on the pages whose OCR-needed labels were frozen
before any of this was built, the replacement inventory routes exactly as the
incumbent does and exactly as the labels say. It does not say the two
inventories agree on a corpus, that the reader's table reconstruction is
better, or that the reader-backed inventory is approved for production. It is
the seam-specific regression the ticket asks for, which permits the named
configuration to be qualified; selection stays with #447 and #739.

It also does not measure Textract. The reader-backed route records
`ocr_engine: textract`, because ADR-0094 makes Textract the OCR provider for
image regions and OCR-routed pages. Nothing calls Textract here: wiring the
scanned read is #739's, and until then ingest reads an OCR region with the
incumbent engine and records that engine, its configuration and the page
scope on the attempt and on any Processing Failure. A decision that names an
engine is not an attempt that used one, and neither borrows the other's name.

## Configuration identity

Recorded in `stage1-routing-replay.json`'s `configuration`: the router version
(`page-inventory-router-reader-v1`), the coordinate frame every recorded box
is in (`displayed crop, PDF points, top-left origin`), the engine the route
names (`textract`), the reader's measured engine (`tagged`) and DPI (36), the
imported commit `c39363e26c2726b61c4e707f589093c67173e538` and the package
digest, the `PdfiumExecutor` contract as enforced on this platform, the Python
version, platform, Corridor commit and run time. The gold set and the
incumbent run it was compared against are named by path, and the holdout
ledger entry is copied into the receipt.
