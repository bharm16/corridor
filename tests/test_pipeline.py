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


def test_a_pdf_route_carries_the_client_to_the_matrix_extractor(monkeypatch):
    captured = {}

    def fake_extract(session, document, *, client=None, max_pages=None):
        captured["args"] = (session, document, client, max_pages)
        return []

    monkeypatch.setattr("corridor.pipeline.stored_file", lambda document: "/tmp/a.pdf")
    monkeypatch.setattr("corridor.extract_matrix.extract_document", fake_extract)

    session = object()
    document = object()
    class Client:
        model = "gpt-zero-row"

    client = Client()
    route = extraction_route(document, client=client)

    assert route.effective_prompt_version == MATRIX_PROMPT_VERSION
    assert route.schema_version == MATRIX_SCHEMA_VERSION
    assert route.model == "gpt-zero-row"
    assert route.extract(session, document) == []
    assert captured["args"] == (session, document, client, None)
