"""Shared extraction outcomes independent of a particular Matrix reader.

These failures used to live inside the retired geometry extractor. Batch and
historical adapters still distinguish a failed attempt from an unsupported
sequencing document, without importing any extraction implementation.
"""


class ExtractionFailed(RuntimeError):
    """The extractor could not complete the read; no partial result is usable."""


class SequencingSemanticsDetected(RuntimeError):
    """The document asserts work sequencing, outside Corridor's modeled scope."""
