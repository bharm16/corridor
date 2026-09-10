"""The names of the customer database's roles and logins, written down once.

Write authority is a database boundary rather than a convention (#492), so
these six names are load-bearing in three different ways: two are the logins
the web and worker capabilities connect as, three are the ``SECURITY DEFINER``
command owners that alone may make something effective, and one is the opt-in
legacy development login that ADR-0081 keeps for the frozen surfaces.

Before this module each name was retyped wherever it was needed — the URL
builder, the deployment bootstrap, the boundary smoke check, the shadow CLI's
capability refusal, the archive's retirement command. That is not a style
problem: every one of those sites *compares* a live PostgreSQL answer against
its own copy of a name, so a rename that missed one would leave a check that
silently never matches, and a capability boundary that reports itself intact
while granting nothing. The names live here so those comparisons cannot drift.

This module deliberately imports nothing. Anything that needs a role name —
including a command adapter that must not build an engine — can read it without
pulling in configuration, an engine, or the customer router.

Migrations keep their own copies. A revision is replayed released history and
inlines every name inside SQL text, so it states them itself;
``tests/test_vocabulary_owners.py`` asserts those copies still equal these.
The control plane is a separate database (ADR-0079) and
``corridor.control_plane_schema`` owns its two role names.
"""

from __future__ import annotations

# The two capability logins. Neither may write accepted authority, neither may
# write the frozen legacy accepted tables, and each holds execute on only its
# own command family.
WEB_CAPABILITY_LOGIN = "corridor_web"
WORKER_CAPABILITY_LOGIN = "corridor_worker"

# The command owners. Accepted authority is written only through the
# record-decision role's commands; the runtime appends segments, facts and
# proposals only through the source-append role's commands.
RECORD_DECISION_ROLE = "corridor_fact_decision_writer"
SOURCE_APPEND_ROLE = "corridor_source_append"
STATEMENT_RETIREMENT_ROLE = "corridor_statement_retirement"

# The opt-in login a legacy development deployment points `web_database_url`
# at, which keeps the blanket read the live-pilot boundary revoked (ADR-0081).
LEGACY_DEV_ROLE = "corridor_legacy_dev"

DATABASE_ROLE_NAMES = frozenset({
    WEB_CAPABILITY_LOGIN,
    WORKER_CAPABILITY_LOGIN,
    RECORD_DECISION_ROLE,
    SOURCE_APPEND_ROLE,
    STATEMENT_RETIREMENT_ROLE,
    LEGACY_DEV_ROLE,
})
