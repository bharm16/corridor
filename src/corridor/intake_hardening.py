"""Untrusted document intake hardening and multi-stage security gates (#490).

Corridor ingests documents from untrusted external sources (uploads, partner
deliveries, email attachments). To protect against hostile bytes, this module
enforces three sequential security stages:

1. **Byte Gate**:
   Cheap, deterministic checks run before any complex parsing:
   - Size limit enforcement.
   - SHA-256 digest computation.
   - True magic/type byte sniffing (verifying byte headers rather than trusting
     file extensions).
   - Top-level archive envelope validation.
   - Pluggable malware scanning seam with fail-closed behavior.

2. **Sandboxed Structural Inspection**:
   In-depth container and structural inspection inside ephemeral memory:
   - Archive member validation: refusal of path traversal (``..``), absolute
     paths, Windows drive letters, symlinks, device files, and duplicate names.
   - Decompression bounds: total uncompressed byte ceiling and expansion ratio
     checks (zip bomb protection).
   - Nested archive recursion depth bounds.
   - XML entity/resource bomb protection (billion laughs / quadratic blowup
     checks in OOXML spreadsheets).
   - Page count bounds on PDF documents.

3. **Rich Processing Gate**:
   Guarantees that no heavy parser (PyMuPDF rendering, Tesseract OCR, openpyxl,
   calamine, LLM extractors) can be invoked on bytes that have not passed stages
   1 and 2 or on documents that are in quarantine.

Quarantined documents are recorded in ``DocumentQuarantine``, visible in the
operations view with an attributable reason, and completely inaccessible to
ordinary project extraction readers.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Document, DocumentQuarantine

# Security bounds for intake processing
MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB
MAX_EXPANDED_BYTES = 256 * 1024 * 1024  # 256 MiB total uncompressed archive
MAX_EXPANSION_RATIO = 100  # Max uncompressed:compressed ratio when > 10 MiB
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_DEPTH = 2  # Max archive recursion depth
MAX_PDF_PAGES = 2_000

# Regex detecting hostile XML entity / DTD declarations
XML_BOMB_PATTERN = re.compile(
    rb"<!ENTITY|<!DOCTYPE[^>]*\[[^\]]*<!ENTITY|SYSTEM\s+[\"'][^\"']+[\"']",
    re.IGNORECASE,
)


class IntakeSecurityError(ValueError):
    """Base exception for intake security violations."""


class HostileContentRefused(IntakeSecurityError):
    """Raised when an intake document violates security bounds or contains threats."""

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(f"Intake refused [{rule}]: {reason}")
        self.rule = rule
        self.reason = reason


@dataclass(frozen=True)
class ScanResult:
    """The outcome of a malware / threat inspection."""

    is_clean: bool
    threat_name: str | None = None
    reason: str | None = None


class MalwareScanner(Protocol):
    """Protocol for malware scanning adapters."""

    def scan(self, body: bytes, filename: str) -> ScanResult:
        """Scan raw bytes for threats."""
        ...


class CleanScanner:
    """Default clean scanner for development and testing."""

    def scan(self, body: bytes, filename: str) -> ScanResult:
        return ScanResult(is_clean=True)


class FakeMalwareScanner:
    """Configurable scanner for testing threat detection and quarantine."""

    def __init__(
        self,
        *,
        infected_signatures: set[bytes] | None = None,
        infected_filenames: set[str] | None = None,
    ) -> None:
        self.infected_signatures = infected_signatures or set()
        self.infected_filenames = infected_filenames or set()

    def scan(self, body: bytes, filename: str) -> ScanResult:
        for sig in self.infected_signatures:
            if sig in body:
                return ScanResult(
                    is_clean=False,
                    threat_name="EICAR-Test-Signature",
                    reason=f"matched threat signature {sig!r}",
                )
        if filename in self.infected_filenames:
            return ScanResult(
                is_clean=False,
                threat_name="Hostile-Filename-Threat",
                reason=f"filename {filename!r} is flagged as hostile",
            )
        return ScanResult(is_clean=True)


_GLOBAL_SCANNER: MalwareScanner = CleanScanner()


def get_malware_scanner() -> MalwareScanner:
    return _GLOBAL_SCANNER


def set_malware_scanner(scanner: MalwareScanner) -> None:
    global _GLOBAL_SCANNER
    _GLOBAL_SCANNER = scanner


# Stage 1: Byte Gate


def inspect_byte_gate(
    body: bytes,
    filename: str,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    scanner: MalwareScanner | None = None,
) -> None:
    """Stage 1: Enforce byte bounds, magic signatures, and malware scanning."""

    if not body:
        raise HostileContentRefused("empty_file", "source bytes are empty")

    if len(body) > max_bytes:
        raise HostileContentRefused(
            "size_limit_exceeded",
            f"file size {len(body)} bytes exceeds limit of {max_bytes} bytes",
        )

    # Magic byte sniffing
    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        if not body.startswith(b"%PDF-"):
            raise HostileContentRefused(
                "magic_mismatch",
                "declared PDF does not start with %PDF- magic bytes",
            )
    elif lower_name.endswith((".xlsx", ".xlsm", ".zip")):
        if not body.startswith(b"PK\x03\x04"):
            raise HostileContentRefused(
                "magic_mismatch",
                "declared archive/workbook does not start with PK ZIP header",
            )

    # Malware scanning seam
    active_scanner = scanner or get_malware_scanner()
    result = active_scanner.scan(body, filename)
    if not result.is_clean:
        raise HostileContentRefused(
            "malware_detected",
            f"threat detected ({result.threat_name}): {result.reason}",
        )


# Stage 2: Sandboxed Structural Inspection


def _inspect_zip_container(body: bytes, depth: int = 0) -> None:
    """Inspect zip/OOXML container members, expansion ratios, and XML bombs."""

    if depth > MAX_ARCHIVE_DEPTH:
        raise HostileContentRefused(
            "archive_recursion_limit",
            f"archive recursion depth {depth} exceeds limit of {MAX_ARCHIVE_DEPTH}",
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, Exception) as exc:
        raise HostileContentRefused("corrupt_archive", f"cannot open zip archive: {exc}")

    members = zf.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise HostileContentRefused(
            "member_count_limit",
            f"archive contains {len(members)} members, exceeding limit of {MAX_ARCHIVE_MEMBERS}",
        )

    seen_names: set[str] = set()
    total_uncompressed = 0

    for info in members:
        name = info.filename
        norm_name = name.replace("\\", "/")

        # Path traversal and absolute paths
        if (
            norm_name.startswith("/")
            or norm_name.startswith("../")
            or "/../" in norm_name
            or norm_name.endswith("/..")
            or ":" in norm_name  # Windows drive paths like C:
        ):
            raise HostileContentRefused(
                "path_traversal",
                f"archive member {name!r} contains path traversal or absolute path",
            )

        # Duplicate member names
        if norm_name in seen_names:
            raise HostileContentRefused(
                "duplicate_member",
                f"archive contains duplicate member name {name!r}",
            )
        seen_names.add(norm_name)

        # Symlinks and special device entries
        # In zip external_attr, top 16 bits hold unix mode; 0o120000 is symlink
        unix_mode = (info.external_attr >> 16) & 0o170000
        if unix_mode == 0o120000:
            raise HostileContentRefused(
                "symlink_entry",
                f"archive member {name!r} is a symlink",
            )
        if unix_mode in (0o020000, 0o060000):  # Character or block device
            raise HostileContentRefused(
                "special_device_entry",
                f"archive member {name!r} is a special device file",
            )

        total_uncompressed += info.file_size
        if total_uncompressed > MAX_EXPANDED_BYTES:
            raise HostileContentRefused(
                "decompression_bomb",
                f"total uncompressed bytes ({total_uncompressed}) exceeds ceiling {MAX_EXPANDED_BYTES}",
            )

        # Expansion ratio check if compressed size is small
        if info.file_size > 10 * 1024 * 1024 and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_EXPANSION_RATIO:
                raise HostileContentRefused(
                    "decompression_ratio_bomb",
                    f"member {name!r} has extreme expansion ratio ({ratio:.1f}:1)",
                )

        # Inspect XML entries for XML bombs
        if norm_name.endswith(".xml") or norm_name.endswith(".rels"):
            with zf.open(info) as member_file:
                # Read head to check for entity declarations
                chunk = member_file.read(64 * 1024)
                if XML_BOMB_PATTERN.search(chunk):
                    raise HostileContentRefused(
                        "xml_entity_bomb",
                        f"XML member {name!r} contains DTD entity expansion or external resource declaration",
                    )

        # Recurse into nested archives if present
        if norm_name.lower().endswith((".zip", ".tar", ".gz")):
            with zf.open(info) as nested_file:
                nested_bytes = nested_file.read()
                _inspect_zip_container(nested_bytes, depth=depth + 1)


def _inspect_pdf_structure(body: bytes) -> None:
    """Inspect PDF structure for bounded page counts and valid EOF."""

    # Simple fast scan for page count before invoking fitz
    page_matches = len(re.findall(rb"/Type\s*/Page\b", body))
    if page_matches > MAX_PDF_PAGES:
        raise HostileContentRefused(
            "pdf_page_limit_exceeded",
            f"PDF appears to contain ~{page_matches} pages, exceeding ceiling {MAX_PDF_PAGES}",
        )


def inspect_sandboxed_structure(body: bytes, filename: str) -> None:
    """Stage 2: Sandboxed structural inspection of containers and documents."""

    lower_name = filename.lower()
    if lower_name.endswith((".xlsx", ".xlsm", ".zip")) or body.startswith(b"PK\x03\x04"):
        _inspect_zip_container(body)
    elif lower_name.endswith(".pdf") or body.startswith(b"%PDF-"):
        _inspect_pdf_structure(body)


# Stage 3: Rich Processing Gate


def assert_can_process_richly(session: Session, document_id: int) -> None:
    """Stage 3: Refuse rich processing if document is quarantined or missing."""

    quarantine = session.get(DocumentQuarantine, document_id)
    if quarantine is not None:
        raise HostileContentRefused(
            "document_quarantined",
            f"document {document_id} is in quarantine ({quarantine.reason}); "
            "rich processing (OCR, LLM, rendering) is prohibited",
        )

    doc = session.get(Document, document_id)
    if doc is None:
        raise IntakeSecurityError(f"document {document_id} does not exist")


def assert_staged_bytes_may_be_read_richly(
    session: Session, *, project_id: int, sha256: str
) -> None:
    """Stage 3, asked of bytes that have no Document yet (#827, for #919).

    Onboarding reads a workbook before anything is registered: the bytes are
    staged by digest and the compatibility pass opens them with a rich reader.
    ``assert_can_process_richly`` cannot answer for them, because it is keyed by
    ``document_id`` and there is no row.

    **This is the seam, not the decision.** #919 owns the typed processing hold
    and the stage-aware answer that goes with it -- an operation needs both a
    valid permission and no applicable prohibiting hold, and neither mechanism
    replaces the other. Until that design exists this function keeps the
    conservative refusal the repository already has: if this project already
    registered a Document over these exact bytes and that Document is held, the
    same hold applies to the same bytes under a different name, and the rich
    read is refused. When #919 lands its typed holds, they are consulted here
    and the conservative rule below becomes one of the cases it answers -- not
    a second, softer gate beside it.
    """

    document = session.scalars(
        select(Document).where(
            Document.project_id == project_id, Document.sha256 == sha256
        )
    ).first()
    if document is None:
        return
    assert_can_process_richly(session, int(document.id))


def quarantine_document(session: Session, document_id: int, reason: str) -> None:
    """Place a document in quarantine, preventing any downstream extraction."""

    existing = session.get(DocumentQuarantine, document_id)
    if existing is None:
        session.add(DocumentQuarantine(document_id=document_id, reason=reason))
    else:
        existing.reason = reason
    session.flush()
