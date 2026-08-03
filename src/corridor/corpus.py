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
import json
import sys
import time
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


@dataclass(frozen=True)
class Source:
    url: str
    doc_type: str
    role: str
    title: str
    doc_date: date | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Manifest:
    project: str
    agency: str | None
    sources: tuple[Source, ...]


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
            )
        )
    return Manifest(
        project=raw["project"],
        agency=raw.get("agency"),
        sources=tuple(sources),
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

    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        for i, source in enumerate(manifest.sources):
            if i and delay:
                time.sleep(delay)
            _fetch_one(source, store, lock, summary, client)
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
) -> None:
    prior = lock["sources"].get(source.url)

    try:
        response = client.get(source.url, headers={"user-agent": USER_AGENT})
    except httpx.HTTPError as exc:
        lock["sources"][source.url] = _failure(prior, status=None, error=str(exc))
        summary.failed.append(source.url)
        return

    if response.status_code != 200:
        lock["sources"][source.url] = _failure(prior, status=response.status_code)
        summary.failed.append(source.url)
        return

    body = response.content
    sha = hashlib.sha256(body).hexdigest()

    if prior and prior.get("sha256") == sha:
        summary.skipped.append(source.url)
        return

    path = _store_path(store, sha, source.url)
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

    lock["sources"][source.url] = {
        "sha256": sha,
        "retrieved_at": _now(),
        "local_path": str(path),
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "bytes": len(body),
        "doc_type": source.doc_type,
        "role": source.role,
        "title": source.title,
        "doc_date": source.doc_date.isoformat() if source.doc_date else None,
        "history": history,
    }
    (summary.drifted if drifted else summary.fetched).append(source.url)


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


def _store_path(store: Path, sha: str, url: str) -> Path:
    # Sharded by the first two hex chars, extension preserved so the store
    # stays browsable by a human looking for a PDF.
    suffix = Path(urlparse(url).path).suffix
    return store / sha[:2] / f"{sha}{suffix}"


def _read_lock(path: Path, manifest: Manifest) -> dict:
    if path.exists():
        lock = json.loads(path.read_text())
        lock.setdefault("sources", {})
        return lock
    return {"project": manifest.project, "sources": {}}


def _write_lock(path: Path, lock: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    manifest_path = Path("corpus/manifest.yaml")
    if not manifest_path.exists():
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 1

    summary = fetch_all(
        load_manifest(manifest_path),
        store=Path("corpus/files"),
        lock_path=Path("corpus/manifest.lock.json"),
    )
    print(
        f"fetched {len(summary.fetched)}  skipped {len(summary.skipped)}  "
        f"drifted {len(summary.drifted)}  failed {len(summary.failed)}"
    )
    for url in summary.drifted:
        print(f"  drift: {url} (previous revision retained)")
    for url in summary.failed:
        print(f"  FAILED: {url}")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
