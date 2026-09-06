"""Process-isolated, single-threaded entry into PDFium for every reader call.

PDFium is not safe to call from more than one thread at a time, even on
different documents; pypdfium2 says so and recommends processes for parallel
work. Corridor's extraction runs in threaded workers, so the rule lives here,
in the package, rather than in every caller's memory (#729; #735 and #739
reuse it):

- `pdfium_entry()` is the in-process guard. One thread holds it at a time. A
  second thread is refused at once with `PdfiumConcurrencyError` rather than
  queued, because a queue would hide exactly the mistake this exists to make
  visible. The same thread may nest, since nesting is not concurrency.
- `read_document()` is the guarded in-process reader, for a caller that is
  already alone in its own process, as `bootstrap.read`'s pool workers are.
- `PdfiumExecutor` runs a reader call in a fresh spawned process with a wall
  time bound, an address-space bound where the platform enforces one (Linux;
  macOS accepts RLIMIT_AS and ignores it, and the contract says so), and
  explicit cleanup: the child is joined or killed before the call returns,
  whatever happened inside it. One process per call is the whole isolation
  story; nothing is pooled, so nothing can leak across calls.

The reader itself (`replacement.reader.read_pdf`) is imported unchanged and
knows nothing of this; every caller in Corridor enters through this module.
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corridor_pdf_reader.replacement.reader import read_pdf

MEASURED_ENGINE = "tagged"
# The harness read every pair at 36 dpi (`bootstrap/read.py`); the render
# only contributes a digest to the reader's page, cells come from glyphs.
MEASURED_DPI = 36


class PdfiumConcurrencyError(RuntimeError):
    """A second thread tried to enter PDFium while another held it."""


class PdfiumExecutionError(RuntimeError):
    """The isolated reader call failed; `kind` names the exception in the child."""

    def __init__(self, kind: str, message: str, trace: str = "") -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.detail = message
        self.trace = trace


class PdfiumExecutionTimeout(PdfiumExecutionError):
    """The child exceeded its wall time and was killed."""


class PdfiumProcessDied(PdfiumExecutionError):
    """The child ended without reporting a result (a crash, or the kernel's memory limit)."""


_state_lock = threading.Lock()
_owner: int | None = None
_depth = 0


@contextmanager
def pdfium_entry() -> Iterator[None]:
    """Hold PDFium for this thread; refuse any other thread meanwhile."""
    global _owner, _depth
    me = threading.get_ident()
    with _state_lock:
        if _owner is not None and _owner != me:
            raise PdfiumConcurrencyError(
                "PDFium is in use by another thread of this process; run the "
                "call through PdfiumExecutor or wait for the current call"
            )
        _owner = me
        _depth += 1
    try:
        yield
    finally:
        with _state_lock:
            _depth -= 1
            if _depth == 0:
                _owner = None


def pdfium_entered() -> bool:
    """Whether some thread of this process is inside PDFium right now."""
    with _state_lock:
        return _owner is not None


def read_document(
    source: Path | str,
    pages: Sequence[int] | None = None,
    *,
    engine: str = MEASURED_ENGINE,
    dpi: int = MEASURED_DPI,
    retain_images: bool = False,
) -> dict[str, Any]:
    """The reader, guarded, in this process. `pages=None` reads every page."""
    path = Path(source)
    with pdfium_entry():
        if pages is None:
            import pypdfium2 as pdfium

            try:
                document = pdfium.PdfDocument(path)
            except pdfium.PdfiumError as exc:
                raise ValueError(f"PDF open/password failure: {exc}") from exc
            try:
                pages = list(range(1, len(document) + 1))
            finally:
                document.close()
        return read_pdf(
            path, list(pages), engine=engine, dpi=dpi, retain_images=retain_images
        )


@dataclass(frozen=True)
class ExecutionLimits:
    wall_seconds: float = 600.0
    memory_bytes: int | None = 4 * 1024**3


def memory_limit_enforceable() -> bool:
    """Linux enforces RLIMIT_AS; macOS accepts the call and ignores it."""
    return sys.platform.startswith("linux")


def _apply_memory_limit(limits: ExecutionLimits) -> None:
    if limits.memory_bytes is None or not memory_limit_enforceable():
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))


def _child_main(
    connection: Any,
    limits: ExecutionLimits,
    function: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    payload: tuple[Any, ...]
    try:
        _apply_memory_limit(limits)
        with pdfium_entry():
            payload = ("ok", function(*args, **kwargs))
    except BaseException as exc:
        payload = ("error", type(exc).__name__, str(exc), traceback.format_exc())
    try:
        connection.send(payload)
    except Exception as exc:
        connection.send(("error", type(exc).__name__, f"result could not be sent: {exc}", ""))
    finally:
        connection.close()


def _finish(process: Any, grace_seconds: float = 5.0) -> int | None:
    """Join the child, killing it if it lingers; return its exit code."""
    process.join(grace_seconds)
    if process.is_alive():
        process.kill()
        process.join()
    code = process.exitcode
    process.close()
    return code


class PdfiumExecutor:
    """Run reader calls one process at a time each, bounded and cleaned up."""

    def __init__(self, limits: ExecutionLimits = ExecutionLimits()) -> None:
        self.limits = limits

    def contract(self) -> dict[str, Any]:
        """What this executor enforces on this platform, for receipts."""
        return {
            "isolation": "one spawned process per call, joined or killed before return",
            "in_process_guard": "pdfium_entry refuses a second thread at once",
            "wall_seconds": self.limits.wall_seconds,
            "memory_bytes": self.limits.memory_bytes,
            "memory_limit_enforced": self.limits.memory_bytes is not None
            and memory_limit_enforceable(),
            "platform": sys.platform,
        }

    def run(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call a module-level function in a fresh process and return its result."""
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_child_main,
            args=(sender, self.limits, function, args, kwargs),
            name="pdfium-reader",
            daemon=True,
        )
        process.start()
        sender.close()
        finished = False
        try:
            if not receiver.poll(self.limits.wall_seconds):
                process.kill()
                raise PdfiumExecutionTimeout(
                    "PdfiumExecutionTimeout",
                    f"reader call exceeded {self.limits.wall_seconds} s and was killed",
                )
            try:
                outcome = receiver.recv()
            except EOFError:
                code = _finish(process)
                finished = True
                raise PdfiumProcessDied(
                    "PdfiumProcessDied",
                    f"reader process ended without a result (exit code {code})",
                ) from None
        finally:
            receiver.close()
            if not finished:
                _finish(process)
        if outcome[0] == "ok":
            return outcome[1]
        raise PdfiumExecutionError(outcome[1], outcome[2], outcome[3])

    def read_document(
        self,
        source: Path | str,
        pages: Sequence[int] | None = None,
        *,
        engine: str = MEASURED_ENGINE,
        dpi: int = MEASURED_DPI,
        retain_images: bool = False,
    ) -> dict[str, Any]:
        """`read_document`, in its own process."""
        return self.run(
            read_document,
            Path(source),
            None if pages is None else list(pages),
            engine=engine,
            dpi=dpi,
            retain_images=retain_images,
        )
