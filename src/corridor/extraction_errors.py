"""Shared extraction outcomes independent of a particular Matrix reader.

These failures used to live inside the retired geometry extractor. Batch and
historical adapters still distinguish a failed attempt from an unsupported
sequencing document, without importing any extraction implementation.
"""


class NoMatrixFound(Exception):
    """No table or sheet in the document had recognizable utility-matrix headers.

    Raised rather than returning an empty list, because those two outcomes
    mean completely different things and only one of them is a bug.
    """


class ExtractionFailed(RuntimeError):
    """The extractor could not complete the read; no partial result is usable."""


class SequencingSemanticsDetected(RuntimeError):
    """The document asserts work sequencing, outside Corridor's modeled scope."""


class NativeObservationFailed(ExtractionFailed):
    """A native refusal whose terminal observation is already persisted."""

    def __init__(self, detail: str, *, observation_id: int):
        super().__init__(f"{detail} (native observation {observation_id})")
        self.observation_id = observation_id
