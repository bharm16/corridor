"""Every installed prompt is one value: file, version, bytes, digest, schema."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from corridor import (
    briefing,
    coordination_summary,
    email_spine,
    evidence_investigator_runtime,
    extract_agreement,
    extract_minutes_v5,
    extraction_failure_diagnosis,
    minutes_spine,
    production_run_explanation,
    prose_interpretation,
    revision_change_explanation,
    source_intake_draft,
)
from corridor.extractor_lineage import deployed_extractor_config, validate_runtime_config
from corridor.prompt_library import PROMPTS, Prompt, installed_prompt, require_installed_prompt
from model_client_support import FakeModelClient


# Every module that declares one installed prompt. `coordination_summary` is
# not listed because it holds `briefing`'s value by import rather than
# declaring its own -- which is the point of that import.
DECLARING_MODULES = (
    briefing,
    email_spine,
    evidence_investigator_runtime,
    extract_agreement,
    extract_minutes_v5,
    extraction_failure_diagnosis,
    minutes_spine,
    production_run_explanation,
    prose_interpretation,
    revision_change_explanation,
    source_intake_draft,
)


def test_the_file_a_prompt_reads_is_the_one_its_version_names():
    """The drift this replaces: a module declared `evidence-investigator-v3`
    beside the bytes of `evidence_investigator_v2.md`, and no reader noticed."""
    for module in DECLARING_MODULES:
        installed = module.PROMPT
        assert isinstance(installed, Prompt), module.__name__
        assert installed.version == module.PROMPT_VERSION, module.__name__
        assert installed.path.name == f"{installed.version.replace('-', '_')}.md", (
            module.__name__
        )
        assert installed.path.parent == PROMPTS, module.__name__


def test_a_prompt_carries_the_exact_bytes_and_digest_of_its_file():
    for module in DECLARING_MODULES:
        installed = module.PROMPT
        raw = installed.path.read_bytes()
        assert installed.data == raw, module.__name__
        assert installed.text == raw.decode("utf-8"), module.__name__
        assert installed.sha256 == hashlib.sha256(raw).hexdigest(), module.__name__


def test_a_prompt_resolves_from_its_checkout_not_the_process_cwd(tmp_path, monkeypatch):
    """Three modules used to name `Path("prompts/x.md")` while the seal joined
    the same constant against the repository root, so from any other working
    directory the run failed closed on bytes that did not match their seal."""
    monkeypatch.chdir(tmp_path)

    for version in ("minutes_v5", "agreement_v3", "prose_interpretation_v1"):
        loaded = installed_prompt(version)
        assert loaded.path.is_absolute()
        assert loaded.path.parent == PROMPTS
        assert loaded.sha256 == hashlib.sha256(loaded.path.read_bytes()).hexdigest()
        assert loaded.text.strip()


def test_no_installed_prompt_path_depends_on_the_working_directory():
    for module in DECLARING_MODULES:
        assert module.PROMPT.path.is_absolute(), module.__name__


@pytest.mark.parametrize(
    ("extractor", "module"),
    (("minutes", extract_minutes_v5), ("agreement", extract_agreement)),
)
def test_the_seal_and_the_request_read_one_prompt(extractor, module, tmp_path, monkeypatch):
    """`extractor_lineage` used to join the same constant against the repo root
    while the runner read it from the process CWD: one constant, two files."""
    monkeypatch.chdir(tmp_path)
    client = FakeModelClient()
    config = deployed_extractor_config(extractor, client=client)

    assert config.prompt_sha256 == module.PROMPT.sha256
    validate_runtime_config(
        config,
        prompt_version=module.PROMPT_VERSION,
        model=client.model,
        prompt_bytes=module.PROMPT.data,
        schema=module.SCHEMA,
    )


def test_a_missing_prompt_file_names_the_file_rather_than_its_bytes():
    with pytest.raises(FileNotFoundError, match="no_such_prompt_v1.md"):
        installed_prompt("no-such-prompt-v1")


def test_the_evidence_investigator_names_the_prompt_its_retained_cohorts_name():
    """Four retained cohort manifests record `evidence-investigator-v2` beside
    this digest; the constant said v3 after a vocabulary sweep bumped it
    without touching the file."""
    assert evidence_investigator_runtime.PROMPT_VERSION == "evidence-investigator-v2"
    assert (
        evidence_investigator_runtime.PROMPT.sha256
        == evidence_investigator_runtime.PROMPT_SHA256
    )
    retained = Path(__file__).resolve().parents[1] / "artifacts/evidence-investigator"
    manifests = sorted(retained.glob("*-cohort-*.json"))
    assert manifests, "the retained cohort manifests are the reason v2 is the name"
    for manifest in manifests:
        text = manifest.read_text()
        if evidence_investigator_runtime.PROMPT_SHA256 not in text:
            continue
        assert f'"{evidence_investigator_runtime.PROMPT_VERSION}"' in text, manifest.name


def test_the_summary_takes_its_schema_from_the_prompt_it_imports():
    """One prompt file had two byte-identical copies of the schema it promises,
    and only `briefing`'s carried the comment naming the ref vocabulary."""
    assert coordination_summary.PROMPT is briefing.PROMPT
    assert coordination_summary.PROMPT.schema is briefing.SENTENCE_SCHEMA


def test_every_model_assistance_family_pairs_its_prompt_with_its_schema():
    for module in (
        revision_change_explanation,
        extraction_failure_diagnosis,
        production_run_explanation,
        source_intake_draft,
    ):
        assert module.PROMPT.schema is not None, module.__name__


def test_one_rule_refuses_a_configuration_that_names_another_prompt():
    class Refused(ValueError):
        pass

    installed = installed_prompt("briefing_v2")
    require_installed_prompt(
        installed.version, installed=installed, error=Refused, family="test"
    )
    with pytest.raises(Refused, match="the configured prompt is not the installed test prompt"):
        require_installed_prompt(
            "briefing_v1", installed=installed, error=Refused, family="test"
        )
