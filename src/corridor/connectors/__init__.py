"""Connector abstractions and adapters for external sources (#496).

This package implements the three intake layers defined by ADR-0078 and ADR-0083:
- ``SourceEnvelope``: The one normalized ingress record produced by all intake channels.
- ``PullConnector``: The four-method pull interface (list_changes, fetch_version,
  get_metadata, checkpoint) with crash-safe checkpoint semantics.
- Adapters: ``BoxPullConnector`` refitting Box and TxDOT RID shared file discovery.
"""

from corridor.connectors.box import BoxPullConnector
from corridor.connectors.pull_connector import (
    ChangeItem,
    PullConnector,
    SourceEnvelope,
    build_delivery_identity,
    build_idempotency_key,
    sync_pull_connector,
)

__all__ = [
    "BoxPullConnector",
    "ChangeItem",
    "PullConnector",
    "SourceEnvelope",
    "build_delivery_identity",
    "build_idempotency_key",
    "sync_pull_connector",
]
