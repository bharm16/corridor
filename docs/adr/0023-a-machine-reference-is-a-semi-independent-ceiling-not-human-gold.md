# A machine reference is a semi-independent ceiling, not human gold

ADR-0008 required a maintainer to author a held-out denominator by hand after the cold extraction. The maintainer rejected recurring row labelling before the WSDOT 9540 seal was spent, so Extraction Measurement instead uses a machine-authored reference produced through a meaningfully different reading path. Every artifact names the exact Extraction Runs, the reference method, its coverage, its author-time or honest backfill provenance, and any shared dependencies with the extractor. Machine-reference bytes and their scope manifest are first-write-only evidence. A shared parser or table detector means the result is a semi-independent ceiling on observed error: a failing score blocks the gate and requires diagnosis, while a passing score is informative but cannot prove completeness or semantic truth.

## Considered options

**Founder-authored gold.** Rejected because the product cannot depend on recurring founder annotation and the cost does not scale to new layouts or projects.

**Treat model or parser consensus as ground truth.** Rejected because correlated readers can share blind spots and confidently agree on the same error.

**Skip Extraction Measurement.** Rejected because mechanical provenance tests alone cannot detect missing rows, layout drift, or a broken measurement population.

## Consequences

The reference must never be derived from the exact Candidate population it scores, and disagreement may diagnose the instrument but does not authorize a retake of a spent holdout. Machine-reference scores cannot support an unqualified recall or semantic-accuracy claim. Stronger evidence may come from authoritative structured originals, downstream customer corrections, or independent audits, but none is fabricated when absent.
