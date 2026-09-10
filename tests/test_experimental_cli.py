"""Experiment-shaped commands share one guarded frame, then add their own body.

The frame tests prove the properties every experimental command relies on:
an explicit database, a refusal before the body runs, the named disposable
database as the session the body sees, and one exit code and sentence per
refusal. Each command then gets one thin test showing its parser and body
run through that frame against the disposable database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from corridor.config import settings
from corridor.evidence_investigator_evaluation_cli import main as evaluation_main
from corridor.evidence_investigator_cli import main as investigator_main
from corridor.evidence_investigator_shadow_cli import main as shadow_main
from corridor.experimental_command import (
    experiment_parser,
    print_receipt,
    run_experiment,
    run_json_command,
)
from corridor.experimental_database import ProductionDatabaseRefusal
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import Project
from corridor.receipts import ArtifactCollision


@pytest.fixture(scope="module")
def disposable_database():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=Path(__file__).resolve().parents[1],
        label="experimental_cli",
    ) as database:
        yield database


def _database_url(database) -> str:
    engine = database.session_factory.kw.get("bind")
    assert engine is not None
    return engine.url.render_as_string(hide_password=False)


class GuardOnlySession:
    def begin(self):
        raise AssertionError("database refusal must precede command transaction")


class GuardSessionFactory:
    def __call__(self):
        session = GuardOnlySession()

        class Context:
            def __enter__(self):
                return session

            def __exit__(self, *_args):
                return False

        return Context()


def refuse_production(_database_url, *, session=None):
    assert session is not None
    raise ProductionDatabaseRefusal("experimental command refused production database")


def allow_any_database(_database_url, *, session=None):
    assert session is not None


class OpenSessionFactory:
    """An already-open session adapter, as the harness hands a command."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return False


# ------------------------------------------------------------------- frame


def test_the_frame_requires_an_explicit_database(capsys):
    parser = experiment_parser(description="frame")
    parser.add_argument("subject")

    assert run_json_command(
        parser, ["subject"], lambda _session, _args: pytest.fail("no database")
    ) == 2
    assert "--database-url" in capsys.readouterr().err


def test_help_is_a_parse_exit_that_prints_no_result(capsys):
    parser = experiment_parser(description="frame")

    assert run_json_command(
        parser, ["--help"], lambda _session, _args: pytest.fail("help runs no body")
    ) == 0
    captured = capsys.readouterr()
    assert "--database-url" in captured.out
    assert "{" not in captured.out


def test_the_frame_refuses_production_before_the_body_runs(capsys):
    parser = experiment_parser(description="frame")

    assert run_json_command(
        parser,
        ["--database-url=postgresql://production"],
        lambda _session, _args: pytest.fail("database refusal must precede the body"),
        session_factory=GuardSessionFactory(),
        database_guard=refuse_production,
    ) == 1
    assert "production database" in capsys.readouterr().err


def test_the_frame_runs_the_body_in_the_named_disposable_database(
    disposable_database, capsys
):
    parser = experiment_parser(description="frame")
    parser.add_argument("subject")

    def body(session, args):
        assert session.scalar(text("select current_database()")) == disposable_database.name
        return {"subject": args.subject, "hidden": True}

    assert run_json_command(
        parser,
        ["subject", f"--database-url={_database_url(disposable_database)}"],
        body,
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"hidden": True, "subject": "subject"}


@pytest.mark.parametrize(
    "refusal",
    [
        ProductionDatabaseRefusal("experimental command refused the configured production database"),
        OSError("scores.json is unreadable"),
        ValueError("unknown project 'missing'"),
        ArtifactCollision("divergent overwrite of an immutable receipt"),
    ],
)
def test_each_refusal_maps_to_exit_1_with_its_sentence(
    disposable_database, capsys, refusal
):
    def body(_session):
        raise refusal

    with disposable_database.session_factory() as session:
        status = run_experiment(
            _database_url(disposable_database),
            body,
            session_factory=OpenSessionFactory(session),
            database_guard=allow_any_database,
        )

    assert status == 1
    assert capsys.readouterr().err == f"{refusal}\n"


def test_a_declared_refusal_family_maps_the_same_way(disposable_database, capsys):
    class NothingHere(Exception):
        pass

    def body(_session):
        raise NothingHere("no measurement to take")

    with disposable_database.session_factory() as session:
        status = run_experiment(
            _database_url(disposable_database),
            body,
            session_factory=OpenSessionFactory(session),
            database_guard=allow_any_database,
            refusals=(NothingHere,),
        )

    assert status == 1
    assert capsys.readouterr().err == "no measurement to take\n"


def test_a_receipt_is_sealed_once_and_the_rerun_says_so(tmp_path, capsys):
    written = {"artifact_identity": "abcdef0123456789ff", "ran_at": "first", "matched": 1}

    print_receipt(
        written,
        output_dir=tmp_path,
        filename="measurement-abcdef0123456789.json",
        preserved="existing immutable artifact preserved",
    )
    path = tmp_path / "measurement-abcdef0123456789.json"
    assert capsys.readouterr().out == f"\n{path}\n"
    assert json.loads(path.read_text()) == written

    print_receipt(
        {**written, "ran_at": "later"},
        output_dir=tmp_path,
        filename="measurement-abcdef0123456789.json",
        preserved="existing immutable artifact preserved",
    )
    assert capsys.readouterr().out == (
        f"\n{path} (existing immutable artifact preserved)\n"
    )
    assert json.loads(path.read_text())["ran_at"] == "first"

    with pytest.raises(ArtifactCollision):
        print_receipt(
            {**written, "matched": 0},
            output_dir=tmp_path,
            filename="measurement-abcdef0123456789.json",
            preserved="existing immutable artifact preserved",
        )


# ---------------------------------------------------------------- commands


def test_direct_candidate_model_uses_the_named_disposable_database(
    disposable_database, monkeypatch, capsys
):
    async def investigate(session, *_args, **_kwargs):
        assert session.scalar(text("select current_database()")) == disposable_database.name
        return SimpleNamespace(
            run=SimpleNamespace(public_id="investigation-run"),
            result=SimpleNamespace(status="abstained", reason="test", packet=None),
        )

    monkeypatch.setattr(
        "corridor.evidence_investigator_cli.configured_direct_runtime",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "corridor.evidence_investigator_cli.configured_runtime_identity",
        lambda _model: object(),
    )
    monkeypatch.setattr(
        "corridor.evidence_investigator_cli.run_receipted_investigation",
        investigate,
    )

    assert investigator_main(
        ["1", f"--database-url={_database_url(disposable_database)}"]
    ) == 0
    assert "investigation-run" in capsys.readouterr().out


def test_shadow_run_uses_the_named_disposable_database(
    disposable_database, monkeypatch, tmp_path, capsys
):
    with disposable_database.session_factory() as setup:
        project = Project(
            slug="experimental-shadow-cli",
            name="Experimental Shadow CLI",
            is_synthetic=True,
        )
        setup.add(project)
        setup.commit()

    async def run_cohort(session, *_args, **_kwargs):
        assert session.scalar(text("select current_database()")) == disposable_database.name
        return SimpleNamespace(
            manifest=SimpleNamespace(
                cohort_id="shadow-cohort",
                manifest_sha256="a" * 64,
            ),
            results=(),
        )

    monkeypatch.setattr(
        "corridor.evidence_investigator_shadow_cli.configured_runtime_identity",
        lambda _model: object(),
    )
    monkeypatch.setattr(
        "corridor.evidence_investigator_shadow_cli.run_v2_shadow_cohort",
        run_cohort,
    )
    monkeypatch.setattr(
        "corridor.evidence_investigator_shadow_cli.write_shadow_cohort_manifest",
        lambda *_args: None,
    )

    assert shadow_main(
        [
            f"--database-url={_database_url(disposable_database)}",
            "run",
            project.slug,
            "--candidate-id=1",
            "--selection-rule=operator:test",
            f"--manifest-path={tmp_path / 'manifest.json'}",
        ]
    ) == 0
    assert "shadow-cohort" in capsys.readouterr().out


def test_shadow_evaluation_uses_the_named_disposable_database(
    disposable_database, monkeypatch, tmp_path, capsys
):
    scores = tmp_path / "scores.json"
    scores.write_text("{}")

    def evaluate(session, *_args, **_kwargs):
        assert session.scalar(text("select current_database()")) == disposable_database.name
        return SimpleNamespace(
            receipt=SimpleNamespace(public_id="evaluation", status="passed"),
            machine_path=tmp_path / "evaluation.json",
            summary_path=tmp_path / "evaluation.md",
        )

    monkeypatch.setattr(
        "corridor.evidence_investigator_evaluation_cli.evaluate_shadow_runs",
        evaluate,
    )

    assert evaluation_main(
        [
            f"--database-url={_database_url(disposable_database)}",
            "--run=run-1",
            f"--human-scores={scores}",
            f"--output-dir={tmp_path / 'out'}",
        ]
    ) == 0
    assert "evaluation" in capsys.readouterr().out
