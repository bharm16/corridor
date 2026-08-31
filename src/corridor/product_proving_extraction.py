"""One exact current extraction attempt for Product Proving.

Product Proving accepts an ``ExtractionOperation`` callback, but constructing
that callback at a call site exposed all of the dangerous choices: which
extractor version to run, which prompt/schema pair to pass, whether to redo an
already-extracted Document, and whether the extractor was allowed to commit.
This module makes those choices once.  Its public seam takes one persisted
Document and a caller-owned model client, runs the current deployed reader in
the caller's transaction, then derives the new Extraction Run from the
database rather than trusting an extractor return value.

The model client is deliberately not closed here.  A proving pass shares one
client across its bounded Documents so provider counters and connection reuse
remain coherent.  The proving executor owns durability; this adapter always
uses ``commit=False``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.extractor_lineage import (
    ExtractorConfig,
    canonical_json_bytes,
    deployed_extractor_config,
    validate_config_json_shape,
)
from corridor.facts import proposal_input_snapshots
from corridor.models import Candidate, Document, ExtractionRun, Project


class ProductProvingExtractionError(ValueError):
    """The bounded attempt did not produce one exact current sealed run."""


_ExtractionImplementation = Callable[[Session, Document, object], None]
_REPO_ROOT = Path(__file__).resolve().parents[2]


def extract_product_proving_document(
    session: Session,
    document: Document,
    *,
    client: object,
) -> int:
    """Run and return one current sealed Extraction Run for ``document``.

    Only Matrix v3 and Minutes v5 Documents are in the proving contract.  The
    operation refuses a missing, widened, failed, legacy-unsealed, or
    configuration-stale attempt.  It does not commit and it does not change an
    Active Run.
    """

    exact_document = _require_exact_document(session, document)
    implementation, extractor_name = _implementation(exact_document.doc_type)
    expected_config = deployed_extractor_config(extractor_name, client=client)
    before_run_ids = set(session.scalars(select(ExtractionRun.id)).all())

    implementation(session, exact_document, client)
    session.flush()

    query = select(ExtractionRun).order_by(ExtractionRun.id)
    if before_run_ids:
        query = query.where(ExtractionRun.id.not_in(before_run_ids))
    new_runs = tuple(session.scalars(query).all())
    if len(new_runs) != 1:
        raise ProductProvingExtractionError(
            "bounded extraction must create exactly one Extraction Run; "
            f"observed {len(new_runs)}"
        )

    run = new_runs[0]
    _validate_run(
        session,
        run=run,
        document=exact_document,
        expected_config=expected_config,
        extractor_name=extractor_name,
    )
    return run.id


def _require_exact_document(session: Session, document: Document) -> Document:
    if not isinstance(document, Document):
        raise TypeError("Product Proving extraction requires a Document")
    if (
        isinstance(document.id, bool)
        or not isinstance(document.id, int)
        or document.id <= 0
    ):
        raise ProductProvingExtractionError(
            "Product Proving extraction requires a persisted Document"
        )

    supplied_identity = (
        document.id,
        document.project_id,
        document.sha256,
        document.doc_type,
    )
    with session.no_autoflush:
        persisted = session.get(Document, document.id, populate_existing=True)
    if persisted is None:
        raise ProductProvingExtractionError("Product Proving Document does not exist")
    persisted_identity = (
        persisted.id,
        persisted.project_id,
        persisted.sha256,
        persisted.doc_type,
    )
    if supplied_identity != persisted_identity:
        raise ProductProvingExtractionError(
            "supplied Document does not match its persisted project/document identity"
        )
    if session.get(Project, persisted.project_id) is None:
        raise ProductProvingExtractionError(
            "Product Proving Document does not belong to a persisted Project"
        )
    return persisted


def _implementation(doc_type: str) -> tuple[_ExtractionImplementation, str]:
    if doc_type == "matrix":
        return _extract_matrix_document, "matrix"
    if doc_type == "minutes":
        return _extract_minutes_document, "minutes"
    raise ProductProvingExtractionError(
        f"Product Proving supports only matrix and minutes Documents; got {doc_type!r}"
    )


def _extract_matrix_document(
    session: Session,
    document: Document,
    client: object,
) -> None:
    """Run the current sealed Matrix route for one exact registered file."""

    from corridor import extract_matrix
    from corridor.extract_project import extract_project
    from corridor.pipeline import ExtractionRoute, extraction_route

    project = session.get(Project, document.project_id)
    assert project is not None  # checked at the public boundary

    def select_current_route(target: Document) -> ExtractionRoute:
        route = extraction_route(target, client=client)
        config = route.extractor_config
        if (
            route.effective_prompt_version != extract_matrix.PROMPT_VERSION
            or route.schema_version != extract_matrix.SCHEMA_VERSION
            or config is None
            or config.config_json.get("extractor") != "matrix"
        ):
            raise ProductProvingExtractionError(
                "Document does not select the current sealed Matrix v3 route"
            )
        return route

    outcomes = extract_project(
        session,
        project,
        select_route=select_current_route,
        redo=True,
        commit=False,
        document_sha256=document.sha256,
    )
    if len(outcomes) != 1 or outcomes[0].document_id != document.id:
        raise ProductProvingExtractionError(
            "Matrix extraction widened beyond the exact Product Proving Document"
        )


def _extract_minutes_document(
    session: Session,
    document: Document,
    client: object,
) -> None:
    """Run Minutes v5 with deterministic Action Items and its exact seal."""

    from corridor.extract_batch import extract_documents
    from corridor.extract_minutes_v5 import (
        MIN_PAGE_CHARS,
        PROMPT_PATH,
        PROMPT_VERSION,
        SCHEMA,
        extract_page_candidates,
        to_candidate,
    )

    config = deployed_extractor_config("minutes", client=client)
    extract_documents(
        session,
        [document],
        client=client,
        system=(_REPO_ROOT / PROMPT_PATH).read_text(),
        schema=SCHEMA,
        min_page_chars=MIN_PAGE_CHARS,
        to_candidate=to_candidate,
        page_candidates=extract_page_candidates,
        items_key="events",
        prompt_version=PROMPT_VERSION,
        extractor_config=config,
        commit=False,
    )


def _validate_run(
    session: Session,
    *,
    run: ExtractionRun,
    document: Document,
    expected_config: ExtractorConfig,
    extractor_name: str,
) -> None:
    if run.document_id != document.id:
        actual = session.get(Document, run.document_id)
        actual_project = actual.project_id if actual is not None else None
        raise ProductProvingExtractionError(
            "bounded extraction created a run for the wrong Document/Project: "
            f"document={run.document_id}, project={actual_project}"
        )
    if run.outcome != "completed" or run.page_errors != 0:
        raise ProductProvingExtractionError(
            "bounded extraction did not create a completed zero-error run"
        )

    config_json = run.extractor_config_json
    if not isinstance(config_json, Mapping):
        raise ProductProvingExtractionError(
            "bounded extraction created an unsealed legacy run"
        )
    try:
        validate_config_json_shape(config_json)
    except (TypeError, ValueError) as error:
        raise ProductProvingExtractionError(
            f"bounded extraction configuration receipt is invalid: {error}"
        ) from error

    stored_digest = sha256(canonical_json_bytes(config_json)).hexdigest()
    if (
        config_json.get("extractor") != extractor_name
        or stored_digest != run.extractor_config_sha256
        or run.extractor_config_sha256 != expected_config.config_sha256
        or dict(config_json) != expected_config.config_json
        or run.prompt_version != expected_config.prompt_version
        or run.model != expected_config.model
        or run.schema_version != expected_config.schema_version
        or run.prompt_sha256 != expected_config.prompt_sha256
        or run.schema_sha256 != expected_config.schema_sha256
        or run.postprocessor_sha256 != expected_config.postprocessor_sha256
    ):
        raise ProductProvingExtractionError(
            "bounded extraction run does not match the current deployed extractor seal"
        )

    _validate_token_usage(run.token_usage_json, document.id)
    candidate_count = (
        len(proposal_input_snapshots(session, run))
        if run.candidate_inputs_json is None
        else session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.extraction_run_id == run.id)
        )
    )
    snapshots = (
        run.candidate_inputs_json
        if run.candidate_inputs_json is not None
        else proposal_input_snapshots(session, run)
    )
    if (
        candidate_count != run.candidate_count
        or not isinstance(snapshots, list)
        or len(snapshots) != run.candidate_count
        or any(
            item.get("project_id") != document.project_id
            or item.get("source_document_id") != document.id
            for item in snapshots
            if isinstance(item, Mapping)
        )
        or any(not isinstance(item, Mapping) for item in snapshots)
    ):
        raise ProductProvingExtractionError(
            "bounded extraction Candidate snapshot does not match its Document/Run"
        )


def _validate_token_usage(value: Any, document_id: int) -> None:
    if not isinstance(value, Mapping):
        raise ProductProvingExtractionError(
            "bounded extraction run has no sealed token-usage receipt"
        )
    if value.get("scope") != "run" or value.get("document_ids") != [document_id]:
        raise ProductProvingExtractionError(
            "bounded extraction token usage does not name only the exact Document"
        )
    measurement = value.get("measurement")
    if measurement == "unavailable":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ProductProvingExtractionError(
                "unavailable token usage must carry an honest reason"
            )
        return
    if measurement != "exact":
        raise ProductProvingExtractionError(
            "bounded extraction token usage measurement is invalid"
        )
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
    ):
        count = value.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProductProvingExtractionError(
                "bounded extraction exact token counts must be non-negative integers"
            )
