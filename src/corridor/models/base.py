"""The one declarative base, and the enumerations every family reads.

This module exists so that the family modules beside it can share exactly one
``MetaData``: a second ``DeclarativeBase`` anywhere in the package would give
Alembic two schemas to autogenerate from and split the fingerprint the baseline
test measures. Nothing here maps a table. The value tuples are here rather than
in the family that reads them most because a source enumeration is read by the
extractors, the readers and the constraints alike -- ``DEP_TYPES`` is named by
the legacy record, by the delta effects and by the notification subjects -- and
duplicating one would let two copies drift.
"""

from sqlalchemy import Enum
from sqlalchemy.orm import DeclarativeBase


__all__ = [
    "ANSWER_SEPARATOR",
    "Base",
    "CANDIDATE_KINDS",
    "CANDIDATE_STATES",
    "CRITICAL_STRATEGIES",
    "DEP_TYPES",
    "DOC_TYPES",
    "EVENT_SOURCE_KINDS",
    "EVENT_TYPES",
    "EXTRACTION_OUTCOMES",
    "NUMBERING_SCHEMES",
    "ORG_TYPES",
    "PARSE_STATUSES",
    "PLACEHOLDER_PARTIES",
    "RESOLUTION_STRATEGIES",
    "REVISION_COMPARISON_STATES",
    "STATEMENT_ATTRIBUTION_STATES",
    "STATEMENT_SCOPE_MODES",
    "SUPPORT_ROLES",
    "TEXT_SOURCES",
    "TIMING_CHANGE_DIRECTIONS",
    "TIMING_PRECISIONS",
    "is_claim",
    "is_critical",
    "is_placeholder_party",
]


DOC_TYPES = (
    "matrix",
    "minutes",
    "agreement",
    "email",
    "plan",
    "schedule",
    "spec",
    "status_report",
    "other",
)
PARSE_STATUSES = ("pending", "parsed", "failed")
# How a matrix names its rows — declared at registration, like a
# document's date, and never inferred from the data (ADR-0030).
# `project-unique`: one number names one conflict across the project
# (the TxDOT UCM form, whose retired rows exist to keep numbers stable).
# `per-party`: each External Party's list counts from 1, so a row's name
# is the party and the number together (the FDOT roundabout form).
NUMBERING_SCHEMES = ("project-unique", "per-party")
EXTRACTION_OUTCOMES = (
    "completed",
    "failed",
    "unreadable",
    "no_matrix",
    "quarantined",
)
REVISION_COMPARISON_STATES = (
    "added",
    "dropped",
    "unchanged",
    "changed",
    "ambiguous",
    "unmatched",
)
SUPPORT_ROLES = ("publication",)
# Where a page's text came from, most reliable first. `cells` is a
# spreadsheet source read natively (ADR-0005): its text was generated from
# the cells rather than recovered from a layout, which is what lets a
# citation against it verify exactly instead of at the 0.9 threshold print
# damage requires.
TEXT_SOURCES = ("cells", "text_layer", "ocr")
DEP_TYPES = (
    "utility_relocation",
    "agreement",
    "permit",
    "row",
    "railroad",
    "access",
    "other",
)
# How a utility conflict is to be resolved, as the document says it
# (ADR-0009). SHRP2 R15B publishes four alternatives; the first is
# decomposed along the Red/Brown split FDOT prints on its plans, which is
# where the line between "the facility moves" and "the facility stays"
# actually falls.
RESOLUTION_STRATEGIES = (
    "relocate",
    "remove",
    "abandon_in_place",
    "adjust_vertical",
    "protect_in_place",
    "change_design",
    "policy_exception",
)

# Criticality is a reading of the strategy, never a stored scale. The three
# here are FDOT's Red: the facility is moved, taken out, or deactivated —
# all of them scheduled work the utility owner must perform. Brown (a
# vertical adjustment to grade, 0.5 days by FDOT's own duration table) and
# Green (it stays) are not, and neither is a resolution that asks nothing
# of the owner at all.
#
# This set and the gold set's `critical` labelling rule are one sentence on
# purpose (ADR-0009). If they diverge, the M7 gate scores one definition
# against another and the number means nothing.
CRITICAL_STRATEGIES = frozenset({"relocate", "remove", "abandon_in_place"})

# What separates two answers inside one asserted resolution value.
#
# A layout that records its strategy as marked columns can mark more than
# one: 12 of WSDOT 9424's rows do, and one of them marks answers from
# opposite sides of the line above (#105). The extractor stores both
# headings and the vocabulary decides what they mean together, so the two
# sides need one agreed separator.
#
# `;` rather than `/`, because `/` is inside a heading this corpus prints —
# `Abandon / Deactivate`. It is also already one of `verify._FIELD_SEPARATORS`,
# so a joined value tokenises into the words the page really carries and
# never reads as invented text.
#
# The trailing space joins and does not split: a reader takes the value
# apart on `;` alone and normalises whitespace per answer anyway, so it
# tolerates a value written without it. Only the writer needs the space,
# and it is here rather than at the join so that one constant governs both.
ANSWER_SEPARATOR = "; "

CANDIDATE_KINDS = ("dependency", "event", "evidence")
CANDIDATE_STATES = ("pending", "accepted", "merged", "rejected")
ORG_TYPES = ("utility", "railroad", "agency", "consultant", "other")
EVENT_TYPES = (
    "commitment",
    "committed_date_change",
    "response",
    "escalation",
    "status_change",
    "closure",
)
EVENT_SOURCE_KINDS = ("cited", "verbal")
TIMING_PRECISIONS = ("day", "month", "approximate", "legacy_unknown")
STATEMENT_ATTRIBUTION_STATES = ("resolved", "unresolved")
STATEMENT_SCOPE_MODES = ("unknown", "selected", "all_active", "carried_forward")
TIMING_CHANGE_DIRECTIONS = ("earlier", "later", "unknown")


def is_claim(value: str | None) -> bool:
    """Does this asserted value say anything a source could disagree with?

    A blank cell is an absent value, not a competing one — the same
    reading the CONTRADICTION query already applied to nulls, because the
    matrix revisions add and drop columns between editions. Empty strings
    are the printed form of the same absence.

    Lives here because both readers of contradiction need it and neither
    may import the other: the exception engine computes CONTRADICTION and
    the ledger renders the "sources disagree" pill, and the ledger is the
    one that depends on the engine.
    """
    return bool(value and value.strip())


def is_critical(strategy: str | None) -> bool:
    """Does this resolution commit the External Party to substantial work?

    Takes the value rather than a Dependency: `changes.py` reads it off a
    stored report snapshot, which is a dict and not an ORM row, and a
    Dependency-shaped signature would force that caller to fake an object.

    `None` is not critical, and that is a reading of silence rather than a
    claim about the record. An inventory records conflicts without ever
    saying how they resolve — Project A's 3,235 rows assert no strategy at
    all — and treating that as critical would mark most of the corpus,
    which is the weakness ADR-0007 diagnosed in itself.
    """
    return strategy in CRITICAL_STRATEGIES


# Values a document prints where an External Party should be, meaning it
# declined to name one. `NA` is not an organization — CONTEXT.md defines an
# External Party as "the organization outside the project that owns a
# Dependency" — and 86 of Project A's rows carry it, 44 of them the whole
# last page of its oldest revision.
#
# The extractor still stores what the document printed. Dropping it would
# lose evidence; the fix is that nothing downstream treats it as a party.
PLACEHOLDER_PARTIES = frozenset(
    {"", "na", "n/a", "tbd", "none", "unknown", "no id", "-", "--", "?", "n.a."}
)


def is_placeholder_party(name: str | None) -> bool:
    """Is this the document declining to name an owner?

    Matched on the whole value, never as a substring: a real party can
    contain a placeholder's letters — `Nakina Telephone` starts with `na`
    — and blocking that would merge a named utility into the nameless
    cohort, which is worse than the defect being fixed.
    """
    return " ".join((name or "").split()).casefold() in PLACEHOLDER_PARTIES


def _enum(*values: str, name: str) -> Enum:
    """A VARCHAR plus a CHECK, not a native PG type.

    These value sets are still moving — `status_report` was added to DOC_TYPES
    once the corpus research found serial reporting. Native enums make
    every such change an ALTER TYPE; a CHECK is a one-line migration.

    `create_constraint` must be passed explicitly: it has defaulted to
    False since SQLAlchemy 1.4, so omitting it yields a bare VARCHAR that
    accepts any string at all, with the enum enforced only in Python.
    """
    return Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


def _statement_attribution_state(context) -> str:
    """Default direct ORM rows honestly from their resolved-party field."""
    return (
        "resolved"
        if context.get_current_parameters().get("stated_external_org_id") is not None
        else "unresolved"
    )


class Base(DeclarativeBase):
    pass
