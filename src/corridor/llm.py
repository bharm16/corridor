"""Thin structured-output client.

Deliberately small: one call shape, strict JSON schema, explicit model.
Extractors get the model injected rather than reaching for a default, so a
test can pass a recorded stub and `make eval` can pin a version.

The wire shape is the **Responses API**. The provider documents it as the
surface reasoning models belong on, and every GPT-5.6 control this pipeline
needs is reachable only there: reasoning effort, image detail, prompt
caching, and output logprobs. `StructuredClient` is unchanged by that
migration, so every stub and every text-only extractor is untouched.

Four defaults here are load-bearing, and each was measured rather than
assumed (spike against `gpt-5.6-luna`, 2026-08-03):

- **`reasoning.effort` is pinned to `none`.** Omitting it silently selects
  `medium`, which spent 333 reasoning tokens and 466 output tokens on a
  question that `none` answered in 130 with 0. Reasoning bills as output.
  Transcription and structure-mapping are perception, not deliberation.
- **Images are sent at `original` detail.** `original` and `auto` both cost
  5,085 input tokens on a 2550x1650 page; `high` costs 3,069 because it
  downscales. Pinning it means digit fidelity never rests on what a default
  happens to resolve to.
- **`store` is false.** Responses are retained server-side for 30 days
  otherwise, which a stateless extraction pipeline has no use for.
- **`strict` is always explicit.** Omitted, the API attempts strict mode and
  silently falls back to non-strict on an incompatible schema. Believing you
  have schema guarantees while running without them is how malformed rows
  reach a reviewer.

Concurrency lives here rather than in the extractors because the calls are
the only slow part and they are completely independent — each is a pure
function of (prompt, page), with no shared state and no ordering
requirement. Nothing about running them together changes what any one of
them returns, so this is a latency change and not a quality one.
"""

from __future__ import annotations

import base64
import json
import random
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from corridor.config import settings

# Enough to transform a 30-minute run into a few minutes; low enough not to
# trip rate limits on a single key. Raise only with evidence.
DEFAULT_WORKERS = 8

# Transient. A page lost to one of these is a page silently not extracted,
# which matters more at concurrency than it did sequentially.
RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4

# `gpt-5.6-luna` accepts these and rejects `minimal` with a 400 naming the
# set. Verified directly rather than inferred from the family docs, which
# warn that each model supports only a subset.
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

# Flex is billed at batch rates and queues behind standard traffic; the
# provider's own examples raise the client timeout to match.
FLEX_TIMEOUT = 900.0


class StructuredClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        images: Sequence[Path | str] = (),
        logprobs: bool = False,
    ) -> dict: ...


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Reasoning bills as output and is invisible without this; cached input
    # bills at a tenth. Neither is separable from the totals after the fact.
    reasoning_tokens: int = 0
    cached_tokens: int = 0


class OpenAIClient:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 180.0,
        max_workers: int = DEFAULT_WORKERS,
        effort: str = "none",
        flex: bool = False,
    ):
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.max_workers = max_workers
        self.flex = flex
        self.timeout = FLEX_TIMEOUT if flex else timeout
        if effort not in EFFORTS:
            raise ValueError(
                f"unknown reasoning effort {effort!r}; expected one of "
                f"{', '.join(EFFORTS)}"
            )
        self.effort = effort
        # One key per client, so a run's requests share a cached prefix.
        # Deliberately not sharded across workers: the provider suggests
        # roughly 15 requests a minute per key, and eight workers will
        # exceed that — but the shared prefix is a few thousand tokens
        # against a per-page image, so a miss costs a fraction of a cent
        # and sharding would buy nothing worth the machinery.
        self.cache_key = f"corridor-{uuid.uuid4().hex[:12]}"
        self.usage = Usage()
        self._usage_lock = threading.Lock()
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Put it in .env (which is gitignored)."
            )
        # One client for the process, so requests reuse connections instead of
        # paying a fresh TCP and TLS handshake per page. httpx.Client is
        # thread-safe; the pool is sized to the worker count.
        self._http = httpx.Client(
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=max_workers * 2,
                max_keepalive_connections=max_workers,
            ),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        images: Sequence[Path | str] = (),
        logprobs: bool = False,
    ) -> dict:
        payload = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": _content(user, images)}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "extraction",
                    "strict": True,
                    "schema": schema,
                }
            },
            "reasoning": {"effort": self.effort},
            "store": False,
            "prompt_cache_key": self.cache_key,
        }
        if logprobs:
            payload["include"] = ["message.output_text.logprobs"]
        if self.flex:
            payload["service_tier"] = "flex"

        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._http.post(
                    f"{self.base_url}/responses",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                last_error = f"transport: {exc}"
                self._backoff(attempt)
                continue

            if response.status_code in RETRY_STATUSES:
                last_error = f"{response.status_code}: {response.text[:200]}"
                self._backoff(attempt, response)
                continue
            if response.status_code != 200:
                raise RuntimeError(
                    f"{self.model} returned {response.status_code}: "
                    f"{response.text[:300]}"
                )

            return self._read(response.json())

        raise RuntimeError(
            f"{self.model} failed after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _read(self, body: dict) -> dict:
        used = body.get("usage") or {}
        with self._usage_lock:
            self.usage.prompt_tokens += used.get("input_tokens", 0)
            self.usage.completion_tokens += used.get("output_tokens", 0)
            self.usage.cached_tokens += (
                used.get("input_tokens_details") or {}
            ).get("cached_tokens", 0)
            self.usage.reasoning_tokens += (
                used.get("output_tokens_details") or {}
            ).get("reasoning_tokens", 0)

        # Truncation must never look like a short page: the JSON would be
        # cut mid-row, and half a matrix silently accepted is worse than a
        # page that failed loudly.
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason", "unknown")
            raise RuntimeError(f"{self.model} stopped early: {reason}")

        # A reasoning item can precede the message, so the message is found
        # by type. Indexing output[0] is the classic port-from-chat bug.
        message = next(
            (item for item in body.get("output") or [] if item.get("type") == "message"),
            None,
        )
        if message is None:
            # A length stop or a filtered response returns no message.
            # Treated as an empty extraction rather than a crash, so one bad
            # page cannot abort a 269-page run.
            return {}

        parts = message.get("content") or []
        refusal = next((p for p in parts if p.get("type") == "refusal"), None)
        if refusal is not None:
            raise RuntimeError(f"{self.model} refused: {refusal.get('refusal')}")

        text_part = next((p for p in parts if p.get("type") == "output_text"), None)
        if text_part is None or not text_part.get("text"):
            return {}

        result = json.loads(text_part["text"])
        if text_part.get("logprobs"):
            # Reserved key, following `_error` in `complete_many`: metadata
            # about the call travels beside the extraction rather than
            # forcing a second return channel through every caller.
            result["_meta"] = {"logprobs": text_part["logprobs"]}
        return result

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                return
        # Jittered, so eight workers backing off together do not synchronise
        # into a thundering retry.
        time.sleep(min(2**attempt, 16) * (0.5 + random.random()))


def _content(user: str, images: Sequence[Path | str]) -> list[dict]:
    """The text part first, images last.

    Prompt caching matches on a shared leading prefix, and the image is the
    only part that differs between pages — putting it last keeps everything
    before it cacheable. A named image that is not on disk raises rather
    than degrading to a text-only call, which would run the vision
    extractor blind.
    """
    content: list[dict] = [{"type": "input_text", "text": user}]
    for image in images:
        encoded = base64.b64encode(Path(image).read_bytes()).decode()
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{encoded}",
                "detail": "original",
            }
        )
    return content


def complete_many(
    client: StructuredClient,
    *,
    system: str,
    schema: dict,
    users: list[str],
    images: list[Sequence[Path | str]] | None = None,
    logprobs: bool = False,
    max_workers: int | None = None,
) -> list[dict]:
    """Run many completions concurrently, results in input order.

    One failure does not kill the batch — it comes back as `{"_error": ...}`
    in its slot, so a single bad page costs that page and nothing else.

    `images` is one image set per user, or None. Omitted rather than passed
    empty when there are none, so text-only stubs that take no `images`
    keyword keep working; `logprobs` is passed the same way.
    """
    if not users:
        return []

    workers = max_workers or getattr(client, "max_workers", DEFAULT_WORKERS)
    results: list[dict] = [{} for _ in users]
    extra = {"logprobs": True} if logprobs else {}

    with ThreadPoolExecutor(max_workers=min(workers, len(users))) as pool:
        futures = {
            pool.submit(
                client.complete,
                system=system,
                user=user,
                schema=schema,
                **({"images": images[i]} if images else {}),
                **extra,
            ): i
            for i, user in enumerate(users)
        }
        for future in futures:
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — one page, not the run
                results[index] = {"_error": str(exc)}

    return results
