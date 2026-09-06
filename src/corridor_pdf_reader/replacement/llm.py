"""Thin structured-output client for the semantics tier.

A port of Corridor's `corridor/llm.py`, cut to what the tier needs: one call
shape on the Responses API, a strict JSON schema, an explicit model, and the
four load-bearing defaults that module measured (reasoning effort `none`,
images at `original` detail, `store` false, `strict` explicit). Concurrency,
usage accounting and the batch helper are left to Corridor's own client,
which this one is replaced by on the port.
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)
MAX_ATTEMPTS = 4
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


class StructuredClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        images: Sequence[Path | str] = (),
    ) -> dict[str, Any]: ...


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file; values never leave this process."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def api_key_from(env_file: Path | None = None) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key and env_file is not None and env_file.exists():
        key = read_env_file(env_file).get("OPENAI_API_KEY", "")
    return key


class OpenAIClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 180.0,
        effort: str = "none",
    ):
        self.model = model
        self.api_key = api_key or api_key_from()
        self.base_url = base_url.rstrip("/")
        if effort not in EFFORTS:
            raise ValueError(f"unknown reasoning effort {effort!r}; expected one of {', '.join(EFFORTS)}")
        self.effort = effort
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        images: Sequence[Path | str] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": _content(user, images)}],
            "text": {"format": {"type": "json_schema", "name": "structure", "strict": True, "schema": schema}},
            "reasoning": {"effort": self.effort},
            "store": False,
        }
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
                _backoff(attempt)
                continue
            if response.status_code in RETRY_STATUSES:
                last_error = f"{response.status_code}: {response.text[:200]}"
                _backoff(attempt, response)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"{self.model} returned {response.status_code}: {response.text[:300]}")
            return self._read(response.json())
        raise RuntimeError(f"{self.model} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def _read(self, body: dict[str, Any]) -> dict[str, Any]:
        used = body.get("usage") or {}
        self.calls += 1
        self.prompt_tokens += used.get("input_tokens", 0)
        self.completion_tokens += used.get("output_tokens", 0)
        self.cached_tokens += (used.get("input_tokens_details") or {}).get("cached_tokens", 0)
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason", "unknown")
            raise RuntimeError(f"{self.model} stopped early: {reason}")
        # A reasoning item can precede the message, so the message is found
        # by type rather than by position.
        message = next((item for item in body.get("output") or [] if item.get("type") == "message"), None)
        if message is None:
            raise RuntimeError(f"{self.model} returned 200 with no message")
        parts = message.get("content") or []
        refusal = next((p for p in parts if p.get("type") == "refusal"), None)
        if refusal is not None:
            raise RuntimeError(f"{self.model} refused: {refusal.get('refusal')}")
        text_part = next((p for p in parts if p.get("type") == "output_text"), None)
        if text_part is None or not str(text_part.get("text") or "").strip():
            raise RuntimeError(f"{self.model} returned 200 with no output text")
        result: dict[str, Any] = json.loads(text_part["text"])
        return result


def _backoff(attempt: int, response: httpx.Response | None = None) -> None:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            time.sleep(min(int(retry_after), 30))
            return
    time.sleep(min(2**attempt, 16) * (0.5 + random.random()))


def _content(user: str, images: Sequence[Path | str]) -> list[dict[str, Any]]:
    """The text first, images last, so the shared prefix stays cacheable."""
    content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
    for image in images:
        encoded = base64.b64encode(Path(image).read_bytes()).decode()
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{encoded}", "detail": "original"})
    return content
