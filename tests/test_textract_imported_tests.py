"""The Textract rung's own tests, imported unchanged with it and collected here (#732).

`src/corridor_pdf_reader/textract/tests/` is commit c39363e's test suite for
the block mapping, the cached and retried client, the lane A glyph re-map,
the render frame, the transport and a run in the harness layout. Every test
runs against a fake service or a file URL: none reaches a network, and the
client's own tests are the record that its cache is read before every call.
Corridor collects tests from `tests/` only and shards them by file, so the
six modules are re-exported into this one, as `bootstrap/tests` are; the
guard below fails if a test name in one module ever shadowed another's.
"""

from __future__ import annotations

from corridor_pdf_reader.textract.tests import (
    test_blocks,
    test_client,
    test_read,
    test_remap,
    test_render,
    test_transport,
)
from corridor_pdf_reader.textract.tests.test_blocks import *  # noqa: F401,F403
from corridor_pdf_reader.textract.tests.test_client import *  # noqa: F401,F403
from corridor_pdf_reader.textract.tests.test_read import *  # noqa: F401,F403
from corridor_pdf_reader.textract.tests.test_remap import *  # noqa: F401,F403
from corridor_pdf_reader.textract.tests.test_render import *  # noqa: F401,F403
from corridor_pdf_reader.textract.tests.test_transport import *  # noqa: F401,F403

IMPORTED = (test_blocks, test_client, test_read, test_remap, test_render, test_transport)


def _tests(module) -> set[str]:
    return {name for name in vars(module) if name.startswith("test_")}


def test_the_six_imported_modules_share_no_test_name():
    names = [_tests(module) for module in IMPORTED]
    assert sum(len(group) for group in names) == len(set().union(*names)) == 33
