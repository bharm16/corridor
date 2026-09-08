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
