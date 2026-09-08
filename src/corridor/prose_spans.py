"""Exact non-overlapping spans over one already chosen page string.

The legacy PDF parser formerly owned both parsing and these text-only
boundaries. Separating the deterministic boundaries lets native segments
reuse the Minutes rules without invoking or importing the incumbent reader.
Offsets always refer to the caller's specific string, never another reading.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class NumberedActionSpan:
    """One numbered Action Item with the marker excluded from exact wording."""

    number: int
    exact_text: str
    start: int
    end: int


_ACTION_ITEMS_HEADING = re.compile(
    r"^[ \t]*Action Items(?:[ \t]*[:\-\u2013\u2014])?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_NUMBERED_ITEM = re.compile(r"^[ \t]*(\d+)\.[ \t]*", re.MULTILINE)
_PAGE_FOOTER = re.compile(r"^[ \t]*Meeting Notes[ \t]*$", re.MULTILINE)
_BLANK_BLOCK_BOUNDARY = re.compile(r"\r?\n[ \t]*\r?\n")
_TEXT_LINE = re.compile(r"[^\r\n]+")
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=[ \t]+[A-Z0-9])")


def numbered_action_spans(text: str) -> tuple[NumberedActionSpan, ...]:
    """Enumerate non-overlapping exact Action Item wording from Minutes text."""

    headings = tuple(_ACTION_ITEMS_HEADING.finditer(text))
    spans: list[NumberedActionSpan] = []
    for heading_index, heading in enumerate(headings):
        section_end = (
            headings[heading_index + 1].start()
            if heading_index + 1 < len(headings)
            else len(text)
        )
        footer = _PAGE_FOOTER.search(text, heading.end(), section_end)
        if footer is not None:
            section_end = footer.start()
        starts = tuple(_NUMBERED_ITEM.finditer(text, heading.end(), section_end))
        for index, marker in enumerate(starts):
            raw_start = marker.end()
            raw_end = (
                starts[index + 1].start() if index + 1 < len(starts) else section_end
            )
            blank = _BLANK_BLOCK_BOUNDARY.search(text, raw_start, raw_end)
            if blank is not None:
                raw_end = blank.start()
            start, end = _trimmed_bounds(text, raw_start, raw_end)
            if start < end:
                spans.append(
                    NumberedActionSpan(
                        number=int(marker.group(1)),
                        exact_text=text[start:end],
                        start=start,
                        end=end,
                    )
                )
    return tuple(spans)


def page_prose_ranges(text: str) -> tuple[tuple[int, int], ...]:
    action_ranges = tuple(
        (item.start, item.end) for item in numbered_action_spans(text)
    )
    spans: list[tuple[int, int]] = []
    cursor = 0
    for start, end in action_ranges:
        spans.extend(_plain_prose_ranges(text, cursor, start))
        spans.append((start, end))
        cursor = end
    spans.extend(_plain_prose_ranges(text, cursor, len(text)))
    return tuple(sorted(spans))


def _plain_prose_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in _TEXT_LINE.finditer(text, start, end):
        line_start, line_end = _trimmed_bounds(text, line.start(), line.end())
        if line_start >= line_end:
            continue
        cursor = line_start
        for boundary in _SENTENCE_BOUNDARY.finditer(text, line_start, line_end):
            sentence_start, sentence_end = _trimmed_bounds(text, cursor, boundary.end())
            if sentence_start < sentence_end:
                ranges.append((sentence_start, sentence_end))
            cursor = boundary.end()
        sentence_start, sentence_end = _trimmed_bounds(text, cursor, line_end)
        if sentence_start < sentence_end:
            ranges.append((sentence_start, sentence_end))
    return ranges


def _trimmed_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
