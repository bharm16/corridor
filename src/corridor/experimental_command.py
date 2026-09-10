"""One command frame for every experiment that runs against a disposable database.

ADR-0049 and ADR-0079 let an experiment run only against an explicitly named
database that the guard in `experimental_database` has proved is not
production. Five entry points obeyed that rule with five copies of the same
frame: the direct and shadow Statement Review Assistant commands and the shadow
evaluation each added ``--database-url``, opened the guarded session, ran one
library call inside a savepoint, printed a JSON dict and mapped refusals to
exit 1; Extraction Measurement and the candidate-model comparison each carried
a hand-rolled ``--flag=value`` parser and the same receipt path under ``out/``.
Deleting any one command broke nothing, and deleting the frame would have
recreated it five times. Extending `experimental_database` was rejected: the
guard is also used by the disposable-database provisioner, which is not a
command and wants nothing to do with argument parsing or exit codes.

This module owns the parts that were the same: the ``--database-url``
argument and its help sentence, the guarded session with the injectable
``session_factory`` and ``database_guard`` that tests use, the refusal
families that become exit 1 with their own sentence, the JSON result
printing and savepoint of the read-mostly commands, and the sealed receipt
path of the measurement commands. A command is its parser plus one body.
The measurement bodies keep their own transaction structure because
`extract_project` commits per document; the savepoint therefore belongs to
`run_json_command`, not to `run_experiment`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import json
from pathlib import Path
import sys

from sqlalchemy.orm import Session

from corridor.experimental_database import (
    DatabaseGuard,
    ProductionDatabaseRefusal,
    SessionFactory,
    experimental_session,
    require_experimental_database,
)
from corridor.receipts import ArtifactCollision, write_sealed

__all__ = [
    "DATABASE_URL_HELP",
    "REFUSALS",
    "experiment_parser",
    "print_receipt",
    "run_command",
    "run_experiment",
    "run_json_command",
    "write_measurement_artifact",
]

DATABASE_URL_HELP = "explicit non-production PostgreSQL database"

# Every exception a command may end with by printing its sentence and exiting
# 1. Anything else is a defect and keeps its traceback.
REFUSALS: tuple[type[Exception], ...] = (
    ProductionDatabaseRefusal,
    OSError,
    ValueError,
    ArtifactCollision,
)


def experiment_parser(
    *, database_url_required: bool = True, **parser_arguments
) -> argparse.ArgumentParser:
    """An argparse parser that already carries ``--database-url``.

    A measurement command validates the flag with its own sentence after
    parsing, so it passes ``database_url_required=False``.
    """
    parser = argparse.ArgumentParser(**parser_arguments)
    parser.add_argument(
        "--database-url",
        required=database_url_required,
        help=DATABASE_URL_HELP,
    )
    return parser


def run_command(
    parser: argparse.ArgumentParser,
    argv: list[str] | None,
    command: Callable[[argparse.Namespace], int],
) -> int:
    """Parse ``argv`` and run ``command``; a parse failure is argparse's exit code."""
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    return command(args)


def run_experiment(
    database_url: str | None,
    body: Callable[[Session], int],
    *,
    session_factory: SessionFactory | None = None,
    database_guard: DatabaseGuard = require_experimental_database,
    refusals: tuple[type[Exception], ...] = (),
) -> int:
    """Run ``body`` on the guarded experimental Session and return its exit code.

    The guard runs before the body, so a refused database constructs nothing.
    A refusal, from `REFUSALS` or the command's declared ``refusals``, prints
    its sentence to stderr and is exit 1.
    """
    try:
        with experimental_session(
            database_url,
            session_factory=session_factory,
            database_guard=database_guard,
        ) as session:
            return body(session)
    except (*REFUSALS, *refusals) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def run_json_command(
    parser: argparse.ArgumentParser,
    argv: list[str] | None,
    body: Callable[[Session, argparse.Namespace], Mapping[str, object]],
    *,
    session_factory: SessionFactory | None = None,
    database_guard: DatabaseGuard = require_experimental_database,
) -> int:
    """A command whose body runs in one savepoint and answers with a JSON dict.

    The dict is printed after the guarded session has closed, as the commands
    always did; a parse exit such as ``--help`` prints nothing.
    """

    def command(args: argparse.Namespace) -> int:
        output: list[Mapping[str, object]] = []

        def run(session: Session) -> int:
            with session.begin_nested():
                output.append(body(session, args))
            return 0

        status = run_experiment(
            args.database_url,
            run,
            session_factory=session_factory,
            database_guard=database_guard,
        )
        if status == 0:
            print(json.dumps(output[0], indent=2, sort_keys=True))
        return status

    return run_command(parser, argv, command)


def write_measurement_artifact(path: Path, written: Mapping[str, object]) -> bool:
    """Create an immutable artifact, or accept an identical rerun.

    Identity deliberately excludes ``ran_at`` so repeating the exact command
    resolves to the same file. The first timestamp remains evidence; every
    other field must agree or the collision fails closed.
    """
    return write_sealed(path, json.dumps(written, indent=2) + "\n", volatile=("ran_at",))


def print_receipt(
    written: Mapping[str, object],
    *,
    output_dir: Path | str | None,
    filename: str,
    preserved: str,
) -> None:
    """Seal one measurement receipt under ``out/`` and print where it is.

    ``output_dir`` replaces ``out/`` for tests. An identical rerun keeps the
    first receipt and says so with the command's ``preserved`` sentence; a
    divergent one raises `ArtifactCollision` for `run_experiment` to refuse.
    """
    out = Path(output_dir) if output_dir is not None else Path("out")
    out.mkdir(exist_ok=True)
    path = out / filename
    created = write_measurement_artifact(path, written)
    suffix = "" if created else f" ({preserved})"
    print(f"\n{path}{suffix}")
