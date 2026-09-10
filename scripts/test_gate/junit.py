"""One reading of a pytest JUnit report.

Three readers existed. `test_timing.py` fell back to `classname.replace(".",
"/")`, which turned a test class into a directory, and counted skipped cases;
the gate matched the class name against the top-level test module, refused a
case outside its assigned partition and left skipped work out; the
engine-absent harness summed test-suite attributes. So `make test-timing
--write` and the CI receipt disagreed about the same run. The gate's rule is
the strictest and the one required CI proves, so it is the one rule here: a
case must name a top-level test file in the partition it was asked to measure,
and only executed work counts.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from xml.etree import ElementTree


def measured_cases(junit: Path, assigned: list[str]) -> tuple[int, dict[str, float]]:
    """Executed case count and per-file seconds, zero for every unexercised file."""
    totals = dict.fromkeys(assigned, 0.0)
    count = 0
    for case in ElementTree.parse(junit).iter("testcase"):
        name = case.get("file")
        if name is None:
            match = re.match(r"tests\.(test_[^.]+)(?:\.|$)", case.get("classname", ""))
            if match is None:
                raise ValueError("JUnit case does not name a top-level test file")
            name = f"tests/{match[1]}.py"
        if name not in totals:
            raise ValueError(f"JUnit case is outside its assigned partition: {name}")
        if case.find("skipped") is not None:
            continue
        seconds = float(case.get("time", "0"))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("JUnit duration must be finite and nonnegative")
        totals[name] += seconds
        count += 1
    return count, totals


def case_counts(junit: Path) -> dict[str, int]:
    """Outcome totals as pytest recorded them on every test suite.

    A collection error appears as a case with no class name, which the
    per-file rule above refuses by design; the engine-absent harness records
    these totals with `--continue-on-collection-errors` precisely to count
    such cases, so it reads the suite attributes rather than the cases.
    """
    suites = ElementTree.parse(junit).getroot()
    return {name: sum(int(suite.get(name, "0")) for suite in suites.iter("testsuite"))
            for name in ("tests", "failures", "errors", "skipped")}
