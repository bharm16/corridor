---
status: accepted
---

# The model reads the structure; the document supplies the values

> **Terminology amendment, 2026-08-27 — [ADR-0048](0048-complete-glossary-adoption-preserves-record-and-source-identity.md).** Active prose follows the complete glossary adoption. Source quotations, historical measurements and interviews, and implementation or provenance identifiers retain their original spelling. Those retained names do not restore earlier customer labels or change decision authority.

Amends ADR-0004.

The validation gate (#68, `docs/m6-validation-gate.md`) measured both extraction paths on the layout the deterministic parser handles best, and the two halves of the result point in opposite directions. On recall and precision they are a wash — 99.2%/97.7% parser, 98.9%/97.4% vision, over 3,235 and 3,240 rows. On transcription accuracy they are not: **3 of 3,235** parser rows carry a field value that is not on the cited page, against **164 of 3,240** vision rows — digits dropped and transposed (`1143+77.787` for `1143+17.787`), concentrated in the stationing fields merge ranking discriminates on, invisible to the model's own confidence (96 of the failures sat at 0.98, 62 at 0.99). Meanwhile the vision path did the one thing the parser structurally cannot: FDOT SR 789 went from zero rows to 66 across 9 owners, because its External Organization lives in a page header and its printed headers (`Conflict #`, `Station Begin`) match no synonym table — PyMuPDF finds that table, and the parser maps none of its columns.

So each path is best at exactly what the other is worst at. Vision generalizes and mistranscribes; geometry transcribes and cannot generalize. The requirements are the highest success rate *and* zero per-document engineering, which is not a compromise between the two paths — it is a division of labor:

**Extraction is one path with three tiers, every tier ending at the same verification gate.**

- **Tier 0 — structured original.** Where a document is published as a spreadsheet, it is the Preferred Source File and is read natively (ADR-0005). No model.
- **Tier 1 — PDF with a text layer, the default.** The model reads the rendered page image and answers only three questions: *is there a matrix here* (`is_utility_matrix`, so `NoMatrixFound` keeps its meaning), *what does the page state for all its rows* (page attributes — the SR 789 owner), and *which printed column is which canonical field*. Code then reads every cell value out of the page's word boxes using the existing geometry machinery. **The model never writes a digit.** The 5.1% error class is not reduced; it is removed by construction, because no transcription surface exists.
- **Tier 2 — no text layer.** Scanned documents fall back to model transcription, token-verified against OCR text, with output logprobs recorded so low-confidence digits sink rows. The fallback is loud: the tier is recorded on every Extracted Proposal, so "how often do we fall back" is a measured number rather than a surprise.

Field-token and quote verification run on every tier — the invariant, not a tier feature.

What a new document costs is the point. A new header spelling, an owner in a page header, a group-title band, a combined station-and-offset column: all were parser code changes or structural impossibilities, and all become nothing — the model reads them off the page. The only surface that ever changes deliberately is the canonical field vocabulary, which is exactly the thing that should be versioned and human-gated. A column the model cannot map to any canonical field is reported unmapped, visibly, for a human to decide.

## Considered options

**Corrected transcription** — keep the model writing values under a repaired call shape (Responses API, `reasoning.effort: none` instead of the silent `medium` default, `detail: original`, the page's text layer injected as ground truth, `pattern`-constrained station fields, logprob flagging). Every one of those levers is documented and real, and this is retained as Tier 2, where there is no text layer to read values from. Rejected as the default because it shrinks the transcription error by an unknown amount where Tier 1 eliminates it by construction — measuring our way toward what the other design simply is.

**The deterministic parser as the sole path** — rejected by the same evidence as ADR-0004, now sharpened: its failure mode was never cell reading (0.1%) but structure recognition, and its synonym table is maintenance per document, not per agency — two FDOT documents in this corpus do not share a header row. It has since been deleted (#63): the synonym half went, and the half that reads cells off word boxes survives as `corridor.geometry`, which is where Tier 1 gets every value.

**Predicted outputs** seeded from the text layer — unavailable: documented for gpt-4o/4.1 only, absent from gpt-5.6-luna's feature list. **Vision fine-tuning** — unavailable: gpt-4o-only, platform winding down. Both recorded so nobody proposes them again.

## Consequences

ADR-0004's core holds — matrix extraction still reads the rendered page image, and for the same reason: the layout is only intact there. What changes is what the model is trusted to produce. "Vision reads the page" becomes "vision reads the page *structure*"; values come from the document's own text layer, which is ADR-0005's principle — the closest thing to the structured original — applied one level down.

The model's output collapses from ~300 transcribed cells to a small mapping, and output tokens were 84% of the token bill at 6× the input price. Structure-mapping is also an easier task than 300-cell transcription, which is what lets the cheapest family member stay the model, with escalation a one-string change gated on evals.

Tier 1 leans on PyMuPDF finding table geometry. It found it on all 11 corpus matrices, including both the parser could not map — but a document will eventually appear where geometry fails, and it must fall to Tier 2 loudly rather than degrade silently.

Project A's existing Extracted Proposals and Ledger records are unaffected. Tier 1 reads the same word boxes the parser read, so re-extraction buys nothing; the forward-only finding posted to #61 stands.

The gate that admitted the vision extractor admits this design the same way: A/B against the deterministic baseline on Project A and against the measured 66-row result on SR 789, before it replaces anything.
