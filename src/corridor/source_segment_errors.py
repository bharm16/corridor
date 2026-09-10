"""Fail-closed source replay failures shared by historical and native locators.

These types used to live with the incumbent parser. Keeping the failure
contract independent lets each recorded reader replay its own locators
without making the new reader import the old parser or creating a cycle.

There are two families here and the line between them is the whole point. A
``SourceSegmentIntegrityError`` says a locator was followed and the source
disagreed; the ``FreshReadingUnavailable`` family says nothing was followed,
because the reader or the exact reading the locator indexes is not obtainable
here. ``locator_validation`` turns the first into *Not found at cited
location* and the second into *Cited location cannot be re-read*, so a refusal
placed in the wrong family makes the product assert something about a
customer's document that no reader ever checked.
"""


class SourceSegmentIntegrityError(ValueError):
    """A segment cannot be proven against its registered source bytes."""


class SourceDocumentDigestMismatch(SourceSegmentIntegrityError):
    """The supplied bytes are not the segment's registered Document."""


class SourceSegmentDigestMismatch(SourceSegmentIntegrityError):
    """Stored or dereferenced segment text does not match its digest."""


class SourceSegmentLocatorMismatch(SourceSegmentIntegrityError):
    """A typed locator is invalid or no longer recovers the stored text."""


class FreshReadingUnavailable(RuntimeError):
    """The reader that established this locator is not installed here.

    Deliberately **not** a ``SourceSegmentIntegrityError``. An integrity error
    means a locator was followed and the source disagreed; this means nothing
    was followed at all, because ADR-0094 retired the reader that could and
    #741 removed it. A caller that caught the two alike would relabel every
    retained citation of that scheme as *Not found at cited location*, which is
    a statement about the source that no reader made
    (``locator_validation.NOT_RE_READABLE`` is the state that says so).

    It lives beside the integrity errors so that ``source_segments`` can raise
    it without importing ``retained_history``, which imports ``source_segments``
    inside a call for exactly the same reason: absence is the answer, and it is
    not allowed to make either contract unimportable.
    """

    def __init__(self, locator_scheme: str, missing: str | None) -> None:
        super().__init__(
            f"a {locator_scheme} locator can only be re-read at its original "
            f"location by the reader that established it, and "
            f"{missing or 'that reader'} is not available here"
        )
        self.locator_scheme = locator_scheme
        self.missing = missing


class NativeReaderUnavailable(FreshReadingUnavailable):
    """The recorded native reader identity or configuration is not installed here.

    The same fact as the base class, for the ``pdf_span``/``pdf_cell`` scheme
    rather than for a retired one. Some native reader is in the product, but
    not the one the locator was established with -- a different reader
    version, a different application assembly, a configuration this build does
    not offer -- so ``read_native_pdf`` returned a reading of a different
    identity and the locator was never followed. Nothing opened the cited
    location, so nothing may say the passage is not at it.
    """


class RecordedReadingNotReproduced(FreshReadingUnavailable):
    """The recorded reader ran and did not return the reading the locator indexes.

    **This is an availability refusal, not an integrity error, and the choice
    is deliberate.** A ``pdf_span`` is a pair of offsets into the page string
    of one exact reading and a ``pdf_cell`` is an address inside one exact
    reading's reconstructed tables; both are meaningless against a different
    reading, which is why ``reading_sha256`` is recorded beside them. When the
    recorded reader identity is installed, runs on the registered bytes, and
    returns a reading with a different ``reading_sha256`` -- a changed text
    projection, an upgrade the identity did not distinguish, a reader that is
    not deterministic -- then the reading the locator points into does not
    exist here. The locator was therefore not followed and no page was opened.

    Calling that ``SourceSegmentLocatorMismatch`` made the Source Passage
    Check report *Not found at cited location*, which asserts that a reader
    went to the cited location and the passage was not there. Nothing asserted
    that. The truthful answer is that this system cannot return to the
    location, which is ``locator_validation.NOT_RE_READABLE`` and the label
    *Cited location cannot be re-read*.

    Two things this refusal deliberately does not do. It does not hide a
    changed source: the registered Document digest is checked first and on its
    own, and a source whose bytes changed still raises
    ``SourceDocumentDigestMismatch``. And it grants no authority: like every
    other non-``valid`` status it leaves ``evidence_link_verified`` false, so a
    recorded ``reading_sha256`` that the reader does not produce -- forged or
    merely superseded, and indistinguishable from here -- can never read as a
    passed check.
    """
