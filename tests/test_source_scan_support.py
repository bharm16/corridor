"""Static guards share work while observing changed contents and file lists."""

import ast
import os
from pathlib import Path

import pytest

import source_scan_support
from source_scan_support import python_files, read_python, source_scan_cache  # noqa: F401


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
