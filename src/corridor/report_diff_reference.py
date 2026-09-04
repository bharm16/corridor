"""The weekly report diff's baseline, rebuilt from the revision it names.

``changes.diff_since_last`` compares this week's reading against the previous
Report Run.  Until #602 the only thing it could compare against was that run's
retained payload, with nothing beside it saying *which* accepted Project Record
revision the record-owned parts of it were.  A record value with no reference
can only ever be checked against itself, so drift between it and the record was
unobservable by construction.  #602 bound each run to ``revision_id``; this
module is the other half of #598's criterion — the diff reading that reference
for the fields the record owns, and the measurement proving the two say the
same thing.

**What a revision reference can rebuild, and what it cannot.**  A Report
Reading's retained entry per dependency holds six things, and they are not one
kind of value:

- ``resolution_strategy`` and ``need_date`` are project record values.  The
  spine carries both as typed structured-cell Facts
  (``fact_types.STRUCTURED_CELL_FACT_TYPES``), effective at a revision, so
  ``record_projection.read_project_record_as_of_revision`` rebuilds them and
  the two can be compared value for value.
- ``id`` is a Ledger identity, not state.  It is resolved from the Ledger by
  ``ref_code`` — ``(project_id, ref_code)`` is unique — so the copy is not
  needed to know which record a change is about.
- ``published_promised_for`` had the same name as a Fact on both sides until
  #633 and was **not the same quantity**.  The spine's ``committed_date`` Fact
  is the source cell; the retained reading holds
  ``evaluation.committed_dates``, the date projected from the external party's
  current statement, which is ``None`` where no statement has been recorded no
  matter what the cell says.  Comparing them, or letting one stand in for the
  other, would change the diff because its inputs changed shape — the single
  failure #598 names.  So it keeps its own name, and is excluded here rather
  than quietly included.
- ``documentation_requirement_met`` and ``constraint_alerts`` are not record
  state at all.  They are a *reading* of the record under one ruleset version,
  one threshold configuration and one date, computed at publication (ADR-0002
  keeps the documentation requirement derived, ADR-0044 keeps the evaluation
  derived).  No revision carries them, because a revision is what the record
  says and these are what a rule said about it on a day.  With
  ``published_promised_for`` and the population, this is why the retained
  payload is the occurrence's own evidence rather than a cache of the record
  (ADR-0092) — a property of the values, not a gap in this pass.

**Why the reference does not overrule a disagreeing reading.**  A project in
``legacy`` operating mode (``operating_mode``) still has its accepted values in
the legacy relations, and two released legacy commands move one without writing
any Fact: ``adjudicate.set_resolution_strategy`` writes the Dependency column,
and a Committed Date Change is projected from ``dependency_events`` rather than
from the source cell the spine holds.  For such a project the spine Fact is the
*source's* reading and the legacy column is the accepted value, so preferring
the reference there would report a change that did not happen — precisely the
failure #598 asks this pass not to introduce.  The disagreement runs the other
way too: ``load_project`` writes the Ledger column and projects the same cell
into the record in one pass, and when only the record moves the reference is
ahead of the reading the report actually published, so taking it would report a
change in a week the report never said one.  So the reference supplies a value
the copy is silent about, and a disagreement is recorded as drift on the
reading and surfaced on the ``Diff`` instead of being resolved silently in
either direction.  ``prove_diff_equivalence`` measures how much of the baseline
the reference reproduces and lists every drift with the run it was found in.

**The one deliberate correction.**  A snapshot written before #96 recorded no
``id`` for its entries, and four such runs are stored; a change derived from
one of them cites no record and cannot find the dismissal decision that took
the record off the working list.  Resolving the identity from the Ledger fixes
that: such a change now drills through, and a record that was *dismissed as
junk* is reported as dismissed rather than as closed — which ADR-0032 names as
the one mistake this report cannot afford.  It is a change in what the diff
says about those runs, it is deliberate, and it is the only one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.record_projection import read_project_record_as_of_revision
from corridor.models import Dependency, ReportRun
from corridor.report_reading import read_entries


# The record fields a revision reference answers in the same shape the retained
# copy stores them, with the value class each Fact is carried by.
REFERENCE_FIELDS: Mapping[str, str] = {
    "resolution_strategy": "text",
    "need_date": "date",
}

# What the retained reading is the only witness of, and why — the occurrence's
# own values under ADR-0092, not a cache of anything a revision holds.
# ``documentation_requirement_met`` (ADR-0002) and ``constraint_alerts``
# (ADR-0044) are a reading of the record under a ruleset, thresholds and a
# date, none of which a revision records. ``published_promised_for`` is the
# statement projection of a record value, which is a different quantity from
# the record's own ``committed_date`` Fact and now carries a different name.
UNREBUILDABLE_FIELDS: Mapping[str, str] = {
    "documentation_requirement_met": (
        "the documentation requirement is computed per reading, never "
        "stored (ADR-0002)"
    ),
    "constraint_alerts": (
        "the evaluated Constraint Alerts of one ruleset, threshold "
        "configuration and date (ADR-0044)"
    ),
    "published_promised_for": (
        "the reading holds the statement-projected date, not the record's "
        "own committed_date Fact"
    ),
}


@dataclass(frozen=True)
class ReferenceBaseline:
    """The record state the spine carries at one revision, by ``ref_code``."""

    revision_id: int | None
    dependencies: Mapping[str, Mapping[str, Any]]

    def covers(self, ref_code: str, field: str) -> bool:
        """Whether the reference can answer this field for this record."""

        return field in self.dependencies.get(ref_code, {})

    @property
    def covered_field_count(self) -> int:
        """Record fields answered — the resolved identity is not one of them."""

        return sum(
            1
            for fields in self.dependencies.values()
            for name in fields
            if name in REFERENCE_FIELDS
        )


@dataclass(frozen=True)
class BaselineDrift:
    """One field where the retained reading and the revision reference differ."""

    ref_code: str
    field: str
    retained: Any
    referenced: Any

    def sentence(self) -> str:
        return (
            f"{self.ref_code}.{self.field}: the retained reading says "
            f"{self.retained!r} and the revision says {self.referenced!r}"
        )


@dataclass(frozen=True)
class BaselineReading:
    """The ``before`` map a diff compares against, and where each part came from."""

    dependencies: Mapping[str, Mapping[str, Any]]
    revision_id: int | None
    covered_fields: int
    compared_fields: int
    drift: tuple[BaselineDrift, ...]
    identities_resolved: int

    @property
    def source(self) -> str:
        """A short name for what the baseline was built from."""

        if self.revision_id is None:
            return "retained_copy_no_reference"
        if self.drift:
            return "revision_reference_with_drift"
        return "revision_reference"


def reference_baseline(
    session: Session, project_id: int, *, revision_id: int | None
) -> ReferenceBaseline:
    """The three record fields the spine carries, as of ``revision_id``.

    Keyed by ``ref_code`` so it lines up with what the diff compares. A Fact
    reaches a Ledger record through its Candidate's ``merged_into``; a Fact
    with no such Candidate belongs to no Ledger row and is left out rather than
    guessed at.
    """

    if revision_id is None:
        return ReferenceBaseline(revision_id=None, dependencies={})
    ref_codes = dict(
        session.execute(
            select(Dependency.id, Dependency.ref_code).where(
                Dependency.project_id == project_id
            )
        ).all()
    )
    dependencies: dict[str, dict[str, Any]] = {}
    for value in read_project_record_as_of_revision(session, project_id, revision_id):
        if value.dependency_id is None:
            continue
        ref_code = ref_codes.get(value.dependency_id)
        if ref_code is None:
            continue
        value_class = REFERENCE_FIELDS.get(value.fact_type)
        if value_class is None:
            continue
        entry = dependencies.setdefault(ref_code, {"id": value.dependency_id})
        if value_class == "date":
            entry[value.fact_type] = (
                value.date_value.isoformat() if value.date_value else None
            )
        else:
            entry[value.fact_type] = value.text_value
    return ReferenceBaseline(revision_id=revision_id, dependencies=dependencies)


def ledger_identities(session: Session, project_id: int) -> Mapping[str, int]:
    """``ref_code`` to Ledger identity, the correspondence the copy duplicated."""

    return {
        ref_code: dependency_id
        for dependency_id, ref_code in session.execute(
            select(Dependency.id, Dependency.ref_code).where(
                Dependency.project_id == project_id
            )
        ).all()
    }


def baseline_for_run(session: Session, run: ReportRun) -> BaselineReading:
    """The ``before`` map for ``run``, built from the revision it names.

    The population and the reading-only fields come from the retained Report
    Reading payload, because neither is revision state: the population a report
    covered is not derivable from a record revision, and the documentation
    requirement and the Constraint Alerts are a reading rather than a value
    (ADR-0092). Everything else is the reference's, with a disagreement
    recorded rather than silently resolved.

    The payload is read through ``report_reading.read_entries``, so a version 1
    payload written under the old key names is compared under the governed ones
    without being rewritten.
    """

    retained = read_entries(run.snapshot_json)
    reference = reference_baseline(
        session, run.project_id, revision_id=run.revision_id
    )
    identities = ledger_identities(session, run.project_id)
    drift: list[BaselineDrift] = []
    compared = 0
    identities_resolved = 0
    dependencies: dict[str, dict[str, Any]] = {}
    for ref_code, published in retained.items():
        entry = dict(published)
        # Identity is a correspondence, not state: the Ledger answers it, and
        # a run written before #96 stored none at all.
        resolved = identities.get(ref_code)
        if resolved is not None:
            if entry.get("id") is None:
                identities_resolved += 1
            entry["id"] = resolved
        referenced = reference.dependencies.get(ref_code, {})
        for field in REFERENCE_FIELDS:
            if field not in referenced:
                continue
            if field not in entry:
                entry[field] = referenced[field]
                continue
            compared += 1
            if entry[field] != referenced[field]:
                drift.append(
                    BaselineDrift(
                        ref_code=ref_code,
                        field=field,
                        retained=entry[field],
                        referenced=referenced[field],
                    )
                )
        dependencies[ref_code] = entry
    return BaselineReading(
        dependencies=dependencies,
        revision_id=run.revision_id,
        covered_fields=reference.covered_field_count,
        compared_fields=compared,
        drift=tuple(drift),
        identities_resolved=identities_resolved,
    )


@dataclass(frozen=True)
class RunEquivalence:
    """What the reference reproduced of one retained run's baseline."""

    run_id: int
    revision_id: int | None
    rows: int
    compared_fields: int
    reference_only_fields: int
    drift: tuple[BaselineDrift, ...]
    identities_resolved: int

    @property
    def agreed(self) -> bool:
        return not self.drift


@dataclass(frozen=True)
class DiffEquivalenceProof:
    """The measured answer to #603: does the reference say what was published?"""

    project_id: int
    runs: tuple[RunEquivalence, ...]

    @property
    def runs_compared(self) -> int:
        return len(self.runs)

    @property
    def runs_with_a_reference(self) -> int:
        return sum(1 for run in self.runs if run.revision_id is not None)

    @property
    def rows_compared(self) -> int:
        return sum(run.rows for run in self.runs)

    @property
    def fields_compared(self) -> int:
        return sum(run.compared_fields for run in self.runs)

    @property
    def drift(self) -> tuple[BaselineDrift, ...]:
        return tuple(item for run in self.runs for item in run.drift)

    @property
    def passed(self) -> bool:
        """Every field the reference and the copy both answered, agreed.

        A proof over no compared field at all proves nothing, and says so
        rather than passing vacuously.
        """

        return self.fields_compared > 0 and not self.drift

    def report(self) -> str:
        lines = [
            f"project {self.project_id}: {self.runs_compared} retained runs, "
            f"{self.runs_with_a_reference} bound to a revision, "
            f"{self.rows_compared} record rows, "
            f"{self.fields_compared} fields compared",
        ]
        lines.extend(item.sentence() for item in self.drift)
        if not self.drift:
            lines.append("no disagreement between the reading and the reference")
        return "\n".join(lines)


def prove_diff_equivalence(
    session: Session, project_id: int
) -> DiffEquivalenceProof:
    """Rebuild every retained run's baseline from its revision and compare.

    The corpus is the project's own retained Report Runs — the exact rows the
    diff would read — so the proof is over matched pairs rather than over
    re-derived state that never published anything.  It measures the record
    fields a revision owns; it never claims to reproduce the occurrence's own
    values, which ADR-0092 places outside a revision's reach.
    """

    runs = session.scalars(
        select(ReportRun)
        .where(ReportRun.project_id == project_id)
        .order_by(ReportRun.id)
    ).all()
    measured = []
    for run in runs:
        reading = baseline_for_run(session, run)
        retained = read_entries(run.snapshot_json)
        reference_only = sum(
            1
            for ref_code, fields in reference_baseline(
                session, run.project_id, revision_id=run.revision_id
            ).dependencies.items()
            for name in fields
            if name in REFERENCE_FIELDS and name not in retained.get(ref_code, {})
        )
        measured.append(
            RunEquivalence(
                run_id=run.id,
                revision_id=run.revision_id,
                rows=len(retained),
                compared_fields=reading.compared_fields,
                reference_only_fields=reference_only,
                drift=reading.drift,
                identities_resolved=reading.identities_resolved,
            )
        )
    return DiffEquivalenceProof(project_id=project_id, runs=tuple(measured))
