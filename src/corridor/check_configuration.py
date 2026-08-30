"""The effective per-project configuration of the existing check thresholds.

The exception engine's thresholds (``STALE_DAYS``, ``DUE_SOON_DAYS``,
``ACTION_DUE_SOON_DAYS``) were fixed module constants, varied only by a test
passing a ``Thresholds`` into ``evaluate*``.  A project that coordinates on a
different cadence had no supported way to declare its own horizons, and a
settings form disconnected from the readers would let an operator change a
number the actual Evaluation ignores.

This module owns exactly one thing: the *effective* ``Thresholds`` for a
project, read from an append-only history of declared configurations.  The
exception engine resolves its thresholds here when a caller states none, so
the product, the command callers, and the direct readers all compute one
project's Evaluation against the same declared inputs.  It only parameterizes
the existing rules — it adds no new rule meaning, urgency policy, schedule, or
model behavior.

Boundaries this module keeps:

- A proposed change is validated for supported shape, unit, and range and is
  refused whole when invalid or incomplete — never silently filled with a
  default or half-applied.
- A save is a new retained identity attributed to the deployment identity
  (never a form-supplied author; production auth is #331).  The earlier
  configuration and the reports published under it stay readable.
- A preview evaluates the project under the proposed inputs without writing a
  configuration row, a Report Run, or moving a comparison baseline.
- No declaration means the supported module defaults, unchanged.  A historical
  Evaluation that never recorded its thresholds stays explicitly *unknown*
  here rather than being backfilled from the current default (ADR-0044: the
  reading is derived, not a stored status that drifts from its evidence).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.exceptions import (
    ACTION_DUE_SOON_DAYS,
    DUE_SOON_DAYS,
    RULESET_VERSION,
    STALE_DAYS,
    RuleFacet,
    Thresholds,
    evaluate_project,
)
from corridor.models import Project, ProjectCheckConfiguration
from corridor.principals import HumanPrincipal, require_human_principal


class InvalidCheckConfiguration(ValueError):
    """A proposed configuration is not a supported, complete set of thresholds."""


@dataclass(frozen=True)
class ThresholdSpec:
    """One supported, configurable threshold: its meaning, unit, and range.

    ``rules`` names the existing checks this number parameterizes, so the
    operations view can say what a change would touch without implying a new
    rule.  ``minimum``/``maximum`` are the supported range a proposal must fall
    within; the values are whole days.
    """

    key: str
    label: str
    unit: str
    default: int
    minimum: int
    maximum: int
    rules: tuple[str, ...]


# The full set of thresholds the exception engine already reads (the three
# fields of ``Thresholds``).  This is a configuration surface over existing
# meanings, so the catalog names only these — no new key adds a new rule.
SUPPORTED_THRESHOLDS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec(
        key="stale_days",
        label="No document in",
        unit="days",
        default=STALE_DAYS,
        minimum=1,
        maximum=3650,
        rules=("STALE",),
    ),
    ThresholdSpec(
        key="due_soon_days",
        label="Need Date within",
        unit="days",
        default=DUE_SOON_DAYS,
        minimum=1,
        maximum=3650,
        rules=("DUE_SOON",),
    ),
    ThresholdSpec(
        key="action_due_soon_days",
        label="Next Action due within",
        unit="days",
        default=ACTION_DUE_SOON_DAYS,
        minimum=1,
        maximum=3650,
        rules=("ACTION_DUE_SOON",),
    ),
)

_SPEC_BY_KEY = {spec.key: spec for spec in SUPPORTED_THRESHOLDS}


def _coerce_days(spec: ThresholdSpec, raw: object) -> int:
    """One supported threshold value: a whole number of days within range.

    Strings arrive from the HTTP form; a blank one is *incomplete*, not zero.
    A boolean or a fractional value is not a day count and is refused rather
    than coerced.
    """
    if isinstance(raw, bool):
        raise InvalidCheckConfiguration(
            f"{spec.label} must be a whole number of {spec.unit}"
        )
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise InvalidCheckConfiguration(
                f"{spec.label} is required; a blank value is not applied"
            )
        try:
            value = int(text)
        except ValueError as exc:
            raise InvalidCheckConfiguration(
                f"{spec.label} must be a whole number of {spec.unit}"
            ) from exc
    else:
        raise InvalidCheckConfiguration(
            f"{spec.label} must be a whole number of {spec.unit}"
        )
    if value < spec.minimum or value > spec.maximum:
        raise InvalidCheckConfiguration(
            f"{spec.label} must be between {spec.minimum} and {spec.maximum} "
            f"{spec.unit}"
        )
    return value


def validate_proposed_thresholds(proposed: Mapping[str, object]) -> Thresholds:
    """Turn a proposed mapping into ``Thresholds`` or refuse it whole.

    Every supported threshold must be present and in range; an unknown key or
    a missing one is refused rather than ignored or defaulted.  This is the one
    gate a save and a preview share, so a preview can never accept a proposal a
    save would reject.
    """
    keys = set(proposed)
    supported = set(_SPEC_BY_KEY)
    unknown = keys - supported
    if unknown:
        raise InvalidCheckConfiguration(
            "unsupported threshold(s): " + ", ".join(sorted(unknown))
        )
    missing = supported - keys
    if missing:
        raise InvalidCheckConfiguration(
            "incomplete configuration; missing " + ", ".join(sorted(missing))
        )
    values = {
        spec.key: _coerce_days(spec, proposed[spec.key])
        for spec in SUPPORTED_THRESHOLDS
    }
    return Thresholds(**values)


@dataclass(frozen=True)
class EffectiveConfiguration:
    """The thresholds a project's readings use now, and their identity.

    ``configuration_id`` is ``None`` when the project has declared nothing and
    is running on the supported defaults; a value names the retained
    declaration in force.  ``thresholds`` are the exact numbers ``evaluate*``
    uses either way, so a reader never has to guess whether a shown value is a
    declaration or a default.
    """

    project_id: int
    thresholds: Thresholds
    ruleset_version: str
    configuration_id: int | None
    declared_by: str | None
    declared_at: datetime | None

    @property
    def is_declared(self) -> bool:
        return self.configuration_id is not None


def _latest_configuration(
    session: Session, project_id: int
) -> ProjectCheckConfiguration | None:
    """The newest declared configuration for the project, or None.

    Newest by id: the table is append-only, so a higher id is a later
    declaration without depending on timestamp resolution.

    A reader resolves its thresholds through here, so it must not crash against
    a database that predates this feature's table. The parallel development
    stack keeps the shared database at the previous migration head until
    integration, and a rolling deploy can serve this code briefly before the
    migration runs; in both, an absent table means no project has declared
    anything, so the effective configuration is the supported default — the
    same answer an empty table gives. ``to_regclass`` returns NULL for an absent
    relation without raising, so this probe never aborts the caller's
    transaction. The write paths still insert into the table and fail loudly, so
    a genuinely missing migration cannot pass silently.
    """
    if (
        session.scalar(select(func.to_regclass("project_check_configurations")))
        is None
    ):
        return None
    return session.scalars(
        select(ProjectCheckConfiguration)
        .where(ProjectCheckConfiguration.project_id == project_id)
        .order_by(ProjectCheckConfiguration.id.desc())
        .limit(1)
    ).first()


def _row_thresholds(row: ProjectCheckConfiguration) -> Thresholds:
    return Thresholds(
        stale_days=row.stale_days,
        due_soon_days=row.due_soon_days,
        action_due_soon_days=row.action_due_soon_days,
    )


def effective_thresholds(session: Session, project_id: int) -> Thresholds:
    """The ``Thresholds`` a new reading of this project must use.

    The exception engine calls this when a caller states no thresholds, so the
    declared configuration reaches every reader — the product, the command
    callers, and the direct callers — through one seam.  No declaration returns
    the supported defaults, leaving existing behavior unchanged.
    """
    row = _latest_configuration(session, project_id)
    return _row_thresholds(row) if row is not None else Thresholds()


def effective_configuration(
    session: Session, project_id: int
) -> EffectiveConfiguration:
    """The effective thresholds together with their declaration identity."""
    row = _latest_configuration(session, project_id)
    if row is None:
        return EffectiveConfiguration(
            project_id=project_id,
            thresholds=Thresholds(),
            ruleset_version=RULESET_VERSION,
            configuration_id=None,
            declared_by=None,
            declared_at=None,
        )
    return EffectiveConfiguration(
        project_id=project_id,
        thresholds=_row_thresholds(row),
        ruleset_version=row.ruleset_version,
        configuration_id=row.id,
        declared_by=row.created_by,
        declared_at=row.created_at,
    )


def configuration_history(
    session: Session, project_id: int
) -> tuple[ProjectCheckConfiguration, ...]:
    """Every declared configuration for the project, newest first.

    The append-only history: the effective row is the first, and the ones
    beneath it are the earlier declarations that stay readable.
    """
    return tuple(
        session.scalars(
            select(ProjectCheckConfiguration)
            .where(ProjectCheckConfiguration.project_id == project_id)
            .order_by(ProjectCheckConfiguration.id.desc())
        ).all()
    )


def save_configuration(
    session: Session,
    project_id: int,
    proposed: Mapping[str, object],
    *,
    principal: HumanPrincipal,
) -> ProjectCheckConfiguration:
    """Validate a proposed change and append a new retained configuration.

    Refuses an invalid or incomplete proposal before any write, and attributes
    the declaration to the deployment identity — never a form-supplied author.
    The insert is a new identity; the append-only trigger keeps the earlier
    rows and the reports published under them intact.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    # Fail before the write if identity did not cross the typed seam.
    require_human_principal(principal)
    thresholds = validate_proposed_thresholds(proposed)
    row = ProjectCheckConfiguration(
        project_id=project_id,
        ruleset_version=RULESET_VERSION,
        stale_days=thresholds.stale_days,
        due_soon_days=thresholds.due_soon_days,
        action_due_soon_days=thresholds.action_due_soon_days,
        created_by=principal.subject,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class ConfigurationPreview:
    """One project's reading under a proposed configuration, written nowhere.

    It identifies the reading (the project, the evaluated date, the ruleset)
    and the proposed inputs, and reports the facet counts the proposal would
    produce beside the effective configuration — enough to judge a change
    without activating it.
    """

    project_id: int
    evaluated_on: date
    ruleset_version: str
    proposed_thresholds: Thresholds
    effective: EffectiveConfiguration
    facets: tuple[RuleFacet, ...]
    affected_constraint_count: int


def preview_configuration(
    session: Session,
    project_id: int,
    proposed: Mapping[str, object],
    *,
    today: date | None = None,
) -> ConfigurationPreview:
    """Evaluate the project under a proposed configuration without writing.

    Passes the proposed thresholds explicitly, so this is a pure read: no
    configuration row is appended, no Report Run is retained, and the
    comparison baseline does not move.  The proposal is validated first, so a
    preview never shows a reading a save would refuse.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    proposed_thresholds = validate_proposed_thresholds(proposed)
    effective = effective_configuration(session, project_id)
    evaluation = evaluate_project(
        session, project_id, today=today, thresholds=proposed_thresholds
    )
    return ConfigurationPreview(
        project_id=project_id,
        evaluated_on=evaluation.today,
        ruleset_version=evaluation.ruleset_version,
        proposed_thresholds=proposed_thresholds,
        effective=effective,
        facets=tuple(evaluation.facets()),
        affected_constraint_count=len(
            {exception.dependency_id for exception in evaluation.found}
        ),
    )
