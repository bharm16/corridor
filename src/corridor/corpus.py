"""Manifest-driven corpus fetching.

Curated intent lives in `corpus/manifest.yaml` — hand-edited, checked in.
Fetch outcome lives in `corpus/manifest.lock.json` — generated. The split is
the one a lockfile makes against a dependency list, and it matters here
because curation is the human part: judging "is this the utility special
provisions" is hard, fetching is not.

Two behaviors are load-bearing rather than incidental:

- **Drift is recorded, never overwritten.** A URL returning different bytes
  keeps both revisions. Public documents get revised by addenda, so this
  hands M8's document-supersession work its evidence for free.
- **A failure never masquerades as a retrieval.** A non-200 records the
  status and leaves `sha256` null rather than storing an error page as
  though it were the document.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from corridor.models import DOC_TYPES

ROLES = ("spine", "stream", "schedule", "evidence")

USER_AGENT = (
    "corridor-corpus/0.1 (research prototype; "
    "+https://github.com/bharm16/corridor)"
)

# Box, and some CDNs behind it, return 404 to anything that does not look
# like a browser. Used only as a retry, so hosts that behave normally still
# see an honest, identifying agent.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


@dataclass(frozen=True)
class Source:
    url: str
    doc_type: str
    role: str
    title: str
    doc_date: date | None = None
    notes: str | None = None
    # When set, `url` names an archive and this names the document inside it.
    # Project A's matrices are reachable no other way. A `::` separator
    # reaches one level deeper — SH 99's meeting notes are a zip inside the
    # zip: "Utility Owner Coordination/Notes.zip::Meeting Notes/x.pdf".
    member: str | None = None


@dataclass(frozen=True)
class Manifest:
    project: str
    agency: str | None
    sources: tuple[Source, ...]
    # Human-readable project name, used by ingest to create the Project row.
    name: str | None = None


@dataclass
class Summary:
    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def load_manifest(path: Path | str) -> Manifest:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    sources = []
    for i, entry in enumerate(raw.get("sources") or []):
        url = entry.get("url")
        doc_type = entry.get("doc_type")
        role = entry.get("role")
        if doc_type not in DOC_TYPES:
            raise ValueError(
                f"source {i} ({url}): unknown doc_type {doc_type!r}; "
                f"expected one of {', '.join(DOC_TYPES)}"
            )
        if role not in ROLES:
            raise ValueError(
                f"source {i} ({url}): unknown role {role!r}; "
                f"expected one of {', '.join(ROLES)}"
            )
        doc_date = entry.get("doc_date")
        sources.append(
            Source(
                url=url,
                doc_type=doc_type,
                role=role,
                title=entry.get("title", ""),
                doc_date=doc_date if isinstance(doc_date, date) else None,
                notes=entry.get("notes"),
                member=entry.get("member"),
            )
        )
    return Manifest(
        project=raw["project"],
        agency=raw.get("agency"),
        sources=tuple(sources),
        name=raw.get("name"),
    )


def fetch_all(
    manifest: Manifest,
    *,
    store: Path | str,
    lock_path: Path | str,
    client: httpx.Client | None = None,
    delay: float = 1.0,
) -> Summary:
    store, lock_path = Path(store), Path(lock_path)
    lock = _read_lock(lock_path, manifest)
    summary = Summary()
    # One open archive per URL per run. A manifest naming seven members of
    # the same 208 MB zip should read its directory once, not seven times.
    archives: dict[str, tuple] = {}

    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        for i, source in enumerate(manifest.sources):
            if i and delay:
                time.sleep(delay)
            _fetch_one(source, store, lock, summary, client, archives)
    finally:
        if owns_client:
            client.close()

    _write_lock(lock_path, lock)
    return summary


def _fetch_one(
    source: Source,
    store: Path,
    lock: dict,
    summary: Summary,
    client: httpx.Client,
    archives: dict,
) -> None:
    if source.member:
        _fetch_member(source, store, lock, summary, client, archives)
    else:
        _fetch_document(source, store, lock, summary, client)


def _fetch_document(source, store, lock, summary, client) -> None:
    key = source.url
    prior = lock["sources"].get(key)

    try:
        response = _get(client, source.url)
    except httpx.HTTPError as exc:
        lock["sources"][key] = _failure(prior, status=None, error=str(exc))
        summary.failed.append(key)
        return

    if response.status_code != 200:
        lock["sources"][key] = _failure(prior, status=response.status_code)
        summary.failed.append(key)
        return

    reason = _reject_reason(response.headers, len(response.content))
    if reason:
        lock["sources"][key] = _failure(prior, status=200, error=reason)
        summary.failed.append(key)
        return

    _store_and_record(
        source,
        key,
        prior,
        response.content,
        store,
        lock,
        summary,
        status=200,
        content_type=response.headers.get("content-type"),
        name=source.url,
    )


def _fetch_member(source, store, lock, summary, client, archives) -> None:
    key = f"{source.url}::{source.member}"
    prior = lock["sources"].get(key)

    if "::" in source.member:
        _fetch_nested_member(source, key, prior, store, lock, summary, client, archives)
        return

    if source.url in archives:
        archive, probe = archives[source.url]
    else:
        try:
            handle, probe = _open_archive(client, source.url)
        except httpx.HTTPError as exc:
            lock["sources"][key] = _failure(prior, status=None, error=str(exc))
            summary.failed.append(key)
            return

        if handle is None:
            lock["sources"][key] = _failure(prior, status=probe.status_code)
            summary.failed.append(key)
            return

        reason = _reject_reason(probe.headers, None)
        if reason:
            lock["sources"][key] = _failure(
                prior, status=probe.status_code, error=reason
            )
            summary.failed.append(key)
            return

        try:
            archive = zipfile.ZipFile(handle)
        except zipfile.BadZipFile as exc:
            lock["sources"][key] = _failure(
                prior, status=200, error=f"not a zip archive: {exc}"
            )
            summary.failed.append(key)
            return
        archives[source.url] = (archive, probe)

    try:
        info = archive.getinfo(source.member)
    except KeyError:
        lock["sources"][key] = _failure(
            prior, status=200, error=f"member {source.member!r} not found in archive"
        )
        summary.failed.append(key)
        return

    # The zip central directory carries a CRC32 per member, so an unchanged
    # member is detectable from a few hundred bytes — no extraction, and
    # certainly no downloading the archive.
    if prior and prior.get("member_crc32") == info.CRC:
        summary.skipped.append(key)
        return

    _store_and_record(
        source,
        key,
        prior,
        archive.read(source.member),
        store,
        lock,
        summary,
        status=200,
        content_type=probe.headers.get("content-type"),
        name=source.member,
        extra={
            "member": source.member,
            "member_crc32": info.CRC,
            "archive_url": source.url,
        },
    )


def _fetch_nested_member(
    source, key, prior, store, lock, summary, client, archives
) -> None:
    """A member of an archive that is itself a member of an archive.

    Idempotency diverges from top-level members on purpose. A top-level
    member re-checks its CRC against the outer directory, which costs a few
    hundred KB of ranges; the same check here would cost the entire inner
    zip (88 MB for SH 99's meeting notes). So a lock record whose file is
    still on disk is trusted. The outer zips are dated snapshots — TxDOT
    revises by publishing new ones, not by mutating old ones — and a forced
    refetch is `rm` on the lock entry.
    """
    if prior and prior.get("sha256") and prior.get("local_path"):
        if Path(prior["local_path"]).exists():
            summary.skipped.append(key)
            return

    inner_path, _, leaf = source.member.partition("::")
    cache_key = (source.url, inner_path)

    if cache_key in archives:
        inner_zip = archives[cache_key]
    else:
        outer = _outer_archive(source, key, prior, lock, summary, client, archives)
        if outer is None:
            return
        try:
            inner_bytes = outer.read(inner_path)
        except KeyError:
            lock["sources"][key] = _failure(
                prior, status=200, error=f"inner archive {inner_path!r} not found"
            )
            summary.failed.append(key)
            return
        try:
            inner_zip = zipfile.ZipFile(io.BytesIO(inner_bytes))
        except zipfile.BadZipFile as exc:
            lock["sources"][key] = _failure(
                prior, status=200, error=f"{inner_path!r} is not a zip: {exc}"
            )
            summary.failed.append(key)
            return
        archives[cache_key] = inner_zip

    try:
        info = inner_zip.getinfo(leaf)
    except KeyError:
        lock["sources"][key] = _failure(
            prior, status=200, error=f"member {leaf!r} not found in {inner_path!r}"
        )
        summary.failed.append(key)
        return

    _store_and_record(
        source,
        key,
        prior,
        inner_zip.read(leaf),
        store,
        lock,
        summary,
        status=200,
        content_type=None,
        name=leaf,
        extra={
            "member": source.member,
            "member_crc32": info.CRC,
            "archive_url": source.url,
        },
    )


def _outer_archive(source, key, prior, lock, summary, client, archives):
    """The opened outer zip, shared across every member that names it."""
    if source.url in archives:
        return archives[source.url][0]
    try:
        handle, probe = _open_archive(client, source.url)
    except httpx.HTTPError as exc:
        lock["sources"][key] = _failure(prior, status=None, error=str(exc))
        summary.failed.append(key)
        return None
    if handle is None:
        lock["sources"][key] = _failure(prior, status=probe.status_code)
        summary.failed.append(key)
        return None
    try:
        archive = zipfile.ZipFile(handle)
    except zipfile.BadZipFile as exc:
        lock["sources"][key] = _failure(
            prior, status=200, error=f"not a zip archive: {exc}"
        )
        summary.failed.append(key)
        return None
    archives[source.url] = (archive, probe)
    return archive


def _store_and_record(
    source,
    key,
    prior,
    body,
    store,
    lock,
    summary,
    *,
    status,
    content_type,
    name,
    extra=None,
) -> None:
    sha = hashlib.sha256(body).hexdigest()

    if prior and prior.get("sha256") == sha:
        summary.skipped.append(key)
        return

    path = _store_path(store, sha, name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    history = list(prior.get("history", [])) if prior else []
    drifted = bool(prior and prior.get("sha256") and prior["sha256"] != sha)
    if drifted:
        history.append(
            {
                "sha256": prior["sha256"],
                "retrieved_at": prior["retrieved_at"],
                "local_path": prior["local_path"],
            }
        )

    record = {
        "sha256": sha,
        "retrieved_at": _now(),
        "local_path": str(path),
        "http_status": status,
        "content_type": content_type,
        "bytes": len(body),
        "doc_type": source.doc_type,
        "role": source.role,
        "title": source.title,
        "doc_date": source.doc_date.isoformat() if source.doc_date else None,
        "history": history,
    }
    record.update(extra or {})
    lock["sources"][key] = record
    (summary.drifted if drifted else summary.fetched).append(key)


def _get(client: httpx.Client, url: str) -> httpx.Response:
    """Polite agent first; browser agent only as a retry.

    Box 404s anything that does not look like a browser, and the 404 body is
    well-formed HTML rather than an error — so hosts that behave normally
    still see an honest, identifying agent, and the one that does not still
    resolves.
    """
    response = client.get(url, headers={"user-agent": USER_AGENT})
    if response.status_code != 200:
        response = client.get(url, headers={"user-agent": BROWSER_UA})
    return response


def _reject_reason(headers, size: int | None) -> str | None:
    """A 200 is not proof that a document was served."""
    content_type = (headers.get("content-type") or "").lower()
    if content_type.startswith("text/html"):
        return "server returned HTML, not a document"

    # Wayback serves partial captures with a 200; the true size is in a
    # header, and the body is a valid-looking prefix of a real file.
    declared = headers.get("x-archive-orig-x-crawler-content-length")
    if size is not None and declared and declared.isdigit():
        if int(declared) > size:
            return f"truncated capture: {size} of {declared} bytes"
    return None


def _open_archive(client: httpx.Client, url: str):
    probe = None
    for user_agent in (USER_AGENT, BROWSER_UA):
        probe = client.get(
            url, headers={"user-agent": user_agent, "Range": "bytes=0-0"}
        )
        if probe.status_code in (200, 206):
            return _HttpRangeFile(client, url, user_agent, probe), probe
    return None, probe


class _HttpRangeFile:
    """A seekable file backed by HTTP range requests.

    `zipfile.ZipFile` accepts any seekable object, so this reads a 208 MB
    archive's central directory and extracts a single member without
    downloading the rest. HEAD is deliberately never used: Box 404s it.
    """

    def __init__(self, client, url, user_agent, probe):
        self.client = client
        self.url = url
        self.user_agent = user_agent
        self.pos = 0
        content_range = probe.headers.get("content-range")
        if content_range:
            self.size = int(content_range.rsplit("/", 1)[-1])
        else:
            self.size = int(probe.headers.get("content-length") or 0)

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        if n <= 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        response = self.client.get(
            self.url,
            headers={
                "user-agent": self.user_agent,
                "Range": f"bytes={self.pos}-{end}",
            },
        )
        response.raise_for_status()
        self.pos += len(response.content)
        return response.content


def _failure(prior: dict | None, *, status: int | None, error: str | None = None) -> dict:
    """Record the attempt without discarding a prior successful retrieval."""
    record = dict(prior) if prior else {
        "sha256": None,
        "retrieved_at": None,
        "local_path": None,
        "content_type": None,
        "bytes": None,
        "history": [],
    }
    record["http_status"] = status
    record["last_error"] = error
    record["last_attempt_at"] = _now()
    return record


def _store_path(store: Path, sha: str, name: str) -> Path:
    # Sharded by the first two hex chars, extension preserved so the store
    # stays browsable by a human looking for a PDF. `name` is a URL for a
    # plain document and a member path for an archive member; urlparse
    # handles both.
    suffix = Path(urlparse(name).path).suffix
    return store / sha[:2] / f"{sha}{suffix}"


def _read_lock(path: Path, manifest: Manifest) -> dict:
    if path.exists():
        lock = json.loads(path.read_text())
        lock.setdefault("sources", {})
    else:
        lock = {"sources": {}}
    # Header travels with every write so ingest can create the Project row.
    lock["project"] = manifest.project
    lock["name"] = manifest.name
    lock["agency"] = manifest.agency
    return lock


def _write_lock(path: Path, lock: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    """Fetch every manifest in corpus/. One project per manifest file;
    each writes its own <stem>.lock.json."""
    manifests = sorted(Path("corpus").glob("*.yaml"))
    if not manifests:
        print("no manifests in corpus/", file=sys.stderr)
        return 1

    failed = 0
    for manifest_path in manifests:
        manifest = load_manifest(manifest_path)
        summary = fetch_all(
            manifest,
            store=Path("corpus/files"),
            lock_path=manifest_path.with_suffix(".lock.json"),
        )
        print(
            f"{manifest.project}: fetched {len(summary.fetched)}  "
            f"skipped {len(summary.skipped)}  drifted {len(summary.drifted)}  "
            f"failed {len(summary.failed)}",
            flush=True,
        )
        for url in summary.drifted:
            print(f"  drift: {url} (previous revision retained)")
        for url in summary.failed:
            print(f"  FAILED: {url}")
        failed += len(summary.failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
