"""Challenger raw results cannot be derived from another PDF engine."""

import ast
from pathlib import Path

import pytest

HERE = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("adapter", "forbidden"),
    (("pdf_oxide_adapter.py", {"pymupdf", "fitz", "pypdfium2"}),
     ("pdfium_adapter.py", {"pymupdf", "fitz", "pdf_oxide"})),
)
def test_challenger_import_boundary(adapter, forbidden):
    tree = ast.parse((HERE / adapter).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(forbidden)


def test_production_source_never_imports_challengers():
    production = HERE.parents[1] / "src" / "corridor"
    for path in production.glob("*.py"):
        assert "pdf_oxide" not in path.read_text()
        assert "pypdfium2" not in path.read_text()
