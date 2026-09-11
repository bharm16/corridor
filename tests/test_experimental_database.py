"""Production-database refusal for measurements, replays, and model trials."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from corridor.config import settings
from corridor.db import Session
from corridor.eval import main as extraction_measurement_main
from corridor.experimental_database import (
    ProductionDatabaseRefusal,
    experimental_session,
    require_experimental_database,
)
from corridor.extraction_runs import record_extraction_run
from corridor.models import Candidate, DocPage, Document, Project


@pytest.fixture(scope="module")
def disposable_database(provision_isolated_database):
    with provision_isolated_database("experimental_guard") as database:
        yield database


def _database_url(database) -> str:
    engine = database.session_factory.kw.get("bind")
    assert engine is not None
    return engine.url.render_as_string(hide_password=False)


def test_explicit_disposable_database_is_accepted(disposable_database):
    identity = require_experimental_database(_database_url(disposable_database))

    assert identity.database == disposable_database.name
    assert identity.system_identifier


def test_configured_production_database_is_refused():
    with pytest.raises(ProductionDatabaseRefusal, match="production database"):
        require_experimental_database(settings.database_url)


def test_host_alias_cannot_disguise_the_production_database():
    aliased = settings.database_url.replace("localhost", "127.0.0.1")

    with pytest.raises(ProductionDatabaseRefusal, match="production database"):
        require_experimental_database(aliased)


@pytest.mark.parametrize("database_url", [None, "", "  "])
def test_experimental_database_must_be_explicit(database_url):
    with pytest.raises(ProductionDatabaseRefusal, match="explicit"):
        require_experimental_database(database_url)


def test_injected_session_must_reach_the_named_disposable_database(
    disposable_database,
):
    with Session() as production_session:
        with pytest.raises(ProductionDatabaseRefusal, match="injected session"):
            require_experimental_database(
                _database_url(disposable_database),
                session=production_session,
            )


def test_the_verified_injected_session_is_the_session_the_command_uses(
    disposable_database,
):
    class AlternatingFactory:
        calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                return disposable_database.session_factory()
            return Session()

    factory = AlternatingFactory()
    with experimental_session(
        _database_url(disposable_database),
        session_factory=factory,
    ) as session:
        observed_database = session.scalar(text("select current_database()"))

    assert observed_database == disposable_database.name
    assert factory.calls == 1


def test_extraction_measurement_runs_through_the_real_guarded_entry_point(
    disposable_database, tmp_path
):
    slug = f"experimental-measurement-{uuid4().hex}"
    with disposable_database.session_factory() as setup:
        project = Project(slug=slug, name="Experimental Measurement", is_synthetic=True)
        setup.add(project)
        setup.flush([project])
        document = Document(
            project_id=project.id,
            registry_id=f"measurement-{uuid4().hex}",
            sha256="a" * 64,
            filename="measurement.pdf",
            doc_type="matrix",
            parse_status="parsed",
            pages=1,
        )
        setup.add(document)
        setup.flush([document])
        setup.add(DocPage(document_id=document.id, page_no=1, text="FOC1-1"))
        candidate = Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {"utility_id": "FOC1-1", "external_org": "AT&T"},
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": "FOC1-1",
                        "verified": True,
                        "whole_row": True,
                    }
                ],
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="matrix-v1",
            model="test-model",
            citations_verified=True,
        )
        run = record_extraction_run(
            setup,
            document,
            prompt_version="matrix-v1",
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model="test-model",
            schema_version="candidate-v1",
            allow_unsealed_legacy=True,
        )
        run_id = run.id
        setup.commit()

    reference = tmp_path / "reference.csv"
    reference.write_text("source_ref,page\nFOC1-1,1\n")

    assert extraction_measurement_main(
        [
            slug,
            str(reference),
            f"--database-url={_database_url(disposable_database)}",
            f"--extraction-run={run_id}",
        ],
        output_dir=tmp_path,
    ) == 0
    assert len(list(tmp_path.glob(f"extraction-measurement-{slug}-*.json"))) == 1
