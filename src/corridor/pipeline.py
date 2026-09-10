"""Raw file to pending candidates.

Extractors never write to the Ledger — they only create Candidates, and the
single path onward is a human keystroke in `corridor.adjudicate`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.extraction_runs import append_source_facts, record_extraction_run
from corridor.facts import carries_source_facts
from corridor.extractor_lineage import (
    ExtractorConfig,
    deployed_extractor_config,
    deployed_native_matrix_config,
    token_usage_delta,
    usage_snapshot,
    zero_token_usage,
)
from corridor.extraction_errors import ExtractionFailed, NativeObservationFailed
from corridor.extraction_errors import NoMatrixFound
from corridor.ingest import SPREADSHEET_SUFFIXES, ingest_document
from corridor.config import settings
from corridor.native_matrix_runtime import NativeMatrixRuntime, configured_native_matrix_runtime
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    DocumentRenditionDerivation,
    ExtractionRun,
    Project,
)
from corridor.row_accounting import RowAccountingFailure
from corridor.storage import stored_file
from corridor.supersession import SupersessionDeclaration, register_supersessions


Extractor = Callable[[Session, Document], list[Candidate]]


@dataclass(frozen=True)
class ExtractionRoute:
    """Which reader will run for one document, and how to name that reading."""

    effective_prompt_version: str
    schema_version: str
    extract: Extractor
    # Configured independently of Candidate count so a model-backed zero-row
    # run still identifies the model that read the document. Deterministic
    # routes, such as native spreadsheet extraction, leave this null.
    model: str | None = None
    # Computed before the reader runs. The receipt owns these exact source
    # bytes even if the checkout changes before anybody reads the run later.
    extractor_config: ExtractorConfig | None = None
    # The cumulative provider counter whose per-document delta is exact.
    # Native routes have no client and record an exact zero instead.
    usage_client: object | None = None
    # Only explicit synthetic/historical adapters may opt into null lineage.
    # Deployed route selection never sets this.
    allow_unsealed_legacy: bool = False
    # Native selection must also be valid before a completed run can resume.
    validate: Callable[[Session, Document], None] | None = None
    pipeline_configuration_sha256: str | None = None


def ingest_manifest(
    session: Session,
    *,
    project_id: int,
    lock_path: Path | str,
    images_dir: Path | str,
    include_proposed: bool = False,
) -> list[Document]:
    """Ingest every successfully fetched source in the manifest lockfile.

    Provenance flows straight through: the lockfile already knows where each
    file came from and when it was retrieved, and losing that at the ingest
    boundary would leave citations bottoming out at a path on disk.
    """
    lock = json.loads(Path(lock_path).read_text())
    documents = []
    declarations: list[SupersessionDeclaration] = []

    for key, record in sorted(lock.get("sources", {}).items()):
        curation_status = record.get("curation_status", "confirmed")
        if curation_status not in {"confirmed", "proposed"}:
            raise ValueError("lockfile curation_status is invalid")
        if curation_status == "proposed" and not include_proposed:
            continue
        if record.get("supersession") is not None:
            declarations.append(_lock_supersession(record["supersession"]))
        # A failed fetch has a null sha256 and nothing on disk. It stays
        # visible in the lockfile rather than being ingested as though it
        # had worked.
        if not record.get("sha256") or not record.get("local_path"):
            continue

        documents.append(
            ingest_document(
                session,
                project_id=project_id,
                path=record["local_path"],
                doc_type=record.get("doc_type", "other"),
                images_dir=images_dir,
                # The leaf path only: a nested member's full path drags the
                # inner zip's name into every citation. "Meeting Notes/Air
                # Liquide/2024.07.30 notes.pdf" is what a reader needs.
                filename=(record.get("member") or _basename(key)).split("::")[-1],
                source_url=record.get("archive_url") or key,
                retrieved_at=record.get("retrieved_at"),
                doc_date=parse_doc_date(record.get("doc_date")),
                registry_id=record.get("registry_id"),
                expected_sha256=record["sha256"],
                numbering_scheme=record.get("numbering_scheme"),
            )
        )

    _register_rendition_derivations(
        session,
        lock,
        project_id=project_id,
        include_proposed=include_proposed,
    )
    _register_complete_supersession_set(
        session,
        declarations,
        project_id=project_id,
    )
    return documents


def _register_rendition_derivations(
    session: Session,
    lock: dict,
    *,
    project_id: int,
    include_proposed: bool,
) -> None:
    """Bind converted Documents to retained originals, idempotently."""

    for record in lock.get("sources", {}).values():
        if record.get("curation_status", "confirmed") == "proposed" and not (
            include_proposed
        ):
            continue
        raw = record.get("derivation")
        if raw is None or not record.get("sha256"):
            continue
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "source_registry_id",
            "source_sha256",
            "tool",
            "tool_version",
        }:
            raise ValueError("lockfile rendition derivation is malformed")
        derived_registry_id = record.get("registry_id")
        source_registry_id = raw.get("source_registry_id")
        source = session.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.registry_id == source_registry_id,
            )
        )
        derived = session.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.registry_id == derived_registry_id,
            )
        )
        if (
            source is None
            or derived is None
            or raw.get("kind") != "format_conversion"
            or source.sha256 != raw.get("source_sha256")
            or derived.sha256 != record.get("sha256")
        ):
            raise ValueError("lockfile rendition derivation identity is inconsistent")
        existing = session.scalar(
            select(DocumentRenditionDerivation).where(
                DocumentRenditionDerivation.derived_document_id == derived.id
            )
        )
        expected = {
            "project_id": project_id,
            "source_document_id": source.id,
            "derived_document_id": derived.id,
            "kind": "format_conversion",
            "source_format": "xls",
            "derived_format": "xlsx",
            "source_sha256": source.sha256,
            "derived_sha256": derived.sha256,
            "tool": raw.get("tool"),
            "tool_version": raw.get("tool_version"),
        }
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in expected.items()):
                raise ValueError("registered rendition derivation does not match lockfile")
            continue
        session.add(DocumentRenditionDerivation(**expected))
        session.flush()


def _lock_supersession(raw) -> SupersessionDeclaration:
    if not isinstance(raw, dict):
        raise ValueError("lockfile supersession must be an object")
    replacement_date = parse_doc_date(raw.get("replacement_date"))
    if replacement_date is None:
        raise ValueError("lockfile supersession replacement_date is required")
    identifiers = {
        "predecessor_registry_id": raw.get("predecessor_registry_id"),
        "successor_registry_id": raw.get("successor_registry_id"),
        "source_registry_id": raw.get("source_registry_id"),
    }
    for name, value in identifiers.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"lockfile supersession {name} must be non-empty")
    source_page = raw.get("source_page")
    if (
        isinstance(source_page, bool)
        or not isinstance(source_page, int)
        or source_page <= 0
    ):
        raise ValueError("lockfile supersession source_page must be positive")
    return SupersessionDeclaration(
        predecessor_registry_id=identifiers["predecessor_registry_id"],
        successor_registry_id=identifiers["successor_registry_id"],
        replacement_date=replacement_date,
        source_registry_id=identifiers["source_registry_id"],
        source_page=source_page,
    )


def _register_complete_supersession_set(
    session: Session,
    declarations: list[SupersessionDeclaration],
    *,
    project_id: int,
) -> None:
    """Register the declaration batch only when every participant exists.

    A failed fetch remains explicit in the lockfile, but it must not prevent
    other successfully fetched sources from being ingested. The declarations
    stay all-or-nothing: if even one registered document is unavailable, no
    edge from the batch is written and a later ingest can retry the full set.
    """
    if not declarations:
        return

    required_registry_ids = {
        registry_id
        for declaration in declarations
        for registry_id in (
            declaration.predecessor_registry_id,
            declaration.successor_registry_id,
            declaration.source_registry_id,
        )
    }
    available_registry_ids = set(
        session.scalars(
            select(Document.registry_id).where(
                Document.project_id == project_id,
                Document.registry_id.in_(required_registry_ids),
            )
        ).all()
    )
    if available_registry_ids != required_registry_ids:
        return

    required_source_pointers = {
        (declaration.source_registry_id, declaration.source_page)
        for declaration in declarations
    }
    available_source_pointers = {
        (registry_id, page_no)
        for registry_id, page_no in session.execute(
            select(Document.registry_id, DocPage.page_no)
            .join(DocPage, DocPage.document_id == Document.id)
            .where(
                Document.project_id == project_id,
                Document.registry_id.in_(
                    {registry_id for registry_id, _ in required_source_pointers}
                ),
            )
        ).all()
        if (registry_id, page_no) in required_source_pointers
    }
    if available_source_pointers != required_source_pointers:
        return

    register_supersessions(session, declarations, project_id=project_id)


@contextmanager
def extraction_attempt(session: Session):
    """Roll back partial reads, while retaining a native terminal refusal.

    Only this narrow failure is already complete: the native runner rolled
    back partial source writes and persisted its observation/artifacts. Raise
    it after releasing the savepoint so the caller can record the failed
    attempt. Unexpected errors and successful-result validation still roll back.
    """
    failure = None
    with session.begin_nested():
        try:
            yield
        except NativeObservationFailed as exc:
            failure = exc
    if failure is not None:
        raise failure


class CapturedCandidates(list[Candidate]):
    """Candidates with the native pipeline's already sealed source run."""

    def __init__(self, candidates, run: ExtractionRun):
        super().__init__(candidates)
        self.run = run


def extraction_route(
    document: Document, *, client=None, native_runtime: NativeMatrixRuntime | None = None,
) -> ExtractionRoute:
    """Choose the reader for one document and expose its effective version.

    Resume and evaluation key off the document attempt rather than the
    candidate rows it happened to produce, so the version recorded for that
    attempt has to be the version of the reader that actually ran.
    """
    if getattr(document, "doc_type", None) == "minutes":
        from corridor.minutes_spine import capture_minutes, minutes_extractor_config
        from corridor.llm import OpenAIClient

        minutes_client = client or OpenAIClient()
        configuration = minutes_extractor_config(minutes_client)

        def extract_minutes_source(session, doc):
            receipt = capture_minutes(session, doc, client=minutes_client)
            return CapturedCandidates([], session.get_one(ExtractionRun, receipt.extraction_run_id))

        return ExtractionRoute(effective_prompt_version=configuration.prompt_version,
            schema_version=configuration.schema_version, extract=extract_minutes_source,
            model=configuration.model, extractor_config=configuration, usage_client=minutes_client)
    if getattr(document, "doc_type", None) == "email":
        if getattr(document, "source_delivery_id", None) is not None:
            from corridor.email_spine import (
                PROMPT_VERSION as EMAIL_PROMPT_VERSION,
                SCHEMA_VERSION as EMAIL_SCHEMA_VERSION,
                capture_email_thread, email_extractor_config, envelope_for_delivery,
            )
            from corridor.llm import OpenAIClient

            email_client = client or OpenAIClient()

            def extract_bound_email(session, doc):
                reading = capture_email_thread(session,
                    envelope_for_delivery(session, doc.source_delivery_id), client=email_client)
                # The thread's closing turn owns the statement; each input
                # Document has its own captured run and attachment provenance.
                from corridor.models import InboundMessage

                message = session.scalar(select(InboundMessage).where(
                    InboundMessage.document_id == doc.id,
                    InboundMessage.project_id == doc.project_id))
                context = next(item for item in reading.turn_context_json if item["message_id"] == message.id)
                return CapturedCandidates([], session.get_one(ExtractionRun, context["extraction_run_id"]))

            return ExtractionRoute(
                effective_prompt_version=EMAIL_PROMPT_VERSION, schema_version=EMAIL_SCHEMA_VERSION,
                extract=extract_bound_email, model=getattr(email_client, "model", None),
                extractor_config=email_extractor_config(email_client), usage_client=email_client,
            )
        # A routed inbound message body reads through the ordinary prose
        # statement extractor (ADR-0058): proposals with quotes verified
        # against the stored message, never a second reading pipeline.
        from corridor.extract_minutes_v5 import (
            PROMPT_VERSION as EMAIL_PROMPT_VERSION,
            extract_document as extract_message_body,
        )
        from corridor.llm import OpenAIClient

        body_client = client or OpenAIClient()

        def extract_email(session: Session, doc: Document) -> list[Candidate]:
            return extract_message_body(session, doc, client=body_client)

        return ExtractionRoute(
            effective_prompt_version=EMAIL_PROMPT_VERSION,
            schema_version=EMAIL_PROMPT_VERSION,
            extract=extract_email,
            model=getattr(body_client, "model", None),
            extractor_config=deployed_extractor_config("minutes", client=body_client),
            usage_client=body_client,
        )

    path = getattr(document, "_stored_path", None) or stored_file(document)
    if path is not None and Path(path).suffix.lower() in SPREADSHEET_SUFFIXES:
        from corridor.extract_sheet import (
            PROMPT_VERSION as SHEET_PROMPT_VERSION,
            SCHEMA_VERSION as SHEET_SCHEMA_VERSION,
            extract_document as extract_sheet,
        )

        return ExtractionRoute(
            effective_prompt_version=SHEET_PROMPT_VERSION,
            schema_version=SHEET_SCHEMA_VERSION,
            extract=extract_sheet,
            extractor_config=deployed_extractor_config("sheet", client=None),
        )

    from corridor.native_pipeline import (
        RecordedPipelineClient, native_pipeline_configuration, run_selected_native_matrix,
    )
    from corridor.native_provider_boundary import AuthorizedNativeMapper, POSTURE
    from corridor.pipeline_contracts import ObservationPlan, content_digest
    from corridor.pipeline_qualification import PipelineQualificationRefused, selected_pipeline_configuration

    runtime = native_runtime
    runtime_error = None
    if runtime is None:
        try:
            runtime = configured_native_matrix_runtime(settings)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            runtime_error = str(exc)
    # A refusal still names the reader and schema that would have run. This
    # placeholder only seals request controls; it is never used to extract.
    matrix_client = runtime.client if runtime else RecordedPipelineClient([])
    plan = runtime.plan if runtime else ObservationPlan(
        mode="fresh_provider", origin_sha256="0" * 64,
        source_permission="public", provider_posture_sha256=POSTURE.digest,
        description="Native matrix route awaiting its configured provider boundary",
    )
    deployment = runtime.deployment if runtime else settings.environment
    extractor_config = deployed_native_matrix_config(client=matrix_client)
    configuration_sha256 = content_digest(native_pipeline_configuration(matrix_client, plan))
    requested_config_error = None
    if client is not None:
        try:
            if deployed_native_matrix_config(client=client) != extractor_config:
                requested_config_error = "requested Matrix client differs from the selected native request configuration"
        except ValueError as exc:
            requested_config_error = str(exc)

    def validate(session: Session, doc: Document) -> None:
        try:
            selected_pipeline_configuration(
                session, doc, deployment=deployment, configuration_sha256=configuration_sha256,
            )
        except PipelineQualificationRefused as exc:
            raise ExtractionFailed(f"native matrix selection refused for deployment {deployment!r}: {exc}") from exc

    def extract(session: Session, doc: Document) -> list[Candidate]:
        try:
            # Also runs for a missing provider record: name a missing or stale
            # selection before any rendering, source reading or outbound call.
            validate(session, doc)
            if requested_config_error:
                raise ExtractionFailed(requested_config_error)
            if runtime is None:
                raise ExtractionFailed(runtime_error or "native matrix runtime is not configured")
            if isinstance(matrix_client, AuthorizedNativeMapper):
                project = session.get_one(Project, doc.project_id)
                if matrix_client.request.project != project.slug:
                    raise ExtractionFailed("native matrix provider boundary does not cover this Document's project")
            source_path = getattr(doc, "_stored_path", None) or stored_file(doc)
            if source_path is None:
                raise ExtractionFailed("native matrix source file is unavailable")
            attempt = uuid4().hex
            # One key belongs to one extraction attempt; --redo creates a new
            # immutable run rather than replacing previous runs or candidates.
            result = run_selected_native_matrix(
                session, doc, deployment=deployment, client=matrix_client, plan=plan,
                source_path=source_path, output_dir=runtime.output_dir / attempt,
                document_label=doc.filename, idempotency_key=f"matrix-route:{doc.id}:{attempt}",
            )
            if result.extraction is None:
                outcome = json.loads(result.observation.receipt_text)["outcome"]
                raise NativeObservationFailed(
                    f"native matrix {outcome['disposition']} at {outcome.get('stage')}: "
                    f"{outcome.get('reason') or outcome.get('type') or 'no source capture'}",
                    observation_id=result.observation.id,
                )
            doc.extraction_tiers = {"native_matrix_cells": len(result.extraction.mapping.pages)}
            doc.header_disagreements = 0
            return CapturedCandidates(result.extraction.candidates, result.extraction.run)
        except PipelineQualificationRefused as exc:
            raise ExtractionFailed(f"native matrix selection refused for deployment {deployment!r}: {exc}") from exc
        except (ValueError, OSError) as exc:
            raise ExtractionFailed(f"native matrix processing refused: {exc}") from exc
        finally:
            if native_runtime is None and runtime is not None:
                runtime.close()

    return ExtractionRoute(
        effective_prompt_version=extractor_config.prompt_version,
        schema_version=extractor_config.schema_version,
        extract=extract, model=extractor_config.model,
        extractor_config=extractor_config, usage_client=matrix_client,
        validate=validate, pipeline_configuration_sha256=configuration_sha256,
    )


@contextmanager
def production_extraction_routes():
    """Own the prose client lazily; Matrix authorization belongs to its route."""
    from corridor.llm import OpenAIClient

    with ExitStack() as resources:
        prose_client = None
        matrix_runtime = None

        def select_route(document: Document) -> ExtractionRoute:
            nonlocal prose_client, matrix_runtime
            if document.doc_type in {"email", "minutes"} and prose_client is None:
                prose_client = OpenAIClient()
                resources.callback(prose_client.close)
            if (document.doc_type == "matrix" and matrix_runtime is None
                    and Path(document.filename).suffix.lower() not in SPREADSHEET_SUFFIXES):
                try:
                    matrix_runtime = configured_native_matrix_runtime(settings)
                except (ValueError, TypeError, KeyError, OSError, RuntimeError):
                    # The route turns absent/malformed configuration into the
                    # document's failed attempt rather than aborting the pass.
                    pass
                else:
                    resources.callback(matrix_runtime.close)
            return extraction_route(document,
                client=prose_client if document.doc_type in {"email", "minutes"} else None,
                native_runtime=matrix_runtime)

        yield select_route


def extract_any(
    session: Session, document: Document, *, client=None
) -> list[Candidate]:
    """Read one matrix, whichever form it was published in (ADR-0005).

    Which reader runs is a property of the document rather than something a
    caller has to know: `make extract` reads a project, and a project may
    publish its matrix as a spreadsheet, as a printout of one, or as both.

    The two readers have deliberately different signatures and that is not
    an inconsistency to smooth over. The page path needs a model and takes
    a client; the native path needs neither, and giving it a parameter it
    would ignore would suggest a model is involved somewhere in reading a
    spreadsheet. It is not.
    """
    route = extraction_route(document, client=client)
    usage_before = usage_snapshot(route.usage_client)
    try:
        with extraction_attempt(session):
            candidates = route.extract(session, document)
            record_routed_run(
                session,
                document,
                route,
                usage_before,
                candidate_count=len(candidates),
                page_errors=0,
                outcome="completed",
                candidates=candidates,
                model=_run_model(candidates, route.model),
                row_accounting_json=getattr(candidates, "row_accounting", None),
            )
    except RowAccountingFailure as exc:
        record_routed_run(
            session,
            document,
            route,
            usage_before,
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model=route.model,
            error_detail=str(exc),
            row_accounting_json=exc.receipt,
        )
        raise
    except NoMatrixFound as exc:
        record_routed_run(
            session,
            document,
            route,
            usage_before,
            candidate_count=0,
            page_errors=1,
            outcome="no_matrix",
            model=route.model,
            error_detail=str(exc),
        )
        raise
    except ExtractionFailed as exc:
        record_routed_run(
            session,
            document,
            route,
            usage_before,
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model=route.model,
            error_detail=str(exc),
        )
        raise
    except Exception as exc:
        record_routed_run(
            session,
            document,
            route,
            usage_before,
            candidate_count=0,
            page_errors=1,
            outcome="failed",
            model=route.model,
            error_detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    return candidates


def _run_model(candidates: list[Candidate], configured_model: str | None) -> str | None:
    """Resolve one configured/observed model without losing zero-row lineage."""

    observed = {candidate.model for candidate in candidates if candidate.model}
    if len(observed) > 1:
        raise ValueError("one extraction run cannot contain multiple models")
    if configured_model is not None and observed and observed != {configured_model}:
        raise ValueError("Candidate model does not match the configured extraction model")
    return configured_model or next(iter(observed), None)


def record_routed_run(
    session: Session,
    document: Document,
    route: ExtractionRoute,
    usage_before: dict[str, int] | None,
    **values,
) -> ExtractionRun:
    captured = values.get("candidates")
    if isinstance(captured, CapturedCandidates):
        run = captured.run
        if (run.document_id != document.id or run.outcome != "completed"
                or run.prompt_version != route.effective_prompt_version
                or run.schema_version != route.schema_version
                or run.candidate_count != len(captured)
                or run.extractor_config_sha256 != route.extractor_config.config_sha256):
            raise ValueError("native source run does not match its extraction route")
        return run
    if route.extractor_config is None and not route.allow_unsealed_legacy:
        raise ValueError("a deployed extraction route must carry its configuration")
    token_usage = None if route.extractor_config is None else (
        zero_token_usage(document.id)
        if route.usage_client is None and route.extractor_config.model is None
        else token_usage_delta(
            usage_before,
            usage_snapshot(route.usage_client),
            document_ids=[document.id],
        )
    )
    command = (
        append_source_facts
        if values.get("outcome", "completed") == "completed"
        and carries_source_facts(tuple(values.get("candidates", ())))
        else record_extraction_run
    )
    result = command(
        session,
        document,
        **(
            {
                "idempotency_key": None,
                "source_path": getattr(document, "_stored_path", None)
                or stored_file(document),
            }
            if command is append_source_facts
            else {}
        ),
        prompt_version=route.effective_prompt_version,
        schema_version=route.schema_version,
        extractor_config=route.extractor_config,
        token_usage=token_usage,
        allow_unsealed_legacy=route.allow_unsealed_legacy,
        **values,
    )
    return result.run if command is append_source_facts else result


def _basename(url: str) -> str:
    return Path(urlparse(url.split("::", 1)[0]).path).name or "document"


def parse_doc_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def ingest_and_extract(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    images_dir: Path | str,
    client=None,
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | None = None,
    doc_date: date | None = None,
    doc_type: str = "matrix",
) -> tuple[Document, list[Candidate]]:
    """Ingest one file and extract it, in that order.

    Extraction needs a model since #63 removed the deterministic parser, so
    a caller either injects a client or one is constructed — which means
    this path needs an API key where it used to need none. That is the
    price of having a single extraction path rather than a second one kept
    alive to avoid it.
    """
    document = ingest_document(
        session,
        project_id=project_id,
        path=path,
        doc_type=doc_type,
        images_dir=images_dir,
        filename=filename,
        source_url=source_url,
        retrieved_at=retrieved_at,
        doc_date=doc_date,
    )
    document._stored_path = Path(path)
    if document.parse_status != "parsed":
        route = extraction_route(document, client=client)
        record_routed_run(
            session,
            document,
            route,
            usage_snapshot(route.usage_client),
            candidate_count=0,
            page_errors=1,
            outcome="unreadable",
            model=route.model,
            error_detail=f"ingest parse_status is {document.parse_status!r}",
        )
        return document, []

    # The same routing `make extract` uses, so a workbook here reads as a
    # workbook rather than failing as a PDF with no page image.
    return document, extract_any(session, document, client=client)
