---
status: accepted
domain: extraction
scope: current product
supersedes:
  - ADR-0063
---

# Unreadable cells resolve by corroboration, not review

Some scanned pages defeat OCR. The value is on the paper, but no single read of it is trustworthy. ADR-0063 proposed double reads producing suggested transcriptions for human confirmation — a person reviewing AI readings of a scan. The maintainer rejected that outright on 2026-08-30: this product exists so people do coordination, not data annotation. No transcription-review surface may exist. This ADR supersedes ADR-0063 (which was never accepted) and states the design that replaces it.

## The three-state value model

A field read from a degraded source is one of:

- **Verified** — the value carries a citation that passes the ordinary checks.
- **Unconfirmed reading** — the value is present in the source but no reading of it is proven. The field holds the best candidate with full provenance ("16 — unconfirmed reading, degraded scan, p.14"), displays flagged as unconfirmed, never contributes to Ready, and upgrades automatically the moment corroboration arrives. It is not empty: the paper says something, and the record says honestly what we think it says and why that isn't proof.
- **Absent** — the source genuinely has no value there.

## The reading harness

One bounded agent works each eligible cell, in the same cage as the Statement Review Assistant: enumerated read-only tools, hard per-cell time/token/spend budgets, a receipt for every step, deterministic validation of the output, no authority-shaped fields. Its tools:

- **Image operations** over the pinned page bytes: deskew, denoise, contrast, upscale, crop and zoom to the cell region.
- **OCR engines and model reads** over those transforms — diverse reads (different models, different transforms) as candidate generation, never as proof.
- **Corpus reads**: the prior revision's paired row (the comparison machinery already pairs them), sibling documents (SUE tables, inventories, agreements, registered messages), and the registries.

The reading and the corroboration feed each other: a corpus candidate tells the reader what to test against the pixels; a pixel candidate tells the searcher what to hunt. If preprocessing yields a usable text layer, the page simply exits the class — the ordinary single-read-plus-citation-check path applies and the harness never runs.

## The admission rule

- **A corroborated value admits through the readable source's citation.** The harness's claim ("16 — corroborated: SUE table p.3, '16-inch gas main'") is checked by code against that readable source, and the exact rules admit on that citation. The agent finds the evidence; the evidence does the proving. Model agreement is never a Record Inclusion predicate — ADR-0042 stands untouched.
- **A reading without corroboration** becomes the unconfirmed state, fail-closed, with all receipts. The living-document machinery upgrades it without ceremony when any corroborating document later arrives — the next revision, the requested workbook, an email stating the fact.
- **A failed attempt** stays unconfirmed with receipts saying why. The chase for the residue is the product's normal machinery — request the structured file, ask on the next call — never a review queue.

Extending admission to corroborated cross-document citations is an expansion of automatic behavior: it activates only after passing the ADR-0050 regression replay.

## Eligibility and discipline

A page enters the harness only under a named, versioned profile with declared deterministic OCR-quality checks, page scope, tool and model identities, and budgets — declared before it can run, refused visibly when missing. The harness cannot widen its own scope, cannot retry beyond its budgets, and everything it reads is pinned bytes. Text inside scans and corpus documents is data, never instructions.

## Consequences

Supersedes ADR-0063 (status updated to superseded; its authority instinct — model agreement is not proof — survives here; its presentation and confirmation flow do not). Ticket #369 is rescoped around this design. No human transcription-review card exists anywhere in the product.
