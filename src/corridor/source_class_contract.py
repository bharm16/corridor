"""The one versioned semantic source-class contract, interpreted by both paths (#951).

Two records name the source classes they permit: the limited onboarding grant's
typed scope (``onboarding_authorization``) and the activated source
authorization's per-binding ``permitted_source_classes``
(``source_authorization``).  #951's amendment makes both effective, and requires
that they interpret a class the *same* way while keeping their *permitted sets*
their own.  This module is that shared interpretation, and nothing but the
interpretation: it recognises a closed set of classes, states the version it
recognises them under, and keeps the facets a class decomposes into -- format,
revision role, channel suitability and processing policy -- separate, so that a
filename, an extension or a MIME type (which name at most the *format*) can never
stand in for the *class* and grant semantic processing on their own.

**Why interpretation is shared but permission is not.** A signed UCM workbook is
a ``ucm_revision`` whether a coordinator uploads it during onboarding or a
connector delivers it after activation; the two paths must agree on that or a
class permitted on one path would be a different thing on the other. What they do
*not* share is which classes each permits: a limited onboarding grant does not
require the ordinary activated source set, so each path passes its own permitted
classes to :func:`evaluate` and reads the same recognition back.

**No inference.** :func:`evaluate` never widens a permitted set, never matches on
a substring, and never treats an unrecognised token as permitted. A class the
contract does not know is ``unrecognised`` -- an attributable reissue or a
validated classification is what resolves it, not a guess here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


#: The version a record was interpreted under, retained on the classification
#: claim so a later reading knows which contract said what a class means. It
#: moves when the recognised set or an interpretation changes; a grant or a
#: delivery classified under an older version is readable history, not silently
#: reinterpreted.
CONTRACT_VERSION = "source-class/2026-09-11"


@dataclass(frozen=True, slots=True)
class SourceClassInterpretation:
    """One recognised class, decomposed into the facets #951 keeps separate.

    The facet values are internal interpretation labels, not customer
    vocabulary: their only job is to hold the dimensions apart so that a format
    a filename could reveal is never the whole of what a class means.
    """

    source_class: str
    source_format: str
    revision_role: str
    channel_suitability: str
    processing_policy: str


#: The closed set of semantic source classes, keyed by the identifier both the
#: onboarding grant scope and the activated bindings already record. Every class
#: here is one that appears in the authorized-set corpus; a new class is a
#: deliberate edit of this contract and a version bump, never a string a caller
#: introduces.
_CONTRACT: dict[str, SourceClassInterpretation] = {
    "ucm_revision": SourceClassInterpretation(
        source_class="ucm_revision",
        source_format="spreadsheet_workbook",
        revision_role="record_revision",
        channel_suitability="connected_or_upload",
        processing_policy="structured_record_extraction",
    ),
    "schedule_export": SourceClassInterpretation(
        source_class="schedule_export",
        source_format="tabular_export",
        revision_role="schedule_revision",
        channel_suitability="connected_or_upload",
        processing_policy="structured_record_extraction",
    ),
    "matrix": SourceClassInterpretation(
        source_class="matrix",
        source_format="tabular_export",
        revision_role="record_revision",
        channel_suitability="connected_or_upload",
        processing_policy="structured_record_extraction",
    ),
    "email": SourceClassInterpretation(
        source_class="email",
        source_format="email_message",
        revision_role="correspondence",
        channel_suitability="connected_inbound",
        processing_policy="verbal_statement_capture",
    ),
    "minutes": SourceClassInterpretation(
        source_class="minutes",
        source_format="narrative_document",
        revision_role="meeting_record",
        channel_suitability="connected_or_upload",
        processing_policy="verbal_statement_capture",
    ),
}

#: The recognised classes, sorted for a stable reading.
KNOWN_SOURCE_CLASSES: tuple[str, ...] = tuple(sorted(_CONTRACT))


#: A declared class was empty, so the delivery states no classification at all.
SOURCE_CLASS_UNDECLARED = "source_class_undeclared"
#: A declared class is not one this contract recognises.
SOURCE_CLASS_UNRECOGNIZED = "source_class_unrecognized"
#: A recognised class the permitted set does not name.
SOURCE_CLASS_NOT_PERMITTED = "source_class_not_permitted"


@dataclass(frozen=True, slots=True)
class SourceClassDecision:
    """What one permitted set says about one declared class.

    ``recognised`` is the shared half -- whether the contract knows the class at
    all -- and ``permitted`` is the caller's half, decided against the set it
    passed. A refused decision names ``reason`` with one of the constants above,
    so an operator sees whether to reissue the permission or the classification.
    """

    permitted: bool
    recognized: bool
    reason: str
    source_class: str


def is_recognized(source_class: str) -> bool:
    """Whether the contract knows this class. Exact identifier, never a prefix."""

    return source_class in _CONTRACT


def interpret(source_class: str) -> SourceClassInterpretation | None:
    """The facets this class decomposes into, or ``None`` if it is unknown."""

    return _CONTRACT.get(source_class)


def evaluate(
    permitted_source_classes: Sequence[str], declared_class: str
) -> SourceClassDecision:
    """Whether ``declared_class`` is permitted by this set, under one interpretation.

    Both paths call this with their own permitted classes. The recognition is
    the contract's; the permission is the set's. Nothing is inferred: an empty
    declaration, an unrecognised class and a recognised-but-excluded class are
    three different refusals, and a permitted class is one the set names exactly.
    """

    declared = (declared_class or "").strip()
    if not declared:
        return SourceClassDecision(
            permitted=False,
            recognized=False,
            reason=SOURCE_CLASS_UNDECLARED,
            source_class="",
        )
    if declared not in _CONTRACT:
        return SourceClassDecision(
            permitted=False,
            recognized=False,
            reason=SOURCE_CLASS_UNRECOGNIZED,
            source_class=declared,
        )
    if declared not in set(permitted_source_classes):
        return SourceClassDecision(
            permitted=False,
            recognized=True,
            reason=SOURCE_CLASS_NOT_PERMITTED,
            source_class=declared,
        )
    return SourceClassDecision(
        permitted=True, recognized=True, reason="", source_class=declared
    )
