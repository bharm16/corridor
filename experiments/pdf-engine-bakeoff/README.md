# PDF engine bake-off environment

This is an isolated, non-production evidence environment for issue #720. It does
not provide Corridor runtime code or an engine adapter.

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
provides a dependency-free strict validator. No benchmark result is committed by
this issue.
