"""Corridor's PDF adapter package: the measured paired-rendition reader, imported unchanged.

This is commit c39363e of the standalone comparison repository's `replacement/`
(the pypdfium2 + pypdf reader with its deterministic table reconstructor and
cell-ID semantics tier) and `bootstrap/` (the paired-rendition harness: corpus,
sealed split, answer keys, scorer, tally), with the fixture PDFs, the node
number-format printer and the baseline receipts (#729, ADR-0006, ADR-0008).
`source-manifest.json` and `provenance.py` make "unchanged" a checked claim.

No production module imports this package. It is the baseline configuration
for #731's measurement and the material #733 onwards integrate behind
disabled adapters; nothing here selects it for production (#447 owns that).
`execution.py` holds the PDFium execution contract every caller goes through,
and `rendition_cli.py` prints a stored Document Rendition as the reader sees it.
"""
