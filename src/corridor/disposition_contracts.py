"""Shared public contracts for disposition, export and recovery (#514).

Provider verification and archive creation previously imported one another for
small digest helpers, and recovery reached into a provider's private paginator.
These stable identities, refusal types and observation helpers sit below those
workflows so each can share the contracts without importing another workflow.
Nothing here constructs clients, opens credentials, or performs mutations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any

from corridor.control_plane import EnvironmentRegistration


class DispositionRefused(ValueError):
    """A hold, retention obligation, open reference, or stale plan stopped disposition."""


class EnvironmentDestructionError(RuntimeError):
    """A provider-native destruction step failed; the sequence is resumable."""


@dataclass(frozen=True)
class AwsDispositionResources:
    """Exact provider inventory approved beside the disposition plan.

    An RDS instance identifier is not the SQL database name. Retain both the
    ARN and immutable DbiResourceId so identifier reuse cannot delete a new DB.
    This profile covers same-region RDS backups, one S3 namespace and dedicated
    customer-managed keys. Other stores/copies require a wider inventory first.
    """

    customer_id: str
    environment_id: str
    deployment_id: str
    account_id: str
    region: str
    database_host: str
    database_port: int
    database_name: str
    db_instance_identifier: str
    db_instance_arn: str
    db_resource_id: str
    object_namespace_ref: str
    final_snapshot_identifier: str
    kms_key_arns: tuple[str, ...]
    whole_environment: dict[str, Any] | None = None

    def require_registration(self, registration: EnvironmentRegistration) -> None:
        for name in ("customer_id", "environment_id", "deployment_id", "database_host", "database_port", "database_name", "object_namespace_ref"):
            if getattr(self, name) != getattr(registration, name):
                raise DispositionRefused("AWS resource inventory differs from the registered environment")

    @property
    def sha256(self) -> str:
        return sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def json_digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provider_rows(client, operation, field, **parameters):
    return [row for page in client.get_paginator(operation).paginate(**parameters)
            for row in page.get(field, [])]


def rds_replica_relationships(row):
    """Keep the provider's physical-source replication relationships explicit."""
    return {"db_resource_id": row["DbiResourceId"],
            "read_replica_instances": sorted(row.get("ReadReplicaDBInstanceIdentifiers", [])),
            "read_replica_clusters": sorted(row.get("ReadReplicaDBClusterIdentifiers", [])),
            "read_replica_source": row.get("ReadReplicaSourceDBInstanceIdentifier")}


def require_no_rds_replicas(row):
    observed = rds_replica_relationships(row)
    if observed["read_replica_instances"] or observed["read_replica_clusters"] or observed["read_replica_source"]:
        raise DispositionRefused("live RDS replicas require expanded custody and disposition inventory")
    return observed


def automated_backup_rows(client, resource_id):
    """Read this physical instance's backups, including AWS's modeled absence.

    AWS may report an empty population as DBInstanceAutomatedBackupNotFound
    instead of returning an empty list. No other provider error proves absence.
    A late not-found response cannot erase rows already observed on an earlier
    page; that inconsistent pagination must be reconciled on another pass.
    """
    backups = []
    try:
        for page in client.get_paginator("describe_db_instance_automated_backups").paginate(DbiResourceId=resource_id):
            backups.extend(page.get("DBInstanceAutomatedBackups", []))
    except client.exceptions.DBInstanceAutomatedBackupNotFoundFault as exc:
        if backups:
            raise DispositionRefused("RDS backup pagination lost an already observed population") from exc
        return []
    return backups
