"""Run one local pytest command and record its actual process completion.

A previous broad suite finished successfully while a separate shell waited
forever for a summary regex that did not recognize pytest's warning count.
This wrapper inherits pytest's output, waits on its process, and replaces a
small JSON receipt as it runs. Timeout and interruption stop only the process
group this invocation created, including its xdist workers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SUITES = ("focused", "test", "slow", "full")
DIAGNOSTIC_REASONS = ("failure-reproduction", "performance-investigation")
DIAGNOSTIC_ENV = "CORRIDOR_LOCAL_BROAD_REASON"


class _Interrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _atomic_receipt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _process_group_empty(process: subprocess.Popen) -> bool:
    """Prove an exited leader has no live group members after a denied signal.

    macOS can report EPERM rather than ESRCH for an already vanished group.
    Leader exit alone is insufficient: a worker may still be running. Inspect
    only process identities and states, never arguments or environment values.
    An unavailable or malformed process snapshot does not establish absence.
    """
    if process.poll() is None:
        return False
    try:
        snapshot = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,stat="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if snapshot.returncode != 0 or not snapshot.stdout.strip():
        return False
    for line in snapshot.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return False
        pid, group, state = fields
        if not pid.isdecimal() or not group.isdecimal() or state[0] not in "RSDTtIZXWU":
            return False
        if int(group) == process.pid and not state.startswith("Z"):
            return False
    return True


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        if not _process_group_empty(process):
            raise


def _stop_group(process: subprocess.Popen, signum: int = signal.SIGTERM) -> None:
    """The child is its own group leader; never signal the caller's group."""
    previous = {
        watched: signal.signal(watched, signal.SIG_IGN)
        for watched in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        _signal_group(process, signum)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit before a stubborn worker. Kill the owned group
        # even then, rather than mistaking leader exit for complete cleanup.
        _signal_group(process, signal.SIGKILL)
        process.wait()
    finally:
        for watched, handler in previous.items():
            signal.signal(watched, handler)


def _stop_and_record(process, receipt, signum=signal.SIGTERM) -> None:
    try:
        _stop_group(process, signum)
    except OSError as error:
        receipt.update(
            cleanup_requested_by=receipt["outcome"], cleanup_complete=False,
            cleanup_errno=error.errno, exit_code=127, outcome="cleanup_failed",
        )
        raise
    else:
        receipt["cleanup_complete"] = True


def run_test_command(
    command: list[str], *, suite: str, receipt_path: Path,
    timeout_seconds: float, heartbeat_seconds: float = 5,
    diagnostic_reason: str | None = None,
    environment: dict[str, str] | None = None,
) -> int:
    """Own one child and publish completion without interpreting its text."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be finite positive seconds")
    if diagnostic_reason is not None and diagnostic_reason not in DIAGNOSTIC_REASONS:
        raise ValueError("unknown broad-test diagnostic reason")
    started = time.monotonic()
    receipt = {
        "schema_version": 1, "suite": suite, "status": "running",
        "pid": None, "runner_pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 0.0, "exit_code": None, "child_exit_code": None,
        "timeout_seconds": timeout_seconds, "outcome": None,
        "diagnostic_reason": diagnostic_reason,
    }
    _atomic_receipt(receipt_path, receipt)
    process = None
    previous_handlers = {}
    interrupted_signal = None

    def interrupt(signum, _frame):
        # Record the request rather than raising during Popen: an exception
        # after fork but before assignment could otherwise lose the child.
        nonlocal interrupted_signal
        interrupted_signal = signum

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, interrupt)
        # No shell or output pipe: exactly this interpreter runs pytest and
        # the terminal receives progress, warnings, failures and the summary.
        child_environment = dict(os.environ if environment is None else environment)
        child_environment.pop(DIAGNOSTIC_ENV, None)
        if diagnostic_reason is not None:
            child_environment[DIAGNOSTIC_ENV] = diagnostic_reason
        process = subprocess.Popen(
            command, start_new_session=True, env=child_environment
        )
        receipt["pid"] = process.pid
        _atomic_receipt(receipt_path, receipt)
        next_heartbeat = time.monotonic() + heartbeat_seconds
        while True:
            if interrupted_signal is not None:
                raise _Interrupted(interrupted_signal)
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                code = process.wait(timeout=min(0.25, remaining))
                if interrupted_signal is not None:
                    raise _Interrupted(interrupted_signal)
                receipt["exit_code"] = code if code >= 0 else 128 - code
                receipt["outcome"] = "exited" if code >= 0 else "signalled"
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= next_heartbeat:
                    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    _atomic_receipt(receipt_path, receipt)
                    next_heartbeat = time.monotonic() + heartbeat_seconds
    except subprocess.TimeoutExpired:
        receipt.update(exit_code=124, outcome="timed_out")
        if process is not None:
            _stop_and_record(process, receipt)
        print(f"Local {suite} tests exceeded {timeout_seconds:g}s; stopped their process group.",
              file=sys.stderr)
    except (_Interrupted, KeyboardInterrupt) as error:
        signum = error.signum if isinstance(error, _Interrupted) else signal.SIGINT
        receipt.update(exit_code=128 + signum, outcome="interrupted")
        # Ignore a repeated terminal interrupt while cleaning up our group.
        for watched in previous_handlers:
            signal.signal(watched, signal.SIG_IGN)
        if process is not None:
            _stop_and_record(process, receipt, signum)
    except OSError:
        receipt.update(exit_code=127, outcome="runner_error")
        if process is not None:
            _stop_and_record(process, receipt)
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        receipt.update(
            status="completed", elapsed_seconds=round(time.monotonic() - started, 3),
            child_exit_code=process.returncode if process is not None else None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        _atomic_receipt(receipt_path, receipt)
    return receipt["exit_code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=SUITES, required=True)
    parser.add_argument("--timeout-seconds", "--timeoutseconds", type=float, default=600)
    parser.add_argument("--diagnostic-reason", choices=DIAGNOSTIC_REASONS)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if (arguments.suite != "focused" and os.environ.get("GITHUB_ACTIONS") != "true"
            and arguments.diagnostic_reason is None):
        print(
            "Broad local testing requires --diagnostic-reason "
            "failure-reproduction or performance-investigation. "
            "Use make test-focused ARGS=\"tests/test_file.py\" during development; "
            "PR CI runs the full required proof.",
            file=sys.stderr,
        )
        return 2
    pytest_args = arguments.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if not any(argument == "-x" or argument.startswith("--maxfail") for argument in pytest_args):
        pytest_args = ["-x", *pytest_args]
    try:
        return run_test_command(
            [sys.executable, "-m", "pytest", *pytest_args], suite=arguments.suite,
            receipt_path=ROOT / "out" / "test-results" / f"{arguments.suite}.json",
            timeout_seconds=arguments.timeout_seconds,
            diagnostic_reason=arguments.diagnostic_reason,
        )
    except (OSError, ValueError) as error:
        print(f"Cannot run local tests: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
