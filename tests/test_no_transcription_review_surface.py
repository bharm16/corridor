"""There is no transcription-review surface, and this is what holds that shut (#739).

ADR-0063 proposed exactly this surface: two model reads of an unreadable cell,
a suggested transcription, and a person to confirm it. The maintainer rejected
it outright, ADR-0064 replaced it with corroboration, and ADR-0094 kept the
rejection when it made Textract the OCR provider — a Textract-only value is an
Unconfirmed reading, upgraded mechanically when a corroborating source arrives,
and never a card in a queue.

That is a promise about what the product does *not* contain, so it is checked
against the real route table, the real templates and the real module graph
rather than asserted in a comment. The rules below are deliberately literal: a
surface of this kind cannot be built without tripping one of them, and each
failure message says which promise was broken.

`display_reading` is the one thing a surface may use, and it is a flag rather
than a control: it answers what state a cell's value is in and whether it may
count toward Ready, and it offers nothing to submit.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from corridor import scanned_reading, unreadable_cells
from corridor.token_layers import EngineIdentity, TokenLayer
from corridor.unreadable_cells import ReadingDisplay
from corridor.web.app import app


def _textract_layer() -> TokenLayer:
    return TokenLayer(
        page_no=1,
        origin="ocr",
        source_sha256="a" * 64,
        identity=EngineIdentity(
            origin="ocr",
            engine="textract",
            engine_version="1.0",
            adapter_version="ocr-textract-v1",
        ),
        tokens=(),
        quality={},
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "corridor" / "web"
TEMPLATES = WEB_ROOT / "templates"

# The vocabulary a transcription-review surface would have to use somewhere:
# in its path, in its handler's name, in its template's name, or in the words
# on the screen. A surface that avoided every one of these would also be
# unreachable and unreadable by the person it was built for.
REVIEW_VOCABULARY = (
    # The stem, not the verb: "transcrib" misses "transcription", which is the
    # word such a surface would actually be called.
    "transcri",
    "unreadable-cell",
    "unreadable_cell",
    "cell-reading",
    "cell_reading",
    "confirm-reading",
    "confirm_reading",
    "unconfirmed-reading",
    "unconfirmed_reading",
    "ocr-review",
    "ocr_review",
)

# Which modules may reach the reading machinery from the web package, and what
# they may take from it. Empty of writers by construction: no HTTP handler may
# run the reading harness, rescue a page, append a reading, or promote one.
# `display_reading` and `contributes_to_ready` are read-only answers about a row
# that already exists and are the only names a surface would ever need.
WEB_READING_IMPORTS: dict[str, frozenset[str]] = {}
READ_ONLY_NAMES = frozenset({"display_reading", "contributes_to_ready", "ReadingDisplay"})
READING_MODULES = (
    "corridor.unreadable_cells",
    "corridor.unreadable_cell_admission",
    "corridor.scanned_reading",
)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _web_modules() -> list[Path]:
    return [
        path
        for path in sorted(WEB_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def test_no_route_offers_a_transcription_review_surface():
    """The real route table, not a grep of the source that declares it."""
    offenders = []
    for route in app.routes:
        path = getattr(route, "path", "")
        name = getattr(route, "name", "") or ""
        endpoint = getattr(route, "endpoint", None)
        haystack = f"{path} {name} {getattr(endpoint, '__name__', '')}".lower()
        hit = [word for word in REVIEW_VOCABULARY if word in haystack]
        if hit:
            offenders.append((path, name, hit))

    assert offenders == []


# Where a control lives in a template: what it is called, what it submits to,
# and what it navigates to. Prose is deliberately not scanned — a screen is
# allowed to say that no transcription is required, and one does — so the rule
# is about the affordances, which is what the rejected surface would have to
# add. Every attribute below either names a form field, submits it, or links
# to a page that would.
CONTROL_ATTRIBUTES = re.compile(
    r"""(?:name|id|for|action|formaction|href|hx-post|hx-get|hx-put|hx-delete)\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)


def test_no_template_offers_a_control_over_a_transcription_or_a_reading():
    offenders = {}
    for template in _templates():
        text = template.read_text(encoding="utf-8")
        hit = sorted(
            {
                word
                for value in CONTROL_ATTRIBUTES.findall(text)
                for word in REVIEW_VOCABULARY
                if word in value.lower()
            }
        )
        if hit:
            offenders[str(template.relative_to(REPO_ROOT))] = hit

    assert offenders == {}
    assert _templates(), "the guard must be scanning real templates"
    assert CONTROL_ATTRIBUTES.findall(
        (TEMPLATES / "queue.html").read_text(encoding="utf-8")
    ), "the control scan must be finding real attributes"


def test_no_template_is_named_for_one():
    offenders = [
        str(template.relative_to(REPO_ROOT))
        for template in _templates()
        if any(word in template.name.lower() for word in REVIEW_VOCABULARY)
    ]

    assert offenders == []


def test_no_web_module_reaches_the_reading_machinery_except_to_read_a_state():
    """A handler cannot run the harness, rescue a page, append a reading or promote one."""
    offenders = {}
    for path in _web_modules():
        relative = str(path.relative_to(REPO_ROOT))
        allowed = WEB_READING_IMPORTS.get(relative, READ_ONLY_NAMES)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.ImportFrom) and node.module in READING_MODULES:
                taken = {alias.name for alias in node.names}
                if not taken <= allowed:
                    offenders.setdefault(relative, set()).update(taken - allowed)
            elif isinstance(node, ast.Import):
                taken = {alias.name for alias in node.names if alias.name in READING_MODULES}
                if taken:
                    offenders.setdefault(relative, set()).update(taken)

    assert offenders == {}


# The one function that takes a transcription, and why it is not the rejected
# surface: the transcription is the *model's* Tier 2 output, not a person's
# typed reading, and the check can only detect disagreement with what the
# provider read. It returns no value anybody may accept — its `corroborates`
# is a constant False — so nothing downstream can turn it into a confirmation.
TIER_2_CHECK = {
    "corridor.scanned_reading.check_transcription_against_reading": {"transcribed"},
}


def test_nothing_accepts_a_persons_transcription_of_a_cell():
    """The rejected surface's shape: a parameter a person's typed reading would arrive in."""
    forbidden = {"transcription", "transcribed", "transcribed_value", "confirmed_value", "suggested_value"}
    offenders = {}
    for module in (unreadable_cells, scanned_reading):
        for name, value in vars(module).items():
            if name.startswith("_") or not callable(value) or not inspect.isfunction(value):
                continue
            if value.__module__ != module.__name__:
                continue
            qualified = f"{module.__name__}.{name}"
            taken = (set(inspect.signature(value).parameters) & forbidden) - TIER_2_CHECK.get(qualified, set())
            if taken:
                offenders[qualified] = sorted(taken)

    assert offenders == {}
    # The exemption is exact: the Tier 2 check still exists and still cannot
    # corroborate, so the reason it is exempt has not quietly gone away.
    assert scanned_reading.check_transcription_against_reading(
        "anything", _textract_layer()
    ).corroborates is False


def test_the_display_of_a_reading_is_a_flag_and_not_a_control():
    """What a surface may render carries no affordance to submit anything."""
    fields = set(ReadingDisplay.__dataclass_fields__)

    assert fields == {"state", "value", "flagged", "contributes_to_ready", "label"}
    assert not any(
        word in name
        for name in fields
        for word in ("confirm", "accept", "correct", "approve", "review", "submit", "action")
    )
