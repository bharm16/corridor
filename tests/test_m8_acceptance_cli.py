"""Command routing for the explicit M8 capture/replay boundary."""

from __future__ import annotations

import json

import pytest

from corridor.m8_acceptance import (
    AcceptanceError,
    AcceptanceBundleSummary,
    AssertionResult,
    CaptureSummary,
    VerificationResult,
)
from corridor.m8_acceptance_cli import ProductionExtractor, main


def _extractor(prompt_version, schema_version, extract_document=None):
    """One stated extractor identity for a capture test.

    The default read must never run: every test that leaves it out asserts
    capture stops before extraction.
    """

    def unreached(*_args, **_kwargs):
        raise AssertionError("capture reached the extractor")

    return ProductionExtractor(
        prompt_version=prompt_version,
        schema_version=schema_version,
        extract_document=extract_document or unreached,
    )


def _json_output(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_capture_refuses_to_hide_an_unconfigured_model_path(
    monkeypatch, tmp_path, capsys
):
    def must_not_construct_client(*_args, **_kwargs):
        raise AssertionError("capture without opt-in must not construct a client")

    def must_not_capture(*_args, **_kwargs):
        raise AssertionError("capture must not start without live-model opt-in")

    monkeypatch.setattr("corridor.llm.OpenAIClient", must_not_construct_client)
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.capture_m8_fixture", must_not_capture
    )

    status = main(
        [
            "capture",
            "--source-lock",
            str(tmp_path / "sources.lock.json"),
            "--output-dir",
            str(tmp_path / "fixture"),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--prompt-version",
            "matrix-v9",
            "--expected-model",
            "gpt-test",
            "--schema-version",
            "matrix-candidate-shape-v3",
            "--expected-clean-git-revision",
            "abc123",
        ]
    )

    assert status == 1
    error = capsys.readouterr().err
    assert "--live-model" in error
    assert "explicit" in error


@pytest.mark.parametrize(
    "missing",
    ["--expected-model", "--expected-clean-git-revision"],
)
def test_capture_requires_the_model_and_clean_revision_pins(
    missing, monkeypatch, tmp_path, capsys
):
    arguments = [
        "capture",
        "--live-model",
        "--source-lock",
        str(tmp_path / "sources.lock.json"),
        "--output-dir",
        str(tmp_path / "fixture"),
        "--postgres-admin-url",
        "postgresql+psycopg://acceptance@localhost/postgres",
        "--prompt-version",
        "matrix-v9",
        "--expected-model",
        "gpt-test",
        "--schema-version",
        "matrix-v9",
        "--expected-clean-git-revision",
        "abc123",
    ]
    index = arguments.index(missing)
    del arguments[index : index + 2]
    monkeypatch.setattr(
        "corridor.llm.OpenAIClient",
        lambda: (_ for _ in ()).throw(
            AssertionError("argument validation must precede client construction")
        ),
    )

    assert main(arguments) == 2
    assert "required" in capsys.readouterr().err


def test_live_capture_constructs_one_client_routes_the_production_extractor_and_closes(
    monkeypatch, tmp_path, capsys
):
    source_lock = tmp_path / "sources.lock.json"
    output_dir = tmp_path / "fixture"
    seen = []
    clients = []

    class Client:
        model = "gpt-test"

        def __init__(self):
            self.closes = 0
            clients.append(self)

        def close(self):
            self.closes += 1

    def production_extract(session, document, *, client):
        seen.append(("extract", session, document, client))
        return ["candidate"]

    def capture(config, *, extract):
        seen.append(("capture", config))
        assert extract("session", "document") == ["candidate"]
        return CaptureSummary(
            fixture_path=output_dir / "fixture.json",
            fixture_sha256="a1" * 32,
            database_name="corridor_m8_acceptance_" + "1" * 32,
            run_count=5,
            candidate_count=18,
        )

    monkeypatch.setattr("corridor.llm.OpenAIClient", Client)
    monkeypatch.setattr("corridor.m8_acceptance_cli.capture_m8_fixture", capture)

    assert main(
        [
            "capture",
            "--live-model",
            "--source-lock",
            str(source_lock),
            "--output-dir",
            str(output_dir),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--prompt-version",
            "matrix-v9",
            "--expected-model",
            "gpt-test",
            "--schema-version",
            "matrix-candidate-shape-v3",
            "--expected-clean-git-revision",
            "abc123",
        ],
        extractor=_extractor("matrix-v9", "matrix-candidate-shape-v3", production_extract),
    ) == 0

    assert len(clients) == 1
    assert clients[0].closes == 1
    _, config = seen[0]
    assert config.source_lock_path == source_lock
    assert config.output_dir == output_dir
    assert config.postgres_admin_url.endswith("/postgres")
    assert config.prompt_version == "matrix-v9"
    assert config.expected_model == "gpt-test"
    assert config.schema_version == "matrix-candidate-shape-v3"
    assert config.expected_clean_git_revision == "abc123"
    assert seen[1] == ("extract", "session", "document", clients[0])
    assert _json_output(capsys) == {
        "candidate_count": 18,
        "command": "capture",
        "database_name": "corridor_m8_acceptance_" + "1" * 32,
        "fixture_path": str(output_dir / "fixture.json"),
        "fixture_sha256": "a1" * 32,
        "run_count": 5,
    }


def test_live_capture_rejects_model_drift_and_still_closes(
    monkeypatch, tmp_path, capsys
):
    clients = []

    class Client:
        model = "configured-model"

        def __init__(self):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("corridor.llm.OpenAIClient", Client)
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.capture_m8_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model drift must be rejected before capture")
        ),
    )

    status = main(
        [
            "capture",
            "--live-model",
            "--source-lock",
            str(tmp_path / "sources.lock.json"),
            "--output-dir",
            str(tmp_path / "fixture"),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
                "--prompt-version",
                "matrix-v9",
                "--expected-model",
                "pinned-model",
                "--schema-version",
                "matrix-candidate-shape-v3",
                "--expected-clean-git-revision",
                "abc123",
            ],
        extractor=_extractor("matrix-v9", "matrix-candidate-shape-v3"),
    )

    assert status == 1
    assert len(clients) == 1
    assert clients[0].closed is True
    assert "configured-model" in capsys.readouterr().err


def test_live_capture_rejects_prompt_drift_before_constructing_client(
    monkeypatch, tmp_path, capsys
):
    def must_not_construct_client(*_args, **_kwargs):
        raise AssertionError("prompt drift must be rejected before client construction")

    monkeypatch.setattr("corridor.llm.OpenAIClient", must_not_construct_client)
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.capture_m8_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prompt drift must be rejected before capture")
        ),
    )

    status = main(
        [
            "capture",
            "--live-model",
            "--source-lock",
            str(tmp_path / "sources.lock.json"),
            "--output-dir",
            str(tmp_path / "fixture"),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--prompt-version",
            "matrix-stale",
            "--expected-model",
            "gpt-test",
            "--schema-version",
            "candidate-shape-v2",
            "--expected-clean-git-revision",
            "abc123",
        ],
        extractor=_extractor("matrix-production", "candidate-shape-v2"),
    )

    assert status == 1
    error = capsys.readouterr().err
    assert "matrix-stale" in error
    assert "matrix-production" in error


def test_live_capture_rejects_schema_drift_before_constructing_client(
    monkeypatch, tmp_path, capsys
):
    def must_not_construct_client(*_args, **_kwargs):
        raise AssertionError("schema drift must be rejected before client construction")

    monkeypatch.setattr("corridor.llm.OpenAIClient", must_not_construct_client)
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.capture_m8_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("schema drift must be rejected before capture")
        ),
    )

    status = main(
        [
            "capture",
            "--live-model",
            "--source-lock",
            str(tmp_path / "sources.lock.json"),
            "--output-dir",
            str(tmp_path / "fixture"),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--prompt-version",
            "matrix-production",
            "--expected-model",
            "gpt-test",
            "--schema-version",
            "matrix-candidate-shape-stale",
            "--expected-clean-git-revision",
            "abc123",
        ],
        extractor=_extractor(
            "matrix-production", "matrix-candidate-shape-production"
        ),
    )

    assert status == 1
    error = capsys.readouterr().err
    assert "matrix-candidate-shape-stale" in error
    assert "matrix-candidate-shape-production" in error


def test_live_capture_closes_the_client_when_capture_fails(
    monkeypatch, tmp_path, capsys
):
    clients = []

    class Client:
        model = "gpt-test"

        def __init__(self):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("corridor.llm.OpenAIClient", Client)
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.capture_m8_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AcceptanceError("capture failed")
        ),
    )

    status = main(
        [
            "capture",
            "--live-model",
            "--source-lock",
            str(tmp_path / "sources.lock.json"),
            "--output-dir",
            str(tmp_path / "fixture"),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
                "--prompt-version",
                "matrix-v9",
                "--expected-model",
                "gpt-test",
                "--schema-version",
                "matrix-candidate-shape-v3",
                "--expected-clean-git-revision",
                "abc123",
            ],
        extractor=_extractor("matrix-v9", "matrix-candidate-shape-v3"),
    )

    assert status == 1
    assert len(clients) == 1
    assert clients[0].closed is True
    assert "capture failed" in capsys.readouterr().err


def test_replay_routes_model_free_without_constructing_a_client(
    monkeypatch, tmp_path, capsys
):
    fixture = tmp_path / "fixture.json"
    transformations = tmp_path / "controlled.json"
    output_dir = tmp_path / "bundle"
    seen = []

    def forbidden_model_client(*_args, **_kwargs):
        raise AssertionError("ordinary replay must not construct a model client")

    monkeypatch.setattr("corridor.llm.OpenAIClient", forbidden_model_client)

    def replay(config):
        seen.append(config)
        return AcceptanceBundleSummary(
            bundle_dir=output_dir,
            manifest_path=output_dir / "manifest.json",
            integrity_manifest_sha256="b2" * 32,
            canonical_content_sha256="c3" * 32,
            fixture_sha256="d4" * 32,
            assertions=(
                AssertionResult(name="real_lane_is_observation_only", passed=True),
            ),
            carried_count=2,
            abstention_counts={"comparison_changed": 1},
            database_name="corridor_m8_acceptance_" + "2" * 32,
        )

    monkeypatch.setattr("corridor.m8_acceptance_cli.run_m8_acceptance", replay)

    assert main(
        [
            "replay",
            "--fixture",
            str(fixture),
            "--transformations",
            str(transformations),
            "--output-dir",
            str(output_dir),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--expected-fixture-sha256",
            "d4" * 32,
            "--expected-transformations-sha256",
            "e5" * 32,
            "--expected-clean-git-revision",
            "abc123",
        ]
    ) == 0

    config = seen.pop()
    assert config.fixture_path == fixture
    assert config.transformations_path == transformations
    assert config.output_dir == output_dir
    assert config.expected_fixture_sha256 == "d4" * 32
    assert config.expected_transformations_sha256 == "e5" * 32
    assert config.expected_clean_git_revision == "abc123"
    assert _json_output(capsys) == {
        "abstention_counts": {"comparison_changed": 1},
        "assertions": [
            {
                "detail": None,
                "expected": None,
                "name": "real_lane_is_observation_only",
                "observed": None,
                "passed": True,
            }
        ],
        "bundle_dir": str(output_dir),
        "canonical_content_sha256": "c3" * 32,
        "carried_count": 2,
        "command": "replay",
        "database_name": "corridor_m8_acceptance_" + "2" * 32,
        "fixture_sha256": "d4" * 32,
        "integrity_manifest_sha256": "b2" * 32,
        "manifest_path": str(output_dir / "manifest.json"),
    }


def test_replay_prints_failed_assertions_and_returns_nonzero(
    monkeypatch, tmp_path, capsys
):
    output_dir = tmp_path / "bundle"
    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.run_m8_acceptance",
        lambda _config: AcceptanceBundleSummary(
            bundle_dir=output_dir,
            manifest_path=output_dir / "manifest.json",
            integrity_manifest_sha256="b2" * 32,
            canonical_content_sha256="c3" * 32,
            fixture_sha256="d4" * 32,
            assertions=(
                AssertionResult(
                    name="controlled_oracle_matches",
                    passed=False,
                    expected="exact",
                    observed="changed",
                ),
            ),
            carried_count=0,
            abstention_counts={"comparison_changed": 1},
            database_name="corridor_m8_acceptance_" + "2" * 32,
        ),
    )

    status = main(
        [
            "replay",
            "--fixture",
            str(tmp_path / "fixture.json"),
            "--transformations",
            str(tmp_path / "controlled.json"),
            "--output-dir",
            str(output_dir),
            "--postgres-admin-url",
            "postgresql+psycopg://acceptance@localhost/postgres",
            "--expected-fixture-sha256",
            "d4" * 32,
            "--expected-transformations-sha256",
            "e5" * 32,
        ]
    )

    assert status == 1
    payload = _json_output(capsys)
    assert payload["assertions"] == [
        {
            "detail": None,
            "expected": "exact",
            "name": "controlled_oracle_matches",
            "observed": "changed",
            "passed": False,
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["verify", "bundle"],
        ["verify", "bundle", "--expected-manifest-sha256", "ABC"],
        ["verify", "bundle", "--expected-manifest-sha256", "AA" * 32],
    ],
)
def test_verify_requires_a_lowercase_sha256_pin(arguments, capsys):
    assert main(arguments) == 2
    error = capsys.readouterr().err
    assert "64 lowercase hexadecimal" in error or "required" in error


def test_verify_routes_only_through_the_public_bundle_verifier(
    monkeypatch, tmp_path, capsys
):
    bundle_dir = tmp_path / "bundle"
    seen = []

    expected_manifest_sha256 = "11" * 32

    def verify(path, *, expected_integrity_manifest_sha256):
        seen.append((path, expected_integrity_manifest_sha256))
        return VerificationResult(
            valid=True,
            integrity_manifest_sha256="f6" * 32,
            canonical_content_sha256="07" * 32,
        )

    monkeypatch.setattr(
        "corridor.m8_acceptance_cli.verify_m8_acceptance_bundle", verify
    )

    assert main(
        [
            "verify",
            str(bundle_dir),
            "--expected-manifest-sha256",
            expected_manifest_sha256,
        ]
    ) == 0

    assert seen == [(bundle_dir, expected_manifest_sha256)]
    assert _json_output(capsys) == {
        "bundle_dir": str(bundle_dir),
        "canonical_content_sha256": "07" * 32,
        "command": "verify",
        "integrity_manifest_sha256": "f6" * 32,
        "valid": True,
    }
