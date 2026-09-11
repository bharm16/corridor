"""Inspect or replay a configured email reading against a retained delivery.

The processing pipeline runs the configured provider. This operator entry point
also accepts a retained strict response, so a fixture or failed run can be
reproduced without another provider call and without changing the MIME bytes.
"""

import argparse
import json
from pathlib import Path

from corridor.db import WorkerSession
from corridor.llm import RequestConfiguration
from corridor.email_spine import capture_email_thread, envelope_for_delivery, inspect_email_thread


class RetainedEmailResponse:
    """An explicitly identified response adapter for offline replay.

    Its configuration is a statement about the retained answer, not an
    impersonation of a provider: the endpoint says it came off disk.
    """

    CONFIGURATION = RequestConfiguration(
        model="retained-response", base_url="retained://offline"
    )

    def __init__(self, response):
        self.response = response

    def configuration(self):
        return self.CONFIGURATION

    @property
    def model(self):
        return self.CONFIGURATION.model

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        return self.response


_CONTRACT = """\
Inspect retained project-bound MIME or replay a strict response without a model call.
  make email-source ARGS="inspect <delivery-id>"
  make email-source ARGS="capture <delivery-id> --response response.json"
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("inspect", "capture"))
    parser.add_argument("delivery_id", type=int)
    parser.add_argument("--response", type=Path, help="strict response JSON from inspect's segment IDs")
    args = parser.parse_args(argv)
    if args.command == "capture" and args.response is None:
        parser.error("capture requires --response; normal configured model processing uses make extract")
    with WorkerSession() as session, session.begin():
        envelope = envelope_for_delivery(session, args.delivery_id)
        if args.command == "inspect":
            result = inspect_email_thread(session, envelope)
        else:
            row = capture_email_thread(session, envelope,
                client=RetainedEmailResponse(json.loads(args.response.read_text())))
            result = {"reading_id": row.id, "thread_id": row.thread_id,
                      "resolution": row.resolution, "source_fact_id": row.source_fact_id,
                      "proposed_delta_id": row.proposed_delta_id, "open_question": row.open_question}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
