"""Import/read project contacts and submit an authenticated onboarding correction.

Imports run under the worker capability. Corrections go through the existing
signed-in web session and CSRF boundary; a CLI actor string is not authentication.
"""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

from corridor.db import WorkerSession
from corridor.project_contacts import contact_history, import_adopted_contacts, import_contact_csv, resolve_contact
from corridor.source_delivery import envelope_for_delivery


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import-csv", "import-ucm"):
        command = commands.add_parser(name)
        command.add_argument("identity_id", type=int, help="delivery ID for CSV, project ID for adopted UCM")
        command.add_argument("--identity", required=True)
        command.add_argument("--source-family")
    read = commands.add_parser("read")
    read.add_argument("project_id", type=int)
    read.add_argument("--as-of", type=datetime.fromisoformat)
    read.add_argument("--organization")
    read.add_argument("--role")
    correct = commands.add_parser("correct")
    correct.add_argument("project_slug")
    correct.add_argument("contact_id", type=int)
    correct.add_argument("--record", type=Path, required=True)
    correct.add_argument("--reason", required=True)
    correct.add_argument("--identity", required=True)
    correct.add_argument("--url", default="http://localhost:8412")
    args = parser.parse_args(argv)
    if args.command == "correct":
        token = os.environ.get("CORRIDOR_SESSION_TOKEN")
        csrf = os.environ.get("CORRIDOR_CSRF_TOKEN")
        if not token or not csrf:
            parser.error("correct requires an existing signed-in CORRIDOR_SESSION_TOKEN and CORRIDOR_CSRF_TOKEN")
        body = {"contact": json.loads(args.record.read_text()), "reason": args.reason, "idempotency_key": args.identity}
        url = f"{args.url.rstrip('/')}/projects/{quote(args.project_slug, safe='')}/contacts/{args.contact_id}/correct"
        request = Request(url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Cookie": f"corridor_session={token}", "x-csrf-token": csrf})
        with build_opener(_NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    else:
        with WorkerSession() as session, session.begin():
            if args.command == "import-csv":
                row = import_contact_csv(session, envelope_for_delivery(session, args.identity_id),
                    import_identity=args.identity, source_family=args.source_family)
                result = {"import_id": row.id, "accounting": row.accounting_json}
            elif args.command == "import-ucm":
                row = import_adopted_contacts(session, project_id=args.identity_id, import_identity=args.identity)
                result = {"import_id": row.id, "accounting": row.accounting_json}
            elif args.organization and args.role and args.as_of:
                result = asdict(resolve_contact(session, project_id=args.project_id,
                    organization_ref=args.organization, responsible_role=args.role, as_of=args.as_of))
            else:
                result = [{"id": row.id, "record": row.values_json, "import_id": row.import_id,
                           "corrects_id": row.corrects_id, "corrected_by": row.corrected_by,
                           "recorded_at": row.recorded_at.isoformat(), "source_locators": row.source_locators}
                          for row in contact_history(session, project_id=args.project_id)]
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
