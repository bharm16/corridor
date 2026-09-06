"""The reader's own tests, imported unchanged with it and collected here (#729).

`src/corridor_pdf_reader/bootstrap/tests/` is commit c39363e's test suite for
the scorer and the semantics tier. Corridor collects tests from `tests/` only
and shards them by file, so the two modules are re-exported into this one
rather than added as a second test root; each test function still runs in
its own module's namespace, and the guard below fails if a name in one module
ever shadowed a test in the other.
"""

from __future__ import annotations

from corridor_pdf_reader.bootstrap.tests import test_score, test_semantics
from corridor_pdf_reader.bootstrap.tests.test_score import *  # noqa: F401,F403
from corridor_pdf_reader.bootstrap.tests.test_semantics import *  # noqa: F401,F403


def _tests(module) -> set[str]:
    return {name for name in vars(module) if name.startswith("test_")}


def test_the_two_imported_modules_share_no_test_name():
    assert _tests(test_score) & _tests(test_semantics) == set()
    assert len(_tests(test_score)) + len(_tests(test_semantics)) == 37
