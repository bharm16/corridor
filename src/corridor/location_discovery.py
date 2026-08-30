"""One concrete adapter that discovers and processes documents from one
explicitly chosen connected location (#350).

This is deliberately *one* adapter for *one* location, not a generic connector
platform. It bridges a live location listing to the primitives that already
exist — the same content-addressed staging, exact-identity registration, and
standing project-processing handoff that product upload intake uses (#349,
``source_intake``, ``ingest``), driven by the one shared Due Work runtime (#332,
``due_work``) rather than a connector-specific scheduler. Nothing here writes a
Constraint Record; extraction and Record Inclusion remain the standing pass's
job, so a registered document is handed off exactly the way an uploaded one is.

What the location contract is, and why it is explicit. ``http-index-v1`` reads a
structured JSON index. ``txdot-rid-box-v1`` reads one exact named link from an
official TxDOT RID page, binds the public Box page's stated shared-file identity,
and enumerates that ZIP through bounded HTTP ranges. It does not crawl arbitrary
HTML or infer a Document kind from a filename. A title and a kind *hint* remain
the location's advisory words and never become an accepted document fact. The
concrete authorized host boundary, the sealed-holdout flag, and every resource
budget are server-owned configuration validated before the adapter is ever
enabled (``due_work.configure_location_discovery``); an unconfigured adapter
scans nothing, and credentials never widen that scope.

Four honesty rules carry the design:

- **Discovery is not registration.** A newly observed reference is coalesced into
  one ``proposed`` intake identity, keyed by where it was observed. It becomes a
  Document only after a person authorizes it — declaring the kind the bytes cannot
  state — and a validated fetch turns the exact bytes into a registered Document.
- **A failure never masquerades as a retrieval.** Every fetch validates the
  source and redirect boundary, the response, its type and size, and the exact
  bytes before intake, and retains a failed attempt beside the last good one. An
  error page, a missing member, a truncated body, or an unauthorized redirect is a
  recorded failure, not a source document.
- **Drift is operations work, never an overwrite.** Different bytes under an
  existing registry identity are retained as a held drift; the old Document is not
  replaced, no Supersession is inferred, and the two are not assumed renditions
  (ADR-0015).
- **Sealed holdouts and budgets bound the machine.** A sealed rehearsal location
  is refused before any fetch or extraction, and every scheduled scope and
  recovery command honors the same exclusion; nested-archive depth, enumeration,
  request, size, time, and total-processing budgets each turn exhaustion into a
  retained, resumable Processing Failure rather than a silent success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urljoin, urlparse
import zipfile

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.config import settings
from corridor.ingest import ingest_document, reparse_document
from corridor.models import (
    DiscoveredReference,
    Document,
    DocumentQuarantine,
    DueWorkSchedule,
    ExtractionRun,
    SourceFetchAttempt,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_intake import (
    ACCEPTED_DOC_TYPES,
    IntakeRefused,
    validate_and_stage,
)
from corridor.storage import stored_file

# Kept equal to ``due_work.HANDLER_LOCATION_DISCOVERY`` without importing that
# module (which imports this one): a test asserts the two strings agree. Reading
# the persisted schedule by this key lets recovery honor the same sealed scope the
# scheduled pass does, without a module cycle.
HANDLER_KEY = "location_discovery"

# The server-owned principal every discovery/fetch act is attributed to. The
# human basis for a registration is the authorization it required.
LOCATION_DISCOVERY_ACTOR = "corridor:location-discovery"

_MIB = 1024 * 1024
_BOX_RANGE_PROBE_BYTES = 64 * 1024
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
)
_BOX_SHARED_FILE = re.compile(
    r'"sharedName":"(?P<shared>[a-zA-Z0-9]+)"\s*,\s*'
    r'"vanityName":"[^"]*"\s*,\s*'
    r'"itemID":(?P<item_id>[0-9]+)\s*,\s*'
    r'"itemType":"(?P<item_type>[^"]+)"'
)


class LocationDiscoveryRefused(ValueError):
    """A location scope, index, or recovery request is unsafe or malformed.

    ``reason`` is a stable machine code so an adapter can branch on it.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DiscoveryBudgets:
    """Every bound a single bounded pass must not exceed (#350, ADR-0046)."""

    max_references: int
    max_requests: int
    max_documents: int
    nested_archive_depth: int
    max_compressed_bytes: int
    max_decompressed_bytes: int
    time_budget_seconds: int

    @classmethod
    def default(cls) -> "DiscoveryBudgets":
        return cls(
            max_references=500,
            max_requests=200,
            max_documents=100,
            nested_archive_depth=2,
            max_compressed_bytes=512 * _MIB,
            max_decompressed_bytes=128 * _MIB,
            time_budget_seconds=55,
        )

    def validated(self) -> "DiscoveryBudgets":
        if not (
            1 <= self.max_references <= 100_000
            and 1 <= self.max_requests <= 100_000
            and 1 <= self.max_documents <= 100_000
            and 1 <= self.nested_archive_depth <= 8
            and _MIB <= self.max_compressed_bytes <= 4096 * _MIB
            and _MIB <= self.max_decompressed_bytes <= 4096 * _MIB
            and 1 <= self.time_budget_seconds <= 3600
        ):
            raise LocationDiscoveryRefused(
                "invalid_budgets", "location discovery budgets are out of range"
            )
        return self


@dataclass(frozen=True)
class LocationScope:
    """The one resolved, server-owned location an enabled adapter may reach."""

    location_id: str
    project_id: int
    index_url: str
    authorized_hosts: frozenset[str]
    sealed: bool
    source_manifest_id: str
    adapter_identity: str
    rid_link_text: str | None = None


@dataclass(frozen=True)
class IndexEntry:
    """One reference as the location's index states it, before any interpretation."""

    url: str
    title: str | None = None
    member: str | None = None
    type_hint: str | None = None


@dataclass(frozen=True)
class BoxSharedFile:
    """One public Box file identity stated by its own shared-file page."""

    shared_name: str
    item_id: int
    filename: str
    archive_url: str


@dataclass(frozen=True)
class DiscoveryPassResult:
    """The honest counts of one discovery-and-fetch pass, for a bounded receipt."""

    location_id: str
    project_id: int
    sealed_excluded: bool
    references_observed: int
    new_references: int
    repeat_references: int
    new_archive_urls: int
    authorized_selected: int
    registered: int
    unchanged: int
    drifted: int
    fetch_failures: int
    budget_exhausted: int
    registered_document_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def health(self) -> str:
        if self.sealed_excluded:
            return "held_sealed"
        if self.fetch_failures or self.drifted or self.budget_exhausted:
            return "attention_required"
        return "healthy"


def summarize_discovery_pass(
    result: DiscoveryPassResult,
    *,
    configuration_version: str,
    observed_at: datetime,
) -> dict:
    """A bounded, counts-only receipt of one pass, for the Due Work handler.

    The rich per-reference detail lives in the durable rows; a receipt keeps only
    counts and a health verdict so it stays within the handler's byte contract
    regardless of how much a location publishes.
    """

    return {
        "schema_version": "location-discovery-result-v1",
        "project_id": result.project_id,
        "configuration_version": configuration_version,
        "observed_at": _aware(observed_at).isoformat(),
        "health": result.health,
        "location_id": result.location_id,
        "sealed_excluded": result.sealed_excluded,
        "references_observed": result.references_observed,
        "new_references": result.new_references,
        "repeat_references": result.repeat_references,
        "new_archive_urls": result.new_archive_urls,
        "authorized_selected": result.authorized_selected,
        "registered": result.registered,
        "unchanged": result.unchanged,
        "drifted": result.drifted,
        "fetch_failures": result.fetch_failures,
        "budget_exhausted": result.budget_exhausted,
    }


@dataclass(frozen=True)
class ParseRecoveryResult:
    """The outcome of one bounded parse-recovery operation on one document."""

    document_id: int
    outcome: str
    pages: int
    reason: str | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    """The attributable outcome of authorizing one discovered reference."""

    reference_id: int
    reference_key: str
    doc_type: str
    registry_id: str | None
    audit_id: int


# --- Discovery: observe references and coalesce them ------------------------


def parse_index(body: bytes) -> tuple[IndexEntry, ...]:
    """Parse the location's structured index into reference entries.

    The contract is explicit on purpose (see the module docstring): a JSON object
    with an ``entries`` array, each entry naming at least a ``url`` and optionally
    a ``member``, ``title``, and ``type_hint``. A malformed index is refused rather
    than half-read, so a truncated or foreign response cannot masquerade as a
    listing.
    """

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LocationDiscoveryRefused(
            "malformed_index", f"location index is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise LocationDiscoveryRefused(
            "malformed_index", "location index must be an object with an entries list"
        )
    entries: list[IndexEntry] = []
    for raw in payload["entries"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not raw["url"]:
            raise LocationDiscoveryRefused(
                "malformed_index", "each location index entry needs a url"
            )
        member = raw.get("member")
        title = raw.get("title")
        type_hint = raw.get("type_hint")
        if member is not None and not isinstance(member, str):
            raise LocationDiscoveryRefused(
                "malformed_index", "a location index member must be a string"
            )
        entries.append(
            IndexEntry(
                url=raw["url"],
                title=title if isinstance(title, str) else None,
                member=member or None,
                type_hint=type_hint if isinstance(type_hint, str) else None,
            )
        )
    return tuple(entries)


class _RIDLinkParser(HTMLParser):
    """Collect anchor text and hrefs without executing or interpreting the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        values = dict(attrs)
        href = values.get("href")
        if isinstance(href, str):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        self.links.append((" ".join("".join(self._text).split()), self._href))
        self._href = None
        self._text = []


class _MetaTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_graph_title: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        if values.get("property") == "og:title" and isinstance(
            values.get("content"), str
        ):
            self.open_graph_title = values["content"]


def parse_txdot_rid_box_link(
    body: bytes,
    *,
    index_url: str,
    link_text: str,
) -> str:
    """Select one exact public Box link from an official TxDOT RID page.

    The operator declares the visible RID label, such as ``Utilities``. The
    adapter follows exactly one matching link. Missing or repeated labels are a
    retained failure instead of an arbitrary first-match choice.
    """

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocationDiscoveryRefused(
            "malformed_rid_page", "TxDOT RID page is not UTF-8"
        ) from exc
    parser = _RIDLinkParser()
    parser.feed(text)
    wanted = " ".join(link_text.split())
    matches = [urljoin(index_url, href) for label, href in parser.links if label == wanted]
    if len(matches) != 1:
        raise LocationDiscoveryRefused(
            "rid_link_ambiguous",
            f"TxDOT RID page must contain exactly one {wanted!r} link; found {len(matches)}",
        )
    parsed = urlparse(matches[0])
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"txdot.box.com", "txdot.app.box.com"}
        or not re.fullmatch(r"/s/[a-zA-Z0-9]+/?", parsed.path)
    ):
        raise LocationDiscoveryRefused(
            "invalid_box_share", "TxDOT RID link is not one public TxDOT Box share"
        )
    return matches[0]


def parse_box_shared_file(body: bytes, *, shared_url: str) -> BoxSharedFile:
    """Bind a public Box page to its stated file identity and ZIP filename."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocationDiscoveryRefused(
            "malformed_box_page", "Box shared-file page is not UTF-8"
        ) from exc
    identity = _BOX_SHARED_FILE.search(text)
    title_parser = _MetaTitleParser()
    title_parser.feed(text)
    title = title_parser.open_graph_title
    if identity is None or title is None:
        raise LocationDiscoveryRefused(
            "malformed_box_page", "Box page does not state one shared-file identity"
        )
    if identity.group("item_type") != "file":
        raise LocationDiscoveryRefused(
            "box_share_not_file", "Box share does not name one downloadable file"
        )
    shared_name = identity.group("shared")
    path_token = urlparse(shared_url).path.rstrip("/").rsplit("/", 1)[-1]
    if shared_name != path_token:
        raise LocationDiscoveryRefused(
            "box_share_identity_mismatch",
            "Box page identity does not match the requested shared link",
        )
    filename = title.removesuffix(" | Powered by Box").strip()
    if not filename.lower().endswith(".zip"):
        raise LocationDiscoveryRefused(
            "box_share_not_archive", "Box shared file is not a ZIP archive"
        )
    item_id = int(identity.group("item_id"))
    archive_url = (
        "https://app.box.com/index.php?rm=box_download_shared_file"
        f"&shared_name={shared_name}&file_id=f_{item_id}"
    )
    return BoxSharedFile(
        shared_name=shared_name,
        item_id=item_id,
        filename=filename,
        archive_url=archive_url,
    )


def effective_source_url(url: str, member: str | None) -> str:
    """The canonical retrieval locator stored as a Document's source url.

    A member reference is ``url::member`` — the same key shape the corpus lockfile
    uses — so a discovered member and a manifest-declared member resolve to the
    same provenance string and a re-observation is recognized, not re-created.
    """

    return f"{url}::{member}" if member else url


def reference_key(location_id: str, source_url: str) -> str:
    """A deterministic identity over where a reference was observed."""

    return hashlib.sha256(f"{location_id}\x00{source_url}".encode()).hexdigest()


def observe_references(
    session: Session,
    scope: LocationScope,
    entries: tuple[IndexEntry, ...],
    *,
    now: datetime,
) -> tuple[int, int, int, int]:
    """Coalesce observed references into proposed intake identities.

    Returns ``(new, repeat, known_registered, new_archive_urls)``. A reference
    already backed by a registered Document — a member of an already-declared dated
    snapshot — is recognized and not counted as a new discovery; a reference seen
    before updates its existing identity in place; only a genuinely new reference
    becomes a ``proposed`` row. No observed title, type, or date is ever promoted to
    an accepted fact.
    """

    now = _aware(now)
    new = repeat = known = 0
    new_archive_urls: set[str] = set()
    prior_archive_urls = set(
        session.scalars(
            select(DiscoveredReference.archive_url).where(
                DiscoveredReference.project_id == scope.project_id,
                DiscoveredReference.location_id == scope.location_id,
                DiscoveredReference.archive_url.is_not(None),
            )
        ).all()
    )
    for entry in entries:
        source_url = effective_source_url(entry.url, entry.member)
        key = reference_key(scope.location_id, source_url)
        row = session.scalar(
            select(DiscoveredReference).where(
                DiscoveredReference.project_id == scope.project_id,
                DiscoveredReference.reference_key == key,
            )
        )
        if row is not None:
            row.last_observed_at = now
            row.observed_count += 1
            repeat += 1
            continue
        already_document = session.scalar(
            select(Document.id).where(
                Document.project_id == scope.project_id,
                Document.source_url == source_url,
            )
        )
        if already_document is not None:
            known += 1
            continue
        archive_url = entry.url if entry.member else None
        session.add(
            DiscoveredReference(
                project_id=scope.project_id,
                location_id=scope.location_id,
                reference_key=key,
                source_url=source_url,
                archive_url=archive_url,
                member=entry.member,
                observed_title=entry.title,
                observed_type_hint=entry.type_hint,
                first_observed_at=now,
                last_observed_at=now,
                observed_count=1,
                state="proposed",
            )
        )
        new += 1
        if archive_url is not None and archive_url not in prior_archive_urls:
            new_archive_urls.add(archive_url)
    session.flush()
    return new, repeat, known, len(new_archive_urls)


# --- Authorization: a person declares the kind the bytes cannot state -------


def authorize_reference(
    session: Session,
    *,
    project_id: int,
    reference_key: str,
    doc_type: str,
    principal: HumanPrincipal,
    registry_id: str | None = None,
    now: datetime | None = None,
) -> AuthorizationResult:
    """Authorize one proposed reference for processing, attributably.

    This is the reference-side analogue of confirming an upload: a person, not the
    machine, declares the document kind (a question the bytes cannot answer,
    ADR-0007) and optionally a stable registry identity. It refuses an unknown
    kind, an unknown reference, or one that is not still proposed, and records one
    append-only receipt. It registers nothing itself — the fetch pass does that
    from the exact bytes.
    """

    principal = require_human_principal(principal)
    if doc_type not in ACCEPTED_DOC_TYPES:
        raise LocationDiscoveryRefused(
            "unknown_doc_type", f"{doc_type!r} is not a document kind Corridor records"
        )
    if registry_id is not None and not registry_id.strip():
        raise LocationDiscoveryRefused(
            "invalid_registry_id", "a registry id must be non-empty when given"
        )
    row = session.scalar(
        select(DiscoveredReference).where(
            DiscoveredReference.project_id == project_id,
            DiscoveredReference.reference_key == reference_key,
        )
    )
    if row is None:
        raise LocationDiscoveryRefused(
            "unknown_reference", "no such discovered reference in this project"
        )
    if row.state != "proposed":
        raise LocationDiscoveryRefused(
            "not_proposed", f"reference is {row.state}, not awaiting authorization"
        )
    moment = _aware(now) if now is not None else datetime.now(timezone.utc)
    row.state = "authorized"
    row.authorized_doc_type = doc_type
    row.authorized_registry_id = registry_id
    row.authorized_by = principal.subject
    row.authorized_at = moment
    session.flush()
    entry = audit.record(
        session,
        principal=principal,
        action=audit.AUTHORIZE_DISCOVERED_REFERENCE,
        entity_type=audit.PROJECT,
        entity_id=project_id,
        after={
            "reference_key": reference_key,
            "source_url": row.source_url,
            "doc_type": doc_type,
            "registry_id": registry_id,
            "location_id": row.location_id,
        },
    )
    return AuthorizationResult(
        reference_id=row.id,
        reference_key=reference_key,
        doc_type=doc_type,
        registry_id=registry_id,
        audit_id=entry.id,
    )


# --- The bounded discovery-and-fetch pass -----------------------------------


def discover_and_process(
    session_factory,
    *,
    scope: LocationScope,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    clock,
) -> DiscoveryPassResult:
    """Run one bounded discovery-and-fetch pass for one enabled location.

    A sealed location is refused before any request. Otherwise the pass fetches and
    parses the index, coalesces discovery, then fetches every authorized reference
    that is not held by an open drift — each reference's fetch, exact-identity
    registration, and retained attempt committing together so a crash cannot leave
    partial content advertised as complete. Budgets stop the pass with a retained,
    resumable Processing Failure rather than a false success. Extraction is not run
    here: a registered Document is handed to the standing project-processing pass.
    """

    budgets = budgets.validated()
    started = _aware(clock.now())

    if scope.sealed:
        # A sealed rehearsal/holdout is excluded before any lower-level fetch or
        # extraction; reading it once would spend the corpus (ADR-0008).
        return DiscoveryPassResult(
            location_id=scope.location_id,
            project_id=scope.project_id,
            sealed_excluded=True,
            references_observed=0,
            new_references=0,
            repeat_references=0,
            new_archive_urls=0,
            authorized_selected=0,
            registered=0,
            unchanged=0,
            drifted=0,
            fetch_failures=0,
            budget_exhausted=0,
        )

    counters = _Counters()

    # --- Discovery phase (its own committed transaction) --------------------
    with session_factory() as session:
        with session.begin():
            index = _fetch_index(
                session, scope, budgets, client, counters, now=started
            )
            if index is None:
                counters.fetch_failures += 1
            else:
                entries, truncated = index
                if truncated:
                    _record_attempt(
                        session,
                        scope,
                        reference_key=None,
                        attempted_at=started,
                        outcome="budget_exhausted",
                        reason=(
                            "enumeration_budget: index named more than "
                            f"{budgets.max_references} references"
                        ),
                        resumable=True,
                    )
                    counters.budget_exhausted += 1
                new, repeat, _known, new_archives = observe_references(
                    session, scope, entries, now=started
                )
                counters.references_observed = len(entries)
                counters.new_references = new
                counters.repeat_references = repeat
                counters.new_archive_urls = new_archives

    # --- Fetch phase (one committed transaction per reference) --------------
    authorized = _authorized_reference_keys(session_factory, scope)
    counters.authorized_selected = len(authorized)
    for key in authorized:
        if counters.registered + counters.unchanged >= budgets.max_documents:
            _pass_budget_exhausted(
                session_factory, scope, clock, "document_budget", key
            )
            counters.budget_exhausted += 1
            break
        if counters.requests >= budgets.max_requests:
            _pass_budget_exhausted(
                session_factory, scope, clock, "request_budget", key
            )
            counters.budget_exhausted += 1
            break
        if _elapsed_seconds(clock, started) >= budgets.time_budget_seconds:
            _pass_budget_exhausted(session_factory, scope, clock, "time_budget", key)
            counters.budget_exhausted += 1
            break
        _process_one_authorized_reference(
            session_factory, scope, budgets, client, clock, key, counters
        )

    return DiscoveryPassResult(
        location_id=scope.location_id,
        project_id=scope.project_id,
        sealed_excluded=False,
        references_observed=counters.references_observed,
        new_references=counters.new_references,
        repeat_references=counters.repeat_references,
        new_archive_urls=counters.new_archive_urls,
        authorized_selected=counters.authorized_selected,
        registered=counters.registered,
        unchanged=counters.unchanged,
        drifted=counters.drifted,
        fetch_failures=counters.fetch_failures,
        budget_exhausted=counters.budget_exhausted,
        registered_document_ids=tuple(counters.registered_document_ids),
    )


@dataclass
class _Counters:
    references_observed: int = 0
    new_references: int = 0
    repeat_references: int = 0
    new_archive_urls: int = 0
    authorized_selected: int = 0
    registered: int = 0
    unchanged: int = 0
    drifted: int = 0
    fetch_failures: int = 0
    budget_exhausted: int = 0
    requests: int = 0
    registered_document_ids: list[int] = field(default_factory=list)


def _authorized_reference_keys(session_factory, scope: LocationScope) -> list[str]:
    """The reference keys ready to fetch: authorized, not held by an open drift.

    A reference whose most recent attempt is a drift is deliberately skipped — it
    is operations work awaiting a human decision, and re-fetching it would only
    re-observe the same unchanged drifted bytes.
    """

    with session_factory() as session:
        rows = session.scalars(
            select(DiscoveredReference)
            .where(
                DiscoveredReference.project_id == scope.project_id,
                DiscoveredReference.location_id == scope.location_id,
                DiscoveredReference.state == "authorized",
            )
            .order_by(DiscoveredReference.id)
        ).all()
        ready: list[str] = []
        for row in rows:
            latest = session.scalar(
                select(SourceFetchAttempt.outcome)
                .where(
                    SourceFetchAttempt.project_id == scope.project_id,
                    SourceFetchAttempt.reference_key == row.reference_key,
                )
                .order_by(SourceFetchAttempt.id.desc())
                .limit(1)
            )
            if latest == "drift":
                continue
            ready.append(row.reference_key)
        return ready


def _process_one_authorized_reference(
    session_factory,
    scope: LocationScope,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    clock,
    key: str,
    counters: _Counters,
) -> None:
    """Fetch, validate, and register one authorized reference in one transaction."""

    with session_factory() as session:
        with session.begin():
            row = session.scalar(
                select(DiscoveredReference).where(
                    DiscoveredReference.project_id == scope.project_id,
                    DiscoveredReference.reference_key == key,
                )
            )
            if row is None or row.state != "authorized":
                return
            attempted_at = _aware(clock.now())
            fetched = _fetch_reference_bytes(scope, row, budgets, client, counters)
            if isinstance(fetched, _FetchFailure):
                _record_attempt(
                    session,
                    scope,
                    reference_key=key,
                    attempted_at=attempted_at,
                    outcome="failed",
                    reason=f"{fetched.reason}: {fetched.message}",
                    resolved_url=fetched.resolved_url,
                    http_status=fetched.http_status,
                    resumable=True,
                )
                counters.fetch_failures += 1
                return
            _register_fetched_bytes(
                session, scope, row, fetched, attempted_at, budgets, counters
            )


def _register_fetched_bytes(
    session: Session,
    scope: LocationScope,
    row: DiscoveredReference,
    fetched: "_FetchedBytes",
    attempted_at: datetime,
    budgets: DiscoveryBudgets,
    counters: _Counters,
) -> None:
    """Validate exact bytes, detect drift, and register — or retain the refusal."""

    filename = _reference_filename(row)
    try:
        staged = validate_and_stage(
            fetched.body,
            filename,
            max_bytes=budgets.max_decompressed_bytes,
        )
    except IntakeRefused as exc:
        _record_attempt(
            session,
            scope,
            reference_key=row.reference_key,
            attempted_at=attempted_at,
            outcome="failed",
            reason=f"{exc.reason}: {exc}",
            resolved_url=fetched.resolved_url,
            http_status=fetched.http_status,
            byte_count=len(fetched.body),
            content_type=fetched.content_type,
            resumable=True,
        )
        counters.fetch_failures += 1
        return

    registry_id = row.authorized_registry_id
    if registry_id is not None:
        prior = session.scalar(
            select(Document).where(
                Document.project_id == scope.project_id,
                Document.registry_id == registry_id,
            )
        )
        if prior is not None and prior.sha256 != staged.sha256:
            # A changed checksum under an existing registry identity is retained
            # operations work: the old Document stands, no Supersession is inferred,
            # and both byte identities are kept (the new bytes are already staged).
            _record_attempt(
                session,
                scope,
                reference_key=row.reference_key,
                attempted_at=attempted_at,
                outcome="drift",
                reason=(
                    "changed checksum under an existing registry identity; "
                    "resolve as a declared Supersession or dismiss"
                ),
                resolved_url=fetched.resolved_url,
                http_status=fetched.http_status,
                byte_count=staged.size_bytes,
                content_type=fetched.content_type,
                sha256=staged.sha256,
                prior_sha256=prior.sha256,
                document_id=prior.id,
            )
            counters.drifted += 1
            return

    pre_existing = session.scalar(
        select(Document.id).where(
            Document.project_id == scope.project_id,
            Document.sha256 == staged.sha256,
        )
    )
    document = ingest_document(
        session,
        project_id=scope.project_id,
        path=staged.stored_path,
        doc_type=row.authorized_doc_type,
        images_dir=Path(settings.corpus_images),
        filename=staged.filename,
        source_url=row.source_url,
        retrieved_at=attempted_at,
        registry_id=registry_id,
        expected_sha256=staged.sha256,
    )
    created = pre_existing is None
    row.state = "registered"
    row.registered_document_id = document.id
    _record_attempt(
        session,
        scope,
        reference_key=row.reference_key,
        attempted_at=attempted_at,
        outcome="registered" if created else "unchanged",
        resolved_url=fetched.resolved_url,
        http_status=fetched.http_status,
        byte_count=staged.size_bytes,
        content_type=fetched.content_type,
        sha256=staged.sha256,
        document_id=document.id,
    )
    if created:
        counters.registered += 1
        counters.registered_document_ids.append(document.id)
    else:
        counters.unchanged += 1


# --- Fetch: authorized boundary, response validation, archive members -------


@dataclass(frozen=True)
class _FetchedBytes:
    body: bytes
    resolved_url: str
    content_type: str | None
    http_status: int


@dataclass(frozen=True)
class _FetchFailure:
    reason: str
    message: str
    resolved_url: str | None = None
    http_status: int | None = None


class _RemoteArchiveError(OSError):
    def __init__(self, failure: _FetchFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class _AuthorizedRangeFile:
    """Seekable bounded HTTP file used by ``zipfile`` for one remote archive."""

    def __init__(
        self,
        *,
        scope: LocationScope,
        url: str,
        size: int,
        budgets: DiscoveryBudgets,
        client: httpx.Client,
        counters: _Counters,
    ) -> None:
        self.scope = scope
        self.url = url
        self.size = size
        self.budgets = budgets
        self.client = client
        self.counters = counters
        self.position = 0

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.position = offset
        elif whence == 1:
            self.position += offset
        elif whence == 2:
            self.position = self.size + offset
        else:
            raise ValueError(f"unsupported seek mode {whence}")
        return self.position

    def read(self, length: int = -1) -> bytes:
        if length is None or length < 0:
            length = self.size - self.position
        if length <= 0 or self.position >= self.size:
            return b""
        end = min(self.position + length, self.size) - 1
        start = self.position
        got = _authorized_get(
            self.scope,
            self.url,
            self.budgets,
            self.client,
            self.counters,
            headers={
                "Range": f"bytes={start}-{end}",
                "User-Agent": _BROWSER_USER_AGENT,
            },
        )
        if isinstance(got, _FetchFailure):
            raise _RemoteArchiveError(got)
        response, resolved_url = got
        self.url = resolved_url
        if response.status_code == 206:
            body = response.content
        elif response.status_code == 200 and len(response.content) == self.size:
            body = response.content[start : end + 1]
        else:
            raise _RemoteArchiveError(
                _FetchFailure(
                    "range_not_supported",
                    f"archive range returned {response.status_code}",
                    resolved_url=resolved_url,
                    http_status=response.status_code,
                )
            )
        self.position += len(body)
        return body


@dataclass
class _RemoteZip:
    archive: zipfile.ZipFile
    resolved_url: str


def _open_remote_zip(
    scope: LocationScope,
    url: str,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
) -> _RemoteZip | _FetchFailure:
    """Open one authorized ZIP from a bounded 64 KiB range probe."""

    got = _authorized_get(
        scope,
        url,
        budgets,
        client,
        counters,
        headers={
            "Range": f"bytes=0-{_BOX_RANGE_PROBE_BYTES - 1}",
            "User-Agent": _BROWSER_USER_AGENT,
        },
    )
    if isinstance(got, _FetchFailure):
        return got
    response, resolved_url = got
    if response.status_code == 200:
        body = response.content
        if len(body) > budgets.max_compressed_bytes:
            return _FetchFailure(
                "archive_too_large",
                f"archive is {len(body)} bytes over the compressed budget",
                resolved_url=resolved_url,
                http_status=200,
            )
        handle = io.BytesIO(body)
    elif response.status_code == 206:
        content_range = response.headers.get("content-range", "")
        try:
            size = int(content_range.rsplit("/", 1)[-1])
        except ValueError:
            return _FetchFailure(
                "invalid_range",
                "archive range response does not state the total size",
                resolved_url=resolved_url,
                http_status=206,
            )
        if size > budgets.max_compressed_bytes:
            return _FetchFailure(
                "archive_too_large",
                f"archive is {size} bytes over the compressed budget",
                resolved_url=resolved_url,
                http_status=206,
            )
        handle = _AuthorizedRangeFile(
            scope=scope,
            url=resolved_url,
            size=size,
            budgets=budgets,
            client=client,
            counters=counters,
        )
    else:
        return _FetchFailure(
            "http_status",
            f"archive probe returned {response.status_code}",
            resolved_url=resolved_url,
            http_status=response.status_code,
        )
    try:
        return _RemoteZip(zipfile.ZipFile(handle), resolved_url)
    except _RemoteArchiveError as exc:
        return exc.failure
    except (OSError, zipfile.BadZipFile) as exc:
        return _FetchFailure(
            "not_an_archive",
            f"container is not a ZIP archive: {exc}",
            resolved_url=resolved_url,
            http_status=response.status_code,
        )


def _fetch_reference_bytes(
    scope: LocationScope,
    row: DiscoveredReference,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
) -> _FetchedBytes | _FetchFailure:
    if row.member:
        return _fetch_archive_member(scope, row, budgets, client, counters)
    return _fetch_plain_document(scope, row.source_url, budgets, client, counters)


def _fetch_plain_document(
    scope: LocationScope,
    url: str,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
) -> _FetchedBytes | _FetchFailure:
    got = _authorized_get(scope, url, budgets, client, counters)
    if isinstance(got, _FetchFailure):
        return got
    response, resolved_url = got
    if response.status_code != 200:
        return _FetchFailure(
            "http_status",
            f"source returned {response.status_code}",
            resolved_url=resolved_url,
            http_status=response.status_code,
        )
    body = response.content
    if len(body) > budgets.max_decompressed_bytes:
        return _FetchFailure(
            "response_too_large",
            f"response is {len(body)} bytes over the size budget",
            resolved_url=resolved_url,
            http_status=200,
        )
    reason = _reject_reason(response.headers, len(body))
    if reason is not None:
        return _FetchFailure(reason[0], reason[1], resolved_url=resolved_url, http_status=200)
    return _FetchedBytes(
        body=body,
        resolved_url=resolved_url,
        content_type=response.headers.get("content-type"),
        http_status=200,
    )


def _fetch_archive_member(
    scope: LocationScope,
    row: DiscoveredReference,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
) -> _FetchedBytes | _FetchFailure:
    member = row.member or ""
    depth = member.count("::") + 1
    if depth > budgets.nested_archive_depth:
        return _FetchFailure(
            "archive_too_deep",
            f"member nesting depth {depth} exceeds the budget "
            f"{budgets.nested_archive_depth}",
        )
    opened = _open_remote_zip(
        scope, row.archive_url or "", budgets, client, counters
    )
    if isinstance(opened, _FetchFailure):
        return opened
    resolved_url = opened.resolved_url
    segments = member.split("::")
    archive = opened.archive
    current: bytes | None = None
    for index, segment in enumerate(segments):
        try:
            info = archive.getinfo(segment)
        except KeyError:
            return _FetchFailure(
                "member_not_found",
                f"member {segment!r} is not in the archive",
                resolved_url=resolved_url,
                http_status=200,
            )
        if info.file_size > budgets.max_decompressed_bytes:
            return _FetchFailure(
                "member_too_large",
                f"member {segment!r} decompresses to {info.file_size} bytes "
                "over the budget",
                resolved_url=resolved_url,
                http_status=200,
            )
        try:
            current = archive.read(segment)
        except _RemoteArchiveError as exc:
            return exc.failure
        if index + 1 < len(segments):
            try:
                archive = zipfile.ZipFile(io.BytesIO(current))
            except zipfile.BadZipFile as exc:
                return _FetchFailure(
                    "not_an_archive",
                    f"container is not a ZIP archive: {exc}",
                    resolved_url=resolved_url,
                    http_status=200,
                )
    assert current is not None
    return _FetchedBytes(
        body=current,
        resolved_url=f"{resolved_url}::{member}",
        content_type=None,
        http_status=200,
    )


def _authorized_get(
    scope: LocationScope,
    url: str,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, str] | _FetchFailure:
    """GET within the authorized host boundary, following redirects manually.

    Every hop's host must be authorized, so a source or a redirect that points off
    the declared location is refused before its bytes are ever read. Each hop
    counts against the request budget.
    """

    current = url
    for _hop in range(6):
        host = urlparse(current).hostname or ""
        if host not in scope.authorized_hosts:
            return _FetchFailure(
                "unauthorized_host",
                f"{host!r} is outside the authorized location",
                resolved_url=current,
            )
        if counters.requests >= budgets.max_requests:
            return _FetchFailure(
                "request_budget",
                "request budget exhausted before the fetch completed",
                resolved_url=current,
            )
        counters.requests += 1
        try:
            response = client.get(
                current,
                follow_redirects=False,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return _FetchFailure("transport_error", str(exc), resolved_url=current)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return _FetchFailure(
                    "invalid_redirect",
                    "redirect without a location header",
                    resolved_url=current,
                    http_status=response.status_code,
                )
            current = urljoin(current, location)
            continue
        return response, current
    return _FetchFailure("too_many_redirects", "redirect chain did not settle", resolved_url=current)


def _reject_reason(headers, size: int) -> tuple[str, str] | None:
    """A 200 is not proof a document was served (mirrors the corpus checks)."""

    content_type = (headers.get("content-type") or "").lower()
    if content_type.startswith("text/html"):
        return ("error_page", "source returned HTML, not a document")
    declared = headers.get("x-archive-orig-x-crawler-content-length")
    if declared and declared.isdigit() and int(declared) > size:
        return ("truncated", f"truncated capture: {size} of {declared} bytes")
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > size:
        return ("truncated", f"truncated download: {size} of {content_length} bytes")
    return None


# --- Bounded parse recovery -------------------------------------------------


def recover_document_parse(
    session: Session,
    *,
    document_id: int,
    principal: HumanPrincipal,
    now: datetime | None = None,
    images_dir: Path | str | None = None,
) -> ParseRecoveryResult:
    """Explicitly re-parse one failed-parse document, attributably and bounded.

    This is the recovery ordinary re-ingest cannot do, because identical bytes make
    it a no-op. It refuses a document under a sealed location (the frozen population
    is never altered), refuses to rewrite a successfully parsed document, and only
    re-parses one whose parse failed — preserving the original file and, through the
    append-only receipt, the prior failure history. A genuinely re-parsed document
    is thereby made eligible for the standing project-processing pass; a document
    that fails again stays visibly failed.
    """

    principal = require_human_principal(principal)
    document = session.get(Document, document_id)
    if document is None:
        raise LocationDiscoveryRefused(
            "unknown_document", f"document {document_id} does not exist"
        )
    if _project_location_is_sealed(session, document.project_id):
        return ParseRecoveryResult(
            document_id=document_id,
            outcome="refused_sealed",
            pages=document.pages or 0,
            reason="the document's location is a sealed holdout and is never re-read",
        )
    if document.parse_status == "parsed":
        return ParseRecoveryResult(
            document_id=document_id,
            outcome="skipped_parsed",
            pages=document.pages or 0,
            reason="a successfully parsed document is never rewritten as a retry",
        )
    if document.parse_status != "failed":
        return ParseRecoveryResult(
            document_id=document_id,
            outcome="skipped_not_failed",
            pages=document.pages or 0,
            reason=f"parse status is {document.parse_status}, not failed",
        )
    path = stored_file(document)
    if path is None:
        raise LocationDiscoveryRefused(
            "bytes_missing", "the original file is not in the content-addressed store"
        )
    resolved_images = Path(images_dir) if images_dir is not None else Path(settings.corpus_images)
    recovered = reparse_document(
        session, document=document, path=path, images_dir=resolved_images
    )
    audit.record(
        session,
        principal=principal,
        action=audit.RECOVER_DOCUMENT_PARSE,
        entity_type=audit.DOCUMENT,
        entity_id=document_id,
        after={
            "outcome": "recovered" if recovered else "still_failed",
            "pages": document.pages or 0,
            "sha256": document.sha256,
        },
    )
    return ParseRecoveryResult(
        document_id=document_id,
        outcome="recovered" if recovered else "still_failed",
        pages=document.pages or 0,
    )


def _project_location_is_sealed(session: Session, project_id: int) -> bool:
    """Whether any enabled location-discovery schedule for the project is sealed.

    Read from the same server-owned schedule the scheduled pass reads, so a
    recovery command honors the exact sealed exclusion a scheduled scope does,
    without importing the Due Work module.
    """

    schedules = session.scalars(
        select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == project_id,
            DueWorkSchedule.handler_key == HANDLER_KEY,
            DueWorkSchedule.disabled_at.is_(None),
        )
    ).all()
    return any(bool(schedule.scope_json.get("sealed")) for schedule in schedules)


# --- Operations visibility --------------------------------------------------


@dataclass(frozen=True)
class OperationsItem:
    """One operations item, its reason, and the one permitted next step."""

    kind: str
    identity: str
    reason: str
    permitted_next_step: str
    document_id: int | None = None


@dataclass(frozen=True)
class OperationsView:
    """Everything a technical operator must keep visible for one project (#350)."""

    proposed_intake: tuple[OperationsItem, ...]
    authorized_pending: tuple[OperationsItem, ...]
    open_drifts: tuple[OperationsItem, ...]
    fetch_failures: tuple[OperationsItem, ...]
    budget_exhaustions: tuple[OperationsItem, ...]
    held_documents: tuple[OperationsItem, ...]


def operations_view(session: Session, project_id: int) -> OperationsView:
    """Project every unresolved operations case with its reason and next step.

    Reuses the existing durable state rather than inventing a parallel one: held
    (quarantined), failed-parse, and permanently unreadable/no-matrix documents are
    surfaced from the records that already carry them, each with the one permitted
    next step. Nothing here relabels a schedule as a matrix, overrides a hold, or
    retries paid extraction; those next steps are descriptions, not actions.
    """

    proposed = tuple(
        OperationsItem(
            kind="proposed_intake",
            identity=row.source_url,
            reason="observed at the location; not yet authorized for processing",
            permitted_next_step=(
                "authorize with a declared document kind, or leave it proposed"
            ),
        )
        for row in session.scalars(
            select(DiscoveredReference)
            .where(
                DiscoveredReference.project_id == project_id,
                DiscoveredReference.state == "proposed",
            )
            .order_by(DiscoveredReference.id)
        ).all()
    )
    latest_attempts = _latest_attempt_per_reference(session, project_id)
    drift_held = {
        attempt.reference_key
        for attempt in latest_attempts
        if attempt.outcome == "drift" and attempt.reference_key is not None
    }
    authorized_pending = tuple(
        OperationsItem(
            kind="authorized_pending",
            identity=row.source_url,
            reason="authorized; awaiting the next discovery/fetch pass",
            permitted_next_step="the scheduled pass will fetch and register it",
        )
        for row in session.scalars(
            select(DiscoveredReference)
            .where(
                DiscoveredReference.project_id == project_id,
                DiscoveredReference.state == "authorized",
            )
            .order_by(DiscoveredReference.id)
        ).all()
        if row.reference_key not in drift_held
    )

    open_drifts: list[OperationsItem] = []
    fetch_failures: list[OperationsItem] = []
    budget_exhaustions: list[OperationsItem] = []
    for attempt in latest_attempts:
        if attempt.outcome == "drift":
            open_drifts.append(
                OperationsItem(
                    kind="drift",
                    identity=attempt.reference_key or attempt.resolved_url or "",
                    reason=(
                        "changed checksum under an existing registry identity; "
                        "the registered document was not overwritten"
                    ),
                    permitted_next_step=(
                        "resolve as a declared Supersession or dismiss — never an "
                        "automatic overwrite or rendition"
                    ),
                    document_id=attempt.document_id,
                )
            )
        elif attempt.outcome == "failed":
            fetch_failures.append(
                OperationsItem(
                    kind="fetch_failure",
                    identity=attempt.reference_key or attempt.resolved_url or "",
                    reason=attempt.reason or "the fetch failed",
                    permitted_next_step="inspect the source; the pass will retry",
                )
            )
        elif attempt.outcome == "budget_exhausted":
            budget_exhaustions.append(
                OperationsItem(
                    kind="budget_exhausted",
                    identity=attempt.reference_key or "pass",
                    reason=attempt.reason or "a processing budget was exhausted",
                    permitted_next_step="re-run the pass to resume the remaining work",
                )
            )

    held = _held_documents(session, project_id)
    return OperationsView(
        proposed_intake=proposed,
        authorized_pending=authorized_pending,
        open_drifts=tuple(open_drifts),
        fetch_failures=tuple(fetch_failures),
        budget_exhaustions=tuple(budget_exhaustions),
        held_documents=held,
    )


def _latest_attempt_per_reference(
    session: Session, project_id: int
) -> list[SourceFetchAttempt]:
    """The most recent fetch attempt for each reference key, newest kept."""

    attempts = session.scalars(
        select(SourceFetchAttempt)
        .where(SourceFetchAttempt.project_id == project_id)
        .order_by(SourceFetchAttempt.id.desc())
    ).all()
    latest: dict[str | None, SourceFetchAttempt] = {}
    for attempt in attempts:
        marker = attempt.reference_key or f"pass:{attempt.id}"
        if marker not in latest:
            latest[marker] = attempt
    return list(latest.values())


def _held_documents(session: Session, project_id: int) -> tuple[OperationsItem, ...]:
    """Held, failed-parse, and permanently unreadable documents, kept visible."""

    items: list[OperationsItem] = []
    quarantined = {
        row.document_id: row.reason
        for row in session.scalars(
            select(DocumentQuarantine)
            .join(Document, Document.id == DocumentQuarantine.document_id)
            .where(Document.project_id == project_id)
        ).all()
    }
    permanent = {
        row.document_id: row.outcome
        for row in session.scalars(
            select(ExtractionRun)
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(
                Document.project_id == project_id,
                ExtractionRun.outcome.in_(("unreadable", "no_matrix")),
            )
        ).all()
    }
    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.id)
    ).all()
    for document in documents:
        if document.id in quarantined:
            items.append(
                OperationsItem(
                    kind="held_quarantined",
                    identity=document.filename,
                    reason=quarantined[document.id],
                    permitted_next_step=(
                        "held; its sequencing is deliberately unread and not a matrix"
                    ),
                    document_id=document.id,
                )
            )
        elif document.parse_status == "failed":
            items.append(
                OperationsItem(
                    kind="parse_failed",
                    identity=document.filename,
                    reason="the file could not be parsed on registration",
                    permitted_next_step="run the bounded parse recovery",
                    document_id=document.id,
                )
            )
        elif document.id in permanent:
            items.append(
                OperationsItem(
                    kind=permanent[document.id],
                    identity=document.filename,
                    reason=f"the reader returned {permanent[document.id]}",
                    permitted_next_step=(
                        "inspect; paid extraction is not retried outside its own policy"
                    ),
                    document_id=document.id,
                )
            )
    return tuple(items)


# --- Small shared helpers ---------------------------------------------------


def _fetch_index(
    session: Session,
    scope: LocationScope,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: "_Counters",
    *,
    now: datetime,
) -> tuple[tuple[IndexEntry, ...], bool] | None:
    """Fetch and parse the index, or record a failure and return None."""

    def fail(reason: str, failure: _FetchFailure) -> None:
        _record_attempt(
            session,
            scope,
            reference_key=None,
            attempted_at=now,
            outcome="failed",
            reason=f"index_fetch/{reason}: {failure.message}",
            resolved_url=failure.resolved_url,
            http_status=failure.http_status,
            resumable=True,
        )

    if scope.adapter_identity == "txdot-rid-box-v1":
        discovered = _fetch_txdot_box_index(scope, budgets, client, counters)
        if isinstance(discovered, _FetchFailure):
            fail(discovered.reason, discovered)
            return None
        entries = discovered
        truncated = len(entries) > budgets.max_references
        return entries[: budgets.max_references], truncated

    if scope.adapter_identity != "http-index-v1":
        fail(
            "unsupported_adapter",
            _FetchFailure(
                "unsupported_adapter",
                f"adapter {scope.adapter_identity!r} is not installed",
                resolved_url=scope.index_url,
            ),
        )
        return None

    got = _authorized_get(scope, scope.index_url, budgets, client, counters)
    if isinstance(got, _FetchFailure):
        fail(got.reason, got)
        return None
    response, resolved_url = got
    if response.status_code != 200 or (
        response.headers.get("content-type", "").lower().startswith("text/html")
    ):
        _record_attempt(
            session,
            scope,
            reference_key=None,
            attempted_at=now,
            outcome="failed",
            reason=f"index_fetch: unusable response {response.status_code}",
            resolved_url=resolved_url,
            http_status=response.status_code,
            resumable=True,
        )
        return None
    try:
        entries = parse_index(response.content)
    except LocationDiscoveryRefused as exc:
        fail(
            exc.reason,
            _FetchFailure(
                exc.reason,
                str(exc),
                resolved_url=resolved_url,
                http_status=200,
            ),
        )
        return None
    truncated = len(entries) > budgets.max_references
    return entries[: budgets.max_references], truncated


def _fetch_txdot_box_index(
    scope: LocationScope,
    budgets: DiscoveryBudgets,
    client: httpx.Client,
    counters: _Counters,
) -> tuple[IndexEntry, ...] | _FetchFailure:
    """Resolve one exact RID link and enumerate its public Box ZIP."""

    got_rid = _authorized_get(
        scope,
        scope.index_url,
        budgets,
        client,
        counters,
        headers={"User-Agent": _BROWSER_USER_AGENT},
    )
    if isinstance(got_rid, _FetchFailure):
        return got_rid
    rid_response, rid_url = got_rid
    if rid_response.status_code != 200:
        return _FetchFailure(
            "rid_http_status",
            f"TxDOT RID page returned {rid_response.status_code}",
            resolved_url=rid_url,
            http_status=rid_response.status_code,
        )
    try:
        share_url = parse_txdot_rid_box_link(
            rid_response.content,
            index_url=rid_url,
            link_text=scope.rid_link_text or "",
        )
    except LocationDiscoveryRefused as exc:
        return _FetchFailure(
            exc.reason,
            str(exc),
            resolved_url=rid_url,
            http_status=200,
        )

    got_share = _authorized_get(
        scope,
        share_url,
        budgets,
        client,
        counters,
        headers={"User-Agent": _BROWSER_USER_AGENT},
    )
    if isinstance(got_share, _FetchFailure):
        return got_share
    share_response, resolved_share_url = got_share
    if share_response.status_code != 200:
        return _FetchFailure(
            "box_http_status",
            f"Box shared-file page returned {share_response.status_code}",
            resolved_url=resolved_share_url,
            http_status=share_response.status_code,
        )
    try:
        shared = parse_box_shared_file(
            share_response.content,
            shared_url=resolved_share_url,
        )
    except LocationDiscoveryRefused as exc:
        return _FetchFailure(
            exc.reason,
            str(exc),
            resolved_url=resolved_share_url,
            http_status=200,
        )

    opened = _open_remote_zip(
        scope,
        shared.archive_url,
        budgets,
        client,
        counters,
    )
    if isinstance(opened, _FetchFailure):
        return opened
    return tuple(
        IndexEntry(
            url=shared.archive_url,
            member=info.filename,
            title=PurePosixPath(info.filename).name,
        )
        for info in opened.archive.infolist()
        if not info.is_dir()
    )


def _pass_budget_exhausted(
    session_factory, scope: LocationScope, clock, which: str, key: str
) -> None:
    """Retain a resumable Processing Failure when a pass-level budget is hit."""

    with session_factory() as session:
        with session.begin():
            _record_attempt(
                session,
                scope,
                reference_key=key,
                attempted_at=_aware(clock.now()),
                outcome="budget_exhausted",
                reason=f"{which}: the bounded pass stopped with work remaining",
                resumable=True,
            )


def _record_attempt(
    session: Session,
    scope: LocationScope,
    *,
    reference_key: str | None,
    attempted_at: datetime,
    outcome: str,
    reason: str | None = None,
    resolved_url: str | None = None,
    http_status: int | None = None,
    byte_count: int | None = None,
    content_type: str | None = None,
    sha256: str | None = None,
    prior_sha256: str | None = None,
    document_id: int | None = None,
    resumable: bool = False,
) -> SourceFetchAttempt:
    attempt = SourceFetchAttempt(
        project_id=scope.project_id,
        reference_key=reference_key,
        location_id=scope.location_id,
        attempted_at=_aware(attempted_at),
        outcome=outcome,
        resolved_url=resolved_url,
        http_status=http_status,
        content_type=content_type,
        byte_count=byte_count,
        sha256=sha256,
        prior_sha256=prior_sha256,
        document_id=document_id,
        reason=reason,
        resumable=resumable,
    )
    session.add(attempt)
    session.flush()
    return attempt


def _reference_filename(row: DiscoveredReference) -> str:
    """The display name for a reference's bytes, from its member or URL basename."""

    if row.member:
        leaf = row.member.split("::")[-1]
        name = PurePosixPath(leaf).name
    else:
        name = PurePosixPath(urlparse(row.source_url).path).name
    return name or row.reference_key


def _elapsed_seconds(clock, started: datetime) -> float:
    return (_aware(clock.now()) - started).total_seconds()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
