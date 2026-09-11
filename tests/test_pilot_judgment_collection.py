"""The pilot's contemporaneous triage observations, collected in the product (#846).

The measurement this file defends is the packet interruption precision
criterion, and the way it dies is subtle: a collection surface whose answers
*become* the population measures how often people who answered were happy. So
the first test drives two interrupting packets onto the real Review screen,
judges one of them, abandons the other, and asks the real measurement reader
what the denominator is. It has to be two.

Everything else here is the rest of the maintainer's decision of 2026-09-10:
all four decisions can carry a judgment, nothing is preselected, an empty
minutes box is not a zero, the judgment is bound to the presentation that was
displayed rather than to whatever the packet became, a refused Save writes no
observation and loses none, a measurement failure never reaches the project
decision, the controls exist only under the pinned pilot configuration, and the
import path takes work performed outside Corridor without ever being able to
supply a denominator.

Nothing here reads a clock: the route's clock seam is overridden, so the
reading cutoff, the decision instant and the observation's own instant are all
stated by the test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from corridor import access, analytics
from corridor.analytics import EventFamily, capture_events
from corridor.consequence_levels import MUST_HANDLE
from corridor.issue_profile import DecisionBlockingPolicy, effective_issue_inventory
from corridor.measurement_collection import binding_for_session
from corridor.models import Project
from corridor.packet_review import read_review_items
from corridor.pilot_measurement import MeasurementPeriod, derive_measurement
from corridor.pilot_observations import (
    DID_NOT_NEED_HANDLING,
    IMPORT_VERSION,
    NEEDED_HANDLING,
    PILOT_MEASUREMENT_FLAG,
    TriageOccurrence,
    import_observations,
    triage_observations,
)
from corridor.principals import HumanPrincipal
from corridor.review_packets import KEEP_CURRENT, NEEDS_COORDINATION
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    configure_issue,
    field_mapping,
    modify,
    register_baseline,
    register_field_mapping,
    register_output_template,
    register_source_row,
    subject,
    support,
)

COORDINATOR = HumanPrincipal("local:coordinator")
JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)

PROMISED_FOR_FIELD = "committed_date"
PROMISED = "2026-11-01"
PROMISED_NOW = "2026-12-15"

# One executable customer policy waiting on the field every proposed change
# below moves, so every packet this file builds is at "Must handle before this
# issue" -- which is exactly the population the interruption criterion counts.
BLOCK_PROMISED = DecisionBlockingPolicy(
    policy="resolve_before_issue:v1",
    required_decision=f"field:{PROMISED_FOR_FIELD}",
    statement="Promised For changes must be decided before this issue.",
)


@pytest.fixture
def project(session: Session, member_project) -> Project:
    row = member_project(COORDINATOR, designations=[access.COORDINATION])
    # `binding_for_session` reads the deployment's own immutable marker, and a
    # measurement period is refused without one. One row is what the contract
    # means by "this customer's database"; the test's transaction rolls it back.
    session.execute(
        text(
            "insert into customer_environment_binding"
            "(singleton, customer_id, environment_id, deployment_id) "
            "values (true, :customer, :environment, :deployment)"
        ),
        {
            "customer": "pilot-partner",
            "environment": "pilot-environment",
            "deployment": "pilot-deployment",
        },
    )
    return row


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """Declare this process the measured pilot, the way a deployment does.

    The cohort table pins the complete flag state before the first measured
    week, and every event binds it, so the declaration that pins the cohort is
    the same one that offers the controls. There is no second switch.
    """

    path = tmp_path / "analytics-binding.json"
    path.write_text(
        json.dumps(
            {
                "code_revision": "git:pilot",
                "product_revision": "ADR-0086-v1",
                "packetizer_rules_version": "packetizer-v1",
                "enabled_feature_flags": [PILOT_MEASUREMENT_FLAG],
            }
        )
    )
    monkeypatch.setenv("CORRIDOR_ANALYTICS_BINDING_FILE", str(path))
    return path


@pytest.fixture
def unpinned(tmp_path, monkeypatch):
    """The same deployment shape with the pilot flag left off."""

    path = tmp_path / "analytics-binding.json"
    path.write_text(
        json.dumps(
            {
                "code_revision": "git:pilot",
                "product_revision": "ADR-0086-v1",
                "packetizer_rules_version": "packetizer-v1",
                "enabled_feature_flags": [],
            }
        )
    )
    monkeypatch.setenv("CORRIDOR_ANALYTICS_BINDING_FILE", str(path))
    return path


@pytest.fixture
def clock():
    """The instant every request in one test is taken at, changeable mid-test."""

    return {"now": CUTOFF}


@pytest.fixture
def client(session, clock):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: clock["now"])
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


class Interrupting:
    """An adopted project whose every source revision must be handled first."""

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[str, int] = {}
        first: int | None = None
        for number in (1, 2):
            fact, _ = self.adopted.capture(
                fact_type=PROMISED_FOR_FIELD,
                value=PROMISED,
                subject_key=subject(number),
            )
            revision = accept_baseline_fact(session, project, fact)
            first = first or revision
            self.revision_of[subject(number)] = revision
        assert first is not None
        self.baseline = register_baseline(session, project, self.adopted.document, first)
        for number in (1, 2):
            register_source_row(
                session,
                project,
                self.baseline,
                row_number=number,
                business_identity=f"UC-{number:03d}",
            )
        register_output_template(
            session, project, identity="district-ucm-template", version="v3"
        )
        register_field_mapping(session, project, field_mapping(PROMISED_FOR_FIELD))
        configure_issue(
            session,
            project,
            principal=COORDINATOR,
            effective_from=JANUARY,
            policies=(BLOCK_PROMISED,),
        )

    def proposes(self, number: int, *, source_revision: str, value: str = PROMISED_NOW) -> None:
        """One source revision proposing a new Promised For for one subject."""

        incoming = Rendition(self.session, self.project, f"ucm-{source_revision}.xlsx")
        fact, segment = incoming.capture(
            fact_type=PROMISED_FOR_FIELD,
            value=value,
            subject_key=subject(number),
        )
        support(self.session, self.project, fact, segment)
        append_deltas(
            self.session,
            self.project,
            incoming,
            source_revision=source_revision,
            values=[
                modify(
                    subject_key=subject(number),
                    field_name=PROMISED_FOR_FIELD,
                    accepted_value=PROMISED,
                    proposed_value=value,
                    baseline_revision=self.revision_of[subject(number)],
                )
            ],
        )


def _items(session: Session, project: Project, *, as_of=CUTOFF):
    return read_review_items(session, project_id=project.id, as_of=as_of).items


def _period(session: Session, project: Project, surfacing, *, as_of=CUTOFF) -> MeasurementPeriod:
    """The declared week, bound to exactly what the screen actually recorded.

    Reading the cohort off a presentation event rather than composing one keeps
    the test about the denominator: a period that disagreed with the events
    would report a cohort problem instead of the number under test.
    """

    inventory = effective_issue_inventory(session, project.id, as_of)
    return MeasurementPeriod(
        period_id=f"week:{uuid4().hex[:8]}",
        project_id=project.id,
        partner_id="pilot-partner",
        customer_id=surfacing.binding.customer_id,
        environment=surfacing.binding.environment,
        database_identity=surfacing.binding.database_identity,
        start=as_of - timedelta(hours=12),
        end=as_of + timedelta(hours=12),
        declared_at=as_of - timedelta(days=1),
        binding=surfacing.binding,
        issue_profile_identity=inventory.profile_identity,
        issue_profile_version=inventory.profile_version,
        issue_profile_sha256=inventory.content_sha256,
        required_artifacts=("updated_ucm",),
        previously_performed_artifacts=("updated_ucm",),
        quiet_max=2,
        ordinary_max=8,
        source_population_complete=False,
        interaction_capture_complete=True,
        retention_policy="pilot-retention",
        access_policy="pilot-access",
    )


def _report(session, project, collected, *, as_of=CUTOFF) -> dict:
    surfacing = next(
        event
        for event in collected.events
        if event.family == EventFamily.PACKET_SURFACING
    )
    period = _period(session, project, surfacing, as_of=as_of)
    return derive_measurement([period], collected.events)["periods"][0]


def _open(client, project, item_key: str):
    return client.get(f"/review/{project.slug}?item={quote(item_key, safe='')}")


def _decide(client, project, fields: dict[str, str]):
    return client.post(f"/review/{project.slug}", data=fields)


# --- the denominator, which collecting feedback must never move -------------


def test_an_unjudged_interrupting_packet_stays_in_the_denominator(
    session: Session, project: Project, client, pinned
):
    """The heart of #846, asked of the real screen and the real reader.

    Two interrupting packets are put in front of the coordinator. One is
    decided and judged; the other is left exactly as a busy week leaves it --
    never opened, never answered. The interruption denominator is two, the
    numerator is one, and the packet nobody answered is reported as unjudged
    rather than dropped. A collection surface whose replies became the
    population would report one of one and pass every week.
    """

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    built.proposes(2, source_revision="2026-09-b")
    judged, ignored = _items(session, project)
    assert judged.consequence == MUST_HANDLE
    assert ignored.consequence == MUST_HANDLE

    with capture_events() as collected:
        _open(client, project, judged.item_key)
        saved = _decide(
            client,
            project,
            {
                "item_key": judged.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in judged.children],
                "judgment": NEEDED_HANDLING,
                "judgment_minutes": "4",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": judged.consequence_rule_version,
            },
        )
    assert saved.status_code == 200

    period = _report(session, project, collected)
    by_key = {packet["item_key"]: packet for packet in period["packets"]}

    assert period["interrupting_packet_denominator"] == 2
    assert period["necessary_interrupting_packets"] == 1
    assert period["unjudged_interrupting_packets"] == 1
    assert by_key[judged.item_key]["necessary"] is True
    assert by_key[ignored.item_key]["necessary"] is None
    assert by_key[ignored.item_key]["outcomes"] == ["ignored/open"]
    assert by_key[judged.item_key]["manual_reconstruction"]["minutes"] == 4


def test_every_interrupting_packet_unjudged_still_counts_them_all(
    session: Session, project: Project, client, pinned
):
    """Zero is reported only when every presented interruption was judged.

    A week in which the coordinator answered nothing reports its full
    denominator with nothing subtracted, which is the reading the criterion
    needs in order to fail.
    """

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    built.proposes(2, source_revision="2026-09-b")

    with capture_events() as collected:
        client.get(f"/review/{project.slug}")

    period = _report(session, project, collected)

    assert period["interrupting_packet_denominator"] == 2
    assert period["necessary_interrupting_packets"] == 0
    assert period["unjudged_interrupting_packets"] == 2


# --- what the controls offer, and what they refuse to assume ----------------


def test_the_controls_appear_only_under_the_pinned_pilot_configuration(
    session: Session, project: Project, client, unpinned
):
    """An ordinary deployment's Review screen carries no measurement at all."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    body = _open(client, project, item.item_key).text

    assert "Pilot measurement" not in body
    assert 'name="judgment"' not in body


def test_a_submitted_judgment_is_not_collected_off_the_pinned_configuration(
    session: Session, project: Project, client, unpinned
):
    """The controls are the only source of these answers, so are the values."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    with capture_events() as collected:
        saved = _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": NEEDED_HANDLING,
                "judgment_minutes": "9",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert saved.status_code == 200
    assert not collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert not collected.by_family(EventFamily.WORK_OBSERVATION)


def test_nothing_is_preselected_and_an_empty_minutes_box_is_not_a_zero(
    session: Session, project: Project, client, pinned
):
    """No default "useful", and an unmeasured minute is unmeasured."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    body = _open(client, project, item.item_key).text
    # The option nobody answered is the only one the browser will send back,
    # and the minutes box starts empty rather than at a measured zero.
    assert '<option value="" selected>Not answered</option>' in body
    assert f'<option value="{NEEDED_HANDLING}">' in body
    assert f'<option value="{DID_NOT_NEED_HANDLING}">' in body
    assert 'name="judgment_minutes"' in body and 'value=""' in body

    with capture_events() as collected:
        saved = _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": "",
                "judgment_minutes": "",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert saved.status_code == 200
    assert not collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert not collected.by_family(EventFamily.WORK_OBSERVATION)


def test_a_judgment_without_time_records_the_judgment_and_no_minutes(
    session: Session, project: Project, client, pinned
):
    """One answer given and one left alone records exactly one observation."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    with capture_events() as collected:
        _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "keep_current",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": DID_NOT_NEED_HANDLING,
                "judgment_minutes": "",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    (sample,) = collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert sample.payload["necessary"] is False
    assert not collected.by_family(EventFamily.WORK_OBSERVATION)


@pytest.mark.parametrize("outcome", ["apply", "keep_current", "defer"])
def test_each_batched_decision_can_carry_a_judgment(
    session: Session, project: Project, client, pinned, outcome
):
    """Apply, Keep current and Defer are the same moment of triage."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    with capture_events() as collected:
        saved = _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": outcome,
                "child": [str(child.delta_id) for child in item.children],
                "defer_until": "2026-10-01",
                "judgment": NEEDED_HANDLING,
                "judgment_minutes": "2",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert saved.status_code == 200
    (sample,) = collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert sample.payload["necessary"] is True
    (observed,) = collected.by_family(EventFamily.WORK_OBSERVATION)
    assert observed.payload["category"] == "manual_reconstruction"
    assert observed.payload["minutes"] == 2


def test_needs_coordination_on_the_focused_screen_carries_a_judgment(
    session: Session, project: Project, client, pinned
):
    """The fourth decision lives on the cross-source screen, and is triage too.

    Two sources answer one Promised For differently, so the item is focused and
    each source is answered on its own. Needs coordination is the answer that
    only exists here, and it carries the same observation the batch does.
    """

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    built.proposes(1, source_revision="2026-09-b", value="2027-01-31")
    (item,) = [row for row in _items(session, project) if row.focused]
    assert item.consequence == MUST_HANDLE

    body = _open(client, project, item.item_key).text
    assert "Pilot measurement" in body

    with capture_events() as collected:
        answered = client.post(
            f"/review/{project.slug}/answers",
            data={
                "item_key": item.item_key,
                "answer_delta": [str(child.delta_id) for child in item.children],
                "answer_outcome": [NEEDS_COORDINATION, KEEP_CURRENT],
                "answer_source": ["", ""],
                "answer_question": ["Which date does the utility hold?", ""],
                "answer_person": ["Dana Reyes", ""],
                "answer_organization": ["", ""],
                "answer_return": ["", ""],
                "judgment": NEEDED_HANDLING,
                "judgment_minutes": "7",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert answered.status_code == 200, answered.text[-2000:]
    (sample,) = collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert sample.payload["necessary"] is True
    assert sample.payload["item_key"] == item.item_key
    (observed,) = collected.by_family(EventFamily.WORK_OBSERVATION)
    assert observed.payload["minutes"] == 7


# --- the occurrence a judgment is about -------------------------------------


def test_a_judgment_binds_the_displayed_occurrence_not_the_current_reading(
    session: Session, project: Project, client, pinned, clock
):
    """The answer is about the packet that was shown, at the version shown.

    The clock moves between the render and the Save, so the reading the Save
    re-takes has a different cutoff from the one the coordinator answered. The
    observation carries what the page displayed; re-deriving it here would
    record a judgment of a presentation that never happened.
    """

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)
    rendered = _open(client, project, item.item_key)
    assert rendered.status_code == 200
    assert f'value="{CUTOFF.isoformat()}"' in rendered.text

    clock["now"] = LATER
    with capture_events() as collected:
        _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": NEEDED_HANDLING,
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    (sample,) = collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert sample.payload["presented_cutoff"] == CUTOFF.isoformat()
    assert sample.payload["presented_consequence_level"] == MUST_HANDLE
    assert sample.occurred_at == CUTOFF
    assert CUTOFF.isoformat() in sample.payload["evidence_reference"]


def test_the_same_submission_twice_is_one_observation(session: Session, project: Project):
    """Repeated submission is idempotent: identical content, identical identity."""

    occurrence = TriageOccurrence(
        item_key="source-revision:ucm-workbook:2026-09",
        cutoff=CUTOFF.isoformat(),
        consequence_level=MUST_HANDLE,
        consequence_rule_version="issue-consequence-v1",
    )
    binding = binding_for_session(session)
    built = [
        triage_observations(
            project_id=project.id,
            actor=COORDINATOR.subject,
            occurrence=occurrence,
            binding=binding,
            judgment=True,
            minutes=3,
        )
        for _ in range(2)
    ]

    assert [event.as_dict() for event in built[0]] == [
        event.as_dict() for event in built[1]
    ]
    assert len({event.event_id for pair in built for event in pair}) == 2


# --- a Save that fails, and a measurement that fails -------------------------


def test_a_stale_save_writes_no_observation_and_keeps_the_answer(
    session: Session, project: Project, client, pinned
):
    """A refused decision records nothing, and does not lose what was typed."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    with capture_events() as collected:
        refused = _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id + 9000) for child in item.children],
                "judgment": NEEDED_HANDLING,
                "judgment_minutes": "6",
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert refused.status_code == 409
    assert not collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert not collected.by_family(EventFamily.WORK_OBSERVATION)
    assert f'<option value="{NEEDED_HANDLING}" selected>' in refused.text
    assert 'value="6"' in refused.text


def test_a_later_stale_save_does_not_overwrite_the_original_observation(
    session: Session, project: Project, client, pinned
):
    """The first contemporaneous judgment is the one the numerator reads."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    with capture_events() as collected:
        _open(client, project, item.item_key)
        _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": NEEDED_HANDLING,
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )
        # The same rendered page submitted again: the changes are gone, the
        # Save is refused, and the contradicting answer records nothing.
        again = _decide(
            client,
            project,
            {
                "item_key": item.item_key,
                "outcome": "apply",
                "child": [str(child.delta_id) for child in item.children],
                "judgment": DID_NOT_NEED_HANDLING,
                "judgment_cutoff": CUTOFF.isoformat(),
                "judgment_consequence": MUST_HANDLE,
                "judgment_rule_version": item.consequence_rule_version,
            },
        )

    assert again.status_code == 409
    (sample,) = collected.by_family(EventFamily.MEASUREMENT_SAMPLE)
    assert sample.payload["necessary"] is True
    period = _report(session, project, collected)
    assert period["packets"][0]["necessary"] is True


def test_a_measurement_failure_leaves_the_project_decision_saved(
    session: Session, project: Project, client, pinned, monkeypatch
):
    """An analytics sink that refuses is not an error page over a saved record."""

    built = Interrupting(session, project)
    built.proposes(1, source_revision="2026-09-a")
    (item,) = _items(session, project)

    class Refusing:
        def emit(self, event):
            if event.family is EventFamily.MEASUREMENT_SAMPLE:
                raise RuntimeError("the measurement sink is unavailable")

    monkeypatch.setattr(analytics, "_CURRENT_EMITTER", Refusing())
    saved = _decide(
        client,
        project,
        {
            "item_key": item.item_key,
            "outcome": "apply",
            "child": [str(child.delta_id) for child in item.children],
            "judgment": NEEDED_HANDLING,
            "judgment_cutoff": CUTOFF.isoformat(),
            "judgment_consequence": MUST_HANDLE,
            "judgment_rule_version": item.consequence_rule_version,
        },
    )

    assert saved.status_code == 200
    assert _items(session, project) == ()


# --- the import path for work performed outside Corridor --------------------


def _external(payload: dict, *, family="work_observation", occurred_at=CUTOFF) -> dict:
    return {
        "schema_version": IMPORT_VERSION,
        "binding": {
            "code_revision": "git:pilot",
            "product_revision": "ADR-0086-v1",
            "packetizer_rules_version": "packetizer-v1",
            "customer_id": "pilot-partner",
            "environment": "pilot-environment",
            "database_identity": "customer-database:abc",
        },
        "observations": [
            {
                "family": family,
                "occurred_at": occurred_at.isoformat(),
                "payload": payload,
            }
        ],
    }


def test_operations_time_performed_outside_corridor_enters_the_same_collector():
    """Setup, triage and connector minutes are real work Corridor cannot watch."""

    document = _external(
        {
            "project_id": 7,
            "actor": "local:operations",
            "category": "operations_triage",
            "minutes": 12,
            "evidence_reference": "operations-log:2026-09-03#4",
        }
    )

    with capture_events() as collected:
        (event,) = import_observations(document)

    assert collected.events == [event]
    assert event.family is EventFamily.WORK_OBSERVATION
    assert event.payload["minutes"] == 12


def test_the_import_cannot_supply_a_presentation_record():
    """A denominator is what Corridor recorded, never what a file declared."""

    document = _external(
        {"project_id": 7, "item_key": "invented", "consequence_level": MUST_HANDLE},
        family="packet_surfacing",
    )

    with capture_events() as collected:
        with pytest.raises(ValueError, match="presentations and decisions"):
            import_observations(document)

    assert collected.events == []


def test_the_import_refuses_a_retrospective_packet_judgment():
    """The contract takes this judgment at triage; an imported one is a relabel."""

    document = _external(
        {
            "project_id": 7,
            "actor": "local:analyst",
            "sample_kind": "packet_usefulness",
            "necessary": True,
            "evidence_reference": "worksheet:row-4",
        },
        family="measurement_sample",
    )

    with pytest.raises(ValueError, match="retrospective relabelling"):
        import_observations(document)


def test_the_import_refuses_an_observation_no_period_could_claim():
    """An unattributed observation is not evidence about any customer."""

    document = _external(
        {
            "project_id": 7,
            "actor": "local:operations",
            "category": "operations_setup",
            "minutes": 5,
            "evidence_reference": "operations-log:2026-09-03#5",
        }
    )
    document["binding"].pop("customer_id")

    with pytest.raises(ValueError, match="binds no customer"):
        import_observations(document)


def test_one_bad_observation_refuses_the_whole_import():
    """A measurement stream half-filled from a rejected file is worse evidence."""

    document = _external(
        {
            "project_id": 7,
            "actor": "local:operations",
            "category": "operations_setup",
            "minutes": 5,
            "evidence_reference": "operations-log:2026-09-03#5",
        }
    )
    document["observations"].append(
        {
            "family": "work_observation",
            "occurred_at": CUTOFF.isoformat(),
            # No reason recorded for the unavailable time.
            "payload": {
                "project_id": 7,
                "actor": "local:operations",
                "category": "operations_setup",
                "minutes": None,
                "evidence_reference": "operations-log:2026-09-03#6",
            },
        }
    )

    with capture_events() as collected:
        with pytest.raises(ValueError, match="observation 1"):
            import_observations(document)

    assert collected.events == []


def test_the_import_command_writes_the_jsonl_the_exporter_reads(tmp_path):
    """The entry point, end to end: a declaration file in, a governed log out."""

    from corridor.pilot_observation_cli import main

    source = tmp_path / "external-observations.json"
    source.write_text(
        json.dumps(
            _external(
                {
                    "project_id": 7,
                    "actor": "local:coordinator",
                    "category": "report_preparation",
                    "minutes": 45,
                    "evidence_reference": "baseline-worksheet:week-1",
                }
            )
        )
    )
    destination = tmp_path / "external-observations.jsonl"

    assert main(["--input", str(source), "--output", str(destination)]) == 0

    (line,) = destination.read_text().splitlines()
    written = json.loads(line)
    assert written["family"] == "work_observation"
    assert written["payload"]["minutes"] == 45
    assert destination.stat().st_mode & 0o077 == 0

