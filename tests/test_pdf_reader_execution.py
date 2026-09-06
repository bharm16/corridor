"""The PDFium execution contract: one thread at a time in a process, one process per call (#729).

The acceptance rule is that concurrent in-process entry fails. It is proved
with a thread that holds PDFium while another calls the guarded reader, so
the refusal does not depend on two reads happening to overlap. The executor
is proved the other way round: two threads read at once through it and both
succeed, because each has its own process.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from corridor_pdf_reader import execution
from corridor_pdf_reader.execution import (
    ExecutionLimits,
    PdfiumConcurrencyError,
    PdfiumExecutionError,
    PdfiumExecutionTimeout,
    PdfiumExecutor,
    pdfium_entered,
    pdfium_entry,
    read_document,
)

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "corridor_pdf_reader" / "corpus" / "fixtures"
FIXTURE = FIXTURES / "fixture-rotation-0.pdf"


# Functions the executor runs must be importable by the spawned child, so
# they live at module level.
def _child_pid() -> int:
    return os.getpid()


def _sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def _raise_value_error() -> None:
    raise ValueError("deliberate failure inside the child")


def _allocate(size: int) -> int:
    block = bytearray(size)
    return len(block)


def _entered_in_child() -> bool:
    return pdfium_entered()


class _Holder:
    """A thread that enters PDFium and stays there until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with pdfium_entry():
            self.entered.set()
            self.release.wait(10)

    def __enter__(self) -> "_Holder":
        self.thread.start()
        assert self.entered.wait(10)
        return self

    def __exit__(self, *_: object) -> None:
        self.release.set()
        self.thread.join(10)


def test_a_second_thread_is_refused_while_another_holds_pdfium():
    with _Holder():
        assert pdfium_entered()
        with pytest.raises(PdfiumConcurrencyError):
            with pdfium_entry():
                pass
    assert not pdfium_entered()
    with pdfium_entry():
        assert pdfium_entered()


def test_the_guarded_reader_refuses_a_concurrent_in_process_call():
    """Two threads entering the reader at once: the second gets a refusal, not a queue."""

    with _Holder():
        started = time.perf_counter()
        with pytest.raises(PdfiumConcurrencyError):
            read_document(FIXTURE, [1], dpi=72)
        assert time.perf_counter() - started < 1.0
    page = read_document(FIXTURE, [1], dpi=72)["pages"][0]
    assert "UTILITY 1149+00" in page["text"]["value"]


def test_the_same_thread_may_nest_without_deadlock():
    with pdfium_entry():
        with pdfium_entry():
            assert pdfium_entered()
        assert pdfium_entered()
    assert not pdfium_entered()


def test_a_failure_inside_the_guard_releases_it():
    with pytest.raises(ValueError):
        with pdfium_entry():
            raise ValueError("inside")
    assert not pdfium_entered()


def test_the_executor_runs_the_call_in_another_process_under_the_guard():
    executor = PdfiumExecutor()

    assert executor.run(_child_pid) != os.getpid()
    assert executor.run(_entered_in_child) is True
    assert not pdfium_entered()
    assert multiprocessing.active_children() == []


def test_the_executor_reads_a_document_in_its_own_process():
    result = PdfiumExecutor().read_document(FIXTURE, dpi=72)

    assert result["engine"] == "tagged"
    assert [page["number"] for page in result["pages"]] == [1]
    assert "UTILITY 1149+00" in result["pages"][0]["text"]["value"]
    assert multiprocessing.active_children() == []


def test_two_threads_may_read_at_once_through_the_executor():
    executor = PdfiumExecutor()
    results: dict[str, object] = {}

    def read(name: str) -> None:
        try:
            results[name] = executor.read_document(FIXTURE, [1], dpi=72)["pages"][0]["text"]["value"]
        except BaseException as exc:  # a failure must be visible, not swallowed
            results[name] = exc

    threads = [threading.Thread(target=read, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert set(results) == {"a", "b"}
    assert all("UTILITY 1149+00" in str(value) for value in results.values()), results
    assert multiprocessing.active_children() == []


def test_a_call_past_its_wall_time_is_killed_and_reported():
    executor = PdfiumExecutor(ExecutionLimits(wall_seconds=1.0))
    started = time.perf_counter()

    with pytest.raises(PdfiumExecutionTimeout):
        executor.run(_sleep_for, 60.0)

    assert time.perf_counter() - started < 20.0
    assert multiprocessing.active_children() == []


def test_a_failure_in_the_child_is_reported_with_its_type():
    with pytest.raises(PdfiumExecutionError) as caught:
        PdfiumExecutor().run(_raise_value_error)

    assert caught.value.kind == "ValueError"
    assert "deliberate failure" in caught.value.detail
    assert "_raise_value_error" in caught.value.trace

    with pytest.raises(PdfiumExecutionError) as rejected:
        PdfiumExecutor().read_document(FIXTURES / "fixture-malformed.pdf")
    assert rejected.value.kind == "ValueError"
    assert "open/password" in rejected.value.detail
    assert multiprocessing.active_children() == []


def test_the_memory_bound_is_enforced_where_the_platform_allows():
    executor = PdfiumExecutor(ExecutionLimits(memory_bytes=256 * 1024**2))
    contract = executor.contract()

    assert contract["memory_bytes"] == 256 * 1024**2
    if not execution.memory_limit_enforceable():
        assert contract["memory_limit_enforced"] is False
        assert contract["platform"] == "darwin"
        pytest.skip("RLIMIT_AS is accepted but not enforced on this platform; recorded in the contract")
    assert contract["memory_limit_enforced"] is True
    with pytest.raises(PdfiumExecutionError) as caught:
        executor.run(_allocate, 1024**3)
    assert caught.value.kind in ("MemoryError", "PdfiumProcessDied")
    assert multiprocessing.active_children() == []


def test_the_contract_names_what_it_enforces():
    contract = PdfiumExecutor().contract()

    assert contract["wall_seconds"] == 600.0
    assert contract["memory_bytes"] == 4 * 1024**3
    assert "one spawned process per call" in contract["isolation"]
    assert "refuses a second thread" in contract["in_process_guard"]
