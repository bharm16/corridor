"""Structured operational logs and the identifiers that join them (#491A).

What the application had was two `logging.getLogger` calls, no handler, and no
shared shape, so an operator answering "why is processing failing today" had to
read the database instead of the logs. A logging library was rejected: the
standard library already owns records, levels, and handlers; what was missing
is one machine-parseable line format and one place to hold the identifier that
ties every line from a single web request or Due Work attempt together.

The identifier lives in a `ContextVar` rather than being threaded through call
signatures, because the lines that most need it are emitted deep inside handler
code that has no reason to know about telemetry. A handler-level filter reads
it at emit time, so a line logged by any `corridor.*` logger inside the scope
carries it without that module importing anything.

This module formats and correlates; it measures nothing. Health and operational
signals are `corridor.operational_health`. The versioned product-analytics
event and receipt contract is `corridor.analytics` and is a different concern —
this module only gives whatever that emitter logs its shared line shape.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from corridor.config import settings


ROLE_WEB = "web"
ROLE_WORKER = "worker"

# Every application logger hangs off this one, so one handler formats them all.
LOGGER_NAME = "corridor"

# Record attributes this module owns. `extra=` refuses to overwrite a standard
# LogRecord attribute, so these names are deliberately unlike any of them.
_FIELDS = "corridor_fields"
_CORRELATION = "corridor_correlation"

# The line's own keys. A caller field or correlation field may not replace one,
# so an attacker-supplied value cannot restate the role or the event name.
_RESERVED = frozenset(
    {"timestamp", "level", "logger", "role", "environment", "event", "error"}
)

_correlation: ContextVar[Mapping[str, Any]] = ContextVar(
    "corridor_correlation_fields", default={}
)

_REQUEST_LOG = logging.getLogger(f"{LOGGER_NAME}.request")

# The library rule: a record from a process that configured nothing is
# swallowed rather than dumped raw by `logging.lastResort`, which would put an
# unstructured line on the standard error of a command whose contract is its
# standard output.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


class StderrStream:
    """Write to whatever ``sys.stderr`` is now, not what it was at startup.

    A handler that captured the stream object outlives the redirection it was
    built under, and then writes into a closed buffer long after the process
    that redirected it moved on.
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


class StructuredFormatter(logging.Formatter):
    """One JSON object per record: the line's own keys, then correlation, then fields."""

    def __init__(self, *, role: str, environment: str) -> None:
        super().__init__()
        self.role = role
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "role": self.role,
            "environment": self.environment,
            "event": record.getMessage(),
        }
        for source in (
            getattr(record, _CORRELATION, {}),
            getattr(record, _FIELDS, {}),
        ):
            for key, value in source.items():
                if key not in _RESERVED:
                    line[key] = value
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class CorrelationFilter(logging.Filter):
    """Attach the emitting context's identifiers to the record.

    On the handler rather than the logger: a filter on ``corridor`` never runs
    for a record logged through ``corridor.due_work``, because ancestor loggers
    contribute handlers to a record, not filters.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        setattr(record, _CORRELATION, dict(_correlation.get()))
        return True


def configure_logging(
    *,
    role: str,
    environment: str | None = None,
    stream: Any = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Install this process's one structured handler, replacing any earlier one."""

    log = logging.getLogger(LOGGER_NAME)
    for handler in [
        item for item in log.handlers if getattr(item, "corridor_telemetry", False)
    ]:
        log.removeHandler(handler)
    handler = logging.StreamHandler(StderrStream() if stream is None else stream)
    handler.setFormatter(
        StructuredFormatter(
            role=role,
            environment=environment
            if environment is not None
            else settings.environment,
        )
    )
    handler.addFilter(CorrelationFilter())
    handler.corridor_telemetry = True
    log.addHandler(handler)
    log.setLevel(level)
    # The structured stream is the application's log output; a second copy on
    # the root handler would be the same event in two shapes.
    log.propagate = False
    return log


def new_correlation_id() -> str:
    """One identifier for one request, for use where nothing durable names it."""

    return uuid4().hex


def current_correlation() -> dict[str, Any]:
    """The identifiers every line emitted right here will carry."""

    return dict(_correlation.get())


@contextmanager
def correlation_scope(**fields: Any) -> Iterator[dict[str, Any]]:
    """Bind identifiers onto every line logged inside this scope, then unbind them."""

    merged = dict(_correlation.get())
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _correlation.set(merged)
    try:
        yield merged
    finally:
        _correlation.reset(token)


def log_event(
    log: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any
) -> None:
    """Log one named operational event with typed fields rather than a sentence."""

    log.log(level, event, extra={_FIELDS: fields})


class RequestCorrelationMiddleware:
    """Give every HTTP request one identifier and one structured completion line.

    Plain ASGI rather than Starlette's ``BaseHTTPMiddleware``: that class runs
    the application in a second task and buffers the response, which this
    application's streamed file downloads and background work cannot afford.
    Setting the context variable around the downstream call also puts the
    identifier on the synchronous endpoints' own log lines, because the
    threadpool that runs them copies the calling context.

    The route *template* is logged, never the request path: a path carries
    project slugs, and a template is the low-cardinality thing an operator
    groups by.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Generated here rather than read from a request header: a caller
        # supplied identifier is unbounded text an operator would then have to
        # trust in every line it labels.
        request_id = new_correlation_id()
        started = time.monotonic()
        status = 0

        async def send_with_identifier(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        with correlation_scope(
            request_id=request_id, method=scope.get("method", "")
        ):
            try:
                await self.app(scope, receive, send_with_identifier)
            finally:
                route = scope.get("route")
                log_event(
                    _REQUEST_LOG,
                    "http_request",
                    route=getattr(route, "path", "unmatched"),
                    status=status,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                )
