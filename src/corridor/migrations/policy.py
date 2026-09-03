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
SCHEMA_BUILDER = "a1c4e7b0d2f3"

# No compatibility marker is needed: the consolidated builder keeps the
# identifier the previous head already carried, so a database standing there
# is stamped correctly and runs nothing.
COMPATIBILITY_MARKER = None

# The revision every supported database is at or past. Upgrades are proved
# from here, and history before it is not executable.
SUPPORTED_FROM_REVISION = SCHEMA_BUILDER

# The single head the executable graph must have.
CURRENT_HEAD = "b2d5f8a1c4e7"

# One supported transition after SUPPORTED_FROM_REVISION. This is the window
# ADR-0065 decided and the shape the graph must return to.
UNRELEASED_EDGE_TARGET = 1

# What the graph carries today: the one transition the window allows, the
# source-append commands (#492), after the twenty-one unreleased transitions
# before it were consolidated into the builder (#548). It is at
# UNRELEASED_EDGE_TARGET and may rise no further. The next migration-bearing
# change folds into b2d5f8a1c4e7 rather than appending another revision.
UNRELEASED_EDGES = 1
