"""The replacement's own distribution obligations (#741, ADR-0094).

Retiring one PDF engine only settles the licence question if the engine that
replaces it is itself distributed lawfully. pypdfium2 ships a prebuilt PDFium
binary whose dependencies carry their own notices, and those notices have to
travel with the bytes we hand anyone. `src/corridor_pdf_reader/replacement/
third-party-notices/` retains the exact notice files of the wheels the reader
was measured against, with a manifest recording each file's digest.

Two different obligations live here and they are easy to confuse:

  * The retained set is *provenance*: it says what the measured build carried.
  * The image ships the notices of the binary it actually installs, which is
    a different platform's build, through that wheel's own dist-info.

So these tests hold the retained set intact and hold open the path by which
the notices reach the image. What the built image actually contains is not
knowable from a unit test -- `scripts/audit_image_engines.py` asks the image.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
READER_ROOT = REPO_ROOT / "src" / "corridor_pdf_reader"
NOTICES_ROOT = READER_ROOT / "replacement" / "third-party-notices"
MANIFEST = NOTICES_ROOT / "manifest.json"


def _manifest() -> list[dict[str, object]]:
    return json.loads(MANIFEST.read_text())


def test_every_declared_notice_is_present_with_its_recorded_digest():
    missing: list[str] = []
    altered: list[str] = []
    for package in _manifest():
        for item in package["files"]:
            path = READER_ROOT / item["path"]
            if not path.is_file():
                missing.append(item["path"])
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                altered.append(item["path"])

    assert missing == [], "a declared notice is not in the tree"
    assert altered == [], "a retained notice's bytes changed"


def test_the_manifest_declares_every_retained_notice_file():
    declared = {
        (READER_ROOT / item["path"]).resolve()
        for package in _manifest()
        for item in package["files"]
    }
    present = {
        path.resolve()
        for path in NOTICES_ROOT.rglob("*")
        if path.is_file() and path != MANIFEST
    }

    assert present - declared == set(), (
        "a notice file is retained without a manifest entry, so nothing "
        "records which distribution it belongs to or what its bytes were"
    )


def test_the_pdfium_binary_build_notices_are_retained():
    """The binary's own dependency notices, not merely pypdfium2's licence.

    pypdfium2 is BSD-3-Clause, but the wheel carries a compiled PDFium and
    the libraries linked into it -- FreeType, libjpeg-turbo, libpng, ICU,
    zlib and the rest -- each of which has its own notice. Shipping only the
    wrapper's licence would satisfy nothing.
    """

    pdfium = next(p for p in _manifest() if p["package"] == "pypdfium2")
    build_notices = {
        Path(item["path"]).name
        for item in pdfium["files"]
        if "BUILD_LICENSES" in item["path"]
    }

    assert "pdfium.txt" in build_notices
    assert "pdfium-binaries.txt" in build_notices
    for library in ("freetype", "libpng", "zlib", "icu", "libopenjpeg"):
        assert any(name.startswith(library) for name in build_notices), (
            f"no retained notice for {library}, which is linked into the "
            "PDFium build this product distributes"
        )


def test_the_image_build_copies_the_whole_reader_tree():
    """The notices reach the image as files, not as package metadata.

    The image installs the project editable, so `corridor_pdf_reader` in the
    running container is the copied source tree. A narrower COPY, or an
    ignore rule, would remove the notices from the distribution without
    breaking a single import.
    """

    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "COPY src/ ./src/" in dockerfile

    ignore = REPO_ROOT / ".dockerignore"
    if ignore.exists():
        relative = NOTICES_ROOT.relative_to(REPO_ROOT).as_posix()
        patterns = [
            line.strip()
            for line in ignore.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        excluded = [
            pattern
            for pattern in patterns
            if not pattern.startswith("!")
            and (
                relative.startswith(pattern.rstrip("/*").rstrip("/"))
                or Path(relative).match(pattern)
            )
        ]
        assert excluded == [], (
            f"the build context excludes the notices: {excluded}"
        )


def test_a_built_wheel_would_carry_the_reader_package():
    """The other way the notices could travel, kept viable.

    Nothing today installs Corridor from a wheel, but a `--no-editable` sync
    or a published artifact would, and the notices have to survive that too.
    Hatchling includes a package directory's non-Python files by default;
    an `exclude` or `only-include` rule is what would drop them.
    """

    configuration = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    wheel = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert "src/corridor_pdf_reader" in wheel["packages"]
    assert "exclude" not in wheel
    assert "only-include" not in wheel
