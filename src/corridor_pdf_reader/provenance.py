"""Where the imported reader came from, checkable by machine.

The package claims that `replacement/`, `bootstrap/`, the fixture PDFs and the
answer-key printer's node files are commit c39363e of the standalone
comparison repository, unchanged (#729). A claim like that is worth nothing
as prose, so `source-manifest.json` records the git blob id and the SHA-256
of every imported file as it was archived from that commit, and this module
holds the one textual change the import made and its exact inverse.

The change: the source packages import each other by the absolute names
`replacement` and `bootstrap` (`from replacement.layout import ...`). Inside
Corridor they live under `corridor_pdf_reader`, and making the old names
importable would have meant installing two generic top-level packages or an
import hook that loads one file under two names. Rewriting the import prefix
is the smaller lie, and it is not a lie at all once the parity test can undo
it: `restore_imports` reverses the rewrite byte for byte, and the restored
bytes must hash to the recorded digests. Nothing else in those files differs
from the commit, and `bootstrap/LOOP-LOG.md` is the one file that grows,
because ADR-0008 requires every holdout access to be appended to it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "source-manifest.json"

SOURCE_COMMIT = "c39363e26c2726b61c4e707f589093c67173e538"
SOURCE_REPOSITORY = "pdf-reader-comparison (local, no remote)"
SOURCE_BRANCH = "codex/standalone-comparison"

# The rewrite touches only `from <package>...` statements at the start of a
# line (indented ones inside functions included) whose package is one of the
# two imported ones. `[.\s]` after the name keeps `from bootstrap_x` alone.
_FORWARD = re.compile(
    rb"^(?P<indent>[ \t]*)from (?P<package>replacement|bootstrap)(?P<rest>[.\s])",
    re.M,
)
_REVERSE = re.compile(
    rb"^(?P<indent>[ \t]*)from corridor_pdf_reader\."
    rb"(?P<package>replacement|bootstrap)(?P<rest>[.\s])",
    re.M,
)


def rewrite_imports(source: bytes) -> bytes:
    """The import the package makes: `from replacement.` -> `from corridor_pdf_reader.replacement.`."""
    return _FORWARD.sub(rb"\g<indent>from corridor_pdf_reader.\g<package>\g<rest>", source)


def restore_imports(source: bytes) -> bytes:
    """The exact inverse of `rewrite_imports`."""
    return _REVERSE.sub(rb"\g<indent>from \g<package>\g<rest>", source)


def git_blob_sha1(data: bytes) -> str:
    """The id git gives these bytes, so `git ls-tree` confirms them directly."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SourceFile:
    path: str
    source_path: str
    blob_sha1: str
    sha256: str
    size: int
    rewritten_imports: bool
    appended: bool

    def original_bytes(self, package_root: Path = PACKAGE_ROOT) -> bytes:
        """The file as it was at the commit, recovered from the package copy."""
        data = (package_root / self.path).read_bytes()
        if self.rewritten_imports:
            data = restore_imports(data)
        if self.appended:
            data = data[: self.size]
        return data


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[SourceFile, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document["commit"] != SOURCE_COMMIT:
        raise ValueError(f"manifest names commit {document['commit']}, not {SOURCE_COMMIT}")
    return tuple(SourceFile(**entry) for entry in document["files"])


def verify(package_root: Path = PACKAGE_ROOT) -> list[str]:
    """Every discrepancy between the package and the recorded commit; empty when none."""
    problems = []
    for entry in load_manifest(package_root / MANIFEST_PATH.name):
        target = package_root / entry.path
        if not target.is_file():
            problems.append(f"{entry.path}: missing")
            continue
        original = entry.original_bytes(package_root)
        if len(original) != entry.size:
            problems.append(f"{entry.path}: {len(original)} bytes, {entry.size} recorded")
        if git_blob_sha1(original) != entry.blob_sha1:
            problems.append(f"{entry.path}: blob {git_blob_sha1(original)}, {entry.blob_sha1} recorded")
        if sha256(original) != entry.sha256:
            problems.append(f"{entry.path}: sha256 differs from the commit")
    return problems


def package_digest(package_root: Path = PACKAGE_ROOT) -> str:
    """One digest over the imported files as they are in the package now.

    Receipts record it beside the commit: the commit says what was imported,
    this says what the package holds at the moment of the measurement,
    rewritten imports and appended log included.
    """
    digest = hashlib.sha256()
    for entry in load_manifest(package_root / MANIFEST_PATH.name):
        digest.update(entry.path.encode("utf-8") + b"\0")
        digest.update(sha256((package_root / entry.path).read_bytes()).encode("ascii") + b"\n")
    return digest.hexdigest()
