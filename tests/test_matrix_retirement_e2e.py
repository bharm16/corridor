"""A public corpus PDF reaches the production route with the engines absent.

Only HTTP is doubled: its answer is the frozen #737 observation, used to
exercise plumbing without transmitting source material. The synthetic test
database simulates a production selection; no fixture receipt is qualification
evidence. A seeded legacy citation exercises retained integrity, not a claim
that a removed reader can reconstruct its original physical location.
"""

from base64 import b64decode
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select, text

from corridor.config import Settings
from corridor.evidence_citations import cite_source_segments, evidence_quotation
from corridor.extract_project import extract_project
from corridor.ingest import ingest_document
from corridor.models import EvidenceLink, ExtractionRun, FactSource, PipelineObservation, SourceSegment
from corridor.native_pipeline import register_pipeline_configuration
from corridor.object_storage import store_bytes
from corridor.pipeline import production_extraction_routes
from corridor.pipeline_contracts import AcceptedEvidence, MaintainerAcceptance, PipelineScope, content_digest
from corridor.pipeline_qualification import record_acceptance, select_qualified_pipeline
from corridor.retained_history import replay_retained_reading
from corridor.source_append import SegmentValues, append_source_segments
from corridor.source_segments import dereference_source_segment
from test_native_matrix import project
from test_native_pipeline import ACTOR
from test_native_provider_boundary import RecordingTransport, _body, _experiment, _request


FIXTURES = Path(__file__).parent / "fixtures/native-matrix-retirement"
pytestmark = pytest.mark.slow


@pytest.mark.parametrize("provider_refuses", [False, True])
def test_corpus_ingest_selected_extraction_and_retained_citation_without_engines(
    session, project, tmp_path, monkeypatch, capsys, provider_refuses,
):
    retained = json.loads((FIXTURES / "retained-request.json").read_text())
    source = FIXTURES / retained["source"]
    assert sha256(source.read_bytes()).hexdigest() == retained["source_sha256"]
    store_bytes(source.read_bytes(), sha256=retained["source_sha256"], suffix=".pdf")
    document = ingest_document(session, project_id=project.id, path=source,
        doc_type="matrix", images_dir=tmp_path / "ingest")
    assert document.parse_status == "parsed"

    # A compatibility fixture, retained before the new extraction is invoked.
    # Its locator intentionally requires the retired reader. Only the stored
    # reading's integrity is claimed here; no cross-reader rebinding occurs.
    words = "Puget Sound Energy - Gas"
    [old] = append_source_segments(session, project_id=project.id, document_id=document.id,
        recorded_verbal_origin_id=None, segments=[SegmentValues(kind="prose_span",
            exact_text=words, content_sha256=sha256(words.encode()).hexdigest(),
            ordinal=1, page_no=1, start_offset=0, end_offset=len(words))])
    link = EvidenceLink(document_id=document.id, page_no=1, quote=words)
    session.add(link)
    session.flush()
    cite_source_segments(session, link, (old,))
    original = evidence_quotation(session, link)

    from corridor.native_provider_boundary import TransportOutcome

    response = (TransportOutcome(status=403, error="test provider refusal") if provider_refuses else
                _body(retained["answer"], response_id="test-replay-not-a-provider-observation"))
    transport = RecordingTransport([response])
    transport.close = lambda: None
    monkeypatch.setattr("corridor.native_matrix_runtime.live_transport", lambda *args: transport)
    record = _experiment(dataset=project.slug, source_sha256s=frozenset({document.sha256}))
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(json.dumps({
        "deployment": "test-retirement", "authorization": record.as_dict(),
        "request": _request(project=project.slug).as_dict(),
        "budget": {"max_calls": 1, "max_pages": 1, "max_total_tokens": 10000},
        "source_sha256s": [document.sha256], "campaign": "synthetic-http-boundary-test",
    }))
    settings = Settings(_env_file=None, CORRIDOR_ENVIRONMENT="test-retirement",
        CORRIDOR_NATIVE_MATRIX_RUNTIME_FILE=str(runtime_file),
        CORRIDOR_NATIVE_MATRIX_OUTPUT_DIR=str(tmp_path / "native"))
    monkeypatch.setattr("corridor.pipeline.settings", settings)
    monkeypatch.setattr("corridor.config.settings", settings)
    from corridor.pipeline_qualification_cli import main

    configuration_path = tmp_path / "configuration.json"
    assert main(["configuration", "--output", str(configuration_path)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert transport.payloads == []
    configuration = json.loads(configuration_path.read_text())
    assert content_digest(configuration) == manifest["configuration_sha256"]
    scope = PipelineScope(deployment=settings.environment, source_sha256s=(document.sha256,),
        corpus_version="WSDOT-9540-gas", corpus_sha256=document.sha256)
    registered = register_pipeline_configuration(session, configuration)
    accepted = record_acceptance(session, MaintainerAcceptance(
        decision="Synthetic production-route regression", configuration_sha256=registered.configuration_sha256,
        implementation_revision=configuration["code_revision"], scope_sha256=scope.identity,
        evidence=(AcceptedEvidence(name="Retained request", reference="tests/fixtures/native-matrix-retirement/retained-request.json",
                                   summary="Frozen request and answer exercise the complete route."),),
        limits=("The HTTP response is a test double; no fresh accuracy or provider observation is claimed.",),
        words="Select this configuration in the disposable test database only.", accepted_on="2026-09-08",
    ), project_id=project.id, scope=scope, actor=ACTOR)
    select_qualified_pipeline(session, acceptance_id=accepted.id, actor=ACTOR,
        reason="Exercise production routing in the test database", expected_selection_id=None)

    # The actual worker capability can read selection and append the native
    # capture, but cannot approve itself or change the accepted record.
    with session.begin_nested():
        session.execute(text("set local role corridor_worker"))
        try:
            with production_extraction_routes() as select_route:
                [outcome] = extract_project(session, project, select_route=select_route, commit=False)
        finally:
            session.execute(text("reset role"))
    if provider_refuses:
        from corridor.models import Candidate, Fact, ProcessingArtifact

        assert outcome.status == "failed"
        [run] = session.scalars(select(ExtractionRun)).all()
        [observation] = session.scalars(select(PipelineObservation)).all()
        assert run.outcome == "failed" and observation.extraction_run_id is None
        assert str(observation.id) in run.error_detail
        assert session.scalar(select(func.count()).select_from(ProcessingArtifact)) > 0
        assert session.scalar(select(func.count()).select_from(Fact)) == 0
        assert session.scalar(select(func.count()).select_from(Candidate)) == 0
        assert len(transport.payloads) == 1
        assert evidence_quotation(session, link) == original
        return
    assert outcome.status == "extracted", outcome.detail
    assert outcome.rows == retained["expected_candidates"]
    assert outcome.effective_prompt_version == "matrix_structure_ids_v1"
    assert session.scalar(select(func.count()).select_from(ExtractionRun)) == 1
    observation = session.scalar(select(PipelineObservation))
    assert observation.extraction_run_id == outcome.extraction_run_id
    assert len(transport.payloads) == 1
    payload = transport.payloads[0]
    assert sha256(payload["instructions"].encode()).hexdigest() == retained["system_sha256"]
    assert content_digest(payload["text"]["format"]["schema"]) == retained["schema_sha256"]
    content = payload["input"][0]["content"]
    assert content[0]["text"] == retained["user"]
    image_bytes = b64decode(content[1]["image_url"].split(",", 1)[1])
    rendered = json.loads(observation.receipt_text)["chain"]["model_context"]["1"]
    # The historical PNG hash remains archived. This selected configuration
    # includes the current platform/PDFium build, so its actual fresh render
    # must be the image sent, not a PNG encoded on another platform.
    assert sha256(image_bytes).hexdigest() == rendered["sha256"]
    assert rendered["dpi"] == 110
    with Image.open(BytesIO(image_bytes)) as image:
        assert image.format == "PNG"
        assert image.size == (1870, 1210)
    assert evidence_quotation(session, link) == original
    replay = replay_retained_reading(old, document=document, path=source)
    assert replay.exact_text == words and replay.original_bytes_verified
    # The new Facts cite durable native cells that dereference from source.
    cells = session.scalars(select(SourceSegment).join(FactSource,
        FactSource.source_segment_id == SourceSegment.id).where(SourceSegment.kind == "pdf_cell")).all()
    assert cells
    segment = next(cell for cell in cells if cell.exact_text == words)
    assert dereference_source_segment(document, segment, source) == words
    from corridor.admission import load_project
    from corridor.models import ActiveExtractionRun, Dependency

    load_project(session, project.id)
    assert session.scalar(select(func.count()).select_from(ActiveExtractionRun)) == 0
    assert session.scalar(select(func.count()).select_from(Dependency)) == 0
