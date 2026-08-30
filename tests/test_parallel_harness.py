"""Public test-harness contract for xdist worker database isolation."""

import os
import re

from sqlalchemy import text

import conftest as harness
from corridor.db import engine


def test_worker_run_identity_carries_the_controller_process():
    run_id = harness.new_worker_run_id()

    assert re.fullmatch(rf"{os.getpid()}_[0-9a-f]{{8}}", run_id)


def test_abandoned_worker_reclamation_keeps_live_process_databases(monkeypatch):
    dead = "corridor_pytest_101_deadbeef_gw0"
    live = "corridor_pytest_202_feedface_gw1"
    legacy = "corridor_pytest_0123456789ab_gw0"
    dropped = []
    monkeypatch.setattr(
        harness,
        "_worker_database_names",
        lambda _admin_url: (dead, live, legacy),
    )
    monkeypatch.setattr(harness, "_process_is_running", lambda pid: pid == 202)
    monkeypatch.setattr(
        harness,
        "_drop_databases",
        lambda _admin_url, names: dropped.extend(names),
    )

    harness.reap_abandoned_worker_databases(object())

    assert dropped == [dead]


def test_run_cleanup_selects_only_its_own_worker_databases(monkeypatch):
    run_id = "101_deadbeef"
    own = f"corridor_pytest_{run_id}_gw0"
    sibling = "corridor_pytest_202_feedface_gw0"
    monkeypatch.setattr(
        harness,
        "_worker_database_names",
        lambda _admin_url: (own, sibling),
    )

    assert harness._run_database_names(object(), run_id) == (own,)


def test_xdist_worker_uses_a_guarded_isolated_database(worker_id):
    with engine.connect() as connection:
        database_name = str(connection.scalar(text("select current_database()")))

    if worker_id == "master":
        assert database_name
        return
    assert re.fullmatch(
        rf"corridor_pytest_[1-9][0-9]*_[0-9a-f]{{8}}_{re.escape(worker_id)}",
        database_name,
    )
