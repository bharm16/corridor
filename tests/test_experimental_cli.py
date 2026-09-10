"""Experiment-shaped commands require a guarded, explicit database."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from corridor.config import settings
from corridor.evidence_investigator_evaluation_cli import main as evaluation_main
from corridor.evidence_investigator_cli import main as investigator_main
from corridor.evidence_investigator_shadow_cli import main as shadow_main
from corridor.experimental_database import ProductionDatabaseRefusal
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import Project


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


def test_direct_candidate_model_requires_an_explicit_database(capsys):
    assert investigator_main(["1"]) == 2
    assert "--database-url" in capsys.readouterr().err


def test_direct_candidate_model_refuses_production_before_runtime_construction(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "corridor.evidence_investigator_cli.configured_direct_runtime",
        lambda **_kwargs: pytest.fail("database refusal must precede model runtime"),
    )

    assert investigator_main(
        ["1", "--database-url=postgresql://production"],
        session_factory=GuardSessionFactory(),
        database_guard=refuse_production,
    ) == 1
    assert "production database" in capsys.readouterr().err


def test_shadow_run_requires_an_explicit_database(tmp_path, capsys):
    assert shadow_main(
        [
            "run",
            "project",
            "--candidate-id=1",
            "--selection-rule=operator:test",
            f"--manifest-path={tmp_path / 'manifest.json'}",
        ]
    ) == 2
    assert "--database-url" in capsys.readouterr().err


def test_shadow_capture_refuses_production_before_opening_a_session(capsys):
    assert shadow_main(
        ["--database-url=postgresql://production", "capture", "shadow-case"],
        session_factory=GuardSessionFactory(),
        database_guard=refuse_production,
    ) == 1
    assert "production database" in capsys.readouterr().err


def test_shadow_run_refuses_production_before_constructing_a_model(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "corridor.evidence_investigator_shadow_cli.configured_direct_runtime",
        lambda **_kwargs: pytest.fail("database refusal must precede model runtime"),
    )

    assert shadow_main(
        [
            "--database-url=postgresql://production",
            "run",
            "project",
            "--candidate-id=1",
            "--selection-rule=operator:test",
            f"--manifest-path={tmp_path / 'manifest.json'}",
        ],
        session_factory=GuardSessionFactory(),
        database_guard=refuse_production,
    ) == 1
    assert "production database" in capsys.readouterr().err


def test_shadow_evaluation_requires_an_explicit_database(tmp_path, capsys):
    scores = tmp_path / "scores.json"
    scores.write_text("{}")

    assert evaluation_main(
        [
            "--run=run-1",
            f"--human-scores={scores}",
            f"--output-dir={tmp_path / 'out'}",
        ]
    ) == 2
    assert "--database-url" in capsys.readouterr().err


def test_shadow_evaluation_refuses_production_before_reading_scores(
    tmp_path, capsys
):
    missing_scores = Path(tmp_path / "missing.json")

    assert evaluation_main(
        [
            "--database-url=postgresql://production",
            "--run=run-1",
            f"--human-scores={missing_scores}",
            f"--output-dir={tmp_path / 'out'}",
        ],
        session_factory=GuardSessionFactory(),
        database_guard=refuse_production,
    ) == 1
    assert "production database" in capsys.readouterr().err


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
