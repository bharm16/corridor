"""Seal the mandatory baseline restoration between Product Proving passes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol
from uuid import UUID

from corridor.m8_acceptance_bundle import publish_verified_bundle, verify_bundle
from corridor.product_proving_database import (
    SharedDevelopmentRestoreSummary,
    VerifiedProductProvingDatabaseBaseline,
)


SCHEMA_VERSION = "corridor.product-proving-restore.v2"
BUNDLE_FILES = ("canonical-content.json", "receipt.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VerifiedFrontendPassEvidence(Protocol):
    """Structural input from the independently verified frontend bundle."""

    valid: bool
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    terminal_database_state_sha256: str
    database_baseline_manifest_sha256: str
    database_baseline_dump_sha256: str
    database_baseline_state_sha256: str
    database_baseline_schema_sha256: str
    database_source_identity: Mapping[str, object]
    database_source_connection_identity: Mapping[str, object]
    product_proving_pass: Any
    execution_id: str


class CorruptProductProvingRestore(ValueError):
    """A restore receipt does not match its sealed pass and baseline."""


@dataclass(frozen=True)
class ProductProvingRestoreReceipt:
    restore_operation_id: str
    pass_number: int
    pass_bundle_manifest_sha256: str
    pass_bundle_canonical_sha256: str
    terminal_database_state_sha256: str
    pass_execution_id: str
    database_baseline_manifest_sha256: str
    database_baseline_dump_sha256: str
    database_baseline_state_sha256: str
    database_baseline_schema_sha256: str
    source_database_identity: Mapping[str, object]
    source_connection_identity: Mapping[str, object]
    previous_database_oid: int
    restored_database_oid: int
    previous_state_sha256: str
    restored_state_sha256: str
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class ProductProvingRestoreBundleSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class VerifiedProductProvingRestore:
    valid: bool
    bundle_dir: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    receipt: ProductProvingRestoreReceipt


def publish_product_proving_restore_bundle(
    output_dir: Path | str,
    *,
    frontend_pass: VerifiedFrontendPassEvidence,
    baseline: VerifiedProductProvingDatabaseBaseline,
    restore: SharedDevelopmentRestoreSummary,
) -> ProductProvingRestoreBundleSummary:
    """Bind one exact terminal pass state to its exact successful restoration."""

    source = baseline.baseline.get("source_database") or {}
    if (
        frontend_pass.valid is not True
        or frontend_pass.database_baseline_manifest_sha256 != baseline.manifest_sha256
        or frontend_pass.database_baseline_dump_sha256 != baseline.dump_sha256
        or frontend_pass.database_baseline_state_sha256
        != baseline.fingerprint.state_sha256
        or frontend_pass.database_baseline_schema_sha256
        != baseline.fingerprint.schema_sha256
        or dict(frontend_pass.database_source_identity) != source
        or dict(frontend_pass.database_source_connection_identity)
        != (baseline.baseline.get("source_connection") or {})
    ):
        raise ValueError("frontend pass does not use the restore baseline")
    receipt = ProductProvingRestoreReceipt(
        restore_operation_id=restore.operation_id,
        pass_number=frontend_pass.product_proving_pass.pass_number,
        pass_bundle_manifest_sha256=frontend_pass.integrity_manifest_sha256,
        pass_bundle_canonical_sha256=frontend_pass.canonical_content_sha256,
        terminal_database_state_sha256=frontend_pass.terminal_database_state_sha256,
        pass_execution_id=frontend_pass.execution_id,
        database_baseline_manifest_sha256=baseline.manifest_sha256,
        database_baseline_dump_sha256=baseline.dump_sha256,
        database_baseline_state_sha256=baseline.fingerprint.state_sha256,
        database_baseline_schema_sha256=baseline.fingerprint.schema_sha256,
        source_database_identity=dict(source),
        source_connection_identity=dict(
            baseline.baseline.get("source_connection") or {}
        ),
        previous_database_oid=restore.previous_database_oid,
        restored_database_oid=restore.restored_database_oid,
        previous_state_sha256=restore.previous_state_sha256,
        restored_state_sha256=restore.restored_state_sha256,
        started_at=restore.started_at,
        completed_at=restore.completed_at,
    )
    _validate_receipt(receipt)
    if receipt.previous_state_sha256 != receipt.terminal_database_state_sha256:
        raise ValueError("restore did not start from the sealed frontend pass state")
    if (
        receipt.restored_state_sha256 != receipt.database_baseline_state_sha256
        or restore.manifest_sha256 != receipt.database_baseline_manifest_sha256
        or restore.dump_sha256 != receipt.database_baseline_dump_sha256
        or restore.source_database_name
        != receipt.source_database_identity.get("database")
    ):
        raise ValueError("restore does not match the verified database baseline")
    canonical = {"schema_version": SCHEMA_VERSION, **asdict(receipt)}
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "receipt.json": canonical,
        },
        canonical_content=canonical,
        bundle_schema_version=SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptProductProvingRestore,
        canonical_json=_canonical_json,
        sha256=_sha256_bytes,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-restore",
        self_verification_failure="new Product Proving restore bundle is invalid",
    )
    verified = verify_product_proving_restore_bundle(
        output_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    if verified.receipt != receipt:
        raise ValueError("self-verified restore receipt changed")
    return ProductProvingRestoreBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def verify_product_proving_restore_bundle(
    bundle_dir: Path | str,
    *,
    expected_integrity_manifest_sha256: str,
) -> VerifiedProductProvingRestore:
    """Verify one restoration chain link without PostgreSQL."""

    root = Path(bundle_dir)
    verified = verify_bundle(
        root,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptProductProvingRestore,
        sha256=_sha256_bytes,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((root / "canonical-content.json").read_bytes())
        receipt_json = json.loads((root / "receipt.json").read_bytes())
        if canonical != receipt_json or canonical.pop("schema_version") != SCHEMA_VERSION:
            raise ValueError
        receipt = ProductProvingRestoreReceipt(**canonical)
        _validate_receipt(receipt)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CorruptProductProvingRestore("restore receipt is invalid") from exc
    if (
        receipt.previous_state_sha256 != receipt.terminal_database_state_sha256
        or receipt.restored_state_sha256 != receipt.database_baseline_state_sha256
    ):
        raise CorruptProductProvingRestore("restore receipt does not close its state chain")
    return VerifiedProductProvingRestore(
        valid=verified.valid,
        bundle_dir=root,
        integrity_manifest_sha256=verified.integrity_manifest_sha256,
        canonical_content_sha256=verified.canonical_content_sha256,
        receipt=receipt,
    )


def _validate_receipt(receipt: ProductProvingRestoreReceipt) -> None:
    if receipt.pass_number not in {1, 2}:
        raise ValueError("restore receipt pass number is invalid")
    try:
        parsed = UUID(receipt.restore_operation_id)
        execution = UUID(receipt.pass_execution_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("restore receipt id is invalid") from exc
    if str(parsed) != receipt.restore_operation_id or parsed.version != 4:
        raise ValueError("restore operation id is not canonical")
    if str(execution) != receipt.pass_execution_id or execution.version != 4:
        raise ValueError("restore pass execution id is not canonical")
    for value in (
        receipt.pass_bundle_manifest_sha256,
        receipt.pass_bundle_canonical_sha256,
        receipt.terminal_database_state_sha256,
        receipt.database_baseline_manifest_sha256,
        receipt.database_baseline_dump_sha256,
        receipt.database_baseline_state_sha256,
        receipt.database_baseline_schema_sha256,
        receipt.previous_state_sha256,
        receipt.restored_state_sha256,
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError("restore receipt digest is invalid")
    source = receipt.source_database_identity
    if (
        not isinstance(source, Mapping)
        or set(source) != {"backend", "host", "port", "username", "database"}
        or any(source.get(key) in {None, ""} for key in source)
    ):
        raise ValueError("restore receipt identity is incomplete")
    connection = receipt.source_connection_identity
    if (
        not isinstance(connection, Mapping)
        or set(connection)
        != {
            "database",
            "username",
            "server_address",
            "server_port",
            "system_identifier",
            "postgres_version",
        }
        or any(connection.get(key) in {None, ""} for key in connection)
        or connection.get("database") != source.get("database")
        or connection.get("username") != source.get("username")
    ):
        raise ValueError("restore server identity is incomplete")
    if (
        isinstance(receipt.previous_database_oid, bool)
        or isinstance(receipt.restored_database_oid, bool)
        or receipt.previous_database_oid <= 0
        or receipt.restored_database_oid <= 0
        or receipt.previous_database_oid == receipt.restored_database_oid
    ):
        raise ValueError("restore database OID transition is invalid")
    try:
        started = datetime.fromisoformat(receipt.started_at)
        completed = datetime.fromisoformat(receipt.completed_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("restore operation time is invalid") from exc
    if (
        started.tzinfo is None
        or completed.tzinfo is None
        or completed < started
    ):
        raise ValueError("restore operation chronology is invalid")


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256_bytes(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()


def _json_sha256(value) -> str:
    return _sha256_bytes(_canonical_json(value))
