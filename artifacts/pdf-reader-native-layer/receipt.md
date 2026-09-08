# #733: native page text and the native Token Layer from the paired-rendition reader

The adapter this receipt measures is disabled. Enabling it is one setting,
`native_reader_token_layer` on `corridor.config.Settings`
(`CORRIDOR_NATIVE_READER_TOKEN_LAYER`), and merging it selects nothing: #447
owns native selection, #739 owns scanned selection, and the incumbent path is
unchanged and remains the default.

Reproduce with:

```bash
make prose-locator-regression ARGS="--output artifacts/pdf-reader-native-layer/733-prose-locator-regression.json"
```

## What was measured

`733-prose-locator-regression.json`, run 2026-09-07 in 628 s over the whole
registered corpus. For every PDF the corpus manifests register, the command
derives the prose Source Segments the production segmentation
(`source_segments.pdf_prose_segments`) appends from the registered bytes, and
re-verifies each against the page text the reader-backed adapter would write —
`token_layers.page_text_projection` over the reader's native token layer. The
content store is read read-only, by digest, and nothing is written into it.

192 of the 195 registered PDFs were read (1,231 pages); the three plan sets
over 30 MB were skipped for size and are named in the receipt. No document
failed to read.

**Every segment dereferences under the reading that wrote it.** The control —
`historical_locator_already_invalid` — is 0 of 159,531, so each number below
is a fact about the new reading rather than about a broken segment set.

**The page text is a projection, on every page.** On 1,231 of 1,231 pages the
projection over the retained token layer is byte-identical to the reader's own
page text, so the page string ingest writes adds no reading of its own.

## The compatibility path, exactly

A segment verifies only when its exact stored text comes back byte for byte
and its digest still matches. Three ways, in order:

1. **unchanged** — the stored offsets still recover the stored text from the
   new page text.
2. **unique occurrence** — the offsets no longer land, and the stored text
   occurs exactly once in the new page text. One position, determined by
   counting.
3. **preserved occurrence ordinal** — the stored text occurs several times,
   the historical reading of the same registered bytes holds the same number
   of occurrences, and the segment's offsets are the *k*-th of them, so *k*
   names one occurrence in the new text.

Rules 2 and 3 recover the segment's own bytes and nothing else. Neither
normalizes, folds whitespace, matches approximately, nor picks the nearest
offset — which is why they are verifications rather than excuses. Rule 3 is a
one-time rebinding: it needs the historical reading once, to compute *k*, and
never again once the new offsets are recorded.

Everything else is **incompatible**, and stays incompatible. The traits below
say which reading difference produced it; they do not reclassify it.

## The numbers

`registered` is `doc_type: minutes` — the class whose prose segments the
record actually holds today. `extended` applies the same segmentation to every
other registered PDF class, so table-bearing and drawing pages are measured
rather than assumed to behave like minutes.

| Lane | Documents | Pages | Segments | Unchanged | Compatible (unique / ordinal) | Incompatible (absent / ambiguous) |
|---|---:|---:|---:|---:|---:|---:|
| registered (minutes) | 145 | 327 | 22,848 | 26 | **21,646** (13,782 / 7,864) | **1,176** (712 / 464) |
| extended | 47 | 904 | 136,683 | 759 | 127,344 (52,834 / 74,510) | 8,580 (5,937 / 2,643) |
| every registered PDF | 192 | 1,231 | 159,531 | 785 | 148,990 (66,616 / 82,374) | 9,756 (6,649 / 3,107) |

By document class, incompatible of segments: minutes 1,176 / 22,848 (5.1 %),
matrix 959 / 84,842 (1.1 %), agreement 91 / 890 (10.2 %), other 28 / 2,351
(1.2 %), plan 7,502 / 48,600 (15.4 %).

**This is not a clean pass, and it is not reported as one.** 1,176 of the
22,848 prose segments the record holds today do not come back under the
replacement reading by any of the three rules. 21,672 of 22,848 (94.9 %) do.

## What the incompatible segments carry

Traits of the incompatible segments' own stored text, counted over all of
them rather than sampled:

- **392** of the 1,176 registered-lane incompatibles contain a line break.
  These are Action Item spans the incumbent wrapped at one place and the
  replacement wraps at another; the stored text carries the incumbent's
  newline, so the exact bytes are absent from the new page text. Every
  multi-line incompatible on the whole corpus is a minutes Action Item.
- **43** across the corpus (25 registered) contain a soft hyphen. The
  replacement resolves `U+00AD` to a hyphen-minus where PDFium reports the
  glyph as a hyphen; the incumbent keeps the soft hyphen, so the two texts
  differ by one code point.
- The remainder are short repeated strings — an owner name, a heading — whose
  occurrence count is not the same under the two readings, so rule 3 has no
  ordinal to carry. Sampled: `Chevron` appears six times in the incumbent's
  page text and five in the replacement's.

`incompatible_samples` in the JSON holds 40 of them verbatim.

## Configuration identity

Recorded in the receipt's `configuration`: the native layer's engine
(`corridor-pdf-reader`), adapter version (`native-reader-v1`), the reader's
measured engine (`tagged`) and DPI (36), the imported commit
`c39363e26c2726b61c4e707f589093c67173e538` and the package digest; the
historical reading's engine and version; the execution contract
`PdfiumExecutor` enforced on this platform; the SHA-256 of every corpus
manifest; the Python version, platform, Corridor commit and run time.

## What this receipt does not claim

It is not the switch criterion. That is the text regression against #731's
frozen paired-rendition inputs, and it is #447's gate, not this one. This
receipt says what happens to the record's existing prose locators when the
native reading changes — no more.
