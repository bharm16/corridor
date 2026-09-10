"""Coverage for the raised rehearsal seam: one clone loop, one head refusal.

Four harnesses used to write this loop themselves. The loop is proved here
once, against a real disposable PostgreSQL database provisioned by the ordinary
provisioner, so a harness test no longer has to re-prove restore verification or
the head guard.

The capture and restore *transport* is not re-proved here: the Compose and
direct ``pg_dump``/``pg_restore`` routes have their own coverage in
``test_sh99_admission_acceptance.py``, and a local client older than the server
cannot run them. This module supplies a pinned source whose capture and restore
move real rows through a real file, which is what the seam actually depends on.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.migrations.policy import CURRENT_HEAD
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.rehearsal_environment import (
    CheckoutObservation,
    RehearsedClone,
    SealedRehearsalEnvironment,
    disposable_provisioner,
    observe_checkout,
    rehearse_on_disposable_clone,
)


ROOT = Path(__file__).resolve().parents[1]
CLAIM_BOUNDARY = {"disposable_clone_only": True, "shared_database_mutated": False}


def _database_url(name: str) -> str:
    return make_url(settings.database_url).set(database=name).render_as_string(
        hide_password=False
    )


def _admin_url() -> str:
    return make_url(settings.database_url).set(database="postgres").render_as_string(
        hide_password=False
    )


def _slugs(database_url: str) -> list[str]:
    """Read one small piece of real committed state from a real database."""

    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            return sorted(
                row[0]
                for row in connection.execute(text("select slug from projects"))
            )
    finally:
        engine.dispose()


def _insert_project(database_url: str, slug: str) -> None:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("insert into projects (slug, name) values (:slug, :name)"),
                {"slug": slug, "name": slug},
            )
    finally:
        engine.dispose()


class _PinnedSource:
    """A pinned source whose capture and restore really move the source rows.

    It stands in for ``SealedRehearsalEnvironment`` at exactly the three
    operations the seam calls, so the loop under test provisions, restores into,
    reads and mutates a genuine migrated PostgreSQL 16 database.
    """

    def __init__(self, source_database_url: str, *, migration_head: str = CURRENT_HEAD):
        self.source_database_url = source_database_url
        self.checkout_migration_head = migration_head
        self.captures = 0
        self.restored_into: list[str] = []

    def capture(self, dump_path: Path) -> None:
        self.captures += 1
        Path(dump_path).write_text(json.dumps(_slugs(self.source_database_url)))

    def restore(self, dump_path: Path, database_name: str) -> None:
        self.restored_into.append(database_name)
        for slug in json.loads(Path(dump_path).read_text()):
            _insert_project(_database_url(database_name), slug)

    @staticmethod
    def clone_url(admin_url: str, database_name: str) -> str:
        return SealedRehearsalEnvironment.clone_url(admin_url, database_name)


def _seam_provisioner():
    return disposable_provisioner(
        repo_root=ROOT,
        label="rehearsal_seam",
        reuse_migrated_template=True,
    )


@pytest.mark.slow
def test_the_seam_captures_restores_verifies_and_runs_one_operation(
    runtime_database, tmp_path
):
    """One call owns capture, provisioning, restore, verification and the pair."""

    source_url = _database_url(runtime_database.name)
    _insert_project(source_url, "pinned-source-project")
    source = _PinnedSource(source_url, migration_head=runtime_database.migration_head)

    def operation(clone):
        _insert_project(clone.database_url, "rehearsed-only-project")
        return {"database": clone.database_name}

    rehearsed = rehearse_on_disposable_clone(
        source,
        postgres_admin_url=_admin_url(),
        provision_database=_seam_provisioner(),
        dump_path=tmp_path / "source.dump",
        read_state=_slugs,
        operation=operation,
        claim_boundary=CLAIM_BOUNDARY,
    )

    assert isinstance(rehearsed, RehearsedClone)
    assert rehearsed.source_state == ["pinned-source-project"]
    assert rehearsed.before == rehearsed.source_state
    assert rehearsed.after == ["pinned-source-project", "rehearsed-only-project"]
    assert rehearsed.result == {"database": rehearsed.database_name}
    assert rehearsed.claim_boundary == CLAIM_BOUNDARY
    assert source.restored_into == [rehearsed.database_name]
    assert len(rehearsed.source_dump_sha256) == 64
    # The rehearsal changed only its own clone.
    assert _slugs(source_url) == ["pinned-source-project"]


@pytest.mark.slow
def test_a_second_clone_of_one_rehearsal_reuses_the_exact_captured_bytes(
    runtime_database, tmp_path
):
    """Two clones may only be compared when they restored identical bytes."""

    source = _PinnedSource(
        _database_url(runtime_database.name),
        migration_head=runtime_database.migration_head,
    )
    dump_path = tmp_path / "source.dump"

    def rehearse():
        return rehearse_on_disposable_clone(
            source,
            postgres_admin_url=_admin_url(),
            provision_database=_seam_provisioner(),
            dump_path=dump_path,
            read_state=_slugs,
            operation=lambda clone: None,
            claim_boundary=CLAIM_BOUNDARY,
        )

    first = rehearse()
    second = rehearse()

    assert source.captures == 1
    assert first.source_dump_sha256 == second.source_dump_sha256
    assert first.database_name != second.database_name
    assert source.restored_into == [first.database_name, second.database_name]


def test_the_seam_refuses_a_clone_outside_the_pinned_migration_head(tmp_path):
    """One head guard, one message, for every harness that used to write it."""

    source = _PinnedSource("postgresql://corridor:secret@localhost:5433/corridor")
    source.capture = lambda dump_path: Path(dump_path).write_bytes(b"dump")
    dump_path = tmp_path / "source.dump"

    @contextmanager
    def provision(_admin_url: str):
        yield SimpleNamespace(
            name="corridor_disposable_rehearsal_seam_1_0123456789ab",
            migration_head="another-head",
            postgres_version="16.10",
        )

    with pytest.raises(ValueError, match="pinned migration head"):
        rehearse_on_disposable_clone(
            source,
            postgres_admin_url="postgresql://corridor:secret@localhost:5433/postgres",
            provision_database=provision,
            dump_path=dump_path,
            read_state=lambda _url: {"state": "read"},
            operation=lambda _clone: pytest.fail("the operation must not run"),
            claim_boundary=CLAIM_BOUNDARY,
        )

    assert source.restored_into == []


def test_the_seam_refuses_a_clone_that_does_not_match_the_pinned_source(tmp_path):
    """Restore verification belongs to the seam, not to each harness."""

    source = _PinnedSource("postgresql://corridor:secret@localhost:5433/corridor")
    source.capture = lambda dump_path: Path(dump_path).write_bytes(b"dump")
    source.restore = lambda _dump_path, _name: None
    reads = iter(({"projects": 1}, {"projects": 0}))

    @contextmanager
    def provision(_admin_url: str):
        yield SimpleNamespace(
            name="corridor_disposable_rehearsal_seam_1_0123456789ab",
            migration_head=CURRENT_HEAD,
            postgres_version="16.10",
        )

    with pytest.raises(ValueError, match="does not match the pinned source"):
        rehearse_on_disposable_clone(
            source,
            postgres_admin_url="postgresql://corridor:secret@localhost:5433/postgres",
            provision_database=provision,
            dump_path=tmp_path / "source.dump",
            read_state=lambda _url: next(reads),
            operation=lambda _clone: pytest.fail("the operation must not run"),
            claim_boundary=CLAIM_BOUNDARY,
        )


def test_the_seam_refuses_a_rehearsal_that_states_no_claim_boundary(tmp_path):
    """Two harnesses' receipts stay apart because each states its own boundary."""

    source = _PinnedSource("postgresql://corridor:secret@localhost:5433/corridor")

    with pytest.raises(ValueError, match="claim boundary"):
        rehearse_on_disposable_clone(
            source,
            postgres_admin_url="postgresql://corridor:secret@localhost:5433/postgres",
            provision_database=lambda _url: pytest.fail("nothing may be provisioned"),
            dump_path=tmp_path / "source.dump",
            read_state=lambda _url: {},
            operation=lambda _clone: None,
            claim_boundary={},
        )


def test_the_seam_raises_the_caller_error_class(tmp_path):
    """A harness that owes its own refusal type keeps it through the seam."""

    class OwnRefusal(ValueError):
        pass

    source = _PinnedSource("postgresql://corridor:secret@localhost:5433/corridor")

    with pytest.raises(OwnRefusal, match="claim boundary"):
        rehearse_on_disposable_clone(
            source,
            postgres_admin_url="postgresql://corridor:secret@localhost:5433/postgres",
            provision_database=lambda _url: pytest.fail("nothing may be provisioned"),
            dump_path=tmp_path / "source.dump",
            read_state=lambda _url: {},
            operation=lambda _clone: None,
            claim_boundary={},
            error_cls=OwnRefusal,
        )


def test_the_checkout_is_observed_once_when_the_environment_is_sealed(monkeypatch):
    """One read of HEAD and one of the worktree, for the whole rehearsal."""

    observed: list[tuple[str, ...]] = []

    def fake_git(_root, *args):
        observed.append(args)
        return "a" * 40 if args == ("rev-parse", "HEAD") else ""

    monkeypatch.setattr("corridor.rehearsal_environment._git", fake_git)
    monkeypatch.setattr(
        "corridor.rehearsal_environment._source_migration_head",
        lambda _root: "source-head",
    )
    monkeypatch.setattr(
        "corridor.rehearsal_environment.read_migration_head",
        lambda *args, **kwargs: "source-head",
    )

    environment = SealedRehearsalEnvironment.open(
        source_database_url="postgresql://corridor:secret@localhost:5433/corridor",
        expected_checkout_revision="a" * 40,
        repo_root=Path("."),
    )

    assert observed == [("rev-parse", "HEAD"), ("status", "--porcelain")]
    assert environment.checkout == {
        "expected_checkout_revision": "a" * 40,
        "revision": "a" * 40,
    }


def test_a_dirty_checkout_is_refused_before_the_database_is_read(monkeypatch):
    monkeypatch.setattr(
        "corridor.rehearsal_environment._git",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else " M src",
    )
    monkeypatch.setattr(
        "corridor.rehearsal_environment.read_migration_head",
        lambda *args, **kwargs: pytest.fail("a dirty checkout is not investigated"),
    )

    with pytest.raises(ValueError, match="clean source checkout"):
        SealedRehearsalEnvironment.open(
            source_database_url="postgresql://corridor:secret@localhost:5433/corridor",
            expected_checkout_revision="a" * 40,
            repo_root=Path("."),
        )


def test_the_checkout_observation_names_its_status_for_a_receipt(monkeypatch):
    """`m8_acceptance` records "clean"/"dirty"; the observation reports it."""

    monkeypatch.setattr(
        "corridor.rehearsal_environment._git",
        lambda _root, *args: "b" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    assert observe_checkout(Path(".")) == CheckoutObservation(
        revision="b" * 40, clean=True
    )
    assert observe_checkout(Path(".")).status == "clean"

    monkeypatch.setattr(
        "corridor.rehearsal_environment._git",
        lambda _root, *args: "b" * 40 if args == ("rev-parse", "HEAD") else "?? new",
    )
    assert observe_checkout(Path(".")).status == "dirty"


def test_a_failed_git_read_is_one_refusal(monkeypatch):
    monkeypatch.setattr(
        "corridor.rehearsal_environment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: not a git repository"
        ),
    )

    with pytest.raises(ValueError, match="cannot observe Git checkout"):
        observe_checkout(Path("."))


def test_the_disposable_provisioner_names_one_labeled_namespace():
    """Each rehearsal's provisioner is built here, not partial-applied per module."""

    provision = disposable_provisioner(repo_root=ROOT, label="rehearsal_seam")
    assert provision.func is provision_disposable_postgres
    assert provision.keywords == {
        "repo_root": ROOT,
        "label": "rehearsal_seam",
    }
