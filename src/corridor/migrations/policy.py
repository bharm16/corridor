"""The machine-readable migration window (#548, ADR-0065).

#423 consolidated 112 development revisions into a bounded window, and the
guard it left behind was a test that listed the permitted filenames.  A list
of filenames is not a ratchet: every migration-bearing change simply added
its name, and the executable chain grew back to 23 revisions while the test
that was supposed to prevent it kept passing.

This module states the window as data instead, so the guard can assert
against Alembic's revision graph rather than a directory listing, and so
adding a revision is a deliberate edit to a recorded policy rather than an
incidental line in a test.

`UNRELEASED_EDGE_TARGET` is where the window belongs: one supported
transition after the recorded supported revision.  `UNRELEASED_EDGES` is
where it is today.  The guard permits the second only while it does not grow,
so the debt can be paid down but not added to; a change that needs another
revision folds into the current unreleased transition, or consolidates first.
"""

from __future__ import annotations

# The revision a fresh database installs from. It builds the whole schema
# rather than replaying development history.
SCHEMA_BUILDER = "b7d3f9a1c2e5"

# The marker a database already stamped at the released head runs instead of
# the builder, so both paths reach the same schema fingerprint.
COMPATIBILITY_MARKER = "c0a1d0b5e11e"

# The revision every supported database is at or past. Upgrades are proved
# from here, and history before it is not executable.
SUPPORTED_FROM_REVISION = COMPATIBILITY_MARKER

# The single head the executable graph must have.
CURRENT_HEAD = "a1c4e7b0d2f3"

# One supported transition after SUPPORTED_FROM_REVISION. This is the window
# ADR-0065 decided and the shape the graph must return to.
UNRELEASED_EDGE_TARGET = 1

# What the graph carries today. It may fall and must never rise: raising it
# is how the 112-revision chain came back after #423. A change needing
# another revision folds into the current unreleased transition, or
# consolidates the chain first and lowers this number.
UNRELEASED_EDGES = 21
