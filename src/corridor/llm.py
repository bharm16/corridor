"""Thin structured-output client.

Deliberately small: one call shape, strict JSON schema, explicit model.
Extractors get the model injected rather than reaching for a default, so a
test can pass a recorded stub and `make eval` can pin a version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from corridor.config import settings


class StructuredClient(Protocol):
    def complete(self, *, system: str, user: str, schema: dict) -> dict: ...


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
    ):
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.timeout = timeout
        self.usage = Usage()
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Put it in .env (which is gitignored)."
            )

    def complete(self, *, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "strict": True, "schema": schema},
            },
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"{self.model} returned {response.status_code}: {response.text[:300]}"
            )

        body = response.json()
        used = body.get("usage") or {}
        self.usage.prompt_tokens += used.get("prompt_tokens", 0)
        self.usage.completion_tokens += used.get("completion_tokens", 0)

        content = body["choices"][0]["message"].get("content")
        if not content:
            # A refusal or a length stop returns no content. Treated as an
            # empty extraction rather than a crash, so one bad page cannot
            # abort a 269-page run.
            return {}
        return json.loads(content)
