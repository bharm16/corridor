"""Hand the identity and authorization history to whoever is entitled to it (#531).

An export that only exists as a function is not an export.  A pilot customer's
security review, or the customer's own offboarding evidence, is a file someone
has to be able to produce; this adapter is the whole of that.

It reads and prints, and nothing else.  There is no argument that filters by
person or by project, because an access review that could be narrowed at the
command line is one whose omissions cannot be seen.  The only narrowing is
``--after-id``, which resumes an earlier export exactly where it ended, so a
customer receiving these periodically receives every act once.
"""

from __future__ import annotations

import argparse

from corridor.db import WorkerSession
from corridor.identity_audit import export


_CONTRACT = """\
The identity and authorization export (#531): every enrollment, sign-in,
sign-out, designation change, and deprovisioning act, oldest first. Resume a
previous export with the id it ended on; nothing else narrows it.
  make identity-audit ARGS="--format=csv --after-id=9100"
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="identity-audit",
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--format", choices=("json", "csv"), default="json", dest="fmt"
    )
    parser.add_argument(
        "--after-id",
        type=int,
        default=None,
        help="resume after this audit_log id, the watermark a previous export ended on",
    )
    return parser


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    args = _parser().parse_args(argv)
    factory = session_factory or WorkerSession
    with factory() as session:
        print(export(session, fmt=args.fmt, after_id=args.after_id))
    return 0


if __name__ == "__main__":  # pragma: no cover - adapter entry point
    raise SystemExit(main())
