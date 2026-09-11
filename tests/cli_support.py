"""Read one command's JSON result, for the tests that drive a command line.

The commands these tests run print one JSON object on stdout and nothing on
stderr, and five CLI test modules each declared the same reader of that pair.
The empty-stderr assertion is the half worth stating once: a command that
starts writing diagnostics to stderr should fail, not depend on which of five
copies a test happened to call.
"""

from __future__ import annotations

import json


def json_output(capsys) -> dict:
    """The JSON object a command printed, having printed nothing else."""

    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)
