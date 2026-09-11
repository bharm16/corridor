"""Inspect/replay minutes or process the registered minutes of an adopted project."""

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from corridor.db import WorkerSession
from corridor.llm import OpenAIClient, RequestConfiguration
from corridor.minutes_spine import capture_minutes, inspect_minutes
from corridor.models import Document, Project
from corridor.operating_mode import is_adopted_baseline


class RetainedMinutesResponse:
    """A retained strict answer, replayed with the configuration it names.

    The configuration is a statement about the retained file, not a live
    provider: the endpoint says the answer came off disk.
    """

    CONFIGURATION = RequestConfiguration(
        model="retained-minutes-response", base_url="retained://offline"
    )

    def __init__(self, path):
        self.output = json.loads(path.read_text())

    def configuration(self):
        return self.CONFIGURATION

    @property
    def model(self):
        return self.CONFIGURATION.model

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        return self.output


_CONTRACT = """\
LLM extraction over coordination meeting notes. Needs OPENAI_API_KEY.
Bound a run to exact registered notes by repeating --document-id; --redo
appends a fresh attempt without changing the declared Current Production Run:
  make minutes ARGS="sh99-grand-parkway --document-id 1435 --document-id 1438 --redo"

Inspect exact minutes references or replay a fixture/provider response.
  make minutes-source ARGS="inspect <document-id>"
  make minutes-source ARGS="capture <document-id> --response response.json --source-family <meeting>"
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("document_id", type=int)
    capture = commands.add_parser("capture")
    capture.add_argument("document_id", type=int)
    capture.add_argument("--source-family")
    capture.add_argument("--source-revision")
    capture.add_argument("--response", type=Path)
    project_command = commands.add_parser("project")
    project_command.add_argument("slug")
    project_command.add_argument("--document-id", type=int)
    args = parser.parse_args(argv)
    with WorkerSession() as session:
        if args.command == "inspect":
            result = inspect_minutes(session, session.get_one(Document, args.document_id))
        else:
            if args.command == "project":
                project = session.scalar(select(Project).where(Project.slug == args.slug))
                if project is None:
                    parser.error("project does not exist")
                if not is_adopted_baseline(session, project.id):
                    from corridor.extract_minutes_v5 import main as legacy_main
                    return legacy_main([args.slug] + (["--document-id", str(args.document_id)] if args.document_id else []))
                query = select(Document).where(Document.project_id == project.id, Document.doc_type == "minutes")
                if args.document_id:
                    query = query.where(Document.id == args.document_id)
                documents = session.scalars(query.order_by(Document.id)).all()
            else:
                documents = [session.get_one(Document, args.document_id)]
            client = RetainedMinutesResponse(args.response) if getattr(args, "response", None) else OpenAIClient()
            try:
                result = []
                for document in documents:
                    receipt = capture_minutes(session, document, client=client,
                        source_family=getattr(args, "source_family", None), source_revision=getattr(args, "source_revision", None))
                    result.append({"capture_id": receipt.id, "document_id": document.id, "outcomes": receipt.output_json["outcomes"]})
                    session.commit()
            finally:
                if isinstance(client, OpenAIClient):
                    client.close()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
