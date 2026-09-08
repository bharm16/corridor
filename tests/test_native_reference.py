"""Method-specific references come from authored source bytes, never scored rows."""

from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.eval import (
    NothingToMeasure,
    load_gold,
    measure,
    verified_machine_reference_scope,
)
from corridor.gold import (
    SpentHoldout,
    author_machine_gold,
    gold_csv,
    machine_gold_paths,
    machine_reference_scope_path,
    publish_machine_reference,
    replay_machine_reference,
)
from corridor.models import Candidate, Document, Project
from corridor.reference_methods import LEGACY_METHOD, NATIVE_METHOD, SPENT_SOURCE_HASHES
from pdf_fixture_support import PdfFixture
from test_eval import record_run


@pytest.fixture(autouse=True)
def stored_source_fixture(monkeypatch):
    monkeypatch.setattr(
        "corridor.gold.stored_file",
        lambda document: getattr(document, "_pdf_path", None),
    )


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    row = Project(
        slug="native-reference-test", name="Native reference test", is_synthetic=True
    )
    session.add(row)
    session.flush()
    return row


ROWS = [
    ["", "", "RECOMMENDED RESOLUTION", "", ""],
    ["Owner", "Utility ID", "Relocation", "Protection in Place", "Notes"],
    ["Owner A", "A-1", "X", "", ""],
    ["Owner B", "A-1", "", "X", ""],
    ["Owner C", "A-3", "X", "X", ""],
    ["", "A-4", "", "", "Not Used"],
    ["", "A-5", "", "", ""],
]


def draw(page, rows, *, top=35):
    for row_number, row in enumerate(rows):
        for column, value in enumerate(row):
            x, y = 25 + column * 175, top + row_number * 36
            page.rect((x, y, x + 175, y + 36))
            if value:
                page.text((x + 5, y + 22), value, fontsize=10)


def source(session, project, tmp_path, *, rows=ROWS, extra=None, additional_pages=()):
    fixture = PdfFixture()
    page = fixture.add_page(width=950, height=900)
    draw(page, rows)
    if extra:
        draw(page, extra, top=400)
    for page_rows in additional_pages:
        draw(fixture.add_page(width=950, height=900), page_rows)
    path = fixture.save(tmp_path / "native-reference.pdf")
    document = Document(
        project_id=project.id,
        filename=path.name,
        sha256=sha256(path.read_bytes()).hexdigest(),
        doc_type="matrix",
        pages=1 + len(additional_pages),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    document._pdf_path = str(path)
    return document, path


def author(session, project, tmp_path):
    return author_machine_gold(
        session,
        project.id,
        directory=tmp_path / "references",
        method=NATIVE_METHOD.name,
    )


def publish(session, project, tmp_path):
    gold = author(session, project, tmp_path)
    paths = publish_machine_reference(
        project.slug, gold, directory=tmp_path / "references"
    )
    return gold, paths


def test_native_reference_enumerates_authored_marks_and_preserves_duplicate_ids(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    gold, (csv, sidecar, scope) = publish(session, project, tmp_path)
    assert [(row.source_ref, row.critical) for row in gold.rows] == [
        ("A-1", "yes"),
        ("A-1", "no"),
        ("A-3", ""),
    ]
    assert (gold.retired, gold.empty_slots) == (1, 1)
    assert csv.name == f"{project.slug}.{NATIVE_METHOD.name}-v1.machine.csv"
    assert [row.source_ref for row in load_gold(csv)] == ["A-1", "A-1", "A-3"]
    metadata = json.loads(scope.read_text())
    assert metadata["method"] == NATIVE_METHOD.name
    assert metadata["limitations"] == list(NATIVE_METHOD.limitations)
    assert metadata["documents"] == [
        {"sha256": document.sha256, "filename": document.filename}
    ]
    assert (
        metadata["native_authoring"]["readings"][0]["document_sha256"]
        == document.sha256
    )
    assert "shared" in sidecar.read_text().lower()
    assert "proposal population" in sidecar.read_text()


def test_changing_scored_candidates_cannot_change_reference_authoring(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    first = author(session, project, tmp_path)
    candidate = Candidate(
        project_id=project.id,
        source_document_id=document.id,
        kind="dependency",
        payload_json={"fields": {"utility_id": "MODEL-999"}},
        source_pages=[1],
    )
    session.add(candidate)
    session.flush()
    second = author(session, project, tmp_path)
    candidate.payload_json = {
        "fields": {"utility_id": "MODEL-888", "resolution_strategy": "invented"}
    }
    session.flush()
    third = author(session, project, tmp_path)
    assert gold_csv(first) == gold_csv(second) == gold_csv(third)
    assert first.authoring_json == second.authoring_json == third.authoring_json
    assert "MODEL" not in gold_csv(third)


def test_multiple_source_authored_grids_refuse_instead_of_dropping_a_denominator(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path, extra=ROWS[:4])
    with pytest.raises(ValueError, match="multiple eligible"):
        author(session, project, tmp_path)
    assert not (tmp_path / "references").exists()
    assert document.sha256


def test_unrelated_larger_grid_cannot_win_by_row_count(session, project, tmp_path):
    # A larger unrelated page grid with no WSDOT header is outside the recipe.
    fixture = PdfFixture()
    page = fixture.add_page(width=950, height=900)
    draw(
        page,
        [
            ["Code", "Amount", "", "", ""],
            *[[str(n), "100", "", "", ""] for n in range(9)],
        ],
    )
    draw(page, ROWS[:4], top=440)
    path = fixture.save(tmp_path / "unrelated.pdf")
    document = Document(
        project_id=project.id,
        filename=path.name,
        sha256=sha256(path.read_bytes()).hexdigest(),
        doc_type="matrix",
    )
    session.add(document)
    session.flush()
    document._pdf_path = str(path)
    # If reconstruction merges the physical tables, the supported six-row
    # header window refuses the combined shape rather than inventing a scope.
    try:
        gold = author(session, project, tmp_path)
    except ValueError as error:
        assert "anchored grid without supported headings" in str(error)
    else:
        assert [row.source_ref for row in gold.rows] == ["A-1", "A-1"]


def test_complete_target_refuses_before_reader_and_legacy_default_paths_stay_stable(
    session, project, tmp_path, monkeypatch
):
    document, _ = source(session, project, tmp_path)
    _, paths = publish(session, project, tmp_path)
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr(
        "corridor.native_reference.read_reference_document",
        lambda *a, **k: pytest.fail("reader called"),
    )
    with pytest.raises(SpentHoldout, match="already authored"):
        author(session, project, tmp_path)
    assert [path.read_bytes() for path in paths] == before
    assert machine_gold_paths(project.slug)[0].name == f"{project.slug}.machine.csv"
    assert document.sha256


@pytest.mark.parametrize("renamed", [False, True])
def test_spent_sources_refuse_before_reader_regardless_of_project_or_path(
    session, project, tmp_path, monkeypatch, renamed
):
    document, _ = source(session, project, tmp_path)
    document.sha256 = next(iter(SPENT_SOURCE_HASHES))
    if not renamed:
        project.slug = "wsdot-9540"
    session.flush()
    monkeypatch.setattr(
        "corridor.native_reference.read_reference_document",
        lambda *a, **k: pytest.fail("spent source read"),
    )
    with pytest.raises(SpentHoldout, match="spent holdout"):
        author_machine_gold(
            session,
            project.id,
            directory=tmp_path / "new-output",
            method=NATIVE_METHOD.name,
        )


def test_native_partial_publication_retries_exact_bytes_and_refuses_divergence(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    gold = author(session, project, tmp_path)
    count = []

    def interrupted(path, data):
        count.append(path)
        if len(count) == 2:
            raise RuntimeError("interrupted")
        path.write_bytes(data)

    with pytest.raises(RuntimeError, match="interrupted"):
        publish_machine_reference(
            project.slug, gold, directory=tmp_path / "references", publisher=interrupted
        )
    first = count[0].read_bytes()
    retried = author(session, project, tmp_path)
    paths = publish_machine_reference(
        project.slug, retried, directory=tmp_path / "references"
    )
    assert paths[0].read_bytes() == first
    divergent = tmp_path / "divergent"
    divergent.mkdir()
    csv, _ = machine_gold_paths(
        project.slug, directory=divergent, method=NATIVE_METHOD.name
    )
    csv.write_text("bad bytes")
    with pytest.raises(SpentHoldout, match="diverges"):
        publish_machine_reference(project.slug, gold, directory=divergent)
    assert csv.read_text() == "bad bytes" and document.sha256


def test_native_reference_replays_exactly_and_refuses_changed_recipe_or_source(
    session, project, tmp_path, monkeypatch
):
    document, path = source(session, project, tmp_path)
    gold, paths = publish(session, project, tmp_path)
    before = [p.read_bytes() for p in paths]
    assert replay_machine_reference(session, project.id, paths[0])["replayed"] is True
    assert [p.read_bytes() for p in paths] == before
    import corridor.native_reference as native

    original = native.authoring_identity
    monkeypatch.setattr(
        native, "authoring_identity", lambda: {**original(), "recipe_sha256": "0" * 64}
    )
    monkeypatch.setattr(
        native,
        "read_reference_document",
        lambda *a, **k: pytest.fail("unavailable recipe read"),
    )
    with pytest.raises(ValueError, match="recipe/configuration is unavailable"):
        replay_machine_reference(session, project.id, paths[0])
    monkeypatch.undo()
    monkeypatch.setattr(
        "corridor.gold.stored_file", lambda record: getattr(record, "_pdf_path", None)
    )
    path.write_bytes(path.read_bytes() + b"changed source")
    with pytest.raises(ValueError, match="bytes do not match"):
        replay_machine_reference(session, project.id, paths[0])


def test_archived_native_method_loads_without_pdf_authoring_engines(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    _, (csv, _, scope) = publish(session, project, tmp_path)
    payload = json.loads(scope.read_text())
    payload["native_authoring"]["readings"][0]["reader_identity"]["native_layer"][
        "engine_version"
    ] = "retired-uninstalled-build"
    scope.write_text(json.dumps(payload))
    script = """
import sys
class AbsentEngine:
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'pymupdf','pypdfium2'} or fullname == 'corridor.native_reference':
   raise ModuleNotFoundError('authoring engine is unavailable')
sys.meta_path.insert(0, AbsentEngine())
from pathlib import Path
from types import SimpleNamespace
from corridor.eval import load_gold, verified_machine_reference_scope
csv, scope, slug, digest = sys.argv[1:]
rows=load_gold(csv)
result=verified_machine_reference_scope(scope,reference_path=csv,reference_bytes=Path(csv).read_bytes(),project_slug=slug,documents=(SimpleNamespace(id=9001,sha256=digest),))
assert len(rows)==3 and result.method=='native-pdf-cell-grid'
assert result.native_authoring['readings'][0]['reader_identity']['native_layer']['engine_version']=='retired-uninstalled-build'
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(csv),
            str(scope),
            project.slug,
            document.sha256,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_evaluation_names_loaded_native_method_and_retains_exact_run_scope(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    _, (csv, _, scope) = publish(session, project, tmp_path)
    run = record_run(session, project, document, "A-1")
    result = measure(
        session,
        project.slug,
        gold_path=csv,
        reference_manifest_path=scope,
        extraction_run_ids={run.id},
    )
    assert result.reference_scope.method == NATIVE_METHOD.name
    assert NATIVE_METHOD.name in result.result.reference_label
    assert result.result.gold_total == 3 and result.result.matched == 1


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("method", "invented", "method/version"),
        ("method_version", "2", "method/version"),
        ("limitations", [], "limitations"),
        ("reference_sha256", "0" * 64, "reference SHA-256"),
        ("native_authoring", {}, "authoring provenance"),
    ],
)
def test_native_method_contract_refuses_altered_scope(
    session, project, tmp_path, field, value, match
):
    document, _ = source(session, project, tmp_path)
    _, (csv, _, scope) = publish(session, project, tmp_path)
    payload = json.loads(scope.read_text())
    payload[field] = value
    scope.write_text(json.dumps(payload))
    with pytest.raises(NothingToMeasure, match=match):
        verified_machine_reference_scope(
            scope,
            reference_path=csv,
            reference_bytes=csv.read_bytes(),
            project_slug=project.slug,
            documents=(document,),
        )


def test_fresh_evaluation_cannot_bypass_spent_source_by_renaming(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    document.sha256 = next(iter(SPENT_SOURCE_HASHES))
    session.flush()
    run = record_run(session, project, document, "A-1")
    with pytest.raises(NothingToMeasure, match="spent holdout"):
        measure(session, project.slug, extraction_run_ids={run.id})


def test_cli_selects_native_method_and_replays_its_manifest(
    session, project, tmp_path, monkeypatch, capsys
):
    from corridor.gold import main

    document, _ = source(session, project, tmp_path)
    monkeypatch.setattr("corridor.db.WorkerSession", lambda: nullcontext(session))
    directory = tmp_path / "references"
    assert (
        main(
            [
                project.slug,
                "--author",
                f"--method={NATIVE_METHOD.name}",
                f"--directory={directory}",
            ]
        )
        == 0
    )
    csv, _ = machine_gold_paths(
        project.slug, directory=directory, method=NATIVE_METHOD.name
    )
    assert main([project.slug, "--replay", str(csv)]) == 0
    assert '"replayed": true' in capsys.readouterr().out and document.sha256


def test_first_write_wsdot_triplets_are_unchanged():
    root = Path(__file__).resolve().parents[1] / "gold"
    expected = {
        "wsdot-9424.machine.csv": "94720ed31bc66616479a5de0d2448774569c50b22cbdb4768d31090c1566a111",
        "wsdot-9424.machine.md": "59ef121cc1404010606d66016b888869b330825c6c6e66f7f4425f3b3e1fa0e5",
        "wsdot-9424.machine.scope.json": "bac9b49ae4e6f7b67b02583eb60fa2a8865199c048d58412b47fee2a3ba01d62",
        "wsdot-9540.machine.csv": "97323bebdedcf3bd6db2d43be3c55cdaa0e662b9125b5835a52962e0e18b5bd9",
        "wsdot-9540.machine.md": "0dda2b0170e86f3e7f463ab6da0774223ea096de307c0a3db7ce97ccf214dcf6",
        "wsdot-9540.machine.scope.json": "f6652482186d3e772b248979707720440168d583ee2a928b1a6bc6c2e9a43c7b",
    }
    assert {
        name: sha256((root / name).read_bytes()).hexdigest() for name in expected
    } == expected
    assert {
        doc["sha256"]
        for doc in json.loads((root / "wsdot-9540.machine.scope.json").read_text())[
            "documents"
        ]
    } == SPENT_SOURCE_HASHES


def test_unmaterializable_nonempty_cells_cannot_shrink_the_reference(
    session, project, tmp_path, monkeypatch
):
    document, _ = source(session, project, tmp_path)
    import corridor.native_reference as native

    original = native.native_segment_values

    def omitted(reading):
        return tuple(
            value
            for value in original(reading)
            if not (value.kind == "pdf_cell" and value.exact_text == "Owner A")
        )

    monkeypatch.setattr(native, "native_segment_values", omitted)
    with pytest.raises(
        ValueError, match="nonempty reader cells without unique typed source values"
    ):
        author(session, project, tmp_path)
    assert document.sha256 and not (tmp_path / "references").exists()


def test_unrelated_legacy_code_and_logs_do_not_define_native_reference_replay(
    session, project, tmp_path, monkeypatch
):
    document, _ = source(session, project, tmp_path)
    _, paths = publish(session, project, tmp_path)
    original = Path.read_bytes
    unrelated_reads = []

    def changed(path):
        data = original(path)
        if path.name in {
            "gold.py",
            "adjudicate.py",
            "models.py",
            "vocabulary.py",
            "token_layers.py",
            "LOOP-LOG.md",
        }:
            unrelated_reads.append(path.name)
            return data + b"\nUnrelated legacy removal or audit log change.\n"
        return data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert replay_machine_reference(session, project.id, paths[0])["replayed"]
    assert unrelated_reads == [] and document.sha256


def test_changed_recorded_reader_configuration_refuses_replay_but_not_archive_loading(
    session, project, tmp_path
):
    document, _ = source(session, project, tmp_path)
    _, (csv, _, scope) = publish(session, project, tmp_path)
    payload = json.loads(scope.read_text())
    payload["native_authoring"]["readings"][0]["reader_identity"]["native_layer"][
        "engine_version"
    ] = "earlier-build"
    scope.write_text(json.dumps(payload))
    archived = verified_machine_reference_scope(
        scope,
        reference_path=csv,
        reference_bytes=csv.read_bytes(),
        project_slug=project.slug,
        documents=(document,),
    )
    assert archived.method == NATIVE_METHOD.name
    with pytest.raises(
        ValueError, match="source/reader configuration or result changed"
    ):
        replay_machine_reference(session, project.id, csv)


def test_cli_bad_replay_contract_refuses_without_reading(
    session, project, tmp_path, monkeypatch, capsys
):
    from corridor.gold import main

    document, _ = source(session, project, tmp_path)
    _, (csv, _, scope) = publish(session, project, tmp_path)
    payload = json.loads(scope.read_text())
    payload["method_version"] = "unknown"
    scope.write_text(json.dumps(payload))
    monkeypatch.setattr("corridor.db.WorkerSession", lambda: nullcontext(session))
    monkeypatch.setattr(
        "corridor.native_reference.read_reference_document",
        lambda *a, **k: pytest.fail("unknown reader invoked"),
    )
    assert main([project.slug, "--replay", str(csv)]) == 1
    assert "unsupported method/version" in capsys.readouterr().err and document.sha256


def test_actual_archived_scoring_has_no_authoring_engine_dependency(
    runtime_database, tmp_path
):
    with runtime_database.session_factory() as session:
        project = Project(
            slug="native-reference-without-engines",
            name="Archived reference",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        document, _ = source(session, project, tmp_path)
        _, (csv, _, scope) = publish(session, project, tmp_path)
        run = record_run(session, project, document, "A-1", "A-1", "A-3")
        run_id, slug = run.id, project.slug
        database_url = session.get_bind().url.render_as_string(hide_password=False)
        session.commit()
    script = """
import os, sys
class AbsentEngine:
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'pymupdf','fitz','pypdfium2'} or fullname == 'corridor.native_reference':
   raise ModuleNotFoundError('historical authoring engine is unavailable')
sys.meta_path.insert(0, AbsentEngine())
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from corridor.eval import measure, artifact
from datetime import datetime, timezone
csv, manifest, slug, run_id = sys.argv[1:]
engine=create_engine(os.environ['NATIVE_REFERENCE_TEST_DATABASE_URL'])
try:
 with Session(engine) as session:
  result=measure(session,slug,gold_path=csv,reference_manifest_path=manifest,extraction_run_ids={int(run_id)})
  assert result.result.matched == result.result.gold_total == 3
  assert result.result.critical_matched == result.result.critical_gold_total == 1
  assert result.reference_scope.method == 'native-pdf-cell-grid'
  written=artifact(result.result,reference_description=result.reference_description,ran_at=datetime.now(timezone.utc),extraction_runs=result.extraction_runs,reference_scope=result.reference_scope)
  assert written['reference_scope']['method'] == 'native-pdf-cell-grid'
  assert written['extraction_run_ids'] == [int(run_id)]
finally:
 engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(csv), str(scope), slug, str(run_id)],
        env={**os.environ, "NATIVE_REFERENCE_TEST_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_shared_completion_helpers_have_one_engine_independent_owner():
    from corridor import extraction_run_queries, extraction_runs

    for name in ("completion_predicate", "is_completed_run", "completed_document_ids"):
        assert getattr(extraction_runs, name) is getattr(extraction_run_queries, name)


def test_spanning_owner_cannot_make_a_second_source_row_disappear(
    session, project, tmp_path
):
    from corridor.native_reference import authoring_identity
    from corridor.token_layers import read_native_pdf
    from corridor.reader_segments import native_segment_values

    fixture = PdfFixture()
    page = fixture.add_page(width=950, height=900)
    rows = [ROWS[0], ROWS[1], ["Owner A", "A-1", "X", "", ""], ["", "A-2", "X", "", ""]]
    for row_no, row in enumerate(rows):
        for column, words in enumerate(row):
            if column == 0 and row_no == 3:
                continue
            x, y = 25 + column * 175, 35 + row_no * 36
            height = 72 if column == 0 and row_no == 2 else 36
            page.rect((x, y, x + 175, y + height))
            if words:
                page.text((x + 5, y + 22), words, fontsize=10)
    path = fixture.save(tmp_path / "spanning-owner.pdf")
    document = Document(
        project_id=project.id,
        filename=path.name,
        sha256=sha256(path.read_bytes()).hexdigest(),
        doc_type="matrix",
        pages=1,
    )
    session.add(document)
    session.flush()
    document._pdf_path = str(path)
    original_bytes = path.read_bytes()
    reading = read_native_pdf(path, source_sha256=document.sha256)
    cells = [
        value for value in native_segment_values(reading) if value.kind == "pdf_cell"
    ]
    assert next(cell for cell in cells if cell.exact_text == "Owner A").row_span == 2
    assert {"A-1", "A-2"} <= {cell.exact_text for cell in cells}
    identity = authoring_identity()
    for _ in range(2):
        with pytest.raises(ValueError, match="does not support row-spanning cells"):
            author(session, project, tmp_path)
        assert not (tmp_path / "references").exists()
    assert path.read_bytes() == original_bytes and authoring_identity() == identity


def test_populated_continuation_without_headers_refuses_before_publication(
    session, project, tmp_path
):
    document, path = source(
        session,
        project,
        tmp_path,
        rows=ROWS[:3],
        additional_pages=([["Owner D", "CONT-2", "X", "", ""]],),
    )
    original = path.read_bytes()
    for _ in range(2):
        with pytest.raises(
            ValueError, match="page 2 has populated content without supported headings"
        ):
            author(session, project, tmp_path)
        assert not (tmp_path / "references").exists()
    assert path.read_bytes() == original and document.pages == 2


def test_supported_continuation_repeats_headers_without_repeating_group_anchor(
    session, project, tmp_path
):
    document, _ = source(
        session,
        project,
        tmp_path,
        rows=ROWS[:3],
        additional_pages=([ROWS[1], ["Owner D", "CONT-2", "", "X", ""]],),
    )
    gold, (csv, _, _) = publish(session, project, tmp_path)
    assert [(row.source_ref, row.page, row.critical) for row in gold.rows] == [
        ("A-1", 1, "yes"),
        ("CONT-2", 2, "no"),
    ]
    assert replay_machine_reference(session, project.id, csv)["replayed"]
    assert document.pages == 2
