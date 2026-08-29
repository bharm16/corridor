"""What a Candidate carries.

A Candidate is an extractor's proposal with its citations, not yet part of
the Project Record. Dependency and event Candidates may reach their explicit
Admission paths; structured Evidence rows remain technical proposals only.
Four extractors produce Candidates and four readers consume them, and
the payload was four hand-written dict literals that did not agree: the two
matrix paths carried `unverified_fields`, `unmapped_columns`,
`low_confidence_tokens` and `tier`; the two prose paths carried none of
them, so `web/queue.py` read every one through `or []` and could not tell a
key that was absent from a value that was empty.

Two of those keys differ for a reason and keep differing: a matrix cell is
transcribed, so every value must appear on the page and `unverified_fields`
names the ones that do not; an obligation read out of an agreement is the
model's phrasing of a paragraph and was never expected to appear verbatim.
The key is always present now, and empty where the question does not apply
— which is a statement, where a missing key was a guess.

Three things are settled here rather than per extractor:

- **The key set.** Every payload has the same shape, so a reader does not
  have to know which extractor produced a row to know what it can ask.
- **`citations_verified`.** It was a conjunction of three checks in
  `extract_matrix`, two in `extract_sheet`, and one in each prose
  extractor — and `adjudicate.accept_candidate` then overwrote it with a
  fourth that read only the quote flag, so a row that sank in the queue
  because a value was not on its page came out of acceptance recorded as
  verified.
- **The dedupe hint's shape.** Which fields discriminate is genuinely per
  document kind — an agreement has no stationing — but the separator and
  the blanks-are-kept rule are not, and `merge.rank_matches` blocks on
  them.
"""

from __future__ import annotations

from collections.abc import Sequence

from corridor.models import Candidate, Document


def dedupe_hint(*parts: str) -> str:
    """The blocking key `merge` groups on, from its discriminating parts.

    Blanks are kept rather than dropped, so a row missing a part does not
    collide with a shorter one that happens to match on the rest.
    """
    return "|".join(parts)


def citations_verified(payload: dict) -> bool:
    """Whether this proposal's citations hold, by one rule.

    A citation holds when its quote was found on the page it names, no
    field value is missing from that page, and no transcribed token was one
    the model hesitated over. The last two are empty for extractors that
    read prose rather than cells, so the rule reduces to the quote there
    without either caller having to special-case it.

    Read off the payload rather than passed in, so Adjudication can ask the
    same question of an edited payload that the extractor asked of the
    original one.
    """
    citations = payload.get("citations") or []
    return (
        bool(citations)
        and all(c.get("verified") for c in citations)
        and not (payload.get("unverified_fields") or [])
        and not (payload.get("low_confidence_tokens") or [])
    )


def propose(
    document: Document,
    *,
    kind: str,
    fields: dict[str, str],
    page_no: int,
    quote: str,
    quote_verified: bool,
    whole_row: bool,
    confidence: float | None,
    prompt_version: str,
    dedupe: str,
    text_source: str | None,
    model: str | None = None,
    tier: str | None = None,
    unverified: Sequence[str] = (),
    unmapped: Sequence[str] = (),
    low_confidence: Sequence[str] = (),
) -> Candidate:
    """One proposal, with its citation and everything a reviewer needs.

    `quote_verified` is passed rather than computed: the two matrix paths
    apply different thresholds to it (ADR-0005 — a citation against cells
    verifies exactly, one against print at 0.9), and that is the caller's
    knowledge, not this module's.
    """
    payload = {
        "kind": kind,
        "fields": fields,
        "citations": [
            {
                "document_id": document.id,
                "page": page_no,
                "quote": quote,
                "verified": quote_verified,
                "whole_row": whole_row,
            }
        ],
        "confidence": confidence,
        # Named rather than counted, so a reviewer sees *which* value is not
        # on the page instead of only that one of them is not. Empty for
        # extractors reading prose, where a field is a phrasing rather than
        # a transcription.
        "unverified_fields": list(unverified),
        # What the document says that the Ledger has no field for. The
        # trigger for a deliberate vocabulary extension, not something an
        # extractor may decide for itself.
        "unmapped_columns": list(unmapped),
        # Transcribed digits the model hesitated on. Empty on any tier that
        # transcribes nothing.
        "low_confidence_tokens": list(low_confidence),
        "tier": tier,
        "dedupe_hint": dedupe,
        # OCR text is materially noisier, and a citation resting on it
        # deserves to be visibly different when a reviewer weighs it.
        "text_source": text_source,
    }

    return Candidate(
        project_id=document.project_id,
        kind=kind,
        payload_json=payload,
        source_document_id=document.id,
        source_pages=[page_no],
        confidence=confidence,
        prompt_version=prompt_version,
        model=model,
        citations_verified=citations_verified(payload),
    )
