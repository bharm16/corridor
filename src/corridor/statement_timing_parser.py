"""Replay stated date precision through one shared, deterministic parser.

Minutes v5 originally owned these exact day/month rules. The spine and legacy
adapter now share them, so a source phrase cannot acquire a second date meaning.
Range options are an explicit extension used by the new minutes contract only.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
import re

_NUMERIC_DAY = re.compile(
    r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(\d{4})\b"
)


_ISO_DAY = re.compile(r"\b(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")


_NUMERIC_MONTH = re.compile(r"\b(0?[1-9]|1[0-2])/(\d{4})\b")


_MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        (
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    if name
}


_NAMED_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+"
    r"(?:(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?(?:,\s*|\s+))?"
    r"(\d{4})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExactTiming:
    """One calendar timing recognized directly in exact Evidence."""

    start_offset: int
    end_offset: int
    value: dict[str, str | None]


def exact_statement_timings(quote: str) -> tuple[ExactTiming, ...]:
    """Read non-overlapping day/month timings directly from exact Evidence."""

    found: list[ExactTiming] = []

    def add(match: re.Match[str], value: dict[str, str | None]) -> None:
        if any(
            match.start() < existing.end_offset
            and existing.start_offset < match.end()
            for existing in found
        ):
            return
        found.append(ExactTiming(match.start(), match.end(), value))

    for match in _NUMERIC_DAY.finditer(quote):
        year, month, day = int(match[3]), int(match[1]), int(match[2])
        try:
            exact = date(year, month, day)
        except ValueError:
            continue
        add(match, _day_timing(match.group(0), exact))

    for match in _ISO_DAY.finditer(quote):
        year, month, day = int(match[1]), int(match[2]), int(match[3])
        try:
            exact = date(year, month, day)
        except ValueError:
            continue
        add(match, _day_timing(match.group(0), exact))

    for match in _NUMERIC_MONTH.finditer(quote):
        year, month = int(match[2]), int(match[1])
        add(match, _month_timing(match.group(0), year, month))

    for match in _NAMED_DATE.finditer(quote):
        year = int(match[3])
        month = _MONTHS[match.group(1).casefold()]
        if match.group(2):
            try:
                exact = date(year, month, int(match.group(2)))
            except ValueError:
                continue
            value = _day_timing(match.group(0), exact)
        else:
            value = _month_timing(match.group(0), year, month)
        add(match, value)

    return tuple(sorted(found, key=lambda timing: timing.start_offset))


def _day_timing(text: str, value: date) -> dict[str, str | None]:
    rendered = value.isoformat()
    return {
        "text": text,
        "precision": "day",
        "start_date": rendered,
        "end_date": rendered,
    }


def _month_timing(text: str, year: int, month: int) -> dict[str, str | None]:
    return {
        "text": text,
        "precision": "month",
        "start_date": date(year, month, 1).isoformat(),
        "end_date": date(year, month, monthrange(year, month)[1]).isoformat(),
    }


def statement_timing_options(text: str) -> tuple[ExactTiming, ...]:
    """Source-cited days, months and explicit endpoint ranges, without guessed precision."""
    exact = exact_statement_timings(text)
    result = []
    index = 0
    while index < len(exact):
        current = exact[index]
        if index + 1 < len(exact):
            following = exact[index + 1]
            gap = text[current.end_offset:following.start_offset].strip().casefold()
            changed = re.search(r"\b(?:changed?|moved?|revised?|rescheduled?|shifted?|extended?)\b", text[:current.start_offset], re.I)
            if gap in {"to", "through", "–", "—", "-"} and not changed:
                if current.value["start_date"] <= following.value["end_date"]:
                    result.append(ExactTiming(current.start_offset, following.end_offset, {
                        "text": text[current.start_offset:following.end_offset], "precision": "range",
                        "start_date": current.value["start_date"], "end_date": following.value["end_date"],
                    }))
                index += 2
                continue
        result.append(current)
        index += 1
    qualified = []
    for option in result:
        qualifier = re.search(r"\b(?:early|mid|late|around|about|approximately)\s+$", text[:option.start_offset], re.I)
        if qualifier:
            option = ExactTiming(qualifier.start(), option.end_offset,
                {"text": text[qualifier.start():option.end_offset], "precision": "approximate", "start_date": None, "end_date": None})
        qualified.append(option)
    return tuple(qualified)


def is_required_timing(text: str, option: ExactTiming) -> bool:
    """Keep an explicit Required By clause out of a party's Promised Timing."""
    prefix = re.split(r"[.;\n]", text[:option.start_offset])[-1]
    roles = list(re.finditer(r"\b(required by|need(?:ed)? by|promised (?:for|by)|moves? to|will (?:finish|complete)|committed to)\b", prefix, re.I))
    return bool(roles and re.match(r"required|need", roles[-1].group(0), re.I))


def promised_timing_options(text: str) -> tuple[ExactTiming, ...]:
    return tuple(option for option in statement_timing_options(text) if not is_required_timing(text, option))
