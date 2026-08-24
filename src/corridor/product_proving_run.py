"""Bounded raw-Document Product Proving Run contract.

This is the highest product-testing seam.  It does not reimplement extraction,
Admission, Adjudication, the Work List, Report, or Approved Export rules.  It
pins their inputs, records the operations and real-frontend phases separately,
and decides whether two complete passes support the narrow ADR-0046 claim.

The receipt is deliberately external to PostgreSQL.  A failed pass stays a
failed pass after a repair, and restoring the sealed development baseline
cannot erase either the failure or the exact reviewed PDF bytes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from corridor.m8_acceptance_bundle import (
    VerificationResult,
    publish_verified_bundle,
    verify_bundle,
)


BUNDLE_SCHEMA_VERSION = "corridor.product-proving-run-bundle.v1"
BUNDLE_FAILURE_SCHEMA_VERSION = "corridor.product-proving-run-failure-bundle.v1"
BUNDLE_FILES = (
    "canonical-content.json",
    "environment.json",
    "receipt.json",
    "receipt.md",
    "pass-1-approved-export.pdf",
    "pass-2-approved-export.pdf",
)
FAILURE_BUNDLE_FILES = (
    "canonical-content.json",
    "environment.json",
    "receipt.json",
    "receipt.md",
)
_OUTCOMES = frozenset({"supported", "not_relevant", "unresolved"})
_PROVENANCE_CLASSES = frozenset(
    {"Assertion", "Derivation", "Work Decision", "Verbal"}
)


class CorruptProductProvingBundle(ValueError):
    """A published Product Proving receipt does not match its sealed facts."""


@dataclass(frozen=True)
class ExpectedPreflight:
    """Caller-held pins that must match before the first proving write."""

    source_revision: str
    origin_main_revision: str
    migration_head: str
    policy_digests: Mapping[str, str]
    documents: Mapping[int, str]
    baseline_runs: Mapping[int, int]
    milestone_sources: Mapping[str, str]
    baseline_fingerprint: str


@dataclass(frozen=True)
class ObservedPreflight:
    """Read-only observation of the checkout and sealed Project Record."""

    source_revision: str
    origin_main_revision: str
    clean_worktree: bool
    migration_head: str
    policy_digests: Mapping[str, str]
    documents: Mapping[int, str]
    baseline_runs: Mapping[int, int]
    milestone_sources: Mapping[str, str]
    baseline_fingerprint: str


@dataclass(frozen=True)
class CandidateSetComparison:
    """Semantic comparison of two exact Extraction Runs for one Document."""

    document_id: int
    baseline_run_id: int
    fresh_run_id: int
    added: tuple[dict[str, Any], ...]
    missing: tuple[dict[str, Any], ...]
    matched_sha256: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return not self.added and not self.missing


@dataclass(frozen=True)
class ProductProvingPass:
    """One terminal pass, including honest failure evidence when present."""

    pass_number: int
    restored_baseline_fingerprint: str
    extraction_comparisons: tuple[CandidateSetComparison, ...]
    extraction_failures: tuple[str, ...]
    admission_completed: bool
    residual_candidate_ids: tuple[int, ...]
    residual_outcomes: Mapping[int, Literal["supported", "not_relevant", "unresolved"]]
    frontend_kind: str
    frontend_actions: tuple[str, ...]
    invalid_action_refused: bool
    invalid_action_write_set: Mapping[str, Any]
    correction_preserved_predecessor: bool
    work_decision_change_preserved_predecessor: bool
    report_pdf_sha256: str
    approved_export_sha256: str
    approved_export_bytes: bytes
    report_provenance_classes: tuple[str, ...]
    write_set: Mapping[str, Sequence[Any]]
    operations_elapsed_seconds: float
    practitioner_elapsed_seconds: float
    non_blocking_friction: tuple[str, ...]
    workarounds: tuple[str, ...]


@dataclass(frozen=True)
class ProductProvingCapture:
    """The complete two-pass observation and narrow claim boundary."""

    expected: ExpectedPreflight
    observed: ObservedPreflight
    pass_one: ProductProvingPass
    pass_two: ProductProvingPass
    final_baseline_fingerprint: str
    simulated_practitioner: bool
    same_project_manual_report_compared: bool
    revision_processing_included: bool


@dataclass(frozen=True)
class ProductProvingFailureCapture:
    """One terminal failure that must remain distinguishable from success."""

    expected: ExpectedPreflight
    observed: ObservedPreflight
    pass_number: int
    phase: str
    errors: tuple[str, ...]
    extraction_comparisons: tuple[CandidateSetComparison, ...]
    extraction_run_receipts: tuple[Mapping[str, Any], ...]
    admission_started: bool
    source_database_mutated: bool
    operations_elapsed_seconds: float


@dataclass(frozen=True)
class ProductProvingBundleSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str


def compare_candidate_sets(
    *,
    document_id: int,
    baseline_run_id: int,
    fresh_run_id: int,
    baseline: Sequence[Mapping[str, Any]],
    fresh: Sequence[Mapping[str, Any]],
) -> CandidateSetComparison:
    """Compare Candidate meaning while ignoring only identities and ordering.

    Confidence, review state, and dedupe hints are not Candidate facts.  The
    Document, kind, supported fields, exact Evidence, prompt version, model,
    source pages, and mechanical citation result remain in the comparison.
    Duplicate semantic Candidates remain significant through the Counter.
    """

    baseline_configuration = _candidate_configuration(baseline, "baseline")
    fresh_configuration = _candidate_configuration(fresh, "fresh")
    if baseline_configuration != fresh_configuration:
        raise ValueError(
            "Product Proving Candidate comparison requires a "
            "configuration-compatible baseline"
        )

    baseline_values = [_canonical_candidate(document_id, item) for item in baseline]
    fresh_values = [_canonical_candidate(document_id, item) for item in fresh]
    baseline_encoded = [_canonical_json(item) for item in baseline_values]
    fresh_encoded = [_canonical_json(item) for item in fresh_values]
    baseline_counts = Counter(baseline_encoded)
    fresh_counts = Counter(fresh_encoded)
    added = _expanded_difference(fresh_counts - baseline_counts)
    missing = _expanded_difference(baseline_counts - fresh_counts)
    matched = tuple(
        sorted(
            sha256(value).hexdigest()
            for value, count in (baseline_counts & fresh_counts).items()
            for _ in range(count)
        )
    )
    return CandidateSetComparison(
        document_id=document_id,
        baseline_run_id=baseline_run_id,
        fresh_run_id=fresh_run_id,
        added=added,
        missing=missing,
        matched_sha256=matched,
    )


def _candidate_configuration(
    candidates: Sequence[Mapping[str, Any]], label: str
) -> tuple[str | None, str | None] | None:
    """Return one extractor identity, or refuse mixed Candidate lineage."""
    configurations = {
        (candidate.get("prompt_version"), candidate.get("model"))
        for candidate in candidates
    }
    if not configurations:
        return None
    if len(configurations) != 1:
        raise ValueError(
            f"Product Proving {label} Candidate set mixes extractor configurations"
        )
    return configurations.pop()


def verify_preflight(expected: ExpectedPreflight, observed: ObservedPreflight) -> None:
    """Refuse before writes when any source, schema, policy, or data pin moved."""

    if not observed.clean_worktree:
        raise ValueError("Product Proving requires a clean source checkout")
    checks = (
        ("source revision", expected.source_revision, observed.source_revision),
        ("origin/main", expected.origin_main_revision, observed.origin_main_revision),
        ("migration head", expected.migration_head, observed.migration_head),
        ("policy digest", dict(expected.policy_digests), dict(observed.policy_digests)),
        ("Document identity", dict(expected.documents), dict(observed.documents)),
        (
            "baseline Extraction Run",
            dict(expected.baseline_runs),
            dict(observed.baseline_runs),
        ),
        (
            "Milestone source",
            dict(expected.milestone_sources),
            dict(observed.milestone_sources),
        ),
        (
            "baseline fingerprint",
            expected.baseline_fingerprint,
            observed.baseline_fingerprint,
        ),
    )
    for label, wanted, found in checks:
        if wanted != found:
            raise ValueError(f"Product Proving {label} does not match the caller pin")


def verify_two_pass_capture(capture: ProductProvingCapture) -> None:
    """Recompute whether the capture earns ADR-0046's bounded claim."""

    verify_preflight(capture.expected, capture.observed)
    if not capture.simulated_practitioner:
        raise ValueError("the run must identify its simulated practitioner")
    if capture.same_project_manual_report_compared:
        raise ValueError("manual Report replacement is outside this proving claim")
    if capture.revision_processing_included:
        raise ValueError("Document revision processing is outside this proving run")
    for expected_number, run in enumerate(
        (capture.pass_one, capture.pass_two), start=1
    ):
        _verify_pass(capture.expected, run, expected_number)
    if (
        capture.final_baseline_fingerprint
        != capture.expected.baseline_fingerprint
    ):
        raise ValueError("final database restore does not match the sealed baseline")
    _verify_equivalent_passes(capture.pass_one, capture.pass_two)


def publish_product_proving_bundle(
    output_dir: Path, capture: ProductProvingCapture
) -> ProductProvingBundleSummary:
    """Publish both terminal passes outside the database and self-verify them."""

    verify_two_pass_capture(capture)
    canonical = _capture_json(capture)
    receipt = {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}
    markdown = _receipt_markdown(canonical).encode()
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "environment.json": {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "source_revision": capture.observed.source_revision,
                "origin_main_revision": capture.observed.origin_main_revision,
                "migration_head": capture.observed.migration_head,
            },
            "receipt.json": receipt,
            "receipt.md": markdown,
            "pass-1-approved-export.pdf": capture.pass_one.approved_export_bytes,
            "pass-2-approved-export.pdf": capture.pass_two.approved_export_bytes,
        },
        canonical_content=canonical,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-run",
        self_verification_failure="new Product Proving bundle failed self-verification",
    )
    verify_product_proving_bundle(
        Path(output_dir),
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    return ProductProvingBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def publish_product_proving_failure_bundle(
    output_dir: Path, capture: ProductProvingFailureCapture
) -> ProductProvingBundleSummary:
    """Seal a terminal failure without allowing success semantics."""

    verify_preflight(capture.expected, capture.observed)
    if capture.pass_number < 1:
        raise ValueError("failed Product Proving pass number must be positive")
    if not capture.phase or not capture.errors:
        raise ValueError("failed Product Proving receipt needs a phase and error")
    if capture.operations_elapsed_seconds < 0:
        raise ValueError("failed Product Proving timing must be non-negative")
    if capture.phase == "extraction_repeatability" and capture.admission_started:
        raise ValueError("repeatability failure must stop before Admission")
    canonical = asdict(capture)
    receipt = {
        "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
        "status": "failed",
        **canonical,
    }
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "environment.json": {
                "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
                "source_revision": capture.observed.source_revision,
                "origin_main_revision": capture.observed.origin_main_revision,
                "migration_head": capture.observed.migration_head,
            },
            "receipt.json": receipt,
            "receipt.md": _failure_markdown(canonical).encode(),
        },
        canonical_content=canonical,
        bundle_schema_version=BUNDLE_FAILURE_SCHEMA_VERSION,
        bundle_files=FAILURE_BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-failure",
        self_verification_failure="new Product Proving failure bundle is invalid",
    )
    verify_product_proving_failure_bundle(
        Path(output_dir),
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    return ProductProvingBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def verify_product_proving_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a closed receipt without PostgreSQL or the original checkout."""

    verified = verify_bundle(
        Path(bundle_dir),
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((Path(bundle_dir) / "canonical-content.json").read_bytes())
        receipt = json.loads((Path(bundle_dir) / "receipt.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptProductProvingBundle("Product Proving receipt is invalid") from exc
    if receipt != {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}:
        raise CorruptProductProvingBundle("receipt does not match canonical content")
    for number in (1, 2):
        pdf = (Path(bundle_dir) / f"pass-{number}-approved-export.pdf").read_bytes()
        expected = canonical[f"pass_{'one' if number == 1 else 'two'}"][
            "approved_export_sha256"
        ]
        if not pdf.startswith(b"%PDF-") or _sha256(pdf) != expected:
            raise CorruptProductProvingBundle(
                f"pass {number} Approved Export does not match its digest"
            )
    return verified


def verify_product_proving_failure_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a terminal failure and refuse any success-shaped receipt."""

    verified = verify_bundle(
        Path(bundle_dir),
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_FAILURE_SCHEMA_VERSION,
        bundle_files=FAILURE_BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((Path(bundle_dir) / "canonical-content.json").read_bytes())
        receipt = json.loads((Path(bundle_dir) / "receipt.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptProductProvingBundle("failure receipt is invalid") from exc
    expected = {
        "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
        "status": "failed",
        **canonical,
    }
    if receipt != expected:
        raise CorruptProductProvingBundle(
            "failure receipt does not match canonical content"
        )
    if not canonical.get("errors") or not canonical.get("phase"):
        raise CorruptProductProvingBundle("failure evidence is incomplete")
    if (
        canonical.get("phase") == "extraction_repeatability"
        and canonical.get("admission_started") is not False
    ):
        raise CorruptProductProvingBundle(
            "repeatability failure incorrectly claims Admission started"
        )
    return verified


def _verify_pass(
    expected: ExpectedPreflight, run: ProductProvingPass, expected_number: int
) -> None:
    if run.pass_number != expected_number:
        raise ValueError("Product Proving pass numbers are not consecutive")
    if run.restored_baseline_fingerprint != expected.baseline_fingerprint:
        raise ValueError(f"pass {run.pass_number} did not start from the sealed baseline")
    if run.extraction_failures:
        raise ValueError(f"pass {run.pass_number} has a failed Extraction Run")
    compared = {item.document_id for item in run.extraction_comparisons}
    if compared != set(expected.documents) or any(
        not item.equal for item in run.extraction_comparisons
    ):
        raise ValueError(
            f"pass {run.pass_number} failed Candidate semantic repeatability"
        )
    if not run.admission_completed:
        raise ValueError(f"pass {run.pass_number} stopped before Admission")
    residual_ids = set(run.residual_candidate_ids)
    if set(run.residual_outcomes) != residual_ids or not set(
        run.residual_outcomes.values()
    ) <= _OUTCOMES:
        raise ValueError(
            f"pass {run.pass_number} did not inspect every residual Candidate"
        )
    if run.frontend_kind != "real_frontend":
        raise ValueError("practitioner work did not use the real frontend")
    if run.workarounds:
        raise ValueError("practitioner phase used a terminal or database workaround")
    if not run.invalid_action_refused or run.invalid_action_write_set:
        raise ValueError("invalid frontend action was not refused atomically")
    if not run.correction_preserved_predecessor:
        raise ValueError("factual correction did not preserve its predecessor")
    if not run.work_decision_change_preserved_predecessor:
        raise ValueError("Work Decision change did not preserve its predecessor")
    if run.report_pdf_sha256 != run.approved_export_sha256:
        raise ValueError("Approved Export does not bind the exact reviewed Report")
    if _sha256(run.approved_export_bytes) != run.approved_export_sha256:
        raise ValueError("Approved Export bytes do not match the recorded digest")
    if not run.approved_export_bytes.startswith(b"%PDF-"):
        raise ValueError("Approved Export is not a PDF")
    if not _PROVENANCE_CLASSES <= set(run.report_provenance_classes):
        raise ValueError("Report does not preserve every required provenance class")
    if run.operations_elapsed_seconds < 0 or run.practitioner_elapsed_seconds < 0:
        raise ValueError("Product Proving timings must be non-negative")


def _verify_equivalent_passes(
    first: ProductProvingPass, second: ProductProvingPass
) -> None:
    first_semantics = {
        comparison.document_id: comparison.matched_sha256
        for comparison in first.extraction_comparisons
    }
    second_semantics = {
        comparison.document_id: comparison.matched_sha256
        for comparison in second.extraction_comparisons
    }
    if first_semantics != second_semantics:
        raise ValueError("the second pass changed Candidate semantic outputs")
    if Counter(first.residual_outcomes.values()) != Counter(
        second.residual_outcomes.values()
    ):
        raise ValueError("the second pass changed residual outcome behavior")
    first_shape = {key: len(value) for key, value in first.write_set.items()}
    second_shape = {key: len(value) for key, value in second.write_set.items()}
    if first_shape != second_shape:
        raise ValueError("the second pass changed declared write-set behavior")


def _canonical_candidate(
    expected_document_id: int, candidate: Mapping[str, Any]
) -> dict[str, Any]:
    document_id = candidate.get("source_document_id")
    if document_id != expected_document_id:
        raise ValueError("Candidate belongs to a different source Document")
    payload = candidate.get("payload_json")
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate payload is absent or invalid")
    fields = payload.get("fields")
    citations = payload.get("citations")
    if not isinstance(fields, Mapping) or not isinstance(citations, list):
        raise ValueError("Candidate facts or Evidence are absent or invalid")
    evidence = []
    for citation in citations:
        if not isinstance(citation, Mapping):
            raise ValueError("Candidate Evidence is invalid")
        evidence.append(
            {
                "document_id": citation.get("document_id"),
                "page": citation.get("page"),
                "quote": citation.get("quote"),
                "verified": citation.get("verified"),
                "whole_row": citation.get("whole_row"),
            }
        )
    return {
        "document_id": document_id,
        "kind": candidate.get("kind"),
        "fields": dict(fields),
        "evidence": sorted(evidence, key=_canonical_json),
        "source_pages": sorted(candidate.get("source_pages") or []),
        "prompt_version": candidate.get("prompt_version"),
        "model": candidate.get("model"),
        "citations_verified": candidate.get("citations_verified"),
    }


def _expanded_difference(counter: Counter[bytes]) -> tuple[dict[str, Any], ...]:
    return tuple(
        json.loads(value)
        for value, count in sorted(counter.items())
        for _ in range(count)
    )


def _capture_json(capture: ProductProvingCapture) -> dict[str, Any]:
    value = asdict(capture)
    for name in ("pass_one", "pass_two"):
        value[name].pop("approved_export_bytes")
        value[name]["extraction_comparisons"] = [
            asdict(comparison)
            for comparison in getattr(capture, name).extraction_comparisons
        ]
    return value


def _receipt_markdown(canonical: Mapping[str, Any]) -> str:
    first = canonical["pass_one"]
    second = canonical["pass_two"]
    return "\n".join(
        (
            "# SH99 bounded Product Proving Run",
            "",
            "Status: **passed**",
            "",
            "This receipt records two consecutive complete passes through the real ",
            "Corridor frontend under a simulated practitioner. It is product testing, ",
            "not independent practitioner validation.",
            "",
            "No same-project manual Report was compared. This run does not prove replacement ",
            "of a project's manual weekly Report.",
            "",
            f"- Pass 1 Approved Export: `{first['approved_export_sha256']}`",
            f"- Pass 2 Approved Export: `{second['approved_export_sha256']}`",
            f"- Restored baseline: `{canonical['final_baseline_fingerprint']}`",
            "- Document revision processing: excluded",
            "",
        )
    )


def _failure_markdown(canonical: Mapping[str, Any]) -> str:
    comparisons = canonical.get("extraction_comparisons") or []
    return "\n".join(
        (
            "# SH99 bounded Product Proving Run failure",
            "",
            "Status: **failed**",
            "",
            f"- Attempted pass: `{canonical['pass_number']}`",
            f"- Terminal phase: `{canonical['phase']}`",
            f"- Admission started: `{str(canonical['admission_started']).lower()}`",
            f"- Fresh Extraction Runs retained: `{len(canonical['extraction_run_receipts'])}`",
            f"- Candidate comparisons retained: `{len(comparisons)}`",
            "",
            "Errors:",
            *[f"- {error}" for error in canonical["errors"]],
            "",
            "This receipt is immutable failure evidence under a simulated practitioner ",
            "claim boundary. It grants no operational authority and supports no workflow ",
            "completeness or manual Report replacement claim.",
            "",
        )
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: bytes) -> str:
    return sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
