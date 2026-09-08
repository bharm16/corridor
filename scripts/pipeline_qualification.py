"""Deliberate schema-owner maintenance adapter, outside the application runtime.

The application CLI module accepts an injected session factory. Only this
explicit make target binds the maintenance credential; web/worker processes
cannot gain it by importing the pipeline runtime.
"""

from corridor.db import Session
from corridor.pipeline_qualification_cli import main


if __name__ == "__main__":
    raise SystemExit(main(session_factory=Session))
