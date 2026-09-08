"""Compatibility entrypoint for Matrix callers after legacy retirement (#766).

The word-box grid and structure/transcription prompts are retired. New reads
use the explicitly selected native pipeline and its own prompt/schema identity;
old runs keep their recorded versions. This entrypoint also preserves the
shared failure types used by project and historical acceptance adapters.
"""

from corridor.native_matrix import PROMPT_VERSION, SCHEMA_VERSION  # noqa: F401
from corridor.vocabulary import TEMPLATE_FIELDS  # noqa: F401


from corridor.extraction_errors import ExtractionFailed, SequencingSemanticsDetected  # noqa: F401


def extract_document(session, document, *, client=None):
    """Use the production route; never fall back to the retired reader."""
    from corridor.pipeline import extraction_route

    return extraction_route(document, client=client).extract(session, document)
