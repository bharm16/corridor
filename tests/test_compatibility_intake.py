"""The customer-approved non-authoritative compatibility lane (#561).

Every workbook here is synthetic (ADR-0046). The lane is deliberately
database-free and calls no model, so most of these tests need neither: they
stage to a temporary content-addressed store and read the receipt back. The one
test that proves "a full run creates zero authoritative rows" opens a separate
owner connection to the configured database and counts the spine and release
tables around a full run.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import date
from unittest.mock import patch

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

import corridor.compatibility_intake as compatibility_intake
from corridor.compatibility_intake import (
    CompatibilityIntakeRefused,
    run_compatibility_intake,
)
from corridor.config import settings
from corridor.field_mapping_manifest import (
    DEMO_EXTERNAL_REFERENCES,
    MappingDeclaration,
)
from corridor.native_provider_boundary import CustomerAuthorization
from corridor.object_storage import content_key, content_store, digest_bytes

CUSTOMER = "Lone Star Transit Authority"
PROJECT = "sr-bl-pilot"
OPERATOR = "local:compatibility-operator"
ENVIRONMENT = "compatibility-lane-dev"
DELETION_DATE = date(2026, 12, 31)

# The partner's own data-dictionary headings, in the reader's role vocabulary
# (#597). Read through this profile, "UCM Record ID" and "Record URL" carry
# reference roles; read with nothing declared they would be unknown columns.
DEMO_HEADINGS = MappingDeclaration(
    external_references=DEMO_EXTERNAL_REFERENCES
).external_reference_headings

HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Size",
    "Material",
    "Station Origin",
    "Start Station",
    "End Station",
    "Resolution Strategy Selected (from Resolution Alternatives)",
    "Promised For",
    "Action Due Date",
    "Comment",
    "UCM Record ID",
    "Record URL",
    "Early TxDOT Utility Activity",
]

ROWS = [
    ["UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel", "SR-BL",
     "1149+00", "1150+00", "Relocate", "2026-03-01", "2026-02-01",
     "pole at station", "UCM-1001", "https://ucm.example/records/1001", "Yes"],
    ["UC-2", "City of Austin", "Water", "8 in", "PVC", "SR-BL",
     "1160+00", "1161+00", "Adjust", "2026-04-01", "2026-03-01",
     "", "UCM-1002", "", ""],
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the content-addressed store at a temporary directory."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _workbook_bytes(tmp_path, name="ucm.xlsx") -> bytes:
    """One synthetic UCM workbook with a second, not-adopted sheet."""

    path = tmp_path / name
    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(list(HEADINGS))
    for row in ROWS:
        sheet.append(list(row))
    book.create_sheet("Drop-Down Lists").append(["Drop-Down Lists"])
    book.save(path)
    return path.read_bytes()


def _authorization(
    source_sha256s,
    *,
    customer=CUSTOMER,
    projects=(PROJECT,),
    stages=("compatibility",),
) -> CustomerAuthorization:
    """A #522 instance reduced to the fields the lane matches. No model budget."""

    return CustomerAuthorization(
        record_id="auth-561-synthetic",
        customer=customer,
        projects=frozenset(projects),
        source_classes=frozenset({"native_matrix"}),
        purposes=frozenset({"compatibility-reading"}),
        stages=frozenset(stages),
        source_sha256s=frozenset(source_sha256s),
        max_calls=0,
        max_pages=0,
        max_total_tokens=0,
        posture_identity="not-applicable-no-model",
        posture_digest="0" * 64,
        retention_disclosed=True,
        signed_by="local:pilot-authorizer",
        signed_on="2026-09-08",
    )


# --- Happy path -------------------------------------------------------------


def test_an_authorized_run_reports_and_stores_the_operations_reading(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    run = run_compatibility_intake(
        body,
        "ucm.xlsx",
        authorization=_authorization([digest_bytes(body)]),
        customer=CUSTOMER,
        project=PROJECT,
        operator=OPERATOR,
        environment=ENVIRONMENT,
        deletion_date=DELETION_DATE,
        external_references=DEMO_HEADINGS,
    )

    assert run.outcome == "reported"
    reading = run.reading
    assert reading is not None
    assert reading.adopted_sheet == "Utility Conflicts"
    assert [sheet.name for sheet in reading.worksheets] == [
        "Utility Conflicts",
        "Drop-Down Lists",
    ]
    assert [sheet.disposition for sheet in reading.worksheets] == [
        "adopted",
        "not_adopted",
    ]
    mapped = {column.field: column.column for column in reading.column_mapping}
    assert mapped["utility_id"] == "A"
    assert mapped["external_org"] == "B"
    assert [column.heading for column in reading.unknown_columns] == [
        "Early TxDOT Utility Activity"
    ]
    assert reading.round_trip.clean
    assert reading.resolved

    # The source workbook was staged, and the receipt is a distinct object.
    assert content_store().resolve(digest_bytes(body)) is not None
    stored = run.receipt.stored_path.read_bytes()
    assert digest_bytes(stored) == run.receipt.receipt_sha256
    assert run.receipt.key == content_key(run.receipt.receipt_sha256, ".json")

    payload = json.loads(stored)
    assert payload["kind"] == "compatibility-intake-receipt"
    assert payload["outcome"] == "reported"
    assert payload["environment"] == ENVIRONMENT
    assert payload["operator"] == OPERATOR
    assert payload["deletion_date"] == DELETION_DATE.isoformat()
    assert payload["source"]["content_sha256"] == digest_bytes(body)
    assert payload["authorization"]["record_id"] == "auth-561-synthetic"
    assert payload["capability"]["resolved"] is True
    assert payload["capability"]["row_count"] == len(ROWS)
    assert payload["report"]["adopted_sheet"] == "Utility Conflicts"


# --- Refusals: no side effect but the refusal receipt -----------------------


def _assert_nothing_staged(body: bytes) -> None:
    """The source bytes never reached the store on a refusal."""

    assert content_store().resolve(digest_bytes(body)) is None


def test_a_run_refuses_when_the_authorization_is_absent(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    with pytest.raises(CompatibilityIntakeRefused) as refused:
        run_compatibility_intake(
            body,
            "ucm.xlsx",
            authorization=None,
            customer=CUSTOMER,
            project=PROJECT,
            operator=OPERATOR,
            environment=ENVIRONMENT,
            deletion_date=DELETION_DATE,
        )

    assert refused.value.reason == "authorization-absent"
    _assert_nothing_staged(body)
    # The one allowed side effect: a refusal receipt.
    receipt = refused.value.receipt
    assert receipt is not None and receipt.outcome == "refused"
    payload = json.loads(receipt.stored_path.read_bytes())
    assert payload["refusal"]["reason"] == "authorization-absent"
    # A refusal receipt embeds no operations reading.
    assert "report" not in payload


def test_a_run_refuses_when_the_compatibility_stage_is_not_authorized(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    with pytest.raises(CompatibilityIntakeRefused) as refused:
        run_compatibility_intake(
            body,
            "ucm.xlsx",
            authorization=_authorization([digest_bytes(body)], stages=("shadow",)),
            customer=CUSTOMER,
            project=PROJECT,
            operator=OPERATOR,
            environment=ENVIRONMENT,
            deletion_date=DELETION_DATE,
        )

    assert refused.value.reason == "authorization-refused"
    assert any("stage" in m for m in refused.value.mismatches)
    _assert_nothing_staged(body)


def test_a_run_refuses_when_the_bytes_are_not_covered(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    with pytest.raises(CompatibilityIntakeRefused) as refused:
        run_compatibility_intake(
            body,
            "ucm.xlsx",
            # Authorization covers the customer, project, and stage, but names a
            # different source digest.
            authorization=_authorization(["a" * 64]),
            customer=CUSTOMER,
            project=PROJECT,
            operator=OPERATOR,
            environment=ENVIRONMENT,
            deletion_date=DELETION_DATE,
        )

    assert refused.value.reason == "authorization-refused"
    assert any("source-digest" in m for m in refused.value.mismatches)
    _assert_nothing_staged(body)


def test_a_run_refuses_a_configuration_that_asks_for_model_enrichment(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    with pytest.raises(CompatibilityIntakeRefused) as refused:
        run_compatibility_intake(
            body,
            "ucm.xlsx",
            # A fully valid authorization: the refusal is the model request, not
            # the authorization.
            authorization=_authorization([digest_bytes(body)]),
            customer=CUSTOMER,
            project=PROJECT,
            operator=OPERATOR,
            environment=ENVIRONMENT,
            deletion_date=DELETION_DATE,
            model_enrichment=True,
        )

    assert refused.value.reason == "model-enrichment-unsupported"
    _assert_nothing_staged(body)


# --- No model ---------------------------------------------------------------


def test_the_happy_path_constructs_no_model_client(tmp_path, store):
    body = _workbook_bytes(tmp_path)
    with patch("corridor.llm.OpenAIClient") as client:
        run = run_compatibility_intake(
            body,
            "ucm.xlsx",
            authorization=_authorization([digest_bytes(body)]),
            customer=CUSTOMER,
            project=PROJECT,
            operator=OPERATOR,
            environment=ENVIRONMENT,
            deletion_date=DELETION_DATE,
            external_references=DEMO_HEADINGS,
        )

    assert run.outcome == "reported"
    client.assert_not_called()


# --- Isolation (a): the lane holds no record or release capability ----------

FORBIDDEN_IMPORTS = {
    "corridor.db",
    "corridor.source_append",
    "corridor.fact_decisions",
    "corridor.delta_resolution",
    "corridor.baseline_adoption",
    "corridor.release_authorization",
    "corridor.llm",
    "corridor.native_pipeline",
}


def test_the_lane_module_holds_no_record_or_release_capability():
    """(a) No engine, session, URL, or write module is even reachable by name."""

    source = inspect.getsource(compatibility_intake)
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert FORBIDDEN_IMPORTS.isdisjoint(imported), sorted(
        FORBIDDEN_IMPORTS & imported
    )

    parameters = set(inspect.signature(run_compatibility_intake).parameters)
    assert parameters.isdisjoint({"session", "engine", "connection", "db"})


# --- Isolation (b): a full run creates zero authoritative rows --------------

SPINE_AND_RELEASE_TABLES = (
    "facts",
    "source_segments",
    "proposed_deltas",
    "support_assessments",
    "project_record_revisions",
    "fact_decisions",
    "delta_record_decisions",
    "release_packages",
    "release_candidates",
)


@pytest.fixture
def owner_connection():
    """A separate owner connection to the configured database, not the lane's.

    The lane holds no database handle at all, so this connection exists only to
    prove, from the outside, that a full run leaves the authoritative tables
    untouched.
    """

    engine = create_engine(settings.database_url, poolclass=NullPool, future=True)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def test_a_full_run_creates_no_spine_or_release_rows(tmp_path, store, owner_connection):
    """(b) Count the authoritative tables around a full run: zero delta.

    Neighbouring database tests are rollback-scoped and commit nothing to the
    configured database, so the only thing that could move these counts is a
    write from the lane itself — and the lane has no way to make one.
    """

    def counts() -> dict[str, int]:
        return {
            table: owner_connection.execute(
                text(f"select count(*) from {table}")
            ).scalar_one()
            for table in SPINE_AND_RELEASE_TABLES
        }

    before = counts()
    body = _workbook_bytes(tmp_path)
    run = run_compatibility_intake(
        body,
        "ucm.xlsx",
        authorization=_authorization([digest_bytes(body)]),
        customer=CUSTOMER,
        project=PROJECT,
        operator=OPERATOR,
        environment=ENVIRONMENT,
        deletion_date=DELETION_DATE,
        external_references=DEMO_HEADINGS,
    )
    after = counts()

    assert run.outcome == "reported"
    assert before == after
