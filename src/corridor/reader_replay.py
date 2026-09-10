"""Replay a native PDF batch without decoding the document for every locator.

The scalar reader remains the historical public seam. Batch callers used it
in a loop, starting one isolated PDFium process and rebuilding every source
segment for each selected segment. This operation reuses the authenticated
reading and immutable index already used by native Fact replay. It owns the
source snapshot until all locators pass and returns nothing if the source
bytes or Document identity changed. Keeping this separate from the frozen
reader assembly preserves the identity of previously captured readings.
"""

from collections.abc import Iterable
from hashlib import sha256
import json
from pathlib import Path

from corridor.models import Document, SourceSegment
from corridor.native_matrix_bindings import native_replay_index
from corridor.reader_segments import NATIVE_KINDS
from corridor.source_segment_errors import (
    NativeReaderUnavailable,
    RecordedReadingNotReproduced,
    SourceDocumentDigestMismatch,
    SourceSegmentDigestMismatch,
    SourceSegmentLocatorMismatch,
)
from corridor.token_layers import NativePdfReading, PDF_SEGMENT_SCHEME, read_native_pdf


def replay_native_segments(
    document: Document,
    segments: Iterable[SourceSegment],
    path: Path | str,
    *,
    native_reading: NativePdfReading | None = None,
) -> tuple[str, ...]:
    """Validate every native locator with one decode per recorded reading.

    An operation that already captured a sealed reading can supply it. A batch
    containing historical configurations otherwise reads each configuration
    once; none is silently interpreted using another configuration's result.

    Every refusal keeps its own family. A locator followed into the reproduced
    reading that reaches different content is a ``SourceSegmentIntegrityError``;
    an absent reader configuration or a reading the installed reader does not
    reproduce is a ``FreshReadingUnavailable``, because no page was opened.
    A write-time caller refuses either way, which is why the distinction used
    to look free -- but the same segments are read later by the Source Passage
    Check, and one family says *Not found at cited location* while the other
    says *Cited location cannot be re-read*.
    """
    original = Path(path)
    identity = (document.id, document.project_id, document.sha256)

    def require_snapshot() -> None:
        if (document.id, document.project_id, document.sha256) != identity:
            raise SourceSegmentLocatorMismatch("native replay Document identity changed")
        if sha256(original.read_bytes()).hexdigest() != identity[2]:
            raise SourceDocumentDigestMismatch(
                "source bytes do not match the registered Document digest"
            )

    require_snapshot()
    if native_reading is not None and (
        not isinstance(native_reading, NativePdfReading)
        or native_reading.rendition_sha256 != identity[2]
    ):
        raise SourceSegmentLocatorMismatch("native replay needs the same sealed reading")
    indexes = {}
    result = []
    for segment in segments:
        if (document.id, document.project_id, document.sha256) != identity or (
            segment.document_id != identity[0] or segment.project_id != identity[1]
        ):
            raise SourceSegmentLocatorMismatch(
                "source segment does not belong to the supplied Document"
            )
        if segment.kind not in NATIVE_KINDS:
            raise SourceSegmentLocatorMismatch("native replay requires a native PDF segment")
        if sha256(segment.exact_text.encode()).hexdigest() != segment.content_sha256:
            raise SourceSegmentDigestMismatch("stored segment digest does not match its text")
        reader_identity = segment.reader_identity or {}
        try:
            config = reader_identity["native_layer"]
            if reader_identity["scheme"] != PDF_SEGMENT_SCHEME:
                raise KeyError("scheme")
            engine, dpi = config["configuration"]["reader_engine"], config["dpi"]
        except (KeyError, TypeError) as exc:
            raise SourceSegmentLocatorMismatch(
                "native segment reader identity is incomplete"
            ) from exc
        key = (json.dumps(reader_identity, sort_keys=True), segment.reading_sha256)
        if key not in indexes:
            reading = native_reading or read_native_pdf(
                original, source_sha256=identity[2], engine=engine, dpi=dpi
            )
            # The batch owes the same two availability answers the scalar
            # reader gives. Both used to be one locator mismatch here, which
            # made the Source Passage Check report *Not found at cited
            # location* for a reading nothing ever opened.
            if reader_identity != reading.identity:
                raise NativeReaderUnavailable(
                    PDF_SEGMENT_SCHEME, "the recorded native reader configuration"
                )
            if segment.reading_sha256 != reading.reading_sha256:
                raise RecordedReadingNotReproduced(
                    PDF_SEGMENT_SCHEME, "the recorded native reading"
                )
            indexes[key] = native_replay_index(reading)
        encoded = indexes[key].get((segment.kind, segment.ordinal))
        if encoded is None:
            raise SourceSegmentLocatorMismatch("native source locator does not exist")
        expected = json.loads(encoded)
        if any(getattr(segment, key) != value for key, value in expected.items()):
            raise SourceSegmentLocatorMismatch("native source locator changed under replay")
        result.append(expected["exact_text"])
    require_snapshot()
    return tuple(result)
