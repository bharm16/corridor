"""Collect the pilot's human observations where the work happens (#846).

`measurement_collection` validates and emits explicit observations, and
`pilot_measurement` reads them, but nothing in the product ever called the
collector. The pilot contract
(`docs/pilot-success-criteria.md`) asks the coordinator to mark each
interrupting packet as genuinely required **at the moment of triage** and to
log the minutes spent rebuilding context outside Corridor for it then; a
judgment reconstructed a week later is a different measurement. This module is
the seam that turns one triage act into those two observations, and the import
path for the work that is genuinely performed outside Corridor.

Three properties hold it in place, and each is the answer to a way this could
invalidate the thing it measures.

**It cannot touch the denominator.** The interruption denominator is built by
`pilot_measurement._period_report` from `packet_surfacing` presentation
records, and nothing here emits, suppresses or filters one. An interrupting
packet nobody judged stays in the denominator as unjudged, which is the whole
reason the collection surface exists rather than a feedback form whose replies
*are* the population. The import path refuses every presentation and decision
family for the same reason: a denominator is a record of what Corridor did,
never a file somebody supplied.

**It observes the occurrence that was displayed, not the one that survives.**
A judgment is bound to `TriageOccurrence` -- the item key, the cutoff of the
reading that presented it, the consequence level shown and the rule that
derived it -- carried from the rendered page. The observation is dated at that
cutoff rather than at the submission instant, so it lands in the period the
packet was presented in and is identical however many times the same rendered
form is submitted. Nothing here re-reads the current reading to fill those in;
that would record a judgment of whatever the packet became.

**A measurement failure is never a project outcome.** `collect_triage_observations`
is called after the decision has committed and answers with what it collected;
it raises nothing, because an analytics sink that is down must not turn a saved
Project Record decision into an error page. No option is preselected and an
empty minutes box records nothing, so an unanswered question stays unanswered
instead of becoming a default "useful" and a zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
from typing import Any

from corridor import digests
from corridor.analytics import (
    AnalyticsBinding,
    AnalyticsEvent,
    EventFamily,
)
from corridor.measurement_collection import collect_observation, validate_observation

logger = logging.getLogger(__name__)


# The pinned parameter that turns the in-product controls on. The pilot's
# cohort table already records "the complete flag state of the pilot
# environment, including every flag left off", and every event binds that
# state, so the declaration that pins the cohort is the same one that offers
# the controls. A deployment that is not the measured pilot renders nothing.
PILOT_MEASUREMENT_FLAG = "pilot_measurement_collection"

# The contract's own two words for the interrupting-packet sample: a packet at
# "Must handle before this issue" either genuinely needed handling before that
# issue or it did not. The first option is neither of them, because a default
# answer is an answer nobody gave.
NOT_JUDGED = ""
NEEDED_HANDLING = "needed_handling"
DID_NOT_NEED_HANDLING = "did_not_need_handling"
JUDGMENT_CHOICES: tuple[tuple[str, str], ...] = (
    (NOT_JUDGED, "Not answered"),
    (NEEDED_HANDLING, "Yes — it needed handling before this issue"),
    (DID_NOT_NEED_HANDLING, "No — it did not need handling before this issue"),
)
_JUDGMENTS = {NEEDED_HANDLING: True, DID_NOT_NEED_HANDLING: False}

# `measurement_collection`'s own vocabulary, named here so the two observations
# this module produces are spelled once.
PACKET_USEFULNESS = "packet_usefulness"
MANUAL_RECONSTRUCTION = "manual_reconstruction"

IMPORT_VERSION = "pilot-observation-import-v1"

# What an import may carry. `validate_observation` already refuses everything
# else, and this set exists so the refusal names the family rather than
# arriving as a generic validation message.
IMPORTABLE_FAMILIES = frozenset(
    {
        EventFamily.WORK_OBSERVATION,
        EventFamily.ARTIFACT_REPAIR,
        EventFamily.MEASUREMENT_SAMPLE,
        EventFamily.PROVIDER_USAGE,
    }
)

# The two judgments the product now collects at triage. The import is for work
# performed outside Corridor; accepting these would let a file supply, after
# the fact, the contemporaneous judgments the criterion is defined by.
CONTEMPORANEOUS_SAMPLE_KINDS = frozenset({PACKET_USEFULNESS, "child_usefulness"})


def collection_is_pinned(binding: AnalyticsBinding) -> bool:
    """Whether this deployment's pinned configuration collects pilot observations."""

    return PILOT_MEASUREMENT_FLAG in binding.enabled_feature_flags


@dataclass(frozen=True, slots=True)
class TriageOccurrence:
    """The exact packet presentation a judgment answers, and its versions.

    Every field is carried from the rendered page. `cutoff` is the reading
    instant the `packet_surfacing` record for this presentation also carries,
    so the observation and the presentation it observes name the same moment
    without either being derived from the other.
    """

    item_key: str
    cutoff: str
    consequence_level: str
    consequence_rule_version: str

    @property
    def identity(self) -> str:
        """The evidence reference: which presentation, at which version."""

        return (
            f"packet-presentation:{self.item_key}@{self.cutoff}"
            f":{self.consequence_level}:{self.consequence_rule_version}"
        )

    @property
    def presented_at(self) -> datetime:
        """The instant the packet was put in front of the coordinator."""

        moment = datetime.fromisoformat(self.cutoff)
        if moment.tzinfo is None:
            raise ValueError("a presented occurrence carries a zoned instant")
        return moment

    def as_payload(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "presented_cutoff": self.cutoff,
            "presented_consequence_level": self.consequence_level,
            "consequence_rule_version": self.consequence_rule_version,
        }


def judgment_from_form(value: str) -> bool | None:
    """The submitted judgment, or nothing at all when none was chosen."""

    return _JUDGMENTS.get(value.strip())


def minutes_from_form(value: str) -> float | None:
    """Measured minutes, or nothing. An unmeasured box is never a zero."""

    text = value.strip()
    if not text:
        return None
    try:
        minutes = float(text)
    except ValueError:
        return None
    if not math.isfinite(minutes) or minutes < 0:
        return None
    # A receipt reads back the number that was typed: 4, not 4.0.
    return int(minutes) if minutes.is_integer() else minutes


def triage_observations(
    *,
    project_id: int,
    actor: str,
    occurrence: TriageOccurrence,
    binding: AnalyticsBinding,
    judgment: bool | None = None,
    minutes: float | None = None,
) -> tuple[AnalyticsEvent, ...]:
    """Build the observations one triage act produced, validated, in order.

    Each event's identity is a digest of its own complete content, so the same
    rendered form submitted twice produces the same event rather than a second
    judgment and a second helping of minutes.
    """

    if not occurrence.item_key or not occurrence.cutoff:
        raise ValueError("a triage observation names the presentation it answers")
    if not actor:
        raise ValueError("a triage observation names the person who made it")
    presented_at = occurrence.presented_at
    events: list[AnalyticsEvent] = []
    if judgment is not None:
        events.append(
            _observation(
                EventFamily.MEASUREMENT_SAMPLE,
                binding=binding,
                occurred_at=presented_at,
                payload={
                    "project_id": project_id,
                    "actor": actor,
                    "sample_kind": PACKET_USEFULNESS,
                    "necessary": judgment,
                    "evidence_reference": occurrence.identity,
                    # The case is this presentation answered this way. A later
                    # answer to the same presentation is a separate case that
                    # the reader orders after it rather than a relabelling of
                    # it, so the first contemporaneous judgment is the one the
                    # numerator reads.
                    "case_identity": f"{occurrence.identity}#{judgment}",
                    **occurrence.as_payload(),
                },
            )
        )
    if minutes is not None:
        events.append(
            _observation(
                EventFamily.WORK_OBSERVATION,
                binding=binding,
                occurred_at=presented_at,
                payload={
                    "project_id": project_id,
                    "actor": actor,
                    "category": MANUAL_RECONSTRUCTION,
                    "minutes": minutes,
                    "evidence_reference": occurrence.identity,
                    **occurrence.as_payload(),
                },
            )
        )
    for event in events:
        validate_observation(event)
    return tuple(events)


def collect_triage_observations(
    *,
    project_id: int,
    actor: str,
    occurrence: TriageOccurrence,
    binding: AnalyticsBinding,
    judgment: bool | None = None,
    minutes: float | None = None,
) -> tuple[AnalyticsEvent, ...]:
    """Collect what this triage act observed, and answer with it.

    Called after the decision has committed, and deliberately total: a refused
    or unavailable measurement sink leaves the recorded project decision exactly
    as it stands and reports nothing collected. A deployment whose pinned
    configuration is not the measured pilot's collects nothing at all, so a
    submission that arrives without the controls that produce it records
    nothing either.
    """

    if (judgment is None and minutes is None) or not collection_is_pinned(binding):
        return ()
    try:
        events = triage_observations(
            project_id=project_id,
            actor=actor,
            occurrence=occurrence,
            binding=binding,
            judgment=judgment,
            minutes=minutes,
        )
        for event in events:
            collect_observation(event)
        return events
    except Exception:  # noqa: BLE001 - measurement never decides a project outcome
        logger.warning(
            "pilot triage observation was not collected",
            exc_info=True,
            extra={"corridor_fields": {"item_key": occurrence.item_key}},
        )
        return ()


def import_observations(document: dict[str, Any]) -> tuple[AnalyticsEvent, ...]:
    """Collect observations of work performed outside Corridor, or refuse them all.

    Every entry is read and validated before any of it is emitted, because a
    measurement stream half-filled from a rejected file is worse evidence than
    an empty one.
    """

    if document.get("schema_version") != IMPORT_VERSION:
        raise ValueError(f"unsupported observation import schema; expected {IMPORT_VERSION}")
    entries = document.get("observations")
    if not isinstance(entries, list) or not entries:
        raise ValueError("an observation import declares at least one observation")
    default = document.get("binding")
    events = [_imported(entry, index, default) for index, entry in enumerate(entries)]
    for event in events:
        collect_observation(event)
    return tuple(events)


def _imported(entry: Any, index: int, default: Any) -> AnalyticsEvent:
    """One declared external observation, refused by position where it is wrong."""

    where = f"observation {index}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not an observation record")
    for name in ("family", "occurred_at", "payload"):
        if entry.get(name) is None:
            raise ValueError(f"{where} declares no {name}")
    try:
        family = EventFamily(entry["family"])
    except ValueError:
        raise ValueError(f"{where} names an unknown event family {entry['family']!r}") from None
    if family not in IMPORTABLE_FAMILIES:
        raise ValueError(
            f"{where} is a {family.value} record. Only work, repair, sampling and "
            "provider observations are imported; presentations and decisions are "
            "what Corridor recorded, and an import may not supply them"
        )
    payload = entry["payload"]
    if not isinstance(payload, dict):
        raise ValueError(f"{where} declares no observation payload")
    if payload.get("sample_kind") in CONTEMPORANEOUS_SAMPLE_KINDS:
        raise ValueError(
            f"{where} is a {payload['sample_kind']} judgment. The contract takes it "
            "at the moment of triage in the product; an imported one is a "
            "retrospective relabelling"
        )
    occurred_at = datetime.fromisoformat(str(entry["occurred_at"]))
    if occurred_at.tzinfo is None:
        raise ValueError(f"{where} declares an instant with no time zone")
    declared = entry.get("binding") or default
    if not isinstance(declared, dict):
        raise ValueError(f"{where} declares no analytics binding, and the file sets no default")
    binding = AnalyticsBinding.from_dict(declared)
    if not (binding.customer_id and binding.environment and binding.database_identity):
        raise ValueError(
            f"{where} binds no customer, environment and database identity, so no "
            "measurement period could ever claim it"
        )
    event = _observation(
        family,
        binding=binding,
        occurred_at=occurred_at,
        payload=payload,
        metric_labels=entry.get("metric_labels") or {},
    )
    try:
        validate_observation(event)
    except ValueError as error:
        raise ValueError(f"{where}: {error}") from None
    return event


def _observation(
    family: EventFamily,
    *,
    binding: AnalyticsBinding,
    occurred_at: datetime,
    payload: dict[str, Any],
    metric_labels: dict[str, str] | None = None,
) -> AnalyticsEvent:
    """One observation whose identity is a digest of everything it records."""

    labels = dict(metric_labels or {})
    content = {
        "family": family.value,
        "occurred_at": occurred_at.isoformat(),
        "binding": binding.as_dict(),
        "payload": payload,
        "metric_labels": labels,
    }
    return AnalyticsEvent(
        family=family,
        binding=binding,
        occurred_at=occurred_at,
        payload=payload,
        metric_labels=labels,
        event_id=digests.canonical_sha256(content),
    )
