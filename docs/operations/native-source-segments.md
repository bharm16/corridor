# Native PDF source segments (#736)

The disabled native adapter produces one `NativePdfReading` from registered
PDF bytes. Its exact result supplies native page text, Token Layers, and
`pdf_span` / `pdf_cell` Source Segments. This implementation does not select
a production configuration; `native_reader_token_layer` remains false.

A reading records the rendition SHA-256, native adapter and locator scheme,
actual PDFium/pypdfium2 and pypdf versions, executable reader source digest,
and native assembly digest. Its result digest includes page geometry,
characters, reconstructed cells and clipped glyphs. Timings, experiment logs,
scoring files and retained incumbent/OCR code do not define native replay.
The reading constructor is sealed; a caller cannot construct it from a
model's literal text. Returned Token Layers do not expose mutable retained
reading state.

`ingest_native_reader(session, document=..., path=..., images_dir=...)` is the
explicit challenger operation, including on an already-ingested document.
It returns the reading and appended segments, and persists new Token Layers.
It preserves historical `DocPage` projections and Source Segments. The
configured native ingest path creates new native segments from the exact
reading that supplied its page projection; it never creates incumbent prose
beside replacement text. Ordinary document deduplication still executes an
explicitly enabled challenger.

Native spans record character bounds and a `span_stream`: `page` is the
visible native projection, `clipped` is a separate clipped-glyph projection.
The latter records the reader's available spacing without claiming to restore
missing whitespace or to be visible page text. Every span retains original
glyph membership and outside/ambiguous membership. A glyph claimed by several
cells cannot become a cell segment; it remains in its source span.

Cells additionally record page, table, row, column and row/column spans. Build
`NativeCellIndex(document, reading)` once for a mapping operation. A
`pdf_cell_id(document, reading.reading_sha256, page, table, row, column)` binds
the project and registered Document as well as the exact source reading.
`select_pdf_cell_segment(..., document=document, index=index, page_no=...,
table_index=..., cell_id=...)` compares one stored segment with the indexed
canonical value and locator. It accepts no value literal. Reusing a reading
in another project does not reuse that project's model-facing cell IDs.

Native replay follows its recorded scheme and reader identity, then compares
the full result, locator, exact text and digest. An unavailable native reader
version refuses replay. Historical `prose_span` segments retain their original
incumbent reader; their offsets are never sliced into replacement page text.
Schema changes add nullable native columns to those old rows and preserve
their IDs, content, offsets and references. The same SECURITY DEFINER source
append command remains the writer; runtime roles receive no raw insert or
accepted-record authority. Downgrade refuses to discard any native reading.

The remaining semantic handoffs are explicit: #737 owns matrix Tier 1 cell-ID
mapping, and #447 owns native Minutes/statement mapping and source-class
qualification. A native-only Minutes source refuses the unimplemented
statement handoff. Adding a shadow reading does not invalidate legitimate
legacy processing. #741 owns retained engine removal and customer-citation
preservation; text-only corpus matches do not prove physical compatibility.

Validation includes a populated supported-predecessor transition, immutable
append and scoped cell refusals, constructed-literal refusals, clipped stream
replay, and pixel inspection through original PDF coordinates, asymmetric
non-zero crop origins, all four rotations, selected detail crops and actual
non-zero deskew. The deskew pixel fixture uses a declared grayscale profile
after the production deskew step so glyph pixels remain observable; ordinary
table morphology intentionally removes text. Paired measurements separately
score the frozen reader and the actual in-memory typed segment handoff. They
do not claim full-corpus database persistence or a customer-citation audit.
