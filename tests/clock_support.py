"""One frozen clock for the modules whose tests drive the clock seam.

Many modules take a ``clock`` and call ``clock.now()`` so a test can say what
time it is. Nineteen test modules each answered that with the same five-line
adapter -- sixteen of them byte for byte -- so the seam's contract was written
nineteen times and readable in none of them.

``now()`` returns the instant the clock was built with, and returns it again:
this clock does not move. A test that needs time to advance owns an adapter
that says so, rather than widening this one.
"""

from __future__ import annotations

from datetime import datetime


class ControlledClock:
    """A clock frozen at one instant, for a module that reads one."""

    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value
