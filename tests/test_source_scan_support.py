"""Static guards share work while observing changed contents and file lists."""

import ast
import os
from pathlib import Path

import pytest

import source_scan_support
from source_scan_support import (  # noqa: F401
    imported_names,
    importers_of,
    python_files,
    read_python,
    source_scan_cache,
)


def test_repeated_reads_share_one_parse_but_same_metadata_edits_are_reparsed(tmp_path, monkeypatch):
    path = tmp_path / "module.py"
    path.write_text("import first\n", encoding="utf-8")
    metadata = path.stat()
    parse = ast.parse
    calls = []

    def counted_parse(*args, **kwargs):
        calls.append(args[0])
        return parse(*args, **kwargs)

    monkeypatch.setattr(source_scan_support.ast, "parse", counted_parse)
    original = read_python(path)
    assert read_python(path) is original

    path.write_text("import other\n", encoding="utf-8")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    changed = read_python(path)

    assert path.stat().st_size == metadata.st_size
    assert path.stat().st_mtime_ns == metadata.st_mtime_ns
    assert changed is not original
    assert isinstance(changed.tree.body[0], ast.Import)
    assert changed.tree.body[0].names[0].name == "other"
    assert changed.nodes == tuple(ast.walk(changed.tree))
    assert calls == ["import first\n", "import other\n"]

    path.write_text("invalid syntax !!!", encoding="utf-8")
    with pytest.raises(SyntaxError):
        read_python(path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        read_python(path)


def test_file_discovery_is_fresh_and_never_enters_hidden_or_cache_directories(tmp_path, monkeypatch):
    visible = tmp_path / "nested"
    visible.mkdir()
    first = visible / "first.py"
    first.write_text("", encoding="utf-8")
    (visible / ".hidden.py").write_text("", encoding="utf-8")
    excluded = [tmp_path / ".venv", visible / "__pycache__"]
    for directory in excluded:
        directory.mkdir()
        (directory / "ignored.py").write_text("invalid syntax !!!", encoding="utf-8")
    scandir = os.scandir

    def guarded_scandir(path):
        assert Path(path) not in excluded, "prune before traversing installed packages"
        return scandir(path)

    monkeypatch.setattr(source_scan_support.os, "scandir", guarded_scandir)
    assert python_files(tmp_path) == (first,)
    second = tmp_path / "second.py"
    second.write_text("", encoding="utf-8")
    assert python_files(tmp_path) == tuple(sorted((first, second)))
    first.unlink()
    assert python_files(tmp_path) == (second,)


def test_file_discovery_does_not_silently_skip_an_unreadable_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        python_files(tmp_path / "missing")


def test_the_import_scanner_sees_every_form_of_dependency(tmp_path):
    """The graph is only as honest as the scanner behind it (#548 shape).

    `from corridor import x` is how most of this codebase imports a sibling,
    and a scanner that only understood `from corridor.x import y` reported an
    acyclic graph that was not one. The provider-spend guard was written after
    that lesson and still saw two forms, so `from corridor_pdf_reader import
    textract_adapter` reached the Textract adapter from any of the twelve
    already-listed reader importers with both guards green. One scanner is
    what stops each guard learning the lesson separately.
    """

    cases = {
        "from corridor.exceptions import review\n": {"corridor.exceptions", "corridor.exceptions.review"},
        "from corridor import disputes, notifications\n": {"corridor", "corridor.disputes", "corridor.notifications"},
        "import corridor.work_decisions\n": {"corridor.work_decisions"},
        "from corridor.web import app\n": {"corridor.web", "corridor.web.app"},
        "from corridor_pdf_reader import textract_adapter\n": {"corridor_pdf_reader", "corridor_pdf_reader.textract_adapter"},
        "from . import sibling\n": set(),
        "from .relative import name\n": set(),
        "import httpx\n": {"httpx"},
    }
    module = tmp_path / "module.py"

    read: dict[str, set[str]] = {}
    for source in cases:
        module.write_text(source, encoding="utf-8")
        read[source] = {name for name, _ in imported_names(module)}
    assert read == cases

    module.write_text("import first\nfrom second import leaf\n", encoding="utf-8")
    assert imported_names(module) == (
        ("first", 1),
        ("second", 2),
        ("second.leaf", 2),
    )


def test_importers_of_names_each_file_that_reaches_the_prefix_by_any_form(tmp_path):
    """A policy asks which files reach a tree; it never learns an import form."""
    root = tmp_path / "src"
    (root / "nested").mkdir(parents=True)
    (root / "dotted.py").write_text("import pkg.leaf\n", encoding="utf-8")
    (root / "nested" / "from_package.py").write_text(
        "from pkg import leaf\n", encoding="utf-8"
    )
    (root / "nested" / "from_module.py").write_text(
        "from pkg.leaf import symbol\n", encoding="utf-8"
    )
    (root / "unrelated.py").write_text("import pkgother\nimport pkg\n", encoding="utf-8")

    assert importers_of("pkg.leaf", (root,)) == {
        root / "dotted.py": (("pkg.leaf", 1),),
        root / "nested" / "from_module.py": (("pkg.leaf", 1), ("pkg.leaf.symbol", 1)),
        root / "nested" / "from_package.py": (("pkg.leaf", 1),),
    }
    assert importers_of("pkg", (root,)).keys() == {
        root / "dotted.py",
        root / "nested" / "from_module.py",
        root / "nested" / "from_package.py",
        root / "unrelated.py",
    }
    assert importers_of("absent", (root,)) == {}
