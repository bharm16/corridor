"""Corridor's PDF adapter package: the measured paired-rendition reader, imported unchanged.

This is commit c39363e of the standalone comparison repository's `replacement/`
(the pypdfium2 + pypdf reader with its deterministic table reconstructor and
cell-ID semantics tier) and `bootstrap/` (the paired-rendition harness: corpus,
sealed split, answer keys, scorer, tally), with the fixture PDFs, the node
number-format printer and the baseline receipts (#729, ADR-0006, ADR-0008).
`source-manifest.json` and `provenance.py` make "unchanged" a checked claim.

This is production. It was imported by nothing when it arrived as a measured
challenger, and that sentence stood here long after it stopped being true:
`ingest`, `token_layers`, `page_inventory`, `scanned_reading`, `native_matrix`
and a dozen more corridor modules import it now, and ADR-0094/ADR-0095 plus
#741 left it as the only PDF reader in the product. What is still true is that
the material here is unchanged from the comparison repository, so the baseline
receipts for #731's measurement remain readable against it, and that nothing in
this package *selects* a configuration for a customer document (#447 owns that).
`execution.py` holds the PDFium execution contract every caller goes through,
and `rendition_cli.py` prints a stored Document Rendition as the reader sees it.
"""
