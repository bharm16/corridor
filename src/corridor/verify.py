"""Citation verification: does this quote actually appear on this page?

This answers exactly one question, and deliberately not the question of
whether the claim is *true*. `evidence_links.verified` means the quote is
really on the cited page and nothing more.

A failed citation is never silently dropped. It marks its candidate
unverified and sinks it in the review queue, because an extractor that
invents a quote is the single most damaging failure this system can have —
a fabricated citation looks exactly like a real one to a reader.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Fixed by v0-build-spec.md §7. Changing it silently changes every
# citation-validity and recall number ever recorded.
THRESHOLD = 0.9

# PDF extraction breaks words across lines with a hyphen. "reloca-\ntion"
# and "relocation" are the same word, and a citation should not turn on
# where the typesetter wrapped a line.
_LINE_BREAK_HYPHEN = re.compile(r"-\s*\n\s*")
_WHITESPACE = re.compile(r"\s+")
_REMOVED_PRINT_BREAK = r"(?:\u00ad|-\s*\n\s*)*"

_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "­": "",  # soft hyphen
    }
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _LINE_BREAK_HYPHEN.sub("", text)
    text = text.translate(_PUNCTUATION)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().casefold()


def match_ratio(quote: str, page_text: str) -> float:
    """Best local match of `quote` anywhere in `page_text`, 0.0 to 1.0."""
    q = normalize(quote)
    p = normalize(page_text)
    if not q or not p:
        return 0.0
    if q in p:
        return 1.0

    # Comparing the quote against the whole page would score near zero on
    # length alone, so anchor on the longest shared run and score the
    # quote-sized window around it.
    matcher = SequenceMatcher(None, q, p, autojunk=False)
    block = matcher.find_longest_match(0, len(q), 0, len(p))
    if block.size == 0:
        return 0.0

    start = max(0, block.b - block.a)
    window = p[start : start + len(q)]
    return SequenceMatcher(None, q, window, autojunk=False).ratio()


def quote_appears_on(quote: str, page_text: str, threshold: float = THRESHOLD) -> bool:
    return match_ratio(quote, page_text) >= threshold


def literal_quote_on_page(quote: str, page_text: str) -> str | None:
    """Return the page's exact characters for a normalization-only match.

    Fuzzy citation verification can establish that a damaged print extraction
    refers to a page without making the stored transcription an exact quote.
    Model-visible Evidence needs the stricter property: the returned value must
    be a literal substring of the registered page.
    """
    if not quote or not page_text:
        return None
    if quote in page_text:
        return quote
    target = normalize(quote)
    if not target or target not in normalize(page_text):
        return None
    equivalents = {
        "'": "['‘’]",
        '"': '["“”]',
        "-": "[-–—−]",
    }
    pieces: list[str] = []
    for character in target:
        if character == " ":
            pieces.append(r"\s+")
        else:
            pieces.append(equivalents.get(character, re.escape(character)))
        pieces.append(_REMOVED_PRINT_BREAK)
    pattern = re.compile("".join(pieces), re.IGNORECASE)
    for match in pattern.finditer(page_text):
        literal = match.group()
        if normalize(literal) == target:
            return literal
    return None


def threshold_for(text_source: str | None) -> float:
    """How closely a quote must match, given where the page text came from.

    `THRESHOLD` is a concession to print damage: a printout loses
    separators between text spans and clips cells at their boundaries, so a
    quote that really is on the page can come back slightly wrong, and
    demanding an exact match would fail true citations.

    None of that can happen to text generated from a spreadsheet's own
    cells (ADR-0005). The value was never recovered from a layout, so a
    near-miss there is a real disagreement rather than damage, and
    accepting one would spend the tolerance on nothing.
    """
    return 1.0 if text_source == "cells" else THRESHOLD


# --------------------------------------------------------------- field values

# Some layouts combine stationing and offset in one column, and the page's
# own tokenization keeps them together — `1112+90,713.40'` is one word.
# Splitting that cell into two real fields is correct and both halves are on
# the page, so the comma must not read as invented text.
#
# Deliberately not `+`: `1149+00` has to stay one token, because splitting it
# is exactly how a transcribed `1140+00` would slip through.
_FIELD_SEPARATORS = re.compile(r"[,;/]")
_EDGE_PUNCTUATION = ".,;:()[]'\"-"

# Below this a token is enum-ish — `R`, `Y`, `UG` — and matches somewhere on
# any page, so requiring it proves nothing and rejecting it fails every row.
MIN_TOKEN_CHARS = 4


def tokens(text: str | None) -> set[str]:
    """Page or field text as tokens, cut on punctuation as well as space.

    Cutting on punctuation rather than matching substrings is what keeps
    this able to catch a clipped cell: `othwell` is still not `rothwell`,
    where a substring test would call it found.
    """
    out = set()
    for word in normalize(text or "").split():
        for piece in _FIELD_SEPARATORS.split(word):
            piece = piece.strip(_EDGE_PUNCTUATION)
            if piece:
                out.add(piece)
    return out


def value_appears_on(value: str | None, page_text: str) -> bool:
    """Is every token of this field value really on this page?

    Citation verification is row-level and fuzzy at 0.9, so a transcribed
    `1140+00` where the document says `1149+00` sits inside a perfectly
    valid row quote and passes. Verified Candidates sort to the top of the
    review queue, so that wrong value would reach a reviewer wearing a
    green check. This is the only mechanical check on field *values*.

    Deliberately biased towards flagging. The stored text stream sometimes
    concatenates spans without a separator (`freeway1148+60`), which costs
    a few false failures — 3 in 40,417 tokens across Project A's five
    matrices. A failure marks a Candidate unverified and sinks it in the
    queue; it never drops it, so being wrong in that direction is cheap
    and being wrong in the other is not.
    """
    return _on_page(value, tokens(page_text))


def unverified_fields(fields: dict[str, str], page_text: str) -> set[str]:
    """Which of these field values are not text on this page.

    Tokenises the page once for the whole row rather than once per field —
    a matrix page is a few thousand tokens and a row has twenty of them.
    """
    page = tokens(page_text)
    return {name for name, value in fields.items() if not _on_page(value, page)}


def _on_page(value: str | None, page: set[str]) -> bool:
    return all(
        token in page for token in tokens(value) if len(token) >= MIN_TOKEN_CHARS
    )
