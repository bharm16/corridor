"""Customer-approved non-authoritative compatibility intake (#561).

A prospective customer hands over an approved workbook and asks one bounded
question: *can Corridor read our file at all, and what would it make of our
columns?* Answering it must not require — and must be structurally unable to
perform — anything that touches the accepted record. This lane is that answer.

Why a separate lane rather than a flag on Adopt Baseline. Adopt Baseline is a
bulk human act that establishes accepted authority from a workbook (#509,
``baseline_adoption``); it previews, then writes one Project Record revision
through the record-decision role's ``SECURITY DEFINER`` command. A prospect who
has signed nothing more than a compatibility authorization must never reach that
writer, and "trust the caller to pass adopt=False" is exactly the kind of
one-flag-away-from-authority design #492 replaced with a boundary. So the lane
holds *no* record-decision or release capability at all: it takes no session and
no engine, imports neither ``corridor.db`` nor any append/decision/release
module, and therefore cannot create a Source Fact, Source Segment, Proposed
Delta, Support Assessment, Project Record revision, or release artifact even by
mistake. What it can do, it composes entirely from primitives that already exist
and already write nothing authoritative:

- the #522 authorization seam (``CustomerAuthorization``), matched here before
  anything else happens, so an unsigned or uncovered request is refused with no
  side effect but its own refusal receipt;
- the #490 intake-hardening gates plus #487 content-addressed staging
  (``source_intake.validate_and_stage``), which harden the bytes and stage them
  by digest without any database row;
- the #509 operations reading (``baseline_workbook.read_baseline_workbook``),
  whose own docstring records that "nothing here touches the database, calls a
  model, or writes anything" — it *is* the capability-and-mapping report this
  ticket asks for, so the lane reuses it rather than inventing a second one;
- #487 storage again, to persist one JSON receipt per run.

The lane calls no model. There is no import of ``corridor.llm`` or the native
provider boundary's transport, and the one configuration knob that could ever
name model work — ``model_enrichment`` — exists only to be refused, so a caller
that asks for enrichment gets a refusal instead of a code path.

**Out of scope, deliberately, and human-gated.** This module is the *tooling*.
Running a real partner's bytes through it and recording the resulting report on
the tracking issue waits on #522's signed authorization instance (still open)
and is a human decision, not something this lane initiates. Every fixture that
exercises it is synthetic (ADR-0046). The persisted #522 record does not exist
yet either, so the lane accepts an injected ``CustomerAuthorization`` and does
not presume where #522 will store it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path

from corridor.baseline_workbook import (
    BaselineWorkbookUnsupported,
    OperationsReading,
    read_baseline_workbook,
)
from corridor.native_provider_boundary import CUSTOMER_STAGES, CustomerAuthorization
from corridor.object_storage import content_key, digest_bytes, store_bytes

# ``validate_and_stage`` runs the #490 gates and stages bytes to #487 storage.
# It takes no session and writes no database row (its only durable effect is the
# content-addressed object), which is what keeps this whole lane database-free.
from corridor.source_intake import validate_and_stage

# The one stage this lane serves. It is already a member of the customer-stage
# vocabulary the #522 boundary recognises, so the lane never invents a stage.
COMPATIBILITY_STAGE = "compatibility"
assert COMPATIBILITY_STAGE in CUSTOMER_STAGES

# Recorded on every receipt, so a run of a later version of this lane is a
# different reading of the same bytes and the two are never pooled — the same
# argument ``baseline_workbook.IMPORTER_VERSION`` makes for its own reading.
LANE_IDENTITY = "corridor.compatibility-intake"
LANE_VERSION = "compatibility_intake_v1"
RECEIPT_KIND = "compatibility-intake-receipt"
RECEIPT_SUFFIX = ".json"


class CompatibilityIntakeRefused(RuntimeError):
    """The request was refused before any workbook was staged or read.

    Raised for a missing or uncovered authorization and for a configuration
    that asks for model enrichment. The refusal still writes one receipt — the
    only side effect a refusal is allowed — and ``receipt`` points at it, so an
    auditor can see *that* a refusal happened and why without the bytes ever
    being staged. ``mismatches`` names every failing check at once rather than
    the first, so one refusal shows the whole gap.
    """

    def __init__(
        self,
        reason: str,
        *,
        mismatches: tuple[str, ...] = (),
        receipt: "CompatibilityReceipt | None" = None,
    ) -> None:
        super().__init__(f"{reason}: {'; '.join(mismatches) or reason}")
        self.reason = reason
        self.mismatches = tuple(mismatches)
        self.receipt = receipt


@dataclass(frozen=True)
class CompatibilityReceipt:
    """Where one run's persisted #487 receipt lives, and what it concluded.

    ``receipt_sha256`` is the digest of the receipt JSON itself (its storage
    identity); ``source_content_sha256`` is the digest of the workbook bytes the
    run was about. ``stored_path`` is the locally staged copy of the receipt.
    """

    kind: str
    outcome: str
    source_content_sha256: str
    receipt_sha256: str
    key: str
    stored_path: Path


@dataclass(frozen=True)
class CompatibilityRun:
    """The outcome of one lane run that got past the authorization gate.

    ``outcome`` is ``"reported"`` when the workbook parsed — ``reading`` then
    carries the #509 operations reading, whose ``resolved`` flag says whether the
    file could become a baseline — or ``"unreadable"`` when the bytes are not a
    workbook this importer can read at all, in which case ``reading`` is ``None``
    and ``parse_error`` states why. A refusal never returns a run; it raises.
    """

    outcome: str
    reading: OperationsReading | None
    receipt: CompatibilityReceipt
    parse_error: str | None = None


def run_compatibility_intake(
    body: bytes,
    filename: str,
    *,
    authorization: CustomerAuthorization | None,
    customer: str,
    project: str,
    operator: str,
    environment: str,
    deletion_date: date,
    external_references: Mapping[str, str] | None = None,
    model_enrichment: bool = False,
) -> CompatibilityRun:
    """Read one approved workbook for compatibility, writing nothing authoritative.

    ``authorization`` is #522's signed instance reduced to matchable fields; the
    run declares which ``customer`` and ``project`` it is for and the gate
    refuses unless the authorization is present, authorizes the
    ``"compatibility"`` stage, and covers that customer, that project, and these
    exact bytes. ``operator`` and ``environment`` identify who ran it and where,
    and ``deletion_date`` is the date these bytes and this receipt are to be
    deleted — recording it in the receipt is what satisfies "#487 storage with a
    digest and a recorded deletion date" without any database row.

    ``external_references`` is the partner's own data-dictionary mapping of
    printed heading to reference role (#597); it is passed straight through to
    the operations reading and defaults to none. ``model_enrichment`` exists only
    to be refused: this lane calls no model, so asking for enrichment is a
    configuration error, not a feature.

    Returns a :class:`CompatibilityRun`. Raises :class:`CompatibilityIntakeRefused`
    for an uncovered authorization or a model-enrichment request — each writes a
    refusal receipt and stages nothing.
    """

    if not isinstance(deletion_date, date):
        raise TypeError(
            "deletion_date must be a date; the receipt records when these bytes "
            "and this receipt are deleted"
        )

    source_content_sha256 = digest_bytes(body)

    problems: list[str] = []
    if model_enrichment:
        problems.append(
            "model-enrichment: this lane calls no model and has no path to one; "
            "a request for model enrichment is refused rather than served"
        )
    problems.extend(
        _authorization_mismatches(authorization, customer, project, source_content_sha256)
    )
    if problems:
        reason = (
            "model-enrichment-unsupported"
            if model_enrichment
            else "authorization-absent"
            if authorization is None
            else "authorization-refused"
        )
        receipt = _store_receipt(
            _base_payload(
                outcome="refused",
                source_content_sha256=source_content_sha256,
                size_bytes=len(body),
                filename=filename,
                customer=customer,
                project=project,
                operator=operator,
                environment=environment,
                deletion_date=deletion_date,
                authorization=authorization,
            )
            | {"refusal": {"reason": reason, "mismatches": problems}}
        )
        raise CompatibilityIntakeRefused(
            reason, mismatches=tuple(problems), receipt=receipt
        )

    # Only now, past the gate, do the bytes touch anything. #490 hardening and
    # #487 staging; still no database and no model.
    staged = validate_and_stage(
        body,
        filename,
        customer_id=customer,
        project_id=project,
        channel="compatibility",
    )

    payload = _base_payload(
        outcome="reported",
        source_content_sha256=staged.sha256,
        size_bytes=staged.size_bytes,
        filename=staged.filename,
        customer=customer,
        project=project,
        operator=operator,
        environment=environment,
        deletion_date=deletion_date,
        authorization=authorization,
    )

    try:
        reading = read_baseline_workbook(
            staged.stored_path, external_references=external_references
        )
    except BaselineWorkbookUnsupported as exc:
        receipt = _store_receipt(
            payload | {"outcome": "unreadable", "parse_error": str(exc)}
        )
        return CompatibilityRun(
            outcome="unreadable",
            reading=None,
            receipt=receipt,
            parse_error=str(exc),
        )

    receipt = _store_receipt(
        payload
        | {"capability": _capability_summary(reading), "report": asdict(reading)}
    )
    return CompatibilityRun(outcome="reported", reading=reading, receipt=receipt)


def _authorization_mismatches(
    authorization: CustomerAuthorization | None,
    customer: str,
    project: str,
    source_content_sha256: str,
) -> list[str]:
    """Every way the authorization fails to cover this request; empty is a pass."""

    if authorization is None:
        return ["authorization-absent: no customer authorization was given"]
    if not isinstance(authorization, CustomerAuthorization):
        return [
            f"authorization-kind: {type(authorization).__name__} is not a "
            "customer authorization"
        ]
    found: list[str] = []
    if COMPATIBILITY_STAGE not in authorization.stages:
        found.append(
            f"stage: {COMPATIBILITY_STAGE!r} is not authorized by record "
            f"{authorization.record_id!r} ({', '.join(sorted(authorization.stages))})"
        )
    if authorization.customer != customer:
        found.append(
            f"customer: {customer!r} is not the customer of record "
            f"{authorization.record_id!r} ({authorization.customer!r})"
        )
    if project not in authorization.projects:
        found.append(
            f"project: {project!r} is not a project of record "
            f"{authorization.record_id!r} ({', '.join(sorted(authorization.projects))})"
        )
    if source_content_sha256 not in authorization.source_sha256s:
        found.append(
            f"source-digest: these bytes ({source_content_sha256[:12]!r}) are "
            f"not covered by record {authorization.record_id!r}"
        )
    return found


def _base_payload(
    *,
    outcome: str,
    source_content_sha256: str,
    size_bytes: int,
    filename: str,
    customer: str,
    project: str,
    operator: str,
    environment: str,
    deletion_date: date,
    authorization: CustomerAuthorization | None,
) -> dict:
    """The fields every receipt binds, whatever the outcome."""

    return {
        "kind": RECEIPT_KIND,
        "lane_identity": LANE_IDENTITY,
        "lane_version": LANE_VERSION,
        "outcome": outcome,
        "recorded_at": _now(),
        "deletion_date": deletion_date.isoformat(),
        "environment": environment,
        "operator": operator,
        "run": {"customer": customer, "project": project, "filename": filename},
        "source": {
            "content_sha256": source_content_sha256,
            "size_bytes": size_bytes,
        },
        "authorization": _authorization_identity(authorization),
    }


def _authorization_identity(authorization: CustomerAuthorization | None) -> dict | None:
    """The authorization's identity, not its whole payload.

    The covered-digest set can be large and is not the receipt's business; what
    the receipt binds is *which* signed authorization this run was made under.
    """

    if authorization is None:
        return None
    return {
        "kind": authorization.kind,
        "record_id": authorization.record_id,
        "customer": authorization.customer,
        "stages": sorted(authorization.stages),
        "signed_by": authorization.signed_by,
        "signed_on": authorization.signed_on,
    }


def _capability_summary(reading: OperationsReading) -> dict:
    """The headline of the operations reading, beside the full report."""

    return {
        "resolved": reading.resolved,
        "adopted_sheet": reading.adopted_sheet,
        "row_count": len(reading.rows),
        "mapped_column_count": len(reading.column_mapping),
        "unknown_column_count": len(reading.unknown_columns),
        "blocking_diagnostic_count": len(reading.blocking_diagnostics),
        "round_trip_clean": reading.round_trip.clean,
    }


def _store_receipt(payload: dict) -> CompatibilityReceipt:
    """Persist one receipt as a content-addressed JSON object (#487)."""

    data = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    receipt_sha256 = digest_bytes(data)
    stored_path = store_bytes(data, sha256=receipt_sha256, suffix=RECEIPT_SUFFIX)
    return CompatibilityReceipt(
        kind=RECEIPT_KIND,
        outcome=payload["outcome"],
        source_content_sha256=payload["source"]["content_sha256"],
        receipt_sha256=receipt_sha256,
        key=content_key(receipt_sha256, RECEIPT_SUFFIX),
        stored_path=stored_path,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
