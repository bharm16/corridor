# #735: render derivatives from PDFium under the measured profiles

What was compared, on which bytes by SHA-256, with which engines and versions,
and the result. An explained difference is recorded as a difference; it is not
a pass, and nothing here selects an engine for production (#447 owns that).

`735-corpus-render-comparison.json` is the full record, one row per document,
profile and measure. It was produced by:

```
make render-rasterizer-compare ARGS="--output artifacts/render-rasterizer-comparison/735-corpus-render-comparison.json"
```

## What was compared

Page 1 of every PDF the corpus manifests lock, by digest, at most 20 MB: 192
documents, under the `review` (200 DPI), `ocr_layout` (300 DPI, grayscale and
denoise) and `table_cv` (300 DPI, deskew, grayscale, denoise, adaptive
threshold, line morphology) profiles, rendered twice — 576 page-profile
comparisons. Three documents were over the byte bound and were not rendered
(`2611bbf6…`, 32 MB; `375b4c36…`, 99 MB; `92f456c2…`, 55 MB). No render
failed under either engine.

The pages carry the rotations and page shapes the corpus actually holds: 179
at 0°, 8 at 90°, 1 at 180° and 4 at 270°, across 145 sets of coordination
minutes, 18 matrices, 16 plan sheets, 10 agreements and 3 other documents.

| | |
|---|---|
| Legacy engine | PyMuPDF 1.28.2 |
| Replacement engine | pypdfium2 5.13.0 on PDFium 153.0.7999.0 |
| Shared | OpenCV 5.0.0, Pillow 12.3.0, NumPy 2.5.2 — the preprocessing is untouched |
| Render profiles | `gold/pdf/v1/render-profiles.json`, SHA-256 `63fef79a098b37c6e454d705be409132aad059914cf2536f0bacd049fab01696`, gold dataset `2026-08-31.2` |

## The result

| Measure | Tolerance | Result |
|---|---|---|
| Media box, crop box, rotation | exact | identical on 576 of 576 |
| Raster dimensions | within 1 pixel | identical on 576 of 576; largest difference 0 px |
| Affine chain, step for step | same steps | identical on 576 of 576 |
| Mean absolute difference over the page | ≤ 0.02 of full scale | median 0.007, worst 0.037; above tolerance on 10 rows |
| Ink placed within 1 pixel, both directions | ≥ 0.98 | median 0.998, 1st percentile 0.960, worst 0.834; below tolerance on 27 rows |
| Ink intersection over union | recorded, not a gate | median 0.855, worst 0.478 |

541 of 576 rows are within every declared tolerance: `review` 182 of 192,
`ocr_layout` 182 of 192, `table_cv` 177 of 192. **Byte equality was never
asked for and is not asserted anywhere**; the two engines produce different
PNGs for every page, which is what two independent rasterizers do.

Intersection over union is recorded on every row and gates nothing, and the
reason is visible in the numbers above: it binarizes a difference, so it
measures how each engine anti-aliases a 300-DPI stroke as much as where the
stroke is. Its median of 0.855 sits beside a median tone difference of 0.7%
and a median ink placement of 99.8%.

## The 35 rows outside a tolerance

Every one was re-rendered under both engines and compared again at each
integer offset from -2 to +2 pixels in both axes. If a page were rendered from
the wrong box, at the wrong rotation, or off by part of a point, one offset
would fit far better than none.

- On **24** of the 35, no offset fits better than none.
- On **11**, a one-pixel offset fits slightly better — ten of them one pixel
  up, on rotated text-dense matrices and scans. On none of the eleven does the
  difference go away there: the best offset leaves between 70% and 97% of the
  unshifted difference (`1b949a1a…` at `ocr_layout`, the largest improvement,
  goes from 0.0216 to 0.0152). A displaced page would collapse to near zero.
  What remains is sub-pixel glyph placement inside a page frame that is
  provably identical: a fixture block whose edges land on whole pixels is
  rendered onto exactly the pixel the manifest chain predicts, by both
  engines, at all four rotations
  (`tests/test_render_profiles.py::test_both_rasterizers_place_a_block_on_the_pixel_the_chain_predicts`).

Three causes account for all 35.

**Tone, on 10 rows (`mean_absolute_difference` above 0.02).** Dense matrices
and plan sheets where PDFium draws marginally heavier. The worst is
`329ab37f3fbc…` (Utility Inventory Matrix, 6/20/2025) at `review`: 0.037 of
full scale, ink placement 1.000 in both directions, page mean level 206.3
against 199.5 out of 255. Aligned exactly — shifting it one pixel in any
direction roughly doubles the difference — and uniformly darker.

**Binarization, on 27 rows (ink placement below 0.98).** Pixels that straddle
the 128 threshold in one engine and not the other, on hairline CAD linework
and on scans. The worst `review` case, `0c0cde2d5fd9…` (Utility conflict
exhibit, draft), has 5.5% of MuPDF's ink pixels with no PDFium ink within a
pixel — while the two rasters differ by 0.4% of full scale in mean tone and
by 0.9 levels of 255 in mean level. A 0.4% difference cannot be a displaced
page; it is faint hairline pixels landing on either side of a threshold.

**Deskew, on 2 rows, and it is worth acting on.** Of the 35 rows, exactly two
have a different `table_cv` deskew angle under the two engines; on the other
33 the two engines' rasters produce the same angle to the tenth of a degree.
The worst row of all,
`fb748d4ac7fb…` (City of Houston Signal Agreement, executed, `table_cv`, ink
IoU 0.478), is not an engine difference in the ordinary sense. `table_cv`
estimates page skew with a Hough transform quantised to 0.1°, and this scan's
skew sits on a bin boundary: MuPDF's raster yields -0.900° and PDFium's
-1.000° (the other, `d0f1a75d3592…`, splits -0.300° against -0.250°). The whole binarized image is then rotated a tenth of a degree apart,
which is up to 5.8 px at the corners of a 3300 px page, and the morphology
that follows amplifies it. The two rasters underneath differ by 0.8% of full
scale. Each derivative's manifest records its own deskew angle and matrix, so
each chain is correct for its own artifact — but the step is data-dependent,
and any change to the input pixels can move it. That is a property of the
profile, not of PDFium, and it would behave the same way if the same engine
were run on a slightly different scan.

## The one systematic difference in the manifests

Nine rows — three documents under all three profiles — record a different
`pdf_user_to_page` flip constant. Page space is the crop box's top-left
corner, so that constant is the crop box's top edge in PDF user space.
PyMuPDF's `page.cropbox` is the crop box already flipped about the media box,
which loses the media box's own origin, and the legacy chain reads its `y1`.
The two agree exactly whenever the media box starts at y = 0, which is every
page in the corpus but these three:

| Document | Media box origin | Legacy shortfall |
|---|---:|---:|
| `bcf988a9faed…` CDOT US 6 Bridges utility matrix (11/27/2012) | y0 = 13.578 pt | 13.578 pt |
| `c15f7ed8671e…` Appendix U3 utility owner contact list | y0 = 11.962 pt | 11.962 pt |
| `e619a4abf604…` Appendix U2 existing utility listing | y0 = 11.962 pt | 11.962 pt |

On those pages the legacy constant is short by exactly the media box origin,
so the legacy chain maps PDF user space a fraction of an inch too low. The
PDFium path reports the true top edge; the legacy path keeps reporting what it
has always reported, because the derivatives already written record it and a
rollback has to reproduce them. `workers/render/legacy_pymupdf.py` says so at
the seam. Correcting the legacy chain is not part of #735.
