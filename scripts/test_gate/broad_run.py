"""Who may run the whole local suite, stated once.

`CLAUDE.md` blocks local full-suite execution by default and names exactly two
diagnostic reasons as the only exceptions. That one rule used to be written
three times along one chain: the Makefile threaded `LOCAL_BROAD_REASON` into
the wrapper, `run_local_tests.py` listed the reasons and the environment
variable it hands its child, and `tests/conftest.py` listed the same variable
and the same two reasons again with no import between them. `make test-timing`
then supplied the authorization itself and called pytest directly, so the
complete non-slow suite ran with no timeout, no process-group cleanup and no
`out/test-results` receipt.

The vocabulary, the variable name and the predicate that recognizes a
whole-suite invocation live here. The wrapper and the collector both consult
this module, so a new reason edits one list and no caller can restate the
policy in a form that admits itself. Standard library only, like every other
module in this package.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path


# The variable the wrapper sets on its child and the collector reads back.
DIAGNOSTIC_ENV = "CORRIDOR_LOCAL_BROAD_REASON"

# The only two reasons CLAUDE.md admits: an observed failure that focused tests
# cannot reproduce, and an explicitly requested suite-performance diagnosis.
DIAGNOSTIC_REASONS = ("failure-reproduction", "performance-investigation")


def authorized(environ: Mapping[str, str]) -> bool:
    """True when this process already carries one of the admitted reasons."""
    return environ.get(DIAGNOSTIC_ENV) in DIAGNOSTIC_REASONS


def selects_whole_suite(paths: Iterable[Path], test_root: Path) -> bool:
    """True when these collection paths reach every test file in the tree.

    Naming the directory selects it, and so does naming every file in it: a
    run that lists all of them is the broad suite under another spelling.
    """
    resolved = {path.resolve() for path in paths}
    root = test_root.resolve()
    if any(path == root or path in root.parents for path in resolved):
        return True
    files = {path.resolve() for path in root.glob("test_*.py")}
    return bool(files) and files <= resolved
