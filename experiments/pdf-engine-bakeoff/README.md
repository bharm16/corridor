# PDF engine bake-off environment

This is the isolated, non-production evidence environment frozen by #720 and
the incumbent-only adapter harness built by #721. It does not provide Corridor
runtime code. `adapter_protocol.py` is the durable adapter boundary; #721's
registry contains exactly one real implementation, `pymupdf`.

The project deliberately uses direct URLs for exactly three Linux x86-64 wheels.
The lock has a SHA-256 for each wheel and no source distribution. Install only
with:

```bash
uv sync --project experiments/pdf-engine-bakeoff --frozen --no-dev
```

Do not replace the URLs with registry constraints. If the wheel is unavailable,
incompatible, or fails to import, stop the experiment. Do not build locally,
download a native engine at runtime, or configure a system PDF engine. In
particular, the pinned `pypdfium2` wheel contains PDFium; a source/system mode is
not an equivalent environment.

`candidate-manifest.v1.json` freezes distribution provenance,
`corpus-manifest.v1.json` points to the unchanged Stage 0 truth,
`result-schema.v1.json` defines the normalized receipt, and `contract.py`
provides a dependency-free strict validator. Run the generated-fixture
self-comparison without resolving or downloading dependencies:

```bash
make pdf-engine-bakeoff
```

Receipts, render digests, per-operation observations, and the compact JSON and
Markdown summaries are written beneath ignored `out/pdf-engine-bakeoff/`.
Timing, process startup, RSS, and byte observations are deliberately outside
the deterministic digest. No challenger module is imported or executed.
