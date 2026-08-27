from pathlib import Path

from corridor.extract_matrix import (
    PROMPT_VERSION as MATRIX_PROMPT_VERSION,
    SCHEMA_VERSION as MATRIX_SCHEMA_VERSION,
)
from corridor.extract_sheet import (
    PROMPT_VERSION as SHEET_PROMPT_VERSION,
    SCHEMA_VERSION as SHEET_SCHEMA_VERSION,
)
from corridor.pipeline import extraction_route


def test_a_spreadsheet_uses_the_native_sheet_prompt_version(monkeypatch):
    monkeypatch.setattr("corridor.pipeline.stored_file", lambda document: "/tmp/a.xlsx")

    route = extraction_route(object())

    assert route.effective_prompt_version == SHEET_PROMPT_VERSION
    assert route.schema_version == SHEET_SCHEMA_VERSION
    assert route.extractor_config is not None
    assert route.extractor_config.prompt_version == SHEET_PROMPT_VERSION
    assert route.extractor_config.config_json["request_controls"] == {
        "provider": "native",
        "model_requests": 0,
    }


def test_a_pdf_route_carries_the_client_to_the_matrix_extractor(monkeypatch):
    captured = {}

    def fake_extract(session, document, *, client=None, max_pages=None, **runtime):
        captured["args"] = (session, document, client, max_pages)
        captured["runtime"] = runtime
        return []

    monkeypatch.setattr("corridor.pipeline.stored_file", lambda document: "/tmp/a.pdf")
    monkeypatch.setattr("corridor.extract_matrix.extract_document", fake_extract)

    session = object()
    document = object()
    class Client:
        model = "gpt-zero-row"
        base_url = "https://provider.example/v1"
        effort = "none"
        flex = False

    client = Client()
    route = extraction_route(document, client=client)

    assert route.effective_prompt_version == MATRIX_PROMPT_VERSION
    assert route.schema_version == MATRIX_SCHEMA_VERSION
    assert route.model == "gpt-zero-row"
    assert route.extractor_config is not None
    assert route.extractor_config.prompt_version == MATRIX_PROMPT_VERSION
    assert route.extractor_config.model == "gpt-zero-row"
    assert route.extract(session, document) == []
    assert captured["args"] == (session, document, client, None)


def test_matrix_route_seals_and_uses_repo_prompts_outside_repo_cwd(
    monkeypatch, tmp_path
):
    shadow = tmp_path / "prompts"
    shadow.mkdir()
    (shadow / "matrix_structure_v3.md").write_text("shadow structure")
    (shadow / "matrix_v1.md").write_text("shadow transcription")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("corridor.pipeline.stored_file", lambda document: "/tmp/a.pdf")

    captured = {}

    def fake_extract(_session, _document, **runtime):
        captured.update(runtime)
        return []

    monkeypatch.setattr("corridor.extract_matrix.extract_document", fake_extract)

    class Client:
        model = "gpt-sealed"
        base_url = "https://provider.example/v1"
        effort = "none"
        flex = False

    route = extraction_route(object(), client=Client())
    route.extract(object(), object())

    root = Path(__file__).resolve().parents[1]
    assert captured["structure_system"] == (
        root / "prompts/matrix_structure_v3.md"
    ).read_text()
    assert captured["transcribe_system"] == (
        root / "prompts/matrix_v1.md"
    ).read_text()
    assert "shadow" not in captured["structure_system"]
    assert "shadow" not in captured["transcribe_system"]
