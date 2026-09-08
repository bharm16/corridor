# #736: exact text and physical source location have separate outcomes

The frozen #733 receipt and its original index remain byte-for-byte unchanged:
`733-prose-locator-regression.json` SHA-256 `82d5349f2e859828612344b9bf7592896b3b16a9c2f55fc8a878de314278a4c3`; `receipt.md` SHA-256 `4aa4f2b8abd52bf5d9471d9f18700e74f37480eed9a9a428ad05eadf6f7a457d`.

That experiment reconstructed incumbent prose spans from registered corpus
bytes. It did not query persisted Source Segments, accepted references or
customer citations. Its original 1,176 / 22,848 Minutes and 9,756 / 159,531
combined incompatibilities concern this generated population only.

The clarified audit was run at implementation commit
`46c6e25c093dd7923cb62018597a247a9f64a2a5` with its unchanged code fingerprints
recorded in [the JSON receipt](736-prose-text-location-clarification.json).
It started at `2026-09-08T05:09:07+00:00` and took 491.3 seconds.
Receipt SHA-256: `ce7ce8f6dc84be3073ce2d30f8edc4849cac47043b969ea30a80d4746f5485ef`. The local content store was read only; no model
or AWS call was made. The command refuses to overwrite an existing receipt.

```bash
CORPUS_STORE=/Users/bryceharmon/Desktop/corridor/corpus/files \
CORRIDOR_STORAGE_BACKEND=filesystem \
make prose-locator-regression \
  ARGS="--output artifacts/pdf-reader-native-layer/736-prose-text-location-clarification.json"
```

| Generated population | PDFs | Pages | Spans | Exact text present | Exact text absent | Physical location unknown |
|---|---:|---:|---:|---:|---:|---:|
| Minutes | 145 | 327 | 22,848 | 22,136 | 712 | 22,848 |
| Other PDF classes | 47 | 904 | 136,683 | 130,746 | 5,937 | 136,683 |
| All covered PDFs | 192 | 1,231 | 159,531 | 152,882 | 6,649 | 159,531 |

Three plan sets exceed the retained 30 MB limit and are identified in the
JSON. There were zero document failures or invalid historical offset/digest
controls. All 1,231 native token projections matched reader page text.

No replacement locator is proved or emitted. Equal strings at old offsets,
equal occurrence counts and ordinal, and a lone replacement occurrence all
remain insufficient to establish the same physical source occurrence. The
old 9,756 incompatibilities comprise 6,649 absent exact strings plus 3,107
ambiguous strings that were present; neither category becomes a verified
location. Of the absent strings, 392 contain line breaks and 43 contain soft
hyphens. These traits do not normalize away a mismatch or establish its cause.

The regression tests include physically reversed duplicate labels in an
authored PDF: both readers contain REVIEW twice and at offset zero, but one
starts at the lower block and the other at the upper block. Reordered columns,
a lone surviving duplicate, multiline text, soft hyphens, invalid controls,
and receipt overwrites are also covered. A same-baseline two-column fixture
produces one STATUSSTATUS word box; that box cannot identify either substring
independently. All these text-only matches retain unknown location.

This audit supplies no database/citation census and grants no production
selection. #447 retains native source-class and historical compatibility
qualification. #741 retains engine removal and customer-citation preservation.
New native locator/rotation/crop/deskew proofs are separate integration tests;
the paired typed-cell handoff is measured through its own registered receipt.
