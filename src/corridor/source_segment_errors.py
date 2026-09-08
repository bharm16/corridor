"""Fail-closed source replay failures shared by historical and native locators.

These types used to live with the incumbent parser. Keeping the failure
contract independent lets each recorded reader replay its own locators
without making the new reader import the old parser or creating a cycle.
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
