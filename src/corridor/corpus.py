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

import argparse
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

from corridor.models import DOC_TYPES, NUMBERING_SCHEMES
from corridor.supersession import SupersessionDeclaration

ROLES = ("spine", "stream", "schedule", "evidence")
CURATION_STATUSES = ("confirmed", "proposed")

USER_AGENT = (
    "corridor-corpus/0.1 (research prototype; +https://github.com/bharm16/corridor)"
)

# Box, and some CDNs behind it, return 404 to anything that does not look
# like a browser. Used only as a retry, so hosts that behave normally still
# see an honest, identifying agent.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


@dataclass(frozen=True)
class XlsConversion:
    """One curated derived XLSX registration for a retained binary XLS."""

    registry_id: str
    title: str
    to: str = "xlsx"


@dataclass(frozen=True)
class Source:
    url: str
    doc_type: str
    role: str
    title: str
    curation_status: str = "confirmed"
    doc_date: date | None = None
    notes: str | None = None
    # When set, `url` names an archive and this names the document inside it.
    # Project A's matrices are reachable no other way. A `::` separator
    # reaches one level deeper — SH 99's meeting notes are a zip inside the
    # zip: "Utility Owner Coordination/Notes.zip::Meeting Notes/x.pdf".
    member: str | None = None
    # Stable human-curated identity used by declared registry relations.
    # Optional for documents that do not participate in one.
    registry_id: str | None = None
    supersession: SupersessionDeclaration | None = None
    # How a matrix names its rows (ADR-0030). None means the manifest is
    # silent and the document keeps its default, project-unique.
    numbering_scheme: str | None = None
    conversion: XlsConversion | None = None


@dataclass(frozen=True)
class Manifest:
    project: str
    agency: str | None
    sources: tuple[Source, ...]
    # Human-readable project name, used by ingest to create the Project row.
    name: str | None = None
    # Layout evidence belongs in the corpus without being materialized into a
    # tracked Project on every bulk ingest.
    ingest_by_default: bool = True
    # An eval holdout. `make corpus` skips it, because fetching is one
    # command away from reading and reading it once spends the corpus for
    # good (#3, corpus-acquisition-spec.md §7.1). A comment in the YAML
    # cannot enforce that — this can.
    sealed: bool = False


@dataclass
class Summary:
    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _load_bool_field(raw: dict, *, path: Path, field: str, default: bool) -> bool:
    if field not in raw:
        return default
    value = raw[field]
    if isinstance(value, bool):
        return value
    raise ValueError(f"{path}: {field} must be a YAML boolean")


def load_manifest(path: Path | str) -> Manifest:
    manifest_path = Path(path)
    raw = yaml.safe_load(manifest_path.read_text()) or {}
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
        registry_id = entry.get("registry_id")
        numbering_scheme = entry.get("numbering_scheme")
        if numbering_scheme is not None and numbering_scheme not in NUMBERING_SCHEMES:
            raise ValueError(
                f"source {i} ({url}): unknown numbering_scheme "
                f"{numbering_scheme!r}; expected one of "
                f"{', '.join(NUMBERING_SCHEMES)}"
            )
        if registry_id is not None and (
            not isinstance(registry_id, str) or not registry_id.strip()
        ):
            raise ValueError(f"source {i} ({url}): registry_id must be non-empty")
        supersession = _load_supersession(
            entry.get("supersession"),
            registry_id=registry_id,
            index=i,
            url=url,
        )
        sources.append(
            Source(
                url=url,
                doc_type=doc_type,
                role=role,
                title=entry.get("title", ""),
                curation_status=_load_curation_status(entry, index=i, url=url),
                doc_date=doc_date if isinstance(doc_date, date) else None,
                notes=entry.get("notes"),
                member=entry.get("member"),
                registry_id=registry_id,
                supersession=supersession,
                numbering_scheme=numbering_scheme,
                conversion=_load_conversion(
                    entry.get("conversion"),
                    source_registry_id=registry_id,
                    member=entry.get("member"),
                    url=url,
                    index=i,
                ),
            )
        )
    _validate_manifest_registry(tuple(sources), path=manifest_path)
    return Manifest(
        project=raw["project"],
        agency=raw.get("agency"),
        sources=tuple(sources),
        name=raw.get("name"),
        ingest_by_default=_load_bool_field(
            raw, path=manifest_path, field="ingest_by_default", default=True
        ),
        sealed=_load_bool_field(raw, path=manifest_path, field="sealed", default=False),
    )


def _load_curation_status(entry: dict, *, index: int, url: str | None) -> str:
    value = entry.get("curation_status", "confirmed")
    if value not in CURATION_STATUSES:
        raise ValueError(
            f"source {index} ({url}): unknown curation_status {value!r}; "
            f"expected one of {', '.join(CURATION_STATUSES)}"
        )
    return value


def _load_supersession(
    raw,
    *,
    registry_id: str | None,
    index: int,
    url: str | None,
) -> SupersessionDeclaration | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"source {index} ({url}): supersession must be an object")
    if not registry_id:
        raise ValueError(f"source {index} ({url}): supersession requires registry_id")
    source = raw.get("source")
    if not isinstance(source, dict):
        raise ValueError(
            f"source {index} ({url}): supersession source must be an object"
        )
    replacement_date = raw.get("replacement_date")
    if not isinstance(replacement_date, date):
        raise ValueError(
            f"source {index} ({url}): replacement_date must be a YAML date"
        )
    source_page = source.get("page")
    if (
        isinstance(source_page, bool)
        or not isinstance(source_page, int)
        or source_page <= 0
    ):
        raise ValueError(
            f"source {index} ({url}): supersession source page must be positive"
        )
    return SupersessionDeclaration(
        predecessor_registry_id=registry_id,
        successor_registry_id=raw.get("successor"),
        replacement_date=replacement_date,
        source_registry_id=source.get("document"),
        source_page=source_page,
    )


def _load_conversion(
    raw,
    *,
    source_registry_id: str | None,
    member: str | None,
    url: str | None,
    index: int,
) -> XlsConversion | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"to", "registry_id", "title"}:
        raise ValueError(
            f"source {index} ({url}): conversion requires to, registry_id, title"
        )
    source_name = member or url or ""
    registry_id = raw.get("registry_id")
    title = raw.get("title")
    if Path(source_name).suffix.lower() != ".xls" or raw.get("to") != "xlsx":
        raise ValueError(
            f"source {index} ({url}): only explicit .xls to xlsx conversion is supported"
        )
    if not source_registry_id:
        raise ValueError(f"source {index} ({url}): conversion requires registry_id")
    if (
        not isinstance(registry_id, str)
        or not registry_id.strip()
        or registry_id == source_registry_id
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise ValueError(
            f"source {index} ({url}): converted rendition identity is invalid"
        )
    return XlsConversion(registry_id=registry_id, title=title)


def _validate_manifest_registry(sources: tuple[Source, ...], *, path: Path) -> None:
    identifiers = [source.registry_id for source in sources if source.registry_id]
    identifiers.extend(
        source.conversion.registry_id
        for source in sources
        if source.conversion is not None
    )
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{path}: registry_id values must be unique")
    known = set(identifiers)
    graph: dict[str, str] = {}
    for source in sources:
        declaration = source.supersession
        if declaration is None:
            continue
        for label, value in (
            ("successor", declaration.successor_registry_id),
            ("source document", declaration.source_registry_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}: supersession {label} must be non-empty")
            if value not in known:
                raise ValueError(
                    f"{path}: supersession {label} {value!r} is not registered"
                )
        if declaration.predecessor_registry_id == declaration.successor_registry_id:
            raise ValueError(f"{path}: a document cannot supersede itself")
        if declaration.source_registry_id == declaration.predecessor_registry_id:
            raise ValueError(
                f"{path}: a supersession source cannot be the predecessor"
            )
        graph[declaration.predecessor_registry_id] = declaration.successor_registry_id
    for start in graph:
        seen: set[str] = set()
        current = start
        while current in graph:
            if current in seen:
                raise ValueError(f"{path}: supersession declarations contain a cycle")
            seen.add(current)
            current = graph[current]


def fetch_all(
    manifest: Manifest,
    *,
    store: Path | str,
    lock_path: Path | str,
    client: httpx.Client | None = None,
    delay: float = 1.0,
    xls_converter=None,
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
            record = lock["sources"].get(_source_key(source))
            if record is not None:
                _sync_registry_metadata(record, source)
            if source.conversion is not None:
                if xls_converter is None:
                    from corridor.spreadsheet_conversion import convert_xls_bytes

                    xls_converter = convert_xls_bytes
                _derive_xlsx(
                    source,
                    store=store,
                    lock=lock,
                    summary=summary,
                    converter=xls_converter,
                )
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


def _derive_xlsx(source, *, store, lock, summary, converter) -> None:
    """Derive one deterministic XLSX without relabeling it as fetched."""

    assert source.conversion is not None
    source_key = _source_key(source)
    key = _derived_source_key(source)
    original = lock["sources"].get(source_key)
    prior = lock["sources"].get(key)
    if not original or not original.get("sha256") or not original.get("local_path"):
        lock["sources"][key] = _failure(
            prior,
            status=None,
            error="source XLS is unavailable for conversion",
        )
        summary.failed.append(key)
        return
    source_sha256 = original["sha256"]
    from corridor.spreadsheet_conversion import CONVERTER_NAME, CONVERTER_VERSION

    if (
        prior
        and prior.get("derivation", {}).get("source_sha256") == source_sha256
        and prior.get("derivation", {}).get("tool") == CONVERTER_NAME
        and prior.get("derivation", {}).get("tool_version") == CONVERTER_VERSION
        and prior.get("local_path")
        and Path(prior["local_path"]).exists()
    ):
        summary.skipped.append(key)
        return

    derived = _converted_source(source)
    try:
        body = converter(Path(original["local_path"]).read_bytes())
    except Exception as exc:
        lock["sources"][key] = _failure(
            prior,
            status=None,
            error=f"XLS conversion failed: {type(exc).__name__}: {exc}",
        )
        summary.failed.append(key)
        return
    _store_and_record(
        derived,
        key,
        prior,
        body,
        store,
        lock,
        summary,
        status=200,
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        name=derived.member,
        extra={
            "member": derived.member,
            "derivation": {
                "kind": "format_conversion",
                "source_registry_id": source.registry_id,
                "source_sha256": source_sha256,
                "tool": CONVERTER_NAME,
                "tool_version": CONVERTER_VERSION,
            },
        },
    )


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
    # certainly no downloading the archive. The skip additionally requires
    # the stored file to still exist: a lock record describes bytes on disk,
    # and a wiped or partial store must heal on the next fetch rather than
    # being trusted into a hole ingest then falls into.
    if prior and prior.get("member_crc32") == info.CRC:
        prior_path = prior.get("local_path")
        if prior_path and Path(prior_path).exists():
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
        # Unchanged bytes never rewrite the lock — but the store file the
        # record points at must exist. If it was wiped, put the bytes we
        # just fetched back where the record says they live.
        prior_path = Path(prior.get("local_path") or _store_path(store, sha, name))
        if not prior_path.exists():
            prior_path.parent.mkdir(parents=True, exist_ok=True)
            prior_path.write_bytes(body)
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
    _sync_registry_metadata(record, source)
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


def _failure(
    prior: dict | None, *, status: int | None, error: str | None = None
) -> dict:
    """Record the attempt without discarding a prior successful retrieval."""
    record = (
        dict(prior)
        if prior
        else {
            "sha256": None,
            "retrieved_at": None,
            "local_path": None,
            "content_type": None,
            "bytes": None,
            "history": [],
        }
    )
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


def _source_key(source: Source) -> str:
    return f"{source.url}::{source.member}" if source.member else source.url


def _derived_source_key(source: Source) -> str:
    return f"derived:{_source_key(source)}:xlsx"


def _converted_source(source: Source) -> Source:
    assert source.conversion is not None
    source_name = source.member or Path(urlparse(source.url).path).name
    converted_name = str(Path(source_name).with_suffix(".xlsx"))
    return Source(
        url=f"derived:{_source_key(source)}",
        member=converted_name,
        doc_type=source.doc_type,
        role=source.role,
        title=source.conversion.title,
        curation_status=source.curation_status,
        doc_date=source.doc_date,
        registry_id=source.conversion.registry_id,
    )


def _sync_registry_metadata(record: dict, source: Source) -> None:
    """Mirror curated declarations without making fetch recency meaningful."""
    record.pop("registry_id", None)
    record.pop("supersession", None)
    record.pop("numbering_scheme", None)
    record.pop("curation_status", None)
    record["curation_status"] = source.curation_status
    if source.registry_id is not None:
        record["registry_id"] = source.registry_id
    if source.numbering_scheme is not None:
        record["numbering_scheme"] = source.numbering_scheme
    declaration = source.supersession
    if declaration is not None:
        record["supersession"] = {
            "predecessor_registry_id": declaration.predecessor_registry_id,
            "successor_registry_id": declaration.successor_registry_id,
            "replacement_date": declaration.replacement_date.isoformat(),
            "source_registry_id": declaration.source_registry_id,
            "source_page": declaration.source_page,
        }


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
    lock["ingest_by_default"] = manifest.ingest_by_default
    for source in manifest.sources:
        record = lock["sources"].get(_source_key(source))
        if record is not None:
            _sync_registry_metadata(record, source)
        if source.conversion is not None:
            derived = lock["sources"].get(_derived_source_key(source))
            if derived is not None:
                _sync_registry_metadata(derived, _converted_source(source))
    return lock


def _write_lock(path: Path, lock: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "manifest",
        nargs="?",
        help="fetch only this manifest; omit to fetch every manifest in corpus/",
    )
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    """Fetch one named manifest, or every manifest in corpus/ by default.

    Each project manifest writes its own <stem>.lock.json.

    A sealed manifest is skipped and named in the output. Sealing exists
    because the eval holdout's whole value is that nobody has looked at it,
    and this loop is how it would get looked at by accident — one glob, no
    prompt, irreversible.
    """
    args = _parse_args([] if argv is None else argv)
    if args.manifest:
        manifests = [Path(args.manifest)]
    else:
        manifests = sorted(Path("corpus").glob("*.yaml"))
    if not manifests:
        print("no manifests in corpus/", file=sys.stderr)
        return 1

    failed = 0
    for manifest_path in manifests:
        manifest = load_manifest(manifest_path)
        if manifest.sealed:
            print(
                f"{manifest.project}: SEALED, not fetched "
                f"({len(manifest.sources)} source(s) left untouched)",
                flush=True,
            )
            continue
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


def _run_cli() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_run_cli())
