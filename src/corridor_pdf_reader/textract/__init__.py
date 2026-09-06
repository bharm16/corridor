"""AWS Textract as the last rung of the PyMuPDF replacement.

One page in, the reader's page shape out: `render` rasterises a page at a
stated resolution, `client` makes the cached and retried AnalyzeDocument
call, `blocks` turns the response into the tables and outside strings that
`bootstrap.read` writes, `remap` puts the document's own glyphs into
Textract's cell geometry on pages that have a text layer, and `read` writes
a harness run that `bootstrap.score` scores unchanged. Nothing under
`replacement/` or in the scorer changes.
"""
