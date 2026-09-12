"""Machine intake as a selected-capability journey (#847, over the #848 harness).

The inbound-mail receipt is the documented primary intake and a *selected*
capability: it is ready when the partner's selected ingress includes mail or
push intake (#535), not a step of the core journey every customer walks. It is
transport-authenticated and runs on the operations capability, so it is served
through its own authentication class of the one route registry rather than
gated as a human screen -- the omission that left it answering 404 before its
own authentication ran on an enforcing deployment.

This walks the capability over the shared harness, under the enforcing boundary,
and finishes on what the human source register shows: an authenticated mail
delivery becomes a row there like any other transport, so machine intake and the
Sources screen are proven to be one delivery model rather than two mechanisms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import journey_matrix
from access_support import seed_membership
from corridor import source_register
from corridor.config import settings
from corridor.web.app import (
    app,
    get_human_principal,
    get_machine_session,
    get_review_clock,
    get_session,
)
from journey_harness import Step, run_scenario
from test_machine_intake_boundary import (
    COORDINATOR,
    SECRET,
    alias_for,
    bind_alias,
    deliver,
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "store"


@pytest.fixture
def journey_client(session, monkeypatch):
    """A client for a deployment that enforces the live-pilot web boundary."""
    monkeypatch.setattr(settings, "live_pilot_web_boundary", True)
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "inbound_service_address", "")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_machine_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (
        lambda: datetime.now(timezone.utc)
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@dataclass
class Walk:
    session: Session
    project: object
    client: TestClient
    carried: dict = field(default_factory=dict)


def _step_machine_intake_delivery(walk: Walk) -> None:
    """A machine delivery is accepted and surfaces on the human source register."""
    alias = alias_for(walk.project)
    before = source_register.read_source_register(
        walk.session, project_id=walk.project.id
    )

    accepted = deliver(
        walk.client, alias=alias, message_id="<journey@example.test>",
        attach=("ucm.csv", "row,value\n1,2\n"),
    )
    assert accepted.status_code == 200, (
        "the enforcing boundary refused an authenticated machine delivery "
        "before its own authentication: " + accepted.text[:200]
    )
    body = accepted.json()
    assert body["project_id"] == walk.project.id, (
        "the delivery was bound to the wrong project: " + str(body)
    )
    assert body["route_status"] == "routed", (
        "the bound-alias delivery was not routed to its project: " + str(body)
    )

    after = source_register.read_source_register(
        walk.session, project_id=walk.project.id
    )
    assert len(after.rows) > len(before.rows), (
        "the accepted mail delivery did not become a row on the source register "
        "the coordinator reads"
    )
    assert any(row.channel == "project_alias" for row in after.rows), (
        "the source register does not show the mail-alias delivery it just took"
    )

    screen = walk.client.get(f"/projects/{walk.project.slug}/sources")
    assert screen.status_code == 200, (
        "the human source register is not served under the enforcing boundary: "
        + screen.text[:200]
    )


MACHINE_INTAKE_STEPS: tuple[Step, ...] = (
    Step(
        name="machine_intake_delivery",
        sentence="an authenticated mail transport delivers a source and it "
        "appears on the coordinator's source register, one delivery model with "
        "the human intake path",
        owner="#847",
        run=_step_machine_intake_delivery,
    ),
)


def test_the_machine_intake_capability_runs_as_a_declared_scenario(
    session, project, journey_client
):
    """Walk the selected capability and report every step against #847."""
    seed_membership(session, project, COORDINATOR)
    bind_alias(session, project=project, alias=alias_for(project))

    walk = Walk(session=session, project=project, client=journey_client)
    report = run_scenario(
        "Machine intake delivery (#847, a selected capability)",
        MACHINE_INTAKE_STEPS,
        walk,
    )
    print("\n" + report.render(), flush=True)

    assert not report.failures, (
        "a step nothing said could fail did:\n" + report.render()
    )
    assert len(report.passed) == len(MACHINE_INTAKE_STEPS), (
        "this capability is built, so every step of it passes:\n" + report.render()
    )


def test_every_machine_intake_row_names_a_step_of_this_scenario():
    """A selected-capability row #847 owns must be a step this scenario walks."""
    declared = {step.name for step in MACHINE_INTAKE_STEPS}
    mine = {
        row.scenario
        for row in journey_matrix.SELECTED_CAPABILITIES
        if row.owner == "#847"
    }
    assert mine and mine <= declared, (
        "unexercised #847 rows: " + ", ".join(sorted(mine - declared))
    )
