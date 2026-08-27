"""Publish one two-pass Product Proving result from independently sealed evidence.

The public success path never accepts a caller-authored capture JSON.  Each pass
must first be observed from the live Project Record and sealed by the frontend
capture boundary.  The final publisher verifies both pass bundles, the guarded
database baseline, the restored live database, and the refreshed Git pins before
it constructs the receipt contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from corridor.m8_acceptance_database import read_migration_head
from corridor.product_proving_database import (
    fingerprint_database_url,
    observe_database_connection_identity,
    verify_product_proving_database_baseline,
)
from corridor.product_proving_execution import (
    observe_git_checkout,
    proving_database_identity,
)
from corridor.product_proving_frontend_capture import verify_frontend_pass_bundle
from corridor.product_proving_restore import verify_product_proving_restore_bundle
from corridor.product_proving_run import (
    DatabaseConnectionIdentityEvidence,
    DatabaseSourceIdentity,
    ProductProvingBundleSummary,
    ProductProvingCapture,
    ProductProvingDatabaseBaselineEvidence,
    ProductProvingFinalSessionEvidence,
    ProductProvingFinalStateEvidence,
    ProductProvingPassExecutionEvidence,
    ProductProvingRestoreEvidence,
    publish_product_proving_bundle,
    verify_two_pass_capture,
)


@dataclass(frozen=True)
class ObservedProductProvingPublicationConfig:
    """Caller-held bundle digests and live local identities for final publication."""

    database_baseline_dir: Path
    database_baseline_manifest_sha256: str
    pass_one_dir: Path
    pass_one_manifest_sha256: str
    pass_two_dir: Path
    pass_two_manifest_sha256: str
    restore_one_dir: Path
    restore_one_manifest_sha256: str
    restore_two_dir: Path
    restore_two_manifest_sha256: str
    source_database_url: str
    repo_root: Path
    output_dir: Path


def publish_observed_product_proving_session(
    config: ObservedProductProvingPublicationConfig,
) -> ProductProvingBundleSummary:
    """Verify sealed passes and exact restoration, then publish the success bundle."""

    baseline = verify_product_proving_database_baseline(
        config.database_baseline_dir,
        expected_manifest_sha256=config.database_baseline_manifest_sha256,
    )
    first = verify_frontend_pass_bundle(
        config.pass_one_dir,
        expected_integrity_manifest_sha256=config.pass_one_manifest_sha256,
    )
    second = verify_frontend_pass_bundle(
        config.pass_two_dir,
        expected_integrity_manifest_sha256=config.pass_two_manifest_sha256,
    )
    restore_one = verify_product_proving_restore_bundle(
        config.restore_one_dir,
        expected_integrity_manifest_sha256=config.restore_one_manifest_sha256,
    )
    restore_two = verify_product_proving_restore_bundle(
        config.restore_two_dir,
        expected_integrity_manifest_sha256=config.restore_two_manifest_sha256,
    )
    if (
        not first.valid
        or not second.valid
        or not restore_one.valid
        or not restore_two.valid
    ):
        raise ValueError("Product Proving frontend pass bundle is not valid")
    if first.product_proving_pass.pass_number != 1 or first.pass_number != 1:
        raise ValueError("first frontend bundle is not Product Proving pass 1")
    if second.product_proving_pass.pass_number != 2 or second.pass_number != 2:
        raise ValueError("second frontend bundle is not Product Proving pass 2")
    if first.expected != second.expected or first.observed != second.observed:
        raise ValueError("Product Proving passes do not share exact preflight pins")
    if (
        first.canonical_content_sha256 == second.canonical_content_sha256
        or first.execution_id == second.execution_id
        or first.terminal_database_state_sha256
        == second.terminal_database_state_sha256
    ):
        raise ValueError("one frontend observation was replayed as both passes")
    if (
        restore_one.receipt.restore_operation_id
        == restore_two.receipt.restore_operation_id
    ):
        raise ValueError("Product Proving restore receipts are not distinct")
    for pass_evidence, restoration, number in (
        (first, restore_one, 1),
        (second, restore_two, 2),
    ):
        if (
            restoration.receipt.pass_number != number
            or restoration.receipt.pass_bundle_manifest_sha256
            != pass_evidence.integrity_manifest_sha256
            or restoration.receipt.pass_bundle_canonical_sha256
            != pass_evidence.canonical_content_sha256
            or restoration.receipt.terminal_database_state_sha256
            != pass_evidence.terminal_database_state_sha256
            or restoration.receipt.pass_execution_id != pass_evidence.execution_id
        ):
            raise ValueError(f"Product Proving pass {number} restore chain is invalid")

    for label, verified in (("pass 1", first), ("pass 2", second)):
        if (
            verified.database_baseline_manifest_sha256
            != baseline.manifest_sha256
            or verified.database_baseline_dump_sha256 != baseline.dump_sha256
            or verified.database_baseline_state_sha256
            != baseline.fingerprint.state_sha256
            or verified.database_baseline_schema_sha256
            != baseline.fingerprint.schema_sha256
            or dict(verified.database_source_identity)
            != (baseline.baseline.get("source_database") or {})
            or dict(verified.database_source_connection_identity)
            != (baseline.baseline.get("source_connection") or {})
        ):
            raise ValueError(f"{label} does not use the verified database baseline")

    if (
        first.prior_restore_operation_id is not None
        or first.prior_restore_bundle_manifest_sha256 is not None
        or first.prior_restore_bundle_canonical_sha256 is not None
        or second.prior_restore_operation_id
        != restore_one.receipt.restore_operation_id
        or second.prior_restore_bundle_manifest_sha256
        != restore_one.integrity_manifest_sha256
        or second.prior_restore_bundle_canonical_sha256
        != restore_one.canonical_content_sha256
    ):
        raise ValueError("Product Proving pass execution order is not restore-chained")

    source_json = baseline.baseline.get("source_database") or {}
    connection_json = baseline.baseline.get("source_connection") or {}
    for label, restoration in (("restore 1", restore_one), ("restore 2", restore_two)):
        receipt = restoration.receipt
        if (
            receipt.database_baseline_manifest_sha256 != baseline.manifest_sha256
            or receipt.database_baseline_dump_sha256 != baseline.dump_sha256
            or receipt.database_baseline_state_sha256
            != baseline.fingerprint.state_sha256
            or receipt.database_baseline_schema_sha256
            != baseline.fingerprint.schema_sha256
            or dict(receipt.source_database_identity) != source_json
            or dict(receipt.source_connection_identity) != connection_json
        ):
            raise ValueError(f"{label} does not use the verified database baseline")

    live_fingerprint = fingerprint_database_url(config.source_database_url)
    if live_fingerprint != baseline.fingerprint:
        raise ValueError("shared development database was not restored to the baseline")
    if proving_database_identity(config.source_database_url) != (
        source_json
    ):
        raise ValueError("final restore did not target the baseline source database")
    final_connection = observe_database_connection_identity(config.source_database_url)
    if final_connection.as_dict() != connection_json:
        raise ValueError("final restore changed the PostgreSQL server identity")
    checkout = observe_git_checkout(config.repo_root)
    if not checkout.clean_worktree:
        raise ValueError("final Product Proving publication requires a clean checkout")
    expected = first.expected
    if (
        checkout.source_revision != expected.source_revision
        or checkout.origin_main_revision != expected.origin_main_revision
    ):
        raise ValueError("final Product Proving Git pins changed after the passes")
    migration_head = read_migration_head(
        config.source_database_url,
        repo_root=config.repo_root,
        error_cls=ValueError,
    )
    if migration_head != expected.migration_head:
        raise ValueError("restored database migration head changed after the passes")
    baseline_checkout = baseline.baseline.get("checkout") or {}
    if baseline_checkout != {
        "revision": expected.source_revision,
        "migration_head": expected.migration_head,
    }:
        raise ValueError("database baseline checkout pins do not match the passes")

    source_identity = _source_identity(source_json)
    source_connection = _connection_identity(connection_json)
    final_evidence = ProductProvingFinalSessionEvidence(
        baseline=ProductProvingDatabaseBaselineEvidence(
            manifest_sha256=baseline.manifest_sha256,
            dump_sha256=baseline.dump_sha256,
            state_sha256=baseline.fingerprint.state_sha256,
            schema_sha256=baseline.fingerprint.schema_sha256,
            source_identity=source_identity,
            source_connection_identity=source_connection,
        ),
        pass_one=_pass_evidence(first),
        restore_one=_restore_evidence(
            restore_one,
            source_identity=source_identity,
            source_connection=source_connection,
        ),
        pass_two=_pass_evidence(second),
        restore_two=_restore_evidence(
            restore_two,
            source_identity=source_identity,
            source_connection=source_connection,
        ),
        final_state=ProductProvingFinalStateEvidence(
            database_state_sha256=live_fingerprint.state_sha256,
            database_schema_sha256=live_fingerprint.schema_sha256,
            source_identity=source_identity,
            source_connection_identity=source_connection,
        ),
    )
    capture = ProductProvingCapture(
        expected=expected,
        observed=first.observed,
        pass_one=first.product_proving_pass,
        pass_two=second.product_proving_pass,
        final_session_evidence=final_evidence,
        final_baseline_fingerprint=live_fingerprint.state_sha256,
        simulated_practitioner=True,
        same_project_manual_report_compared=False,
        revision_processing_included=False,
    )
    verify_two_pass_capture(capture)
    return publish_product_proving_bundle(config.output_dir, capture)


def _source_identity(value: object) -> DatabaseSourceIdentity:
    if not isinstance(value, dict):
        raise ValueError("database source identity is invalid")
    try:
        return DatabaseSourceIdentity(**value)
    except TypeError as exc:
        raise ValueError("database source identity is invalid") from exc


def _connection_identity(value: object) -> DatabaseConnectionIdentityEvidence:
    if not isinstance(value, dict):
        raise ValueError("database connection identity is invalid")
    try:
        return DatabaseConnectionIdentityEvidence(**value)
    except TypeError as exc:
        raise ValueError("database connection identity is invalid") from exc


def _pass_evidence(frontend) -> ProductProvingPassExecutionEvidence:
    return ProductProvingPassExecutionEvidence(
        execution_id=frontend.execution_id,
        pass_number=frontend.pass_number,
        prior_restore_operation_id=frontend.prior_restore_operation_id,
        prior_restore_bundle_manifest_sha256=(
            frontend.prior_restore_bundle_manifest_sha256
        ),
        prior_restore_bundle_canonical_sha256=(
            frontend.prior_restore_bundle_canonical_sha256
        ),
        started_at=frontend.started_at,
        starting_database_state_sha256=frontend.observed.baseline_fingerprint,
        starting_database_schema_sha256=frontend.database_baseline_schema_sha256,
        frontend_bundle_manifest_sha256=frontend.integrity_manifest_sha256,
        frontend_bundle_canonical_sha256=frontend.canonical_content_sha256,
        terminal_database_state_sha256=frontend.terminal_database_state_sha256,
    )


def _restore_evidence(
    verified,
    *,
    source_identity: DatabaseSourceIdentity,
    source_connection: DatabaseConnectionIdentityEvidence,
) -> ProductProvingRestoreEvidence:
    receipt = verified.receipt
    return ProductProvingRestoreEvidence(
        restore_operation_id=receipt.restore_operation_id,
        pass_number=receipt.pass_number,
        pass_execution_id=receipt.pass_execution_id,
        pass_bundle_manifest_sha256=receipt.pass_bundle_manifest_sha256,
        pass_bundle_canonical_sha256=receipt.pass_bundle_canonical_sha256,
        restore_bundle_manifest_sha256=verified.integrity_manifest_sha256,
        restore_bundle_canonical_sha256=verified.canonical_content_sha256,
        previous_database_state_sha256=receipt.previous_state_sha256,
        restored_database_state_sha256=receipt.restored_state_sha256,
        database_baseline_manifest_sha256=(
            receipt.database_baseline_manifest_sha256
        ),
        database_baseline_dump_sha256=receipt.database_baseline_dump_sha256,
        database_baseline_state_sha256=receipt.database_baseline_state_sha256,
        database_baseline_schema_sha256=receipt.database_baseline_schema_sha256,
        database_source_identity=source_identity,
        database_source_connection_identity=source_connection,
        started_at=receipt.started_at,
        completed_at=receipt.completed_at,
    )
