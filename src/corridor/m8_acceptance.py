"""Isolated capture and deterministic replay for the M8 mechanical gate.

This replaces the prior subjective #148 inspection with exported, machine-
verifiable evidence.  The real NHHIP lane is observation-only: a one-time,
explicit capture freezes exact Candidate inputs, while ordinary replay avoids
calling the model and reconstructs those inputs from pinned bytes.  Ledger
behavior is exercised only in a separate generated conformance project.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any
from uuid import uuid4

import pymupdf
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.extract_project import Extractor
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.principals import HumanPrincipal
from corridor.ingest import ingest_document
from corridor.models import (
    Assertion,
    AutomaticCarryForwardReceipt,
    Candidate,
    Dependency,
    DocPage,
    Document,
    EvidenceLink,
    ExtractionRun,
    OperativeSupport,
    Project,
)
from corridor.m8_acceptance_bundle import (
    VerificationResult,
    verify_bundle as _verify_bundle,
    write_bundle as _write_bundle_impl,
)
from corridor.m8_acceptance_contract import (
    AcceptanceError,
    AssertionResult,
    CLAIM_BOUNDARY,
    ControlledContradiction,
)
from corridor.m8_acceptance_controlled import (
    _run_controlled_lane,
    _skipped_controlled_lane,
)
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres as _provision_disposable_postgres_impl,
    require_local_postgres_host as _require_local_postgres_host_impl,
    require_postgres_16 as _require_postgres_16_impl,
)
from corridor.m8_acceptance_fixture import (
    first_contract_difference as _first_contract_difference_impl,
    load_captured_fixture as _load_captured_fixture_impl,
    load_transformations as _load_transformations_impl,
)
from corridor.m8_acceptance_publication import publish_directory_once
from corridor.m8_acceptance_projection import project_extraction_observation
from corridor.revision_comparison import (
    DEFAULT_MATCHER_CONFIG,
    DEFAULT_MATCHER_VERSION,
    create_revision_comparison,
    read_revision_comparison,
)
from corridor.supersession import SupersessionDeclaration, register_supersessions

# The acceptance fixture declares within its disposable database; the
# subject matches the principal every m8 receipt already records.
_ACCEPTANCE_PRINCIPAL = HumanPrincipal("local:m8-acceptance-fixture")


RID_INDEX_ID = "nhhip-rid-index-2026-05-01"
NHHIP_REVISION_IDS = (
    "nhhip-ucm-2025-06-20",
    "nhhip-ucm-2025-07-22",
    "nhhip-ucm-2025-10-24",
    "nhhip-ucm-2025-12-15",
    "nhhip-ucm-2026-02-13",
)
CAPTURE_SCHEMA_VERSION = "corridor.m8.real-chain-fixture.v1"
BUNDLE_SCHEMA_VERSION = "corridor.m8.acceptance-bundle.v1"
DATABASE_PREFIX = "corridor_m8_acceptance_"
_DATABASE_NAME = re.compile(r"^corridor_m8_acceptance_[0-9a-f]{32}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLE_FILES = (
    "assertions.json",
    "canonical-content.json",
    "controlled-lane.json",
    "environment.json",
    "real-chain.json",
)
_CONTROLLED_TRANSFORMATIONS_CONTRACT = {
    "schema_version": "corridor.m8.controlled-transformations.v1",
    "test_precondition": {
        "principal": "local:m8-acceptance-fixture",
        "purpose": (
            "Simulate attributable starting Admission and support in disposable "
            "state; this is not human review."
        ),
    },
    "policy": {
        "name": "exact-unchanged-support",
        "version": "1",
        "matcher_version": DEFAULT_MATCHER_VERSION,
    },
    "cases": [
        {
            "case_id": "readiness-exact",
            "expected_correspondence": "exact_unchanged",
            "support_precondition": "readiness_inherited",
            "successor": {"kind": "exact"},
        },
        {
            "case_id": "publication-exact",
            "expected_correspondence": "exact_unchanged",
            "support_precondition": "publication_only",
            "successor": {"kind": "exact"},
        },
        {
            "case_id": "changed",
            "expected_correspondence": "changed",
            "support_precondition": "readiness_inherited",
            "successor": {
                "kind": "changed",
                "field_updates": {"external_org": "Changed Power Company"},
            },
        },
        {
            "case_id": "dropped",
            "expected_correspondence": "dropped",
            "support_precondition": "readiness_inherited",
            "successor": {"kind": "dropped", "rows": 0},
        },
        {
            "case_id": "ambiguous",
            "expected_correspondence": "ambiguous",
            "support_precondition": "readiness_inherited",
            "successor": {"kind": "ambiguous", "rows": 2},
        },
        {
            "case_id": "fan-in-ambiguous",
            "expected_correspondence": "ambiguous",
            "support_precondition": "readiness_inherited",
            "successor": {"kind": "fan_in_exact"},
        },
        {
            "case_id": "fan-out-ambiguous",
            "expected_correspondence": "ambiguous",
            "support_precondition": "readiness_inherited",
            "successor": {"kind": "fan_out_exact"},
        },
        {
            "case_id": "unmatched",
            "expected_correspondence": "unmatched",
            "support_precondition": "readiness_inherited",
            "predecessor": {"remove_identity": True},
            "successor": {"kind": "unmatched", "rows": 1},
        },
        {
            "case_id": "normalized-only",
            "expected_correspondence": "normalized_only_unchanged",
            "support_precondition": "readiness_inherited",
            "successor": {
                "kind": "normalized_only",
                "station_format": "spaced",
            },
        },
        {
            "case_id": "unverified-citation",
            "expected_correspondence": "exact_unchanged",
            "support_precondition": "unverified_citation",
            "successor": {
                "kind": "exact",
                "citation_shape": "unverified_single",
            },
        },
        {
            "case_id": "multiple-citations",
            "expected_correspondence": "exact_unchanged",
            "support_precondition": "multiple_citations",
            "successor": {
                "kind": "exact",
                "citation_shape": "multiple",
            },
        },
        {
            "case_id": "human-edited",
            "expected_correspondence": "exact_unchanged",
            "support_precondition": "human_edited",
            "human_edit": {
                "field": "external_org",
                "value": "Reviewer-normalized owner",
            },
            "successor": {"kind": "exact"},
        },
    ],
}
class CorruptAcceptanceFixture(AcceptanceError):
    """A captured fixture or referenced source no longer matches its digest."""


class CorruptAcceptanceBundle(AcceptanceError):
    """An exported bundle no longer matches its integrity manifest."""


class ReplayContradiction(AcceptanceError):
    """A claimed replay invariant failed after partial evidence existed."""

    def __init__(
        self,
        assertion: "AssertionResult",
        *,
        real_raw: dict[str, Any],
        real_canonical: dict[str, Any],
    ) -> None:
        super().__init__(assertion.detail or assertion.name)
        self.assertion = assertion
        self.real_raw = real_raw
        self.real_canonical = real_canonical


@dataclass(frozen=True)
class AcceptanceCaptureConfig:
    source_lock_path: Path
    output_dir: Path
    postgres_admin_url: str
    prompt_version: str
    expected_model: str
    schema_version: str
    expected_clean_git_revision: str | None
    unsafe_allow_unpinned_test_capture: bool = False


@dataclass(frozen=True)
class AcceptanceRunConfig:
    fixture_path: Path
    transformations_path: Path
    output_dir: Path
    postgres_admin_url: str
    expected_fixture_sha256: str
    expected_transformations_sha256: str
    expected_clean_git_revision: str | None


@dataclass(frozen=True)
class _ReplayFailureDetail:
    name: str
    observed: Any
    expected: Any
    detail: str | None


@dataclass(frozen=True)
class CaptureSummary:
    fixture_path: Path
    fixture_sha256: str
    database_name: str
    run_count: int
    candidate_count: int


@dataclass(frozen=True)
class AcceptanceBundleSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    fixture_sha256: str
    assertions: tuple[AssertionResult, ...]
    carried_count: int
    abstention_counts: dict[str, int]
    database_name: str

    @property
    def content_sha256(self) -> str:
        """Compatibility name for the normalized replay identity."""

        return self.canonical_content_sha256


@dataclass(frozen=True)
class _Source:
    registry_id: str
    record: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class _SourceChain:
    lock_path: Path
    lock_sha256: str
    rid_index: _Source
    revisions: tuple[_Source, ...]
    declarations: tuple[SupersessionDeclaration, ...]


def provision_disposable_postgres(admin_url: str) -> Iterator[ProvisionedDatabase]:
    return _provision_disposable_postgres_impl(
        admin_url,
        repo_root=_REPO_ROOT,
        error_cls=AcceptanceError,
        database_prefix=DATABASE_PREFIX,
    )


def _require_postgres_16(version: str) -> None:
    _require_postgres_16_impl(version, error_cls=AcceptanceError)


def _require_local_postgres_host(host: str | None) -> None:
    _require_local_postgres_host_impl(host, error_cls=AcceptanceError)


def capture_m8_fixture(
    config: AcceptanceCaptureConfig,
    *,
    extract: Extractor,
    provision_database: DatabaseProvisioner = provision_disposable_postgres,
) -> CaptureSummary:
    """Explicitly capture fresh exact NHHIP inputs from an injected extractor."""

    _validate_capture_config(config)
    chain = _load_source_chain(config.source_lock_path)
    git_state = _git_state()
    if config.expected_clean_git_revision is not None:
        if git_state["revision"] != config.expected_clean_git_revision:
            raise AcceptanceError("capture Git revision does not match the pin")
        if git_state["status"] != "clean":
            raise AcceptanceError("authoritative capture requires a clean repository")

    with provision_database(config.postgres_admin_url) as database:
        with tempfile.TemporaryDirectory(prefix="corridor-m8-capture-") as image_root:
            with database.session_factory() as session:
                content = _capture_chain(
                    session,
                    chain=chain,
                    extract=extract,
                    prompt_version=config.prompt_version,
                    expected_model=config.expected_model,
                    schema_version=config.schema_version,
                    images_dir=Path(image_root),
                    git_state=git_state,
                    database=database,
                )
                content["capture"]["authoritative"] = bool(
                    config.expected_clean_git_revision is not None
                    and not config.unsafe_allow_unpinned_test_capture
                )
                session.commit()

        fixture_sha256 = _json_sha256(content)
        fixture = {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "content": content,
            "fixture_sha256": fixture_sha256,
        }
        fixture_path = _publish_capture_fixture(config.output_dir, fixture, chain)
        return CaptureSummary(
            fixture_path=fixture_path,
            fixture_sha256=fixture_sha256,
            database_name=database.name,
            run_count=len(content["runs"]),
            candidate_count=sum(run["candidate_count"] for run in content["runs"]),
        )


def _validate_capture_config(config: AcceptanceCaptureConfig) -> None:
    if not config.prompt_version:
        raise AcceptanceError("capture prompt_version must be non-empty")
    if not config.schema_version:
        raise AcceptanceError("capture schema_version must be non-empty")
    if not config.expected_model:
        raise AcceptanceError("capture expected_model must be non-empty")
    if (
        config.expected_clean_git_revision is None
        and not config.unsafe_allow_unpinned_test_capture
    ):
        raise AcceptanceError(
            "authoritative capture requires an expected clean Git revision"
        )


def _load_source_chain(lock_path: Path) -> _SourceChain:
    lock_path = Path(lock_path)
    raw = json.loads(lock_path.read_text())
    by_registry: dict[str, tuple[str, dict[str, Any]]] = {}
    for source_key, record in (raw.get("sources") or {}).items():
        registry_id = record.get("registry_id")
        if registry_id in {RID_INDEX_ID, *NHHIP_REVISION_IDS}:
            if registry_id in by_registry:
                raise CorruptAcceptanceFixture(
                    f"duplicate source registry id {registry_id}"
                )
            by_registry[registry_id] = (source_key, record)
    required = {RID_INDEX_ID, *NHHIP_REVISION_IDS}
    if set(by_registry) != required:
        missing = sorted(required - set(by_registry))
        raise CorruptAcceptanceFixture(
            f"source lock does not contain the exact NHHIP chain; missing {missing}"
        )

    def source(registry_id: str) -> _Source:
        source_key, record = by_registry[registry_id]
        path_value = record.get("local_path")
        expected_sha256 = record.get("sha256")
        if not isinstance(path_value, str) or not path_value:
            raise CorruptAcceptanceFixture(f"{registry_id} has no local_path")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise CorruptAcceptanceFixture(f"{registry_id} has no valid sha256")
        path = Path(path_value)
        if not path.is_absolute():
            path = (_REPO_ROOT / path).resolve()
        if not path.is_file():
            raise CorruptAcceptanceFixture(
                f"{registry_id} source bytes are absent; run make corpus"
            )
        actual_sha256 = _sha256(path.read_bytes())
        if actual_sha256 != expected_sha256:
            raise CorruptAcceptanceFixture(
                f"{registry_id} source hash does not match the lock"
            )
        copied = deepcopy(record)
        copied["source_key"] = source_key
        copied["local_path"] = str(path)
        return _Source(registry_id=registry_id, record=copied, path=path)

    rid_index = source(RID_INDEX_ID)
    revisions = tuple(source(registry_id) for registry_id in NHHIP_REVISION_IDS)
    declarations: list[SupersessionDeclaration] = []
    for ordinal, predecessor in enumerate(revisions[:-1]):
        raw_declaration = predecessor.record.get("supersession")
        if not isinstance(raw_declaration, dict):
            raise CorruptAcceptanceFixture(
                f"{predecessor.registry_id} has no supersession declaration"
            )
        expected_successor = revisions[ordinal + 1].registry_id
        if raw_declaration.get("successor_registry_id") != expected_successor:
            raise CorruptAcceptanceFixture(
                f"{predecessor.registry_id} does not declare {expected_successor}"
            )
        if raw_declaration.get("source_registry_id") != RID_INDEX_ID:
            raise CorruptAcceptanceFixture("NHHIP edge is not sourced to the RID index")
        try:
            replacement_date = date.fromisoformat(raw_declaration["replacement_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptAcceptanceFixture(
                f"{predecessor.registry_id} has an invalid replacement date"
            ) from exc
        source_page = raw_declaration.get("source_page")
        if not isinstance(source_page, int) or isinstance(source_page, bool):
            raise CorruptAcceptanceFixture("RID source page must be an integer")
        declarations.append(
            SupersessionDeclaration(
                predecessor_registry_id=predecessor.registry_id,
                successor_registry_id=expected_successor,
                replacement_date=replacement_date,
                source_registry_id=RID_INDEX_ID,
                source_page=source_page,
            )
        )
    return _SourceChain(
        lock_path=lock_path,
        lock_sha256=_sha256(lock_path.read_bytes()),
        rid_index=rid_index,
        revisions=revisions,
        declarations=tuple(declarations),
    )


def _capture_chain(
    session: Session,
    *,
    chain: _SourceChain,
    extract: Extractor,
    prompt_version: str,
    expected_model: str,
    schema_version: str,
    images_dir: Path,
    git_state: dict[str, str],
    database: ProvisionedDatabase,
) -> dict[str, Any]:
    project = Project(
        slug=f"m8-capture-{uuid4().hex}",
        name="NHHIP M8 capture",
        is_synthetic=False,
    )
    session.add(project)
    session.flush([project])
    rid_document = _ingest_source(
        session, project.id, chain.rid_index, images_dir / "rid"
    )
    rid_page_numbers = set(
        session.scalars(
            select(DocPage.page_no).where(DocPage.document_id == rid_document.id)
        ).all()
    )
    if any(declaration.source_page not in rid_page_numbers for declaration in chain.declarations):
        raise CorruptAcceptanceFixture("RID index is missing a declared source page")
    _verify_rid_declarations(session, rid_document, chain)

    run_records: list[dict[str, Any]] = []
    comparison_records: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = [_document_observation(session, rid_document, chain.rid_index)]
    run_by_registry: dict[str, ExtractionRun] = {}

    for ordinal, source in enumerate(chain.revisions):
        document = _ingest_source(
            session,
            project.id,
            source,
            images_dir / source.registry_id,
        )
        if ordinal:
            register_supersessions(
                session,
                (chain.declarations[ordinal - 1],),
                project_id=project.id,
            )
            if session.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .where(ExtractionRun.document_id == document.id)
            ):
                raise AcceptanceError("successor extraction preceded registration")

        run = _record_exact_extraction(
            session,
            document,
            extract=extract,
            prompt_version=prompt_version,
            model=expected_model,
            schema_version=schema_version,
        )
        if run.candidate_inputs_json is None:
            raise AcceptanceError("capture did not produce an exact run snapshot")
        if run.model != expected_model:
            raise AcceptanceError(
                f"{source.registry_id} model {run.model!r} does not match the pin"
            )
        if run.schema_version != schema_version:
            raise AcceptanceError(
                f"{source.registry_id} schema version does not match the pin"
            )
        declare_active_run(
            session, document.id, run.id, principal=_ACCEPTANCE_PRINCIPAL
        )
        run_by_registry[source.registry_id] = run
        stable_inputs = _stable_capture_inputs(
            run.candidate_inputs_json,
            registry_id=source.registry_id,
            project_id=project.id,
            document_id=document.id,
        )
        run_records.append(
            {
                "registry_id": source.registry_id,
                "capture_document_id": document.id,
                "capture_run_id": run.id,
                "prompt_version": run.prompt_version,
                "model": run.model,
                "schema_version": run.schema_version,
                "outcome": run.outcome,
                "page_errors": run.page_errors,
                "candidate_count": run.candidate_count,
                "candidate_inputs": stable_inputs,
            }
        )
        observations.append(
            project_extraction_observation(
                base_observation=_document_observation(session, document, source),
                document=document,
                stable_inputs=stable_inputs,
                outcome=run.outcome,
                candidate_count=run.candidate_count,
            )
        )

        if ordinal:
            predecessor_id = chain.revisions[ordinal - 1].registry_id
            predecessor_run = run_by_registry[predecessor_id]
            comparison = create_revision_comparison(
                session,
                predecessor_run.id,
                run.id,
            )
            readback = read_revision_comparison(session, comparison.id)
            _assert_partition(readback)
            comparison_records.append(
                _capture_comparison_record(
                    readback,
                    predecessor_registry_id=predecessor_id,
                    successor_registry_id=source.registry_id,
                )
            )

    selected_seed = _select_seed(run_records, comparison_records)
    counts = _ledger_counts(session, project.id)
    if any(counts.values()):
        raise AcceptanceError("real-chain capture wrote to the Ledger")
    return {
        "claim_boundary": CLAIM_BOUNDARY,
        "capture": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "git_revision": git_state["revision"],
            "git_status": git_state["status"],
            "prompt_version": prompt_version,
            "model": expected_model,
            "schema_version": schema_version,
            "postgres_version": database.postgres_version,
            "migration_head": database.migration_head,
        },
        "source_lock_sha256": chain.lock_sha256,
        "rid_index": _fixture_source(chain.rid_index),
        "sources": [_fixture_source(source) for source in chain.revisions],
        "supersession_edges": [
            {
                "predecessor_registry_id": item.predecessor_registry_id,
                "successor_registry_id": item.successor_registry_id,
                "replacement_date": item.replacement_date.isoformat(),
                "source_registry_id": item.source_registry_id,
                "source_page": item.source_page,
            }
            for item in chain.declarations
        ],
        "runs": run_records,
        "comparisons": comparison_records,
        "observations": observations,
        "selected_seed": selected_seed,
        "matcher": {
            "version": DEFAULT_MATCHER_VERSION,
            "config": deepcopy(DEFAULT_MATCHER_CONFIG),
            "config_sha256": _json_sha256(DEFAULT_MATCHER_CONFIG),
        },
        "ledger_counts": counts,
    }


def _record_exact_extraction(
    session: Session,
    document: Document,
    *,
    extract: Extractor,
    prompt_version: str,
    model: str | None,
    schema_version: str,
) -> ExtractionRun:
    """Record one targeted completed attempt with independently pinned schema."""

    with session.begin_nested():
        candidates = tuple(extract(session, document))
        return record_extraction_run(
            session,
            document,
            prompt_version=prompt_version,
            candidate_count=len(candidates),
            page_errors=0,
            candidates=candidates,
            model=model,
            schema_version=schema_version,
        )


def _ingest_source(
    session: Session,
    project_id: int,
    source: _Source,
    images_dir: Path,
) -> Document:
    record = source.record
    filename = record.get("member") or Path(record["local_path"]).name
    return ingest_document(
        session,
        project_id=project_id,
        path=source.path,
        doc_type=record.get("doc_type", "other"),
        images_dir=images_dir,
        filename=filename,
        source_url=record.get("archive_url") or record.get("source_key"),
        retrieved_at=record.get("retrieved_at"),
        doc_date=(
            date.fromisoformat(record["doc_date"])
            if record.get("doc_date")
            else None
        ),
        registry_id=source.registry_id,
        expected_sha256=record["sha256"],
    )


def _stable_capture_inputs(
    inputs: list[dict[str, Any]],
    *,
    registry_id: str,
    project_id: int,
    document_id: int,
) -> list[dict[str, Any]]:
    stable = []
    for ordinal, item in enumerate(inputs, start=1):
        if item.get("project_id") != project_id:
            raise AcceptanceError("capture Candidate crosses its project")
        if item.get("source_document_id") != document_id:
            raise AcceptanceError("capture Candidate crosses its document")
        payload = deepcopy(item.get("payload_json") or {})
        for citation in payload.get("citations") or []:
            if citation.get("document_id") != document_id:
                raise AcceptanceError("capture citation crosses its document")
            citation.pop("document_id", None)
            citation["document_registry_id"] = registry_id
        stable.append(
            {
                "candidate_key": f"candidate:{registry_id}:{ordinal:06d}",
                "capture_candidate_id": item.get("candidate_id"),
                "kind": item.get("kind"),
                "source_registry_id": registry_id,
                "payload_json": payload,
                "source_pages": list(item.get("source_pages") or []),
                "confidence": item.get("confidence"),
                "prompt_version": item.get("prompt_version"),
                "model": item.get("model"),
                "citations_verified": item.get("citations_verified"),
                "state": item.get("state"),
            }
        )
    return stable


def _capture_comparison_record(
    readback,
    *,
    predecessor_registry_id: str,
    successor_registry_id: str,
) -> dict[str, Any]:
    return {
        "comparison_key": (
            f"comparison:{predecessor_registry_id}:{successor_registry_id}"
        ),
        "capture_comparison_id": readback.comparison.id,
        "predecessor_registry_id": predecessor_registry_id,
        "successor_registry_id": successor_registry_id,
        "matcher_version": readback.comparison.matcher_version,
        "matcher_config": deepcopy(readback.comparison.matcher_config),
        "capture_content_sha256": readback.comparison.content_sha256,
        "finding_counts": dict(
            sorted(Counter(item.state for item in readback.findings).items())
        ),
        "findings": [
            {
                "ordinal": finding.ordinal,
                "state": finding.state,
                "predecessor_candidate_ids": list(
                    finding.predecessor_candidate_ids
                ),
                "successor_candidate_ids": list(finding.successor_candidate_ids),
                "match_score": finding.match_score,
                "field_changes": deepcopy(finding.field_changes),
                "matcher_detail": deepcopy(finding.matcher_detail),
            }
            for finding in readback.findings
        ],
    }


def _select_seed(
    runs: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> dict[str, Any]:
    by_capture_id: dict[int, dict[str, Any]] = {}
    for run in runs:
        for item in run["candidate_inputs"]:
            by_capture_id[item["capture_candidate_id"]] = item
    choices = []
    for comparison in comparisons:
        for finding in comparison["findings"]:
            if (
                finding["state"] != "unchanged"
                or len(finding["predecessor_candidate_ids"]) != 1
                or len(finding["successor_candidate_ids"]) != 1
            ):
                continue
            predecessor = by_capture_id[finding["predecessor_candidate_ids"][0]]
            successor = by_capture_id[finding["successor_candidate_ids"][0]]
            predecessor_fields = predecessor["payload_json"].get("fields")
            successor_fields = successor["payload_json"].get("fields")
            citations = successor["payload_json"].get("citations") or []
            if (
                not isinstance(predecessor_fields, dict)
                or predecessor_fields != successor_fields
                or len(citations) != 1
                or citations[0].get("verified") is not True
            ):
                continue
            identity = {
                "comparison_key": comparison["comparison_key"],
                "predecessor_candidate_key": predecessor["candidate_key"],
                "successor_candidate_key": successor["candidate_key"],
                "fields": predecessor_fields,
            }
            choices.append((_json_sha256(identity), identity))
    if not choices:
        raise AcceptanceError("the captured chain has no deterministic exact seed")
    selection_sha256, selected = min(choices, key=lambda item: item[0])
    return {**selected, "selection_sha256": selection_sha256}


def _assert_partition(readback) -> None:
    predecessor_ids = [
        candidate_id
        for finding in readback.findings
        for candidate_id in finding.predecessor_candidate_ids
    ]
    successor_ids = [
        candidate_id
        for finding in readback.findings
        for candidate_id in finding.successor_candidate_ids
    ]
    expected_predecessors = [
        item["candidate_id"] for item in readback.predecessor_inputs
    ]
    expected_successors = [item["candidate_id"] for item in readback.successor_inputs]
    if Counter(predecessor_ids) != Counter(expected_predecessors):
        raise AcceptanceError("comparison does not partition predecessor inputs")
    if Counter(successor_ids) != Counter(expected_successors):
        raise AcceptanceError("comparison does not partition successor inputs")


def _document_observation(
    session: Session,
    document: Document,
    source: _Source,
) -> dict[str, Any]:
    pages = session.scalars(
        select(DocPage)
        .where(DocPage.document_id == document.id)
        .order_by(DocPage.page_no)
    ).all()
    dimensions: Counter[str] = Counter()
    with pymupdf.open(source.path) as pdf:
        for page in pdf:
            dimensions[f"{page.rect.width:.1f}x{page.rect.height:.1f}"] += 1
    return {
        "registry_id": source.registry_id,
        "sha256": source.record["sha256"],
        "bytes": source.path.stat().st_size,
        "pages": len(pages),
        "page_dimensions": dict(sorted(dimensions.items())),
        "text_characters": sum(len(page.text or "") for page in pages),
        "text_source_counts": dict(
            sorted(Counter(page.text_source for page in pages).items())
        ),
        "page_text_sha256": [
            _sha256((page.text or "").encode()) for page in pages
        ],
    }


def _fixture_source(source: _Source) -> dict[str, Any]:
    record = source.record
    return {
        "registry_id": source.registry_id,
        "sha256": record["sha256"],
        "bytes": source.path.stat().st_size,
        "fixture_relpath": f"sources/{source.registry_id}.pdf",
        "doc_type": record.get("doc_type", "other"),
        "doc_date": record.get("doc_date"),
        "filename": record.get("member") or source.path.name,
        "source_url": record.get("archive_url") or record.get("source_key"),
        "retrieved_at": record.get("retrieved_at"),
    }


def _write_fixture_sources(chain: _SourceChain, output_dir: Path) -> None:
    """Materialize content-addressed source bytes without host-specific paths."""

    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    for source in (chain.rid_index, *chain.revisions):
        destination = source_dir / f"{source.registry_id}.pdf"
        if destination.is_symlink():
            raise AcceptanceError(
                f"refusing to overwrite fixture source symlink {destination.name}"
            )
        shutil.copyfile(source.path, destination)
        if _sha256(destination.read_bytes()) != source.record["sha256"]:
            raise CorruptAcceptanceFixture(
                f"copied fixture source hash changed for {source.registry_id}"
            )


def _publish_capture_fixture(
    output_dir: Path,
    fixture: dict[str, Any],
    chain: _SourceChain,
) -> Path:
    try:
        published_dir = publish_directory_once(
            output_dir,
            temp_prefix="corridor-m8-capture",
            build=lambda stage_dir: _stage_capture_fixture(
                stage_dir, fixture=fixture, chain=chain
            ),
        )
    except FileExistsError as exc:
        raise AcceptanceError("fixture output directory already exists") from exc
    return published_dir / "fixture.json"


def _stage_capture_fixture(
    output_dir: Path,
    *,
    fixture: dict[str, Any],
    chain: _SourceChain,
) -> None:
    _write_fixture_sources(chain, output_dir)
    (output_dir / "fixture.json").write_bytes(_canonical_json(fixture) + b"\n")


def _verify_rid_declarations(
    session: Session,
    rid_document: Document,
    chain: _SourceChain,
) -> None:
    """Prove each locked predecessor and replacement date share one RID row."""

    pages = {
        page.page_no: page.text or ""
        for page in session.scalars(
            select(DocPage).where(DocPage.document_id == rid_document.id)
        ).all()
    }
    if any(item.source_page not in pages for item in chain.declarations):
        raise CorruptAcceptanceFixture("RID index is missing a declared source page")
    revision_by_id = {source.registry_id: source for source in chain.revisions}
    rows_by_page = {
        page_no: _rid_rows(chain.rid_index.path, page_no)
        for page_no in {item.source_page for item in chain.declarations}
    }
    for declaration in chain.declarations:
        predecessor = revision_by_id.get(declaration.predecessor_registry_id)
        if predecessor is None:
            raise CorruptAcceptanceFixture("RID edge predecessor is not in the chain")
        filename = predecessor.record.get("member") or predecessor.path.name
        filename_token = _filename_token(filename)
        matching_rows = tuple(
            row
            for row in rows_by_page.get(declaration.source_page, ())
            if filename_token in _filename_token(row)
        )
        if len(matching_rows) != 1 or not any(
            spelling in matching_rows[0].casefold()
            for spelling in _date_spellings(declaration.replacement_date)
        ):
            raise CorruptAcceptanceFixture(
                "RID row does not bind declared filename and replacement date: "
                f"{filename!r}, {declaration.replacement_date.isoformat()}"
            )

    terminal = chain.revisions[-1]
    terminal_filename = terminal.record.get("member") or terminal.path.name
    declared_rows = tuple(
        row for rows in rows_by_page.values() for row in rows
    )
    if not any(
        _filename_token(terminal_filename) in _filename_token(row)
        for row in declared_rows
    ):
        raise CorruptAcceptanceFixture(
            f"RID row does not contain terminal filename {terminal_filename!r}"
        )


def _rid_rows(path: Path, page_no: int) -> tuple[str, ...]:
    """Recover visual table rows by clustering PDF words on their y-axis."""

    with pymupdf.open(path) as pdf:
        if page_no < 1 or page_no > len(pdf):
            return ()
        words = pdf[page_no - 1].get_text("words", sort=True)
    groups: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: ((item[1] + item[3]) / 2, item[0])):
        center = (word[1] + word[3]) / 2
        if not groups or abs(center - groups[-1]["center"]) > 2.5:
            groups.append({"center": center, "words": [word]})
            continue
        group = groups[-1]
        group["words"].append(word)
        group["center"] = sum(
            (item[1] + item[3]) / 2 for item in group["words"]
        ) / len(group["words"])
    return tuple(
        " ".join(str(word[4]) for word in sorted(group["words"], key=lambda item: item[0]))
        for group in groups
    )


def _filename_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _date_spellings(value: date) -> tuple[str, ...]:
    return (
        value.isoformat(),
        f"{value.month}/{value.day}/{value.year}",
        f"{value.month:02d}/{value.day:02d}/{value.year}",
    )


def _ledger_counts(session: Session, project_id: int) -> dict[str, int]:
    dependency_ids = select(Dependency.id).where(Dependency.project_id == project_id)
    return {
        "dependencies": session.scalar(
            select(func.count()).select_from(Dependency).where(Dependency.project_id == project_id)
        ),
        "assertions": session.scalar(
            select(func.count()).select_from(Assertion).where(
                Assertion.dependency_id.in_(dependency_ids)
            )
        ),
        "evidence_links": session.scalar(
            select(func.count()).select_from(EvidenceLink).where(
                EvidenceLink.dependency_id.in_(dependency_ids)
            )
        ),
        "operative_support": session.scalar(
            select(func.count()).select_from(OperativeSupport).where(
                OperativeSupport.dependency_id.in_(dependency_ids)
            )
        ),
        "carry_forward_receipts": session.scalar(
            select(func.count())
            .select_from(AutomaticCarryForwardReceipt)
            .where(AutomaticCarryForwardReceipt.project_id == project_id)
        ),
    }


def _git_state() -> dict[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status_output = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {"revision": revision, "status": "dirty" if status_output else "clean"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_m8_acceptance(
    config: AcceptanceRunConfig,
    *,
    provision_database: DatabaseProvisioner = provision_disposable_postgres,
) -> AcceptanceBundleSummary:
    """Replay a captured real chain and controlled oracle without a model."""

    fixture_content, fixture_sha256, chain = _load_captured_fixture(
        config.fixture_path,
        expected_sha256=config.expected_fixture_sha256,
    )
    transformations, transformations_sha256 = _load_transformations(
        config.transformations_path,
        expected_sha256=config.expected_transformations_sha256,
    )
    git_state = _git_state()
    if config.expected_clean_git_revision is not None:
        if git_state["revision"] != config.expected_clean_git_revision:
            raise AcceptanceError("replay Git revision does not match the pin")
        if git_state["status"] != "clean":
            raise AcceptanceError("pinned acceptance replay requires a clean repository")

    assertions: list[AssertionResult] = []
    with provision_database(config.postgres_admin_url) as database:
        with tempfile.TemporaryDirectory(prefix="corridor-m8-replay-") as image_root:
            with database.session_factory() as session:
                try:
                    real_raw, real_canonical = _replay_real_chain(
                        session,
                        chain=chain,
                        fixture_content=fixture_content,
                        images_dir=Path(image_root) / "real",
                    )
                    controlled_raw, controlled_canonical = _run_controlled_lane(
                        session,
                        seed=fixture_content["selected_seed"],
                        transformations=transformations,
                        transformations_sha256=transformations_sha256,
                    )
                    assertions.extend(
                        _acceptance_assertions(real_raw, controlled_raw, database)
                    )
                except ReplayContradiction as exc:
                    real_raw = exc.real_raw
                    real_canonical = exc.real_canonical
                    controlled_raw = _skipped_controlled_lane(
                        reason="real_chain_replay_failed"
                    )
                    controlled_canonical = deepcopy(controlled_raw)
                    assertions.append(exc.assertion)
                except ControlledContradiction as exc:
                    controlled_raw = exc.controlled_raw
                    controlled_canonical = exc.controlled_canonical
                    assertions.append(exc.assertion)
                session.commit()

        environment = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "replayed_at": datetime.now(timezone.utc).isoformat(),
            "database_name": database.name,
            "database_disposable_name_valid": bool(
                _DATABASE_NAME.fullmatch(database.name)
            ),
            "git_revision": git_state["revision"],
            "git_status": git_state["status"],
            "postgres_version": database.postgres_version,
            "migration_head": database.migration_head,
            "fixture_sha256": fixture_sha256,
            "transformations_sha256": transformations_sha256,
            "source_lock_sha256": fixture_content["source_lock_sha256"],
            "prompt_version": fixture_content["capture"]["prompt_version"],
            "model": fixture_content["capture"]["model"],
            "schema_pin": fixture_content["capture"]["schema_version"],
            "matcher_version": fixture_content["matcher"]["version"],
            "matcher_config_sha256": fixture_content["matcher"][
                "config_sha256"
            ],
            "policy_sha256": controlled_raw["policy"]["policy_sha256"],
            "claim_boundary": CLAIM_BOUNDARY,
        }
        canonical_content = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "claim_boundary": CLAIM_BOUNDARY,
            "environment": {
                key: environment[key]
                for key in (
                    "git_revision",
                    "git_status",
                    "postgres_version",
                    "migration_head",
                    "fixture_sha256",
                    "transformations_sha256",
                    "source_lock_sha256",
                    "prompt_version",
                    "model",
                    "schema_pin",
                    "matcher_version",
                    "matcher_config_sha256",
                    "policy_sha256",
                )
            },
            "real_chain": real_canonical,
            "controlled_lane": controlled_canonical,
            "assertions": [
                {"name": item.name, "passed": item.passed}
                for item in assertions
            ],
        }
        manifest_path, manifest_sha256, canonical_sha256 = _write_bundle(
            config.output_dir,
            environment=environment,
            real_chain=real_raw,
            controlled_lane=controlled_raw,
            assertions=assertions,
            canonical_content=canonical_content,
        )
        database_name = database.name

    abstention_counts = dict(
        sorted(
            Counter(
                case["abstention_reason"]
                for case in controlled_raw.get("cases", [])
                if case["abstention_reason"] is not None
            ).items()
        )
    )
    return AcceptanceBundleSummary(
        bundle_dir=config.output_dir,
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
        fixture_sha256=fixture_sha256,
        assertions=tuple(assertions),
        carried_count=controlled_raw["passes"]["first"]["carried"],
        abstention_counts=abstention_counts,
        database_name=database_name,
    )


def verify_m8_acceptance_bundle(
    bundle_dir: Path,
    *,
    expected_integrity_manifest_sha256: str,
) -> VerificationResult:
    return _verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=_BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptAcceptanceBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )


def _load_captured_fixture(
    fixture_path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], str, _SourceChain]:
    return _load_captured_fixture_impl(
        fixture_path,
        expected_sha256=expected_sha256,
        capture_schema_version=CAPTURE_SCHEMA_VERSION,
        claim_boundary=CLAIM_BOUNDARY,
        build_chain=_source_chain_from_captured_fixture,
        sha256=_sha256,
        json_sha256=_json_sha256,
        corrupt_fixture_error_cls=CorruptAcceptanceFixture,
    )


def _load_transformations(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    return _load_transformations_impl(
        path,
        expected_sha256=expected_sha256,
        contract=_CONTROLLED_TRANSFORMATIONS_CONTRACT,
        acceptance_error_cls=AcceptanceError,
        sha256=_sha256,
    )


def _first_contract_difference(
    expected: Any,
    observed: Any,
    *,
    path: str,
) -> str | None:
    return _first_contract_difference_impl(expected, observed, path=path)


def _source_chain_from_captured_fixture(
    content: dict[str, Any],
    fixture_path: Path,
) -> _SourceChain:
    records = [content.get("rid_index"), *(content.get("sources") or [])]
    if any(not isinstance(record, dict) for record in records):
        raise CorruptAcceptanceFixture("captured fixture source records are invalid")
    by_registry = {record["registry_id"]: record for record in records}
    required = {RID_INDEX_ID, *NHHIP_REVISION_IDS}
    if set(by_registry) != required:
        raise CorruptAcceptanceFixture("captured fixture does not contain the exact chain")
    fixture_root = Path(fixture_path).parent.resolve()

    def source(registry_id: str) -> _Source:
        record = deepcopy(by_registry[registry_id])
        relative = record.get("fixture_relpath")
        if not isinstance(relative, str):
            raise CorruptAcceptanceFixture(f"{registry_id} has no fixture path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
            raise CorruptAcceptanceFixture(f"{registry_id} has an unsafe fixture path")
        path = (fixture_root / Path(*pure.parts)).resolve()
        try:
            path.relative_to(fixture_root)
        except ValueError as exc:
            raise CorruptAcceptanceFixture(f"{registry_id} fixture path escapes") from exc
        value = path.read_bytes()
        if path.is_symlink() or not path.is_file():
            raise CorruptAcceptanceFixture(f"{registry_id} source bytes are absent")
        if _sha256(value) != record.get("sha256") or len(value) != record.get("bytes"):
            raise CorruptAcceptanceFixture(f"{registry_id} source bytes changed")
        record.update(
            {
                "local_path": str(path),
                "member": record.get("filename"),
                "archive_url": record.get("source_url"),
                "source_key": record.get("source_url"),
            }
        )
        return _Source(registry_id=registry_id, record=record, path=path)

    declarations = tuple(
        SupersessionDeclaration(
            predecessor_registry_id=item["predecessor_registry_id"],
            successor_registry_id=item["successor_registry_id"],
            replacement_date=date.fromisoformat(item["replacement_date"]),
            source_registry_id=item["source_registry_id"],
            source_page=item["source_page"],
        )
        for item in content.get("supersession_edges") or ()
    )
    if len(declarations) != len(NHHIP_REVISION_IDS) - 1:
        raise CorruptAcceptanceFixture("captured fixture has the wrong edge count")
    return _SourceChain(
        lock_path=Path(fixture_path),
        lock_sha256=content["source_lock_sha256"],
        rid_index=source(RID_INDEX_ID),
        revisions=tuple(source(item) for item in NHHIP_REVISION_IDS),
        declarations=declarations,
    )


def _replay_real_chain(
    session: Session,
    *,
    chain: _SourceChain,
    fixture_content: dict[str, Any],
    images_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct fresh exact runs from captured Candidate bytes only."""

    project = Project(
        slug=f"m8-real-replay-{uuid4().hex}",
        name="NHHIP M8 model-free replay",
        is_synthetic=False,
    )
    session.add(project)
    session.flush([project])
    rid_document = _ingest_source(
        session, project.id, chain.rid_index, images_dir / "rid"
    )
    _verify_rid_declarations(session, rid_document, chain)

    expected_runs = {
        item["registry_id"]: item for item in fixture_content["runs"]
    }
    expected_comparisons = {
        item["comparison_key"]: item
        for item in fixture_content["comparisons"]
    }
    documents: list[dict[str, Any]] = [
        {
            "registry_id": RID_INDEX_ID,
            "document_id": rid_document.id,
            "filename": rid_document.filename,
            "sha256": rid_document.sha256,
            "pages": rid_document.pages,
        }
    ]
    observations = [
        _document_observation(session, rid_document, chain.rid_index)
    ]
    raw_runs: list[dict[str, Any]] = []
    active_declarations: list[dict[str, Any]] = []
    raw_comparisons: list[dict[str, Any]] = []
    canonical_runs: list[dict[str, Any]] = []
    canonical_comparisons: list[dict[str, Any]] = []
    run_by_registry: dict[str, ExtractionRun] = {}
    key_by_candidate_id: dict[int, str] = {}

    def contradiction(
        *,
        name: str,
        observed: Any,
        expected: Any,
        detail: str,
    ) -> None:
        failure = _ReplayFailureDetail(
            name=name,
            observed=deepcopy(observed),
            expected=deepcopy(expected),
            detail=detail,
        )
        raise ReplayContradiction(
            AssertionResult(
                name=name,
                passed=False,
                observed=deepcopy(observed),
                expected=deepcopy(expected),
                detail=detail,
            ),
            real_raw=_replay_real_raw(
                project_id=project.id,
                documents=documents,
                runs=raw_runs,
                active_run_declarations=active_declarations,
                comparisons=raw_comparisons,
                observations=observations,
                ledger_counts=_ledger_counts(session, project.id),
                failure=failure,
            ),
            real_canonical=_replay_real_canonical(
                documents=documents,
                runs=canonical_runs,
                comparisons=canonical_comparisons,
                observations=observations,
                ledger_counts=_ledger_counts(session, project.id),
                failure=failure,
            ),
        )

    for ordinal, source in enumerate(chain.revisions):
        document = _ingest_source(
            session,
            project.id,
            source,
            images_dir / source.registry_id,
        )
        documents.append(
            {
                "registry_id": source.registry_id,
                "document_id": document.id,
                "filename": document.filename,
                "sha256": document.sha256,
                "pages": document.pages,
            }
        )
        if ordinal:
            register_supersessions(
                session,
                (chain.declarations[ordinal - 1],),
                project_id=project.id,
            )
            attempts_before = session.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .where(ExtractionRun.document_id == document.id)
            )
            if attempts_before:
                contradiction(
                    name="real_chain_replay_registration_precedes_extraction",
                    observed={
                        "registry_id": source.registry_id,
                        "attempts_before_registration": attempts_before,
                    },
                    expected={
                        "registry_id": source.registry_id,
                        "attempts_before_registration": 0,
                    },
                    detail="real successor extraction preceded registration",
                )

        expected = expected_runs[source.registry_id]
        run = _record_exact_extraction(
            session,
            document,
            extract=_captured_extractor(expected),
            prompt_version=expected["prompt_version"],
            model=expected["model"],
            schema_version=expected["schema_version"],
        )
        if (
            run.prompt_version != expected["prompt_version"]
            or run.model != expected["model"]
            or run.schema_version != expected["schema_version"]
            or run.candidate_count != expected["candidate_count"]
            or run.page_errors != expected["page_errors"]
            or run.outcome != expected["outcome"]
        ):
            contradiction(
                name="real_chain_replay_run_pins_match_capture",
                observed={
                    "registry_id": source.registry_id,
                    "prompt_version": run.prompt_version,
                    "model": run.model,
                    "schema_version": run.schema_version,
                    "candidate_count": run.candidate_count,
                    "page_errors": run.page_errors,
                    "outcome": run.outcome,
                },
                expected={
                    "registry_id": source.registry_id,
                    "prompt_version": expected["prompt_version"],
                    "model": expected["model"],
                    "schema_version": expected["schema_version"],
                    "candidate_count": expected["candidate_count"],
                    "page_errors": expected["page_errors"],
                    "outcome": expected["outcome"],
                },
                detail=f"replayed run pins drifted for {source.registry_id}",
            )
        stable_inputs = _stable_capture_inputs(
            run.candidate_inputs_json or [],
            registry_id=source.registry_id,
            project_id=project.id,
            document_id=document.id,
        )
        observed_inputs = _inputs_without_database_ids(stable_inputs)
        expected_inputs = _inputs_without_database_ids(expected["candidate_inputs"])
        if observed_inputs != expected_inputs:
            contradiction(
                name="real_chain_replay_exact_inputs_match_capture",
                observed={
                    "registry_id": source.registry_id,
                    "candidate_inputs": observed_inputs,
                },
                expected={
                    "registry_id": source.registry_id,
                    "candidate_inputs": expected_inputs,
                },
                detail=f"replayed exact inputs drifted for {source.registry_id}",
            )
        for item in stable_inputs:
            candidate_id = item["capture_candidate_id"]
            if not isinstance(candidate_id, int):
                contradiction(
                    name="real_chain_replay_candidate_ids_are_exact",
                    observed={
                        "registry_id": source.registry_id,
                        "candidate_key": item["candidate_key"],
                        "capture_candidate_id": candidate_id,
                    },
                    expected="integer capture_candidate_id",
                    detail="replayed Candidate has no exact identifier",
                )
            key_by_candidate_id[candidate_id] = item["candidate_key"]

        declare_active_run(
            session, document.id, run.id, principal=_ACCEPTANCE_PRINCIPAL
        )
        run_by_registry[source.registry_id] = run
        active_declarations.append(
            {
                "registry_id": source.registry_id,
                "document_id": document.id,
                "extraction_run_id": run.id,
            }
        )
        raw_runs.append(
            {
                "registry_id": source.registry_id,
                "document_id": document.id,
                "extraction_run_id": run.id,
                "prompt_version": run.prompt_version,
                "model": run.model,
                "schema_version": run.schema_version,
                "outcome": run.outcome,
                "page_errors": run.page_errors,
                "candidate_count": run.candidate_count,
                "candidate_inputs": deepcopy(run.candidate_inputs_json),
            }
        )
        canonical_runs.append(
            {
                "registry_id": source.registry_id,
                "prompt_version": run.prompt_version,
                "model": run.model,
                "schema_version": run.schema_version,
                "outcome": run.outcome,
                "page_errors": run.page_errors,
                "candidate_count": run.candidate_count,
                "candidate_inputs": _inputs_without_database_ids(stable_inputs),
            }
        )
        observations.append(
            project_extraction_observation(
                base_observation=_document_observation(session, document, source),
                document=document,
                stable_inputs=stable_inputs,
                outcome=run.outcome,
                candidate_count=run.candidate_count,
            )
        )

        if ordinal:
            predecessor_registry_id = chain.revisions[ordinal - 1].registry_id
            comparison = create_revision_comparison(
                session,
                run_by_registry[predecessor_registry_id].id,
                run.id,
            )
            readback = read_revision_comparison(session, comparison.id)
            try:
                _assert_partition(readback)
            except AcceptanceError as exc:
                contradiction(
                    name="real_chain_replay_comparisons_are_exact_partitions",
                    observed=_raw_comparison_export(
                        readback,
                        predecessor_registry_id=predecessor_registry_id,
                        successor_registry_id=source.registry_id,
                    ),
                    expected={
                        "comparison_key": f"{predecessor_registry_id}->{source.registry_id}",
                        "partition": "complete and exclusive",
                    },
                    detail=str(exc),
                )
            raw_comparisons.append(
                _raw_comparison_export(
                    readback,
                    predecessor_registry_id=predecessor_registry_id,
                    successor_registry_id=source.registry_id,
                )
            )
            normalized = _normalized_comparison(
                readback,
                predecessor_registry_id=predecessor_registry_id,
                successor_registry_id=source.registry_id,
                key_by_candidate_id=key_by_candidate_id,
            )
            comparison_key = normalized["comparison_key"]
            expected_normalized = _normalized_captured_comparison(
                expected_comparisons[comparison_key], fixture_content["runs"]
            )
            if normalized != expected_normalized:
                contradiction(
                    name="real_chain_replay_comparisons_match_capture",
                    observed=normalized,
                    expected=expected_normalized,
                    detail=f"replayed comparison drifted for {comparison_key}",
                )
            canonical_comparisons.append(normalized)

    ledger_counts = _ledger_counts(session, project.id)
    if any(ledger_counts.values()):
        contradiction(
            name="real_lane_is_observation_only",
            observed=ledger_counts,
            expected={key: 0 for key in ledger_counts},
            detail="real-chain replay wrote to the Ledger",
        )
    raw = _replay_real_raw(
        project_id=project.id,
        documents=documents,
        runs=raw_runs,
        active_run_declarations=active_declarations,
        comparisons=raw_comparisons,
        observations=observations,
        ledger_counts=ledger_counts,
    )
    canonical = _replay_real_canonical(
        documents=documents,
        runs=canonical_runs,
        comparisons=canonical_comparisons,
        observations=observations,
        ledger_counts=ledger_counts,
    )
    return raw, canonical


def _replay_real_raw(
    *,
    project_id: int,
    documents: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
    active_run_declarations: Sequence[dict[str, Any]],
    comparisons: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    ledger_counts: dict[str, Any],
    failure: _ReplayFailureDetail | None = None,
) -> dict[str, Any]:
    payload = {
        "project_id": project_id,
        "claims": CLAIM_BOUNDARY,
        "documents": list(documents),
        "runs": list(runs),
        "active_run_declarations": list(active_run_declarations),
        "comparisons": list(comparisons),
        "observations": list(observations),
        "ledger_counts": deepcopy(ledger_counts),
    }
    if failure is not None:
        payload["replay_failure"] = asdict(failure)
    return payload


def _replay_real_canonical(
    *,
    documents: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
    comparisons: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    ledger_counts: dict[str, Any],
    failure: _ReplayFailureDetail | None = None,
) -> dict[str, Any]:
    payload = {
        "claims": CLAIM_BOUNDARY,
        "documents": [
            {
                "registry_id": item["registry_id"],
                "sha256": item["sha256"],
                "pages": item["pages"],
            }
            for item in documents
        ],
        "runs": list(runs),
        "comparisons": list(comparisons),
        "observations": list(observations),
        "ledger_counts": deepcopy(ledger_counts),
    }
    if failure is not None:
        payload["replay_failure"] = asdict(failure)
    return payload


def _captured_extractor(run_record: dict[str, Any]) -> Extractor:
    """Build Candidates from captured JSON; this seam cannot call a model."""

    def extract(session: Session, document: Document) -> list[Candidate]:
        candidates: list[Candidate] = []
        for item in run_record["candidate_inputs"]:
            payload = deepcopy(item["payload_json"])
            for citation in payload.get("citations") or []:
                if citation.pop("document_registry_id", None) != document.registry_id:
                    raise CorruptAcceptanceFixture(
                        "captured citation registry does not match its document"
                    )
                citation["document_id"] = document.id
            candidate = Candidate(
                project_id=document.project_id,
                kind=item["kind"],
                payload_json=payload,
                source_document_id=document.id,
                source_pages=list(item["source_pages"]),
                confidence=item["confidence"],
                prompt_version=item["prompt_version"],
                model=item["model"],
                citations_verified=item["citations_verified"],
                state=item["state"],
            )
            session.add(candidate)
            candidates.append(candidate)
        return candidates

    return extract


def _inputs_without_database_ids(inputs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in inputs:
        copied = deepcopy(item)
        copied.pop("capture_candidate_id", None)
        normalized.append(copied)
    return normalized


def _raw_comparison_export(
    readback,
    *,
    predecessor_registry_id: str,
    successor_registry_id: str,
) -> dict[str, Any]:
    comparison = readback.comparison
    return {
        "comparison_id": comparison.id,
        "predecessor_registry_id": predecessor_registry_id,
        "successor_registry_id": successor_registry_id,
        "predecessor_extraction_run_id": comparison.predecessor_extraction_run_id,
        "successor_extraction_run_id": comparison.successor_extraction_run_id,
        "matcher_version": comparison.matcher_version,
        "matcher_config": deepcopy(comparison.matcher_config),
        "content_sha256": comparison.content_sha256,
        "finding_count": comparison.finding_count,
        "finding_counts": dict(
            sorted(Counter(item.state for item in readback.findings).items())
        ),
        "predecessor_inputs": deepcopy(list(readback.predecessor_inputs)),
        "successor_inputs": deepcopy(list(readback.successor_inputs)),
        "findings": [
            {
                "finding_id": item.id,
                "ordinal": item.ordinal,
                "state": item.state,
                "predecessor_candidate_ids": list(item.predecessor_candidate_ids),
                "successor_candidate_ids": list(item.successor_candidate_ids),
                "match_score": item.match_score,
                "field_changes": deepcopy(item.field_changes),
                "matcher_detail": deepcopy(item.matcher_detail),
            }
            for item in readback.findings
        ],
    }


def _normalized_comparison(
    readback,
    *,
    predecessor_registry_id: str,
    successor_registry_id: str,
    key_by_candidate_id: dict[int, str],
) -> dict[str, Any]:
    return {
        "comparison_key": (
            f"comparison:{predecessor_registry_id}:{successor_registry_id}"
        ),
        "predecessor_registry_id": predecessor_registry_id,
        "successor_registry_id": successor_registry_id,
        "matcher_version": readback.comparison.matcher_version,
        "matcher_config": deepcopy(readback.comparison.matcher_config),
        "finding_counts": dict(
            sorted(Counter(item.state for item in readback.findings).items())
        ),
        "findings": [
            {
                "ordinal": item.ordinal,
                "state": item.state,
                "predecessor_candidate_keys": [
                    key_by_candidate_id[value]
                    for value in item.predecessor_candidate_ids
                ],
                "successor_candidate_keys": [
                    key_by_candidate_id[value]
                    for value in item.successor_candidate_ids
                ],
                "match_score": item.match_score,
                "field_changes": deepcopy(item.field_changes),
            }
            for item in readback.findings
        ],
    }


def _normalized_captured_comparison(
    comparison: dict[str, Any],
    runs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    key_by_candidate_id = {
        item["capture_candidate_id"]: item["candidate_key"]
        for run in runs
        for item in run["candidate_inputs"]
    }
    return {
        "comparison_key": comparison["comparison_key"],
        "predecessor_registry_id": comparison["predecessor_registry_id"],
        "successor_registry_id": comparison["successor_registry_id"],
        "matcher_version": comparison["matcher_version"],
        "matcher_config": deepcopy(comparison["matcher_config"]),
        "finding_counts": deepcopy(comparison["finding_counts"]),
        "findings": [
            {
                "ordinal": item["ordinal"],
                "state": item["state"],
                "predecessor_candidate_keys": [
                    key_by_candidate_id[value]
                    for value in item["predecessor_candidate_ids"]
                ],
                "successor_candidate_keys": [
                    key_by_candidate_id[value]
                    for value in item["successor_candidate_ids"]
                ],
                "match_score": item["match_score"],
                "field_changes": deepcopy(item["field_changes"]),
            }
            for item in comparison["findings"]
        ],
    }


def _acceptance_assertions(
    real: dict[str, Any],
    controlled: dict[str, Any],
    database: ProvisionedDatabase,
) -> list[AssertionResult]:
    cases = {item["case_id"]: item for item in controlled["cases"]}
    expected_abstentions = {
        "changed": "comparison_changed",
        "dropped": "comparison_dropped",
        "ambiguous": "comparison_ambiguous",
        "fan-in-ambiguous": "comparison_ambiguous",
        "fan-out-ambiguous": "comparison_ambiguous",
        "unmatched": "comparison_unmatched",
        "normalized-only": "successor_fields_not_exact",
        "unverified-citation": "successor_provenance_unsafe",
        "multiple-citations": "successor_provenance_unsafe",
        "human-edited": "successor_fields_not_exact",
    }
    expected_lifecycle = [
        "awaiting_extraction",
        "extraction_failed",
        "awaiting_active_run",
        "awaiting_comparison",
        "actionable",
    ]
    expected_lifecycle_observations = [
        *expected_lifecycle[:-1],
        "unchanged",
    ]
    drift = controlled["policy"]["drift_outcome"]
    checks = [
        AssertionResult(
            "disposable_database_name_is_guarded",
            bool(_DATABASE_NAME.fullmatch(database.name)),
            observed=database.name,
            expected=f"{DATABASE_PREFIX}<32 lowercase hex>",
        ),
        AssertionResult(
            "real_lane_is_observation_only",
            not any(real["ledger_counts"].values()),
            observed=real["ledger_counts"],
            expected={key: 0 for key in real["ledger_counts"]},
        ),
        AssertionResult(
            "real_chain_has_exact_fresh_runs_and_active_declarations",
            len(real["runs"]) == 5
            and len(real["active_run_declarations"]) == 5
            and len({item["extraction_run_id"] for item in real["runs"]}) == 5,
            observed={
                "runs": len(real["runs"]),
                "active": len(real["active_run_declarations"]),
            },
            expected={"runs": 5, "active": 5},
        ),
        AssertionResult(
            "real_comparisons_are_exact_partitions",
            len(real["comparisons"]) == 4,
            observed=len(real["comparisons"]),
            expected=4,
        ),
        AssertionResult(
            "successor_scope_fails_closed",
            controlled["scope_fail_closed"]
            == {
                "default_candidate_hidden": True,
                "historical_override_visible": True,
            },
            observed=controlled["scope_fail_closed"],
            expected={
                "default_candidate_hidden": True,
                "historical_override_visible": True,
            },
        ),
        AssertionResult(
            "superseded_citation_is_immediate",
            controlled["superseded_citation_observed"] is True,
            observed=controlled["superseded_citation_observed"],
            expected=True,
        ),
        AssertionResult(
            "controlled_lifecycle_is_fail_closed",
            [item["review_status"] for item in controlled["lifecycle"]]
            == expected_lifecycle
            and [item["observed_status"] for item in controlled["lifecycle"]]
            == expected_lifecycle_observations,
            observed=[
                item["observed_status"] for item in controlled["lifecycle"]
            ],
            expected=expected_lifecycle_observations,
        ),
        AssertionResult(
            "failed_attempt_is_atomic_and_never_active",
            controlled["failed_attempt"]["outcome"] == "failed"
            and controlled["failed_attempt"]["candidate_count"] == 0
            and controlled["failed_attempt"]["page_errors"] == 1
            and controlled["failed_attempt"][
                "partial_candidate_count_after_failure"
            ]
            == 0
            and controlled["failed_attempt"]["active_at_failure"] is False
            and controlled["failed_attempt"]["committed_before_retry"] is True
            and controlled["failed_attempt"]["comparison_count_after_failure"] == 0,
            observed=controlled["failed_attempt"],
            expected={
                "outcome": "failed",
                "candidate_count": 0,
                "page_errors": 1,
                "partial_candidate_count_after_failure": 0,
                "active_at_failure": False,
                "committed_before_retry": True,
                "comparison_count_after_failure": 0,
            },
        ),
        AssertionResult(
            "controlled_correspondence_matches_the_pinned_oracle",
            all(
                item["expected_correspondence"]
                == item["observed_correspondence"]
                for item in controlled["cases"]
            ),
            observed={
                item["case_id"]: item["observed_correspondence"]
                for item in controlled["cases"]
            },
            expected={
                item["case_id"]: item["expected_correspondence"]
                for item in controlled["cases"]
            },
        ),
        AssertionResult(
            "automatic_carry_is_disabled_by_default",
            controlled["before_policy"]
            == {
                "eligible_route": "human_reconfirmation",
                "automatic_writes": 0,
            },
            observed=controlled["before_policy"],
            expected={
                "eligible_route": "human_reconfirmation",
                "automatic_writes": 0,
            },
        ),
        AssertionResult(
            "exact_support_carries_once_and_is_idempotent",
            controlled["passes"]
            == {
                "first": {"carried": 2},
                "second": {"carried": 0},
                "receipt_count": 2,
            },
            observed=controlled["passes"],
            expected={
                "first": {"carried": 2},
                "second": {"carried": 0},
                "receipt_count": 2,
            },
        ),
        AssertionResult(
            "readiness_is_inherited_but_not_invented",
            cases["readiness-exact"]["inherited_scopes"]
            == ["publication", "readiness"]
            and cases["readiness-exact"]["ready_after"] is True
            and cases["publication-exact"]["inherited_scopes"]
            == ["publication"]
            and cases["publication-exact"]["ready_after"] is False,
            observed={
                "readiness-exact": {
                    "scopes": cases["readiness-exact"]["inherited_scopes"],
                    "ready": cases["readiness-exact"]["ready_after"],
                },
                "publication-exact": {
                    "scopes": cases["publication-exact"]["inherited_scopes"],
                    "ready": cases["publication-exact"]["ready_after"],
                },
            },
        ),
        AssertionResult(
            "unsafe_cases_abstain_without_ledger_mutation",
            all(
                cases[case_id]["outcome"] == "abstained"
                and cases[case_id]["abstention_reason"] == reason
                and cases[case_id]["ledger_unchanged"] is True
                and cases[case_id]["unresolved_after"] is True
                for case_id, reason in expected_abstentions.items()
            ),
            observed={
                case_id: {
                    "reason": cases[case_id]["abstention_reason"],
                    "ledger_unchanged": cases[case_id]["ledger_unchanged"],
                    "unresolved_after": cases[case_id]["unresolved_after"],
                }
                for case_id in expected_abstentions
            },
            expected=expected_abstentions,
        ),
        AssertionResult(
            "fan_out_ambiguity_is_mechanically_real",
            cases["fan-out-ambiguous"]["observed_correspondence"] == "ambiguous"
            and cases["fan-out-ambiguous"]["outcome"] == "abstained"
            and cases["fan-out-ambiguous"]["abstention_reason"]
            == "comparison_ambiguous"
            and cases["fan-out-ambiguous"]["ledger_unchanged"] is True
            and cases["fan-out-ambiguous"]["finding_predecessor_candidate_ids"]
            == [cases["fan-out-ambiguous"]["predecessor_candidate_id"]]
            and cases["fan-out-ambiguous"]["finding_successor_candidate_ids"]
            == cases["fan-out-ambiguous"]["successor_candidate_ids"]
            and len(cases["fan-out-ambiguous"]["successor_candidate_ids"]) == 2,
            observed={
                key: cases["fan-out-ambiguous"][key]
                for key in (
                    "observed_correspondence",
                    "outcome",
                    "abstention_reason",
                    "ledger_unchanged",
                    "predecessor_candidate_id",
                    "finding_predecessor_candidate_ids",
                    "successor_candidate_ids",
                    "finding_successor_candidate_ids",
                )
            },
            expected=(
                "one predecessor and two successors in an ambiguous finding; "
                "comparison_ambiguous abstention with no Ledger mutation"
            ),
        ),
        AssertionResult(
            "automatic_carry_never_performs_admission_or_record_origination",
            controlled["automation_write_boundary"]["new_dependency_rows"] == 0
            and controlled["automation_write_boundary"]["new_assertion_rows"]
            == 0
            and controlled["automation_write_boundary"]
            ["admission_audit_actions_created"]
            == {"human": 0, "machine": 0, "other": 0, "total": 0}
            and bool(
                controlled["automation_write_boundary"]
                ["successor_candidate_states"]
            )
            and set(
                controlled["automation_write_boundary"]
                ["successor_candidate_states"].values()
            )
            == {"pending"}
            and controlled["automation_write_boundary"]
            ["observed_mutation_categories"]
            == controlled["automation_write_boundary"]
            ["allowed_mutation_categories"]
            and all(
                entry["action"] == "automatic_carry_forward"
                and entry["actor"] == "corridor:automatic-carry-forward"
                and entry["human_principal"] is None
                for entry in controlled["automation_write_boundary"]
                ["audit_entries_created"]
            )
            and len(
                controlled["automation_write_boundary"]["audit_entries_created"]
            )
            == controlled["passes"]["receipt_count"],
            observed=controlled["automation_write_boundary"],
            expected=(
                "no Dependency, Assertion, Admission, or Candidate mutation; "
                "only Evidence, Operative Support, carry receipts, and machine audit"
            ),
        ),
        AssertionResult(
            "carried_cases_record_real_ledger_mutation",
            cases["readiness-exact"]["ledger_unchanged"] is False
            and cases["publication-exact"]["ledger_unchanged"] is False,
            observed={
                case_id: cases[case_id]["ledger_unchanged"]
                for case_id in ("readiness-exact", "publication-exact")
            },
            expected={"readiness-exact": False, "publication-exact": False},
        ),
        AssertionResult(
            "policy_drift_pauses_until_reauthorized_then_resumes",
            drift["outcome"] == "paused_then_reauthorized_and_resumed"
            and drift["approval_replaced"] is True
            and drift["initial_approval_preserved"] is True
            and drift["pause"]
            == {
                "carried": 0,
                "reasons": ["comparison_policy_unapproved"],
                "ledger_unchanged": True,
                "active_remained_initial": True,
            }
            and drift["replacement"]["rules_digest"]
            == drift["drifted_rules_digest"]
            and drift["replacement"]["active_pointer_replaced"] is True
            and drift["replacement"]["authorization_count"] == 2
            and drift["resume"]
            == {
                "carried": 1,
                "receipt_bound_to_replacement": True,
                "idempotent_second_carried": 0,
                "comparison_count": 1,
                "comparison_preserved": True,
            },
            observed=drift,
            expected="pause, replacement authorization, one carry, idempotence",
        ),
        AssertionResult(
            "claims_are_mechanical_only",
            real["claims"] == CLAIM_BOUNDARY
            and controlled["claim_boundary"] == CLAIM_BOUNDARY,
            observed={"real": real["claims"], "controlled": controlled["claim_boundary"]},
            expected=CLAIM_BOUNDARY,
        ),
    ]
    return checks


def _write_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    real_chain: dict[str, Any],
    controlled_lane: dict[str, Any],
    assertions: Sequence[AssertionResult],
    canonical_content: dict[str, Any],
) -> tuple[Path, str, str]:
    return _write_bundle_impl(
        output_dir,
        environment=environment,
        real_chain=real_chain,
        controlled_lane=controlled_lane,
        assertions=assertions,
        canonical_content=canonical_content,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=_BUNDLE_FILES,
        error_cls=AcceptanceError,
        corrupt_bundle_error_cls=CorruptAcceptanceBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
