"""One Extracted Proposal, built the way an extractor builds one.

``src/corridor/candidates.py`` exists to settle this payload.  Its docstring
says the shape "was four hand-written dict literals that did not agree", that
the key set "is settled here rather than per extractor" so a reader "does not
have to know which extractor produced a row to know what it can ask", and that
``citations_verified`` had four answers before it had one.  The test tree never
adopted any of that: a census of ``tests/`` finds 105 ``Candidate(payload_json=
{...})`` literals across 64 modules, and three modules importing ``propose``.

Those literals are shapes no extractor can produce.  ``propose`` always emits
``unmapped_columns``, ``tier`` and ``text_source``; of the 105 literals four
carry the first, six the second and twenty the third.
``src/corridor/facts.py`` reads all three back through ``.get(...)`` -- the
"cannot tell a key that was absent from a value that was empty" failure
``candidates.py`` says was fixed -- so a reader that started distinguishing the
two would pass here and fail on a real extraction.  ``tests/test_web.py`` had
grown a second helper whose entire body was patching those three keys back
onto the first one's output for five tests.

``propose`` is deep for an extractor, which knows every one of its ten required
keyword arguments and why, and shallow for a test, which knows the row and the
quote and has no opinion about the rest.  That asymmetry is why the tree went
around it.  The defaults live here; the payload is still written there.  No key
of it is named in this module, so a key added to ``propose`` arrives in every
test that calls this without one of them being edited.

``fields`` and ``quote`` stay required.  A default row would let a test assert
against a matrix row it never named, which is the same class of mistake as
asserting against a payload no extractor emits.
"""

from __future__ import annotations

from collections.abc import Sequence

from corridor.candidates import propose
from corridor.models import Candidate, Document
from corridor.vocabulary import dedupe_hint


PROMPT_VERSION = "txdot_ucm_v1"


def proposal(
    document: Document,
    *,
    fields: dict[str, str],
    quote: str,
    kind: str = "dependency",
    page_no: int = 1,
    quote_verified: bool = True,
    whole_row: bool = True,
    confidence: float | None = 1.0,
    prompt_version: str = PROMPT_VERSION,
    dedupe: str | None = None,
    text_source: str | None = "text_layer",
    model: str | None = None,
    tier: str | None = None,
    unverified: Sequence[str] = (),
    unmapped: Sequence[str] = (),
    low_confidence: Sequence[str] = (),
) -> Candidate:
    """One proposal against `document`, in the shape an extractor writes.

    Every argument is ``propose``'s, defaulted where a test would otherwise
    repeat what it does not care about.  ``dedupe`` defaults to what
    ``corridor.vocabulary`` says discriminates one row from another, which is
    the same blocking key the matrix readers pass; a caller whose rows collide
    on party, kind and stationing passes its own. ``text_source`` defaults to
    the text layer because a fixture's page almost always carries one; a
    workbook fixture says ``cells``.

    The Candidate is not added to a Session.  Whether a proposal is flushed,
    sealed into an ExtractionRun, or declared active is the calling module's
    subject, not this one's.
    """

    return propose(
        document,
        kind=kind,
        fields=fields,
        page_no=page_no,
        quote=quote,
        quote_verified=quote_verified,
        whole_row=whole_row,
        confidence=confidence,
        prompt_version=prompt_version,
        dedupe=dedupe_hint(fields) if dedupe is None else dedupe,
        text_source=text_source,
        model=model,
        tier=tier,
        unverified=unverified,
        unmapped=unmapped,
        low_confidence=low_confidence,
    )
