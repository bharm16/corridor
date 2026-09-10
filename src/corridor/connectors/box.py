"""Exact observed snapshots of public Box shares (ADR-0078, ADR-0083, #496).

The public shared-file page gives an item identity and a current download URL,
not a native Box version history. Treating that item ID as both version and
cursor skipped replacements after restart and returned current bytes for any
requested version. Listing now captures bytes and names their digest as an
observed version; fetch_version returns only that captured version. Every poll
must read the configured shares to detect same-item byte changes. Intermediate
versions that were never observed are not recoverable through this adapter.

The opaque cursor identifies the complete observed snapshot and source scope.
A changed snapshot re-lists its members; the delivery ledger deduplicates known
versions. Legacy item-ID cursors cannot prove a byte version and replay once.
Checkpoint verifies this pass's fetched set; the polling runtime, not this
adapter's memory, owns durable checkpoint storage (ADR-0089).
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Callable, Sequence

import httpx

from corridor.connectors.pull_connector import ChangeItem
from corridor.location_discovery import BoxSharedFile, parse_box_shared_file


_CURSOR_PREFIX = "box-snapshot-v1:"


class BoxPullConnector:
    """The existing PullConnector seam over exact public-share observations."""

    def __init__(
        self,
        *,
        shared_urls: Sequence[str] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        initial_cursor: str | None = None,
    ) -> None:
        self.shared_urls = tuple(shared_urls or ())
        self._fetcher = fetcher or self._default_http_fetch
        self._registered_files: dict[str, BoxSharedFile] = {}
        self._files_by_id: dict[str, BoxSharedFile] = {}
        self._listed: dict[tuple[str, str], bytes] = {}
        self._fetched: set[tuple[str, str]] = set()
        self._pending_token: str | None = None
        self._checkpoints: list[str] = [initial_cursor] if initial_cursor else []

    @staticmethod
    def _default_http_fetch(url: str) -> bytes:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        return response.content

    def register_shared_file(self, shared_file: BoxSharedFile) -> None:
        self._registered_files[str(shared_file.item_id)] = shared_file

    def list_changes(self, cursor: str | None = None) -> tuple[Sequence[ChangeItem], str]:
        """Capture one snapshot; failed observation cannot authorize a checkpoint."""
        self._pending_token = None
        self._listed = {}
        self._fetched = set()
        self._files_by_id = {}
        scope = sha256(json.dumps(
            [sorted(set(self.shared_urls)),
             sorted({file.shared_name for file in self._registered_files.values()})],
            separators=(",", ":"),
        ).encode()).hexdigest()
        prefix = _CURSOR_PREFIX + scope + ":"
        if cursor is not None and not cursor.isdecimal():
            digest = cursor.removeprefix(prefix)
            if not cursor.startswith(prefix) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Box cursor is not a snapshot of this configured scope")

        files = dict(self._registered_files)
        for url in sorted(set(self.shared_urls)):
            shared = parse_box_shared_file(self._fetcher(url), shared_url=url)
            item_id = str(shared.item_id)
            if item_id in files and files[item_id] != shared:
                raise ValueError("Box item has conflicting shared-file metadata")
            files[item_id] = shared

        listed: dict[tuple[str, str], bytes] = {}
        items = []
        for item_id, shared in sorted(files.items()):
            body = self._fetcher(shared.archive_url)
            if not isinstance(body, bytes) or not body:
                raise ValueError("Box observed version must contain bytes")
            version = "sha256:" + sha256(body).hexdigest()
            listed[item_id, version] = body
            items.append(ChangeItem(
                item_id=item_id, version_id=version, name=shared.filename,
                metadata={
                    "shared_name": shared.shared_name,
                    "archive_url": shared.archive_url,
                    "version_kind": "observed_content_sha256",
                },
            ))
        token = prefix + sha256(json.dumps(
            sorted(listed), separators=(",", ":"),
        ).encode()).hexdigest()
        self._files_by_id = files
        if token == cursor:
            items = []
        else:
            self._listed = listed
        self._pending_token = token
        return tuple(items), token

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        """Return exactly the listed bytes, never re-fetch a mutable download."""
        key = item_id, version_id
        if key not in self._listed:
            raise ValueError("Box version was not listed in this snapshot")
        self._fetched.add(key)
        return self._listed[key]

    def get_metadata(self, item_id: str) -> dict[str, Any]:
        shared = self._files_by_id.get(item_id)
        if shared is None:
            raise KeyError(f"unknown Box item_id: {item_id!r}")
        return {
            "item_id": shared.item_id,
            "filename": shared.filename,
            "shared_name": shared.shared_name,
            "archive_url": shared.archive_url,
            "version_kind": "observed_content_sha256",
        }

    def checkpoint(self, token: str) -> None:
        if token != self._pending_token or self._fetched != set(self._listed):
            raise ValueError("Box checkpoint does not cover the fetched snapshot")
        self._checkpoints.append(token)

    @property
    def current_checkpoint(self) -> str | None:
        return self._checkpoints[-1] if self._checkpoints else None
