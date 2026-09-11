"""Handing back the exact retained bytes of one prepared or approved issue (#830).

#529 retains every candidate artifact in the content-addressed store and #533
seals the same bytes into an immutable package receipt, and neither had a way
out. The Issue section printed the artifact type, the renderer and the digest
and stopped there, and the only downloads in the application were the legacy
single-report family (ADR-0040) — outside the live pilot surface, and part of
the release model ADR-0086 replaced. A package digest is not a customer
deliverable.

This module is only the presentation half. The bytes come from
``release_authorization``'s retrieval functions, which resolve the storage key
themselves and verify the digest on the way out; nothing here reads an object,
re-renders anything, or performs a second integrity check that could disagree
with the store's.

**The browser never names a location.** A request carries a project slug, a
candidate id or an issue number, and an artifact type from the closed
vocabulary the issue content publishes. There is no parameter here or on the
routes above for a storage key, a digest or a path, so no identifier a caller
can supply reaches bytes outside the candidate or package the project owns.

**The filename is built, never echoed.** ``download_name`` composes the name
out of parts it sanitizes itself: every character outside ``a-z0-9`` becomes a
hyphen, so a retained slug or artifact type carrying a path separator, a
quote, a newline or a percent escape cannot arrive in the
``Content-Disposition`` header as one, and cannot climb out of the browser's
download directory. The suffix is looked up in #529's own
``ARTIFACT_SUFFIXES`` and is ``.bin`` for a type that has none, so an
unrecognized type is named as opaque rather than as whatever it claims.

**The media type is declared, never sniffed.** Each suffix maps to one
non-executable type, an unknown suffix is an opaque byte stream, and every
response carries ``nosniff`` and ``attachment`` so a browser cannot decide for
itself to render a retained file as a document.

**A bundle is the whole set or nothing.** ``bundle_bytes`` is handed the
complete set that ``retrieve_released_package`` has already read and verified,
and only then writes an archive; there is no path through this module that
writes an entry before the last member has verified. The archive is written
with a fixed entry timestamp, so the same package bundles to the same bytes
however long after its approval it is asked for.

Terminology: nothing here coins a word. The artifact words a person reads come
from ``issue_content.ARTIFACT_WORDS``; these names are file names.
"""

from __future__ import annotations

from io import BytesIO
import re
from urllib.parse import quote
import zipfile

from starlette.responses import Response

from corridor.release_authorization import SealedArtifact
from corridor.release_candidate import ARTIFACT_SUFFIXES


#: What a download is of. Part of the file name, so a saved candidate artifact
#: and a saved approved one are told apart in a downloads folder.
CANDIDATE_DOWNLOAD = "candidate"
ISSUE_DOWNLOAD = "issue"

#: The extension every retained artifact has no entry for.
UNKNOWN_SUFFIX = ".bin"

BUNDLE_SUFFIX = ".zip"
BUNDLE_MEDIA_TYPE = "application/zip"

#: One non-executable media type per retained suffix. A suffix with no entry
#: is served as an opaque byte stream rather than guessed at.
MEDIA_TYPES: dict[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    BUNDLE_SUFFIX: BUNDLE_MEDIA_TYPE,
}

OPAQUE_MEDIA_TYPE = "application/octet-stream"

_UNSAFE = re.compile(r"[^a-z0-9]+")


def _safe(value: str) -> str:
    """One name part with nothing in it a file system or a header can read."""

    return _UNSAFE.sub("-", (value or "").lower()).strip("-")


def download_name(
    *,
    project_slug: str,
    kind: str,
    number: int,
    artifact_type: str | None = None,
) -> str:
    """The file this download lands as, from sanitized parts only.

    ``artifact_type`` of ``None`` names the whole approved package, which is
    the bundle. ``number`` is the candidate's row id or the issue's number in
    the project's release chain, and is rendered from an ``int`` so it cannot
    carry anything at all.
    """

    if artifact_type is None:
        suffix = BUNDLE_SUFFIX
        last = "package"
    else:
        suffix = ARTIFACT_SUFFIXES.get(artifact_type, UNKNOWN_SUFFIX)
        last = _safe(artifact_type) or "artifact"
    parts = [_safe(project_slug), _safe(kind), str(int(number)), last]
    return "-".join(part for part in parts if part) + suffix


def member_name(artifact_type: str) -> str:
    """The name one artifact keeps inside a bundle."""

    suffix = ARTIFACT_SUFFIXES.get(artifact_type, UNKNOWN_SUFFIX)
    return (_safe(artifact_type) or "artifact") + suffix


def bundle_bytes(members: tuple[tuple[SealedArtifact, bytes], ...]) -> bytes:
    """One archive of an already-read, already-verified complete set.

    The entry timestamp is fixed rather than taken from a clock, so the same
    package bundles to the same bytes whenever it is asked for — the archive
    is as retrievable as the artifacts inside it (ADR-0086 as amended by
    ADR-0091).
    """

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for artifact, data in members:
            entry = zipfile.ZipInfo(
                member_name(artifact.artifact_type), date_time=(1980, 1, 1, 0, 0, 0)
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    return buffer.getvalue()


def download_response(data: bytes, *, filename: str) -> Response:
    """Fixed bytes under a built name, as an attachment a browser will not run."""

    suffix = filename[filename.rfind(".") :] if "." in filename else ""
    return Response(
        content=data,
        media_type=MEDIA_TYPES.get(suffix, OPAQUE_MEDIA_TYPE),
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
