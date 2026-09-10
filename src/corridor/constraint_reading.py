"""One reading type behind the Constraint Log, the Coordination Report and the
alert engine.

Before this module there were two shapes for one subject. A legacy project's
readers took a ``Dependency`` row; an adopted-baseline project's readers took an
``AcceptedConstraint`` that had grown twenty-four properties named exactly like
that row's columns, eleven of which were hard-coded to ``None`` because the
accepted record has no such value at all. Duck-typing over those Nones made
every reader assert an absence: the alert engine fired ``MISSING_OWNER`` and
``ORPHAN`` from them until it was patched rule by rule, and the report published
"No key dates are linked to these Constraints" when the true fact was that the
reader could not see key dates.

So the reading is its own type and every field is one of two things: a value, or
a ``NotAvailable`` marker carrying the reason in the words the report shows a
customer. The two populations become two adapters onto it — nothing else reads a
``Dependency`` column off an accepted record, and no absence is invented.

The adapters take their subject duck-typed on purpose. Importing
``models.Dependency`` here would add a consumer to ADR-0081 stage 4's ratchet
for a module whose whole job is to stop legacy shapes spreading; reading the
columns off whatever the caller resolved keeps this module free of the frozen
tables (``tests/test_native_accepted_readers.py`` asserts that).

``NotAvailable`` is falsy and stringifies to its reason, so an incidental
renderer degrades exactly as it did when the field was ``None``. What may not
happen any more is a *rule* treating it as an absence: the engine tests
``isinstance(..., NotAvailable)`` and skips explicitly, which is the difference
between "we did not check this" and "this is empty".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Mapping

from corridor.models import is_critical

UNAVAILABLE = "not available in this mode"

LEGACY = "legacy"
ACCEPTED_RECORD = "accepted_record"


@dataclass(frozen=True)
class NotAvailable:
    """This reader cannot see this fact, and says so rather than guessing."""

    reason: str = UNAVAILABLE

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return self.reason


# The reasons, in the words the report already uses for its coverage
# declarations (``issue_rendering.CHECKS_NOT_RUN`` is the model): a reader who
# meets one of these should learn why the cell is empty, not that the project is.
NO_COORDINATION_DECISION = NotAvailable(
    "this project's accepted record carries no internal owner, next action or "
    "action due date, so none is shown or checked"
)
NO_KEY_DATE_LINK = NotAvailable(
    "key dates are not linked to constraints in this mode, so nothing is "
    "measured against one"
)
NO_DOCUMENTATION_REVIEW = NotAvailable(
    "this project's accepted record records no documentation review outcome, so "
    "no constraint is shown as reviewed or not reviewed"
)
NO_LEGACY_EVIDENCE_DATES = NotAvailable(
    "document dates are not tracked against this project's accepted values, so "
    "no age is measured from them"
)
NO_LEGACY_DISPUTES = NotAvailable(
    "a source that disagrees with the accepted record is raised as a proposed "
    "change with a question about that one value, not as a disagreement here"
)
NO_LEGACY_SUPPORT_REGISTRY = NotAvailable(
    "supporting documentation for an accepted value is read from its support "
    "assessments, not from the legacy support registry"
)
NO_DISMISSAL = NotAvailable(
    "an accepted record is removed by a record decision, never by dismissing a row"
)
NO_LEGACY_FIELD = NotAvailable(
    "this project's accepted record holds no such value"
)


def accepted_record_title(record) -> str:
    """The matrix materialization policy: utility type and party.

    Never ``conflict_description`` reinterpreted as a title. It lives beside the
    adapter that builds the reading, so the appendix and the reading cannot
    compose one record's title two ways.
    """
    utility = record.value("utility_type") or "Utility"
    org_name = record.value("external_org")
    return f"{utility} — {org_name}" if org_name else utility


def available(value: Any) -> bool:
    """Whether a reading's field is a value at all."""
    return not isinstance(value, NotAvailable)


@dataclass(frozen=True)
class ConstraintReading:
    """One Constraint as the log, the report and the alert engine read it.

    ``subject`` is the row or accepted record the reading came from, retained so
    a renderer that needs the population's own provenance (an accepted field's
    deciding revision, a legacy row's identity) has it without a second lookup.
    ``mode`` names which adapter produced this, so a section can declare what it
    cannot populate instead of publishing an emptiness.
    """

    id: Any
    ref_code: str
    mode: str
    subject: Any

    # Values the accepted record and the legacy row both carry.
    org_name: str | None = None
    external_org_id: int | None = None
    resolution_strategy: str | None = None
    source_ref: str | None = None
    title: str | None = None
    dep_type: str | None = None
    location_desc: str | None = None
    station_from: str | None = None
    station_to: str | None = None
    external_contact: str | None = None
    notes: str | None = None
    # The date the alert engine measures lateness against: the projected
    # External Party statement date for a legacy row, the accepted Promised For
    # value for an adopted record. The *published* statement projection stays
    # separate and stays absent for an adopted record (ADR-0092).
    committed_date: date | None = None
    need_date: date | None = None

    # Values only one population has.
    evidence_required: str | None | NotAvailable = NO_LEGACY_FIELD
    internal_owner: str | None | NotAvailable = NO_COORDINATION_DECISION
    next_action: str | None | NotAvailable = NO_COORDINATION_DECISION
    action_due_date: date | None | NotAvailable = NO_COORDINATION_DECISION
    milestone_id: int | None | NotAvailable = NO_KEY_DATE_LINK
    # A deferred column on the legacy row: every query that gathers readings
    # undefers it, because touching it per row otherwise costs one statement per
    # record and the Evaluation seam is bounded (``tests/test_exceptions.py``).
    milestone_registration_id: int | None | NotAvailable = NO_KEY_DATE_LINK
    dismissed_at: datetime | None | NotAvailable = NO_DISMISSAL

    # The liveness, readiness, support and contradiction facts the rules need.
    is_ready: bool | NotAvailable = NO_DOCUMENTATION_REVIEW
    readiness_lapsed: bool | NotAvailable = NO_DOCUMENTATION_REVIEW
    has_closure: bool = False
    has_supporting_documentation: bool | NotAvailable = NO_LEGACY_FIELD
    last_evidenced_at: date | None | NotAvailable = NO_LEGACY_EVIDENCE_DATES
    contradicted_fields: tuple[str, ...] | NotAvailable = NO_LEGACY_DISPUTES
    superseded_scopes: tuple[Any, ...] | NotAvailable = NO_LEGACY_SUPPORT_REGISTRY
    # The ported SUPERSEDED_CITATION inputs: the accepted value still depends on
    # a replaced Document Revision and nothing current stands beside it
    # (ADR-0016's predicate, ADR-0090's carrier).
    depends_on_superseded_support: bool | NotAvailable = NO_LEGACY_SUPPORT_REGISTRY
    superseded_support_replaced_on: date | None | NotAvailable = (
        NO_LEGACY_SUPPORT_REGISTRY
    )
    # What the record stands on, counted, for the log's Backing column.
    source_count: int = 0
    checked_source_count: int = 0

    @property
    def critical(self) -> bool:
        """Read from the strategy, never stored beside it (ADR-0009)."""
        return is_critical(self.resolution_strategy)

    @property
    def live(self) -> bool:
        """Neither proven Ready, nor lapsed out of currency, nor closed.

        Where readiness is not available at all the record is live: an accepted
        record has no documentation review outcome to be ready by, and treating
        the missing outcome as "ready" would silence every date check on it.
        """
        ready = self.is_ready is True
        lapsed = self.readiness_lapsed is True
        return not ready and not lapsed and not self.has_closure

    def with_committed_date(self, committed_date: date | None) -> ConstraintReading:
        """The same reading against a caller's stated date surface."""
        return replace(self, committed_date=committed_date)


def legacy_constraint_reading(
    dependency,
    *,
    support,
    contradicted_fields: tuple[str, ...] = (),
    has_closure: bool = False,
    committed_date: date | None = None,
    org_name: str | None = None,
    source_count: int = 0,
) -> ConstraintReading:
    """Read one legacy `dependencies` row as a Constraint reading.

    ``dependency`` and ``support`` are taken duck-typed: this module holds no
    import of a frozen table's class, and the caller has already resolved both.
    """
    return ConstraintReading(
        id=dependency.id,
        ref_code=dependency.ref_code,
        mode=LEGACY,
        subject=dependency,
        org_name=org_name,
        external_org_id=dependency.external_org_id,
        resolution_strategy=dependency.resolution_strategy,
        source_ref=dependency.source_ref,
        title=dependency.title,
        dep_type=dependency.dep_type,
        location_desc=dependency.location_desc,
        station_from=dependency.station_from,
        station_to=dependency.station_to,
        external_contact=dependency.external_contact,
        notes=dependency.notes,
        committed_date=committed_date,
        need_date=dependency.need_date,
        evidence_required=dependency.evidence_required,
        internal_owner=dependency.internal_owner,
        next_action=dependency.next_action,
        action_due_date=dependency.action_due_date,
        milestone_id=dependency.milestone_id,
        milestone_registration_id=dependency.milestone_registration_id,
        dismissed_at=dependency.dismissed_at,
        is_ready=support.is_ready,
        readiness_lapsed=bool(support.readiness and not support.current_readiness),
        has_closure=has_closure,
        has_supporting_documentation=bool(support.verified_evidence_count),
        last_evidenced_at=support.last_evidenced_at,
        contradicted_fields=tuple(contradicted_fields),
        superseded_scopes=tuple(support.superseded_scopes),
        source_count=source_count,
        checked_source_count=support.verified_evidence_count,
    )


def accepted_constraint_reading(
    record,
    *,
    support_in_use: Mapping[int, Any],
) -> ConstraintReading:
    """Read one accepted Project Record subject as a Constraint reading.

    ``support_in_use`` is ``accepted_field_reading.accepted_support_in_use``'s
    result for the project: the one Supporting Documentation in Use resolver
    (ADR-0017), keyed by Fact. A value with no entry stands on nothing, which is
    ADR-0090's re-based ``MISSING_EVIDENCE`` — never the Source Passage Check,
    because a passage being where it was cited says nothing about whether it
    supports the value beside it (ADR-0082).

    Every field this record cannot supply keeps its declared marker. Nothing is
    hard-coded to ``None`` and nothing is hard-coded to ``False``: the four rules
    ADR-0090 retires for the accepted record are retired because the facts they
    read are declared unavailable, not because a rule was special-cased.
    """
    standing = [support_in_use.get(field.fact_id) for field in record.fields.values()]
    held = [item for item in standing if item is not None]
    replaced = [
        item.replaced_on
        for item in held
        if item.depends_on_superseded and item.replaced_on is not None
    ]
    org_name = record.value("external_org")
    return ConstraintReading(
        id=record.id,
        ref_code=record.ref_code,
        mode=ACCEPTED_RECORD,
        subject=record,
        org_name=org_name,
        external_org_id=record.external_org_id,
        resolution_strategy=record.resolution_strategy,
        source_ref=record.value("utility_id"),
        title=accepted_record_title(record),
        dep_type="utility_relocation",
        location_desc=" / ".join(
            value
            for name in ("alignment", "location_start", "location_end")
            if (value := record.value(name))
        )
        or None,
        station_from=record.station_from,
        station_to=record.station_to,
        external_contact=record.value("external_org_contact"),
        notes=record.value("notes"),
        committed_date=record.value("committed_date"),
        need_date=record.need_date,
        has_closure=record.is_closed,
        has_supporting_documentation=bool(held),
        depends_on_superseded_support=any(
            item.depends_on_superseded for item in held
        ),
        superseded_support_replaced_on=min(replaced) if replaced else None,
        source_count=len(record.source_passages),
        checked_source_count=len(record.checked_source_passages),
    )
