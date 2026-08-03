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
