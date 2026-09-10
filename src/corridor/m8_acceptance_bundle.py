"""Bundle writing and verification for M8 acceptance exports.

Every caller used to hand this module its own `canonical_json`, `sha256` and
`json_sha256` callables plus two exception classes, five injected parameters
that existed only because each module owned a private copy of the same digest.
`corridor.digests` owns the encodings now, so the only thing a caller still
chooses is *which declared encoding a retained bundle was sealed with*, and
that is one named argument rather than three functions.

Refusals are this module's own types. A caller that needs its own exception
class in its own public seam catches `BundleRefused`/`CorruptBundle` and
re-raises; it no longer passes a class in so this module can raise on its
behalf.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal

from corridor import digests
from corridor.m8_acceptance_publication import publish_directory_once


class BundleRefused(ValueError):
    """One closed export could not be published, or failed self-verification."""


class CorruptBundle(BundleRefused):
    """A published export does not match the manifest identity pinned for it."""


# `canonical` is what a new bundle uses. `ascii-escaped` exists only because
# bundles already on disk were sealed with non-ASCII escaped and must keep
# re-verifying: `artifacts/product-proving/sh99-8da8568-extraction-\
# repeatability-failed/manifest.json` pins a `canonical_content_sha256` that
# only the escaped encoding reproduces.
BundleEncoding = Literal["canonical", "ascii-escaped"]

_ENCODINGS = {
    "canonical": (digests.canonical_json, digests.canonical_sha256),
    "ascii-escaped": (digests.ascii_escaped_json, digests.ascii_escaped_sha256),
}


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    integrity_manifest_sha256: str
    canonical_content_sha256: str


def write_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    real_chain: dict[str, Any],
    controlled_lane: dict[str, Any],
    assertions,
    canonical_content: dict[str, Any],
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    encoding: BundleEncoding = "canonical",
) -> tuple[Path, str, str]:
    exports = {
        "environment.json": environment,
        "real-chain.json": real_chain,
        "controlled-lane.json": controlled_lane,
        "assertions.json": {
            "claim_boundary": canonical_content["claim_boundary"],
            "assertions": [asdict(item) for item in assertions],
        },
        "canonical-content.json": canonical_content,
    }
    return publish_verified_bundle(
        output_dir,
        exports=exports,
        canonical_content=canonical_content,
        bundle_schema_version=bundle_schema_version,
        bundle_files=bundle_files,
        encoding=encoding,
        temp_prefix="corridor-m8-bundle",
        self_verification_failure="new acceptance bundle failed self-verification",
    )


def publish_verified_bundle(
    output_dir: Path,
    *,
    exports: dict[str, Any | bytes],
    canonical_content: dict[str, Any],
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    encoding: BundleEncoding = "canonical",
    temp_prefix: str,
    self_verification_failure: str,
) -> tuple[Path, str, str]:
    """Publish and self-verify any closed, digest-pinned acceptance export."""

    canonical_json, json_sha256 = _ENCODINGS[encoding]
    canonical_sha256 = json_sha256(canonical_content)
    try:
        published_dir = publish_directory_once(
            output_dir,
            temp_prefix=temp_prefix,
            build=lambda stage_dir: _stage_bundle(
                stage_dir,
                exports=exports,
                canonical_sha256=canonical_sha256,
                bundle_schema_version=bundle_schema_version,
                bundle_files=bundle_files,
                canonical_json=canonical_json,
            ),
        )
    except FileExistsError as exc:
        raise BundleRefused("bundle output directory already exists") from exc
    manifest_path = published_dir / "manifest.json"
    manifest_sha256 = digests.sha256_bytes(manifest_path.read_bytes())
    verified = verify_bundle(
        published_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
        bundle_schema_version=bundle_schema_version,
        bundle_files=bundle_files,
        encoding=encoding,
    )
    if verified.canonical_content_sha256 != canonical_sha256:
        raise BundleRefused(self_verification_failure)
    return manifest_path, manifest_sha256, canonical_sha256


def verify_bundle(
    bundle_dir: Path,
    *,
    expected_integrity_manifest_sha256: str,
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    encoding: BundleEncoding = "canonical",
) -> VerificationResult:
    """Verify the export set against a caller-held manifest identity pin."""

    _, json_sha256 = _ENCODINGS[encoding]
    sha256 = digests.sha256_bytes
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    if re.fullmatch(r"[0-9a-f]{64}", expected_integrity_manifest_sha256) is None:
        raise CorruptBundle("expected manifest digest is invalid")
    if manifest_path.is_symlink():
        raise CorruptBundle("manifest.json must not be a symlink")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise CorruptBundle("manifest.json is absent or invalid") from exc
    actual_manifest_sha256 = sha256(manifest_bytes)
    if actual_manifest_sha256 != expected_integrity_manifest_sha256:
        raise CorruptBundle(
            "manifest digest does not match the caller-provided pin"
        )
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise CorruptBundle("manifest.json is absent or invalid") from exc
    if manifest.get("schema_version") != bundle_schema_version:
        raise CorruptBundle("manifest.json has an unsupported schema")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(bundle_files):
        raise CorruptBundle("manifest.json does not name the exact export set")
    try:
        actual_entries = {path.name: path for path in bundle_dir.iterdir()}
    except OSError as exc:
        raise CorruptBundle("bundle directory is absent or invalid") from exc
    if set(actual_entries) != {"manifest.json", *bundle_files}:
        raise CorruptBundle("bundle contains an unmanifested export")
    if any(
        path.is_symlink() or not path.is_file()
        for path in actual_entries.values()
    ):
        raise CorruptBundle("bundle contains an unmanifested export")

    root = bundle_dir.resolve()
    for relative_path, expected in sorted(files.items()):
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != relative_path
            or len(pure.parts) != 1
        ):
            raise CorruptBundle(f"unsafe manifest path {relative_path!r}")
        path = bundle_dir / relative_path
        if path.is_symlink():
            raise CorruptBundle(f"{relative_path} must not be a symlink")
        try:
            path.resolve().relative_to(root)
            value = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise CorruptBundle(f"{relative_path} is absent or unsafe") from exc
        if not isinstance(expected, dict):
            raise CorruptBundle(f"{relative_path} manifest entry is invalid")
        if expected.get("bytes") != len(value) or expected.get("sha256") != sha256(
            value
        ):
            raise CorruptBundle(f"{relative_path} does not match its digest")

    try:
        canonical = json.loads((bundle_dir / "canonical-content.json").read_bytes())
    except json.JSONDecodeError as exc:
        raise CorruptBundle("canonical-content.json is invalid") from exc
    canonical_sha256 = json_sha256(canonical)
    if manifest.get("canonical_content_sha256") != canonical_sha256:
        raise CorruptBundle("canonical-content.json identity does not match")
    return VerificationResult(
        valid=True,
        integrity_manifest_sha256=actual_manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def _stage_bundle(
    output_dir: Path,
    *,
    exports: dict[str, Any | bytes],
    canonical_sha256: str,
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    canonical_json,
) -> None:
    file_manifest: dict[str, dict[str, Any]] = {}
    for relative_path in bundle_files:
        export = exports[relative_path]
        value = export if isinstance(export, bytes) else canonical_json(export) + b"\n"
        target = output_dir / relative_path
        target.write_bytes(value)
        file_manifest[relative_path] = {
            "bytes": len(value),
            "sha256": digests.sha256_bytes(value),
        }
    manifest = {
        "schema_version": bundle_schema_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canonical_content_sha256": canonical_sha256,
        "files": file_manifest,
    }
    (output_dir / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
