"""The imported reader and Textract rung are commit c39363e, unchanged, and nothing in production reaches them (#729, #732).

Three claims the ticket makes are checked here mechanically rather than in
prose: every imported file (the reader, the harness and, since #732, the
Textract rung) still hashes to the blob git recorded for it once the one
import-prefix rewrite is undone; no module under `src/corridor` (or
the render worker) imports the package, so no production path can call PDFium
through it before #447's selection; and the configuration the package
declares is the one loop-020 measured.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import tomllib

import pytest
from test_architecture import PYMUPDF_PACKAGES, TESSERACT_PACKAGES

from corridor_pdf_reader import provenance

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "corridor_pdf_reader"
PRODUCTION_ROOTS = (REPO_ROOT / "src" / "corridor", REPO_ROOT / "workers" / "render")

IMPORT_LINE = re.compile(rb"^[ \t]*from (corridor_pdf_reader\.)?(replacement|bootstrap|textract)[.\s]")


def test_every_imported_file_matches_commit_c39363e():
    manifest = provenance.load_manifest()

    assert len(manifest) == 101
    assert provenance.SOURCE_COMMIT == "c39363e26c2726b61c4e707f589093c67173e538"
    assert provenance.verify() == []


def test_the_only_textual_change_is_the_import_prefix():
    """Line by line: every differing line is an import statement, and the
    syntax trees differ in nothing but those module names."""

    rewritten = [entry for entry in provenance.load_manifest() if entry.rewritten_imports]
    assert len(rewritten) == 26

    for entry in rewritten:
        current = (PACKAGE_ROOT / entry.path).read_bytes()
        original = entry.original_bytes()
        assert provenance.rewrite_imports(original) == current, entry.path
        old_lines, new_lines = original.splitlines(), current.splitlines()
        assert len(old_lines) == len(new_lines), entry.path
        changed = [
            (old, new)
            for old, new in zip(old_lines, new_lines, strict=True)
            if old != new
        ]
        assert changed, entry.path
        assert all(
            IMPORT_LINE.match(old) and IMPORT_LINE.match(new) for old, new in changed
        ), (entry.path, changed)

        before = list(ast.walk(ast.parse(original)))
        after = list(ast.walk(ast.parse(current)))
        assert len(before) == len(after), entry.path
        for old, new in zip(before, after, strict=True):
            assert type(old) is type(new), entry.path
            if isinstance(old, ast.ImportFrom):
                assert isinstance(new, ast.ImportFrom)
                assert new.module in (old.module, f"corridor_pdf_reader.{old.module}"), (
                    entry.path,
                    old.module,
                    new.module,
                )
                assert [alias.name for alias in old.names] == [alias.name for alias in new.names]


def test_files_the_manifest_leaves_unrewritten_are_byte_identical():
    for entry in provenance.load_manifest():
        if entry.rewritten_imports or entry.appended:
            continue
        data = (PACKAGE_ROOT / entry.path).read_bytes()

        assert hashlib.sha256(data).hexdigest() == entry.sha256, entry.path
        assert provenance.git_blob_sha1(data) == entry.blob_sha1, entry.path


def test_the_loop_log_only_grows():
    """ADR-0008: holdout access is appended to the experiment log, never edited into it."""

    entry = next(e for e in provenance.load_manifest() if e.path == "bootstrap/LOOP-LOG.md")
    data = (PACKAGE_ROOT / entry.path).read_bytes()

    assert entry.appended
    assert provenance.git_blob_sha1(data[: entry.size]) == entry.blob_sha1


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# The production modules that enter PDFium through the package's execution
# contract, each with the reason it needs glyph geometry no pure-Python reader
# supplies (#740). None of them produces a PDF fact for the record, and none of
# them runs the reader's own algorithm: selection for the record stays with
# #447. A module leaves this table the day it stops importing the package.
PRODUCTION_IMPORTERS = {
    "src/corridor/web/queue.py": (
        "quote highlights on the review page are PDFium text-search boxes under "
        "pdfium_entry; best effort, degrading to an unmarked page"
    ),
}


def test_no_production_module_imports_the_reader_package():
    """No production selection happens here (#729, #447).

    A static rule rather than a runtime one: the package holds PDFium, which
    must not be entered by any threaded extraction worker, and the parent
    ticket keeps "we imported it" apart from "it is approved for production".
    The modules in ``PRODUCTION_IMPORTERS`` are the exact exceptions, each
    recorded with its reason; the rule fails on any other importer and on a
    listed module that no longer imports the package.
    """

    offenders = []
    for root in PRODUCTION_ROOTS:
        for path in sorted(root.rglob("*.py")):
            names = _imported_names(path)
            if any(name == "corridor_pdf_reader" or name.startswith("corridor_pdf_reader.") for name in names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
            source = path.read_text(encoding="utf-8")
            if "import_module(" in source and "corridor_pdf_reader" in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)} (dynamic import)")

    assert offenders == sorted(PRODUCTION_IMPORTERS)


def test_the_package_never_imports_pymupdf_or_tesseract():
    # The engine names come from the architecture guard so the two checks
    # cannot drift, and so this file names no engine in a literal that the
    # guard would count as a use of it.
    forbidden = set(PYMUPDF_PACKAGES) | set(TESSERACT_PACKAGES)
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        names = {name.split(".")[0] for name in _imported_names(path)}
        if names & forbidden:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_the_declared_engines_are_the_measured_versions():
    """Corridor's pins, the imported reader's pins, and the installed wheels agree."""

    corridor = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    reader = tomllib.loads(
        (PACKAGE_ROOT / "replacement" / "pyproject.toml").read_text(encoding="utf-8")
    )
    measured = {"pypdfium2": "5.13.0", "pypdf": "6.17.0", "pdf-oxide": "0.3.77", "pillow": "12.3.0"}

    for name, version in measured.items():
        assert f"{name}=={version}" in reader["project"]["dependencies"], name
        assert importlib.metadata.version(name) == version, name
    for name in ("pypdfium2", "pypdf", "pdf-oxide"):
        assert f"{name}=={measured[name]}" in corridor["project"]["dependencies"], name
    assert "xlrd==2.0.2" in corridor["dependency-groups"]["pdf-reader-experiment"]
    assert "pdf-reader-experiment" not in corridor.get("tool", {}).get("uv", {}).get(
        "default-groups", []
    )


def test_the_imported_defaults_are_the_measured_ones():
    """Engine `tagged`, 36 dpi, four jobs, no rescue variant anywhere in the reader."""

    read = (PACKAGE_ROOT / "bootstrap" / "read.py").read_text(encoding="utf-8")
    assert "dpi=36, retain_images=False" in read
    assert 'parser.add_argument("--jobs", type=int, default=4)' in read

    receipts = json.loads(
        (PACKAGE_ROOT / "receipts" / "loop-020" / "read-receipts.json").read_text(encoding="utf-8")
    )
    assert receipts["engine"] == "tagged"

    mentions = [
        str(path.relative_to(PACKAGE_ROOT))
        for directory in ("replacement", "bootstrap")
        for path in sorted((PACKAGE_ROOT / directory).rglob("*.py"))
        if "rescue" in path.read_text(encoding="utf-8").lower()
    ]
    assert mentions == []


def test_the_fixture_pdfs_match_their_declared_digests():
    fixtures = json.loads((PACKAGE_ROOT / "corpus" / "fixtures.json").read_text(encoding="utf-8"))

    assert len(fixtures) == 11
    for fixture in fixtures:
        data = (PACKAGE_ROOT / fixture["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == fixture["sha256"], fixture["id"]


@pytest.mark.parametrize("name", ["package.json", "pnpm-lock.yaml", "package-lock.json"])
def test_the_node_lockfiles_pin_the_same_ssf(name: str):
    """`npm ci` reads the generated package-lock.json; its integrity hashes are
    the imported pnpm-lock.yaml's, so the declared step installs what was measured."""

    directory = PACKAGE_ROOT / "paired_trial"
    text = (directory / name).read_text(encoding="utf-8")
    assert "0.11.2" in text
    if name != "package.json":
        pnpm = (directory / "pnpm-lock.yaml").read_text(encoding="utf-8")
        for integrity in re.findall(r"integrity: (sha512-\S+)}", pnpm):
            assert integrity in text, integrity
