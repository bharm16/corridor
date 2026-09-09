"""Whole-environment export bytes and external custody verification (#514).

Earlier disposition accepted two custody booleans and could delete without an
export. This module creates a restorable PostgreSQL custom-format dump plus the
exact object versions, and verifies every archived member before accepting a
versioned external copy. It deliberately knows no customer table or model.
Credentials are passed through an explicit pgpass file; no connection URL or
secret value is printed, retained in the archive, or put on a command line.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from corridor.environment_disposition import DispositionRefused
from corridor.principals import require_human_principal


_CHUNK = 1024 * 1024


def _copy_digest(source, destination=None):
    checksum = sha256()
    size = 0
    while chunk := source.read(_CHUNK):
        checksum.update(chunk)
        size += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return checksum.hexdigest(), size


def export_environment_archive(*, resources, inventory, clients, output_path,
                               pgpass_file, database_username, principal, before_export,
                               unavailability_disclosure: bytes = b"", run=subprocess.run,
                               restore_run=subprocess.run, postgres_tls_root_cert=None):
    """Create a complete archive after the caller has frozen application writes.

    ``before_export`` is the operator's fresh binding/hold/quiescence check; the
    selected provider's ``_require_quiescent`` supplies ECS observations. The
    supplied credentials must be a read-capable whole-database export identity.
    The function executes pg_dump, checks its exit status, preserves all object
    versions/delete markers, then repeats object inventory and the freeze guard.
    """
    from corridor.aws_environment_disposition import digest
    actor = require_human_principal(principal).subject
    if "custody" in inventory:
        raise DispositionRefused("export inventory must precede its custody receipt")
    before_export()
    bucket = resources.object_namespace_ref.removeprefix("s3:").rstrip("/")
    if not resources.object_namespace_ref.startswith("s3:") or "/" in bucket:
        raise DispositionRefused("whole-environment export requires a dedicated bucket")
    s3 = clients["s3"]
    parameters = {"Bucket": bucket, "ExpectedBucketOwner": resources.account_id}

    def versions():
        rows = []
        for page in s3.get_paginator("list_object_versions").paginate(**parameters):
            rows.extend({"key": r["Key"], "version_id": r["VersionId"], "delete_marker": False}
                        for r in page.get("Versions", []))
            rows.extend({"key": r["Key"], "version_id": r["VersionId"], "delete_marker": True}
                        for r in page.get("DeleteMarkers", []))
        return sorted(rows, key=lambda row: (row["key"], row["version_id"], row["delete_marker"]))

    if s3.get_bucket_versioning(**parameters).get("Status") != "Enabled":
        raise DispositionRefused("export requires enabled S3 versioning")
    before = versions()
    with tempfile.TemporaryDirectory(prefix="corridor-export-") as temporary:
        directory = Path(temporary)
        database = directory / "database.dump"
        command = ["pg_dump", "--format=custom", "--no-password", "--host", resources.database_host,
                   "--port", str(resources.database_port), "--dbname", resources.database_name,
                   "--username", database_username, "--file", str(database)]
        # No ambient PGHOST/PGDATABASE/PGSERVICE is inherited. libpq credentials
        # come only from the explicit pgpass file, kept outside exported bytes.
        import os
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        environment["PGPASSFILE"] = str(Path(pgpass_file).resolve(strict=True))
        if postgres_tls_root_cert is not None:
            environment["PGSSLMODE"] = "verify-full"
            environment["PGSSLROOTCERT"] = str(Path(postgres_tls_root_cert).resolve(strict=True))
        run(command, env=environment, check=True, capture_output=True)
        if not database.is_file():
            raise DispositionRefused("pg_dump did not create a PostgreSQL custom-format archive")
        with database.open("rb") as body:
            if body.read(5) != b"PGDMP":
                raise DispositionRefused("pg_dump did not create a PostgreSQL custom-format archive")
        database_validation = validate_postgres_dump(database, run=restore_run)
        members = []
        output = Path(output_path)
        # mkstemp creates the full customer archive privately, independent of
        # ambient umask. Stage beside the destination so the final replace is
        # atomic and leaves any previously verified export intact on failure.
        descriptor, staged_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".partial", dir=output.parent)
        staged_output = Path(staged_name)
        try:
            with os.fdopen(descriptor, "w+b") as staged_stream:
                with tarfile.open(fileobj=staged_stream, mode="w") as archive:
                    def append_file(path, name, **source):
                        with path.open("rb") as body:
                            checksum, length = _copy_digest(body)
                        archive.add(path, arcname=name, recursive=False)
                        members.append({"name": name, "sha256": checksum, "size": length, **source})
                    append_file(database, "database.dump", kind="postgresql")
                    for index, item in enumerate(before):
                        if item["delete_marker"]:
                            continue
                        path = directory / "object"
                        response = s3.get_object(**parameters, Key=item["key"], VersionId=item["version_id"])
                        if response.get("VersionId") != item["version_id"]:
                            response["Body"].close()
                            raise DispositionRefused("source object version changed during export")
                        with response["Body"] as body, path.open("wb") as destination:
                            shutil.copyfileobj(body, destination, _CHUNK)
                        append_file(path, f"objects/{index}", kind="source", **item)
                    if unavailability_disclosure:
                        disclosure = directory / "disclosure"
                        disclosure.write_bytes(unavailability_disclosure)
                        append_file(disclosure, "unavailability-disclosure", kind="disclosure")
                    manifest = {"schema": "corridor-environment-export-v1", "environment_id": resources.environment_id,
                        "inventory_sha256": digest(inventory), "db_resource_id": resources.db_resource_id,
                        "exported_by": actor, "members": members, "object_versions": before,
                        "database_validation": database_validation,
                        "unavailability_disclosure": "unavailability-disclosure" if unavailability_disclosure else None}
                    path = directory / "manifest.json"
                    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
                    archive.add(path, arcname="manifest.json", recursive=False)
                before_export()
                if before != versions():
                    raise DispositionRefused("object versions changed during export; repeat after writes are frozen")
                staged_stream.flush()
                staged_stream.seek(0)
                checksum, _ = _copy_digest(staged_stream)
                staged_stream.seek(0)
                verify_export_archive(staged_stream, expected_sha256=checksum,
                    environment_id=resources.environment_id, inventory_sha256=digest(inventory), restore_run=restore_run)
                os.fchmod(staged_stream.fileno(), 0o600)
                os.fsync(staged_stream.fileno())
            os.replace(staged_output, output)
            return {"path": str(output.resolve()), "sha256": checksum, "manifest": manifest}
        finally:
            staged_output.unlink(missing_ok=True)



def validate_postgres_dump(path, *, run=subprocess.run):
    """Parse the entire custom archive without connecting to or executing SQL.

    A PGDMP prefix and pg_restore --list validate neither data blocks nor their
    compression streams. Materializing all restore output to /dev/null forces
    pg_restore to consume the complete archive while executing no contained SQL.
    """
    import os
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    command = ["pg_restore", "--file", os.devnull, "--no-owner", "--no-privileges", str(path)]
    try:
        run(command, env=environment, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DispositionRefused("PostgreSQL full archive validation failed") from exc
    with Path(path).open("rb") as stream:
        checksum, size = _copy_digest(stream)
    return {"method": "pg-restore-full-stream-v1", "database_sha256": checksum, "database_size": size}


def verify_export_archive(stream, *, expected_sha256, environment_id, inventory_sha256,
                          restore_run=subprocess.run):
    """Verify untrusted archive bytes without extracting member paths."""
    # A seekable spool avoids loading a customer's database into Python memory.
    with tempfile.TemporaryFile() as spool:
        actual, _ = _copy_digest(stream, spool)
        if actual != expected_sha256:
            raise DispositionRefused("external export bytes differ from the custody digest")
        spool.seek(0)
        with tarfile.open(fileobj=spool, mode="r:") as archive:
            entries = archive.getmembers()
            if len({entry.name for entry in entries}) != len(entries) or any(not e.isfile() for e in entries):
                raise DispositionRefused("export archive has duplicate or non-file members")
            try:
                entry = archive.getmember("manifest.json")
            except KeyError as exc:
                raise DispositionRefused("export archive has no manifest") from exc
            if entry.size > 32 * 1024 * 1024:
                raise DispositionRefused("export manifest exceeds the bounded verification limit")
            with archive.extractfile(entry) as body:
                manifest = json.load(body)
            if not isinstance(manifest, Mapping) or (manifest.get("schema"), manifest.get("environment_id"), manifest.get("inventory_sha256")) != (
                    "corridor-environment-export-v1", environment_id, inventory_sha256):
                raise DispositionRefused("export manifest does not bind this environment inventory")
            members = manifest.get("members", [])
            names = [row["name"] for row in members]
            if len(set(names)) != len(names) or set(names) | {"manifest.json"} != {e.name for e in entries}:
                raise DispositionRefused("archive members differ from its manifest")
            databases = [row for row in members if row.get("kind") == "postgresql"]
            if len(databases) != 1 or databases[0]["name"] != "database.dump":
                raise DispositionRefused("export needs exactly one whole PostgreSQL dump")
            expected_objects = {(r["key"], r["version_id"]) for r in manifest.get("object_versions", []) if not r["delete_marker"]}
            actual_objects = [(r.get("key"), r.get("version_id")) for r in members if r.get("kind") == "source"]
            if len(set(actual_objects)) != len(actual_objects) or set(actual_objects) != expected_objects:
                raise DispositionRefused("archive omits or duplicates a retained object version")
            if manifest.get("unavailability_disclosure") and manifest["unavailability_disclosure"] not in names:
                raise DispositionRefused("source-unavailability disclosure bytes are missing")
            for row in members:
                with archive.extractfile(row["name"]) as body:
                    if row["kind"] == "postgresql":
                        with tempfile.NamedTemporaryFile(prefix="corridor-restore-check-", suffix=".dump") as dump:
                            shutil.copyfileobj(body, dump, _CHUNK)
                            dump.flush()
                            validation = validate_postgres_dump(dump.name, run=restore_run)
                        if manifest.get("database_validation") != validation:
                            raise DispositionRefused("export database validation evidence differs from parsed archive")
                        body.seek(0)
                    checksum, length = _copy_digest(body)
                if (checksum, length) != (row["sha256"], row["size"]):
                    raise DispositionRefused("export member content does not match its manifest")
            return manifest


def transfer_export_custody(*, client, archive_path, bucket, key, owner,
                            environment_id, inventory_sha256, principal):
    """Upload, read back the exact version, and retain a named human acceptance."""
    actor = require_human_principal(principal).subject
    with Path(archive_path).open("rb") as stream:
        checksum, _ = _copy_digest(stream)
    client.upload_file(str(archive_path), bucket, key, ExtraArgs={"ExpectedBucketOwner": owner})
    head = client.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
    version = head.get("VersionId")
    if not version or version == "null":
        raise DispositionRefused("export custody requires a versioned external object")
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version, ExpectedBucketOwner=owner)
    with response["Body"] as body:
        verify_export_archive(body, expected_sha256=checksum, environment_id=environment_id,
                              inventory_sha256=inventory_sha256)
    return {"bucket": bucket, "key": key, "owner": owner, "version_id": version,
            "sha256": checksum, "accepted_by": actor}
