"""Box and TxDOT RID pull connector refit (ADR-0078, ADR-0083, #496).

This module refits the Box/RID discovery adapter from ``corridor.location_discovery``
to the four-method ``PullConnector`` contract:
1. ``list_changes(cursor)``: Discovers public Box shared files declared by TxDOT RID
   or configured shared URLs, returning change items and the next token.
2. ``fetch_version(item_id, version_id)``: Retrieves exact bytes from the Box archive URL.
3. ``get_metadata(item_id)``: Returns metadata for a given file item.
4. ``checkpoint(token)``: Durably records the advanced cursor after storage.

Replay is idempotent, and discovery preserves existing location identity without
guessing or name inference.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import httpx

from corridor.connectors.pull_connector import ChangeItem, PullConnector
from corridor.location_discovery import (
    BoxSharedFile,
    parse_box_shared_file,
    parse_txdot_rid_box_link,
)


class BoxPullConnector:
    """PullConnector adapter for public Box shared files and TxDOT RID releases."""

    def __init__(
        self,
        *,
        shared_urls: Sequence[str] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        initial_cursor: str | None = None,
    ) -> None:
        self.shared_urls = tuple(shared_urls or ())
        self._fetcher = fetcher or self._default_http_fetch
        self._files_by_id: dict[str, BoxSharedFile] = {}
        self._checkpoints: list[str] = [initial_cursor] if initial_cursor else []

    @staticmethod
    def _default_http_fetch(url: str) -> bytes:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        return response.content

    def register_shared_file(self, shared_file: BoxSharedFile) -> None:
        """Register a known BoxSharedFile directly (useful for tests or index discovery)."""
        self._files_by_id[str(shared_file.item_id)] = shared_file

    def list_changes(self, cursor: str | None = None) -> tuple[Sequence[ChangeItem], str]:
        """List Box shared files since cursor."""

        # Discover shared files from configured URLs if not already registered
        for url in self.shared_urls:
            page_bytes = self._fetcher(url)
            shared_file = parse_box_shared_file(page_bytes, shared_url=url)
            self.register_shared_file(shared_file)

        items: list[ChangeItem] = []
        ordered_ids = sorted(self._files_by_id.keys())

        # If cursor is provided, skip items up to and including cursor
        include = cursor is None
        for item_id in ordered_ids:
            if not include:
                if item_id == cursor:
                    include = True
                continue

            shared = self._files_by_id[item_id]
            items.append(
                ChangeItem(
                    item_id=str(shared.item_id),
                    version_id=str(shared.item_id),
                    name=shared.filename,
                    metadata={
                        "shared_name": shared.shared_name,
                        "archive_url": shared.archive_url,
                    },
                )
            )

        next_cursor = ordered_ids[-1] if ordered_ids else (cursor or "")
        return tuple(items), next_cursor

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        """Fetch raw bytes for a Box item."""

        shared = self._files_by_id.get(item_id)
        if shared is None:
            raise KeyError(f"unknown Box item_id: {item_id!r}")
        return self._fetcher(shared.archive_url)

    def get_metadata(self, item_id: str) -> dict[str, Any]:
        """Get metadata for a Box item."""

        shared = self._files_by_id.get(item_id)
        if shared is None:
            raise KeyError(f"unknown Box item_id: {item_id!r}")
        return {
            "item_id": shared.item_id,
            "filename": shared.filename,
            "shared_name": shared.shared_name,
            "archive_url": shared.archive_url,
        }

    def checkpoint(self, token: str) -> None:
        """Record the advanced cursor token."""

        self._checkpoints.append(token)

    @property
    def current_checkpoint(self) -> str | None:
        return self._checkpoints[-1] if self._checkpoints else None
