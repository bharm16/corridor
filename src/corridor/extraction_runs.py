"""Authoritative lineage for immutable extraction attempts.

Receipts, Candidate attachment, resume selection, and Active Run declaration
meet here.  Keeping those operations together prevents a reader from silently
reconstructing lineage from candidate existence, timestamps, or row order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sys

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from corridor import audit
from corridor.extractor_lineage import (
    ExtractorConfig,
    canonical_json_bytes,
    validate_config_json_shape,
)
from corridor.facts import (
    append_extracted_proposals,
    append_statement_wording_facts,
    append_structured_cell_facts,
)
from corridor.models import (
    EXTRACTION_OUTCOMES,
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Candidate,
    Document,
    ExtractionRun,
    ExtractionRunCandidate,
    ExtractorConfiguration,
    Fact,
    SourceFactAppendReceipt,
    SourceSegment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.record_inclusion import request_record_inclusion
from corridor.revision_reconciliation_request import request_revision_reconciliation
from corridor.row_accounting import validate_row_accounting
from corridor.source_append import append_source_fact_receipt
from corridor.source_segments import (
    append_ingested_source_segments,
    dereference_source_segment,
)


def completion_predicate():
    """Which runs count as completed for resume/eval selection.

    Only a document with zero page failures is complete. A successful zero-row
    read still counts because it has `page_errors == 0`. A document with any
    failed page must retry as a whole, so its run is history, not completion,
    even if some pages appeared to yield candidates before the failure.
    """
    return and_(
        ExtractionRun.outcome == "completed", ExtractionRun.page_errors == 0
    )


def is_completed_run(run: ExtractionRun) -> bool:
    """Object-level form of the one extraction completion rule."""

    return run.outcome == "completed" and run.page_errors == 0


def completed_document_ids(
    session: Session, project_id: int, *, prompt_version: str | None = None
) -> set[int]:
    query = (
        select(ExtractionRun.document_id)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, completion_predicate())
    )
    if prompt_version is not None:
        query = query.where(ExtractionRun.prompt_version == prompt_version)
    return set(session.scalars(query.distinct()).all())


class SourceFactAppendConflict(ValueError):
    """An idempotency key was reused for different scoped append content."""


@dataclass(frozen=True)
class SourceFactAppendResult:
    """The original or newly created rows for one scoped append command."""

    run: ExtractionRun
    facts: tuple[Fact, ...]
    created: bool


def record_extraction_run(
    session: Session,
    document: Document,
    **values,
) -> ExtractionRun:
    """Append one run receipt; callers with spine Facts use ``append_source_facts``."""

    return _record_extraction_run(session, document, **values)


def append_native_matrix_source_facts(
    session: Session, document: Document, *, mapping,
    source_path: str | Path, idempotency_key: str | None,
    token_usage: Mapping[str, object], fail_after_stage: str | None = None,
) -> dict:
    """Atomic native capture: stored sources, sealed Facts, then compatibility.

    The existing spreadsheet command needs Candidate IDs while materializing
    its cells. Native IDs already name the source row, so this route can append
    Facts first and never stage a legacy-only result. It neither selects a
    production run nor requests legacy automatic Record Inclusion.
    """
    from corridor.facts import (
        append_native_matrix_facts, materialize_native_matrix_fields,
        native_matrix_candidates,
    )
    from corridor.native_matrix_bindings import ACCOUNTING_SCHEMA, NativeMatrixMapping
    from corridor.reader_segments import append_native_segments

    if not isinstance(mapping, NativeMatrixMapping):
        raise TypeError("native source append requires its sealed mapping")
    mapping.certify(document)
    if sha256(Path(source_path).read_bytes()).hexdigest() != document.sha256:
        raise ValueError("native source append bytes do not match the Document")
    if fail_after_stage not in {None, "segments", "run", "facts", "proposals", "receipt"}:
        raise ValueError("unknown native append failure stage")
    config = ExtractorConfig(**mapping.config_record)
    content_sha256 = mapping.identity
    key = idempotency_key or f"native-matrix:{content_sha256}"
    lock_project(session, document.project_id)
    existing = session.scalar(select(SourceFactAppendReceipt).where(
        SourceFactAppendReceipt.project_id == document.project_id,
        SourceFactAppendReceipt.idempotency_key == key,
    ))
    if existing is None:
        existing = session.scalar(select(SourceFactAppendReceipt).where(
            SourceFactAppendReceipt.project_id == document.project_id,
            SourceFactAppendReceipt.content_sha256 == content_sha256,
        ))
    if existing is not None:
        if existing.content_sha256 != content_sha256:
            raise SourceFactAppendConflict("native append key names another mapping")
        return _native_append_result(session, existing.extraction_run_id, created=False)

    with session.begin_nested():
        append_native_segments(session, document, mapping.native_reading)
        _fail_after(fail_after_stage, "segments")
        prepared = materialize_native_matrix_fields(session, document, mapping)
        accounting = {
            **mapping.row_accounting, "schema_version": ACCOUNTING_SCHEMA,
            "native_mapping": {
                "identity": mapping.identity,
                "reading_sha256": mapping.native_reading.reading_sha256,
                "pages": mapping.pages,
            },
            "field_materialization": [field.outcome for field in prepared],
        }
        count = accounting["extracted_row_count"]
        validate_row_accounting(accounting, prompt_version=config.prompt_version,
                                outcome="completed", candidate_count=count)
        lineage, sealed = _validated_lineage(
            document=document, prompt_version=config.prompt_version, model=config.model,
            schema_version=config.schema_version, extractor_config=config,
            token_usage=token_usage, allow_unsealed_legacy=False,
        )
        register_extractor_configuration(session, config_json=sealed,
                                         config_sha256=config.config_sha256)
        run = ExtractionRun(
            document_id=document.id, prompt_version=config.prompt_version,
            outcome="completed", candidate_count=count, page_errors=0,
            model=config.model, schema_version=config.schema_version,
            candidate_inputs_json=None, row_accounting_json=accounting, **lineage,
        )
        session.add(run)
        session.flush([run])
        _fail_after(fail_after_stage, "run")
        facts = append_native_matrix_facts(session, document, run, prepared)
        _fail_after(fail_after_stage, "facts")
        candidates = native_matrix_candidates(document, mapping, prepared)
        if len(candidates) != count:
            raise ValueError("native candidates do not match sealed row accounting")
        session.add_all(candidates)
        session.flush()
        for candidate in candidates:
            session.add(ExtractionRunCandidate(extraction_run_id=run.id, candidate_id=candidate.id))
        append_extracted_proposals(session, document, run, candidates, facts)
        for candidate in candidates:
            candidate.extraction_run_id = run.id
        session.flush()
        _fail_after(fail_after_stage, "proposals")
        append_source_fact_receipt(
            session, project_id=document.project_id, document_id=document.id,
            extraction_run_id=run.id, idempotency_key=key, content_sha256=content_sha256,
        )
        session.flush()
        _fail_after(fail_after_stage, "receipt")
    return _native_append_result(session, run.id, created=True)


def _native_append_result(session: Session, run_id: int, *, created: bool) -> dict:
    run = session.get_one(ExtractionRun, run_id)
    return {
        "run": run,
        "facts": tuple(session.scalars(select(Fact).where(Fact.extraction_run_id == run_id).order_by(Fact.id)).all()),
        "candidates": tuple(session.scalars(select(Candidate).where(Candidate.extraction_run_id == run_id).order_by(Candidate.id)).all()),
        "field_outcomes": tuple(deepcopy(run.row_accounting_json["field_materialization"])),
        "created": created,
    }


def append_source_facts(
    session: Session,
    document: Document,
    *,
    idempotency_key: str | None,
    prompt_version: str,
    candidate_count: int,
    page_errors: int,
    candidates: Sequence[Candidate],
    outcome: str = "completed",
    model: str | None = None,
    schema_version: str | None = None,
    error_detail: str | None = None,
    extractor_config: ExtractorConfig | None = None,
    token_usage: Mapping[str, object] | None = None,
    row_accounting_json: dict | None = None,
    allow_unsealed_legacy: bool = False,
    source_path: str | Path | None = None,
    fail_after_stage: str | None = None,
) -> SourceFactAppendResult:
    """Atomically validate and append one rendition's run, segments, and Facts."""

    candidates = tuple(candidates)
    run_values = {
        "prompt_version": prompt_version,
        "candidate_count": candidate_count,
        "page_errors": page_errors,
        "candidates": candidates,
        "outcome": outcome,
        "model": model,
        "schema_version": schema_version,
        "error_detail": error_detail,
        "extractor_config": extractor_config,
        "token_usage": token_usage,
        "row_accounting_json": row_accounting_json,
        "allow_unsealed_legacy": allow_unsealed_legacy,
    }
    content = {
        "document_sha256": document.sha256,
        "prompt_version": run_values.get("prompt_version"),
        "schema_version": run_values.get("schema_version"),
        "model": run_values.get("model"),
        "extractor_config_sha256": (
            extractor_config.config_sha256 if extractor_config is not None else None
        ),
        "token_usage": deepcopy(token_usage),
        "candidate_count": run_values.get("candidate_count"),
        "page_errors": run_values.get("page_errors"),
        "row_accounting_json": run_values.get("row_accounting_json"),
        "candidates": [
            {
                "kind": candidate.kind,
                "payload_json": deepcopy(candidate.payload_json),
                "source_pages": list(candidate.source_pages or []),
                "confidence": candidate.confidence,
                "prompt_version": candidate.prompt_version,
                "model": candidate.model,
            }
            for candidate in candidates
        ],
    }
    content_sha256 = sha256(canonical_json_bytes(content)).hexdigest()
    key = idempotency_key or f"content:{content_sha256}"
    lock_project(session, document.project_id)
    existing = session.scalar(
        select(SourceFactAppendReceipt).where(
            SourceFactAppendReceipt.project_id == document.project_id,
            SourceFactAppendReceipt.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.content_sha256 != content_sha256:
            raise SourceFactAppendConflict(
                "source Fact append key is already bound to different content"
            )
    else:
        existing = session.scalar(
            select(SourceFactAppendReceipt).where(
                SourceFactAppendReceipt.project_id == document.project_id,
                SourceFactAppendReceipt.content_sha256 == content_sha256,
            )
        )
    if existing is not None:
        run = session.get(ExtractionRun, existing.extraction_run_id)
        if run is None:
            raise SourceFactAppendConflict("source Fact append receipt lost its run")
        facts = tuple(
            session.scalars(
                select(Fact)
                .where(Fact.extraction_run_id == run.id)
                .order_by(Fact.id)
            ).all()
        )
        return SourceFactAppendResult(run, facts, False)

    if source_path is None:
        raise ValueError("new source Fact append requires original source bytes")

    allowed_stages = {None, "segments", "run", "facts", "receipt"}
    if fail_after_stage not in allowed_stages:
        raise ValueError("unknown source Fact append failure stage")
    with session.begin_nested():
        append_ingested_source_segments(session, document, source_path)
        session.flush()
        segments = tuple(
            session.scalars(
                select(SourceSegment)
                .where(SourceSegment.document_id == document.id)
                .order_by(SourceSegment.ordinal)
            ).all()
        )
        if not segments:
            raise ValueError("source Fact append requires rendition segments")
        for segment in segments:
            if segment.project_id != document.project_id:
                raise ValueError("source segment crosses project scope")
            if sha256(segment.exact_text.encode()).hexdigest() != segment.content_sha256:
                raise ValueError("source segment digest is invalid")
            dereference_source_segment(document, segment, source_path)
        _fail_after(fail_after_stage, "segments")
        run = _record_extraction_run(
            session,
            document,
            _capture_candidate_inputs=False,
            scoped_fact_append=True,
            **run_values,
        )
        _fail_after(fail_after_stage, "run")
        facts = tuple(
            session.scalars(
                select(Fact)
                .where(Fact.extraction_run_id == run.id)
                .order_by(Fact.id)
            ).all()
        )
        for candidate in candidates:
            session.add(
                ExtractionRunCandidate(
                    extraction_run_id=run.id, candidate_id=candidate.id
                )
            )
        session.flush()
        append_extracted_proposals(session, document, run, candidates, facts)
        for candidate in candidates:
            candidate.extraction_run_id = run.id
        session.flush()
        _fail_after(fail_after_stage, "facts")
        append_source_fact_receipt(
            session,
            project_id=document.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            idempotency_key=key,
            content_sha256=content_sha256,
        )
        session.flush()
        _fail_after(fail_after_stage, "receipt")
    return SourceFactAppendResult(run, facts, True)


def _fail_after(requested: str | None, stage: str) -> None:
    if requested == stage:
        raise RuntimeError(f"injected source Fact append failure after {stage}")


def _record_extraction_run(
    session: Session,
    document: Document,
    *,
    prompt_version: str,
    candidate_count: int,
    page_errors: int,
    outcome: str = "completed",
    candidates: Sequence[Candidate] = (),
    model: str | None = None,
    schema_version: str | None = None,
    error_detail: str | None = None,
    extractor_config: ExtractorConfig | None = None,
    token_usage: Mapping[str, object] | None = None,
    row_accounting_json: dict | None = None,
    allow_unsealed_legacy: bool = False,
    _capture_candidate_inputs: bool = True,
    scoped_fact_append: bool = False,
) -> ExtractionRun:
    """Append one terminal attempt and attach every Candidate it produced."""
    if not prompt_version:
        raise ValueError("prompt_version must be non-empty")
    if outcome not in EXTRACTION_OUTCOMES:
        raise ValueError(f"unknown extraction outcome {outcome!r}")
    if candidate_count < 0 or page_errors < 0:
        raise ValueError("candidate_count and page_errors must be non-negative")
    if outcome == "completed" and page_errors:
        raise ValueError("a completed extraction run cannot have page errors")
    if outcome != "completed" and (candidate_count or candidates):
        raise ValueError("a non-completed extraction run cannot own Candidates")
    if outcome != "completed" and page_errors == 0:
        raise ValueError("a non-completed extraction run must record an error")
    if candidate_count != len(candidates):
        raise ValueError("candidate_count does not match the attached candidates")
    if len({id(candidate) for candidate in candidates}) != len(candidates):
        raise ValueError("an Extraction Run cannot repeat a Candidate")
    row_accounting = validate_row_accounting(
        deepcopy(row_accounting_json),
        prompt_version=prompt_version,
        outcome=outcome,
        candidate_count=candidate_count,
    )
    lineage, sealed_configuration = _validated_lineage(
        document=document,
        prompt_version=prompt_version,
        model=model,
        schema_version=schema_version,
        extractor_config=extractor_config,
        token_usage=token_usage,
        allow_unsealed_legacy=allow_unsealed_legacy,
    )
    for candidate in candidates:
        if candidate.extraction_run_id is not None:
            raise ValueError(
                f"Candidate {candidate.id} already belongs to Extraction Run "
                f"{candidate.extraction_run_id}"
            )
        if candidate.source_document_id != document.id:
            raise ValueError("a run cannot own another document's Candidate")
        if candidate.project_id != document.project_id:
            raise ValueError("a run cannot own another project's Candidate")
        if candidate.prompt_version != prompt_version:
            raise ValueError("Candidate prompt_version does not match its run")
        if candidate.model != model:
            raise ValueError("Candidate model does not match its run")

    if candidates:
        session.add_all(candidates)
        # Candidate ids are part of the immutable input identity. They can be
        # assigned before the run because lineage is nullable only during this
        # one in-transaction construction step.
        session.flush(list(candidates))
        candidate_ids = [candidate.id for candidate in candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("an Extraction Run cannot repeat a Candidate")
    candidate_inputs = (
        [candidate_input_snapshot(candidate) for candidate in candidates]
        if _capture_candidate_inputs
        else None
    )

    if sealed_configuration is not None:
        register_extractor_configuration(
            session,
            config_json=sealed_configuration,
            config_sha256=lineage["extractor_config_sha256"],
        )
    run = ExtractionRun(
        document_id=document.id,
        prompt_version=prompt_version,
        outcome=outcome,
        candidate_count=candidate_count,
        page_errors=page_errors,
        model=model,
        schema_version=schema_version,
        error_detail=error_detail,
        candidate_inputs_json=candidate_inputs,
        row_accounting_json=row_accounting,
        **lineage,
    )
    session.add(run)
    session.flush([run])
    if _capture_candidate_inputs:
        for candidate in candidates:
            candidate.extraction_run_id = run.id
    if outcome == "completed":
        append_structured_cell_facts(session, document, run, tuple(candidates))
        has_prose_segments = session.scalar(
            select(SourceSegment.id)
            .where(
                SourceSegment.document_id == document.id,
                SourceSegment.kind == "prose_span",
            )
            .limit(1)
        )
        if scoped_fact_append and has_prose_segments is not None:
            append_statement_wording_facts(session, document, run, tuple(candidates))
        # The one producer #342 owns: a completed reading leaves the project's
        # Record Inclusion needing reconciliation. Bumping the durable watermark
        # in this same transaction is what makes a crash after the extraction
        # commit but before the load recoverable — the completed run is on the
        # record if and only if the project is marked pending (#342, ADR-0029).
        request_record_inclusion(
            session, document.project_id, "extraction_completed"
        )
    return run


def register_extractor_configuration(
    session: Session,
    *,
    config_json: Mapping[str, object],
    config_sha256: str,
) -> ExtractorConfiguration:
    """Store one sealed configuration once, keyed by its digest (#605).

    Every run of one deployed extractor seals a byte-identical receipt, so the
    second and thousandth run find the row the first one registered and
    reference it.  Two workers that seal the same configuration at the same
    moment race for that first insert; the loser converges on the winner's row
    rather than failing an extraction over a value both of them computed
    identically.
    """

    existing = session.get(ExtractorConfiguration, config_sha256)
    if existing is None:
        try:
            with session.begin_nested():
                registered = ExtractorConfiguration(
                    config_sha256=config_sha256,
                    config_json=dict(config_json),
                )
                session.add(registered)
                session.flush([registered])
                return registered
        except IntegrityError:
            existing = session.get(ExtractorConfiguration, config_sha256)
            if existing is None:
                raise
    if existing.config_json != dict(config_json):
        # The digest is over the receipt, so this is only reachable if a row
        # was stored under a digest that is not its own. Refuse rather than
        # silently binding the run to a configuration it did not run.
        raise ValueError(
            f"stored extractor configuration {config_sha256} does not match "
            "the sealed receipt this run was produced under"
        )
    return existing


def extractor_configuration(
    session: Session, run: ExtractionRun
) -> dict | None:
    """The sealed receipt a run was produced under, or ``None`` if unknown.

    ``None`` is a real answer, not a missing one: a run written before the
    seal existed has no configuration recorded, and reconstructing one from
    whatever is deployed today would read exactly like a measurement while
    being an invention.
    """

    if run.extractor_config_sha256 is None:
        return None
    stored = session.get(ExtractorConfiguration, run.extractor_config_sha256)
    if stored is not None:
        return deepcopy(stored.config_json)
    # A legacy row whose inline copy has not been registered. The foreign key
    # makes this unreachable in a migrated database; reading the copy rather
    # than raising keeps an unmigrated one honest instead of blank.
    return deepcopy(run.extractor_config_json)


def _validated_lineage(
    *,
    document: Document,
    prompt_version: str,
    model: str | None,
    schema_version: str | None,
    extractor_config: ExtractorConfig | None,
    token_usage: Mapping[str, object] | None,
    allow_unsealed_legacy: bool,
) -> tuple[dict, dict | None]:
    """Validate one all-or-nothing sealed receipt before database insertion."""

    if extractor_config is None and token_usage is None:
        if allow_unsealed_legacy is True:
            # Explicit historical/synthetic construction remains
            # representable. A migration cannot infer exact prompt or rule
            # bytes for rows created before this contract.
            return {}, None
        raise ValueError(
            "new Extraction Runs require exact extractor configuration and "
            "token usage; pass allow_unsealed_legacy=True only for an "
            "explicit historical or synthetic fixture"
        )
    if allow_unsealed_legacy is not False:
        raise ValueError("a sealed Extraction Run cannot use the legacy escape hatch")
    if extractor_config is None or token_usage is None:
        raise ValueError(
            "extractor_config and token_usage must be recorded together"
        )
    if extractor_config.prompt_version != prompt_version:
        raise ValueError("extractor configuration prompt_version does not match")
    if extractor_config.model != model:
        raise ValueError("extractor configuration model does not match")
    if extractor_config.schema_version != schema_version:
        raise ValueError("extractor configuration schema_version does not match")

    config_json = deepcopy(extractor_config.config_json)
    validate_config_json_shape(config_json)
    expected_config = {
        "prompt_version": prompt_version,
        "model": model,
        "schema_version": schema_version,
        "prompt_sha256": extractor_config.prompt_sha256,
        "schema_sha256": extractor_config.schema_sha256,
        "postprocessor_sha256": extractor_config.postprocessor_sha256,
    }
    for name, expected in expected_config.items():
        if config_json.get(name) != expected:
            raise ValueError(f"extractor configuration {name} is inconsistent")
    digest = sha256(canonical_json_bytes(config_json)).hexdigest()
    if digest != extractor_config.config_sha256:
        raise ValueError("extractor configuration SHA-256 is inconsistent")

    usage = deepcopy(dict(token_usage))
    scope = usage.get("scope")
    members = usage.get("document_ids")
    if scope not in {"run", "batch"} or not isinstance(members, list):
        raise ValueError("token usage requires run or batch document membership")
    if any(
        isinstance(member, bool) or not isinstance(member, int) or member <= 0
        for member in members
    ) or len(set(members)) != len(members):
        raise ValueError(
            "token usage document membership must contain unique positive integers"
        )
    if document.id not in members:
        raise ValueError("token usage does not include the Extraction Run document")
    if scope == "run" and members != [document.id]:
        raise ValueError("run-scoped token usage must name only its document")
    if scope == "batch" and len(members) < 2:
        raise ValueError("batch-scoped token usage must name the complete batch")

    if usage.get("measurement") == "unavailable":
        if not str(usage.get("reason") or "").strip():
            raise ValueError("unavailable token usage must state why")
    elif usage.get("measurement") == "exact":
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    "measured token usage requires non-negative integer counts"
                )
    else:
        raise ValueError("token usage measurement must be exact or unavailable")

    # The receipt is returned beside the lineage rather than in it: the run
    # stores the digest, and the configuration itself is stored once in
    # ``extractor_configurations`` (#605).
    return (
        {
            "prompt_sha256": extractor_config.prompt_sha256,
            "schema_sha256": extractor_config.schema_sha256,
            "postprocessor_sha256": extractor_config.postprocessor_sha256,
            "extractor_config_sha256": extractor_config.config_sha256,
            "token_usage_json": usage,
        },
        config_json,
    )


def candidate_input_snapshot(candidate: Candidate) -> dict:
    """The immutable extractor-time input a run owns.

    ``extraction_run_id`` is intentionally absent: the containing
    ExtractionRun supplies that identity and does not exist until after this
    snapshot is assembled. Every other value the comparison may later need is
    copied before Adjudication can edit the live Candidate.
    """

    return {
        "candidate_id": candidate.id,
        "project_id": candidate.project_id,
        "kind": candidate.kind,
        "source_document_id": candidate.source_document_id,
        "payload_json": deepcopy(candidate.payload_json),
        "source_pages": list(candidate.source_pages or []),
        "confidence": candidate.confidence,
        "prompt_version": candidate.prompt_version,
        "model": candidate.model,
        "citations_verified": candidate.citations_verified,
        "state": candidate.state,
    }


def declare_active_run(
    session: Session,
    document_id: int,
    extraction_run_id: int,
    *,
    principal: HumanPrincipal,
) -> ExtractionRun:
    """Declare a completed run operative; never infer one from recency.

    The declarer is recorded, declarations append rather than overwrite,
    and re-declaring the run already current records nothing new — an
    identical rerun does not duplicate an outcome.
    ``active_extraction_runs`` stays the one-row projection readers join,
    maintained to equal the chain tail.
    """
    return _declare(
        session,
        document_id,
        extraction_run_id,
        declared_by=require_human_principal(principal).subject,
    )


def declare_active_run_by_policy(
    session: Session, document_id: int, extraction_run_id: int
) -> ExtractionRun:
    """Declare a run the machine chose, under the declaring actor.

    Only the unambiguous case reaches here (see
    ``declare_single_run_documents_by_policy``): choosing between several
    completed runs is the inference Active Runs exist to forbid, and it
    stays a human act. What the machine does is name the only reading a
    document has, which is a fact rather than a choice — and ADR-0029
    put it in the pipeline so a project is not dark until someone says so.
    """
    return _declare(
        session,
        document_id,
        extraction_run_id,
        declared_by=audit.ACTIVE_RUN_DECLARATION_ACTOR,
        request_inclusion=False,
    )


def _declare(
    session: Session,
    document_id: int,
    extraction_run_id: int,
    *,
    declared_by: str,
    request_inclusion: bool = True,
) -> ExtractionRun:
    project_id = session.scalar(
        select(Document.project_id).where(Document.id == document_id)
    )
    if project_id is None:
        raise ValueError("document does not exist")
    lock_project(session, project_id)

    run = session.scalar(
        select(ExtractionRun).where(
            ExtractionRun.id == extraction_run_id,
            ExtractionRun.document_id == document_id,
        )
    )
    if run is None:
        raise ValueError("extraction run does not belong to the document")
    if run.outcome != "completed" or run.page_errors != 0:
        raise ValueError("only a completed extraction run can be active")

    current = session.get(
        ActiveExtractionRun,
        document_id,
        populate_existing=True,
    )
    tail = current_active_run_declaration(session, document_id)
    current_run_id = current.extraction_run_id if current is not None else None
    tail_run_id = tail.extraction_run_id if tail is not None else None
    if current_run_id != tail_run_id:
        raise ValueError(
            "the active run projection diverged from its declaration history"
        )
    if tail_run_id == extraction_run_id:
        return run

    session.add(
        ActiveRunDeclaration(
            document_id=document_id,
            extraction_run_id=extraction_run_id,
            declared_by=declared_by,
            predecessor_declaration_id=tail.id if tail is not None else None,
        )
    )
    if current is None:
        session.add(
            ActiveExtractionRun(
                document_id=document_id, extraction_run_id=extraction_run_id
            )
        )
    else:
        session.execute(
            update(ActiveExtractionRun)
            .where(ActiveExtractionRun.document_id == document_id)
            .values(extraction_run_id=extraction_run_id, declared_at=func.now())
        )
        session.expire(current)
    # A changed Current Production Run changes which exact runs a revision pair
    # compares, so it is a revision pair to reconsider. Bumping the durable
    # watermark in this same transaction reaches reconciliation on commit and
    # leaves nothing on rollback (#343).
    request_revision_reconciliation(session, project_id, "active_run_declared")
    if request_inclusion:
        # A human declaration changes which exact Candidate inputs ordinary
        # Record Inclusion can see. The completed run may have been reconciled
        # while it was still ambiguous, so the run-completion watermark is not
        # enough here. Use the shared durable handoff in this same transaction:
        # rollback leaves no load request; a crash after commit leaves work for
        # the recovery pass.
        request_record_inclusion(session, project_id, "active_run_declared")
    return run


class MultipleRunsNeedExplicitChoice(ValueError):
    """A document holds several completed runs; bulk declaration refuses
    to pick one — recency is exactly the inference Active Runs exist to
    forbid."""


def declare_single_run_documents(
    session: Session, project_id: int, *, principal: HumanPrincipal
) -> list[ExtractionRun]:
    """Declare the only completed run of every undeclared document.

    One operator command, one stated intent, one honest per-run receipt
    (docs/sh99-date-rehearsal.md): the SH 99 event stream spans over a
    hundred minutes documents with exactly one run each, where per-document
    declaration is ceremony and inference is forbidden. Only the
    unambiguous case is covered: a document with several completed runs
    raises before anything is declared — all-or-nothing, so a partial
    declaration can never masquerade as a reviewed one.
    """
    declarer = require_human_principal(principal)
    lock_project(session, project_id)

    rows = session.execute(
        select(ExtractionRun, Document)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, completion_predicate())
        .order_by(ExtractionRun.id)
    ).all()

    runs_by_document: dict[int, list[ExtractionRun]] = {}
    registry_ids: dict[int, str] = {}
    for run, document in rows:
        runs_by_document.setdefault(document.id, []).append(run)
        # registry_id is nullable for legacy and ad-hoc documents; the
        # refusal must still name them.
        registry_ids[document.id] = (
            document.registry_id or f"document {document.id}"
        )

    ambiguous = sorted(
        registry_ids[document_id]
        for document_id, runs in runs_by_document.items()
        if len(runs) > 1
    )
    if ambiguous:
        raise MultipleRunsNeedExplicitChoice(
            "these documents hold several completed runs and need an "
            f"explicit human choice: {', '.join(ambiguous)} — nothing was "
            "declared"
        )

    declared: list[ExtractionRun] = []
    for document_id, runs in sorted(runs_by_document.items()):
        [run] = runs
        if session.get(ActiveExtractionRun, document_id) is not None:
            continue
        declared.append(
            declare_active_run(
                session, document_id, run.id, principal=declarer
            )
        )
    return declared


@dataclass(frozen=True)
class MechanicalDeclarations:
    """What the landing stage declared, and what it left for a human."""

    declared: list[ExtractionRun]
    # Documents holding several completed runs, named as a reader knows
    # them. Left undeclared rather than guessed, and surfaced rather than
    # raised: one ambiguous document must not keep a whole project dark.
    ambiguous: list[str]


def declare_single_run_documents_by_policy(
    session: Session, project_id: int
) -> MechanicalDeclarations:
    """Name the only reading every undeclared document has.

    The human form of this refuses as a whole when any document is
    ambiguous, because it answers an operator's stated intent to declare
    a project. This one runs unattended whenever documents land
    (ADR-0029), so it declares what is unambiguous and reports what is
    not — the ambiguous document waits for a human to choose its run,
    and every other document's conflicts reach the list meanwhile.
    """
    lock_project(session, project_id)

    rows = session.execute(
        select(ExtractionRun, Document)
        .join(Document, Document.id == ExtractionRun.document_id)
        .where(Document.project_id == project_id, completion_predicate())
        .order_by(ExtractionRun.id)
    ).all()

    runs_by_document: dict[int, list[ExtractionRun]] = {}
    registry_ids: dict[int, str] = {}
    for run, document in rows:
        runs_by_document.setdefault(document.id, []).append(run)
        registry_ids[document.id] = (
            document.registry_id or document.filename or f"document {document.id}"
        )

    declared: list[ExtractionRun] = []
    ambiguous: list[str] = []
    for document_id, runs in sorted(runs_by_document.items()):
        if session.get(ActiveExtractionRun, document_id) is not None:
            continue
        if len(runs) > 1:
            ambiguous.append(registry_ids[document_id])
            continue
        declared.append(
            declare_active_run_by_policy(session, document_id, runs[0].id)
        )
    return MechanicalDeclarations(declared=declared, ambiguous=sorted(ambiguous))


def current_active_run_declaration(
    session: Session, document_id: int
) -> ActiveRunDeclaration | None:
    """The declaration no later declaration has superseded.

    A chain fact: the tail is the row nothing names as its predecessor,
    never the greatest id or the newest timestamp.
    """
    successor = aliased(ActiveRunDeclaration)
    return session.scalar(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document_id,
            ~select(successor.id)
            .where(successor.predecessor_declaration_id == ActiveRunDeclaration.id)
            .exists(),
        )
    )


def active_run_for_document(
    session: Session, document_id: int
) -> ExtractionRun | None:
    """Return only the explicitly declared Active Run for a document."""
    return session.scalar(
        select(ExtractionRun)
        .join(
            ActiveExtractionRun,
            ActiveExtractionRun.extraction_run_id == ExtractionRun.id,
        )
        .where(ActiveExtractionRun.document_id == document_id)
    )


def main(argv: list[str], *, session_factory=None) -> int:
    """Declare an Active Run from explicit document and run identifiers.

    The declarer is the deployment-resolved principal, exactly as the web
    ingress resolves it — never free text from the command line. The
    `--single-run-documents <project-slug>` form declares the only
    completed run of every undeclared document in one command
    (declare_single_run_documents); ambiguity refuses before declaring.
    """
    if len(argv) == 2 and argv[0] == "--single-run-documents":
        return _single_run_documents_main(
            argv[1], session_factory=session_factory
        )
    if len(argv) != 2:
        print(
            "usage: active-run <document-id> <extraction-run-id>\n"
            "       active-run --single-run-documents <project-slug>",
            file=sys.stderr,
        )
        return 2
    try:
        document_id, extraction_run_id = (int(value) for value in argv)
    except ValueError:
        print("document-id and extraction-run-id must be integers", file=sys.stderr)
        return 2
    if document_id <= 0 or extraction_run_id <= 0:
        print("document-id and extraction-run-id must be positive", file=sys.stderr)
        return 2

    from corridor.config import settings
    from corridor.principals import HumanPrincipal, InvalidHumanPrincipal

    try:
        principal = HumanPrincipal(settings.human_principal)
    except InvalidHumanPrincipal:
        print(
            "declaring an Active Run is an attributable act: set "
            "CORRIDOR_HUMAN_PRINCIPAL to a namespaced subject such as "
            "'local:alice'",
            file=sys.stderr,
        )
        return 2

    if session_factory is None:
        from corridor.db import WorkerSession as session_factory

    with session_factory() as session:
        try:
            run = declare_active_run(
                session, document_id, extraction_run_id, principal=principal
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        run_summary = (run.id, run.prompt_version, run.outcome)
        session.commit()
    run_id, prompt_version, outcome = run_summary
    print(
        f"document {document_id}: Active Run {run_id} "
        f"({prompt_version}, {outcome}) declared by {principal.subject}"
    )
    return 0


def _single_run_documents_main(slug: str, *, session_factory=None) -> int:
    from corridor.config import settings
    from corridor.principals import InvalidHumanPrincipal

    try:
        principal = HumanPrincipal(settings.human_principal)
    except InvalidHumanPrincipal:
        print(
            "declaring Active Runs is an attributable act: set "
            "CORRIDOR_HUMAN_PRINCIPAL to a namespaced subject such as "
            "'local:alice'",
            file=sys.stderr,
        )
        return 2

    if session_factory is None:
        from corridor.db import WorkerSession as session_factory

    from corridor.models import Project

    with session_factory() as session:
        project_id = session.scalar(
            select(Project.id).where(Project.slug == slug)
        )
        if project_id is None:
            print(f"no project with slug {slug!r}", file=sys.stderr)
            return 2
        try:
            declared = declare_single_run_documents(
                session, project_id, principal=principal
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        summaries = [(run.document_id, run.id) for run in declared]
        session.commit()

    for document_id, run_id in summaries:
        print(
            f"document {document_id}: Active Run {run_id} declared "
            f"by {principal.subject}"
        )
    print(f"{len(summaries)} declaration(s) for project {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
