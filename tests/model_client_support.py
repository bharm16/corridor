"""One recording model client for the whole suite.

Some thirty test modules each defined their own `def complete` stub, and no
two signatures agreed: several took no `images`, most took no `logprobs`, a
few took `**kw`. `llm.complete_many` then grew a conditional-keyword hack so
the narrowest of them kept working, which left the test doubles as the real
authority on the shape of the provider seam.

This is the one double. It takes the whole call, records it, and answers from
a script, so a change to `StructuredClient` is made once here rather than
thirty times across the suite.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any

from corridor.llm import RequestConfiguration, Usage

# The configuration a test client states. It is a declared value like any
# other client's, so nothing has to impersonate a live provider to be sealed.
FAKE_CONFIGURATION = RequestConfiguration(model="test-model")


@dataclass(frozen=True)
class ModelCall:
    """One recorded request, exactly as the seam delivered it."""

    system: str
    user: str
    schema: Mapping[str, Any]
    images: tuple[Path | str, ...] = ()
    logprobs: bool = False


Answer = Mapping[str, Any] | BaseException
Script = Answer | Sequence[Answer] | Callable[[ModelCall], Answer]


class FakeModelClient:
    """A scripted `StructuredClient` that records every call it was given.

    `answers` is one answer, a sequence consumed in order, or a callable of
    the `ModelCall`. An exception instance is raised rather than returned,
    because a failure is raised on this seam and is never a payload key.
    """

    def __init__(
        self,
        answers: Script | None = None,
        *,
        configuration: RequestConfiguration = FAKE_CONFIGURATION,
        max_workers: int = 1,
        tokens_per_call: Mapping[str, int] | None = None,
    ) -> None:
        self._answers = {} if answers is None else answers
        self._configuration = configuration
        self._tokens_per_call = dict(tokens_per_call or {})
        self._lock = threading.Lock()
        self.max_workers = max_workers
        self.calls: list[ModelCall] = []
        self.closed = False
        # Only a client that was asked to meter reports usage: a receipt
        # distinguishes "no counters" from "zero tokens".
        if tokens_per_call is not None:
            self.usage = Usage()

    # -- the declared configuration -----------------------------------------

    def configuration(self) -> RequestConfiguration:
        return self._configuration

    @property
    def model(self) -> str:
        return self._configuration.model

    @property
    def effort(self) -> str:
        return self._configuration.effort

    @property
    def flex(self) -> bool:
        return self._configuration.flex

    @property
    def base_url(self) -> str:
        return self._configuration.base_url

    # -- the one call shape --------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        images: Sequence[Path | str] = (),
        logprobs: bool = False,
    ) -> dict[str, Any]:
        call = ModelCall(
            system=system, user=user, schema=schema,
            images=tuple(images), logprobs=logprobs,
        )
        with self._lock:
            self.calls.append(call)
            for name, count in self._tokens_per_call.items():
                setattr(self.usage, name, getattr(self.usage, name) + count)
            answer = self._next(call, len(self.calls) - 1)
        if isinstance(answer, BaseException):
            raise answer
        return dict(answer)

    def _next(self, call: ModelCall, index: int) -> Answer:
        script = self._answers
        if callable(script):
            return script(call)
        if isinstance(script, Sequence) and not isinstance(script, (str, bytes)):
            return script[index] if index < len(script) else {}
        return script

    def close(self) -> None:
        self.closed = True


def answers_for(**by_marker: Mapping[str, Any]) -> Callable[[ModelCall], Mapping[str, Any]]:
    """Answer by the first marker found in the call's user text.

    Several stubs branched on the page text they were shown; this keeps that
    where the test can read it instead of inside a bespoke class.
    """

    def respond(call: ModelCall) -> Mapping[str, Any]:
        for marker, answer in by_marker.items():
            if marker in call.user:
                return answer
        return {}

    return respond


class RecordedAdapter(FakeModelClient):
    """A recorded structured-output adapter; no network, no paid model call.

    The explanation and intake-draft seams read an adapter identity and its
    contract version beside the answer, so a double for them states those too.
    Five test modules had a byte-identical copy of this class.
    """

    adapter = "fake-adapter"
    adapter_contract_version = "fake-adapter-v1"

    def __init__(self, result=None, *, raises=None, **kwargs) -> None:
        super().__init__(raises if raises is not None else ({} if result is None else result), **kwargs)
