"""Thin structured-output client.

Deliberately small: one call shape, strict JSON schema, explicit model.
Extractors get the model injected rather than reaching for a default, so a
test can pass a recorded stub and `make eval` can pin a version.

Concurrency lives here rather than in the extractors because the calls are
the only slow part and they are completely independent — each is a pure
function of (prompt, page text), with no shared state and no ordering
requirement. Nothing about running them together changes what any one of
them returns, so this is a latency change and not a quality one.
"""

from __future__ import annotations

import base64
import json
import random
import threading
import time
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


class StructuredClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        images: Sequence[Path | str] = (),
    ) -> dict: ...


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


class OpenAIClient:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 180.0,
        max_workers: int = DEFAULT_WORKERS,
    ):
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.timeout = timeout
        self.max_workers = max_workers
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
            timeout=timeout,
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
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _user_content(user, images)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "strict": True, "schema": schema},
            },
        }

        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._http.post(
                    f"{self.base_url}/chat/completions",
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

            body = response.json()
            used = body.get("usage") or {}
            with self._usage_lock:
                self.usage.prompt_tokens += used.get("prompt_tokens", 0)
                self.usage.completion_tokens += used.get("completion_tokens", 0)

            content = body["choices"][0]["message"].get("content")
            if not content:
                # A refusal or a length stop returns no content. Treated as an
                # empty extraction rather than a crash, so one bad page cannot
                # abort a 269-page run.
                return {}
            return json.loads(content)

        raise RuntimeError(
            f"{self.model} failed after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> None:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                return
        # Jittered, so eight workers backing off together do not synchronise
        # into a thundering retry.
        time.sleep(min(2**attempt, 16) * (0.5 + random.random()))


def _user_content(user: str, images: Sequence[Path | str]) -> str | list[dict]:
    """A plain string without images, multipart content with them.

    Text-only callers and their stubs then see exactly the request they saw
    before. A named image that is not on disk raises rather than degrading
    to a text-only call, which would run the vision extractor blind.
    """
    if not images:
        return user

    content: list[dict] = [{"type": "text", "text": user}]
    for image in images:
        encoded = base64.b64encode(Path(image).read_bytes()).decode()
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
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
    max_workers: int | None = None,
) -> list[dict]:
    """Run many completions concurrently, results in input order.

    One failure does not kill the batch — it comes back as `{"_error": ...}`
    in its slot, so a single bad page costs that page and nothing else.

    `images` is one image set per user, or None. Omitted rather than passed
    empty when there are none, so text-only stubs that take no `images`
    keyword keep working.
    """
    if not users:
        return []

    workers = max_workers or getattr(client, "max_workers", DEFAULT_WORKERS)
    results: list[dict] = [{} for _ in users]

    with ThreadPoolExecutor(max_workers=min(workers, len(users))) as pool:
        futures = {
            pool.submit(
                client.complete,
                system=system,
                user=user,
                schema=schema,
                **({"images": images[i]} if images else {}),
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
