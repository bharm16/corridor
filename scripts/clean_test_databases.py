"""Drop the scratch PostgreSQL databases a development machine accumulates.

Every isolated-database test, every hand-made verification copy, and every
abandoned parallel-run template leaves a database behind on the one local
PostgreSQL.  One cohort of four lanes is enough to add several; a session that
ends badly leaves the rest.  Nothing collected them, so 74 of them — 837 MB —
were swept by hand on 2026-09-03, which is exactly the periodic archaeology
this exists to prevent.

The rules are deliberately narrow, because the alternative to a cautious sweep
is no sweep at all:

- the configured development database is never a candidate, whatever it is
  named;
- `postgres` and the PostgreSQL templates are never candidates;
- a database with an open backend is never a candidate — something is using it;
- a name is a candidate only when it matches a known scratch pattern, so an
  unrecognised database is kept rather than guessed at.

The first and now authoritative rule is
`corridor.m8_acceptance_database.is_disposable_database_name`: the module that
mints a disposable database exports the predicate for recognising one, so a new
workflow's databases are swept the day it is written. The hand-written patterns
below stay because names produced *before* that namespace existed are still on
disk, and because a hand-made verification copy has no minting module at all.
They are history, not the place to add a new harness.

Age is deliberately *not* a rule.  A database directory's modification time
tracks the last checkpoint that touched it, not when it was created, so on a
running server every database looks recently modified and the signal says
nothing.

Usage:
    uv run python scripts/clean_test_databases.py            # dry run
    uv run python scripts/clean_test_databases.py --apply
    uv run python scripts/clean_test_databases.py --keep corridor_pre_baseline_95da88f
"""

from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url


# A name is swept only when it matches one of these.  Each is a prefix a test
# harness or a documented manual verification actually produced; the trailing
# `.+` keeps a bare prefix from matching a database someone meant to keep.
#
# Everything named `corridor_disposable_<label>_<pid>_<hex>` is recognised by
# the minting module's own predicate instead; nothing new belongs in this list.
SCRATCH_PATTERNS = (
    r"corridor_pytest_.+",
    r"corridor_due_work_test_.+",
    r"corridor_m8_acceptance_.+",
    r"corridor_event_admission_race_.+",
    r"corridor_experimental_(cli|guard)_.+",
    r"corridor_proving_restore_._.+",
    r"corridor_issue\d+.*",
    r"corridor_pr\d+_.+",
    r"corridor_tdd_.+",
    r"corridor_lane\d*.*",
    r"corridor_baseline_\d+",
    r"corridor_consol_.+",
    r"corridor_\d{3}_.+",
    r"corridor_a\d+_validation_.+",
    r"corridor_\d+_\d+_verify",
    r"corridor_ei_v\d+_.+",
    r"corridor_integration_.+",
    r"corridor_root\d+_.+",
    r"corridor_verify\d+",
    r"corridor_timing",
    r"a\d{3}_statement_.+",
    r"a\d{3}_historical_.+",
    r"issue\d+_.+",
)

_SCRATCH = re.compile(rf"^(?:{'|'.join(SCRATCH_PATTERNS)})$")

NEVER_SWEEP = frozenset({"postgres", "template0", "template1"})


def is_scratch_name(name: str) -> bool:
    """Whether a database name is one a harness or verification copy produced.

    The minted namespace is asked first, so a workflow that adds a label is
    swept without editing this file.
    """

    from corridor.m8_acceptance_database import is_disposable_database_name

    return is_disposable_database_name(name) or bool(_SCRATCH.fullmatch(name))


def sweepable(
    candidates: dict[str, int], *, protected: frozenset[str]
) -> list[str]:
    """The databases to drop, from `{name: open backend count}`.

    Kept deliberately pure so the rules can be tested without a server: the
    protection that matters is which names are excluded, not how they were
    listed.
    """

    return sorted(
        name
        for name, backends in candidates.items()
        if name not in protected
        and name not in NEVER_SWEEP
        and backends == 0
        and is_scratch_name(name)
    )


def _candidates(engine) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select d.datname, "
                "  (select count(*) from pg_stat_activity a "
                "     where a.datname = d.datname) "
                "from pg_database d where not d.datistemplate"
            )
        ).all()
    return {str(name): int(backends) for name, backends in rows}


_CONTRACT = """\
Dry run by default; --apply drops. Never touches the configured development
database, a database with an open connection, or a name it does not recognise.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="drop the databases; without it nothing is changed",
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="NAME",
        help="protect one more database by name; repeatable",
    )
    arguments = parser.parse_args(argv)

    from corridor.config import settings

    configured: URL = make_url(settings.database_url)
    protected = frozenset({configured.database or "corridor", *arguments.keep})
    admin = create_engine(
        configured.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        candidates = _candidates(admin)
        doomed = sweepable(candidates, protected=protected)
        kept = sorted(set(candidates) - set(doomed) - NEVER_SWEEP)

        for name in kept:
            reason = (
                "protected"
                if name in protected
                else "in use"
                if candidates[name]
                else "unrecognised name"
            )
            print(f"keep  {name}  ({reason})")
        for name in doomed:
            if not arguments.apply:
                print(f"would drop  {name}")
                continue
            with admin.connect() as connection:
                connection.execute(
                    text(
                        "select pg_terminate_backend(pid) from pg_stat_activity"
                        " where datname = :name and pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                connection.execute(text(f'drop database if exists "{name}"'))
            print(f"dropped  {name}")
        verb = "dropped" if arguments.apply else "would drop"
        print(f"\n{verb} {len(doomed)}, kept {len(kept)}")
        if not arguments.apply and doomed:
            print("nothing was changed; re-run with --apply")
    finally:
        admin.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
