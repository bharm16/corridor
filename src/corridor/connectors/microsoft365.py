"""Graph library/shared-mailbox adapter through the four-method pull contract.

#497's offline build uses recorded Graph pages and exact version bytes. It
does not acquire credentials or connect a tenant. Listing retains native
version identities and opaque delta links; the existing sync/ledger runtime
owns durable storage and checkpoint advancement. Content never selects a
customer/project. Missing versions, cursor drift, and unsupported deletion
events refuse the pass instead of silently advancing past missing evidence.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlsplit

from corridor.connectors.pull_connector import ChangeItem


@dataclass(frozen=True)
class GraphLocation:
    tenant_id: str
    kind: str
    resource_id: str
    folder_id: str = ""
    mailbox_type: str = ""

    def __post_init__(self):
        if not self.tenant_id or not self.resource_id or self.kind not in {"library", "mailbox"}:
            raise ValueError("Graph location requires tenant and library/shared mailbox identity")
        if self.kind == "mailbox" and (self.mailbox_type != "shared" or not self.folder_id):
            raise ValueError("only an explicitly configured shared mailbox folder is supported")

    @property
    def channel(self) -> str:
        return "m365-library-v1" if self.kind == "library" else "m365-shared-mailbox-v1"

    @property
    def delta_url(self) -> str:
        resource = quote(self.resource_id, safe="")
        path = (f"drives/{resource}/root/delta" if self.kind == "library" else
                f"users/{resource}/mailFolders/{quote(self.folder_id, safe='')}/messages/delta")
        return "https://graph.microsoft.com/v1.0/" + path

    def external_identity(self, item_id: str) -> str:
        return ":".join(quote(value, safe="") for value in
                        ("graph", self.tenant_id, self.kind, self.resource_id, item_id))


class GraphTransport(Protocol):
    """A provider must return the requested version or refuse; never latest."""

    def page(self, url: str) -> Mapping[str, Any]: ...
    def version(self, item_id: str, version_id: str) -> bytes: ...


class RecordedGraphTransport:
    """An offline immutable response set, with no network fallback."""

    def __init__(self, *, pages: Mapping[str, dict], versions: Mapping[tuple[str, str], bytes]):
        self._pages = deepcopy(dict(pages))
        self._versions = {key: bytes(value) for key, value in versions.items()}

    def page(self, url: str) -> Mapping[str, Any]:
        if url not in self._pages:
            raise ValueError("recorded Graph page unavailable; checkpoint unchanged")
        return deepcopy(self._pages[url])

    def version(self, item_id: str, version_id: str) -> bytes:
        try:
            return self._versions[item_id, version_id]
        except KeyError:
            raise ValueError("recorded exact version unavailable; checkpoint unchanged") from None


class Microsoft365PullConnector:
    """One trusted location, bounded pagination, native versions, exact bytes."""

    def __init__(self, location: GraphLocation, transport: GraphTransport):
        self.location = location
        self.transport = transport
        self.current_checkpoint: str | None = None
        self._listed: dict[tuple[str, str], tuple[str, ChangeItem]] = {}
        self._fetched: set[tuple[str, str]] = set()
        self._pending_token: str | None = None

    def _check_url(self, url: str) -> None:
        actual, expected = urlsplit(url), urlsplit(self.location.delta_url)
        if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path) or actual.fragment:
            raise ValueError("Graph cursor is outside the configured location")

    def list_changes(self, cursor: str | None = None) -> tuple[tuple[ChangeItem, ...], str]:
        self._listed = {}
        self._fetched = set()
        self._pending_token = None
        url = cursor or self.location.delta_url
        seen: set[str] = set()
        # Graph can repeat an item within a delta round; its last occurrence
        # is the service's final state for that round, not lexical ID order.
        latest: dict[str, Mapping[str, Any]] = {}
        for _ in range(100):
            self._check_url(url)
            if url in seen:
                raise ValueError("Graph pagination cycle; checkpoint unchanged")
            seen.add(url)
            page = self.transport.page(url)
            if "error" in page or not isinstance(page.get("value"), list):
                raise ValueError("Graph delta response failed; checkpoint unchanged")
            for row in page["value"]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ValueError("Graph change lacks its native identity")
                latest[row["id"]] = row
                if len(latest) > 10000:
                    raise ValueError("Graph change budget exceeded; checkpoint unchanged")
            next_url, final_url = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
            if bool(next_url) == bool(final_url):
                raise ValueError("Graph page needs exactly one continuation token")
            url = next_url or final_url
            if not isinstance(url, str):
                raise ValueError("Graph continuation token must be a URL")
            self._check_url(url)
            if final_url:
                break
        else:
            raise ValueError("Graph page budget exceeded; checkpoint unchanged")
        for native_id, row in latest.items():
            if "deleted" in row or "@removed" in row:
                raise ValueError("Graph deletion needs explicit retained handling; checkpoint unchanged")
            if self.location.kind == "library":
                if "folder" in row:
                    continue
                if "file" not in row or "remoteItem" in row:
                    raise ValueError("Graph item is not a file in the configured library")
                if row.get("parentReference", {}).get("driveId", self.location.resource_id) != self.location.resource_id:
                    raise ValueError("Graph item is outside the configured location")
                version, name = row.get("eTag"), row.get("name")
            else:
                if row.get("isDraft"):
                    raise ValueError("Graph draft is not inbound shared-mailbox evidence")
                version = row.get("changeKey")
                name = sha256(native_id.encode()).hexdigest() + ".eml"
            if not isinstance(version, str) or not version or not isinstance(name, str) or not name:
                raise ValueError("Graph change lacks an exact version or filename")
            identity = self.location.external_identity(native_id)
            item = ChangeItem(identity, version, name,
                {key: row[key] for key in ("createdDateTime", "lastModifiedDateTime", "receivedDateTime", "sentDateTime") if key in row},
                {"provider": "microsoft-graph", "tenant_id": self.location.tenant_id,
                 "resource_id": self.location.resource_id, "native_id": native_id,
                 "filename": name, "graph_metadata": deepcopy({key: value for key, value in row.items()
                     if key != "@microsoft.graph.downloadUrl"}), "source_class": self.location.kind})
            self._listed[identity, version] = (native_id, item)
        self._pending_token = url
        return tuple(item for _, item in self._listed.values()), url

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        key = (item_id, version_id)
        if key not in self._listed:
            raise ValueError("version was not listed in this configured location")
        body = self.transport.version(self._listed[key][0], version_id)
        if not isinstance(body, bytes) or not body:
            raise ValueError("Graph exact version must contain bytes")
        self._fetched.add(key)
        return body

    def get_metadata(self, item_id: str) -> dict[str, Any]:
        items = [item for _, item in self._listed.values() if item.item_id == item_id]
        if len(items) != 1:
            raise ValueError("Graph item was not listed uniquely")
        return deepcopy(items[0].metadata)

    def checkpoint(self, token: str) -> None:
        # Durable acceptance is the sync/ledger runtime's responsibility;
        # this additionally refuses tokens this adapter never fully fetched.
        if token != self._pending_token or self._fetched != set(self._listed):
            raise ValueError("Graph checkpoint does not cover the fetched page set")
        self.current_checkpoint = token
